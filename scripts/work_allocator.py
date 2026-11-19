"""Deterministic, evidence-preserving allocation of Clockify work effort.

Evidence is deliberately kept separate from allocated Clockify time.  Evidence
may overlap; allocations never do.  This module is pure stdlib code and has no
Clockify or filesystem side effects.
"""
from dataclasses import dataclass, field as dataclass_field
import datetime as dt
from datetime import datetime, timedelta
from fractions import Fraction
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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


# ---------------------------------------------------------------------------
# Billing guards for parallel agent sessions
# ---------------------------------------------------------------------------
#
# Two rules, decided on 2026-11-19, bound how much session time may be billed:
#
# * The same wall-clock minute is never billed to two clients.  Where sessions
#   for different clients were observed at the same time, the contested time is
#   split between those clients in proportion to their estimated effort there.
#   Time already taken by another client's fixed entry (an existing Clockify
#   entry or a recorded meeting) is not available to a session at all.
# * Per client and day, billed time may not exceed ``day_multiplier`` times the
#   wall-clock time covered; per client and calendar month it may not exceed
#   ``month_multiplier`` times the sum of the daily wall-clock times.  Parallel
#   sessions for one client may therefore still overlap, up to these caps.
#
# This module is pure stdlib code with no Clockify or filesystem side effects.
# It measures the same quantities as the read-only timesheet checker: a row is
# attributed to the local day of its start, a day's wall-clock time is the union
# of that client's rows, and a month's wall-clock time is the sum of its days.

BILLING_CAPS_ROUTING_KEY = "billing_caps"
DEFAULT_DAY_MULTIPLIER = Fraction(3, 2)
DEFAULT_MONTH_MULTIPLIER = Fraction(5, 4)
DEFAULT_TIMEZONE = "Europe/Bucharest"
# Each day must satisfy the month multiplier on its own.  A month is a
# weighted average of its days, so this holds the month cap without any state
# shared between runs, whatever order days are processed or reprocessed in.
MONTH_SCOPE_PER_DAY = "per_day"
# The month is checked on everything added so far.  Only valid when one caller
# feeds a whole month in chronological order (the offline replay does).
MONTH_SCOPE_CUMULATIVE = "cumulative"
MINUTE = 60

Interval = tuple[dt.datetime, dt.datetime]


class BillingCapsError(AllocationError):
    """Raised for an invalid billing-cap declaration or interval."""


def _multiplier(value: Any, default: Fraction, field: str) -> Fraction:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise BillingCapsError(f"{BILLING_CAPS_ROUTING_KEY}.{field} must be a number")
    try:
        parsed = Fraction(str(value))
    except (ValueError, ZeroDivisionError) as exc:
        raise BillingCapsError(f"{BILLING_CAPS_ROUTING_KEY}.{field} must be a number") from exc
    if parsed < 1:
        raise BillingCapsError(f"{BILLING_CAPS_ROUTING_KEY}.{field} must be at least 1")
    return parsed


@dataclass(frozen=True)
class BillingPolicy:
    day_multiplier: Fraction = DEFAULT_DAY_MULTIPLIER
    month_multiplier: Fraction = DEFAULT_MONTH_MULTIPLIER
    project_clients: Mapping[str, str] = dataclass_field(default_factory=dict)
    timezone: str = DEFAULT_TIMEZONE

    def client_for_project(self, project_suffix: Any) -> str:
        """Return the billed client of a routed project, or "" without a project.

        A routed project missing from the declared map is its own client.  That
        keeps its minutes exclusive against every other client instead of
        silently merging it into one.
        """
        suffix = str(project_suffix or "").strip()
        if not suffix:
            return ""
        return self.project_clients.get(suffix) or f"project:{suffix}"

    def declared_client(self, project_suffix: Any) -> str:
        """Return the client only when the project is declared; "" otherwise."""
        return self.project_clients.get(str(project_suffix or "").strip(), "")

    def document(self) -> dict[str, Any]:
        return {
            "day_multiplier": str(self.day_multiplier),
            "month_multiplier": str(self.month_multiplier),
            "timezone": self.timezone,
        }


def billing_policy_from_routing(routing: Mapping[str, Any] | None) -> BillingPolicy | None:
    """Read the routing declaration; without one the guards are off.

    A run replays against its own routing snapshot, so results produced
    before the declaration existed keep reproducing unchanged.
    """
    if BILLING_CAPS_ROUTING_KEY not in (routing or {}):
        return None
    declaration = (routing or {}).get(BILLING_CAPS_ROUTING_KEY)
    timezone = str((routing or {}).get("timezone") or DEFAULT_TIMEZONE)
    if not isinstance(declaration, Mapping):
        raise BillingCapsError(f"{BILLING_CAPS_ROUTING_KEY} must be an object")
    unknown = set(declaration) - {"day_multiplier", "month_multiplier", "project_clients"}
    if unknown:
        raise BillingCapsError(f"{BILLING_CAPS_ROUTING_KEY} has unsupported fields: {sorted(unknown)}")
    clients = declaration.get("project_clients", {})
    if not isinstance(clients, Mapping) or any(
        not isinstance(key, str) or not key or not isinstance(value, str) or not value
        for key, value in clients.items()
    ):
        raise BillingCapsError(f"{BILLING_CAPS_ROUTING_KEY}.project_clients must map project suffixes to client names")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise BillingCapsError("routing timezone is invalid") from exc
    return BillingPolicy(
        day_multiplier=_multiplier(declaration.get("day_multiplier"), DEFAULT_DAY_MULTIPLIER, "day_multiplier"),
        month_multiplier=_multiplier(declaration.get("month_multiplier"), DEFAULT_MONTH_MULTIPLIER, "month_multiplier"),
        project_clients=dict(clients),
        timezone=timezone,
    )


def _check_billing_interval(start: dt.datetime, end: dt.datetime) -> None:
    if not isinstance(start, dt.datetime) or not isinstance(end, dt.datetime):
        raise BillingCapsError("interval bounds must be datetimes")
    if start.tzinfo is None or end.tzinfo is None:
        raise BillingCapsError("interval bounds must be timezone-aware")
    if end <= start:
        raise BillingCapsError("interval must have positive duration")


def interval_seconds(intervals: Iterable[Interval]) -> int:
    return sum(int((end - start).total_seconds()) for start, end in intervals)


def merge_intervals(intervals: Iterable[Interval]) -> list[Interval]:
    """Return the union; touching intervals become one span."""
    merged: list[Interval] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def intersect_intervals(left: Iterable[Interval], right: Iterable[Interval]) -> list[Interval]:
    result: list[Interval] = []
    others = merge_intervals(right)
    for start, end in merge_intervals(left):
        for other_start, other_end in others:
            low, high = max(start, other_start), min(end, other_end)
            if low < high:
                result.append((low, high))
    return result


def _floor_minute(value: dt.datetime) -> dt.datetime:
    return value.replace(second=0, microsecond=0)


def _ceil_minute(value: dt.datetime) -> dt.datetime:
    floored = _floor_minute(value)
    return floored if floored == value else floored + dt.timedelta(minutes=1)


@dataclass(frozen=True)
class BillingClaim:
    """Observed session time one client could be billed for.

    ``density`` is the estimated effort per second of the claim (0..1).  It
    weighs the client's share of time that another client claims as well.
    """

    client: str
    start: dt.datetime
    end: dt.datetime
    density: Fraction = Fraction(1)

    def __post_init__(self) -> None:
        _check_billing_interval(self.start, self.end)
        if not self.client:
            raise BillingCapsError("a claim needs a client")
        if self.density < 0:
            raise BillingCapsError("claim density must not be negative")


@dataclass(frozen=True)
class ExclusiveShares:
    """Per client, the time its sessions may use; no two clients share any."""

    owned: Mapping[str, tuple[Interval, ...]]
    contested_seconds: int
    blocked_seconds: Mapping[str, int]

    def clip(self, client: str, intervals: Iterable[Interval]) -> list[Interval]:
        return intersect_intervals(intervals, self.owned.get(client, ()))


def exclusive_shares(
    claims: Sequence[BillingClaim],
    blocked: Sequence[tuple[str, dt.datetime, dt.datetime]] = (),
) -> ExclusiveShares:
    """Give every claimed second to at most one client.

    ``blocked`` holds fixed entries as ``(client, start, end)``: a client may
    not use time covered by another client's fixed entry.  The remaining time
    claimed by several clients is split per contiguous run, in whole minutes,
    proportionally to the claims' weights; rounding is carried to the next run
    with the same contenders so the proportion holds over a day.
    """
    for client, start, end in blocked:
        _check_billing_interval(start, end)
    points = sorted({
        point
        for claim in claims for point in (claim.start, claim.end)
    } | {
        point
        for _client, start, end in blocked for point in (start, end)
    })
    owned: dict[str, list[Interval]] = {}
    blocked_seconds: dict[str, int] = {}
    contested_seconds = 0
    carry: dict[tuple[tuple[str, ...], str], Fraction] = {}
    run: list[tuple[dt.datetime, dt.datetime, dict[str, Fraction]]] = []
    run_key: tuple[str, ...] = ()

    def close_run() -> None:
        nonlocal run, run_key, contested_seconds
        if not run:
            return
        start, end = run[0][0], run[-1][1]
        contested_seconds += int((end - start).total_seconds())
        weight = {
            client: sum(
                (weights[client] * int((high - low).total_seconds()) for low, high, weights in run),
                Fraction(0),
            )
            for client in run_key
        }
        if not any(weight.values()):
            weight = {client: Fraction(1) for client in run_key}
        total = sum(weight.values(), Fraction(0))
        # Whoever was already working keeps the head of the run and whoever
        # continues afterwards gets its tail, so shares stay contiguous.
        touching = [claim for claim in claims if claim.start < end and start < claim.end]
        order = sorted(run_key, key=lambda client: (
            min(claim.start for claim in touching if claim.client == client),
            max(claim.end for claim in touching if claim.client == client),
            client,
        ))
        grid_start, grid_end = _ceil_minute(start), _floor_minute(end)
        minutes = max(0, int((grid_end - grid_start).total_seconds()) // MINUTE)
        if minutes == 0:
            # Shorter than one aligned minute: it cannot be divided into
            # billable whole minutes, so the contender owed the most takes it.
            length = Fraction(int((end - start).total_seconds()), MINUTE)
            entitlement = {
                client: length * weight[client] / total + carry.get((run_key, client), Fraction(0))
                for client in run_key
            }
            winner = max(order, key=lambda client: (entitlement[client], -order.index(client)))
            for client in run_key:
                carry[(run_key, client)] = entitlement[client] - (length if client == winner else 0)
            owned.setdefault(winner, []).append((start, end))
            run, run_key = [], ()
            return
        entitlement = {
            client: Fraction(minutes) * weight[client] / total + carry.get((run_key, client), Fraction(0))
            for client in run_key
        }
        share = {client: 0 for client in run_key}
        for _ in range(minutes):
            winner = max(order, key=lambda client: (entitlement[client] - share[client], -order.index(client)))
            share[winner] += 1
        for client in run_key:
            carry[(run_key, client)] = entitlement[client] - share[client]
        holders = [client for client in order if share[client]]
        cursor = start
        for index, client in enumerate(holders):
            chunk_end = (
                end if index == len(holders) - 1
                else (grid_start if index == 0 else cursor) + dt.timedelta(minutes=share[client])
            )
            owned.setdefault(client, []).append((cursor, chunk_end))
            cursor = chunk_end
        run, run_key = [], ()

    for low, high in zip(points, points[1:]):
        weights: dict[str, Fraction] = {}
        for claim in claims:
            if claim.start <= low and high <= claim.end:
                weights[claim.client] = weights.get(claim.client, Fraction(0)) + claim.density
        if not weights:
            close_run()
            continue
        blockers = {client for client, start, end in blocked if start <= low and high <= end}
        eligible = tuple(sorted(client for client in weights if not blockers - {client}))
        for client in weights:
            if client not in eligible:
                blocked_seconds[client] = blocked_seconds.get(client, 0) + int((high - low).total_seconds())
        if len(eligible) < 2:
            close_run()
            if eligible:
                owned.setdefault(eligible[0], []).append((low, high))
            continue
        if run and (run_key != eligible or run[-1][1] != low):
            close_run()
        run_key = eligible
        run.append((low, high, {client: weights[client] for client in eligible}))
    close_run()
    return ExclusiveShares(
        owned={client: tuple(merge_intervals(intervals)) for client, intervals in owned.items()},
        contested_seconds=contested_seconds,
        blocked_seconds=blocked_seconds,
    )


@dataclass
class _Day:
    billed: int
    intervals: list[Interval]

    def wall(self, extra: Interval | None = None) -> int:
        return interval_seconds(merge_intervals([*self.intervals, *([extra] if extra else [])]))


class CapLedger:
    """Running billed and wall-clock totals per client and day.

    Fixed entries and non-overlapping allocations are added unconditionally.
    Time that would overlap what the client is already billed for is offered
    through ``acceptable_seconds`` and only granted within the caps.
    """

    def __init__(
        self,
        policy: BillingPolicy,
        *,
        month_scope: str = MONTH_SCOPE_PER_DAY,
        month_context: Mapping[tuple[str, int, int], tuple[int, int]] | None = None,
    ) -> None:
        if month_scope not in {MONTH_SCOPE_PER_DAY, MONTH_SCOPE_CUMULATIVE}:
            raise BillingCapsError("unsupported month scope")
        self.policy = policy
        self.month_scope = month_scope
        self._zone = ZoneInfo(policy.timezone)
        self._days: dict[tuple[str, dt.date], _Day] = {}
        # (client, year, month) -> (billed seconds, wall seconds) known from elsewhere.
        self._month_context = dict(month_context or {})

    def day_of(self, start: dt.datetime) -> dt.date:
        return start.astimezone(self._zone).date()

    def add(self, client: str, start: dt.datetime, end: dt.datetime) -> None:
        _check_billing_interval(start, end)
        day = self._days.setdefault((client, self.day_of(start)), _Day(0, []))
        day.billed += int((end - start).total_seconds())
        day.intervals.append((start, end))

    def _month_totals(self, client: str, day: dt.date) -> tuple[int, int]:
        billed, wall = self._month_context.get((client, day.year, day.month), (0, 0))
        for (other_client, other_day), state in self._days.items():
            if other_client == client and other_day != day and (other_day.year, other_day.month) == (day.year, day.month):
                billed += state.billed
                wall += state.wall()
        return billed, wall

    def _fits(self, client: str, start: dt.datetime, length: int, *, month: bool) -> bool:
        day_key = self.day_of(start)
        state = self._days.get((client, day_key)) or _Day(0, [])
        candidate = (start, start + dt.timedelta(seconds=length))
        wall_before, wall_after = state.wall(), state.wall(candidate)
        if length == wall_after - wall_before:
            # Pure wall-clock time: it adds nothing billed in parallel.
            return True
        billed_after = state.billed + length
        if billed_after > self.policy.day_multiplier * wall_after:
            return False
        if not month:
            return True
        if self.month_scope == MONTH_SCOPE_PER_DAY:
            return billed_after <= self.policy.month_multiplier * wall_after
        month_billed, month_wall = self._month_totals(client, day_key)
        return month_billed + billed_after <= self.policy.month_multiplier * (month_wall + wall_after)

    def _longest(self, client: str, start: dt.datetime, end: dt.datetime, *, month: bool) -> int:
        total = int((end - start).total_seconds())
        if self._fits(client, start, total, month=month):
            return total
        length = (total - 1) // MINUTE * MINUTE
        while length > 0:
            if self._fits(client, start, length, month=month):
                return length
            length -= MINUTE
        return 0

    def acceptable_seconds(self, client: str, start: dt.datetime, end: dt.datetime) -> int:
        """Longest prefix of the interval the caps allow: all of it, else whole minutes."""
        _check_billing_interval(start, end)
        return self._longest(client, start, end, month=True)

    def acceptable_seconds_day_only(self, client: str, start: dt.datetime, end: dt.datetime) -> int:
        """The same prefix under the day cap alone; tells which cap bound."""
        _check_billing_interval(start, end)
        return self._longest(client, start, end, month=False)

    def client_days(self) -> list[dict[str, Any]]:
        return [
            {
                "client": client,
                "day": day.isoformat(),
                "billed_seconds": state.billed,
                "wall_seconds": state.wall(),
            }
            for (client, day), state in sorted(self._days.items())
        ]


def measure_billing(rows: Iterable[tuple[str, dt.datetime, dt.datetime]], policy: BillingPolicy) -> dict[str, Any]:
    """Measure billed rows the way the read-only timesheet checker does."""
    rows = list(rows)
    ledger = CapLedger(policy)
    for client, start, end in rows:
        ledger.add(client, start, end)
    days = ledger.client_days()
    months: dict[tuple[str, str], list[int]] = {}
    for row in days:
        totals = months.setdefault((row["client"], row["day"][:7]), [0, 0])
        totals[0] += row["billed_seconds"]
        totals[1] += row["wall_seconds"]
    points = sorted({point for _client, start, end in rows for point in (start, end)})
    cross = 0
    for low, high in zip(points, points[1:]):
        if len({client for client, start, end in rows if start <= low and high <= end}) > 1:
            cross += int((high - low).total_seconds())
    return {
        "client_days": days,
        "client_months": [
            {"client": client, "month": month, "billed_seconds": billed, "wall_seconds": wall}
            for (client, month), (billed, wall) in sorted(months.items())
        ],
        "cross_client_seconds": cross,
        "days_over_cap": [
            row for row in days
            if row["billed_seconds"] > policy.day_multiplier * row["wall_seconds"]
        ],
        "months_over_cap": [
            {"client": client, "month": month, "billed_seconds": billed, "wall_seconds": wall}
            for (client, month), (billed, wall) in sorted(months.items())
            if billed > policy.month_multiplier * wall
        ],
    }


def cap_entries(
    entries: Sequence[Mapping[str, Any]],
    policy: BillingPolicy,
    *,
    month_scope: str = MONTH_SCOPE_CUMULATIVE,
) -> dict[str, Any]:
    """Apply both rules to already-billed entries; an offline what-if only.

    Entries are ``{"id", "client", "start", "end"}`` with aware datetimes and
    are processed in chronological order: where one client's entries overlap
    beyond a cap, the later entry is the one shortened.
    """
    ordered = sorted(entries, key=lambda row: (row["start"], row["end"], str(row["id"])))
    shares = exclusive_shares([BillingClaim(str(row["client"]), row["start"], row["end"]) for row in ordered])
    ledger = CapLedger(policy, month_scope=month_scope)
    kept: list[dict[str, Any]] = []
    cut = {"cross_client": 0, "day_cap": 0, "month_cap": 0}
    per_client: dict[str, dict[str, int]] = {}
    pieces: list[tuple[dt.datetime, dt.datetime, Mapping[str, Any]]] = []
    for row in ordered:
        client = str(row["client"])
        totals = per_client.setdefault(client, {"billed_before": 0, "billed_after": 0, **{key: 0 for key in cut}})
        original = int((row["end"] - row["start"]).total_seconds())
        own = shares.clip(client, [(row["start"], row["end"])])
        lost = original - interval_seconds(own)
        totals["billed_before"] += original
        totals["cross_client"] += lost
        cut["cross_client"] += lost
        pieces.extend((start, end, row) for start, end in own)
    for start, end, row in sorted(pieces, key=lambda piece: (piece[0], piece[1], str(piece[2]["id"]))):
        client = str(row["client"])
        totals = per_client[client]
        length = int((end - start).total_seconds())
        allowed = ledger.acceptable_seconds(client, start, end)
        day_only = ledger.acceptable_seconds_day_only(client, start, end)
        totals["day_cap"] += length - day_only
        totals["month_cap"] += day_only - allowed
        cut["day_cap"] += length - day_only
        cut["month_cap"] += day_only - allowed
        if allowed:
            piece_end = start + dt.timedelta(seconds=allowed)
            ledger.add(client, start, piece_end)
            totals["billed_after"] += allowed
            kept.append({"id": row["id"], "client": client, "start": start, "end": piece_end})
    return {
        "entries": kept,
        "contested_wall_seconds": shares.contested_seconds,
        "cut_seconds": cut,
        "clients": per_client,
        "client_days": ledger.client_days(),
    }
