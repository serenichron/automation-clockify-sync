#!/usr/bin/env python3
"""Extract session context from Claude JSONL and Hermes DB for Clockify reconciliation.

For each session, extracts:
- All user messages with timestamps
- Assistant message before (context that prompted user response)
- Assistant message after (response to user)
- Last message of session (conclusion)
- Skips tool calls, system messages, attachments

Output: structured JSON per session with enough detail to understand what work was done.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

BUCHAREST = dt.timezone(dt.timedelta(hours=3))


def parse_dt(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    try:
        parsed = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except Exception:
        return None


def local_str(d: dt.datetime | None) -> str | None:
    if not d:
        return None
    return d.astimezone(BUCHAREST).strftime("%Y-%m-%d %H:%M")


def extract_claude_jsonl_context(path: Path, since: dt.datetime, until: dt.datetime) -> dict[str, Any] | None:
    """Extract user messages with context from a Claude Code JSONL session file."""
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

        parsed.append({
            "type": t,
            "content": str(content),
            "timestamp": ts,
        })

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
            "timestamp": local_str(p["timestamp"]),
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

    return {
        "source": "claude_jsonl",
        "session_id": path.stem,
        "path": str(path),
        "label": label,
        "start": local_str(first_ts),
        "end": local_str(last_ts),
        "duration_hours": round((last_ts - first_ts).total_seconds() / 3600, 2),
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
                parsed.append({
                    "role": role,
                    "content": str(content or ""),
                    "timestamp": msg_dt,
                    "tool_name": tool_name or "",
                })

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
                    "timestamp": local_str(p["timestamp"]),
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

            out.append({
                "source": "hermes_db",
                "session_id": sid,
                "model": model,
                "cwd": cwd or "",
                "start": local_str(start_dt),
                "end": local_str(end_dt) if end_dt else None,
                "duration_hours": round((end_dt - start_dt).total_seconds() / 3600, 2) if end_dt else None,
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


def main() -> int:
    ap = argparse.ArgumentParser(description="Extract session context for Clockify reconciliation")
    ap.add_argument("--since", required=True, help="YYYY-MM-DD")
    ap.add_argument("--until", required=True, help="YYYY-MM-DD")
    ap.add_argument("--claude-projects", default=str(Path.home() / ".claude/projects"),
                    help="Claude Code projects directory")
    ap.add_argument("--hermes-db", default=str(Path.home() / ".hermes/state.db"),
                    help="Hermes state.db path")
    ap.add_argument("--output", default=None, help="Output JSON file path")
    ap.add_argument("--max-sessions", type=int, default=0,
                    help="Max sessions to process (0 = all)")
    args = ap.parse_args()

    since = dt.datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=BUCHAREST)
    until = dt.datetime.strptime(args.until, "%Y-%m-%d").replace(tzinfo=BUCHAREST) + dt.timedelta(days=1)

    results: list[dict[str, Any]] = []

    # 1. Claude JSONL files
    cbase = Path(args.claude_projects)
    if cbase.exists():
        jsonl_files = sorted(cbase.glob("*/*.jsonl"))
        count = 0
        for p in jsonl_files:
            sp = str(p)
            if any(f in sp for f in ["/subagents/", "multica-command", "claude-mem-observer",
                                      "/multica/"]):
                continue
            ctx = extract_claude_jsonl_context(p, since, until)
            if ctx and ctx.get("user_messages"):
                results.append(ctx)
                count += 1
                if args.max_sessions and count >= args.max_sessions:
                    break
        print(f"Claude JSONL: {count} sessions with user messages", file=sys.stderr)

    # 2. Hermes DB
    if Path(args.hermes_db).exists():
        hermes_sessions = extract_hermes_db_context(args.hermes_db, since, until)
        hermes_with_msgs = [s for s in hermes_sessions if s.get("user_messages") and "error" not in s]
        results.extend(hermes_with_msgs)
        print(f"Hermes DB: {len(hermes_with_msgs)} sessions with user messages", file=sys.stderr)

    results.sort(key=lambda x: x.get("start") or "")

    total_user_msgs = sum(len(r.get("user_messages", [])) for r in results)
    print(f"\nTotal: {len(results)} sessions, {total_user_msgs} user messages", file=sys.stderr)

    output = {
        "date_range": {"since": args.since, "until": args.until},
        "total_sessions": len(results),
        "total_user_messages": total_user_msgs,
        "sessions": results,
    }

    if args.output:
        Path(args.output).write_text(json.dumps(output, indent=2, ensure_ascii=False, default=str))
        print(f"Output written to {args.output}", file=sys.stderr)
    else:
        print(json.dumps(output, indent=2, ensure_ascii=False, default=str))

    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(main())
