#!/usr/bin/env python3
"""Post one explicitly approved portfolio to Clockify, resumably and safely.

Dry-run is the default. Execution requires a passing portfolio audit and the
caller-supplied SHA-256 of the exact approved portfolio. Interrupted runs are
safe to resume: live exact matches are skipped, while any non-identical live
overlap blocks the run before another entry is created.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import AbstractSet, Any, Iterable, Mapping, Sequence
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.clockify_sync_collect import clockify_env_candidates, load_env_file
from scripts import clockify_portfolio_replay as portfolio_replay
from scripts import posting_receipts, reconciliation_manifest


API = "https://api.clockify.me/api/v1"
SCHEMA_VERSION = "clockify-approved-portfolio-post/v1"
BOUNDARY_ADJUSTMENT_ALGORITHM = "clockify-subminute-boundaries/v1"
POST_HTTP_TIMEOUT_NAME = "CLOCKIFY_POST_HTTP_TIMEOUT_SECONDS"
POST_HTTP_TIMEOUT_DEFAULT_SECONDS = 45
POST_HTTP_TIMEOUT_MIN_SECONDS = 5
POST_HTTP_TIMEOUT_MAX_SECONDS = 120


class PortfolioPostError(ValueError):
    pass


@dataclass(frozen=True)
class PriorReceiptCandidate:
    review_id: str
    segment_index: int
    clockify_entry_id: str
    disposition: str
    recorded_start: str | None
    recorded_end: str | None
    recorded_duration_seconds: int | None


def post_http_timeout_seconds(environment: Mapping[str, Any]) -> int:
    """Return the validated approval-gated Clockify request timeout."""
    raw = (
        environment[POST_HTTP_TIMEOUT_NAME]
        if POST_HTTP_TIMEOUT_NAME in environment
        else os.environ.get(POST_HTTP_TIMEOUT_NAME)
    )
    if raw is None:
        return POST_HTTP_TIMEOUT_DEFAULT_SECONDS
    text = str(raw)
    if not text.isascii() or not text.isdecimal():
        raise PortfolioPostError(
            f"{POST_HTTP_TIMEOUT_NAME} must be an integer from "
            f"{POST_HTTP_TIMEOUT_MIN_SECONDS} through {POST_HTTP_TIMEOUT_MAX_SECONDS}"
        )
    value = int(text)
    if not POST_HTTP_TIMEOUT_MIN_SECONDS <= value <= POST_HTTP_TIMEOUT_MAX_SECONDS:
        raise PortfolioPostError(
            f"{POST_HTTP_TIMEOUT_NAME} must be an integer from "
            f"{POST_HTTP_TIMEOUT_MIN_SECONDS} through {POST_HTTP_TIMEOUT_MAX_SECONDS}"
        )
    return value


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_write(path: Path, document: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _parse(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise PortfolioPostError("portfolio timestamps must contain an offset")
    return parsed.astimezone(dt.timezone.utc)


def _utc(value: str) -> str:
    return _parse(value).strftime("%Y-%m-%dT%H:%M:%SZ")


def _request(
    path: str,
    api_key: str,
    *,
    method: str = "GET",
    payload: Mapping[str, Any] | None = None,
    timeout_seconds: int = POST_HTTP_TIMEOUT_DEFAULT_SECONDS,
) -> Any:
    data = None
    headers = {"X-Api-Key": api_key}
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(API + path, data=data, headers=headers, method=method)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            try:
                if exc.code != 429 and not 500 <= exc.code < 600:
                    raise PortfolioPostError(f"Clockify HTTP {exc.code}") from exc
                if method != "GET" and 500 <= exc.code < 600:
                    raise PortfolioPostError(
                        f"Clockify HTTP {exc.code} requires write readback"
                    ) from exc
                if attempt == 3:
                    raise PortfolioPostError(
                        f"Clockify HTTP {exc.code} after retries"
                    ) from exc
            finally:
                exc.close()
        except (TimeoutError, urllib.error.URLError) as exc:
            if method != "GET" or attempt == 3:
                raise PortfolioPostError("Clockify transport failed") from exc
        time.sleep(2**attempt)
    raise AssertionError("unreachable retry state")


def _paged(path: str, api_key: str, *, timeout_seconds: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    separator = "&" if "?" in path else "?"
    for page in range(1, 101):
        value = _request(
            f"{path}{separator}page={page}&page-size=200",
            api_key,
            timeout_seconds=timeout_seconds,
        )
        if not isinstance(value, list):
            raise PortfolioPostError("Clockify list response is invalid")
        batch = [item for item in value if isinstance(item, dict)]
        rows.extend(batch)
        if len(batch) < 200:
            return rows
    raise PortfolioPostError("Clockify pagination exceeded safety limit")


def _unique_by_suffix(rows: Iterable[Mapping[str, Any]], kind: str) -> dict[str, str]:
    values: dict[str, list[str]] = {}
    for row in rows:
        identifier = str(row.get("id") or "").strip()
        if identifier:
            for length in (6, 8):
                values.setdefault(identifier[-length:], []).append(identifier)
    return {
        suffix: ids[0]
        for suffix, ids in values.items()
        if len(set(ids)) == 1
    }


def _resolved_routes(
    routing: Mapping[str, Any],
    project_ids: Mapping[str, str],
    tag_ids: Mapping[str, str],
) -> dict[tuple[str, tuple[str, ...]], dict[str, Any]]:
    resolved: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for section in ("session_routes", "meeting_routes"):
        for route in routing.get(section, []):
            if not isinstance(route, Mapping) or not route.get("project_name"):
                continue
            project_name = str(route["project_name"])
            tag_names = tuple(str(value) for value in route.get("tag_names", []))
            project_suffix = str(route.get("project_suffix") or "")
            tag_suffixes = [str(value) for value in route.get("tag_suffixes", [])]
            if not project_suffix or len(tag_names) != len(tag_suffixes):
                continue
            if project_suffix not in project_ids:
                raise PortfolioPostError(f"Clockify project suffix is missing: {project_suffix}")
            missing_tags = [suffix for suffix in tag_suffixes if suffix not in tag_ids]
            if missing_tags:
                raise PortfolioPostError(f"Clockify tag suffixes are missing: {', '.join(missing_tags)}")
            value = {
                "project_id": project_ids[project_suffix],
                "tag_ids": sorted(tag_ids[suffix] for suffix in tag_suffixes),
                "billable": bool(route.get("billable", True)),
            }
            key = (project_name, tag_names)
            if key in resolved and resolved[key] != value:
                raise PortfolioPostError(f"routing is ambiguous for {project_name} / {', '.join(tag_names)}")
            resolved[key] = value
    return resolved


def _merged_segments(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = row.get("allocation_segments")
    if not isinstance(raw, list) or not raw:
        raise PortfolioPostError("portfolio row lacks allocation segments")
    segments: list[tuple[dt.datetime, dict[str, Any]]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise PortfolioPostError("portfolio contains an invalid allocation segment")
        segment = dict(item)
        start = str(segment.get("start") or "")
        end = str(segment.get("end") or "")
        try:
            start_time = _parse(start)
            end_time = _parse(end)
            int(segment.get("duration_minutes") or 0)
            int(segment.get("duration_seconds") or 0)
        except (TypeError, ValueError) as error:
            raise PortfolioPostError(
                "portfolio contains an invalid allocation segment"
            ) from error
        if end_time <= start_time:
            raise PortfolioPostError("portfolio contains an invalid allocation segment")
        segments.append((start_time, segment))
    merged: list[dict[str, Any]] = []
    exact_row = "duration_seconds" in row
    exact_segments: list[bool] = []
    for start_time, segment in sorted(segments, key=lambda item: item[0]):
        start = str(segment.get("start") or "")
        end = str(segment.get("end") or "")
        end_time = _parse(end)
        minutes = int(segment.get("duration_minutes") or 0)
        seconds = int(segment.get("duration_seconds") or 0)
        exact_segments.append("duration_seconds" in segment)
        actual_seconds = int((end_time - start_time).total_seconds())
        if minutes != actual_seconds // 60:
            raise PortfolioPostError("portfolio segment duration is inconsistent")
        if "duration_seconds" in segment and seconds != actual_seconds:
            raise PortfolioPostError("portfolio segment seconds are inconsistent")
        seconds = actual_seconds
        if merged and _parse(merged[-1]["end"]) == start_time:
            merged[-1]["end"] = end
            merged[-1]["duration_seconds"] += seconds
            merged[-1]["duration_minutes"] = merged[-1]["duration_seconds"] // 60
        else:
            merged.append({
                "start": start, "end": end, "duration_minutes": minutes,
                "duration_seconds": seconds,
            })
    if exact_row or any(exact_segments):
        if not exact_row or not all(exact_segments):
            raise PortfolioPostError("portfolio row mixes exact and legacy allocation segments")
        total_seconds = sum(item["duration_seconds"] for item in merged)
        if total_seconds != int(row.get("duration_seconds") or 0):
            raise PortfolioPostError("portfolio row seconds do not match its allocation segments")
        if int(row.get("duration_minutes") or 0) != total_seconds // 60:
            raise PortfolioPostError("portfolio row minutes are not derived from its exact seconds")
    elif sum(item["duration_minutes"] for item in merged) != int(row.get("duration_minutes") or 0):
        raise PortfolioPostError("portfolio row minutes do not match its allocation segments")
    return merged


def _recompute_duration_fields(plan: dict[str, Any]) -> None:
    try:
        start = _parse(str(plan.get("start") or ""))
        end = _parse(str(plan.get("end") or ""))
    except (TypeError, ValueError) as error:
        raise PortfolioPostError("portfolio contains an invalid posting window") from error
    seconds = int((end - start).total_seconds())
    if seconds <= 0:
        raise PortfolioPostError("adjusted portfolio block is not positive")
    plan["duration_seconds"] = seconds
    plan["duration_minutes"] = seconds // 60


def _verify_approved_duration_seconds(plans: Iterable[Mapping[str, Any]]) -> None:
    approved_seconds: dict[str, set[int]] = {}
    posted_seconds: dict[str, int] = {}
    for plan in plans:
        review_id = str(plan["review_id"])
        approved = int(plan.get("approved_duration_seconds") or 0)
        if approved > 0:
            approved_seconds.setdefault(review_id, set()).add(approved)
        posted_seconds[review_id] = posted_seconds.get(review_id, 0) + int(
            plan["duration_seconds"]
        )
    for review_id, expected in approved_seconds.items():
        if len(expected) != 1 or posted_seconds.get(review_id) != next(iter(expected)):
            raise PortfolioPostError(
                "adjusted portfolio review seconds do not match approved duration"
            )


def _plans(
    portfolio: Mapping[str, Any],
    routes: Mapping[tuple[str, tuple[str, ...]], Mapping[str, Any]],
) -> list[dict[str, Any]]:
    activities = portfolio.get("activities")
    if not isinstance(activities, list) or not activities:
        raise PortfolioPostError("portfolio activities are missing")
    plans: list[dict[str, Any]] = []
    for row in activities:
        if not isinstance(row, Mapping):
            raise PortfolioPostError("portfolio activity is invalid")
        project_name = str(row.get("client_project") or "").strip()
        tag_names = [str(value) for value in row.get("tag_names", [])]
        route = routes.get((project_name, tuple(tag_names)))
        if route is None:
            raise PortfolioPostError(f"Clockify route is missing: {project_name} / {', '.join(tag_names)}")
        segments = _merged_segments(row)
        approved_duration_seconds = sum(
            segment["duration_seconds"] for segment in segments
        )
        for index, segment in enumerate(segments, 1):
            plans.append({
                "review_id": str(row.get("review_id") or ""),
                "segment_index": index,
                "start": _utc(segment["start"]),
                "end": _utc(segment["end"]),
                "duration_minutes": segment["duration_minutes"],
                "duration_seconds": segment["duration_seconds"],
                "approved_duration_seconds": approved_duration_seconds,
                "project_name": project_name,
                "project_id": route["project_id"],
                "tag_names": tag_names,
                "tag_ids": route["tag_ids"],
                "description": str(row.get("description") or "").strip(),
                "billable": route["billable"],
            })
    ordered = sorted(plans, key=lambda item: (item["start"], item["end"], item["review_id"]))
    for prior, current in zip(ordered, ordered[1:]):
        if _parse(current["start"]) < _parse(prior["end"]):
            raise PortfolioPostError("approved portfolio blocks overlap")
    return ordered


def _live_entry(entry: Mapping[str, Any]) -> dict[str, Any] | None:
    interval = entry.get("timeInterval")
    if not isinstance(interval, Mapping) or not interval.get("start") or not interval.get("end"):
        return None
    return {
        "id": str(entry.get("id") or ""),
        "start": _utc(str(interval["start"])),
        "end": _utc(str(interval["end"])),
        "project_id": str(entry.get("projectId") or ""),
        "tag_ids": sorted(str(value) for value in entry.get("tagIds", [])),
        "description": str(entry.get("description") or "").strip(),
    }


def _exact(plan: Mapping[str, Any], live: Mapping[str, Any]) -> bool:
    return all(plan[key] == live[key] for key in ("start", "end", "project_id", "tag_ids", "description"))


def _overlaps(plan: Mapping[str, Any], live: Mapping[str, Any]) -> bool:
    return _parse(plan["start"]) < _parse(live["end"]) and _parse(live["start"]) < _parse(plan["end"])


def _align_subminute_boundaries(
    plans: list[dict[str, Any]],
    live: list[dict[str, Any]],
    exact_keys: set[tuple[str, int]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Shift minute-rounded starts past live second-level endings.

    Durations remain exact. A shift may cascade through directly contiguous
    approved blocks. Individual boundary shifts and trims remain sub-minute,
    while their restoration within one approved review row may total 60 seconds
    or more.
    """
    adjusted: list[dict[str, Any]] = []
    original_times = {
        (plan["review_id"], plan["segment_index"]): (plan["start"], plan["end"])
        for plan in plans
    }
    previous: dict[str, Any] | None = None
    for original in plans:
        key = (original["review_id"], original["segment_index"])
        plan = dict(original)
        if key in exact_keys:
            previous = plan
            adjusted.append(plan)
            continue
        for _attempt in range(8):
            blockers = [entry for entry in live if _overlaps(plan, entry)]
            if previous is not None and _overlaps(plan, previous):
                blockers.append(previous)
            if not blockers:
                break
            blocker_end = max(_parse(entry["end"]) for entry in blockers)
            start = _parse(plan["start"])
            shift = int((blocker_end - start).total_seconds())
            if not 0 < shift < 60 or blocker_end.strftime("%Y-%m-%dT%H:%M") != start.strftime("%Y-%m-%dT%H:%M"):
                break
            end = _parse(plan["end"])
            plan["start"] = blocker_end.strftime("%Y-%m-%dT%H:%M:%SZ")
            plan["end"] = (end + dt.timedelta(seconds=shift)).strftime("%Y-%m-%dT%H:%M:%SZ")
        adjusted.append(plan)
        previous = plan

    # A sub-minute start shift can consume a following fixed Clockify block.
    # Trim only those shifted seconds, then restore them in a later free block
    # of the same approved review row so row-level minutes remain exact.
    deficits: dict[str, int] = {}
    for plan in adjusted:
        key = (plan["review_id"], plan["segment_index"])
        original_start, original_end = original_times[key]
        if plan["start"] == original_start:
            continue
        end_blockers = [
            entry for entry in live
            if _overlaps(plan, entry) and _parse(entry["start"]) >= _parse(original_end)
        ]
        if not end_blockers:
            continue
        boundary = min(_parse(entry["start"]) for entry in end_blockers)
        lost = int((_parse(plan["end"]) - boundary).total_seconds())
        if not 0 < lost < 60:
            continue
        plan["end"] = boundary.strftime("%Y-%m-%dT%H:%M:%SZ")
        deficits[plan["review_id"]] = deficits.get(plan["review_id"], 0) + lost

    for review_id, seconds in deficits.items():
        restored = False
        for index in range(len(adjusted) - 1, -1, -1):
            plan = adjusted[index]
            key = (plan["review_id"], plan["segment_index"])
            if plan["review_id"] != review_id or key in exact_keys:
                continue
            candidate = dict(plan)
            candidate["end"] = (
                _parse(plan["end"]) + dt.timedelta(seconds=seconds)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            if any(_overlaps(candidate, entry) for entry in live):
                continue
            if index + 1 < len(adjusted) and _overlaps(candidate, adjusted[index + 1]):
                continue
            plan["end"] = candidate["end"]
            restored = True
            break
        if not restored:
            raise PortfolioPostError(
                f"cannot preserve approved minutes after sub-minute boundary trim: {review_id}"
            )

    for plan in adjusted:
        key = (plan["review_id"], plan["segment_index"])
        if key in exact_keys:
            continue
        _recompute_duration_fields(plan)
    _verify_approved_duration_seconds(adjusted)

    changes: list[dict[str, Any]] = []
    for plan in adjusted:
        key = (plan["review_id"], plan["segment_index"])
        original_start, original_end = original_times[key]
        if plan["start"] != original_start or plan["end"] != original_end:
            changes.append({
                "review_id": plan["review_id"],
                "segment_index": plan["segment_index"],
                "original_start": original_start,
                "original_end": original_end,
                "posted_start": plan["start"],
                "posted_end": plan["end"],
                "algorithm": BOUNDARY_ADJUSTMENT_ALGORITHM,
            })
    return adjusted, changes


def _live_entries(
    workspace: str,
    user: str,
    api_key: str,
    plans: Iterable[Mapping[str, Any]],
    *,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    windows = [(str(plan["start"]), str(plan["end"])) for plan in plans]
    if not windows:
        raise PortfolioPostError("approved portfolio contains no posting windows")
    start = min(_parse(value) for value, _end in windows) - dt.timedelta(days=1)
    end = max(_parse(value) for _start, value in windows) + dt.timedelta(days=1)
    query = urllib.parse.urlencode({
        "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    })
    rows = _paged(
        f"/workspaces/{workspace}/user/{user}/time-entries?{query}",
        api_key,
        timeout_seconds=timeout_seconds,
    )
    return [value for row in rows if (value := _live_entry(row)) is not None]


def _period_live_entries(
    workspace: str,
    user: str,
    api_key: str,
    *,
    period_start: str,
    period_end: str,
    timeout_seconds: int,
) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({
        "start": _utc(period_start),
        "end": _utc(period_end),
    })
    rows = _paged(
        f"/workspaces/{workspace}/user/{user}/time-entries?{query}",
        api_key,
        timeout_seconds=timeout_seconds,
    )
    return [value for row in rows if (value := _live_entry(row)) is not None]


def _receipt_item(plan: Mapping[str, Any], entry_id: str, disposition: str) -> dict[str, Any]:
    return {
        "review_id": plan["review_id"],
        "segment_index": plan["segment_index"],
        "clockify_entry_id": entry_id,
        "start": plan["start"],
        "end": plan["end"],
        "duration_minutes": plan["duration_minutes"],
        "duration_seconds": plan["duration_seconds"],
        "project_name": plan["project_name"],
        "description_digest": hashlib.sha256(plan["description"].encode("utf-8")).hexdigest(),
        "disposition": disposition,
    }


def _receipt_key(item: Mapping[str, Any], *, kind: str) -> tuple[str, int]:
    review_id = str(item.get("review_id") or "")
    try:
        segment_index = int(item.get("segment_index") or 0)
    except (TypeError, ValueError) as error:
        raise PortfolioPostError(f"prior posting receipt contains an invalid {kind} key") from error
    if not review_id or segment_index <= 0:
        raise PortfolioPostError(f"prior posting receipt contains an invalid {kind} key")
    return review_id, segment_index


def _same_instant(left: Any, right: Any) -> bool:
    try:
        return _parse(str(left)) == _parse(str(right))
    except (TypeError, ValueError, PortfolioPostError):
        return False


def _prior_receipt_candidates(
    path: Path,
    portfolio_sha: str,
    approved_keys: AbstractSet[tuple[str, int]],
) -> Sequence[PriorReceiptCandidate]:
    prior = _read(path)
    if not isinstance(prior, Mapping) or prior.get("portfolio_sha256") != portfolio_sha:
        raise PortfolioPostError("prior posting receipt does not match the approved portfolio")
    candidates: list[PriorReceiptCandidate] = []
    seen_keys: set[tuple[str, int]] = set()
    seen_ids: set[str] = set()
    for disposition in ("created", "already_existing"):
        items = prior.get(disposition, [])
        if not isinstance(items, list):
            raise PortfolioPostError("prior posting receipt items must be a list")
        for item in items:
            if not isinstance(item, Mapping):
                raise PortfolioPostError("prior posting receipt contains an invalid item")
            key = _receipt_key(item, kind="receipt")
            if key in seen_keys:
                raise PortfolioPostError("prior posting receipt contains a duplicate receipt key")
            if key not in approved_keys:
                raise PortfolioPostError("prior posting receipt contains an unknown approved key")
            entry_id = str(item.get("clockify_entry_id") or "").strip()
            if not entry_id:
                raise PortfolioPostError("prior posting receipt lacks a Clockify entry ID")
            if entry_id in seen_ids:
                raise PortfolioPostError(
                    "prior posting receipt contains a duplicate Clockify entry ID"
                )
            try:
                recorded_start = _utc(str(item["start"])) if "start" in item else None
                recorded_end = _utc(str(item["end"])) if "end" in item else None
            except (TypeError, ValueError, PortfolioPostError) as error:
                raise PortfolioPostError(
                    "prior posting receipt contains an invalid audit timestamp"
                ) from error
            raw_duration = item.get("duration_seconds")
            if raw_duration is not None and (
                isinstance(raw_duration, bool)
                or not isinstance(raw_duration, int)
                or raw_duration <= 0
            ):
                raise PortfolioPostError("prior posting receipt contains invalid duration seconds")
            candidates.append(PriorReceiptCandidate(
                key[0], key[1], entry_id, disposition,
                recorded_start, recorded_end, raw_duration,
            ))
            seen_keys.add(key)
            seen_ids.add(entry_id)
    return tuple(candidates)


def _resolve_prior_candidates(
    candidates: Iterable[PriorReceiptCandidate],
    live: Iterable[Mapping[str, Any]],
    approved_by_key: Mapping[tuple[str, int], Mapping[str, Any]],
) -> tuple[dict[tuple[str, int], dict[str, Any]], list[dict[str, Any]]]:
    live_rows = [dict(entry) for entry in live]
    live_by_id: dict[str, dict[str, Any]] = {}
    for entry in live_rows:
        entry_id = str(entry.get("id") or "").strip()
        if not entry_id:
            raise PortfolioPostError("fresh Clockify readback contains an empty entry ID")
        if entry_id in live_by_id:
            raise PortfolioPostError("fresh Clockify readback contains a duplicate entry ID")
        live_by_id[entry_id] = entry
    resolved: dict[tuple[str, int], dict[str, Any]] = {}
    removed_ids: set[str] = set()
    for candidate in candidates:
        key = (candidate.review_id, candidate.segment_index)
        if key in resolved:
            raise PortfolioPostError("prior posting receipt contains a duplicate receipt key")
        entry = live_by_id.get(candidate.clockify_entry_id)
        if entry is None:
            raise PortfolioPostError("prior Clockify entry is absent from fresh readback")
        approved = approved_by_key[key]
        semantic_match = (
            str(entry.get("project_id") or "") == str(approved.get("project_id") or "")
            and sorted(str(value) for value in entry.get("tag_ids", []))
            == sorted(str(value) for value in approved.get("tag_ids", []))
            and str(entry.get("description") or "").strip()
            == str(approved.get("description") or "").strip()
        )
        if not semantic_match:
            raise PortfolioPostError("prior Clockify entry semantic fields differ from approval")
        resolved[key] = entry
        removed_ids.add(candidate.clockify_entry_id)
    return resolved, [entry for entry in live_rows if entry["id"] not in removed_ids]


def _validate_prior_candidates(
    candidates_by_key: Mapping[tuple[str, int], Mapping[str, Any]],
    derived_by_key: Mapping[tuple[str, int], Mapping[str, Any]],
    receipt_candidates: Mapping[tuple[str, int], PriorReceiptCandidate],
) -> dict[tuple[str, int], dict[str, Any]]:
    accepted: dict[tuple[str, int], dict[str, Any]] = {}
    if set(candidates_by_key) != set(receipt_candidates):
        raise PortfolioPostError("prior candidate identity sets do not match")
    for key, receipt_candidate in receipt_candidates.items():
        live = candidates_by_key[key]
        derived = derived_by_key.get(key)
        if derived is None:
            raise PortfolioPostError("prior candidate has no freshly derived plan")
        if not _exact(derived, live):
            raise PortfolioPostError(
                "prior Clockify entry differs from its freshly derived plan"
            )
        live_seconds = int(
            (_parse(str(live["end"])) - _parse(str(live["start"]))).total_seconds()
        )
        if live_seconds <= 0 or live_seconds != int(derived["duration_seconds"]):
            raise PortfolioPostError("prior Clockify entry duration differs from derivation")
        if (
            receipt_candidate.recorded_start is not None
            and not _same_instant(receipt_candidate.recorded_start, live["start"])
        ) or (
            receipt_candidate.recorded_end is not None
            and not _same_instant(receipt_candidate.recorded_end, live["end"])
        ):
            raise PortfolioPostError("prior receipt audit bounds contradict fresh readback")
        if (
            receipt_candidate.recorded_duration_seconds is not None
            and receipt_candidate.recorded_duration_seconds != live_seconds
        ):
            raise PortfolioPostError("prior receipt audit duration contradicts fresh readback")
        accepted[key] = dict(live)
    return accepted


def _normalized_snapshot_sha256(entries: Iterable[Mapping[str, Any]]) -> str:
    """Return a stable digest for the live fields that affect derivation."""
    rows = [
        {
            "id": str(entry.get("id") or ""),
            "start": _utc(str(entry.get("start") or "")),
            "end": _utc(str(entry.get("end") or "")),
            "project_id": str(entry.get("project_id") or ""),
            "tag_ids": sorted(str(value) for value in entry.get("tag_ids", [])),
            "description": str(entry.get("description") or "").strip(),
        }
        for entry in entries
    ]
    rows.sort(key=lambda row: (
        row["start"], row["end"], row["id"], row["project_id"],
        tuple(row["tag_ids"]), row["description"],
    ))
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _adjustment_digest(
    portfolio_sha: str,
    blocker_snapshot_sha256: str,
    adjustments: Iterable[Mapping[str, Any]],
) -> str:
    payload = {
        "portfolio_sha256": portfolio_sha,
        "algorithm": BOUNDARY_ADJUSTMENT_ALGORITHM,
        "blocker_snapshot_sha256": blocker_snapshot_sha256,
        "adjustments": [
            {
                "review_id": str(item.get("review_id") or ""),
                "segment_index": int(item.get("segment_index") or 0),
                "original_start": str(item.get("original_start") or ""),
                "original_end": str(item.get("original_end") or ""),
                "posted_start": str(item.get("posted_start") or ""),
                "posted_end": str(item.get("posted_end") or ""),
                "algorithm": str(item.get("algorithm") or ""),
            }
            for item in sorted(
                adjustments,
                key=lambda item: _receipt_key(item, kind="boundary adjustment"),
            )
        ],
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _verify_approved_artifacts(
    portfolio: Mapping[str, Any],
    quality: Mapping[str, Any],
    replay: Mapping[str, Any],
    *,
    require_flash_validation: bool = True,
) -> None:
    """Require one clean, Flash-validated, replay-bound posting package."""
    activities = portfolio.get("activities")
    repair = portfolio.get("repair")
    if portfolio.get("external_writes") is not False:
        raise PortfolioPostError("portfolio safety contract is invalid")
    if not isinstance(activities, list) or not all(
        isinstance(row, Mapping) for row in activities
    ):
        raise PortfolioPostError("portfolio activities are invalid")
    if (
        not isinstance(repair, Mapping)
        or repair.get("status") not in {"complete", "pass"}
        or repair.get("unresolved_wording") != []
    ):
        raise PortfolioPostError("portfolio repair has not cleanly completed")
    if require_flash_validation and any(
        row.get("validation_status") != "flash_validated" for row in activities
    ):
        raise PortfolioPostError(
            "portfolio activity lacks successful Flash portfolio validation"
        )
    if quality.get("status") != "pass":
        raise PortfolioPostError("portfolio quality report is not passing")
    identity = replay.get("identity")
    artifacts = identity.get("artifacts") if isinstance(identity, Mapping) else None
    if replay.get("status") != "pass" or not isinstance(artifacts, Mapping):
        raise PortfolioPostError("portfolio immutable replay is not passing")
    if artifacts.get("repair") != portfolio_replay._digest(portfolio):
        raise PortfolioPostError("portfolio replay is not bound to the repair")
    if artifacts.get("quality") != portfolio_replay._digest(quality):
        raise PortfolioPostError("portfolio replay is not bound to the quality report")


def _approval_digest(path: Path) -> str:
    return "sha256:" + _sha256(path)


def _approval_context(
    args: argparse.Namespace,
    *,
    portfolio_path: Path,
    quality_path: Path,
    replay_path: Path,
    routing_path: Path,
    routing: Mapping[str, Any],
) -> tuple[posting_receipts.ApprovalReceipt, posting_receipts.PostEventStore]:
    if not all(getattr(args, name, None) for name in (
        "approval_receipt", "approval_events", "post_events", "period_manifest",
    )):
        raise PortfolioPostError("approval receipt, event ledgers, and period manifest are required for execution")
    try:
        manifest = reconciliation_manifest.ReconciliationManifest.from_document(
            _read(Path(args.period_manifest).resolve())
        )
        reconciliation_manifest._verify_artifact_refs(list(manifest.artifacts))
        identity = manifest.identity
        member_id = str(routing.get("clockify_user_id") or "")
        if not member_id or member_id != identity.member_id:
            raise PortfolioPostError("routing does not match the period manifest member")
        operation_identity = posting_receipts.derive_operation_identity(
            operation="clockify_post", period_id=identity.period_id,
            workspace_id=identity.workspace_id, member_id=identity.member_id,
        )
        receipt = posting_receipts.ApprovalReceiptStore(
            Path(args.approval_events).resolve()
        ).require(
            str(args.approval_receipt), operation_identity=operation_identity,
            now=dt.datetime.now(dt.timezone.utc),
        )
        expected = {
            "portfolio_digest": _approval_digest(portfolio_path),
            "quality_digest": _approval_digest(quality_path),
            "replay_digest": _approval_digest(replay_path),
            "routing_digest": _approval_digest(routing_path),
        }
        if any(getattr(receipt, field) != value for field, value in expected.items()):
            raise PortfolioPostError("approval receipt artifact digest does not match execution inputs")
        period = identity.document()
        if (
            receipt.operation != "clockify_post"
            or receipt.period_id != identity.period_id
            or receipt.workspace_id != identity.workspace_id
            or receipt.member_id != identity.member_id
            or receipt.period_start != period["since_utc"]
            or receipt.period_end != period["until_utc"]
        ):
            raise PortfolioPostError("approval receipt target or period does not match period manifest")
        artifact_digests = {str(value.get("digest")) for value in manifest.artifacts}
        if any(getattr(receipt, field) not in artifact_digests for field in (
            "correction_log_digest", "coverage_digest", "residual_exception_digest",
        )):
            raise PortfolioPostError("approval receipt correction, coverage, or residual digest is absent from period manifest")
        store = posting_receipts.PostEventStore(Path(args.post_events).resolve())
        store.derive_receipt(operation_identity)
        return receipt, store
    except (OSError, json.JSONDecodeError, posting_receipts.PostingReceiptError,
            reconciliation_manifest.ManifestError) as error:
        raise PortfolioPostError(f"approval receipt validation failed: {error}") from error


def _append_post_event(
    store: posting_receipts.PostEventStore,
    approval: posting_receipts.ApprovalReceipt,
    plan: Mapping[str, Any],
    disposition: str,
    *,
    entry_id: str | None = None,
    live_readback_digest: str | None = None,
) -> None:
    try:
        store.append(posting_receipts.PostEvent(
            disposition=disposition, operation=approval.operation,
            operation_identity=approval.operation_identity, period_id=approval.period_id,
            workspace_id=approval.workspace_id, member_id=approval.member_id,
            review_id=str(plan["review_id"]), segment_index=int(plan["segment_index"]),
            recorded_at=dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            clockify_entry_id=entry_id, live_readback_digest=live_readback_digest,
        ))
    except posting_receipts.PostingReceiptError as error:
        raise PortfolioPostError(f"post event validation failed: {error}") from error


def run(args: argparse.Namespace) -> dict[str, Any]:
    if args.execute and not getattr(args, "approval_receipt", None):
        raise PortfolioPostError("approval receipt is required for execution")
    portfolio_path = args.portfolio.resolve()
    quality_path = args.quality_report.resolve()
    replay_path = args.replay_integrity.resolve()
    routing_path = args.routing.resolve()
    portfolio_sha = _sha256(portfolio_path)
    if portfolio_sha != args.expected_portfolio_sha256:
        raise PortfolioPostError("approved portfolio digest does not match")
    quality = _read(quality_path)
    portfolio = _read(portfolio_path)
    replay = _read(replay_path)
    routing = _read(routing_path)
    if not all(isinstance(value, Mapping) for value in (portfolio, quality, replay, routing)):
        raise PortfolioPostError("approved posting artifacts are invalid")
    approval: posting_receipts.ApprovalReceipt | None = None
    post_event_store: posting_receipts.PostEventStore | None = None
    if args.execute:
        approval, post_event_store = _approval_context(
            args, portfolio_path=portfolio_path, quality_path=quality_path,
            replay_path=replay_path, routing_path=routing_path, routing=routing,
        )
    _verify_approved_artifacts(
        portfolio, quality, replay, require_flash_validation=approval is None
    )
    environment = load_env_file(
        clockify_env_candidates(), ["CLOCKIFY_API_KEY", "CLOCKIFY_WORKSPACE_ID"]
    )
    if environment.get("_missing"):
        raise PortfolioPostError("Clockify credentials are unavailable")
    timeout_seconds = post_http_timeout_seconds(environment)
    api_key = str(environment["CLOCKIFY_API_KEY"])
    workspace = str(environment["CLOCKIFY_WORKSPACE_ID"])
    user = str(routing.get("clockify_user_id") or "")
    if not user:
        raise PortfolioPostError("Clockify user ID is missing from routing")

    project_ids = _unique_by_suffix(
        _paged(
            f"/workspaces/{workspace}/projects?archived=false",
            api_key,
            timeout_seconds=timeout_seconds,
        ),
        "project",
    )
    tag_ids = _unique_by_suffix(
        _paged(
            f"/workspaces/{workspace}/tags",
            api_key,
            timeout_seconds=timeout_seconds,
        ),
        "tag",
    )
    approved_plans = _plans(
        portfolio, _resolved_routes(routing, project_ids, tag_ids)
    )
    approved_by_key = {
        _receipt_key(plan, kind="approved plan"): plan for plan in approved_plans
    }
    if len(approved_by_key) != len(approved_plans):
        raise PortfolioPostError("approved portfolio contains duplicate posting keys")
    prior_path = None if args.execute else args.prior_receipt
    if not args.execute and prior_path is None and args.receipt.exists():
        prior_path = args.receipt
    live = _live_entries(
        workspace, user, api_key, approved_plans, timeout_seconds=timeout_seconds
    )
    live_snapshot_sha256 = _normalized_snapshot_sha256(live)
    candidates = (
        _prior_receipt_candidates(
            prior_path.resolve(), portfolio_sha, set(approved_by_key)
        )
        if prior_path is not None else ()
    )
    receipt_candidates = {
        (candidate.review_id, candidate.segment_index): candidate
        for candidate in candidates
    }
    candidate_live, blockers = _resolve_prior_candidates(
        candidates, live, approved_by_key
    )
    exact: dict[tuple[str, int], dict[str, Any]] = {}
    for plan in approved_plans:
        matches = [entry for entry in blockers if _exact(plan, entry)]
        if len(matches) > 1:
            raise PortfolioPostError("multiple exact Clockify entries match one approved block")
        if matches:
            exact[(plan["review_id"], plan["segment_index"])] = matches[0]
    blocker_snapshot_sha256 = _normalized_snapshot_sha256(blockers)
    plans, boundary_adjustments = _align_subminute_boundaries(
        [dict(plan) for plan in approved_plans], blockers, set(exact)
    )
    derived_by_key = {
        _receipt_key(plan, kind="freshly derived plan"): plan for plan in plans
    }
    if len(derived_by_key) != len(plans):
        raise PortfolioPostError("fresh derivation contains duplicate posting keys")
    validated_candidates = _validate_prior_candidates(
        candidate_live, derived_by_key, receipt_candidates
    )
    for key in validated_candidates:
        if key in exact:
            raise PortfolioPostError(
                "multiple exact Clockify entries match one derived block"
            )
    exact.update(validated_candidates)
    for plan in plans:
        key = (plan["review_id"], plan["segment_index"])
        if key in exact:
            continue
        matches = [entry for entry in blockers if _exact(plan, entry)]
        if len(matches) > 1:
            raise PortfolioPostError("multiple exact Clockify entries match one adjusted block")
        if matches:
            exact[key] = matches[0]
    for prior, current in zip(plans, plans[1:]):
        if _parse(current["start"]) < _parse(prior["end"]):
            raise PortfolioPostError("boundary-adjusted approved blocks overlap")
    conflicts: list[dict[str, str]] = []
    for plan in plans:
        key = (plan["review_id"], plan["segment_index"])
        if key in exact:
            continue
        overlaps = [entry for entry in blockers if _overlaps(plan, entry)]
        if overlaps:
            conflicts.append({
                "review_id": plan["review_id"],
                "clockify_entry_id": overlaps[0]["id"],
                "approved_window": f"{plan['start']}..{plan['end']}",
                "live_window": f"{overlaps[0]['start']}..{overlaps[0]['end']}",
                "approved_description": plan["description"],
                "live_description": overlaps[0]["description"],
            })
    if conflicts:
        detail = ", ".join(
            f"{item['review_id']} {item['approved_window']} [{item['approved_description']}] "
            f"overlaps {item['clockify_entry_id']} {item['live_window']} "
            f"[{item['live_description']}]"
            for item in conflicts
        )
        raise PortfolioPostError(
            f"{len(conflicts)} approved blocks conflict with live Clockify entries: {detail}"
        )

    post_history: dict[tuple[str, int], dict[str, Any]] = {}
    if args.execute:
        if approval is None or post_event_store is None:
            raise AssertionError("execution approval context is missing")
        try:
            post_history = {
                _receipt_key(entry, kind="post event"): dict(entry)
                for entry in post_event_store.derive_receipt(approval.operation_identity)["entries"]
            }
        except (KeyError, TypeError, posting_receipts.PostingReceiptError) as error:
            raise PortfolioPostError(f"post event history is invalid: {error}") from error
        if set(post_history) - set(approved_by_key):
            raise PortfolioPostError("post event history contains an unknown approved key")

    planned_seconds = sum(plan["duration_seconds"] for plan in plans)
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "status": "dry_run" if not args.execute else "running",
        "portfolio": str(portfolio_path),
        "portfolio_sha256": portfolio_sha,
        "quality_sha256": _sha256(quality_path),
        "replay_sha256": _sha256(args.replay_integrity.resolve()),
        "review_rows": len(portfolio["activities"]),
        "planned_blocks": len(plans),
        "planned_seconds": planned_seconds,
        "planned_minutes": planned_seconds // 60,
        "boundary_adjustment_algorithm": BOUNDARY_ADJUSTMENT_ALGORITHM,
        "live_snapshot_sha256": live_snapshot_sha256,
        "blocker_snapshot_sha256": blocker_snapshot_sha256,
        "boundary_adjustments": boundary_adjustments,
        "boundary_adjustments_sha256": _adjustment_digest(
            portfolio_sha, blocker_snapshot_sha256, boundary_adjustments
        ),
        "created": [],
        "already_existing": [],
    }
    for plan in plans:
        key = (plan["review_id"], plan["segment_index"])
        if key in exact:
            receipt["already_existing"].append(
                _receipt_item(plan, exact[key]["id"], "already_existing")
            )
            if args.execute:
                if approval is None or post_event_store is None:
                    raise AssertionError("execution approval context is missing")
                historical = post_history.get(key)
                if historical is None:
                    _append_post_event(post_event_store, approval, plan, "planned")
                elif historical.get("clockify_entry_id") not in {None, str(exact[key]["id"])}:
                    raise PortfolioPostError("post event Clockify entry does not match fresh readback")
                if historical is None or historical.get("clockify_entry_id") is None:
                    _append_post_event(
                        post_event_store, approval, plan, "already_existing",
                        entry_id=str(exact[key]["id"]),
                        live_readback_digest="sha256:" + live_snapshot_sha256,
                    )
    if not args.execute:
        _atomic_write(args.receipt.resolve(), receipt)
        return receipt

    for plan in plans:
        key = (plan["review_id"], plan["segment_index"])
        if key in exact:
            continue
        if key in post_history:
            raise PortfolioPostError(
                "planned post has no exact fresh Clockify readback; refusing to repeat POST"
            )
        if approval is None or post_event_store is None:
            raise AssertionError("execution approval context is missing")
        _append_post_event(post_event_store, approval, plan, "planned")
        payload = {
            "start": plan["start"],
            "end": plan["end"],
            "description": plan["description"],
            "projectId": plan["project_id"],
            "tagIds": plan["tag_ids"],
            "billable": plan["billable"],
        }
        try:
            created = _request(
                f"/workspaces/{workspace}/time-entries",
                api_key,
                method="POST",
                payload=payload,
                timeout_seconds=timeout_seconds,
            )
        except PortfolioPostError:
            refreshed = _live_entries(
                workspace, user, api_key, plans, timeout_seconds=timeout_seconds
            )
            recovered_matches = [entry for entry in refreshed if _exact(plan, entry)]
            if len(recovered_matches) != 1:
                receipt["status"] = "interrupted"
                _atomic_write(args.receipt.resolve(), receipt)
                if len(recovered_matches) > 1:
                    raise PortfolioPostError(
                        "multiple exact Clockify entries match ambiguous POST recovery"
                    )
                raise
            recovered = recovered_matches[0]
            readback_digest = "sha256:" + _normalized_snapshot_sha256(refreshed)
            _append_post_event(
                post_event_store, approval, plan, "recovered_after_ambiguous_response",
                entry_id=str(recovered["id"]), live_readback_digest=readback_digest,
            )
            receipt["created"].append(
                _receipt_item(plan, recovered["id"], "recovered_after_ambiguous_response")
            )
        else:
            if not isinstance(created, Mapping) or not created.get("id"):
                receipt["status"] = "interrupted"
                _atomic_write(args.receipt.resolve(), receipt)
                raise PortfolioPostError("Clockify create response lacks an entry ID")
            refreshed = _live_entries(
                workspace, user, api_key, plans, timeout_seconds=timeout_seconds
            )
            created_matches = [entry for entry in refreshed if _exact(plan, entry)]
            if len(created_matches) != 1 or str(created_matches[0]["id"]) != str(created["id"]):
                receipt["status"] = "interrupted"
                _atomic_write(args.receipt.resolve(), receipt)
                if len(created_matches) > 1:
                    raise PortfolioPostError(
                        "multiple exact Clockify entries match POST readback"
                    )
                raise PortfolioPostError("Clockify create response is absent from exact live readback")
            readback_digest = "sha256:" + _normalized_snapshot_sha256(refreshed)
            _append_post_event(
                post_event_store, approval, plan, "created",
                entry_id=str(created["id"]), live_readback_digest=readback_digest,
            )
            receipt["created"].append(_receipt_item(plan, str(created["id"]), "created"))

    if approval is None or post_event_store is None:
        raise AssertionError("execution approval context is missing")
    final_live = _period_live_entries(
        workspace, user, api_key, period_start=approval.period_start,
        period_end=approval.period_end, timeout_seconds=timeout_seconds,
    )
    final_ids = {str(entry["id"]) for entry in final_live}
    try:
        final_events = post_event_store.verify()
    except posting_receipts.PostingReceiptError as error:
        raise PortfolioPostError(f"post event history is incomplete: {error}") from error
    if any(event.disposition == "interrupted" for event in final_events):
        raise PortfolioPostError("post event history contains an interrupted entry")
    for event in final_events:
        if event.clockify_entry_id and event.clockify_entry_id not in final_ids:
            raise PortfolioPostError("post event Clockify entry is absent from final live readback")
    receipt["post_events"] = post_event_store.derive_receipt(approval.operation_identity)
    outcomes = {
        _receipt_key(entry, kind="post event"): entry
        for entry in receipt["post_events"]["entries"]
    }
    receipt["created"] = []
    receipt["already_existing"] = []
    for plan in plans:
        outcome = outcomes[(plan["review_id"], plan["segment_index"])]
        disposition = str(outcome["disposition"])
        entry_id = str(outcome.get("clockify_entry_id") or "")
        if disposition == "already_existing":
            receipt["already_existing"].append(_receipt_item(plan, entry_id, disposition))
        else:
            receipt["created"].append(_receipt_item(plan, entry_id, disposition))
    receipt["final_live_readback_sha256"] = "sha256:" + _normalized_snapshot_sha256(final_live)
    receipt["status"] = "complete"
    _atomic_write(args.receipt.resolve(), receipt)
    return receipt


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("portfolio", type=Path)
    parser.add_argument("--quality-report", type=Path, required=True)
    parser.add_argument("--replay-integrity", type=Path, required=True)
    parser.add_argument("--routing", type=Path, default=Path("routing.json"))
    parser.add_argument("--receipt", type=Path, required=True)
    parser.add_argument("--prior-receipt", type=Path)
    parser.add_argument("--approval-receipt")
    parser.add_argument("--approval-events", type=Path)
    parser.add_argument("--post-events", type=Path)
    parser.add_argument("--period-manifest", type=Path)
    parser.add_argument("--expected-portfolio-sha256", required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args(argv)
    if args.execute and not all((
        args.approval_receipt, args.approval_events, args.post_events, args.period_manifest,
    )):
        parser.error(
            "--execute requires --approval-receipt, --approval-events, --post-events, and --period-manifest"
        )
    return args


def main(argv: list[str] | None = None) -> int:
    try:
        result = run(parse_args(argv))
    except (OSError, json.JSONDecodeError, PortfolioPostError) as exc:
        print(f"clockify portfolio post: {exc}", file=os.sys.stderr)
        return 2
    print(json.dumps({
        "status": result["status"],
        "review_rows": result["review_rows"],
        "planned_blocks": result["planned_blocks"],
        "planned_minutes": result["planned_minutes"],
        "created": len(result["created"]),
        "already_existing": len(result["already_existing"]),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
