#!/usr/bin/env python3
"""Post one explicitly approved portfolio to Clockify, resumably and safely.

Dry-run is the default. Execution requires a passing portfolio audit and the
caller-supplied SHA-256 of the exact approved portfolio. Interrupted runs are
safe to resume: live exact matches are skipped, while any non-identical live
overlap blocks the run before another entry is created.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Iterable, Mapping
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.clockify_sync_collect import clockify_env_candidates, load_env_file
from scripts import clockify_portfolio_replay as portfolio_replay


API = "https://api.clockify.me/api/v1"
SCHEMA_VERSION = "clockify-approved-portfolio-post/v1"
POST_HTTP_TIMEOUT_NAME = "CLOCKIFY_POST_HTTP_TIMEOUT_SECONDS"
POST_HTTP_TIMEOUT_DEFAULT_SECONDS = 45
POST_HTTP_TIMEOUT_MIN_SECONDS = 5
POST_HTTP_TIMEOUT_MAX_SECONDS = 120


class PortfolioPostError(ValueError):
    pass


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
    segments = sorted((dict(item) for item in raw), key=lambda item: str(item.get("start")))
    merged: list[dict[str, Any]] = []
    for segment in segments:
        start = str(segment.get("start") or "")
        end = str(segment.get("end") or "")
        minutes = int(segment.get("duration_minutes") or 0)
        if not start or not end or _parse(end) <= _parse(start):
            raise PortfolioPostError("portfolio contains an invalid allocation segment")
        if minutes != int((_parse(end) - _parse(start)).total_seconds() // 60):
            raise PortfolioPostError("portfolio segment duration is inconsistent")
        if merged and merged[-1]["end"] == start:
            merged[-1]["end"] = end
            merged[-1]["duration_minutes"] += minutes
        else:
            merged.append({"start": start, "end": end, "duration_minutes": minutes})
    if sum(item["duration_minutes"] for item in merged) != int(row.get("duration_minutes") or 0):
        raise PortfolioPostError("portfolio row minutes do not match its allocation segments")
    return merged


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
        for index, segment in enumerate(_merged_segments(row), 1):
            plans.append({
                "review_id": str(row.get("review_id") or ""),
                "segment_index": index,
                "start": _utc(segment["start"]),
                "end": _utc(segment["end"]),
                "duration_minutes": segment["duration_minutes"],
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
    approved blocks, but any adjustment of 60 seconds or more remains a hard
    conflict.
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


def _receipt_item(plan: Mapping[str, Any], entry_id: str, disposition: str) -> dict[str, Any]:
    return {
        "review_id": plan["review_id"],
        "segment_index": plan["segment_index"],
        "clockify_entry_id": entry_id,
        "start": plan["start"],
        "end": plan["end"],
        "duration_minutes": plan["duration_minutes"],
        "project_name": plan["project_name"],
        "description_digest": hashlib.sha256(plan["description"].encode("utf-8")).hexdigest(),
        "disposition": disposition,
    }


def _apply_prior_receipt(
    plans: list[dict[str, Any]],
    path: Path,
    portfolio_sha: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    prior = _read(path)
    if not isinstance(prior, Mapping) or prior.get("portfolio_sha256") != portfolio_sha:
        raise PortfolioPostError("prior posting receipt does not match the approved portfolio")
    items = [
        item
        for name in ("created", "already_existing")
        for item in prior.get(name, [])
        if isinstance(item, Mapping)
    ]
    by_key = {
        (str(item.get("review_id")), int(item.get("segment_index") or 0)): item
        for item in items
    }
    restored: list[dict[str, Any]] = []
    for original in plans:
        plan = dict(original)
        item = by_key.get((plan["review_id"], plan["segment_index"]))
        if item is not None:
            plan["start"] = str(item.get("start") or "")
            plan["end"] = str(item.get("end") or "")
            if not plan["start"] or not plan["end"]:
                raise PortfolioPostError("prior posting receipt contains an invalid window")
        restored.append(plan)
    return restored, [
        dict(item) for item in prior.get("boundary_adjustments", [])
        if isinstance(item, Mapping)
    ]


def _verify_approved_artifacts(
    portfolio: Mapping[str, Any],
    quality: Mapping[str, Any],
    replay: Mapping[str, Any],
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
    if any(row.get("validation_status") != "flash_validated" for row in activities):
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


def run(args: argparse.Namespace) -> dict[str, Any]:
    portfolio_path = args.portfolio.resolve()
    quality_path = args.quality_report.resolve()
    portfolio_sha = _sha256(portfolio_path)
    if portfolio_sha != args.expected_portfolio_sha256:
        raise PortfolioPostError("approved portfolio digest does not match")
    quality = _read(quality_path)
    portfolio = _read(portfolio_path)
    replay = _read(args.replay_integrity.resolve())
    if not all(isinstance(value, Mapping) for value in (portfolio, quality, replay)):
        raise PortfolioPostError("approved posting artifacts are invalid")
    _verify_approved_artifacts(portfolio, quality, replay)
    environment = load_env_file(
        clockify_env_candidates(), ["CLOCKIFY_API_KEY", "CLOCKIFY_WORKSPACE_ID"]
    )
    if environment.get("_missing"):
        raise PortfolioPostError("Clockify credentials are unavailable")
    timeout_seconds = post_http_timeout_seconds(environment)
    api_key = str(environment["CLOCKIFY_API_KEY"])
    workspace = str(environment["CLOCKIFY_WORKSPACE_ID"])
    routing = _read(args.routing.resolve())
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
    plans = _plans(portfolio, _resolved_routes(routing, project_ids, tag_ids))
    prior_path = args.prior_receipt
    if prior_path is None and args.receipt.exists():
        prior_path = args.receipt
    prior_adjustments: list[dict[str, Any]] | None = None
    if prior_path is not None:
        plans, prior_adjustments = _apply_prior_receipt(
            plans, prior_path.resolve(), portfolio_sha
        )
    live = _live_entries(
        workspace, user, api_key, plans, timeout_seconds=timeout_seconds
    )
    exact: dict[tuple[str, int], dict[str, Any]] = {}
    for plan in plans:
        matches = [entry for entry in live if _exact(plan, entry)]
        if len(matches) > 1:
            raise PortfolioPostError("multiple exact Clockify entries match one approved block")
        if matches:
            exact[(plan["review_id"], plan["segment_index"])] = matches[0]
    if prior_adjustments is None:
        plans, boundary_adjustments = _align_subminute_boundaries(plans, live, set(exact))
    else:
        boundary_adjustments = prior_adjustments
    for plan in plans:
        key = (plan["review_id"], plan["segment_index"])
        if key in exact:
            continue
        matches = [entry for entry in live if _exact(plan, entry)]
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
        overlaps = [entry for entry in live if _overlaps(plan, entry)]
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

    receipt = {
        "schema_version": SCHEMA_VERSION,
        "status": "dry_run" if not args.execute else "running",
        "portfolio": str(portfolio_path),
        "portfolio_sha256": portfolio_sha,
        "quality_sha256": _sha256(quality_path),
        "replay_sha256": _sha256(args.replay_integrity.resolve()),
        "review_rows": len(portfolio["activities"]),
        "planned_blocks": len(plans),
        "planned_minutes": sum(plan["duration_minutes"] for plan in plans),
        "boundary_adjustments": boundary_adjustments,
        "created": [],
        "already_existing": [],
    }
    for plan in plans:
        key = (plan["review_id"], plan["segment_index"])
        if key in exact:
            receipt["already_existing"].append(
                _receipt_item(plan, exact[key]["id"], "already_existing")
            )
    if not args.execute:
        _atomic_write(args.receipt.resolve(), receipt)
        return receipt

    for plan in plans:
        key = (plan["review_id"], plan["segment_index"])
        if key in exact:
            continue
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
            recovered = next((entry for entry in refreshed if _exact(plan, entry)), None)
            if recovered is None:
                receipt["status"] = "interrupted"
                _atomic_write(args.receipt.resolve(), receipt)
                raise
            receipt["created"].append(_receipt_item(plan, recovered["id"], "recovered"))
        else:
            if not isinstance(created, Mapping) or not created.get("id"):
                receipt["status"] = "interrupted"
                _atomic_write(args.receipt.resolve(), receipt)
                raise PortfolioPostError("Clockify create response lacks an entry ID")
            receipt["created"].append(_receipt_item(plan, str(created["id"]), "created"))
        _atomic_write(args.receipt.resolve(), receipt)

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
    parser.add_argument("--expected-portfolio-sha256", required=True)
    parser.add_argument("--execute", action="store_true")
    return parser.parse_args(argv)


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
