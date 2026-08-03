#!/usr/bin/env python3
"""Immutable, local evidence-ledger primitives for Clockify work accounting.

This module deliberately performs no network or Clockify operations.  It turns
already-collected snapshot data into content-addressed evidence observations.
The evidence timeline may overlap; future effort allocations are intentionally
outside this contract.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "evidence-ledger/v1"
EVENT_ID_PREFIX = "ev-"
MANIFEST_ID_PREFIX = "elm-"
VALID_SOURCE_STATUSES = frozenset({"complete", "partial", "unavailable", "excluded"})
LEGACY_ALIAS_KEYS = (
    "candidate_key",
    "review_item_id",
    "review_id",
    "id",
)


class FrozenDict(dict):
    """A JSON-serializable mapping that refuses all mutations."""

    def __init__(self, value: Mapping[str, Any] | None = None) -> None:
        dict.__init__(self, value or {})

    @staticmethod
    def _immutable(*_args: Any, **_kwargs: Any) -> None:
        raise TypeError("Evidence contracts are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable


def canonical_json(value: Any) -> str:
    """Serialize JSON-compatible data deterministically for stable digests."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_hex(value: Any) -> str:
    """Return the SHA-256 of a canonical JSON value or UTF-8 text."""
    encoded = value.encode("utf-8") if isinstance(value, str) else canonical_json(value).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _json_value(value: Any) -> Any:
    """Make a defensive immutable-ish JSON copy and reject non-JSON inputs."""
    def normalize(item: Any) -> Any:
        if isinstance(item, (dt.datetime, dt.date)):
            return item.isoformat()
        if isinstance(item, Mapping):
            return {
                str(key): normalize(nested)
                for key, nested in item.items()
                if not str(key).startswith("_")
            }
        if isinstance(item, (list, tuple)):
            return [normalize(nested) for nested in item]
        return item

    try:
        return json.loads(canonical_json(normalize(value)))
    except (TypeError, ValueError) as exc:
        raise ValueError("Evidence values must be JSON-compatible") from exc


def _compact_mapping(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if not value:
        return {}
    return {str(key): _json_value(item) for key, item in value.items() if item not in (None, "", [], {})}


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return FrozenDict({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


@dataclasses.dataclass(frozen=True)
class EvidenceEvent:
    """A content-addressed observation from one source, not an allocation."""

    source_type: str
    source_ref: Mapping[str, Any]
    observed_at: str | None = None
    raw_source_span: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    attributes: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    legacy_aliases: Mapping[str, str] = dataclasses.field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION
    evidence_id: str = ""

    def __post_init__(self) -> None:
        if not self.source_type or not isinstance(self.source_type, str):
            raise ValueError("Evidence event requires source_type")
        if not isinstance(self.source_ref, Mapping) or not self.source_ref:
            raise ValueError("Evidence event requires a non-empty source_ref")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"Unsupported evidence schema: {self.schema_version}")
        if not isinstance(self.raw_source_span, Mapping):
            raise ValueError("raw_source_span must be an object")
        if not isinstance(self.attributes, Mapping):
            raise ValueError("attributes must be an object")
        if not isinstance(self.legacy_aliases, Mapping):
            raise ValueError("legacy_aliases must be an object")
        document = self.document(include_id=False)
        expected = EVENT_ID_PREFIX + sha256_hex(document)
        if self.evidence_id and self.evidence_id != expected:
            raise ValueError("Evidence ID does not match event content")
        object.__setattr__(self, "evidence_id", expected)
        object.__setattr__(self, "source_ref", _freeze_json(_compact_mapping(self.source_ref)))
        object.__setattr__(self, "raw_source_span", _freeze_json(_compact_mapping(self.raw_source_span)))
        object.__setattr__(self, "attributes", _freeze_json(_compact_mapping(self.attributes)))
        aliases = {str(key): str(value) for key, value in self.legacy_aliases.items() if value not in (None, "")}
        object.__setattr__(self, "legacy_aliases", _freeze_json(dict(sorted(aliases.items()))))

    def document(self, *, include_id: bool = True) -> dict[str, Any]:
        document: dict[str, Any] = {
            "schema_version": self.schema_version,
            "source_type": self.source_type,
            "source_ref": _compact_mapping(self.source_ref),
            "observed_at": self.observed_at,
            "raw_source_span": _compact_mapping(self.raw_source_span),
            "attributes": _compact_mapping(self.attributes),
            "legacy_aliases": _compact_mapping(self.legacy_aliases),
        }
        if include_id:
            document["evidence_id"] = self.evidence_id
        return document

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "EvidenceEvent":
        return cls(
            source_type=str(document.get("source_type") or ""),
            source_ref=document.get("source_ref") or {},
            observed_at=document.get("observed_at"),
            raw_source_span=document.get("raw_source_span") or {},
            attributes=document.get("attributes") or {},
            legacy_aliases=document.get("legacy_aliases") or {},
            schema_version=str(document.get("schema_version") or ""),
            evidence_id=str(document.get("evidence_id") or ""),
        )


def evidence_event(
    source_type: str,
    source_ref: Mapping[str, Any],
    *,
    observed_at: str | None = None,
    raw_source_span: Mapping[str, Any] | None = None,
    attributes: Mapping[str, Any] | None = None,
    legacy_aliases: Mapping[str, str] | None = None,
) -> EvidenceEvent:
    """Convenient constructor for callers that have JSON snapshot records."""
    return EvidenceEvent(
        source_type=source_type,
        source_ref=source_ref,
        observed_at=observed_at,
        raw_source_span=raw_source_span or {},
        attributes=attributes or {},
        legacy_aliases=legacy_aliases or {},
    )


def event_digest(events: Iterable[EvidenceEvent]) -> str:
    """Digest an evidence set independent of collection order or exact duplicates."""
    unique: dict[str, dict[str, Any]] = {}
    for event in events:
        document = event.document()
        previous = unique.get(event.evidence_id)
        if previous is not None and previous != document:
            raise ValueError("Evidence ID collision with distinct content")
        unique[event.evidence_id] = document
    documents = list(unique.values())
    documents.sort(key=lambda document: document["evidence_id"])
    return sha256_hex(documents)


def _normal_source_inventory(source_inventory: Mapping[str, Mapping[str, Any]] | None) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for source, details in (source_inventory or {}).items():
        if not isinstance(details, Mapping):
            raise ValueError(f"Source inventory entry must be an object: {source}")
        status = str(details.get("status") or "")
        if status not in VALID_SOURCE_STATUSES:
            raise ValueError(f"Unsupported source status for {source}: {status}")
        item = {"status": status}
        for key in ("expected_count", "observed_count", "reason"):
            if key in details and details[key] not in (None, ""):
                item[key] = _json_value(details[key])
        normalized[str(source)] = item
    return dict(sorted(normalized.items()))


def source_completeness(source_inventory: Mapping[str, Mapping[str, Any]] | None) -> dict[str, Any]:
    """Summarize source availability without silently treating missing data as work."""
    inventory = _normal_source_inventory(source_inventory)
    incomplete: list[str] = []
    for source, details in inventory.items():
        if details["status"] not in {"complete", "excluded"}:
            incomplete.append(source)
            continue
        expected, observed = details.get("expected_count"), details.get("observed_count")
        if expected is not None and observed is not None and expected != observed:
            incomplete.append(source)
    return {
        "status": "complete" if not incomplete else "incomplete",
        "sources": inventory,
        "incomplete_sources": incomplete,
    }


def _collector_source_status(details: Mapping[str, Any]) -> tuple[str, str | None]:
    """Map collector status/complete fields into the ledger's fail-closed states."""
    raw_status = str(details.get("status") or "").casefold()
    complete = details.get("complete")
    if raw_status in {"ok", "available", "success", "complete"}:
        return ("complete" if complete is not False else "partial", None)
    if raw_status == "partial":
        return "partial", None
    if raw_status in {"excluded", "disabled", "not_requested"}:
        return "excluded", None
    if raw_status:
        return "unavailable", f"collector status: {raw_status}"
    if complete is True:
        return "complete", None
    if complete is False:
        return "partial", "collector marked source incomplete"
    return "partial", "collector status missing"


def _collector_count(details: Mapping[str, Any], record_keys: Sequence[str], count_keys: Sequence[str]) -> int | None:
    for key in record_keys:
        value = details.get(key)
        if isinstance(value, list):
            return len(value)
    for key in count_keys:
        value = details.get(key)
        if isinstance(value, int) and value >= 0:
            return value
    return None


def source_inventory_from_collector(snapshot: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Derive fail-closed source coverage from a full or compact collector snapshot.

    ``observed_count`` comes from materialized records when present, otherwise
    from the collector's count metadata. ``expected_count`` is retained only
    when the collector explicitly supplied one; a count is never fabricated.
    """
    if not isinstance(snapshot, Mapping):
        raise ValueError("Collector snapshot must be an object")
    groups = (
        ("clockify", snapshot.get("clockify"), ("entries",), ("entry_count",)),
        ("fathom", snapshot.get("fathom"), ("meetings",), ("meeting_count",)),
        ("multica_issues", snapshot.get("multica_issues"), ("issues",), ("issue_count",)),
    )
    inventory: dict[str, dict[str, Any]] = {}
    for name, value, record_keys, count_keys in groups:
        if not isinstance(value, Mapping):
            inventory[name] = {"status": "unavailable", "reason": "collector source missing"}
            continue
        status, reason = _collector_source_status(value)
        entry: dict[str, Any] = {"status": status}
        observed = _collector_count(value, record_keys, count_keys)
        if observed is not None:
            entry["observed_count"] = observed
        expected = value.get("expected_count")
        if isinstance(expected, int) and expected >= 0:
            entry["expected_count"] = expected
        if reason:
            entry["reason"] = reason
        inventory[name] = entry
    for machine in _records(snapshot.get("sessions")):
        machine_name = str(machine.get("machine") or "unknown")
        status, reason = _collector_source_status(machine)
        observed = sum(
            len(value)
            for key in ("codex_sessions", "claude_bursts", "hermes_sessions", "hermes_db_sessions")
            if isinstance((value := machine.get(key)), list)
        )
        entry = {"status": status, "observed_count": observed}
        expected = machine.get("expected_count")
        if isinstance(expected, int) and expected >= 0:
            entry["expected_count"] = expected
        if reason:
            entry["reason"] = reason
        inventory[f"sessions/{machine_name}"] = entry
        repository_status = str(machine.get("repository_evidence_status") or "").lower()
        repository_entry: dict[str, Any] = {
            "status": repository_status
            if repository_status in VALID_SOURCE_STATUSES
            else ("partial" if status != "complete" else "complete"),
            "observed_count": len(machine.get("repository_events", []))
            if isinstance(machine.get("repository_events"), list)
            else 0,
        }
        if machine.get("collector_contract") == "legacy_metadata_fallback":
            repository_entry.update(
                status="unavailable",
                reason="legacy remote collector cannot export repository evidence",
            )
        inventory[f"repositories/{machine_name}"] = repository_entry
    return _normal_source_inventory(inventory)


@dataclasses.dataclass(frozen=True)
class LedgerManifest:
    """Digest-bound inventory for an immutable set of evidence observations."""

    events_digest: str
    event_count: int
    source_inventory: Mapping[str, Mapping[str, Any]] = dataclasses.field(default_factory=dict)
    schema_version: str = SCHEMA_VERSION
    manifest_id: str = ""

    def __post_init__(self) -> None:
        if self.event_count < 0:
            raise ValueError("event_count cannot be negative")
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError(f"Unsupported ledger schema: {self.schema_version}")
        object.__setattr__(self, "source_inventory", _freeze_json(_normal_source_inventory(self.source_inventory)))
        expected = MANIFEST_ID_PREFIX + sha256_hex(self.document(include_id=False))
        if self.manifest_id and self.manifest_id != expected:
            raise ValueError("Manifest ID does not match manifest content")
        object.__setattr__(self, "manifest_id", expected)

    def document(self, *, include_id: bool = True) -> dict[str, Any]:
        document: dict[str, Any] = {
            "schema_version": self.schema_version,
            "events_digest": self.events_digest,
            "event_count": self.event_count,
            "source_inventory": _normal_source_inventory(self.source_inventory),
            "source_completeness": source_completeness(self.source_inventory),
        }
        if include_id:
            document["manifest_id"] = self.manifest_id
        return document

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> "LedgerManifest":
        return cls(
            events_digest=str(document.get("events_digest") or ""),
            event_count=int(document.get("event_count", -1)),
            source_inventory=document.get("source_inventory") or {},
            schema_version=str(document.get("schema_version") or ""),
            manifest_id=str(document.get("manifest_id") or ""),
        )


@dataclasses.dataclass(frozen=True)
class EvidenceLedger:
    """Immutable evidence set with duplicate-safe append and alias resolution."""

    events: tuple[EvidenceEvent, ...] = ()
    source_inventory: Mapping[str, Mapping[str, Any]] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        by_id: dict[str, EvidenceEvent] = {}
        for event in self.events:
            if not isinstance(event, EvidenceEvent):
                raise ValueError("Ledger events must be EvidenceEvent instances")
            current = by_id.get(event.evidence_id)
            if current is not None and current.document() != event.document():
                raise ValueError("Evidence ID collision with distinct content")
            by_id[event.evidence_id] = event
        object.__setattr__(self, "events", tuple(by_id[event_id] for event_id in sorted(by_id)))
        object.__setattr__(self, "source_inventory", _freeze_json(_normal_source_inventory(self.source_inventory)))

    @property
    def manifest(self) -> LedgerManifest:
        return LedgerManifest(
            events_digest=event_digest(self.events),
            event_count=len(self.events),
            source_inventory=self.source_inventory,
        )

    def append(self, events: Iterable[EvidenceEvent]) -> "EvidenceLedger":
        """Return a new ledger; exact duplicates are idempotent, never rewritten."""
        return EvidenceLedger(self.events + tuple(events), self.source_inventory)

    def aliases(self) -> dict[str, str]:
        result: dict[str, str] = {}
        for event in self.events:
            for alias_type, alias in event.legacy_aliases.items():
                key = f"{alias_type}:{alias}"
                previous = result.get(key)
                if previous and previous != event.evidence_id:
                    raise ValueError(f"Ambiguous legacy alias: {key}")
                result[key] = event.evidence_id
        return result

    def resolve(self, reference: str) -> EvidenceEvent | None:
        for event in self.events:
            if event.evidence_id == reference:
                return event
        evidence_id = self.aliases().get(reference)
        return next((event for event in self.events if event.evidence_id == evidence_id), None)

    def validate(self, manifest: LedgerManifest | Mapping[str, Any]) -> None:
        candidate = manifest if isinstance(manifest, LedgerManifest) else LedgerManifest.from_document(manifest)
        actual = self.manifest
        if candidate.document() != actual.document():
            raise ValueError("Ledger manifest validation failed: event digest, count, or source inventory differs")


def _legacy_aliases(record: Mapping[str, Any]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for key in LEGACY_ALIAS_KEYS:
        value = record.get(key)
        if value not in (None, ""):
            aliases[key] = str(value)
    return aliases


def _span(record: Mapping[str, Any]) -> dict[str, Any]:
    return _compact_mapping({
        "start": record.get("start"),
        "end": record.get("end"),
        "time": record.get("time"),
        "path": record.get("path"),
        "cwd": record.get("cwd"),
        "session_id": record.get("session_id"),
    })


def _snapshot_attributes(source_type: str, record: Mapping[str, Any]) -> dict[str, Any]:
    """Retain semantic content while excluding source identity, spans, and allocation."""
    shared = {
        "label": record.get("label") or record.get("source_label"),
        "description": record.get("description"),
        "status": record.get("status"),
        "project": record.get("client_project") or record.get("project_name"),
        "reason": record.get("reason"),
        "evidence_level": record.get("evidence_level"),
    }
    if source_type == "fathom":
        shared.update({
            "title": record.get("title") or record.get("meeting_title"),
            "summary": record.get("summary") or record.get("default_summary"),
            "action_items": record.get("action_items"),
            "transcript": record.get("transcript"),
            "transcript_language": record.get("transcript_language"),
            "semantic_evidence_status": record.get("semantic_evidence_status"),
        })
    elif source_type == "multica":
        shared.update({
            "title": record.get("title"),
            "key": record.get("key"),
            "project_id": record.get("project_id"),
        })
    elif source_type in {"codex_sessions", "claude_bursts", "hermes_sessions", "hermes_db_sessions"}:
        shared.update({
            "title": record.get("title"),
            "originator": record.get("originator"),
            "model": record.get("model") or record.get("model_provider"),
            "first_user_message": record.get("first_user_message"),
            "last_assistant_message": record.get("last_assistant_message"),
            "user_messages": record.get("user_messages"),
            "message_count": record.get("message_count"),
            "event_count": len(record.get("events")) if isinstance(record.get("events"), list) else 0,
        })
    excluded = {
        "id", "recording_id", "session_id", "machine", "source", "provenance",
        "start", "end", "time", "timestamp", "path", "cwd", "events", "allocation",
    }
    for key, value in record.items():
        if key not in excluded and key not in shared:
            shared[str(key)] = value
    return _compact_mapping(shared)


_MULTICA_COMPLETED_STATUSES = frozenset({
    "completed",
    "complete",
    "done",
    "closed",
    "resolved",
    "cancelled",
    "canceled",
})


def _multica_observed_at(record: Mapping[str, Any]) -> str | None:
    """Select the issue action timestamp that describes its current state.

    Creation establishes an issue, updates describe active issues, and a terminal
    completion is the authoritative action for a completed issue.  The explicit
    fallback order keeps incomplete historical records deterministic without
    fabricating a collection-time timestamp.
    """
    status = str(record.get("status") or "").strip().casefold()
    fields = (
        ("completed_at", "updated_at", "created_at")
        if status in _MULTICA_COMPLETED_STATUSES
        else ("updated_at", "created_at", "completed_at")
    )
    return next(
        (str(record[field]) for field in fields if record.get(field) not in (None, "")),
        None,
    )


def _snapshot_event(source_type: str, record: Mapping[str, Any], index: int) -> EvidenceEvent:
    provenance = record.get("provenance") if isinstance(record.get("provenance"), Mapping) else {}
    source_ref = _compact_mapping({
        "source_type": source_type,
        "source_id": record.get("recording_id") or record.get("id") or record.get("session_id") or f"row-{index}",
        "machine": record.get("machine") or provenance.get("source_machine"),
        "session_id": record.get("session_id") or provenance.get("source_session_id"),
    })
    attributes = _snapshot_attributes(source_type, record)
    return evidence_event(
        source_type,
        source_ref,
        observed_at=(
            _multica_observed_at(record)
            if source_type == "multica"
            else str(record.get("start") or record.get("timestamp") or "") or None
        ),
        raw_source_span=_span(record),
        attributes=attributes,
        legacy_aliases=_legacy_aliases(record),
    )


def _records(value: Any) -> list[Mapping[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, Mapping)]
    if isinstance(value, Mapping):
        return [value]
    return []


def _session_event(
    session_type: str,
    session: Mapping[str, Any],
    event: Mapping[str, Any],
    machine_name: str,
    ordinal: int,
) -> EvidenceEvent:
    """Represent one collected message/tool event without substituting session time."""
    session_id = str(session.get("session_id") or session.get("id") or "unknown")
    timestamp = event.get("timestamp")
    timestamp_text = str(timestamp) if timestamp not in (None, "") else None
    attributes = _compact_mapping({
        "role": event.get("role"),
        "kind": event.get("kind") or "message",
        # This is deliberately not shortened: semantic analysis needs complete context.
        "content": event.get("content"),
        "tool_name": event.get("tool_name"),
    })
    return evidence_event(
        f"{session_type}_event",
        {
            "source_type": session_type,
            "source_id": f"{session_id}:event:{ordinal}",
            "machine": machine_name,
            "session_id": session_id,
            "ordinal": ordinal,
        },
        observed_at=timestamp_text,
        raw_source_span={
            "timestamp": timestamp_text,
            "timestamp_status": "available" if timestamp_text else "missing",
            "session_start": session.get("start"),
            "session_end": session.get("end"),
            "path": session.get("path"),
            "cwd": session.get("cwd"),
        },
        attributes=attributes,
    )


def normalize_collector_snapshot(snapshot: Mapping[str, Any]) -> list[EvidenceEvent]:
    """Normalize collector snapshots without network access or effort allocation.

    Collector shapes vary by source version, so unknown branches are ignored
    rather than inferred.  The retained span and source reference preserve the
    raw evidence location for later semantic analysis.
    """
    if not isinstance(snapshot, Mapping):
        raise ValueError("Collector snapshot must be an object")
    events: list[EvidenceEvent] = []
    enriched = snapshot.get("enriched_context") or snapshot.get("context")
    source_groups = (
        ("clockify", _records(snapshot.get("clockify", {}).get("entries", []) if isinstance(snapshot.get("clockify"), Mapping) else [])),
        ("fathom", _records(snapshot.get("fathom", {}).get("meetings", []) if isinstance(snapshot.get("fathom"), Mapping) else [])),
        ("multica", _records(snapshot.get("multica_issues", {}).get("issues", []) if isinstance(snapshot.get("multica_issues"), Mapping) else [])),
        ("enriched_context", _records(enriched) if not isinstance(enriched, Mapping) else []),
    )
    for source_type, records in source_groups:
        events.extend(_snapshot_event(source_type, record, index) for index, record in enumerate(records, 1))
    if isinstance(enriched, Mapping):
        for context_type, records in enriched.items():
            for index, record in enumerate(_records(records), 1):
                events.append(_snapshot_event(f"enriched_{context_type}", record, index))
    for machine in _records(snapshot.get("sessions")):
        machine_name = str(machine.get("machine") or "unknown")
        for key in (
            "codex_sessions",
            "claude_bursts",
            "hermes_sessions",
            "hermes_db_sessions",
            "repository_events",
        ):
            for index, record in enumerate(_records(machine.get(key)), 1):
                merged = dict(record)
                merged.setdefault("machine", machine_name)
                events.append(_snapshot_event(key, merged, index))
                for event_ordinal, message in enumerate(_records(record.get("events")), 1):
                    events.append(_session_event(key, merged, message, machine_name, event_ordinal))
    return list(EvidenceLedger(tuple(events)).events)


_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_URL_RE = re.compile(r"\b(?:https?|ftp|file|mailto)://[^\s<>()]+|\bwww\.[^\s<>()]+", re.IGNORECASE)
_DOMAIN_RE = re.compile(
    r"(?<![@A-Za-z0-9_-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+(?:com|net|org|io|ai|app|dev|ro|agency|co|uk|eu|info|biz|me|tv|edu|gov)(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
_PATH_RE = re.compile(r"(?<![A-Za-z0-9])(?:~[\\/]|/(?:Users|home|private|tmp|var|etc|opt|usr|work)(?:[\\/]|$)|[A-Za-z]:[\\/])[^\s`<>()]*")
_HASH_RE = re.compile(r"\b[a-fA-F0-9]{7,}\b")
_PRIVATE_ID_RE = re.compile(r"\b(?:[A-Za-z]{1,8}[-_])?[A-Za-z0-9]{16,}\b")
_NEEDS_REVIEW_RE = re.compile(r"\bneeds[\s\u00a0\u2000-\u200b\u202f\u205f\u3000_-]+review\b", re.IGNORECASE)
_ELLIPSIS_RE = re.compile(r"(?:\.{2,}|\u2025|\u2026|\u22ef)")
_SPACED_DOTS_RE = re.compile(r"\.\s+\.(?:\s+\.)?")
_MARKUP_RE = re.compile(r"(?:`|\*{1,3}|_{1,3}|[\[\]]|^\s*(?:#{1,6}|>|[-+]|\d+[.)])\s+|\|)", re.MULTILINE)
_COMMAND_LIKE_RE = re.compile(
    r"(?:^|\s)(?:\$\s*)?(?:sudo\s+)?(?:git\s+(?:status|diff|log|commit|push|pull|checkout|switch)|curl\s+|wget\s+|ssh\s+|scp\s+|rsync\s+|pytest(?:\s|$)|py\.test(?:\s|$)|python(?:3(?:\.\d+)?)?\s+-m\s+pytest\b|npm\s+(?:run|test|ci|install)\b|npx\s+|docker\s+|kubectl\s+|terraform\s+|make\s+(?:test|check|build)\b|bash\s+|zsh\s+)",
    re.IGNORECASE,
)
_PROMPT_LIKE_RE = re.compile(
    r"\b(?:system|user|assistant)\s+(?:prompt|message)\b|\btool\s+call\b|\bprompt\b|(?:^\s*(?:please\s+)?|\s—\s(?:please\s+)?|\b(?:need|want|ask)\s+(?:you\s+)?to\s+)(?:update|read|write|investigate|troubleshoot|configure|set\s+up|perform|research|prepare|add|remove|fix|check|implement|create|review|analyze|run|deploy|merge|push|commit)\b|\b(?:follow|obey)\s+(?:these\s+)?(?:rules?|instructions?)\b|\b(?:must|should)\s+(?:not\s+)?(?:always|never)\b",
    re.IGNORECASE,
)
_STATUS_LIKE_RE = re.compile(
    r"\b(?:needs[\s_-]*review|in[\s_-]*progress|wip|queued|pending|awaiting|standing\s+by|still\s+(?:running|waiting)|run(?:ning)?\s+status|status\s*[:—-]|all\s+(?:done|complete)|task\s+(?:is\s+)?(?:done|complete)|agent\s+(?:is\s+)?(?:running|completed|waiting))\b",
    re.IGNORECASE,
)


def source_features_for_text(value: str) -> list[str]:
    """Return a stable, non-identifying feature vector for one source record.

    The vector intentionally records only the failure *shapes* found in a
    supplied description.  It never returns words, captures, offsets, or a
    redacted rendering of the input, so it is safe to commit alongside the
    independently curated behavioral disposition.
    """
    text = str(value)
    features: set[str] = set()
    checks = (
        ("needs_review", _NEEDS_REVIEW_RE),
        ("truncation_ellipsis", _ELLIPSIS_RE),
        ("truncation_spaced_dots", _SPACED_DOTS_RE),
        ("markup", _MARKUP_RE),
        ("url", _URL_RE),
        ("domain", _DOMAIN_RE),
        ("path", _PATH_RE),
        ("email", _EMAIL_RE),
        ("hash", _HASH_RE),
        ("command_like", _COMMAND_LIKE_RE),
        ("prompt_like", _PROMPT_LIKE_RE),
        ("status_like", _STATUS_LIKE_RE),
    )
    for name, pattern in checks:
        if pattern.search(text):
            features.add(name)
    return sorted(features)


def source_feature_contract(lines: Sequence[str]) -> list[dict[str, Any]]:
    """Create the no-prose provenance vector for all logical source records."""
    return [
        {"source_line": index, "source_features": source_features_for_text(line)}
        for index, line in enumerate(lines, 1)
    ]


def source_feature_digest(vectors: Sequence[Mapping[str, Any]]) -> str:
    """Content-address an ordered source-feature contract."""
    return hashlib.sha256(
        "".join(canonical_json(vector) + "\n" for vector in vectors).encode("utf-8")
    ).hexdigest()


def redact_legacy_text(value: str) -> tuple[str, list[str]]:
    """Irreversibly redact high-risk text while returning safe failure classes."""
    text = str(value)
    classes: list[str] = []
    substitutions = (
        (_EMAIL_RE, "[REDACTED_EMAIL]", "email"),
        (_URL_RE, "[REDACTED_URL]", "url"),
        (_PATH_RE, "[REDACTED_PATH]", "path"),
        (_HASH_RE, "[REDACTED_HASH]", "hash"),
        (_PRIVATE_ID_RE, "[REDACTED_IDENTIFIER]", "private_identifier"),
    )
    for pattern, replacement, failure_class in substitutions:
        if pattern.search(text):
            classes.append(failure_class)
            text = pattern.sub(replacement, text)
    features = source_features_for_text(text)
    classes.extend({
        "needs_review": "needs_review_marker",
        "truncation_ellipsis": "truncation_marker",
        "truncation_spaced_dots": "truncation_marker",
        "markup": "presentation_markup",
    }[feature] for feature in features if feature in {
        "needs_review", "truncation_ellipsis", "truncation_spaced_dots", "markup",
    })
    # Description text could still contain personally identifying prose.  Keep
    # only deterministic safe tokens in the committed corpus.
    safe_text = " ".join(f"[{item.upper()}]" for item in sorted(set(classes))) or "[LEGACY_DESCRIPTION]"
    return safe_text, sorted(set(classes))


def build_regression_corpus(lines: Sequence[str]) -> list[dict[str, Any]]:
    """Create a no-prose provenance contract, not a semantic classification.

    Human-curated dispositions and safe expected render parts remain a separate
    review boundary.  This builder deliberately cannot infer them from the
    private source descriptions.
    """
    records: list[dict[str, Any]] = []
    for index, line in enumerate(lines, 1):
        _sanitized, classes = redact_legacy_text(line)
        records.append({
            "record_id": f"clockify-regression-v1-{index:03d}",
            "source_line": index,
            "source_features": source_features_for_text(line),
            "failure_classes": classes,
        })
    return records


def verify_regression_corpus(
    records: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    *,
    source_path: Path | None = None,
) -> None:
    """Verify corpus structure and, when supplied, its private input contract.

    This proves only that an immutable feature vector matches the supplied
    source.  It intentionally does *not* claim that a source line mechanically
    proves the human-reviewed render, split, omit, or exception disposition.
    """
    if manifest.get("schema_version") != "clockify-regression-corpus/v1":
        raise ValueError("Unsupported regression corpus schema")
    source = manifest.get("source")
    if not isinstance(source, Mapping):
        raise ValueError("Regression corpus manifest source must be an object")
    if source.get("logical_record_count") != len(records):
        raise ValueError("Regression corpus logical record count differs")
    vectors: list[dict[str, Any]] = []
    for expected_line, record in enumerate(records, 1):
        if record.get("source_line") != expected_line:
            raise ValueError("Regression corpus source lines must be contiguous")
        features = record.get("source_features")
        if not isinstance(features, list) or features != sorted(set(features)):
            raise ValueError("Regression corpus source features must be sorted unique lists")
        if any(not isinstance(feature, str) or not re.fullmatch(r"[a-z_]+", feature) for feature in features):
            raise ValueError("Regression corpus source features are malformed")
        vectors.append({"source_line": expected_line, "source_features": features})
    expected_feature_digest = manifest.get("source_feature_digest")
    if expected_feature_digest != source_feature_digest(vectors):
        raise ValueError("Regression corpus source feature digest differs")
    if source_path is None:
        return
    raw = source_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != source.get("sha256"):
        raise ValueError("Regression corpus source hash differs")
    actual_vectors = source_feature_contract(raw.decode("utf-8").splitlines())
    if vectors != actual_vectors:
        raise ValueError("Regression corpus source feature vectors differ")


def corpus_digest(records: Sequence[Mapping[str, Any]]) -> str:
    """Digest JSONL corpus records in their immutable source order."""
    return hashlib.sha256("".join(canonical_json(record) + "\n" for record in records).encode("utf-8")).hexdigest()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(canonical_json(row) + "\n" for row in rows), encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    corpus = subparsers.add_parser("build-corpus", help="Build an irreversibly redacted JSONL corpus")
    corpus.add_argument("--input", type=Path, required=True)
    corpus.add_argument("--records", type=Path, required=True)
    corpus.add_argument("--manifest", type=Path, required=True)
    corpus.add_argument("--redaction-policy", default="v2: retain only non-identifying source features; discard original text and semantic claims")
    args = parser.parse_args(argv)
    if args.command == "build-corpus":
        raw = args.input.read_text(encoding="utf-8")
        records = build_regression_corpus(raw.splitlines())
        _write_jsonl(args.records, records)
        manifest = {
            "schema_version": "clockify-regression-corpus/v1",
            "source": {"sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(), "logical_record_count": len(records)},
            "redaction_policy": args.redaction_policy,
            "records_digest": corpus_digest(records),
            "source_feature_digest": source_feature_digest(source_feature_contract(raw.splitlines())),
        }
        _write_json(args.manifest, manifest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
