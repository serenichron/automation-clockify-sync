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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd(),
                        help="checkout holding state/ and runs/ (read-only)")
    sub = parser.add_subparsers(dest="command", required=True)
    item1 = sub.add_parser("partitions")
    item1.add_argument("--threshold", type=float,
                       default=semantic.PARTITION_NORMALIZATION_MAX_ROUTED_SHARE)
    sub.add_parser("cache-rejections")
    args = parser.parse_args(argv)
    if args.command == "partitions":
        result = partitions(args.repo, args.threshold)
    else:
        result = cache_rejections(args.repo)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
