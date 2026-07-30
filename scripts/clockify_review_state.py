#!/usr/bin/env python3
"""Durable, local review state for Clockify collector run bundles.

The collector deliberately produces fresh, display-oriented proposal IDs on
every run.  This tool turns those records into stable review items without
contacting Clockify, Sheets, Multica, or any other external service.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATE = ROOT / "state" / "review-items.json"
SCHEMA_VERSION = 1
DISPOSITIONS = {"pending", "ambiguous", "approved", "posted", "rejected", "superseded"}
ACTIVE_DISPOSITIONS = {"pending", "ambiguous"}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


def _normal_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _normal_time(value: Any) -> str:
    """Normalize harmless formatting differences while retaining the actual instant."""
    value = _normal_text(value)
    return value.replace(" ", "T") if re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d", value) else value


def _source_parts(record: dict[str, Any]) -> tuple[str, str]:
    source = record.get("source", "")
    if isinstance(source, list):
        source = ",".join(str(v) for v in source)
    source = _normal_text(source)
    machine = _normal_text(record.get("machine"))
    if ":" in source:
        source_type, source_machine = source.split(":", 1)
        return source_type, machine or source_machine
    return source or _normal_text(record.get("source_type")), machine


def _time_parts(record: dict[str, Any]) -> tuple[str, str]:
    start, end = record.get("start"), record.get("end")
    if start or end:
        return _normal_time(start), _normal_time(end)
    raw = str(record.get("time") or "")
    # Collector ambiguous rows use an en dash; accept an ASCII dash surrounded by
    # spaces too, but do not split the dashes inside ISO dates.
    if "–" in raw:
        left, right = raw.split("–", 1)
        return _normal_time(left), _normal_time(right)
    if " - " in raw:
        left, right = raw.split(" - ", 1)
        return _normal_time(left), _normal_time(right)
    return _normal_time(raw), ""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _digest(prefix: str, value: Any) -> str:
    return prefix + hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()[:20]


def _clean_record(record: dict[str, Any]) -> dict[str, Any]:
    """Drop ephemeral display IDs, retaining all review-relevant collector data."""
    cleaned = copy.deepcopy(record)
    cleaned.pop("id", None)
    return cleaned


def _identity(record: dict[str, Any]) -> dict[str, Any]:
    source_type, source_machine = _source_parts(record)
    start, end = _time_parts(record)
    label = _normal_text(record.get("source_label") or record.get("label"))
    provenance = record.get("provenance")
    if not isinstance(provenance, dict):
        provenance = {}
    provenance = {str(k): provenance[k] for k in sorted(provenance) if provenance[k] not in (None, "", [], {})}
    anchor = {
        "source_type": source_type,
        "source_machine": source_machine,
        "session_id": _normal_text(record.get("session_id") or provenance.get("source_session_id")),
        "path": _normal_text(record.get("path") or record.get("cwd") or provenance.get("path")),
        "start": start or _normal_time(provenance.get("burst_start")),
        "end": end or _normal_time(provenance.get("burst_end")),
        "label": label,
    }
    # Remove empty optional components; source + time remain the safe last resort.
    anchor = {key: value for key, value in anchor.items() if value}
    project = _normal_text(record.get("client_project") or record.get("project"))
    return {
        "provided_candidate_key": _normal_text(record.get("candidate_key")),
        "provenance": provenance,
        "anchor": anchor,
        "fallback": {**anchor, "project": project},
    }


def _keys(record: dict[str, Any]) -> dict[str, str]:
    identity = _identity(record)
    return {
        "candidate_key": identity["provided_candidate_key"],
        "provenance_key": _digest("pv-", identity["provenance"]) if identity["provenance"] else "",
        "anchor_key": _digest("sa-", identity["anchor"]),
        "fallback_key": _digest("sf-", identity["fallback"]),
    }


def _new_state() -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "items": {}}


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return _new_state()
    state = _read_json(path)
    if not isinstance(state, dict) or state.get("schema_version") != SCHEMA_VERSION or not isinstance(state.get("items"), dict):
        raise ValueError(f"Unsupported or invalid review state: {path}")
    for item_id, item in state["items"].items():
        if not isinstance(item, dict) or item.get("id") != item_id or item.get("disposition") not in DISPOSITIONS:
            raise ValueError(f"Invalid review item in state: {item_id}")
    return state


def _find_item(state: dict[str, Any], keys: dict[str, str]) -> dict[str, Any] | None:
    items = state["items"]
    for name in ("candidate_key", "provenance_key", "anchor_key", "fallback_key"):
        value = keys[name]
        if not value:
            continue
        matches = [item for item in items.values() if item.get("match_keys", {}).get(name) == value]
        if len(matches) == 1:
            return matches[0]
    return None


def _item_view(item: dict[str, Any]) -> dict[str, Any]:
    current = item.get("current", {})
    return {
        "id": item["id"],
        "candidate_key": item["candidate_key"],
        "disposition": item["disposition"],
        "revision": item["revision"],
        "last_seen_run": item.get("last_seen_run"),
        "start": current.get("start"),
        "end": current.get("end"),
        "duration_minutes": current.get("duration_minutes"),
        "time": current.get("time"),
        "source": current.get("source"),
        "label": current.get("source_label") or current.get("label"),
        "description": current.get("description"),
        "client_project": current.get("client_project"),
        "tag_names": current.get("tag_names"),
        "confidence": current.get("confidence"),
        "reason": current.get("reason"),
    }


def _add_warning(warnings: list[dict[str, str]], kind: str, source: str, reason: str) -> None:
    warnings.append({"type": kind, "source": source, "reason": reason})


def _load_records(run_dir: Path, warnings: list[dict[str, str]]) -> list[tuple[str, dict[str, Any]]]:
    records: list[tuple[str, dict[str, Any]]] = []
    for filename, disposition in (("proposals.json", "pending"), ("ambiguous.json", "ambiguous")):
        path = run_dir / filename
        if not path.exists():
            _add_warning(warnings, "source_unavailable", filename, "Expected collector output is missing.")
            continue
        data = _read_json(path)
        if not isinstance(data, list):
            raise ValueError(f"Expected a JSON list in {path}")
        for record in data:
            if not isinstance(record, dict):
                raise ValueError(f"Expected object records in {path}")
            records.append((disposition, record))
    return records


def _evidence_warnings(run_dir: Path, warnings: list[dict[str, str]]) -> None:
    report = run_dir / "run-report.json"
    if not report.exists():
        return
    try:
        evidence = _read_json(report).get("evidence", {})
    except (OSError, ValueError, json.JSONDecodeError):
        _add_warning(warnings, "coverage_warning", "run-report.json", "Could not read collector evidence status.")
        return
    if not isinstance(evidence, dict):
        return
    healthy = {"ok", "available", "success", "complete"}
    sessions = evidence.get("sessions", [])
    if isinstance(sessions, list):
        for session in sessions:
            if not isinstance(session, dict):
                continue
            status = _normal_text(session.get("status"))
            if status and status not in healthy:
                source = f"sessions/{session.get('machine') or 'unknown'}"
                reason = f"Collector evidence status: {status}."
                errors = session.get("errors") or []
                if errors:
                    reason += f" {str(errors[0])[:200]}"
                _add_warning(warnings, "source_unavailable", source, reason)
    for source, status_data in sorted(evidence.items()):
        if not isinstance(status_data, dict):
            continue
        status = _normal_text(status_data.get("status"))
        if status and status not in healthy:
            _add_warning(warnings, "source_unavailable", source, f"Collector evidence status: {status}.")


def set_disposition(state: dict[str, Any], item_id: str, disposition: str, run_id: str = "manual") -> None:
    if disposition not in DISPOSITIONS:
        raise ValueError(f"Unsupported disposition: {disposition}")
    item = state["items"].get(item_id)
    if item is None:
        raise ValueError(f"Unknown review item: {item_id}")
    old = item["disposition"]
    if old != disposition:
        item["disposition"] = disposition
        item.setdefault("history", []).append({"run_id": run_id, "action": "disposition_changed", "from": old, "to": disposition})


def ingest_run(run_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Merge one run bundle into state and return its deterministic snapshot."""
    run_dir = run_dir.resolve()
    run_id = run_dir.name
    warnings: list[dict[str, str]] = []
    records = _load_records(run_dir, warnings)
    _evidence_warnings(run_dir, warnings)
    # Sorting removes collector ordering from both IDs and state history.
    prepared = sorted(
        ((disposition, _clean_record(record), _keys(record)) for disposition, record in records),
        key=lambda row: (_canonical(row[2]), _canonical(row[1])),
    )
    categories: dict[str, list[dict[str, Any]]] = {
        "new": [], "changed": [], "carried_pending": [], "resolved_disappeared": []
    }
    seen_ids: set[str] = set()
    for initial_disposition, record, keys in prepared:
        item = _find_item(state, keys)
        if item is None:
            stable_key = keys["candidate_key"] or keys["provenance_key"] or keys["anchor_key"] or keys["fallback_key"]
            item_id = _digest("rvi-", {"stable_key": stable_key})
            # Extremely unlikely hash collision; fail safely rather than merge records.
            if item_id in state["items"]:
                raise ValueError(f"Review item ID collision for {item_id}")
            item = {
                "id": item_id,
                "candidate_key": stable_key,
                "match_keys": keys,
                "disposition": initial_disposition,
                "first_seen_run": run_id,
                "last_seen_run": run_id,
                "revision": 1,
                "current": record,
                "history": [{"run_id": run_id, "action": "created", "disposition": initial_disposition}],
            }
            state["items"][item_id] = item
            categories["new"].append(_item_view(item))
        else:
            # Preserve every prior matching key: a collector upgrade may start
            # emitting provenance/candidate keys after older fallback-only runs.
            item.setdefault("match_keys", {}).update({k: v for k, v in keys.items() if v})
            prior = item.get("current", {})
            revised = _canonical(prior) != _canonical(record)
            if revised:
                item["revision"] = int(item.get("revision", 1)) + 1
                item["current"] = record
                item.setdefault("history", []).append({"run_id": run_id, "action": "revised", "revision": item["revision"]})
            # A later routed proposal is concrete evidence that an earlier
            # ambiguous source can move into review.  Never make the opposite
            # transition automatically, and never change a terminal decision.
            if item["disposition"] == "ambiguous" and initial_disposition == "pending":
                item["disposition"] = "pending"
                item.setdefault("history", []).append({"run_id": run_id, "action": "routed_for_review", "from": "ambiguous", "to": "pending"})
            item["last_seen_run"] = run_id
            if revised:
                categories["changed"].append(_item_view(item))
            elif item["disposition"] in ACTIVE_DISPOSITIONS:
                categories["carried_pending"].append(_item_view(item))
        seen_ids.add(item["id"])

    for item in sorted(state["items"].values(), key=lambda value: value["id"]):
        if item["id"] in seen_ids:
            continue
        if item["disposition"] in ACTIVE_DISPOSITIONS:
            categories["carried_pending"].append(_item_view(item))
        else:
            categories["resolved_disappeared"].append(_item_view(item))
    if not records and categories["carried_pending"]:
        _add_warning(warnings, "coverage_warning", "collector candidates", "Run contains zero candidates; active review items were carried forward without closure.")
    for values in categories.values():
        values.sort(key=lambda value: value["id"])
    warnings.sort(key=lambda value: (value["type"], value["source"], value["reason"]))
    return {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "run_dir": str(run_dir),
        "categories": categories,
        "coverage_warnings": warnings,
        "summary": {name: len(values) for name, values in categories.items()},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Maintain durable local Clockify review state")
    parser.add_argument("run_dir", type=Path, help="Collector run directory containing proposals.json and ambiguous.json")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE, help=f"State path (default: {DEFAULT_STATE})")
    parser.add_argument("--dry-run", action="store_true", help="Print snapshot without writing state or snapshot files")
    parser.add_argument("--set-disposition", action="append", default=[], metavar="ITEM_ID=STATUS", help="Apply an explicit local disposition before ingesting")
    args = parser.parse_args(argv)
    try:
        state = load_state(args.state)
        for change in args.set_disposition:
            item_id, sep, disposition = change.partition("=")
            if not sep or not item_id or not disposition:
                raise ValueError("--set-disposition must be ITEM_ID=STATUS")
            set_disposition(state, item_id, disposition, args.run_dir.name)
        snapshot = ingest_run(args.run_dir, state)
        if not args.dry_run:
            _write_json(args.state, state)
            _write_json(args.run_dir / "review-snapshot.json", snapshot)
        print(json.dumps(snapshot, indent=2, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"clockify review state: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
