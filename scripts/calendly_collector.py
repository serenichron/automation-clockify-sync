"""Pure Calendly recording normalization and credential-free CLI preflight."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping


CALENDLY_COMPATIBILITY_VERSION = "calendly-recordings/v1"


class CalendlyCollectorError(ValueError):
    """Raised when a Calendly source record or collection interval is invalid."""


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _parse_required_utc(value: object) -> datetime:
    if not isinstance(value, str) or not value or not value.endswith("Z"):
        raise CalendlyCollectorError("timestamp must be an explicit UTC instant")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
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
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, str] = {}
    for key in ("email", "name"):
        item = value.get(key)
        if item is not None and str(item):
            result[key] = str(item)
    return result


def _normal_people(value: object) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise CalendlyCollectorError("participants must be a list")
    return [_normal_person(item) for item in value if isinstance(item, Mapping)]


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
        entry: dict[str, Any] = {"text": str(item["text"])}
        if item.get("offset_seconds") is not None:
            entry["offset_seconds"] = int(item["offset_seconds"])
        if item.get("speaker") is not None:
            entry["speaker"] = str(item["speaker"])
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


def _interval(value: str) -> datetime:
    return _parse_required_utc(value)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="calendly_collector")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("preflight", "collect"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--since", required=True, type=_interval)
        sub.add_argument("--until", required=True, type=_interval)
        sub.add_argument("--output", required=True, type=lambda value: Path(value).expanduser().resolve())
        if command == "collect":
            sub.add_argument("--checkpoint-root", required=True, type=lambda value: Path(value).expanduser().resolve())
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
        document = {**interval, "status": "ready"}
    else:
        document = {**interval, "recordings": [], "scheduled_without_recording": []}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, sort_keys=True) + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
