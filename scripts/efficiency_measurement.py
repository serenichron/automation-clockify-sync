#!/usr/bin/env python3
"""Offline, read-only before/after measurements for review-pipeline efficiency fixes.

Every subcommand reads sealed artifacts (analyzer cache, review diagnostics,
evidence ledgers, saved Clockify pages, cycle state) and prints numeric-only
JSON. Nothing here calls a model, Clockify, or writes into the inputs.
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import semantic_analyzer as semantic  # noqa: E402

BOOKKEEPING_CODES = {
    "contract_rejected_invalid_evidence_ids",
    "contract_rejected_duplicate_evidence",
    "contract_rejected_omitted_evidence",
}


def _jsonl(paths: Iterable[str]) -> Iterable[dict[str, Any]]:
    for path in paths:
        try:
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    try:
                        value = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        yield value
        except OSError:
            continue


def _review_diagnostics(repo: Path) -> dict[str, dict[str, Any]]:
    paths = [str(repo / "state" / "analyzer-cache.jsonl.review-diagnostics.jsonl")]
    paths += glob.glob(str(repo / "runs" / "*" / "*review-diagnostics*.jsonl"))
    paths += glob.glob(str(repo / "runs" / "*" / "replay-fixture" / "*review-diag*"))
    unique: dict[str, dict[str, Any]] = {}
    for value in _jsonl(paths):
        if "cache_key" in value and "coverage_contract" in value:
            unique.setdefault(str(value["cache_key"]), value)
    return unique


def partitions(repo: Path, threshold: float) -> dict[str, Any]:
    """Item 1: replay sealed numeric rejection skeletons through the normalizer."""
    diagnostics = _review_diagnostics(repo)
    outcome: dict[str, dict[str, int]] = {}
    routed_shares: list[float] = []
    for diagnostic in diagnostics.values():
        code = str(diagnostic.get("failure_code"))
        bucket = outcome.setdefault(code, {
            "skeletons": 0, "already_exact_cover": 0, "lossless": 0,
            "quarantine_within_threshold": 0, "quarantine_over_threshold": 0,
            "members_total": 0, "members_routed_when_applied": 0,
        })
        bucket["skeletons"] += 1
        member_counts = {
            item["bundle_ref"]: item["allowed_member_range"][1]
            for item in diagnostic["coverage_contract"]
        }
        normalized = semantic.normalize_review_partitions(
            {section: diagnostic.get(section, []) for section in ("activities", "exceptions", "omissions")},
            member_counts=member_counts,
        )
        if normalized is None:
            bucket["already_exact_cover"] += 1
            continue
        replacement, report = normalized
        _assert_exact_cover(replacement, member_counts)
        bucket["members_total"] += report["members_total"]
        share = report["members_routed"] / max(1, report["members_total"])
        if code in BOOKKEEPING_CODES:
            routed_shares.append(share)
        if report["members_routed"] == 0:
            bucket["lossless"] += 1
        elif share <= threshold:
            bucket["quarantine_within_threshold"] += 1
            bucket["members_routed_when_applied"] += report["members_routed"]
        else:
            bucket["quarantine_over_threshold"] += 1
    routed_shares.sort()
    deciles = {
        f"p{int(q * 100)}": round(routed_shares[min(len(routed_shares) - 1, int(q * len(routed_shares)))], 3)
        for q in (0.1, 0.25, 0.5, 0.75, 0.9)
    } if routed_shares else {}
    return {
        "corpus": "unique numeric review-rejection skeletons (state + runs sidecars)",
        "threshold_routed_share": threshold,
        "skeletons": len(diagnostics),
        "by_failure_code": outcome,
        "bookkeeping_routed_share_quantiles": deciles,
    }


def _assert_exact_cover(replacement: dict[str, Any], member_counts: dict[str, int]) -> None:
    seen: list[tuple[str, int]] = []
    for section in ("activities", "exceptions", "omissions"):
        for row in replacement[section]:
            for partition in row["evidence_partitions"]:
                for start, end in partition["member_ranges"]:
                    seen.extend((partition["bundle_ref"], n) for n in range(start, end + 1))
    expected = {(ref, n) for ref, total in member_counts.items() for n in range(1, total + 1)}
    if len(seen) != len(set(seen)) or set(seen) != expected:
        raise SystemExit("normalizer lost or duplicated evidence")


def cache_rejections(repo: Path) -> dict[str, Any]:
    """Item 1 denominator: sealed decisions by status/code in the live cache."""
    counts: dict[str, int] = {}
    for record in _jsonl([str(repo / "state" / "analyzer-cache.jsonl")]):
        key = record.get("failure_code") or record.get("status") or "unknown"
        counts[str(key)] = counts.get(str(key), 0) + 1
    total = sum(counts.values())
    bookkeeping = sum(counts.get(code, 0) for code in BOOKKEEPING_CODES)
    return {"records": total, "by_code": dict(sorted(counts.items())),
            "bookkeeping_rejections": bookkeeping,
            "bookkeeping_share_of_records": round(bookkeeping / max(1, total), 4)}


def _run_analysis_events(run_dir: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Rebuild the events analyze_ledger would send, without any inference."""
    from scripts import meeting_reconciliation
    from scripts import work_accounting_pipeline as pipeline

    ledger, events = pipeline.load_ledger(run_dir / "evidence" / "evidence-ledger.json")
    identities = meeting_reconciliation.manifest_member_identities(ledger.manifest.document())
    analysis_events, noise = pipeline._analysis_events(events, identities)
    routing = json.loads((run_dir / "routing.json").read_text(encoding="utf-8"))
    actor_contract = routing.get("semantic_actor_contract")
    hinted = pipeline._with_semantic_route_hints(
        analysis_events, routing, normalize_meeting_domains_type=actor_contract is not None,
    )
    if actor_contract is not None:
        hinted = semantic.with_actor_context(
            hinted, subject_binding=routing.get("semantic_subject_binding"))
    return hinted, {"ledger_events": len(events), "noise_events": len(noise),
                    "analysis_events": len(analysis_events)}


def projection(run_dirs: list[Path]) -> dict[str, Any]:
    """Item 2: extraction request bytes under the historical and v2 projection."""
    model = semantic.CURRENT_LIVE_FLASH_ROUTE[0]
    output: dict[str, Any] = {"runs": {}}
    totals = {"v1": 0, "v2": 0}
    for run_dir in run_dirs:
        events, counts = _run_analysis_events(run_dir)
        row: dict[str, Any] = dict(counts)
        for variant, variant_events in (("v1", events), ("v2", semantic.with_projection_contract(events))):
            chunks = semantic.chunk_events(
                variant_events, model=model, target_body_bytes=semantic.DEFAULT_CHUNK_BODY_BYTES,
                private_text_approved=True,
            )
            body_bytes = sum(
                len(semantic.canonical_json(semantic._body_for(
                    chunk, model=model, mode="extract", private_text_approved=True)).encode("utf-8"))
                for chunk in chunks
            )
            sizes = [len(semantic.project_event(dict(event))["content"].encode("utf-8"))
                     for event in variant_events]
            members = sum(len(chunk) for chunk in chunks)
            if members != len(variant_events):
                raise SystemExit("projection lost an evidence member")
            row[variant] = {"chunks": len(chunks), "extract_body_bytes": body_bytes,
                            "content_bytes": sum(sizes), "members": members,
                            "members_over_32kb": sum(size > 32_768 for size in sizes)}
            totals[variant] += body_bytes
        row["saved_extract_body_bytes"] = row["v1"]["extract_body_bytes"] - row["v2"]["extract_body_bytes"]
        output["runs"][run_dir.name] = row
    output["total_extract_body_bytes"] = totals
    output["saved_share"] = round(1 - totals["v2"] / max(1, totals["v1"]), 4)
    return output


def clockify_overlap(repo: Path) -> dict[str, Any]:
    """Item 3: replay saved overlap checkpoints under the v3 start bound."""
    import datetime as dt
    import math
    from scripts import clockify_sync_collect as collector

    def instant(value: Any) -> dt.datetime | None:
        return collector.parse_dt(value) if isinstance(value, str) and value else None

    lookback = collector.CLOCKIFY_OVERLAP_LOOKBACK
    overlap: list[dict[str, Any]] = []
    per_day: list[float] = []
    durations: list[float] = []
    for manifest_path in sorted(glob.glob(str(repo / "state" / "collector-checkpoints" / "*" /
                                               "source-checkpoints" / "*" / "manifest.json"))):
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
        identity = manifest.get("identity", {})
        if identity.get("source") != "clockify" or not manifest.get("complete"):
            continue
        since, until = instant(identity["since_utc"]), instant(identity["until_utc"])
        rows: list[dict[str, Any]] = []
        wire = 0
        for page in manifest["pages"]:
            saved = json.loads((Path(manifest_path).parent / page["path"]).read_text(encoding="utf-8"))
            rows.extend(saved["payload"])
            wire += (saved.get("metadata") or {}).get("wire_row_count", len(saved["payload"]))
        for row in rows:
            start, end = instant(row["timeInterval"].get("start")), instant(row["timeInterval"].get("end"))
            if start and end:
                durations.append((end - start).total_seconds())
        version = identity["compatibility_version"]
        if version == collector.CLOCKIFY_CHECKPOINT_COMPATIBILITY_VERSION:
            per_day.append(len(rows) / max(1e-9, (until - since).total_seconds() / 86400))
            continue
        bound = since - lookback
        retained_v3 = [row for row in rows if (instant(row["timeInterval"].get("start")) or bound) >= bound]
        elapsed = None
        if manifest.get("completed_at") and (manifest.get("metadata") or {}).get("snapshot_at"):
            elapsed = (instant(manifest["completed_at"]) - instant(manifest["metadata"]["snapshot_at"])).total_seconds()
        overlap.append({
            "window": [identity["since_utc"], identity["until_utc"]], "version": version,
            "pages": len(manifest["pages"]), "wire_rows": wire, "retained_rows": len(rows),
            "retained_rows_under_v3_bound": len(retained_v3),
            "overlap_set_identical": sorted(map(json.dumps, rows)) == sorted(map(json.dumps, retained_v3)),
            "collection_seconds": elapsed,
        })
    density = max(per_day) if per_day else 0.0
    window_days = lookback.total_seconds() / 86400 + 2
    estimated_rows = math.ceil(density * window_days)
    return {
        "lookback_days": lookback.total_seconds() / 86400,
        "longest_saved_entry_hours": round(max(durations) / 3600, 2) if durations else None,
        "saved_entries_with_duration": len(durations),
        "overlap_checkpoints": overlap,
        "max_entries_per_day_in_saved_v1_windows": round(density, 1),
        "estimated_v3_rows_for_two_day_slice": estimated_rows,
        "estimated_v3_pages": max(1, math.ceil((estimated_rows + 1) / collector.CLOCKIFY_PAGE_SIZE)),
    }


def circuit_breaker(journal: Path, threshold: int, cooldown_hours: float) -> dict[str, Any]:
    """Item 4: replay recorded failed cycles of one slice through the breaker.

    The journal is `journalctl --user -u clockify-review-cycle.service -o
    short-iso` text. Each failed run is paired with its "Consumed" CPU line and
    the slice it printed.
    """
    import datetime as dt
    import re

    consumed = re.compile(r"^(\S+) .*Consumed (?:(\d+)min )?([\d.]+)s CPU time")
    printed = re.compile(r'^\S+ .*\{"slices": \[\{"since": "([0-9-]+)"')
    runs: list[tuple[dt.datetime, float, str | None]] = []
    since: str | None = None
    for line in journal.read_text(encoding="utf-8").splitlines():
        if match := printed.match(line):
            since = match.group(1) if '"status": "failed"' in line else None
        elif match := consumed.match(line):
            seconds = float(match.group(2) or 0) * 60 + float(match.group(3))
            runs.append((dt.datetime.fromisoformat(match.group(1)), seconds, since))
            since = None
    failing = [(at, cpu) for at, cpu, slice_since in runs if slice_since is not None]
    executed: list[tuple[dt.datetime, float]] = []
    failures, last_failure = 0, None
    cooldown = dt.timedelta(hours=cooldown_hours)
    for at, cpu in failing:
        if failures >= threshold and last_failure is not None and at - last_failure < cooldown:
            continue
        executed.append((at, cpu))
        failures += 1
        last_failure = at
    return {
        "threshold": threshold, "cooldown_hours": cooldown_hours,
        "failed_cycles_recorded": len(failing),
        "failed_cycle_cpu_minutes_recorded": round(sum(cpu for _, cpu in failing) / 60, 1),
        "cycles_with_breaker": len(executed),
        "cpu_minutes_with_breaker": round(sum(cpu for _, cpu in executed) / 60, 1),
        "first_failure": failing[0][0].isoformat() if failing else None,
        "last_failure": failing[-1][0].isoformat() if failing else None,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(),
                        help="checkout holding state/ and runs/ (read-only)")
    sub = parser.add_subparsers(dest="command", required=True)
    item1 = sub.add_parser("partitions")
    item1.add_argument("--threshold", type=float,
                       default=semantic.PARTITION_NORMALIZATION_MAX_ROUTED_SHARE)
    sub.add_parser("cache-rejections")
    item2 = sub.add_parser("projection")
    item2.add_argument("runs", nargs="+", help="run directory names under runs/")
    sub.add_parser("clockify-overlap")
    item4 = sub.add_parser("circuit-breaker")
    item4.add_argument("journal", type=Path)
    item4.add_argument("--threshold", type=int, default=2)
    item4.add_argument("--cooldown-hours", type=float, default=72)
    args = parser.parse_args(argv)
    if args.command == "partitions":
        result = partitions(args.repo, args.threshold)
    elif args.command == "circuit-breaker":
        result = circuit_breaker(args.journal, args.threshold, args.cooldown_hours)
    elif args.command == "clockify-overlap":
        result = clockify_overlap(args.repo)
    elif args.command == "projection":
        result = projection([args.repo / "runs" / name for name in args.runs])
    else:
        result = cache_rejections(args.repo)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
