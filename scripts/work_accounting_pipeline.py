#!/usr/bin/env python3
"""Evidence-grounded semantic work accounting for one Clockify run bundle.

This stage replaces legacy burst proposals with semantic activities, fixed
meeting blocks, and strict non-overlapping effort allocations.  It writes only
inside the selected local run directory.
"""
from __future__ import annotations

import argparse
import copy
import dataclasses
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping

try:
    from scripts import caveman_renderer
    from scripts import clockify_sync_collect as collector
    from scripts import evidence_ledger
    from scripts import meeting_reconciliation
    from scripts import review_corrections
    from scripts import semantic_analyzer
    from scripts import work_allocator
except ModuleNotFoundError:
    import caveman_renderer  # type: ignore[no-redef]
    import clockify_sync_collect as collector  # type: ignore[no-redef]
    import evidence_ledger  # type: ignore[no-redef]
    import meeting_reconciliation  # type: ignore[no-redef]
    import review_corrections  # type: ignore[no-redef]
    import semantic_analyzer  # type: ignore[no-redef]
    import work_allocator  # type: ignore[no-redef]


SCHEMA_VERSION = 1
ALLOCATION_MODE = "non_overlapping_v1"
MEETING_RECONCILIATION_MIN_RATIO = 0.8
NOISE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("heartbeat", re.compile(r"^\s*(?:heartbeat|health[- ]?check)(?:\s*[:—-].*)?\s*$", re.I)),
    ("standing_by", re.compile(r"^\s*(?:standing by|still waiting|no change(?: yet)?)(?:[.!])?\s*$", re.I)),
    ("tool_transport", re.compile(r"^\s*\[(?:tool_result|tool_ref|thinking)(?::[^]]+)?]\s*$", re.I)),
    ("session_control", re.compile(r"^\s*/(?:goal|review|compact|status)\b", re.I)),
    ("approval_wait", re.compile(r"^\s*(?:approval request|awaiting board approval|waiting for approval)(?:\s*[:—-].*)?\s*$", re.I)),
    ("polling", re.compile(r"^\s*(?:polling|checking again|still running|download(?:s)? running)(?:\s*[:—-].*)?\s*$", re.I)),
    ("injected_wrapper", re.compile(r"<(?:codex_internal_context|command-message|local-command)", re.I)),
)
AUTONOMOUS_MULTICA_SESSION_RE = re.compile(
    r"\byou are running as (?:a )?(?:local coding )?agent for a multica workspace\b|"
    r"\byour assigned issue id is\b",
    re.I,
)


class WorkAccountingError(RuntimeError):
    """Invalid or incomplete local accounting input."""


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _parse_dt(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=collector.BUCHAREST)
    return parsed


def _iso(value: dt.datetime) -> str:
    return value.isoformat(timespec="minutes")


def _minutes(start: dt.datetime, end: dt.datetime) -> int:
    return max(0, int((end - start).total_seconds() // 60))


def _drop_failed_allocations(
    allocation: work_allocator.AllocationResult,
    failed_activity_ids: set[str],
) -> work_allocator.AllocationResult:
    """Remove rejected activities without moving accepted allocations into freed time."""
    if not failed_activity_ids:
        return allocation
    removed = [
        (row.start, row.end)
        for row in allocation.allocations
        if row.activity_id in failed_activity_ids
    ]
    free = [*allocation.unallocated_capacity.intervals, *removed]
    free.sort(key=lambda value: (value[0], value[1]))
    merged: list[tuple[dt.datetime, dt.datetime]] = []
    for start, end in free:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return work_allocator.AllocationResult(
        evidence=tuple(
            row for row in allocation.evidence
            if row.activity_id not in failed_activity_ids
        ),
        allocations=tuple(
            row for row in allocation.allocations
            if row.activity_id not in failed_activity_ids
        ),
        unallocated_capacity=work_allocator.UnallocatedCapacity(
            sum(_minutes(start, end) for start, end in merged),
            tuple(merged),
        ),
        covered_by_existing=tuple(
            row for row in allocation.covered_by_existing
            if row.activity_id not in failed_activity_ids
        ),
        contested_time=tuple(
            row for row in allocation.contested_time
            if row.activity_id not in failed_activity_ids
        ),
    )


def _span(event: Mapping[str, Any]) -> tuple[dt.datetime | None, dt.datetime | None]:
    raw = event.get("raw_source_span") if isinstance(event.get("raw_source_span"), Mapping) else {}
    start = _parse_dt(
        raw.get("start")
        or raw.get("timestamp")
        or event.get("observed_at")
        or raw.get("session_start")
    )
    end = _parse_dt(raw.get("end") or raw.get("timestamp") or raw.get("session_end"))
    if start and not end:
        end = start + dt.timedelta(minutes=1)
    if start and end and end <= start:
        end = start + dt.timedelta(minutes=1)
    return start, end


def _attributes(event: Mapping[str, Any]) -> Mapping[str, Any]:
    value = event.get("attributes")
    return value if isinstance(value, Mapping) else {}


def classify_noise(event: Mapping[str, Any]) -> str | None:
    """High-precision deterministic noise classification; uncertain text stays."""
    attrs = _attributes(event)
    content = str(attrs.get("content") or "").strip()
    role = str(attrs.get("role") or "").lower()
    kind = str(attrs.get("kind") or "").lower()
    if role == "system":
        return "system_message"
    if (
        role == "tool"
        or kind == "tool"
        or kind == "tool_transport"
        or kind in semantic_analyzer.TOOL_KINDS
    ):
        return "tool_transport"
    for reason, pattern in NOISE_PATTERNS:
        if content and pattern.search(content):
            return reason
    return None


def load_ledger(path: Path) -> tuple[evidence_ledger.EvidenceLedger, list[dict[str, Any]]]:
    document = _read_json(path)
    if document.get("schema_version") != evidence_ledger.SCHEMA_VERSION:
        raise WorkAccountingError("unsupported evidence ledger schema")
    events = tuple(
        evidence_ledger.EvidenceEvent.from_document(value)
        for value in document.get("events", [])
    )
    manifest_document = document.get("manifest") or {}
    manifest = evidence_ledger.LedgerManifest.from_document(manifest_document)
    ledger = evidence_ledger.EvidenceLedger(events, manifest.source_inventory)
    ledger.validate(manifest)
    return ledger, [event.document() for event in ledger.events]


def _analysis_events(events: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    event_list = list(events)
    retained: list[dict[str, Any]] = []
    noise: list[dict[str, str]] = []
    has_message_events = any(
        str(event.get("source_type", "")).endswith("_event")
        for event in event_list
    )
    canonical_session_keys = {
        (
            str(source.get("source_type") or ""),
            str(source.get("machine") or ""),
            str(source.get("session_id") or ""),
        )
        for event in event_list
        for source in (
            event.get("source_ref")
            if isinstance(event.get("source_ref"), Mapping)
            else {},
        )
        if (
            source.get("source_type")
            and source.get("machine")
            and source.get("session_id")
            and str(event.get("source_type") or "")
            == f"{source.get('source_type')}_event"
        )
    }
    autonomous_session_keys = {
        (
            str(source.get("source_type") or ""),
            str(source.get("machine") or ""),
            str(source.get("session_id") or ""),
        )
        for event in event_list
        for source in (
            event.get("source_ref")
            if isinstance(event.get("source_ref"), Mapping)
            else {},
        )
        if (
            str(event.get("source_type") or "")
            in {"codex_sessions", "hermes_db_sessions", "claude_bursts"}
            and source.get("source_type")
            and source.get("machine")
            and source.get("session_id")
            and AUTONOMOUS_MULTICA_SESSION_RE.search(
                str(_attributes(event).get("first_user_message") or "")
            )
        )
    }
    for event in event_list:
        source_type = str(event.get("source_type") or "")
        if source_type == "clockify":
            continue
        if source_type in {"fathom", "calendly"}:
            eligible, exclusion = _meeting_is_eligible(event)
            semantic_status = str(_attributes(event).get("semantic_evidence_status") or "")
            if not eligible or semantic_status == "title_only":
                noise.append({
                    "evidence_id": str(event.get("evidence_id")),
                    "reason": f"recording_preclassified:{exclusion or semantic_status}",
                })
                continue
        source = (
            event.get("source_ref")
            if isinstance(event.get("source_ref"), Mapping)
            else {}
        )
        session_key = (
            str(source.get("source_type") or source_type),
            str(source.get("machine") or ""),
            str(source.get("session_id") or ""),
        )
        if session_key in autonomous_session_keys:
            noise.append({
                "evidence_id": str(event.get("evidence_id")),
                "reason": "autonomous_background_session",
            })
            continue
        if has_message_events and source_type.startswith("enriched_"):
            # Canonical message events are richer; old enriched snippets would
            # duplicate and bias the semantic model.
            continue
        if source_type in {"codex_sessions", "hermes_db_sessions", "claude_bursts"}:
            if session_key in canonical_session_keys:
                noise.append({
                    "evidence_id": str(event.get("evidence_id")),
                    "reason": "duplicate_session_summary",
                })
                continue
        reason = classify_noise(event)
        if reason:
            noise.append({"evidence_id": str(event.get("evidence_id")), "reason": reason})
            continue
        retained.append(event)
    return retained, sorted(noise, key=lambda value: value["evidence_id"])


def _load_corrections(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    try:
        decisions = review_corrections.load_decisions(path)
        return review_corrections.derive_learning_cases(decisions)
    except review_corrections.ReviewDecisionError as exc:
        raise WorkAccountingError(f"review correction log is invalid: {exc}") from exc


def _load_regression_cases(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    try:
        return review_corrections.derive_regression_cases(
            review_corrections.load_decisions(path)
        )
    except review_corrections.ReviewDecisionError as exc:
        raise WorkAccountingError(f"review correction log is invalid: {exc}") from exc


def analyze_ledger(
    events: list[dict[str, Any]],
    *,
    analysis_fixture: Path | None = None,
    corrections: list[dict[str, Any]] | None = None,
    analyzer_cache_path: Path | None = None,
    analyzer_target_body_bytes: int | None = None,
    analyzer_max_events_per_chunk: int | None = None,
    analyzer_workers: int | None = None,
    review_taxonomy: list[dict[str, Any]] | None = None,
    review_routing: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    known = {str(event.get("evidence_id")) for event in events}
    if analysis_fixture:
        fixture = _read_json(analysis_fixture)
        raw_activities = fixture.get("activities", [])
        reviewed = bool(raw_activities) and all(
            isinstance(activity, Mapping)
            and activity.get("semantic_reviewer_model")
            for activity in raw_activities
        )
        result = semantic_analyzer.validate_result(
            fixture,
            known_evidence_ids=known,
            provider_model="fixture",
            analyzer_tier="fixture",
            semantic_validation=not reviewed,
        )
        if reviewed:
            semantic_analyzer._validate_review_taxonomy(
                result,
                review_taxonomy or [],
            )
            provenance = {
                tuple(sorted(str(value) for value in activity.get("evidence_ids", []))): {
                    key: activity.get(key)
                    for key in (
                        "analyzer_model",
                        "analyzer_tier",
                        "analyzer_revision",
                        "semantic_reviewer_model",
                        "semantic_reviewer_revision",
                        "review_prompt_version",
                    )
                }
                for activity in raw_activities
            }
            for activity in result["activities"]:
                activity.update(
                    provenance.get(tuple(activity["evidence_ids"]), {})
                )
        # A completed semantic run is also a valid offline fixture. Preserve
        # its replay identity metadata after revalidating the semantic rows;
        # validate_result intentionally returns only the provider contract.
        for key in (
            "review_prompt_version",
            "evidence_bundle_schema_version",
            "evidence_bundle_manifest",
            "ledger_event_count",
            "ledger_evidence_digest",
            "analysis_chunks",
            "analyzer_cache",
        ):
            if key in fixture:
                result[key] = copy.deepcopy(fixture[key])
        return result
    primary = semantic_analyzer.AnalyzerEndpoint.from_env(
        "CLOCKIFY_ANALYZER_PRIMARY",
        default_model=semantic_analyzer.DEFAULT_PRIMARY_MODEL,
    )
    if primary is None:
        raise WorkAccountingError(
            "semantic analyzer is not configured; CLOCKIFY_ANALYZER_PRIMARY_URL is required"
        )
    fallback = semantic_analyzer.AnalyzerEndpoint.from_env("CLOCKIFY_ANALYZER_FALLBACK")
    cache = (
        semantic_analyzer.AnalyzerResponseCache(analyzer_cache_path)
        if analyzer_cache_path is not None
        else None
    )
    tuning = {
        key: value
        for key, value in {
            "target_body_bytes": analyzer_target_body_bytes,
            "max_events_per_chunk": analyzer_max_events_per_chunk,
            "max_workers": analyzer_workers,
        }.items()
        if value is not None
    }
    routing = dict(review_routing or {"session_routes": [], "meeting_routes": []})
    if review_routing is None:
        for choice in review_taxonomy or []:
            for pattern in choice.get("selection_guidance", []):
                routing["session_routes"].append({
                    "pattern": pattern,
                    "project_name": choice.get("project_name"),
                    "prefix": choice.get("prefix"),
                    "tag_names": choice.get("tag_names", []),
                    "confidence": "medium",
                })
    hinted_events = _with_semantic_route_hints(events, routing)
    return semantic_analyzer.analyze_tiered(
        hinted_events,
        primary=primary,
        fallback=fallback,
        corrections=corrections,
        cache=cache,
        review_taxonomy=review_taxonomy,
        **tuning,
    )


def _semantic_review_taxonomy(routing: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Expose only valid Clockify project/task choices to semantic review."""
    choices: dict[tuple[str, str, tuple[str, ...]], dict[str, Any]] = {}
    for section in ("session_routes", "meeting_routes"):
        for route in routing.get(section, []):
            if not isinstance(route, Mapping) or not route.get("project_name"):
                continue
            tag_names = tuple(sorted(str(value) for value in route.get("tag_names", [])))
            key = (
                str(route.get("project_name") or ""),
                str(route.get("prefix") or "SC"),
                tag_names,
            )
            choice = choices.setdefault(key, {
                "project_name": key[0],
                "prefix": key[1],
                "tag_names": list(tag_names),
                "billable": bool(route.get("billable", True)),
                "selection_guidance": [],
            })
            guidance = choice["selection_guidance"]
            for field in ("pattern", "email_domain", "title_regex"):
                value = str(route.get(field) or "").strip()
                if value and value not in guidance:
                    guidance.append(value)
    # Prefix overrides are billing-identification aliases, not new Clockify
    # projects.  Expose every existing task type for the matched project so the
    # Flash reviewer can select both the correct task and the required prefix.
    base_choices = list(choices.values())
    for override in routing.get("prefix_overrides", []):
        if not isinstance(override, Mapping):
            continue
        project_prefix = str(override.get("project_name_prefix") or "").casefold()
        prefix = str(override.get("prefix") or "").strip()
        patterns = [str(value) for value in override.get("patterns", []) if str(value)]
        if not project_prefix or not prefix or not patterns:
            continue
        for base in base_choices:
            if not str(base["project_name"]).casefold().startswith(project_prefix):
                continue
            key = (
                str(base["project_name"]),
                prefix,
                tuple(base["tag_names"]),
            )
            choices.setdefault(key, {
                **base,
                "prefix": prefix,
                "selection_guidance": patterns,
            })
    for choice in choices.values():
        choice["selection_guidance"].sort()
    return [choices[key] for key in sorted(choices)]


def _semantic_route_hint(
    event: Mapping[str, Any], routing: Mapping[str, Any]
) -> dict[str, Any] | None:
    record = _event_record(event)
    if event.get("source_type") in {"fathom", "calendly"}:
        candidate = collector.route_meeting(record, dict(routing))
    else:
        candidate = _route_session_record(record, routing)
    action = str(candidate.get("action") or "")
    if action == "ambiguous":
        return None
    if action == "skip":
        return {
            "action": "skip",
            "reason": str(candidate.get("reason") or "local route excludes source"),
        }
    project_name = str(candidate.get("project_name") or "")
    if not project_name:
        return None
    candidate = _apply_prefix_override(candidate, [event], routing)
    return {
        "action": "route",
        "project_name": project_name,
        "prefix": str(candidate.get("prefix") or "SC"),
        "tag_names": sorted(str(value) for value in candidate.get("tag_names", [])),
        "confidence": str(candidate.get("confidence") or "low"),
    }


def _with_semantic_route_hints(
    events: list[dict[str, Any]], routing: Mapping[str, Any]
) -> list[dict[str, Any]]:
    hinted: list[dict[str, Any]] = []
    for event in events:
        copied = dict(event)
        if hint := _semantic_route_hint(event, routing):
            copied["semantic_route_hint"] = hint
        hinted.append(copied)
    return hinted


def _route_session_record(
    record: Mapping[str, Any], routing: Mapping[str, Any]
) -> dict[str, Any]:
    context = str(record.get("cwd") or record.get("path") or "")
    normalized_context = re.sub(r"[^a-z0-9]+", "-", context.casefold()).strip("-")
    labels = [
        str(record.get("label") or ""),
        str(record.get("title") or ""),
        normalized_context,
    ]
    return collector.route_session(
        {"label": " ".join(value for value in labels if value), "path": context},
        dict(routing),
    )


def _routes_by_name(routing: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for route in routing.get("session_routes", []):
        if not isinstance(route, dict) or not route.get("project_name"):
            continue
        result.setdefault(str(route["project_name"]).casefold(), route)
    for route in routing.get("meeting_routes", []):
        if not isinstance(route, dict) or not route.get("project_name"):
            continue
        result.setdefault(str(route["project_name"]).casefold(), route)
    return result


def _routes_by_selection(
    routing: Mapping[str, Any],
) -> dict[tuple[str, str, tuple[str, ...]], dict[str, Any]]:
    result: dict[tuple[str, str, tuple[str, ...]], dict[str, Any]] = {}
    for section in ("session_routes", "meeting_routes"):
        for route in routing.get(section, []):
            if not isinstance(route, dict) or not route.get("project_name"):
                continue
            key = (
                str(route["project_name"]).casefold(),
                str(route.get("prefix") or "SC"),
                tuple(sorted(str(value) for value in route.get("tag_names", []))),
            )
            result.setdefault(key, route)
            for override in routing.get("prefix_overrides", []):
                if not isinstance(override, Mapping):
                    continue
                project_prefix = str(
                    override.get("project_name_prefix") or ""
                ).casefold()
                prefix = str(override.get("prefix") or "").strip()
                if project_prefix and prefix and key[0].startswith(project_prefix):
                    result.setdefault(
                        (key[0], prefix, key[2]),
                        {
                            **route,
                            "base_prefix": str(route.get("prefix") or "SC"),
                            "prefix": prefix,
                        },
                    )
    return result


def _apply_prefix_override(
    route: Mapping[str, Any],
    cited_events: list[Mapping[str, Any]],
    routing: Mapping[str, Any],
) -> dict[str, Any]:
    selected = dict(route)
    selected["prefix"] = str(
        selected.pop("base_prefix", None) or selected.get("prefix") or "SC"
    )
    project_name = str(selected.get("project_name") or "").casefold()
    searchable = " ".join(
        json.dumps(_event_record(event), ensure_ascii=False, sort_keys=True)
        for event in cited_events
    ).casefold()
    for override in routing.get("prefix_overrides", []):
        if not isinstance(override, Mapping):
            continue
        project_prefix = str(override.get("project_name_prefix") or "").casefold()
        patterns = [str(value).casefold() for value in override.get("patterns", [])]
        if (
            project_prefix
            and project_name.startswith(project_prefix)
            and any(pattern and pattern in searchable for pattern in patterns)
        ):
            selected["prefix"] = str(override.get("prefix") or selected["prefix"])
            break
    return selected


def _event_record(event: Mapping[str, Any]) -> dict[str, Any]:
    attrs = dict(_attributes(event))
    raw = event.get("raw_source_span") if isinstance(event.get("raw_source_span"), Mapping) else {}
    source = event.get("source_ref") if isinstance(event.get("source_ref"), Mapping) else {}
    record = {
        **attrs,
        "start": raw.get("start") or raw.get("timestamp") or event.get("observed_at"),
        "end": raw.get("end") or raw.get("timestamp"),
        "path": raw.get("path"),
        "cwd": raw.get("cwd"),
        "machine": source.get("machine"),
        "session_id": source.get("session_id"),
    }
    if event.get("source_type") == "calendly":
        record["calendar_invitees"] = list(attrs.get("participants") or [])
    return record


def resolve_route(
    activity: Mapping[str, Any],
    cited_events: list[dict[str, Any]],
    routing: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    deterministic_routes: list[dict[str, Any]] = []
    skipped_routes: list[str] = []
    for event in cited_events:
        record = _event_record(event)
        if event.get("source_type") in {"fathom", "calendly"}:
            candidate = collector.route_meeting(record, dict(routing))
        else:
            candidate = _route_session_record(record, routing)
        if candidate.get("action") == "skip":
            skipped_routes.append(str(candidate.get("reason") or "deterministic route excluded source"))
            continue
        if candidate.get("action") != "ambiguous":
            deterministic_routes.append(candidate)

    route_identities = {
        (
            str(route.get("project_suffix") or ""),
            str(route.get("project_name") or "").casefold(),
            tuple(sorted(str(value) for value in route.get("tag_suffixes", []))),
            bool(route.get("billable", True)),
        )
        for route in deterministic_routes
    }
    if skipped_routes and deterministic_routes:
        return None, "cited evidence mixes excluded and billable sources; semantic split required"
    if skipped_routes and not deterministic_routes:
        return None, sorted(skipped_routes)[0]
    if len(route_identities) > 1:
        names = sorted({str(route.get("project_name") or "unknown") for route in deterministic_routes})
        return None, f"cited evidence spans multiple deterministic routes; semantic split required: {', '.join(names)}"
    deterministic = deterministic_routes[0] if deterministic_routes else None

    recommended = activity.get("project_recommendation") or {}
    recommended_name = str(recommended.get("name") or "").casefold()
    recommended_tags = tuple(
        sorted(str(value) for value in recommended.get("tag_names", []))
    )
    if activity.get("semantic_reviewer_model") and recommended_name:
        recommended_prefix = str(recommended.get("prefix") or "SC")
        reviewed_route = _routes_by_selection(routing).get(
            (recommended_name, recommended_prefix, recommended_tags)
        )
        if reviewed_route is None:
            return None, "Flash review selected an unavailable Clockify project/task type"
        return _apply_prefix_override(reviewed_route, cited_events, routing), None
    named = _routes_by_name(routing).get(recommended_name) if recommended_name else None
    route = deterministic or named
    if route is None:
        return None, "no deterministic Clockify project route"
    if deterministic and recommended_name:
        deterministic_name = str(deterministic.get("project_name") or "").casefold()
        if recommended_name != deterministic_name:
            return None, (
                f"semantic project recommendation conflicts with deterministic route: "
                f"{recommended.get('name')} vs {deterministic.get('project_name')}"
            )
    return _apply_prefix_override(route, cited_events, routing), None


def _existing_blocks(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks = []
    for event in events:
        if event.get("source_type") != "clockify":
            continue
        start, end = _span(event)
        if not start or not end:
            continue
        blocks.append(
            {
                "block_id": str(event["evidence_id"]),
                "start": start,
                "end": end,
                "kind": "existing_clockify",
            }
        )
    return blocks


def _recording_events(
    events: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Bind source evidence to the one canonical meeting it represents."""
    recording_sources = [
        event for event in events if event.get("source_type") in {"fathom", "calendly"}
    ]
    by_source_id = {
        f"{event['source_type']}:{(event.get('source_ref') or {}).get('source_id')}": event
        for event in recording_sources
    }
    reconciliation = meeting_reconciliation.reconcile_meetings(
        [event for event in recording_sources if event.get("source_type") == "fathom"],
        [event for event in recording_sources if event.get("source_type") == "calendly"],
        vlad_identities={"vlad@serenichron.com"},
    )
    result = []
    for meeting in reconciliation.meetings:
        source_events = [by_source_id[source_id] for source_id in meeting.source_ids]
        result.append({
            "meeting": meeting,
            "events": source_events,
            "source_evidence_ids": [str(event["evidence_id"]) for event in source_events],
        })
    exceptions = []
    for exception in reconciliation.exceptions:
        source_evidence_ids = sorted({
            str(by_source_id[source_id]["evidence_id"])
            for source_id in (
                *exception.get("source_ids", []),
                *exception.get("candidate_source_ids", []),
            )
            if source_id in by_source_id
        })
        exceptions.append({
            "reason": str(exception.get("reason") or "canonical reconciliation exception"),
            "source_evidence_ids": source_evidence_ids,
        })
    return result, exceptions


def _meeting_is_eligible(event: Mapping[str, Any]) -> tuple[bool, str | None]:
    attrs = _attributes(event)
    start, end = _span(event)
    if not start or not end:
        return False, "invalid_meeting_window"
    if _minutes(start, end) < 5 and not attrs.get("transcript"):
        return False, "short_recording_without_transcript"
    recorded_by = str(attrs.get("recorded_by_email") or "").strip().casefold()
    organizer = attrs.get("organizer")
    if not recorded_by and isinstance(organizer, Mapping):
        recorded_by = str(organizer.get("email") or "").strip().casefold()
    invitees = attrs.get("calendar_invitees", attrs.get("participants"))
    invitee_emails = {
        str(value.get("email") or "").strip().casefold()
        for value in invitees
        if isinstance(value, Mapping)
    } if isinstance(invitees, list) else set()
    vlad_attended = "vlad@serenichron.com" in invitee_emails
    if recorded_by == "vlad@serenichron.com" or vlad_attended:
        return True, None
    if recorded_by:
        return False, "not_vlads_meeting"
    return False, "unknown_meeting_ownership"


def _overlap_ratio(
    start: dt.datetime,
    end: dt.datetime,
    other_start: dt.datetime,
    other_end: dt.datetime,
) -> float:
    overlap = max(dt.timedelta(), min(end, other_end) - max(start, other_start))
    duration = end - start
    return overlap.total_seconds() / duration.total_seconds() if duration.total_seconds() else 0.0


def _meeting_matches_existing_block(
    start: dt.datetime,
    end: dt.datetime,
    block: Mapping[str, Any],
) -> bool:
    """Return whether an existing block credibly represents this full meeting.

    A one-sided comparison would reconcile a short meeting against a much
    longer Clockify entry that merely contains it.  Require substantial overlap
    from both perspectives before treating the two records as the same work.
    """
    return (
        _overlap_ratio(start, end, block["start"], block["end"])
        >= MEETING_RECONCILIATION_MIN_RATIO
        and _overlap_ratio(block["start"], block["end"], start, end)
        >= MEETING_RECONCILIATION_MIN_RATIO
    )


def _canonical_meeting_span(
    meeting: meeting_reconciliation.CanonicalMeeting,
    representative: Mapping[str, Any],
) -> tuple[dt.datetime, dt.datetime]:
    """Use canonical instants while retaining the source's stable display zone."""
    start, end = _parse_dt(meeting.start), _parse_dt(meeting.end)
    source_start, _source_end = _span(representative)
    assert start and end
    if source_start is not None:
        start, end = start.astimezone(source_start.tzinfo), end.astimezone(source_start.tzinfo)
    return start, end


def _meeting_split_candidate(
    meeting: meeting_reconciliation.CanonicalMeeting,
    activity: Mapping[str, Any],
    route: Mapping[str, Any],
    evidence_ids: list[str],
    source_evidence_ids: list[str],
    index: int,
) -> tuple[meeting_reconciliation.MeetingSplit, dt.datetime, dt.datetime]:
    """Turn one timestamped semantic activity into a split-validation input."""
    spans = [
        span for span in activity.get("evidence_spans", [])
        if isinstance(span, Mapping)
        and (
            str(span.get("evidence_id") or "") in source_evidence_ids
            or (
                not span.get("evidence_id")
                and len(evidence_ids) == 1
                and evidence_ids[0] in source_evidence_ids
            )
        )
    ]
    if len(spans) != 1:
        raise meeting_reconciliation.MeetingReconciliationError(
            "meeting split requires exactly one canonical source timestamped evidence span"
        )
    start, end = _parse_dt(spans[0].get("start")), _parse_dt(spans[0].get("end"))
    if start is None or end is None:
        raise meeting_reconciliation.MeetingReconciliationError(
            "meeting split requires timestamped boundary evidence"
        )
    meeting_start = _parse_dt(meeting.start)
    assert meeting_start is not None
    start_offset = int((start - meeting_start).total_seconds())
    end_offset = int((end - meeting_start).total_seconds())
    evidence_id = str(spans[0].get("evidence_id") or evidence_ids[0])
    if evidence_id not in source_evidence_ids:
        raise meeting_reconciliation.MeetingReconciliationError(
            "meeting split boundary must cite a canonical source evidence ID"
        )
    task_name = ", ".join(sorted(str(value) for value in route.get("tag_names", [])))
    return (
        meeting_reconciliation.MeetingSplit(
            canonical_id=meeting.canonical_id,
            index=index,
            start=start.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            end=end.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            route={"project_name": route.get("project_name"), "task_name": task_name},
            evidence_ids=(f"{evidence_id}:{start_offset}-{end_offset}",),
        ),
        start,
        end,
    )


def _workstream_daily_envelopes(
    activities: Iterable[Mapping[str, Any]],
    events_by_id: Mapping[str, dict[str, Any]],
) -> dict[str, dict[str, tuple[dt.datetime, dt.datetime]]]:
    """Bound flexible placement to observed human spans in one workstream.

    Evidence can justify moving an allocation inside the span of related work,
    but unrelated activity elsewhere that day must never widen its capacity.
    """
    grouped: dict[str, dict[str, list[tuple[dt.datetime, dt.datetime]]]] = {}
    for activity in activities:
        workstream_id = str(activity.get("workstream_id") or "")
        if not workstream_id:
            continue
        for evidence_id in activity.get("evidence_ids", []):
            event = events_by_id.get(str(evidence_id))
            if not event:
                continue
            attrs = _attributes(event)
            source_type = str(event.get("source_type") or "")
            role = str(attrs.get("role") or "").lower()
            if source_type.endswith("_event") and role not in {"user", "human"}:
                continue
            if source_type in {"clockify", "fathom"}:
                continue
            start, end = _span(event)
            if not start or not end:
                continue
            grouped.setdefault(workstream_id, {}).setdefault(
                start.date().isoformat(), []
            ).append((start, end))
    return {
        workstream_id: {
            day: (min(start for start, _ in intervals), max(end for _, end in intervals))
            for day, intervals in days.items()
        }
        for workstream_id, days in grouped.items()
    }


def _authoritative_spans(cited_events: list[dict[str, Any]]) -> list[dict[str, str]]:
    spans = []
    for event in cited_events:
        start, end = _span(event)
        if start and end:
            spans.append(
                {
                    "evidence_id": str(event["evidence_id"]),
                    "start": _iso(start),
                    "end": _iso(end),
                }
            )
    return spans


def _allowed_intervals(
    cited_events: list[dict[str, Any]],
    workstream_daily: Mapping[str, tuple[dt.datetime, dt.datetime]],
) -> list[dict[str, str]]:
    days: set[str] = set()
    for event in cited_events:
        start, _ = _span(event)
        if start:
            days.add(start.date().isoformat())
    return [
        {"start": _iso(workstream_daily[day][0]), "end": _iso(workstream_daily[day][1])}
        for day in sorted(days)
        if day in workstream_daily and workstream_daily[day][1] > workstream_daily[day][0]
    ]


def _proposal(
    activity: Mapping[str, Any],
    route: Mapping[str, Any],
    description: str,
    start: dt.datetime,
    end: dt.datetime,
    evidence_ids: list[str],
    segment: int,
) -> dict[str, Any]:
    activity_id = str(activity["activity_id"])
    review_activity_key = semantic_analyzer.stable_digest(
        "wka-",
        {
            "activity_id": activity_id,
            "workstream_id": str(activity.get("workstream_id") or ""),
            "evidence_ids": sorted(str(value) for value in evidence_ids),
        },
    )
    candidate_key = semantic_analyzer.stable_digest(
        "wks-",
        {
            "review_activity_key": review_activity_key,
            "segment": segment,
            "allocation_mode": ALLOCATION_MODE,
        },
    )
    return {
        "id": f"S{segment:03d}",
        "candidate_key": candidate_key,
        "review_activity_key": review_activity_key,
        "allocation_segment": segment,
        "activity_id": activity_id,
        "workstream_id": activity.get("workstream_id"),
        "start": _iso(start),
        "end": _iso(end),
        "duration_minutes": _minutes(start, end),
        "client_project": route.get("project_name"),
        "clockify_project_suffix": route.get("project_suffix"),
        "tag_suffixes": list(route.get("tag_suffixes", [])),
        "tag_names": list(route.get("tag_names", [])),
        "billable": route.get("billable", True),
        "source": [f"evidence:{value}" for value in evidence_ids],
        "source_label": activity.get("object"),
        "confidence": activity.get("semantic_confidence"),
        "timing_confidence": activity.get("timing_confidence"),
        "description": description,
        "rendered_description": description,
        "rationale": activity.get("split_rationale") or activity.get("merge_rationale"),
        "allocation_mode": ALLOCATION_MODE,
        "effort": activity.get("effort"),
        "provenance": {
            "source_type": "semantic_activity",
            "source_session_id": activity_id,
            "source_machine": "cross-machine",
            "burst_start": _iso(start),
            "burst_end": _iso(end),
            "evidence_ids": evidence_ids,
            "analyzer_model": activity.get("analyzer_model"),
            "semantic_reviewer_model": activity.get("semantic_reviewer_model"),
            "semantic_reviewer_revision": activity.get("semantic_reviewer_revision"),
            "review_prompt_version": activity.get("review_prompt_version"),
            "prompt_version": activity.get("prompt_version"),
            "schema_version": activity.get("schema_version"),
        },
    }


def run_accounting(
    run_dir: Path,
    *,
    root: Path,
    analysis_fixture: Path | None = None,
    corrections_path: Path | None = None,
    analyzer_cache_path: Path | None = None,
    analyzer_target_body_bytes: int | None = None,
    analyzer_max_events_per_chunk: int | None = None,
    analyzer_workers: int | None = None,
) -> dict[str, Any]:
    ledger_path = run_dir / "evidence" / "evidence-ledger.json"
    ledger, all_events = load_ledger(ledger_path)
    completeness = ledger.manifest.document().get("source_completeness", {})
    incomplete = [str(value) for value in completeness.get("incomplete_sources", [])]
    coordinator = os.environ.get(
        "CLOCKIFY_AUTOPILOT_COORDINATOR", "omarchy-precision"
    ).strip()
    required = {"clockify", "fathom", "multica_issues"}
    hard_missing = [
        source
        for source in incomplete
        if source in required
        or source in {f"sessions/{coordinator}", f"repositories/{coordinator}"}
        or not source.startswith(("sessions/", "repositories/"))
    ]
    if hard_missing:
        missing = ", ".join(hard_missing) or "unknown"
        raise WorkAccountingError(
            f"required evidence is incomplete; semantic accounting is blocked: {missing}"
        )
    analysis_events, noise = _analysis_events(all_events)
    routing = _read_json(root / "routing.json")
    corrections = _load_corrections(corrections_path)
    regression_cases = _load_regression_cases(corrections_path)
    _write_json(run_dir / "review-learning-cases.json", corrections)
    _write_json(run_dir / "review-regression-cases.json", regression_cases)
    analysis = analyze_ledger(
        analysis_events,
        analysis_fixture=analysis_fixture,
        corrections=corrections,
        analyzer_cache_path=analyzer_cache_path,
        analyzer_target_body_bytes=analyzer_target_body_bytes,
        analyzer_max_events_per_chunk=analyzer_max_events_per_chunk,
        analyzer_workers=analyzer_workers,
        review_taxonomy=_semantic_review_taxonomy(routing),
        review_routing=routing,
    )
    analysis.setdefault("ledger_event_count", len(analysis_events))
    analysis.setdefault("ledger_evidence_digest", semantic_analyzer.stable_digest(
        "led-", sorted(event["evidence_id"] for event in analysis_events)
    ))
    analysis.setdefault("analysis_chunks", [])
    analysis.setdefault(
        "analyzer_cache",
        {
            "schema_version": semantic_analyzer.ANALYZER_CACHE_SCHEMA_VERSION,
            "status": "disabled",
        },
    )
    analysis["noise_classifications"] = noise
    _write_json(run_dir / "semantic-analysis.json", analysis)

    events_by_id = {str(event["evidence_id"]): event for event in all_events}
    existing = _existing_blocks(all_events)
    try:
        recordings, recording_exceptions = _recording_events(all_events)
    except meeting_reconciliation.MeetingReconciliationError as exc:
        raise WorkAccountingError(f"canonical meeting reconciliation failed: {exc}") from exc
    quarantined_evidence_ids = {
        evidence_id
        for exception in recording_exceptions
        for evidence_id in exception["source_evidence_ids"]
    }
    recordings_by_id = {
        entry["meeting"].canonical_id: entry for entry in recordings
    }
    recording_by_evidence_id = {
        evidence_id: entry
        for entry in recordings
        for evidence_id in entry["source_evidence_ids"]
    }
    eligible_recordings = []
    fathom_manifest: dict[str, dict[str, Any]] = {}
    for entry in recordings:
        meeting = entry["meeting"]
        source_events = entry["events"]
        representative = next(
            (event for event in source_events if event.get("source_type") == "fathom"),
            source_events[0],
        )
        eligible, exclusion = _meeting_is_eligible(representative)
        meeting_id = meeting.canonical_id
        manifest_base = {
            "canonical_id": meeting_id,
            "source_evidence_ids": entry["source_evidence_ids"],
        }
        if any(evidence_id in quarantined_evidence_ids for evidence_id in entry["source_evidence_ids"]):
            fathom_manifest[meeting_id] = {
                **manifest_base,
                "status": "exception",
                "reason": "canonical_reconciliation_exception",
            }
        elif eligible:
            eligible_recordings.append(entry)
            if _attributes(representative).get("semantic_evidence_status") == "title_only":
                fathom_manifest[meeting_id] = {
                    **manifest_base,
                    "status": "exception",
                    "reason": "title_only",
                }
            else:
                fathom_manifest[meeting_id] = {**manifest_base, "status": "unresolved"}
        else:
            fathom_manifest[meeting_id] = {
                **manifest_base, "status": "excluded", "reason": exclusion
            }

    fixed = list(existing)
    meeting_conflicts: list[dict[str, Any]] = []
    for entry in eligible_recordings:
        meeting = entry["meeting"]
        representative = next(
            (event for event in entry["events"] if event.get("source_type") == "fathom"),
            entry["events"][0],
        )
        start, end = _canonical_meeting_span(meeting, representative)
        meeting_id = meeting.canonical_id
        overlapping_blocks = [
            block
            for block in fixed
            if _overlap_ratio(start, end, block["start"], block["end"]) > 0
        ]
        matching_existing = [
            block
            for block in overlapping_blocks
            if block["kind"] == "existing_clockify"
            and _meeting_matches_existing_block(start, end, block)
        ]
        if len(overlapping_blocks) == 1 and len(matching_existing) == 1:
            fathom_manifest[meeting_id].update({
                "status": "reconciled",
                "reason": "existing_clockify_meeting_match",
                "fixed_block_ids": [matching_existing[0]["block_id"]],
            })
            continue
        if overlapping_blocks:
            block_ids = [str(block["block_id"]) for block in overlapping_blocks]
            fathom_manifest[meeting_id].update({
                "status": "exception",
                "reason": "meeting_overlap",
                "fixed_block_ids": block_ids,
            })
            meeting_conflicts.append(
                {
                    "id": meeting_id,
                    "reason": "eligible Fathom meeting overlaps fixed Clockify time without a reciprocal meeting match",
                    "exception_kind": "fixed_block_conflict",
                    "conflict_reason": "meeting_overlap",
                    "evidence_ids": entry["source_evidence_ids"],
                    "fixed_block_ids": block_ids,
                }
            )
            continue
        fixed.append(
            {
                "block_id": meeting_id,
                "start": start,
                "end": end,
                "kind": "fathom_meeting",
            }
        )
        fathom_manifest[meeting_id].update({"fixed_block_ids": [meeting_id]})

    workstream_envelopes = _workstream_daily_envelopes(
        analysis.get("activities", []), events_by_id
    )
    activity_context: dict[str, dict[str, Any]] = {}
    allocation_demands = []
    ambiguous: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    meeting_proposals: list[dict[str, Any]] = []
    meeting_activities: dict[str, list[dict[str, Any]]] = {}
    meeting_attempts: dict[str, list[dict[str, Any]]] = {}

    ambiguous.extend(meeting_conflicts)
    ambiguous.extend({
        "id": exception["source_evidence_ids"][0] if exception["source_evidence_ids"] else "canonical-reconciliation",
        "reason": exception["reason"],
        "exception_kind": "canonical_meeting_reconciliation",
        "evidence_ids": exception["source_evidence_ids"],
    } for exception in recording_exceptions)

    for meeting_id, status in fathom_manifest.items():
        if status.get("status") == "exception" and status.get("reason") == "title_only":
            ambiguous.append({
                "id": meeting_id,
                "reason": "Fathom meeting lacks transcript, summary, or action items",
                "exception_kind": "insufficient_meeting_evidence",
                "evidence_ids": status["source_evidence_ids"],
            })

    for exception in analysis.get("exceptions", []):
        ambiguous.append({
            "id": semantic_analyzer.stable_digest("semx-", exception),
            "reason": exception.get("reason") or "semantic analyzer exception",
            "exception_kind": exception.get("kind") or "semantic_exception",
            "evidence_ids": list(exception.get("evidence_ids", [])),
        })
    for omission in analysis.get("omissions", []):
        skipped.append({
            "id": semantic_analyzer.stable_digest("omit-", omission),
            "reason": omission.get("reason") or "semantic analyzer omission",
            "lifecycle": omission.get("lifecycle"),
            "evidence_ids": list(omission.get("evidence_ids", [])),
        })

    for activity in analysis.get("activities", []):
        activity_id = str(activity.get("activity_id") or "")
        evidence_ids = [str(value) for value in activity.get("evidence_ids", [])]
        cited = [events_by_id[value] for value in evidence_ids if value in events_by_id]
        if len(cited) != len(evidence_ids):
            ambiguous.append({"id": activity_id, "reason": "activity cites missing evidence", "exception_kind": "invalid_evidence"})
            continue
        lifecycle = str(activity.get("lifecycle") or "")
        if lifecycle in {"planned", "noise"}:
            skipped.append({"id": activity_id, "reason": f"semantic lifecycle: {lifecycle}", "evidence_ids": evidence_ids})
            continue
        meeting_events = [
            event for event in cited if event.get("source_type") in {"fathom", "calendly"}
        ]
        canonical_ids = {
            entry["meeting"].canonical_id
            for event in meeting_events
            if (entry := recording_by_evidence_id.get(str(event["evidence_id"])))
        }
        meeting_id = next(iter(canonical_ids)) if len(canonical_ids) == 1 else None
        attempt = None
        if lifecycle == "meeting" or meeting_events:
            if meeting_id is None:
                ambiguous.append({"id": activity_id, "reason": "meeting activity requires exactly one canonical recording", "exception_kind": "meeting_evidence", "evidence_ids": evidence_ids})
                continue
            attempt = {"activity_id": activity_id, "evidence_ids": evidence_ids, "failures": []}
            meeting_attempts.setdefault(meeting_id, []).append(attempt)
        route, route_error = resolve_route(activity, cited, routing)
        if route_error or route is None:
            if attempt is not None:
                attempt["failures"].append(str(route_error or "meeting route is unavailable"))
            else:
                ambiguous.append({"id": activity_id, "reason": route_error, "exception_kind": "routing", "evidence_ids": evidence_ids})
            continue
        if activity.get("semantic_reviewer_model"):
            # The independent Flash reviewer owns semantic clarity and useful
            # wording. Python only assembles its reviewed fields with the
            # authoritative route prefix; it does not overrule the review with
            # grammar heuristics.
            description = (
                f"{str(route.get('prefix') or 'SC')} — "
                f"{str(activity.get('action') or '')} "
                f"{str(activity.get('object') or '')} "
                f"{str(activity.get('outcome') or '')}"
            ).strip()
        else:
            rendered = caveman_renderer.try_render(
                {
                    "prefix": str(route.get("prefix") or "SC"),
                    "action": activity.get("action"),
                    "object": activity.get("object"),
                    "outcome": activity.get("outcome"),
                }
            )
            if not rendered.ok:
                if attempt is not None:
                    attempt["failures"].append(str(rendered.error))
                else:
                    ambiguous.append({
                        "id": activity_id,
                        "reason": str(rendered.error),
                        "exception_kind": "description_contract",
                        "evidence_ids": evidence_ids,
                    })
                continue
            description = str(rendered.description)
        activity["rendered_description"] = description
        if attempt is not None:
            assert meeting_id is not None
            entry = recordings_by_id[meeting_id]
            meeting = entry["meeting"]
            representative = next(
                (event for event in entry["events"] if event.get("source_type") == "fathom"),
                entry["events"][0],
            )
            attrs = _attributes(representative)
            if attrs.get("semantic_evidence_status") == "title_only":
                ambiguous.append({"id": activity_id, "reason": "title-only Fathom evidence cannot support a meeting outcome", "exception_kind": "insufficient_meeting_evidence", "evidence_ids": evidence_ids})
                fathom_manifest[meeting_id].update({"status": "exception", "reason": "title_only"})
                attempt["failures"].append("title-only meeting evidence")
                continue
            start, end = _canonical_meeting_span(meeting, representative)
            meeting_status = fathom_manifest.get(meeting_id, {}).get("status")
            if meeting_status == "reconciled":
                skipped.append({"id": activity_id, "reason": "meeting already reconciled by existing Clockify entry", "evidence_ids": evidence_ids})
                attempt["failures"].append("meeting already reconciled")
                continue
            if meeting_status == "exception":
                skipped.append({"id": activity_id, "reason": "meeting has a fixed-block conflict", "evidence_ids": evidence_ids})
                attempt["failures"].append("meeting has a fixed-block conflict")
                continue
            meeting_activities.setdefault(meeting_id, []).append({
                "activity": activity,
                "route": route,
                "description": description,
                "evidence_ids": evidence_ids,
                "entry": entry,
            })
            attempt["candidate"] = True
            continue

        spans = _authoritative_spans(cited)
        intervals = _allowed_intervals(
            cited,
            workstream_envelopes.get(str(activity.get("workstream_id") or ""), {}),
        )
        if not spans or not intervals:
            ambiguous.append({"id": activity_id, "reason": "no observed working-day envelope for cited evidence", "exception_kind": "timing_evidence", "evidence_ids": evidence_ids})
            continue
        demand = {
            **activity,
            "evidence_spans": spans,
            "allowed_intervals": intervals,
            "confidence": activity.get("semantic_confidence"),
            "attention_signal": sum(2 if str(_attributes(event).get("role")).lower() == "user" else 1 for event in cited),
        }
        allocation_demands.append(demand)
        activity_context[activity_id] = {"activity": activity, "route": route, "description": description, "evidence_ids": evidence_ids}

    for meeting_id, attempts in meeting_attempts.items():
        failures = sorted({
            failure for attempt in attempts for failure in attempt["failures"]
        })
        if len(attempts) <= 1 or not failures:
            continue
        if fathom_manifest[meeting_id].get("status") != "unresolved":
            continue
        meeting_activities.pop(meeting_id, None)
        fathom_manifest[meeting_id].update({
            "status": "exception", "reason": "invalid_meeting_split",
        })
        ambiguous.append({
            "id": meeting_id,
            "reason": "; ".join(failures),
            "exception_kind": "invalid_meeting_split",
            "evidence_ids": fathom_manifest[meeting_id]["source_evidence_ids"],
        })

    for meeting_id, candidates in meeting_activities.items():
        entry = recordings_by_id[meeting_id]
        meeting = entry["meeting"]
        representative = next(
            (event for event in entry["events"] if event.get("source_type") == "fathom"),
            entry["events"][0],
        )
        if len(candidates) == 1:
            candidate = candidates[0]
            start, end = _canonical_meeting_span(meeting, representative)
            meeting_proposals.append(_proposal(
                candidate["activity"], candidate["route"], candidate["description"],
                start, end, entry["source_evidence_ids"], 1,
            ))
            fathom_manifest[meeting_id].update({
                "status": "proposed", "activity_id": str(candidate["activity"].get("activity_id") or ""),
            })
            continue
        try:
            split_candidates = [
                (candidate, *_meeting_split_candidate(
                    meeting, candidate["activity"], candidate["route"],
                    candidate["evidence_ids"], entry["source_evidence_ids"], index,
                ))
                for index, candidate in enumerate(candidates)
            ]
            split_candidates.sort(key=lambda value: value[1].start)
            splits = tuple(
                dataclasses.replace(value[1], index=index)
                for index, value in enumerate(split_candidates)
            )
            validated = meeting_reconciliation.validate_meeting_splits(meeting, splits)
        except meeting_reconciliation.MeetingReconciliationError as exc:
            fathom_manifest[meeting_id].update({"status": "exception", "reason": "invalid_meeting_split"})
            ambiguous.append({
                "id": meeting_id,
                "reason": str(exc),
                "exception_kind": "invalid_meeting_split",
                "evidence_ids": entry["source_evidence_ids"],
            })
            continue
        for segment, (candidate, _split, start, end) in zip(validated, split_candidates):
            proposal = _proposal(
                candidate["activity"], candidate["route"], candidate["description"],
                start, end, candidate["evidence_ids"], segment.index + 1,
            )
            proposal["provenance"]["canonical_meeting_id"] = meeting_id
            proposal["provenance"]["timestamped_split_evidence_ids"] = list(segment.evidence_ids)
            meeting_proposals.append(proposal)
        fathom_manifest[meeting_id].update({
            "status": "proposed",
            "activity_ids": [str(candidate["activity"].get("activity_id") or "") for candidate in candidates],
        })

    allocation = work_allocator.allocate_work(allocation_demands, fixed)
    proposals = list(meeting_proposals)
    activity_segment_counts: dict[str, int] = {}
    for segment in allocation.allocations:
        context = activity_context[segment.activity_id]
        activity_segment_counts[segment.activity_id] = activity_segment_counts.get(segment.activity_id, 0) + 1
        proposals.append(
            _proposal(
                context["activity"],
                context["route"],
                context["description"],
                segment.start,
                segment.end,
                context["evidence_ids"],
                activity_segment_counts[segment.activity_id],
            )
        )
    for conflict in allocation.contested_time:
        ambiguous.append({
            "id": conflict.activity_id,
            "activity_id": conflict.activity_id,
            "workstream_id": conflict.workstream_id,
            "reason": conflict.reason,
            "exception_kind": "contested_time",
            "requested_minutes": conflict.requested_minutes,
            "allocated_minutes": conflict.allocated_minutes,
            "unallocated_minutes": conflict.unallocated_minutes,
        })

    for meeting_id, status in fathom_manifest.items():
        if status["status"] == "unresolved":
            status.update({"status": "exception", "reason": "no semantic meeting activity"})
            ambiguous.append({"id": meeting_id, "reason": "eligible Fathom meeting has no semantic activity", "exception_kind": "missing_meeting_activity", "evidence_ids": status["source_evidence_ids"]})

    proposals.sort(key=lambda value: (value["start"], value["candidate_key"]))
    correction_regression = review_corrections.evaluate_regression_cases(
        regression_cases, proposals
    )
    failed_targets: dict[tuple[str, str], dict[str, Any]] = {}
    for regression in correction_regression["results"]:
        if regression["status"] != "fail":
            continue
        target = (regression["activity_id"], regression["evidence_fingerprint"])
        existing_failure = failed_targets.get(target)
        if existing_failure is None:
            failed_targets[target] = {
                **regression,
                "failures": list(regression["failures"]),
                "regression_case_ids": [regression["regression_case_id"]],
            }
            continue
        existing_failure["failures"] = sorted(set(
            [*existing_failure["failures"], *regression["failures"]]
        ))
        existing_failure["regression_case_ids"].append(regression["regression_case_id"])
    if failed_targets:
        proposals_by_target: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for proposal in proposals:
            if target := review_corrections.proposal_target(proposal):
                proposals_by_target.setdefault(target, []).append(proposal)
        proposals = [
            proposal for proposal in proposals
            if review_corrections.proposal_target(proposal) not in failed_targets
        ]
        failed_activity_ids = {target[0] for target in failed_targets}
        allocation = _drop_failed_allocations(allocation, failed_activity_ids)
        for target, failure in sorted(failed_targets.items()):
            related = proposals_by_target.get(target, [])
            evidence_ids = sorted({
                str(evidence_id)
                for proposal in related
                for evidence_id in (proposal.get("provenance") or {}).get("evidence_ids", [])
            })
            for proposal in related:
                for evidence_id in (proposal.get("provenance") or {}).get("evidence_ids", []):
                    recording = recording_by_evidence_id.get(str(evidence_id))
                    meeting_key = (
                        recording["meeting"].canonical_id
                        if recording is not None
                        else str(evidence_id)
                    )
                    meeting_status = fathom_manifest.get(meeting_key)
                    if not meeting_status or meeting_status.get("activity_id") != target[0]:
                        continue
                    if failure["decision"] == "skip":
                        meeting_status.update({
                            "status": "excluded",
                            "reason": "review_correction_skip",
                        })
                    else:
                        meeting_status.update({
                            "status": "exception",
                            "reason": "correction_regression",
                        })
            if failure["decision"] == "skip":
                skipped.append({
                    "id": target[0],
                    "reason": "preserved evidence-bound skip decision",
                    "evidence_ids": evidence_ids,
                    "evidence_fingerprint": target[1],
                    "regression_case_ids": sorted(failure["regression_case_ids"]),
                })
            else:
                ambiguous.append({
                    "id": target[0],
                    "activity_id": target[0],
                    "reason": "; ".join(failure["failures"]),
                    "exception_kind": "correction_regression",
                    "evidence_ids": evidence_ids,
                    "evidence_fingerprint": target[1],
                    "regression_case_ids": sorted(failure["regression_case_ids"]),
                })
    for index, proposal in enumerate(proposals, 1):
        proposal["id"] = f"P{index:03d}"
    ambiguous.sort(key=lambda value: (str(value.get("exception_kind")), str(value.get("id"))))
    for index, value in enumerate(ambiguous, 1):
        value.setdefault("activity_id", value.get("id"))
        value["id"] = f"A{index:03d}"

    def serialize(value: Any) -> Any:
        if dataclasses.is_dataclass(value):
            return {key: serialize(item) for key, item in dataclasses.asdict(value).items()}
        if isinstance(value, dt.datetime):
            return _iso(value)
        if isinstance(value, tuple):
            return [serialize(item) for item in value]
        if isinstance(value, list):
            return [serialize(item) for item in value]
        if isinstance(value, dict):
            return {str(key): serialize(item) for key, item in value.items()}
        return value

    result = {
        "schema_version": SCHEMA_VERSION,
        "allocation_mode": ALLOCATION_MODE,
        "ledger_manifest": ledger.manifest.document(),
        "semantic_analysis": {
            "prompt_version": analysis.get("prompt_version"),
            "activity_count": len(analysis.get("activities", [])),
            "exception_count": len(analysis.get("exceptions", [])),
            "omission_count": len(analysis.get("omissions", [])),
            "noise_count": len(noise),
            "learning_case_count": len(corrections),
        },
        "proposals": proposals,
        "ambiguous": ambiguous,
        "skipped": skipped,
        "allocation": serialize(allocation),
        "fathom_reconciliation": [
            {"evidence_id": value["source_evidence_ids"][0], **value}
            for key, value in sorted(fathom_manifest.items())
        ],
        "correction_regression": correction_regression,
        "external_writes": False,
        "coverage_warnings": [
            {
                "source": source,
                "reason": "peer evidence unavailable; interval retained for later backfill",
            }
            for source in incomplete
            if source not in hard_missing
        ],
    }
    # The initial analyzer artifact is written before deterministic routing and
    # rendering so failures remain inspectable. Rewrite it with the final
    # rendered_description values for activities that became proposals.
    _write_json(run_dir / "semantic-analysis.json", analysis)
    _write_json(run_dir / "allocation-report.json", result["allocation"])
    _write_json(run_dir / "fathom-reconciliation.json", result["fathom_reconciliation"])
    _write_json(run_dir / "review-regression-results.json", correction_regression)
    _write_json(run_dir / "proposals.json", proposals)
    _write_json(run_dir / "ambiguous.json", ambiguous)
    _write_json(run_dir / "skipped.json", skipped)
    # This is the durable completion marker consumed by the service runner.
    # Publish it only after every required artifact has been atomically replaced.
    _write_json(run_dir / "work-accounting-result.json", result)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--analysis-fixture", type=Path)
    parser.add_argument("--corrections", type=Path)
    parser.add_argument("--analyzer-cache", type=Path)
    parser.add_argument("--analyzer-target-body-bytes", type=int)
    parser.add_argument("--analyzer-max-events-per-chunk", type=int)
    parser.add_argument("--analyzer-workers", type=int)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run_accounting(
            args.run_dir.resolve(),
            root=args.root.resolve(),
            analysis_fixture=args.analysis_fixture,
            corrections_path=args.corrections,
            analyzer_cache_path=args.analyzer_cache,
            analyzer_target_body_bytes=args.analyzer_target_body_bytes,
            analyzer_max_events_per_chunk=args.analyzer_max_events_per_chunk,
            analyzer_workers=args.analyzer_workers,
        )
    except (WorkAccountingError, semantic_analyzer.AnalyzerError, work_allocator.AllocationError, ValueError) as exc:
        print(f"work accounting blocked: {exc}", file=sys.stderr)
        return 2
    print((args.run_dir / "work-accounting-result.json").resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
