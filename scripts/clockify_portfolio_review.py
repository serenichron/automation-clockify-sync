#!/usr/bin/env python3
"""Consolidate reviewed Clockify micro-activities into invoice-worthy rows.

The script is a local packaging stage. It sends only the already-approved
semantic evidence projection to the pinned Flash reviewer, writes local JSON/CSV
artifacts, and never writes to Clockify, Google Sheets, Multica, or schedules.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import csv
import datetime as dt
import json
from pathlib import Path
import sys
import threading
from typing import Any, Callable, Iterable, Mapping

try:
    from scripts import semantic_analyzer
except ImportError:  # pragma: no cover - direct execution fallback
    import semantic_analyzer  # type: ignore[no-redef]


DEFAULT_MAX_ACTIVITIES = 20
DEFAULT_WORKERS = 4
TARGET_BODY_BYTES = 1_100_000
PORTFOLIO_SINGLE_ACTIVITY_RECOVERY_PROMPT_VERSION = (
    "clockify-portfolio-single-activity-recovery-v2"
)


class PortfolioReviewError(RuntimeError):
    """Raised when a review package cannot be produced safely."""


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _parse(value: Any) -> dt.datetime:
    return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _iso(value: dt.datetime) -> str:
    return value.isoformat(timespec="seconds")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _activity_date(activity: Mapping[str, Any], proposals: list[dict[str, Any]]) -> str:
    starts = [_parse(row["start"]) for row in proposals]
    if not starts:
        raise PortfolioReviewError(
            f"activity {activity.get('activity_id')} has no proposal allocation"
        )
    return min(starts).date().isoformat()


def _taxonomy(routing: Mapping[str, Any]) -> list[dict[str, Any]]:
    choices: dict[tuple[str, str, tuple[str, ...]], dict[str, Any]] = {}
    for section in ("session_routes", "meeting_routes"):
        for route in routing.get(section, []):
            if not isinstance(route, Mapping) or not route.get("project_name"):
                continue
            tags = tuple(sorted(str(value) for value in route.get("tag_names", [])))
            key = (
                str(route.get("project_name") or ""),
                str(route.get("prefix") or "SC"),
                tags,
            )
            choice = choices.setdefault(key, {
                "project_name": key[0],
                "prefix": key[1],
                "tag_names": list(tags),
                "billable": bool(route.get("billable", True)),
                "selection_guidance": [],
            })
            for field in ("pattern", "email_domain", "title_regex"):
                value = str(route.get(field) or "").strip()
                if value and value not in choice["selection_guidance"]:
                    choice["selection_guidance"].append(value)
    for choice in choices.values():
        choice["selection_guidance"].sort()
    return [choices[key] for key in sorted(choices)]


def _group_key(
    activity: Mapping[str, Any], proposals: list[dict[str, Any]]
) -> tuple[str, str, tuple[str, ...]]:
    project = activity.get("project_recommendation")
    if not isinstance(project, Mapping):
        project = {}
    return (
        _activity_date(activity, proposals),
        str(project.get("name") or proposals[0].get("client_project") or ""),
        tuple(sorted(str(value) for value in project.get("tag_names", []))),
    )


def _event_span(event: Mapping[str, Any]) -> tuple[dt.datetime, dt.datetime] | None:
    span = semantic_analyzer._safe_time_span(event)
    if span is None:
        return None
    return _parse(span["start"]), _parse(span["end"])


def _sort_activity(
    activity: Mapping[str, Any], events_by_id: Mapping[str, dict[str, Any]]
) -> tuple[str, str, str]:
    spans = [
        span
        for evidence_id in activity.get("evidence_ids", [])
        if (event := events_by_id.get(str(evidence_id))) is not None
        if (span := _event_span(event)) is not None
    ]
    earliest = min((span[0] for span in spans), default=dt.datetime.max)
    return (
        str(activity.get("workstream") or "").casefold(),
        earliest.isoformat(),
        str(activity.get("activity_id") or ""),
    )


def _body_size(
    activities: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    taxonomy: list[dict[str, Any]],
    model: str,
) -> int:
    body = semantic_analyzer._review_body(
        events,
        candidate={"activities": activities, "exceptions": [], "omissions": []},
        taxonomy=taxonomy,
        model=model,
        review_scope="portfolio",
        review_prompt_version=semantic_analyzer.PORTFOLIO_REVIEW_PROMPT_VERSION,
    )
    return len(semantic_analyzer.canonical_json(body).encode("utf-8"))


def _partition_group(
    activities: list[dict[str, Any]],
    *,
    events_by_id: Mapping[str, dict[str, Any]],
    taxonomy: list[dict[str, Any]],
    model: str,
    max_activities: int,
) -> list[list[dict[str, Any]]]:
    ordered = sorted(activities, key=lambda row: _sort_activity(row, events_by_id))
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for activity in ordered:
        candidate = [*current, activity]
        evidence_ids = {
            str(value)
            for row in candidate
            for value in row.get("evidence_ids", [])
        }
        events = [events_by_id[value] for value in sorted(evidence_ids)]
        too_large = (
            len(candidate) > max_activities
            or _body_size(candidate, events, taxonomy=taxonomy, model=model)
            > TARGET_BODY_BYTES
        )
        if too_large and current:
            groups.append(current)
            current = [activity]
        else:
            current = candidate
    if current:
        groups.append(current)
    return groups


def _split_minutes(total: int, weights: list[int]) -> list[int]:
    if total <= 0 or not weights:
        return [0 for _ in weights]
    safe = [max(1, int(value)) for value in weights]
    denominator = sum(safe)
    raw = [total * value / denominator for value in safe]
    result = [int(value) for value in raw]
    for index in sorted(
        range(len(result)), key=lambda value: raw[value] - result[value], reverse=True
    )[: total - sum(result)]:
        result[index] += 1
    return result


def _allocate_from_pool(
    intervals: list[tuple[dt.datetime, dt.datetime]], minutes: list[int]
) -> list[list[dict[str, Any]]]:
    pool = [[start, end] for start, end in sorted(intervals)]
    output: list[list[dict[str, Any]]] = []
    for requested in minutes:
        remaining = requested
        segments: list[dict[str, Any]] = []
        while remaining > 0 and pool:
            start, end = pool[0]
            available = max(0, int((end - start).total_seconds() // 60))
            if available == 0:
                pool.pop(0)
                continue
            used = min(remaining, available)
            segment_end = start + dt.timedelta(minutes=used)
            segments.append({
                "start": _iso(start),
                "end": _iso(segment_end),
                "duration_minutes": used,
            })
            remaining -= used
            if segment_end >= end:
                pool.pop(0)
            else:
                pool[0][0] = segment_end
        if remaining:
            raise PortfolioReviewError("portfolio allocation exceeded its source pool")
        output.append(segments)
    return output


def _description(activity: Mapping[str, Any]) -> str:
    project = activity.get("project_recommendation")
    prefix = project.get("prefix") if isinstance(project, Mapping) else ""
    return " ".join(
        str(value or "").strip()
        for value in (
            f"{prefix or 'SC'} —",
            activity.get("action"),
            activity.get("object"),
            activity.get("outcome"),
        )
        if str(value or "").strip()
    )


def _confidence(activity: Mapping[str, Any]) -> str:
    return str(activity.get("semantic_confidence") or "low")


def _minutes(proposals: Iterable[Mapping[str, Any]]) -> int:
    """Return authoritative proposal minutes, rejecting malformed values."""
    total = 0
    for proposal in proposals:
        value = proposal.get("duration_minutes")
        if isinstance(value, bool):
            raise PortfolioReviewError("source proposal duration must be a nonnegative integer")
        try:
            minutes = int(value or 0)
        except (TypeError, ValueError) as exc:
            raise PortfolioReviewError(
                "source proposal duration must be a nonnegative integer"
            ) from exc
        if minutes < 0:
            raise PortfolioReviewError("source proposal duration must be a nonnegative integer")
        total += minutes
    return total


def _seconds(proposals: Iterable[Mapping[str, Any]]) -> int:
    """Return exact source duration, rejecting any supplied seconds mismatch."""
    total = 0
    for proposal in proposals:
        declared = proposal.get("duration_seconds")
        if "start" not in proposal or "end" not in proposal:
            if declared is not None:
                if isinstance(declared, bool) or not isinstance(declared, int) or declared <= 0:
                    raise PortfolioReviewError("source proposal seconds must be a positive integer")
                total += declared
                continue
            minutes = _minutes([proposal])
            total += minutes * 60
            continue
        try:
            start, end = _parse(proposal["start"]), _parse(proposal["end"])
        except (KeyError, TypeError, ValueError) as error:
            raise PortfolioReviewError("source proposal bounds are invalid") from error
        actual = int((end - start).total_seconds())
        if declared is not None and (
            isinstance(declared, bool) or not isinstance(declared, int) or declared != actual
        ):
            raise PortfolioReviewError("source proposal seconds do not match its bounds")
        if actual <= 0:
            raise PortfolioReviewError("source proposal duration must be positive")
        total += actual
    return total


def _allocate_seconds_from_pool(
    intervals: list[tuple[dt.datetime, dt.datetime]], seconds: list[int]
) -> list[list[dict[str, Any]]]:
    pool = [[start, end] for start, end in sorted(intervals)]
    output: list[list[dict[str, Any]]] = []
    for requested in seconds:
        remaining = requested
        segments: list[dict[str, Any]] = []
        while remaining > 0 and pool:
            start, end = pool[0]
            available = max(0, int((end - start).total_seconds()))
            if available == 0:
                pool.pop(0)
                continue
            used = min(remaining, available)
            segment_end = start + dt.timedelta(seconds=used)
            segments.append({
                "start": _iso(start),
                "end": _iso(segment_end),
                "duration_minutes": used // 60,
                "duration_seconds": used,
            })
            remaining -= used
            if segment_end >= end:
                pool.pop(0)
            else:
                pool[0][0] = segment_end
        if remaining:
            raise PortfolioReviewError("portfolio allocation exceeded its source pool")
        output.append(segments)
    return output


def _exclusion_reasons(
    source_activities: Iterable[Mapping[str, Any]],
    exceptions: Iterable[Mapping[str, Any]],
    omissions: Iterable[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Summarize evidence-backed Flash dispositions for an excluded group.

    Source proposals are allocated only at activity-group level, so reason
    counts cite immutable evidence rather than guessed minute shares.
    """
    source_evidence_ids = {
        str(evidence_id)
        for activity in source_activities
        for evidence_id in activity.get("evidence_ids", [])
        if str(evidence_id)
    }
    accounted_ids: set[str] = set()
    counts: dict[tuple[str, str], int] = {}
    for disposition, entries in (("exception", exceptions), ("omission", omissions)):
        for entry in entries:
            if str(entry.get("kind") or "") == "analyzer_review_failure":
                raise PortfolioReviewError(
                    "analyzer review failure cannot be counted as an exclusion"
                )
            evidence_ids = entry.get("evidence_ids")
            cited_ids = {
                str(evidence_id)
                for evidence_id in evidence_ids
                if str(evidence_id)
            } if isinstance(evidence_ids, list) else set()
            cited_ids.intersection_update(source_evidence_ids)
            if not cited_ids:
                continue
            reason = str(entry.get("reason") or "").strip()
            if not reason:
                raise PortfolioReviewError(
                    "excluded source minutes lack a nonempty exception/omission reason"
                )
            accounted_ids.update(cited_ids)
            key = (disposition, reason)
            counts[key] = counts.get(key, 0) + len(cited_ids)
    missing_ids = sorted(source_evidence_ids - accounted_ids)
    if missing_ids:
        raise PortfolioReviewError(
            "excluded source minutes lack an exception/omission reason for "
            f"{len(missing_ids)} evidence item(s)"
        )
    return [
        {"disposition": disposition, "reason": reason, "evidence_count": count}
        for (disposition, reason), count in sorted(counts.items())
    ]


def _group_accounting(
    source_activities: list[dict[str, Any]],
    source_proposals: list[dict[str, Any]],
    reviewed_rows: list[dict[str, Any]],
    exceptions: list[dict[str, Any]],
    omissions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Conserve a group's source pool without inventing excluded allocations."""
    exact = any("duration_seconds" in row for row in [*source_proposals, *reviewed_rows])
    if exact:
        if any("duration_seconds" not in row for row in source_proposals):
            raise PortfolioReviewError("group source proposals mix exact and legacy durations")
        if any("duration_seconds" not in row for row in reviewed_rows):
            raise PortfolioReviewError("group review rows mix exact and legacy durations")
        source_seconds = _seconds(source_proposals)
        review_seconds = sum(int(row["duration_seconds"]) for row in reviewed_rows)
        if review_seconds > source_seconds:
            raise PortfolioReviewError("group review seconds exceed its source seconds")
        excluded_seconds = source_seconds - review_seconds
        source_minutes = source_seconds // 60
        review_minutes = review_seconds // 60
        excluded_minutes = excluded_seconds // 60
    else:
        source_minutes = _minutes(source_proposals)
        review_minutes = sum(int(row.get("duration_minutes") or 0) for row in reviewed_rows)
        if review_minutes > source_minutes:
            raise PortfolioReviewError("group review minutes exceed its source minutes")
        excluded_minutes = source_minutes - review_minutes
    exclusion_reasons = (
        _exclusion_reasons(source_activities, exceptions, omissions)
        if (excluded_seconds if exact else excluded_minutes) else []
    )
    if (excluded_seconds if exact else excluded_minutes) and not exclusion_reasons:
        raise PortfolioReviewError(
            "excluded source duration lacks a nonempty exception/omission reason"
        )
    if not exact and source_minutes != review_minutes + excluded_minutes:
        raise PortfolioReviewError("group source minute accounting does not balance")
    if exact and source_seconds != review_seconds + excluded_seconds:
        raise PortfolioReviewError("group source second accounting does not balance")
    result = {
        "source_minutes": source_minutes,
        "review_minutes": review_minutes,
        "excluded_minutes": excluded_minutes,
        "exclusion_reasons": exclusion_reasons,
    }
    if exact:
        result.update({
            "source_seconds": source_seconds,
            "review_seconds": review_seconds,
            "excluded_seconds": excluded_seconds,
        })
    return result


def _portfolio_accounting(
    source_minutes: int, review_minutes: int, excluded_minutes: int
) -> None:
    if review_minutes > source_minutes:
        raise PortfolioReviewError("portfolio review minutes exceed source minutes")
    if source_minutes != review_minutes + excluded_minutes:
        raise PortfolioReviewError("portfolio source minute accounting does not balance")


def _is_analyzer_review_failure(reviewed: Mapping[str, Any]) -> bool:
    """Return whether a review result is only the retryable analyzer sentinel."""
    activities = reviewed.get("activities")
    exceptions = reviewed.get("exceptions")
    omissions = reviewed.get("omissions")
    return (
        activities == []
        and omissions == []
        and isinstance(exceptions, list)
        and bool(exceptions)
        and all(
            isinstance(exception, Mapping)
            and str(exception.get("kind") or "") == "analyzer_review_failure"
            for exception in exceptions
        )
    )


def _review_with_bisection(
    source_activities: list[dict[str, Any]],
    reviewer: Callable[[list[dict[str, Any]]], dict[str, Any]],
    *,
    events_by_id: Mapping[str, dict[str, Any]],
    single_activity_reviewer: Callable[
        [list[dict[str, Any]]], dict[str, Any]
    ] | None = None,
    single_activity_fallback: Callable[
        [list[dict[str, Any]]], dict[str, Any]
    ] | None = None,
) -> list[tuple[tuple[int, ...], list[dict[str, Any]], dict[str, Any]]]:
    """Retry only analyzer failures on deterministic smaller source partitions."""
    ordered = sorted(source_activities, key=lambda row: _sort_activity(row, events_by_id))

    def review_partition(
        activities: list[dict[str, Any]], path: tuple[int, ...]
    ) -> list[tuple[tuple[int, ...], list[dict[str, Any]], dict[str, Any]]]:
        reviewed = reviewer(activities)
        if not _is_analyzer_review_failure(reviewed):
            return [(path, activities, reviewed)]
        if len(activities) == 1:
            if single_activity_reviewer is not None:
                recovered = single_activity_reviewer(activities)
                if not _is_analyzer_review_failure(recovered):
                    return [(path, activities, recovered)]
            if single_activity_fallback is not None:
                fallback = single_activity_fallback(activities)
                if not _is_analyzer_review_failure(fallback):
                    return [(path, activities, fallback)]
            activity_id = str(activities[0].get("activity_id") or "")
            raise PortfolioReviewError(
                "analyzer review failed for single source activity "
                f"{activity_id or '<missing activity ID>'}"
            )
        midpoint = len(activities) // 2
        return [
            *review_partition(activities[:midpoint], (*path, 1)),
            *review_partition(activities[midpoint:], (*path, 2)),
        ]

    return review_partition(ordered, ())


def _package_review(
    reviewed: dict[str, Any],
    source_activities: list[dict[str, Any]],
    source_proposals: list[dict[str, Any]],
    events_by_id: Mapping[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    intervals = [(_parse(row["start"]), _parse(row["end"])) for row in source_proposals]
    exact = any("duration_seconds" in row for row in source_proposals)
    activities = sorted(
        reviewed["activities"], key=lambda row: _sort_activity(row, events_by_id)
    )
    weights = [
        int((row.get("effort") or {}).get("recommended_minutes") or 1)
        for row in activities
    ]
    if exact:
        allocations = _allocate_seconds_from_pool(
            intervals, _split_minutes(_seconds(source_proposals), weights)
        )
    else:
        allocations = _allocate_from_pool(
            intervals, _split_minutes(_minutes(source_proposals), weights)
        )
    source_by_evidence = {
        str(evidence_id): str(activity.get("activity_id") or "")
        for activity in source_activities
        for evidence_id in activity.get("evidence_ids", [])
    }
    packaged: list[dict[str, Any]] = []
    for activity, segments in zip(activities, allocations, strict=True):
        evidence_ids = sorted(str(value) for value in activity.get("evidence_ids", []))
        project = activity.get("project_recommendation")
        if not isinstance(project, Mapping):
            project = {}
        duration_seconds = (
            sum(int(segment["duration_seconds"]) for segment in segments)
            if exact
            else None
        )
        row = {
            "review_id": semantic_analyzer.stable_digest(
                "pvi-", {"activity_id": activity["activity_id"], "evidence_ids": evidence_ids}
            ),
            "activity_id": activity["activity_id"],
            "source_activity_ids": sorted({source_by_evidence[value] for value in evidence_ids}),
            "evidence_ids": evidence_ids,
            "allocation_segments": segments,
            "start": segments[0]["start"],
            "end": segments[-1]["end"],
            "duration_minutes": (
                duration_seconds // 60
                if duration_seconds is not None
                else sum(int(segment["duration_minutes"]) for segment in segments)
            ),
            "client_project": str(project.get("name") or ""),
            "tag_names": [str(value) for value in project.get("tag_names", [])],
            "confidence": _confidence(activity),
            "description": _description(activity),
            "disposition": "pending",
            "review_prompt_version": activity.get("review_prompt_version"),
            "semantic_reviewer_model": activity.get("semantic_reviewer_model"),
            "semantic_reviewer_revision": activity.get("semantic_reviewer_revision"),
            "validation_status": activity.get("portfolio_validation_status")
            or "flash_validated",
        }
        if duration_seconds is not None:
            row["duration_seconds"] = duration_seconds
        packaged.append(row)
    return packaged, list(reviewed["exceptions"]), list(reviewed["omissions"])


def _write_csv(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    fields = [
        "Review ID", "Segments", "Start", "End", "Duration (min)", "Project",
        "Tags", "Confidence", "Description", "Disposition", "Source Activities",
        "Validation",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "Review ID": row["review_id"],
                "Segments": "; ".join(
                    f"{value['start']} - {value['end']}" for value in row["allocation_segments"]
                ),
                "Start": row["start"],
                "End": row["end"],
                "Duration (min)": row["duration_minutes"],
                "Project": row["client_project"],
                "Tags": ", ".join(row["tag_names"]),
                "Confidence": row["confidence"],
                "Description": row["description"],
                "Disposition": row["disposition"],
                "Source Activities": ", ".join(row["source_activity_ids"]),
                "Validation": row["validation_status"],
            })
    temporary.replace(path)


def run(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = args.run_dir.resolve()
    ledger_document = _read_json(run_dir / "evidence" / "evidence-ledger.json")
    events = ledger_document.get("events", [])
    if not isinstance(events, list):
        raise PortfolioReviewError("evidence ledger does not contain an events list")
    events_by_id = {str(row["evidence_id"]): row for row in events}
    analysis = _read_json(args.analysis_fixture)
    proposals = _read_json(run_dir / "proposals.json")
    routing = _read_json(args.routing)
    taxonomy = _taxonomy(routing)
    endpoint = semantic_analyzer.AnalyzerEndpoint.from_env(
        "CLOCKIFY_ANALYZER_PRIMARY",
        default_model=semantic_analyzer.DEFAULT_PRIMARY_MODEL,
    )
    if endpoint is None:
        raise PortfolioReviewError("CLOCKIFY_ANALYZER_PRIMARY_URL is required")
    semantic_analyzer._require_private_text_approval(events, None)
    cache = semantic_analyzer.AnalyzerResponseCache(args.cache)

    target_proposals = [
        row for row in proposals
        if args.since <= str(row.get("start") or "")[:10] <= args.until
    ]
    proposals_by_activity: dict[str, list[dict[str, Any]]] = {}
    for proposal in target_proposals:
        proposals_by_activity.setdefault(str(proposal.get("activity_id") or ""), []).append(proposal)
    activities_by_id = {
        str(row.get("activity_id") or ""): row for row in analysis.get("activities", [])
    }
    missing = sorted(set(proposals_by_activity) - set(activities_by_id))
    if missing:
        raise PortfolioReviewError(f"proposal activities missing from analysis: {len(missing)}")

    carried: list[dict[str, Any]] = []
    carried_reports: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str, tuple[str, ...]], list[dict[str, Any]]] = {}
    for activity_id, activity_proposals in sorted(proposals_by_activity.items()):
        activity = activities_by_id[activity_id]
        evidence_events = [events_by_id[str(value)] for value in activity.get("evidence_ids", [])]
        if any(str(row.get("source_type")) == "fathom" for row in evidence_events):
            reviewed = {"activities": [activity], "exceptions": [], "omissions": []}
            packaged, _, _ = _package_review(
                reviewed, [activity], activity_proposals, events_by_id
            )
            carried.extend(packaged)
            key = _group_key(activity, activity_proposals)
            carried_reports.append({
                "date": key[0],
                "project": key[1],
                "part": 1,
                "source_activities": 1,
                "reviewed_activities": len(packaged),
                "exceptions": 0,
                "omissions": 0,
                "evidence_count": len(activity.get("evidence_ids", [])),
                "review_ids": sorted(row["review_id"] for row in packaged),
                **_group_accounting([activity], activity_proposals, packaged, [], []),
            })
            continue
        grouped.setdefault(_group_key(activity, activity_proposals), []).append(activity)

    probes: set[str] = set()
    probe_lock = threading.Lock()

    def probe_once(candidate: semantic_analyzer.AnalyzerEndpoint) -> None:
        key = f"{candidate.url}|{candidate.model}|{candidate.revision}"
        with probe_lock:
            if key not in probes:
                semantic_analyzer.probe_endpoint(candidate)
                probes.add(key)

    reviewed_rows = list(carried)
    exceptions: list[dict[str, Any]] = []
    omissions: list[dict[str, Any]] = []
    group_reports: list[dict[str, Any]] = list(carried_reports)
    tasks: list[tuple[tuple[str, str, tuple[str, ...]], int, list[dict[str, Any]]]] = []
    for key in sorted(grouped):
        for part, source_activities in enumerate(
            _partition_group(
                grouped[key], events_by_id=events_by_id, taxonomy=taxonomy,
                model=endpoint.model, max_activities=args.max_activities,
            ),
            1,
        ):
            tasks.append((key, part, source_activities))

    def review_source_group(source_activities: list[dict[str, Any]]) -> dict[str, Any]:
        evidence_ids = {
            str(value)
            for activity in source_activities
            for value in activity.get("evidence_ids", [])
        }
        group_events = [events_by_id[value] for value in sorted(evidence_ids)]
        spans = {
            value: span for value in evidence_ids
            if (span := semantic_analyzer._safe_time_span(events_by_id[value])) is not None
        }
        reviewed = semantic_analyzer._call_semantic_review(
            endpoint,
            group_events,
            candidate={"activities": source_activities, "exceptions": [], "omissions": []},
            taxonomy=taxonomy,
            tier="portfolio_flash_review",
            transport=semantic_analyzer.http_transport,
            known_evidence_ids=evidence_ids,
            evidence_time_spans=spans,
            cache=cache,
            before_transport=probe_once,
            cancelled=None,
            review_scope="portfolio",
            review_prompt_version=semantic_analyzer.PORTFOLIO_REVIEW_PROMPT_VERSION,
        )
        if _is_analyzer_review_failure(reviewed):
            return reviewed
        return semantic_analyzer._call_semantic_review(
            endpoint,
            group_events,
            candidate=reviewed,
            taxonomy=taxonomy,
            tier="portfolio_flash_validation",
            transport=semantic_analyzer.http_transport,
            known_evidence_ids=evidence_ids,
            evidence_time_spans=spans,
            cache=cache,
            before_transport=probe_once,
            cancelled=None,
            review_scope="portfolio_validation",
            review_prompt_version=semantic_analyzer.PORTFOLIO_VALIDATION_PROMPT_VERSION,
        )

    def recover_single_source_activity(
        source_activities: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Give one failed leaf an independent Flash validation pass."""
        if len(source_activities) != 1:
            raise PortfolioReviewError(
                "single-activity recovery received multiple activities"
            )
        evidence_ids = {
            str(value)
            for value in source_activities[0].get("evidence_ids", [])
        }
        group_events = [events_by_id[value] for value in sorted(evidence_ids)]
        spans = {
            value: span for value in evidence_ids
            if (span := semantic_analyzer._safe_time_span(events_by_id[value])) is not None
        }
        return semantic_analyzer._call_semantic_review(
            endpoint,
            group_events,
            candidate={
                "activities": source_activities,
                "exceptions": [],
                "omissions": [],
            },
            taxonomy=taxonomy,
            tier="portfolio_flash_single_activity_recovery",
            transport=semantic_analyzer.http_transport,
            known_evidence_ids=evidence_ids,
            evidence_time_spans=spans,
            cache=cache,
            before_transport=probe_once,
            cancelled=None,
            review_scope="portfolio_single_activity_recovery",
            review_prompt_version=(
                PORTFOLIO_SINGLE_ACTIVITY_RECOVERY_PROMPT_VERSION
            ),
        )

    def carry_single_source_activity(
        source_activities: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Keep an already reviewed activity after bounded portfolio failures.

        Base analysis has already passed a Flash reviewer. Portfolio review is
        an optional packaging layer, so repeated opaque-ID copy errors must not
        discard an evidence-backed accomplishment or abort the whole interval.
        The marker keeps this explicit in JSON, CSV, and quality reporting.
        """
        if len(source_activities) != 1:
            raise PortfolioReviewError(
                "single-activity fallback received multiple activities"
            )
        carried = copy.deepcopy(source_activities[0])
        carried["portfolio_validation_status"] = (
            "source_semantic_review_carried_after_flash_contract_failure"
        )
        return {"activities": [carried], "exceptions": [], "omissions": []}

    def review_task(
        task: tuple[tuple[str, str, tuple[str, ...]], int, list[dict[str, Any]]]
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        key, part, source_activities = task
        packaged_rows: list[dict[str, Any]] = []
        task_exceptions: list[dict[str, Any]] = []
        task_omissions: list[dict[str, Any]] = []
        task_reports: list[dict[str, Any]] = []
        reviewed_partitions = _review_with_bisection(
            source_activities,
            review_source_group,
            events_by_id=events_by_id,
            single_activity_reviewer=recover_single_source_activity,
            single_activity_fallback=carry_single_source_activity,
        )
        for retry_partition, leaf_activities, reviewed in reviewed_partitions:
            evidence_ids = {
                str(value)
                for activity in leaf_activities
                for value in activity.get("evidence_ids", [])
            }
            source_proposals = [
                proposal
                for activity in leaf_activities
                for proposal in proposals_by_activity[str(activity["activity_id"])]
            ]
            packaged, group_exceptions, group_omissions = _package_review(
                reviewed, leaf_activities, source_proposals, events_by_id
            )
            accounting = _group_accounting(
                leaf_activities,
                source_proposals,
                packaged,
                group_exceptions,
                group_omissions,
            )
            report = {
                "date": key[0],
                "project": key[1],
                "part": part,
                "source_activities": len(leaf_activities),
                "reviewed_activities": len(packaged),
                "exceptions": len(group_exceptions),
                "omissions": len(group_omissions),
                "evidence_count": len(evidence_ids),
                "review_ids": sorted(row["review_id"] for row in packaged),
                "validation_fallbacks": sum(
                    row["validation_status"] != "flash_validated"
                    for row in packaged
                ),
                **accounting,
            }
            if retry_partition:
                report["retry_partition"] = list(retry_partition)
            packaged_rows.extend(packaged)
            task_exceptions.extend(group_exceptions)
            task_omissions.extend(group_omissions)
            task_reports.append(report)
        return packaged_rows, task_exceptions, task_omissions, task_reports

    status_path = args.output_dir / "portfolio-status.json"
    started_at = _now()
    status = {
        "schema_version": 1,
        "status": "running",
        "started_at": started_at,
        "updated_at": started_at,
        "total_groups": len(tasks),
        "completed_groups": 0,
        "workers": args.workers,
        "model": endpoint.model,
        "revision": endpoint.revision,
        "external_writes": False,
    }
    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(status_path, status)
    outcomes_by_index: dict[int, tuple[
        list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]
    ]] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {
            executor.submit(review_task, task): index
            for index, task in enumerate(tasks)
        }
        try:
            for future in as_completed(futures):
                outcomes_by_index[futures[future]] = future.result()
                status.update({
                    "completed_groups": len(outcomes_by_index),
                    "updated_at": _now(),
                })
                _write_json(status_path, status)
        except BaseException:
            status.update({"status": "failed", "updated_at": _now()})
            _write_json(status_path, status)
            for future in futures:
                future.cancel()
            raise
    outcomes = [outcomes_by_index[index] for index in range(len(tasks))]
    for packaged, group_exceptions, group_omissions, reports in outcomes:
        reviewed_rows.extend(packaged)
        exceptions.extend(group_exceptions)
        omissions.extend(group_omissions)
        group_reports.extend(reports)

    reviewed_rows.sort(key=lambda row: (row["start"], row["review_id"]))
    exact = any("duration_seconds" in row for row in [*target_proposals, *reviewed_rows])
    if exact:
        if any("duration_seconds" not in row for row in target_proposals):
            raise PortfolioReviewError("portfolio source proposals mix exact and legacy durations")
        if any("duration_seconds" not in row for row in reviewed_rows):
            raise PortfolioReviewError("portfolio review rows mix exact and legacy durations")
        source_seconds = _seconds(target_proposals)
        review_seconds = sum(int(row["duration_seconds"]) for row in reviewed_rows)
        excluded_seconds = sum(int(report["excluded_seconds"]) for report in group_reports)
        if source_seconds != sum(int(report["source_seconds"]) for report in group_reports):
            raise PortfolioReviewError("portfolio groups do not cover all source seconds")
        if review_seconds != sum(int(report["review_seconds"]) for report in group_reports):
            raise PortfolioReviewError("portfolio groups do not cover all review seconds")
        _portfolio_accounting(source_seconds, review_seconds, excluded_seconds)
        source_minutes = source_seconds // 60
        review_minutes = review_seconds // 60
        excluded_minutes = excluded_seconds // 60
    else:
        source_minutes = _minutes(target_proposals)
        review_minutes = sum(int(row["duration_minutes"]) for row in reviewed_rows)
        excluded_minutes = sum(int(report["excluded_minutes"]) for report in group_reports)
        if source_minutes != sum(int(report["source_minutes"]) for report in group_reports):
            raise PortfolioReviewError("portfolio groups do not cover all source minutes")
        if review_minutes != sum(int(report["review_minutes"]) for report in group_reports):
            raise PortfolioReviewError("portfolio groups do not cover all review minutes")
        _portfolio_accounting(source_minutes, review_minutes, excluded_minutes)
    result = {
        "schema_version": 1,
        "review_prompt_version": semantic_analyzer.PORTFOLIO_REVIEW_PROMPT_VERSION,
        "model": endpoint.model,
        "revision": endpoint.revision,
        "source_run": str(run_dir),
        "source_analysis": str(args.analysis_fixture.resolve()),
        "range": {"since": args.since, "until": args.until},
        "external_writes": False,
        "source_proposal_segments": len(target_proposals),
        "source_activity_count": len(proposals_by_activity),
        "review_activity_count": len(reviewed_rows),
        "source_minutes": source_minutes,
        "review_minutes": review_minutes,
        "excluded_minutes": excluded_minutes,
        "activities": reviewed_rows,
        "exceptions": exceptions,
        "omissions": omissions,
        "groups": group_reports,
        "cache": cache.summary(),
    }
    if exact:
        result.update({
            "source_seconds": source_seconds,
            "review_seconds": review_seconds,
            "excluded_seconds": excluded_seconds,
        })
    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(args.output_dir / "portfolio-review.json", result)
    _write_csv(args.output_dir / "portfolio-review.csv", reviewed_rows)
    status.update({
        "status": "complete",
        "completed_groups": len(tasks),
        "review_activity_count": len(reviewed_rows),
        "review_minutes": result["review_minutes"],
        "excluded_minutes": result["excluded_minutes"],
        "exceptions": len(exceptions),
        "omissions": len(omissions),
        "updated_at": _now(),
    })
    _write_json(status_path, status)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--analysis-fixture", type=Path, required=True)
    parser.add_argument("--routing", type=Path, default=Path("routing.json"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--since", default="2026-07-01")
    parser.add_argument("--until", default="2026-07-31")
    parser.add_argument("--max-activities", type=int, default=DEFAULT_MAX_ACTIVITIES)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args(argv)
    if args.max_activities <= 0:
        parser.error("--max-activities must be positive")
    if args.workers <= 0:
        parser.error("--workers must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        result = run(parse_args(argv))
    except (OSError, ValueError, semantic_analyzer.AnalyzerError, PortfolioReviewError) as exc:
        print(f"clockify portfolio review: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "activities": result["review_activity_count"],
        "minutes": result["review_minutes"],
        "excluded_minutes": result["excluded_minutes"],
        "exceptions": len(result["exceptions"]),
        "omissions": len(result["omissions"]),
        "output": str((parse_args(argv).output_dir / "portfolio-review.csv").resolve()),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
