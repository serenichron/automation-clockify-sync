#!/usr/bin/env python3
"""Tiered, evidence-grounded semantic analysis for Clockify reconciliation.

The analyzer is deliberately a pure JSON stage.  It receives normalized,
content-addressed evidence and returns structured activity claims.  It never
allocates Clockify time, mutates review state, or writes to an external system.

Provider configuration is explicit and fail-closed.  A model is not considered
available merely because its name appears in repository documentation.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable, Iterable, Mapping
import urllib.error
import urllib.request


SCHEMA_VERSION = 1
PROMPT_VERSION = "clockify-semantic-v1"
DEFAULT_PRIMARY_MODEL = "deepseek-v4-flash:cloud"
DEFAULT_MAX_BODY_BYTES = 1_450_000
PRIVATE_TEXT_APPROVAL_ENV = "CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED"
LIFECYCLES = {
    "completed",
    "advanced",
    "investigated",
    "meeting",
    "planned",
    "blocked",
    "noise",
}
CONFIDENCE_LEVELS = {"low", "medium", "high"}
_ATOMIC_FIELD_SEPARATOR_RE = re.compile(
    r"[;•]|(?:^|\s)(?:then|plus|also)(?:\s|$)|(?:^|\s)\d+[.)](?:\s|$)",
    re.IGNORECASE,
)
_COMPOUND_ACTION_RE = re.compile(r"(?:^|\s)(?:and|or|&|/)(?:\s|$)", re.IGNORECASE)
SAFE_EVIDENCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
SAFE_TIMESTAMP_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:?\d{2})?)?$"
)
URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\]\[\"']+")
EMAIL_RE = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
PATH_RE = re.compile(r"(?<![\w/])/(?:[^\s/]+/)*[^\s]+|\b[A-Za-z]:\\[^\s]+")
SECRET_RE = re.compile(
    r"(?i)\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|password|"
    r"authorization|credential|private[_-]?key)\b\s*(?:=|:|is)\s*[^\s,;]+"
)
BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
HASH_RE = re.compile(r"(?i)\b(?:sha(?:1|224|256|384|512):?)?[a-f0-9]{32,}\b")
PHONE_CANDIDATE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d().\s-]{6,}\d)(?!\w)")
STREET_ADDRESS_RE = re.compile(
    r"(?ix)\b(?:"
    r"\d{1,5}\s+(?:strada|str\.?|bulevardul|bulevard|bd\.?|calea|"
    r"(?:ș|s)oseaua|aleea)\s+[A-ZĂÂÎȘȚ][\wĂÂÎȘȚăâîșț'’-]+"
    r"(?:\s+[A-ZĂÂÎȘȚ][\wĂÂÎȘȚăâîșț'’-]+){0,4}"
    r"|\d{1,5}\s+[A-ZĂÂÎȘȚ][\wĂÂÎȘȚăâîșț'’-]+"
    r"(?:\s+[A-ZĂÂÎȘȚ][\wĂÂÎȘȚăâîșț'’-]+){0,4}\s+"
    r"(?:street|st\.?|road|rd\.?|avenue|ave\.?|boulevard|blvd\.?|lane|ln\.?|drive|dr\.?|way)"
    r"|(?:strada|str\.?|bulevardul|bulevard|bd\.?|calea|(?:ș|s)oseaua|aleea)\s+"
    r"[A-ZĂÂÎȘȚ][\wĂÂÎȘȚăâîșț'’-]+(?:\s+[A-ZĂÂÎȘȚ][\wĂÂÎȘȚăâîșț'’-]+){0,4}"
    r"(?:\s+(?:nr\.?\s*)?\d+[A-Za-z]?)?"
    r")\b"
)
PERSON_NAME_RE = re.compile(
    r"(?x)\b(?P<context>(?i:call|contact|phone|message|ask|tell|meet|met|"
    r"spoke\s+with|talked\s+to|follow\s+up\s+with|meeting\s+with))\s+"
    r"(?P<name>[A-ZĂÂÎȘȚ][a-zăâîșț'’-]{1,30}(?:\s+[A-ZĂÂÎȘȚ][a-zăâîșț'’-]{1,30}){1,3})"
    r"(?=\s+(?i:at|via|on|about|regarding|for|to)\b|\s*[,.;:])"
)
COMMAND_RE = re.compile(
    r"(?i)^\s*(?:[$>#]\s*)?`?(?:curl|ssh|scp|rsync|git|python(?:3)?|pip(?:3)?|npm|npx|"
    r"pytest|rm|mv|cp|chmod|docker|kubectl|terraform|ansible|brew)\b(?:\s|$)"
)
TRANSPORT_RE = re.compile(
    r"(?i)(?:<\|(?:system|developer|assistant|user)[^>]*\|>|"
    r"(?:^|\s)(?:system|developer|assistant|user)\s*(?:prompt|message)?\s*:|"
    r"(?:tool_call|tool_result|function_call|custom_tool_call|<command-|<local-command-))"
)
TOOL_KINDS = {"tool_call", "tool_result", "function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output"}
SAFE_ENTITY_TOKENS = {
    "clockify", "tst", "tstprep", "serenichron", "codex", "claude",
    "hermes", "multica", "fathom", "ollama", "wordpress", "cloudron",
    "headroom", "google", "github",
}
PROJECTED_EVENT_FIELDS = {
    "evidence_id", "source_category", "time_span", "role", "content",
    "project_context", "meeting_context",
}


class AnalyzerError(RuntimeError):
    """Fail-closed analyzer or contract error."""


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def stable_digest(prefix: str, value: Any, length: int = 24) -> str:
    digest = hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}{digest[:length]}"


def _one_line(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _safe_timestamp(value: Any) -> str:
    """Return a timestamp only when it is a bounded, timestamp-shaped scalar."""
    text = str(value or "").strip()
    if not SAFE_TIMESTAMP_RE.fullmatch(text):
        return ""
    return text


def _timestamp_instant(value: Any) -> dt.datetime | None:
    """Parse a validated timestamp for instant-safe comparisons."""
    text = _safe_timestamp(value)
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _ordered_timestamps(start: Any, end: Any) -> bool:
    left = _timestamp_instant(start)
    right = _timestamp_instant(end)
    if left is None or right is None:
        return False
    if (left.tzinfo is None) != (right.tzinfo is None):
        return False
    return left <= right


def _timestamp_spans_overlap(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    left_start = _timestamp_instant(left.get("start"))
    left_end = _timestamp_instant(left.get("end"))
    right_start = _timestamp_instant(right.get("start"))
    right_end = _timestamp_instant(right.get("end"))
    if None in {left_start, left_end, right_start, right_end}:
        return False
    assert left_start is not None and left_end is not None
    assert right_start is not None and right_end is not None
    aware = {value.tzinfo is not None for value in (left_start, left_end, right_start, right_end)}
    if len(aware) != 1:
        return False
    return left_start <= right_end and right_start <= left_end


def _safe_time_span(event: dict[str, Any]) -> dict[str, str] | None:
    raw_span = event.get("raw_source_span")
    span = raw_span if isinstance(raw_span, dict) else {}
    projected_span = event.get("time_span") if isinstance(event.get("time_span"), dict) else {}
    start = _safe_timestamp(
        projected_span.get("start") or event.get("observed_start") or span.get("start") or span.get("timestamp")
        or event.get("observed_at") or event.get("timestamp")
    )
    end = _safe_timestamp(projected_span.get("end") or event.get("observed_end") or span.get("end")) or start
    if not start or not end or not _ordered_timestamps(start, end):
        return None
    return {"start": start, "end": end}


def _source_category(event: dict[str, Any]) -> str:
    source = str(event.get("source_type") or event.get("source_category") or "").casefold()
    if "fathom" in source or "meeting" in source:
        return "meeting"
    if "clockify" in source:
        return "clockify"
    if "multica" in source or "issue" in source:
        return "issue"
    if "repository" in source or "commit" in source:
        return "repository"
    if any(name in source for name in ("codex", "claude", "hermes", "agent", "session")):
        return "agent_session"
    return "other"


def _safe_text(value: Any) -> str:
    """Redact transport and direct-identifying data without semantic clipping."""
    if not isinstance(value, str):
        return ""
    text = value.replace("\x00", " ")
    lines: list[str] = []
    for line in text.splitlines() or [text]:
        if TRANSPORT_RE.search(line) or COMMAND_RE.search(line) or line.lstrip().startswith(("$ ", "> ")):
            continue
        line = URL_RE.sub("[url removed]", line)
        line = EMAIL_RE.sub("[email removed]", line)
        line = PATH_RE.sub("[path removed]", line)
        line = BEARER_RE.sub("[credential removed]", line)
        line = SECRET_RE.sub("[credential removed]", line)
        line = HASH_RE.sub("[hash removed]", line)
        line = _redact_phone_numbers(line)
        line = STREET_ADDRESS_RE.sub("[address removed]", line)
        line = PERSON_NAME_RE.sub(_redact_contextual_person, line)
        lines.append(line)
    safe = _one_line(" ".join(lines))
    if any(pattern.search(safe) for pattern in (URL_RE, EMAIL_RE, PATH_RE, BEARER_RE, SECRET_RE, HASH_RE, COMMAND_RE, TRANSPORT_RE)):
        raise AnalyzerError("semantic projection contains unsafe text")
    return safe


def _redact_phone_numbers(value: str) -> str:
    """Redact phone-shaped scalars without eating dates, versions, or durations."""
    def replacement(match: re.Match[str]) -> str:
        candidate = match.group(0)
        digits = re.sub(r"\D", "", candidate)
        if not 7 <= len(digits) <= 15:
            return candidate
        if re.search(r"\b\d{4}-\d{2}-\d{2}\b", candidate) or ":" in candidate:
            return candidate
        separators = len(re.findall(r"[\s().-]", candidate))
        if not candidate.lstrip().startswith("+") and len(digits) < 10 and separators < 2:
            return candidate
        return "[phone removed]"

    return PHONE_CANDIDATE_RE.sub(replacement, value)


def _redact_contextual_person(match: re.Match[str]) -> str:
    """Redact name-shaped contact subjects while preserving known products."""
    name = match.group("name")
    tokens = {token.casefold() for token in re.findall(r"[\wĂÂÎȘȚăâîșț]+", name)}
    if tokens & SAFE_ENTITY_TOKENS:
        return match.group(0)
    return f"{match.group('context')} [person removed]"


def _safe_context_items(
    value: Any,
    *,
    text_keys: tuple[str, ...] = ("description", "text", "summary"),
) -> list[str]:
    if not isinstance(value, list):
        return []
    output: list[str] = []
    for item in value:
        raw = item
        if isinstance(item, dict):
            raw = next((item.get(key) for key in text_keys if item.get(key)), "")
        text = _safe_text(raw)
        if text:
            output.append(text)
    return output


def private_text_approved(value: bool | None = None) -> bool:
    """Require an explicit runtime decision before private prose may leave locally."""
    if value is not None:
        return bool(value)
    return os.environ.get(PRIVATE_TEXT_APPROVAL_ENV, "").strip().casefold() in {
        "1", "true", "yes", "approved",
    }


def _requires_private_text_approval(events: Iterable[dict[str, Any]]) -> bool:
    for event in events:
        # Detect the actual outbound projection, not a hand-maintained subset of
        # source fields. This also covers legacy/enriched source categories and
        # newly added projected text without opening an approval bypass.
        projected = project_event(event)
        if any(projected.get(field) for field in ("content", "project_context", "meeting_context")):
            return True
    return False


def _require_private_text_approval(
    events: Iterable[dict[str, Any]],
    approved: bool | None,
) -> None:
    if _requires_private_text_approval(events) and not private_text_approved(approved):
        raise AnalyzerError(
            f"private semantic text egress requires explicit {PRIVATE_TEXT_APPROVAL_ENV}=approved"
        )


def _safe_summary(value: Any) -> str:
    if isinstance(value, dict):
        value = (
            value.get("markdown_formatted")
            or value.get("summary")
            or value.get("text")
            or ""
        )
    return _safe_text(value)


def _safe_artifact_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        if not isinstance(item, str):
            continue
        name = item.replace("\\", "/").rsplit("/", 1)[-1]
        if safe := _safe_text(name):
            names.append(safe)
    return names


def _tool_payload(event: dict[str, Any], attributes: dict[str, Any]) -> bool:
    values = (event.get("kind"), event.get("type"), attributes.get("kind"), attributes.get("type"))
    return any(str(value or "").casefold() in TOOL_KINDS for value in values)


def project_event(event: dict[str, Any]) -> dict[str, Any]:
    """Create the sole, privacy-safe event shape permitted to leave the machine."""
    if not isinstance(event, dict):
        raise AnalyzerError("cannot project a non-object evidence event")
    evidence_id = str(event.get("evidence_id") or "")
    if not SAFE_EVIDENCE_ID_RE.fullmatch(evidence_id):
        raise AnalyzerError("cannot project evidence with an unsafe evidence ID")
    attributes = event.get("attributes") if isinstance(event.get("attributes"), dict) else {}
    project_context = event.get("project_context") if isinstance(event.get("project_context"), dict) else {}
    meeting_context = event.get("meeting_context") if isinstance(event.get("meeting_context"), dict) else {}
    role = str(event.get("role") or attributes.get("role") or "source").casefold()
    if role not in {"user", "assistant", "tool", "meeting", "system", "source"}:
        role = "source"
    is_tool = _tool_payload(event, attributes)
    content_values = [] if is_tool or role == "system" else [
        event.get("content"), attributes.get("content"), attributes.get("description"),
        attributes.get("label"), attributes.get("reason"), attributes.get("first_user_message"),
        attributes.get("last_assistant_message"), attributes.get("subject"),
    ]
    content = _one_line(" ".join(filter(None, (_safe_text(value) for value in content_values))))
    artifact_names = _safe_artifact_names(attributes.get("artifacts"))
    if artifact_names:
        content = _one_line(f"{content} Artifacts: {', '.join(artifact_names)}")
    project_name = _safe_text(
        project_context.get("name") or attributes.get("project") or attributes.get("project_name") or event.get("project")
    )
    meeting_title = _safe_text(meeting_context.get("title") or attributes.get("title") or attributes.get("meeting_title"))
    meeting_summary = _safe_summary(
        meeting_context.get("summary")
        or attributes.get("summary")
        or attributes.get("default_summary")
    )
    projected: dict[str, Any] = {
        "evidence_id": evidence_id,
        "source_category": _source_category(event),
        "role": role,
        "content": content,
    }
    if time_span := _safe_time_span(event):
        projected["time_span"] = time_span
    if project_name:
        projected["project_context"] = {"name": project_name}
    meeting_context = {
        key: value for key, value in {
            "title": meeting_title,
            "summary": meeting_summary,
            "action_items": _safe_context_items(meeting_context.get("action_items") or attributes.get("action_items")),
            "transcript": _safe_context_items(
                meeting_context.get("transcript") or attributes.get("transcript"),
                text_keys=("text",),
            ),
        }.items() if value
    }
    if meeting_context:
        projected["meeting_context"] = meeting_context
    if not set(projected) <= PROJECTED_EVENT_FIELDS:
        raise AnalyzerError("semantic projection shape is unsafe")
    return projected


def project_events(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Project deterministically, never forwarding raw evidence objects."""
    return sorted((project_event(dict(event)) for event in events), key=_event_sort_key)


def _project_corrections(corrections: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Keep only generalized learning rules; correction history itself stays local."""
    output: list[dict[str, Any]] = []
    for correction in corrections or []:
        if not isinstance(correction, dict):
            raise AnalyzerError("cannot project a non-object review correction")
        if correction.get("local_only") or any(
            key in correction
            for key in (
                "expected_field_patch",
                "evidence_fingerprint",
                "regression_case_id",
                "review_item_id",
                "reviewer",
            )
        ):
            raise AnalyzerError("local-only review corrections cannot enter analyzer requests")
        item = {
            "category": _safe_text(correction.get("category")),
            "decision": _safe_text(correction.get("decision")),
            "instruction": _safe_text(correction.get("instruction")),
            "patched_fields": sorted(
                _safe_text(value) for value in correction.get("patched_fields", [])
                if _safe_text(value)
            ) if isinstance(correction.get("patched_fields"), list) else [],
        }
        output.append({key: value for key, value in item.items() if value})
    return sorted(output, key=canonical_json)


def _event_sort_key(event: dict[str, Any]) -> tuple[str, str, str]:
    span = event.get("raw_source_span") if isinstance(event.get("raw_source_span"), dict) else {}
    safe_span = event.get("time_span") if isinstance(event.get("time_span"), dict) else {}
    return (
        str(
            safe_span.get("start")
            or event.get("observed_start")
            or event.get("observed_at")
            or event.get("timestamp")
            or span.get("start")
            or span.get("timestamp")
            or ""
        ),
        str(event.get("source_type") or ""),
        str(event.get("evidence_id") or ""),
    )


def _event_day(event: dict[str, Any]) -> str:
    span = event.get("raw_source_span") if isinstance(event.get("raw_source_span"), dict) else {}
    safe_span = event.get("time_span") if isinstance(event.get("time_span"), dict) else {}
    value = str(
        safe_span.get("start")
        or event.get("observed_start")
        or event.get("observed_at")
        or event.get("timestamp")
        or span.get("start")
        or span.get("timestamp")
        or ""
    )
    return value[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", value) else "unknown"


def _request_messages(
    events: list[dict[str, Any]],
    *,
    mode: str,
    corrections: list[dict[str, Any]] | None = None,
    private_text_approved: bool | None = None,
) -> list[dict[str, str]]:
    _require_private_text_approval(events, private_text_approved)
    system = """You reconstruct human work for a Clockify ledger from cited evidence.
Return JSON only. Never quote a prompt or status message as a work description.
Separate independent accomplishments. Merge duplicated evidence for the same work.
Every reviewable activity needs a specific split_rationale explaining why it is one
atomic accomplishment. Never join verbs with "and", "or", "then", or "also".
Give related atomic accomplishments the same short parent workstream name even when
their specific objects differ; unrelated work must not share a workstream.
Classify planned work, waiting, polling, agent chatter, heartbeats, and autonomous
background execution as planned or noise, not completed human work. A blocker is
loggable only when the evidence proves substantive diagnosis or remediation.
Do not invent projects, outcomes, evidence, effort, or meeting purpose. For a
title-only meeting with no supported outcome, emit an exception. Effort is human
attention, not process runtime or empty wall-clock time. Evidence IDs must be copied
exactly from the input. Account for every input evidence ID exactly once across
activities, exceptions, and omissions. Exceptions and omissions require cited
evidence IDs. Do not allocate start/end Clockify blocks.
Write action + object + outcome so the final prefixed description is 8-14 words,
using terse past-tense Caveman wording and no Markdown, IDs, paths, URLs, or status prose.

Output object:
{
  "activities": [{
    "lifecycle": "completed|advanced|investigated|meeting|planned|blocked|noise",
    "workstream": "short stable parent workstream name",
    "action": "short verb phrase",
    "object": "specific work object",
    "outcome": "bounded evidenced outcome",
    "evidence_ids": ["ev-..."],
    "evidence_spans": [{"start":"ISO-like", "end":"ISO-like"}],
    "project_recommendation": {"name":"", "prefix":"", "tag_names":[]},
    "effort": {"minimum_minutes":1, "recommended_minutes":1, "maximum_minutes":1},
    "semantic_confidence": "low|medium|high",
    "timing_confidence": "low|medium|high",
    "split_rationale": "",
    "merge_rationale": "",
    "omit_rationale": ""
  }],
  "exceptions": [{"kind":"insufficient_evidence|conflicting_evidence", "evidence_ids":[], "reason":""}],
  "omissions": [{"lifecycle":"planned|noise", "evidence_ids":[], "reason":""}]
}
"""
    payload = {
        "mode": mode,
        "schema_version": SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "events": project_events(events),
        "review_corrections": _project_corrections(corrections),
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": canonical_json(payload)},
    ]


def _body_for(
    events: list[dict[str, Any]],
    *,
    model: str,
    mode: str,
    corrections: list[dict[str, Any]] | None = None,
    private_text_approved: bool | None = None,
) -> dict[str, Any]:
    return {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": _request_messages(
            events,
            mode=mode,
            corrections=corrections,
            private_text_approved=private_text_approved,
        ),
    }


def _safe_provisional_activity(activity: dict[str, Any]) -> dict[str, Any]:
    """Return the sole activity shape permitted in a cross-chunk request."""
    evidence_ids = sorted(str(value) for value in activity.get("evidence_ids", []))
    if not evidence_ids or any(not SAFE_EVIDENCE_ID_RE.fullmatch(value) for value in evidence_ids):
        raise AnalyzerError("synthesis activity contains unsafe evidence IDs")
    spans = activity.get("evidence_spans", [])
    if not isinstance(spans, list):
        raise AnalyzerError("synthesis activity evidence_spans must be a list")
    safe_spans = _validated_spans(spans, evidence_ids=evidence_ids, evidence_time_spans=None) if spans else []
    project = activity.get("project_recommendation")
    if not isinstance(project, dict):
        raise AnalyzerError("synthesis activity project_recommendation must be an object")
    effort = activity.get("effort")
    if not isinstance(effort, dict):
        raise AnalyzerError("synthesis activity effort must be an object")
    return {
        "activity_id": str(activity.get("activity_id") or ""),
        "workstream": _safe_text(activity.get("workstream") or activity.get("object")),
        "lifecycle": str(activity.get("lifecycle") or ""),
        "action": _safe_text(activity.get("action")),
        "object": _safe_text(activity.get("object")),
        "outcome": _safe_text(activity.get("outcome")),
        "evidence_ids": evidence_ids,
        "evidence_spans": safe_spans,
        "project_recommendation": {
            "name": _safe_text(project.get("name")),
            "prefix": _safe_text(project.get("prefix")),
            "tag_names": sorted(
                _safe_text(value) for value in project.get("tag_names", [])
                if _safe_text(value)
            ) if isinstance(project.get("tag_names"), list) else [],
        },
        "effort": {
            "minimum_minutes": _positive_int(effort.get("minimum_minutes"), "minimum_minutes"),
            "recommended_minutes": _positive_int(effort.get("recommended_minutes"), "recommended_minutes"),
            "maximum_minutes": _positive_int(effort.get("maximum_minutes"), "maximum_minutes"),
        },
        "semantic_confidence": _confidence(activity.get("semantic_confidence"), "semantic_confidence"),
        "timing_confidence": _confidence(activity.get("timing_confidence"), "timing_confidence"),
        "split_rationale": _safe_text(activity.get("split_rationale")),
        "merge_rationale": _safe_text(activity.get("merge_rationale")),
        "omit_rationale": _safe_text(activity.get("omit_rationale")),
    }


def _synthesis_messages(activities: list[dict[str, Any]], *, workstream_id: str) -> list[dict[str, str]]:
    """Build a privacy-safe request to reconcile one repeated workstream."""
    if not SAFE_EVIDENCE_ID_RE.fullmatch(workstream_id):
        raise AnalyzerError("synthesis workstream ID is unsafe")
    provisional = sorted(
        (_safe_provisional_activity(activity) for activity in activities),
        key=lambda value: (value["activity_id"], canonical_json(value)),
    )
    system = """You reconcile provisional Clockify semantic activities from repeated workstream evidence.
Return JSON only, using the same activities/exceptions/omissions schema supplied for extraction.
Merge provisional activities only when their cited evidence proves the same single atomic accomplishment.
When accomplishments differ, preserve separate activities and give each a distinct, specific object.
Preserve the supplied parent workstream name for related atomic accomplishments.
Every input evidence ID must appear in exactly one returned activity; do not add, omit, or move evidence
to exceptions or omissions. Copy evidence IDs and evidence spans exactly from supported input evidence.
Do not invent projects, outcomes, effort, or timing. Never include paths, URLs, emails, secrets, IDs,
or status prose in descriptive fields."""
    payload = {
        "mode": "synthesize",
        "schema_version": SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "workstream_id": workstream_id,
        "provisional_activities": provisional,
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": canonical_json(payload)},
    ]


def _synthesis_body(
    activities: list[dict[str, Any]], *, model: str, workstream_id: str
) -> dict[str, Any]:
    return {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": _synthesis_messages(activities, workstream_id=workstream_id),
    }


def chunk_events(
    events: Iterable[dict[str, Any]],
    *,
    model: str = DEFAULT_PRIMARY_MODEL,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    corrections: list[dict[str, Any]] | None = None,
    private_text_approved: bool | None = None,
) -> list[list[dict[str, Any]]]:
    """Partition without truncating any event, preferring day boundaries.

    Oversized individual evidence is rejected: silently clipping it would violate
    the complete-context and cited-evidence contract.
    """
    # Size exactly what may leave the machine, never the raw immutable ledger.
    ordered = project_events(events)
    by_day: dict[str, list[dict[str, Any]]] = {}
    for event in ordered:
        by_day.setdefault(_event_day(event), []).append(event)

    chunks: list[list[dict[str, Any]]] = []
    for day in sorted(by_day):
        current: list[dict[str, Any]] = []
        for event in by_day[day]:
            one_size = len(
                canonical_json(
                    _body_for(
                        [event],
                        model=model,
                        mode="extract",
                        corrections=corrections,
                        private_text_approved=private_text_approved,
                    )
                ).encode("utf-8")
            )
            if one_size > max_body_bytes:
                raise AnalyzerError(
                    f"evidence event {event.get('evidence_id') or '<unknown>'} "
                    f"exceeds analyzer request ceiling ({one_size} bytes)"
                )
            trial = [*current, event]
            trial_size = len(
                canonical_json(
                    _body_for(
                        trial,
                        model=model,
                        mode="extract",
                        corrections=corrections,
                        private_text_approved=private_text_approved,
                    )
                ).encode("utf-8")
            )
            if current and trial_size > max_body_bytes:
                chunks.append(current)
                current = [event]
            else:
                current = trial
        if current:
            chunks.append(current)
    return chunks


def _json_object_from_response(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and any(
        key in value for key in ("activities", "exceptions", "omissions")
    ):
        return value
    try:
        content = value["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise AnalyzerError("analyzer response lacks JSON message content") from exc
    if isinstance(content, dict):
        result = content
    else:
        text = str(content or "").strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I)
        try:
            result = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AnalyzerError("analyzer returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise AnalyzerError("analyzer JSON must be an object")
    return result


def _confidence(value: Any, field: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in CONFIDENCE_LEVELS:
        raise AnalyzerError(f"invalid {field}: {value!r}")
    return normalized


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise AnalyzerError(f"invalid {field}: {value!r}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise AnalyzerError(f"invalid {field}: {value!r}") from exc
    if parsed <= 0:
        raise AnalyzerError(f"{field} must be positive")
    return parsed


def _normalized_identity(value: Any) -> str:
    """Normalize harmless wording/punctuation variation for stable semantic IDs."""
    text = _one_line(value).casefold()
    return re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).strip()


def _validate_atomic_parts(action: str, obj: str, outcome: str, split_rationale: str) -> None:
    """Reject obvious compound records; semantic edge cases remain review-gated."""
    if not split_rationale:
        raise AnalyzerError("reviewable activity requires an explicit atomicity rationale")
    action_words = re.findall(r"[\w'-]+", action, flags=re.UNICODE)
    if not action_words or len(action_words) > 3 or _COMPOUND_ACTION_RE.search(action):
        raise AnalyzerError("activity action must express one atomic verb phrase")
    for name, value in (("action", action), ("object", obj), ("outcome", outcome)):
        if _ATOMIC_FIELD_SEPARATOR_RE.search(value) or re.search(r"[.!?]\s+[A-Z]", value):
            raise AnalyzerError(f"activity {name} contains multiple accomplishment clauses")


def _validated_spans(
    spans: list[Any],
    *,
    evidence_ids: list[str],
    evidence_time_spans: dict[str, dict[str, str]] | None,
) -> list[dict[str, str]]:
    if not spans:
        raise AnalyzerError("reviewable activity requires nonempty evidence_spans")
    normalized: list[dict[str, str]] = []
    for span in spans:
        if not isinstance(span, dict):
            raise AnalyzerError("evidence_span must be an object")
        start = _safe_timestamp(span.get("start"))
        end = _safe_timestamp(span.get("end"))
        if not start or not end or not _ordered_timestamps(start, end):
            raise AnalyzerError("evidence_span requires valid start and end timestamps")
        normalized.append({"start": start, "end": end})
    normalized = sorted({(item["start"], item["end"]) for item in normalized})
    result = [{"start": start, "end": end} for start, end in normalized]
    if evidence_time_spans:
        cited_spans = [evidence_time_spans[item] for item in evidence_ids if item in evidence_time_spans]
        # When local evidence exposes a deterministic timestamp range, every
        # reviewable claimed span must overlap a cited range.
        if cited_spans and any(
            not any(_timestamp_spans_overlap(item, cited) for cited in cited_spans)
            for item in result
        ):
            raise AnalyzerError("evidence_span is not supported by cited evidence")
    return result


def validate_result(
    result: dict[str, Any],
    *,
    known_evidence_ids: set[str],
    provider_model: str,
    analyzer_tier: str,
    evidence_time_spans: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    activities = result.get("activities", [])
    exceptions = result.get("exceptions", [])
    omissions = result.get("omissions", [])
    if not all(isinstance(value, list) for value in (activities, exceptions, omissions)):
        raise AnalyzerError("activities, exceptions, and omissions must be lists")

    def evidence_ids_for(value: Any, kind: str) -> list[str]:
        if not isinstance(value, list):
            raise AnalyzerError(f"{kind} evidence_ids must be a list")
        evidence_ids = [str(item) for item in value]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise AnalyzerError(f"{kind} evidence_ids must not repeat")
        if not evidence_ids or not set(evidence_ids) <= known_evidence_ids:
            if kind == "activity":
                raise AnalyzerError("activity contains missing or unknown evidence IDs")
            raise AnalyzerError(f"{kind} requires known evidence IDs")
        return sorted(evidence_ids)

    normalized_activities: list[dict[str, Any]] = []
    for raw in activities:
        if not isinstance(raw, dict):
            raise AnalyzerError("activity must be an object")
        lifecycle = str(raw.get("lifecycle") or "").strip().lower()
        if lifecycle not in LIFECYCLES:
            raise AnalyzerError(f"invalid lifecycle: {lifecycle!r}")
        evidence_ids = evidence_ids_for(raw.get("evidence_ids"), "activity")
        action = _one_line(raw.get("action"))
        obj = _one_line(raw.get("object"))
        outcome = _one_line(raw.get("outcome"))
        split_rationale = _one_line(raw.get("split_rationale"))
        workstream = _one_line(raw.get("workstream") or obj)
        if lifecycle not in {"planned", "noise"} and (not action or not obj or not outcome):
            raise AnalyzerError("reviewable activity requires action, object, and outcome")
        if lifecycle not in {"planned", "noise"} and not workstream:
            raise AnalyzerError("reviewable activity requires a workstream")
        if lifecycle not in {"planned", "noise"}:
            _validate_atomic_parts(action, obj, outcome, split_rationale)
        effort = raw.get("effort")
        if not isinstance(effort, dict):
            raise AnalyzerError("activity effort must be an object")
        minimum = _positive_int(effort.get("minimum_minutes"), "minimum_minutes")
        recommended = _positive_int(effort.get("recommended_minutes"), "recommended_minutes")
        maximum = _positive_int(effort.get("maximum_minutes"), "maximum_minutes")
        if not minimum <= recommended <= maximum:
            raise AnalyzerError("effort must satisfy minimum <= recommended <= maximum")
        project = raw.get("project_recommendation") or {}
        if not isinstance(project, dict):
            raise AnalyzerError("project_recommendation must be an object")
        raw_spans = raw.get("evidence_spans") or []
        if not isinstance(raw_spans, list):
            raise AnalyzerError("evidence_spans must be a list")
        spans = (
            _validated_spans(
                raw_spans,
                evidence_ids=evidence_ids,
                evidence_time_spans=evidence_time_spans,
            )
            if lifecycle not in {"planned", "noise"}
            else []
        )
        identity = {
            "evidence_ids": evidence_ids,
            "lifecycle": lifecycle,
            "project": _normalized_identity(project.get("name")),
            "object": _normalized_identity(obj),
        }
        workstream_identity = {
            "project": _normalized_identity(project.get("name")),
            "workstream": _normalized_identity(workstream),
        }
        normalized_activities.append(
            {
                "activity_id": stable_digest("act-", identity),
                "workstream_id": stable_digest("ws-", workstream_identity),
                "workstream": workstream,
                "lifecycle": lifecycle,
                "action": action,
                "object": obj,
                "outcome": outcome,
                "evidence_ids": evidence_ids,
                "evidence_spans": spans,
                "project_recommendation": {
                    "name": _one_line(project.get("name")),
                    "prefix": _one_line(project.get("prefix")),
                    "tag_names": sorted(set(str(v) for v in project.get("tag_names", []))),
                },
                "effort": {
                    "minimum_minutes": minimum,
                    "recommended_minutes": recommended,
                    "maximum_minutes": maximum,
                },
                "semantic_confidence": _confidence(
                    raw.get("semantic_confidence"), "semantic_confidence"
                ),
                "timing_confidence": _confidence(
                    raw.get("timing_confidence"), "timing_confidence"
                ),
                "split_rationale": split_rationale,
                "merge_rationale": _one_line(raw.get("merge_rationale")),
                "omit_rationale": _one_line(raw.get("omit_rationale")),
                "rendered_description": None,
                "analyzer_model": provider_model,
                "analyzer_tier": analyzer_tier,
                "prompt_version": PROMPT_VERSION,
                "schema_version": SCHEMA_VERSION,
            }
        )

    def normalize_nonactivities(values: list[Any], kind: str) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for value in values:
            if not isinstance(value, dict):
                raise AnalyzerError(f"{kind} record must be an object")
            evidence_ids = evidence_ids_for(value.get("evidence_ids"), kind)
            output.append(
                {
                    **value,
                    "evidence_ids": evidence_ids,
                    "reason": _one_line(value.get("reason")),
                }
            )
        return sorted(output, key=canonical_json)

    normalized_exceptions = normalize_nonactivities(exceptions, "exception")
    normalized_omissions = normalize_nonactivities(omissions, "omission")
    classified_ids = [
        evidence_id
        for collection in (normalized_activities, normalized_exceptions, normalized_omissions)
        for record in collection
        for evidence_id in record.get("evidence_ids", [])
    ]
    missing_ids = known_evidence_ids - set(classified_ids)
    citation_counts: dict[str, int] = {}
    for evidence_id in classified_ids:
        citation_counts[evidence_id] = citation_counts.get(evidence_id, 0) + 1
    duplicate_ids = sorted(
        evidence_id for evidence_id, count in citation_counts.items() if count > 1
    )
    if missing_ids:
        raise AnalyzerError("semantic result omitted known evidence IDs")
    if duplicate_ids:
        raise AnalyzerError("semantic result reassigned evidence IDs more than once")

    normalized_activities.sort(key=lambda value: value["activity_id"])
    activity_ids = [value["activity_id"] for value in normalized_activities]
    if len(activity_ids) != len(set(activity_ids)):
        raise AnalyzerError(
            "activity identity collision; split activities require distinct specific objects"
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "activities": normalized_activities,
        "exceptions": normalized_exceptions,
        "omissions": normalized_omissions,
    }


@dataclasses.dataclass(frozen=True)
class AnalyzerEndpoint:
    name: str
    url: str
    model: str
    api_key: str = ""
    timeout_seconds: int = 120

    @classmethod
    def from_env(cls, prefix: str, *, default_model: str = "") -> "AnalyzerEndpoint | None":
        url = os.environ.get(f"{prefix}_URL", "").strip()
        if not url:
            return None
        return cls(
            name=prefix.lower(),
            url=url,
            model=os.environ.get(f"{prefix}_MODEL", default_model).strip(),
            api_key=os.environ.get(f"{prefix}_API_KEY", "").strip(),
            timeout_seconds=int(os.environ.get(f"{prefix}_TIMEOUT_SECONDS", "120")),
        )


Transport = Callable[[AnalyzerEndpoint, dict[str, Any]], dict[str, Any]]


def http_transport(endpoint: AnalyzerEndpoint, body: dict[str, Any]) -> dict[str, Any]:
    encoded = canonical_json(body).encode("utf-8")
    request = urllib.request.Request(
        endpoint.url,
        data=encoded,
        headers={
            "Content-Type": "application/json",
            **(
                {"Authorization": f"Bearer {endpoint.api_key}"}
                if endpoint.api_key
                else {}
            ),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=endpoint.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        # Never include request headers/body: they can contain credentials or evidence.
        raise AnalyzerError(f"analyzer endpoint {endpoint.name} failed: {type(exc).__name__}") from exc


def probe_endpoint(endpoint: AnalyzerEndpoint, transport: Transport = http_transport) -> dict[str, Any]:
    body = {
        "model": endpoint.model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "Return JSON only."},
            {"role": "user", "content": '{"probe":"clockify-semantic-v1"}'},
        ],
    }
    raw = transport(endpoint, body)
    response = raw if isinstance(raw, dict) and "choices" not in raw else _json_object_from_response(raw)
    return {"status": "ok", "endpoint": endpoint.name, "model": endpoint.model, "response": response}


def _call_validated(
    endpoint: AnalyzerEndpoint,
    events: list[dict[str, Any]],
    *,
    tier: str,
    transport: Transport,
    corrections: list[dict[str, Any]] | None = None,
    known_evidence_ids: set[str] | None = None,
    evidence_time_spans: dict[str, dict[str, str]] | None = None,
    private_text_approved: bool | None = None,
) -> dict[str, Any]:
    body = _body_for(
        events,
        model=endpoint.model,
        mode="extract",
        corrections=corrections,
        private_text_approved=private_text_approved,
    )
    if len(canonical_json(body).encode("utf-8")) > DEFAULT_MAX_BODY_BYTES:
        raise AnalyzerError("analyzer body exceeds configured request ceiling")
    response = _json_object_from_response(transport(endpoint, body))
    return validate_result(
        response,
        known_evidence_ids=known_evidence_ids or {str(event.get("evidence_id")) for event in events},
        provider_model=endpoint.model,
        analyzer_tier=tier,
        evidence_time_spans=evidence_time_spans,
    )


def _call_synthesis_validated(
    endpoint: AnalyzerEndpoint,
    activities: list[dict[str, Any]],
    *,
    workstream_id: str,
    tier: str,
    transport: Transport,
    known_evidence_ids: set[str],
    evidence_time_spans: dict[str, dict[str, str]],
) -> dict[str, Any]:
    """Synthesize one repeated workstream and reject any lost evidence."""
    body = _synthesis_body(activities, model=endpoint.model, workstream_id=workstream_id)
    if len(canonical_json(body).encode("utf-8")) > DEFAULT_MAX_BODY_BYTES:
        raise AnalyzerError("synthesis body exceeds configured request ceiling")
    result = validate_result(
        _json_object_from_response(transport(endpoint, body)),
        known_evidence_ids=known_evidence_ids,
        provider_model=endpoint.model,
        analyzer_tier=tier,
        evidence_time_spans=evidence_time_spans,
    )
    if result["exceptions"] or result["omissions"]:
        raise AnalyzerError("synthesis must preserve cited activities, not emit nonactivities")
    if len(result["activities"]) > len(activities):
        raise AnalyzerError("synthesis must not split provisional activities")
    citation_counts: dict[str, int] = {}
    for activity in result["activities"]:
        for evidence_id in activity["evidence_ids"]:
            citation_counts[evidence_id] = citation_counts.get(evidence_id, 0) + 1
    if set(citation_counts) != known_evidence_ids or any(
        count != 1 for count in citation_counts.values()
    ):
        raise AnalyzerError("synthesis lost or reassigned cited evidence IDs")
    if len(result["activities"]) > 1:
        workstreams = [activity["workstream_id"] for activity in result["activities"]]
        if len(workstreams) != len(set(workstreams)):
            raise AnalyzerError(
                "synthesis split activities require distinct specific objects"
            )
    return result


def _requires_stronger_fallback(result: dict[str, Any]) -> bool:
    return any(
        activity.get("semantic_confidence") == "low"
        or activity.get("timing_confidence") == "low"
        for activity in result.get("activities", [])
    ) or any(
        str(exception.get("kind") or "").casefold() == "conflicting_evidence"
        for exception in result.get("exceptions", [])
        if isinstance(exception, dict)
    )


def _defer_unresolved_low_confidence(result: dict[str, Any]) -> dict[str, Any]:
    """Turn unresolved low-confidence claims into explicit exceptions."""
    retained: list[dict[str, Any]] = []
    exceptions = list(result.get("exceptions", []))
    for activity in result.get("activities", []):
        confidence_fields = [
            field
            for field in ("semantic_confidence", "timing_confidence")
            if activity.get(field) == "low"
        ]
        if not confidence_fields:
            retained.append(activity)
            continue
        exceptions.append(
            {
                "kind": "low_confidence",
                "evidence_ids": list(activity.get("evidence_ids", [])),
                "reason": f"stronger analyzer retained low {', '.join(confidence_fields)}",
            }
        )
    return {
        **result,
        "activities": retained,
        "exceptions": sorted(exceptions, key=canonical_json),
    }


def analyze_tiered(
    events: Iterable[dict[str, Any]],
    *,
    primary: AnalyzerEndpoint,
    fallback: AnalyzerEndpoint | None = None,
    corrections: list[dict[str, Any]] | None = None,
    transport: Transport = http_transport,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    private_text_approved: bool | None = None,
) -> dict[str, Any]:
    original_events = sorted((dict(event) for event in events), key=_event_sort_key)
    if not original_events:
        return {
            "schema_version": SCHEMA_VERSION,
            "prompt_version": PROMPT_VERSION,
            "ledger_event_count": 0,
            "activities": [],
            "exceptions": [],
            "omissions": [],
            "analysis_chunks": [],
        }
    original_by_id = {str(event.get("evidence_id")): event for event in original_events}
    if len(original_by_id) != len(original_events):
        raise AnalyzerError("evidence IDs must be unique before semantic analysis")
    _require_private_text_approval(original_events, private_text_approved)
    projected_events = project_events(original_events)
    chunks = chunk_events(
        projected_events,
        model=primary.model,
        max_body_bytes=max_body_bytes,
        corrections=corrections,
        private_text_approved=private_text_approved,
    )
    results: list[dict[str, Any]] = []
    metadata: list[dict[str, Any]] = []
    probed: set[AnalyzerEndpoint] = set()

    def probe_once(endpoint: AnalyzerEndpoint) -> None:
        if endpoint not in probed:
            probe_endpoint(endpoint, transport=transport)
            probed.add(endpoint)

    for index, chunk in enumerate(chunks):
        chunk_ids = {str(event.get("evidence_id")) for event in chunk}
        chunk_spans = {
            evidence_id: span
            for evidence_id in chunk_ids
            if (span := _safe_time_span(original_by_id[evidence_id])) is not None
        }
        try:
            probe_once(primary)
            result = _call_validated(
                primary,
                chunk,
                tier="primary",
                transport=transport,
                corrections=corrections,
                known_evidence_ids=chunk_ids,
                evidence_time_spans=chunk_spans,
                private_text_approved=private_text_approved,
            )
            used = primary
            tier = "primary"
            if _requires_stronger_fallback(result) and fallback is not None:
                probe_once(fallback)
                result = _call_validated(
                    fallback,
                    chunk,
                    tier="fallback",
                    transport=transport,
                    corrections=corrections,
                    known_evidence_ids=chunk_ids,
                    evidence_time_spans=chunk_spans,
                    private_text_approved=private_text_approved,
                )
                used = fallback
                tier = "fallback"
        except AnalyzerError as primary_error:
            if fallback is None:
                raise AnalyzerError(
                    f"primary analyzer failed for chunk {index + 1}: {primary_error}"
                ) from primary_error
            probe_once(fallback)
            result = _call_validated(
                fallback,
                chunk,
                tier="fallback",
                transport=transport,
                corrections=corrections,
                known_evidence_ids=chunk_ids,
                evidence_time_spans=chunk_spans,
                private_text_approved=private_text_approved,
            )
            used = fallback
            tier = "fallback"
        if _requires_stronger_fallback(result):
            result = _defer_unresolved_low_confidence(result)
        results.append(result)
        metadata.append(
            {
                "chunk": index + 1,
                "event_count": len(chunk),
                "evidence_digest": stable_digest(
                    "ech-", sorted(event["evidence_id"] for event in chunk)
                ),
                "endpoint": used.name,
                "model": used.model,
                "tier": tier,
            }
        )

    # Exact stable identities are deterministic duplicates.  Broader workstream
    # grouping is only a candidate set: any semantic merge must pass through a
    # separately validated synthesis request below.
    activities_by_id = {
        activity["activity_id"]: activity
        for result in results
        for activity in result["activities"]
    }
    grouped: dict[str, list[dict[str, Any]]] = {}
    for activity in activities_by_id.values():
        grouped.setdefault(activity["workstream_id"], []).append(activity)

    for workstream_id in sorted(grouped):
        provisional = sorted(grouped[workstream_id], key=lambda value: value["activity_id"])
        if len(provisional) < 2:
            continue
        synthesis_ids = {
            evidence_id
            for activity in provisional
            for evidence_id in activity["evidence_ids"]
        }
        synthesis_spans = {
            evidence_id: span
            for evidence_id in synthesis_ids
            if (span := _safe_time_span(original_by_id[evidence_id])) is not None
        }
        try:
            probe_once(primary)
            synthesized = _call_synthesis_validated(
                primary,
                provisional,
                workstream_id=workstream_id,
                tier="primary",
                transport=transport,
                known_evidence_ids=synthesis_ids,
                evidence_time_spans=synthesis_spans,
            )
            used = primary
            tier = "primary"
        except AnalyzerError as primary_error:
            if fallback is None:
                raise AnalyzerError(
                    f"primary analyzer failed for synthesis {workstream_id}: {primary_error}"
                ) from primary_error
            probe_once(fallback)
            synthesized = _call_synthesis_validated(
                fallback,
                provisional,
                workstream_id=workstream_id,
                tier="fallback",
                transport=transport,
                known_evidence_ids=synthesis_ids,
                evidence_time_spans=synthesis_spans,
            )
            used = fallback
            tier = "fallback"
        for activity in provisional:
            del activities_by_id[activity["activity_id"]]
        for activity in synthesized["activities"]:
            if activity["activity_id"] in activities_by_id:
                raise AnalyzerError("synthesis activity identity collides with another workstream")
            activities_by_id[activity["activity_id"]] = activity
    exceptions = [value for result in results for value in result["exceptions"]]
    omissions = [value for result in results for value in result["omissions"]]
    return {
        "schema_version": SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "ledger_event_count": len(original_events),
        "ledger_evidence_digest": stable_digest(
            "led-", sorted(event["evidence_id"] for event in original_events)
        ),
        "activities": sorted(activities_by_id.values(), key=lambda value: value["activity_id"]),
        "exceptions": sorted(exceptions, key=canonical_json),
        "omissions": sorted(omissions, key=canonical_json),
        "analysis_chunks": metadata,
    }


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledger", type=Path, nargs="?", help="evidence-ledger.json")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--fixture", type=Path, help="validated analyzer response fixture")
    parser.add_argument("--probe", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    primary = AnalyzerEndpoint.from_env(
        "CLOCKIFY_ANALYZER_PRIMARY", default_model=DEFAULT_PRIMARY_MODEL
    )
    fallback = AnalyzerEndpoint.from_env("CLOCKIFY_ANALYZER_FALLBACK")
    if primary is None:
        print("semantic analyzer: CLOCKIFY_ANALYZER_PRIMARY_URL is required", file=sys.stderr)
        return 2
    if args.probe:
        print(canonical_json(probe_endpoint(primary)))
        return 0
    if args.ledger is None:
        print("semantic analyzer: ledger path is required", file=sys.stderr)
        return 2
    ledger = _load_json(args.ledger)
    events = ledger.get("events", [])
    if args.fixture:
        fixture = validate_result(
            _load_json(args.fixture),
            known_evidence_ids={str(event.get("evidence_id")) for event in events},
            provider_model="fixture",
            analyzer_tier="fixture",
        )
        result = {
            **fixture,
            "ledger_event_count": len(events),
            "ledger_evidence_digest": stable_digest(
                "led-", sorted(event["evidence_id"] for event in events)
            ),
            "analysis_chunks": [],
        }
    else:
        result = analyze_tiered(events, primary=primary, fallback=fallback)
    output = args.output or args.ledger.with_name("semantic-analysis.json")
    _write_json(output, result)
    print(output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
