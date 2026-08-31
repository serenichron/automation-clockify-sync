#!/usr/bin/env python3
"""Fail-closed preparation and authorization for Clockify finance publication."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from scripts import clockify_currency
from scripts import clockify_period_readback
from scripts import posting_receipts
from scripts import reconciliation_manifest


SCHEMA_VERSION = "publication-contract/v1"
PREPARED_STATE = "publication_prepared"
AUTHORIZED_STATE = "publication_authorized"
OPERATIONS = ("shared_report_update", "slack_correction")
_DIGEST = re.compile(r"^sha256:[a-f0-9]{64}$")


class PublicationGateError(ValueError):
    """Publication evidence is incomplete, drifted, or outside its approval."""


def _canonical(value: object) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise PublicationGateError("value is not canonical JSON") from exc


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _digest_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise PublicationGateError(f"{field} must be a SHA-256 digest")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PublicationGateError(f"{field} must be a non-empty string")
    return value


def _timestamp(value: object, field: str) -> datetime:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PublicationGateError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PublicationGateError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _utc(value: datetime) -> str:
    value = value.astimezone(timezone.utc)
    suffix = value.strftime("%Y-%m-%dT%H:%M:%S")
    if value.microsecond:
        suffix += f".{value.microsecond:06d}"
    return suffix + "Z"


def _decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        raise PublicationGateError(f"{field} must be a nonnegative finite Decimal")
    return value


def _identity(
    *, period_id: object, workspace_id: object, member_id: object, timezone_name: object,
    period_start: object, period_end: object, revision: object,
) -> reconciliation_manifest.PeriodIdentity:
    try:
        identity = reconciliation_manifest.PeriodIdentity(
            member_id=_text(member_id, "member_id"), workspace_id=_text(workspace_id, "workspace_id"),
            timezone=_text(timezone_name, "timezone"), since=_timestamp(period_start, "period_start"),
            until=_timestamp(period_end, "period_end"), revision=revision,
        )
    except reconciliation_manifest.ManifestError as exc:
        raise PublicationGateError(f"publication period is invalid: {exc}") from exc
    if _text(period_id, "period_id") != identity.period_id:
        raise PublicationGateError("period_id does not match publication target")
    return identity


def publication_idempotency_key(
    period: reconciliation_manifest.ReconciliationManifest | reconciliation_manifest.PeriodIdentity | Mapping[str, object],
) -> str:
    """Return the one stable idempotency identity for a period revision."""
    if isinstance(period, reconciliation_manifest.ReconciliationManifest):
        identity = period.identity
    elif isinstance(period, reconciliation_manifest.PeriodIdentity):
        identity = period
    elif isinstance(period, Mapping):
        if "manifest_digest" in period:
            try:
                identity = reconciliation_manifest.ReconciliationManifest.from_document(period).identity
            except reconciliation_manifest.ManifestError as exc:
                raise PublicationGateError(f"publication manifest is invalid: {exc}") from exc
        else:
            candidate = period.get("period", period)
            if not isinstance(candidate, Mapping):
                raise PublicationGateError("publication period is invalid")
            try:
                identity = reconciliation_manifest.PeriodIdentity(
                    member_id=_text(candidate.get("member_id"), "member_id"),
                    workspace_id=_text(candidate.get("workspace_id"), "workspace_id"),
                    timezone=_text(candidate.get("timezone"), "timezone"),
                    since=_timestamp(candidate.get("period_start", candidate.get("since_utc")), "period_start"),
                    until=_timestamp(candidate.get("period_end", candidate.get("until_utc")), "period_end"),
                    revision=candidate.get("revision"),
                )
            except reconciliation_manifest.ManifestError as exc:
                raise PublicationGateError(f"publication period is invalid: {exc}") from exc
    else:
        raise PublicationGateError("publication period is invalid")
    try:
        zone = ZoneInfo(identity.timezone)
    except ZoneInfoNotFoundError as exc:
        raise PublicationGateError("publication timezone is unsupported") from exc
    start = identity.since.astimezone(zone).date().isoformat()
    end = identity.until.astimezone(zone).date().isoformat()
    return f"finance-report:{identity.workspace_id}:{identity.member_id}:{start}:{end}:{identity.revision}"


def _quote_from_document(value: object) -> clockify_currency.FxQuoteReceipt:
    if isinstance(value, clockify_currency.FxQuoteReceipt):
        return value
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version", "provider", "effective_date", "fetched_at", "base_currency", "rates", "payload_digest",
    } or value.get("schema_version") != clockify_currency.SCHEMA_VERSION:
        raise PublicationGateError("FX quote is invalid")
    try:
        rates = value["rates"]
        if not isinstance(rates, Mapping):
            raise ValueError("rates are invalid")
        return clockify_currency.FxQuoteReceipt(
            provider=value["provider"], effective_date=date.fromisoformat(_text(value["effective_date"], "FX effective_date")),
            fetched_at=_timestamp(value["fetched_at"], "FX fetched_at"), base_currency=value["base_currency"],
            rates={str(code): Decimal(str(rate)) for code, rate in rates.items()}, payload_digest=value["payload_digest"],
        )
    except (ValueError, clockify_currency.CurrencyContractError) as exc:
        raise PublicationGateError(f"FX quote is invalid: {exc}") from exc


def _summary_from_value(
    value: object, quote: clockify_currency.FxQuoteReceipt, publication_date: date,
) -> clockify_currency.CurrencySummary:
    if value is None:
        raise PublicationGateError("currency summary is required")
    if isinstance(value, clockify_currency.CurrencySummary):
        return value
    if isinstance(value, Mapping) and "summary" in value:
        value = value["summary"]
    try:
        return clockify_currency.CurrencySummary.from_dict(value, quote=quote, publication_date=publication_date)
    except clockify_currency.CurrencyContractError as exc:
        raise PublicationGateError(f"currency summary is invalid: {exc}") from exc


def _contract_currency(
    native: Mapping[str, Decimal], summary: clockify_currency.CurrencySummary,
) -> tuple[dict[str, Decimal], dict[str, Decimal], Decimal]:
    normalized_native = {str(code): _decimal(amount, "native currency amount") for code, amount in native.items()}
    if not normalized_native or normalized_native != dict(summary.native_buckets):
        raise PublicationGateError("currency summary does not match Clockify native buckets")
    return normalized_native, dict(summary.usd_buckets), summary.usd_equivalent_total


@dataclass(frozen=True)
class PublicationContract:
    period_id: str
    workspace_id: str
    member_id: str
    timezone: str
    period_start: str
    period_end: str
    revision: int
    manifest_digest: str
    event_history_digest: str
    post_receipt_digest: str
    api_readback_digest: str
    shared_report_readback_digest: str
    api_readback_refreshed_at: str
    shared_report_readback_refreshed_at: str
    duration_seconds: int
    native_buckets: Mapping[str, Decimal]
    usd_buckets: Mapping[str, Decimal]
    usd_equivalent_total: Decimal
    fx_quote: clockify_currency.FxQuoteReceipt
    report_target: str
    slack_target: str
    idempotency_key: str
    contract_digest: str
    state: str = PREPARED_STATE

    def __post_init__(self) -> None:
        if self.state != PREPARED_STATE:
            raise PublicationGateError("publication contract state is invalid")
        identity = _identity(
            period_id=self.period_id, workspace_id=self.workspace_id, member_id=self.member_id,
            timezone_name=self.timezone, period_start=self.period_start, period_end=self.period_end,
            revision=self.revision,
        )
        for field in (
            "manifest_digest", "event_history_digest", "post_receipt_digest", "api_readback_digest",
            "shared_report_readback_digest", "contract_digest",
        ):
            _digest_text(getattr(self, field), field)
        _timestamp(self.api_readback_refreshed_at, "api_readback_refreshed_at")
        _timestamp(self.shared_report_readback_refreshed_at, "shared_report_readback_refreshed_at")
        if isinstance(self.duration_seconds, bool) or not isinstance(self.duration_seconds, int) or self.duration_seconds < 0:
            raise PublicationGateError("duration_seconds is invalid")
        summary = clockify_currency.CurrencySummary(
            native_buckets=dict(self.native_buckets), usd_buckets=dict(self.usd_buckets),
            usd_equivalent_total=self.usd_equivalent_total,
        )
        if not isinstance(self.fx_quote, clockify_currency.FxQuoteReceipt):
            raise PublicationGateError("FX quote is invalid")
        expected_key = publication_idempotency_key(identity)
        if self.idempotency_key != expected_key:
            raise PublicationGateError("idempotency key does not match publication target")
        _text(self.report_target, "report_target")
        _text(self.slack_target, "slack_target")
        object.__setattr__(self, "native_buckets", dict(summary.native_buckets))
        object.__setattr__(self, "usd_buckets", dict(summary.usd_buckets))

    def _unsigned_document(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION, "state": self.state, "period_id": self.period_id,
            "workspace_id": self.workspace_id, "member_id": self.member_id, "timezone": self.timezone,
            "period_start": self.period_start, "period_end": self.period_end, "revision": self.revision,
            "manifest_digest": self.manifest_digest, "event_history_digest": self.event_history_digest,
            "post_receipt_digest": self.post_receipt_digest, "api_readback_digest": self.api_readback_digest,
            "shared_report_readback_digest": self.shared_report_readback_digest,
            "api_readback_refreshed_at": self.api_readback_refreshed_at,
            "shared_report_readback_refreshed_at": self.shared_report_readback_refreshed_at,
            "duration_seconds": self.duration_seconds,
            "native_buckets": {code: format(amount, "f") for code, amount in sorted(self.native_buckets.items())},
            "usd_buckets": {code: format(amount, "f") for code, amount in sorted(self.usd_buckets.items())},
            "usd_equivalent_total": format(self.usd_equivalent_total, "f"), "fx_quote": self.fx_quote.to_dict(),
            "report_target": self.report_target, "slack_target": self.slack_target,
            "idempotency_key": self.idempotency_key,
        }

    def document(self) -> dict[str, object]:
        return {**self._unsigned_document(), "contract_digest": self.contract_digest}

    @classmethod
    def from_document(cls, value: object) -> "PublicationContract":
        required = {
            "schema_version", "state", "period_id", "workspace_id", "member_id", "timezone", "period_start", "period_end", "revision",
            "manifest_digest", "event_history_digest", "post_receipt_digest", "api_readback_digest",
            "shared_report_readback_digest", "api_readback_refreshed_at", "shared_report_readback_refreshed_at",
            "duration_seconds",
            "native_buckets", "usd_buckets", "usd_equivalent_total", "fx_quote", "report_target", "slack_target", "idempotency_key", "contract_digest",
        }
        if not isinstance(value, Mapping) or set(value) != required or value.get("schema_version") != SCHEMA_VERSION:
            raise PublicationGateError("publication contract fields are invalid")
        quote = _quote_from_document(value["fx_quote"])
        try:
            summary = clockify_currency.CurrencySummary.from_dict(
                {"native_buckets": value["native_buckets"], "usd_buckets": value["usd_buckets"], "usd_equivalent_total": value["usd_equivalent_total"]},
                quote=quote, publication_date=quote.effective_date,
            )
        except clockify_currency.CurrencyContractError as exc:
            raise PublicationGateError(f"publication contract currency is invalid: {exc}") from exc
        contract = cls(
            period_id=value["period_id"], workspace_id=value["workspace_id"], member_id=value["member_id"], timezone=value["timezone"],
            period_start=value["period_start"], period_end=value["period_end"], revision=value["revision"],
            manifest_digest=value["manifest_digest"], event_history_digest=value["event_history_digest"], post_receipt_digest=value["post_receipt_digest"],
            api_readback_digest=value["api_readback_digest"], shared_report_readback_digest=value["shared_report_readback_digest"],
            api_readback_refreshed_at=value["api_readback_refreshed_at"], shared_report_readback_refreshed_at=value["shared_report_readback_refreshed_at"],
            duration_seconds=value["duration_seconds"],
            native_buckets=summary.native_buckets, usd_buckets=summary.usd_buckets, usd_equivalent_total=summary.usd_equivalent_total,
            fx_quote=quote, report_target=value["report_target"], slack_target=value["slack_target"],
            idempotency_key=value["idempotency_key"], contract_digest=value["contract_digest"], state=value["state"],
        )
        if contract.contract_digest != _digest(contract._unsigned_document()):
            raise PublicationGateError("publication contract digest does not match its contents")
        return contract


@dataclass(frozen=True)
class AuthorizedPublication:
    contract: PublicationContract
    approval_id: str
    approval_digest: str
    approver: str
    expires_at: str
    operations: tuple[str, str]
    authorization_digest: str
    state: str = AUTHORIZED_STATE

    def __post_init__(self) -> None:
        if self.state != AUTHORIZED_STATE or not isinstance(self.contract, PublicationContract):
            raise PublicationGateError("authorized publication state is invalid")
        for field in ("approval_id", "approver"):
            _text(getattr(self, field), field)
        _timestamp(self.expires_at, "approval expiry")
        _digest_text(self.approval_digest, "approval_digest")
        _digest_text(self.authorization_digest, "authorization_digest")
        if tuple(self.operations) != OPERATIONS:
            raise PublicationGateError("publication approval operations are invalid")

    @property
    def contract_digest(self) -> str:
        return self.contract.contract_digest

    @property
    def idempotency_key(self) -> str:
        return self.contract.idempotency_key

    def _unsigned_document(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION, "state": self.state, "contract": self.contract.document(),
            "contract_digest": self.contract.contract_digest, "approval_id": self.approval_id,
            "approval_digest": self.approval_digest, "approver": self.approver, "expires_at": self.expires_at,
            "operations": list(self.operations), "idempotency_key": self.idempotency_key,
        }

    def document(self) -> dict[str, object]:
        return {**self._unsigned_document(), "authorization_digest": self.authorization_digest}

    @classmethod
    def from_document(cls, value: object) -> "AuthorizedPublication":
        required = {
            "schema_version", "state", "contract", "contract_digest", "approval_id", "approval_digest", "approver", "expires_at", "operations", "idempotency_key", "authorization_digest",
        }
        if not isinstance(value, Mapping) or set(value) != required or value.get("schema_version") != SCHEMA_VERSION:
            raise PublicationGateError("authorized publication fields are invalid")
        contract = PublicationContract.from_document(value["contract"])
        operations = value["operations"]
        if not isinstance(operations, list):
            raise PublicationGateError("publication approval operations are invalid")
        authorized = cls(
            contract=contract, approval_id=value["approval_id"], approval_digest=value["approval_digest"], approver=value["approver"],
            expires_at=value["expires_at"], operations=tuple(operations), authorization_digest=value["authorization_digest"], state=value["state"],
        )
        if value["contract_digest"] != contract.contract_digest or value["idempotency_key"] != contract.idempotency_key:
            raise PublicationGateError("authorized publication does not bind its contract")
        if authorized.authorization_digest != _digest(authorized._unsigned_document()):
            raise PublicationGateError("authorized publication digest does not match its contents")
        return authorized


def _manifest(value: reconciliation_manifest.ReconciliationManifest | Mapping[str, object]) -> reconciliation_manifest.ReconciliationManifest:
    try:
        document = value.document() if isinstance(value, reconciliation_manifest.ReconciliationManifest) else value
        manifest = reconciliation_manifest.ReconciliationManifest.from_document(document)
        reconciliation_manifest._verify_artifact_refs(list(manifest.artifacts))
    except reconciliation_manifest.ManifestError as exc:
        raise PublicationGateError(f"manifest artifact verification failed: {exc}") from exc
    if manifest.state != "verifying":
        raise PublicationGateError("manifest state is not ready for publication")
    if manifest.blockers:
        raise PublicationGateError("manifest has active blockers")
    return manifest


def _verified_manifest(
    value: reconciliation_manifest.ReconciliationManifest | Mapping[str, object], events: Path | str,
) -> reconciliation_manifest.ReconciliationManifest:
    """Re-derive one supplied manifest from its canonical append-only history."""
    supplied = _manifest(value)
    if not isinstance(events, (Path, str)):
        raise PublicationGateError("period events path is invalid")
    try:
        derived = reconciliation_manifest.ReconciliationCoordinator(
            supplied.identity, reconciliation_manifest.CoordinatorEventStore(Path(events)),
        ).derive()
    except reconciliation_manifest.ManifestError as exc:
        raise PublicationGateError(f"period event history is invalid: {exc}") from exc
    if supplied.document() != derived.document():
        raise PublicationGateError("supplied manifest does not match canonical period event history")
    return _manifest(derived)


def _approved_manifest_prefix(
    identity: reconciliation_manifest.PeriodIdentity,
    history: Sequence[reconciliation_manifest.CoordinatorEvent],
    approval: posting_receipts.ApprovalReceipt,
) -> reconciliation_manifest.ReconciliationManifest:
    """Find the one approved coordinator prefix bound into a consumed post approval."""
    matches: list[reconciliation_manifest.ReconciliationManifest] = []
    try:
        for end in range(1, len(history) + 1):
            candidate = reconciliation_manifest.derive_manifest_from_verified_events(
                identity, history[:end],
            )
            if candidate.events_digest == approval.event_history_digest:
                matches.append(candidate)
    except reconciliation_manifest.ManifestError as exc:
        raise PublicationGateError(f"approval history prefix is unverifiable: {exc}") from exc
    if not matches:
        raise PublicationGateError("approval history prefix is absent from canonical events")
    if len(matches) != 1:
        raise PublicationGateError("approval history prefix is ambiguous")
    prefix = matches[0]
    if prefix.state != "approved":
        raise PublicationGateError("approval history prefix is not in the approved state")
    if prefix.manifest_digest != approval.manifest_digest:
        raise PublicationGateError("approval manifest digest does not match its approved history prefix")
    return prefix


def _load_json(path: Path, field: str) -> Mapping[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicationGateError(f"{field} is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise PublicationGateError(f"{field} is not an object")
    return value


def _require_bound_evidence(
    manifest: reconciliation_manifest.ReconciliationManifest, value: object, label: str,
) -> None:
    """Require a supplied pure-gate document to be one of the manifest's verified artifacts."""
    if not isinstance(value, Mapping):
        raise PublicationGateError(f"{label} evidence is invalid")
    candidate = _canonical(value)
    for reference in manifest.artifacts:
        try:
            artifact = _load_json(Path(str(reference["path"])), "manifest artifact")
        except PublicationGateError:
            continue
        if _canonical(artifact) == candidate:
            return
    raise PublicationGateError(f"{label} artifact is absent from the verified manifest")


def _slice_bundles(manifest: reconciliation_manifest.ReconciliationManifest) -> tuple[Mapping[str, object], ...]:
    bundles: list[Mapping[str, object]] = []
    for reference in manifest.artifacts:
        path = Path(str(reference["path"]))
        try:
            document = _load_json(path, "manifest artifact")
        except PublicationGateError:
            continue
        if "run_id" in document and "evidence_ledger" in document:
            bundles.append(document)
    if not bundles:
        raise PublicationGateError("verified slice bundle is required")
    return tuple(bundles)


def _interval(value: object, field: str) -> tuple[datetime, datetime]:
    if not isinstance(value, Mapping):
        raise PublicationGateError(f"{field} is invalid")
    start = _timestamp(value.get("since"), f"{field} since")
    end = _timestamp(value.get("until"), f"{field} until")
    if start >= end:
        raise PublicationGateError(f"{field} must be half-open")
    return start, end


def _required_slices(
    coverage: Mapping[str, object], identity: reconciliation_manifest.PeriodIdentity,
) -> tuple[tuple[str, datetime, datetime], ...]:
    value = coverage.get("required_slices")
    if not isinstance(value, list) or not value:
        raise PublicationGateError("coverage must enumerate required slices")
    required: list[tuple[str, datetime, datetime]] = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"slice_id", "since", "until"}:
            raise PublicationGateError("required slice is invalid")
        interval = _interval(item, "required slice")
        required.append((_text(item.get("slice_id"), "required slice ID"), *interval))
    if len({item[0] for item in required}) != len(required):
        raise PublicationGateError("required slice IDs must be unique")
    ordered = sorted(required, key=lambda item: item[1])
    expected_start = identity.since.astimezone(timezone.utc)
    expected_end = identity.until.astimezone(timezone.utc)
    if ordered[0][1] != expected_start or ordered[-1][2] != expected_end:
        raise PublicationGateError("required slices do not cover the publication period")
    if any(previous[2] != current[1] for previous, current in zip(ordered, ordered[1:])):
        raise PublicationGateError("required slices are not a contiguous non-overlapping union")
    return tuple(required)


def _require_slices(
    manifest: reconciliation_manifest.ReconciliationManifest,
    required: tuple[tuple[str, datetime, datetime], ...],
) -> None:
    identity = manifest.identity
    expected_target = {
        "period_id": identity.period_id, "workspace_id": identity.workspace_id, "member_id": identity.member_id,
    }
    expected = set(required)
    completed: list[tuple[str, datetime, datetime]] = []
    for bundle in _slice_bundles(manifest):
        target = bundle.get("target")
        if not isinstance(target, Mapping) or dict(target) != expected_target:
            raise PublicationGateError("slice target does not match the publication target")
        slice_id = _text(bundle.get("slice_id"), "slice ID")
        interval = _interval(bundle.get("date_range"), "slice date range")
        candidate = (slice_id, *interval)
        if candidate not in expected:
            raise PublicationGateError("slice interval is not required by bound coverage")
        ledger = bundle.get("evidence_ledger")
        completeness = ledger.get("source_completeness") if isinstance(ledger, Mapping) else None
        if not isinstance(completeness, Mapping) or completeness.get("status") != "complete" or completeness.get("incomplete_sources") != []:
            raise PublicationGateError("slice coverage is incomplete")
        evidence = bundle.get("evidence")
        calendly = evidence.get("calendly") if isinstance(evidence, Mapping) else None
        if not isinstance(calendly, Mapping) or calendly.get("status") not in {"ok", "excluded"} or calendly.get("complete") is not True:
            raise PublicationGateError("Calendly coverage is incomplete")
        accounting = bundle.get("meeting_accounting")
        if not isinstance(accounting, Mapping) or accounting.get("status") != "complete" or accounting.get("unresolved_exceptions") != []:
            raise PublicationGateError("canonical meeting accounting is incomplete")
        for field, label in (
            ("semantic_exceptions", "semantic"), ("routing_exceptions", "routing"),
            ("overlap_exceptions", "overlap"), ("billability_exceptions", "billability"),
        ):
            if bundle.get(field, []) != []:
                raise PublicationGateError(f"unresolved {label} exceptions block publication")
        completed.append(candidate)
    if len(completed) != len(set(completed)) or set(completed) != expected:
        raise PublicationGateError("completed slices do not exactly cover the required intervals")


def _require_coverage(
    value: object, identity: reconciliation_manifest.PeriodIdentity,
    events: Sequence[reconciliation_manifest.CoordinatorEvent],
) -> tuple[tuple[str, datetime, datetime], ...]:
    if not isinstance(value, Mapping):
        raise PublicationGateError("coverage evidence is invalid")
    completeness = value.get("source_completeness", value)
    if not isinstance(completeness, Mapping):
        raise PublicationGateError("coverage evidence is invalid")
    incomplete = completeness.get("incomplete_sources", [])
    limitations = value.get("limitations", [])
    if not isinstance(incomplete, list) or not isinstance(limitations, list):
        raise PublicationGateError("coverage evidence is invalid")
    required = _required_slices(value, identity)
    status = completeness.get("status", value.get("status"))
    if status == "complete" and not incomplete:
        if limitations:
            raise PublicationGateError("complete coverage cannot carry limitations")
        return required
    if status != "incomplete" or not incomplete:
        raise PublicationGateError("coverage is incomplete")
    sources = {_text(source, "coverage incomplete source").lower() for source in incomplete}
    expected_fields = {
        "approval_id", "approver", "approved_at", "period_id", "period_start", "period_end", "source", "coverage_digest",
    }
    approved: set[str] = set()
    period = identity.document()
    for limitation in limitations:
        if not isinstance(limitation, Mapping) or set(limitation) != expected_fields:
            raise PublicationGateError("coverage limitation lacks immutable approval binding")
        source = _text(limitation.get("source"), "coverage limitation source").lower()
        _text(limitation.get("approval_id"), "coverage approval_id")
        _text(limitation.get("approver"), "coverage approver")
        _timestamp(limitation.get("approved_at"), "coverage approval timestamp")
        _digest_text(limitation.get("coverage_digest"), "coverage digest")
        if (
            limitation.get("period_id") != identity.period_id
            or _timestamp(limitation.get("period_start"), "coverage period_start") != identity.since.astimezone(timezone.utc)
            or _timestamp(limitation.get("period_end"), "coverage period_end") != identity.until.astimezone(timezone.utc)
        ):
            raise PublicationGateError("coverage limitation does not bind the exact half-open period")
        if not any(
            event.event_type == "coverage_limitation_approved" and dict(event.payload) == dict(limitation)
            for event in events
        ):
            raise PublicationGateError("coverage limitation lacks matching verified approval event")
        approved.add(source)
    if sources != approved:
        raise PublicationGateError("coverage is incomplete")
    return required


def _require_pass(value: object, name: str) -> None:
    if not isinstance(value, Mapping) or value.get("status") not in {"pass", "passed"}:
        raise PublicationGateError(f"{name} did not pass")
    if name == "replay":
        identity = value.get("identity")
        artifacts = identity.get("artifacts") if isinstance(identity, Mapping) else None
        if not isinstance(artifacts, Mapping) or not artifacts:
            raise PublicationGateError("replay has no immutable artifact binding")


def _readback(
    value: object, identity: reconciliation_manifest.PeriodIdentity, *, evidence_kind: str, now: datetime,
) -> clockify_period_readback.ClockifyPeriodReadback:
    try:
        readback = clockify_period_readback.normalize_readback(value)
    except clockify_period_readback.ClockifyReadbackError as exc:
        raise PublicationGateError(f"Clockify readback is invalid: {exc}") from exc
    expected_start = identity.since.astimezone(timezone.utc)
    expected_end = identity.until.astimezone(timezone.utc)
    if (
        readback.evidence_kind != evidence_kind or readback.workspace_id != identity.workspace_id or readback.member_id != identity.member_id
        or readback.timezone != identity.timezone or readback.period_start.astimezone(timezone.utc) != expected_start
        or readback.period_end.astimezone(timezone.utc) != expected_end or readback.include_running or readback.include_deleted
    ):
        raise PublicationGateError("Clockify readback target does not match the manifest")
    now_utc = now.astimezone(timezone.utc)
    age_seconds = (now_utc - readback.refreshed_at).total_seconds()
    if age_seconds < 0:
        raise PublicationGateError("Clockify readback freshness is in the future")
    if age_seconds > 900:
        raise PublicationGateError("Clockify readback freshness is stale")
    if evidence_kind == "report" and (not readback.request_receipt or not readback.native_costs):
        raise PublicationGateError("shared-report readback has no native currency evidence")
    if evidence_kind == "ledger" and not readback.entry_ids:
        raise PublicationGateError("Clockify readback has no native currency buckets")
    return readback


def _require_post_provenance(
    value: object, *, approval_receipt_id: object, approval_events: Path | str, post_events: Path | str,
    identity: reconciliation_manifest.PeriodIdentity,
    history: Sequence[reconciliation_manifest.CoordinatorEvent], now: datetime,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or value.get("status") != "complete":
        raise PublicationGateError("Clockify post receipt is incomplete")
    _digest_text(value.get("final_live_readback_sha256"), "Clockify post receipt final readback")
    supplied_post_events = value.get("post_events")
    if not isinstance(supplied_post_events, Mapping):
        raise PublicationGateError("Clockify post receipt has no verified post events")
    expected_operation = posting_receipts.derive_operation_identity(
        operation="clockify_post", period_id=identity.period_id, workspace_id=identity.workspace_id, member_id=identity.member_id,
    )
    if not isinstance(approval_events, (Path, str)) or not isinstance(post_events, (Path, str)):
        raise PublicationGateError("post provenance paths are invalid")
    try:
        approval = posting_receipts.ApprovalReceiptStore(Path(approval_events)).require_consumed(
            _text(approval_receipt_id, "post approval receipt ID"), operation_identity=expected_operation, now=now,
        )
        events = posting_receipts.PostEventStore(Path(post_events))
        verified_events = events.verify()
        derived = events.derive_receipt(expected_operation)
    except posting_receipts.PostingReceiptError as exc:
        raise PublicationGateError(f"Clockify post approval or event history is invalid: {exc}") from exc
    period = identity.document()
    if (
        approval.operation != "clockify_post" or approval.period_id != identity.period_id
        or approval.workspace_id != identity.workspace_id or approval.member_id != identity.member_id
        or _timestamp(approval.period_start, "post approval period_start") != identity.since.astimezone(timezone.utc)
        or _timestamp(approval.period_end, "post approval period_end") != identity.until.astimezone(timezone.utc)
    ):
        raise PublicationGateError("Clockify post approval target does not match the manifest")
    _approved_manifest_prefix(identity, history, approval)
    for event in verified_events:
        if (
            event.operation != "clockify_post" or event.operation_identity != expected_operation
            or event.period_id != identity.period_id or event.workspace_id != identity.workspace_id
            or event.member_id != identity.member_id
        ):
            raise PublicationGateError("Clockify post event target does not match the manifest")
    if _canonical(supplied_post_events) != _canonical(derived):
        raise PublicationGateError("Clockify post receipt post_events do not match verified history")
    return derived


def _new_contract(
    manifest: reconciliation_manifest.ReconciliationManifest, post_receipt: Mapping[str, object],
    api_readback: clockify_period_readback.ClockifyPeriodReadback,
    shared_report_readback: clockify_period_readback.ClockifyPeriodReadback,
    quote: clockify_currency.FxQuoteReceipt, summary: clockify_currency.CurrencySummary,
    *, report_target: str, slack_target: str,
) -> PublicationContract:
    native, usd, total = _contract_currency(shared_report_readback.native_costs, summary)
    identity = manifest.identity
    period = identity.document()
    unsigned = {
        "schema_version": SCHEMA_VERSION, "state": PREPARED_STATE, "period_id": identity.period_id,
        "workspace_id": identity.workspace_id, "member_id": identity.member_id, "timezone": identity.timezone,
        "period_start": period["since_utc"], "period_end": period["until_utc"], "revision": identity.revision,
        "manifest_digest": manifest.manifest_digest, "event_history_digest": manifest.events_digest,
        "post_receipt_digest": _digest(post_receipt), "api_readback_digest": api_readback.digest,
        "shared_report_readback_digest": shared_report_readback.digest,
        "api_readback_refreshed_at": _utc(api_readback.refreshed_at),
        "shared_report_readback_refreshed_at": _utc(shared_report_readback.refreshed_at),
        "duration_seconds": shared_report_readback.duration_seconds,
        "native_buckets": {code: format(amount, "f") for code, amount in sorted(native.items())},
        "usd_buckets": {code: format(amount, "f") for code, amount in sorted(usd.items())},
        "usd_equivalent_total": format(total, "f"), "fx_quote": quote.to_dict(),
        "report_target": report_target, "slack_target": slack_target,
        "idempotency_key": publication_idempotency_key(identity),
    }
    return PublicationContract(
        period_id=identity.period_id, workspace_id=identity.workspace_id, member_id=identity.member_id, timezone=identity.timezone,
        period_start=period["since_utc"], period_end=period["until_utc"], revision=identity.revision,
        manifest_digest=manifest.manifest_digest, event_history_digest=manifest.events_digest,
        post_receipt_digest=unsigned["post_receipt_digest"], api_readback_digest=api_readback.digest,
        shared_report_readback_digest=shared_report_readback.digest,
        api_readback_refreshed_at=unsigned["api_readback_refreshed_at"],
        shared_report_readback_refreshed_at=unsigned["shared_report_readback_refreshed_at"],
        duration_seconds=shared_report_readback.duration_seconds, native_buckets=native, usd_buckets=usd, usd_equivalent_total=total,
        fx_quote=quote, report_target=report_target, slack_target=slack_target,
        idempotency_key=unsigned["idempotency_key"], contract_digest=_digest(unsigned),
    )


def prepare_publication(
    manifest: reconciliation_manifest.ReconciliationManifest | Mapping[str, object], *, events: Path | str,
    approval_receipt_id: str, approval_events: Path | str, post_events: Path | str,
    post_receipt: Mapping[str, object],
    api_readback: clockify_period_readback.ClockifyPeriodReadback | Mapping[str, object],
    shared_report_readback: clockify_period_readback.ClockifyPeriodReadback | Mapping[str, object],
    currency_summary: clockify_currency.CurrencySummary | Mapping[str, object] | None = None,
    fx_quote: clockify_currency.FxQuoteReceipt | Mapping[str, object] | None = None,
    quality: Mapping[str, object] | None = None, replay: Mapping[str, object] | None = None,
    coverage: Mapping[str, object] | None = None, slack_target: str | None = None,
    now: datetime | None = None,
) -> PublicationContract:
    """Produce a complete immutable prepared contract without external mutation."""
    check_now = now or datetime.now(timezone.utc)
    if check_now.tzinfo is None or check_now.utcoffset() is None:
        raise PublicationGateError("publication clock must include a timezone")
    ready_manifest = _verified_manifest(manifest, events)
    try:
        history = reconciliation_manifest.CoordinatorEventStore(Path(events)).verify(ready_manifest.identity)
    except reconciliation_manifest.ManifestError as exc:
        raise PublicationGateError(f"period event history is invalid: {exc}") from exc
    _bound_input_artifacts(ready_manifest, (Path(approval_events), Path(post_events)))
    _require_bound_evidence(ready_manifest, coverage, "coverage")
    required_slices = _require_coverage(coverage, ready_manifest.identity, history)
    _require_slices(ready_manifest, required_slices)
    _require_bound_evidence(ready_manifest, quality, "quality")
    _require_bound_evidence(ready_manifest, replay, "replay")
    _require_pass(quality, "quality")
    _require_pass(replay, "replay")
    api = _readback(api_readback, ready_manifest.identity, evidence_kind="ledger", now=check_now)
    report = _readback(shared_report_readback, ready_manifest.identity, evidence_kind="report", now=check_now)
    _require_bound_evidence(ready_manifest, api.to_dict(), "Clockify API readback")
    _require_bound_evidence(ready_manifest, report.to_dict(), "shared-report readback")
    derived_post_events = _require_post_provenance(
        post_receipt, approval_receipt_id=approval_receipt_id, approval_events=approval_events,
        post_events=post_events, identity=ready_manifest.identity, history=history, now=check_now,
    )
    _require_bound_evidence(ready_manifest, post_receipt, "post receipt")
    if ready_manifest.identity.since < ready_manifest.identity.until and not derived_post_events.get("entries"):
        raise PublicationGateError("nonzero period has no terminal Clockify post entries")
    try:
        clockify_period_readback.verify_readback(api, report, post_receipt=derived_post_events)
    except clockify_period_readback.ClockifyReadbackError as exc:
        raise PublicationGateError(f"Clockify readbacks do not verify: {exc}") from exc
    if not isinstance(report.request_receipt, Mapping):
        raise PublicationGateError("shared-report request receipt is required")
    report_target = "shared-report:" + _text(report.request_receipt.get("shared_report_id"), "shared-report ID")
    slack = _text(slack_target, "slack_target")
    quote = _quote_from_document(fx_quote)
    _require_bound_evidence(ready_manifest, quote.to_dict(), "FX quote")
    clock = check_now.astimezone(ZoneInfo(ready_manifest.identity.timezone))
    try:
        expected_summary = clockify_currency.convert_native_buckets(
            report.native_costs, quote, publication_date=clock.date(),
        )
    except clockify_currency.CurrencyContractError as exc:
        raise PublicationGateError(f"currency evidence is invalid: {exc}") from exc
    summary = expected_summary if currency_summary is None else _summary_from_value(currency_summary, quote, clock.date())
    if summary != expected_summary:
        raise PublicationGateError("currency summary does not match Clockify readback and FX quote")
    return _new_contract(
        ready_manifest, post_receipt, api, report, quote, summary,
        report_target=report_target, slack_target=slack,
    )


def _approval(value: object) -> Mapping[str, object]:
    required = {
        "schema_version", "approval_id", "approver", "approved_at", "expires_at", "operations", "report_target", "slack_target",
        "period_id", "workspace_id", "member_id", "period_start", "period_end", "revision", "contract_digest", "idempotency_key",
    }
    if not isinstance(value, Mapping) or set(value) != required or value.get("schema_version") != "publication-approval/v1":
        raise PublicationGateError("publication approval fields are invalid")
    return value


def authorize_publication(
    prepared: PublicationContract | Mapping[str, object], publication_approval: Mapping[str, object], *, now: datetime,
) -> AuthorizedPublication:
    """Bind a separate, still-valid publication approval to one prepared contract."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise PublicationGateError("publication authorization clock must include a timezone")
    document = prepared.document() if isinstance(prepared, PublicationContract) else prepared
    contract = PublicationContract.from_document(document)
    try:
        publication_date = now.astimezone(ZoneInfo(contract.timezone)).date()
        clockify_currency.convert_native_buckets(
            contract.native_buckets, contract.fx_quote, publication_date=publication_date,
        )
    except (ZoneInfoNotFoundError, clockify_currency.CurrencyContractError) as exc:
        raise PublicationGateError(f"publication currency evidence is invalid: {exc}") from exc
    now_utc = now.astimezone(timezone.utc)
    if (
        _timestamp(contract.api_readback_refreshed_at, "api_readback_refreshed_at") > now_utc
        or _timestamp(contract.shared_report_readback_refreshed_at, "shared_report_readback_refreshed_at") > now_utc
    ):
        raise PublicationGateError("prepared readback freshness is in the future")
    approval = _approval(publication_approval)
    approved_at = _timestamp(approval["approved_at"], "publication approval timestamp")
    expires_at = _timestamp(approval["expires_at"], "publication approval expiry")
    if expires_at <= approved_at or expires_at <= now_utc:
        raise PublicationGateError("publication approval is expired")
    if tuple(approval["operations"]) != OPERATIONS:
        raise PublicationGateError("publication approval operations do not name the report and Slack bundle")
    expected = {
        "report_target": contract.report_target, "slack_target": contract.slack_target, "period_id": contract.period_id,
        "workspace_id": contract.workspace_id, "member_id": contract.member_id, "period_start": contract.period_start,
        "period_end": contract.period_end, "revision": contract.revision, "contract_digest": contract.contract_digest,
        "idempotency_key": contract.idempotency_key,
    }
    for field, required_value in expected.items():
        if approval[field] != required_value:
            raise PublicationGateError(f"publication approval {field.replace('_', ' ')} does not match the prepared contract")
    approval_digest = _digest(approval)
    unsigned = {
        "schema_version": SCHEMA_VERSION, "state": AUTHORIZED_STATE, "contract": contract.document(),
        "contract_digest": contract.contract_digest, "approval_id": approval["approval_id"], "approval_digest": approval_digest,
        "approver": approval["approver"], "expires_at": _utc(expires_at), "operations": list(OPERATIONS),
        "idempotency_key": contract.idempotency_key,
    }
    return AuthorizedPublication(
        contract=contract, approval_id=_text(approval["approval_id"], "approval_id"), approval_digest=approval_digest,
        approver=_text(approval["approver"], "approver"), expires_at=unsigned["expires_at"], operations=OPERATIONS,
        authorization_digest=_digest(unsigned),
    )


def _bound_input_artifacts(manifest: reconciliation_manifest.ReconciliationManifest, paths: Sequence[Path]) -> None:
    expected = {str(reference["path"]): str(reference["digest"]) for reference in manifest.artifacts}
    for path in paths:
        resolved = path.resolve()
        try:
            actual = "sha256:" + hashlib.sha256(resolved.read_bytes()).hexdigest()
        except OSError as exc:
            raise PublicationGateError("publication input artifact is unavailable") from exc
        if expected.get(str(resolved)) != actual:
            raise PublicationGateError("publication input artifact is absent from or drifted from the manifest")


def _output(path: Path, document: Mapping[str, object]) -> None:
    destination = Path(path).expanduser()
    root = destination.parent.resolve()
    root.mkdir(parents=True, exist_ok=True)
    try:
        safe_path = reconciliation_manifest._path_below(root, destination)
        reconciliation_manifest._write_canonical(root, safe_path, document)
    except reconciliation_manifest.ManifestError as exc:
        raise PublicationGateError(f"publication output is unsafe: {exc}") from exc


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--manifest", type=Path, required=True)
    prepare.add_argument("--events", type=Path, required=True)
    prepare.add_argument("--post-receipt", type=Path, required=True)
    prepare.add_argument("--approval-receipt-id", required=True)
    prepare.add_argument("--approval-events", type=Path, required=True)
    prepare.add_argument("--post-events", type=Path, required=True)
    prepare.add_argument("--api-readback", type=Path, required=True)
    prepare.add_argument("--shared-report-readback", type=Path, required=True)
    prepare.add_argument("--fx-quote", type=Path, required=True)
    prepare.add_argument("--quality", type=Path, required=True)
    prepare.add_argument("--replay", type=Path, required=True)
    prepare.add_argument("--coverage", type=Path, required=True)
    prepare.add_argument("--slack-target", required=True)
    prepare.add_argument("--output", type=Path, required=True)
    authorize = commands.add_parser("authorize")
    authorize.add_argument("--prepared", type=Path, required=True)
    authorize.add_argument("--approval", type=Path, required=True)
    authorize.add_argument("--output", type=Path, required=True)
    return parser.parse_args(argv)


def run(args: argparse.Namespace, *, now: datetime | None = None) -> int:
    if args.command == "prepare":
        manifest = _manifest(_load_json(args.manifest, "period manifest"))
        input_paths = (
            args.post_receipt, args.approval_events, args.post_events, args.api_readback,
            args.shared_report_readback, args.fx_quote, args.quality, args.replay, args.coverage,
        )
        _bound_input_artifacts(manifest, input_paths)
        contract = prepare_publication(
            manifest, events=args.events, approval_receipt_id=args.approval_receipt_id,
            approval_events=args.approval_events, post_events=args.post_events,
            post_receipt=_load_json(args.post_receipt, "post receipt"),
            api_readback=_load_json(args.api_readback, "Clockify API readback"),
            shared_report_readback=_load_json(args.shared_report_readback, "shared-report readback"),
            fx_quote=_load_json(args.fx_quote, "FX quote"), quality=_load_json(args.quality, "quality"),
            replay=_load_json(args.replay, "replay"), coverage=_load_json(args.coverage, "coverage"),
            slack_target=args.slack_target, now=now,
        )
        _output(args.output, contract.document())
        print(_canonical({"state": contract.state, "contract_digest": contract.contract_digest}))
        return 0
    prepared = PublicationContract.from_document(_load_json(args.prepared, "prepared publication"))
    authorized = authorize_publication(prepared, _load_json(args.approval, "publication approval"), now=now or datetime.now(timezone.utc))
    _output(args.output, authorized.document())
    print(_canonical({"state": authorized.state, "authorization_digest": authorized.authorization_digest}))
    return 0


def main(argv: Sequence[str] | None = None, *, now: datetime | None = None) -> int:
    try:
        return run(parse_args(argv), now=now)
    except (PublicationGateError, OSError) as exc:
        print(f"clockify publication gate: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
