#!/usr/bin/env python3
"""Immutable approval and append-only Clockify posting receipt contracts."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping


APPROVAL_SCHEMA_VERSION = "approval-receipt/v1"
POST_EVENT_SCHEMA_VERSION = "post-event/v1"
TERMINAL_DISPOSITIONS = {
    "created",
    "already_existing",
    "recovered_after_ambiguous_response",
    "interrupted",
}
POST_DISPOSITIONS = TERMINAL_DISPOSITIONS | {"planned"}
ARTIFACT_DIGEST_FIELDS = (
    "portfolio_digest",
    "quality_digest",
    "replay_digest",
    "routing_digest",
    "correction_log_digest",
    "coverage_digest",
)


class PostingReceiptError(ValueError):
    """Raised when immutable approval or posting evidence is invalid."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Mapping[str, Any]) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def derive_operation_identity(
    *, operation: str, period_id: str, workspace_id: str, member_id: str
) -> str:
    """Derive the only valid identity for an externally mutating operation."""
    target = {
        "operation": _required_text(operation, "operation"),
        "period_id": _required_text(period_id, "period_id"),
        "workspace_id": _required_text(workspace_id, "workspace_id"),
        "member_id": _required_text(member_id, "member_id"),
    }
    return "opid-" + hashlib.sha256(canonical_json(target).encode("utf-8")).hexdigest()


def _parse_timestamp(value: str, label: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise PostingReceiptError(f"{label} must be a timestamp")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise PostingReceiptError(f"{label} must be an ISO-8601 timestamp") from error
    if result.tzinfo is None:
        raise PostingReceiptError(f"{label} must include an offset")
    return result.astimezone(timezone.utc)


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PostingReceiptError(f"{label} is required")
    return value


def _digest_text(value: object, label: str) -> str:
    text = _required_text(value, label)
    if not text.startswith("sha256:"):
        raise PostingReceiptError(f"{label} must be a sha256 digest")
    return text


def _append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(canonical_json(record) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _anchor_path(path: Path) -> Path:
    return path.with_name(path.name + ".anchor.jsonl")


def _pending_path(path: Path) -> Path:
    return path.with_name(path.name + ".pending.json")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    output: list[dict[str, Any]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line:
            raise PostingReceiptError(f"blank receipt ledger line {line_number}")
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as error:
            raise PostingReceiptError(f"invalid receipt ledger JSON at line {line_number}") from error
        if not isinstance(raw, dict):
            raise PostingReceiptError(f"receipt ledger line {line_number} must be an object")
        output.append(raw)
    return output


def _verify_chain(records: list[dict[str, Any]], schema_version: str) -> None:
    previous: str | None = None
    for sequence, record in enumerate(records):
        if record.get("schema_version") != schema_version or record.get("sequence") != sequence:
            raise PostingReceiptError("receipt ledger integrity sequence failure")
        if record.get("previous_digest") != previous:
            raise PostingReceiptError("receipt ledger integrity predecessor failure")
        payload = {key: value for key, value in record.items() if key != "event_digest"}
        expected = _digest(payload)
        if record.get("event_digest") != expected:
            raise PostingReceiptError("receipt ledger integrity digest failure")
        previous = expected


def _verified_anchors(path: Path, schema_version: str) -> list[dict[str, Any]]:
    anchors = _read_jsonl(_anchor_path(path))
    previous: str | None = None
    for sequence, anchor in enumerate(anchors):
        payload = {key: value for key, value in anchor.items() if key != "anchor_digest"}
        expected = _digest(payload)
        if (
            set(anchor) != {
                "anchor_schema_version", "ledger_schema_version", "sequence", "previous_anchor_digest",
                "ledger_count", "ledger_head", "anchor_digest",
            }
            or anchor.get("anchor_schema_version") != "receipt-ledger-anchor/v1"
            or anchor.get("ledger_schema_version") != schema_version
            or anchor.get("sequence") != sequence
            or anchor.get("previous_anchor_digest") != previous
            or not isinstance(anchor.get("ledger_count"), int)
            or anchor.get("ledger_count") < 1
            or not isinstance(anchor.get("ledger_head"), str)
            or anchor.get("anchor_digest") != expected
        ):
            raise PostingReceiptError("receipt ledger anchor integrity failure")
        previous = expected
    return anchors


def _verify_anchor(path: Path, records: list[dict[str, Any]], schema_version: str) -> None:
    anchors = _verified_anchors(path, schema_version)
    if not anchors:
        if records:
            raise PostingReceiptError("receipt ledger anchor is missing")
        return
    latest = anchors[-1]
    actual_head = records[-1]["event_digest"] if records else None
    if latest["ledger_count"] != len(records) or latest["ledger_head"] != actual_head:
        raise PostingReceiptError("receipt ledger anchor does not match ledger head")


def _append_anchor(path: Path, records: list[dict[str, Any]], schema_version: str) -> None:
    anchors = _verified_anchors(path, schema_version)
    previous = anchors[-1]["anchor_digest"] if anchors else None
    record: dict[str, Any] = {
        "anchor_schema_version": "receipt-ledger-anchor/v1",
        "ledger_schema_version": schema_version,
        "sequence": len(anchors),
        "previous_anchor_digest": previous,
        "ledger_count": len(records),
        "ledger_head": records[-1]["event_digest"],
    }
    record["anchor_digest"] = _digest(record)
    _append_jsonl(_anchor_path(path), record)


def _write_pending(path: Path, pending: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(canonical_json(pending) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(path)
    except FileExistsError as error:
        raise PostingReceiptError("pending receipt transaction already exists") from error


def _clear_pending(path: Path) -> None:
    if not path.exists():
        return
    path.unlink()
    _fsync_directory(path)


def _load_pending(path: Path, schema_version: str) -> dict[str, Any] | None:
    pending_path = _pending_path(path)
    if not pending_path.exists():
        return None
    try:
        value = json.loads(pending_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PostingReceiptError("pending receipt transaction is invalid") from error
    if not isinstance(value, dict):
        raise PostingReceiptError("pending receipt transaction is invalid")
    required = {
        "pending_schema_version", "ledger_schema_version", "base_count", "base_head", "record",
        "expected_head", "pending_digest",
    }
    payload = {key: item for key, item in value.items() if key != "pending_digest"}
    if (
        set(value) != required
        or value.get("pending_schema_version") != "receipt-ledger-pending/v1"
        or value.get("ledger_schema_version") != schema_version
        or not isinstance(value.get("base_count"), int)
        or isinstance(value.get("base_count"), bool)
        or value["base_count"] < 0
        or value.get("base_head") is not None and not isinstance(value.get("base_head"), str)
        or not isinstance(value.get("record"), dict)
        or not isinstance(value.get("expected_head"), str)
        or value.get("pending_digest") != _digest(payload)
    ):
        raise PostingReceiptError("pending receipt transaction is invalid")
    record = value["record"]
    if (
        record.get("schema_version") != schema_version
        or record.get("sequence") != value["base_count"]
        or record.get("previous_digest") != value["base_head"]
        or record.get("event_digest") != value["expected_head"]
        or record.get("event_digest") != _digest(
            {key: item for key, item in record.items() if key != "event_digest"}
        )
    ):
        raise PostingReceiptError("pending receipt transaction contradicts its record")
    return value


def _anchor_matches(anchors: list[dict[str, Any]], count: int, head: str | None) -> bool:
    if count == 0:
        return not anchors and head is None
    return bool(anchors) and anchors[-1].get("ledger_count") == count and anchors[-1].get("ledger_head") == head


def _recover_pending(path: Path, schema_version: str) -> None:
    pending = _load_pending(path, schema_version)
    if pending is None:
        return
    records = _read_jsonl(path)
    _verify_chain(records, schema_version)
    anchors = _verified_anchors(path, schema_version)
    base_count = pending["base_count"]
    base_head = pending["base_head"]
    expected_head = pending["expected_head"]
    expected_record = pending["record"]
    primary_is_base = len(records) == base_count and (
        (records[-1]["event_digest"] if records else None) == base_head
    )
    primary_is_extended = len(records) == base_count + 1 and (
        records[-1]["event_digest"] if records else None
    ) == expected_head and canonical_json(records[-1]) == canonical_json(expected_record)
    anchor_is_base = _anchor_matches(anchors, base_count, base_head)
    anchor_is_extended = _anchor_matches(anchors, base_count + 1, expected_head)
    if primary_is_base and anchor_is_base:
        candidate_records = records + [expected_record]
    elif primary_is_extended and (anchor_is_base or anchor_is_extended):
        candidate_records = records
    else:
        raise PostingReceiptError("pending receipt transaction contradicts ledger state")
    _validate_pending_semantics(candidate_records, schema_version)
    if primary_is_base and anchor_is_base:
        _append_jsonl(path, expected_record)
        records.append(expected_record)
        _append_anchor(path, records, schema_version)
    elif primary_is_extended and anchor_is_base:
        _append_anchor(path, records, schema_version)
    elif primary_is_extended and anchor_is_extended:
        pass
    _clear_pending(_pending_path(path))


def _append_transaction(
    path: Path, records: list[dict[str, Any]], record: dict[str, Any], schema_version: str
) -> None:
    pending: dict[str, Any] = {
        "pending_schema_version": "receipt-ledger-pending/v1",
        "ledger_schema_version": schema_version,
        "base_count": len(records),
        "base_head": records[-1]["event_digest"] if records else None,
        "record": record,
        "expected_head": record["event_digest"],
    }
    pending["pending_digest"] = _digest(pending)
    _write_pending(_pending_path(path), pending)
    _append_jsonl(path, record)
    _append_anchor(path, records + [record], schema_version)
    _clear_pending(_pending_path(path))


@dataclass(frozen=True)
class ApprovalReceipt:
    approval_id: str
    approver: str
    approved_at: str
    expires_at: str
    operation: str
    operation_identity: str
    period_id: str
    period_start: str
    period_end: str
    workspace_id: str
    member_id: str
    portfolio_digest: str
    quality_digest: str
    replay_digest: str
    routing_digest: str
    correction_log_digest: str
    coverage_digest: str
    residual_exception_digest: str
    single_use: bool = True

    @property
    def artifact_digests(self) -> dict[str, str]:
        return {field: getattr(self, field) for field in ARTIFACT_DIGEST_FIELDS}

    def document(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_document(cls, value: Mapping[str, Any]) -> "ApprovalReceipt":
        expected = {field.name for field in cls.__dataclass_fields__.values()}
        if set(value) != expected:
            raise PostingReceiptError("approval receipt fields are invalid")
        return cls(**dict(value))

    def validate(self) -> None:
        for field in (
            "approval_id", "approver", "operation", "operation_identity", "period_id",
            "workspace_id", "member_id",
        ):
            _required_text(getattr(self, field), field)
        approved_at = _parse_timestamp(self.approved_at, "approved_at")
        expires_at = _parse_timestamp(self.expires_at, "expires_at")
        start = _parse_timestamp(self.period_start, "period_start")
        end = _parse_timestamp(self.period_end, "period_end")
        if expires_at <= approved_at:
            raise PostingReceiptError("approval expiry must follow approval")
        if end <= start:
            raise PostingReceiptError("period end must follow period start")
        for field in ARTIFACT_DIGEST_FIELDS + ("residual_exception_digest",):
            _digest_text(getattr(self, field), field)
        if not isinstance(self.single_use, bool):
            raise PostingReceiptError("single_use must be a boolean")
        if self.operation_identity != derive_operation_identity(
            operation=self.operation,
            period_id=self.period_id,
            workspace_id=self.workspace_id,
            member_id=self.member_id,
        ):
            raise PostingReceiptError("approval operation identity does not bind its operation and target")


def _validate_approval_records(records: list[dict[str, Any]]) -> None:
    approvals: dict[str, ApprovalReceipt] = {}
    consumed: set[str] = set()
    for record in records:
        record_type = record.get("record_type")
        if record_type == "approval":
            if set(record) != {
                "schema_version", "record_type", "sequence", "previous_digest", "receipt", "event_digest"
            }:
                raise PostingReceiptError("approval receipt record fields are invalid")
            receipt_value = record.get("receipt")
            if not isinstance(receipt_value, Mapping):
                raise PostingReceiptError("approval receipt record is invalid")
            receipt = ApprovalReceipt.from_document(receipt_value)
            receipt.validate()
            if receipt.approval_id in approvals:
                raise PostingReceiptError("approval receipt repeats approval_id")
            approvals[receipt.approval_id] = receipt
        elif record_type == "consumed":
            if set(record) != {
                "schema_version", "record_type", "sequence", "previous_digest", "approval_id",
                "operation_identity", "consumed_at", "event_digest",
            }:
                raise PostingReceiptError("approval consumption record fields are invalid")
            approval_id = _required_text(record.get("approval_id"), "approval_id")
            identity = _required_text(record.get("operation_identity"), "operation_identity")
            consumed_at = _parse_timestamp(record.get("consumed_at"), "consumed_at")
            receipt = approvals.get(approval_id)
            if receipt is None or identity != receipt.operation_identity:
                raise PostingReceiptError("approval consumption does not bind its approval")
            if approval_id in consumed:
                raise PostingReceiptError("approval receipt has duplicate consumption")
            if consumed_at >= _parse_timestamp(receipt.expires_at, "expires_at"):
                raise PostingReceiptError("approval consumption is expired")
            consumed.add(approval_id)
        else:
            raise PostingReceiptError("approval receipt record type is invalid")


class ApprovalReceiptStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def _records(self) -> list[dict[str, Any]]:
        _recover_pending(self.path, APPROVAL_SCHEMA_VERSION)
        records = _read_jsonl(self.path)
        _verify_chain(records, APPROVAL_SCHEMA_VERSION)
        _verify_anchor(self.path, records, APPROVAL_SCHEMA_VERSION)
        _validate_approval_records(records)
        return records

    def _state(self) -> tuple[dict[str, ApprovalReceipt], set[str]]:
        records = self._records()
        approvals: dict[str, ApprovalReceipt] = {}
        consumed: set[str] = set()
        for record in records:
            if record["record_type"] == "approval":
                receipt = ApprovalReceipt.from_document(record["receipt"])
                approvals[receipt.approval_id] = receipt
            else:
                consumed.add(str(record["approval_id"]))
        return approvals, consumed

    def append(self, receipt: ApprovalReceipt) -> None:
        receipt.validate()
        approvals, _ = self._state()
        if receipt.approval_id in approvals:
            raise PostingReceiptError("approval receipt repeats approval_id")
        records = _read_jsonl(self.path)
        record: dict[str, Any] = {
            "schema_version": APPROVAL_SCHEMA_VERSION,
            "record_type": "approval",
            "sequence": len(records),
            "previous_digest": records[-1]["event_digest"] if records else None,
            "receipt": receipt.document(),
        }
        record["event_digest"] = _digest(record)
        _append_transaction(self.path, records, record, APPROVAL_SCHEMA_VERSION)

    def require(self, receipt_id: str, *, operation_identity: str, now: datetime) -> ApprovalReceipt:
        if now.tzinfo is None:
            raise PostingReceiptError("approval check time must include an offset")
        approvals, consumed = self._state()
        receipt = approvals.get(receipt_id)
        if receipt is None:
            raise PostingReceiptError("approval receipt is missing")
        if receipt.operation_identity != operation_identity:
            raise PostingReceiptError("approval receipt operation identity does not match")
        if _parse_timestamp(receipt.expires_at, "expires_at") <= now.astimezone(timezone.utc):
            raise PostingReceiptError("approval receipt is expired")
        if receipt.single_use and receipt_id in consumed:
            raise PostingReceiptError("approval receipt is consumed")
        return receipt

    def consume(self, receipt_id: str, *, operation_identity: str, consumed_at: str) -> None:
        consumed_time = _parse_timestamp(consumed_at, "consumed_at")
        self.require(receipt_id, operation_identity=operation_identity, now=consumed_time)
        records = _read_jsonl(self.path)
        record: dict[str, Any] = {
            "schema_version": APPROVAL_SCHEMA_VERSION,
            "record_type": "consumed",
            "sequence": len(records),
            "previous_digest": records[-1]["event_digest"] if records else None,
            "approval_id": receipt_id,
            "operation_identity": operation_identity,
            "consumed_at": consumed_at,
        }
        record["event_digest"] = _digest(record)
        _append_transaction(self.path, records, record, APPROVAL_SCHEMA_VERSION)

    def verify(self) -> None:
        self._records()


@dataclass(frozen=True)
class PostEvent:
    disposition: str
    operation: str
    operation_identity: str
    period_id: str
    workspace_id: str
    member_id: str
    review_id: str
    segment_index: int
    recorded_at: str
    clockify_entry_id: str | None = None
    live_readback_digest: str | None = None

    def document(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_document(cls, value: Mapping[str, Any]) -> "PostEvent":
        expected = {field.name for field in cls.__dataclass_fields__.values()}
        if set(value) != expected:
            raise PostingReceiptError("post event fields are invalid")
        return cls(**dict(value))

    def validate(self) -> None:
        if self.disposition not in POST_DISPOSITIONS:
            raise PostingReceiptError("post event disposition is invalid")
        for field in ("operation", "operation_identity", "period_id", "workspace_id", "member_id", "review_id"):
            _required_text(getattr(self, field), field)
        if not isinstance(self.segment_index, int) or isinstance(self.segment_index, bool) or self.segment_index < 0:
            raise PostingReceiptError("segment_index must be a non-negative integer")
        _parse_timestamp(self.recorded_at, "recorded_at")
        if self.disposition in {"created", "recovered_after_ambiguous_response"}:
            _required_text(self.clockify_entry_id, "Clockify entry")
            _digest_text(self.live_readback_digest, "live readback")
        elif self.disposition == "already_existing":
            _required_text(self.clockify_entry_id, "Clockify entry")
            if self.live_readback_digest is not None:
                _digest_text(self.live_readback_digest, "live readback")
        elif self.disposition in {"planned", "interrupted"}:
            if self.clockify_entry_id is not None or self.live_readback_digest is not None:
                raise PostingReceiptError(f"{self.disposition} event cannot claim Clockify readback")
        if self.operation_identity != derive_operation_identity(
            operation=self.operation,
            period_id=self.period_id,
            workspace_id=self.workspace_id,
            member_id=self.member_id,
        ):
            raise PostingReceiptError("post event operation identity does not bind its operation and target")


def _validate_post_events(records: list[dict[str, Any]], *, complete: bool) -> tuple[PostEvent, ...]:
    events: list[PostEvent] = []
    target: tuple[str, str, str, str] | None = None
    planned: set[tuple[str, int]] = set()
    terminal: set[tuple[str, int]] = set()
    for record in records:
        payload = {key: value for key, value in record.items() if key not in {
            "schema_version", "sequence", "previous_digest", "event_digest"
        }}
        event = PostEvent.from_document(payload)
        event.validate()
        event_target = (event.operation_identity, event.period_id, event.workspace_id, event.member_id)
        if target is None:
            target = event_target
        elif event_target != target:
            if event.operation_identity != target[0]:
                raise PostingReceiptError("post event operation drift")
            raise PostingReceiptError("post event target drift")
        key = (event.review_id, event.segment_index)
        if event.disposition == "planned":
            if key in planned:
                raise PostingReceiptError("post event repeats planned entry")
            planned.add(key)
        else:
            if key not in planned:
                raise PostingReceiptError("post event terminal lacks planned entry")
            if key in terminal:
                raise PostingReceiptError("post event has duplicate terminal entry")
            terminal.add(key)
        events.append(event)
    if complete and planned != terminal:
        raise PostingReceiptError("post event history has an unterminated terminal entry")
    return tuple(events)


def _validate_pending_semantics(records: list[dict[str, Any]], schema_version: str) -> None:
    if schema_version == APPROVAL_SCHEMA_VERSION:
        _validate_approval_records(records)
    elif schema_version == POST_EVENT_SCHEMA_VERSION:
        _validate_post_events(records, complete=False)
    else:
        raise PostingReceiptError("pending receipt transaction has an unknown schema")


class PostEventStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def _events(self, *, complete: bool) -> tuple[PostEvent, ...]:
        _recover_pending(self.path, POST_EVENT_SCHEMA_VERSION)
        records = _read_jsonl(self.path)
        _verify_chain(records, POST_EVENT_SCHEMA_VERSION)
        _verify_anchor(self.path, records, POST_EVENT_SCHEMA_VERSION)
        return _validate_post_events(records, complete=complete)

    def append(self, event: PostEvent) -> None:
        event.validate()
        existing = self._events(complete=False)
        if existing:
            first = existing[0]
            if event.operation_identity != first.operation_identity:
                raise PostingReceiptError("post event operation drift")
            if (event.period_id, event.workspace_id, event.member_id) != (
                first.period_id, first.workspace_id, first.member_id
            ):
                raise PostingReceiptError("post event target drift")
        planned = {(value.review_id, value.segment_index) for value in existing if value.disposition == "planned"}
        terminal = {(value.review_id, value.segment_index) for value in existing if value.disposition != "planned"}
        key = (event.review_id, event.segment_index)
        if event.disposition == "planned" and key in planned:
            raise PostingReceiptError("post event repeats planned entry")
        if event.disposition != "planned" and (key not in planned or key in terminal):
            raise PostingReceiptError("post event has duplicate terminal entry")
        records = _read_jsonl(self.path)
        record: dict[str, Any] = {
            "schema_version": POST_EVENT_SCHEMA_VERSION,
            "sequence": len(records),
            "previous_digest": records[-1]["event_digest"] if records else None,
            **event.document(),
        }
        record["event_digest"] = _digest(record)
        _append_transaction(self.path, records, record, POST_EVENT_SCHEMA_VERSION)

    def verify(self) -> tuple[PostEvent, ...]:
        return self._events(complete=True)

    def derive_receipt(self, operation_identity: str) -> dict[str, Any]:
        events = self._events(complete=False)
        if events and events[0].operation_identity != operation_identity:
            raise PostingReceiptError("post event operation identity does not match")
        planned = [event for event in events if event.disposition == "planned"]
        terminal = {
            (event.review_id, event.segment_index): event
            for event in events if event.disposition != "planned"
        }
        entries: list[dict[str, Any]] = []
        for event in planned:
            outcome = terminal.get((event.review_id, event.segment_index))
            entries.append({
                "review_id": event.review_id,
                "segment_index": event.segment_index,
                "disposition": outcome.disposition if outcome else "interrupted",
                "clockify_entry_id": outcome.clockify_entry_id if outcome else None,
                "live_readback_digest": outcome.live_readback_digest if outcome else None,
            })
        records = _read_jsonl(self.path)
        return {
            "operation_identity": operation_identity,
            "entries": entries,
            "post_events_digest": _digest({"event_digests": [record["event_digest"] for record in records]}),
        }
