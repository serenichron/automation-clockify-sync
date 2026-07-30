#!/usr/bin/env python3
"""Clockify proposal quality review — Hermes subagent.

Reads the latest run's proposals.json and enriched context, then:
1. Verifies each description is meaningful (not raw command, not [NEEDS REVIEW] without context)
2. For [NEEDS REVIEW] entries: attempts to infer actual work from session content
3. Flags prefix mismatches (e.g. SC used for a TSTP session)
4. Logs quality report + routing.json improvement suggestions
5. Updates the Google Sheet with improved descriptions

Usage:
    python3 clockify_sync_quality.py <run-id>
    python3 clockify_sync_quality.py latest
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")


def find_run(run_id: str) -> Path:
    if run_id == "latest":
        runs = sorted(RUNS.iterdir())
        if not runs:
            print("No runs found.")
            sys.exit(1)
        return runs[-1]
    p = RUNS / run_id
    if not p.exists():
        print(f"Run {run_id} not found in {RUNS}")
        sys.exit(1)
    return p


def get_enriched_context(run_dir: Path) -> dict[str, list[dict]]:
    """Load enriched context from the run's evidence directory."""
    enriched_path = run_dir / "evidence" / "enriched-context.json"
    if not enriched_path.exists():
        return {"claude_contexts": [], "hermes_contexts": []}
    return load_json(enriched_path)


def get_proposals(run_dir: Path) -> list[dict]:
    props_path = run_dir / "proposals.json"
    if not props_path.exists():
        print(f"No proposals.json in {run_dir}")
        return []
    return load_json(props_path)


def get_routing() -> dict:
    routing_path = ROOT / "routing.json"
    if not routing_path.exists():
        return {"session_routes": []}
    return load_json(routing_path)


def find_context_for_proposal(proposal: dict, enriched: dict) -> dict | None:
    """Find the enriched context entry matching a proposal's session."""
    sources = proposal.get("source", [])
    source_label = proposal.get("source_label", "")
    session_id = None

    for s in sources:
        if ":" in s:
            session_id = s.split(":")[-1]

    # Search Claude contexts
    for ctx in enriched.get("claude_contexts", []):
        if ctx.get("session_id") == session_id or ctx.get("label") == source_label:
            return ctx

    # Search Hermes contexts
    for ctx in enriched.get("hermes_contexts", []):
        if ctx.get("session_id") == session_id:
            return ctx

    return None


def infer_work_from_context(ctx: dict | None) -> str | None:
    """Try to determine what work was done from enriched session context."""
    if not ctx:
        return None

    user_msgs = ctx.get("user_messages", [])
    if not user_msgs:
        return None

    # Collect all user messages that aren't system noise
    real_msgs = []
    for um in user_msgs:
        content = (um.get("user_message") or "").strip()
        if not content:
            continue
        lower = content.lower()
        # Skip system/command messages
        if lower.startswith("<command-") or lower.startswith("<local-command-"):
            continue
        if lower.startswith("[tool_") or lower.startswith("[image:"):
            continue
        if lower.startswith("/goal") or lower.startswith("/review"):
            continue
        real_msgs.append(content)

    if not real_msgs:
        return None

    # Use the first real user message as the best indicator of intent
    first = real_msgs[0][:200]

    # Check if the last assistant message describes what was accomplished
    last_assistant = ctx.get("last_message", "")
    if last_assistant and len(last_assistant) > 20:
        # If the assistant response is more descriptive, use it
        if len(last_assistant) > len(first):
            return last_assistant[:200]

    return first


def check_prefix_match(proposal: dict, routes: list[dict]) -> str | None:
    """Check if the proposal's prefix matches its route. Returns a warning or None."""
    source_label = proposal.get("source_label", "").lower()
    project = proposal.get("client_project", "")

    # Find the matching route
    for r in routes:
        pat = r.get("pattern", "").lower()
        if pat and (pat in source_label or pat in project.lower()):
            expected_prefix = r.get("prefix", "SC")
            current_desc = proposal.get("description", "")
            # Check if the description starts with the expected prefix
            if current_desc and not current_desc.startswith(expected_prefix) and not current_desc.startswith("[NEEDS REVIEW]"):
                return f"Prefix mismatch: expected '{expected_prefix}' but description starts with '{current_desc.split(' — ')[0]}'"
            return None

    return None


def review_proposal(proposal: dict, enriched: dict, routes: list[dict]) -> dict:
    """Review a single proposal and return quality assessment."""
    issues = []
    suggestions = []
    improved_description = None

    desc = proposal.get("description", "")
    is_needs_review = "[NEEDS REVIEW]" in desc

    # 1. Check prefix match
    prefix_warning = check_prefix_match(proposal, routes)
    if prefix_warning:
        issues.append(prefix_warning)

    # 2. If [NEEDS REVIEW], try to infer work
    if is_needs_review:
        ctx = find_context_for_proposal(proposal, enriched)
        inferred = infer_work_from_context(ctx)
        if inferred:
            prefix = "SC"
            for r in routes:
                pat = r.get("pattern", "").lower()
                label = proposal.get("source_label", "").lower()
                if pat and pat in label:
                    prefix = r.get("prefix", "SC")
                    break
            improved_description = f"{prefix} — {inferred[:120]}"
            if len(inferred) > 120:
                improved_description = improved_description[:improved_description.rfind(" ")] + "..."
            suggestions.append(f"Inferred description from session context: {improved_description}")
        else:
            issues.append("No enriched context available to infer work")

    # 3. Check for raw command in description
    if desc and ("/goal" in desc or "/review" in desc or "<command-" in desc):
        issues.append("Description contains raw command text instead of work summary")

    # 4. Check for duplicate descriptions (same text as another proposal)
    # (handled at aggregate level)

    return {
        "id": proposal["id"],
        "description": desc,
        "issues": issues,
        "suggestions": suggestions,
        "improved_description": improved_description,
        "has_issues": len(issues) > 0,
        "has_suggestion": improved_description is not None,
    }


def find_duplicate_descriptions(proposals: list[dict]) -> list[str]:
    """Find proposals with identical descriptions."""
    desc_counts: dict[str, list[str]] = {}
    for p in proposals:
        d = p.get("description", "")
        desc_counts.setdefault(d, []).append(p["id"])
    return [f"{ids} share same description: {d[:80]}..." for d, ids in desc_counts.items() if len(ids) > 1]


def suggest_route_improvements(proposals: list[dict], routes: list[dict]) -> list[str]:
    """Suggest routing.json improvements based on proposal patterns."""
    suggestions = []
    existing_patterns = {r.get("pattern", "").lower() for r in routes}

    # Find labels that appear frequently but aren't explicitly routed
    label_counts: dict[str, int] = {}
    for p in proposals:
        label = p.get("source_label", "")
        if label and label.lower() not in existing_patterns:
            label_counts[label] = label_counts.get(label, 0) + 1

    for label, count in sorted(label_counts.items(), key=lambda x: -x[1]):
        if count >= 3:
            suggestions.append(f"Label '{label}' appears {count}x with no explicit route — consider adding to routing.json")

    return suggestions


def main():
    if len(sys.argv) < 2:
        print("Usage: python3 clockify_sync_quality.py <run-id>")
        sys.exit(1)

    run_dir = find_run(sys.argv[1])
    print(f"Reviewing run: {run_dir.name}")

    proposals = get_proposals(run_dir)
    enriched = get_enriched_context(run_dir)
    routing = get_routing()
    routes = routing.get("session_routes", [])

    print(f"Proposals: {len(proposals)}")
    print(f"Enriched Claude contexts: {len(enriched.get('claude_contexts', []))}")
    print(f"Enriched Hermes contexts: {len(enriched.get('hermes_contexts', []))}")
    print()

    # Review each proposal
    reviews = []
    needs_review_count = 0
    inferred_count = 0
    prefix_issues = 0

    for p in proposals:
        review = review_proposal(p, enriched, routes)
        reviews.append(review)
        if "[NEEDS REVIEW]" in p.get("description", ""):
            needs_review_count += 1
        if review["has_suggestion"]:
            inferred_count += 1
        if review["has_issues"]:
            prefix_issues += 1

    # Aggregate checks
    duplicates = find_duplicate_descriptions(proposals)
    route_suggestions = suggest_route_improvements(proposals, routes)

    # Build quality report
    report = {
        "run_id": run_dir.name,
        "total_proposals": len(proposals),
        "needs_review": needs_review_count,
        "inferred_descriptions": inferred_count,
        "prefix_issues": prefix_issues,
        "duplicate_descriptions": duplicates,
        "route_improvement_suggestions": route_suggestions,
        "reviews": reviews,
    }

    report_path = run_dir / "quality_report.json"
    write_json(report_path, report)
    print(f"Quality report written to {report_path}")
    print()

    # Summary
    print("=== QUALITY REPORT SUMMARY ===")
    print(f"  [NEEDS REVIEW]: {needs_review_count}")
    print(f"  Inferred descriptions: {inferred_count}")
    print(f"  Prefix issues: {prefix_issues}")
    print(f"  Duplicate descriptions: {len(duplicates)}")
    print(f"  Route improvement suggestions: {len(route_suggestions)}")
    print()

    if inferred_count > 0:
        print("=== INFERRED DESCRIPTIONS ===")
        for r in reviews:
            if r["improved_description"]:
                print(f"  {r['id']}: {r['description'][:80]}")
                print(f"    → {r['improved_description'][:120]}")
                print()

    if route_suggestions:
        print("=== ROUTE IMPROVEMENT SUGGESTIONS ===")
        for s in route_suggestions:
            print(f"  • {s}")
        print()

    if duplicates:
        print("=== DUPLICATE DESCRIPTIONS ===")
        for d in duplicates:
            print(f"  • {d}")
        print()

    # Update the Google Sheet with improved descriptions
    if inferred_count > 0:
        print("Updating Google Sheet with improved descriptions...")
        try:
            import subprocess
            import urllib.request

            # Read credentials
            creds_path = Path.home() / ".config" / "gws" / "credentials.json"
            if creds_path.exists():
                creds = json.loads(creds_path.read_text())
                token = creds.get("access_token", "")

                # Refresh token if needed
                if token:
                    # Build updated rows
                    rows = []
                    rows.append(["Row ID", "Date", "Start", "End", "Duration (min)", "Duration (h)",
                                 "Project", "Tags", "Source", "Confidence",
                                 "Description", "Rationale", "Status", "Notes"])

                    # Map review improvements
                    improved_map = {r["id"]: r["improved_description"] for r in reviews if r["improved_description"]}

                    for p in proposals:
                        start = p.get("start", "")
                        end = p.get("end", "")
                        desc = improved_map.get(p["id"], p.get("description", ""))
                        rows.append([
                            p["id"],
                            start[:10] if start else "",
                            start[11:16] if len(start) >= 16 else "",
                            end[11:16] if len(end) >= 16 else "",
                            p.get("duration_minutes", 0),
                            round(p.get("duration_minutes", 0) / 60, 1),
                            p.get("client_project", ""),
                            ", ".join(p.get("tag_names", [])),
                            ", ".join(p.get("source", [])),
                            p.get("confidence", ""),
                            desc,
                            p.get("rationale", ""),
                            "",  # Status
                            "",  # Notes
                        ])

                    body = json.dumps({"values": rows, "majorDimension": "ROWS"})
                    sheet_id = "1CwH2kEaKjUaEX8rTUIR0GQfuQJM4WuE_u-AOmaioMZo"
                    url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/Proposals!A1:N{len(rows)}?valueInputOption=USER_ENTERED"
                    req = urllib.request.Request(url, data=body.encode(), method="PUT")
                    req.add_header("Authorization", f"Bearer {token}")
                    req.add_header("Content-Type", "application/json")
                    with urllib.request.urlopen(req) as resp:
                        result = json.loads(resp.read())
                        print(f"  Sheet updated: {result.get('updatedCells', 0)} cells")
                else:
                    print("  No access token available — skipping sheet update")
            else:
                print("  No credentials file found — skipping sheet update")
        except Exception as e:
            print(f"  Sheet update failed: {e}")

    print()
    print("Done.")


if __name__ == "__main__":
    main()
