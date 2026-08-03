#!/usr/bin/env python3
"""Run the local Clockify review pipeline and emit a compact action contract.

This command performs external reads through the collector and writes only
local run artifacts and durable review state. It never mutates Clockify,
Google Sheets, Multica, schedules, or agent configuration.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from typing import Any

try:
    from scripts import review_acceptance
except ModuleNotFoundError:  # direct script execution
    import review_acceptance  # type: ignore[no-redef]


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
RUNS = ROOT / "runs"
DEFAULT_STATE = ROOT / "state" / "review-items.json"
DEFAULT_CORRECTIONS = ROOT / "state" / "review-corrections.jsonl"
DEFAULT_ACCEPTANCE_LEDGER = ROOT / "state" / "review-acceptance.jsonl"
REVIEW_MODES = {"shadow_all", "exceptions_only"}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_result(
    run_dir: Path,
    quality: dict[str, Any],
    snapshot: dict[str, Any] | None,
    *,
    review_mode: str = "shadow_all",
    acceptance_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if review_mode not in REVIEW_MODES:
        raise ValueError(f"unsupported review mode: {review_mode}")
    run_report: dict[str, Any] = {}
    accounting: dict[str, Any] = {}
    try:
        if (run_dir / "run-report.json").is_file():
            value = _read_json(run_dir / "run-report.json")
            run_report = value if isinstance(value, dict) else {}
        if (run_dir / "work-accounting-result.json").is_file():
            value = _read_json(run_dir / "work-accounting-result.json")
            accounting = value if isinstance(value, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        # Quality and durable state remain authoritative for action selection;
        # unreadable optional summary metadata is simply omitted here.
        run_report = {}
        accounting = {}
    categories = (snapshot or {}).get("categories", {})
    category_names = ("new", "changed", "carried_pending")
    categorized = [
        (name, item)
        for name in category_names
        for item in categories.get(name, [])
        if isinstance(item, dict)
    ]
    new_all = list(categories.get("new", []))
    changed_all = list(categories.get("changed", []))
    exception_rows = [
        {"category": name, **item}
        for name, item in categorized
        if str(item.get("disposition") or "") == "ambiguous"
    ]
    clean_rows = [
        {"category": name, **item}
        for name, item in categorized
        if str(item.get("disposition") or "") == "pending"
    ]
    clean_ids = sorted({str(item.get("id") or "") for item in clean_rows if item.get("id")})
    clean_members = sorted(
        (
            {
                "review_item_id": str(item.get("id") or ""),
                "revision": int(item.get("revision") or 0),
                "evidence_fingerprint": str(item.get("evidence_fingerprint") or ""),
            }
            for item in clean_rows
            if item.get("id")
        ),
        key=lambda item: item["review_item_id"],
    )
    exception_delta = [
        item for item in exception_rows if item["category"] in {"new", "changed"}
    ]
    clean_delta_count = sum(
        item["category"] in {"new", "changed"} for item in clean_rows
    )
    clean_batch_id = (
        "rbatch-" + hashlib.sha256(
            json.dumps(clean_members, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24]
        if clean_ids else None
    )
    if review_mode == "exceptions_only":
        new_items = [item for item in new_all if str(item.get("disposition") or "") == "ambiguous"]
        changed_items = [item for item in changed_all if str(item.get("disposition") or "") == "ambiguous"]
    else:
        new_items = new_all
        changed_items = changed_all
    warnings = list((snapshot or {}).get("coverage_warnings", []))
    quality_status = str(quality.get("status") or "blocked")

    if quality_status == "blocked":
        action = "blocked"
    elif warnings:
        action = "coverage_warning"
    elif review_mode == "exceptions_only" and exception_delta:
        action = "review_exceptions"
    elif review_mode == "exceptions_only" and clean_delta_count:
        action = "review_batch"
    elif new_items or changed_items:
        action = "review_delta"
    else:
        action = "no_comment"

    summary = (snapshot or {}).get("summary", {})
    return {
        "schema_version": 1,
        "run_id": run_dir.name,
        "run_dir": str(run_dir.resolve()),
        "action": action,
        "should_comment": action != "no_comment",
        "should_update_issue_description": False,
        "external_writes": False,
        "review_mode": review_mode,
        "acceptance_gate": acceptance_gate or {
            "exceptions_only_eligible": False,
            "status": "not_recorded",
        },
        "quality_status": quality_status,
        "quality_summary": quality.get("summary", {}),
        "date_range": run_report.get("date_range"),
        "source_completeness": (
            (run_report.get("evidence_ledger") or {}).get("source_completeness")
            if isinstance(run_report.get("evidence_ledger"), dict)
            else None
        ),
        "accounting_summary": {
            "proposals": len(accounting.get("proposals", [])),
            "exceptions": len(accounting.get("ambiguous", [])),
            "omissions": len(accounting.get("skipped", [])),
            "contested_time": len(
                [
                    value
                    for value in accounting.get("ambiguous", [])
                    if isinstance(value, dict)
                    and value.get("exception_kind") == "contested_time"
                ]
            ),
            "fathom_records": len(accounting.get("fathom_reconciliation", [])),
        },
        "review_summary": {
            "new": int(summary.get("new", len(new_items))),
            "changed": int(summary.get("changed", len(changed_items))),
            "carried_pending": int(summary.get("carried_pending", 0)),
            "resolved_disappeared": int(summary.get("resolved_disappeared", 0)),
        },
        "new": new_items,
        "changed": changed_items,
        "exceptions": exception_delta if review_mode == "exceptions_only" else [],
        "active_exception_count": len(exception_rows),
        "clean_batch": {
            "batch_id": clean_batch_id,
            "count": len(clean_ids),
            "review_item_ids": clean_ids,
            "members": clean_members,
            "new": sum(item["category"] == "new" for item in clean_rows),
            "changed": sum(item["category"] == "changed" for item in clean_rows),
            "carried_pending": sum(item["category"] == "carried_pending" for item in clean_rows),
        },
        "coverage_warnings": warnings,
        "paths": {
            "run_report": str((run_dir / "run-report.md").resolve()),
            "quality_report": str((run_dir / "quality_report.json").resolve()),
            "evidence_ledger": str((run_dir / "evidence" / "evidence-ledger.json").resolve()),
            "semantic_analysis": str((run_dir / "semantic-analysis.json").resolve()),
            "work_accounting_result": str((run_dir / "work-accounting-result.json").resolve()),
            "review_snapshot": (
                str((run_dir / "review-snapshot.json").resolve())
                if snapshot is not None
                else None
            ),
        },
    }


def write_summary(path: Path, result: dict[str, Any]) -> None:
    summary = result["review_summary"]
    lines = [
        f"# Clockify review action — {result['run_id']}",
        "",
        f"- Action: `{result['action']}`",
        f"- Review mode: `{result['review_mode']}`",
        f"- Exceptions-only eligible: `{str(bool(result['acceptance_gate'].get('exceptions_only_eligible'))).lower()}`",
        f"- Quality: `{result['quality_status']}`",
        (
            "- Delta: "
            f"{summary['new']} new, {summary['changed']} changed; "
            f"{summary['carried_pending']} carried pending"
        ),
        f"- Coverage warnings: {len(result['coverage_warnings'])}",
        (
            "- Clean batch: "
            f"{result['clean_batch']['count']} rows; "
            f"ID `{result['clean_batch']['batch_id'] or 'none'}`"
        ),
        (
            "- Accounting: "
            f"{result['accounting_summary']['proposals']} proposals, "
            f"{result['accounting_summary']['exceptions']} exceptions, "
            f"{result['accounting_summary']['contested_time']} contested"
        ),
        f"- Run report: `{result['paths']['run_report']}`",
        "",
    ]
    if result["review_mode"] == "exceptions_only":
        delta = [(str(item.get("category") or "exception"), item) for item in result["exceptions"]]
    else:
        delta = [("new", item) for item in result["new"]]
        delta.extend(("changed", item) for item in result["changed"])
    if delta:
        lines.extend(
            [
                "## Genuine exceptions" if result["review_mode"] == "exceptions_only" else "## Actionable delta",
                "",
                "| Kind | Review ID | Project | Description |",
                "|---|---|---|---|",
            ]
        )
        for kind, item in delta:
            description = str(item.get("description") or item.get("reason") or "")
            description = " ".join(description.split()).replace("|", "\\|")
            project = str(item.get("client_project") or "ambiguous").replace("|", "\\|")
            lines.append(
                f"| {kind} | {item.get('id', '')} | {project} | {description} |"
            )
        lines.append("")
    if result["coverage_warnings"]:
        lines.extend(["## Coverage warnings", ""])
        for warning in result["coverage_warnings"]:
            lines.append(
                f"- {warning.get('source', 'unknown')}: {warning.get('reason', '')}"
            )
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_current_review_csv(path: Path, snapshot: dict[str, Any]) -> None:
    """Write a stable-ID review export without changing any external Sheet."""
    categories = snapshot.get("categories", {})
    rows_by_id: dict[str, dict[str, Any]] = {}
    for name in ("new", "changed", "carried_pending"):
        for item in categories.get(name, []):
            rows_by_id[str(item.get("id") or "")] = item
    fields = [
        "Review ID",
        "Segments",
        "Start",
        "End",
        "Duration (min)",
        "Project",
        "Tags",
        "Source",
        "Confidence",
        "Description",
        "Disposition",
        "Revision",
        "Last Seen Run",
        "Reason",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item_id, item in sorted(rows_by_id.items()):
            segments = item.get("allocation_segments")
            if not isinstance(segments, list):
                segments = []
            start = item.get("start")
            end = item.get("end")
            raw_time = str(item.get("time") or "")
            if not start or not end:
                if "–" in raw_time:
                    start, end = raw_time.split("–", 1)
                elif " - " in raw_time:
                    start, end = raw_time.split(" - ", 1)
            duration = item.get("duration_minutes")
            if not duration and start and end:
                try:
                    start_dt = dt.datetime.fromisoformat(str(start).replace("Z", "+00:00"))
                    end_dt = dt.datetime.fromisoformat(str(end).replace("Z", "+00:00"))
                    duration = max(1, int((end_dt - start_dt).total_seconds() / 60))
                except ValueError:
                    duration = None
            source = item.get("source")
            if isinstance(source, list):
                source = ", ".join(str(value) for value in source)
            tags = item.get("tag_names")
            if isinstance(tags, list):
                tags = ", ".join(str(value) for value in tags)
            writer.writerow(
                {
                    "Review ID": item_id,
                    "Segments": "; ".join(
                        f"{segment.get('start', '')} - {segment.get('end', '')}"
                        for segment in segments
                        if isinstance(segment, dict)
                    ),
                    "Start": start or raw_time,
                    "End": end,
                    "Duration (min)": duration,
                    "Project": item.get("client_project"),
                    "Tags": tags,
                    "Source": source,
                    "Confidence": item.get("confidence"),
                    "Description": item.get("description") or item.get("label"),
                    "Disposition": item.get("disposition"),
                    "Revision": item.get("revision"),
                    "Last Seen Run": item.get("last_seen_run"),
                    "Reason": item.get("reason"),
                }
            )


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _collector_run_dir(stdout: str) -> Path:
    candidates = [
        Path(line.strip()).parent
        for line in stdout.splitlines()
        if line.strip().endswith("/run-report.md")
    ]
    if len(candidates) != 1:
        raise ValueError("Collector did not emit exactly one run-report.md path.")
    run_dir = candidates[0].resolve()
    if run_dir.parent != RUNS.resolve() or not (run_dir / "run-report.json").is_file():
        raise ValueError(f"Collector emitted an invalid run directory: {run_dir}")
    return run_dir


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="YYYY-MM-DD")
    parser.add_argument("--until", help="YYYY-MM-DD inclusive")
    parser.add_argument("--no-enrich", action="store_true")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--corrections", type=Path, default=DEFAULT_CORRECTIONS)
    parser.add_argument(
        "--review-mode",
        choices=sorted(REVIEW_MODES),
        default="shadow_all",
        help="shadow_all reviews the full denominator; exceptions_only requires a passing acceptance ledger.",
    )
    parser.add_argument(
        "--acceptance-ledger",
        type=Path,
        default=DEFAULT_ACCEPTANCE_LEDGER,
    )
    parser.add_argument(
        "--analysis-fixture",
        type=Path,
        help="Offline validated semantic response fixture; never used by scheduled production runs.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    acceptance_gate: dict[str, Any] = {
        "exceptions_only_eligible": False,
        "status": "not_recorded",
    }
    if args.acceptance_ledger.exists():
        try:
            acceptance_gate = review_acceptance.evaluate_gate(
                review_acceptance.load_ledger(args.acceptance_ledger)
            )
            acceptance_gate["status"] = "evaluated"
        except (OSError, json.JSONDecodeError, review_acceptance.AcceptanceError) as exc:
            if args.review_mode == "exceptions_only":
                print(f"clockify review run: acceptance ledger invalid: {exc}", file=sys.stderr)
                return 2
            acceptance_gate = {
                "exceptions_only_eligible": False,
                "status": "invalid",
                "reason": str(exc),
            }
    if args.review_mode == "exceptions_only" and not acceptance_gate.get("exceptions_only_eligible"):
        print(
            "clockify review run: exceptions_only is locked until one passing 90% baseline "
            "and two later consecutive passing 95% guarded periods",
            file=sys.stderr,
        )
        return 2
    collector = [
        sys.executable,
        str(SCRIPTS / "clockify_sync_collect.py"),
        "run",
    ]
    if args.since:
        collector.extend(["--since", args.since])
    if args.until:
        collector.extend(["--until", args.until])
    collector.append("--no-enrich" if args.no_enrich else "--enrich")

    collected = _run(collector)
    if collected.returncode != 0:
        print(collected.stderr or collected.stdout, file=sys.stderr)
        return collected.returncode or 2
    try:
        run_dir = _collector_run_dir(collected.stdout)
    except ValueError as exc:
        print(f"clockify review run: {exc}", file=sys.stderr)
        return 2

    accounting_command = [
        sys.executable,
        str(SCRIPTS / "work_accounting_pipeline.py"),
        str(run_dir),
        "--root",
        str(ROOT),
        "--corrections",
        str(args.corrections),
    ]
    if args.analysis_fixture:
        accounting_command.extend(["--analysis-fixture", str(args.analysis_fixture)])
    accounted = _run(accounting_command)
    if accounted.returncode != 0:
        quality = {
            "status": "blocked",
            "summary": {
                "semantic_analysis": "unavailable_or_invalid",
                "reason": (accounted.stderr or accounted.stdout).strip()[:500],
            },
        }
        _write_json(run_dir / "quality_report.json", quality)
        result = build_result(
            run_dir,
            quality,
            None,
            review_mode=args.review_mode,
            acceptance_gate=acceptance_gate,
        )
        result["paths"]["work_accounting_result"] = None
        result_path = run_dir / "autopilot-result.json"
        summary_path = run_dir / "autopilot-summary.md"
        _write_json(result_path, result)
        write_summary(summary_path, result)
        print(result_path)
        return accounted.returncode or 2

    checked = _run(
        [
            sys.executable,
            str(SCRIPTS / "clockify_sync_quality.py"),
            run_dir.name,
            "--runs-root",
            str(RUNS),
            "--root",
            str(ROOT),
        ]
    )
    if checked.returncode != 0:
        print(checked.stderr or checked.stdout, file=sys.stderr)
        return checked.returncode or 2
    quality = _read_json(run_dir / "quality_report.json")

    snapshot = None
    if quality.get("status") != "blocked":
        reconciled = _run(
            [
                sys.executable,
                str(SCRIPTS / "clockify_review_state.py"),
                str(run_dir),
                "--state",
                str(args.state),
            ]
        )
        if reconciled.returncode != 0:
            print(reconciled.stderr or reconciled.stdout, file=sys.stderr)
            return reconciled.returncode or 2
        snapshot = _read_json(run_dir / "review-snapshot.json")

    result = build_result(
        run_dir,
        quality,
        snapshot,
        review_mode=args.review_mode,
        acceptance_gate=acceptance_gate,
    )
    result["paths"]["work_accounting_result"] = str(
        (run_dir / "work-accounting-result.json").resolve()
    )
    result_path = run_dir / "autopilot-result.json"
    summary_path = run_dir / "autopilot-summary.md"
    if snapshot is not None:
        review_csv = run_dir / "review-current.csv"
        write_current_review_csv(review_csv, snapshot)
        result["paths"]["review_current_csv"] = str(review_csv.resolve())
    else:
        result["paths"]["review_current_csv"] = None
    _write_json(result_path, result)
    write_summary(summary_path, result)
    print(result_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
