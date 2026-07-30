#!/usr/bin/env python3
"""Read-only quality gate for Clockify reconciliation proposals.

The quality gate never updates Google Sheets, Clockify, Multica, or any other
external system. It writes only ``quality_report.json`` in the selected run
directory unless ``--dry-run`` is used.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"
BUCHAREST = dt.timezone(dt.timedelta(hours=3))


def load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")


def find_run(run_id: str, runs_root: Path = RUNS) -> Path:
    if run_id == "latest":
        runs = sorted(p for p in runs_root.iterdir() if p.is_dir())
        if not runs:
            raise FileNotFoundError(f"No runs found in {runs_root}")
        return runs[-1]
    path = runs_root / run_id
    if not path.is_dir():
        raise FileNotFoundError(f"Run {run_id} not found in {runs_root}")
    return path


def get_enriched_context(run_dir: Path) -> dict[str, list[dict[str, Any]]]:
    path = run_dir / "evidence" / "enriched-context.json"
    if not path.exists():
        return {"claude_contexts": [], "hermes_contexts": [], "codex_contexts": []}
    result = load_json(path)
    result.setdefault("claude_contexts", [])
    result.setdefault("hermes_contexts", [])
    result.setdefault("codex_contexts", [])
    return result


def get_proposals(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "proposals.json"
    return load_json(path) if path.exists() else []


def get_routing(root: Path = ROOT) -> dict[str, Any]:
    path = root / "routing.json"
    return load_json(path) if path.exists() else {"session_routes": []}


def parse_timestamp(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BUCHAREST)
    return parsed.astimezone(BUCHAREST)


def _provenance(proposal: dict[str, Any]) -> dict[str, Any]:
    provenance = dict(proposal.get("provenance") or {})
    nested_aliases = {
        "source_machine": "machine",
        "source_session_id": "session_id",
        "source_path": "path",
    }
    for source_key, canonical_key in nested_aliases.items():
        if provenance.get(source_key) and not provenance.get(canonical_key):
            provenance[canonical_key] = provenance[source_key]
    aliases = {
        "source_type": "source_type",
        "source_machine": "machine",
        "source_session_id": "session_id",
        "source_path": "path",
        "burst_start": "burst_start",
        "burst_end": "burst_end",
    }
    for proposal_key, provenance_key in aliases.items():
        if proposal.get(proposal_key) and not provenance.get(provenance_key):
            provenance[provenance_key] = proposal[proposal_key]
    provenance.setdefault("burst_start", proposal.get("start"))
    provenance.setdefault("burst_end", proposal.get("end"))
    return provenance


def _source_matches(context: dict[str, Any], source_type: str) -> bool:
    context_source = str(context.get("source", "")).lower()
    expected = {
        "claude": ("claude",),
        "codex": ("codex",),
        "hermes": ("hermes",),
        "hermes_legacy": ("hermes",),
    }.get(source_type.lower(), (source_type.lower(),))
    return any(token and token in context_source for token in expected)


def _context_overlap_seconds(
    context: dict[str, Any], start: dt.datetime | None, end: dt.datetime | None
) -> float:
    if not start or not end:
        return 0
    context_start = parse_timestamp(context.get("start"))
    context_end = parse_timestamp(context.get("end"))
    if not context_start or not context_end:
        timestamps = [
            parse_timestamp(message.get("_parsed_ts") or message.get("timestamp"))
            for message in context.get("user_messages", [])
        ]
        timestamps = [timestamp for timestamp in timestamps if timestamp]
        if not timestamps:
            return 0
        context_start, context_end = min(timestamps), max(timestamps)
    return max(0.0, (min(end, context_end) - max(start, context_start)).total_seconds())


def find_context_for_proposal(
    proposal: dict[str, Any], enriched: dict[str, list[dict[str, Any]]]
) -> dict[str, Any] | None:
    """Match context only by exact structured identity.

    Label-only fallback is intentionally forbidden: several sessions frequently
    share one repository label, and selecting the first such session caused
    unrelated descriptions to be copied into proposals.
    """

    provenance = _provenance(proposal)
    session_id = str(provenance.get("session_id") or "")
    source_type = str(provenance.get("source_type") or "")
    if not session_id or not source_type:
        return None

    contexts = [
        *enriched.get("claude_contexts", []),
        *enriched.get("hermes_contexts", []),
        *enriched.get("codex_contexts", []),
    ]
    exact = [
        context
        for context in contexts
        if str(context.get("session_id") or "") == session_id
        and _source_matches(context, source_type)
    ]
    if not exact:
        return None
    if len(exact) == 1:
        return exact[0]

    start = parse_timestamp(provenance.get("burst_start"))
    end = parse_timestamp(provenance.get("burst_end"))
    ranked = sorted(
        exact,
        key=lambda context: _context_overlap_seconds(context, start, end),
        reverse=True,
    )
    best_overlap = _context_overlap_seconds(ranked[0], start, end)
    if len(ranked) > 1 and best_overlap == _context_overlap_seconds(ranked[1], start, end):
        return None
    return ranked[0]


def _single_line(value: str, limit: int = 160) -> str:
    value = re.sub(r"\s+", " ", value).strip(" \t\r\n—-")
    if len(value) <= limit:
        return value
    shortened = value[: limit + 1].rsplit(" ", 1)[0]
    return f"{shortened}…"


def _plain_topic(value: str, limit: int = 120) -> str:
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", value)
    value = re.sub(r"```(?:[A-Za-z0-9_+-]+)?", " ", value)
    value = re.sub(r"[`*_#]+", "", value)
    value = re.sub(r"\s*\|\s*", " / ", value)
    return _single_line(value, limit)


def _real_user_message(value: str) -> bool:
    lower = value.strip().lower()
    if not lower:
        return False
    prefixes = (
        "<command-",
        "<local-command-",
        "[tool_",
        "[image:",
        "[note:",
        "[your active task list",
        "/goal",
        "/review",
        "reply exactly ",
    )
    return not lower.startswith(prefixes)


def _messages_in_burst(
    proposal: dict[str, Any], context: dict[str, Any]
) -> list[dict[str, Any]]:
    provenance = _provenance(proposal)
    start = parse_timestamp(provenance.get("burst_start"))
    end = parse_timestamp(provenance.get("burst_end"))
    if not start or not end:
        return list(context.get("user_messages", []))
    # Include the response tail after the last user message without reaching the
    # next >30-minute burst.
    end = end + dt.timedelta(minutes=30)
    result = []
    for message in context.get("user_messages", []):
        timestamp = parse_timestamp(message.get("_parsed_ts") or message.get("timestamp"))
        if timestamp and start <= timestamp <= end:
            result.append(message)
    return result


def infer_work_from_context(
    proposal: dict[str, Any], context: dict[str, Any] | None
) -> str | None:
    if not context:
        return None
    messages = _messages_in_burst(proposal, context)
    real_messages = [
        message
        for message in messages
        if _real_user_message(str(message.get("user_message") or ""))
    ]
    if not real_messages:
        return None

    first = _single_line(str(real_messages[0].get("user_message") or ""), 140)
    # A bounded assistant response attached to the same user message is often a
    # better completion statement, but it remains only a suggestion for review.
    response = _single_line(str(real_messages[-1].get("next_assistant") or ""), 140)
    if response and len(response) > 20 and not response.lower().startswith(("failed ", "error ")):
        return response
    return first or None


def _reviewable_description_topic(description: str) -> str | None:
    """Recover a useful row-local topic without removing the review gate."""
    heading = re.search(
        r"(?i)(?:#{1,6}\s*)?\b(?:goal|task|objective)\s*:\s*"
        r"(.+?)(?=\s+(?:\*\*|__)|[\r\n]|$)",
        description,
    )
    if not heading:
        heading = re.search(
            r"(?i)(?:^|\s)/goal\s+(.+?)(?=[.\r\n]|$)", description
        )
    if heading:
        return _plain_topic(heading.group(1).strip(" :-"), 120)

    topic = description.split(" — ", 1)[-1]
    topic = topic.replace("[NEEDS REVIEW]", "", 1).strip(" :-")
    topic = re.sub(
        r"(?i)^(?:done|completed|fixed)\s*[.!:—-]+\s*", "", topic
    )
    topic = _plain_topic(topic, 120)
    if len(topic) < 20:
        return None
    if topic.lower().startswith(("unlabeled session", "here it is")):
        return None
    return topic


def _route_for_proposal(
    proposal: dict[str, Any], routes: list[dict[str, Any]]
) -> dict[str, Any] | None:
    label = str(proposal.get("source_label") or "").lower()
    project = str(proposal.get("client_project") or "").lower()
    project_suffix = str(proposal.get("clockify_project_suffix") or "")

    # Stable routed identity must win over fuzzy source-label text. Meeting
    # titles can mention Serenichron even when the routed Clockify project is a
    # client project (for example, "Serenichron × Lens of Alex").
    if project_suffix:
        for route in routes:
            if str(route.get("project_suffix") or "") == project_suffix:
                return route

    if project:
        for route in routes:
            if str(route.get("project_name") or "").lower() == project:
                return route

    for route in routes:
        pattern = str(route.get("pattern") or "").lower()
        if pattern and (pattern in label or pattern in project):
            return route
    return None


def check_prefix_match(
    proposal: dict[str, Any], routes: list[dict[str, Any]]
) -> str | None:
    route = _route_for_proposal(proposal, routes)
    if not route:
        return None
    expected = route.get("prefix", "SC")
    description = str(proposal.get("description") or "")
    if description and not description.startswith((expected, "[NEEDS REVIEW]")):
        actual = description.split(" — ", 1)[0]
        return f"Prefix mismatch: expected '{expected}', found '{actual}'"
    return None


def review_proposal(
    proposal: dict[str, Any],
    enriched: dict[str, list[dict[str, Any]]],
    routes: list[dict[str, Any]],
) -> dict[str, Any]:
    issues: list[str] = []
    suggestions: list[str] = []
    improved_description = None
    description = str(proposal.get("description") or "")

    prefix_warning = check_prefix_match(proposal, routes)
    if prefix_warning:
        issues.append(prefix_warning)

    context = None
    if "[NEEDS REVIEW]" in description:
        context = find_context_for_proposal(proposal, enriched)
        inferred = _reviewable_description_topic(description)
        if not inferred:
            inferred = infer_work_from_context(proposal, context)
        if inferred:
            route = _route_for_proposal(proposal, routes) or {}
            improved_description = f"{route.get('prefix', 'SC')} — {_single_line(inferred, 120)}"
            suggestions.append(f"Review row-specific suggestion: {improved_description}")
        elif not _provenance(proposal).get("session_id"):
            issues.append("Missing structured session provenance; context inference disabled")
        else:
            issues.append("No exact row-specific context available to infer work")

    if any(token in description for token in ("/goal", "/review", "<command-")):
        issues.append("Description contains raw command text instead of a work summary")
    lower_description = description.casefold()
    if any(
        token in lower_description
        for token in (
            "you've hit your session limit",
            "you’ve hit your session limit",
            "you're out of usage credits",
            "you’re out of usage credits",
            "reply with exactly ",
            "permissions instructions",
        )
    ):
        issues.append("Description contains runtime or injected system noise")
    if "\n" in description:
        issues.append("Description is not single-line")
    if len(description) > 180:
        issues.append("Description exceeds 180 characters")
    start = parse_timestamp(proposal.get("start"))
    end = parse_timestamp(proposal.get("end"))
    if not start or not end or end <= start:
        issues.append("Proposal has an invalid or empty time window")
    else:
        try:
            duration = int(proposal.get("duration_minutes"))
        except (TypeError, ValueError):
            duration = 0
        wall_minutes = int((end - start).total_seconds() / 60)
        if duration <= 0 or duration > wall_minutes:
            issues.append("Proposal duration is invalid for its time window")

    return {
        "id": proposal.get("id"),
        "candidate_key": proposal.get("candidate_key"),
        "description": description,
        "context_match": str(context.get("session_id")) if context else None,
        "issues": issues,
        "suggestions": suggestions,
        "improved_description": improved_description,
        "has_issues": bool(issues),
        "has_suggestion": improved_description is not None,
    }


def find_duplicate_descriptions(proposals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    descriptions: dict[str, list[str]] = {}
    for proposal in proposals:
        description = _single_line(str(proposal.get("description") or ""), 1000)
        if description:
            descriptions.setdefault(description, []).append(str(proposal.get("id")))
    return [
        {"row_ids": row_ids, "description": description}
        for description, row_ids in descriptions.items()
        if len(row_ids) > 1
    ]


def find_duplicate_candidate_keys(
    proposals: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    keys: dict[str, list[str]] = {}
    for proposal in proposals:
        key = str(proposal.get("candidate_key") or "")
        if key:
            keys.setdefault(key, []).append(str(proposal.get("id")))
    return [
        {"candidate_key": key, "row_ids": row_ids}
        for key, row_ids in sorted(keys.items())
        if len(row_ids) > 1
    ]


def suggest_route_improvements(
    proposals: list[dict[str, Any]], routes: list[dict[str, Any]]
) -> list[str]:
    suggestions = []
    existing_patterns = {str(route.get("pattern") or "").lower() for route in routes}
    label_counts: dict[str, int] = {}
    for proposal in proposals:
        label = str(proposal.get("source_label") or "")
        if label and label.lower() not in existing_patterns:
            label_counts[label] = label_counts.get(label, 0) + 1
    for label, count in sorted(label_counts.items(), key=lambda item: (-item[1], item[0])):
        if count >= 3:
            suggestions.append(
                f"Label '{label}' appears {count} times with no exact route pattern"
            )
    return suggestions


def build_report(
    run_id: str,
    proposals: list[dict[str, Any]],
    enriched: dict[str, list[dict[str, Any]]],
    routes: list[dict[str, Any]],
) -> dict[str, Any]:
    reviews = [review_proposal(proposal, enriched, routes) for proposal in proposals]
    duplicates = find_duplicate_descriptions(proposals)
    duplicate_keys = find_duplicate_candidate_keys(proposals)
    missing_keys = [
        str(proposal.get("id"))
        for proposal in proposals
        if not proposal.get("candidate_key")
    ]
    missing_provenance = [
        str(proposal.get("id"))
        for proposal in proposals
        if not _provenance(proposal).get("session_id")
        and str(_provenance(proposal).get("source_type") or "") != "fathom"
    ]
    needs_review = sum("[NEEDS REVIEW]" in str(p.get("description") or "") for p in proposals)
    issue_count = sum(bool(review["issues"]) for review in reviews)

    if missing_keys or missing_provenance or issue_count or duplicates or duplicate_keys:
        status = "blocked"
    elif needs_review:
        status = "review_required"
    else:
        status = "pass"

    return {
        "schema_version": 2,
        "run_id": run_id,
        "status": status,
        "external_writes": False,
        "summary": {
            "total_proposals": len(proposals),
            "needs_review": needs_review,
            "inferred_suggestions": sum(bool(r["improved_description"]) for r in reviews),
            "rows_with_issues": issue_count,
            "duplicate_description_groups": len(duplicates),
            "duplicate_candidate_key_groups": len(duplicate_keys),
            "missing_candidate_keys": len(missing_keys),
            "missing_structured_provenance": len(missing_provenance),
        },
        "missing_candidate_key_rows": missing_keys,
        "missing_provenance_rows": missing_provenance,
        "duplicate_descriptions": duplicates,
        "duplicate_candidate_keys": duplicate_keys,
        "route_improvement_suggestions": suggest_route_improvements(proposals, routes),
        "reviews": reviews,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", help="Run directory name or 'latest'")
    parser.add_argument("--runs-root", type=Path, default=RUNS)
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the quality report without writing quality_report.json",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit 2 unless the quality status is pass",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run_dir = find_run(args.run_id, args.runs_root)
    except FileNotFoundError as exc:
        print(exc)
        return 1

    proposals = get_proposals(run_dir)
    enriched = get_enriched_context(run_dir)
    routes = get_routing(args.root).get("session_routes", [])
    report = build_report(run_dir.name, proposals, enriched, routes)

    if not args.dry_run:
        report_path = run_dir / "quality_report.json"
        write_json(report_path, report)
        print(f"Quality report written to {report_path}")
    print(json.dumps({"run_id": run_dir.name, **report["summary"], "status": report["status"]}))
    if args.strict and report["status"] != "pass":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
