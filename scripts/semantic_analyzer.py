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
import copy
import dataclasses
import datetime as dt
import fcntl
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
PROMPT_VERSION = "clockify-semantic-v7"
ANALYZER_CACHE_SCHEMA_VERSION = "clockify-analyzer-cache/v1"
DEFAULT_PRIMARY_MODEL = "deepseek-v4-flash:cloud"
DEFAULT_MAX_BODY_BYTES = 1_450_000
DEFAULT_CHUNK_BODY_BYTES = 500_000
DEFAULT_MAX_EVENTS_PER_CHUNK = 250
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


class AnalyzerContractError(AnalyzerError):
    """A sealed provider response rejected by the semantic output contract."""


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
    # Redaction can expose a second adjacent identifier only after whitespace
    # and punctuation are normalized. Converge every direct-identifier class
    # instead of weakening the final fail-closed assertion.
    for pattern, replacement in (
        (URL_RE, "[url removed]"),
        (EMAIL_RE, "[email removed]"),
        (PATH_RE, "[path removed]"),
        (BEARER_RE, "[credential removed]"),
        (SECRET_RE, "[credential removed]"),
        (HASH_RE, "[hash removed]"),
    ):
        safe = _sub_until_stable(pattern, replacement, safe)
    if any(pattern.search(safe) for pattern in (URL_RE, EMAIL_RE, PATH_RE, BEARER_RE, SECRET_RE, HASH_RE, COMMAND_RE, TRANSPORT_RE)):
        raise AnalyzerError("semantic projection contains unsafe text")
    return safe


def _sub_until_stable(
    pattern: re.Pattern[str], replacement: str, value: str, *, max_passes: int = 8
) -> str:
    for _ in range(max_passes):
        updated = pattern.sub(replacement, value)
        if updated == value:
            return value
        value = updated
    if pattern.search(value):
        raise AnalyzerError("semantic projection redaction did not converge")
    return value


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


def _evidence_reference_maps(evidence_ids: Iterable[str]) -> tuple[dict[str, str], dict[str, str]]:
    """Create compact deterministic model references and their local inverse."""
    originals = sorted(str(value) for value in evidence_ids)
    if len(originals) != len(set(originals)):
        raise AnalyzerError("cannot alias duplicate evidence IDs")
    if any(not SAFE_EVIDENCE_ID_RE.fullmatch(value) for value in originals):
        raise AnalyzerError("cannot alias an unsafe evidence ID")
    forward = {value: f"ref-{index:04d}" for index, value in enumerate(originals, 1)}
    return forward, {alias: original for original, alias in forward.items()}


def _restore_evidence_references(
    response: Mapping[str, Any], *, evidence_ids: Iterable[str]
) -> dict[str, Any]:
    """Resolve model-facing evidence refs without trusting copied long hashes."""
    _, inverse = _evidence_reference_maps(evidence_ids)
    restored = copy.deepcopy(dict(response))
    for classification in ("activities", "exceptions", "omissions"):
        records = restored.get(classification)
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict) or not isinstance(record.get("evidence_ids"), list):
                continue
            values: list[str] = []
            for raw in record["evidence_ids"]:
                alias = str(raw)
                if alias not in inverse:
                    raise AnalyzerError("analyzer returned an unknown evidence reference")
                values.append(inverse[alias])
            record["evidence_ids"] = values
    return restored


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
Implementation plus verification of that same change is one accomplishment, not two.
For example, implementing a stable identity and confirming that same identity survives
allocation movement must be one activity citing both evidence items, with a specific
merge_rationale. Verification becomes separate only when it produces an independently
meaningful deliverable or diagnoses a distinct problem.
Every reviewable activity needs a specific split_rationale explaining why it is one
atomic accomplishment. Never join verbs with "and", "or", "then", or "also".
Every activities[] item MUST have a non-empty split_rationale, including an already
atomic item or an item created by merging duplicate evidence. Never leave it blank.
The action must start with a capitalized past-tense verb phrase of one to three words. Put the specific
object and bounded result in object and outcome, not in a long action.
Preserve the evidence's concrete nouns and quantities in object and outcome. Never
replace a specific result such as removing duplicate buffers with a generic claim such
as reducing usage. Do not repeat the object's final word as the outcome's first word.
Give related atomic accomplishments the same short parent workstream name even when
their specific objects differ; unrelated work must not share a workstream.
Classify planned work, waiting, polling, agent chatter, heartbeats, and autonomous
background execution as planned or noise, not completed human work. A blocker is
loggable only when the evidence proves substantive diagnosis or remediation.
Do not invent projects, outcomes, evidence, effort, or meeting purpose. For a
title-only meeting with no supported outcome, emit an exception. Effort is human
attention, not process runtime or empty wall-clock time. Evidence IDs are short refs
such as ref-0001 and must be copied exactly from the input. Account for every input
evidence ref exactly once across
activities, exceptions, and omissions. Exceptions and omissions require cited
evidence refs. Do not allocate start/end Clockify blocks.
Write action + object + outcome so the final prefixed description is 8-14 words,
using terse past-tense Caveman wording and no Markdown, IDs, paths, URLs, or status prose.
Project prefix and tags are recommendations only. Leave them blank when the evidence
does not explicitly support them; deterministic routing owns the final project and tags.

Output object:
{
  "activities": [{
    "lifecycle": "completed|advanced|investigated|meeting|planned|blocked|noise",
    "workstream": "short stable parent workstream name",
    "action": "short verb phrase",
    "object": "specific work object",
    "outcome": "bounded evidenced outcome",
    "evidence_ids": ["ref-0001"],
    "evidence_spans": [{"start":"ISO-like", "end":"ISO-like"}],
    "project_recommendation": {"name":"", "prefix":"", "tag_names":[]},
    "effort": {"minimum_minutes":1, "recommended_minutes":1, "maximum_minutes":1},
    "semantic_confidence": "low|medium|high",
    "timing_confidence": "low|medium|high",
    "split_rationale": "why this is exactly one atomic accomplishment",
    "merge_rationale": "",
    "omit_rationale": ""
  }],
  "exceptions": [{"kind":"insufficient_evidence|conflicting_evidence", "evidence_ids":[], "reason":""}],
  "omissions": [{"lifecycle":"planned|noise", "evidence_ids":[], "reason":""}]
}
"""
    projected = project_events(events)
    aliases, _ = _evidence_reference_maps(event["evidence_id"] for event in projected)
    model_events = [
        {**event, "evidence_id": aliases[event["evidence_id"]]}
        for event in projected
    ]
    payload = {
        "mode": mode,
        "schema_version": SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "events": model_events,
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
        "seed": 0,
        "response_format": {"type": "json_object"},
        "messages": _request_messages(
            events,
            mode=mode,
            corrections=corrections,
            private_text_approved=private_text_approved,
        ),
    }


def _escaped_json_content_bytes(value: str) -> int:
    """Return UTF-8 bytes needed when value is embedded in a JSON string."""
    # canonical_json(value) includes precisely the two enclosing JSON quotes.
    return len(canonical_json(value).encode("utf-8")) - 2


def _alias_extra_bytes(count: int) -> int:
    """Return bytes added when extraction aliases grow past ``ref-0001``.

    The aliases are assigned from the sorted evidence-ID set.  Their values do
    not affect serialization except when the decimal index becomes wider, so
    the total adjustment depends only on the number of events in the request.
    """
    extra = 0
    start = 10_000
    width = 5
    while start <= count:
        end = min(count, (10 ** width) - 1)
        extra += (end - start + 1) * (width - 4)
        start = end + 1
        width += 1
    return extra


def _chunk_event_bytes(event: dict[str, Any]) -> int:
    """Return the embedded request-payload size of one projected event."""
    evidence_id = str(event.get("evidence_id") or "")
    if not SAFE_EVIDENCE_ID_RE.fullmatch(evidence_id):
        raise AnalyzerError("cannot alias an unsafe evidence ID")
    # All aliases through ref-9999 have this same byte length.  Wider aliases
    # are accounted for collectively by _alias_extra_bytes.
    model_event = {**event, "evidence_id": "ref-0001"}
    return _escaped_json_content_bytes(canonical_json(model_event))


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
    aliases, _ = _evidence_reference_maps(
        evidence_id
        for activity in provisional
        for evidence_id in activity["evidence_ids"]
    )
    model_provisional = [
        {
            **activity,
            "evidence_ids": [aliases[value] for value in activity["evidence_ids"]],
        }
        for activity in provisional
    ]
    system = """You reconcile provisional Clockify semantic activities from repeated workstream evidence.
Return JSON only, using the same activities/exceptions/omissions schema supplied for extraction.
Merge provisional activities only when their cited evidence proves the same single atomic accomplishment.
When accomplishments differ, preserve separate activities and give each a distinct, specific object.
Preserve the supplied parent workstream name for related atomic accomplishments.
Every input evidence ref must appear in exactly one returned activity; do not add, omit, or move evidence
to exceptions or omissions. Copy the short evidence refs and evidence spans exactly from supported input.
Every returned activity must have a non-empty split_rationale; merged evidence must also have a specific
non-empty merge_rationale.
Do not invent projects, outcomes, effort, or timing. Never include paths, URLs, emails, secrets, IDs,
or status prose in descriptive fields."""
    payload = {
        "mode": "synthesize",
        "schema_version": SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "workstream_id": workstream_id,
        "provisional_activities": model_provisional,
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
        "seed": 0,
        "response_format": {"type": "json_object"},
        "messages": _synthesis_messages(activities, workstream_id=workstream_id),
    }


def chunk_events(
    events: Iterable[dict[str, Any]],
    *,
    model: str = DEFAULT_PRIMARY_MODEL,
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES,
    target_body_bytes: int | None = None,
    max_events_per_chunk: int = DEFAULT_MAX_EVENTS_PER_CHUNK,
    corrections: list[dict[str, Any]] | None = None,
    private_text_approved: bool | None = None,
) -> list[list[dict[str, Any]]]:
    """Partition without truncating any event, preferring day boundaries.

    Oversized individual evidence is rejected: silently clipping it would violate
    the complete-context and cited-evidence contract.
    """
    if max_events_per_chunk <= 0:
        raise AnalyzerError("max_events_per_chunk must be positive")
    if target_body_bytes is None:
        target_body_bytes = max_body_bytes
    target_body_bytes = min(target_body_bytes, max_body_bytes)
    if target_body_bytes <= 0:
        raise AnalyzerError("target_body_bytes must be positive")
    # Size exactly what may leave the machine, never the raw immutable ledger.
    ordered = project_events(events)
    _require_private_text_approval(ordered, private_text_approved)
    # This empty request gives the exact model/prompt/correction envelope once.
    # Candidate event text is then accounted for incrementally below; rebuilding
    # a complete request for every event made large immutable ledgers quadratic.
    empty_body_bytes = len(
        canonical_json(
            _body_for(
                [],
                model=model,
                mode="extract",
                corrections=corrections,
                private_text_approved=private_text_approved,
            )
        ).encode("utf-8")
    )
    by_day: dict[str, list[dict[str, Any]]] = {}
    for event in ordered:
        by_day.setdefault(_event_day(event), []).append(event)

    chunks: list[list[dict[str, Any]]] = []
    for day in sorted(by_day):
        current: list[dict[str, Any]] = []
        current_event_bytes = 0
        for event in by_day[day]:
            event_bytes = _chunk_event_bytes(event)
            one_size = empty_body_bytes + event_bytes + _alias_extra_bytes(1)
            if one_size > max_body_bytes:
                raise AnalyzerError(
                    f"evidence event {event.get('evidence_id') or '<unknown>'} "
                    f"exceeds analyzer request ceiling ({one_size} bytes)"
                )
            trial_count = len(current) + 1
            trial_size = (
                empty_body_bytes
                + current_event_bytes
                + event_bytes
                + len(current)
                + _alias_extra_bytes(trial_count)
            )
            if current and (
                trial_size > target_body_bytes
                or len(current) >= max_events_per_chunk
            ):
                chunks.append(current)
                current = [event]
                current_event_bytes = event_bytes
            else:
                current.append(event)
                current_event_bytes += event_bytes
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


def _five_minute_effort(value: int) -> int:
    """Normalize model estimates to ordinary timesheet granularity."""
    return max(5, ((value + 2) // 5) * 5)


def _effort_band(recommended: int) -> tuple[int, int]:
    """Derive conservative deterministic bounds around a central estimate."""
    minimum_unrounded = max(1, (recommended * 2) // 3)
    maximum_unrounded = max(recommended, (recommended * 4 + 2) // 3)
    minimum = max(5, (minimum_unrounded // 5) * 5)
    maximum = max(recommended, ((maximum_unrounded + 4) // 5) * 5)
    return minimum, maximum


def _normalized_identity(value: Any) -> str:
    """Normalize harmless wording/punctuation variation for stable semantic IDs."""
    text = _one_line(value).casefold()
    return re.sub(r"[^\w]+", " ", text, flags=re.UNICODE).strip()


def _normalize_action(value: Any) -> str:
    """Canonicalize casing and redundant same-change verification verbs only."""
    action = _one_line(value)
    redundant_verification = re.fullmatch(
        r"([\w'-]+)\s+(?:and|then|&)\s+(?:verified|validated|confirmed|tested)",
        action,
        flags=re.IGNORECASE | re.UNICODE,
    )
    if redundant_verification:
        action = redundant_verification.group(1)
    return action[:1].upper() + action[1:] if action else action


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
        action = _normalize_action(raw.get("action"))
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
        raw_minimum = _positive_int(effort.get("minimum_minutes"), "minimum_minutes")
        raw_recommended = _positive_int(effort.get("recommended_minutes"), "recommended_minutes")
        raw_maximum = _positive_int(effort.get("maximum_minutes"), "maximum_minutes")
        if not raw_minimum <= raw_recommended <= raw_maximum:
            raise AnalyzerError("effort must satisfy minimum <= recommended <= maximum")
        recommended = _five_minute_effort(raw_recommended)
        minimum, maximum = _effort_band(recommended)
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


class AnalyzerResponseCache:
    """Append-only cache for validated semantic responses.

    The cache stores no request prose.  Keys bind the route and complete request body
    by digest; records retain only the validated structured response needed to make an
    immutable replay independent of provider wording drift.
    """

    def __init__(self, path: Path):
        self.path = path
        self._records: dict[str, dict[str, Any]] = {}
        self.hits = 0
        self.misses = 0
        self.used: dict[str, str] = {}
        self._load()

    @staticmethod
    def _request_identity(endpoint: AnalyzerEndpoint, body: Mapping[str, Any]) -> dict[str, str]:
        body_digest = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        route_digest = hashlib.sha256(
            canonical_json(
                {"name": endpoint.name, "url": endpoint.url, "model": endpoint.model}
            ).encode("utf-8")
        ).hexdigest()
        cache_key = stable_digest(
            "arc-",
            {
                "schema_version": ANALYZER_CACHE_SCHEMA_VERSION,
                "prompt_version": PROMPT_VERSION,
                "semantic_schema_version": SCHEMA_VERSION,
                "route_digest": route_digest,
                "body_digest": body_digest,
            },
            length=64,
        )
        return {
            "cache_key": cache_key,
            "body_digest": body_digest,
            "route_digest": route_digest,
        }

    @staticmethod
    def _decision_digest(value: Mapping[str, Any]) -> str:
        return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()

    def _validate_record(self, value: Any, *, line_number: int) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise AnalyzerError(f"analyzer cache line {line_number} is not an object")
        required = {
            "schema_version",
            "cache_key",
            "body_digest",
            "route_digest",
            "model",
            "prompt_version",
            "semantic_schema_version",
            "status",
            "decision_digest",
        }
        status = str(value.get("status") or "")
        variant_field = "response" if status == "accepted" else "failure_code"
        if status not in {"accepted", "rejected"} or set(value) != required | {variant_field}:
            raise AnalyzerError(f"analyzer cache line {line_number} has unsupported fields")
        if value.get("schema_version") != ANALYZER_CACHE_SCHEMA_VERSION:
            raise AnalyzerError(f"analyzer cache line {line_number} has an unsupported schema")
        prompt_version = str(value.get("prompt_version") or "")
        semantic_schema_version = value.get("semantic_schema_version")
        if not re.fullmatch(r"clockify-semantic-v[1-9][0-9]*", prompt_version):
            raise AnalyzerError(f"analyzer cache line {line_number} has an invalid prompt version")
        if not isinstance(semantic_schema_version, int) or semantic_schema_version < 1:
            raise AnalyzerError(f"analyzer cache line {line_number} has an invalid semantic schema version")
        for name in ("body_digest", "route_digest", "decision_digest"):
            if not re.fullmatch(r"[a-f0-9]{64}", str(value.get(name) or "")):
                raise AnalyzerError(f"analyzer cache line {line_number} has an invalid {name}")
        if not re.fullmatch(r"arc-[a-f0-9]{64}", str(value.get("cache_key") or "")):
            raise AnalyzerError(f"analyzer cache line {line_number} has an invalid cache key")
        expected_key = stable_digest(
            "arc-",
            {
                "schema_version": ANALYZER_CACHE_SCHEMA_VERSION,
                "prompt_version": prompt_version,
                "semantic_schema_version": semantic_schema_version,
                "route_digest": value["route_digest"],
                "body_digest": value["body_digest"],
            },
            length=64,
        )
        if value["cache_key"] != expected_key:
            raise AnalyzerError(f"analyzer cache line {line_number} identity digest differs")
        decision = {"status": status, variant_field: value[variant_field]}
        if status == "accepted" and not isinstance(value.get("response"), dict):
            raise AnalyzerError(f"analyzer cache line {line_number} response is invalid")
        if status == "rejected" and value.get("failure_code") != "contract_rejected":
            raise AnalyzerError(f"analyzer cache line {line_number} rejection is invalid")
        if self._decision_digest(decision) != value.get("decision_digest"):
            raise AnalyzerError(f"analyzer cache line {line_number} decision digest differs")
        return copy.deepcopy(value)

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
                try:
                    lines = handle.read().splitlines()
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise AnalyzerError("analyzer cache cannot be read") from exc
        self._merge_lines(lines)

    def _merge_lines(self, lines: Iterable[str]) -> None:
        for line_number, line in enumerate(lines, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AnalyzerError(f"analyzer cache line {line_number} is invalid JSON") from exc
            record = self._validate_record(value, line_number=line_number)
            key = str(record["cache_key"])
            prior = self._records.get(key)
            if prior is not None and prior != record:
                raise AnalyzerError(f"analyzer cache key conflicts at line {line_number}")
            self._records[key] = record

    def lookup(self, endpoint: AnalyzerEndpoint, body: Mapping[str, Any]) -> dict[str, Any] | None:
        identity = self._request_identity(endpoint, body)
        record = self._records.get(identity["cache_key"])
        if record is None and self.path.exists():
            # Another guarded run may have appended after this instance loaded.
            self._load()
            record = self._records.get(identity["cache_key"])
        if record is None:
            self.misses += 1
            return None
        if record["body_digest"] != identity["body_digest"] or record["route_digest"] != identity["route_digest"]:
            raise AnalyzerError("analyzer cache identity collision")
        self.hits += 1
        self.used[identity["cache_key"]] = str(record["decision_digest"])
        if record["status"] == "rejected":
            raise AnalyzerContractError(
                "analyzer cache records a contract-rejected response"
            )
        return copy.deepcopy(record["response"])

    def _store_record(self, record: dict[str, Any]) -> None:
        key = str(record["cache_key"])
        prior = self._records.get(key)
        if prior is not None:
            if prior != record:
                raise AnalyzerError("analyzer cache cannot replace an existing decision")
            self.used[key] = str(record["decision_digest"])
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    handle.seek(0)
                    self._merge_lines(handle.read().splitlines())
                    prior = self._records.get(key)
                    if prior is not None:
                        if prior != record:
                            raise AnalyzerError(
                                "analyzer cache cannot replace an existing decision"
                            )
                    else:
                        handle.seek(0, os.SEEK_END)
                        handle.write(canonical_json(record) + "\n")
                        handle.flush()
                        os.fsync(handle.fileno())
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise AnalyzerError("analyzer cache cannot be written") from exc
        self._records[key] = record
        self.used[key] = str(record["decision_digest"])

    def store_accepted(
        self,
        endpoint: AnalyzerEndpoint,
        body: Mapping[str, Any],
        response: Mapping[str, Any],
    ) -> None:
        identity = self._request_identity(endpoint, body)
        response_value = copy.deepcopy(dict(response))
        decision = {"status": "accepted", "response": response_value}
        record = {
            "schema_version": ANALYZER_CACHE_SCHEMA_VERSION,
            **identity,
            "model": endpoint.model,
            "prompt_version": PROMPT_VERSION,
            "semantic_schema_version": SCHEMA_VERSION,
            "status": "accepted",
            "decision_digest": self._decision_digest(decision),
            "response": response_value,
        }
        self._store_record(record)

    def store_rejected(self, endpoint: AnalyzerEndpoint, body: Mapping[str, Any]) -> None:
        identity = self._request_identity(endpoint, body)
        decision = {"status": "rejected", "failure_code": "contract_rejected"}
        self._store_record(
            {
                "schema_version": ANALYZER_CACHE_SCHEMA_VERSION,
                **identity,
                "model": endpoint.model,
                "prompt_version": PROMPT_VERSION,
                "semantic_schema_version": SCHEMA_VERSION,
                "status": "rejected",
                "decision_digest": self._decision_digest(decision),
                "failure_code": "contract_rejected",
            }
        )

    def summary(self) -> dict[str, Any]:
        return {
            "schema_version": ANALYZER_CACHE_SCHEMA_VERSION,
            "hits": self.hits,
            "misses": self.misses,
            "records": [
                {"cache_key": key, "decision_digest": digest}
                for key, digest in sorted(self.used.items())
            ],
        }


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
            {"role": "user", "content": '{"probe":"clockify-semantic-v7"}'},
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
    cache: AnalyzerResponseCache | None = None,
    before_transport: Callable[[AnalyzerEndpoint], None] | None = None,
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
    response = cache.lookup(endpoint, body) if cache is not None else None
    cache_miss = response is None
    if response is None:
        if before_transport is not None:
            before_transport(endpoint)
        raw_response = transport(endpoint, body)
        try:
            response = _json_object_from_response(raw_response)
        except AnalyzerError as exc:
            if cache is not None and cache_miss:
                cache.store_rejected(endpoint, body)
            raise AnalyzerContractError(str(exc)) from exc
    try:
        restored_response = _restore_evidence_references(
            response,
            evidence_ids=known_evidence_ids
            or {str(event.get("evidence_id")) for event in events},
        )
        result = validate_result(
            restored_response,
            known_evidence_ids=known_evidence_ids or {str(event.get("evidence_id")) for event in events},
            provider_model=endpoint.model,
            analyzer_tier=tier,
            evidence_time_spans=evidence_time_spans,
        )
    except AnalyzerError as exc:
        if cache is not None and cache_miss:
            cache.store_rejected(endpoint, body)
        raise AnalyzerContractError(str(exc)) from exc
    if cache is not None and cache_miss:
        cache.store_accepted(endpoint, body, response)
    return result


def _call_synthesis_validated(
    endpoint: AnalyzerEndpoint,
    activities: list[dict[str, Any]],
    *,
    workstream_id: str,
    tier: str,
    transport: Transport,
    known_evidence_ids: set[str],
    evidence_time_spans: dict[str, dict[str, str]],
    cache: AnalyzerResponseCache | None = None,
    before_transport: Callable[[AnalyzerEndpoint], None] | None = None,
) -> dict[str, Any]:
    """Synthesize one repeated workstream and reject any lost evidence."""
    body = _synthesis_body(activities, model=endpoint.model, workstream_id=workstream_id)
    if len(canonical_json(body).encode("utf-8")) > DEFAULT_MAX_BODY_BYTES:
        raise AnalyzerError("synthesis body exceeds configured request ceiling")
    response = cache.lookup(endpoint, body) if cache is not None else None
    cache_miss = response is None
    if response is None:
        if before_transport is not None:
            before_transport(endpoint)
        raw_response = transport(endpoint, body)
        try:
            response = _json_object_from_response(raw_response)
        except AnalyzerError as exc:
            if cache is not None and cache_miss:
                cache.store_rejected(endpoint, body)
            raise AnalyzerContractError(str(exc)) from exc
    try:
        restored_response = _restore_evidence_references(
            response, evidence_ids=known_evidence_ids
        )
        result = validate_result(
            restored_response,
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
    except AnalyzerError as exc:
        if cache is not None and cache_miss:
            cache.store_rejected(endpoint, body)
        raise AnalyzerContractError(str(exc)) from exc
    if cache is not None and cache_miss:
        cache.store_accepted(endpoint, body, response)
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
    target_body_bytes: int = DEFAULT_CHUNK_BODY_BYTES,
    max_events_per_chunk: int = DEFAULT_MAX_EVENTS_PER_CHUNK,
    private_text_approved: bool | None = None,
    cache: AnalyzerResponseCache | None = None,
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
            "analyzer_cache": (
                cache.summary()
                if cache is not None
                else {"schema_version": ANALYZER_CACHE_SCHEMA_VERSION, "status": "disabled"}
            ),
        }
    original_by_id = {str(event.get("evidence_id")): event for event in original_events}
    if len(original_by_id) != len(original_events):
        raise AnalyzerError("evidence IDs must be unique before semantic analysis")
    _require_private_text_approval(original_events, private_text_approved)
    chunks = chunk_events(
        original_events,
        model=primary.model,
        max_body_bytes=max_body_bytes,
        target_body_bytes=target_body_bytes,
        max_events_per_chunk=max_events_per_chunk,
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
        fallback_status = "not_needed"
        try:
            result = _call_validated(
                primary,
                chunk,
                tier="primary",
                transport=transport,
                corrections=corrections,
                known_evidence_ids=chunk_ids,
                evidence_time_spans=chunk_spans,
                private_text_approved=private_text_approved,
                cache=cache,
                before_transport=probe_once,
            )
        except AnalyzerError as primary_error:
            if fallback is None:
                raise AnalyzerError(
                    f"primary analyzer failed for chunk {index + 1}: {primary_error}"
                ) from primary_error
            try:
                result = _call_validated(
                    fallback,
                    chunk,
                    tier="fallback",
                    transport=transport,
                    corrections=corrections,
                    known_evidence_ids=chunk_ids,
                    evidence_time_spans=chunk_spans,
                    private_text_approved=private_text_approved,
                    cache=cache,
                    before_transport=probe_once,
                )
            except AnalyzerError as fallback_error:
                if not all(isinstance(error, AnalyzerContractError) for error in (
                    primary_error, fallback_error
                )):
                    raise AnalyzerError(
                        f"primary and fallback analyzers failed without dual contract "
                        f"rejection for chunk {index + 1}: primary={primary_error}; "
                        f"fallback={fallback_error}"
                    ) from fallback_error
                # Both configured routes were usable at startup but rejected this
                # bounded semantic partition.  Preserve complete evidence as one
                # explicit local exception so a single model failure cannot halt
                # reconciliation of every other chunk.
                result = {
                    "schema_version": SCHEMA_VERSION,
                    "prompt_version": PROMPT_VERSION,
                    "activities": [],
                    "exceptions": [{
                        "kind": "analyzer_failure",
                        "evidence_ids": sorted(chunk_ids),
                        "reason": "primary and fallback rejected this semantic chunk",
                    }],
                    "omissions": [],
                }
                used = primary
                tier = "exception"
                fallback_status = "failed_exception"
                fallback_failure_digest = stable_digest(
                    "aer-",
                    {
                        "mode": "extract",
                        "evidence_ids": sorted(chunk_ids),
                        "primary": {"name": primary.name, "model": primary.model},
                        "fallback": {"name": fallback.name, "model": fallback.model},
                        "prompt_version": PROMPT_VERSION,
                        "schema_version": SCHEMA_VERSION,
                    },
                )
            else:
                used = fallback
                tier = "fallback"
                fallback_status = "used_after_primary_failure"
        else:
            used = primary
            tier = "primary"
            if _requires_stronger_fallback(result) and fallback is not None:
                primary_result = result
                try:
                    result = _call_validated(
                        fallback,
                        chunk,
                        tier="fallback",
                        transport=transport,
                        corrections=corrections,
                        known_evidence_ids=chunk_ids,
                        evidence_time_spans=chunk_spans,
                        private_text_approved=private_text_approved,
                        cache=cache,
                        before_transport=probe_once,
                    )
                except AnalyzerError:
                    # A validated low-confidence primary decision is still useful
                    # evidence.  If the stronger route fails, retain only its
                    # review-safe portions and turn unresolved claims into
                    # explicit exceptions instead of retrying a sealed rejection.
                    result = _defer_unresolved_low_confidence(primary_result)
                    fallback_status = "failed_deferred"
                else:
                    used = fallback
                    tier = "fallback"
                    fallback_status = "used_for_low_confidence"
        if _requires_stronger_fallback(result):
            result = _defer_unresolved_low_confidence(result)
        results.append(result)
        chunk_metadata = {
                "chunk": index + 1,
                "event_count": len(chunk),
                "evidence_digest": stable_digest(
                    "ech-", sorted(event["evidence_id"] for event in chunk)
                ),
                "endpoint": used.name,
                "model": used.model,
                "tier": tier,
                "fallback_status": fallback_status,
            }
        if fallback_status == "failed_exception":
            chunk_metadata["failure_digest"] = fallback_failure_digest
            chunk_metadata["fallback_endpoint"] = fallback.name
            chunk_metadata["fallback_model"] = fallback.model
        metadata.append(chunk_metadata)

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

    synthesis_exceptions: list[dict[str, Any]] = []
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
            synthesized = _call_synthesis_validated(
                primary,
                provisional,
                workstream_id=workstream_id,
                tier="primary",
                transport=transport,
                known_evidence_ids=synthesis_ids,
                evidence_time_spans=synthesis_spans,
                cache=cache,
                before_transport=probe_once,
            )
            used = primary
            tier = "primary"
        except AnalyzerError as primary_error:
            if fallback is None:
                raise AnalyzerError(
                    f"primary analyzer failed for synthesis {workstream_id}: {primary_error}"
                ) from primary_error
            try:
                synthesized = _call_synthesis_validated(
                    fallback,
                    provisional,
                    workstream_id=workstream_id,
                    tier="fallback",
                    transport=transport,
                    known_evidence_ids=synthesis_ids,
                    evidence_time_spans=synthesis_spans,
                    cache=cache,
                    before_transport=probe_once,
                )
            except AnalyzerError as fallback_error:
                if not all(isinstance(error, AnalyzerContractError) for error in (
                    primary_error, fallback_error
                )):
                    raise AnalyzerError(
                        f"primary and fallback analyzers failed without dual contract "
                        f"rejection for synthesis {workstream_id}: "
                        f"primary={primary_error}; fallback={fallback_error}"
                    ) from fallback_error
                # Extraction was individually valid, but neither configured
                # route could decide whether these repeated workstream claims
                # should merge.  Do not emit potentially duplicate proposals
                # and do not let one unresolved workstream halt other work.
                for activity in provisional:
                    del activities_by_id[activity["activity_id"]]
                synthesis_exceptions.append({
                    "kind": "analyzer_synthesis_failure",
                    "evidence_ids": sorted(synthesis_ids),
                    "reason": "primary and fallback rejected workstream synthesis",
                    "failure_digest": stable_digest(
                        "aer-",
                        {
                            "mode": "synthesize",
                            "workstream_id": workstream_id,
                            "evidence_ids": sorted(synthesis_ids),
                            "primary": {"name": primary.name, "model": primary.model},
                            "fallback": {"name": fallback.name, "model": fallback.model},
                            "prompt_version": PROMPT_VERSION,
                            "schema_version": SCHEMA_VERSION,
                        },
                    ),
                    "primary_model": primary.model,
                    "fallback_model": fallback.model,
                })
                continue
            used = fallback
            tier = "fallback"
        for activity in provisional:
            del activities_by_id[activity["activity_id"]]
        for activity in synthesized["activities"]:
            if activity["activity_id"] in activities_by_id:
                raise AnalyzerError("synthesis activity identity collides with another workstream")
            activities_by_id[activity["activity_id"]] = activity
    exceptions = [value for result in results for value in result["exceptions"]]
    exceptions.extend(synthesis_exceptions)
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
        "analyzer_cache": (
            cache.summary()
            if cache is not None
            else {"schema_version": ANALYZER_CACHE_SCHEMA_VERSION, "status": "disabled"}
        ),
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
