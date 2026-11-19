#!/usr/bin/env python3
"""Replay the parallel-billing guards over saved Clockify entries, offline.

Reads time entries already exported to JSON (the daily timesheet check's
``entries-YYYY-MM.private.json`` files) and reports what the two billing rules
would have changed: minutes billed to more than one client at once, and
minutes above the per-day and per-month parallel caps.  Nothing is fetched
from or written to Clockify.

The replay works on finished entries, so it is an estimate of the effect, not
a re-run of the accounting pipeline: entries are taken in chronological order
and the later of two overlapping entries is the one shortened.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import work_allocator


def _parse(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_entries(paths: Sequence[Path], *, zone: ZoneInfo, first: dt.date, last: dt.date) -> list[dict[str, Any]]:
    """Return the member's finished billable entries that start in the range."""
    seen: dict[str, dict[str, Any]] = {}
    for path in paths:
        for row in json.loads(path.read_text(encoding="utf-8")):
            if not row.get("me") or not row.get("billable") or not row.get("start") or not row.get("end"):
                continue
            start, end = _parse(row["start"]), _parse(row["end"])
            if end <= start or not first <= start.astimezone(zone).date() <= last:
                continue
            seen.setdefault(str(row["id"]), {
                "id": str(row["id"]),
                "client": str(row.get("client") or "(no client)"),
                "start": start,
                "end": end,
            })
    return sorted(seen.values(), key=lambda row: (row["start"], row["end"], row["id"]))


def _months(measured: Mapping[str, Any]) -> dict[str, dict[str, int]]:
    """Billed and wall-clock seconds per month, per client and over all clients."""
    months: dict[str, dict[str, int]] = {}
    for row in measured["client_months"]:
        totals = months.setdefault(row["month"], {"billed_seconds": 0, "client_wall_seconds": 0})
        totals["billed_seconds"] += row["billed_seconds"]
        totals["client_wall_seconds"] += row["wall_seconds"]
    return months


def _all_client_wall(entries: Sequence[Mapping[str, Any]], policy: work_allocator.BillingPolicy) -> dict[str, int]:
    """Wall-clock seconds per month with every client merged into one."""
    merged = work_allocator.measure_billing([("all", row["start"], row["end"]) for row in entries], policy)
    return {row["month"]: row["wall_seconds"] for row in merged["client_months"]}


def replay(entries: Sequence[Mapping[str, Any]], policy: work_allocator.BillingPolicy, month_scope: str) -> dict[str, Any]:
    rows = [(row["client"], row["start"], row["end"]) for row in entries]
    before = work_allocator.measure_billing(rows, policy)
    capped = work_allocator.cap_entries(entries, policy, month_scope=month_scope)
    after = work_allocator.measure_billing(
        [(row["client"], row["start"], row["end"]) for row in capped["entries"]], policy)
    return {
        "month_scope": month_scope,
        "entries": len(entries),
        "before": before,
        "after": after,
        "before_all_client_wall": _all_client_wall(entries, policy),
        "after_all_client_wall": _all_client_wall(capped["entries"], policy),
        "contested_wall_seconds": capped["contested_wall_seconds"],
        "cut_seconds": capped["cut_seconds"],
        "clients": capped["clients"],
    }


def _minutes(seconds: int) -> str:
    return f"{seconds / 60:,.0f}"


def _ratio(billed: int, wall: int) -> str:
    return f"{billed / wall:.2f}" if wall else "–"


def render(result: Mapping[str, Any], *, first: dt.date, last: dt.date) -> str:
    before, after = result["before"], result["after"]
    lines = [
        f"## Replay {first} – {last} ({result['entries']} entries, month scope: {result['month_scope']})",
        "",
        "| Measure | Before | After |",
        "|---|---|---|",
        f"| Minutes billed | {_minutes(sum(r['billed_seconds'] for r in before['client_days']))} "
        f"| {_minutes(sum(r['billed_seconds'] for r in after['client_days']))} |",
        f"| Wall-clock minutes billed to two or more clients | {_minutes(before['cross_client_seconds'])} "
        f"| {_minutes(after['cross_client_seconds'])} |",
        f"| Client-days above the day cap | {len(before['days_over_cap'])} | {len(after['days_over_cap'])} |",
        f"| Client-months above the month cap | {len(before['months_over_cap'])} | {len(after['months_over_cap'])} |",
        "",
        "| Minutes removed by | Minutes |",
        "|---|---|",
        f"| Same minute billed to another client | {_minutes(result['cut_seconds']['cross_client'])} |",
        f"| Day cap | {_minutes(result['cut_seconds']['day_cap'])} |",
        f"| Month cap | {_minutes(result['cut_seconds']['month_cap'])} |",
        "",
        "| Month | Multiplier over all clients, before | after |",
        "|---|---|---|",
    ]
    before_months, after_months = _months(before), _months(after)
    for month in sorted(before_months):
        lines.append(
            f"| {month} | {_ratio(before_months[month]['billed_seconds'], result['before_all_client_wall'].get(month, 0))} "
            f"| {_ratio(after_months.get(month, {}).get('billed_seconds', 0), result['after_all_client_wall'].get(month, 0))} |")
    lines += ["", "| Client | Month | Billed min before | after | Multiplier before | after |", "|---|---|---|---|---|---|"]
    after_by = {(row["client"], row["month"]): row for row in after["client_months"]}
    for row in before["client_months"]:
        other = after_by.get((row["client"], row["month"]), {"billed_seconds": 0, "wall_seconds": 0})
        lines.append(
            f"| {row['client']} | {row['month']} | {_minutes(row['billed_seconds'])} | {_minutes(other['billed_seconds'])} "
            f"| {_ratio(row['billed_seconds'], row['wall_seconds'])} | {_ratio(other['billed_seconds'], other['wall_seconds'])} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("entries", type=Path, nargs="+", help="exported entries JSON files or directories")
    parser.add_argument("--from", dest="first", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--to", dest="last", type=dt.date.fromisoformat, required=True)
    parser.add_argument("--routing", type=Path, default=ROOT / "routing.json")
    parser.add_argument("--month-scope", choices=[work_allocator.MONTH_SCOPE_CUMULATIVE, work_allocator.MONTH_SCOPE_PER_DAY],
                        default=work_allocator.MONTH_SCOPE_CUMULATIVE)
    parser.add_argument("--json", type=Path, help="also write the full result as JSON")
    args = parser.parse_args(argv)
    try:
        policy = work_allocator.billing_policy_from_routing(json.loads(args.routing.read_text(encoding="utf-8")))
    except (OSError, ValueError) as exc:
        print(f"billing caps replay: routing is unusable: {exc}", file=sys.stderr)
        return 2
    if policy is None:
        print("billing caps replay: routing declares no billing_caps", file=sys.stderr)
        return 2
    paths: list[Path] = []
    for path in args.entries:
        paths.extend(sorted(path.glob("entries-????-??.private.json")) if path.is_dir() else [path])
    if not paths:
        print("billing caps replay: no entries files found", file=sys.stderr)
        return 2
    try:
        entries = load_entries(paths, zone=ZoneInfo(policy.timezone), first=args.first, last=args.last)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"billing caps replay: entries are unusable: {exc}", file=sys.stderr)
        return 2
    result = replay(entries, policy, args.month_scope)
    if args.json:
        args.json.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    sys.stdout.write(render(result, first=args.first, last=args.last))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
