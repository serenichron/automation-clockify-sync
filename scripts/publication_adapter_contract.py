"""Protocol-only publication orchestration with an append-only receipt journal."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Callable, Iterator, Mapping, Protocol

from scripts import clockify_publication_gate as publication_gate


SCHEMA_VERSION = "publication-receipt/v1"
ZERO_DIGEST = "sha256:" + "0" * 64
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


class PublicationAdapterError(RuntimeError):
    """A publication is untrusted, incomplete, or could not be delivered."""


def _canonical(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PublicationAdapterError("publication receipt is not canonical JSON") from exc


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PublicationAdapterError(f"{field} is required")
    return value.strip()


def _digest_text(value: object, field: str) -> str:
    value = _text(value, field)
    if not _DIGEST.fullmatch(value):
        raise PublicationAdapterError(f"{field} must be a SHA-256 digest")
    return value


def _utc(value: datetime, field: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise PublicationAdapterError(f"{field} must include a timezone")
    value = value.astimezone(timezone.utc)
    rendered = value.strftime("%Y-%m-%dT%H:%M:%S")
    if value.microsecond:
        rendered += f".{value.microsecond:06d}"
    return rendered + "Z"


def _timestamp(value: object, field: str) -> datetime:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PublicationAdapterError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None or _utc(parsed, field) != raw:
        raise PublicationAdapterError(f"{field} must be a canonical UTC timestamp")
    return parsed.astimezone(timezone.utc)


def _buckets(value: object, field: str) -> dict[str, Decimal]:
    if not isinstance(value, Mapping) or not value:
        raise PublicationAdapterError(f"{field} must be a non-empty currency mapping")
    result: dict[str, Decimal] = {}
    for currency, amount in value.items():
        if not isinstance(currency, str) or not _CURRENCY.fullmatch(currency):
            raise PublicationAdapterError(f"{field} has an invalid currency")
        try:
            parsed = amount if isinstance(amount, Decimal) else Decimal(str(amount))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise PublicationAdapterError(f"{field} has an invalid amount") from exc
        if not parsed.is_finite() or parsed < 0:
            raise PublicationAdapterError(f"{field} has an invalid amount")
        result[currency] = parsed
    return dict(sorted(result.items()))


@dataclass(frozen=True)
class SharedReportReceipt:
    """Exact verified projection of the refreshed shared Clockify report."""

    contract_digest: str
    authorization_digest: str
    idempotency_identity: str
    report_target: str
    readback_digest: str
    duration_seconds: int
    native_buckets: Mapping[str, Decimal]
    verified_at: datetime
    status: str = "verified"

    def __post_init__(self) -> None:
        _digest_text(self.contract_digest, "contract_digest")
        _digest_text(self.authorization_digest, "authorization_digest")
        _text(self.idempotency_identity, "idempotency_identity")
        _text(self.report_target, "report_target")
        _digest_text(self.readback_digest, "readback_digest")
        if isinstance(self.duration_seconds, bool) or not isinstance(self.duration_seconds, int) or self.duration_seconds < 0:
            raise PublicationAdapterError("duration_seconds must be a non-negative integer")
        if self.status != "verified":
            raise PublicationAdapterError("shared report receipt is not verified")
        _utc(self.verified_at, "verified_at")
        object.__setattr__(self, "native_buckets", _buckets(self.native_buckets, "native_buckets"))

    def document(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "shared_report",
            "status": self.status,
            "contract_digest": self.contract_digest,
            "authorization_digest": self.authorization_digest,
            "idempotency_identity": self.idempotency_identity,
            "report_target": self.report_target,
            "readback_digest": self.readback_digest,
            "duration_seconds": self.duration_seconds,
            "native_buckets": {key: format(value, "f") for key, value in self.native_buckets.items()},
            "verified_at": _utc(self.verified_at, "verified_at"),
        }

    @classmethod
    def from_document(cls, value: object) -> "SharedReportReceipt":
        required = {
            "schema_version", "kind", "status", "contract_digest", "authorization_digest", "idempotency_identity",
            "report_target", "readback_digest", "duration_seconds", "native_buckets", "verified_at",
        }
        if (
            isinstance(value, Mapping)
            and set(value) == required - {"authorization_digest"}
            and value.get("schema_version") == SCHEMA_VERSION
            and value.get("kind") == "shared_report"
        ):
            raise PublicationAdapterError(
                "legacy shared report receipt lacks authorization binding; "
                "recapture under the current authorization"
            )
        if not isinstance(value, Mapping) or set(value) != required or value.get("schema_version") != SCHEMA_VERSION or value.get("kind") != "shared_report":
            raise PublicationAdapterError("shared report receipt has unexpected fields")
        return cls(
            contract_digest=value["contract_digest"], authorization_digest=value["authorization_digest"],
            idempotency_identity=value["idempotency_identity"],
            report_target=value["report_target"], readback_digest=value["readback_digest"],
            duration_seconds=value["duration_seconds"], native_buckets=_buckets(value["native_buckets"], "native_buckets"),
            verified_at=_timestamp(value["verified_at"], "verified_at"), status=value["status"],
        )


@dataclass(frozen=True)
class SlackReceipt:
    """Exact verified result of Slack's idempotent correction upsert."""

    contract_digest: str
    idempotency_identity: str
    slack_target: str
    content_digest: str
    message_id: str
    verified_at: datetime
    status: str = "verified"

    def __post_init__(self) -> None:
        _digest_text(self.contract_digest, "contract_digest")
        _text(self.idempotency_identity, "idempotency_identity")
        _text(self.slack_target, "slack_target")
        _digest_text(self.content_digest, "content_digest")
        _text(self.message_id, "message_id")
        if self.status != "verified":
            raise PublicationAdapterError("Slack receipt is not verified")
        _utc(self.verified_at, "verified_at")

    def document(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "slack",
            "status": self.status,
            "contract_digest": self.contract_digest,
            "idempotency_identity": self.idempotency_identity,
            "slack_target": self.slack_target,
            "content_digest": self.content_digest,
            "message_id": self.message_id,
            "verified_at": _utc(self.verified_at, "verified_at"),
        }

    @classmethod
    def from_document(cls, value: object) -> "SlackReceipt":
        required = {
            "schema_version", "kind", "status", "contract_digest", "idempotency_identity",
            "slack_target", "content_digest", "message_id", "verified_at",
        }
        if not isinstance(value, Mapping) or set(value) != required or value.get("schema_version") != SCHEMA_VERSION or value.get("kind") != "slack":
            raise PublicationAdapterError("Slack receipt has unexpected fields")
        return cls(
            contract_digest=value["contract_digest"], idempotency_identity=value["idempotency_identity"],
            slack_target=value["slack_target"], content_digest=value["content_digest"], message_id=value["message_id"],
            verified_at=_timestamp(value["verified_at"], "verified_at"), status=value["status"],
        )


@dataclass(frozen=True)
class PublicationReceipt:
    """The durable completion receipt which joins verified report and Slack effects."""

    contract_digest: str
    authorization_digest: str
    idempotency_identity: str
    report_receipt: SharedReportReceipt
    slack_receipt: SlackReceipt
    completed_at: datetime
    state: str = "published"

    def __post_init__(self) -> None:
        _digest_text(self.contract_digest, "contract_digest")
        _digest_text(self.authorization_digest, "authorization_digest")
        _text(self.idempotency_identity, "idempotency_identity")
        if self.state != "published":
            raise PublicationAdapterError("publication receipt state is invalid")
        if not isinstance(self.report_receipt, SharedReportReceipt) or not isinstance(self.slack_receipt, SlackReceipt):
            raise PublicationAdapterError("publication receipt requires report and Slack receipts")
        for receipt in (self.report_receipt, self.slack_receipt):
            if (receipt.contract_digest, receipt.idempotency_identity) != (self.contract_digest, self.idempotency_identity):
                raise PublicationAdapterError("publication receipt does not bind its external receipts")
        if self.report_receipt.authorization_digest != self.authorization_digest:
            raise PublicationAdapterError("publication receipt does not bind its report authorization")
        _utc(self.completed_at, "completed_at")

    def document(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "kind": "publication",
            "state": self.state,
            "contract_digest": self.contract_digest,
            "authorization_digest": self.authorization_digest,
            "idempotency_identity": self.idempotency_identity,
            "report_receipt": self.report_receipt.document(),
            "slack_receipt": self.slack_receipt.document(),
            "completed_at": _utc(self.completed_at, "completed_at"),
        }

    @classmethod
    def from_document(cls, value: object) -> "PublicationReceipt":
        required = {
            "schema_version", "kind", "state", "contract_digest", "authorization_digest",
            "idempotency_identity", "report_receipt", "slack_receipt", "completed_at",
        }
        if not isinstance(value, Mapping) or set(value) != required or value.get("schema_version") != SCHEMA_VERSION or value.get("kind") != "publication":
            raise PublicationAdapterError("publication receipt has unexpected fields")
        return cls(
            contract_digest=value["contract_digest"], authorization_digest=value["authorization_digest"],
            idempotency_identity=value["idempotency_identity"],
            report_receipt=SharedReportReceipt.from_document(value["report_receipt"]),
            slack_receipt=SlackReceipt.from_document(value["slack_receipt"]),
            completed_at=_timestamp(value["completed_at"], "completed_at"), state=value["state"],
        )


class PublicationAdapter(Protocol):
    """All external mutation/readback is injected behind this small boundary."""

    def update_report(self, authorized: publication_gate.AuthorizedPublication) -> None: ...

    def read_report(self, authorized: publication_gate.AuthorizedPublication) -> SharedReportReceipt: ...

    def upsert_slack(
        self, authorized: publication_gate.AuthorizedPublication, report_receipt: SharedReportReceipt,
    ) -> SlackReceipt: ...


class PublicationReceiptStore:
    """Integrity-checked append-only journal within one local trust domain."""

    def __init__(
        self, path: Path | str,
        *, on_report_persisted: Callable[[SharedReportReceipt], None] | None = None,
    ) -> None:
        self.path = Path(path)
        self.anchor_path = self.path.with_name(self.path.name + ".anchor.jsonl")
        self.lock_path = self.path.with_name(self.path.name + ".lock")
        self.on_report_persisted = on_report_persisted

    @contextmanager
    def execution_lock(self, identity: str) -> Iterator[None]:
        _text(identity, "idempotency_identity")
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _documents(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PublicationAdapterError("publication receipt journal is unavailable") from exc
        if not text or not text.endswith("\n"):
            raise PublicationAdapterError("publication receipt journal is truncated")
        result: list[dict[str, Any]] = []
        for line in text.splitlines():
            if not line:
                raise PublicationAdapterError("publication receipt journal contains a blank line")
            try:
                document = json.loads(line)
            except json.JSONDecodeError as exc:
                raise PublicationAdapterError("publication receipt journal contains invalid JSON") from exc
            if not isinstance(document, dict) or _canonical(document) != line:
                raise PublicationAdapterError("publication receipt journal is not canonical")
            result.append(document)
        return result

    def _records(self) -> list[tuple[str, SharedReportReceipt | PublicationReceipt]]:
        records: list[tuple[str, SharedReportReceipt | PublicationReceipt]] = []
        previous = ZERO_DIGEST
        for sequence, document in enumerate(self._documents(self.path), start=1):
            expected = {"sequence", "kind", "receipt", "previous_digest", "record_digest"}
            if set(document) != expected or document.get("sequence") != sequence:
                raise PublicationAdapterError("publication receipt sequence is invalid")
            if document.get("previous_digest") != previous:
                raise PublicationAdapterError("publication receipt predecessor digest is invalid")
            kind = document.get("kind")
            if kind == "shared_report":
                receipt = SharedReportReceipt.from_document(document.get("receipt"))
            elif kind == "publication":
                receipt = PublicationReceipt.from_document(document.get("receipt"))
            else:
                raise PublicationAdapterError("publication receipt kind is invalid")
            expected_digest = _digest({key: document[key] for key in expected if key != "record_digest"})
            if document.get("record_digest") != expected_digest:
                raise PublicationAdapterError("publication receipt digest is invalid")
            previous = expected_digest
            records.append((kind, receipt))
        self._verify_anchors(len(records), previous)
        return records

    def _verify_anchors(self, record_count: int, record_digest: str) -> None:
        anchors = self._documents(self.anchor_path)
        if not record_count:
            if anchors:
                raise PublicationAdapterError("publication receipt anchors have no journal")
            return
        if not anchors:
            raise PublicationAdapterError("publication receipt journal has no anchor")
        previous = ZERO_DIGEST
        for sequence, anchor in enumerate(anchors, start=1):
            expected = {"sequence", "record_count", "record_digest", "previous_anchor_digest", "anchor_digest"}
            if set(anchor) != expected or anchor.get("sequence") != sequence:
                raise PublicationAdapterError("publication receipt anchor sequence is invalid")
            if anchor.get("previous_anchor_digest") != previous:
                raise PublicationAdapterError("publication receipt anchor predecessor is invalid")
            expected_digest = _digest({key: anchor[key] for key in expected if key != "anchor_digest"})
            if anchor.get("anchor_digest") != expected_digest:
                raise PublicationAdapterError("publication receipt anchor digest is invalid")
            previous = expected_digest
        head = anchors[-1]
        if head.get("record_count") != record_count or head.get("record_digest") != record_digest:
            raise PublicationAdapterError("publication receipt journal does not match its anchor")

    @staticmethod
    def _append(path: Path, document: Mapping[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = _canonical(dict(document)) + "\n"
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            remaining = memoryview(payload.encode("utf-8"))
            while remaining:
                written = os.write(descriptor, remaining)
                if written <= 0:
                    raise PublicationAdapterError("publication receipt append did not make progress")
                remaining = remaining[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _append_receipt(self, kind: str, receipt: SharedReportReceipt | PublicationReceipt) -> None:
        records = self._records()
        documents = self._documents(self.path)
        previous = ZERO_DIGEST if not documents else _digest_text(documents[-1]["record_digest"], "record_digest")
        record = {
            "sequence": len(records) + 1, "kind": kind, "receipt": receipt.document(),
            "previous_digest": previous,
        }
        record["record_digest"] = _digest(record)
        self._append(self.path, record)
        anchors = self._documents(self.anchor_path)
        anchor = {
            "sequence": len(anchors) + 1, "record_count": record["sequence"],
            "record_digest": record["record_digest"],
            "previous_anchor_digest": ZERO_DIGEST if not anchors else anchors[-1]["anchor_digest"],
        }
        anchor["anchor_digest"] = _digest(anchor)
        self._append(self.anchor_path, anchor)

    def _state(self) -> tuple[dict[str, SharedReportReceipt], dict[str, PublicationReceipt]]:
        reports: dict[str, SharedReportReceipt] = {}
        publications: dict[str, PublicationReceipt] = {}
        for kind, receipt in self._records():
            identity = receipt.idempotency_identity
            destination: dict[str, Any] = reports if kind == "shared_report" else publications
            if identity in destination and destination[identity] != receipt:
                raise PublicationAdapterError("publication receipt identity has conflicting history")
            destination[identity] = receipt
        return reports, publications

    def report_receipt(self, identity: str) -> SharedReportReceipt | None:
        return self._state()[0].get(identity)

    def publication_receipt(self, identity: str) -> PublicationReceipt | None:
        return self._state()[1].get(identity)

    def persist_report(self, receipt: SharedReportReceipt) -> SharedReportReceipt:
        reports, _ = self._state()
        existing = reports.get(receipt.idempotency_identity)
        if existing is not None:
            if existing != receipt:
                raise PublicationAdapterError("report receipt identity has drifted")
            return existing
        self._append_receipt("shared_report", receipt)
        if self.on_report_persisted is not None:
            self.on_report_persisted(receipt)
        return receipt

    def persist_publication(self, receipt: PublicationReceipt) -> PublicationReceipt:
        _, publications = self._state()
        existing = publications.get(receipt.idempotency_identity)
        if existing is not None:
            if existing != receipt:
                raise PublicationAdapterError("publication receipt identity has drifted")
            return existing
        self._append_receipt("publication", receipt)
        return receipt

    def published_receipts(self) -> list[PublicationReceipt]:
        return list(self._state()[1].values())

    def verify(self) -> None:
        self._state()


def _authorized(value: object, now: datetime) -> publication_gate.AuthorizedPublication:
    if not isinstance(value, publication_gate.AuthorizedPublication):
        raise PublicationAdapterError("publication is not authorized")
    try:
        authorized = publication_gate.AuthorizedPublication.from_document(value.document())
    except publication_gate.PublicationGateError as exc:
        raise PublicationAdapterError(f"publication authorization is invalid: {exc}") from exc
    if now.tzinfo is None or now.utcoffset() is None:
        raise PublicationAdapterError("publication clock must include a timezone")
    expires_at = datetime.fromisoformat(authorized.expires_at.replace("Z", "+00:00"))
    if expires_at <= now.astimezone(timezone.utc):
        raise PublicationAdapterError("publication authorization is expired")
    return authorized


def _verify_report(
    authorized: publication_gate.AuthorizedPublication, receipt: SharedReportReceipt,
) -> SharedReportReceipt:
    contract = authorized.contract
    if (
        receipt.contract_digest, receipt.authorization_digest,
        receipt.idempotency_identity, receipt.report_target,
    ) != (
        authorized.contract_digest, authorized.authorization_digest,
        authorized.idempotency_key, contract.report_target,
    ):
        raise PublicationAdapterError("shared report receipt does not bind the authorization")
    if receipt.duration_seconds != contract.duration_seconds or dict(receipt.native_buckets) != dict(contract.native_buckets):
        raise PublicationAdapterError("shared report readback does not exactly match the authorized contract")
    return receipt


def expected_slack_content_digest(
    authorized: publication_gate.AuthorizedPublication,
) -> str:
    """Bind the finance facts that an approved Slack correction may publish."""
    contract = authorized.contract
    return _digest({
        "schema_version": "clockify-finance-slack-content/v1",
        "period_id": contract.period_id,
        "period_start": contract.period_start,
        "period_end": contract.period_end,
        "revision": contract.revision,
        "duration_seconds": contract.duration_seconds,
        "native_buckets": {
            code: format(amount, "f")
            for code, amount in sorted(contract.native_buckets.items())
        },
        "usd_buckets": {
            code: format(amount, "f")
            for code, amount in sorted(contract.usd_buckets.items())
        },
        "usd_equivalent_total": format(contract.usd_equivalent_total, "f"),
        "report_target": contract.report_target,
    })


def _verify_slack(
    authorized: publication_gate.AuthorizedPublication, receipt: SlackReceipt,
) -> SlackReceipt:
    if (receipt.contract_digest, receipt.idempotency_identity, receipt.slack_target) != (
        authorized.contract_digest, authorized.idempotency_key, authorized.contract.slack_target,
    ):
        raise PublicationAdapterError("Slack receipt does not bind the authorization")
    if receipt.content_digest != expected_slack_content_digest(authorized):
        raise PublicationAdapterError("Slack receipt content does not match the authorized contract")
    return receipt


def execute_authorized_publication(
    authorized: publication_gate.AuthorizedPublication,
    adapter: PublicationAdapter,
    receipt_store: PublicationReceiptStore,
    *,
    now: datetime,
) -> PublicationReceipt:
    """Refresh/read the report, persist its proof, then perform exactly one Slack upsert."""
    if not isinstance(receipt_store, PublicationReceiptStore):
        raise PublicationAdapterError("publication receipt store is invalid")
    verified_authorization = _authorized(authorized, now)
    identity = verified_authorization.idempotency_key
    with receipt_store.execution_lock(identity):
        published = receipt_store.publication_receipt(identity)
        if published is not None:
            if (published.contract_digest, published.authorization_digest) != (
                verified_authorization.contract_digest, verified_authorization.authorization_digest,
            ):
                raise PublicationAdapterError("published receipt does not bind the authorization")
            return published
        report = receipt_store.report_receipt(identity)
        if report is None:
            try:
                adapter.update_report(verified_authorization)
                report = adapter.read_report(verified_authorization)
            except PublicationAdapterError:
                raise
            except Exception as exc:
                raise PublicationAdapterError("shared report refresh or readback failed") from exc
            if not isinstance(report, SharedReportReceipt):
                raise PublicationAdapterError("shared report adapter did not return a receipt")
            report = _verify_report(verified_authorization, report)
            report = receipt_store.persist_report(report)
        else:
            report = _verify_report(verified_authorization, report)
        try:
            slack = adapter.upsert_slack(verified_authorization, report)
        except PublicationAdapterError:
            raise
        except Exception as exc:
            raise PublicationAdapterError("Slack upsert failed") from exc
        if not isinstance(slack, SlackReceipt):
            raise PublicationAdapterError("Slack adapter did not return a receipt")
        slack = _verify_slack(verified_authorization, slack)
        return receipt_store.persist_publication(PublicationReceipt(
            contract_digest=verified_authorization.contract_digest,
            authorization_digest=verified_authorization.authorization_digest,
            idempotency_identity=identity,
            report_receipt=report,
            slack_receipt=slack,
            completed_at=now,
        ))
