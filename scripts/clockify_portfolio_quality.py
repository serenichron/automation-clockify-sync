#!/usr/bin/env python3
"""Audit immutable-evidence and allocation integrity of a portfolio review.

The audit is deliberately read-only.  It identifies wording rows that need the
separate Flash repair stage, but never changes their descriptions itself.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import re
import statistics
import sys
from typing import Any, Mapping, Sequence

try:
    from scripts import caveman_renderer, evidence_ledger, work_accounting_pipeline
except ImportError:  # pragma: no cover - direct execution fallback
    import caveman_renderer  # type: ignore[no-redef]
    import evidence_ledger  # type: ignore[no-redef]
    import work_accounting_pipeline  # type: ignore[no-redef]


REQUIRED_MODELS = frozenset({
    "deepseek-v4-flash:cloud",
    "deepseek-v4-flash:0731-cloud",
})
REQUIRED_MODEL = "deepseek-v4-flash:cloud"
REQUIRED_REVISION = "6ca9e29c41ded618e527ee40e305ed5e4d8319b571d5b6695a30e1df65f103cc"
_PREFIX_RE = re.compile(r"^([A-Za-z][A-Za-z0-9 &-]{0,24}) — ")


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _parse(value: Any) -> dt.datetime:
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _positive_minutes(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return None
    return minutes if minutes > 0 else None


def _nonnegative_minutes(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return None
    return minutes if minutes >= 0 else None


def _review_range(document: Mapping[str, Any]) -> tuple[dt.date, dt.date] | None:
    source = document.get("range")
    if not isinstance(source, Mapping):
        return None
    try:
        since = dt.date.fromisoformat(str(source.get("since") or ""))
        until = dt.date.fromisoformat(str(source.get("until") or ""))
    except ValueError:
        return None
    return (since, until) if since <= until else None


def _validated_ledger(ledger_document: Mapping[str, Any] | None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Return immutable ledger events or reject anything not digest-bound."""
    if not isinstance(ledger_document, Mapping):
        raise ValueError("immutable evidence ledger is required")
    if ledger_document.get("schema_version") != evidence_ledger.SCHEMA_VERSION:
        raise ValueError("unsupported evidence ledger schema")
    raw_events = ledger_document.get("events")
    manifest_document = ledger_document.get("manifest")
    if not isinstance(raw_events, list) or not isinstance(manifest_document, Mapping):
        raise ValueError("evidence ledger requires manifest and event list")
    if not all(isinstance(event, Mapping) for event in raw_events):
        raise ValueError("evidence ledger events must be objects")
    events = [evidence_ledger.EvidenceEvent.from_document(event) for event in raw_events]
    ids = [event.evidence_id for event in events]
    if len(ids) != len(set(ids)):
        raise ValueError("evidence ledger contains duplicate evidence IDs")
    candidate_manifest = evidence_ledger.LedgerManifest.from_document(manifest_document)
    immutable = evidence_ledger.EvidenceLedger(tuple(events), candidate_manifest.source_inventory)
    immutable.validate(candidate_manifest)
    return [event.document() for event in immutable.events], immutable.manifest.document()


def _event_interval(event: Mapping[str, Any]) -> tuple[dt.datetime, dt.datetime] | None:
    """Use accounting's Bucharest-normalized source span contract."""
    start, end = work_accounting_pipeline._span(event)
    if start is None or end is None:
        return None
    return (start, end) if end > start else None


def _covers(pool: Sequence[tuple[dt.datetime, dt.datetime]], start: dt.datetime, end: dt.datetime) -> bool:
    """Return whether contiguous authoritative proposal time covers a segment."""
    cursor = start
    for pool_start, pool_end in sorted(pool):
        if pool_end <= cursor:
            continue
        if pool_start > cursor:
            return False
        cursor = max(cursor, pool_end)
        if cursor >= end:
            return True
    return False


def _coverage_exclusion(coverage: dict[str, Any], evidence_id: str, reason: str) -> None:
    coverage["excluded"] += 1
    coverage["excluded_by_reason"][reason] = coverage["excluded_by_reason"].get(reason, 0) + 1
    coverage["excluded_evidence"].append({"evidence_id": evidence_id, "reason": reason})


def _reconciled_by_existing_clockify(
    meeting: Mapping[str, Any], clockify_blocks: Sequence[Mapping[str, Any]]
) -> bool:
    """Use the accounting pipeline's reciprocal-overlap reconciliation rule."""
    interval = _event_interval(meeting)
    if interval is None:
        return False
    start, end = interval
    overlapping = [
        block for block in clockify_blocks
        if work_accounting_pipeline._overlap_ratio(start, end, block["start"], block["end"]) > 0
    ]
    matching = [
        block for block in overlapping
        if work_accounting_pipeline._meeting_matches_existing_block(start, end, block)
    ]
    return len(overlapping) == 1 and len(matching) == 1


def _accounted_fathom_ids(document: Mapping[str, Any]) -> tuple[set[str], list[dict[str, Any]]]:
    accounted: set[str] = set()
    issues: list[dict[str, Any]] = []
    for collection in ("exceptions", "omissions"):
        entries = document.get(collection, [])
        if not isinstance(entries, list):
            issues.append({"reason": f"{collection} must be a list"})
            continue
        for entry in entries:
            if not isinstance(entry, Mapping):
                issues.append({"reason": f"{collection} contains a non-object entry"})
                continue
            evidence_ids = entry.get("evidence_ids")
            reason = str(entry.get("reason") or "").strip()
            if not isinstance(evidence_ids, list):
                continue
            if not reason:
                issues.append({"reason": f"{collection} evidence account has no reason"})
                continue
            for evidence_id in evidence_ids:
                value = str(evidence_id or "")
                if value:
                    accounted.add(value)
    return accounted, issues


def _routing_taxonomy(routing: Mapping[str, Any] | None) -> dict[tuple[str, tuple[str, ...]], set[str]]:
    """Map exact project/tag selections to their allowed rendered prefixes."""
    if not isinstance(routing, Mapping):
        raise ValueError("canonical routing taxonomy is required")
    result: dict[tuple[str, tuple[str, ...]], set[str]] = {}
    for section in ("session_routes", "meeting_routes"):
        routes = routing.get(section, [])
        if not isinstance(routes, list):
            raise ValueError(f"routing {section} must be a list")
        for route in routes:
            if not isinstance(route, Mapping):
                raise ValueError("routing entry must be an object")
            project = str(route.get("project_name") or "").strip()
            prefix = str(route.get("prefix") or "").strip()
            raw_tags = route.get("tag_names")
            if not project or not prefix or not isinstance(raw_tags, list):
                continue
            tags = tuple(sorted(str(tag).strip() for tag in raw_tags if str(tag).strip()))
            if not tags:
                continue
            result.setdefault((project, tags), set()).add(prefix)
    for override in routing.get("prefix_overrides", []):
        if not isinstance(override, Mapping):
            raise ValueError("routing prefix override must be an object")
        project_prefix = str(override.get("project_name_prefix") or "").casefold()
        prefix = str(override.get("prefix") or "").strip()
        if not project_prefix or not prefix:
            continue
        for (project, _tags), allowed in result.items():
            if project.casefold().startswith(project_prefix):
                allowed.add(prefix)
    if not result:
        raise ValueError("canonical routing taxonomy has no usable routes")
    return result


def _row_route(row: Mapping[str, Any], review_id: str) -> tuple[str, tuple[str, ...], str] | None:
    project = str(row.get("client_project") or "").strip()
    raw_tags = row.get("tag_names")
    description = row.get("description")
    if not project or not isinstance(raw_tags, list) or not isinstance(description, str):
        return None
    tags = tuple(sorted(str(tag).strip() for tag in raw_tags if str(tag).strip()))
    match = _PREFIX_RE.match(description)
    if not tags or match is None:
        return None
    return project, tags, match.group(1)


def _proposal_routes_by_activity(
    source_proposals: Sequence[Mapping[str, Any]] | None,
) -> dict[str, set[tuple[str, tuple[str, ...]]]]:
    result: dict[str, set[tuple[str, tuple[str, ...]]]] = {}
    if source_proposals is None:
        return result
    for proposal in source_proposals:
        activity_id = str(proposal.get("activity_id") or "").strip()
        project = str(proposal.get("client_project") or "").strip()
        tags = proposal.get("tag_names")
        if not activity_id or not project or not isinstance(tags, list):
            continue
        normalized_tags = tuple(sorted(str(tag).strip() for tag in tags if str(tag).strip()))
        if normalized_tags:
            result.setdefault(activity_id, set()).add((project, normalized_tags))
    return result


def _fathom_coverage(
    document: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    row_intervals_by_evidence: Mapping[str, Sequence[tuple[dt.datetime, dt.datetime]]],
    review_range: tuple[dt.date, dt.date] | None,
    manifest: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    coverage: dict[str, Any] = {
        "expected": 0,
        "represented": 0,
        "excluded": 0,
        "missing": 0,
        "unverifiable": 0,
        "uncovered": 0,
        "missing_evidence_ids": [],
        "excluded_by_reason": {},
        "excluded_evidence": [],
    }
    issues: list[dict[str, Any]] = []
    inventory = manifest.get("source_inventory")
    fathom_inventory = inventory.get("fathom") if isinstance(inventory, Mapping) else None
    fathom_events = [event for event in events if event.get("source_type") == "fathom"]
    clockify_blocks = work_accounting_pipeline._existing_blocks(events)
    if not isinstance(fathom_inventory, Mapping) or fathom_inventory.get("status") != "complete":
        issues.append({"reason": "Fathom source completeness cannot be proved"})
    elif (
        "expected_count" in fathom_inventory
        and "observed_count" in fathom_inventory
        and fathom_inventory["expected_count"] != fathom_inventory["observed_count"]
    ):
        issues.append({"reason": "Fathom source expected and observed counts differ"})
    elif isinstance(fathom_inventory.get("observed_count"), int) and fathom_inventory["observed_count"] != len(fathom_events):
        issues.append({"reason": "Fathom source inventory does not match immutable ledger events"})
    accounted, account_issues = _accounted_fathom_ids(document)
    issues.extend(account_issues)
    if review_range is None:
        return coverage, issues
    start_day, end_day = review_range
    for event in fathom_events:
        evidence_id = str(event.get("evidence_id") or "")
        interval = _event_interval(event)
        if interval is None:
            coverage["unverifiable"] += 1
            issues.append({"reason": "Fathom event has no valid meeting window", "evidence_id": evidence_id})
            continue
        start, end = interval
        if end.date() < start_day or start.date() > end_day:
            _coverage_exclusion(coverage, evidence_id, "outside_review_range")
            continue
        eligible, exclusion = work_accounting_pipeline._meeting_is_eligible(event)
        if not eligible:
            _coverage_exclusion(coverage, evidence_id, f"ineligible:{exclusion or 'unknown'}")
            continue
        coverage["expected"] += 1
        row_intervals = row_intervals_by_evidence.get(evidence_id, [])
        if row_intervals and _covers(row_intervals, start, end):
            coverage["represented"] += 1
        elif row_intervals:
            coverage["uncovered"] += 1
            coverage["missing"] += 1
            coverage["missing_evidence_ids"].append(evidence_id)
            issues.append({"reason": "eligible Fathom meeting interval is not covered by cited row allocation", "evidence_id": evidence_id})
        elif _reconciled_by_existing_clockify(event, clockify_blocks):
            _coverage_exclusion(coverage, evidence_id, "existing_clockify_meeting_match")
        elif evidence_id in accounted:
            _coverage_exclusion(coverage, evidence_id, "document_omission_or_exception")
        else:
            coverage["missing"] += 1
            coverage["missing_evidence_ids"].append(evidence_id)
    if coverage["missing"]:
        issues.append({"reason": "eligible Fathom coverage incomplete", "count": coverage["missing"]})
    return coverage, issues


def _source_pool_issues(
    source_proposals: Sequence[Mapping[str, Any]] | None,
    segments: Sequence[tuple[dt.datetime, dt.datetime, str]],
    document: Mapping[str, Any],
    review_range: tuple[dt.date, dt.date] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    metrics: dict[str, Any] = {
        "available": source_proposals is not None,
        "in_range_proposal_count": 0,
        "in_range_proposal_minutes": 0,
    }
    if source_proposals is None:
        return [], metrics
    pool: list[tuple[dt.datetime, dt.datetime]] = []
    issues: list[dict[str, Any]] = []
    proposal_minutes = 0
    for proposal in source_proposals:
        try:
            start = _parse(proposal.get("start"))
        except (AttributeError, TypeError, ValueError):
            issues.append({"reason": "invalid authoritative source proposal window"})
            continue
        if review_range is None:
            continue
        if not review_range[0] <= start.date() <= review_range[1]:
            continue
        metrics["in_range_proposal_count"] += 1
        try:
            end = _parse(proposal.get("end"))
        except (TypeError, ValueError):
            issues.append({"reason": "invalid authoritative source proposal window"})
            continue
        minutes = _positive_minutes(proposal.get("duration_minutes"))
        actual = int((end - start).total_seconds() // 60)
        if end <= start or minutes is None or minutes != actual:
            issues.append({"reason": "invalid authoritative source proposal duration"})
            continue
        pool.append((start, end))
        proposal_minutes += minutes
        metrics["in_range_proposal_minutes"] += minutes
    for prior, current in zip(sorted(pool), sorted(pool)[1:]):
        if current[0] < prior[1]:
            issues.append({"reason": "authoritative source proposal pool overlaps"})
            break
    source_minutes = document.get("source_minutes")
    if source_minutes is not None:
        if _nonnegative_minutes(source_minutes) != proposal_minutes:
            issues.append({"reason": "source_minutes does not match authoritative proposal pool"})
    for start, end, review_id in segments:
        if not _covers(pool, start, end):
            issues.append({"review_id": review_id, "reason": "allocation lies outside authoritative source proposal pool"})
    return issues, metrics


def _minute_accounting_issues(
    document: Mapping[str, Any], row_total: int, segment_total: int
) -> list[dict[str, Any]]:
    """Validate the review's explicit retained-versus-excluded minute ledger."""
    issues: list[dict[str, Any]] = []
    totals: dict[str, int] = {}
    for field in ("source_minutes", "review_minutes", "excluded_minutes"):
        value = _nonnegative_minutes(document.get(field)) if field in document else None
        if value is None:
            issues.append({"reason": f"missing or invalid {field}"})
        else:
            totals[field] = value
    if len(totals) == 3:
        if totals["review_minutes"] != row_total or totals["review_minutes"] != segment_total:
            issues.append({"reason": "review_minutes does not equal row and segment minutes"})
        if totals["source_minutes"] != totals["review_minutes"] + totals["excluded_minutes"]:
            issues.append({"reason": "source_minutes does not equal review_minutes plus excluded_minutes"})
    groups = document.get("groups")
    if not isinstance(groups, list) or not all(isinstance(group, Mapping) for group in groups):
        return [*issues, {"reason": "groups must be a list of accounting objects"}]
    group_totals = {"source_minutes": 0, "review_minutes": 0, "excluded_minutes": 0}
    for index, group in enumerate(groups, 1):
        values: dict[str, int] = {}
        for field in group_totals:
            value = _nonnegative_minutes(group.get(field))
            if value is None:
                issues.append({"group": index, "reason": f"missing or invalid group {field}"})
            else:
                values[field] = value
                group_totals[field] += value
        if len(values) == 3 and values["source_minutes"] != values["review_minutes"] + values["excluded_minutes"]:
            issues.append({"group": index, "reason": "group source minutes do not balance"})
        reasons = group.get("exclusion_reasons")
        exceptions = _nonnegative_minutes(group.get("exceptions"))
        omissions = _nonnegative_minutes(group.get("omissions"))
        if not isinstance(reasons, list):
            issues.append({"group": index, "reason": "group exclusion_reasons must be a list"})
            reasons = []
        if exceptions is None or omissions is None:
            issues.append({"group": index, "reason": "group exception and omission counts must be nonnegative"})
        if values.get("excluded_minutes", 0) > 0:
            if not reasons:
                issues.append({"group": index, "reason": "excluded group minutes lack evidence-backed exclusion reasons"})
            if (exceptions or 0) + (omissions or 0) <= 0:
                issues.append({"group": index, "reason": "excluded group minutes lack exception or omission count"})
            for reason in reasons:
                if (
                    not isinstance(reason, Mapping)
                    or reason.get("disposition") not in {"exception", "omission"}
                    or not str(reason.get("reason") or "").strip()
                    or _positive_minutes(reason.get("evidence_count")) is None
                ):
                    issues.append({"group": index, "reason": "group exclusion reason is not evidence-backed"})
        elif reasons:
            issues.append({"group": index, "reason": "group has exclusion reasons without excluded minutes"})
    if len(totals) == 3 and group_totals != totals:
        issues.append({"reason": "group minute totals do not equal portfolio totals"})
    return issues


def audit(
    document: Mapping[str, Any],
    evidence_ledger_document: Mapping[str, Any] | None = None,
    *,
    source_proposals: Sequence[Mapping[str, Any]] | None = None,
    routing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a fail-closed integrity report without changing the review."""
    structural: list[dict[str, Any]] = []
    semantic_repairs: list[dict[str, Any]] = []
    rows = document.get("activities", []) if isinstance(document, Mapping) else []
    if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
        return {
            "schema_version": 2,
            "status": "blocked",
            "external_writes": False,
            "structural_issues": [{"reason": "activities must be a list of objects"}],
            "semantic_repair_rows": [],
            "fathom_coverage": {"expected": 0, "represented": 0, "excluded": 0, "missing": 0, "unverifiable": 0, "missing_evidence_ids": [], "excluded_by_reason": {}, "excluded_evidence": []},
        }
    review_range = _review_range(document)
    if review_range is None:
        structural.append({"reason": "invalid review range"})
    events: list[dict[str, Any]] = []
    manifest: dict[str, Any] = {}
    try:
        events, manifest = _validated_ledger(evidence_ledger_document)
    except (TypeError, ValueError) as exc:
        structural.append({"reason": f"immutable evidence ledger invalid: {exc}"})

    if document.get("model") not in REQUIRED_MODELS:
        structural.append({"reason": "document model is not the required Flash model"})
    if document.get("revision") != REQUIRED_REVISION:
        structural.append({"reason": "document revision is not the required Flash revision"})

    ids: set[str] = set()
    row_evidence_ids: set[str] = set()
    row_intervals_by_evidence: dict[str, list[tuple[dt.datetime, dt.datetime]]] = {}
    intervals: list[tuple[dt.datetime, dt.datetime, str]] = []
    durations: list[int] = []
    segment_total = 0
    try:
        taxonomy = _routing_taxonomy(routing) if routing is not None else {}
    except ValueError as exc:
        taxonomy = {}
        structural.append({"reason": str(exc)})
    proposal_routes = _proposal_routes_by_activity(source_proposals)
    for row in rows:
        review_id = str(row.get("review_id") or "")
        if not review_id or review_id in ids:
            structural.append({"review_id": review_id, "reason": "missing or duplicate review ID"})
        ids.add(review_id)
        evidence_ids = row.get("evidence_ids")
        normalized_ids: list[str] = []
        if not isinstance(evidence_ids, list) or not evidence_ids:
            structural.append({"review_id": review_id, "reason": "missing evidence provenance"})
        else:
            normalized_ids = [str(value or "") for value in evidence_ids]
            if "" in normalized_ids or len(normalized_ids) != len(set(normalized_ids)):
                structural.append({"review_id": review_id, "reason": "duplicate or empty evidence ID in row"})
            unknown = sorted(set(normalized_ids) - {event.get("evidence_id") for event in events})
            if unknown:
                structural.append({"review_id": review_id, "reason": "row evidence absent from immutable ledger", "evidence_ids": unknown})
            row_evidence_ids.update(normalized_ids)
        project = str(row.get("client_project") or "").strip()
        tags = row.get("tag_names")
        if not project or not isinstance(tags, list) or not tags or any(not str(tag).strip() for tag in tags):
            structural.append({"review_id": review_id, "reason": "missing project or tag provenance"})
        if routing is not None:
            route = _row_route(row, review_id)
            if route is None:
                structural.append({"review_id": review_id, "reason": "description lacks an exact route prefix/project/tag selection"})
            elif route[2] not in taxonomy.get((route[0], route[1]), set()):
                structural.append({"review_id": review_id, "reason": "description prefix/project/tag route is absent from canonical routing taxonomy"})
            source_activity_ids = row.get("source_activity_ids")
            if not isinstance(source_activity_ids, list) or not source_activity_ids:
                structural.append({"review_id": review_id, "reason": "missing source activity route provenance"})
            else:
                source_ids = [str(value or "") for value in source_activity_ids]
                if "" in source_ids or len(source_ids) != len(set(source_ids)):
                    structural.append({"review_id": review_id, "reason": "invalid source activity route provenance"})
                source_routes = set().union(*(proposal_routes.get(value, set()) for value in source_ids))
                if len(source_routes) != 1:
                    structural.append({"review_id": review_id, "reason": "cited source proposals have mixed, missing, or unknown routes"})
                elif route is not None and (route[0], route[1]) != next(iter(source_routes)):
                    structural.append({"review_id": review_id, "reason": "row route differs from cited source proposal route"})
        if row.get("semantic_reviewer_model") not in REQUIRED_MODELS:
            structural.append({"review_id": review_id, "reason": "row model is not the required Flash model"})
        if row.get("semantic_reviewer_revision") != REQUIRED_REVISION:
            structural.append({"review_id": review_id, "reason": "row revision is not the required Flash revision"})
        if row.get("validation_status") != "flash_validated":
            structural.append({
                "review_id": review_id,
                "reason": "row lacks successful Flash portfolio validation",
            })
        description = row.get("description")
        try:
            caveman_renderer.validate_description(
                description,
                max_words=24,
                allow_compact_technical_slashes=True,
                allow_compact_technical_underscores=True,
            )
            words = len(description.split())
            if not 8 <= words <= 24:
                raise caveman_renderer.CavemanValidationError("description outside accepted 8-24 word convention")
        except (AttributeError, caveman_renderer.CavemanValidationError) as exc:
            semantic_repairs.append({
                "review_id": review_id,
                "reason": f"description requires Flash wording repair: {exc}",
                "description": description if isinstance(description, str) else "",
            })
        segments = row.get("allocation_segments")
        if not isinstance(segments, list) or not segments:
            structural.append({"review_id": review_id, "reason": "missing allocation segments"})
            segments = []
        row_segment_minutes = 0
        parsed_segments: list[tuple[dt.datetime, dt.datetime]] = []
        for segment in segments:
            if not isinstance(segment, Mapping):
                structural.append({"review_id": review_id, "reason": "invalid allocation segment"})
                continue
            try:
                start, end = _parse(segment.get("start")), _parse(segment.get("end"))
            except (TypeError, ValueError):
                structural.append({"review_id": review_id, "reason": "invalid allocation segment"})
                continue
            minutes = _positive_minutes(segment.get("duration_minutes"))
            actual = int((end - start).total_seconds() // 60)
            if end <= start or minutes is None or minutes != actual:
                structural.append({"review_id": review_id, "reason": "invalid allocation segment"})
                continue
            if review_range is not None and not (review_range[0] <= start.date() <= review_range[1] and review_range[0] <= end.date() <= review_range[1]):
                structural.append({"review_id": review_id, "reason": "allocation outside review range"})
            row_segment_minutes += minutes
            segment_total += minutes
            parsed_segments.append((start, end))
            intervals.append((start, end, review_id))
        for evidence_id in normalized_ids if isinstance(evidence_ids, list) else []:
            row_intervals_by_evidence.setdefault(evidence_id, []).extend(parsed_segments)
        duration = _positive_minutes(row.get("duration_minutes"))
        if duration is None or duration != row_segment_minutes:
            structural.append({"review_id": review_id, "reason": "duration does not match segments"})
        else:
            durations.append(duration)
        if parsed_segments and (row.get("start") is not None or row.get("end") is not None):
            if row.get("start") != min(start for start, _ in parsed_segments).isoformat(timespec="minutes") or row.get("end") != max(end for _, end in parsed_segments).isoformat(timespec="minutes"):
                structural.append({"review_id": review_id, "reason": "row bounds do not match allocation segments"})
    intervals.sort()
    overlaps = []
    for prior, current in zip(intervals, intervals[1:]):
        if current[0] < prior[1]:
            overlaps.append({"first_review_id": prior[2], "second_review_id": current[2], "first_end": prior[1].isoformat(), "second_start": current[0].isoformat()})
    if overlaps:
        structural.append({"reason": "allocation overlaps", "count": len(overlaps)})
    row_total = sum(durations)
    structural.extend(_minute_accounting_issues(document, row_total, segment_total))
    if source_proposals is not None and not all(isinstance(row, Mapping) for row in source_proposals):
        structural.append({"reason": "authoritative source proposals must be objects"})
        source_pool = {"available": True, "in_range_proposal_count": 0, "in_range_proposal_minutes": 0}
    else:
        source_issues, source_pool = _source_pool_issues(
            source_proposals, intervals, document, review_range
        )
        structural.extend(source_issues)
    coverage, fathom_issues = _fathom_coverage(document, events, row_intervals_by_evidence, review_range, manifest)
    structural.extend(fathom_issues)
    fragmentation = {
        "row_count": len(rows),
        "total_minutes": row_total,
        "median_minutes": statistics.median(durations) if durations else 0,
        "rows_at_most_5_minutes": sum(value <= 5 for value in durations),
        "rows_at_most_10_minutes": sum(value <= 10 for value in durations),
    }
    return {
        "schema_version": 2,
        "status": "blocked" if structural else "needs_semantic_repair" if semantic_repairs else "pass",
        "external_writes": False,
        "structural_issues": structural,
        "semantic_repair_rows": semantic_repairs,
        "overlaps": overlaps,
        "fragmentation": fragmentation,
        "fathom_coverage": coverage,
        "source_proposal_coverage": source_pool,
        "ledger": {"manifest_id": manifest.get("manifest_id"), "event_count": manifest.get("event_count")} if manifest else None,
        "exceptions": len(document.get("exceptions", [])) if isinstance(document.get("exceptions", []), list) else None,
        "omissions": len(document.get("omissions", [])) if isinstance(document.get("omissions", []), list) else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("portfolio_review", type=Path)
    parser.add_argument("--evidence-ledger", type=Path, required=True)
    parser.add_argument("--source-proposals", type=Path)
    parser.add_argument("--routing", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        proposals = _read(args.source_proposals) if args.source_proposals else None
        report = audit(_read(args.portfolio_review), _read(args.evidence_ledger), source_proposals=proposals, routing=_read(args.routing))
        _write(args.output, report)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"clockify portfolio quality: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
