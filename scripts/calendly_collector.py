"""Pure Calendly recording normalization and credential-free CLI preflight."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping
import urllib.parse
import urllib.request

try:
    from scripts.collector_checkpoints import (
        CheckpointError,
        CheckpointIdentity,
        CheckpointState,
        PageCheckpointStore,
    )
except ModuleNotFoundError:  # Support direct execution from this directory.
    from collector_checkpoints import (  # type: ignore[no-redef]
        CheckpointError,
        CheckpointIdentity,
        CheckpointState,
        PageCheckpointStore,
    )


CALENDLY_COMPATIBILITY_VERSION = "calendly-recordings/v1"
CALENDLY_MAX_PAGES = 1000


class CalendlyCollectorError(ValueError):
    """Raised when a Calendly source record or collection interval is invalid."""


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _parse_required_utc(value: object) -> datetime:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value):
        raise CalendlyCollectorError("timestamp must be canonical UTC YYYY-MM-DDTHH:MM:SSZ")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise CalendlyCollectorError("timestamp is invalid") from error
    if parsed.tzinfo is None:
        raise CalendlyCollectorError("timestamp must be timezone-aware")
    return parsed.astimezone(timezone.utc)


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _required_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CalendlyCollectorError("record identity is required")
    return value


def _normal_person(value: object) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise CalendlyCollectorError("person must be an object")
    result: dict[str, str] = {}
    for key in ("email", "name"):
        item = value.get(key)
        if item is not None:
            if not isinstance(item, str) or not item:
                raise CalendlyCollectorError("person fields must be non-empty strings")
            result[key] = item
    if not result:
        raise CalendlyCollectorError("person must contain email or name")
    return result


def _normal_people(value: object) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise CalendlyCollectorError("participants must be a list")
    result = []
    for item in value:
        if not isinstance(item, Mapping):
            raise CalendlyCollectorError("participant items must be objects")
        result.append(_normal_person(item))
    return result


def _normal_transcript(value: object) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise CalendlyCollectorError("transcript must be a list")
    result = []
    for item in value:
        if not isinstance(item, Mapping):
            raise CalendlyCollectorError("transcript items must be objects")
        if "text" not in item:
            raise CalendlyCollectorError("transcript text is required")
        if not isinstance(item["text"], str):
            raise CalendlyCollectorError("transcript text must be a string")
        entry: dict[str, Any] = {"text": item["text"]}
        if item.get("offset_seconds") is not None:
            if isinstance(item["offset_seconds"], bool) or not isinstance(item["offset_seconds"], int) or item["offset_seconds"] < 0:
                raise CalendlyCollectorError("transcript offset must be a non-negative integer")
            entry["offset_seconds"] = item["offset_seconds"]
        if item.get("speaker") is not None:
            if not isinstance(item["speaker"], str):
                raise CalendlyCollectorError("transcript speaker must be a string")
            entry["speaker"] = item["speaker"]
        result.append(entry)
    return result


def normalized_recording(record: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise CalendlyCollectorError("recording must be an object")
    start = _parse_required_utc(record.get("recording_start_time"))
    end = _parse_required_utc(record.get("recording_end_time"))
    if end <= start:
        raise CalendlyCollectorError("recording window is invalid")
    document: dict[str, Any] = {
        "recording_id": _required_id(record.get("uri")),
        "meeting_id": _required_id(record.get("event_uri")),
        "title": str(record.get("name") or "Calendly recording"),
        "start": _iso_utc(start),
        "end": _iso_utc(end),
        "duration_seconds": int((end - start).total_seconds()),
        "organizer": _normal_person(record.get("organizer")),
        "participants": _normal_people(record.get("participants")),
        "join_url": str(record.get("join_url") or ""),
        "transcript": _normal_transcript(record.get("transcript")),
        "summary": str(record.get("summary") or ""),
    }
    return {**document, "source_digest": "sha256:" + _digest(document)}


def scheduled_without_recording(event: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        raise CalendlyCollectorError("event must be an object")
    start = _parse_required_utc(event.get("start_time"))
    end = _parse_required_utc(event.get("end_time"))
    if end <= start:
        raise CalendlyCollectorError("scheduled event window is invalid")
    return {
        "meeting_id": _required_id(event.get("uri")),
        "title": str(event.get("name") or "Calendly event"),
        "start": _iso_utc(start),
        "end": _iso_utc(end),
        "reason": "scheduled_without_recording",
    }


def _cursor_reference(cursor: str | None) -> str:
    if not cursor:
        return "initial"
    return "sha256:" + hashlib.sha256(cursor.encode("utf-8")).hexdigest()[:12]


def _gateway_configuration(cenv: Mapping[str, Any]) -> tuple[str, str]:
    missing = cenv.get("_missing")
    if missing:
        raise CalendlyCollectorError("Calendly recording gateway is unavailable")
    endpoint = cenv.get("CALENDLY_RECORDINGS_URL")
    token = cenv.get("CALENDLY_GATEWAY_TOKEN")
    read_only = cenv.get("CALENDLY_GATEWAY_READ_ONLY")
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise CalendlyCollectorError("Calendly recording gateway is unavailable")
    if not isinstance(token, str) or not token.strip():
        raise CalendlyCollectorError("Calendly recording gateway is unavailable")
    if str(read_only).casefold() != "true":
        raise CalendlyCollectorError("Calendly recording gateway is unavailable")
    parsed = urllib.parse.urlsplit(endpoint)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise CalendlyCollectorError("Calendly recording gateway is unavailable")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")), token


def _request_fingerprint(cenv: Mapping[str, Any], since: datetime, until: datetime) -> str:
    endpoint, _token = _gateway_configuration(cenv)
    request = {
        "endpoint": endpoint,
        "query": {"since": _iso_utc(since), "until": _iso_utc(until)},
        "pagination_parameter": "page_token",
        "read_only": True,
    }
    return "sha256:" + _digest(request)


def _checkpoint_identity(cenv: Mapping[str, Any], since: datetime, until: datetime) -> CheckpointIdentity:
    return CheckpointIdentity(
        source="calendly",
        since_utc=_iso_utc(since),
        until_utc=_iso_utc(until),
        request_fingerprint=_request_fingerprint(cenv, since, until),
        compatibility_version=CALENDLY_COMPATIBILITY_VERSION,
    )


def _recordings_url(endpoint: str, since: datetime, until: datetime, cursor: str | None) -> str:
    query = {"since": _iso_utc(since), "until": _iso_utc(until)}
    if cursor:
        query["page_token"] = cursor
    return endpoint + "?" + urllib.parse.urlencode(query)


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def _http_json(url: str, headers: Mapping[str, str]) -> Any:
    request = urllib.request.Request(url, headers=dict(headers))
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _page_response(data: Any) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], str | None]:
    if not isinstance(data, Mapping) or not isinstance(data.get("collection"), (list, tuple)):
        raise CalendlyCollectorError("Calendly recordings response did not contain a collection")
    items = data["collection"]
    if not all(isinstance(item, Mapping) for item in items):
        raise CalendlyCollectorError("Calendly recordings collection is invalid")
    scheduled = data.get("scheduled_without_recording", data.get("scheduled_events", []))
    if not isinstance(scheduled, (list, tuple)) or not all(isinstance(item, Mapping) for item in scheduled):
        raise CalendlyCollectorError("Calendly scheduled event collection is invalid")
    pagination = data.get("pagination", {})
    if not isinstance(pagination, Mapping):
        raise CalendlyCollectorError("Calendly pagination is invalid")
    next_cursor = pagination.get("next_page_token")
    if next_cursor is not None and (not isinstance(next_cursor, str) or not next_cursor.strip()):
        raise CalendlyCollectorError("Calendly pagination cursor is invalid")
    return list(items), list(scheduled), next_cursor.strip() if isinstance(next_cursor, str) else None


def _page_signature(
    items: list[Mapping[str, Any]], scheduled: list[Mapping[str, Any]]
) -> str:
    return "sha256:" + _digest({
        "collection": _mutable(items),
        "scheduled_without_recording": _mutable(scheduled),
    })


def _mutable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _mutable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mutable(item) for item in value]
    return value


def _checkpoint_items(
    checkpoint_store: PageCheckpointStore,
    state: CheckpointState,
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], set[str], set[str], str | None]:
    raw: list[Mapping[str, Any]] = []
    scheduled: list[Mapping[str, Any]] = []
    seen_cursors: set[str] = set()
    seen_pages: set[str] = set()
    cursor: str | None = None
    for page_number, saved_page in enumerate(checkpoint_store.iter_pages(state), start=1):
        items, page_scheduled, next_cursor = _page_response(saved_page["payload"])
        if dict(saved_page["metadata"]) != {"request_cursor": cursor}:
            raise CheckpointError("checkpoint Calendly request cursor is invalid")
        if dict(saved_page["continuation"]) != {"cursor": next_cursor}:
            raise CheckpointError("checkpoint Calendly page continuation is invalid")
        signature = _page_signature(items, page_scheduled)
        if saved_page["signature"] != signature:
            raise CheckpointError("checkpoint Calendly page signature is invalid")
        if signature in seen_pages:
            raise CheckpointError("checkpoint Calendly page repeated")
        seen_pages.add(signature)
        if next_cursor:
            if next_cursor in seen_cursors:
                raise CheckpointError("checkpoint Calendly pagination cursor repeated")
            seen_cursors.add(next_cursor)
        elif page_number != len(state.pages):
            raise CheckpointError("checkpoint Calendly terminal page has a successor")
        raw.extend(items)
        scheduled.extend(page_scheduled)
        cursor = next_cursor
    expected_continuation: dict[str, str | None] = {} if not state.pages else {"cursor": cursor}
    if dict(state.continuation) != expected_continuation:
        raise CheckpointError("checkpoint Calendly continuation is invalid")
    if state.complete and (not state.pages or cursor is not None):
        raise CheckpointError("completed Calendly checkpoint has a continuation")
    return raw, scheduled, seen_cursors, seen_pages, cursor


def _failure(status: str, *, page: int, cursor: str | None) -> dict[str, Any]:
    return {
        "status": status,
        "complete": False,
        "recordings": [],
        "scheduled_without_recording": [],
        "pages_fetched": page - 1,
        "pagination": {"status": "incomplete"},
        "failure": {"page": page, "cursor": _cursor_reference(cursor)},
    }


def _complete_result(
    raw: list[Mapping[str, Any]],
    scheduled: list[Mapping[str, Any]],
    *,
    pages_fetched: int,
) -> dict[str, Any]:
    return {
        "status": "ok",
        "complete": True,
        "recordings": [normalized_recording(_mutable(record)) for record in raw],
        "scheduled_without_recording": [scheduled_without_recording(_mutable(event)) for event in scheduled],
        "pages_fetched": pages_fetched,
        "pagination": {"status": "complete", "pages_fetched": pages_fetched},
    }


def fetch_calendly(
    cenv: Mapping[str, Any],
    since: datetime,
    until: datetime,
    *,
    checkpoint_store: PageCheckpointStore | None = None,
    http_json: Any | None = None,
) -> dict[str, Any]:
    """Fetch one bounded, read-only recording collection without exposing partial data."""
    if since.tzinfo is None or until.tzinfo is None or until <= since:
        return _failure("invalid_interval", page=1, cursor=None)
    cursor: str | None = None
    pages = 0
    try:
        endpoint, token = _gateway_configuration(cenv)
        state: CheckpointState | None = None
        raw: list[Mapping[str, Any]] = []
        scheduled: list[Mapping[str, Any]] = []
        seen_cursors: set[str] = set()
        seen_pages: set[str] = set()
        if checkpoint_store is not None:
            state = checkpoint_store.open(_checkpoint_identity(cenv, since, until))
            raw, scheduled, seen_cursors, seen_pages, cursor = _checkpoint_items(checkpoint_store, state)
            pages = len(state.pages)
            if state.pages and cursor is None and not state.complete:
                state = checkpoint_store.mark_complete(state)
        while state is None or not state.complete:
            if pages >= CALENDLY_MAX_PAGES:
                raise CalendlyCollectorError("Calendly pagination exceeded safety limit")
            data = (http_json or _http_json)(
                _recordings_url(endpoint, since, until, cursor), _headers(token)
            )
            items, page_scheduled, next_cursor = _page_response(data)
            signature = _page_signature(items, page_scheduled)
            if signature in seen_pages:
                raise CalendlyCollectorError("Calendly pagination page repeated")
            if next_cursor and next_cursor in seen_cursors:
                raise CalendlyCollectorError("Calendly pagination cursor repeated")
            if state is not None:
                state = checkpoint_store.append_page(
                    state,
                    payload=data,
                    continuation={"cursor": next_cursor},
                    signature=signature,
                    metadata={"request_cursor": cursor},
                )
            raw.extend(items)
            scheduled.extend(page_scheduled)
            seen_pages.add(signature)
            pages += 1
            if next_cursor:
                seen_cursors.add(next_cursor)
                cursor = next_cursor
                continue
            cursor = None
            if state is not None:
                state = checkpoint_store.mark_complete(state)
            break
        return _complete_result(raw, scheduled, pages_fetched=pages)
    except CalendlyCollectorError:
        return _failure("capability_unavailable", page=pages + 1, cursor=cursor)
    except Exception:
        return _failure("incomplete", page=pages + 1, cursor=cursor)


def _interval(value: str) -> datetime:
    return _parse_required_utc(value)


def _safe_path(value: str, root: Path) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    lexical = candidate
    try:
        relative = lexical.relative_to(root)
    except ValueError as error:
        raise argparse.ArgumentTypeError("path must remain below the invocation root") from error
    current = root
    for part in relative.parts:
        if part == ".":
            continue
        if part == "..":
            current = current.parent
            continue
        current = current / part
        if current.is_symlink():
            raise argparse.ArgumentTypeError("symlinked path components are not allowed")
    candidate = lexical.resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as error:
        raise argparse.ArgumentTypeError("path must remain below the invocation root") from error
    return candidate


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="calendly_collector")
    subparsers = parser.add_subparsers(dest="command", required=True)
    root = Path.cwd().resolve()
    for command in ("preflight", "collect"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--since", required=True, type=_interval)
        sub.add_argument("--until", required=True, type=_interval)
        sub.add_argument("--output", required=True, type=lambda value: _safe_path(value, root))
        if command == "collect":
            sub.add_argument("--checkpoint-root", required=True, type=lambda value: _safe_path(value, root))
    args = parser.parse_args(argv)
    if args.until <= args.since:
        parser.error("--until must be after --since")
    return args


def run(args: argparse.Namespace) -> int:
    interval = {
        "compatibility_version": CALENDLY_COMPATIBILITY_VERSION,
        "since": _iso_utc(args.since),
        "until": _iso_utc(args.until),
    }
    if args.command == "preflight":
        document = {
            **interval,
            "status": "incomplete",
            "complete": False,
            "reason": "capability_unavailable",
            "capability": "calendly_gateway_unconfigured",
        }
    else:
        document = {
            **interval,
            "status": "incomplete",
            "complete": False,
            "reason": "capability_unavailable",
            "capability": "calendly_gateway_unconfigured",
            "checkpoint_root_id": "sha256:" + _digest(str(args.checkpoint_root)),
            "recordings": [],
            "scheduled_without_recording": [],
        }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, sort_keys=True) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
