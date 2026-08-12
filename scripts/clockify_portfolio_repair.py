#!/usr/bin/env python3
"""Repair unsafe Clockify portfolio wording with the pinned Flash reviewer only.

This is a local repair stage. It repairs description wording and source-backed
missing project/tag routes. It may separate validator-proven noise evidence from
a retained row, or reclassify an entire row as noise/exception, while conserving
source/review/excluded minutes and stable review IDs. It never writes to Clockify,
Sheets, Multica, or another external system. The only external call is the
explicitly approved private Flash inference configured for the semantic analyzer.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import csv
import datetime as dt
import json
from pathlib import Path
import re
import sys
import threading
from typing import Any, Callable, Iterable, Mapping

try:
    from scripts import caveman_renderer, semantic_analyzer
except ImportError:  # pragma: no cover - direct script execution fallback
    import caveman_renderer  # type: ignore[no-redef]
    import semantic_analyzer  # type: ignore[no-redef]


REPAIR_SCHEMA_VERSION = 1
APPROVED_FLASH_REVISION = "d3f1c87447216481a8001f48c517a51e13bfb141853a8df5e52f81bf765dabc3"
DEFAULT_WORKERS = 4
MAX_WORKERS = 4
SINGLE_ACTIVITY_REPAIR_PROMPT_VERSION = (
    "clockify-portfolio-repair-single-activity-v1"
)
WORDING_RECOVERY_PROMPT_VERSION = "clockify-portfolio-wording-recovery-v1"
MAX_WORDING_RECOVERY_ATTEMPTS = 2
PORTFOLIO_DESCRIPTION_MAX_WORDS = 24
_PREFIX_RE = re.compile(r"^([A-Za-z][A-Za-z0-9 &-]{0,24}) — (.+)$")
_WORD_RE = re.compile(r"\b[\w'-]+\b")


class PortfolioRepairError(RuntimeError):
    """Raised when a description-only repair cannot be proved safe."""


Transport = Callable[[semantic_analyzer.AnalyzerEndpoint, dict[str, Any]], dict[str, Any]]


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


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _word_count(value: str) -> int:
    return len(_WORD_RE.findall(value))


def repair_reasons(description: Any) -> list[str]:
    """Return deterministic reasons to ask Flash for a wording replacement.

    This intentionally identifies unsafe wording but never repairs it locally.
    """
    value = description if isinstance(description, str) else ""
    reasons: list[str] = []
    words = _word_count(value)
    if not 8 <= words <= PORTFOLIO_DESCRIPTION_MAX_WORDS:
        reasons.append("description outside accepted 8-24 word convention")
    try:
        caveman_renderer.validate_description(
            value,
            max_words=PORTFOLIO_DESCRIPTION_MAX_WORDS,
            allow_compact_technical_slashes=True,
            allow_compact_technical_underscores=True,
        )
    except caveman_renderer.CavemanValidationError as exc:
        reasons.append(f"description violates Caveman safety contract: {exc}")
    return reasons


def _prefix(description: Any, review_id: str) -> tuple[str, str]:
    if not isinstance(description, str):
        raise PortfolioRepairError(f"review {review_id} description is not text")
    matched = _PREFIX_RE.fullmatch(description)
    if matched is None:
        raise PortfolioRepairError(
            f"review {review_id} has no safe Prefix — description structure"
        )
    return matched.group(1), matched.group(2)


def _event_span(event: Mapping[str, Any]) -> dict[str, str] | None:
    return semantic_analyzer._safe_time_span(event)


def _route_missing(row: Mapping[str, Any]) -> bool:
    tags = row.get("tag_names")
    return (
        not str(row.get("client_project") or "").strip()
        or not isinstance(tags, list)
        or not tags
    )


def _carry_fathom_exceptions(
    result: dict[str, Any], reconciliation: list[Mapping[str, Any]] | None
) -> list[str]:
    """Expose proposal-blocking meeting conflicts without inventing time rows."""
    if reconciliation is None:
        return []
    accounted = {
        str(evidence_id)
        for item in [
            *result.get("activities", []),
            *result.get("exceptions", []),
            *result.get("omissions", []),
        ]
        if isinstance(item, Mapping)
        for evidence_id in item.get("evidence_ids", [])
    }
    carried: list[str] = []
    exceptions = result.setdefault("exceptions", [])
    for item in reconciliation:
        if not isinstance(item, Mapping) or item.get("status") != "exception":
            continue
        evidence_id = str(item.get("evidence_id") or "")
        reason = str(item.get("reason") or "").strip()
        if not evidence_id or not reason:
            raise PortfolioRepairError("Fathom reconciliation exception is incomplete")
        if evidence_id in accounted:
            continue
        exceptions.append({
            "kind": "fathom_reconciliation",
            "evidence_ids": [evidence_id],
            "reason": reason,
        })
        accounted.add(evidence_id)
        carried.append(evidence_id)
    return sorted(carried)


def _routing_taxonomy(routing: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the complete, de-duplicated Clockify taxonomy from routing.json."""
    choices: dict[tuple[str, str, tuple[str, ...]], dict[str, Any]] = {}
    for section in ("session_routes", "meeting_routes"):
        for route in routing.get(section, []):
            if not isinstance(route, Mapping) or not route.get("project_name"):
                continue
            tags = tuple(sorted(str(value) for value in route.get("tag_names", [])))
            key = (str(route["project_name"]), str(route.get("prefix") or "SC"), tags)
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
    if not choices:
        raise PortfolioRepairError("routing contains no exact Clockify project/tag taxonomy")
    return [choices[key] for key in sorted(choices)]


def _source_route(
    row: Mapping[str, Any],
    *,
    proposals_by_activity: Mapping[str, list[Mapping[str, Any]]],
    taxonomy: list[dict[str, Any]],
) -> dict[str, Any]:
    """Resolve one source-backed route, refusing mixed or unconfigured routes."""
    review_id = str(row.get("review_id") or "")
    source_ids = [str(value) for value in row.get("source_activity_ids", [])]
    if not source_ids or len(source_ids) != len(set(source_ids)):
        raise PortfolioRepairError(f"review {review_id} has invalid source activity provenance")
    routes: set[tuple[str, tuple[str, ...]]] = set()
    for activity_id in source_ids:
        proposals = proposals_by_activity.get(activity_id)
        if not proposals:
            raise PortfolioRepairError(
                f"review {review_id} source activity {activity_id} lacks an authoritative proposal"
            )
        for proposal in proposals:
            project = str(proposal.get("client_project") or "").strip()
            raw_tags = proposal.get("tag_names")
            tags = tuple(sorted(str(value) for value in raw_tags or []))
            if not project or not isinstance(raw_tags, list) or not tags:
                raise PortfolioRepairError(
                    f"review {review_id} source activity {activity_id} lacks an exact project/tag route"
                )
            routes.add((project, tags))
    if len(routes) != 1:
        raise PortfolioRepairError(f"review {review_id} source proposals have inconsistent routes")
    project, tags = next(iter(routes))
    matches = [
        route for route in taxonomy
        if route["project_name"] == project and tuple(route["tag_names"]) == tags
    ]
    if len(matches) != 1:
        raise PortfolioRepairError(
            f"review {review_id} source route is absent or ambiguous in routing taxonomy"
        )
    return copy.deepcopy(matches[0])


def _candidate_for_row(
    row: Mapping[str, Any], *, route: Mapping[str, Any], body: str
) -> dict[str, Any]:
    """Build a semantic hint, keeping the source wording visible to Flash.

    It is deliberately only a hint: Flash receives the evidence bundle as its
    authority and returns the action/object/outcome.  This module renders that
    returned structured wording with the pre-existing row prefix.
    """
    review_id = str(row.get("review_id") or "")
    evidence_ids = [str(value) for value in row.get("evidence_ids", [])]
    if not review_id or not evidence_ids or len(evidence_ids) != len(set(evidence_ids)):
        raise PortfolioRepairError(
            f"review {review_id or '<missing>'} has invalid evidence provenance"
        )
    duration = int(row.get("duration_minutes") or 0)
    if duration <= 0:
        raise PortfolioRepairError(f"review {review_id} has invalid duration")
    confidence = str(row.get("confidence") or "medium").casefold()
    if confidence not in semantic_analyzer.CONFIDENCE_LEVELS:
        confidence = "medium"
    return {
        "lifecycle": "completed",
        "action": "Repair",
        "object": "portfolio description",
        "outcome": body,
        "workstream": "portfolio wording repair",
        "evidence_ids": evidence_ids,
        "evidence_spans": [],
        "project_recommendation": {
            "name": str(route["project_name"]),
            "prefix": str(route["prefix"]),
            "tag_names": list(route["tag_names"]),
        },
        "effort": {
            "minimum_minutes": duration,
            "recommended_minutes": duration,
            "maximum_minutes": duration,
        },
        "semantic_confidence": confidence,
        "timing_confidence": "medium",
        "split_rationale": "one wording-only portfolio row",
        "merge_rationale": "",
        "omit_rationale": "",
    }


def _locked_taxonomy(row: Mapping[str, Any], *, prefix: str) -> list[dict[str, Any]]:
    project = str(row.get("client_project") or "")
    if not project:
        raise PortfolioRepairError(
            f"review {row.get('review_id')} has no client project for semantic validation"
        )
    return [{
        "project_name": project,
        "prefix": prefix,
        "tag_names": [str(value) for value in row.get("tag_names", [])],
        "billable": True,
    }]


def _review_replacement(
    row: Mapping[str, Any],
    *,
    events_by_id: Mapping[str, dict[str, Any]],
    endpoint: semantic_analyzer.AnalyzerEndpoint,
    cache: semantic_analyzer.AnalyzerResponseCache,
    transport: Transport,
    probe_once: Callable[[semantic_analyzer.AnalyzerEndpoint], None],
    route: Mapping[str, Any],
    full_taxonomy: list[dict[str, Any]] | None,
    repair_route: bool,
) -> dict[str, Any]:
    review_id = str(row.get("review_id") or "")
    _, body = _prefix(row.get("description"), review_id)
    prefix = str(route["prefix"])
    candidate = _candidate_for_row(row, route=route, body=body)
    taxonomy = full_taxonomy if repair_route else _locked_taxonomy(row, prefix=prefix)
    if taxonomy is None:
        raise PortfolioRepairError("route repair requires the complete routing taxonomy")
    evidence_ids = set(candidate["evidence_ids"])
    try:
        events = [events_by_id[value] for value in sorted(evidence_ids)]
    except KeyError as exc:
        raise PortfolioRepairError(
            f"review {review_id} references evidence absent from the immutable ledger"
        ) from exc
    spans = {
        evidence_id: span
        for evidence_id in evidence_ids
        if (span := _event_span(events_by_id[evidence_id])) is not None
    }
    reviewed = semantic_analyzer._call_semantic_review(
        endpoint,
        events,
        candidate={"activities": [candidate], "exceptions": [], "omissions": []},
        taxonomy=taxonomy,
        tier="portfolio_flash_wording_repair",
        transport=transport,
        known_evidence_ids=evidence_ids,
        evidence_time_spans=spans,
        cache=cache,
        before_transport=probe_once,
        cancelled=None,
        review_scope="portfolio",
        review_prompt_version=semantic_analyzer.PORTFOLIO_REVIEW_PROMPT_VERSION,
    )
    validated = semantic_analyzer._call_semantic_review(
        endpoint,
        events,
        candidate=reviewed,
        taxonomy=taxonomy,
        tier="portfolio_flash_wording_validation",
        transport=transport,
        known_evidence_ids=evidence_ids,
        evidence_time_spans=spans,
        cache=cache,
        before_transport=probe_once,
        cancelled=None,
        review_scope="portfolio_validation",
        review_prompt_version=semantic_analyzer.PORTFOLIO_VALIDATION_PROMPT_VERSION,
    )
    activities = validated.get("activities", [])
    if (
        not activities
        and validated.get("exceptions")
        and all(
            str(item.get("kind") or "") == "analyzer_review_failure"
            for item in validated["exceptions"]
        )
        and not validated.get("omissions")
    ):
        validated = semantic_analyzer._call_semantic_review(
            endpoint,
            events,
            candidate={"activities": [candidate], "exceptions": [], "omissions": []},
            taxonomy=taxonomy,
            tier="portfolio_flash_wording_single_activity_recovery",
            transport=transport,
            known_evidence_ids=evidence_ids,
            evidence_time_spans=spans,
            cache=cache,
            before_transport=probe_once,
            cancelled=None,
            review_scope="portfolio_single_activity_recovery",
            review_prompt_version=SINGLE_ACTIVITY_REPAIR_PROMPT_VERSION,
        )
        activities = validated.get("activities", [])
    if len(activities) != 1:
        if (
            not activities
            and not any(
                str(item.get("kind") or "") == "analyzer_review_failure"
                for item in validated.get("exceptions", [])
            )
            and (validated.get("exceptions") or validated.get("omissions"))
        ):
            return {
                "disposition": "excluded",
                "exceptions": copy.deepcopy(validated.get("exceptions", [])),
                "omissions": copy.deepcopy(validated.get("omissions", [])),
            }
        raise PortfolioRepairError(
            f"review {review_id} Flash validation did not return one replacement activity"
        )
    def inspect_replacement(
        result: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], set[str]]:
        result_activities = result.get("activities", [])
        if len(result_activities) != 1:
            raise PortfolioRepairError(
                f"review {review_id} wording recovery did not preserve one activity"
            )
        result_activity = result_activities[0]
        result_evidence_ids = set(result_activity.get("evidence_ids", []))
        result_nonactivity_ids = {
            str(evidence_id)
            for item in [
                *result.get("exceptions", []),
                *result.get("omissions", []),
            ]
            for evidence_id in item.get("evidence_ids", [])
        }
        if (
            result_evidence_ids.intersection(result_nonactivity_ids)
            or result_evidence_ids | result_nonactivity_ids != evidence_ids
        ):
            raise PortfolioRepairError(
                f"review {review_id} Flash validation changed evidence provenance"
            )
        return result_activity, result_evidence_ids

    def render_replacement(result_activity: Mapping[str, Any]) -> str | None:
        try:
            rendered = caveman_renderer.render(
                {
                    "prefix": prefix,
                    "action": result_activity.get("action"),
                    "object": result_activity.get("object"),
                    "outcome": result_activity.get("outcome"),
                },
                max_words=PORTFOLIO_DESCRIPTION_MAX_WORDS,
                allow_compact_technical_slashes=True,
                allow_compact_technical_underscores=True,
            )
        except caveman_renderer.CavemanValidationError:
            return None
        return (
            rendered
            if 8 <= _word_count(rendered) <= PORTFOLIO_DESCRIPTION_MAX_WORDS
            else None
        )

    replacement, replacement_evidence_ids = inspect_replacement(validated)
    description = render_replacement(replacement)
    wording_attempt = 0
    while description is None and wording_attempt < MAX_WORDING_RECOVERY_ATTEMPTS:
        wording_attempt += 1
        validated = semantic_analyzer._call_semantic_review(
            endpoint,
            events,
            candidate=validated,
            taxonomy=taxonomy,
            tier=f"portfolio_flash_wording_recovery_{wording_attempt}",
            transport=transport,
            known_evidence_ids=evidence_ids,
            evidence_time_spans=spans,
            cache=cache,
            before_transport=probe_once,
            cancelled=None,
            review_scope=(
                "portfolio_wording_recovery"
                if wording_attempt == 1
                else "portfolio_wording_recovery_retry"
            ),
            review_prompt_version=WORDING_RECOVERY_PROMPT_VERSION,
        )
        replacement, replacement_evidence_ids = inspect_replacement(validated)
        description = render_replacement(replacement)
    if description is None:
        raise PortfolioRepairError(
            f"review {review_id} Flash wording recovery remained outside Caveman contract"
        )
    return {
        "disposition": "activity",
        "description": description,
        "client_project": str(route["project_name"]),
        "tag_names": list(route["tag_names"]),
        "evidence_ids": sorted(replacement_evidence_ids),
        "exceptions": copy.deepcopy(validated.get("exceptions", [])),
        "omissions": copy.deepcopy(validated.get("omissions", [])),
    }


def repair_document(
    document: Mapping[str, Any],
    ledger: Mapping[str, Any],
    *,
    endpoint: semantic_analyzer.AnalyzerEndpoint | None,
    cache: semantic_analyzer.AnalyzerResponseCache,
    transport: Transport = semantic_analyzer.http_transport,
    workers: int = DEFAULT_WORKERS,
    routing: Mapping[str, Any] | None = None,
    source_proposals: list[Mapping[str, Any]] | None = None,
    fathom_reconciliation: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a copy whose rows change only in allowed Flash-approved fields."""
    rows = document.get("activities")
    events = ledger.get("events")
    if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
        raise PortfolioRepairError("portfolio review does not contain activity objects")
    if not isinstance(events, list) or not all(isinstance(event, Mapping) for event in events):
        raise PortfolioRepairError("evidence ledger does not contain event objects")
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= MAX_WORKERS:
        raise PortfolioRepairError(f"workers must be between 1 and {MAX_WORKERS}")
    events_by_id = {str(event.get("evidence_id") or ""): dict(event) for event in events}
    if "" in events_by_id or len(events_by_id) != len(events):
        raise PortfolioRepairError("evidence ledger has missing or duplicate evidence IDs")
    repairs: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for row in rows:
        review_id = str(row.get("review_id") or "")
        if not review_id or review_id in seen_ids:
            raise PortfolioRepairError("portfolio review has missing or duplicate review IDs")
        seen_ids.add(review_id)
        reasons = repair_reasons(row.get("description"))
        if _route_missing(row):
            reasons.append("missing client project or tag route")
        if reasons:
            repairs.append({
                "review_id": review_id,
                "reasons": reasons,
                "repair_route": _route_missing(row),
            })

    result = copy.deepcopy(dict(document))
    if not repairs:
        carried_fathom_ids = _carry_fathom_exceptions(
            result, fathom_reconciliation
        )
        result["repair"] = {
            "schema_version": REPAIR_SCHEMA_VERSION,
            "status": "pass",
            "external_writes": False,
            "candidate_rows": [],
            "repaired_review_ids": [],
            "cache": cache.summary(),
            "carried_fathom_exception_ids": carried_fathom_ids,
        }
        return result

    if endpoint is None:
        raise PortfolioRepairError("CLOCKIFY_ANALYZER_PRIMARY_URL is required when repairs are needed")
    if endpoint.model not in semantic_analyzer.APPROVED_PRIMARY_MODELS:
        raise PortfolioRepairError(
            "portfolio repair requires the approved DeepSeek V4 Flash cloud alias"
        )
    if endpoint.revision != APPROVED_FLASH_REVISION:
        raise PortfolioRepairError("portfolio repair requires the approved exact Flash revision")
    route_repairs = [item for item in repairs if item["repair_route"]]
    full_taxonomy: list[dict[str, Any]] | None = None
    proposals_by_activity: dict[str, list[Mapping[str, Any]]] = {}
    if route_repairs:
        if not isinstance(routing, Mapping) or source_proposals is None:
            raise PortfolioRepairError(
                "missing-route repair requires --routing and --source-proposals"
            )
        full_taxonomy = _routing_taxonomy(routing)
        for proposal in source_proposals:
            if not isinstance(proposal, Mapping):
                raise PortfolioRepairError("source proposals must contain objects")
            activity_id = str(proposal.get("activity_id") or "")
            if not activity_id:
                raise PortfolioRepairError("source proposal lacks activity_id")
            proposals_by_activity.setdefault(activity_id, []).append(proposal)
        for item in route_repairs:
            row = next(row for row in rows if str(row.get("review_id")) == item["review_id"])
            item["source_route"] = _source_route(
                row, proposals_by_activity=proposals_by_activity, taxonomy=full_taxonomy
            )
    semantic_analyzer._require_private_text_approval(list(events_by_id.values()), None)
    probes: set[tuple[str, str, str]] = set()
    probe_lock = threading.Lock()

    def probe_once(candidate: semantic_analyzer.AnalyzerEndpoint) -> None:
        key = (candidate.url, candidate.model, candidate.revision)
        with probe_lock:
            if key not in probes:
                semantic_analyzer.probe_endpoint(candidate, transport=transport)
                probes.add(key)

    rows_by_id = {str(row["review_id"]): row for row in rows}

    def repair_one(item: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
        review_id = str(item["review_id"])
        row = rows_by_id[review_id]
        if item["repair_route"]:
            route = item["source_route"]
        else:
            prefix, _ = _prefix(row.get("description"), review_id)
            route = {
                "project_name": str(row.get("client_project") or ""),
                "prefix": prefix,
                "tag_names": [str(value) for value in row.get("tag_names", [])],
            }
        return review_id, _review_replacement(
            row,
            events_by_id=events_by_id,
            endpoint=endpoint,
            cache=cache,
            transport=transport,
            probe_once=probe_once,
            route=route,
            full_taxonomy=full_taxonomy,
            repair_route=bool(item["repair_route"]),
        )

    replacements: dict[str, dict[str, Any]] = {}
    unresolved: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(workers, len(repairs))) as executor:
        futures = {
            executor.submit(repair_one, item): item for item in repairs
        }
        for future in as_completed(futures):
            item = futures[future]
            try:
                review_id, replacement = future.result()
            except (PortfolioRepairError, semantic_analyzer.AnalyzerError) as exc:
                if item["repair_route"]:
                    raise
                unresolved[str(item["review_id"])] = str(exc)
                continue
            replacements[review_id] = replacement
    excluded_rows: dict[str, dict[str, Any]] = {
        review_id: replacement
        for review_id, replacement in replacements.items()
        if replacement.get("disposition") == "excluded"
    }
    kept_rows: list[dict[str, Any]] = []
    for row in result["activities"]:
        review_id = str(row["review_id"])
        if review_id in excluded_rows:
            continue
        if review_id in replacements:
            replacement = replacements[review_id]
            row["description"] = replacement["description"]
            row["evidence_ids"] = replacement["evidence_ids"]
            if any(
                item["review_id"] == review_id and item["repair_route"]
                for item in repairs
            ):
                row["client_project"] = replacement["client_project"]
                row["tag_names"] = replacement["tag_names"]
            if replacement["exceptions"] or replacement["omissions"]:
                matching_groups = [
                    group
                    for group in result.get("groups", [])
                    if review_id in group.get("review_ids", [])
                ]
                if len(matching_groups) != 1:
                    raise PortfolioRepairError(
                        f"review {review_id} evidence trim lacks one authoritative group mapping"
                    )
                group = matching_groups[0]
                for disposition in ("exceptions", "omissions"):
                    items = replacement[disposition]
                    result[disposition].extend(copy.deepcopy(items))
                    group[disposition] = int(group[disposition]) + len(items)
        kept_rows.append(row)
    result["activities"] = kept_rows

    for review_id, replacement in excluded_rows.items():
        source_row = rows_by_id[review_id]
        minutes = int(source_row.get("duration_minutes") or 0)
        if minutes <= 0:
            raise PortfolioRepairError(
                f"review {review_id} exclusion has no positive allocated minutes"
            )
        matching_groups = [
            group
            for group in result.get("groups", [])
            if review_id in group.get("review_ids", [])
        ]
        if len(matching_groups) != 1:
            raise PortfolioRepairError(
                f"review {review_id} exclusion lacks one authoritative group mapping"
            )
        group = matching_groups[0]
        group["review_ids"] = [
            value for value in group["review_ids"] if value != review_id
        ]
        group["review_minutes"] = int(group["review_minutes"]) - minutes
        group["excluded_minutes"] = int(group["excluded_minutes"]) + minutes
        group["reviewed_activities"] = int(group["reviewed_activities"]) - 1
        for disposition in ("exceptions", "omissions"):
            items = replacement[disposition]
            result[disposition].extend(copy.deepcopy(items))
            group[disposition] = int(group[disposition]) + len(items)
            group["exclusion_reasons"].extend({
                "disposition": disposition[:-1],
                "reason": str(item.get("reason") or ""),
                "evidence_count": len(item.get("evidence_ids", [])),
            } for item in items)
        if group["source_minutes"] != group["review_minutes"] + group["excluded_minutes"]:
            raise PortfolioRepairError(
                f"review {review_id} exclusion broke group minute accounting"
            )
        result["review_minutes"] = int(result["review_minutes"]) - minutes
        result["excluded_minutes"] = int(result["excluded_minutes"]) + minutes
    if excluded_rows:
        result["review_activity_count"] = len(result["activities"])
        if result["source_minutes"] != result["review_minutes"] + result["excluded_minutes"]:
            raise PortfolioRepairError("repair broke portfolio minute accounting")
    carried_fathom_ids = _carry_fathom_exceptions(result, fathom_reconciliation)
    result["repair"] = {
        "schema_version": REPAIR_SCHEMA_VERSION,
        "status": "complete_with_warnings" if unresolved else "complete",
        "external_writes": False,
        "model": endpoint.model,
        "revision": endpoint.revision,
        "workers": workers,
        "route_repaired_review_ids": sorted(
            str(item["review_id"]) for item in route_repairs
            if str(item["review_id"]) not in excluded_rows
        ),
        "excluded_review_ids": sorted(excluded_rows),
        "evidence_trimmed_review_ids": sorted(
            review_id
            for review_id, replacement in replacements.items()
            if replacement.get("disposition") == "activity"
            and (replacement["exceptions"] or replacement["omissions"])
        ),
        "candidate_rows": repairs,
        "repaired_review_ids": sorted(
            review_id
            for review_id, replacement in replacements.items()
            if replacement.get("disposition") == "activity"
        ),
        "cache": cache.summary(),
        "carried_fathom_exception_ids": carried_fathom_ids,
        "unresolved_wording": [
            {"review_id": review_id, "reason": unresolved[review_id]}
            for review_id in sorted(unresolved)
        ],
    }
    return result


def _write_csv(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    fields = [
        "Review ID", "Segments", "Start", "End", "Duration (min)", "Project",
        "Tags", "Confidence", "Description", "Disposition", "Source Activities",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({
                "Review ID": row.get("review_id", ""),
                "Segments": "; ".join(
                    f"{value['start']} - {value['end']}"
                    for value in row.get("allocation_segments", [])
                ),
                "Start": row.get("start", ""),
                "End": row.get("end", ""),
                "Duration (min)": row.get("duration_minutes", ""),
                "Project": row.get("client_project", ""),
                "Tags": ", ".join(row.get("tag_names", [])),
                "Confidence": row.get("confidence", ""),
                "Description": row.get("description", ""),
                "Disposition": row.get("disposition", ""),
                "Source Activities": ", ".join(row.get("source_activity_ids", [])),
            })
    temporary.replace(path)


def run(args: argparse.Namespace) -> dict[str, Any]:
    source = args.portfolio_review.resolve()
    output_dir = args.output_dir.resolve()
    output_json = output_dir / "portfolio-repair.json"
    if output_json == source:
        raise PortfolioRepairError("repair output must not overwrite the source review")
    document = _read_json(source)
    ledger = _read_json(args.evidence_ledger.resolve())
    routing = _read_json(args.routing.resolve()) if args.routing is not None else None
    source_proposals = (
        _read_json(args.source_proposals.resolve())
        if args.source_proposals is not None
        else None
    )
    fathom_reconciliation = (
        _read_json(args.fathom_reconciliation.resolve())
        if args.fathom_reconciliation is not None
        else None
    )
    if fathom_reconciliation is not None and (
        not isinstance(fathom_reconciliation, list)
        or not all(isinstance(item, Mapping) for item in fathom_reconciliation)
    ):
        raise PortfolioRepairError("Fathom reconciliation must contain a list of objects")
    cache = semantic_analyzer.AnalyzerResponseCache(args.cache.resolve())
    rows = document.get("activities")
    needs_repair = isinstance(rows, list) and any(
        isinstance(row, Mapping)
        and (repair_reasons(row.get("description")) or _route_missing(row))
        for row in rows
    )
    endpoint = (
        semantic_analyzer.AnalyzerEndpoint.from_env(
            "CLOCKIFY_ANALYZER_PRIMARY",
            default_model=semantic_analyzer.DEFAULT_PRIMARY_MODEL,
        )
        if needs_repair
        else None
    )
    started_at = _now()
    status_path = output_dir / "portfolio-repair-status.json"
    _write_json(status_path, {
        "schema_version": REPAIR_SCHEMA_VERSION,
        "status": "running",
        "started_at": started_at,
        "updated_at": started_at,
        "external_writes": False,
    })
    try:
        result = repair_document(
            document,
            ledger,
            endpoint=endpoint,
            cache=cache,
            workers=args.workers,
            routing=routing,
            source_proposals=source_proposals,
            fathom_reconciliation=fathom_reconciliation,
        )
        _write_json(output_json, result)
        _write_csv(output_dir / "portfolio-repair.csv", result["activities"])
    except BaseException:
        _write_json(status_path, {
            "schema_version": REPAIR_SCHEMA_VERSION,
            "status": "failed",
            "started_at": started_at,
            "updated_at": _now(),
            "external_writes": False,
        })
        raise
    _write_json(status_path, {
        "schema_version": REPAIR_SCHEMA_VERSION,
        "status": result["repair"]["status"],
        "started_at": started_at,
        "updated_at": _now(),
        "external_writes": False,
        "repaired_review_ids": result["repair"]["repaired_review_ids"],
    })
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("portfolio_review", type=Path)
    parser.add_argument("--evidence-ledger", type=Path, required=True)
    parser.add_argument("--routing", type=Path)
    parser.add_argument("--source-proposals", type=Path)
    parser.add_argument("--fathom-reconciliation", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    args = parser.parse_args(argv)
    if not 1 <= args.workers <= MAX_WORKERS:
        parser.error(f"--workers must be between 1 and {MAX_WORKERS}")
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        result = run(args)
    except (OSError, ValueError, json.JSONDecodeError, semantic_analyzer.AnalyzerError, PortfolioRepairError) as exc:
        print(f"clockify portfolio repair: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "repaired": len(result["repair"]["repaired_review_ids"]),
        "status": result["repair"]["status"],
        "output": str((args.output_dir / "portfolio-repair.csv").resolve()),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
