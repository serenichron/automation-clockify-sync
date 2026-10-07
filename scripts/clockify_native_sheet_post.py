#!/usr/bin/env python3
"""Post one exact native Google Sheet approval snapshot to Clockify.

`plan` is read-only. `execute` requires an approval receipt bound to the exact
plan and records durable per-row intent/readback events before reporting
completion. Optional explicit source adoptions account for retained prior
accomplishments; their current payload is not reposted or applied as an update.
Distinct reviewed rows and all approved intervals are otherwise preserved.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping, Sequence
import urllib.parse
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import clockify_period_readback, clockify_post_approved_portfolio as legacy, clockify_source_adoptions
from scripts.clockify_sync_collect import clockify_env_candidates, load_env_file


PLAN_SCHEMA = "clockify-native-sheet-plan/v1"
APPROVAL_SCHEMA = "clockify-native-sheet-approval/v1"
EVENT_SCHEMA = "clockify-native-sheet-post-event/v1"
RECEIPT_SCHEMA = "clockify-native-sheet-post-receipt/v1"


class NativePostError(ValueError):
    pass


class AmbiguousCreate(NativePostError):
    """A create request may have reached Clockify but did not return safely."""


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _document_digest(document: Mapping[str, Any], field: str) -> str:
    return _digest({key: value for key, value in document.items() if key != field})


def _atomic_write(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("x", encoding="utf-8") as handle:
        handle.write(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _cell_value(cell: Mapping[str, Any]) -> Any:
    value = cell.get("effectiveValue", cell.get("userEnteredValue", {}))
    if not isinstance(value, Mapping):
        return ""
    for key in ("stringValue", "numberValue", "boolValue"):
        if key in value:
            return value[key]
    return ""


def _sheet_rows(document: Mapping[str, Any], *, pending_only: bool = True
                ) -> tuple[str, str, list[tuple[int, dict[str, Any]]]]:
    structured = document.get("structuredContent")
    if not isinstance(structured, Mapping):
        raise NativePostError("native Sheet capture lacks structuredContent")
    spreadsheet_id = str(structured.get("spreadsheetId") or "")
    sheets = structured.get("sheets")
    if not spreadsheet_id or not isinstance(sheets, list) or len(sheets) != 1:
        raise NativePostError("native Sheet capture target is invalid")
    sheet = sheets[0]
    if not isinstance(sheet, Mapping):
        raise NativePostError("native Sheet capture sheet is invalid")
    properties = sheet.get("properties")
    data = sheet.get("data")
    if not isinstance(properties, Mapping) or not isinstance(data, list) or not data:
        raise NativePostError("native Sheet capture grid is invalid")
    title = str(properties.get("title") or "")
    grid = data[0]
    raw_rows = grid.get("rowData") if isinstance(grid, Mapping) else None
    if not title or not isinstance(raw_rows, list) or not raw_rows:
        raise NativePostError("native Sheet capture rows are missing")
    first = raw_rows[0]
    headers = [_cell_value(cell) for cell in first.get("values", [])]
    expected = [
        "Review ID", "Start", "End", "Duration (min)", "Project", "Tags",
        "Source", "Confidence", "Description", "Disposition", "Revision",
        "Last Seen Run", "Reason", "Review Status", "Review Notes",
    ]
    if headers != expected:
        raise NativePostError("native Sheet capture headers differ from the posting contract")
    rows: list[tuple[int, dict[str, Any]]] = []
    for row_number, raw in enumerate(raw_rows[1:], 2):
        values = [_cell_value(cell) for cell in raw.get("values", [])]
        values += [""] * (len(expected) - len(values))
        if pending_only and (str(values[9]).strip().lower() != "pending"
                             or str(values[13]).strip().lower() != "unposted"):
            continue
        rows.append((row_number, dict(zip(expected, values))))
    if not rows:
        raise NativePostError("native Sheet capture has no pending/unposted rows")
    return spreadsheet_id, title, rows


def _route_values(routing: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    values: list[Mapping[str, Any]] = []
    for section in ("session_routes", "meeting_routes", "evidence_routes"):
        raw = routing.get(section, [])
        if not isinstance(raw, list) or any(not isinstance(item, Mapping) for item in raw):
            raise NativePostError(f"routing {section} is invalid")
        values.extend(raw)
    lifecycle = routing.get("client_lifecycle_routes", [])
    if not isinstance(lifecycle, list) or any(not isinstance(item, Mapping) for item in lifecycle):
        raise NativePostError("routing client_lifecycle_routes is invalid")
    for item in lifecycle:
        activation = item.get("activation")
        route = activation.get("route") if isinstance(activation, Mapping) else None
        if route is not None:
            if not isinstance(route, Mapping):
                raise NativePostError("routing lifecycle activation route is invalid")
            values.append(route)
    return values


def _resolve_routes(routing: Mapping[str, Any], projects: Sequence[Mapping[str, Any]],
                    tags: Sequence[Mapping[str, Any]]) -> dict[tuple[str, tuple[str, ...]], dict[str, Any]]:
    project_ids = legacy._unique_by_suffix(projects, "project")
    tag_ids = legacy._unique_by_suffix(tags, "tag")
    result: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for route in _route_values(routing):
        project_name = str(route.get("project_name") or "").strip()
        project_suffix = str(route.get("project_suffix") or "")
        tag_names = tuple(str(value) for value in route.get("tag_names", []))
        tag_suffixes = [str(value) for value in route.get("tag_suffixes", [])]
        if not project_name or not project_suffix or len(tag_names) != len(tag_suffixes):
            continue
        if project_suffix not in project_ids or any(value not in tag_ids for value in tag_suffixes):
            raise NativePostError(f"Clockify route IDs are unavailable for {project_name}")
        resolved = {"projectId": project_ids[project_suffix],
                    "tagIds": sorted(tag_ids[value] for value in tag_suffixes),
                    "taskId": route.get("task_id"), "billable": bool(route.get("billable", True))}
        key = (project_name, tag_names)
        if key in result and result[key] != resolved:
            raise NativePostError(f"Clockify route is ambiguous for {project_name}")
        result[key] = resolved
    return result


def _parse_sheet_time(value: Any, timezone: str) -> dt.datetime:
    try:
        try:
            parsed = dt.datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
        except ValueError:
            parsed = dt.datetime.strptime(str(value), "%Y-%m-%d %H:%M")
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone))
    except (ValueError, TypeError) as error:
        raise NativePostError("Sheet posting timestamps are invalid") from error
    return parsed.astimezone(dt.timezone.utc)


def _utc(value: dt.datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalized_live(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    interval = entry.get("timeInterval")
    if not isinstance(interval, Mapping) or not interval.get("start") or not interval.get("end"):
        return None
    return {"id": str(entry.get("id") or ""), "start": legacy._utc(str(interval["start"])),
            "end": legacy._utc(str(interval["end"])), "projectId": str(entry.get("projectId") or ""),
            "tagIds": sorted(str(value) for value in entry.get("tagIds", [])),
            "taskId": str(entry.get("taskId") or "") or None,
            "description": str(entry.get("description") or "").strip(),
            "billable": entry.get("billable")}


def _live_digest(entries: Sequence[Mapping[str, Any]]) -> str:
    normalized = [value for item in entries if (value := _normalized_live(item)) is not None]
    return _digest(sorted(normalized, key=lambda item: (item["start"], item["end"], item["id"])))


def _payload_matches(payload: Mapping[str, Any], entry: Mapping[str, Any]) -> bool:
    interval = entry.get("timeInterval")
    if not isinstance(interval, Mapping):
        return False
    try:
        start = legacy._parse(str(interval.get("start") or ""))
        end = legacy._parse(str(interval.get("end") or ""))
        if start != legacy._parse(payload["start"]) or end != legacy._parse(payload["end"]):
            return False
        if "duration" in interval:
            seconds = clockify_period_readback._duration(interval["duration"], "Clockify duration")
            if dt.timedelta(seconds=seconds) != end - start:
                return False
    except (ValueError, ArithmeticError):
        return False
    live = _normalized_live(entry)
    if live is None:
        return False
    return all((
        live["start"] == payload["start"], live["end"] == payload["end"],
        live["projectId"] == payload["projectId"], live["tagIds"] == sorted(payload["tagIds"]),
        live["taskId"] == (str(payload.get("taskId") or "") or None),
        live["description"] == str(payload["description"]).strip(),
        isinstance(live["billable"], bool) and live["billable"] == payload["billable"],
    ))


def _overlap(payload: Mapping[str, Any], entry: Mapping[str, Any]) -> dict[str, Any] | None:
    live = _normalized_live(entry)
    if live is None:
        return None
    start = max(legacy._parse(payload["start"]), legacy._parse(live["start"]))
    end = min(legacy._parse(payload["end"]), legacy._parse(live["end"]))
    seconds = int((end - start).total_seconds())
    if seconds <= 0:
        return None
    return {"clockify_entry_id": live["id"], "start": _utc(start), "end": _utc(end),
            "overlap_seconds": seconds}


def _row_interval(row: Mapping[str, Any], timezone: str) -> tuple[dt.datetime, dt.datetime, int]:
    start = _parse_sheet_time(row["Start"], timezone)
    end = _parse_sheet_time(row["End"], timezone)
    minutes = row["Duration (min)"]
    seconds = int((end - start).total_seconds())
    # Only tolerate float-display roundoff; timestamps remain the exact authority.
    if (isinstance(minutes, bool) or not isinstance(minutes, (int, float))
            or not math.isfinite(minutes) or seconds <= 0
            or not math.isclose(minutes * 60, seconds, rel_tol=0, abs_tol=1e-9)):
        raise NativePostError(f"Sheet posting duration is inconsistent for {row['Review ID']}")
    return start, end, seconds


def _row_payload(row: Mapping[str, Any], routes: Mapping[Any, Any], timezone: str
                 ) -> tuple[dict[str, Any], int]:
    start, end, seconds = _row_interval(row, timezone)
    tag_names = tuple(value.strip() for value in str(row["Tags"]).split(",") if value.strip())
    key = (str(row["Project"]).strip(), tag_names)
    route = routes.get(key)
    if route is None:
        raise NativePostError(f"Clockify route is missing for {key[0]} / {', '.join(tag_names)}")
    return {"start": _utc(start), "end": _utc(end),
            "description": str(row["Description"]).strip(), **route}, seconds


def _same_interval_route(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    return all(left[key] == right[key] for key in ("start", "end", "projectId", "taskId", "tagIds", "billable"))


def _posted_source_credit(item: Mapping[str, Any], source: str,
                          rows: Sequence[tuple[int, Mapping[str, Any]]], routes: Mapping[Any, Any],
                          timezone: str, live_entries: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    if not re.fullmatch(r"act-[0-9a-f]{24}", source):
        return None
    candidates = []
    for row_number, row in rows:
        if (str(row["Source"]).strip() != source or not str(row["Review ID"]).strip()
                or str(row["Disposition"]).strip().lower() != "approved"
                or str(row["Review Status"]).strip().lower() != "posted"):
            continue
        try:
            payload, _ = _row_payload(row, routes, timezone)
        except NativePostError:
            continue  # Unverified historical rows are not authority to suppress a POST.
        if _same_interval_route(payload, item["payload"]):
            candidates.append((row_number, row, payload))
    matches = [(row_number, row, payload, entry)
               for row_number, row, payload in candidates for entry in live_entries
               if _payload_matches(payload, entry) and str(entry.get("id") or "")]
    native_ids = {str(entry["id"]) for _, _, _, entry in matches}
    if len(native_ids) > 1:
        raise NativePostError("canonical Sheet source identity is ambiguous; audit prior native entries before posting")
    if not matches:
        return None
    # Previously credited aliases can carry renamed descriptions. Retain the
    # first captured fact that exactly proves the one distinct native identity.
    row_number, row, payload, entry = matches[0]
    credit = {"source": source, "current_review_id": item["review_id"],
              "current_payload_digest": item["payload_digest"],
              "prior_row_number": row_number, "prior_row": dict(row), "payload": payload,
              "clockify_entry_id": str(entry["id"]), "readback_digest": _live_digest([entry])}
    credit["credit_digest"] = _digest(credit)
    return credit


def _validate_sheet_source_credit(item: Mapping[str, Any], timezone: str) -> None:
    try:
        credit = item["sheet_source_credit"]
        row = credit["prior_row"]
        start, end, _ = _row_interval(row, timezone)
        if (set(credit) != {"source", "current_review_id", "current_payload_digest", "prior_row_number",
                           "prior_row", "payload", "clockify_entry_id", "readback_digest", "credit_digest"}
                or credit["credit_digest"] != _document_digest(credit, "credit_digest")
                or "prior_entry_credit" in item
                or not re.fullmatch(r"act-[0-9a-f]{24}", credit["source"])
                or credit["source"] != item["source"] or credit["source"] != str(row["Source"]).strip()
                or credit["current_review_id"] != item["review_id"]
                or credit["current_payload_digest"] != item["payload_digest"]
                or item["payload_digest"] != _digest(item["payload"])
                or not str(row["Review ID"]).strip()
                or type(credit["prior_row_number"]) is not int or credit["prior_row_number"] < 2
                or str(row["Disposition"]).strip().lower() != "approved"
                or str(row["Review Status"]).strip().lower() != "posted"
                or credit["payload"]["start"] != _utc(start) or credit["payload"]["end"] != _utc(end)
                or credit["payload"]["description"] != str(row["Description"]).strip()
                or not _same_interval_route(credit["payload"], item["payload"])
                or not isinstance(credit["clockify_entry_id"], str) or not credit["clockify_entry_id"]
                or not re.fullmatch(r"[0-9a-f]{64}", credit["readback_digest"])):
            raise NativePostError("sealed Sheet source credit binding differs")
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise NativePostError("sealed Sheet source credit is invalid") from error


def _batch_alias_leaders(entries: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    leaders: dict[str, str] = {}
    aliases: dict[str, str] = {}
    for item in entries:
        if "prior_entry_credit" in item or "sheet_source_credit" in item:
            continue
        key = _canonical(item["payload"])
        if key in leaders:
            aliases[item["review_id"]] = leaders[key]
        else:
            leaders[key] = item["review_id"]
    return aliases


def _check_unique_native_ids(entries: Sequence[Mapping[str, Any]], entry_ids: Sequence[str],
                             confirmed: Mapping[str, Mapping[str, Any]] | None = None) -> None:
    by_id: dict[str, Mapping[str, Any]] = {}
    for item, entry_id in zip(entries, entry_ids):
        prior = by_id.get(entry_id)
        if not entry_id:
            raise NativePostError("each review row must bind one unique Clockify entry")
        if prior is not None:
            aliases = _batch_alias_leaders(entries)
            event = (confirmed or {}).get(item["review_id"], {})
            leader = (confirmed or {}).get(prior["review_id"], {})
            if (aliases.get(item["review_id"]) == prior["review_id"]
                    and event.get("disposition") == "same_batch_payload_credit"
                    and event.get("alias_of_review_id") == prior["review_id"]
                    and leader.get("clockify_entry_id") == entry_id
                    and item["payload"] == prior["payload"]):
                continue
            left, right = prior.get("sheet_source_credit"), item.get("sheet_source_credit")
            # Only exact aliases of the same sealed prior Sheet fact can share credit.
            keys = ("source", "prior_row_number", "prior_row", "payload", "clockify_entry_id", "readback_digest")
            if (left is None or right is None or left["clockify_entry_id"] != entry_id
                    or any(left[key] != right[key] for key in keys)):
                raise NativePostError("each review row must bind one unique Clockify entry")
        by_id[entry_id] = item


def build_plan(document: Mapping[str, Any], *, capture_sha256: str, routing: Mapping[str, Any],
               routing_sha256: str, timezone: str, workspace_id: str, member_id: str,
               projects: Sequence[Mapping[str, Any]], tags: Sequence[Mapping[str, Any]],
               live_entries: Sequence[Mapping[str, Any]],
               source_adoptions: Mapping[str, Any] | None = None) -> dict[str, Any]:
    spreadsheet_id, sheet_title, rows = _sheet_rows(document)
    _, _, captured_rows = _sheet_rows(document, pending_only=False)
    routes = _resolve_routes(routing, projects, tags)
    entries: list[dict[str, Any]] = []
    total_seconds = 0
    seen: set[str] = set()
    for row_number, row in rows:
        review_id = str(row["Review ID"]).strip()
        if not review_id or review_id in seen:
            raise NativePostError("Sheet posting review IDs are missing or duplicated")
        seen.add(review_id)
        payload, seconds = _row_payload(row, routes, timezone)
        total_seconds += seconds
        overlaps = [value for item in live_entries if (value := _overlap(payload, item)) is not None]
        entries.append({"review_id": review_id, "row_number": row_number,
                        "duration_minutes": seconds // 60 if seconds % 60 == 0 else seconds / 60,
                        "payload": payload,
                        "payload_digest": _digest(payload), "live_overlaps": overlaps})
    result = {"schema_version": PLAN_SCHEMA, "spreadsheet_id": spreadsheet_id,
              "sheet_title": sheet_title, "capture_sha256": capture_sha256,
              "routing_sha256": routing_sha256, "timezone": timezone,
              "workspace_id": workspace_id, "member_id": member_id,
              "live_snapshot_sha256": _live_digest(live_entries), "entries": entries,
              "row_count": len(entries),
              "total_minutes": total_seconds // 60 if total_seconds % 60 == 0 else total_seconds / 60}
    result["review_ids_sha256"] = _digest(sorted(seen))
    if source_adoptions is not None:
        try:
            credits = clockify_source_adoptions.credits(
                source_adoptions, entries, workspace_id=workspace_id, member_id=member_id,
                live_entries=live_entries,
            )
        except clockify_source_adoptions.AdoptionError as error:
            raise NativePostError(str(error)) from error
        for item in entries:
            if item["review_id"] in credits:
                item["prior_entry_credit"] = credits[item["review_id"]]
        result["source_adoptions_sha256"] = _digest(sorted(
            source_adoptions["declarations"], key=lambda value: value["current_review_id"],
        ))
    for item, (_, row) in zip(entries, rows):
        if "prior_entry_credit" in item:
            continue  # Explicit audited adoptions keep their existing contract.
        source = str(row["Source"]).strip()
        credit = _posted_source_credit(item, source, captured_rows, routes, timezone, live_entries)
        if credit is not None:
            item.update(source=source, sheet_source_credit=credit)
    result["plan_digest"] = _document_digest(result, "plan_digest")
    return result


def approval_template(plan: Mapping[str, Any], *, approval_id: str, approver: str,
                      approved_at: str, expires_at: str) -> dict[str, Any]:
    return {"schema_version": APPROVAL_SCHEMA, "approval_id": approval_id, "approver": approver,
            "approved_at": approved_at, "expires_at": expires_at,
            **{key: plan[key] for key in ("spreadsheet_id", "sheet_title", "capture_sha256",
                                          "routing_sha256", "workspace_id", "member_id",
                                          "live_snapshot_sha256", "review_ids_sha256",
                                          "plan_digest", "row_count", "total_minutes")}}


def _validate_approval(plan: Mapping[str, Any], approval: Mapping[str, Any], now: dt.datetime) -> str:
    if approval.get("schema_version") != APPROVAL_SCHEMA or plan.get("schema_version") != PLAN_SCHEMA:
        raise NativePostError("approval or plan schema is invalid")
    if plan.get("plan_digest") != _document_digest(plan, "plan_digest"):
        raise NativePostError("approval-bound plan digest is invalid")
    for key in ("spreadsheet_id", "sheet_title", "capture_sha256", "routing_sha256",
                "workspace_id", "member_id", "live_snapshot_sha256", "review_ids_sha256",
                "plan_digest", "row_count", "total_minutes"):
        if approval.get(key) != plan.get(key):
            raise NativePostError("approval does not match exact plan")
    try:
        approved = legacy._parse(str(approval["approved_at"]))
        expires = legacy._parse(str(approval["expires_at"]))
    except (KeyError, ValueError) as error:
        raise NativePostError("approval timestamps are invalid") from error
    if not approved <= now.astimezone(dt.timezone.utc) < expires:
        raise NativePostError("approval is not currently valid")
    approval_id = str(approval.get("approval_id") or "")
    if not approval_id or not str(approval.get("approver") or ""):
        raise NativePostError("approval identity is missing")
    if any(item["payload_digest"] != _digest(item["payload"]) for item in plan["entries"]):
        raise NativePostError("approved row payload digest differs from its exact payload")
    return _digest(approval)


def _events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return _decode_events(path.read_bytes())


def _decode_events(content: bytes) -> list[dict[str, Any]]:
    output = []
    previous = None
    for sequence, line in enumerate(content.decode("utf-8").splitlines()):
        record = json.loads(line)
        payload = {key: value for key, value in record.items() if key != "event_digest"}
        expected = _digest(payload)
        if (record.get("schema_version") != EVENT_SCHEMA or record.get("sequence") != sequence
                or record.get("previous_digest") != previous or record.get("event_digest") != expected):
            raise NativePostError("post event ledger integrity failure")
        output.append(record)
        previous = expected
    return output


def _append_event(path: Path, event: Mapping[str, Any]) -> None:
    records = _events(path)
    record = {"schema_version": EVENT_SCHEMA, "sequence": len(records),
              "previous_digest": records[-1]["event_digest"] if records else None, **event}
    record["event_digest"] = _digest(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(_canonical(record) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def _confirmed_by_review(records: Sequence[Mapping[str, Any]], approval_digest: str,
                         plan_digest: str) -> tuple[dict[str, Mapping[str, Any]],
                                                    dict[str, Mapping[str, Any]],
                                                    dict[str, Mapping[str, Any]]]:
    intents: dict[str, Mapping[str, Any]] = {}
    responses: dict[str, Mapping[str, Any]] = {}
    confirmed: dict[str, Mapping[str, Any]] = {}
    for record in records:
        if record.get("approval_digest") != approval_digest or record.get("plan_digest") != plan_digest:
            raise NativePostError("post event ledger approval binding drift")
        review_id = str(record.get("review_id") or "")
        if record.get("event_type") == "intent":
            if review_id in intents:
                raise NativePostError("post event ledger repeats intent")
            intents[review_id] = record
        elif record.get("event_type") == "created_response":
            if review_id not in intents or review_id in responses or review_id in confirmed:
                raise NativePostError("post event ledger create response ordering failure")
            if not str(record.get("clockify_entry_id") or ""):
                raise NativePostError("post event ledger create response lacks an entry ID")
            responses[review_id] = record
        elif record.get("event_type") == "confirmed":
            if review_id not in intents or review_id in confirmed:
                raise NativePostError("post event ledger terminal ordering failure")
            response = responses.get(review_id)
            if (record.get("disposition") == "created" and response is not None and
                    response.get("clockify_entry_id") != record.get("clockify_entry_id")):
                raise NativePostError("post event ledger created terminal lacks its response identity")
            confirmed[review_id] = record
        else:
            raise NativePostError("post event ledger event type is invalid")
    return intents, responses, confirmed


@contextmanager
def execution_lock(events_path: Path):
    lock_path = events_path.with_name(events_path.name + ".execution.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise NativePostError("Clockify Sheet posting is already in progress") from error
        yield
    finally:
        os.close(descriptor)


def execute_plan(plan: Mapping[str, Any], approval: Mapping[str, Any], events_path: Path,
                 receipt_path: Path, gateway: Any, *, now: dt.datetime) -> dict[str, Any]:
    with execution_lock(events_path):
        return _execute_plan_locked(plan, approval, events_path, receipt_path, gateway, now=now)


def _item_matches(item: Mapping[str, Any], entry: Mapping[str, Any]) -> bool:
    sheet_credit = item.get("sheet_source_credit")
    if sheet_credit is not None:
        return (str(entry.get("id") or "") == sheet_credit["clockify_entry_id"]
                and _payload_matches(sheet_credit["payload"], entry)
                and _live_digest([entry]) == sheet_credit["readback_digest"])
    credit = item.get("prior_entry_credit")
    if credit is None:
        return _payload_matches(item["payload"], entry)
    if credit.get("verification_basis") == "audited_manual_meeting":
        return (clockify_source_adoptions.current_live_matches(
            credit["payload"], entry, workspace_id=credit["workspace_id"],
            member_id=credit["member_id"], entry_id=credit["clockify_entry_id"],
        ) and _digest(entry) == credit["retained_entry_digest"]
                and _live_digest([entry]) == credit["readback_digest"])
    if credit.get("verification_basis") == "current_live_snapshot":
        binding = credit["confirmed_binding"]
        if not clockify_source_adoptions.current_live_matches(
            credit["payload"], entry, workspace_id=binding["workspace_id"],
            member_id=binding["member_id"], entry_id=credit["clockify_entry_id"],
        ):
            return False
    return (str(entry.get("id") or "") == credit["clockify_entry_id"]
            and _payload_matches(credit["payload"], entry)
            and _live_digest([entry]) == credit["readback_digest"])


def _execute_plan_locked(plan: Mapping[str, Any], approval: Mapping[str, Any], events_path: Path,
                         receipt_path: Path, gateway: Any, *, now: dt.datetime) -> dict[str, Any]:
    approval_digest = _validate_approval(plan, approval, now)
    plan_digest = str(plan["plan_digest"])
    gateway.verify_target(str(plan["workspace_id"]), str(plan["member_id"]))
    entries = list(plan["entries"])
    period_start = min(item["payload"]["start"] for item in entries)
    period_end = max(item["payload"]["end"] for item in entries)
    records = _events(events_path)
    intents, responses, confirmed = _confirmed_by_review(records, approval_digest, plan_digest)
    aliases = _batch_alias_leaders(entries)
    by_review = {item["review_id"]: item for item in entries}
    for record in records:
        item = by_review.get(record.get("review_id"))
        if item is None or record.get("payload_digest") != _digest(item["payload"]):
            raise NativePostError("post event payload differs from approved row")
        if "alias_of_review_id" in record:
            if (record["alias_of_review_id"] != aliases.get(item["review_id"])
                    or record["event_type"] == "created_response"
                    or (record["event_type"] == "confirmed"
                        and record.get("disposition") != "same_batch_payload_credit")):
                raise NativePostError("post event alias binding differs from approved batch")
    for review_id, leader_id in aliases.items():
        intent = intents.get(review_id)
        if intent is not None and review_id not in confirmed and intent.get("alias_of_review_id") != leader_id:
            raise NativePostError("unmarked historical alias intent may already have posted")
        terminal = confirmed.get(review_id)
        if terminal is not None and terminal.get("disposition") == "same_batch_payload_credit":
            leader = confirmed.get(leader_id)
            if (intent is None or intent.get("alias_of_review_id") != leader_id
                    or terminal.get("alias_of_review_id") != leader_id or leader is None
                    or terminal.get("clockify_entry_id") != leader.get("clockify_entry_id")):
                raise NativePostError("post event alias lacks confirmed leader binding")
    current = gateway.period_entries(period_start, period_end)
    if not records and _live_digest(current) != plan["live_snapshot_sha256"]:
        raise NativePostError("live Clockify snapshot drifted after approval")

    # Consume only sealed plan facts and fresh direct GETs before creating even
    # an unrelated row. Original artifact paths are provenance, not replay IO.
    declared = [item["prior_entry_credit"]["declaration"] for item in entries if "prior_entry_credit" in item]
    credited_readbacks: dict[str, Mapping[str, Any]] = {}
    if declared or "source_adoptions_sha256" in plan:
        if _digest(sorted(declared, key=lambda value: value["current_review_id"])) != plan.get("source_adoptions_sha256"):
            raise NativePostError("source adoption snapshot differs from approved plan")
        try:
            credits = clockify_source_adoptions.validate_sealed_credits(
                entries, workspace_id=str(plan["workspace_id"]), member_id=str(plan["member_id"]),
            )
        except clockify_source_adoptions.AdoptionError as error:
            raise NativePostError(str(error)) from error
        for item in entries:
            if "prior_entry_credit" not in item:
                continue
            readback = gateway.entry_by_id(credits[item["review_id"]]["clockify_entry_id"])
            if not isinstance(readback, Mapping) or not _item_matches(item, readback):
                raise NativePostError("source adoption prior entry failed exact direct GET readback")
            credited_readbacks[item["review_id"]] = readback

    for item in entries:
        if "sheet_source_credit" not in item:
            continue
        _validate_sheet_source_credit(item, str(plan["timezone"]))
        readback = gateway.entry_by_id(item["sheet_source_credit"]["clockify_entry_id"])
        if not isinstance(readback, Mapping) or not _item_matches(item, readback):
            raise NativePostError("Sheet source credit prior entry failed exact direct GET readback")
        credited_readbacks[item["review_id"]] = readback
    credited_items = [item for item in entries if item["review_id"] in credited_readbacks]
    _check_unique_native_ids(credited_items, [str(credited_readbacks[item["review_id"]]["id"])
                                            for item in credited_items])

    # An unbound exact match is not proof of source credit, but it is also
    # not permission to create another identical native entry. Check the
    # whole batch before any new intent or POST; distinct overlaps stay valid.
    for item in entries:
        review_id = item["review_id"]
        if (review_id in confirmed or review_id in intents or review_id in responses
                or review_id in aliases or review_id in credited_readbacks):
            continue
        exact_ids = sorted(str(entry["id"]) for entry in current
                           if entry.get("id") and _payload_matches(item["payload"], entry))
        if exact_ids:
            raise NativePostError(
                f"existing exact payload for {review_id}; verify source identity before posting: "
                + ", ".join(exact_ids))

    for item in entries:
        review_id = item["review_id"]
        payload = item["payload"]
        terminal = confirmed.get(review_id)
        if terminal is not None:
            readback = gateway.entry_by_id(str(terminal.get("clockify_entry_id") or ""))
            if not isinstance(readback, Mapping) or not _item_matches(item, readback):
                raise NativePostError("receipt-bound Clockify entry failed exact GET readback")
            continue
        intent = intents.get(review_id)
        response = responses.get(review_id)
        alias_of = aliases.get(review_id)
        if alias_of is not None:
            leader = confirmed.get(alias_of)
            if leader is None or response is not None:
                raise NativePostError("batch payload alias lacks confirmed leader")
            entry_id = str(leader["clockify_entry_id"])
            proof_entry = gateway.entry_by_id(entry_id)
            if not isinstance(proof_entry, Mapping) or not _payload_matches(payload, proof_entry):
                raise NativePostError("batch payload alias failed exact direct GET readback")
            if intent is None:
                _append_event(events_path, {"event_type": "intent", "approval_digest": approval_digest,
                              "plan_digest": plan_digest, "review_id": review_id,
                              "payload_digest": item["payload_digest"], "alias_of_review_id": alias_of,
                              "before_entry_ids": sorted(str(entry["id"]) for entry in current if entry.get("id")),
                              "recorded_at": _utc(now)})
            disposition = "same_batch_payload_credit"
        elif "prior_entry_credit" in item or "sheet_source_credit" in item:
            if response is not None:
                raise NativePostError("existing credit row unexpectedly has a create response")
            if intent is None:
                _append_event(events_path, {"event_type": "intent", "approval_digest": approval_digest,
                              "plan_digest": plan_digest, "review_id": review_id,
                              "payload_digest": item["payload_digest"],
                              "before_entry_ids": sorted(str(entry["id"]) for entry in current if entry.get("id")),
                              "recorded_at": _utc(now)})
            credit = item.get("sheet_source_credit", item.get("prior_entry_credit"))
            entry_id = credit["clockify_entry_id"]
            proof_entry = credited_readbacks[review_id]
            disposition = (
                "existing_credit" if "sheet_source_credit" in item else
                "credited_manual_meeting" if credit.get("verification_basis") == "audited_manual_meeting" else
                "credited_prior_source"
            )
        elif response is not None:
            entry_id = str(response["clockify_entry_id"])
            recovered = gateway.entry_by_id(entry_id)
            if not isinstance(recovered, Mapping) or not _payload_matches(payload, recovered):
                raise NativePostError("created Clockify entry failed direct GET readback")
            disposition = "created"
            proof_entry = recovered
        elif intent is not None:
            prior_ids = set(intent["before_entry_ids"])
            recovery_live = gateway.recovery_entries(period_start, period_end)
            recovered = [entry for entry in recovery_live if str(entry.get("id")) not in prior_ids
                         and _payload_matches(payload, entry)]
            if len(recovered) != 1:
                raise NativePostError("ambiguous prior POST lacks one unique exact GET recovery")
            disposition = "recovered_after_ambiguous_response"
            entry_id = str(recovered[0]["id"])
            proof_entry = recovered[0]
        else:
            before_ids = sorted(str(entry.get("id")) for entry in current if entry.get("id"))
            _append_event(events_path, {"event_type": "intent", "approval_digest": approval_digest,
                          "plan_digest": plan_digest, "review_id": review_id,
                          "payload_digest": item["payload_digest"], "before_entry_ids": before_ids,
                          "recorded_at": _utc(now)})
            try:
                created = gateway.create({key: value for key, value in payload.items()
                                          if key != "taskId" or value is not None})
            except AmbiguousCreate:
                refreshed = gateway.period_entries(period_start, period_end)
                matches = [entry for entry in refreshed if str(entry.get("id")) not in set(before_ids)
                           and _payload_matches(payload, entry)]
                if len(matches) != 1:
                    raise NativePostError("ambiguous POST lacks one unique exact GET recovery")
                entry_id = str(matches[0]["id"])
                disposition = "recovered_after_ambiguous_response"
                current = refreshed
                proof_entry = matches[0]
            else:
                if not isinstance(created, Mapping) or not created.get("id"):
                    raise NativePostError("Clockify create response lacks an entry ID")
                entry_id = str(created["id"])
                _append_event(events_path, {"event_type": "created_response",
                              "approval_digest": approval_digest, "plan_digest": plan_digest,
                              "review_id": review_id, "payload_digest": item["payload_digest"],
                              "clockify_entry_id": entry_id, "recorded_at": _utc(now)})
                readback = gateway.entry_by_id(entry_id)
                if not isinstance(readback, Mapping) or not _payload_matches(payload, readback):
                    raise NativePostError("created Clockify entry failed direct GET readback")
                disposition = "created"
                proof_entry = readback
        if all(str(entry.get("id") or "") != entry_id for entry in current):
            current.append(dict(proof_entry))
        _append_event(events_path, {"event_type": "confirmed", "approval_digest": approval_digest,
                      "plan_digest": plan_digest, "review_id": review_id,
                      "payload_digest": item["payload_digest"], "clockify_entry_id": entry_id,
                      "disposition": disposition, "readback_digest": _live_digest([proof_entry]),
                      **({"alias_of_review_id": alias_of} if disposition == "same_batch_payload_credit" else {}),
                      "recorded_at": _utc(now)})
        records = _events(events_path)
        intents, responses, confirmed = _confirmed_by_review(records, approval_digest, plan_digest)

    final_live = gateway.period_entries(period_start, period_end)
    records = _events(events_path)
    _, _, confirmed = _confirmed_by_review(records, approval_digest, plan_digest)
    if len(confirmed) != len(entries):
        raise NativePostError("post event ledger does not cover every approved row")
    _check_unique_native_ids(entries, [str(confirmed[item["review_id"]].get("clockify_entry_id") or "")
                                      for item in entries], confirmed)
    receipt_entries = []
    for item in entries:
        event = confirmed[item["review_id"]]
        readback = gateway.entry_by_id(str(event["clockify_entry_id"]))
        if not isinstance(readback, Mapping) or not _item_matches(item, readback):
            raise NativePostError("final per-ID Clockify readback differs from approved payload")
        receipt_entry = {"review_id": item["review_id"], "row_number": item["row_number"],
                                "payload_digest": item["payload_digest"],
                                "clockify_entry_id": event["clockify_entry_id"],
                                "disposition": event["disposition"],
                                "live_overlaps": item["live_overlaps"]}
        if event["disposition"] == "same_batch_payload_credit":
            receipt_entry.update(alias_of_review_id=event["alias_of_review_id"],
                                 posting_semantics="same_batch_identical_payload_not_posted")
        if "prior_entry_credit" in item:
            credit = item["prior_entry_credit"]
            if credit.get("verification_basis") == "audited_manual_meeting":
                receipt_entry.update(
                    posting_semantics="manual_native_entry_retained_current_payload_not_posted",
                    retained_native_payload=credit["payload"], retained_entry_digest=credit["retained_entry_digest"],
                    approved_current_seconds=credit["approved_current_seconds"],
                    retained_native_seconds=credit["retained_native_seconds"], posted_seconds=0,
                    adoption_declaration_digest=credit["declaration_digest"],
                    adoption_verification_basis="audited_manual_meeting",
                )
            else:
                receipt_entry.update(
                    posting_semantics="prior_accomplishment_retained_current_payload_not_posted",
                    prior_approved_payload=credit["payload"], prior_payload_digest=_digest(credit["payload"]),
                    adoption_declaration_digest=credit["declaration_digest"],
                    adoption_verification_basis=credit.get("verification_basis", "historical_native_readback"),
                )
        if "sheet_source_credit" in item:
            credit = item["sheet_source_credit"]
            receipt_entry.update(
                posting_semantics="existing_source_retained_current_payload_not_posted",
                prior_approved_payload=credit["payload"], prior_payload_digest=_digest(credit["payload"]),
                source=credit["source"], prior_review_id=credit["prior_row"]["Review ID"],
                prior_row_number=credit["prior_row_number"], source_credit_digest=credit["credit_digest"],
            )
        receipt_entries.append(receipt_entry)
    receipt = {"schema_version": RECEIPT_SCHEMA, "status": "complete",
               "approval_id": approval["approval_id"], "approval_digest": approval_digest,
               "plan_digest": plan_digest, "row_count": len(entries),
               "total_minutes": plan["total_minutes"], "entries": receipt_entries,
               "final_live_snapshot_sha256": _live_digest(final_live),
               "event_ledger_sha256": hashlib.sha256(events_path.read_bytes()).hexdigest()}
    if any("sheet_source_credit" in item for item in entries):
        # Approval totals count visible rows; credit totals count retained native
        # entries once, even when multiple current rows are exact source aliases.
        credited_ids: set[str] = set()
        credited_seconds = posted_seconds = 0
        for item in entries:
            event = confirmed[item["review_id"]]
            if event["disposition"] in {"existing_credit", "credited_prior_source", "credited_manual_meeting"}:
                if event["clockify_entry_id"] not in credited_ids:
                    credited_ids.add(event["clockify_entry_id"])
                    retained = item["prior_entry_credit"]["payload"] if event["disposition"] == "credited_manual_meeting" else item["payload"]
                    credited_seconds += int((legacy._parse(retained["end"])
                                             - legacy._parse(retained["start"])).total_seconds())
            else:
                posted_seconds += int((legacy._parse(item["payload"]["end"])
                                       - legacy._parse(item["payload"]["start"])).total_seconds())
        receipt.update(posted_minutes=posted_seconds / 60, existing_credit_minutes=credited_seconds / 60)
    manual_credits = [item["prior_entry_credit"] for item in entries
                      if item.get("prior_entry_credit", {}).get("verification_basis") == "audited_manual_meeting"]
    if manual_credits:
        receipt.update(
            approved_current_seconds=sum(clockify_source_adoptions._seconds(item["payload"]) for item in entries),
            retained_native_seconds=sum(credit["retained_native_seconds"] for credit in manual_credits),
            posted_seconds=sum(clockify_source_adoptions._seconds(item["payload"]) for item in entries
                               if confirmed[item["review_id"]]["disposition"] in {"created", "recovered_after_ambiguous_response"}),
        )
    if any(event["disposition"] == "same_batch_payload_credit" for event in confirmed.values()):
        posted_seconds = alias_seconds = 0
        for item in entries:
            seconds = int((legacy._parse(item["payload"]["end"])
                           - legacy._parse(item["payload"]["start"])).total_seconds())
            disposition = confirmed[item["review_id"]]["disposition"]
            if disposition == "same_batch_payload_credit":
                alias_seconds += seconds
            elif disposition not in {"existing_credit", "credited_prior_source", "credited_manual_meeting"}:
                posted_seconds += seconds
        receipt.update(posted_minutes=posted_seconds / 60, same_batch_alias_minutes=alias_seconds / 60)
    _atomic_write(receipt_path, receipt)
    return receipt


class ClockifyGateway:
    def __init__(self, api_key: str, timeout_seconds: int):
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    def verify_target(self, workspace: str, member: str) -> None:
        account = legacy._request("/user", self.api_key, timeout_seconds=self.timeout_seconds)
        target = legacy._request(f"/workspaces/{workspace}", self.api_key,
                                 timeout_seconds=self.timeout_seconds)
        if not isinstance(account, Mapping) or str(account.get("id")) != member:
            raise NativePostError("configured Clockify account does not match approved member")
        if not isinstance(target, Mapping) or str(target.get("id")) != workspace:
            raise NativePostError("configured Clockify workspace does not match approval")

    def period_entries(self, start: str, end: str) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"start": legacy._utc(start), "end": legacy._utc(end)})
        return legacy._paged(
            f"/workspaces/{self.workspace}/user/{self.member}/time-entries?{query}",
            self.api_key, timeout_seconds=self.timeout_seconds,
        )

    def recovery_entries(self, start: str, end: str) -> list[dict[str, Any]]:
        widened_start = legacy._parse(start) - dt.timedelta(days=1)
        widened_end = legacy._parse(end) + dt.timedelta(days=1)
        return self.period_entries(_utc(widened_start), _utc(widened_end))

    def entry_by_id(self, entry_id: str) -> Any:
        return legacy._request(
            f"/workspaces/{self.workspace}/time-entries/{urllib.parse.quote(entry_id, safe='')}",
            self.api_key, timeout_seconds=self.timeout_seconds,
        )

    def create(self, payload: Mapping[str, Any]) -> Any:
        try:
            return legacy._request(f"/workspaces/{self.workspace}/time-entries", self.api_key,
                                   method="POST", payload=payload,
                                   timeout_seconds=self.timeout_seconds)
        except legacy.PortfolioPostError as error:
            raise AmbiguousCreate(str(error)) from error

    def bind_target(self, workspace: str, member: str) -> None:
        self.workspace = workspace
        self.member = member


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _gateway(routing: Mapping[str, Any]) -> tuple[ClockifyGateway, str, str, str]:
    environment = load_env_file(clockify_env_candidates(), ["CLOCKIFY_API_KEY", "CLOCKIFY_WORKSPACE_ID"])
    if environment.get("_missing"):
        raise NativePostError("Clockify credentials are unavailable")
    workspace = str(environment["CLOCKIFY_WORKSPACE_ID"])
    member = str(routing.get("clockify_user_id") or "")
    gateway = ClockifyGateway(str(environment["CLOCKIFY_API_KEY"]), legacy.post_http_timeout_seconds(environment))
    gateway.bind_target(workspace, member)
    gateway.verify_target(workspace, member)
    return gateway, workspace, member, str(environment["CLOCKIFY_API_KEY"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan_parser = commands.add_parser("plan")
    plan_parser.add_argument("--sheet-capture", type=Path, required=True)
    plan_parser.add_argument("--expected-capture-sha256", required=True)
    plan_parser.add_argument("--routing", type=Path, required=True)
    plan_parser.add_argument("--source-adoptions", type=Path,
                             help="optional explicit audited source/confirmed-POST adoption snapshot")
    plan_parser.add_argument("--timezone", default="Europe/Bucharest")
    plan_parser.add_argument("--output", type=Path, required=True)
    approval_parser = commands.add_parser("approval-template")
    approval_parser.add_argument("--plan", type=Path, required=True)
    approval_parser.add_argument("--approval-id", required=True)
    approval_parser.add_argument("--approver", required=True)
    approval_parser.add_argument("--approved-at", required=True)
    approval_parser.add_argument("--expires-at", required=True)
    approval_parser.add_argument("--output", type=Path, required=True)
    execute_parser = commands.add_parser("execute")
    execute_parser.add_argument("--plan", type=Path, required=True)
    execute_parser.add_argument("--approval-receipt", type=Path, required=True)
    execute_parser.add_argument("--events", type=Path, required=True)
    execute_parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            capture_bytes = args.sheet_capture.read_bytes()
            actual = hashlib.sha256(capture_bytes).hexdigest()
            if actual != args.expected_capture_sha256:
                raise NativePostError("native Sheet capture digest differs from expected")
            document = json.loads(capture_bytes)
            routing_bytes = args.routing.read_bytes()
            routing = json.loads(routing_bytes)
            gateway, workspace, member, api_key = _gateway(routing)
            projects = legacy._paged(f"/workspaces/{workspace}/projects?archived=false", api_key,
                                     timeout_seconds=gateway.timeout_seconds)
            tags = legacy._paged(f"/workspaces/{workspace}/tags", api_key,
                                 timeout_seconds=gateway.timeout_seconds)
            _, _, rows = _sheet_rows(document)
            starts = [_parse_sheet_time(row["Start"], args.timezone) for _, row in rows]
            ends = [_parse_sheet_time(row["End"], args.timezone) for _, row in rows]
            live_entries = gateway.period_entries(_utc(min(starts)), _utc(max(ends)))
            plan = build_plan(document, capture_sha256=actual, routing=routing,
                              routing_sha256=hashlib.sha256(routing_bytes).hexdigest(),
                              timezone=args.timezone, workspace_id=workspace, member_id=member,
                              projects=projects, tags=tags, live_entries=live_entries,
                              source_adoptions=_load(args.source_adoptions) if args.source_adoptions else None)
            _atomic_write(args.output, plan)
            result = {"status": "planned", "row_count": plan["row_count"],
                      "total_minutes": plan["total_minutes"], "plan_digest": plan["plan_digest"]}
        elif args.command == "approval-template":
            plan = _load(args.plan)
            approval = approval_template(plan, approval_id=args.approval_id,
                                         approver=args.approver, approved_at=args.approved_at,
                                         expires_at=args.expires_at)
            _atomic_write(args.output, approval)
            result = {"status": "approval_recorded", "approval_id": args.approval_id,
                      "plan_digest": plan["plan_digest"]}
        else:
            plan = _load(args.plan)
            routing = {"clockify_user_id": plan.get("member_id")}
            gateway, workspace, member, _ = _gateway(routing)
            if (workspace, member) != (plan.get("workspace_id"), plan.get("member_id")):
                raise NativePostError("runtime Clockify target differs from approved plan")
            result = execute_plan(plan, _load(args.approval_receipt), args.events, args.receipt,
                                  gateway, now=dt.datetime.now(dt.timezone.utc))
    except (OSError, json.JSONDecodeError, NativePostError, legacy.PortfolioPostError) as error:
        print(f"clockify native Sheet post: {error}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
