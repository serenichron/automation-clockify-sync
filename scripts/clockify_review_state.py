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


def _evidence_ids(record: dict[str, Any]) -> list[str]:
    provenance = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
    values = record.get("evidence_ids", provenance.get("evidence_ids", []))
    if not isinstance(values, (list, tuple, set)):
        return []
    return sorted({str(value).strip() for value in values if str(value).strip()})


def _evidence_fingerprint(record: dict[str, Any]) -> str:
    values = _evidence_ids(record)
    if not values:
        return ""
    return "evfp:sha256:" + hashlib.sha256(_canonical({"evidence_ids": values}).encode("utf-8")).hexdigest()


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
        "provided_activity_key": _normal_text(record.get("review_activity_key")),
        "activity_id": _normal_text(record.get("activity_id")),
        "workstream_id": _normal_text(record.get("workstream_id")),
        "evidence_ids": _evidence_ids(record),
        "evidence_fingerprint": _evidence_fingerprint(record),
        "provenance": provenance,
        "anchor": anchor,
        "fallback": {**anchor, "project": project},
    }


def _keys(record: dict[str, Any]) -> dict[str, str]:
    identity = _identity(record)
    return {
        "candidate_key": identity["provided_candidate_key"],
        "activity_key": identity["provided_activity_key"] or (
            _digest(
                "act-",
                {
                    "activity_id": identity["activity_id"],
                    "workstream_id": identity["workstream_id"],
                    "evidence_fingerprint": identity["evidence_fingerprint"],
                },
            )
            if identity["activity_id"] and identity["evidence_fingerprint"]
            else ""
        ),
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


def _find_item(state: dict[str, Any], keys: dict[str, str], *, excluded_ids: set[str] | None = None) -> dict[str, Any] | None:
    items = state["items"]
    excluded_ids = excluded_ids or set()
    # Semantic allocation rows are collapsed into one activity-level review
    # record before matching.  That makes activity_key safe to use: allocation
    # movement or one-to-many segment changes cannot map one review decision to
    # several independently reviewable rows.
    for name in ("activity_key", "candidate_key", "provenance_key", "anchor_key", "fallback_key"):
        value = keys[name]
        if not value:
            continue
        matches = [item for item in items.values() if item["id"] not in excluded_ids and item.get("match_keys", {}).get(name) == value]
        if len(matches) == 1:
            return matches[0]
    return None


def _item_view(item: dict[str, Any]) -> dict[str, Any]:
    current = item.get("current", {})
    return {
        "id": item["id"],
        "candidate_key": item["candidate_key"],
        "activity_id": current.get("activity_id"),
        "workstream_id": current.get("workstream_id"),
        "evidence_ids": _evidence_ids(current),
        "evidence_fingerprint": _evidence_fingerprint(current),
        "disposition": item["disposition"],
        "revision": item["revision"],
        "last_seen_run": item.get("last_seen_run"),
        "start": current.get("start"),
        "end": current.get("end"),
        "duration_minutes": current.get("duration_minutes"),
        "allocation_segments": current.get("allocation_segments", []),
        "time": current.get("time"),
        "source": current.get("source"),
        "label": current.get("source_label") or current.get("label"),
        "description": current.get("description"),
        "client_project": current.get("client_project"),
        "tag_names": current.get("tag_names"),
        "confidence": current.get("confidence"),
        "reason": current.get("reason"),
        "parent_review_item_id": item.get("parent_review_item_id"),
        "supersedes": item.get("supersedes", []),
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


def _semantic_allocation(record: dict[str, Any]) -> bool:
    provenance = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
    return bool(
        record.get("activity_id")
        and _evidence_ids(record)
        and record.get("start")
        and record.get("end")
        and (
            record.get("allocation_mode")
            or provenance.get("source_type") == "semantic_activity"
        )
    )


def _aggregate_semantic_allocations(
    records: list[tuple[str, dict[str, Any]]],
) -> list[tuple[str, dict[str, Any]]]:
    """Collapse placement segments into one durable activity review item.

    Clockify may eventually receive several non-overlapping entries for one
    atomic accomplishment, but the human decision is about that accomplishment.
    Keeping the full segment collection on one item prevents allocation movement
    from creating fresh rvi IDs or orphaning an approve/skip/modify decision.
    """
    passthrough: list[tuple[str, dict[str, Any]]] = []
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for disposition, raw in records:
        record = _clean_record(raw)
        keys = _keys(record)
        if not _semantic_allocation(record) or not keys["activity_key"]:
            passthrough.append((disposition, record))
            continue
        groups.setdefault((disposition, keys["activity_key"]), []).append(record)

    for (disposition, activity_key), values in sorted(groups.items()):
        ordered = sorted(
            values,
            key=lambda value: (
                _normal_time(value.get("start")),
                _normal_time(value.get("end")),
                _normal_text(value.get("candidate_key")),
            ),
        )
        common_fields = (
            "activity_id", "workstream_id", "description", "client_project",
            "clockify_project_suffix", "tag_suffixes", "tag_names", "billable",
            "source", "source_label", "confidence", "timing_confidence",
            "rationale", "allocation_mode", "effort",
        )
        signature = {
            field: _canonical(value.get(field))
            for field in common_fields
            for value in ordered[:1]
        }
        if any(
            any(_canonical(value.get(field)) != signature[field] for field in common_fields)
            for value in ordered[1:]
        ):
            # Conflicting semantic rows must remain separately visible rather
            # than being silently combined under one human decision.
            passthrough.extend((disposition, value) for value in ordered)
            continue
        normalized_provenance = []
        for value in ordered:
            provenance = copy.deepcopy(value.get("provenance") or {})
            if isinstance(provenance, dict):
                provenance.pop("burst_start", None)
                provenance.pop("burst_end", None)
            normalized_provenance.append(provenance)
        if any(
            _canonical(value) != _canonical(normalized_provenance[0])
            for value in normalized_provenance[1:]
        ):
            passthrough.extend((disposition, value) for value in ordered)
            continue
        aggregate = copy.deepcopy(ordered[0])
        segments = [
            {
                "candidate_key": str(value.get("candidate_key") or ""),
                "segment": int(value.get("allocation_segment") or index),
                "start": value.get("start"),
                "end": value.get("end"),
                "duration_minutes": value.get("duration_minutes"),
            }
            for index, value in enumerate(ordered, 1)
        ]
        aggregate["candidate_key"] = activity_key
        aggregate["review_activity_key"] = activity_key
        aggregate["start"] = segments[0]["start"]
        aggregate["end"] = segments[-1]["end"]
        aggregate["duration_minutes"] = sum(
            int(segment.get("duration_minutes") or 0) for segment in segments
        )
        aggregate["allocation_segments"] = segments
        aggregate["proposal_candidate_keys"] = [segment["candidate_key"] for segment in segments]
        if isinstance(aggregate.get("provenance"), dict):
            aggregate["provenance"]["burst_start"] = segments[0]["start"]
            aggregate["provenance"]["burst_end"] = segments[-1]["end"]
        aggregate.pop("allocation_segment", None)
        passthrough.append((disposition, aggregate))
    return passthrough


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


def _parent_reference(record: dict[str, Any]) -> tuple[str, str]:
    """Return an explicit parent type/value, never inferring a legacy split."""
    provenance = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
    parent = record.get("parent") if isinstance(record.get("parent"), dict) else {}
    for key in ("parent_review_item_id", "supersedes"):
        value = record.get(key, parent.get(key, provenance.get(key)))
        if isinstance(value, list):
            value = value[0] if len(value) == 1 else ""
        if value:
            return "review_item_id", str(value)
    for key in ("parent_candidate_key", "legacy_candidate_key"):
        value = record.get(key, parent.get(key, provenance.get(key)))
        if value:
            return "candidate_key", str(value)
    return "", ""


def _declared_parent_fingerprint(record: dict[str, Any]) -> str:
    provenance = record.get("provenance") if isinstance(record.get("provenance"), dict) else {}
    parent = record.get("parent") if isinstance(record.get("parent"), dict) else {}
    return str(record.get("parent_evidence_fingerprint") or parent.get("evidence_fingerprint") or provenance.get("parent_evidence_fingerprint") or "")


def _parent_item(state: dict[str, Any], kind: str, value: str) -> dict[str, Any] | None:
    if kind == "review_item_id":
        return state["items"].get(value)
    matches = [item for item in state["items"].values() if item.get("candidate_key") == value]
    return matches[0] if len(matches) == 1 else None


def _split_plans(
    state: dict[str, Any],
    prepared: list[tuple[str, dict[str, Any], dict[str, str]]],
    warnings: list[dict[str, str]],
) -> dict[str, dict[str, Any]]:
    """Validate explicit legacy-to-segment migrations before touching history.

    A source activity ID alone is intentionally insufficient.  The parent must
    be named and fingerprinted, have two or more distinct children, and every
    parent evidence ID must be covered by the child evidence union.
    """
    groups: dict[str, list[tuple[dict[str, Any], dict[str, str]]]] = {}
    for _, record, keys in prepared:
        kind, value = _parent_reference(record)
        if kind and value:
            groups.setdefault(f"{kind}:{value}", []).append((record, keys))
    valid: dict[str, dict[str, Any]] = {}
    for group_key, children in sorted(groups.items()):
        kind, value = group_key.split(":", 1)
        parent = _parent_item(state, kind, value)
        reason = ""
        if parent is None:
            reason = "explicit parent review item was not found"
        elif len(children) < 2:
            reason = "split requires at least two child segments in the same replay"
        else:
            parent_fingerprint = _evidence_fingerprint(parent.get("current", {}))
            declared = {_declared_parent_fingerprint(record) for record, _ in children}
            child_ids = {str(record.get("activity_id") or "") for record, _ in children}
            child_evidence = set().union(*[set(_evidence_ids(record)) for record, _ in children])
            if not parent_fingerprint or declared != {parent_fingerprint}:
                reason = "parent evidence fingerprint is missing or does not match durable state"
            elif not all(_evidence_ids(record) for record, _ in children):
                reason = "every split child must carry cited evidence IDs"
            elif len(child_ids) != len(children) or "" in child_ids:
                reason = "split children require distinct stable activity IDs"
            elif child_evidence != set(_evidence_ids(parent.get("current", {}))):
                reason = "child evidence does not deterministically cover the parent evidence"
        if reason:
            _add_warning(warnings, "migration_required", group_key, reason)
            continue
        for record, keys in children:
            marker = _canonical({"record": record, "keys": keys})
            valid[marker] = {"parent": parent, "parent_fingerprint": _evidence_fingerprint(parent.get("current", {}))}
    return valid


def ingest_run(run_dir: Path, state: dict[str, Any]) -> dict[str, Any]:
    """Merge one run bundle into state and return its deterministic snapshot."""
    run_dir = run_dir.resolve()
    run_id = run_dir.name
    warnings: list[dict[str, str]] = []
    records = _aggregate_semantic_allocations(_load_records(run_dir, warnings))
    _evidence_warnings(run_dir, warnings)
    # Sorting removes collector ordering from both IDs and state history.
    prepared = sorted(
        ((disposition, _clean_record(record), _keys(record)) for disposition, record in records),
        key=lambda row: (_canonical(row[2]), _canonical(row[1])),
    )
    split_plans = _split_plans(state, prepared, warnings)
    categories: dict[str, list[dict[str, Any]]] = {
        "new": [], "changed": [], "carried_pending": [], "resolved_disappeared": []
    }
    seen_ids: set[str] = set()
    for initial_disposition, record, keys in prepared:
        split_plan = split_plans.get(_canonical({"record": record, "keys": keys}))
        item = _find_item(
            state,
            keys,
            excluded_ids={split_plan["parent"]["id"]} if split_plan is not None else None,
        )
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
            if split_plan is not None:
                parent = split_plan["parent"]
                item["parent_review_item_id"] = parent["id"]
                item["supersedes"] = [parent["id"]]
                item["parent_evidence_fingerprint"] = split_plan["parent_fingerprint"]
                item["history"].append({
                    "run_id": run_id,
                    "action": "split_child_linked",
                    "parent_review_item_id": parent["id"],
                    "parent_evidence_fingerprint": split_plan["parent_fingerprint"],
                })
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

    # Only a fully validated batch may supersede the legacy parent.  This is
    # deliberately after child creation so a malformed partial batch cannot
    # hide the original review item.
    superseded_parents: set[str] = set()
    for plan in split_plans.values():
        parent = plan["parent"]
        if parent["id"] in superseded_parents or parent["id"] not in state["items"]:
            continue
        old = parent["disposition"]
        if old != "superseded":
            parent["disposition"] = "superseded"
            parent["superseded_by"] = sorted(
                item["id"] for item in state["items"].values()
                if item.get("parent_review_item_id") == parent["id"]
            )
            parent.setdefault("history", []).append({
                "run_id": run_id,
                "action": "superseded_by_verified_split",
                "from": old,
                "children": parent["superseded_by"],
                "evidence_fingerprint": plan["parent_fingerprint"],
            })
        superseded_parents.add(parent["id"])

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
