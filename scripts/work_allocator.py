"""Deterministic, evidence-preserving allocation of Clockify work effort.

Evidence is deliberately kept separate from allocated Clockify time.  Evidence
may overlap; allocations never do.  This module is pure stdlib code and has no
Clockify or filesystem side effects.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Iterable, Mapping, Sequence


class AllocationError(ValueError):
    """Base class for invalid allocation input or an invalid schedule."""


class ScheduleValidationError(AllocationError):
    """Raised when a proposed allocation schedule is not strictly usable."""


@dataclass(frozen=True)
class EvidenceSpan:
    """An immutable observed span; overlaps are valid and intentionally kept."""

    evidence_id: str
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        _validate_interval(self.start, self.end, "evidence span")


@dataclass(frozen=True)
class EffortEstimate:
    minimum_minutes: int
    recommended_minutes: int
    maximum_minutes: int

    def __post_init__(self) -> None:
        values = (self.minimum_minutes, self.recommended_minutes, self.maximum_minutes)
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise AllocationError("effort minutes must be integers")
        if self.minimum_minutes < 0 or not (
            self.minimum_minutes <= self.recommended_minutes <= self.maximum_minutes
        ):
            raise AllocationError("effort must satisfy 0 <= minimum <= recommended <= maximum")


@dataclass(frozen=True)
class ActivityDemand:
    """A proposed semantic activity and its permitted placement envelope."""

    activity_id: str
    workstream_id: str
    evidence_spans: tuple[EvidenceSpan, ...]
    effort: EffortEstimate
    allowed_start: datetime | None = None
    allowed_end: datetime | None = None
    confidence: str | float | int = "medium"
    attention_signal: float = 0.0
    allowed_intervals: tuple[tuple[datetime, datetime], ...] = ()

    def __post_init__(self) -> None:
        if not self.activity_id or not self.workstream_id:
            raise AllocationError("activity_id and workstream_id are required")
        intervals = tuple(self.allowed_intervals)
        if not intervals:
            if self.allowed_start is None or self.allowed_end is None:
                raise AllocationError("an allowed envelope or allowed intervals are required")
            intervals = ((self.allowed_start, self.allowed_end),)
        for start, end in intervals:
            _validate_interval(start, end, "allowed interval")
        intervals = tuple(sorted(intervals, key=lambda item: (item[0], item[1])))
        for previous, current in zip(intervals, intervals[1:]):
            if previous[1] > current[0]:
                raise AllocationError("allowed intervals must not overlap")
        # Keep the old bounding fields available to existing callers, but never
        # allocate against that span when it contains an unobserved gap.
        object.__setattr__(self, "allowed_intervals", intervals)
        object.__setattr__(self, "allowed_start", intervals[0][0])
        object.__setattr__(self, "allowed_end", intervals[-1][1])
        if not isinstance(self.attention_signal, (int, float)):
            raise AllocationError("attention_signal must be numeric")


@dataclass(frozen=True)
class FixedBlock:
    """An immutable existing Clockify entry or Fathom meeting."""

    block_id: str
    start: datetime
    end: datetime
    kind: str = "existing_clockify"
    semantic_activity_id: str | None = None
    covered_activity_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.block_id:
            raise AllocationError("block_id is required")
        _validate_interval(self.start, self.end, "fixed block")


@dataclass(frozen=True)
class AllocationSegment:
    allocation_id: str
    activity_id: str
    workstream_id: str
    start: datetime
    end: datetime
    duration_minutes: int
    evidence_ids: tuple[str, ...]


@dataclass(frozen=True)
class CoveredByExisting:
    activity_id: str
    fixed_block_id: str
    reason: str = "explicit semantic activity match"


@dataclass(frozen=True)
class ContestedTime:
    activity_id: str
    workstream_id: str
    requested_minutes: int
    allocated_minutes: int
    unallocated_minutes: int
    minimum_minutes: int
    reason: str = "recommended effort exceeds available observed capacity"


@dataclass(frozen=True)
class UnallocatedCapacity:
    total_minutes: int
    intervals: tuple[tuple[datetime, datetime], ...]


@dataclass(frozen=True)
class AllocationResult:
    """A schedule plus immutable evidence references and explicit exceptions."""

    evidence: tuple[ActivityDemand, ...]
    allocations: tuple[AllocationSegment, ...]
    unallocated_capacity: UnallocatedCapacity
    covered_by_existing: tuple[CoveredByExisting, ...]
    contested_time: tuple[ContestedTime, ...]

    def __getitem__(self, name: str) -> Any:
        return getattr(self, name)

    def get(self, name: str, default: Any = None) -> Any:
        return getattr(self, name, default)

    def as_dict(self) -> dict[str, Any]:
        """Return a shallowly serializable view without modifying the evidence."""
        return {
            "evidence": self.evidence,
            "allocations": self.allocations,
            "unallocated_capacity": self.unallocated_capacity,
            "covered_by_existing": self.covered_by_existing,
            "contested_time": self.contested_time,
        }


def _validate_interval(start: datetime, end: datetime, label: str) -> None:
    if not isinstance(start, datetime) or not isinstance(end, datetime) or start >= end:
        raise AllocationError(f"{label} requires datetime start before end")
    if (start.tzinfo is None) != (end.tzinfo is None):
        raise AllocationError(f"{label} start and end must both be naive or timezone-aware")


def _minutes(start: datetime, end: datetime) -> int:
    seconds = (end - start).total_seconds()
    if seconds % 60:
        raise AllocationError("all allocated intervals must use whole minutes")
    return int(seconds // 60)


def _parse_datetime(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AllocationError(f"{field} must be an ISO datetime") from exc
    raise AllocationError(f"{field} must be a datetime")


def _first(mapping: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return default


def _as_evidence_span(value: EvidenceSpan | Mapping[str, Any]) -> EvidenceSpan:
    if isinstance(value, EvidenceSpan):
        return value
    if not isinstance(value, Mapping):
        raise AllocationError("evidence spans must be EvidenceSpan objects or mappings")
    return EvidenceSpan(
        str(_first(value, "evidence_id", "id")),
        _parse_datetime(_first(value, "start"), "evidence.start"),
        _parse_datetime(_first(value, "end"), "evidence.end"),
    )


def _as_effort(value: EffortEstimate | Mapping[str, Any]) -> EffortEstimate:
    if isinstance(value, EffortEstimate):
        return value
    if not isinstance(value, Mapping):
        raise AllocationError("effort must be an EffortEstimate or mapping")
    return EffortEstimate(
        _first(value, "minimum_minutes", "min_minutes", "min"),
        _first(value, "recommended_minutes", "recommended", "recommended_effort_minutes"),
        _first(value, "maximum_minutes", "max_minutes", "max"),
    )


def _as_activity(value: ActivityDemand | Mapping[str, Any]) -> ActivityDemand:
    if isinstance(value, ActivityDemand):
        return value
    if not isinstance(value, Mapping):
        raise AllocationError("activities must be ActivityDemand objects or mappings")
    envelope = value.get("allowed_envelope", value.get("working_day_envelope", {}))
    if not isinstance(envelope, Mapping):
        raise AllocationError("allowed_envelope must be a mapping")
    start = _first(value, "allowed_start", "envelope_start", default=_first(envelope, "start"))
    end = _first(value, "allowed_end", "envelope_end", default=_first(envelope, "end"))
    raw_intervals = _first(
        value,
        "allowed_intervals",
        "working_day_envelopes",
        "working_day_intervals",
        default=(),
    )
    if isinstance(raw_intervals, (str, bytes)) or not isinstance(raw_intervals, Sequence):
        raise AllocationError("allowed_intervals must be a sequence")
    intervals = tuple(_as_allowed_interval(interval) for interval in raw_intervals)
    raw_spans = value.get("evidence_spans", ())
    evidence_ids = tuple(str(item) for item in value.get("evidence_ids", ()))
    spans: list[EvidenceSpan] = []
    for index, raw_span in enumerate(raw_spans):
        if isinstance(raw_span, Mapping) and "evidence_id" not in raw_span and "id" not in raw_span:
            # Analyzer records may cite IDs at activity level.  Bind only an
            # unambiguous one-to-one or one-to-many relationship; guessing an
            # evidence source would violate the ledger contract.
            if len(evidence_ids) == 1:
                raw_span = {**raw_span, "evidence_id": evidence_ids[0]}
            elif len(evidence_ids) == len(raw_spans):
                raw_span = {**raw_span, "evidence_id": evidence_ids[index]}
            else:
                raise AllocationError("each evidence span requires an explicit, unambiguous evidence ID")
        spans.append(_as_evidence_span(raw_span))
    return ActivityDemand(
        activity_id=str(_first(value, "activity_id", "id")),
        workstream_id=str(_first(value, "workstream_id", "workstream")),
        evidence_spans=tuple(spans),
        effort=_as_effort(value.get("effort", value.get("active_effort", {}))),
        allowed_start=_parse_datetime(start, "allowed_start") if start is not None else None,
        allowed_end=_parse_datetime(end, "allowed_end") if end is not None else None,
        allowed_intervals=intervals,
        confidence=value.get("confidence", "medium"),
        attention_signal=float(value.get("attention_signal", value.get("human_attention_signal", 0.0))),
    )


def _as_allowed_interval(value: Any) -> tuple[datetime, datetime]:
    if isinstance(value, Mapping):
        start, end = value.get("start"), value.get("end")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) == 2:
        start, end = value
    else:
        raise AllocationError("each allowed interval must contain start and end")
    return _parse_datetime(start, "allowed_interval.start"), _parse_datetime(end, "allowed_interval.end")


def _as_fixed_block(value: FixedBlock | Mapping[str, Any]) -> FixedBlock:
    if isinstance(value, FixedBlock):
        return value
    if not isinstance(value, Mapping):
        raise AllocationError("fixed blocks must be FixedBlock objects or mappings")
    covered = _first(value, "covered_activity_ids", "semantic_activity_ids", default=())
    if isinstance(covered, str):
        covered = (covered,)
    semantic_activity_id = _first(value, "semantic_activity_id", "activity_id", default=None)
    return FixedBlock(
        str(_first(value, "block_id", "id")),
        _parse_datetime(_first(value, "start"), "fixed.start"),
        _parse_datetime(_first(value, "end"), "fixed.end"),
        str(value.get("kind", value.get("source_type", "existing_clockify"))),
        str(semantic_activity_id) if semantic_activity_id is not None else None,
        tuple(str(item) for item in covered),
    )


def _confidence_score(value: str | float | int) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return {"high": 3.0, "medium": 2.0, "low": 1.0}.get(str(value).lower(), 0.0)


def _overlaps(left_start: datetime, left_end: datetime, right_start: datetime, right_end: datetime) -> bool:
    return left_start < right_end and right_start < left_end


def validate_strict_non_overlap(intervals: Iterable[Any]) -> None:
    """Raise when any two supplied blocks/allocations overlap (touching is valid)."""
    normalized: list[tuple[datetime, datetime, str]] = []
    for value in intervals:
        start = getattr(value, "start", None)
        end = getattr(value, "end", None)
        label = getattr(value, "allocation_id", None) or getattr(value, "block_id", None) or "interval"
        _validate_interval(start, end, str(label))
        normalized.append((start, end, str(label)))
    normalized.sort(key=lambda item: (item[0], item[1], item[2]))
    for previous, current in zip(normalized, normalized[1:]):
        if previous[1] > current[0]:
            raise ScheduleValidationError(f"overlapping intervals: {previous[2]} and {current[2]}")


def _subtract(blocks: Sequence[tuple[datetime, datetime]], start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    cursor = start
    free: list[tuple[datetime, datetime]] = []
    for block_start, block_end in blocks:
        if block_end <= cursor or block_start >= end:
            continue
        if cursor < block_start:
            free.append((cursor, min(block_start, end)))
        if block_end > cursor:
            cursor = max(cursor, block_end)
        if cursor >= end:
            break
    if cursor < end:
        free.append((cursor, end))
    return free


def _whole_minute_intervals(
    intervals: Iterable[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Quantize free capacity inward without changing exact occupied spans."""
    quantized: list[tuple[datetime, datetime]] = []
    for start, end in intervals:
        whole_start = start.replace(second=0, microsecond=0)
        if start.second or start.microsecond:
            whole_start += timedelta(minutes=1)
        whole_end = end.replace(second=0, microsecond=0)
        if whole_start < whole_end:
            quantized.append((whole_start, whole_end))
    return quantized


def _merge_intervals(
    intervals: Iterable[tuple[datetime, datetime]],
) -> list[tuple[datetime, datetime]]:
    """Return the occupied union; touching intervals are a single span."""
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals, key=lambda item: (item[0], item[1])):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _validate_proposed_schedule(
    fixed_blocks: Sequence[FixedBlock],
    allocations: Sequence[AllocationSegment],
) -> None:
    """Allow historical fixed overlap, but never let it leak into proposals."""
    validate_strict_non_overlap(allocations)
    for allocation in allocations:
        for block in fixed_blocks:
            if _overlaps(allocation.start, allocation.end, block.start, block.end):
                raise ScheduleValidationError(
                    f"allocation {allocation.allocation_id} overlaps fixed block {block.block_id}"
                )


def _explicitly_covered(activity: ActivityDemand, block: FixedBlock) -> bool:
    return activity.activity_id == block.semantic_activity_id or activity.activity_id in block.covered_activity_ids


def allocate_work(
    activities: Sequence[ActivityDemand | Mapping[str, Any]],
    fixed_blocks: Sequence[FixedBlock | Mapping[str, Any]] = (),
) -> AllocationResult:
    """Allocate recommended effort without making evidence or unmet demand disappear.

    Activities are prioritised by confidence, then explicit human-attention
    signal, then stable activity ID.  The allocation itself is chronological
    inside each valid envelope, so an activity can use several genuine free
    segments instead of being forced into one largest gap.
    """
    activity_rows = tuple(_as_activity(row) for row in activities)
    fixed_rows = tuple(_as_fixed_block(row) for row in fixed_blocks)
    if len({row.activity_id for row in activity_rows}) != len(activity_rows):
        raise AllocationError("activity_id values must be unique")
    if len({row.block_id for row in fixed_rows}) != len(fixed_rows):
        raise AllocationError("block_id values must be unique")

    # Clockify history is evidence, not a schedule we can repair: historical
    # entries may overlap.  Keep every FixedBlock for reconciliation/audit,
    # while reserving only their occupied union for new proposals.
    fixed_occupied = _merge_intervals((block.start, block.end) for block in fixed_rows)
    occupied = list(fixed_occupied)
    allocations: list[AllocationSegment] = []
    covered: list[CoveredByExisting] = []
    contested: list[ContestedTime] = []
    ordered = sorted(
        activity_rows,
        key=lambda row: (-_confidence_score(row.confidence), -row.attention_signal, row.activity_id),
    )

    for activity in ordered:
        supporting = next((block for block in fixed_rows if _explicitly_covered(activity, block)), None)
        if supporting:
            covered.append(CoveredByExisting(activity.activity_id, supporting.block_id))
            continue

        remaining = activity.effort.recommended_minutes
        allocated = 0
        segment_number = 0
        allocation_start = len(allocations)
        for interval_start, interval_end in activity.allowed_intervals:
            for free_start, free_end in _whole_minute_intervals(
                _subtract(occupied, interval_start, interval_end)
            ):
                if remaining <= 0:
                    break
                capacity = _minutes(free_start, free_end)
                take = min(capacity, remaining)
                if take <= 0:
                    continue
                segment_number += 1
                allocated_end = free_start + timedelta(minutes=take)
                allocations.append(
                    AllocationSegment(
                        f"{activity.activity_id}:allocation:{segment_number:02d}",
                        activity.activity_id,
                        activity.workstream_id,
                        free_start,
                        allocated_end,
                        take,
                        tuple(span.evidence_id for span in activity.evidence_spans),
                    )
                )
                occupied.append((free_start, allocated_end))
                occupied.sort(key=lambda item: (item[0], item[1]))
                allocated += take
                remaining -= take
            if remaining <= 0:
                break
        if remaining and allocated < activity.effort.minimum_minutes:
            # A token-sized row below the evidence-backed minimum would be the
            # same silent compression the contested-time contract forbids.
            # Roll it back completely and expose the full demand as contested.
            del allocations[allocation_start:]
            occupied = _merge_intervals(
                [
                    *fixed_occupied,
                    *((row.start, row.end) for row in allocations),
                ]
            )
            allocated = 0
            remaining = activity.effort.recommended_minutes
        if remaining:
            contested.append(
                ContestedTime(
                    activity.activity_id,
                    activity.workstream_id,
                    activity.effort.recommended_minutes,
                    allocated,
                    remaining,
                    activity.effort.minimum_minutes,
                )
            )

    allocations.sort(key=lambda row: (row.start, row.end, row.allocation_id))
    covered.sort(key=lambda row: (row.activity_id, row.fixed_block_id))
    contested.sort(key=lambda row: row.activity_id)
    _validate_proposed_schedule(fixed_rows, allocations)
    all_occupied = _merge_intervals(
        [*fixed_occupied, *((row.start, row.end) for row in allocations)]
    )

    # Remaining capacity is metadata, not a demand that this allocator may fill.
    envelope_bounds = sorted(
        {interval for row in activity_rows for interval in row.allowed_intervals},
        key=lambda item: (item[0], item[1]),
    )
    unallocated_intervals: list[tuple[datetime, datetime]] = []
    for start, end in envelope_bounds:
        unallocated_intervals.extend(
            _whole_minute_intervals(_subtract(all_occupied, start, end))
        )
    # Different demand envelopes may overlap.  Merge only for honest capacity totals.
    merged = _merge_intervals(unallocated_intervals)
    return AllocationResult(
        evidence=activity_rows,
        allocations=tuple(allocations),
        unallocated_capacity=UnallocatedCapacity(sum(_minutes(start, end) for start, end in merged), tuple(merged)),
        covered_by_existing=tuple(covered),
        contested_time=tuple(contested),
    )


# A short alias makes the primary pure operation convenient for pipeline callers.
allocate = allocate_work
