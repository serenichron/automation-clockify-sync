#!/usr/bin/env python3
"""Pure, deterministic reconciliation of immutable Fathom and Calendly records."""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
import re
import sys
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEDUP_VERSION = "meeting-dedup/v1"
DEDUP_TOLERANCE = dt.timedelta(minutes=5)
SCHEMA_VERSION = "meeting-reconciliation/v1"
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_LOCAL_MINUTE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")
_TIMESTAMPED_EVIDENCE = re.compile(r"^[^:]+:(\d+)-(\d+)$")


class MeetingReconciliationError(ValueError):
    """Raised for malformed or unverifiable local reconciliation inputs."""


def _canonical(value: object) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise MeetingReconciliationError("record is not JSON-compatible") from error


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _parse_utc(value: object) -> dt.datetime:
    if not isinstance(value, str):
        raise MeetingReconciliationError("recording timestamp is required")
    if _UTC.fullmatch(value):
        try:
            return dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)
        except ValueError as error:
            raise MeetingReconciliationError("recording timestamp is invalid") from error
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise MeetingReconciliationError("recording timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise MeetingReconciliationError("recording timestamp must be timezone-aware")
    return parsed.astimezone(dt.timezone.utc).replace(microsecond=0)


def _iso_utc(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_local_minute(value: str, timezone: ZoneInfo) -> dt.datetime:
    """Convert one exact local-minute Fathom timestamp without guessing DST."""
    try:
        local = dt.datetime.strptime(value, "%Y-%m-%d %H:%M")
    except ValueError as error:  # The caller has already checked the exact shape.
        raise MeetingReconciliationError("Fathom local timestamp is invalid") from error
    instants = set()
    for fold in (0, 1):
        candidate = local.replace(tzinfo=timezone, fold=fold)
        round_trip = candidate.astimezone(dt.timezone.utc).astimezone(timezone)
        if round_trip.replace(tzinfo=None) == local:
            instants.add(candidate.astimezone(dt.timezone.utc))
    if not instants:
        raise MeetingReconciliationError("Fathom local timestamp does not exist in manifest timezone")
    if len(instants) != 1:
        raise MeetingReconciliationError("Fathom local timestamp is ambiguous in manifest timezone")
    return next(iter(instants))


def _identity(value: object) -> str:
    return value.strip().casefold() if isinstance(value, str) else ""

def _provider_identity(value: object) -> str:
    """Normalize provider-issued textual or integer identifiers without guessing."""
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return _identity(value)


def _identity_set(value: object) -> frozenset[str]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        raise MeetingReconciliationError("member identities must be a sequence")
    identities = frozenset(item for item in (_identity(item) for item in value) if item)
    if not identities:
        raise MeetingReconciliationError("at least one member identity is required")
    return identities


_LEGACY_MEMBER_IDENTITIES = frozenset({"vlad@serenichron.com"})


def manifest_member_identities(manifest: Mapping[str, Any]) -> frozenset[str]:
    """Read the bound member set while retaining compatible legacy artifacts."""
    if not isinstance(manifest, Mapping):
        raise MeetingReconciliationError("period manifest must be an object")
    for key in ("member_identities", "vlad_identities"):
        if key in manifest:
            return _identity_set(manifest[key])
    identity = manifest.get("identity")
    if isinstance(identity, Mapping):
        for key in ("member_identities", "vlad_identities"):
            if key in identity:
                return _identity_set(identity[key])
    return _LEGACY_MEMBER_IDENTITIES


def _person_identities(value: object) -> frozenset[str]:
    """Normalize a person to typed identity tokens without conflating fields."""
    if isinstance(value, str):
        identity = _identity(value)
        return frozenset({f"name:{identity}"}) if identity else frozenset()
    if not isinstance(value, Mapping):
        return frozenset()
    return frozenset(
        f"{key}:{identity}"
        for key in ("email", "name", "id")
        if (identity := _identity(value.get(key)))
    )


def _identity_values(tokens: frozenset[str], kind: str) -> frozenset[str]:
    prefix = f"{kind}:"
    return frozenset(token.removeprefix(prefix) for token in tokens if token.startswith(prefix))


def _is_member(tokens: frozenset[str], member_identities: frozenset[str]) -> bool:
    values = (
        _identity_values(tokens, "email")
        | _identity_values(tokens, "name")
        | _identity_values(tokens, "id")
    )
    return bool(values & member_identities)


def _people_are_compatible(left: frozenset[str], right: frozenset[str]) -> bool:
    left_emails, right_emails = _identity_values(left, "email"), _identity_values(right, "email")
    if left_emails and right_emails:
        return bool(left_emails & right_emails)
    left_ids, right_ids = _identity_values(left, "id"), _identity_values(right, "id")
    if left_ids and right_ids:
        return bool(left_ids & right_ids)
    return bool(left & right)


def _same_people(
    left_people: tuple[frozenset[str], ...], right_people: tuple[frozenset[str], ...]
) -> bool:
    """Require an unambiguous one-to-one match between participant people."""
    if not left_people or len(left_people) != len(right_people):
        return False
    left = tuple(sorted(left_people, key=lambda tokens: tuple(sorted(tokens))))
    right = tuple(sorted(right_people, key=lambda tokens: tuple(sorted(tokens))))
    partners = [
        tuple(index for index, person in enumerate(right) if _people_are_compatible(source, person))
        for source in left
    ]
    if any(len(indices) != 1 for indices in partners):
        return False
    return len({indices[0] for indices in partners}) == len(right)


def _people(value: object) -> tuple[dict[str, str], ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise MeetingReconciliationError("participants must be a sequence")
    normalized: list[dict[str, str]] = []
    for person in value:
        if not isinstance(person, Mapping):
            raise MeetingReconciliationError("participant must be an object")
        item = {key: str(person[key]) for key in ("email", "name", "id") if isinstance(person.get(key), str) and person[key].strip()}
        if item:
            normalized.append(item)
    return tuple(sorted(normalized, key=lambda item: _canonical(item)))


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _items(value: object) -> tuple[Mapping[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or not all(isinstance(item, Mapping) for item in value):
        raise MeetingReconciliationError("semantic items must be objects")
    return tuple(dict(item) for item in value)


@dataclasses.dataclass(frozen=True)
class _Candidate:
    provider: str
    source_id: str
    source_digest: str
    provider_ids: frozenset[str]
    meeting_ids: frozenset[str]
    join_urls: frozenset[str]
    start: dt.datetime
    end: dt.datetime
    title: str
    organizer: Mapping[str, str]
    participants: tuple[Mapping[str, str], ...]
    participant_people: tuple[frozenset[str], ...]
    summary: str
    transcript: tuple[Mapping[str, Any], ...]
    action_items: tuple[Mapping[str, Any], ...]

    @property
    def duration_seconds(self) -> int:
        return int((self.end - self.start).total_seconds())

    @property
    def semantic_score(self) -> int:
        return int(bool(self.summary)) + len(self.transcript) + len(self.action_items)


@dataclasses.dataclass(frozen=True)
class CanonicalMeeting:
    canonical_id: str
    source_ids: tuple[str, ...]
    source_digests: tuple[str, ...]
    start: str
    end: str
    duration_seconds: int
    title: str
    organizer: Mapping[str, str]
    participants: tuple[Mapping[str, str], ...]
    join_url: str
    summary: str
    transcript: tuple[Mapping[str, Any], ...]
    action_items: tuple[Mapping[str, Any], ...]

    def document(self) -> dict[str, Any]:
        return {
            "canonical_id": self.canonical_id,
            "source_ids": list(self.source_ids),
            "source_digests": list(self.source_digests),
            "start": self.start,
            "end": self.end,
            "duration_seconds": self.duration_seconds,
            "title": self.title,
            "organizer": dict(self.organizer),
            "participants": [dict(item) for item in self.participants],
            "join_url": self.join_url,
            "summary": self.summary,
            "transcript": [dict(item) for item in self.transcript],
            "action_items": [dict(item) for item in self.action_items],
        }


@dataclasses.dataclass(frozen=True)
class MeetingSplit:
    """One evidence-bound, fixed-duration route inside a canonical meeting."""

    canonical_id: str
    index: int
    start: str
    end: str
    route: Mapping[str, Any]
    evidence_ids: tuple[str, ...]

    @property
    def duration_seconds(self) -> int:
        return int((_parse_utc(self.end) - _parse_utc(self.start)).total_seconds())

    @property
    def duration_minutes(self) -> int:
        return self.duration_seconds // 60


def _split_route_identity(route: Mapping[str, Any]) -> tuple[str, str]:
    project = _text(route.get("project_name"))
    task = _text(route.get("task_name"))
    if not project or not task:
        raise MeetingReconciliationError("split route must include a validated project and task")
    return project.casefold(), task.casefold()


def _split_boundaries_are_evidenced(
    meeting_start: dt.datetime, split: MeetingSplit
) -> bool:
    start_offset = int((_parse_utc(split.start) - meeting_start).total_seconds())
    end_offset = int((_parse_utc(split.end) - meeting_start).total_seconds())
    for evidence_id in split.evidence_ids:
        match = _TIMESTAMPED_EVIDENCE.fullmatch(evidence_id)
        if match and (int(match.group(1)), int(match.group(2))) == (start_offset, end_offset):
            return True
    return False


def validate_meeting_splits(
    meeting: CanonicalMeeting,
    splits: Iterable[MeetingSplit],
    *,
    granularity_minutes: int = 5,
) -> tuple[MeetingSplit, ...]:
    """Validate a complete, evidence-timestamped partition of one meeting."""
    if not isinstance(meeting, CanonicalMeeting):
        raise MeetingReconciliationError("canonical meeting is required")
    if not isinstance(granularity_minutes, int) or granularity_minutes <= 0:
        raise MeetingReconciliationError("split granularity must be a positive whole number of minutes")
    values = tuple(splits)
    if not values:
        raise MeetingReconciliationError("meeting split list cannot be empty")
    if not all(isinstance(split, MeetingSplit) for split in values):
        raise MeetingReconciliationError("meeting splits must use MeetingSplit records")
    meeting_start, meeting_end = _parse_utc(meeting.start), _parse_utc(meeting.end)
    if any(split.canonical_id != meeting.canonical_id for split in values):
        raise MeetingReconciliationError("meeting splits must share the canonical meeting ID")
    ordered = tuple(sorted(values, key=lambda split: split.index))
    if tuple(split.index for split in ordered) != tuple(range(len(ordered))):
        raise MeetingReconciliationError("meeting split indexes must be consecutive and ordered")
    if ordered != values:
        raise MeetingReconciliationError("meeting splits must be ordered by index")
    routes: set[tuple[str, str]] = set()
    granularity_seconds = granularity_minutes * 60
    cursor = meeting_start
    for index, split in enumerate(ordered):
        start, end = _parse_utc(split.start), _parse_utc(split.end)
        if end <= start:
            raise MeetingReconciliationError("meeting split duration must be positive")
        if start != cursor:
            raise MeetingReconciliationError("meeting splits must cover the full meeting without gaps or overlap")
        duration_seconds = int((end - start).total_seconds())
        is_final = index == len(ordered) - 1
        if duration_seconds % granularity_seconds and not is_final:
            raise MeetingReconciliationError("non-final meeting splits must use the configured minute granularity")
        if is_final and end != meeting_end:
            raise MeetingReconciliationError("meeting splits must cover the full meeting without gaps or overlap")
        if not _split_boundaries_are_evidenced(meeting_start, split):
            raise MeetingReconciliationError("timestamped boundary evidence is required for every meeting split")
        route_identity = _split_route_identity(split.route)
        if route_identity in routes:
            raise MeetingReconciliationError("meeting splits must use distinct validated routes")
        routes.add(route_identity)
        cursor = end
    if cursor != meeting_end:
        raise MeetingReconciliationError("meeting splits must cover the full meeting without gaps or overlap")
    return ordered


@dataclasses.dataclass(frozen=True)
class MeetingReconciliation:
    algorithm_version: str
    tolerance_seconds: int
    meetings: tuple[CanonicalMeeting, ...]
    exclusions: tuple[Mapping[str, Any], ...]
    exceptions: tuple[Mapping[str, Any], ...]
    input_digests: Mapping[str, str] = dataclasses.field(default_factory=dict)
    member_identities: tuple[str, ...] = ()

    def document(self) -> dict[str, Any]:
        contents = {
            "schema_version": SCHEMA_VERSION,
            "algorithm_version": self.algorithm_version,
            "tolerance_seconds": self.tolerance_seconds,
            "input_digests": dict(sorted(self.input_digests.items())),
            "member_identities": list(self.member_identities),
            "meetings": [meeting.document() for meeting in self.meetings],
            "exclusions": [dict(item) for item in self.exclusions],
            "exceptions": [dict(item) for item in self.exceptions],
        }
        return {"reconciliation_id": "mr-" + _digest(contents).removeprefix("sha256:"), **contents}


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MeetingReconciliationError(f"{name} must be an object")
    return value


def _candidate(provider: str, record: Mapping[str, Any], vlad: frozenset[str]) -> _Candidate:
    source = _mapping(record, "recording")
    if "source_type" in source and "attributes" in source:  # immutable ledger event
        if source.get("source_type") != provider:
            raise MeetingReconciliationError("evidence event has an unexpected source type")
        attributes = _mapping(source["attributes"], "event attributes")
        source_ref = _mapping(source.get("source_ref"), "event source reference")
        span = _mapping(source.get("raw_source_span"), "event source span")
        source = {**attributes, "recording_id": attributes.get("recording_id") or source_ref.get("source_id"), "start": span.get("start"), "end": span.get("end"), "source_digest": attributes.get("source_digest") or source.get("evidence_id")}
    identifier = _provider_identity(source.get("recording_id") or source.get("id") or source.get("source_id"))
    if not identifier:
        raise MeetingReconciliationError("recording identity is required")
    start, end = _parse_utc(source.get("start")), _parse_utc(source.get("end"))
    if end <= start:
        raise MeetingReconciliationError("recording window is invalid")
    digest = _text(source.get("source_digest"))
    if not _SHA256.fullmatch(digest):
        digest = _digest({key: value for key, value in source.items() if key != "source_digest"})
    provider_ids = frozenset(
        item for item in (_provider_identity(source.get(key)) for key in ("recording_id", "id", "provider_recording_id")) if item
    )
    meeting_ids = frozenset(
        item for item in (_provider_identity(source.get(key)) for key in ("meeting_id", "event_id", "calendar_event_id", "cross_provider_meeting_id")) if item
    )
    join_urls = frozenset(
        item for item in (_identity(source.get(key)) for key in ("join_url", "share_url", "url")) if item
    )
    participants = _people(source.get("participants", source.get("calendar_invitees", ())))
    organizer = _mapping(source.get("organizer") or {}, "organizer")
    organizer_normal = {key: str(organizer[key]) for key in ("email", "name", "id") if isinstance(organizer.get(key), str) and organizer[key].strip()}
    # The fallback rule is deliberately limited to participant/invitee lists.
    # A recorder or organizer is provenance, not evidence of attendance.
    participant_people = tuple(
        identities
        for person in participants
        if (identities := _person_identities(person)) and not _is_member(identities, vlad)
    )
    return _Candidate(
        provider=provider,
        source_id=f"{provider}:{identifier}", source_digest=digest,
        provider_ids=provider_ids, meeting_ids=meeting_ids, join_urls=join_urls,
        start=start, end=end, title=_text(source.get("title")), organizer=organizer_normal,
        participants=participants, participant_people=participant_people,
        summary=_text(source.get("summary") or source.get("default_summary")),
        transcript=_items(source.get("transcript")), action_items=_items(source.get("action_items")),
    )


def _normalized_candidates(fathom: Iterable[Mapping[str, Any]], calendly: Iterable[Mapping[str, Any]], vlad_identities: object) -> tuple[tuple[_Candidate, ...], tuple[_Candidate, ...]]:
    vlad = _identity_set(vlad_identities)
    def load(provider: str, values: Iterable[Mapping[str, Any]]) -> tuple[_Candidate, ...]:
        if isinstance(values, (str, bytes)):
            raise MeetingReconciliationError(f"{provider} recordings must be a sequence")
        result = tuple(_candidate(provider, _mapping(value, "recording"), vlad) for value in values)
        ids = [item.source_id for item in result]
        if len(set(ids)) != len(ids):
            raise MeetingReconciliationError(f"duplicate {provider} recording identity")
        return tuple(sorted(result, key=lambda item: item.source_id))
    return load("fathom", fathom), load("calendly", calendly)


def _window_agrees(left: _Candidate, right: _Candidate, tolerance: dt.timedelta) -> bool:
    return abs(left.start - right.start) <= tolerance and abs(left.end - right.end) <= tolerance


def _relation(left: _Candidate, right: _Candidate, tolerance: dt.timedelta) -> tuple[str | None, str | None]:
    """Return a verified match strength or a review-only ambiguity reason."""
    explicit = bool(left.provider_ids & right.provider_ids) or bool(left.meeting_ids & right.meeting_ids) or bool(left.join_urls & right.join_urls)
    same_people = _same_people(left.participant_people, right.participant_people)
    conflicting_people = (
        bool(left.participant_people)
        and bool(right.participant_people)
        and not same_people
    )
    near_window = _window_agrees(left, right, tolerance)
    if explicit:
        if conflicting_people:
            return None, "participant_conflict"
        if not near_window:
            return None, "timing_conflict"
        if left.provider_ids & right.provider_ids:
            return "shared_identity", None
        return "cross_provider_identity", None
    if same_people and near_window:
        return "participant_window", None
    if same_people and not near_window:
        return None, "timing_conflict"
    if near_window:
        if not left.participant_people or not right.participant_people:
            return None, "missing_participants"
        if conflicting_people:
            return None, "participant_conflict"
    return None, None


def _has_priority_identity(left: _Candidate, right: _Candidate, strength: str, tolerance: dt.timedelta) -> bool:
    """Return whether a pair belongs to one deduplication priority tier.

    A conflict at a stronger tier must block a weaker fallback even though it
    cannot form a canonical match itself.
    """
    if strength == "shared_identity":
        return bool(left.provider_ids & right.provider_ids)
    if strength == "cross_provider_identity":
        return bool(left.meeting_ids & right.meeting_ids) or bool(left.join_urls & right.join_urls)
    if strength == "participant_window":
        return _window_agrees(left, right, tolerance)
    raise MeetingReconciliationError("unknown reconciliation match priority")


def _exception(source_ids: Sequence[str], reason: str, candidates: Sequence[str] = ()) -> dict[str, Any]:
    return {"kind": "duplicate_ambiguous", "reason": reason, "source_ids": sorted(source_ids), "candidate_source_ids": sorted(candidates)}


def _match_in_priority_order(fathom: tuple[_Candidate, ...], calendly: tuple[_Candidate, ...], tolerance: dt.timedelta) -> tuple[tuple[tuple[_Candidate, ...], ...], tuple[Mapping[str, Any], ...]]:
    remaining_f = {item.source_id: item for item in fathom}
    remaining_c = {item.source_id: item for item in calendly}
    groups: list[tuple[_Candidate, ...]] = []
    exceptions: list[Mapping[str, Any]] = []
    quarantined: set[str] = set()
    for strength in ("shared_identity", "cross_provider_identity", "participant_window"):
        tier_pairs: list[tuple[_Candidate, _Candidate, str | None, str | None]] = []
        for left in remaining_f.values():
            for right in remaining_c.values():
                if left.source_id in quarantined or right.source_id in quarantined:
                    continue
                if not _has_priority_identity(left, right, strength, tolerance):
                    continue
                relation, reason = _relation(left, right, tolerance)
                tier_pairs.append((left, right, relation, reason))
        edges = [
            (left, right)
            for left, right, relation, _reason in tier_pairs
            if relation == strength
        ]
        by_source: dict[str, list[str]] = {}
        for left, right, _relation_kind, _reason in tier_pairs:
            by_source.setdefault(left.source_id, []).append(right.source_id)
            by_source.setdefault(right.source_id, []).append(left.source_id)
        ambiguous = {source for source, values in by_source.items() if len(values) > 1}
        for source in sorted(ambiguous):
            if source in remaining_f or source in remaining_c:
                exceptions.append(_exception((source, *by_source[source]), "multiple_candidates", by_source[source]))
        for left, right, _relation_kind, reason in tier_pairs:
            if reason:
                exceptions.append(_exception((left.source_id, right.source_id), reason))
        # An uncertainty at a stronger priority must never be revisited by a
        # weaker rule. Quarantine both endpoints of every ambiguous or
        # conflicting priority pair.
        for left, right, _relation_kind, reason in tier_pairs:
            if left.source_id in ambiguous or right.source_id in ambiguous:
                quarantined.update((left.source_id, right.source_id))
            if reason:
                quarantined.update((left.source_id, right.source_id))
        for left, right in edges:
            if left.source_id in ambiguous or right.source_id in ambiguous:
                continue
            if left.source_id not in remaining_f or right.source_id not in remaining_c:
                continue
            groups.append((left, right))
            remaining_f.pop(left.source_id)
            remaining_c.pop(right.source_id)
    for left in remaining_f.values():
        for right in remaining_c.values():
            if left.source_id in quarantined or right.source_id in quarantined:
                continue
            _relation_kind, reason = _relation(left, right, tolerance)
            if reason:
                exceptions.append(_exception((left.source_id, right.source_id), reason))
    groups.extend((item,) for item in remaining_f.values())
    groups.extend((item,) for item in remaining_c.values())
    groups.sort(key=lambda group: tuple(sorted(item.source_id for item in group)))
    unique = { _canonical(item): item for item in exceptions }
    return tuple(groups), tuple(unique[key] for key in sorted(unique))


def _canonical_meeting(group: tuple[_Candidate, ...], algorithm_version: str) -> CanonicalMeeting:
    ordered = tuple(sorted(group, key=lambda item: item.source_id))
    # Fathom is authoritative when it supplies at least as much semantic evidence;
    # otherwise the richer recording retains its exact, never-averaged timing.
    preferred = max(ordered, key=lambda item: (item.semantic_score, item.provider == "fathom", item.source_id))
    fallbacks = tuple(item for item in ordered if item != preferred)
    def first_text(field: str) -> str:
        value = getattr(preferred, field)
        if value:
            return value
        return next((getattr(item, field) for item in fallbacks if getattr(item, field)), "")
    def first_items(field: str) -> tuple[Mapping[str, Any], ...]:
        value = getattr(preferred, field)
        if value:
            return value
        return next((getattr(item, field) for item in fallbacks if getattr(item, field)), ())
    organizer = preferred.organizer or next((item.organizer for item in fallbacks if item.organizer), {})
    participants = preferred.participants or next((item.participants for item in fallbacks if item.participants), ())
    identity = {"algorithm_version": algorithm_version, "source_ids": [item.source_id for item in ordered], "source_digests": [item.source_digest for item in ordered]}
    return CanonicalMeeting(
        canonical_id="cm-" + _digest(identity).removeprefix("sha256:"),
        source_ids=tuple(item.source_id for item in ordered), source_digests=tuple(item.source_digest for item in ordered),
        start=_iso_utc(preferred.start), end=_iso_utc(preferred.end), duration_seconds=preferred.duration_seconds,
        title=first_text("title"), organizer=organizer, participants=participants,
        join_url=next(iter(sorted(preferred.join_urls)), "") or next((next(iter(sorted(item.join_urls)), "") for item in fallbacks if item.join_urls), ""),
        summary=first_text("summary"), transcript=first_items("transcript"), action_items=first_items("action_items"),
    )


def reconcile_meetings(fathom: Iterable[Mapping[str, Any]], calendly: Iterable[Mapping[str, Any]], *, vlad_identities: object, tolerance: dt.timedelta = DEDUP_TOLERANCE, algorithm_version: str = DEDUP_VERSION) -> MeetingReconciliation:
    if not isinstance(tolerance, dt.timedelta) or tolerance.total_seconds() < 0 or tolerance != dt.timedelta(seconds=int(tolerance.total_seconds())):
        raise MeetingReconciliationError("tolerance must be a non-negative whole-second duration")
    if not isinstance(algorithm_version, str) or not algorithm_version:
        raise MeetingReconciliationError("algorithm version is required")
    member_identities = _identity_set(vlad_identities)
    f_candidates, c_candidates = _normalized_candidates(fathom, calendly, member_identities)
    groups, exceptions = _match_in_priority_order(f_candidates, c_candidates, tolerance)
    meetings = tuple(_canonical_meeting(group, algorithm_version) for group in groups)
    return MeetingReconciliation(
        algorithm_version, int(tolerance.total_seconds()), meetings, (), exceptions,
        member_identities=tuple(sorted(member_identities)),
    )


def normalize_ledger_recordings(
    records: Iterable[Mapping[str, Any]], manifest: Mapping[str, Any],
) -> list[Mapping[str, Any]]:
    """Convert local-minute Fathom ledger spans through the bound timezone."""
    normalized: list[Mapping[str, Any]] = []
    timezone: ZoneInfo | None = None
    for record in records:
        candidate = dict(record)
        span = record.get("raw_source_span")
        if record.get("source_type") == "fathom" and isinstance(span, Mapping):
            local_fields = [
                field for field in ("start", "end")
                if isinstance(span.get(field), str) and _LOCAL_MINUTE.fullmatch(span[field])
            ]
            if local_fields:
                timezone = timezone or _manifest_timezone(manifest)
                candidate["raw_source_span"] = {
                    **dict(span),
                    **{
                        field: _iso_utc(_parse_local_minute(span[field], timezone))
                        for field in local_fields
                    },
                }
        normalized.append(candidate)
    return normalized


def _read_json(path: Path) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MeetingReconciliationError(f"cannot read JSON input: {path}") from error
    return _mapping(value, "JSON input")


def _artifact_reference(manifest: Mapping[str, Any]) -> Mapping[str, Any]:
    for container_name in ("artifacts", "sources"):
        container = manifest.get(container_name)
        if isinstance(container, Mapping):
            for key in ("fathom", "fathom_evidence", "fathom_meetings"):
                if isinstance(container.get(key), Mapping):
                    return _mapping(container[key], "Fathom artifact reference")
    if isinstance(manifest.get("fathom"), Mapping):
        return _mapping(manifest["fathom"], "Fathom artifact reference")
    raise MeetingReconciliationError("period manifest has no Fathom artifact reference")


def _manifest_vlad_identities(manifest: Mapping[str, Any]) -> Sequence[str]:
    return tuple(sorted(manifest_member_identities(manifest)))


def _manifest_timezone(manifest: Mapping[str, Any]) -> ZoneInfo:
    value = manifest.get("timezone") or manifest.get("period_timezone")
    identity = manifest.get("identity")
    if value is None and isinstance(identity, Mapping):
        value = identity.get("timezone")
    if not isinstance(value, str) or not value:
        raise MeetingReconciliationError("period manifest has no timezone for local Fathom timestamps")
    try:
        return ZoneInfo(value)
    except ZoneInfoNotFoundError as error:
        raise MeetingReconciliationError("period manifest timezone is invalid") from error


def _normalize_fathom_ingestion(records: Sequence[Mapping[str, Any]], manifest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Adapt the collector's local-minute artifact only at the CLI boundary."""
    timezone: ZoneInfo | None = None
    normalized = normalize_ledger_recordings(records, manifest)
    for item in normalized:
        for field in ("start", "end"):
            value = item.get(field)
            if isinstance(value, str) and _LOCAL_MINUTE.fullmatch(value):
                timezone = timezone or _manifest_timezone(manifest)
                item[field] = _iso_utc(_parse_local_minute(value, timezone))
    return normalized


def _source_records(document: Mapping[str, Any], key: str) -> Sequence[Mapping[str, Any]]:
    if document.get("complete") is not True:
        raise MeetingReconciliationError("recording source is incomplete")
    records = document.get(key)
    if not isinstance(records, list) or not all(isinstance(record, Mapping) for record in records):
        raise MeetingReconciliationError(f"recording source has no valid {key}")
    return records


def _verify_calendly_records(records: Sequence[Mapping[str, Any]]) -> None:
    for record in records:
        supplied = record.get("source_digest")
        calculated = _digest({key: value for key, value in record.items() if key != "source_digest"})
        if supplied != calculated:
            raise MeetingReconciliationError("Calendly recording source digest does not verify")


def _atomic_write(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(_canonical(document) + b"\n")
    temporary.replace(path)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period-manifest", type=Path, required=True)
    parser.add_argument("--fathom-from-manifest", action="store_true", required=True)
    parser.add_argument("--calendly", type=Path, required=True)
    parser.add_argument("--algorithm", default=DEDUP_VERSION)
    parser.add_argument("--tolerance-seconds", type=int, default=int(DEDUP_TOLERANCE.total_seconds()))
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.tolerance_seconds < 0:
            raise MeetingReconciliationError("tolerance seconds must be non-negative")
        manifest_path = args.period_manifest.resolve()
        manifest = _read_json(manifest_path)
        reference = _artifact_reference(manifest)
        relative_path = reference.get("path")
        expected_digest = reference.get("digest", reference.get("sha256"))
        if not isinstance(relative_path, str) or not relative_path or not isinstance(expected_digest, str) or not _SHA256.fullmatch(expected_digest):
            raise MeetingReconciliationError("Fathom artifact reference is not digest-bound")
        fathom_path = (manifest_path.parent / relative_path).resolve() if not Path(relative_path).is_absolute() else Path(relative_path).resolve()
        if _file_digest(fathom_path) != expected_digest:
            raise MeetingReconciliationError("Fathom artifact digest does not verify")
        calendly_path = args.calendly.resolve()
        fathom_document = _read_json(fathom_path)
        calendly_document = _read_json(calendly_path)
        fathom_records = _normalize_fathom_ingestion(_source_records(fathom_document, "meetings"), manifest)
        calendly_records = _source_records(calendly_document, "recordings")
        _verify_calendly_records(calendly_records)
        result = reconcile_meetings(
            fathom_records, calendly_records, vlad_identities=_manifest_vlad_identities(manifest),
            tolerance=dt.timedelta(seconds=args.tolerance_seconds), algorithm_version=args.algorithm,
        )
        result = dataclasses.replace(result, input_digests={"fathom": _file_digest(fathom_path), "calendly": _file_digest(calendly_path)})
        _atomic_write(args.output.resolve(), result.document())
    except MeetingReconciliationError as error:
        print(f"meeting reconciliation failed: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
