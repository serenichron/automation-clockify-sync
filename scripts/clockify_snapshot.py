"""Canonical semantic snapshots for Clockify time-entry ledgers."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Iterable, Mapping


def _utc(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Clockify snapshot timestamps must contain an offset")
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _interval_value(entry: Mapping[str, Any], field: str) -> object:
    if entry.get(field) is not None:
        return entry[field]
    interval = entry.get("timeInterval", entry.get("time_interval", {}))
    return interval.get(field) if isinstance(interval, Mapping) else None


def _project_id(entry: Mapping[str, Any]) -> object:
    if entry.get("project_id") is not None:
        return entry["project_id"]
    if entry.get("projectId") is not None:
        return entry["projectId"]
    project = entry.get("project")
    return project.get("id") if isinstance(project, Mapping) else None


def _tag_ids(entry: Mapping[str, Any]) -> Iterable[object]:
    value = entry.get("tag_ids")
    if value is None:
        value = entry.get("tagIds", entry.get("tags", ()))
    if not isinstance(value, (list, tuple)):
        return ()
    return (item.get("id") if isinstance(item, Mapping) else item for item in value)


def normalized_snapshot_sha256(entries: Iterable[Mapping[str, Any]]) -> str:
    """Digest the semantic Clockify fields used to prove a final live ledger.

    The normalized row and ordering exactly match the original poster digest;
    aliases only make the same identity available from raw Clockify responses.
    """
    rows = [
        {
            "id": str(entry.get("id") or ""),
            "start": _utc(str(_interval_value(entry, "start") or "")),
            "end": _utc(str(_interval_value(entry, "end") or "")),
            "project_id": str(_project_id(entry) or ""),
            "tag_ids": sorted(str(value) for value in _tag_ids(entry)),
            "description": str(entry.get("description") or "").strip(),
            "billable": entry.get("billable"),
        }
        for entry in entries
    ]
    rows.sort(key=lambda row: (
        row["start"], row["end"], row["id"], row["project_id"],
        tuple(row["tag_ids"]), row["description"], row["billable"],
    ))
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
