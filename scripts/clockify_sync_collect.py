#!/usr/bin/env python3
"""Clockify reconciliation dry-run collector for Serenichron.

Collects sanitized evidence from Clockify, Fathom, Multica, and local/remote
Hermes/Claude session metadata. Writes a run bundle under ../runs/<run_id>/.
No Clockify writes are performed by this script.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import email.utils
import fnmatch
import gzip
import hashlib
import json
import os
import pwd
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Mapping

try:
    from scripts.calendly_collector import fetch_calendly
    from scripts.collector_checkpoints import (
        CheckpointError,
        CheckpointIdentity,
        CheckpointState,
        PageCheckpointStore,
    )
    from scripts.collector_slices import (
        BacklogError,
        BacklogIdentity,
        BacklogStore,
        CollectionSlice,
        plan_slices,
    )
except ModuleNotFoundError:  # Support direct execution from this directory.
    from calendly_collector import fetch_calendly  # type: ignore[no-redef]
    from collector_checkpoints import (  # type: ignore[no-redef]
        CheckpointError,
        CheckpointIdentity,
        CheckpointState,
        PageCheckpointStore,
    )
    from collector_slices import (  # type: ignore[no-redef]
        BacklogError,
        BacklogIdentity,
        BacklogStore,
        CollectionSlice,
        plan_slices,
    )

ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"
CLOCKIFY_API = "https://api.clockify.me/api/v1"
CLOCKIFY_HTTP_TIMEOUT_NAME = "CLOCKIFY_HTTP_TIMEOUT_SECONDS"
CLOCKIFY_HTTP_TIMEOUT_DEFAULT_SECONDS = 30
CLOCKIFY_HTTP_TIMEOUT_MIN_SECONDS = 5
CLOCKIFY_HTTP_TIMEOUT_MAX_SECONDS = 120
FATHOM_API = "https://api.fathom.ai/external/v1"
FATHOM_CREATION_LOOKBACK = dt.timedelta(days=1)
FATHOM_MAX_PAGES = 1000
FATHOM_MAX_RETRY_ATTEMPTS = 3
FATHOM_MAX_RETRY_DELAY_SECONDS = 60
FATHOM_MAX_COLLECTION_RETRIES = 12
FATHOM_MAX_COLLECTION_RETRY_DELAY_SECONDS = 600
FATHOM_COLLECTION_RETRY_DEADLINE_SECONDS = 1800
CLOCKIFY_PAGE_SIZE = 200
CLOCKIFY_CHECKPOINT_COMPATIBILITY_VERSION = "clockify-pagination/v1"
FATHOM_CHECKPOINT_COMPATIBILITY_VERSION = "fathom-cursor-pagination/v2"
MULTICA_PAGE_SIZE = 100
MULTICA_CHECKPOINT_COMPATIBILITY_VERSION = "multica-offset-pagination/v1"
BACKLOG_COMPATIBILITY_VERSION = "collector-slice-bundles/v1"
CANONICAL_EXPORT_TIMEOUT_SECONDS = 900
CANONICAL_EXPORT_TIMEOUT_MIN_SECONDS = 60
CANONICAL_EXPORT_TIMEOUT_MAX_SECONDS = 1800
CANONICAL_EXPORT_ENVELOPE_PREFIX = "clockify-canonical-v1:"
COMPATIBLE_CANONICAL_EXPORT_DIGESTS = frozenset({
    # Approved 5d329568 / dd455bb fleet exporter. Later coordinator-only
    # attestation changes do not alter its evidence-export contract.
    "fd8d72d4f3469a91087568da1a953c1f8bb09a45bef572a0a4390476101053bb",
})
BUCHAREST = ZoneInfo("Europe/Bucharest")

MULTICA_PROFILE = "desktop-api.multica.ai"


def _home_candidates() -> list[Path]:
    """Return task-local and OS-account homes without duplicates."""
    homes = [Path.home()]
    try:
        homes.append(Path(pwd.getpwuid(os.getuid()).pw_dir))
    except (KeyError, OSError):
        pass
    return list(dict.fromkeys(homes))


def clockify_env_candidates() -> list[str]:
    candidates = [os.environ.get("CLOCKIFY_ENV_FILE", "")]
    for home in _home_candidates():
        candidates.extend(
            [
                str(home / ".config/serenichron/clockify.env"),
                str(home / "Work/clockify/.env"),
            ]
        )
    return list(dict.fromkeys(candidates))


def fathom_env_candidates() -> list[str]:
    candidates = [os.environ.get("FATHOM_ENV_FILE", "")]
    candidates.extend(
        str(home / ".config/serenichron/fathom.env")
        for home in _home_candidates()
    )
    return list(dict.fromkeys(candidates))


def calendly_env_candidates() -> list[str]:
    candidates = [os.environ.get("CALENDLY_ENV_FILE", "")]
    candidates.extend(
        str(home / ".config/serenichron/calendly.env")
        for home in _home_candidates()
    )
    return list(dict.fromkeys(candidates))


def load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")


def collector_script_sha256() -> str:
    """Digest the exact collector code that is executing this command."""
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def collector_runtime_identity() -> dict[str, Any]:
    """Record the exact local checkout that assembled a dry-run bundle."""
    sha = None
    dirty = None
    try:
        top_level = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        top_level_path = top_level.stdout.strip()
        if (
            top_level.returncode != 0
            or not top_level_path
            or Path(top_level_path).resolve() != ROOT.resolve()
        ):
            raise RuntimeError("collector root is not a Git worktree")
        proc = subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if proc.returncode == 0:
            sha = proc.stdout.strip() or None
        # Durable state and Syncthing scratch files are intentionally untracked
        # and cannot change the committed collector implementation. Record only
        # tracked-file drift against the exact candidate SHA.
        status = subprocess.run(
            [
                "git",
                "-C",
                str(ROOT),
                "status",
                "--porcelain",
                "--untracked-files=no",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if status.returncode == 0:
            dirty = bool(status.stdout.strip())
    except Exception:
        pass
    return {
        "collector_path": str(Path(__file__).resolve()),
        "canonical_root": str(ROOT),
        "git_sha": sha,
        "git_dirty": dirty,
    }


def load_env_file(candidates: list[str], required_keys: list[str]) -> dict[str, Any]:
    env: dict[str, str] = {}
    used = None
    for c in candidates:
        if c and Path(c).exists():
            used = c
            for raw in Path(c).read_text().splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
            break
    missing = [k for k in required_keys if not env.get(k)]
    return {"_env_file": used or "missing", "_missing": missing, **env}


def iso_utc(d: dt.datetime) -> str:
    if d.tzinfo is None:
        d = d.replace(tzinfo=BUCHAREST)
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_dt(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        parsed = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=BUCHAREST)
        return parsed
    except Exception:
        return None


def local_dt_string(d: dt.datetime | None) -> str | None:
    if not d:
        return None
    return d.astimezone(BUCHAREST).strftime("%Y-%m-%d %H:%M")


def http_json(
    url: str,
    headers: dict[str, str],
    *,
    timeout_seconds: int = CLOCKIFY_HTTP_TIMEOUT_DEFAULT_SECONDS,
) -> Any:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout_seconds) as r:
        return json.loads(r.read())


def _fathom_retry_delay(error: urllib.error.HTTPError, attempt: int) -> int:
    """Return a capped rate-limit delay, preferring Fathom's Retry-After."""
    fallback = min(FATHOM_MAX_RETRY_DELAY_SECONDS, 2 ** attempt)
    retry_after = (error.headers or {}).get("Retry-After")
    if not retry_after:
        return fallback
    try:
        return min(FATHOM_MAX_RETRY_DELAY_SECONDS, max(0, int(retry_after)))
    except (TypeError, ValueError):
        pass
    try:
        retry_at = email.utils.parsedate_to_datetime(retry_after)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=dt.timezone.utc)
        seconds = int((retry_at - dt.datetime.now(dt.timezone.utc)).total_seconds())
        return min(FATHOM_MAX_RETRY_DELAY_SECONDS, max(0, seconds))
    except (TypeError, ValueError, IndexError, OverflowError):
        return fallback


class FathomRetryBudgetExhausted(RuntimeError):
    """A collection-wide Fathom retry budget was exhausted."""


class FathomRetryBudget:
    """Keep rate-limit retries bounded across every page of one collection."""

    def __init__(self) -> None:
        self.started_at = time.monotonic()
        self.retry_delays: list[int] = []

    def policy(self) -> dict[str, Any]:
        return {
            "max_attempts_per_page": FATHOM_MAX_RETRY_ATTEMPTS,
            "max_retries_per_collection": FATHOM_MAX_COLLECTION_RETRIES,
            "max_delay_per_retry_seconds": FATHOM_MAX_RETRY_DELAY_SECONDS,
            "max_total_retry_delay_seconds": FATHOM_MAX_COLLECTION_RETRY_DELAY_SECONDS,
            "retry_deadline_seconds": FATHOM_COLLECTION_RETRY_DEADLINE_SECONDS,
            "retryable_http_statuses": [429],
        }

    def reserve_retry(self, delay: int) -> None:
        elapsed = max(0, int(time.monotonic() - self.started_at))
        if len(self.retry_delays) >= FATHOM_MAX_COLLECTION_RETRIES:
            raise FathomRetryBudgetExhausted("collection_retry_count_exhausted")
        if sum(self.retry_delays) + delay > FATHOM_MAX_COLLECTION_RETRY_DELAY_SECONDS:
            raise FathomRetryBudgetExhausted("collection_retry_delay_exhausted")
        if elapsed + delay > FATHOM_COLLECTION_RETRY_DEADLINE_SECONDS:
            raise FathomRetryBudgetExhausted("collection_retry_deadline_exhausted")
        self.retry_delays.append(delay)

    def require_time_remaining(self) -> None:
        elapsed = max(0, int(time.monotonic() - self.started_at))
        if elapsed > FATHOM_COLLECTION_RETRY_DEADLINE_SECONDS:
            raise FathomRetryBudgetExhausted("collection_retry_deadline_exhausted")


def fathom_http_json_with_retry(
    url: str, headers: dict[str, str], retry_budget: FathomRetryBudget
) -> Any:
    """Fetch one Fathom page, retrying only bounded HTTP 429 responses."""
    for attempt in range(FATHOM_MAX_RETRY_ATTEMPTS):
        try:
            return http_json(url, headers)
        except urllib.error.HTTPError as error:
            if error.code != 429 or attempt + 1 >= FATHOM_MAX_RETRY_ATTEMPTS:
                raise
            delay = _fathom_retry_delay(error, attempt)
            retry_budget.reserve_retry(delay)
            time.sleep(delay)
    raise AssertionError("Fathom retry loop exhausted without returning or raising")


def canonical_export_timeout_seconds() -> int:
    """Return the bounded timeout for canonical month-scale remote exports."""
    raw = os.environ.get("CLOCKIFY_CANONICAL_EXPORT_TIMEOUT_SECONDS", "")
    try:
        configured = int(raw) if raw else CANONICAL_EXPORT_TIMEOUT_SECONDS
    except ValueError:
        configured = CANONICAL_EXPORT_TIMEOUT_SECONDS
    return min(
        CANONICAL_EXPORT_TIMEOUT_MAX_SECONDS,
        max(CANONICAL_EXPORT_TIMEOUT_MIN_SECONDS, configured),
    )


def canonical_export_envelope(payload: dict[str, Any]) -> str:
    """Compress a canonical export into one noise-tolerant transport line."""
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    compressed = gzip.compress(raw, compresslevel=1, mtime=0)
    return CANONICAL_EXPORT_ENVELOPE_PREFIX + base64.b64encode(compressed).decode("ascii")


def canonical_export_payload(stdout: str, machine_name: str) -> dict[str, Any] | None:
    """Find and decode the last machine-specific payload without exposing stdout."""
    for line in reversed(stdout.splitlines()):
        try:
            if line.startswith(CANONICAL_EXPORT_ENVELOPE_PREFIX):
                encoded = line[len(CANONICAL_EXPORT_ENVELOPE_PREFIX):]
                compressed = base64.b64decode(encoded, validate=True)
                candidate = json.loads(gzip.decompress(compressed))
            else:
                candidate = json.loads(line)
        except (ValueError, gzip.BadGzipFile, json.JSONDecodeError):
            continue
        if isinstance(candidate, dict) and candidate.get("machine") == machine_name:
            return candidate
    return None


def clockify_http_timeout_seconds(cenv: Mapping[str, str]) -> int:
    """Return the validated read-only Clockify request timeout."""
    raw = (
        cenv[CLOCKIFY_HTTP_TIMEOUT_NAME]
        if CLOCKIFY_HTTP_TIMEOUT_NAME in cenv
        else os.environ.get(CLOCKIFY_HTTP_TIMEOUT_NAME)
    )
    if raw is None:
        return CLOCKIFY_HTTP_TIMEOUT_DEFAULT_SECONDS
    if not re.fullmatch(r"[0-9]+", raw):
        raise ValueError(
            f"{CLOCKIFY_HTTP_TIMEOUT_NAME} must be an integer from "
            f"{CLOCKIFY_HTTP_TIMEOUT_MIN_SECONDS} through "
            f"{CLOCKIFY_HTTP_TIMEOUT_MAX_SECONDS}"
        )
    value = int(raw)
    if not CLOCKIFY_HTTP_TIMEOUT_MIN_SECONDS <= value <= CLOCKIFY_HTTP_TIMEOUT_MAX_SECONDS:
        raise ValueError(
            f"{CLOCKIFY_HTTP_TIMEOUT_NAME} must be an integer from "
            f"{CLOCKIFY_HTTP_TIMEOUT_MIN_SECONDS} through "
            f"{CLOCKIFY_HTTP_TIMEOUT_MAX_SECONDS}"
        )
    return value


def clockify_get(path: str, cenv: dict[str, str]) -> Any:
    return http_json(
        CLOCKIFY_API + path,
        {"X-Api-Key": cenv["CLOCKIFY_API_KEY"]},
        timeout_seconds=clockify_http_timeout_seconds(cenv),
    )


def latest_clockify_entry(cenv: dict[str, str], user_id: str) -> dt.datetime | None:
    ws = cenv["CLOCKIFY_WORKSPACE_ID"]
    end = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1)
    start = end - dt.timedelta(days=45)
    path = f"/workspaces/{ws}/user/{user_id}/time-entries?start={iso_utc(start)}&end={iso_utc(end)}&page-size=200"
    entries = clockify_get(path, cenv)
    latest = None
    for e in entries:
        raw = e.get("timeInterval", {}).get("start")
        d = parse_dt(raw)
        if d and (latest is None or d > latest):
            latest = d
    return latest


def compute_range(args: argparse.Namespace, routing: dict[str, Any], cenv: dict[str, str]) -> tuple[dt.datetime, dt.datetime, str]:
    if args.since:
        since = dt.datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=BUCHAREST)
        reason = "explicit --since"
    else:
        latest = None
        if not cenv.get("_missing"):
            try:
                latest = latest_clockify_entry(cenv, routing["clockify_user_id"])
            except Exception:
                latest = None
        if latest:
            since = (latest.astimezone(BUCHAREST) - dt.timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
            reason = "7 days before latest Clockify entry"
        else:
            since = (dt.datetime.now(BUCHAREST) - dt.timedelta(days=3)).replace(hour=0, minute=0, second=0, microsecond=0)
            reason = "fallback last 3 days"
    if args.until:
        until = dt.datetime.strptime(args.until, "%Y-%m-%d").replace(tzinfo=BUCHAREST) + dt.timedelta(days=1)
    else:
        until = dt.datetime.now(BUCHAREST)
    return since, until, reason


def label_from_claude_path(path: str, base: str) -> str:
    try:
        rel = str(Path(path).relative_to(base))
    except Exception:
        rel = path.replace(base, "").lstrip("/")
    top = rel.split("/")[0]
    label = top
    prefixes = [
        ("-Users-blackthorne-Work-", ""),
        ("-Users-blackthorne-", ""),
        ("-home-blackthorne-Work-", ""),
        ("-home-blackthorne-", ""),
    ]
    for p, r in prefixes:
        if label.startswith(p):
            return r + label[len(p):]
    return label


def is_skip_path(path: str) -> bool:
    fragments = ["/subagents/", "multica-command", "claude-mem-observer", "/multica/"]
    return any(f in path for f in fragments)


BURST_GAP_SECONDS = 30 * 60
DESCRIPTION_LIMIT = 180
CONTEXT_LIMIT = 320


def _one_line(value: str, limit: int = CONTEXT_LIMIT) -> str:
    """Normalize evidence text for a single Clockify description/table cell."""
    value = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(value) <= limit:
        return value
    cut = value[: limit - 3].rsplit(" ", 1)[0]
    return (cut or value[: limit - 3]).rstrip() + "..."


def _plain_description_text(value: str, limit: int = CONTEXT_LIMIT) -> str:
    """Remove presentation markup while retaining task-specific text."""
    value = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", str(value or ""))
    value = re.sub(r"```(?:[A-Za-z0-9_+-]+)?", " ", value)
    value = re.sub(r"[`*_#]+", "", value)
    value = re.sub(r"[★☆]\s*(?:Insight|Tip)?\s*[\u2500-\u257f]+", " ", value)
    value = re.sub(r"[\u2500-\u257f]{3,}", " ", value)
    value = re.sub(r"\s*\|\s*", " / ", value)
    return _one_line(value, limit)


def _is_system_message(msg: str) -> bool:
    """Reject injected wrappers, tool noise, and other non-human session context."""
    lower = _one_line(msg, 2000).lower()
    if not lower:
        return True
    if re.fullmatch(r"(?:[a-z0-9]+_)*ok[.!]?", lower):
        return True
    if lower.startswith(("<command-", "<local-command-", "<teammate-message", "[tool_", "[image:", "[thinking]", "![", "/home/", "/users/")):
        return True
    if lower.startswith("<") and ">" in lower[:80]:
        return True
    if lower.startswith(
        (
            "you're out of usage credits",
            "you’re out of usage credits",
            "rate limit exceeded",
            "you've hit your session limit",
            "you’ve hit your session limit",
            "this session is being continued from a previous conversation",
            "reply with exactly ",
            "reply exactly ",
        )
    ):
        return True
    return any(marker in lower for marker in (
        "permissions instructions",
        "codex agent history",
        "<app-context>",
        "# agents.md instructions",
    ))


def _strip_command_wrapper(msg: str) -> str:
    """Retain actual text from a Claude command wrapper, if it contains any."""
    cleaned = []
    for line in str(msg or "").strip().splitlines():
        stripped = line.strip()
        if stripped.startswith("<") and stripped.endswith(">"):
            continue
        cleaned.append(stripped)
    result = "\n".join(cleaned).strip()
    # Codex Desktop prepends file-context scaffolding to the actual request.
    # Keep the request, not the generated file inventory.
    marker = "# my request for codex:"
    marker_index = result.lower().find(marker)
    if marker_index >= 0:
        result = result[marker_index + len(marker):].strip()
    return result


def _session_directive(msg: str) -> str:
    """Recover the human task embedded in a session-scoped hook wrapper."""
    match = re.search(
        r'(?is)\bcondition:\s*["“](.+?)["”]\s*\.\s*'
        r"Briefly acknowledge\b",
        str(msg or ""),
    )
    if not match:
        # Fleet collectors intentionally cap raw context. Preserve the useful
        # beginning of a condition even when the closing wrapper was truncated.
        match = re.search(
            r'(?is)\bcondition:\s*["“](.+)$',
            str(msg or ""),
        )
    if not match:
        return ""
    value = re.split(r"(?i)\bBriefly acknowledge\b", match.group(1), maxsplit=1)[0]
    return _plain_description_text(value.strip(" \t\r\n\"“”"))


def _is_low_information_context(msg: str) -> bool:
    """Reject acknowledgements and setup-only directions as work summaries."""
    value = _one_line(msg, 200).casefold()
    return bool(
        re.fullmatch(
            r"(?:yes(?:,?\s+please)?(?:\s+do\s+that)?|"
            r"ok(?:ay)?|approved|yes,?\s+approved|proceed|go\s+ahead|"
            r"read\s+(?:the\s+)?.{0,40}\s+skills?\s+first)[.!]?",
            value,
        )
    )


def _looks_like_task_request(msg: str) -> bool:
    """Identify direct work requests without trying to semantically summarize them."""
    value = _one_line(msg, 240).casefold()
    value = re.sub(
        r"^(?:please|can you|could you|would you|i want you to|we need to)\s+",
        "",
        value,
    )
    return bool(
        re.match(
            r"^(?:analy[sz]e|audit|check|compare|configure|create|debug|"
            r"diagnose|draft|fix|implement|investigate|prepare|research|"
            r"review|test|troubleshoot|update|verify|write)\b",
            value,
        )
    )


def _problem_context_summary(msg: str) -> str:
    """Summarize a small set of recognizable error reports without guessing."""
    lower = _one_line(msg, 1000).casefold()
    if "stop hook error" in lower or "hook evaluator api error" in lower:
        return "Troubleshoot Claude Code Stop hook evaluator error"
    return ""


def _is_contextless_description(description: Any) -> bool:
    """Recognize generic rows even after the overlap allocator annotates them."""
    description = str(description or "").removesuffix(
        " (trimmed around parallel work)"
    )
    return bool(
        re.search(
            r" — \[NEEDS REVIEW\] Unlabeled session on .+ \(\d+ msgs, \d+m\)$",
            description,
        )
    )


def _explicit_context_heading(msg: str) -> str:
    """Extract a concise task title embedded in otherwise generic prose."""
    match = re.search(
        r"(?i)(?:#{1,6}\s*)?\b(?:goal|task|objective)\s*:\s*"
        r"(.+?)(?=\s+(?:\*\*|__)|[\r\n]|$)",
        msg,
    )
    if not match:
        match = re.search(r"(?i)(?:^|\s)/goal\s+(.+?)(?=[.\r\n]|$)", msg)
    if not match:
        return ""
    return _plain_description_text(match.group(1).strip(" :-"))


def _meaningful_context(msg: str) -> str:
    msg = _strip_command_wrapper(msg)
    if _is_system_message(msg):
        return ""
    result = (
        _explicit_context_heading(msg)
        or _session_directive(msg)
        or _plain_description_text(msg)
    )
    return "" if _is_low_information_context(result) else result


def _message_content(content: Any) -> str:
    """Extract human-readable text only; tool payloads are never descriptions."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content or "")
    return "\n".join(
        str(item.get("text", ""))
        for item in content
        if isinstance(item, dict) and item.get("type") == "text"
    )


def _message_evidence(content: Any, role: str) -> list[dict[str, Any]]:
    """Normalize text and tool activity while excluding hidden reasoning."""
    if isinstance(content, str):
        return [{"role": role, "kind": "message", "content": content}]
    if not isinstance(content, list):
        return [{"role": role, "kind": "message", "content": str(content or "")}]
    events: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        item_type = str(item.get("type") or "")
        if item_type == "text":
            events.append({"role": role, "kind": "message", "content": str(item.get("text") or "")})
        elif item_type == "tool_use":
            events.append(
                {
                    "role": role,
                    "kind": "tool_call",
                    "tool_name": str(item.get("name") or "unknown"),
                    "content": json.dumps(item.get("input") or {}, ensure_ascii=False, sort_keys=True),
                }
            )
        elif item_type == "tool_result":
            value = item.get("content")
            if not isinstance(value, str):
                value = json.dumps(value, ensure_ascii=False, sort_keys=True)
            events.append(
                {
                    "role": "tool",
                    "kind": "tool_result",
                    "tool_name": str(item.get("tool_use_id") or ""),
                    "content": value,
                }
            )
        # Thinking and tool-reference transport are deliberately not evidence.
    return events


def _serialized_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Preserve complete normalized message evidence without display truncation."""
    return [
        {
            "timestamp": local_dt_string(event.get("timestamp")),
            "role": str(event.get("role") or "unknown"),
            "kind": str(event.get("kind") or "message"),
            "content": str(event.get("content") or ""),
            **(
                {"tool_name": str(event.get("tool_name"))}
                if event.get("tool_name")
                else {}
            ),
        }
        for event in events
    ]


def _partition_bursts(events: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group on direct user activity, then attach assistant context without bridging gaps."""
    users = sorted(
        (event for event in events if event.get("timestamp") and event.get("role") == "user"),
        key=lambda event: event["timestamp"],
    )
    if not users:
        return []
    bursts: list[list[dict[str, Any]]] = []
    for user in users:
        if not bursts or (user["timestamp"] - bursts[-1][-1]["timestamp"]).total_seconds() > BURST_GAP_SECONDS:
            bursts.append([user])
        else:
            bursts[-1].append(user)
    # Each assistant result belongs to the current user burst only until the
    # following burst starts. It can enrich context but never merge user work.
    for event in events:
        if event.get("role") == "user" or not event.get("timestamp"):
            continue
        for index, burst in enumerate(bursts):
            start = burst[0]["timestamp"]
            last_user = burst[-1]["timestamp"]
            next_start = bursts[index + 1][0]["timestamp"] if index + 1 < len(bursts) else None
            if (event["timestamp"] >= start
                    and event["timestamp"] <= last_user + dt.timedelta(seconds=BURST_GAP_SECONDS)
                    and (next_start is None or event["timestamp"] < next_start)):
                burst.append(event)
                break
    return [sorted(burst, key=lambda event: event["timestamp"]) for burst in bursts]


def _burst_context(events: list[dict[str, Any]]) -> tuple[str, str, list[dt.datetime]]:
    """Return first meaningful user context, final useful assistant result, and user times."""
    first_user = ""
    last_assistant = ""
    user_timestamps: list[dt.datetime] = []
    for event in events:
        if event["role"] == "user":
            user_timestamps.append(event["timestamp"])
            if not first_user:
                candidate = _meaningful_context(event.get("content", ""))
                # Acknowledgements such as "ok" or "yes" carry no billable
                # work context; keep scanning within the same burst.
                if len(candidate) >= 10:
                    first_user = candidate
        elif event["role"] == "assistant":
            useful = _meaningful_context(event.get("content", ""))
            if useful:
                last_assistant = useful
    return first_user, last_assistant, user_timestamps


def _record_provenance(record: dict[str, Any]) -> dict[str, Any]:
    """Stable, compact source identity used by review and durable deduplication."""
    source_type = {
        "claude_jsonl": "claude",
        "hermes_legacy": "hermes",
        "hermes_db": "hermes",
    }.get(record.get("source"), record.get("source", "unknown"))
    provenance = {
        "source_type": source_type,
        "source_machine": record.get("machine", "unknown"),
        "source_session_id": record.get("session_id", ""),
        "burst_start": record.get("start", ""),
        "burst_end": record.get("end", ""),
    }
    evidence_path = record.get("path") or record.get("cwd")
    if evidence_path:
        provenance["path"] = evidence_path
    return provenance


def _candidate_key(record: dict[str, Any]) -> str:
    provenance = _record_provenance(record)
    # The session id deliberately participates: identical labels/times on different
    # machines or sessions must remain independently reviewable candidates.
    canonical = json.dumps(provenance, sort_keys=True, separators=(",", ":"))
    return "ck-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]



def _is_weekend_short_session(
    record: dict[str, Any], rules: dict[str, Any]
) -> bool:
    start = parse_dt(record.get("start"))
    duration = int(record.get("duration_minutes") or 0)
    max_minutes = int(rules.get("weekend_short_max_minutes", 60))
    return bool(start and start.weekday() >= 5 and duration <= max_minutes)


def _replica_key(proposal: dict[str, Any]) -> tuple[str, str, str, str] | None:
    """Identify the same synced session burst observed on multiple machines."""
    provenance = proposal.get("provenance")
    if not isinstance(provenance, dict):
        return None
    source_type = str(provenance.get("source_type") or "")
    session_id = str(provenance.get("source_session_id") or "")
    start = str(provenance.get("burst_start") or "")
    end = str(provenance.get("burst_end") or "")
    if not all((source_type, session_id, start, end)):
        return None
    return source_type, session_id, start, end


def _dedupe_replicated_candidates(
    proposals: list[dict[str, Any]], skipped: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for proposal in proposals:
        key = _replica_key(proposal)
        if key is None or key not in seen:
            result.append(proposal)
            if key is not None:
                seen[key] = proposal
            continue
        kept = seen[key]
        skipped.append(
            {
                "id": proposal.get("candidate_key"),
                "source": proposal.get("source"),
                "time": f"{proposal.get('start')}–{proposal.get('end')}",
                "label": proposal.get("source_label"),
                "reason": (
                    "replicated session evidence already represented by "
                    f"{kept.get('candidate_key')}"
                ),
            }
        )
    return result


def _merge_adjacent_same_work(
    proposals: list[dict[str, Any]], skipped: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Consolidate immediate same-description continuation sessions."""
    result: list[dict[str, Any]] = []
    for proposal in sorted(
        proposals, key=lambda row: (str(row.get("start") or ""), str(row.get("id") or ""))
    ):
        start = parse_dt(proposal.get("start"))
        merged = None
        for prior in reversed(result):
            prior_end = parse_dt(prior.get("end"))
            if not start or not prior_end:
                continue
            gap = (start - prior_end).total_seconds()
            if gap > BURST_GAP_SECONDS:
                break
            if gap < 0:
                continue
            same_work = (
                prior.get("description") == proposal.get("description")
                and prior.get("clockify_project_suffix")
                == proposal.get("clockify_project_suffix")
                and str((prior.get("provenance") or {}).get("source_type") or "")
                == str((proposal.get("provenance") or {}).get("source_type") or "")
            )
            if same_work:
                merged = prior
                break
        if merged is None:
            result.append(proposal)
            continue

        component_keys = list(merged.get("merged_candidate_keys") or [])
        if not component_keys:
            component_keys.append(str(merged.get("candidate_key") or ""))
        component_keys.append(str(proposal.get("candidate_key") or ""))
        component_keys = sorted(set(key for key in component_keys if key))
        merged["merged_candidate_keys"] = component_keys
        canonical = json.dumps(component_keys, separators=(",", ":"))
        merged["candidate_key"] = (
            "ckm-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]
        )
        merged["end"] = proposal.get("end")
        merged["duration_minutes"] = int(merged.get("duration_minutes") or 0) + int(
            proposal.get("duration_minutes") or 0
        )
        provenance = merged.get("provenance") or {}
        provenance["burst_end"] = proposal.get("end")
        merged["provenance"] = provenance
        skipped.append(
            {
                "id": proposal.get("candidate_key"),
                "source": proposal.get("source"),
                "time": f"{proposal.get('start')}–{proposal.get('end')}",
                "label": proposal.get("source_label"),
                "reason": (
                    "adjacent same-work continuation merged into "
                    f"{merged.get('candidate_key')}"
                ),
            }
        )
    for index, proposal in enumerate(result, start=1):
        proposal["id"] = f"P{index:03d}"
    return result


def _resolve_candidate_overlaps(
    proposals: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    rules: dict[str, Any],
) -> list[dict[str, Any]]:
    """Allocate focused candidates into non-conflicting review windows."""
    soft_overlap_minutes = int(rules.get("soft_overlap_minutes", 15))
    min_minutes = int(rules.get("min_minutes", 10))
    confidence_rank = {"high": 0, "medium": 1, "low": 2}

    def priority(row: dict[str, Any]) -> tuple[int, int, float, str]:
        provenance = row.get("provenance") or {}
        source_rank = 0 if provenance.get("source_type") == "fathom" else 1
        start = parse_dt(row.get("start"))
        end = parse_dt(row.get("end"))
        span = (end - start).total_seconds() if start and end else float("inf")
        return (
            source_rank,
            confidence_rank.get(str(row.get("confidence") or "low"), 3),
            span,
            str(row.get("start") or ""),
        )

    accepted: list[dict[str, Any]] = []
    for proposal in sorted(proposals, key=priority):
        start = parse_dt(proposal.get("start"))
        end = parse_dt(proposal.get("end"))
        if not start or not end or end <= start:
            skipped.append(
                {
                    "id": proposal.get("candidate_key"),
                    "source": proposal.get("source"),
                    "time": f"{proposal.get('start')}–{proposal.get('end')}",
                    "label": proposal.get("source_label"),
                    "reason": "invalid proposal window during overlap allocation",
                }
            )
            continue

        occupied: list[tuple[dt.datetime, dt.datetime]] = []
        for prior in accepted:
            prior_start = parse_dt(prior.get("start"))
            prior_end = parse_dt(prior.get("end"))
            if not prior_start or not prior_end:
                continue
            left, right = max(start, prior_start), min(end, prior_end)
            if right > left:
                occupied.append((left, right))
        if not occupied:
            accepted.append(proposal)
            continue

        occupied.sort()
        merged_occupied: list[list[dt.datetime]] = []
        for left, right in occupied:
            if not merged_occupied or left > merged_occupied[-1][1]:
                merged_occupied.append([left, right])
            else:
                merged_occupied[-1][1] = max(merged_occupied[-1][1], right)
        overlap_minutes = sum(
            (right - left).total_seconds() / 60
            for left, right in merged_occupied
        )
        if overlap_minutes < soft_overlap_minutes:
            accepted.append(proposal)
            continue

        free: list[tuple[dt.datetime, dt.datetime]] = []
        cursor = start
        for left, right in merged_occupied:
            if left > cursor:
                free.append((cursor, left))
            cursor = max(cursor, right)
        if cursor < end:
            free.append((cursor, end))
        free = [
            (left, right)
            for left, right in free
            if (right - left).total_seconds() / 60 >= min_minutes
        ]
        if not free:
            skipped.append(
                {
                    "id": proposal.get("candidate_key"),
                    "source": proposal.get("source"),
                    "time": f"{proposal.get('start')}–{proposal.get('end')}",
                    "label": proposal.get("source_label"),
                    "reason": (
                        "fully covered by higher-priority proposal windows during "
                        "overlap allocation"
                    ),
                }
            )
            continue

        free_start, free_end = max(
            free,
            key=lambda segment: (
                (segment[1] - segment[0]).total_seconds(),
                -segment[0].timestamp(),
            ),
        )
        free_minutes = int((free_end - free_start).total_seconds() / 60)
        original_window = f"{proposal.get('start')}–{proposal.get('end')}"
        proposal["allocation"] = {
            "original_window": original_window,
            "overlap_minutes_removed": int(overlap_minutes),
            "rule": "largest free segment after higher-priority candidates",
        }
        proposal["start"] = local_dt_string(free_start)
        proposal["end"] = local_dt_string(free_end)
        proposal["duration_minutes"] = min(
            int(proposal.get("duration_minutes") or free_minutes), free_minutes
        )
        proposal["description"] = _one_line(
            f"{proposal.get('description', '')} (trimmed around parallel work)",
            DESCRIPTION_LIMIT,
        )
        proposal["rationale"] = (
            f"{proposal.get('rationale', '')}; {int(overlap_minutes)}m overlap "
            "removed by deterministic allocation"
        ).strip("; ")
        accepted.append(proposal)

    accepted.sort(key=lambda row: (str(row.get("start") or ""), str(row.get("id") or "")))
    for index, proposal in enumerate(accepted, start=1):
        proposal["id"] = f"P{index:03d}"
    return accepted


def compute_active_duration(ts_list: list[dt.datetime], gap_engaged_sec: int = 120, sporadic_min: int = 4) -> tuple[int, str, list[dict]]:
    """Compute realistic active time from user message timestamps.
    
    Two scenarios:
    1. Engaged: gap between consecutive user messages <= gap_engaged_sec
       → bill the full interval (user was actively working)
    2. Sporadic: gap > gap_engaged_sec
       → bill sporadic_min per user message (check-in mode)
    
    Returns (total_minutes, method_description, segment_details)
    Returns 0 for single-message sessions (likely autopilot/health-check pings).
    """
    if not ts_list:
        return 0, "no timestamps", []
    
    ts_list = sorted(ts_list)
    
    # Single message: skip entirely (autopilot health-check, native-ok pings, etc.)
    if len(ts_list) <= 1:
        return 0, "single message — skipped", []
    
    total_sec = 0
    segments = []
    
    for i in range(len(ts_list)):
        if i == 0:
            # First message: bill sporadic_min as base (no preceding gap to measure)
            seg_sec = sporadic_min * 60
            segments.append({"index": i, "ts": local_dt_string(ts_list[i]), "mode": "first", "seconds": seg_sec})
            total_sec += seg_sec
            continue
        
        gap = (ts_list[i] - ts_list[i-1]).total_seconds()
        
        if gap <= gap_engaged_sec:
            # Engaged: bill the full interval
            seg_sec = gap
            segments.append({"index": i, "ts": local_dt_string(ts_list[i]), "mode": "engaged", "gap_s": round(gap), "seconds": seg_sec})
            total_sec += seg_sec
        else:
            # Sporadic: bill sporadic_min
            seg_sec = sporadic_min * 60
            segments.append({"index": i, "ts": local_dt_string(ts_list[i]), "mode": "sporadic", "gap_s": round(gap), "seconds": seg_sec})
            total_sec += seg_sec
    
    total_min = max(1, round(total_sec / 60))
    engaged_count = sum(1 for s in segments if s["mode"] == "engaged")
    sporadic_count = sum(1 for s in segments if s["mode"] == "sporadic")
    method = f"computed: {engaged_count} engaged + {sporadic_count} sporadic segments"
    return total_min, method, segments



def _extract_claude_events(path: Path, since: dt.datetime, until: dt.datetime) -> list[dict[str, Any]]:
    """Extract timestamped Claude user/assistant events for per-burst attribution."""
    events: list[dict[str, Any]] = []
    try:
        for line in path.read_text(errors="ignore").splitlines():
            try:
                obj = json.loads(line)
            except Exception:
                continue
            t = parse_dt(obj.get("timestamp"))
            if not t:
                continue
            msg_type = obj.get("type", "")
            if msg_type not in ("user", "assistant"):
                continue
            content = obj.get("message", {}).get("content", "")
            for normalized in _message_evidence(content, msg_type):
                events.append(
                    {
                        "timestamp": t.astimezone(BUCHAREST),
                        "cwd": str(obj.get("cwd") or ""),
                        **normalized,
                    }
                )
    except Exception:
        pass
    return events


def _window_overlaps(
    start: dt.datetime,
    end: dt.datetime,
    since: dt.datetime,
    until: dt.datetime,
) -> bool:
    """Keep full burst boundaries when any part intersects the evidence window."""
    return end >= since and start < until


def parse_claude_jsonl_file(path: Path | str, base: str, since: dt.datetime, until: dt.datetime, machine: str) -> list[dict[str, Any]]:
    out = []
    p = Path(path)
    if is_skip_path(str(p)):
        return out
    events = _extract_claude_events(p, since, until)
    if not events:
        return out
    label = label_from_claude_path(str(p), base)
    for burst_events in _partition_bursts(events):
        first_user, last_assistant, burst_ts = _burst_context(burst_events)
        if not burst_ts:
            continue
        # Retain the established duration/window semantics: only direct user
        # activity determines the Clockify window, while assistant events supply
        # context and participate in burst-boundary detection.
        bs, be = burst_ts[0], burst_ts[-1]
        if not _window_overlaps(bs, be, since, until):
            continue
        cnt = len(burst_ts)
        raw_wallclock = max(1, int((be - bs).total_seconds() / 60))
        heartbeat = all(t.minute in (29, 44) for t in burst_ts)
        # Compute active duration with the new engagement-aware algorithm
        active_min, method, _segments = compute_active_duration(burst_ts)
        out.append({
            "source": "claude",
            "machine": machine,
            "session_id": p.stem,
            "path": str(p),
            "cwd": next(
                (str(event.get("cwd")) for event in burst_events if event.get("cwd")),
                "",
            ),
            "label": label,
            "start": local_dt_string(bs),
            "end": local_dt_string(be),
            "duration_minutes": min(active_min, raw_wallclock),  # Capped at wall-clock span
            "raw_wallclock_minutes": raw_wallclock,
            "user_messages": cnt,
            "heartbeat_like": heartbeat,
            "evidence_level": method,
            "first_user_message": first_user,
            "last_assistant_message": last_assistant,
            "events": _serialized_events(burst_events),
        })
    return out


def collect_repository_events(
    session_result: dict[str, Any],
    since: dt.datetime,
    until: dt.datetime,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Collect commit-backed work products from observed session CWDs.

    Dirty working-tree paths are deliberately excluded: their age and author are
    not proven by current state.  Commit metadata and changed paths are bounded,
    immutable evidence that may corroborate an accomplishment without becoming
    a Clockify allocation by themselves.
    """
    candidate_cwds = {
        str(record.get("cwd") or "").strip()
        for key in ("claude_bursts", "hermes_sessions", "hermes_db_sessions", "codex_sessions")
        for record in session_result.get(key, [])
        if isinstance(record, dict) and str(record.get("cwd") or "").strip()
    }
    roots: set[str] = set()
    errors: list[str] = []
    for cwd in sorted(candidate_cwds):
        path = Path(cwd)
        if not path.is_absolute() or not path.is_dir():
            continue
        try:
            resolved = subprocess.run(
                ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except FileNotFoundError:
            return [], [], ["git executable unavailable for repository evidence"]
        except subprocess.TimeoutExpired:
            errors.append(f"repository root lookup timed out: {path}")
            continue
        if resolved.returncode == 0 and resolved.stdout.strip():
            roots.add(resolved.stdout.strip())

    records: list[dict[str, Any]] = []
    for root in sorted(roots):
        try:
            log = subprocess.run(
                [
                    # The requested date range is the completeness boundary.
                    # A hard commit-count cap can silently turn a busy month
                    # into partial evidence while reporting it as complete.
                    "git", "-C", root, "log", "--all",
                    f"--since={since.isoformat()}", f"--until={until.isoformat()}",
                    "--format=%x1e%H%x1f%cI%x1f%aI%x1f%s",
                    "--name-only",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except subprocess.TimeoutExpired:
            errors.append(f"commit evidence timed out: {root}")
            continue
        if log.returncode != 0:
            errors.append(f"commit evidence unavailable: {root}")
            continue
        for raw in log.stdout.split("\x1e"):
            raw = raw.strip("\n")
            if not raw:
                continue
            header, *path_lines = raw.splitlines()
            parts = header.split("\x1f", 3)
            if len(parts) != 4:
                errors.append(f"malformed commit evidence: {root}")
                continue
            commit_sha, committed_at, authored_at, subject = parts
            artifacts = sorted({line.strip() for line in path_lines if line.strip()})
            records.append(
                {
                    "source": "git_commit",
                    "machine": session_result.get("machine"),
                    "id": f"{root}:{commit_sha}",
                    "commit_sha": commit_sha,
                    "repository_root": root,
                    "cwd": root,
                    "start": committed_at,
                    "end": committed_at,
                    "authored_at": authored_at,
                    "subject": subject,
                    "artifacts": artifacts,
                    "evidence_level": "immutable git commit metadata",
                }
            )
    records.sort(key=lambda value: (str(value.get("start")), str(value.get("commit_sha"))))
    return records, sorted(roots), errors


def collect_local_sessions(machine: dict[str, Any], since: dt.datetime, until: dt.datetime) -> dict[str, Any]:
    result = {"machine": machine["name"], "status": "ok",
              "claude_bursts": [], "hermes_sessions": [], "hermes_db_sessions": [],
              "codex_sessions": [], "repository_events": [], "errors": []}
    cbase = machine.get("claude_projects")
    if cbase and Path(cbase).exists():
        for p in Path(cbase).glob("*/*.jsonl"):
            result["claude_bursts"].extend(parse_claude_jsonl_file(p, cbase, since, until, machine["name"]))
    else:
        result["errors"].append(f"claude_projects not found: {cbase}")
    hbase = machine.get("hermes_sessions")
    if hbase and Path(hbase).exists():
        for p in Path(hbase).glob("request_dump_*.json"):
            try:
                obj = json.loads(p.read_text(errors="ignore"))
                start = parse_dt(obj.get("session_start"))
                last = parse_dt(obj.get("last_updated"))
                touch = last or start
                if not (touch and since <= touch.astimezone(BUCHAREST) < until):
                    continue
                msgs = obj.get("messages", [])
                user_count = sum(1 for m in msgs if m.get("role") == "user")
                total_count = obj.get("message_count") or len(msgs)
                if start and last:
                    real_span_td = (last - start).total_seconds() / 3600
                else:
                    real_span_td = 0
                if real_span_td >= 4:
                    est_minutes = int(user_count * 7 + (total_count - user_count) * 0.5)
                    estimate_start = start or touch
                    estimate_end = estimate_start + dt.timedelta(minutes=est_minutes)
                    evidence = f"session spans {real_span_td:.1f}h but has only {user_count} user msgs; estimated {est_minutes}m via content heuristic"
                else:
                    estimate_start = start
                    estimate_end = last
                    evidence = f"session_start/last_updated used (span={real_span_td:.1f}h, {user_count} user msgs)"
                result["hermes_sessions"].append({
                    "source": "hermes_legacy",
                    "machine": machine["name"],
                    "session_id": obj.get("session_id") or p.stem,
                    "path": str(p),
                    "start": local_dt_string(estimate_start),
                    "end": local_dt_string(estimate_end),
                    "real_span_hours": round(real_span_td, 2),
                    "user_messages": user_count,
                    "total_messages": total_count,
                    "model": obj.get("model"),
                    "platform": obj.get("platform"),
                    "evidence_level": evidence,
                    "events": [
                        {
                            "timestamp": local_dt_string(parse_dt(message.get("timestamp"))),
                            "role": str(message.get("role") or "unknown"),
                            "kind": "message",
                            "content": str(message.get("content") or ""),
                            "ordinal": index,
                        }
                        for index, message in enumerate(msgs)
                        if isinstance(message, dict)
                    ],
                })
            except Exception as e:
                result["errors"].append(f"hermes legacy parse failed {p.name}: {e}")
    else:
        result["errors"].append(f"hermes_sessions not found: {hbase}")
    hdb = machine.get("hermes_db")
    if hdb and Path(hdb).exists():
        result["hermes_db_sessions"] = collect_hermes_db_sessions(hdb, machine["name"], since, until)
    else:
        result["errors"].append(f"hermes_db not found: {hdb}")
    codex_home = machine.get("codex_home")
    if codex_home and Path(codex_home).exists():
        result["codex_sessions"] = collect_codex_sessions(codex_home, machine["name"], since, until)
    else:
        result["errors"].append(f"codex_home not found: {codex_home}")
    repository_events, repository_roots, repository_errors = collect_repository_events(
        result, since, until
    )
    result["repository_events"] = repository_events
    result["repository_roots"] = repository_roots
    result["repository_evidence_status"] = "partial" if repository_errors else "complete"
    result["errors"].extend(repository_errors)
    if result["errors"]:
        result["status"] = "partial"
    return result


def _remote_claude_contract() -> str:
    """Remote stdlib-only Claude parser, kept behaviorally aligned with local bursts."""
    return r'''def skip_path(p):
    return any(f in str(p) for f in ['/subagents/','/multica/','multica-runtime','claude-mem-observer','multica-command'])
def one_line(v, limit=320):
    v=' '.join(str(v or '').split())
    if len(v)<=limit: return v
    cut=v[:limit-3].rsplit(' ',1)[0]
    return (cut or v[:limit-3]).rstrip()+'...'
def system_message(v):
    v=one_line(v,2000).lower()
    if not v: return True
    if v.startswith(('<command-','<local-command-','<teammate-message','[tool_','[image:','[thinking]','![','/home/','/users/')): return True
    if v.startswith('<') and '>' in v[:80]: return True
    return any(marker in v for marker in ('permissions instructions','codex agent history','<app-context>','# agents.md instructions'))
def clean_context(v):
    lines=[]
    for line in str(v or '').strip().splitlines():
        line=line.strip()
        if line.startswith('<') and line.endswith('>'): continue
        lines.append(line)
    v='\n'.join(lines)
    return '' if system_message(v) else one_line(v)
def message_text(content):
    if isinstance(content,str): return content
    if not isinstance(content,list): return str(content or '')
    return '\n'.join(str(item.get('text','')) for item in content if isinstance(item,dict) and item.get('type')=='text')
def active_duration(ts):
    if len(ts)<=1: return 0,'single message — skipped'
    total=240; engaged=0; sporadic=0
    for i in range(1,len(ts)):
        gap=(ts[i]-ts[i-1]).total_seconds()
        if gap<=120:
            total+=gap; engaged+=1
        else:
            total+=240; sporadic+=1
    return max(1,round(total/60)), 'computed: '+str(engaged)+' engaged + '+str(sporadic)+' sporadic segments'
def parse_claude(p):
    if skip_path(p): return []
    events=[]
    try:
        for line in Path(p).read_text(errors='ignore').splitlines():
            try: o=json.loads(line)
            except Exception: continue
            t=parse_dt(o.get('timestamp'))
            role=o.get('type')
            if not t or role not in ('user','assistant'): continue
            events.append({'timestamp':t.astimezone(BUCHAREST),'role':role,'content':message_text(o.get('message',{}).get('content',''))})
    except Exception: return []
    users=sorted((event for event in events if event['role']=='user'), key=lambda event:event['timestamp'])
    if not users: return []
    bursts=[]
    for user in users:
        if not bursts or (user['timestamp']-bursts[-1][-1]['timestamp']).total_seconds()>1800: bursts.append([user])
        else: bursts[-1].append(user)
    for event in events:
        if event['role']!='assistant': continue
        for i,burst in enumerate(bursts):
            next_start=bursts[i+1][0]['timestamp'] if i+1<len(bursts) else None
            last_user=burst[-1]['timestamp']
            if event['timestamp']>=burst[0]['timestamp'] and event['timestamp']<=last_user+dt.timedelta(seconds=1800) and (next_start is None or event['timestamp']<next_start):
                burst.append(event); break
    out=[]
    for burst in bursts:
        burst.sort(key=lambda event:event['timestamp'])
        first_user=''; last_assistant=''; user_ts=[]
        for event in burst:
            if event['role']=='user':
                user_ts.append(event['timestamp'])
                candidate=clean_context(event['content'])
                if not first_user and len(candidate)>=10: first_user=candidate
            else:
                candidate=clean_context(event['content'])
                if candidate: last_assistant=candidate
        if not user_ts: continue
        bs,be=user_ts[0],user_ts[-1]
        if be<SINCE or bs>=UNTIL: continue
        raw=max(1,int((be-bs).total_seconds()/60))
        active,method=active_duration(user_ts)
        out.append({'start':local_str(bs),'end':local_str(be),'duration_minutes':min(active,raw),'raw_wallclock_minutes':raw,'user_messages':len(user_ts),'label':label(p,CBASE),'session_id':Path(p).stem,'path':str(p),'first_user_message':first_user,'last_assistant_message':last_assistant,'evidence_level':method,'heartbeat_like':all(t.minute in (29,44) for t in user_ts),'source':'claude','machine':MACHINE})
    return out
'''


def _remote_codex_contract() -> str:
    """Remote Codex extraction, using the same row-local user-burst context contract."""
    return r'''try:
    if CXBASE and Path(CXBASE).exists():
        db=Path(CXBASE)/'state_5.sqlite'
        if db.exists():
            import sqlite3
            conn=sqlite3.connect('file:'+str(db)+'?mode=ro', uri=True)
            lo=int((SINCE-dt.timedelta(days=1)).timestamp())
            rows=conn.execute('SELECT id, rollout_path, cwd, title, first_user_message, thread_source, archived FROM threads WHERE updated_at >= ? ORDER BY updated_at', (lo,)).fetchall()
            conn.close()
            for sid, rollout_path, cwd, session_title, first_msg, thread_source, archived in rows:
                if thread_source=='subagent' or not rollout_path or not Path(rollout_path).exists(): continue
                try:
                    lines=Path(rollout_path).read_text(errors='ignore').splitlines()
                    if not lines or json.loads(lines[0]).get('type')!='session_meta': continue
                    events=[]
                    for line in lines[1:]:
                        try: o=json.loads(line)
                        except Exception: continue
                        if o.get('type')!='event_msg': continue
                        p=o.get('payload',{}); kind=p.get('type'); t=parse_dt(o.get('timestamp'))
                        if kind not in ('user_message','agent_message') or not t: continue
                        events.append({'timestamp':t.astimezone(BUCHAREST),'role':'user' if kind=='user_message' else 'assistant','content':p.get('message','')})
                    users=sorted((event for event in events if event['role']=='user'), key=lambda event:event['timestamp'])
                    if not users: continue
                    bursts=[]
                    for user in users:
                        if not bursts or (user['timestamp']-bursts[-1][-1]['timestamp']).total_seconds()>1800: bursts.append([user])
                        else: bursts[-1].append(user)
                    for event in events:
                        if event['role']!='assistant': continue
                        for i,burst in enumerate(bursts):
                            next_start=bursts[i+1][0]['timestamp'] if i+1<len(bursts) else None
                            last_user=burst[-1]['timestamp']
                            if event['timestamp']>=burst[0]['timestamp'] and event['timestamp']<=last_user+dt.timedelta(seconds=1800) and (next_start is None or event['timestamp']<next_start):
                                burst.append(event); break
                    for burst in bursts:
                        burst.sort(key=lambda event:event['timestamp'])
                        first_user=''; last_assistant=''; user_ts=[]
                        for event in burst:
                            if event['role']=='user':
                                user_ts.append(event['timestamp']); candidate=clean_context(event['content'])
                                if not first_user and len(candidate)>=10: first_user=candidate
                            else:
                                candidate=clean_context(event['content'])
                                if candidate: last_assistant=candidate
                        if not user_ts: continue
                        bs,be=user_ts[0],user_ts[-1]; raw=max(1,int((be-bs).total_seconds()/60)); active,method=active_duration(user_ts)
                        if be<SINCE or bs>=UNTIL: continue
                        title=first_user or last_assistant or ''
                        res['codex_sessions'].append({'source':'codex','machine':MACHINE,'session_id':sid,'path':str(rollout_path),'cwd':cwd or '','title':title,'start':local_str(bs),'end':local_str(be),'duration_minutes':min(active,raw),'raw_wallclock_minutes':raw,'user_messages':len(user_ts),'first_user_message':first_user,'last_assistant_message':last_assistant,'evidence_level':method,'archived':bool(archived)})
                except Exception: pass
        else:
            res['errors'].append('codex state_5.sqlite not found')
    else:
        res['errors'].append('codex_home not found: '+CXBASE)
except Exception as e: res['errors'].append('codex scan: '+str(e)[:200])
'''


def collect_remote_sessions(
    machine: dict[str, Any],
    since: dt.datetime,
    until: dt.datetime,
    ssh_options: list[str],
    *,
    coordinator_identity: dict[str, Any] | None = None,
) -> dict[str, Any]:
    host = machine["host"]
    result = {"machine": machine["name"], "status": "unavailable",
              "claude_bursts": [], "hermes_sessions": [], "hermes_db_sessions": [],
              "codex_sessions": [], "repository_events": [], "errors": []}
    collector_root = str(machine.get("collector_root") or "").strip()
    if collector_root:
        canonical_timeout = canonical_export_timeout_seconds()
        expected_script_digest = collector_script_sha256()
        coordinator = coordinator_identity or collector_runtime_identity()
        coordinator_git_sha = str(coordinator.get("git_sha") or "").strip()
        script = str(Path(collector_root) / "scripts" / "clockify_sync_collect.py")
        def canonical_command_for(expected_digest: str) -> str:
            remote_parts = [
                "python3",
                script,
                "export-local",
                "--machine-json",
                json.dumps(machine, ensure_ascii=False, separators=(",", ":")),
                "--since",
                since.isoformat(),
                "--until",
                until.isoformat(),
                "--expected-collector-sha256",
                expected_digest,
                "--encoded-output",
            ]
            if coordinator_git_sha:
                remote_parts.extend(["--coordinator-git-sha", coordinator_git_sha])
            return " ".join(shlex.quote(part) for part in remote_parts)

        def run_canonical(expected_digest: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                ["ssh", *ssh_options, host, canonical_command_for(expected_digest)],
                capture_output=True,
                text=True,
                timeout=canonical_timeout,
            )

        try:
            negotiated_digest = expected_script_digest
            canonical = run_canonical(negotiated_digest)
            if canonical.returncode == 0:
                exported = canonical_export_payload(canonical.stdout, machine["name"])
                if exported is None:
                    result["errors"].append(
                        "canonical remote evidence export returned an invalid attestation payload; no legacy metadata fallback used"
                    )
                    result["canonical_export"] = {
                        "status": "invalid_payload",
                        "returncode": canonical.returncode,
                        "stdout_bytes": len(canonical.stdout.encode("utf-8", errors="replace")),
                    }
                    return result
                if isinstance(exported, dict):
                    attestation = exported.get("canonical_export_attestation")
                    if not isinstance(attestation, dict):
                        result["errors"].append(
                            "canonical remote evidence export missing code attestation; no legacy metadata fallback used"
                        )
                        return result
                    remote_digest = str(attestation.get("collector_script_sha256") or "")
                    runtime = attestation.get("runtime_identity")
                    if (
                        remote_digest != negotiated_digest
                        and remote_digest in COMPATIBLE_CANONICAL_EXPORT_DIGESTS
                    ):
                        # The first request is an attestation-only handshake.
                        # Rerun only an explicitly allowlisted exporter digest;
                        # arbitrary remote code never receives evidence-export
                        # authority from its self-reported checksum.
                        negotiated_digest = remote_digest
                        canonical = run_canonical(negotiated_digest)
                        if canonical.returncode != 0:
                            result["errors"].append(
                                "compatible canonical remote evidence export unavailable; no legacy metadata fallback used"
                            )
                            return result
                        exported = canonical_export_payload(
                            canonical.stdout, machine["name"]
                        )
                        if not isinstance(exported, dict):
                            result["errors"].append(
                                "compatible canonical remote evidence export returned an invalid attestation payload; no legacy metadata fallback used"
                            )
                            return result
                        attestation = exported.get("canonical_export_attestation")
                        if not isinstance(attestation, dict):
                            result["errors"].append(
                                "compatible canonical remote evidence export missing code attestation; no legacy metadata fallback used"
                            )
                            return result
                        remote_digest = str(
                            attestation.get("collector_script_sha256") or ""
                        )
                        runtime = attestation.get("runtime_identity")
                    if remote_digest != negotiated_digest:
                        result["errors"].append(
                            "canonical remote evidence export script digest mismatch; no legacy metadata fallback used"
                        )
                        return result
                    if not isinstance(runtime, dict):
                        result["errors"].append(
                            "canonical remote evidence export missing runtime identity; no legacy metadata fallback used"
                        )
                        return result
                    remote_git_sha = str(runtime.get("git_sha") or "").strip()
                    remote_git_dirty = runtime.get("git_dirty")
                    if remote_git_sha:
                        if not isinstance(remote_git_dirty, bool):
                            result["errors"].append(
                                "canonical remote evidence export missing Git worktree state; no legacy metadata fallback used"
                            )
                            return result
                        git_sha_match = bool(
                            coordinator_git_sha
                            and remote_git_sha == coordinator_git_sha
                        )
                        # The exporter is a standalone script. Its exact content
                        # digest, not an unrelated repository commit, is the
                        # executable compatibility boundary. This permits a
                        # clean fleet host to lag on analyzer-only commits while
                        # still failing closed on exporter drift.
                        if remote_git_dirty:
                            bundle_provenance = (
                                "git_worktree_content_attested_dirty"
                            )
                        else:
                            bundle_provenance = (
                                "git_worktree"
                                if git_sha_match
                                else "git_worktree_content_attested"
                            )
                    else:
                        git_sha_match = None
                        bundle_provenance = "non_git_bundle"
                    exported["collector_contract"] = "canonical_export_v1"
                    exported["canonical_export"] = {
                        "timeout_seconds": canonical_timeout,
                        "provenance": "full_context_remote_export",
                        "bundle_provenance": bundle_provenance,
                        "collector_script_sha256": remote_digest,
                        "coordinator_collector_script_sha256": expected_script_digest,
                        "collector_digest_match": (
                            remote_digest == expected_script_digest
                        ),
                        "coordinator_git_sha": coordinator_git_sha or None,
                        "remote_git_sha": remote_git_sha or None,
                        "remote_git_dirty": remote_git_dirty,
                        "git_sha_match": git_sha_match,
                    }
                    return exported
            result["errors"].append(
                "canonical remote evidence export unavailable; legacy metadata fallback used"
            )
        except subprocess.TimeoutExpired:
            result["errors"].append(
                "canonical remote evidence export timed out; no legacy metadata fallback used"
            )
            result["canonical_export"] = {
                "timeout_seconds": canonical_timeout,
                "provenance": "full_context_remote_export",
                "status": "timed_out",
            }
            return result
        except Exception as exc:
            result["errors"].append(
                f"canonical remote evidence export unavailable ({type(exc).__name__}); "
                "legacy metadata fallback used"
            )
    remote_code = "\nimport datetime as dt, json\nfrom pathlib import Path\nBUCHAREST=dt.timezone(dt.timedelta(hours=3))\nMACHINE=__MACHINE__\nCBASE=__CBASE__\nHBASE=__HBASE__\nHDB=__HDB__\nCXBASE=__CXBASE__\nSINCE=dt.datetime.fromisoformat(__SINCE__)\nUNTIL=dt.datetime.fromisoformat(__UNTIL__)\ndef parse_dt(s):\n    if not s: return None\n    try:\n        x=dt.datetime.fromisoformat(str(s).replace('Z','+00:00'))\n        return x if x.tzinfo else x.replace(tzinfo=BUCHAREST)\n    except Exception: return None\ndef local_str(x):\n    return x.astimezone(BUCHAREST).strftime('%Y-%m-%d %H:%M') if x else None\ndef label(path, base):\n    rel=str(path).replace(base,'').lstrip('/')\n    top=rel.split('/')[0]\n    for pref in ['-Users-blackthorne-Work-','-Users-blackthorne-','-home-blackthorne-Work-','-home-blackthorne-']:\n        if top.startswith(pref): return top[len(pref):]\n    return top\ndef skip_path(p):\n    return any(f in str(p) for f in ['/subagents/','multica-runtime','claude-mem-observer','multica-command'])\ndef parse_claude(p):\n    if skip_path(p): return []\n    first_user=''; last_assistant=''; ts=[]\n    try:\n        for line in Path(p).read_text(errors='ignore').splitlines():\n            try: o=json.loads(line)\n            except Exception: continue\n            t=parse_dt(o.get('timestamp'))\n            if not t or not (SINCE <= t.astimezone(BUCHAREST) < UNTIL): continue\n            msg_type=o.get('type')\n            c=o.get('message',{}).get('content','')\n            if isinstance(c,list):\n                text_parts=[]\n                for item in c:\n                    if isinstance(item,dict):\n                        if item.get('type')=='text': text_parts.append(item.get('text',''))\n                        elif item.get('type')=='tool_use': text_parts.append('[tool_use: '+item.get('name','?')+']')\n                        elif item.get('type')=='tool_result': text_parts.append('[tool_result]')\n                        elif item.get('type')=='thinking': text_parts.append('[thinking]')\n                        elif item.get('type')=='tool_reference': text_parts.append('[tool_ref: '+item.get('tool_name','?')+']')\n                c='\\n'.join(text_parts)\n            c=str(c)\n            ts.append(t.astimezone(BUCHAREST))\n            if msg_type=='user' and not first_user: first_user=c[:300]\n            if msg_type=='assistant': last_assistant=c[:300]\n    except Exception: return []\n    if not ts: return []\n    ts.sort()\n    bursts=[]\n    start=end=ts[0]\n    count=1\n    for t in ts[1:]:\n        if (t-end).total_seconds()>1800:\n            bursts.append((start,end,count))\n            start=t; count=1\n        else: count+=1\n        end=t\n    bursts.append((start,end,count))\n    out=[]\n    for bs,be,cnt in bursts:\n        gap=(be-bs).total_seconds()\n        raw_wallclock=max(1,int(gap/60))\n        if gap>600:\n            active=int(cnt*2.5)\n            method='content heuristic'\n        else:\n            active=max(1,int(gap/60))\n            method='wall clock'\n        out.append({'start':local_str(bs),'end':local_str(be),'duration_minutes':min(active,raw_wallclock),'raw_wallclock_minutes':raw_wallclock,'user_messages':cnt,'label':label(p,CBASE),'first_user_message':first_user[:200],'last_assistant_message':last_assistant[:200],'evidence_level':method,'heartbeat_like':False,'source':'claude','machine':MACHINE})\n    return out\nres={'machine':MACHINE,'status':'ok','claude_bursts':[],'hermes_sessions':[],'hermes_db_sessions':[],'codex_sessions':[],'errors':[]}\ntry:\n    if CBASE and Path(CBASE).exists():\n        for p in Path(CBASE).glob('*/*.jsonl'):\n            res['claude_bursts'].extend(parse_claude(p))\n    else:\n        res['errors'].append('claude_projects not found: '+CBASE)\nexcept Exception as e: res['errors'].append('claude scan: '+str(e)[:200])\ntry:\n    if HBASE and Path(HBASE).exists():\n        for p in Path(HBASE).glob('request_dump_*.json'):\n            try:\n                obj=json.loads(p.read_text(errors='ignore'))\n                start=parse_dt(obj.get('session_start'))\n                last=parse_dt(obj.get('last_updated'))\n                touch=last or start\n                if not (touch and SINCE <= touch.astimezone(BUCHAREST) < UNTIL): continue\n                msgs=obj.get('messages',[])\n                user_count=sum(1 for m in msgs if m.get('role')=='user')\n                total_count=obj.get('message_count') or len(msgs)\n                if start and last:\n                    real_span_td=(last-start).total_seconds()/3600\n                else: real_span_td=0\n                if real_span_td>=4:\n                    est_minutes=int(user_count*7+(total_count-user_count)*0.5)\n                    est_start=start or touch\n                    est_end=est_start+dt.timedelta(minutes=est_minutes)\n                    evidence='session spans '+str(round(real_span_td,1))+'h but has only '+str(user_count)+' user msgs; estimated '+str(est_minutes)+'m via content heuristic'\n                else:\n                    est_start=start; est_end=last\n                    evidence='session_start/last_updated used (span='+str(round(real_span_td,1))+'h, '+str(user_count)+' user msgs)'\n                res['hermes_sessions'].append({'source':'hermes_legacy','machine':MACHINE,'session_id':obj.get('session_id') or p.stem,'path':str(p),'start':local_str(est_start),'end':local_str(est_end),'real_span_hours':round(real_span_td,2),'user_messages':user_count,'total_messages':total_count,'model':obj.get('model'),'platform':obj.get('platform'),'evidence_level':evidence})\n            except Exception as e: res['errors'].append('hermes legacy parse failed '+p.name+': '+str(e)[:200])\n    else:\n        res['errors'].append('hermes_sessions not found: '+HBASE)\nexcept Exception as e: res['errors'].append('hermes scan: '+str(e)[:200])\ntry:\n    if HDB and Path(HDB).exists():\n        import sqlite3\n        conn=sqlite3.connect(HDB)\n        since_ts=SINCE.timestamp()\n        until_ts=UNTIL.timestamp()\n        rows=conn.execute('SELECT id, started_at, ended_at, message_count, model, cwd, estimated_cost_usd, title, input_tokens, output_tokens FROM sessions WHERE started_at >= ? AND started_at < ? ORDER BY started_at', (since_ts, until_ts)).fetchall()\n        for row in rows:\n            sid, started_at, ended_at, msg_count, model, cwd, cost, title, in_tok, out_tok = row\n            if not started_at: continue\n            start_dt=dt.datetime.fromtimestamp(started_at, tz=BUCHAREST)\n            end_dt=dt.datetime.fromtimestamp(ended_at, tz=BUCHAREST) if ended_at else None\n            duration_m=int((end_dt-start_dt).total_seconds()/60) if end_dt else None\n            first_msg=conn.execute('SELECT content FROM messages WHERE session_id = ? AND role = \"user\" ORDER BY timestamp LIMIT 1', (sid,)).fetchone()\n            first_content=(first_msg[0][:300] if first_msg and first_msg[0] else '') if first_msg else ''\n            res['hermes_db_sessions'].append({'source':'hermes_db','machine':MACHINE,'session_id':sid,'start':local_str(start_dt),'end':local_str(end_dt) if end_dt else None,'duration_minutes':duration_m,'message_count':msg_count,'model':model,'cwd':cwd or '','estimated_cost_usd':cost or 0.0,'input_tokens':in_tok or 0,'output_tokens':out_tok or 0,'title':title or '','first_user_message':first_content})\n        conn.close()\n    else:\n        res['errors'].append('hermes_db not found: '+HDB)\nexcept Exception as e: res['errors'].append('hermes_db scan: '+str(e)[:200])\ntry:\n    if CXBASE and Path(CXBASE).exists():\n        db=Path(CXBASE)/'state_5.sqlite'\n        if db.exists():\n            import sqlite3\n            conn=sqlite3.connect('file:'+str(db)+'?mode=ro', uri=True)\n            lo=int((SINCE-dt.timedelta(days=1)).timestamp())\n            rows=conn.execute('SELECT id, rollout_path, cwd, title, first_user_message, thread_source, archived FROM threads WHERE updated_at >= ? ORDER BY updated_at', (lo,)).fetchall()\n            conn.close()\n            for sid, rollout_path, cwd, title, first_msg, thread_source, archived in rows:\n                if thread_source=='subagent': continue\n                if not rollout_path or not Path(rollout_path).exists(): continue\n                try:\n                    lines=Path(rollout_path).read_text(errors='ignore').splitlines()\n                    if not lines: continue\n                    meta=json.loads(lines[0])\n                    if meta.get('type')!='session_meta': continue\n                    ts_list=[]\n                    for line in lines[1:]:\n                        try: o=json.loads(line)\n                        except: continue\n                        if o.get('type')!='event_msg': continue\n                        p=o.get('payload',{})\n                        if p.get('type')!='user_message': continue\n                        t=parse_dt(o.get('timestamp'))\n                        if t and SINCE <= t.astimezone(BUCHAREST) < UNTIL: ts_list.append(t.astimezone(BUCHAREST))\n                    if not ts_list: continue\n                    ts_list.sort()\n                    bursts=[]\n                    start=end=ts_list[0]\n                    count=1\n                    for t in ts_list[1:]:\n                        if (t-end).total_seconds()>1800:\n                            bursts.append((start,end,count))\n                            start=t; count=1\n                        else: count+=1\n                        end=t\n                    bursts.append((start,end,count))\n                    for bs,be,cnt in bursts:\n                        gap=(be-bs).total_seconds()\n                        active=max(1,int(gap/60)) if gap<=600 else int(cnt*2.5)\n                        res['codex_sessions'].append({'source':'codex','machine':MACHINE,'session_id':sid,'cwd':cwd or '','title':title or first_msg or '','start':local_str(bs),'end':local_str(be),'duration_minutes':active,'user_messages':cnt,'archived':bool(archived)})\n                except Exception: pass\n        else:\n            res['errors'].append('codex state_5.sqlite not found')\n    else:\n        res['errors'].append('codex_home not found: '+CXBASE)\nexcept Exception as e: res['errors'].append('codex scan: '+str(e)[:200])\nprint(json.dumps(res))\n"
    remote_code = remote_code.replace(
        "from pathlib import Path\nBUCHAREST=dt.timezone(dt.timedelta(hours=3))",
        "from pathlib import Path\nfrom zoneinfo import ZoneInfo\n"
        "BUCHAREST=ZoneInfo('Europe/Bucharest')",
    )
    remote_code = re.sub(
        r"def skip_path\(p\):.*?(?=res=\{)",
        lambda _match: _remote_claude_contract(),
        remote_code,
        flags=re.DOTALL,
    )
    remote_code = re.sub(
        r"try:\n    if CXBASE.*?(?=print\(json\.dumps\(res\)\))",
        lambda _match: _remote_codex_contract(),
        remote_code,
        flags=re.DOTALL,
    )
    remote_code = (remote_code
        .replace('__MACHINE__', repr(machine['name']))
        .replace('__CBASE__', repr(machine.get('claude_projects', '')))
        .replace('__HBASE__', repr(machine.get('hermes_sessions', '')))
        .replace('__HDB__', repr(machine.get('hermes_db', '')))
        .replace('__CXBASE__', repr(machine.get('codex_home', '')))
        .replace('__SINCE__', repr(since.isoformat()))
        .replace('__UNTIL__', repr(until.isoformat())))
    cmd = ["ssh", *ssh_options, host, "python3 - <<'PY'\n" + remote_code + "\nPY"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        if proc.returncode != 0:
            result["errors"].append(proc.stderr.strip()[:500] or proc.stdout.strip()[:500])
            return result
        legacy = json.loads(proc.stdout.strip().splitlines()[-1])
        legacy["status"] = "partial"
        legacy.setdefault("errors", []).extend(result["errors"])
        legacy["collector_contract"] = "legacy_metadata_fallback"
        legacy.setdefault("repository_events", [])
        legacy["repository_evidence_status"] = "unavailable"
        return legacy
    except Exception as e:
        result["errors"].append(str(e))
    return result


def machine_is_local(machine: dict[str, Any], hostname: str | None = None) -> bool:
    """Resolve a fleet entry locally on its own host and through SSH elsewhere."""
    kind = str(machine.get("kind") or "ssh").lower()
    if kind == "local":
        return True
    if kind != "auto":
        return False

    current = (hostname or socket.gethostname()).lower().rstrip(".")
    current_short = current.split(".", 1)[0]
    aliases = {
        str(machine.get("name") or "").lower().rstrip("."),
        str(machine.get("host") or "").lower().rstrip("."),
        str(machine.get("host") or "").lower().split(".", 1)[0],
    }
    aliases.update(
        str(alias).lower().rstrip(".")
        for alias in machine.get("local_hostnames", [])
        if alias
    )
    alias_shorts = {alias.split(".", 1)[0] for alias in aliases}
    return current in aliases or current_short in alias_shorts



def collect_hermes_db_sessions(db_path: str, machine: str, since: dt.datetime, until: dt.datetime) -> list[dict[str, Any]]:
    """Query Hermes state.db for sessions with timestamps, messages, model, and cost data."""
    out: list[dict[str, Any]] = []
    try:
        import sqlite3, time
        conn = sqlite3.connect(db_path)
        since_ts = since.timestamp()
        until_ts = until.timestamp()
        rows = conn.execute("""
            SELECT id, started_at, ended_at, message_count, model, cwd,
                   estimated_cost_usd, title, input_tokens, output_tokens
            FROM sessions
            WHERE started_at >= ? AND started_at < ?
            ORDER BY started_at
        """, (since_ts, until_ts)).fetchall()
        for row in rows:
            sid, started_at, ended_at, msg_count, model, cwd, cost, title, in_tok, out_tok = row
            if not started_at:
                continue
            start_dt = dt.datetime.fromtimestamp(started_at, tz=BUCHAREST)
            end_dt = dt.datetime.fromtimestamp(ended_at, tz=BUCHAREST) if ended_at else None
            duration_m = int((end_dt - start_dt).total_seconds() / 60) if end_dt else None
            first_msg = conn.execute(
                "SELECT content FROM messages WHERE session_id = ? AND role = 'user' ORDER BY timestamp LIMIT 1",
                (sid,)
            ).fetchone()
            first_content = (first_msg[0][:300] if first_msg and first_msg[0] else "") if first_msg else ""
            message_rows = conn.execute(
                "SELECT role, timestamp, content, tool_name FROM messages "
                "WHERE session_id = ? ORDER BY timestamp",
                (sid,),
            ).fetchall()
            events = []
            for index, (role, timestamp, content, tool_name) in enumerate(message_rows):
                event_dt = (
                    dt.datetime.fromtimestamp(timestamp, tz=BUCHAREST)
                    if timestamp
                    else None
                )
                events.append(
                    {
                        "timestamp": local_dt_string(event_dt),
                        "role": str(role or "unknown"),
                        "kind": "tool" if tool_name else "message",
                        "content": str(content or ""),
                        "tool_name": str(tool_name or ""),
                        "ordinal": index,
                    }
                )
            out.append({
                "source": "hermes_db",
                "machine": machine,
                "session_id": sid,
                "start": local_dt_string(start_dt),
                "end": local_dt_string(end_dt) if end_dt else None,
                "duration_minutes": duration_m,
                "message_count": msg_count,
                "model": model,
                "cwd": cwd or "",
                "estimated_cost_usd": cost or 0.0,
                "input_tokens": in_tok or 0,
                "output_tokens": out_tok or 0,
                "title": title or "",
                "first_user_message": first_content,
                "events": events,
            })
        conn.close()
    except Exception as e:
        out.append({"source": "hermes_db", "machine": machine, "error": str(e)})
    return out


def parse_codex_rollout_file(path: Path, machine: str, since: dt.datetime, until: dt.datetime,
                             cwd_override: str | None = None, title_override: str | None = None) -> list[dict[str, Any]]:
    """Parse a Codex rollout JSONL file into burst records.

    Codex Desktop/CLI stores live sessions as rollout-*.jsonl under
    sessions/YYYY/MM/DD/. Line 0 is session_meta (cwd, originator). User turns are
    event_msg payloads of type 'user_message'. We build bursts from those
    timestamps with the same 30-min gap + engagement-aware duration as Claude.
    """
    out: list[dict[str, Any]] = []
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except Exception:
        return out
    if not lines:
        return out
    try:
        meta = json.loads(lines[0])
    except Exception:
        return out
    if meta.get("type") != "session_meta":
        return out
    payload = meta.get("payload", {})
    sid = payload.get("id", path.stem)
    cwd = cwd_override if cwd_override is not None else payload.get("cwd", "")
    originator = payload.get("originator", "")
    model_provider = payload.get("model_provider", "")
    # Skip subagent sessions — these are agent-internal, analogous to Claude /subagents/
    if payload.get("thread_source") == "subagent" or (isinstance(payload.get("source"), dict) and payload["source"].get("subagent")):
        return out

    events: list[dict[str, Any]] = []
    for line in lines[1:]:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        t = parse_dt(obj.get("timestamp"))
        if not t:
            continue
        p = obj.get("payload", {})
        if obj.get("type") == "event_msg":
            event_type = p.get("type")
            if event_type not in ("user_message", "agent_message"):
                continue
            events.append({
                "timestamp": t.astimezone(BUCHAREST),
                "role": "user" if event_type == "user_message" else "assistant",
                "kind": "message",
                "content": p.get("message", ""),
            })
        elif obj.get("type") == "response_item":
            item = p.get("item") if isinstance(p.get("item"), dict) else p
            item_type = str(item.get("type") or "")
            if item_type in {"function_call", "custom_tool_call", "tool_call"}:
                events.append(
                    {
                        "timestamp": t.astimezone(BUCHAREST),
                        "role": "assistant",
                        "kind": "tool_call",
                        "tool_name": str(item.get("name") or item.get("tool_name") or "unknown"),
                        "content": str(item.get("arguments") or item.get("input") or ""),
                    }
                )
            elif item_type in {"function_call_output", "custom_tool_call_output", "tool_result"}:
                events.append(
                    {
                        "timestamp": t.astimezone(BUCHAREST),
                        "role": "tool",
                        "kind": "tool_result",
                        "tool_name": str(item.get("name") or item.get("call_id") or ""),
                        "content": str(item.get("output") or item.get("content") or ""),
                    }
                )
    if not events:
        return out
    for burst_events in _partition_bursts(events):
        first_user, last_assistant, burst_ts = _burst_context(burst_events)
        if not burst_ts:
            continue
        bs, be = burst_ts[0], burst_ts[-1]
        if not _window_overlaps(bs, be, since, until):
            continue
        cnt = len(burst_ts)
        raw_wallclock = max(1, int((be - bs).total_seconds() / 60))
        active_min, method, _segments = compute_active_duration(burst_ts)
        # The first real user context is per-burst, never the session-wide title.
        title = first_user or _meaningful_context(title_override or "")
        label = cwd.rstrip("/").split("/")[-1] if cwd else (title or "codex")
        out.append({
            "source": "codex",
            "machine": machine,
            "session_id": sid,
            "path": str(path),
            "cwd": cwd,
            "label": label,
            "title": title,
            "originator": originator,
            "model_provider": model_provider,
            "start": local_dt_string(bs),
            "end": local_dt_string(be),
            "duration_minutes": min(active_min, raw_wallclock),
            "raw_wallclock_minutes": raw_wallclock,
            "user_messages": cnt,
            "evidence_level": method,
            "first_user_message": first_user,
            "last_assistant_message": last_assistant,
            "events": _serialized_events(burst_events),
        })
    return out


def collect_codex_sessions_from_db(codex_home: str, machine: str, since: dt.datetime, until: dt.datetime) -> list[dict[str, Any]] | None:
    """Enumerate Codex sessions from state_5.sqlite (the authoritative thread index).

    The threads table carries id, rollout_path (live OR archived), cwd, title,
    first_user_message, thread_source — the cleanest source for routing + descriptions.
    We still parse each rollout_path for per-user-message timestamps to compute real
    burst durations. Returns None if the DB is missing so the caller can fall back.
    """
    db = Path(codex_home) / "state_5.sqlite"
    if not db.exists():
        return None
    out: list[dict[str, Any]] = []
    try:
        import sqlite3
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        # updated_at is epoch seconds; widen the lower bound a little to catch
        # sessions that started before `since` but whose bursts fall inside the window.
        lo = int((since - dt.timedelta(days=1)).timestamp())
        rows = conn.execute(
            "SELECT id, rollout_path, cwd, title, first_user_message, thread_source, archived "
            "FROM threads WHERE updated_at >= ? ORDER BY updated_at",
            (lo,),
        ).fetchall()
        conn.close()
    except Exception:
        return None
    for sid, rollout_path, cwd, title, first_msg, thread_source, archived in rows:
        if thread_source == "subagent":
            continue
        if not rollout_path or not Path(rollout_path).exists():
            continue
        clean_title = (title or first_msg or "").strip()
        recs = parse_codex_rollout_file(
            Path(rollout_path), machine, since, until,
            cwd_override=cwd or "", title_override=clean_title[:120] or None,
        )
        for r in recs:
            if archived:
                r["archived"] = True
        out.extend(recs)
    return out


def collect_codex_sessions(codex_home: str, machine: str, since: dt.datetime, until: dt.datetime) -> list[dict[str, Any]]:
    """Collect Codex sessions, preferring state_5.sqlite, falling back to the rollout tree.

    Primary store: sessions/YYYY/MM/DD/rollout-*.jsonl (Codex Desktop/CLI). The
    threads table in state_5.sqlite indexes those with cwd/title/first_user_message,
    so it is the richest enumeration source. session_index.jsonl is only a sparse
    name cache (no cwd/duration) and is NOT used.
    """
    db_recs = collect_codex_sessions_from_db(codex_home, machine, since, until)
    if db_recs is not None:
        return db_recs

    # Fallback: scan the filesystem directly (DB unavailable / remote).
    out: list[dict[str, Any]] = []
    seen_sids: set[str] = set()
    sessions_dir = Path(codex_home) / "sessions"
    archived_dir = Path(codex_home) / "archived_sessions"
    if sessions_dir.exists():
        for p in sessions_dir.rglob("rollout-*.jsonl"):
            try:
                if dt.datetime.fromtimestamp(p.stat().st_mtime, tz=BUCHAREST) < since:
                    continue
            except Exception:
                pass
            recs = parse_codex_rollout_file(p, machine, since, until)
            for r in recs:
                seen_sids.add(r["session_id"])
            out.extend(recs)
    if archived_dir.exists():
        for p in sorted(archived_dir.glob("rollout-*.jsonl")):
            recs = parse_codex_rollout_file(p, machine, since, until)
            for r in recs:
                if r["session_id"] in seen_sids:
                    continue
                seen_sids.add(r["session_id"])
                r["archived"] = True
                out.append(r)
    return out



def extract_claude_jsonl_context(path: Path, since: dt.datetime, until: dt.datetime) -> dict[str, Any] | None:
    """Extract user messages with context (prev/next assistant) from a Claude Code JSONL session."""
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except Exception:
        return None
    if not lines:
        return None
    parsed: list[dict[str, Any]] = []
    for line in lines:
        try:
            obj = json.loads(line)
        except Exception:
            continue
        t = obj.get("type", "")
        msg = obj.get("message", {})
        content = msg.get("content", "")
        ts = parse_dt(obj.get("timestamp"))
        if isinstance(content, list):
            text_parts = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        text_parts.append(item.get("text", ""))
                    elif item.get("type") == "tool_use":
                        text_parts.append(f"[tool_use: {item.get('name', '?')}]")
                    elif item.get("type") == "tool_result":
                        text_parts.append("[tool_result]")
                    elif item.get("type") == "thinking":
                        text_parts.append("[thinking]")
                    elif item.get("type") == "tool_reference":
                        text_parts.append(f"[tool_ref: {item.get('tool_name', '?')}]")
            content = "\n".join(text_parts)
        parsed.append({"type": t, "content": str(content), "timestamp": ts})
    if not parsed:
        return None
    timestamps = [p["timestamp"] for p in parsed if p["timestamp"]]
    if not timestamps:
        return None
    first_ts = min(timestamps)
    last_ts = max(timestamps)
    if not (since <= first_ts.astimezone(BUCHAREST) < until or since <= last_ts.astimezone(BUCHAREST) < until):
        return None
    user_msgs = []
    for i, p in enumerate(parsed):
        if p["type"] != "user":
            continue
        if not p["timestamp"]:
            continue
        if not (since <= p["timestamp"].astimezone(BUCHAREST) < until):
            continue
        prev_assistant = ""
        for j in range(i - 1, -1, -1):
            if parsed[j]["type"] == "assistant":
                prev_assistant = parsed[j]["content"][:300]
                break
        next_assistant = ""
        for j in range(i + 1, len(parsed)):
            if parsed[j]["type"] == "assistant":
                next_assistant = parsed[j]["content"][:300]
                break
        user_msgs.append({
            "timestamp": local_dt_string(p["timestamp"]),
            "_parsed_ts": p["timestamp"],
            "user_message": p["content"][:500],
            "prev_assistant": prev_assistant[:300],
            "next_assistant": next_assistant[:300],
        })
    last_msg = ""
    last_type = ""
    for p in reversed(parsed):
        if p["type"] in ("assistant", "user") and p["content"].strip():
            last_msg = p["content"][:500]
            last_type = p["type"]
            break
    label = ""
    cbase = str(path)
    for prefix in ["-Users-blackthorne-Work-", "-Users-blackthorne-", "-home-blackthorne-Work-", "-home-blackthorne-"]:
        if prefix in cbase:
            parts = cbase.split(prefix)
            if len(parts) > 1:
                label = parts[1].split("/")[0]
            break
    if not label:
        label = path.parent.name
    # Compute active duration from REAL user messages only (exclude auto-generated)
    real_ums = [um for um in user_msgs if not um["user_message"].startswith("[tool_result]") and not um["user_message"].startswith("[tool_ref") and not um["user_message"].startswith("<command-") and not um["user_message"].startswith("<local-command-")]
    enriched_ts = [um.get("_parsed_ts") for um in real_ums if um.get("_parsed_ts")]
    if not enriched_ts and real_ums:
        enriched_ts = [parse_dt(um.get("timestamp")) for um in real_ums if um.get("timestamp")]
    active_min = 0
    active_method = ""
    if enriched_ts:
        enriched_ts = [t for t in enriched_ts if t]
        if enriched_ts:
            active_min, active_method, _ = compute_active_duration(enriched_ts)
    return {
        "source": "claude_jsonl",
        "session_id": path.stem,
        "path": str(path),
        "label": label,
        "start": local_dt_string(first_ts),
        "end": local_dt_string(last_ts),
        "duration_hours": round((last_ts - first_ts).total_seconds() / 3600, 2),
        "computed_active_minutes": active_min,
        "computed_method": active_method,
        "total_messages": len(parsed),
        "user_message_count": len(user_msgs),
        "user_messages": user_msgs,
        "last_message_type": last_type,
        "last_message": last_msg,
    }


def extract_hermes_db_context(db_path: str, since: dt.datetime, until: dt.datetime) -> list[dict[str, Any]]:
    """Extract user messages with context from Hermes state.db."""
    out: list[dict[str, Any]] = []
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        since_ts = since.timestamp()
        until_ts = until.timestamp()
        sessions = conn.execute("""
            SELECT id, started_at, ended_at, message_count, model, cwd
            FROM sessions
            WHERE (started_at >= ? AND started_at < ?) OR (ended_at >= ? AND ended_at < ?)
            ORDER BY started_at
        """, (since_ts, until_ts, since_ts, until_ts)).fetchall()
        for s in sessions:
            sid, started_at, ended_at, msg_count, model, cwd = s
            if not started_at:
                continue
            start_dt = dt.datetime.fromtimestamp(started_at, tz=BUCHAREST)
            end_dt = dt.datetime.fromtimestamp(ended_at, tz=BUCHAREST) if ended_at else None
            msgs = conn.execute("""
                SELECT role, timestamp, content, tool_name
                FROM messages WHERE session_id = ?
                ORDER BY timestamp
            """, (sid,)).fetchall()
            if not msgs:
                continue
            parsed = []
            for m in msgs:
                role, ts, content, tool_name = m
                if not ts:
                    continue
                msg_dt = dt.datetime.fromtimestamp(ts, tz=BUCHAREST)
                parsed.append({"role": role, "content": str(content or ""), "timestamp": msg_dt, "tool_name": tool_name or ""})
            if not parsed:
                continue
            user_msgs = []
            for i, p in enumerate(parsed):
                if p["role"] != "user":
                    continue
                if not (since <= p["timestamp"] < until):
                    continue
                prev_assistant = ""
                for j in range(i - 1, -1, -1):
                    if parsed[j]["role"] == "assistant":
                        prev_assistant = parsed[j]["content"][:300]
                        break
                next_assistant = ""
                for j in range(i + 1, len(parsed)):
                    if parsed[j]["role"] == "assistant":
                        next_assistant = parsed[j]["content"][:300]
                        break
                user_msgs.append({
                    "timestamp": local_dt_string(p["timestamp"]),
                    "_parsed_ts": p["timestamp"],
                    "user_message": p["content"][:500],
                    "prev_assistant": prev_assistant[:300],
                    "next_assistant": next_assistant[:300],
                })
            last_msg = ""
            last_role = ""
            for p in reversed(parsed):
                if p["role"] in ("assistant", "user") and p["content"].strip():
                    last_msg = p["content"][:500]
                    last_role = p["role"]
                    break
            active_ts = [um["_parsed_ts"] for um in user_msgs if um.get("_parsed_ts")]
            active_min, active_method = 0, ""
            if active_ts:
                active_min, active_method, _ = compute_active_duration(active_ts)
            out.append({
                "source": "hermes_db",
                "session_id": sid,
                "model": model,
                "cwd": cwd or "",
                "start": local_dt_string(start_dt),
                "end": local_dt_string(end_dt) if end_dt else None,
                "duration_hours": round((end_dt - start_dt).total_seconds() / 3600, 2) if end_dt else None,
                "computed_active_minutes": active_min,
                "computed_method": active_method,
                "total_messages": len(parsed),
                "user_message_count": len(user_msgs),
                "user_messages": user_msgs,
                "last_message_role": last_role,
                "last_message": last_msg,
            })
        conn.close()
    except Exception as e:
        out.append({"source": "hermes_db", "error": str(e)})
    return out



def _clockify_page_signature(entries: list[Mapping[str, Any]]) -> str:
    canonical = json.dumps(
        [str(entry.get("id") or "") for entry in entries],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _clockify_checkpoint_identity(
    workspace_id: str,
    user_id: str,
    since: dt.datetime,
    until: dt.datetime,
) -> CheckpointIdentity:
    request_contract = {
        "endpoint": "/workspaces/{workspace_id}/user/{user_id}/time-entries",
        "workspace_id": workspace_id,
        "user_id": user_id,
        "page_size": CLOCKIFY_PAGE_SIZE,
        "query": {"start": iso_utc(since), "end": iso_utc(until)},
    }
    request_fingerprint = "sha256:" + hashlib.sha256(
        json.dumps(request_contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return CheckpointIdentity(
        source="clockify",
        since_utc=iso_utc(since),
        until_utc=iso_utc(until),
        request_fingerprint=request_fingerprint,
        compatibility_version=CLOCKIFY_CHECKPOINT_COMPATIBILITY_VERSION,
    )


def _clockify_checkpoint_snapshot(state: CheckpointState) -> dt.datetime:
    snapshot_at = state.metadata.get("snapshot_at")
    if not isinstance(snapshot_at, str):
        raise CheckpointError("checkpoint snapshot_at is invalid")
    observed_at = parse_dt(snapshot_at)
    if observed_at is None:
        raise CheckpointError("checkpoint snapshot_at is invalid")
    return observed_at


def _clockify_checkpoint_entries(
    checkpoint_store: PageCheckpointStore,
    state: CheckpointState,
) -> tuple[list[Mapping[str, Any]], set[str]]:
    entries: list[Mapping[str, Any]] = []
    seen_pages: set[str] = set()
    final_page_size: int | None = None
    for page_number, saved_page in enumerate(checkpoint_store.iter_pages(state), start=1):
        payload = saved_page["payload"]
        if not isinstance(payload, tuple):
            raise CheckpointError("checkpoint Clockify page payload is invalid")
        rows = [entry for entry in payload if isinstance(entry, Mapping)]
        continuation = saved_page["continuation"]
        if not isinstance(continuation, Mapping) or dict(continuation) != {
            "page": page_number + 1
        }:
            raise CheckpointError("checkpoint Clockify page continuation is invalid")
        signature = saved_page["signature"]
        if not isinstance(signature, str) or signature != _clockify_page_signature(rows):
            raise CheckpointError("checkpoint Clockify page signature is invalid")
        if rows and signature in seen_pages:
            raise CheckpointError("checkpoint Clockify pagination repeated a page")
        seen_pages.add(signature)
        entries.extend(rows)
        final_page_size = len(rows)
    if state.complete and (
        final_page_size is None or final_page_size >= CLOCKIFY_PAGE_SIZE
    ):
        raise CheckpointError("completed Clockify checkpoint has no short final page")
    return entries, seen_pages


def _clockify_checkpoint_page(state: CheckpointState) -> int:
    page = len(state.pages) + 1
    expected_continuation: dict[str, int] = {} if page == 1 else {"page": page}
    if dict(state.continuation) != expected_continuation:
        raise CheckpointError("checkpoint Clockify continuation page is invalid")
    return page


def fetch_clockify(
    cenv: dict[str, str],
    routing: dict[str, Any],
    since: dt.datetime,
    until: dt.datetime,
    *,
    snapshot_at: dt.datetime | None = None,
    checkpoint_store: PageCheckpointStore | None = None,
) -> dict[str, Any]:
    if cenv.get("_missing"):
        return {
            "status": "missing_credentials",
            "missing": cenv["_missing"],
            "entries": [],
            "running_entry_count": 0,
            "complete": False,
        }
    ws = cenv["CLOCKIFY_WORKSPACE_ID"]
    user = routing["clockify_user_id"]
    observed_at = snapshot_at or dt.datetime.now(dt.timezone.utc)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=dt.timezone.utc)
    try:
        if checkpoint_store is None:
            entries: list[Mapping[str, Any]] = []
            seen_pages: set[tuple[str, ...]] = set()
            page = 1
            while True:
                page_entries = clockify_get(
                    f"/workspaces/{ws}/user/{user}/time-entries?"
                    f"start={iso_utc(since)}&end={iso_utc(until)}&page={page}&page-size={CLOCKIFY_PAGE_SIZE}",
                    cenv,
                )
                if not isinstance(page_entries, list):
                    raise ValueError("Clockify time-entry response did not contain a list")
                rows = [entry for entry in page_entries if isinstance(entry, dict)]
                signature = tuple(str(entry.get("id") or "") for entry in rows)
                if rows and signature in seen_pages:
                    raise ValueError("Clockify pagination repeated a page")
                seen_pages.add(signature)
                entries.extend(rows)
                if len(rows) < CLOCKIFY_PAGE_SIZE:
                    break
                page += 1
                if page > 100:
                    raise ValueError("Clockify pagination exceeded safety limit")
            pages_fetched = page
        else:
            identity = _clockify_checkpoint_identity(ws, user, since, until)
            checkpoint_state = checkpoint_store.open(
                identity,
                initial_metadata={"snapshot_at": iso_utc(observed_at)},
            )
            observed_at = _clockify_checkpoint_snapshot(checkpoint_state)
            entries, seen_pages = _clockify_checkpoint_entries(
                checkpoint_store, checkpoint_state
            )
            page = _clockify_checkpoint_page(checkpoint_state)
            pages_fetched = len(checkpoint_state.pages)
            while not checkpoint_state.complete:
                if page > 100:
                    raise ValueError("Clockify pagination exceeded safety limit")
                page_entries = clockify_get(
                    f"/workspaces/{ws}/user/{user}/time-entries?"
                    f"start={iso_utc(since)}&end={iso_utc(until)}&page={page}&page-size={CLOCKIFY_PAGE_SIZE}",
                    cenv,
                )
                if not isinstance(page_entries, list):
                    raise ValueError("Clockify time-entry response did not contain a list")
                rows = [entry for entry in page_entries if isinstance(entry, Mapping)]
                signature = _clockify_page_signature(rows)
                if rows and signature in seen_pages:
                    raise ValueError("Clockify pagination repeated a page")
                seen_pages.add(signature)
                checkpoint_state = checkpoint_store.append_page(
                    checkpoint_state,
                    payload=page_entries,
                    continuation={"page": page + 1},
                    signature=signature,
                )
                entries.extend(rows)
                pages_fetched += 1
                if len(rows) < CLOCKIFY_PAGE_SIZE:
                    checkpoint_state = checkpoint_store.mark_complete(checkpoint_state)
                    break
                page += 1
        snapshot_boundary = min(until, observed_at)
        sanitized = []
        running_entry_count = 0
        running_snapshot_count = 0
        for e in entries:
            ti = e.get("timeInterval", {})
            running = not ti.get("end")
            if running:
                running_entry_count += 1
            start_dt = parse_dt(ti.get("start"))
            fixed_end = parse_dt(ti.get("end"))
            running_snapshot = None
            if running and start_dt and start_dt < snapshot_boundary:
                # Clockify has not supplied an immutable end. For this
                # collection only, the observed snapshot makes it an existing
                # fixed block through the earlier of collection time and the
                # requested range boundary.
                fixed_end = snapshot_boundary
                running_snapshot_count += 1
                running_snapshot = {
                    "observed_at": iso_utc(observed_at),
                    "boundary": iso_utc(snapshot_boundary),
                    "basis": "collection_snapshot_boundary",
                }
            sanitized.append({
                "id_suffix": e.get("id", "")[-8:],
                "description": e.get("description", ""),
                "project_id_suffix": (e.get("projectId") or "")[-6:],
                "tag_id_suffixes": [(t or "")[-8:] for t in e.get("tagIds", [])],
                "start": local_dt_string(start_dt),
                "end": local_dt_string(fixed_end),
                "running": running,
                "running_snapshot": running_snapshot,
                "duration": ti.get("duration"),
                "billable": e.get("billable"),
            })
        return {
            # Pagination is complete even when Clockify has a currently
            # running entry only if each one has a temporary end explicitly
            # bounded by this collection snapshot, never by a guessed future
            # end. An unparseable or future start remains fail-closed.
            "status": (
                "ok" if running_snapshot_count == running_entry_count else "partial"
            ),
            "entries": sanitized,
            "pages_fetched": pages_fetched,
            "running_entry_count": running_entry_count,
            "running_entry_snapshot_count": running_snapshot_count,
            "collection_snapshot": {
                "observed_at": iso_utc(observed_at),
                "boundary": iso_utc(snapshot_boundary),
                "requested_until": iso_utc(until),
            },
            "complete": running_snapshot_count == running_entry_count,
        }
    except Exception as e:
        return {
            "status": "error",
            "error": str(e) if checkpoint_store is None else "Clockify checkpoint collection failed",
            "entries": [],
            "running_entry_count": 0,
            "complete": False,
        }


def _fathom_cursor_reference(cursor: str | None) -> str:
    if not cursor:
        return "initial"
    return "sha256:" + hashlib.sha256(cursor.encode("utf-8")).hexdigest()[:12]


def _fathom_page_response(data: Any) -> tuple[list[Mapping[str, Any]], str | None]:
    if isinstance(data, list):
        return [item for item in data if isinstance(item, Mapping)], None
    if not isinstance(data, Mapping) or not isinstance(data.get("items"), (list, tuple)):
        raise ValueError("Fathom meetings response did not contain a list")
    next_cursor = (
        data.get("next_cursor")
        or data.get("nextCursor")
        or data.get("next_page_token")
        or (data.get("pagination") or {}).get("next_cursor")
    )
    return (
        [item for item in data["items"] if isinstance(item, Mapping)],
        str(next_cursor or "").strip() or None,
    )


def _fathom_page_signature(items: list[Mapping[str, Any]]) -> str:
    canonical = json.dumps(
        [str(item.get("recording_id") or item.get("id") or "") for item in items],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _fathom_checkpoint_identity(
    since: dt.datetime,
    until: dt.datetime,
) -> CheckpointIdentity:
    creation_search_start = since - FATHOM_CREATION_LOOKBACK
    request_contract = {
        "endpoint": "/meetings",
        "query": {
            "created_after": iso_utc(creation_search_start),
            "created_before": iso_utc(until),
            "limit": 50,
            "include_summary": "true",
            "include_action_items": "true",
            "include_transcript": "true",
        },
        "occurrence_interval": {"since": iso_utc(since), "until": iso_utc(until)},
        "creation_lookback_seconds": int(FATHOM_CREATION_LOOKBACK.total_seconds()),
    }
    request_fingerprint = "sha256:" + hashlib.sha256(
        json.dumps(request_contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return CheckpointIdentity(
        source="fathom",
        since_utc=iso_utc(since),
        until_utc=iso_utc(until),
        request_fingerprint=request_fingerprint,
        compatibility_version=FATHOM_CHECKPOINT_COMPATIBILITY_VERSION,
    )


def _fathom_checkpoint_items(
    checkpoint_store: PageCheckpointStore,
    state: CheckpointState,
) -> tuple[list[Mapping[str, Any]], set[str], str | None]:
    items: list[Mapping[str, Any]] = []
    seen_cursors: set[str] = set()
    cursor: str | None = None
    for page_number, saved_page in enumerate(checkpoint_store.iter_pages(state), start=1):
        page_items, next_cursor = _fathom_page_response(saved_page["payload"])
        metadata = saved_page["metadata"]
        if not isinstance(metadata, Mapping) or dict(metadata) != {
            "request_cursor": cursor
        }:
            raise CheckpointError("checkpoint Fathom request cursor is invalid")
        continuation = saved_page["continuation"]
        if not isinstance(continuation, Mapping) or dict(continuation) != {
            "cursor": next_cursor
        }:
            raise CheckpointError("checkpoint Fathom page continuation is invalid")
        signature = saved_page["signature"]
        if not isinstance(signature, str) or signature != _fathom_page_signature(page_items):
            raise CheckpointError("checkpoint Fathom page signature is invalid")
        if next_cursor:
            if next_cursor in seen_cursors:
                raise CheckpointError("checkpoint Fathom pagination cursor repeated")
            seen_cursors.add(next_cursor)
        elif page_number != len(state.pages):
            raise CheckpointError("checkpoint Fathom terminal page has a successor")
        items.extend(page_items)
        cursor = next_cursor
    expected_continuation: dict[str, str | None] = (
        {} if not state.pages else {"cursor": cursor}
    )
    if dict(state.continuation) != expected_continuation:
        raise CheckpointError("checkpoint Fathom continuation is invalid")
    if state.complete and (not state.pages or cursor is not None):
        raise CheckpointError("completed Fathom checkpoint has a continuation")
    return items, seen_cursors, cursor


def _fathom_failure(
    reason: str,
    *,
    page: int,
    cursor: str | None,
    retry_budget: FathomRetryBudget,
) -> dict[str, Any]:
    return {
        "status": "error",
        "error": reason,
        "meetings": [],
        "complete": False,
        "failure": {
            "page": page,
            "cursor": _fathom_cursor_reference(cursor),
            "retry_count": len(retry_budget.retry_delays),
            "retry_delays_seconds": list(retry_budget.retry_delays),
            "retry_policy": retry_budget.policy(),
        },
    }


def fetch_fathom(
    fenv: dict[str, str],
    since: dt.datetime,
    until: dt.datetime,
    *,
    checkpoint_store: PageCheckpointStore | None = None,
) -> dict[str, Any]:
    if fenv.get("_missing"):
        return {
            "status": "missing_credentials",
            "missing": fenv["_missing"],
            "meetings": [],
            "complete": False,
        }
    headers = {"X-Api-Key": fenv["FATHOM_API_KEY"]}
    items: list[Mapping[str, Any]] = []
    cursor: str | None = None
    pages = 0
    retry_budget = FathomRetryBudget()
    creation_search_start = since - FATHOM_CREATION_LOOKBACK
    try:
        seen_cursors: set[str] = set()
        checkpoint_state: CheckpointState | None = None
        if checkpoint_store is not None:
            checkpoint_state = checkpoint_store.open(_fathom_checkpoint_identity(since, until))
            items, seen_cursors, cursor = _fathom_checkpoint_items(
                checkpoint_store, checkpoint_state
            )
            pages = len(checkpoint_state.pages)
            if checkpoint_state.pages and cursor is None and not checkpoint_state.complete:
                checkpoint_state = checkpoint_store.mark_complete(checkpoint_state)
        while checkpoint_state is None or not checkpoint_state.complete:
            if pages >= FATHOM_MAX_PAGES:
                raise ValueError("Fathom pagination exceeded safety limit")
            retry_budget.require_time_remaining()
            query = {
                # Fathom recording records are created at or shortly after the
                # meeting occurs. Query the accounting window with one day of
                # lead, then enforce occurrence overlap locally. A 1970 history
                # crawl needlessly rate-limited on thousands of unrelated rows.
                "created_after": iso_utc(creation_search_start),
                "created_before": iso_utc(until),
                "limit": 50,
                # Fathom exposes semantic meeting content through opt-in fields
                # on the list endpoint. There is no documented
                # GET /meetings/{recording_id} hydration endpoint.
                "include_summary": "true",
                "include_action_items": "true",
                "include_transcript": "true",
            }
            if cursor:
                query["cursor"] = cursor
            params = urllib.parse.urlencode(query)
            data = fathom_http_json_with_retry(
                f"{FATHOM_API}/meetings?{params}", headers, retry_budget
            )
            page_items, next_cursor = _fathom_page_response(data)
            if next_cursor and next_cursor in seen_cursors:
                raise ValueError("Fathom pagination cursor repeated")
            if checkpoint_state is not None:
                checkpoint_state = checkpoint_store.append_page(
                    checkpoint_state,
                    payload=data,
                    continuation={"cursor": next_cursor},
                    signature=_fathom_page_signature(page_items),
                    metadata={"request_cursor": cursor},
                )
            items.extend(page_items)
            pages += 1
            if next_cursor:
                seen_cursors.add(next_cursor)
                cursor = next_cursor
                continue
            cursor = None
            if checkpoint_state is not None:
                checkpoint_state = checkpoint_store.mark_complete(checkpoint_state)
            break

        meetings = []
        for m in items:
            recording_id = m.get("recording_id") or m.get("id")
            recording_start = parse_dt(m.get("recording_start_time"))
            recording_end = parse_dt(m.get("recording_end_time"))
            scheduled_start = parse_dt(m.get("scheduled_start_time"))
            scheduled_end = parse_dt(m.get("scheduled_end_time"))
            if recording_start and recording_end and recording_end > recording_start:
                start, end, timing_basis = recording_start, recording_end, "recording"
            elif scheduled_start and scheduled_end and scheduled_end > scheduled_start:
                start, end, timing_basis = scheduled_start, scheduled_end, "scheduled"
            else:
                start, end, timing_basis = None, None, "unavailable"
            if start and end:
                if not (start < until and end > since):
                    continue
            else:
                created_at = parse_dt(m.get("created_at"))
                if not created_at or not (since <= created_at < until):
                    continue
            summary = m.get("default_summary") or m.get("summary")
            action_items = m.get("action_items") or []
            transcript = m.get("transcript")
            semantic_available = bool(summary or action_items or transcript)
            meetings.append({
                "recording_id": recording_id,
                "title": m.get("title") or m.get("meeting_title"),
                "start": local_dt_string(start),
                "end": local_dt_string(end),
                "timing_basis": timing_basis,
                "share_url": m.get("share_url") or m.get("url"),
                "calendar_invitees": [{"email": i.get("email"), "name": i.get("name"), "is_external": i.get("is_external")} for i in m.get("calendar_invitees", [])[:20]],
                "domains_type": m.get("calendar_invitees_domains_type"),
                "calendar_invitees_domains_type": m.get("calendar_invitees_domains_type"),
                "recorded_by_email": (m.get("recorded_by") or {}).get("email"),
                "summary": summary,
                "action_items": action_items,
                "transcript": transcript,
                "transcript_language": m.get("transcript_language"),
                "semantic_evidence_status": (
                    "available" if semantic_available else "title_only"
                ),
            })
        return {
            "status": "ok",
            "meetings": meetings,
            "pages_fetched": pages,
            "complete": True,
            "creation_search": {
                "start": iso_utc(creation_search_start),
                "end": iso_utc(until),
                "lookback_days": FATHOM_CREATION_LOOKBACK.days,
                "basis": "record_created_at_or_after_meeting_observed_live",
            },
            "occurrence_filter": {
                "start": iso_utc(since),
                "end": iso_utc(until),
                "basis": "recording_or_scheduled_overlap",
            },
            "semantic_content_requested": [
                "summary",
                "action_items",
                "transcript",
            ],
            "retry_count": len(retry_budget.retry_delays),
            "retry_delays_seconds": list(retry_budget.retry_delays),
            "retry_policy": retry_budget.policy(),
        }
    except FathomRetryBudgetExhausted as error:
        return _fathom_failure(
            str(error), page=pages + 1, cursor=cursor, retry_budget=retry_budget
        )
    except urllib.error.HTTPError as error:
        return _fathom_failure(
            f"Fathom HTTP {error.code}",
            page=pages + 1,
            cursor=cursor,
            retry_budget=retry_budget,
        )
    except Exception as e:
        return _fathom_failure(
            str(e) if checkpoint_store is None else "Fathom checkpoint collection failed",
            page=pages + 1,
            cursor=cursor,
            retry_budget=retry_budget,
        )


def multica_profile_config() -> dict[str, Any] | None:
    env_config = {
        "token": os.environ.get("MULTICA_TOKEN"),
        "server_url": os.environ.get("MULTICA_SERVER_URL"),
        "workspace_id": os.environ.get("MULTICA_WORKSPACE_ID"),
    }
    if all(env_config.values()):
        return env_config
    for home in _home_candidates():
        path = home / f".multica/profiles/{MULTICA_PROFILE}/config.json"
        if not path.exists():
            continue
        try:
            return json.loads(path.read_text())
        except Exception:
            continue
    return None


def _multica_server_origin(server: str) -> str:
    parsed = urllib.parse.urlsplit(server)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Multica server URL has no valid HTTP origin")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("Multica server URL has an invalid port") from error
    return f"{parsed.scheme.lower()}://{host}{f':{port}' if port else ''}"


def _multica_page_rows(data: Any) -> list[Mapping[str, Any]]:
    if isinstance(data, Mapping):
        if "issues" in data:
            items = data["issues"]
        elif "items" in data:
            items = data["items"]
        else:
            raise ValueError("Multica issues response did not contain a list")
    elif isinstance(data, list):
        items = data
    else:
        raise ValueError("Multica issues response did not contain a list")
    if not isinstance(items, (list, tuple)):
        raise ValueError("Multica issues response did not contain a list")
    return [item for item in items if isinstance(item, Mapping)]


def _multica_page_signature(rows: list[Mapping[str, Any]]) -> str:
    canonical = json.dumps(
        [str(row.get("id") or row.get("key") or row.get("identifier") or "") for row in rows],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _multica_checkpoint_identity(
    server_origin: str,
    workspace_id: str,
    since: dt.datetime | None,
    until: dt.datetime | None,
    endpoint_path: str,
) -> CheckpointIdentity:
    since_utc = iso_utc(since) if since is not None else ""
    until_utc = iso_utc(until) if until is not None else ""
    request_contract = {
        "server_origin": server_origin,
        "workspace_id": workspace_id,
        "endpoint_path": endpoint_path,
        "page_size": MULTICA_PAGE_SIZE,
        "api_contract": "multica-issues-offset-limit/v1",
        "activity_interval": {"since": since_utc, "until": until_utc},
    }
    request_fingerprint = "sha256:" + hashlib.sha256(
        json.dumps(request_contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return CheckpointIdentity(
        source="multica_issues",
        since_utc=since_utc,
        until_utc=until_utc,
        request_fingerprint=request_fingerprint,
        compatibility_version=MULTICA_CHECKPOINT_COMPATIBILITY_VERSION,
    )


def _multica_checkpoint_rows(
    checkpoint_store: PageCheckpointStore,
    state: CheckpointState,
    endpoint_path: str,
) -> tuple[list[Mapping[str, Any]], set[str], int, bool]:
    if dict(state.metadata) != {"endpoint_path": endpoint_path}:
        raise CheckpointError("checkpoint Multica endpoint path is invalid")
    rows: list[Mapping[str, Any]] = []
    seen_pages: set[str] = set()
    offset = 0
    final_page_size: int | None = None
    for page_number, saved_page in enumerate(checkpoint_store.iter_pages(state), start=1):
        page_rows = _multica_page_rows(saved_page["payload"])
        metadata = saved_page["metadata"]
        if not isinstance(metadata, Mapping) or dict(metadata) != {
            "endpoint_path": endpoint_path,
            "request_offset": offset,
        }:
            raise CheckpointError("checkpoint Multica request path or offset is invalid")
        continuation = saved_page["continuation"]
        next_offset = offset + len(page_rows)
        if not isinstance(continuation, Mapping) or dict(continuation) != {
            "offset": next_offset
        }:
            raise CheckpointError("checkpoint Multica page continuation is invalid")
        signature = saved_page["signature"]
        if not isinstance(signature, str) or signature != _multica_page_signature(page_rows):
            raise CheckpointError("checkpoint Multica page signature is invalid")
        if page_rows and signature in seen_pages:
            raise CheckpointError("checkpoint Multica pagination repeated a page")
        if len(page_rows) < MULTICA_PAGE_SIZE and page_number != len(state.pages):
            raise CheckpointError("checkpoint Multica terminal page has a successor")
        seen_pages.add(signature)
        rows.extend(page_rows)
        offset = next_offset
        final_page_size = len(page_rows)
    expected_continuation: dict[str, int] = {} if not state.pages else {"offset": offset}
    if dict(state.continuation) != expected_continuation:
        raise CheckpointError("checkpoint Multica continuation offset is invalid")
    if state.complete and (
        final_page_size is None or final_page_size >= MULTICA_PAGE_SIZE
    ):
        raise CheckpointError("completed Multica checkpoint has no short final page")
    terminal_ready = final_page_size is not None and final_page_size < MULTICA_PAGE_SIZE
    return rows, seen_pages, offset, terminal_ready


def _multica_sanitized_issues(
    rows: list[Mapping[str, Any]],
    since: dt.datetime | None,
    until: dt.datetime | None,
) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    for row in rows:
        issue = {
            "id": row.get("id"),
            "key": row.get("key") or row.get("identifier"),
            "title": row.get("title"),
            "description": row.get("description"),
            "status": row.get("status"),
            "project_id": row.get("project_id") or row.get("projectId"),
            "created_at": row.get("created_at") or row.get("createdAt"),
            "updated_at": row.get("updated_at") or row.get("updatedAt"),
            "completed_at": row.get("completed_at") or row.get("completedAt"),
            "labels": row.get("labels") or [],
        }
        if since is not None and until is not None:
            activity_times = [
                parse_dt(issue.get(field))
                for field in ("created_at", "updated_at", "completed_at")
            ]
            if not any(
                value is not None
                and since <= value.astimezone(since.tzinfo or BUCHAREST) < until
                for value in activity_times
            ):
                continue
        issues.append(issue)
    return issues


def _multica_result(
    rows: list[Mapping[str, Any]],
    pages: int,
    since: dt.datetime | None,
    until: dt.datetime | None,
) -> dict[str, Any]:
    return {
        "status": "ok",
        "issues": _multica_sanitized_issues(rows, since, until),
        "pages_fetched": pages,
        "complete": True,
        "activity_window": (
            {"since": since.isoformat(), "until": until.isoformat()}
            if since is not None and until is not None
            else None
        ),
    }


def _multica_failure() -> dict[str, Any]:
    return {
        "status": "error",
        "issues": [],
        "complete": False,
        "note": "Unable to fetch issues with known API paths; autopilot can use CLI/API fallback.",
    }


def fetch_multica_issues(
    since: dt.datetime | None = None,
    until: dt.datetime | None = None,
    *,
    checkpoint_store: PageCheckpointStore | None = None,
) -> dict[str, Any]:
    cfg = multica_profile_config()
    if not cfg:
        return {"status": "missing_profile", "issues": []}
    token = cfg.get("token") or cfg.get("access_token") or cfg.get("auth_token")
    server = cfg.get("server_url") or cfg.get("serverUrl") or cfg.get("base_url")
    workspace_id = cfg.get("workspace_id") or cfg.get("workspaceId") or os.environ.get("MULTICA_WORKSPACE_ID")
    if not (token and server and workspace_id):
        return {"status": "profile_incomplete", "issues": []}
    # Try common API shapes, keeping output sanitized. Exact page-size results
    # are not proof of completeness, so continue with offset pagination until
    # the server returns a short page. A server that ignores offset is rejected
    # via the repeated-page guard instead of silently reporting 100 as complete.
    paths = ["/api/issues", "/issues"]
    headers = {"Authorization": f"Bearer {token}", "X-Workspace-ID": workspace_id}
    if checkpoint_store is not None:
        try:
            server_origin = _multica_server_origin(server)
            states = [
                (
                    path,
                    checkpoint_store.open(
                        _multica_checkpoint_identity(
                            server_origin, workspace_id, since, until, path
                        ),
                        initial_metadata={"endpoint_path": path},
                    ),
                )
                for path in paths
            ]
            committed = [(path, state) for path, state in states if state.pages]
            if len(committed) > 1:
                raise CheckpointError("multiple Multica endpoints have committed checkpoints")
            candidates = committed or states
            for path, checkpoint_state in candidates:
                rows, seen_pages, offset, terminal_ready = _multica_checkpoint_rows(
                    checkpoint_store, checkpoint_state, path
                )
                pages = len(checkpoint_state.pages)
                if terminal_ready and not checkpoint_state.complete:
                    checkpoint_state = checkpoint_store.mark_complete(checkpoint_state)
                try:
                    while not checkpoint_state.complete:
                        if pages >= 100:
                            raise ValueError("Multica issues pagination exceeded safety limit")
                        data = http_json(
                            server.rstrip("/")
                            + f"{path}?limit={MULTICA_PAGE_SIZE}&offset={offset}",
                            headers,
                        )
                        page_rows = _multica_page_rows(data)
                        signature = _multica_page_signature(page_rows)
                        if page_rows and signature in seen_pages:
                            raise ValueError("Multica issues pagination repeated a page")
                        checkpoint_state = checkpoint_store.append_page(
                            checkpoint_state,
                            payload=data,
                            continuation={"offset": offset + len(page_rows)},
                            signature=signature,
                            metadata={
                                "endpoint_path": path,
                                "request_offset": offset,
                            },
                        )
                        rows.extend(page_rows)
                        seen_pages.add(signature)
                        pages += 1
                        offset += len(page_rows)
                        if len(page_rows) < MULTICA_PAGE_SIZE:
                            checkpoint_state = checkpoint_store.mark_complete(checkpoint_state)
                            break
                    return _multica_result(rows, pages, since, until)
                except CheckpointError:
                    raise
                except Exception:
                    if checkpoint_state.pages:
                        raise
                    # A path is only interchangeable before a page is committed.
                    continue
        except Exception:
            return _multica_failure()
        return _multica_failure()
    for path in paths:
        try:
            seen_pages: set[tuple[str, ...]] = set()
            offset = 0
            pages = 0
            rows: list[Mapping[str, Any]] = []
            while True:
                data = http_json(
                    server.rstrip("/") + f"{path}?limit={MULTICA_PAGE_SIZE}&offset={offset}",
                    headers,
                )
                page = _multica_page_rows(data)
                signature = tuple(
                    str(item.get("id") or item.get("key") or item.get("identifier") or "")
                    for item in page
                )
                if page and signature in seen_pages:
                    raise ValueError("Multica issues pagination repeated a page")
                seen_pages.add(signature)
                pages += 1
                rows.extend(page)
                if len(page) < MULTICA_PAGE_SIZE:
                    break
                offset += len(page)
                if pages >= 100:
                    raise ValueError("Multica issues pagination exceeded safety limit")
            return _multica_result(rows, pages, since, until)
        except Exception:
            continue
    return _multica_failure()


def route_session(burst: dict[str, Any], routing: dict[str, Any]) -> dict[str, Any]:
    label = (burst.get("label") or burst.get("path") or "").lower()
    path = (burst.get("path") or "").lower()
    for r in routing.get("session_routes", []):
        pat = r.get("pattern", "").lower()
        if pat and (pat in label or pat in path or fnmatch.fnmatch(label, pat.lower())):
            if r.get("action") == "skip":
                return {"action": "skip", "reason": r.get("reason", "route skip")}
            return {"action": "propose", **r}
    return {"action": "ambiguous", "reason": "No route matched session label/path"}


def route_meeting(meeting: dict[str, Any], routing: dict[str, Any]) -> dict[str, Any]:
    """Route a Fathom meeting by invitee domain or title pattern."""
    title = str(meeting.get("title") or "")
    domains_type = str(meeting.get("calendar_invitees_domains_type") or "")
    invitee_emails = [
        str(invitee.get("email") or "").lower()
        for invitee in meeting.get("calendar_invitees", [])
        if isinstance(invitee, dict)
    ]
    invitees = [
        invitee
        for invitee in meeting.get("calendar_invitees", [])
        if isinstance(invitee, dict)
    ]
    if (
        not domains_type
        and invitees
        and all(not invitee.get("is_external") for invitee in invitees)
    ):
        domains_type = "internal_only"
    for route in routing.get("meeting_routes", []):
        domain = str(route.get("email_domain") or "").lower()
        title_regex = route.get("title_regex")
        required_domains_type = str(route.get("domains_type") or "")
        if required_domains_type and required_domains_type != domains_type:
            continue
        if domain and any(email.endswith(f"@{domain}") for email in invitee_emails):
            return {"action": "propose", **route}
        if title_regex:
            try:
                if re.search(str(title_regex), title, flags=re.IGNORECASE):
                    return {"action": "propose", **route}
            except re.error:
                continue
        if required_domains_type and not domain and not title_regex:
            return {"action": "propose", **route}
    return {"action": "ambiguous", "reason": "No meeting route matched invitees/title"}


def overlaps_existing(candidate: dict[str, Any], existing: list[dict[str, Any]]) -> bool:
    cs = parse_dt(candidate.get("start"))
    ce = parse_dt(candidate.get("end"))
    if not cs or not ce:
        return False
    for e in existing:
        es = parse_dt(e.get("start"))
        ee = parse_dt(e.get("end"))
        if not es or not ee:
            continue
        latest_start = max(cs, es)
        earliest_end = min(ce, ee)
        ov = (earliest_end - latest_start).total_seconds()
        dur = max(1, (ce - cs).total_seconds())
        if ov > 0 and ov / dur >= 0.50:
            return True
    return False


def _make_description(project: str, burst: dict[str, Any], confidence: str = "medium", prefix: str = "SC") -> str:
    """Build a human-readable description from session context.

    Format: [PREFIX] — [concise action summary]
    Prefix comes from the matched route in routing.json (e.g. SC, TSTP, LoA, Kontas, TaxCamp, SZ).
    Unlabeled/low-confidence sessions get [NEEDS REVIEW] marker.
    """
    first = (burst.get("first_user_message") or "").strip()
    last = (burst.get("last_assistant_message") or "").strip()
    label = burst.get("label", "")
    msgs = burst.get("user_messages", 0)
    dur = burst.get("duration_minutes", 0)
    machine = burst.get("machine", "")
    source = burst.get("source", "claude")

    # Check if this is an unlabeled/low-confidence session
    is_unlabeled = (
        label in ("Work", "-claude", "-home-blackthorne", "llm-self", "cpu", "-tmp", "session_")
        or (label and label.startswith("-"))
        or (label == "Work" and confidence == "low")
    )

    def described(summary: str) -> str:
        return _one_line(f"{prefix} — {summary}", DESCRIPTION_LIMIT)

    explicit_first = _explicit_context_heading(first) or _session_directive(first)
    problem_first = _problem_context_summary(first)
    cleaned_first = _meaningful_context(first)
    cleaned_last = _meaningful_context(last)

    if is_unlabeled:
        context = (
            explicit_first
            or problem_first
            or (cleaned_first if _looks_like_task_request(cleaned_first) else "")
            or cleaned_last
            or cleaned_first
        )
        if context:
            return described(f"[NEEDS REVIEW] {context}")
        return described(f"[NEEDS REVIEW] Unlabeled session on {machine} ({msgs} msgs, {dur}m)")

    if len(explicit_first) > 10:
        return described(explicit_first)

    # Prefer the row-local assistant result because it describes what was
    # accomplished; fall back to the row-local user request.
    if len(cleaned_last) > 10:
        return described(cleaned_last)
    if len(cleaned_first) > 10:
        return described(cleaned_first)

    # Try enriched context: if we have user_messages with content, use the first real one
    user_msgs = burst.get("user_messages_detail", [])
    if user_msgs:
        for um in user_msgs:
            content = (um.get("user_message") or "").strip()
            if len(content) > 10 and not _is_system_message(content):
                return described(content)

    # Last resort: a label alone is not a safe description, even if routing
    # confidence is otherwise high.
    return described(f"[NEEDS REVIEW] {label} ({msgs} msgs, {dur}m)")


def _make_rationale(burst: dict[str, Any]) -> str:
    first = _meaningful_context(burst.get("first_user_message") or "")
    if len(first) > 10:
        return f"Session context: {first[:200]}"
    # Try enriched user messages
    user_msgs = burst.get("user_messages_detail", [])
    if user_msgs:
        for um in user_msgs:
            content = (um.get("user_message") or "").strip()
            if len(content) > 10 and not _is_system_message(content):
                return f"Session context: {content[:200]}"
    return f"{burst.get('user_messages')} user messages across {burst.get('duration_minutes')}m; route matched {burst.get('label')}"


def build_proposals(evidence: dict[str, Any], routing: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    proposals: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    existing = evidence.get("clockify", {}).get("entries", [])
    rules = routing.get("skip_rules", {})

    for meeting in evidence.get("fathom", {}).get("meetings", []):
        start = meeting.get("start")
        end = meeting.get("end")
        start_dt = parse_dt(start)
        end_dt = parse_dt(end)
        if not start_dt or not end_dt or end_dt <= start_dt:
            skipped.append({
                "id": str(meeting.get("recording_id") or "fathom"),
                "source": "fathom",
                "time": f"{start}–{end}",
                "label": meeting.get("title"),
                "reason": "invalid or missing meeting time window",
            })
            continue
        recording_id = str(meeting.get("recording_id") or "")
        invitees = [
            invitee
            for invitee in meeting.get("calendar_invitees", [])
            if isinstance(invitee, dict) and invitee.get("email")
        ]
        external_invitees = [invitee for invitee in invitees if invitee.get("is_external")]
        title = str(meeting.get("title") or "Fathom meeting")
        meeting_record = {
            "source": "fathom",
            "machine": "fathom",
            "session_id": recording_id,
            "path": meeting.get("share_url"),
            "label": title,
            "start": start,
            "end": end,
        }
        if not external_invitees and len(invitees) <= 1 and re.search(
            r"\bimpromptu\b", title, flags=re.IGNORECASE
        ):
            skipped.append({
                "id": _candidate_key(meeting_record),
                "source": "fathom",
                "time": f"{start}–{end}",
                "label": title,
                "reason": "solo impromptu recording treated as recorder misfire",
            })
            continue
        if meeting.get("semantic_evidence_status") == "title_only":
            ambiguous.append({
                "id": f"A{len(ambiguous)+1:03d}",
                "candidate_key": _candidate_key(meeting_record),
                "provenance": _record_provenance(meeting_record),
                "source": "fathom",
                "time": f"{start}–{end}",
                "label": title,
                "reason": (
                    "Fathom supplied only a meeting title; no transcript, summary, "
                    "or action items support a truthful outcome"
                ),
                "machine": "fathom",
                "exception_kind": "insufficient_meeting_evidence",
            })
            continue
        route = route_meeting(meeting, routing)
        if route["action"] == "ambiguous":
            ambiguous.append({
                "id": f"A{len(ambiguous)+1:03d}",
                "candidate_key": _candidate_key(meeting_record),
                "provenance": _record_provenance(meeting_record),
                "source": "fathom",
                "time": f"{start}–{end}",
                "label": title,
                "reason": route["reason"],
                "machine": "fathom",
            })
            continue
        duration = max(1, int((end_dt - start_dt).total_seconds() / 60))
        candidate = {
            "id": f"P{len(proposals)+1:03d}",
            "start": start,
            "end": end,
            "duration_minutes": duration,
            "client_project": route.get("project_name"),
            "clockify_project_suffix": route.get("project_suffix"),
            "tag_suffixes": route.get("tag_suffixes", []),
            "tag_names": route.get("tag_names", []),
            "billable": route.get("billable", True),
            "source": [f"fathom:{recording_id}"],
            "source_label": title,
            "confidence": "high",
            "description": _one_line(
                f"{route.get('prefix', 'SC')} — {title}", DESCRIPTION_LIMIT
            ),
            "rationale": f"Fathom recording {recording_id}; invitee/title route matched",
            "candidate_key": _candidate_key(meeting_record),
            "provenance": _record_provenance(meeting_record),
        }
        if overlaps_existing(candidate, existing):
            skipped.append({
                "id": candidate["candidate_key"],
                "source": "fathom",
                "time": f"{start}–{end}",
                "label": title,
                "reason": "covered by existing Clockify entry overlap",
            })
            continue
        proposals.append(candidate)

    for machine in evidence.get("sessions", []):
        for b in machine.get("claude_bursts", []):
            rid_base = hashlib.sha1(json.dumps(b, sort_keys=True).encode()).hexdigest()[:8]
            if b.get("heartbeat_like"):
                skipped.append({"id": rid_base, "source": "claude", "time": f"{b.get('start')}–{b.get('end')}", "label": b.get("label"), "reason": "heartbeat-like timestamp pattern"})
                continue
            if _is_weekend_short_session(b, rules):
                skipped.append({"id": rid_base, "source": "claude", "time": f"{b.get('start')}–{b.get('end')}", "label": b.get("label"), "reason": "weekend session at or below 60 minutes"})
                continue
            if b.get("duration_minutes", 0) < rules.get("min_minutes", 10) and b.get("user_messages", 0) < rules.get("min_user_messages", 5):
                skipped.append({"id": rid_base, "source": "claude", "time": f"{b.get('start')}–{b.get('end')}", "label": b.get("label"), "reason": "trivial burst below duration/message threshold"})
                continue
            # Skip autopilot health-check sessions (multica-workspaces-*)
            if b.get("label","").startswith("multica-workspaces") or b.get("label","").startswith("multica-workspaces-desktop"):
                skipped.append({"id": rid_base, "source": "claude", "time": f"{b.get('start')}–{b.get('end')}", "label": b.get("label"), "reason": "autopilot health-check session"})
                continue
            route = route_session(b, routing)
            if route["action"] == "skip":
                skipped.append({"id": rid_base, "source": "claude", "time": f"{b.get('start')}–{b.get('end')}", "label": b.get("label"), "reason": route.get("reason")})
                continue
            if route["action"] == "ambiguous":
                ambiguous.append({"id": f"A{len(ambiguous)+1:03d}", "source": "claude", "time": f"{b.get('start')}–{b.get('end')}", "label": b.get("label"), "reason": route.get("reason"), "machine": b.get("machine")})
                continue
            cand = {
                "id": f"P{len(proposals)+1:03d}",
                "start": b.get("start"),
                "end": b.get("end"),
                "duration_minutes": b.get("duration_minutes"),
                "client_project": route.get("project_name"),
                "clockify_project_suffix": route.get("project_suffix"),
                "tag_suffixes": route.get("tag_suffixes", []),
                "tag_names": route.get("tag_names", []),
                "billable": route.get("billable", True),
                "source": [f"claude:{b.get('machine')}"],
                "source_label": b.get("label"),
                "confidence": route.get("confidence", "medium"),
                "description": _make_description(route.get('project_name'), b, route.get('confidence', 'medium'), route.get('prefix', 'SC')),
                "rationale": _make_rationale(b),
                "candidate_key": _candidate_key(b),
                "provenance": _record_provenance(b),
            }
            if overlaps_existing(cand, existing):
                skipped.append({"id": rid_base, "source": "claude", "time": f"{b.get('start')}–{b.get('end')}", "label": b.get("label"), "reason": "covered by existing Clockify entry overlap"})
                continue
            proposals.append(cand)
        for hs in machine.get("hermes_sessions", []):
            rid_base = hashlib.sha1(json.dumps(hs, sort_keys=True).encode()).hexdigest()[:8]
            label = Path(hs.get("path","")).stem
            route = route_session(hs, routing)
            if route["action"] == "skip":
                skipped.append({"id": rid_base, "source": "hermes", "time": f"{hs.get('start')}–{hs.get('end')}", "label": label, "reason": route.get("reason")})
                continue
            if route["action"] == "ambiguous":
                ambiguous.append({"id": f"A{len(ambiguous)+1:03d}", "source": "hermes", "time": f"{hs.get('start')}–{hs.get('end')}", "label": hs.get("evidence_level",""), "reason": route.get("reason"), "machine": hs.get("machine")})
                continue
            est_duration = None
            est_start = parse_dt(hs.get("start"))
            est_end = parse_dt(hs.get("end"))
            if est_start and est_end:
                est_duration = max(3, int((est_end - est_start).total_seconds() / 60))
            weekend_record = {**hs, "duration_minutes": est_duration or 0}
            if _is_weekend_short_session(weekend_record, rules):
                skipped.append({"id": rid_base, "source": "hermes", "time": f"{hs.get('start')}–{hs.get('end')}", "label": label, "reason": "weekend session at or below 60 minutes"})
                continue
            if est_duration and est_duration < rules.get("min_minutes", 10) and hs.get("user_messages", 0) < rules.get("min_user_messages", 5):
                skipped.append({"id": rid_base, "source": "hermes", "time": f"{hs.get('start')}–{hs.get('end')}", "label": label, "reason": "trivial burst below duration/message threshold"})
                continue
            if est_duration and overlaps_existing(hs, existing):
                skipped.append({"id": rid_base, "source": "hermes", "time": f"{hs.get('start')}–{hs.get('end')}", "label": label, "reason": "covered by existing Clockify entry overlap"})
                continue
            cand = {
                "id": f"P{len(proposals)+1:03d}",
                "start": hs.get("start"),
                "end": hs.get("end"),
                "duration_minutes": est_duration,
                "client_project": route.get("project_name"),
                "clockify_project_suffix": route.get("project_suffix"),
                "tag_suffixes": route.get("tag_suffixes", []),
                "tag_names": route.get("tag_names", []),
                "billable": route.get("billable", True),
                "source": [f"hermes:{hs.get('machine')}"],
                "source_label": label,
                "confidence": route.get("confidence", "medium"),
                "description": f"{route.get('prefix', 'SC')} — {label} ({hs.get('user_messages',0)} user msgs across {est_duration or '?'}m, estimated)",
                "rationale": hs.get("evidence_level", "estimated duration"),
                "candidate_key": _candidate_key(hs),
                "provenance": _record_provenance(hs),
            }
            proposals.append(cand)
        for cs in machine.get("codex_sessions", []):
            if cs.get("error"):
                continue
            # Route by cwd first, then title
            route_path = cs.get("cwd", "")
            route_label = route_path.split("/")[-1] if route_path else (cs.get("title") or "codex")
            route = route_session({"label": route_label, "path": route_path}, routing)
            if route["action"] == "skip":
                continue
            if route["action"] == "ambiguous":
                # Try routing by title
                route = route_session({"label": cs.get("title",""), "path": ""}, routing)
                if route["action"] in ("skip", "ambiguous"):
                    continue
            start = cs.get("start") or cs.get("updated_at")
            if not start:
                continue
            end = cs.get("end") or start
            duration = cs.get("duration_minutes")
            weekend_record = {**cs, "start": start, "duration_minutes": duration or 0}
            if _is_weekend_short_session(weekend_record, rules):
                skipped.append({"id": f"P{len(proposals)+1:03d}", "source": "codex", "time": f"{start}–{end}", "label": route_label, "reason": "weekend session at or below 60 minutes"})
                continue
            # Skip trivial bursts (parity with Claude/Hermes thresholds)
            if duration is not None and duration < 10 and (cs.get("user_messages") or 0) < 5:
                skipped.append({"id": f"P{len(proposals)+1:03d}", "source": "codex", "time": f"{start}–{end}", "label": route_label, "reason": "trivial burst below duration/message threshold"})
                continue
            cand = {
                "id": f"P{len(proposals)+1:03d}",
                "start": start,
                "end": end,
                "duration_minutes": duration if duration is not None else 4,
                "client_project": route.get("project_name"),
                "clockify_project_suffix": route.get("project_suffix"),
                "tag_suffixes": route.get("tag_suffixes", []),
                "tag_names": route.get("tag_names", []),
                "billable": route.get("billable", True),
                "source": [f"codex:{cs.get('machine','?')}"],
                "source_label": route_label,
                "confidence": "medium" if duration is not None else "low",
                "description": _make_description(route.get('project_name'), cs, "medium" if duration is not None else "low", route.get('prefix', 'SC')),
                "rationale": _make_rationale(cs),
                "candidate_key": _candidate_key(cs),
                "provenance": _record_provenance(cs),
            }
            proposals.append(cand)
    proposals = _dedupe_replicated_candidates(proposals, skipped)
    proposals = _merge_adjacent_same_work(proposals, skipped)
    proposals = _resolve_candidate_overlaps(proposals, skipped, rules)
    reviewable_proposals = []
    for proposal in proposals:
        if _is_contextless_description(proposal.get("description")):
            ambiguous.append(
                {
                    **proposal,
                    "id": f"A{len(ambiguous)+1:03d}",
                    "machine": (proposal.get("provenance") or {}).get(
                        "source_machine"
                    ),
                    "time": f"{proposal.get('start')}–{proposal.get('end')}",
                    "label": proposal.get("source_label"),
                    "reason": (
                        "No row-specific work context was recoverable; "
                        "review, revise, or reject manually"
                    ),
                }
            )
            continue
        reviewable_proposals.append(proposal)
    proposals = reviewable_proposals
    return proposals, ambiguous, skipped


def write_markdown(run_dir: Path, report: dict[str, Any]) -> None:
    lines = []
    lines.append(f"# Clockify sync dry-run — {report['run_id']}")
    lines.append("")
    lines.append(f"Date range: {report['date_range']['since']} → {report['date_range']['until']} ({report['date_range']['reason']})")
    lines.append(f"Safety: dry-run only; no Clockify writes performed.")
    identity = report.get("runtime_identity", {})
    dirty_suffix = " dirty" if identity.get("git_dirty") else ""
    lines.append(
        f"Collector identity: {identity.get('collector_path')} "
        f"(root: {identity.get('canonical_root')}; "
        f"git: {identity.get('git_sha') or 'unavailable'}{dirty_suffix})"
    )
    lines.append("")
    lines.append("## Evidence status")
    ledger_receipt = report.get("evidence_ledger", {})
    if isinstance(ledger_receipt, Mapping) and ledger_receipt.get("ledger_digest"):
        lines.append(f"- Evidence ledger receipt: {ledger_receipt['ledger_digest']}")
    calendly_evidence = report["evidence"].get("calendly", {"status": "unavailable"})
    lines.append(f"- Clockify: {report['evidence']['clockify']['status']} ({len(report['evidence']['clockify'].get('entries', []))} existing entries)")
    lines.append(f"- Fathom: {report['evidence']['fathom']['status']} ({len(report['evidence']['fathom'].get('meetings', []))} meetings)")
    lines.append(f"- Calendly: {calendly_evidence['status']} ({len(calendly_evidence.get('recordings', []))} recordings; {len(calendly_evidence.get('scheduled_without_recording', []))} scheduled without recording)")
    lines.append(f"- Multica issues: {report['evidence']['multica_issues']['status']} ({len(report['evidence']['multica_issues'].get('issues', []))} issues)")
    for s in report['evidence']['sessions']:
        lines.append(f"- Sessions/{s['machine']}: {s['status']} — {len(s.get('claude_bursts', []))} Claude bursts, {len(s.get('hermes_sessions', []))} Hermes legacy, {len(s.get('hermes_db_sessions', []))} Hermes DB, {len(s.get('codex_sessions', []))} Codex sessions")
        for err in s.get('errors', [])[:5]:
            lines.append(f"  - warning: {err}")
    lines.append("")
    lines.append("## Collection-stage diagnostics")
    lines.append(
        f"- {len(report['proposals'])} legacy candidates, "
        f"{len(report['ambiguous'])} legacy ambiguities, "
        f"{len(report['skipped'])} legacy exclusions"
    )
    lines.append("- These are local diagnostic counts only; their extracted text is not reviewable Clockify output.")
    lines.append("")
    lines.append("## Skipped summary")
    reasons: dict[str, int] = {}
    for s in report['skipped']:
        reasons[s.get('reason','unknown')] = reasons.get(s.get('reason','unknown'), 0) + 1
    if reasons:
        for reason, count in sorted(reasons.items(), key=lambda x: (-x[1], x[0])):
            lines.append(f"- {count}: {reason}")
    else:
        lines.append("- None")
    lines.append("")
    lines.append("## Approval instruction")
    lines.append("Semantic accounting must replace the empty proposal placeholders before durable review. Approve only stable review item IDs from review-snapshot.json. This run did not post entries.")
    (run_dir / "run-report.md").write_text("\n".join(lines) + "\n")


def collector_checkpoint_root() -> Path:
    configured = os.environ.get("CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser()
    return ROOT / "state" / "collector-checkpoints"


def _cleanup_checkpoint_identity_digest(path: Path) -> str | None:
    digest = path.name
    if len(digest) == 64 and all(character in "0123456789abcdef" for character in digest):
        return digest
    return None


def cleanup_checkpoints(args: argparse.Namespace) -> int:
    root = Path(args.checkpoint_root)
    if not root.is_absolute() or root.is_symlink() or not root.is_dir():
        print("checkpoint root must be an existing absolute directory", file=sys.stderr)
        return 2
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.completed_before):
        print("completed-before must be YYYY-MM-DD", file=sys.stderr)
        return 2
    try:
        cutoff = dt.datetime.combine(
            dt.date.fromisoformat(args.completed_before),
            dt.time.min,
            tzinfo=dt.timezone.utc,
        )
    except ValueError:
        print("completed-before must be a valid UTC date", file=sys.stderr)
        return 2

    try:
        checkpoint_roots = tuple(
            backlog / "source-checkpoints"
            for backlog in root.iterdir()
            if not backlog.is_symlink()
            and backlog.is_dir()
            and not (backlog / "source-checkpoints").is_symlink()
            and (backlog / "source-checkpoints").is_dir()
        )
        before = tuple(
            checkpoint
            for checkpoint_root in checkpoint_roots
            for checkpoint in checkpoint_root.iterdir()
            if not checkpoint.is_symlink() and checkpoint.is_dir()
        )
        removed = tuple(
            checkpoint
            for checkpoint_root in checkpoint_roots
            for checkpoint in PageCheckpointStore(checkpoint_root).remove_completed_before(cutoff)
        )
    except (CheckpointError, OSError, ValueError):
        print("checkpoint cleanup failed safely", file=sys.stderr)
        return 2
    removed_set = set(removed)
    digests = sorted(
        digest
        for path in removed_set
        if (digest := _cleanup_checkpoint_identity_digest(path)) is not None
    )
    print(
        f"removed={len(removed_set)} preserved={len(before) - len(removed_set)} "
        f"removed_identity_digests={','.join(digests) or 'none'}"
    )
    return 0


def _backlog_compatibility_version(
    routing: Mapping[str, Any], fleet: Mapping[str, Any]
) -> str:
    payload = {
        "contract": BACKLOG_COMPATIBILITY_VERSION,
        "collector_sha256": collector_script_sha256(),
        "routing": routing,
        "fleet": fleet,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{BACKLOG_COMPATIBILITY_VERSION}:{hashlib.sha256(encoded.encode()).hexdigest()}"


def _slice_run_dir(slice_: CollectionSlice, compatibility_version: str) -> Path:
    since = slice_.since.astimezone(BUCHAREST).strftime("%Y%m%d")
    until = slice_.until.astimezone(BUCHAREST).strftime("%Y%m%d")
    compatibility_digest = hashlib.sha256(compatibility_version.encode()).hexdigest()
    return RUNS / f"{since}-{until}-{slice_.slice_id[7:]}-{compatibility_digest}"


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _verified_existing_slice_bundle(
    run_dir: Path, since: dt.datetime, until: dt.datetime, reason: str
) -> tuple[Path, dict[str, object]]:
    report_path = run_dir / "run-report.md"
    report_json_path = run_dir / "run-report.json"
    ledger_path = run_dir / "evidence" / "evidence-ledger.json"
    if (
        run_dir.is_symlink()
        or not run_dir.is_dir()
        or any(path.is_symlink() or not path.is_file() for path in (report_path, report_json_path, ledger_path))
    ):
        raise BacklogError("existing slice bundle is missing required immutable artifacts")
    try:
        markdown = report_path.read_text()
        report = json.loads(report_json_path.read_text())
        ledger = json.loads(ledger_path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise BacklogError("existing slice bundle is invalid") from error
    if not isinstance(report, dict) or not isinstance(ledger, dict):
        raise BacklogError("existing slice bundle document is invalid")
    if report.get("run_id") != run_dir.name or report.get("date_range") != {
        "since": local_dt_string(since),
        "until": local_dt_string(until),
        "reason": reason,
    }:
        raise BacklogError("existing slice bundle identity does not match")
    manifest = ledger.get("manifest")
    completeness = manifest.get("source_completeness") if isinstance(manifest, dict) else None
    if not isinstance(completeness, Mapping) or completeness.get("status") != "complete":
        raise BacklogError("existing slice bundle is not complete")
    reported_ledger = report.get("evidence_ledger")
    if not isinstance(reported_ledger, Mapping):
        raise BacklogError("existing slice bundle has no ledger receipt")
    expected_ledger = {
        "manifest_id": manifest.get("manifest_id"),
        "event_count": manifest.get("event_count"),
        "events_digest": manifest.get("events_digest"),
        "source_completeness": completeness,
        "ledger_digest": _file_digest(ledger_path),
    }
    if any(reported_ledger.get(key) != value for key, value in expected_ledger.items()):
        raise BacklogError("existing slice bundle ledger receipt does not match")
    if f"- Evidence ledger receipt: {expected_ledger['ledger_digest']}\n" not in markdown:
        raise BacklogError("existing slice bundle Markdown receipt does not bind its ledger")
    return report_path, report


def _slice_is_complete(report: Mapping[str, object]) -> bool:
    ledger = report.get("evidence_ledger")
    if not isinstance(ledger, Mapping):
        return False
    completeness = ledger.get("source_completeness")
    evidence = report.get("evidence")
    calendly = evidence.get("calendly") if isinstance(evidence, Mapping) else None
    return (
        isinstance(completeness, Mapping)
        and completeness.get("status") == "complete"
        and isinstance(calendly, Mapping)
        and calendly.get("status") == "ok"
        and calendly.get("complete") is True
    )


def _claim_slice_run_dir(run_dir: Path) -> bool:
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        run_dir.mkdir()
    except FileExistsError:
        return False
    return True


def _preserve_incomplete_run(
    run_dir: Path, *, owned_by_current_invocation: bool
) -> None:
    if (
        not owned_by_current_invocation
        or not run_dir.exists()
        or run_dir.is_symlink()
        or not run_dir.is_dir()
    ):
        return
    candidate = run_dir.with_name(f"{run_dir.name}-incomplete")
    suffix = 1
    while candidate.exists():
        candidate = run_dir.with_name(f"{run_dir.name}-incomplete-{suffix}")
        suffix += 1
    os.replace(run_dir, candidate)


def _collect_slice(
    args: argparse.Namespace,
    routing: dict[str, Any],
    fleet: dict[str, Any],
    cenv: dict[str, str],
    fenv: dict[str, str],
    since: dt.datetime,
    until: dt.datetime,
    reason: str,
    checkpoint_store: PageCheckpointStore,
    run_dir: Path,
    owned_run_dirs: set[Path] | None = None,
    *,
    calendly_env: dict[str, str] | None = None,
) -> tuple[Path, dict[str, object]]:
    requested_slices = plan_slices(since, until, zone=BUCHAREST)
    if len(requested_slices) != 1:
        raise ValueError("_collect_slice requires one bounded collection slice")
    claimed_run_dir = _claim_slice_run_dir(run_dir)
    if not claimed_run_dir:
        return _verified_existing_slice_bundle(run_dir, since, until, reason)
    if owned_run_dirs is not None:
        owned_run_dirs.add(run_dir)
    runtime_identity = collector_runtime_identity()
    run_id = run_dir.name

    evidence = {
        "clockify": fetch_clockify(
            cenv, routing, since, until, checkpoint_store=checkpoint_store
        ),
        "fathom": fetch_fathom(fenv, since, until, checkpoint_store=checkpoint_store),
        "calendly": fetch_calendly(
            calendly_env or {"_missing": ["CALENDLY_RECORDINGS_URL"]},
            since,
            until,
            checkpoint_store=checkpoint_store,
        ),
        "multica_issues": fetch_multica_issues(
            since, until, checkpoint_store=checkpoint_store
        ),
        "sessions": [],
    }
    for m in fleet.get("machines", []):
        if not m.get("enabled", True):
            continue
        if machine_is_local(m):
            evidence["sessions"].append(collect_local_sessions(m, since, until))
        elif m.get("kind") in ("ssh", "auto"):
            evidence["sessions"].append(
                collect_remote_sessions(
                    m,
                    since,
                    until,
                    fleet.get("ssh_options", []),
                    coordinator_identity=runtime_identity,
                )
            )

    # Enriched session context (user messages with prev/next assistant)
    if getattr(args, 'enrich', False):
        enriched = {"claude_contexts": [], "hermes_contexts": []}
        for m in fleet.get("machines", []):
            cbase = m.get("claude_projects", "")
            if cbase:
                cpath = Path(cbase)
                if cpath.exists():
                    for p in sorted(cpath.glob("*/*.jsonl")):
                        sp = str(p)
                        if any(f in sp for f in ["/subagents/", "multica-command", "claude-mem-observer", "/multica/"]):
                            continue
                        ctx = extract_claude_jsonl_context(p, since, until)
                        if ctx and ctx.get("user_messages"):
                            ctx["machine"] = m.get("name")
                            enriched["claude_contexts"].append(ctx)
            hdb = m.get("hermes_db")
            if hdb and Path(hdb).exists():
                hermes_ctx = extract_hermes_db_context(hdb, since, until)
                for hc in hermes_ctx:
                    hc["machine"] = m.get("name")
                enriched["hermes_contexts"].extend(hermes_ctx)
        evidence["enriched_context"] = enriched
        print(f"Enriched: {len(enriched['claude_contexts'])} Claude, {len(enriched['hermes_contexts'])} Hermes sessions across {len(fleet.get('machines', []))} machines", file=sys.stderr)

    proposals, ambiguous, skipped = build_proposals(evidence, routing)
    report = {
        "run_id": run_id,
        "runtime_identity": runtime_identity,
        "date_range": {"since": local_dt_string(since), "until": local_dt_string(until), "reason": reason},
        "safety": {"dry_run": True, "clockify_posted": False},
        "paths": {
            "run_dir": str(run_dir),
            "report_json": str(run_dir / "run-report.json"),
            "report_md": str(run_dir / "run-report.md"),
            "proposals_json": str(run_dir / "proposals.json"),
            "legacy_proposals_json": str(run_dir / "legacy-proposals.json"),
        },
        "evidence": evidence,
        "proposals": proposals,
        "ambiguous": ambiguous,
        "skipped": skipped,
        "issue_reconciliation": {"matched_existing_issues": [], "proposed_multica_comments": [], "no_action_items": ["Downstream issue mutations are disabled by default."]},
    }
    write_json(run_dir / "evidence" / "clockify-existing.json", evidence["clockify"])
    write_json(run_dir / "evidence" / "fathom-meetings.json", evidence["fathom"])
    write_json(run_dir / "evidence" / "calendly-recordings.json", evidence["calendly"])
    write_json(run_dir / "evidence" / "multica-issues.json", evidence["multica_issues"])
    write_json(run_dir / "evidence" / "sessions.json", evidence["sessions"])
    if "enriched_context" in evidence:
        write_json(run_dir / "evidence" / "enriched-context.json", evidence["enriched_context"])
    try:
        from scripts.evidence_ledger import (
            EvidenceLedger,
            normalize_collector_snapshot,
            source_inventory_from_collector,
        )
    except ModuleNotFoundError:
        from evidence_ledger import (  # type: ignore[no-redef]
            EvidenceLedger,
            normalize_collector_snapshot,
            source_inventory_from_collector,
        )
    ledger = EvidenceLedger(
        tuple(normalize_collector_snapshot(evidence)),
        source_inventory_from_collector(evidence),
        BUCHAREST.key,
    )
    ledger_path = run_dir / "evidence" / "evidence-ledger.json"
    write_json(
        ledger_path,
        {
            "schema_version": ledger.manifest.schema_version,
            "manifest": ledger.manifest.document(),
            "events": [event.document() for event in ledger.events],
        },
    )
    report["paths"]["evidence_ledger"] = str(ledger_path)
    report["evidence_ledger"] = {
        "manifest_id": ledger.manifest.manifest_id,
        "event_count": ledger.manifest.event_count,
        "events_digest": ledger.manifest.events_digest,
        "source_completeness": ledger.manifest.document()["source_completeness"],
        "ledger_digest": _file_digest(ledger_path),
    }
    write_json(run_dir / "legacy-proposals.json", proposals)
    write_json(run_dir / "legacy-ambiguous.json", ambiguous)
    write_json(run_dir / "legacy-skipped.json", skipped)
    # Only work_accounting_pipeline may populate reviewable artifacts.  Empty
    # placeholders keep a blocked collector run from exposing extraction-based
    # descriptions as if they were semantic recommendations.
    write_json(run_dir / "proposals.json", [])
    write_json(run_dir / "ambiguous.json", [])
    write_json(run_dir / "skipped.json", [])
    # Write a COMPACT run-report.json: the full evidence (enriched_context + per-message
    # session data) is megabytes and already lives in evidence/ files. Embedding it here
    # bloated the report to ~2.4MB and was a likely trigger for the agent's provider
    # HTTP 400 on oversized payloads. Keep only summaries + pointers; the agent reads
    # the evidence/ files by path when it needs detail.
    sess_summary = []
    for s in evidence.get("sessions", []):
        sess_summary.append({
            "machine": s.get("machine"),
            "status": s.get("status"),
            "claude_bursts": len(s.get("claude_bursts", [])),
            "hermes_sessions": len(s.get("hermes_sessions", [])),
            "hermes_db_sessions": len(s.get("hermes_db_sessions", [])),
            "codex_sessions": len(s.get("codex_sessions", [])),
            "repository_events": len(s.get("repository_events", [])),
            "repository_evidence_status": s.get("repository_evidence_status"),
            "errors": s.get("errors", []),
        })
    compact = dict(report)
    compact["legacy_candidate_summary"] = {
        "candidates": len(proposals),
        "ambiguous": len(ambiguous),
        "skipped": len(skipped),
    }
    compact.pop("proposals", None)
    compact.pop("ambiguous", None)
    compact.pop("skipped", None)
    compact["evidence"] = {
        "clockify": {"status": evidence.get("clockify", {}).get("status"),
                     "entry_count": len(evidence.get("clockify", {}).get("entries", []))},
        "fathom": {"status": evidence.get("fathom", {}).get("status"),
                   "meeting_count": len(evidence.get("fathom", {}).get("meetings", []))},
        "calendly": {
            "status": evidence.get("calendly", {}).get("status"),
            "complete": evidence.get("calendly", {}).get("complete") is True,
            "recording_count": len(evidence.get("calendly", {}).get("recordings", [])),
            "scheduled_without_recording_count": len(
                evidence.get("calendly", {}).get("scheduled_without_recording", [])
            ),
        },
        "multica_issues": {"status": evidence.get("multica_issues", {}).get("status"),
                           "issue_count": len(evidence.get("multica_issues", {}).get("issues", []))},
        "sessions": sess_summary,
        "evidence_files": {
            "clockify": str(run_dir / "evidence" / "clockify-existing.json"),
            "fathom": str(run_dir / "evidence" / "fathom-meetings.json"),
            "calendly": str(run_dir / "evidence" / "calendly-recordings.json"),
            "multica_issues": str(run_dir / "evidence" / "multica-issues.json"),
            "sessions": str(run_dir / "evidence" / "sessions.json"),
            "enriched_context": str(run_dir / "evidence" / "enriched-context.json") if "enriched_context" in evidence else None,
            "evidence_ledger": str(ledger_path),
        },
    }
    write_json(run_dir / "run-report.json", compact)
    write_markdown(run_dir, report)
    return run_dir / "run-report.md", report


def run(args: argparse.Namespace) -> int:
    routing = load_json(ROOT / "routing.json")
    fleet = load_json(ROOT / "fleet.json")
    cenv = load_env_file(clockify_env_candidates(), ["CLOCKIFY_API_KEY", "CLOCKIFY_WORKSPACE_ID"])
    fenv = load_env_file(fathom_env_candidates(), ["FATHOM_API_KEY"])
    calendly_env = load_env_file(
        calendly_env_candidates(),
        [
            "CALENDLY_RECORDINGS_URL",
            "CALENDLY_GATEWAY_TOKEN",
            "CALENDLY_GATEWAY_READ_ONLY",
        ],
    )
    since, until, reason = compute_range(args, routing, cenv)
    slices = plan_slices(since, until, zone=BUCHAREST)
    identity = BacklogIdentity(
        since_utc=iso_utc(since),
        until_utc=iso_utc(until),
        timezone=BUCHAREST.key,
        max_days=2,
        compatibility_version=_backlog_compatibility_version(routing, fleet),
    )
    backlog_store = BacklogStore(collector_checkpoint_root())
    try:
        backlog = backlog_store.open(identity, slices)
    except BacklogError:
        print("collector backlog state is not safe to resume", file=sys.stderr)
        return 2
    checkpoint_store = PageCheckpointStore(backlog.directory / "source-checkpoints")
    receipts = {receipt.slice_id: receipt for receipt in backlog.completed}

    for slice_ in backlog.slices:
        receipt = receipts.get(slice_.slice_id)
        if receipt is not None:
            expected_report_path = (
                _slice_run_dir(slice_, identity.compatibility_version) / "run-report.md"
            )
            if receipt.result_path != expected_report_path:
                print("collector slice receipt is not safe to reuse", file=sys.stderr)
                return 2
            try:
                report_path, report = _verified_existing_slice_bundle(
                    receipt.result_path.parent,
                    slice_.since,
                    slice_.until,
                    reason,
                )
            except (BacklogError, OSError, ValueError):
                print("collector slice receipt is not safe to reuse", file=sys.stderr)
                return 2
            if report_path != expected_report_path or not _slice_is_complete(report):
                print("collector slice receipt is not safe to reuse", file=sys.stderr)
                return 2
            print(str(report_path), flush=True)
            del report
            continue
        run_dir = _slice_run_dir(slice_, identity.compatibility_version)
        owned_run_dirs: set[Path] = set()
        try:
            report_path, report = _collect_slice(
                args,
                routing,
                fleet,
                cenv,
                fenv,
                slice_.since,
                slice_.until,
                reason,
                checkpoint_store,
                run_dir,
                owned_run_dirs,
                calendly_env=calendly_env,
            )
        except (BacklogError, CheckpointError, OSError, ValueError):
            _preserve_incomplete_run(
                run_dir,
                owned_by_current_invocation=run_dir in owned_run_dirs,
            )
            print("collector slice did not complete safely", file=sys.stderr)
            return 2
        if not _slice_is_complete(report):
            _preserve_incomplete_run(
                run_dir,
                owned_by_current_invocation=run_dir in owned_run_dirs,
            )
            return 2
        try:
            backlog = backlog_store.record_complete(
                backlog, slice_.slice_id, report_path, _file_digest(report_path)
            )
        except (BacklogError, OSError):
            print("collector slice receipt could not be recorded safely", file=sys.stderr)
            return 2
        receipts = {receipt.slice_id: receipt for receipt in backlog.completed}
        print(str(report_path), flush=True)
        del report
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Clockify sync dry-run collector")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--since", help="YYYY-MM-DD")
    r.add_argument("--until", help="YYYY-MM-DD inclusive")
    r.add_argument("--enrich", action="store_true", default=True,
                    help="Extract user message context (prev/next assistant) for enriched descriptions (default: on)")
    r.add_argument("--no-enrich", action="store_false", dest="enrich",
                    help="Disable enriched context extraction")
    export = sub.add_parser("export-local")
    export.add_argument("--machine-json", required=True)
    export.add_argument("--since", required=True)
    export.add_argument("--until", required=True)
    export.add_argument("--expected-collector-sha256", required=True)
    export.add_argument("--coordinator-git-sha")
    export.add_argument("--encoded-output", action="store_true")
    cleanup = sub.add_parser("cleanup-checkpoints")
    cleanup.add_argument("--completed-before", required=True, help="UTC date boundary YYYY-MM-DD")
    cleanup.add_argument("--checkpoint-root", required=True, help="existing absolute checkpoint root")
    args = ap.parse_args()
    if args.cmd == "run":
        return run(args)
    if args.cmd == "cleanup-checkpoints":
        return cleanup_checkpoints(args)
    if args.cmd == "export-local":
        machine = json.loads(args.machine_json)
        if not isinstance(machine, dict) or not machine.get("name"):
            raise ValueError("export-local requires a named machine object")
        since = parse_dt(args.since)
        until = parse_dt(args.until)
        if not since or not until or until <= since:
            raise ValueError("export-local requires a valid since/until range")
        expected_digest = str(args.expected_collector_sha256).strip()
        actual_digest = collector_script_sha256()
        runtime_identity = collector_runtime_identity()
        if expected_digest != actual_digest:
            mismatch = {
                "machine": machine["name"],
                "status": "unavailable",
                "canonical_export_attestation": {
                    "collector_script_sha256": actual_digest,
                    "runtime_identity": runtime_identity,
                },
            }
            print(
                canonical_export_envelope(mismatch)
                if args.encoded_output
                else json.dumps(mismatch, ensure_ascii=False)
            )
            return 0
        exported = collect_local_sessions(machine, since, until)
        exported["canonical_export_attestation"] = {
            "collector_script_sha256": actual_digest,
            "runtime_identity": runtime_identity,
        }
        print(
            canonical_export_envelope(exported)
            if args.encoded_output
            else json.dumps(exported, ensure_ascii=False)
        )
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
