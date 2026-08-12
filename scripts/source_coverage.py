#!/usr/bin/env python3
"""Durable per-source coverage debt for intermittent fleet collectors.

The ledger is local scheduler state.  It never claims that an unavailable
source contained no work: a failed interval remains debt until a later run
successfully recollects that source from the original interval start.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
PEER_PREFIXES = ("sessions/", "repositories/")


def _day(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"schema_version": SCHEMA_VERSION, "sources": {}}
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        return {"schema_version": SCHEMA_VERSION, "sources": {}}
    if not isinstance(value.get("sources"), dict):
        value["sources"] = {}
    return value


def bootstrap_from_runs(runs_root: Path, coordinator: str) -> dict[str, Any]:
    """Reconstruct debt from historical immutable run reports on first deploy."""
    ledger: dict[str, Any] = {"schema_version": SCHEMA_VERSION, "sources": {}}
    for report_path in sorted(runs_root.glob("*/run-report.json")):
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(report, Mapping):
            continue
        evidence = report.get("evidence_ledger")
        date_range = report.get("date_range")
        if not isinstance(evidence, Mapping) or not isinstance(date_range, Mapping):
            continue
        completeness = evidence.get("source_completeness")
        since = str(date_range.get("since") or "")
        if not isinstance(completeness, Mapping) or _day(since) is None:
            continue
        ledger = update(
            ledger,
            completeness=completeness,
            interval_since=since,
            interval_until=str(date_range.get("until") or "") or None,
            coordinator=coordinator,
            run_id=str(report.get("run_id") or report_path.parent.name),
            attempted_at=str(report.get("generated_at") or report_path.parent.name),
        )
    return ledger


def write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def is_peer(source: str, coordinator: str) -> bool:
    return source.startswith(PEER_PREFIXES) and source.rsplit("/", 1)[-1] != coordinator


def effective_since(requested_since: str | None, ledger: Mapping[str, Any]) -> str | None:
    candidates = [_day(requested_since)]
    sources = ledger.get("sources")
    if isinstance(sources, Mapping):
        candidates.extend(
            _day(value.get("debt_since"))
            for value in sources.values()
            if isinstance(value, Mapping)
        )
    valid = [value for value in candidates if value]
    return min(valid) if valid else None


def update(
    ledger: Mapping[str, Any],
    *,
    completeness: Mapping[str, Any],
    interval_since: str,
    interval_until: str | None,
    coordinator: str,
    run_id: str,
    attempted_at: str,
) -> dict[str, Any]:
    result = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": attempted_at,
        "sources": {
            str(key): dict(value)
            for key, value in (ledger.get("sources") or {}).items()
            if isinstance(value, Mapping)
        },
    }
    since = _day(interval_since)
    until = _day(interval_until)
    if since is None:
        return result
    inventory = completeness.get("sources")
    if not isinstance(inventory, Mapping):
        return result
    for source, raw in inventory.items():
        source = str(source)
        if not is_peer(source, coordinator) or not isinstance(raw, Mapping):
            continue
        previous = result["sources"].get(source, {})
        status = str(raw.get("status") or "partial")
        item = {
            **previous,
            "last_status": status,
            "last_attempt_at": attempted_at,
            "last_run_id": run_id,
        }
        if status in {"complete", "excluded"}:
            debt_since = _day(previous.get("debt_since"))
            if debt_since is None or since <= debt_since:
                item.pop("debt_since", None)
                if until:
                    item["covered_through"] = until
        else:
            debt_since = _day(previous.get("debt_since"))
            item["debt_since"] = min(value for value in (debt_since, since) if value)
            if raw.get("reason"):
                item["reason"] = str(raw["reason"])[:300]
        result["sources"][source] = item
    return result


def active_debt(ledger: Mapping[str, Any]) -> dict[str, str]:
    sources = ledger.get("sources")
    if not isinstance(sources, Mapping):
        return {}
    return {
        str(source): debt
        for source, value in sources.items()
        if isinstance(value, Mapping) and (debt := _day(value.get("debt_since")))
    }
