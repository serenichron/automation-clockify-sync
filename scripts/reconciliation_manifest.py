#!/usr/bin/env python3
"""Canonical reconciliation period identities and append-only event history."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone as utc_timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


PERIOD_COMPATIBILITY_VERSION = "reconciliation-period/v1"
EVENT_COMPATIBILITY_VERSION = "reconciliation-event/v1"
MANIFEST_COMPATIBILITY_VERSION = "reconciliation-manifest/v1"
ZERO_DIGEST = "sha256:" + "0" * 64
_DIGEST_PREFIX = "sha256:"
_PRIVATE_PAYLOAD_KEYS = frozenset({
    "api_key", "credential", "cursor", "raw_payload", "transcript",
})

ADVANCING_EVENTS = {
    "period_opened": "collecting",
    "collection_complete": "reconciling",
    "reconciliation_complete": "awaiting_review",
    "review_approved": "approved",
    "posting_started": "posting",
    "posting_complete": "verifying",
    "clockify_readback_verified": "verifying",
    "publication_prepared": "publication_prepared",
    "publication_authorized": "publication_authorized",
    "shared_report_verified": "publication_authorized",
    "publication_complete": "published",
}
BLOCKER_EVENTS = frozenset({
    "coverage_incomplete", "semantic_exceptions", "awaiting_approval",
    "post_interrupted", "readback_mismatch", "report_mismatch",
    "currency_quote_unavailable", "publication_deferred",
})
AUDIT_EVENTS = frozenset({
    "report_residual_resolved", "fathom_repair_complete", "coverage_limitation_approved",
})
KNOWN_EVENT_TYPES = frozenset(ADVANCING_EVENTS) | BLOCKER_EVENTS | AUDIT_EVENTS


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
            occurred = _parse_timestamp(_utc_timestamp(occurred_at, "occurred_at"), "occurred_at")
            if events and occurred < events[-1].occurred_at:
                raise ManifestError("event timestamps must be monotonic")
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


def _artifact_from_document(document: object) -> ArtifactIdentity:
    if not isinstance(document, Mapping) or set(document) != {
        "path", "schema_version", "compatibility_version", "digest",
    }:
        raise ManifestError("artifact reference has unexpected fields")
    return ArtifactIdentity(
        path=Path(_required_text(document["path"], "artifact path")),
        schema_version=_required_text(document["schema_version"], "artifact schema_version"),
        compatibility_version=_required_text(
            document["compatibility_version"], "artifact compatibility_version"
        ),
        digest=_digest_text(document["digest"], "artifact digest"),
    )


def _artifact_digest(path: Path) -> str:
    try:
        with path.open("rb") as source:
            digest = hashlib.sha256()
            while chunk := source.read(65_536):
                digest.update(chunk)
    except OSError as exc:
        raise ManifestError("artifact is unavailable") from exc
    return _DIGEST_PREFIX + digest.hexdigest()


def _verify_artifact_refs(value: object) -> tuple[ArtifactIdentity, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ManifestError("event artifacts must be a list")
    artifacts: list[ArtifactIdentity] = []
    for reference in value:
        artifact = _artifact_from_document(reference)
        if not artifact.path.is_file() or _artifact_digest(artifact.path) != artifact.digest:
            raise ManifestError("artifact digest mismatch")
        artifacts.append(artifact)
    return tuple(artifacts)


def _identity_from_document(document: object) -> PeriodIdentity:
    if not isinstance(document, Mapping) or set(document) != {
        "compatibility_version", "member_id", "workspace_id", "timezone", "since_utc", "until_utc", "revision",
    }:
        raise ManifestError("manifest period has unexpected fields")
    return PeriodIdentity(
        member_id=_required_text(document["member_id"], "member_id"),
        workspace_id=_required_text(document["workspace_id"], "workspace_id"),
        timezone=_required_text(document["timezone"], "timezone"),
        since=_parse_timestamp(document["since_utc"], "since_utc"),
        until=_parse_timestamp(document["until_utc"], "until_utc"),
        revision=document["revision"],
        compatibility_version=document["compatibility_version"],
    )


@dataclass(frozen=True)
class ReconciliationManifest:
    identity: PeriodIdentity
    state: str
    event_count: int
    events_digest: str
    artifacts: tuple[dict[str, object], ...]
    blockers: tuple[str, ...]
    manifest_digest: str
    compatibility_version: str = MANIFEST_COMPATIBILITY_VERSION

    def __post_init__(self) -> None:
        if self.compatibility_version != MANIFEST_COMPATIBILITY_VERSION:
            raise ManifestError("unsupported manifest compatibility version")
        if self.state not in {
            "collecting", "reconciling", "awaiting_review", "approved", "posting", "verifying",
            "publication_prepared", "publication_authorized", "published",
        }:
            raise ManifestError("manifest state is invalid")
        if isinstance(self.event_count, bool) or not isinstance(self.event_count, int) or self.event_count < 0:
            raise ManifestError("manifest event_count is invalid")
        _digest_text(self.events_digest, "manifest events_digest")
        _digest_text(self.manifest_digest, "manifest digest")
        for artifact in self.artifacts:
            _artifact_from_document(artifact)
        if tuple(sorted(set(self.blockers))) != self.blockers or any(event not in BLOCKER_EVENTS for event in self.blockers):
            raise ManifestError("manifest blockers are invalid")

    def _unsigned_document(self) -> dict[str, object]:
        return {
            "schema_version": MANIFEST_COMPATIBILITY_VERSION,
            "compatibility_version": self.compatibility_version,
            "period": self.identity.document(),
            "state": self.state,
            "event_count": self.event_count,
            "events_digest": self.events_digest,
            "artifacts": list(self.artifacts),
            "blockers": list(self.blockers),
        }

    def document(self) -> dict[str, object]:
        return {**self._unsigned_document(), "manifest_digest": self.manifest_digest}

    @classmethod
    def from_document(cls, document: object) -> "ReconciliationManifest":
        if not isinstance(document, Mapping) or set(document) != {
            "schema_version", "compatibility_version", "period", "state", "event_count", "events_digest",
            "artifacts", "blockers", "manifest_digest",
        }:
            raise ManifestError("manifest has unexpected fields")
        if document["schema_version"] != MANIFEST_COMPATIBILITY_VERSION:
            raise ManifestError("unsupported manifest schema version")
        artifacts = document["artifacts"]
        blockers = document["blockers"]
        if not isinstance(artifacts, list) or not isinstance(blockers, list) or not all(isinstance(item, str) for item in blockers):
            raise ManifestError("manifest artifacts or blockers are invalid")
        manifest = cls(
            identity=_identity_from_document(document["period"]),
            state=document["state"],
            event_count=document["event_count"],
            events_digest=document["events_digest"],
            artifacts=tuple(dict(item) for item in artifacts if isinstance(item, Mapping)),
            blockers=tuple(blockers),
            manifest_digest=_digest_text(document["manifest_digest"], "manifest digest"),
            compatibility_version=document["compatibility_version"],
        )
        if len(manifest.artifacts) != len(artifacts) or manifest.manifest_digest != _digest(manifest._unsigned_document()):
            raise ManifestError("manifest digest does not match manifest content")
        return manifest


def _manifest(
    identity: PeriodIdentity,
    state: str,
    events: Sequence[CoordinatorEvent],
    artifacts: Sequence[ArtifactIdentity],
    blockers: set[str],
) -> ReconciliationManifest:
    artifact_documents = tuple(
        artifact.document()
        for artifact in sorted(artifacts, key=lambda item: _canonical(item.document()))
    )
    unsigned = {
        "schema_version": MANIFEST_COMPATIBILITY_VERSION,
        "compatibility_version": MANIFEST_COMPATIBILITY_VERSION,
        "period": identity.document(),
        "state": state,
        "event_count": len(events),
        "events_digest": _digest([event.event_digest for event in events]),
        "artifacts": list(artifact_documents),
        "blockers": sorted(blockers),
    }
    return ReconciliationManifest(
        identity=identity,
        state=state,
        event_count=len(events),
        events_digest=unsigned["events_digest"],
        artifacts=artifact_documents,
        blockers=tuple(unsigned["blockers"]),
        manifest_digest=_digest(unsigned),
    )


class ReconciliationCoordinator:
    def __init__(self, identity: PeriodIdentity, store: CoordinatorEventStore):
        if not isinstance(identity, PeriodIdentity) or not isinstance(store, CoordinatorEventStore):
            raise ManifestError("coordinator requires a period identity and event store")
        self.identity = identity
        self.store = store

    def derive(self) -> ReconciliationManifest:
        events = self.store.verify(self.identity)
        state = "collecting"
        blockers: set[str] = set()
        artifacts: list[ArtifactIdentity] = []
        context = {"readback_verified": False, "authorization": None, "report_verified": False}
        for index, event in enumerate(events):
            artifacts.extend(_verify_artifact_refs(event.payload.get("artifacts")))
            state = self._apply_transition(state, event, index, context, blockers)
        unique_artifacts = { _canonical(artifact.document()): artifact for artifact in artifacts }
        return _manifest(self.identity, state, events, tuple(unique_artifacts.values()), blockers)

    @staticmethod
    def _apply_transition(
        state: str,
        event: CoordinatorEvent,
        index: int,
        context: dict[str, object],
        blockers: set[str],
    ) -> str:
        if event.event_type not in KNOWN_EVENT_TYPES:
            raise ManifestError("unknown reconciliation event type")
        if event.event_type in BLOCKER_EVENTS:
            blockers.add(event.event_type)
            return state
        if event.event_type in AUDIT_EVENTS:
            return state
        if event.event_type == "period_opened":
            if index != 0:
                raise ManifestError("period_opened is legal only as the first event")
            return state
        expected = {
            "collection_complete": "collecting",
            "reconciliation_complete": "reconciling",
            "review_approved": "awaiting_review",
            "posting_started": "approved",
            "posting_complete": "posting",
            "clockify_readback_verified": "verifying",
            "publication_prepared": "verifying",
            "publication_authorized": "publication_prepared",
            "shared_report_verified": "publication_authorized",
            "publication_complete": "publication_authorized",
        }[event.event_type]
        if state != expected:
            raise ManifestError(f"illegal transition from {state} using {event.event_type}")
        if event.event_type == "clockify_readback_verified":
            context["readback_verified"] = True
        elif event.event_type == "publication_prepared":
            if not context["readback_verified"]:
                raise ManifestError("publication_prepared requires verified Clockify readback")
        elif event.event_type == "publication_authorized":
            context["authorization"] = _publication_binding(event.payload)
        elif event.event_type == "shared_report_verified":
            if _publication_binding(event.payload) != context["authorization"] or not _verified_receipt(
                event.payload, "report_receipt", context["authorization"]
            ):
                raise ManifestError("shared report receipt does not bind the publication authorization")
            context["report_verified"] = True
        elif event.event_type == "publication_complete":
            if not context["report_verified"] or _publication_binding(event.payload) != context["authorization"]:
                raise ManifestError("publication_complete requires bound report and Slack receipts")
            if not _verified_receipt(event.payload, "slack_receipt", context["authorization"]):
                raise ManifestError("publication_complete requires bound report and Slack receipts")
        return ADVANCING_EVENTS[event.event_type]


def _publication_binding(payload: Mapping[str, object]) -> tuple[str, str]:
    return (
        _digest_text(payload.get("contract_digest"), "contract_digest"),
        _required_text(payload.get("idempotency_identity"), "idempotency_identity"),
    )


def _verified_receipt(
    payload: Mapping[str, object], key: str, binding: object,
) -> bool:
    receipt = payload.get(key)
    if not isinstance(receipt, Mapping) or receipt.get("status") != "verified":
        return False
    try:
        return _publication_binding(receipt) == binding
    except ManifestError:
        return False


def _path_below(root: Path, path: Path) -> Path:
    root = root.resolve()
    resolved = path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ManifestError("output path must remain below the private recovery root") from exc
    return resolved


def _write_canonical(path: Path, document: object) -> None:
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(descriptor, _canonical(document) + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)


def _routing_value(path: Path, field: str) -> str:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError("routing input is not valid JSON") from exc
    if not isinstance(document, Mapping):
        raise ManifestError("routing input is not an object")
    return _required_text(document.get(field), field)


def _parse_period_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ManifestError("period boundary is not ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ManifestError("period boundary must be timezone-aware")
    return parsed


def _load_manifest(path: Path) -> ReconciliationManifest:
    try:
        return ReconciliationManifest.from_document(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError("manifest is not valid JSON") from exc


def _verify_import_inventory(path: Path) -> None:
    if not path.exists():
        return
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ManifestError("imported artifacts inventory is not valid JSON") from exc
    if not isinstance(document, Mapping) or set(document) != {"artifacts"} or not isinstance(document["artifacts"], list):
        raise ManifestError("imported artifacts inventory is invalid")
    for entry in document["artifacts"]:
        if not isinstance(entry, Mapping) or set(entry) != {
            "kind", "path", "schema_version", "compatibility_version", "digest",
        }:
            raise ManifestError("imported artifact has unexpected fields")
        _required_text(entry["kind"], "artifact kind")
        _verify_artifact_refs([{key: entry[key] for key in entry if key != "kind"}])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--workspace-id-from-routing", type=Path, required=True)
    init.add_argument("--member-id-from-routing", type=Path, required=True)
    init.add_argument("--timezone", required=True)
    init.add_argument("--since", required=True)
    init.add_argument("--until", required=True)
    init.add_argument("--revision", type=int, required=True)
    init.add_argument("--events", type=Path, required=True)
    init.add_argument("--manifest", type=Path, required=True)
    init.add_argument("--dry-run", action="store_true")
    imported = commands.add_parser("import-artifacts")
    imported.add_argument("--events", type=Path, required=True)
    imported.add_argument("--manifest", type=Path, required=True)
    imported.add_argument("--diagnostic", type=Path, required=True)
    imported.add_argument("--discover-preserved-august", action="store_true")
    imported.add_argument("--output", type=Path, required=True)
    verify = commands.add_parser("verify")
    verify.add_argument("--events", type=Path, required=True)
    verify.add_argument("--manifest", type=Path, required=True)
    return parser


def run(args: argparse.Namespace) -> int:
    root = args.manifest.resolve().parent
    manifest_path = _path_below(root, args.manifest)
    events_path = _path_below(root, args.events)
    if args.command == "init":
        identity = PeriodIdentity(
            member_id=_routing_value(args.member_id_from_routing, "member_id"),
            workspace_id=_routing_value(args.workspace_id_from_routing, "workspace_id"),
            timezone=args.timezone,
            since=_parse_period_datetime(args.since),
            until=_parse_period_datetime(args.until),
            revision=args.revision,
        )
        if args.dry_run:
            print(identity.period_id)
            return 0
        if events_path.exists() or manifest_path.exists():
            raise ManifestError("period outputs already exist")
        root.mkdir(parents=True, exist_ok=True)
        store = CoordinatorEventStore(events_path)
        store.append(identity, "period_opened", {"revision": identity.revision}, occurred_at=datetime.now(utc_timezone.utc))
        _write_canonical(manifest_path, ReconciliationCoordinator(identity, store).derive().document())
        print(identity.period_id)
        return 0
    manifest = _load_manifest(manifest_path)
    coordinator = ReconciliationCoordinator(manifest.identity, CoordinatorEventStore(events_path))
    derived = coordinator.derive()
    if derived.document() != manifest.document():
        raise ManifestError("manifest does not match verified event history")
    if args.command == "import-artifacts":
        output_path = _path_below(root, args.output)
        try:
            diagnostic = json.loads(args.diagnostic.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ManifestError("diagnostic inventory is not valid JSON") from exc
        if not isinstance(diagnostic, Mapping) or set(diagnostic) != {"artifacts"} or not isinstance(diagnostic["artifacts"], list):
            raise ManifestError("diagnostic inventory is invalid")
        artifacts: list[dict[str, object]] = []
        for item in diagnostic["artifacts"]:
            if not isinstance(item, Mapping) or set(item) != {"kind", "path", "schema_version", "compatibility_version"}:
                raise ManifestError("diagnostic artifact has unexpected fields")
            identity = ArtifactIdentity(
                path=Path(_required_text(item["path"], "artifact path")).resolve(),
                schema_version=_required_text(item["schema_version"], "artifact schema_version"),
                compatibility_version=_required_text(item["compatibility_version"], "artifact compatibility_version"),
                digest=_artifact_digest(Path(_required_text(item["path"], "artifact path")).resolve()),
            )
            artifacts.append({"kind": _required_text(item["kind"], "artifact kind"), **identity.document()})
        _write_canonical(output_path, {"artifacts": sorted(artifacts, key=_canonical)})
        print(output_path)
        return 0
    _verify_import_inventory(root / "imported-artifacts.json")
    print(manifest.state)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return run(build_parser().parse_args(argv))
    except ManifestError as exc:
        print(str(exc), file=os.sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
