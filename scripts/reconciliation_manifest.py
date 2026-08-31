#!/usr/bin/env python3
"""Canonical reconciliation period identities and append-only event history."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone as utc_timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PERIOD_COMPATIBILITY_VERSION = "reconciliation-period/v1"
EVENT_COMPATIBILITY_VERSION = "reconciliation-event/v1"
ZERO_DIGEST = "sha256:" + "0" * 64
_DIGEST_PREFIX = "sha256:"
_PRIVATE_PAYLOAD_KEYS = frozenset({
    "api_key", "credential", "cursor", "raw_payload", "transcript",
})


class ManifestError(RuntimeError):
    """Raised when immutable coordinator state is invalid or unsafe."""


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(
            value, allow_nan=False, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ManifestError("value is not canonical JSON") from exc


def _digest(value: object) -> str:
    return _DIGEST_PREFIX + hashlib.sha256(_canonical(value)).hexdigest()


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{field} must be a non-empty string")
    return value


def _digest_text(value: object, field: str) -> str:
    value = _required_text(value, field)
    if not value.startswith(_DIGEST_PREFIX) or len(value) != len(_DIGEST_PREFIX) + 64:
        raise ManifestError(f"{field} must be a SHA-256 digest")
    if any(character not in "0123456789abcdef" for character in value[len(_DIGEST_PREFIX):]):
        raise ManifestError(f"{field} must be a SHA-256 digest")
    return value


def _utc_timestamp(value: datetime, field: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ManifestError(f"{field} must be timezone-aware")
    value = value.astimezone(utc_timezone.utc)
    suffix = value.strftime("%Y-%m-%dT%H:%M:%S")
    if value.microsecond:
        suffix += f".{value.microsecond:06d}"
    return suffix + "Z"


def _parse_timestamp(value: object, field: str) -> datetime:
    value = _required_text(value, field)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ManifestError(f"{field} is not an ISO-8601 timestamp") from exc
    if _utc_timestamp(parsed, field) != value:
        raise ManifestError(f"{field} must be a canonical UTC timestamp")
    return parsed.astimezone(utc_timezone.utc)


def _safe_payload(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ManifestError("event payload must be an object")

    def check(item: object) -> None:
        if isinstance(item, Mapping):
            for key, nested in item.items():
                if not isinstance(key, str):
                    raise ManifestError("event payload keys must be strings")
                if key.lower() in _PRIVATE_PAYLOAD_KEYS:
                    raise ManifestError(f"event payload contains prohibited private field: {key}")
                check(nested)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                check(nested)
        elif item is None or isinstance(item, (str, int, float, bool)):
            return
        else:
            raise ManifestError("event payload must contain JSON values")

    payload = dict(value)
    check(payload)
    _canonical(payload)
    return payload


@dataclass(frozen=True)
class ArtifactIdentity:
    path: Path
    schema_version: str
    compatibility_version: str
    digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ManifestError("artifact path must be absolute")
        _required_text(self.schema_version, "artifact schema_version")
        _required_text(self.compatibility_version, "artifact compatibility_version")
        _digest_text(self.digest, "artifact digest")

    def document(self) -> dict[str, object]:
        return {
            "path": str(self.path),
            "schema_version": self.schema_version,
            "compatibility_version": self.compatibility_version,
            "digest": self.digest,
        }


@dataclass(frozen=True)
class PeriodIdentity:
    member_id: str
    workspace_id: str
    timezone: str
    since: datetime
    until: datetime
    revision: int
    compatibility_version: str = PERIOD_COMPATIBILITY_VERSION

    def __post_init__(self) -> None:
        _required_text(self.member_id, "member_id")
        _required_text(self.workspace_id, "workspace_id")
        _required_text(self.timezone, "timezone")
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ManifestError("timezone is unsupported") from exc
        since = _parse_timestamp(_utc_timestamp(self.since, "since"), "since")
        until = _parse_timestamp(_utc_timestamp(self.until, "until"), "until")
        if since >= until:
            raise ManifestError("period interval must be half-open with since before until")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise ManifestError("revision must be an integer of at least one")
        if self.compatibility_version != PERIOD_COMPATIBILITY_VERSION:
            raise ManifestError("unsupported period compatibility version")

    def document(self) -> dict[str, object]:
        return {
            "compatibility_version": self.compatibility_version,
            "member_id": self.member_id,
            "workspace_id": self.workspace_id,
            "timezone": self.timezone,
            "since_utc": _utc_timestamp(self.since, "since"),
            "until_utc": _utc_timestamp(self.until, "until"),
            "revision": self.revision,
        }

    @property
    def period_id(self) -> str:
        return "rperiod-" + _digest(self.document())[len(_DIGEST_PREFIX):]


def _event_document(
    sequence: int,
    period_id: str,
    event_type: str,
    payload: Mapping[str, object],
    previous_digest: str,
    occurred_at: datetime,
) -> dict[str, object]:
    unsigned = {
        "sequence": sequence,
        "period_id": period_id,
        "event_type": event_type,
        "payload": dict(payload),
        "previous_digest": previous_digest,
        "occurred_at": _utc_timestamp(occurred_at, "occurred_at"),
    }
    return {**unsigned, "event_digest": _digest(unsigned)}


@dataclass(frozen=True)
class CoordinatorEvent:
    sequence: int
    period_id: str
    event_type: str
    payload: dict[str, object]
    previous_digest: str
    occurred_at: datetime
    event_digest: str

    @classmethod
    def from_document(cls, document: object) -> "CoordinatorEvent":
        if not isinstance(document, Mapping):
            raise ManifestError("event line must contain an object")
        expected = {
            "sequence", "period_id", "event_type", "payload", "previous_digest",
            "occurred_at", "event_digest",
        }
        if set(document) != expected:
            raise ManifestError("event line has unexpected fields")
        sequence = document["sequence"]
        if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
            raise ManifestError("event sequence must be a positive integer")
        period_id = _required_text(document["period_id"], "event period_id")
        event_type = _required_text(document["event_type"], "event type")
        payload = _safe_payload(document["payload"])
        previous_digest = _digest_text(document["previous_digest"], "previous_digest")
        occurred_at = _parse_timestamp(document["occurred_at"], "occurred_at")
        event_digest = _digest_text(document["event_digest"], "event_digest")
        expected_digest = _digest({
            "sequence": sequence,
            "period_id": period_id,
            "event_type": event_type,
            "payload": payload,
            "previous_digest": previous_digest,
            "occurred_at": _utc_timestamp(occurred_at, "occurred_at"),
        })
        if event_digest != expected_digest:
            raise ManifestError("event digest does not match event content")
        return cls(sequence, period_id, event_type, payload, previous_digest, occurred_at, event_digest)

    def document(self) -> dict[str, object]:
        return _event_document(
            self.sequence, self.period_id, self.event_type, self.payload,
            self.previous_digest, self.occurred_at,
        )


class CoordinatorEventStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def append(
        self,
        identity: PeriodIdentity,
        event_type: str,
        payload: Mapping[str, object],
        *,
        occurred_at: datetime,
    ) -> CoordinatorEvent:
        if not isinstance(identity, PeriodIdentity):
            raise ManifestError("identity must be a PeriodIdentity")
        event_type = _required_text(event_type, "event type")
        payload = _safe_payload(payload)
        _utc_timestamp(occurred_at, "occurred_at")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            events = self._read_from_descriptor(descriptor, identity)
            previous_digest = events[-1].event_digest if events else ZERO_DIGEST
            event = CoordinatorEvent.from_document(_event_document(
                len(events) + 1, identity.period_id, event_type, payload,
                previous_digest, occurred_at,
            ))
            os.lseek(descriptor, 0, os.SEEK_END)
            os.write(descriptor, _canonical(event.document()) + b"\n")
            os.fsync(descriptor)
            return event
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def load(self, identity: PeriodIdentity) -> tuple[CoordinatorEvent, ...]:
        return self.verify(identity)

    def verify(self, identity: PeriodIdentity) -> tuple[CoordinatorEvent, ...]:
        if not isinstance(identity, PeriodIdentity):
            raise ManifestError("identity must be a PeriodIdentity")
        if not self.path.exists():
            return ()
        descriptor = os.open(self.path, os.O_RDONLY)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH)
            return self._read_from_descriptor(descriptor, identity)
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def _read_from_descriptor(
        self, descriptor: int, identity: PeriodIdentity
    ) -> tuple[CoordinatorEvent, ...]:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 65_536):
            chunks.append(chunk)
        raw = b"".join(chunks)
        if not raw:
            return ()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ManifestError("event history is not UTF-8") from exc
        if not text.endswith("\n"):
            raise ManifestError("event history has a truncated final line")
        events: list[CoordinatorEvent] = []
        for line in text.splitlines():
            if not line.strip():
                raise ManifestError("event history contains a blank line")
            try:
                document = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ManifestError("event history contains invalid JSON") from exc
            if _canonical(document).decode("utf-8") != line:
                raise ManifestError("event history line is not canonical JSON")
            events.append(CoordinatorEvent.from_document(document))
        self._verify_chain(identity, events)
        return tuple(events)

    @staticmethod
    def _verify_chain(identity: PeriodIdentity, events: list[CoordinatorEvent]) -> None:
        previous_digest = ZERO_DIGEST
        previous_time: datetime | None = None
        for expected_sequence, event in enumerate(events, start=1):
            if event.sequence != expected_sequence:
                raise ManifestError("event sequences must be contiguous from one")
            if event.period_id != identity.period_id:
                raise ManifestError("event period does not match requested identity")
            if event.previous_digest != previous_digest:
                raise ManifestError("event predecessor digest does not match")
            if previous_time is not None and event.occurred_at < previous_time:
                raise ManifestError("event timestamps must be monotonic")
            previous_digest = event.event_digest
            previous_time = event.occurred_at
