#!/usr/bin/env python3
"""Clockify reconciliation dry-run collector for Serenichron.

Collects sanitized evidence from Clockify, Fathom, Multica, and local/remote
Hermes/Claude session metadata. Writes a run bundle under ../runs/<run_id>/.
No Clockify writes are performed by this script.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
RUNS = ROOT / "runs"
CLOCKIFY_API = "https://api.clockify.me/api/v1"
FATHOM_API = "https://api.fathom.ai/external/v1"
BUCHAREST = dt.timezone(dt.timedelta(hours=3))  # display timezone; DST precision is non-critical for first dry-run

CLOCKIFY_ENV_CANDIDATES = [
    os.environ.get("CLOCKIFY_ENV_FILE", ""),
    str(Path.home() / ".config/serenichron/clockify.env"),
    str(Path.home() / "Work/clockify/.env"),
    "/home/blackthorne/Work/clockify/.env",
]
FATHOM_ENV_CANDIDATES = [
    os.environ.get("FATHOM_ENV_FILE", ""),
    str(Path.home() / ".config/serenichron/fathom.env"),
]
MULTICA_PROFILE = "desktop-api.multica.ai"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n")


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


def http_json(url: str, headers: dict[str, str]) -> Any:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def clockify_get(path: str, cenv: dict[str, str]) -> Any:
    return http_json(CLOCKIFY_API + path, {"X-Api-Key": cenv["CLOCKIFY_API_KEY"]})


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
            since = (latest.astimezone(BUCHAREST) - dt.timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            reason = "one day before latest Clockify entry"
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
    fragments = ["/subagents/", "paperclip-instances", "claude-mem-observer", "serenichron-paperclip-command"]
    return any(f in path for f in fragments)


def _extract_message_text(content: Any) -> str:
    """Extract text from a Claude JSONL message content field (string or list)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(item.get("text", ""))
                elif item.get("type") == "tool_use":
                    parts.append("[tool_use: " + item.get("name", "?") + "]")
                elif item.get("type") == "tool_result":
                    parts.append("[tool_result]")
        return " ".join(parts)
    return ""


def _is_auto_generated(text: str) -> bool:
    """Check if a message text looks auto-generated by Claude Code CLI."""
    if not text:
        return True
    if text.startswith("<") or text.startswith("</"):
        return True
    if text.startswith("A session-scoped Stop hook"):
        return True
    if text.startswith("Goal set"):
        return True
    if text.startswith("<local-command-stdout"):
        return True
    if text.startswith("<local-command-result"):
        return True
    if text.startswith("Session-scoped"):
        return True
    return False


def parse_claude_jsonl_file(path: Path | str, base: str, since: dt.datetime, until: dt.datetime, machine: str) -> list[dict[str, Any]]:
    out = []
    ts_list: list[dt.datetime] = []
    first_user: str = ""
    last_assistant: str = ""
    p = Path(path)
    if is_skip_path(str(p)):
        return out
    try:
        for line in p.read_text(errors="ignore").splitlines():
            try:
                obj = json.loads(line)
            except Exception:
                continue
            t = parse_dt(obj.get("timestamp"))
            if not t or not (since <= t.astimezone(BUCHAREST) < until):
                continue
            msg_type = obj.get("type")
            content = obj.get("message", {}).get("content", "")
            text = _extract_message_text(content)
            if msg_type == "user":
                if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
                    continue
                ts_list.append(t.astimezone(BUCHAREST))
                if not first_user and not _is_auto_generated(text):
                    first_user = text[:300]
            elif msg_type == "assistant" and text:
                last_assistant = text[:300]
    except Exception:
        return out
    ts_list.sort()
    if not ts_list:
        return out
    bursts = []
    start = end = ts_list[0]
    count = 1
    for t in ts_list[1:]:
        if (t - end).total_seconds() > 30 * 60:
            bursts.append((start, end, count))
            start = t
            count = 1
        else:
            count += 1
        end = t
    bursts.append((start, end, count))
    label = label_from_claude_path(str(p), base)
    for bs, be, cnt in bursts:
        duration = max(1, int((be - bs).total_seconds() / 60))
        heartbeat = all(t.minute in (29, 44) for t in ts_list if bs <= t <= be)
        out.append({
            "source": "claude",
            "machine": machine,
            "session_id": p.stem,
            "path": str(p),
            "label": label,
            "start": local_dt_string(bs),
            "end": local_dt_string(be),
            "duration_minutes": duration,
            "user_messages": cnt,
            "heartbeat_like": heartbeat,
            "first_user_message": first_user,
            "last_assistant_message": last_assistant,
            "evidence_level": "timestamps+user_message_count",
        })
    return out


def collect_local_sessions(machine: dict[str, Any], since: dt.datetime, until: dt.datetime) -> dict[str, Any]:
    result = {"machine": machine["name"], "status": "ok", "claude_bursts": [], "hermes_sessions": [], "errors": []}
    cbase = machine.get("claude_projects")
    if cbase and Path(cbase).exists():
        for p in Path(cbase).glob("*/*.jsonl"):
            result["claude_bursts"].extend(parse_claude_jsonl_file(p, cbase, since, until, machine["name"]))
    else:
        result["errors"].append(f"claude_projects not found: {cbase}")
    hbase = machine.get("hermes_sessions")
    if hbase and Path(hbase).exists():
        for p in Path(hbase).glob("session_*.json"):
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
                # Estimate span
                if start and last:
                    real_span_td = (last - start).total_seconds() / 3600  # hours
                else:
                    real_span_td = 0
                # Heuristic for active duration
                if real_span_td >= 4:
                    # Long-running persistent Hermes session: wall-clock span is not billable
                    # Estimate: ~7 min per user msg + ~0.5 min per assistant/tool msg
                    est_minutes = int(user_count * 7 + (total_count - user_count) * 0.5)
                    estimate_start = start or touch
                    estimate_end = estimate_start + dt.timedelta(minutes=est_minutes)
                    evidence = f"session spans {real_span_td:.1f}h but has only {user_count} user msgs; estimated {est_minutes}m via content heuristic"
                else:
                    # Short enough that wall-clock span is reasonable
                    estimate_start = start
                    estimate_end = last
                    evidence = f"session_start/last_updated used (span={real_span_td:.1f}h, {user_count} user msgs)"
                result["hermes_sessions"].append({
                    "source": "hermes",
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
                })
            except Exception as e:
                result["errors"].append(f"hermes parse failed {p.name}: {e}")
    else:
        result["errors"].append(f"hermes_sessions not found: {hbase}")
    return result


def collect_remote_sessions(machine: dict[str, Any], since: dt.datetime, until: dt.datetime, ssh_options: list[str]) -> dict[str, Any]:
    host = machine["host"]
    result = {"machine": machine["name"], "status": "unavailable", "claude_bursts": [], "hermes_sessions": [], "errors": []}
    remote_code = "\nimport datetime as dt, json\nfrom pathlib import Path\nBUCHAREST=dt.timezone(dt.timedelta(hours=3))\nMACHINE=__MACHINE__\nCBASE=__CBASE__\nHBASE=__HBASE__\nSINCE=dt.datetime.fromisoformat(__SINCE__)\nUNTIL=dt.datetime.fromisoformat(__UNTIL__)\ndef parse_dt(s):\n    if not s: return None\n    try:\n        x=dt.datetime.fromisoformat(str(s).replace('Z','+00:00'))\n        return x if x.tzinfo else x.replace(tzinfo=BUCHAREST)\n    except Exception: return None\ndef local_str(x):\n    return x.astimezone(BUCHAREST).strftime('%Y-%m-%d %H:%M') if x else None\ndef label(path, base):\n    rel=str(path).replace(base,'').lstrip('/')\n    top=rel.split('/')[0]\n    for pref in ['-Users-blackthorne-Work-','-Users-blackthorne-','-home-blackthorne-Work-','-home-blackthorne-']:\n        if top.startswith(pref): return top[len(pref):]\n    return top\ndef skip_path(p):\n    return any(f in str(p) for f in ['/subagents/','paperclip-instances','claude-mem-observer','serenichron-paperclip-command'])\ndef extract_text(c):\n    if isinstance(c,str): return c\n    if isinstance(c,list):\n        parts=[]\n        for item in c:\n            if isinstance(item,dict):\n                if item.get('type')=='text': parts.append(item.get('text',''))\n                elif item.get('type')=='tool_use': parts.append('[tool_use: '+item.get('name','?')+']')\n                elif item.get('type')=='tool_result': parts.append('[tool_result]')\n        return ' '.join(parts)\n    return ''\ndef is_auto(t):\n    if not t: return True\n    if t.startswith('<') or t.startswith('</'): return True\n    if t.startswith('A session-scoped Stop hook'): return True\n    if t.startswith('Goal set'): return True\n    if t.startswith('<local-command-stdout'): return True\n    if t.startswith('<local-command-result'): return True\n    if t.startswith('Session-scoped'): return True\n    return False\ndef parse_claude(p):\n    if skip_path(p): return []\n    first_user=''; last_assistant=''; ts=[]\n    try:\n        for line in Path(p).read_text(errors='ignore').splitlines():\n            try: o=json.loads(line)\n            except Exception: continue\n            t=parse_dt(o.get('timestamp'))\n            if not t or not (SINCE <= t.astimezone(BUCHAREST) < UNTIL): continue\n            msg_type=o.get('type')\n            c=o.get('message',{}).get('content','')\n            text=extract_text(c)\n            if msg_type=='user':\n                if isinstance(c,list) and any(isinstance(i,dict) and i.get('type')=='tool_result' for i in c): continue\n                ts.append(t.astimezone(BUCHAREST))\n                if not first_user and not is_auto(text): first_user=text[:300]\n            elif msg_type=='assistant' and text:\n                last_assistant=text[:300]\n    except Exception: return []\n    ts.sort(); out=[]\n    if not ts: return out\n    bs=be=ts[0]; cnt=1; bursts=[]\n    for t in ts[1:]:\n        if (t-be).total_seconds()>1800:\n            bursts.append((bs,be,cnt)); bs=t; cnt=1\n        else: cnt+=1\n        be=t\n    bursts.append((bs,be,cnt)); lab=label(str(p), CBASE)\n    for bs,be,cnt in bursts:\n        inb=[t for t in ts if bs<=t<=be]\n        out.append({'source':'claude','machine':MACHINE,'session_id':Path(p).stem,'path':str(p),'label':lab,'start':local_str(bs),'end':local_str(be),'duration_minutes':max(1,int((be-bs).total_seconds()/60)),'user_messages':cnt,'heartbeat_like':all(t.minute in (29,44) for t in inb),'first_user_message':first_user,'last_assistant_message':last_assistant,'evidence_level':'remote timestamps+user_message_count'})\n    return out\nres={'machine':MACHINE,'status':'ok','claude_bursts':[],'hermes_sessions':[],'errors':[]}\ntry:\n    cb=Path(CBASE)\n    if cb.exists():\n        for p in cb.glob('*/*.jsonl'): res['claude_bursts'].extend(parse_claude(p))\n    else: res['errors'].append('claude_projects not found: '+CBASE)\nexcept Exception as e: res['errors'].append('claude parse: '+str(e)[:200])\ntry:\n    hb=Path(HBASE)\n    if hb.exists():\n        for p in hb.glob('session_*.json'):\n            try:\n                o=json.loads(p.read_text(errors='ignore'))\n                st=parse_dt(o.get('session_start')); lu=parse_dt(o.get('last_updated')); touch=lu or st\n                if not (touch and SINCE <= touch.astimezone(BUCHAREST) < UNTIL): continue\n                msgs=o.get('messages',[]); user_c=sum(1 for m in msgs if m.get('role')=='user')\n                total_c=o.get('message_count') or len(msgs)\n                if st and lu:\n                    span_h=(lu-st).total_seconds()/3600\n                else: span_h=0\n                if span_h>=4:\n                    est_m=int(user_c*7+(total_c-user_c)*0.5)\n                    est_e=st+dt.timedelta(minutes=est_m) if st else touch\n                    ev=f'session spans {span_h:.1f}h but has only {user_c} user msgs; estimated {est_m}m via content heuristic'\n                else:\n                    est_e=lu; ev=f'session_start/last_updated used (span={span_h:.1f}h, {user_c} user msgs)'\n                res['hermes_sessions'].append({'source':'hermes','machine':MACHINE,'session_id':o.get('session_id') or p.stem,'path':str(p),'start':local_str(st),'end':local_str(est_e),'real_span_hours':round(span_h,2),'user_messages':user_c,'total_messages':total_c,'model':o.get('model'),'platform':o.get('platform'),'evidence_level':ev})\n            except Exception as e: res['errors'].append('hermes parse failed '+p.name+': '+str(e)[:120])\n    else: res['errors'].append('hermes_sessions not found: '+HBASE)\nexcept Exception as e: res['errors'].append('hermes scan: '+str(e)[:200])\nprint(json.dumps(res))\n"
    remote_code = (remote_code
        .replace('__MACHINE__', repr(machine['name']))
        .replace('__CBASE__', repr(machine.get('claude_projects', '')))
        .replace('__HBASE__', repr(machine.get('hermes_sessions', '')))
        .replace('__SINCE__', repr(since.isoformat()))
        .replace('__UNTIL__', repr(until.isoformat())))
    cmd = ["ssh", *ssh_options, host, "python3 - <<'PY'\n" + remote_code + "\nPY"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        if proc.returncode != 0:
            result["errors"].append(proc.stderr.strip()[:500] or proc.stdout.strip()[:500])
            return result
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except Exception as e:
        result["errors"].append(str(e))
    return result


def fetch_clockify(cenv: dict[str, str], routing: dict[str, Any], since: dt.datetime, until: dt.datetime) -> dict[str, Any]:
    if cenv.get("_missing"):
        return {"status": "missing_credentials", "missing": cenv["_missing"], "entries": []}
    ws = cenv["CLOCKIFY_WORKSPACE_ID"]
    user = routing["clockify_user_id"]
    try:
        entries = clockify_get(f"/workspaces/{ws}/user/{user}/time-entries?start={iso_utc(since)}&end={iso_utc(until)}&page-size=200", cenv)
        sanitized = []
        for e in entries:
            ti = e.get("timeInterval", {})
            sanitized.append({
                "id_suffix": e.get("id", "")[-8:],
                "description": e.get("description", ""),
                "project_id_suffix": (e.get("projectId") or "")[-6:],
                "tag_id_suffixes": [(t or "")[-8:] for t in e.get("tagIds", [])],
                "start": local_dt_string(parse_dt(ti.get("start"))),
                "end": local_dt_string(parse_dt(ti.get("end"))) if ti.get("end") else None,
                "duration": ti.get("duration"),
                "billable": e.get("billable"),
            })
        return {"status": "ok", "entries": sanitized}
    except Exception as e:
        return {"status": "error", "error": str(e), "entries": []}


def fetch_fathom(fenv: dict[str, str], since: dt.datetime, until: dt.datetime) -> dict[str, Any]:
    if fenv.get("_missing"):
        return {"status": "missing_credentials", "missing": fenv["_missing"], "meetings": []}
    params = urllib.parse.urlencode({"created_after": iso_utc(since), "created_before": iso_utc(until), "limit": 50})
    try:
        data = http_json(f"{FATHOM_API}/meetings?{params}", {"X-Api-Key": fenv["FATHOM_API_KEY"]})
        items = data.get("items", data if isinstance(data, list) else [])
        meetings = []
        for m in items:
            start = parse_dt(m.get("recording_start_time")) or parse_dt(m.get("scheduled_start_time"))
            end = parse_dt(m.get("recording_end_time")) or parse_dt(m.get("scheduled_end_time"))
            meetings.append({
                "recording_id": m.get("recording_id") or m.get("id"),
                "title": m.get("title") or m.get("meeting_title"),
                "start": local_dt_string(start),
                "end": local_dt_string(end),
                "share_url": m.get("share_url") or m.get("url"),
                "calendar_invitees": [{"email": i.get("email"), "name": i.get("name"), "is_external": i.get("is_external")} for i in m.get("calendar_invitees", [])[:20]],
                "domains_type": m.get("calendar_invitees_domains_type"),
                "recorded_by_email": (m.get("recorded_by") or {}).get("email"),
            })
        return {"status": "ok", "meetings": meetings}
    except Exception as e:
        return {"status": "error", "error": str(e), "meetings": []}


def multica_profile_config() -> dict[str, Any] | None:
    p = Path.home() / f".multica/profiles/{MULTICA_PROFILE}/config.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def fetch_multica_issues() -> dict[str, Any]:
    cfg = multica_profile_config()
    if not cfg:
        return {"status": "missing_profile", "issues": []}
    token = cfg.get("token") or cfg.get("access_token") or cfg.get("auth_token")
    server = cfg.get("server_url") or cfg.get("serverUrl") or cfg.get("base_url")
    workspace_id = cfg.get("workspace_id") or cfg.get("workspaceId") or os.environ.get("MULTICA_WORKSPACE_ID")
    if not (token and server and workspace_id):
        return {"status": "profile_incomplete", "issues": []}
    # Try common API shapes, keeping output sanitized.
    paths = ["/api/issues?limit=100", "/issues?limit=100"]
    headers = {"Authorization": f"Bearer {token}", "X-Workspace-ID": workspace_id}
    for path in paths:
        try:
            data = http_json(server.rstrip("/") + path, headers)
            items = data.get("issues") or data.get("items") or (data if isinstance(data, list) else [])
            issues = []
            for it in items[:100]:
                issues.append({"id": it.get("id"), "key": it.get("key") or it.get("identifier"), "title": it.get("title"), "status": it.get("status"), "project_id": it.get("project_id") or it.get("projectId")})
            return {"status": "ok", "issues": issues}
        except Exception:
            continue
    return {"status": "error", "issues": [], "note": "Unable to fetch issues with known API paths; autopilot can use CLI/API fallback."}


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


def _make_description(route: dict[str, Any], burst: dict[str, Any]) -> str:
    """Build a one-line description from session metadata for agent analysis.
    
    The description uses the project + label + metadata to produce a concise
    summary. Raw session text (first_user_message, last_assistant_message) is
    kept in the burst data for the agent to analyze, but the description itself
    is a synthesized label-based summary — the agent performs the analysis.
    """
    label = burst.get("label", "")
    msgs = burst.get("user_messages", 0)
    dur = burst.get("duration_minutes", 0)
    project = route.get("project_name", "")
    return f"{project} — {label} ({msgs} msgs, {dur}m)"


def build_proposals(evidence: dict[str, Any], routing: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    proposals: list[dict[str, Any]] = []
    ambiguous: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    existing = evidence.get("clockify", {}).get("entries", [])
    rules = routing.get("skip_rules", {})
    for machine in evidence.get("sessions", []):
        for b in machine.get("claude_bursts", []):
            rid_base = hashlib.sha1(json.dumps(b, sort_keys=True).encode()).hexdigest()[:8]
            if b.get("heartbeat_like"):
                skipped.append({"id": rid_base, "source": "claude", "time": f"{b.get('start')}–{b.get('end')}", "label": b.get("label"), "reason": "heartbeat-like timestamp pattern"})
                continue
            if b.get("duration_minutes", 0) < rules.get("min_minutes", 10) and b.get("user_messages", 0) < rules.get("min_user_messages", 5):
                skipped.append({"id": rid_base, "source": "claude", "time": f"{b.get('start')}–{b.get('end')}", "label": b.get("label"), "reason": "trivial burst below duration/message threshold"})
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
                "description": _make_description(route, b),
                "rationale": f"{b.get('user_messages')} user messages across {b.get('duration_minutes')}m; route matched {b.get('label')}",
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
                "description": f"{route.get('project_name')} — {label} ({hs.get('user_messages',0)} user msgs across {est_duration or '?'}m, estimated)",
                "rationale": hs.get("evidence_level", "estimated duration"),
            }
            proposals.append(cand)
    return proposals, ambiguous, skipped


def write_markdown(run_dir: Path, report: dict[str, Any]) -> None:
    lines = []
    lines.append(f"# Clockify sync dry-run — {report['run_id']}")
    lines.append("")
    lines.append(f"Date range: {report['date_range']['since']} → {report['date_range']['until']} ({report['date_range']['reason']})")
    lines.append(f"Safety: dry-run only; no Clockify writes performed.")
    lines.append("")
    lines.append("## Evidence status")
    lines.append(f"- Clockify: {report['evidence']['clockify']['status']} ({len(report['evidence']['clockify'].get('entries', []))} existing entries)")
    lines.append(f"- Fathom: {report['evidence']['fathom']['status']} ({len(report['evidence']['fathom'].get('meetings', []))} meetings)")
    lines.append(f"- Multica issues: {report['evidence']['multica_issues']['status']} ({len(report['evidence']['multica_issues'].get('issues', []))} issues)")
    for s in report['evidence']['sessions']:
        lines.append(f"- Sessions/{s['machine']}: {s['status']} — {len(s.get('claude_bursts', []))} Claude bursts, {len(s.get('hermes_sessions', []))} Hermes sessions")
        for err in s.get('errors', [])[:3]:
            lines.append(f"  - warning: {err}")
    lines.append("")
    lines.append("## Proposal table")
    lines.append("| ID | Time | Dur | Project | Tags | Source | Confidence | Description |")
    lines.append("|---|---:|---:|---|---|---|---|---|")
    for p in report['proposals']:
        tags = ", ".join(p.get('tag_names') or p.get('tag_suffixes') or [])
        src = ", ".join(p.get('source', []))
        lines.append(f"| {p['id']} | {p.get('start')}–{str(p.get('end',''))[-5:]} | {p.get('duration_minutes')}m | {p.get('client_project')} | {tags} | {src} | {p.get('confidence')} | {p.get('description')} |")
    if not report['proposals']:
        lines.append("| — | — | — | — | — | — | — | No high-confidence proposals generated. |")
    lines.append("")
    lines.append("## Ambiguous rows")
    for a in report['ambiguous'][:50]:
        lines.append(f"- {a['id']}: {a.get('time')} {a.get('source')} on {a.get('machine')} — {a.get('reason')} — {a.get('label')}")
    if not report['ambiguous']:
        lines.append("- None")
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
    lines.append("Approve specific row IDs (for example: P001, P003) before any Clockify posting step. This run did not post entries.")
    (run_dir / "run-report.md").write_text("\n".join(lines) + "\n")


def run(args: argparse.Namespace) -> int:
    routing = load_json(ROOT / "routing.json")
    fleet = load_json(ROOT / "fleet.json")
    cenv = load_env_file(CLOCKIFY_ENV_CANDIDATES, ["CLOCKIFY_API_KEY", "CLOCKIFY_WORKSPACE_ID"])
    fenv = load_env_file(FATHOM_ENV_CANDIDATES, ["FATHOM_API_KEY"])
    since, until, reason = compute_range(args, routing, cenv)
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = RUNS / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    evidence = {
        "clockify": fetch_clockify(cenv, routing, since, until),
        "fathom": fetch_fathom(fenv, since, until),
        "multica_issues": fetch_multica_issues(),
        "sessions": [],
    }
    for m in fleet.get("machines", []):
        if not m.get("enabled", True):
            continue
        if m.get("kind") == "local":
            evidence["sessions"].append(collect_local_sessions(m, since, until))
        elif m.get("kind") == "ssh":
            evidence["sessions"].append(collect_remote_sessions(m, since, until, fleet.get("ssh_options", [])))

    proposals, ambiguous, skipped = build_proposals(evidence, routing)
    report = {
        "run_id": run_id,
        "date_range": {"since": local_dt_string(since), "until": local_dt_string(until), "reason": reason},
        "safety": {"dry_run": True, "clockify_posted": False},
        "paths": {"run_dir": str(run_dir), "report_json": str(run_dir / "run-report.json"), "report_md": str(run_dir / "run-report.md"), "proposals_json": str(run_dir / "proposals.json")},
        "evidence": evidence,
        "proposals": proposals,
        "ambiguous": ambiguous,
        "skipped": skipped,
        "issue_reconciliation": {"matched_existing_issues": [], "proposed_multica_comments": [], "no_action_items": ["Downstream issue mutations are disabled by default."]},
    }
    write_json(run_dir / "evidence" / "clockify-existing.json", evidence["clockify"])
    write_json(run_dir / "evidence" / "fathom-meetings.json", evidence["fathom"])
    write_json(run_dir / "evidence" / "multica-issues.json", evidence["multica_issues"])
    write_json(run_dir / "evidence" / "sessions.json", evidence["sessions"])
    write_json(run_dir / "proposals.json", proposals)
    write_json(run_dir / "ambiguous.json", ambiguous)
    write_json(run_dir / "skipped.json", skipped)
    write_json(run_dir / "run-report.json", report)
    write_markdown(run_dir, report)
    print(str(run_dir / "run-report.md"))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Clockify sync dry-run collector")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--since", help="YYYY-MM-DD")
    r.add_argument("--until", help="YYYY-MM-DD inclusive")
    args = ap.parse_args()
    if args.cmd == "run":
        return run(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
