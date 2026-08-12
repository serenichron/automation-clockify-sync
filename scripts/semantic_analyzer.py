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
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
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
import threading
from typing import Any, Callable, Iterable, Mapping
import urllib.error
import urllib.request

try:
    from scripts import caveman_renderer
except ImportError:  # pragma: no cover - direct script execution fallback
    import caveman_renderer  # type: ignore[no-redef]


SCHEMA_VERSION = 1
PROMPT_VERSION = "clockify-semantic-v17"
REVIEW_PROMPT_VERSION = "clockify-semantic-review-v6"
PORTFOLIO_REVIEW_PROMPT_VERSION = "clockify-portfolio-review-v2"
PORTFOLIO_VALIDATION_PROMPT_VERSION = "clockify-portfolio-validation-v1"
ANALYZER_CACHE_SCHEMA_VERSION = "clockify-analyzer-cache/v2"
EVIDENCE_BUNDLE_SCHEMA_VERSION = "clockify-semantic-evidence-bundle/v1"
DEFAULT_PRIMARY_MODEL = "deepseek-v4-flash:cloud"
APPROVED_PRIMARY_MODELS = frozenset({
    DEFAULT_PRIMARY_MODEL,
    "deepseek-v4-flash:0731-cloud",
})
FORBIDDEN_ANALYZER_MODEL_MARKERS = ("deepseek-v4-pro",)
DEFAULT_MAX_BODY_BYTES = 1_450_000
# Operational limits are deliberately well below the hard request ceiling.  The
# cloud routes rejected or timed out on larger, mixed workstreams; small bounded
# partitions make failures reviewable without dropping their evidence.
DEFAULT_CHUNK_BODY_BYTES = 250_000
DEFAULT_MAX_EVENTS_PER_CHUNK = 250
DEFAULT_ANALYZER_WORKERS = 4
MAX_CONTRACT_REPAIR_ATTEMPTS = 2
MAX_TIMEOUT_RECOVERY_ATTEMPTS = 3
MAX_CONNECTION_RECOVERY_ATTEMPTS = 3
# A dual contract rejection can be caused by a request that is semantically too
# dense for both available routes, not by the underlying evidence being bad.
# Recover only by deterministic bisection.  The default extraction limit is
# 250 events, so eight splits reach singleton evidence without an unbounded
# retry tree.
MAX_PARTITION_RECOVERY_DEPTH = 8
MIN_PARTITION_RECOVERY_EVENTS = 1
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
_SECONDARY_ACTION_RE = re.compile(
    r"\b(?:and|then|also)\s+(?:"
    r"[a-z][a-z'-]*(?:ed|en)|built|brought|cut|did|found|kept|made|ran|sent|set|"
    r"taught|took|wrote"
    r")\b",
    re.IGNORECASE,
)
_PARALLEL_RESULT_RE = re.compile(
    r"\b(?:[a-z][a-z'-]*\s+){0,3}(?:[a-z][a-z'-]*(?:ed|en)|built|found|made|"
    r"sent|set|wrote)\s+and\s+(?:[a-z][a-z'-]*\s+){0,3}(?:"
    r"[a-z][a-z'-]*(?:ed|en)|built|found|made|sent|set|wrote)\b",
    re.IGNORECASE,
)
_OUTCOME_ACTION_START_RE = re.compile(
    r"^(?:created|generated|produced|scheduled|wrote|built|launched|provisioned|"
    r"implemented|integrated|updated|provided|sent|saved|committed|drafted|"
    r"configured|deployed|published|uploaded|migrated|installed|executed|completed|"
    r"processed|researched|recommended|incorporated|aligned|wired)\b",
    re.IGNORECASE,
)
_ANTI_SPLIT_RATIONALE_RE = re.compile(
    r"\bboth(?:\s+[a-z][a-z'-]*){0,4}\s+and\b|\b(?:performed|delivered)\s+both\b|"
    r"\b(?:two|three|multiple|several)\s+(?:[a-z][a-z'-]*\s+){0,2}"
    r"(?:requests|tasks|deliverables|outcomes|changes)\b",
    re.IGNORECASE,
)
SAFE_EVIDENCE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
BUNDLE_REF_RE = re.compile(r"^b-[0-9]{4}$")
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
PROVIDER_ACTIVITY_FIELDS = {
    "lifecycle", "workstream", "action", "object", "outcome",
    "evidence_partitions", "evidence_spans", "project_recommendation",
    "effort", "semantic_confidence", "timing_confidence",
    "split_rationale", "merge_rationale", "omit_rationale",
}
PROVIDER_EXCEPTION_FIELDS = {"kind", "evidence_partitions", "reason"}
PROVIDER_OMISSION_FIELDS = {"lifecycle", "evidence_partitions", "reason"}
PROVIDER_SYNTHESIS_ACTIVITY_FIELDS = (
    PROVIDER_ACTIVITY_FIELDS - {"evidence_partitions"}
) | {"evidence_ids"}
PROVIDER_SYNTHESIS_EXCEPTION_FIELDS = {
    "kind", "evidence_ids", "reason",
}
PROVIDER_SYNTHESIS_OMISSION_FIELDS = {
    "lifecycle", "evidence_ids", "reason",
}


class AnalyzerError(RuntimeError):
    """Fail-closed analyzer or contract error."""


class AnalyzerTimeoutError(AnalyzerError):
    """A bounded analyzer transport timed out without a usable response."""


class AnalyzerTransportError(AnalyzerError):
    """A request-specific transport failed without a usable response."""


class AnalyzerRetryableHTTPError(AnalyzerTransportError):
    """A transient HTTP status is eligible for bounded transport recovery."""

    def __init__(self, message: str, *, status_code: int):
        super().__init__(message)
        self.status_code = status_code


class _ValidatedAnalysis(dict[str, Any]):
    """Schema-shaped analysis plus ephemeral fallback-only timing evidence."""

    def __init__(self, value: Mapping[str, Any], *, low_timing_evidence_ids: set[str]):
        super().__init__(value)
        self.low_timing_evidence_ids = frozenset(low_timing_evidence_ids)


class AnalyzerContractError(AnalyzerError):
    """A sealed provider response rejected by the semantic output contract."""


class AnalyzerCancelledError(AnalyzerError):
    """A concurrent extraction result was superseded by a fatal peer failure."""


_CONTRACT_FAILURE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("invalid_json", "invalid json"),
    ("missing_json_content", "lacks json message content"),
    ("omitted_evidence", "semantic result omitted known evidence ids"),
    ("duplicate_evidence", "semantic result reassigned evidence ids"),
    ("missing_atomicity_rationale", "explicit atomicity rationale"),
    ("compound_action", "one atomic verb phrase"),
    ("compound_field", "multiple accomplishment clauses"),
    ("unsupported_human_work", "human accomplishment support"),
    ("description_contract", "caveman render contract"),
    ("missing_evidence_span", "nonempty evidence_spans"),
    ("invalid_evidence_span", "valid start and end timestamps"),
    ("unsupported_evidence_span", "not supported by cited evidence"),
    ("invalid_output_lists", "activities, exceptions, and omissions must be lists"),
    ("invalid_evidence_ids", "evidence_ids"),
    ("invalid_evidence_ids", "evidence ids"),
    ("invalid_evidence_ids", "unknown evidence reference"),
    ("invalid_evidence_ids", "requires known evidence ids"),
    ("invalid_evidence_ids", "evidence_partitions"),
    ("invalid_evidence_ids", "evidence partition"),
    ("missing_activity_fields", "requires action, object, and outcome"),
    ("missing_workstream", "requires a workstream"),
    ("missing_nonactivity_fields", "requires a nonempty reason"),
    ("missing_nonactivity_fields", "omission lifecycle must be planned or noise"),
    ("invalid_effort", "effort"),
    ("invalid_effort", "must be positive"),
    ("invalid_project", "project_recommendation"),
    ("omitted_evidence", "omitted known evidence ids"),
    ("duplicate_evidence", "reassigned evidence ids"),
    ("identity_collision", "activity identity collision"),
    ("invalid_lifecycle", "invalid lifecycle"),
    ("invalid_confidence", "confidence"),
    ("synthesis_nonactivity", "synthesis must preserve cited activities"),
    ("synthesis_split", "synthesis must not split provisional activities"),
)
CONTRACT_FAILURE_CODES = {
    "contract_rejected",
    "contract_rejected_other",
    *(f"contract_rejected_{code}" for code, _needle in _CONTRACT_FAILURE_PATTERNS),
}
CACHE_REJECTION_CODES = CONTRACT_FAILURE_CODES | {
    "transport_error",
    "transport_timeout",
}
SYNTHESIS_INTEGRITY_FAILURE_CODES = {
    "contract_rejected_invalid_evidence_ids",
    "contract_rejected_omitted_evidence",
    "contract_rejected_duplicate_evidence",
}


def _contract_failure_code(error: BaseException) -> str:
    """Return a privacy-safe operational reason for a rejected model response."""
    message = str(error).casefold()
    cached_code = re.search(r"\b(contract_rejected(?:_[a-z_]+)?)\b", message)
    if cached_code and cached_code.group(1) in CONTRACT_FAILURE_CODES:
        return cached_code.group(1)
    for code, needle in _CONTRACT_FAILURE_PATTERNS:
        if needle in message:
            return f"contract_rejected_{code}"
    return "contract_rejected_other"


def _repair_instruction(failure_code: str) -> str:
    """Return narrow, privacy-safe corrective guidance for one sealed rejection."""
    if failure_code not in CONTRACT_FAILURE_CODES:
        raise AnalyzerError("repair feedback code is invalid")
    category = failure_code.removeprefix("contract_rejected_")
    instructions = {
        "compound_action": "Return one atomic past-tense action per activity; split unrelated actions.",
        "compound_field": "Keep each action, object, and outcome to one accomplishment clause.",
        "unsupported_human_work": (
            "Do not turn a user request or assistant status into completed human work. "
            "A non-meeting accomplishment must cite both the human instruction and its "
            "result from the same conversation; otherwise emit an omission or exception."
        ),
        "description_contract": (
            "Rewrite action, object, and outcome so their exact neutral render "
            "'SC — action object outcome' is 5-14 words, targets 8-14 words, "
            "and contains no slash, underscore, Markdown, path, URL, hash, "
            "first-person wording, status prose, or truncation."
        ),
        "omitted_evidence": "Account for every supplied bundle member exactly once with ranges.",
        "duplicate_evidence": "Keep member ranges disjoint across the full result.",
        "missing_evidence_span": "Give every reviewable activity a supported nonempty evidence span.",
        "invalid_evidence_span": "Use only valid evidence spans supported by cited refs.",
        "unsupported_evidence_span": "Keep each claimed span within its cited evidence interval.",
        "missing_atomicity_rationale": "Give every reviewable activity a nonempty atomicity rationale.",
        "invalid_evidence_ids": "Use only supplied bundle refs and valid inclusive member ranges.",
        "missing_activity_fields": "Include lifecycle, action, object, outcome, evidence, spans, and effort.",
        "missing_workstream": "Give each reviewable activity a short stable workstream name.",
        "missing_nonactivity_fields": (
            "Give every exception a nonempty kind and reason, and every omission "
            "a planned or noise lifecycle plus a nonempty reason."
        ),
        "invalid_effort": "Use positive ordered effort minutes for every activity.",
        "invalid_project": "Use an object for project recommendation, with blank fields when unsupported.",
        "invalid_output_lists": "Return activities, exceptions, and omissions as JSON lists.",
        "synthesis_nonactivity": "Synthesis must return only activities and preserve all cited refs.",
        "synthesis_split": "Synthesis may merge provisional activities but must not split them.",
    }
    return instructions.get(category, "Return only contract-valid JSON that preserves all supplied evidence exactly once.")


def _repair_system_addendum(failure_code: str) -> str:
    """Make category-only repair guidance prominent without leaking rejected prose."""
    if failure_code not in CONTRACT_FAILURE_CODES:
        raise AnalyzerError("repair feedback code is invalid")
    return (
        "\n\nCORRECTIVE RETRY: The previous response was rejected under the "
        f"allowlisted category {failure_code}. {_repair_instruction(failure_code)} "
        "Return a complete replacement result. Do not quote, summarize, or refer to "
        "the rejected response."
    )


def _raise_if_cancelled(cancelled: Callable[[], bool] | None) -> None:
    if cancelled is not None and cancelled():
        raise AnalyzerCancelledError("semantic extraction cancelled after fatal chunk failure")


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


def _semantic_context_key(event: Mapping[str, Any]) -> tuple[str, ...]:
    """Return a local-only stable boundary for keeping related evidence contiguous."""
    source_type = str(event.get("source_type") or "unknown")
    source_ref = event.get("source_ref") if isinstance(event.get("source_ref"), Mapping) else {}
    attributes = event.get("attributes") if isinstance(event.get("attributes"), Mapping) else {}
    machine = str(source_ref.get("machine") or "")
    if session_id := str(source_ref.get("session_id") or ""):
        return ("session", source_type, machine, session_id)
    if source_type == "repository_events":
        return (
            "repository",
            str(attributes.get("repository_root") or source_ref.get("source_id") or "unknown"),
        )
    if source_type == "multica":
        return (
            "multica",
            str(attributes.get("project_id") or "unknown"),
            str(attributes.get("key") or source_ref.get("source_id") or "unknown"),
        )
    if source_type == "fathom":
        return ("meeting", str(source_ref.get("source_id") or "unknown"))
    return ("source", source_type, machine, str(source_ref.get("source_id") or "unknown"))


def _bundle_context_key(day: str, context: tuple[str, ...]) -> tuple[str, ...]:
    """Keep identified contexts whole across midnight; bound unstable sources by day."""
    if context and context[0] in {"session", "repository", "multica", "meeting"}:
        return context
    return ("day", day, *context)


def _contextual_events(
    events: Iterable[dict[str, Any]],
) -> list[tuple[str, tuple[str, ...], dict[str, Any], dict[str, Any]]]:
    """Return deterministic local context plus the sole outbound projection."""
    contextual: list[tuple[str, tuple[str, ...], dict[str, Any], dict[str, Any]]] = []
    for value in events:
        raw = dict(value)
        projected = project_event(raw)
        if "time_span" in projected and "time_span" not in raw:
            raw["time_span"] = dict(projected["time_span"])
        contextual.append(
            (_event_day(raw), _semantic_context_key(raw), raw, projected)
        )
    return sorted(
        contextual,
        key=lambda item: (item[0], _event_sort_key(item[3]), item[1]),
    )


def _semantic_evidence_bundles(
    events: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build reversible local manifests and privacy-safe model bundles.

    Bundle identities are content-addressed locally.  Providers see only a
    request-local bundle reference and numeric member ordinals; original
    evidence, session, repository, issue, meeting, and machine identifiers stay
    in the local manifest.
    """
    contextual = _contextual_events(events)
    # Context-local order is deliberate.  Another machine or session may be
    # interleaved on the wall clock, but it must not fragment the source
    # conversation needed for semantic reconstruction.  Member ranges are
    # ordinal citation partitions, never claims of continuous elapsed time;
    # every member retains its own timestamp span for later allocation.
    grouped: dict[tuple[str, ...], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for day, context, raw, projected in contextual:
        grouped.setdefault(_bundle_context_key(day, context), []).append((raw, projected))
    ordered_groups = sorted(
        grouped.items(),
        key=lambda item: (_event_sort_key(item[1][0][1]), item[0]),
    )
    payload_bundles: list[dict[str, Any]] = []
    manifest: list[dict[str, Any]] = []
    for ordinal, (context, members) in enumerate(ordered_groups, 1):
        if ordinal > 9_999:
            raise AnalyzerError("semantic request contains too many evidence bundles")
        bundle_ref = f"b-{ordinal:04d}"
        original_ids = [str(raw.get("evidence_id") or "") for raw, _ in members]
        if (
            len(original_ids) != len(set(original_ids))
            or any(not SAFE_EVIDENCE_ID_RE.fullmatch(value) for value in original_ids)
        ):
            raise AnalyzerError("semantic evidence bundle contains invalid evidence IDs")
        member_payloads = [
            {
                "member": index,
                **{
                    key: value
                    for key, value in projected.items()
                    if key != "evidence_id"
                },
            }
            for index, (_raw, projected) in enumerate(members, 1)
        ]
        context_digest = stable_digest(
            "sec-", {"context": list(context)}, length=64
        )
        projected_digest = stable_digest(
            "sep-", member_payloads, length=64
        )
        bundle_id = stable_digest(
            "seb-",
            {
                "schema_version": EVIDENCE_BUNDLE_SCHEMA_VERSION,
                "context_digest": context_digest,
                "projected_digest": projected_digest,
                "evidence_ids": original_ids,
            },
            length=64,
        )
        payload_bundles.append(
            {
                "bundle_ref": bundle_ref,
                "context_type": context[2] if context[0] == "day" else context[0],
                "member_count": len(member_payloads),
                "members": member_payloads,
            }
        )
        manifest.append(
            {
                "bundle_id": bundle_id,
                "bundle_ref": bundle_ref,
                "context_digest": context_digest,
                "projected_digest": projected_digest,
                "member_count": len(member_payloads),
                "evidence_ids": original_ids,
            }
        )
    return payload_bundles, manifest


def _provider_response_cache_safe(
    response: Mapping[str, Any], *, original_evidence_ids: Iterable[str]
) -> None:
    """Reject accepted cache material containing local IDs or private residue."""
    serialized = canonical_json(response)
    if any(str(value) in serialized for value in original_evidence_ids):
        raise AnalyzerError("provider response exposed an original evidence ID")

    def inspect(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                inspect(key)
                inspect(item)
            return
        if isinstance(value, list):
            for item in value:
                inspect(item)
            return
        if isinstance(value, str) and _safe_text(value) != _one_line(value):
            raise AnalyzerError("provider response contains unsafe cache text")

    inspect(response)


def _restore_extraction_partitions(
    response: Mapping[str, Any], *, events: Iterable[dict[str, Any]]
) -> dict[str, Any]:
    """Expand model bundle/member ranges to immutable original evidence IDs."""
    response = _normalize_provider_response(response, mode="extract")
    _bundles, manifest = _semantic_evidence_bundles(events)
    by_ref = {str(item["bundle_ref"]): item for item in manifest}
    restored = copy.deepcopy(dict(response))
    for classification in ("activities", "exceptions", "omissions"):
        records = restored.get(classification)
        if not isinstance(records, list):
            raise AnalyzerError("activities, exceptions, and omissions must be lists")
        for record in records:
            partitions = record.pop("evidence_partitions", None)
            if not isinstance(partitions, list) or not partitions:
                raise AnalyzerError(
                    f"{classification} evidence_partitions must be a nonempty list"
                )
            expanded: list[str] = []
            seen_bundle_refs: set[str] = set()
            for partition in partitions:
                if not isinstance(partition, dict) or set(partition) != {
                    "bundle_ref", "member_ranges"
                }:
                    raise AnalyzerError("evidence partition has unsupported fields")
                bundle_ref = str(partition.get("bundle_ref") or "")
                if (
                    not BUNDLE_REF_RE.fullmatch(bundle_ref)
                    or bundle_ref not in by_ref
                    or bundle_ref in seen_bundle_refs
                ):
                    raise AnalyzerError("evidence partition has an unknown or repeated bundle ref")
                seen_bundle_refs.add(bundle_ref)
                ranges = partition.get("member_ranges")
                if not isinstance(ranges, list) or not ranges:
                    raise AnalyzerError("evidence partition member_ranges must be nonempty")
                member_ids = list(by_ref[bundle_ref]["evidence_ids"])
                covered: set[int] = set()
                for bounds in ranges:
                    if (
                        not isinstance(bounds, list)
                        or len(bounds) != 2
                        or any(isinstance(value, bool) or not isinstance(value, int) for value in bounds)
                    ):
                        raise AnalyzerError("evidence partition range must contain two integers")
                    start, end = bounds
                    if start < 1 or end < start or end > len(member_ids):
                        raise AnalyzerError("evidence partition range is reversed or out of bounds")
                    positions = set(range(start, end + 1))
                    if covered.intersection(positions):
                        raise AnalyzerError("evidence partition ranges overlap")
                    covered.update(positions)
                expanded.extend(member_ids[index - 1] for index in sorted(covered))
            if len(expanded) != len(set(expanded)):
                raise AnalyzerError("provider record repeats expanded evidence members")
            record["evidence_ids"] = expanded
    return restored


def _normalize_provider_response(
    response: Mapping[str, Any], *, mode: str
) -> dict[str, Any]:
    """Discard provider decoration without weakening the semantic contract.

    Cloud routes may nondeterministically add harmless metadata even when asked
    for structured JSON.  Only the three required classification collections
    and their allowlisted record fields can affect accounting.  Citation
    structures and all required semantic fields are deliberately left to the
    existing strict validators, so missing or malformed decisions still fail
    closed.
    """
    if not isinstance(response, Mapping):
        raise AnalyzerError("analyzer JSON must be an object")
    if mode == "extract":
        field_contracts = {
            "activities": PROVIDER_ACTIVITY_FIELDS,
            "exceptions": PROVIDER_EXCEPTION_FIELDS,
            "omissions": PROVIDER_OMISSION_FIELDS,
        }
    elif mode == "synthesize":
        field_contracts = {
            "activities": PROVIDER_SYNTHESIS_ACTIVITY_FIELDS,
            "exceptions": PROVIDER_SYNTHESIS_EXCEPTION_FIELDS,
            "omissions": PROVIDER_SYNTHESIS_OMISSION_FIELDS,
        }
    else:
        raise AnalyzerError("provider response normalization mode is invalid")

    normalized: dict[str, Any] = {}
    for classification, allowed_fields in field_contracts.items():
        records = response.get(classification)
        if not isinstance(records, list):
            raise AnalyzerError("activities, exceptions, and omissions must be lists")
        normalized_records: list[dict[str, Any]] = []
        for record in records:
            if not isinstance(record, Mapping):
                raise AnalyzerError(f"{classification} provider record must be an object")
            normalized_records.append(
                {
                    key: copy.deepcopy(value)
                    for key, value in record.items()
                    if key in allowed_fields
                }
            )
        normalized[classification] = normalized_records
    return normalized


def _request_messages(
    events: list[dict[str, Any]],
    *,
    mode: str,
    corrections: list[dict[str, Any]] | None = None,
    private_text_approved: bool | None = None,
    repair_failure_code: str | None = None,
    repair_attempt: int | None = None,
    timeout_recovery_attempt: int | None = None,
    connection_recovery_attempt: int | None = None,
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
One user message, one assistant response, or one continuous work session does not make
multiple deliverables atomic. Split independent objects, artifacts, and outcomes even
when the user requested them together. Never compress result lists into comma-separated
object or outcome clauses. The split_rationale must describe the single accomplishment;
it must not justify merging by saying multiple requests or deliverables happened together.
Every reviewable activity needs a specific split_rationale explaining why it is one
atomic accomplishment. Never join verbs with "and", "or", "then", or "also".
Every activities[] item MUST have a non-empty split_rationale, including an already
atomic item or an item created by merging duplicate evidence. Never leave it blank.
The action must start with a capitalized past-tense verb phrase of one to three words. Put the specific
object and bounded result in object and outcome, not in a long action.
Preserve the evidence's concrete nouns and quantities in object and outcome. Never
replace a specific result such as removing duplicate buffers with a generic claim such
as reducing usage. Preserve domain qualifiers that distinguish the work object: if the
evidence says "review identity", the object or outcome must retain both "review" and
"identity"; putting the qualifier only in workstream does not preserve it for the
Clockify description. Do not repeat the object's final word as the outcome's first word.
Give related atomic accomplishments the same short parent workstream name even when
their specific objects differ; unrelated work must not share a workstream.
Classify planned work, waiting, polling, agent chatter, heartbeats, and autonomous
background execution as planned or noise, not completed human work. A blocker is
loggable only when the evidence proves substantive diagnosis or remediation.
User-role evidence is intent, not proof that requested work happened. Assistant-role
evidence alone is status or autonomous output, not proof of human-attention work.
Except for meetings, a completed, advanced, investigated, or blocked accomplishment
must cite both a user instruction and an assistant result from the same conversation.
Repository commits are corroboration only. Never treat a commit, commit subject, or
changed artifact as standalone proof of human-attention work. Cite repository evidence
with the paired interactive-session accomplishment it supports, or classify it as noise
when no such supported accomplishment is present.
Do not invent projects, outcomes, evidence, effort, or meeting purpose.
MEETING SUFFICIENCY IS A HARD GATE. A Fathom bundle containing only a title and
timestamps, with no summary, action items, transcript, or other outcome-bearing
content, MUST produce exactly one insufficient_evidence exception covering all
of that bundle's members. It MUST NOT produce an activity. Words such as discovery,
review, planning, or delivery in the title do not prove meeting purpose or outcome.
Effort is human attention, not process runtime or empty wall-clock time. Evidence arrives as
semantic bundles. Each bundle has a short bundle_ref and ordered numeric members.
Apply confidence consistently. Semantic confidence is high when a direct user
instruction and matching assistant result prove the same outcome, medium when the
outcome is supported indirectly, and low only for conflict or insufficiency. Timing
confidence is high for a fixed recorded meeting, medium for timestamped paired
conversation evidence, and low only when spans are missing, coarse, or conflicting.
Do not mark timing low merely because human-effort minutes remain an estimate.
Cite evidence only with evidence_partitions shaped as
{"bundle_ref":"b-0001","member_ranges":[[1,4],[7,7]]}. Ranges are inclusive.
Use one range for a whole contiguous accomplishment instead of copying every member.
You may split one bundle across multiple atomic activities, but member ranges must
never overlap. Account for every member of every input bundle exactly once across
activities, exceptions, and omissions. Exceptions and omissions also require
evidence_partitions. Do not return original evidence IDs. Do not allocate start/end
Clockify blocks.
Write action + object + outcome so the exact neutral render
"SC — {action} {object} {outcome}" has 5-14 total words and targets 8-14.
Use terse past-tense Caveman wording. Do not use slashes, underscores, Markdown,
IDs, paths, URLs, hashes, first-person wording, status prose, or truncation.
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
    "evidence_partitions": [{"bundle_ref":"b-0001","member_ranges":[[1,4]]}],
    "evidence_spans": [{"start":"ISO-like", "end":"ISO-like"}],
    "project_recommendation": {"name":"", "prefix":"", "tag_names":[]},
    "effort": {"minimum_minutes":1, "recommended_minutes":1, "maximum_minutes":1},
    "semantic_confidence": "low|medium|high",
    "timing_confidence": "low|medium|high",
    "split_rationale": "why this is exactly one atomic accomplishment",
    "merge_rationale": "",
    "omit_rationale": ""
  }],
  "exceptions": [{"kind":"insufficient_evidence|conflicting_evidence", "evidence_partitions":[{"bundle_ref":"b-0002","member_ranges":[[1,2]]}], "reason":""}],
  "omissions": [{"lifecycle":"planned|noise", "evidence_partitions":[{"bundle_ref":"b-0003","member_ranges":[[1,5]]}], "reason":""}]
}
"""
    if repair_failure_code is not None:
        if (
            not isinstance(repair_attempt, int)
            or isinstance(repair_attempt, bool)
            or not 1 <= repair_attempt <= MAX_CONTRACT_REPAIR_ATTEMPTS
        ):
            raise AnalyzerError("repair attempt is invalid")
        system += _repair_system_addendum(repair_failure_code)
    if timeout_recovery_attempt is not None:
        if (
            repair_failure_code is not None
            or not isinstance(timeout_recovery_attempt, int)
            or isinstance(timeout_recovery_attempt, bool)
            or not 1 <= timeout_recovery_attempt <= MAX_TIMEOUT_RECOVERY_ATTEMPTS
        ):
            raise AnalyzerError("timeout recovery attempt is invalid")
        system += (
            "\n\nTIMEOUT RECOVERY: The prior request returned no response. Produce the "
            "complete JSON result concisely in this single bounded attempt. Do not "
            "quote or mention the prior request."
        )
    if connection_recovery_attempt is not None:
        if (
            repair_failure_code is not None
            or timeout_recovery_attempt is not None
            or not isinstance(connection_recovery_attempt, int)
            or isinstance(connection_recovery_attempt, bool)
            or not 1
            <= connection_recovery_attempt
            <= MAX_CONNECTION_RECOVERY_ATTEMPTS
        ):
            raise AnalyzerError("connection recovery attempt is invalid")
        system += (
            "\n\nCONNECTION RECOVERY: The prior transport returned no usable response. "
            "Produce the complete JSON result concisely in this single bounded "
            "attempt. Do not quote or mention the prior request."
        )
    model_bundles, _manifest = _semantic_evidence_bundles(events)
    payload = {
        "mode": mode,
        "schema_version": SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "evidence_bundle_schema_version": EVIDENCE_BUNDLE_SCHEMA_VERSION,
        "bundles": model_bundles,
        "review_corrections": _project_corrections(corrections),
    }
    if repair_failure_code is not None:
        payload["repair_feedback"] = {
            "failure_code": repair_failure_code,
            "instruction": _repair_instruction(repair_failure_code),
            "attempt": repair_attempt,
            "maximum_attempts": MAX_CONTRACT_REPAIR_ATTEMPTS,
        }
    if timeout_recovery_attempt is not None:
        payload["timeout_recovery"] = {
            "attempt": timeout_recovery_attempt,
            "maximum_attempts": MAX_TIMEOUT_RECOVERY_ATTEMPTS,
        }
    if connection_recovery_attempt is not None:
        payload["connection_recovery"] = {
            "attempt": connection_recovery_attempt,
            "maximum_attempts": MAX_CONNECTION_RECOVERY_ATTEMPTS,
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
    repair_failure_code: str | None = None,
    repair_attempt: int | None = None,
    timeout_recovery_attempt: int | None = None,
    connection_recovery_attempt: int | None = None,
) -> dict[str, Any]:
    return {
        "model": model,
        "temperature": 0,
        "seed": (
            2_000 + connection_recovery_attempt
            if connection_recovery_attempt is not None
            else (
                1_000 + timeout_recovery_attempt
                if timeout_recovery_attempt is not None
                else repair_attempt or 0
            )
        ),
        "response_format": {"type": "json_object"},
        "messages": _request_messages(
            events,
            mode=mode,
            corrections=corrections,
            private_text_approved=private_text_approved,
            repair_failure_code=repair_failure_code,
            repair_attempt=repair_attempt,
            timeout_recovery_attempt=timeout_recovery_attempt,
            connection_recovery_attempt=connection_recovery_attempt,
        ),
    }


def _semantic_review_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Project semantic hints without local identifiers or trusted citations.

    The independent reviewer reconstructs evidence partitions from the supplied
    bundles.  This projection works for both raw extraction responses and
    restored post-synthesis analyses, so synthesis can never become the final
    unreviewed semantic authority.
    """
    projected: dict[str, list[dict[str, Any]]] = {
        "activities": [],
        "exceptions": [],
        "omissions": [],
    }
    for raw in candidate.get("activities", []):
        if not isinstance(raw, Mapping):
            continue
        project = raw.get("project_recommendation")
        effort = raw.get("effort")
        projected["activities"].append({
            "lifecycle": _safe_text(raw.get("lifecycle")),
            "action": _safe_text(raw.get("action")),
            "object": _safe_text(raw.get("object")),
            "outcome": _safe_text(raw.get("outcome")),
            "workstream": _safe_text(raw.get("workstream")),
            "project_recommendation": (
                {
                    "name": _safe_text(project.get("name")),
                    "prefix": _safe_text(project.get("prefix")),
                    "tag_names": sorted(
                        _safe_text(value)
                        for value in project.get("tag_names", [])
                        if _safe_text(value)
                    ),
                }
                if isinstance(project, Mapping)
                else {}
            ),
            "effort": (
                {
                    "minimum_minutes": effort.get("minimum_minutes"),
                    "recommended_minutes": effort.get("recommended_minutes"),
                    "maximum_minutes": effort.get("maximum_minutes"),
                }
                if isinstance(effort, Mapping)
                else {}
            ),
            "semantic_confidence": _safe_text(raw.get("semantic_confidence")),
            "timing_confidence": _safe_text(raw.get("timing_confidence")),
            "split_rationale": _safe_text(raw.get("split_rationale")),
            "merge_rationale": _safe_text(raw.get("merge_rationale")),
            "omit_rationale": _safe_text(raw.get("omit_rationale")),
        })
    for raw in candidate.get("exceptions", []):
        if isinstance(raw, Mapping):
            projected["exceptions"].append({
                "kind": _safe_text(raw.get("kind")),
                "reason": _safe_text(raw.get("reason")),
            })
    for raw in candidate.get("omissions", []):
        if isinstance(raw, Mapping):
            projected["omissions"].append({
                "lifecycle": _safe_text(raw.get("lifecycle")),
                "reason": _safe_text(raw.get("reason")),
            })
    return projected

def _review_messages(
    events: list[dict[str, Any]],
    *,
    candidate: Mapping[str, Any],
    taxonomy: list[dict[str, Any]],
    repair_failure_code: str | None = None,
    transport_recovery_attempt: int | None = None,
    transport_failure_code: str | None = None,
    review_scope: str = "extraction",
    review_prompt_version: str = REVIEW_PROMPT_VERSION,
) -> list[dict[str, str]]:
    """Build an independent semantic-review request for one extraction."""
    system = f"""You are the independent Clockify accounting reviewer.
Use the evidence bundles as authority. The first-pass candidate is untrusted and
may be accepted, corrected, split, merged, omitted, or replaced by an exception.

Review semantically, not with brittle grammar rules. For every proposed activity:
- verify that the cited evidence proves human-attention work and the stated outcome;
- choose the correct client/project level and task type only from clockify_taxonomy;
- judge whether effort is plausible human attention rather than process runtime;
- produce one concise human-readable task/outcome description through action,
  object, and outcome. The neutral render `Prefix — action object outcome` must be
  a single line and normally 8-14 words. Use plain Caveman wording. Never include
  NEEDS REVIEW markers, Markdown, URLs, domains, filesystem paths, commit hashes,
  raw commands, prompt text, agent chatter, or copied session-message prose. Split
  distinct accomplishments instead of cramming them into one long description;
- keep one independently meaningful accomplishment per activity;
- use meetings as fixed blocks and require outcome-bearing meeting evidence;
- treat a human instruction plus its resulting assistant/tool evidence in the same
  interactive session as supported human-directed work. Do not omit an accomplishment
  merely because an assistant or tool delivered the result;
- use repository commits only to corroborate a supported interactive-session
  accomplishment. A commit, subject, or changed artifact alone never proves
  human-attention work and must not become a standalone activity;
- classify plans, waiting, polling, unsolicited status chatter, and genuinely
  autonomous background execution without a paired human instruction as planned/noise.

Return the extraction response shape with exactly these top-level lists:
activities, exceptions, omissions. The candidate is only a semantic hint and its
citations and local identifiers are intentionally absent. Reconstruct citations
solely from the evidence bundles. Preserve provider field names and shape, but do
not preserve candidate values that evidence or taxonomy shows are wrong.
Use evidence_partitions with only supplied bundle_ref/member_ranges. Account for
every supplied bundle member exactly once across the three lists. Never invent
evidence, clients, projects, tags, outcomes, or effort. Project name, prefix, and
tag_names must exactly match one clockify_taxonomy row, or all must be blank when
the evidence is insufficient. A member route_hint is a privacy-safe local routing
signal derived from source location or calendar metadata. Validate it against the
semantic evidence; keep it when consistent, override it only with a better exact
taxonomy choice, and split or raise an exception when one activity mixes conflicting
client routes. Taxonomy selection_guidance is context, not an output field. Every
exception requires a nonempty kind and reason.
Every omission requires lifecycle planned or noise and a nonempty reason. Prefer a
corrected evidence-supported activity over omission when the candidate merely chose
an invalid project or task type. This review uses {review_prompt_version}."""
    if review_scope == "portfolio":
        system += """

PORTFOLIO CONSOLIDATION: This is a second-pass review of already reviewed
micro-activities from one bounded project/day portfolio. Reconstruct the
invoice-worthy accomplishments proved by the complete evidence, rather than
preserving one row per message, command, check, test, fix, or status update.
Merge implementation, debugging, review, deployment, verification, and follow-up
substeps when they jointly advanced the same bounded deliverable, incident,
decision, or client outcome—even when the candidate used slightly different
workstream or object wording. Keep genuinely independent deliverables separate.
Do not merge unrelated work merely because it shared a day or project.

Default to the smallest useful set of invoice rows. A candidate deserves its own
row only when its outcome would make sense on an invoice without the neighboring
candidate. Merge setup→execution, search→recovery or transfer, diagnosis→fix,
implementation→test, review→decision, deployment→verification, and retry→success
chains into the final delivered outcome. The enabling step is not an independent
accomplishment merely because it used a different command, artifact, or verb.

An activity under ten minutes should survive only when the evidence proves a
genuinely isolated short deliverable or fixed meeting. Normally combine supported
microsteps into a useful 20–90 minute accomplishment, and allow a larger bounded
entry when one sustained workstream genuinely requires it. Estimate total human
attention across the merged evidence; never use process runtime and never invent
time. Prefer a small number of clear invoice-review rows over a transcript-like
activity inventory. Each returned activity needs exactly one independently
meaningful outcome and a complete Caveman description of 8–14 total words after
adding the supplied prefix. Never exceed 14 total words; keep quantities only when
they materially explain the delivered result.
"""
    elif review_scope == "portfolio_validation":
        system += """

FINAL PORTFOLIO VALIDATION: Independently compare the candidate with the complete
evidence and accounting contract. Validate the client/project level, task type,
human-attention effort, consolidation boundary, and human-readable task/outcome
description. Correct plausible candidate rows instead of rejecting them for a
wording defect. Preserve a merge when its evidence forms one setup→result or
diagnosis→fix deliverable; split only genuinely independent invoice outcomes.
Every accepted description must be complete Caveman wording of 8–14 total words
after adding the supplied prefix. Never exceed 14 total words. Remove secondary
details before sacrificing the core verb, object, or delivered result. Return the
fully corrected replacement and account for every evidence member exactly once.
"""
    elif review_scope == "portfolio_single_activity_recovery":
        system += """

FAILED-LEAF PORTFOLIO RECOVERY: This request contains one source candidate that
the earlier portfolio pass could not cite correctly. Independently validate its
client, task type, outcome, human-attention effort, and Caveman wording against
the complete evidence. Do not preserve it when the evidence shows it is wrong.

Prefer exactly one corrected activity when the evidence forms one bounded
diagnosis, fix, decision, or delivered outcome. Do not split commands, checks,
observations, implementation steps, and verification from that same outcome into
separate invoice rows. When every member of a supplied bundle supports that one
outcome, cite the bundle's entire allowed_member_range as one inclusive range.
Split only when the evidence proves genuinely independent invoice outcomes.

Before returning JSON, audit the citations against coverage_contract: every
integer member must occur exactly once across activities, exceptions, and
omissions; no range may overlap, repeat, reverse, or exceed its allowed bounds.
Return one fully corrected replacement with complete 8-14-word Caveman wording.
"""
    elif review_scope in {
        "portfolio_wording_recovery",
        "portfolio_wording_recovery_retry",
    }:
        system += """

FINAL WORDING RECOVERY: Preserve the candidate's evidence classification,
project route, task type, and bounded accomplishment unless the evidence proves
them wrong. Rewrite the retained activity's action, object, and outcome as plain
Caveman fields. The action, object, and outcome must never contain the project
prefix, an em dash separator, Markdown, IDs, paths, URLs, hashes, prompt text,
or copied status prose. Do not place `Prefix —` inside any field. Keep one verb
in the action and one bounded delivered result in the outcome. The exact render
`Prefix — action object outcome` must contain 8-14 total words. Preserve any
evidence members already classified as noise or exception, and account for every
bundle member exactly once.
"""
        if review_scope == "portfolio_wording_recovery_retry":
            system += """

WORDING RETRY: The prior Flash rewrite still failed the rendering contract.
Simplify the wording materially. Use ordinary words and spaces only: no slash,
backslash, colon, semicolon, hash, Markdown-like punctuation, version string,
domain, path-shaped token, abbreviation chain, or embedded prefix. Count the
complete `Prefix — action object outcome` render before returning it and keep it
between 8 and 14 words.
"""
    elif review_scope != "extraction":
        raise AnalyzerError("semantic review scope is invalid")
    if repair_failure_code is not None:
        system += (
            "\n\nSTRUCTURAL REPAIR: The prior review could not be consumed under "
            f"{repair_failure_code}. {_repair_instruction(repair_failure_code)} "
            "Use coverage_contract as the exact citation ledger. Return one complete "
            "schema-valid replacement. "
            "Do not discuss the prior response."
        )
    if transport_recovery_attempt is not None:
        if (
            transport_failure_code not in {"transport_timeout", "transport_error"}
            or isinstance(transport_recovery_attempt, bool)
            or not isinstance(transport_recovery_attempt, int)
            or not 1 <= transport_recovery_attempt <= MAX_TIMEOUT_RECOVERY_ATTEMPTS
        ):
            raise AnalyzerError("semantic review transport recovery is invalid")
        system += (
            "\n\nTRANSPORT RECOVERY: The prior independent review transport "
            "returned no usable response. Review the complete supplied evidence "
            "and candidate in this bounded attempt. Do not mention the prior call."
        )
    model_bundles, manifest = _semantic_evidence_bundles(events)
    events_by_id = {
        str(event.get("evidence_id")): event
        for event in events
        if isinstance(event, Mapping)
    }
    for bundle, local_manifest in zip(model_bundles, manifest, strict=True):
        evidence_ids = local_manifest.get("evidence_ids", [])
        members = bundle.get("members", [])
        if len(evidence_ids) != len(members):
            raise AnalyzerError("semantic route-hint member mapping is invalid")
        for member, evidence_id in zip(members, evidence_ids, strict=True):
            event = events_by_id.get(str(evidence_id), {})
            hint = event.get("semantic_route_hint") if isinstance(event, Mapping) else None
            if isinstance(hint, Mapping) and hint:
                member["route_hint"] = copy.deepcopy(dict(hint))
    payload = {
        "mode": "review",
        "schema_version": SCHEMA_VERSION,
        "extractor_prompt_version": PROMPT_VERSION,
        "review_prompt_version": review_prompt_version,
        "bundles": model_bundles,
        "coverage_contract": [
            {
                "bundle_ref": str(bundle["bundle_ref"]),
                "allowed_member_range": [1, int(bundle["member_count"])],
            }
            for bundle in model_bundles
        ],
        "candidate": _semantic_review_candidate(candidate),
        "clockify_taxonomy": copy.deepcopy(taxonomy),
    }
    if review_scope != "extraction":
        payload["review_scope"] = review_scope
    if repair_failure_code is not None:
        payload["repair_feedback"] = {
            "failure_code": repair_failure_code,
            "instruction": _repair_instruction(repair_failure_code),
            "coverage_rule": (
                "Every supplied member integer must appear exactly once across "
                "activities, exceptions, and omissions, and every range must stay "
                "inside its bundle's allowed_member_range."
            ),
        }
    if transport_recovery_attempt is not None:
        payload["review_transport_recovery"] = {
            "failure_code": transport_failure_code,
            "attempt": transport_recovery_attempt,
            "maximum_attempts": MAX_TIMEOUT_RECOVERY_ATTEMPTS,
        }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": canonical_json(payload)},
    ]


def _review_body(
    events: list[dict[str, Any]],
    *,
    candidate: Mapping[str, Any],
    taxonomy: list[dict[str, Any]],
    model: str,
    repair_failure_code: str | None = None,
    transport_recovery_attempt: int | None = None,
    transport_failure_code: str | None = None,
    review_scope: str = "extraction",
    review_prompt_version: str = REVIEW_PROMPT_VERSION,
) -> dict[str, Any]:
    return {
        "model": model,
        "temperature": 0,
        "seed": (
            4_000 + transport_recovery_attempt
            if transport_recovery_attempt is not None
            else 101 if repair_failure_code is None else 102
        ),
        "response_format": {"type": "json_object"},
        "messages": _review_messages(
            events,
            candidate=candidate,
            taxonomy=taxonomy,
            repair_failure_code=repair_failure_code,
            transport_recovery_attempt=transport_recovery_attempt,
            transport_failure_code=transport_failure_code,
            review_scope=review_scope,
            review_prompt_version=review_prompt_version,
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
    """Return a conservative embedded size for one projected bundle member."""
    evidence_id = str(event.get("evidence_id") or "")
    if not SAFE_EVIDENCE_ID_RE.fullmatch(evidence_id):
        raise AnalyzerError("cannot alias an unsafe evidence ID")
    model_member = {
        "member": 1,
        **{key: value for key, value in event.items() if key != "evidence_id"},
    }
    # Amortize the bundle envelope into each member.  Final transport sizing is
    # still enforced by _call_validated against the hard ceiling.
    return _escaped_json_content_bytes(canonical_json(model_member)) + 128


def _context_turn_units(
    context: tuple[str, ...], events: list[dict[str, Any]]
) -> list[list[dict[str, Any]]]:
    """Keep each user instruction with its following assistant/tool evidence."""
    if not context or context[0] != "session":
        return [[event] for event in events]
    roles = [str(project_event(event).get("role") or "source") for event in events]
    if "user" not in roles:
        return [[event] for event in events]
    units: list[list[dict[str, Any]]] = []
    prefix: list[dict[str, Any]] = []
    turn: list[dict[str, Any]] = []
    for event, role in zip(events, roles, strict=True):
        if role == "user":
            if turn:
                units.append(turn)
            turn = [*prefix, event]
            prefix = []
        elif turn:
            turn.append(event)
        else:
            prefix.append(event)
    if turn:
        units.append(turn)
    elif prefix:
        units.append(prefix)
    return units


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


def _synthesis_messages(
    activities: list[dict[str, Any]], *, workstream_id: str,
    repair_failure_code: str | None = None, repair_attempt: int | None = None,
    transport_recovery_attempt: int | None = None,
) -> list[dict[str, str]]:
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
Preserve every concrete domain qualifier from the provisional objects in the returned object or outcome;
putting a qualifier only in workstream does not preserve it for the Clockify description.
One request, response, or continuous session does not justify merging independent deliverables.
Never compress multiple objects or results into comma-separated fields.
Preserve the supplied parent workstream name for related atomic accomplishments.
Every input evidence ref must appear in exactly one returned activity; do not add, omit, or move evidence
to exceptions or omissions. Copy the short evidence refs and evidence spans exactly from supported input.
Every returned activity must have a non-empty split_rationale; merged evidence must also have a specific
non-empty merge_rationale.
Do not invent projects, outcomes, effort, or timing. Never include paths, URLs, emails, secrets, IDs,
or status prose in descriptive fields."""
    if repair_failure_code is not None:
        if (
            not isinstance(repair_attempt, int)
            or isinstance(repair_attempt, bool)
            or not 1 <= repair_attempt <= MAX_CONTRACT_REPAIR_ATTEMPTS
        ):
            raise AnalyzerError("repair attempt is invalid")
        system += _repair_system_addendum(repair_failure_code)
    if transport_recovery_attempt is not None:
        if (
            repair_failure_code is not None
            or not isinstance(transport_recovery_attempt, int)
            or isinstance(transport_recovery_attempt, bool)
            or not 1
            <= transport_recovery_attempt
            <= MAX_CONNECTION_RECOVERY_ATTEMPTS
        ):
            raise AnalyzerError("synthesis transport recovery attempt is invalid")
        system += (
            "\n\nTRANSPORT RECOVERY: The prior synthesis transport returned no "
            "usable response. Reconcile the complete supplied workstream in this "
            "single bounded attempt. Do not quote or mention the prior request."
        )
    payload = {
        "mode": "synthesize",
        "schema_version": SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "workstream_id": workstream_id,
        "provisional_activities": model_provisional,
    }
    if repair_failure_code is not None:
        payload["repair_feedback"] = {
            "failure_code": repair_failure_code,
            "instruction": _repair_instruction(repair_failure_code),
            "attempt": repair_attempt,
            "maximum_attempts": MAX_CONTRACT_REPAIR_ATTEMPTS,
        }
    if transport_recovery_attempt is not None:
        payload["transport_recovery"] = {
            "attempt": transport_recovery_attempt,
            "maximum_attempts": MAX_CONNECTION_RECOVERY_ATTEMPTS,
        }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": canonical_json(payload)},
    ]


def _synthesis_body(
    activities: list[dict[str, Any]], *, model: str, workstream_id: str,
    repair_failure_code: str | None = None, repair_attempt: int | None = None,
    transport_recovery_attempt: int | None = None,
) -> dict[str, Any]:
    return {
        "model": model,
        "temperature": 0,
        "seed": (
            3_000 + transport_recovery_attempt
            if transport_recovery_attempt is not None
            else repair_attempt or 0
        ),
        "response_format": {"type": "json_object"},
        "messages": _synthesis_messages(
            activities, workstream_id=workstream_id,
            repair_failure_code=repair_failure_code, repair_attempt=repair_attempt,
            transport_recovery_attempt=transport_recovery_attempt,
        ),
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
    # Retain local context keys only long enough to keep each session, repository,
    # issue, or meeting contiguous.  The keys themselves never enter a request.
    contextual = _contextual_events(events)
    ordered = [projected for _day, _context, _raw, projected in contextual]
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
    by_context: dict[
        tuple[str, ...], list[tuple[str, dict[str, Any], dict[str, Any]]]
    ] = {}
    for day, context, raw, projected in contextual:
        by_context.setdefault(_bundle_context_key(day, context), []).append(
            (day, raw, projected)
        )

    chunks: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_event_bytes = 0
    current_day: str | None = None
    bundles = sorted(
        by_context.items(),
        key=lambda item: (_event_sort_key(item[1][0][2]), item[0]),
    )
    for context, contextual_bundle in bundles:
        bundle_day = contextual_bundle[0][0]
        bundle = [(raw, projected) for _day, raw, projected in contextual_bundle]
        bundle_bytes = [_chunk_event_bytes(projected) for _raw, projected in bundle]
        bundle_count = len(bundle)
        combined_count = len(current) + bundle_count
        combined_size = (
            empty_body_bytes
            + current_event_bytes
            + sum(bundle_bytes)
            + max(0, combined_count - 1)
            + _alias_extra_bytes(combined_count)
        )
        bundle_size = (
            empty_body_bytes
            + sum(bundle_bytes)
            + max(0, bundle_count - 1)
            + _alias_extra_bytes(bundle_count)
        )
        if (
            current
            and bundle_count <= max_events_per_chunk
            and bundle_size <= target_body_bytes
            and (
                bundle_day != current_day
                or combined_count > max_events_per_chunk
                or combined_size > target_body_bytes
            )
        ):
            chunks.append(current)
            current = []
            current_event_bytes = 0
            current_day = None
        event_bytes_by_id = {
            str(raw.get("evidence_id")): event_bytes
            for (raw, _projected), event_bytes in zip(bundle, bundle_bytes, strict=True)
        }
        for unit in _context_turn_units(context, [raw for raw, _projected in bundle]):
            unit_event_bytes = sum(
                event_bytes_by_id[str(raw.get("evidence_id"))] for raw in unit
            )
            approximate_unit_size = (
                empty_body_bytes + unit_event_bytes + max(0, len(unit) - 1) + 1_024
            )
            unit_size = approximate_unit_size
            if approximate_unit_size > max_body_bytes:
                unit_size = len(canonical_json(_body_for(
                    unit,
                    model=model,
                    mode="extract",
                    corrections=corrections,
                    private_text_approved=private_text_approved,
                )).encode("utf-8"))
            if unit_size > max_body_bytes:
                raise AnalyzerError(
                    f"indivisible evidence turn {unit[0].get('evidence_id') or '<unknown>'} "
                    f"exceeds analyzer request ceiling ({unit_size} bytes)"
                )
            trial_count = len(current) + len(unit)
            trial_size = (
                empty_body_bytes
                + current_event_bytes
                + unit_event_bytes
                + max(0, trial_count - 1)
                + _alias_extra_bytes(trial_count)
            )
            if current and (
                trial_size > target_body_bytes
                or trial_count > max_events_per_chunk
            ):
                chunks.append(current)
                current = list(unit)
                current_event_bytes = unit_event_bytes
                current_day = _event_day(unit[0])
            else:
                current.extend(unit)
                current_event_bytes += unit_event_bytes
                if current_day is None:
                    current_day = bundle_day
    if current:
        chunks.append(current)
    for chunk in chunks:
        body_size = len(canonical_json(_body_for(
            chunk,
            model=model,
            mode="extract",
            corrections=corrections,
            private_text_approved=private_text_approved,
        )).encode("utf-8"))
        if body_size > max_body_bytes:
            raise AnalyzerError(
                f"semantic evidence chunk exceeds analyzer request ceiling ({body_size} bytes)"
            )
    return chunks


def _turn_aware_recovery_split(
    chunk: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int] | None:
    """Bisect at a context or user-turn boundary without orphaning results.

    A user message starts an indivisible conversation turn containing the
    assistant/tool evidence up to the next user message.  Session metadata that
    precedes the first user message travels with that first turn.  A single
    remaining turn is deliberately unsplittable and becomes a safe analyzer
    exception if both routes reject it.
    """
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for event in chunk:
        grouped.setdefault(_semantic_context_key(event), []).append(event)

    units: list[list[dict[str, Any]]] = []
    for context, events in grouped.items():
        units.extend(_context_turn_units(context, events))

    if len(units) < 2:
        return None
    total = sum(len(unit) for unit in units)
    boundary = min(
        range(1, len(units)),
        key=lambda index: (
            abs(sum(len(unit) for unit in units[:index]) * 2 - total),
            index,
        ),
    )
    left = [event for unit in units[:boundary] for event in unit]
    right = [event for unit in units[boundary:] for event in unit]
    if not left or not right:
        return None
    return left, right, len(left)


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


def _bounded_evidence_capacity_minutes(
    evidence_ids: Iterable[str],
    evidence_time_spans: Mapping[str, Mapping[str, str]] | None,
) -> int | None:
    """Return the union of fully observed cited intervals, rounded up to five minutes.

    A source interval is an upper bound on attention, not a claim that every
    observed minute was active work.  Point observations and partial source
    timing deliberately return ``None`` so they cannot manufacture an effort
    estimate.
    """
    if not evidence_time_spans:
        return None
    intervals: list[tuple[dt.datetime, dt.datetime]] = []
    for evidence_id in evidence_ids:
        span = evidence_time_spans.get(evidence_id)
        if not isinstance(span, Mapping):
            return None
        start = _timestamp_instant(span.get("start"))
        end = _timestamp_instant(span.get("end"))
        if (
            start is None
            or end is None
            or start >= end
            or (start.tzinfo is None) != (end.tzinfo is None)
        ):
            return None
        intervals.append((start, end))
    if not intervals:
        return None
    if len({value.tzinfo is not None for interval in intervals for value in interval}) != 1:
        return None
    intervals.sort()
    merged: list[tuple[dt.datetime, dt.datetime]] = []
    for start, end in intervals:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    seconds = sum((end - start).total_seconds() for start, end in merged)
    return max(5, ((int(seconds) + 299) // 300) * 5)


def _effort_from_bounded_capacity(capacity_minutes: int | None) -> tuple[int, int, int] | None:
    """Use a bounded source interval as a ceiling, never as full attention.

    The returned recommended effort is the largest five-minute estimate whose
    conservative band fits inside the observed capacity.  This reuses the
    existing 2/3--4/3 effort-band contract and keeps allocation demand below
    the observed interval instead of filling it wholesale.
    """
    if capacity_minutes is None:
        return None
    candidates = [
        recommended
        for recommended in range(5, capacity_minutes + 1, 5)
        if _effort_band(recommended)[1] <= capacity_minutes
    ]
    if not candidates:
        return None
    recommended = candidates[-1]
    minimum, maximum = _effort_band(recommended)
    return minimum, recommended, maximum


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
    if _ANTI_SPLIT_RATIONALE_RE.search(split_rationale):
        raise AnalyzerError(
            "activity split rationale contains multiple accomplishment clauses"
        )
    action_words = re.findall(r"[\w'-]+", action, flags=re.UNICODE)
    if not action_words or len(action_words) > 3 or _COMPOUND_ACTION_RE.search(action):
        raise AnalyzerError("activity action must express one atomic verb phrase")
    for name, value in (("action", action), ("object", obj), ("outcome", outcome)):
        if (
            _ATOMIC_FIELD_SEPARATOR_RE.search(value)
            or "," in value
            or _SECONDARY_ACTION_RE.search(value)
            or _PARALLEL_RESULT_RE.search(value)
            or re.search(r"[.!?]\s+[A-Z]", value)
        ):
            raise AnalyzerError(f"activity {name} contains multiple accomplishment clauses")
    if _OUTCOME_ACTION_START_RE.search(outcome):
        raise AnalyzerError("activity outcome contains a second accomplishment action")


def _evidence_support(events: Iterable[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Return local-only human-attention support for deterministic truth gates."""
    support: dict[str, dict[str, str]] = {}
    for event in events:
        projected = project_event(event)
        evidence_id = str(projected["evidence_id"])
        support[evidence_id] = {
            "role": str(projected.get("role") or "source"),
            "source_category": str(projected.get("source_category") or "other"),
            "context": canonical_json(_semantic_context_key(event)),
        }
    return support


def _validate_human_accomplishment(
    lifecycle: str,
    evidence_ids: Iterable[str],
    evidence_support: Mapping[str, Mapping[str, str]] | None,
) -> None:
    """Reject requests and autonomous status as completed human-attention work.

    Source snapshots and non-session corroboration remain neutral.  When explicit
    conversational roles are cited, however, a reviewable non-meeting claim must
    contain a user instruction and an assistant result from the same session.
    """
    if evidence_support is None or lifecycle in {"planned", "noise"}:
        return
    cited = [evidence_support.get(evidence_id, {}) for evidence_id in evidence_ids]
    if lifecycle == "meeting":
        if not any(item.get("source_category") == "meeting" for item in cited):
            raise AnalyzerError("meeting lacks human accomplishment support")
        return
    if lifecycle not in {"completed", "advanced", "investigated", "blocked"}:
        return
    explicit_session_roles = [
        item
        for item in cited
        if item.get("source_category") == "agent_session"
        and item.get("role") in {"user", "assistant"}
    ]
    if not explicit_session_roles:
        return
    roles_by_context: dict[str, set[str]] = {}
    for item in explicit_session_roles:
        roles_by_context.setdefault(str(item.get("context") or ""), set()).add(
            str(item.get("role") or "")
        )
    if not any({"user", "assistant"} <= roles for roles in roles_by_context.values()):
        raise AnalyzerError(
            "reviewable activity lacks paired human accomplishment support"
        )


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


def bind_activity_evidence_spans(
    result: Mapping[str, Any],
    *,
    evidence_time_spans: Mapping[str, Mapping[str, str]] | None,
) -> dict[str, Any]:
    """Replace model-copied spans with exact immutable-ledger spans.

    Models decide semantic membership, not timestamps.  Once cited evidence
    references have been restored locally, the ledger is the only authority for
    activity spans.  Missing or malformed ledger spans remain visible because
    ``validate_result`` still rejects a reviewable activity without a valid
    supported span.
    """
    bound = copy.deepcopy(dict(result))
    activities = bound.get("activities")
    if not isinstance(activities, list):
        return bound
    source = evidence_time_spans or {}
    for activity in activities:
        if not isinstance(activity, dict):
            continue
        lifecycle = str(activity.get("lifecycle") or "").strip().lower()
        if lifecycle in {"planned", "noise"}:
            activity["evidence_spans"] = []
            continue
        evidence_ids = activity.get("evidence_ids")
        if not isinstance(evidence_ids, list):
            continue
        spans = {
            (str(span.get("start") or ""), str(span.get("end") or ""))
            for evidence_id in evidence_ids
            if isinstance((span := source.get(str(evidence_id))), Mapping)
        }
        activity["evidence_spans"] = [
            {"start": start, "end": end}
            for start, end in sorted(spans)
            if start and end
        ]
    return bound


def validate_result(
    result: dict[str, Any],
    *,
    known_evidence_ids: set[str],
    provider_model: str,
    analyzer_tier: str,
    provider_revision: str = "",
    evidence_time_spans: dict[str, dict[str, str]] | None = None,
    evidence_support: Mapping[str, Mapping[str, str]] | None = None,
    semantic_validation: bool = True,
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
        if semantic_validation:
            _validate_human_accomplishment(
                lifecycle,
                evidence_ids,
                evidence_support,
            )
        action = (
            _normalize_action(raw.get("action"))
            if semantic_validation
            else _one_line(raw.get("action"))
        )
        obj = _one_line(raw.get("object"))
        outcome = _one_line(raw.get("outcome"))
        split_rationale = _one_line(raw.get("split_rationale"))
        workstream = _one_line(raw.get("workstream") or obj)
        if lifecycle not in {"planned", "noise"} and (not action or not obj or not outcome):
            raise AnalyzerError("reviewable activity requires action, object, and outcome")
        if lifecycle not in {"planned", "noise"} and not workstream:
            raise AnalyzerError("reviewable activity requires a workstream")
        if semantic_validation and lifecycle not in {"planned", "noise"}:
            _validate_atomic_parts(action, obj, outcome, split_rationale)
            try:
                caveman_renderer.render(
                    {
                        "prefix": "SC",
                        "action": action,
                        "object": obj,
                        "outcome": outcome,
                    }
                )
            except caveman_renderer.CavemanValidationError as exc:
                raise AnalyzerError(
                    f"activity violates Caveman render contract: {exc}"
                ) from exc
        effort = raw.get("effort")
        if not isinstance(effort, dict):
            raise AnalyzerError("activity effort must be an object")
        raw_minimum = _positive_int(effort.get("minimum_minutes"), "minimum_minutes")
        raw_recommended = _positive_int(effort.get("recommended_minutes"), "recommended_minutes")
        raw_maximum = _positive_int(effort.get("maximum_minutes"), "maximum_minutes")
        if not raw_minimum <= raw_recommended <= raw_maximum:
            raise AnalyzerError("effort must satisfy minimum <= recommended <= maximum")
        if semantic_validation:
            recommended = _five_minute_effort(raw_recommended)
            minimum, maximum = _effort_band(recommended)
        else:
            minimum, recommended, maximum = (
                raw_minimum,
                raw_recommended,
                raw_maximum,
            )
        model_timing_confidence = _confidence(
            raw.get("timing_confidence"), "timing_confidence"
        )
        timing_confidence = model_timing_confidence
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
        # Fully observed source intervals give a stable ceiling for attention.
        # They are intentionally not treated as all-active wall time: only the
        # largest conservative effort band that fits within the ceiling is used.
        if semantic_validation and lifecycle not in {"planned", "noise"}:
            bounded_effort = _effort_from_bounded_capacity(
                _bounded_evidence_capacity_minutes(evidence_ids, evidence_time_spans)
            )
            if bounded_effort is not None:
                minimum, recommended, maximum = bounded_effort
                # Exact source start/end make timing reviewable, but not certain
                # enough to claim uninterrupted human attention.
                timing_confidence = "medium"
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
                "timing_confidence": timing_confidence,
                "split_rationale": split_rationale,
                "merge_rationale": _one_line(raw.get("merge_rationale")),
                "omit_rationale": _one_line(raw.get("omit_rationale")),
                "rendered_description": None,
                "analyzer_model": provider_model,
                "analyzer_revision": provider_revision,
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
            reason = _one_line(value.get("reason"))
            if not reason:
                raise AnalyzerError(f"{kind} requires a nonempty reason")
            if kind == "exception":
                record_kind = _one_line(value.get("kind"))
                if not record_kind:
                    raise AnalyzerError("exception requires a nonempty kind")
            else:
                lifecycle = str(value.get("lifecycle") or "").strip().lower()
                if lifecycle not in {"planned", "noise"}:
                    raise AnalyzerError(
                        "omission lifecycle must be planned or noise"
                    )
            output.append(
                {
                    **value,
                    "evidence_ids": evidence_ids,
                    "reason": reason,
                    **(
                        {"kind": record_kind}
                        if kind == "exception"
                        else {"lifecycle": lifecycle}
                    ),
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


def _low_model_timing_evidence_ids(response: Mapping[str, Any]) -> set[str]:
    """Keep low model timing as an internal fallback signal, not review JSON."""
    return {
        str(evidence_id)
        for activity in response.get("activities", [])
        if isinstance(activity, Mapping)
        and _confidence(activity.get("timing_confidence"), "timing_confidence") == "low"
        for evidence_id in activity.get("evidence_ids", [])
    }


@dataclasses.dataclass(frozen=True)
class AnalyzerEndpoint:
    name: str
    url: str
    model: str
    api_key: str = ""
    timeout_seconds: int = 120
    revision: str = ""
    cf_access_client_id: str = ""
    cf_access_client_secret: str = ""

    def __post_init__(self) -> None:
        normalized_model = self.model.strip().casefold()
        if any(marker in normalized_model for marker in FORBIDDEN_ANALYZER_MODEL_MARKERS):
            raise AnalyzerError(
                "DeepSeek V4 Pro is not approved for the Clockify accounting process"
            )

    @classmethod
    def from_env(cls, prefix: str, *, default_model: str = "") -> "AnalyzerEndpoint | None":
        url = os.environ.get(f"{prefix}_URL", "").strip()
        primary_openai_route = prefix == "CLOCKIFY_ANALYZER_PRIMARY"
        if not url and primary_openai_route:
            url = os.environ.get("OPENAI_BASE_URL", "").strip()
        if not url:
            return None
        if url.rstrip("/").endswith("/v1"):
            url = url.rstrip("/") + "/chat/completions"
        endpoint = cls(
            name=prefix.lower(),
            url=url,
            model=(
                os.environ.get(f"{prefix}_MODEL")
                or (os.environ.get("OPENAI_MODEL") if primary_openai_route else "")
                or default_model
            ).strip(),
            api_key=(
                os.environ.get(f"{prefix}_API_KEY")
                or (os.environ.get("OPENAI_API_KEY") if primary_openai_route else "")
                or ""
            ).strip(),
            timeout_seconds=int(os.environ.get(f"{prefix}_TIMEOUT_SECONDS", "120")),
            revision=os.environ.get(f"{prefix}_REVISION", "").strip(),
            cf_access_client_id=(
                os.environ.get(f"{prefix}_CF_ACCESS_CLIENT_ID")
                or (os.environ.get("CF_ACCESS_CLIENT_ID") if primary_openai_route else "")
                or ""
            ).strip(),
            cf_access_client_secret=(
                os.environ.get(f"{prefix}_CF_ACCESS_CLIENT_SECRET")
                or (os.environ.get("CF_ACCESS_CLIENT_SECRET") if primary_openai_route else "")
                or ""
            ).strip(),
        )
        if bool(endpoint.cf_access_client_id) != bool(endpoint.cf_access_client_secret):
            raise AnalyzerError("Cloudflare Access service credentials must be a complete pair")
        if prefix == "CLOCKIFY_ANALYZER_PRIMARY" and endpoint.model not in APPROVED_PRIMARY_MODELS:
            raise AnalyzerError(
                "Clockify primary analyzer must use the approved DeepSeek V4 Flash cloud alias"
            )
        return endpoint


Transport = Callable[[AnalyzerEndpoint, dict[str, Any]], dict[str, Any]]


class AnalyzerResponseCache:
    """Append-only cache for validated semantic responses.

    The cache stores no request prose.  Keys bind the route and complete request body
    by digest; records retain only the validated structured response needed to make an
    immutable replay independent of provider wording drift.
    """

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.RLock()
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
                {
                    "name": endpoint.name,
                    "url": endpoint.url,
                    "model": endpoint.model,
                    "revision": endpoint.revision,
                }
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
        if status == "accepted":
            try:
                _provider_response_cache_safe(
                    value["response"], original_evidence_ids=[]
                )
            except AnalyzerError as exc:
                raise AnalyzerError(
                    f"analyzer cache line {line_number} response is unsafe"
                ) from exc
        if status == "rejected" and value.get("failure_code") not in CACHE_REJECTION_CODES:
            raise AnalyzerError(f"analyzer cache line {line_number} rejection is invalid")
        if self._decision_digest(decision) != value.get("decision_digest"):
            raise AnalyzerError(f"analyzer cache line {line_number} decision digest differs")
        return copy.deepcopy(value)

    def _load(self) -> None:
        with self._lock:
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
        with self._lock:
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
                if record["failure_code"] == "transport_timeout":
                    raise AnalyzerTimeoutError(
                        "analyzer cache records transport_timeout"
                    )
                if record["failure_code"] == "transport_error":
                    raise AnalyzerTransportError(
                        "analyzer cache records transport_error"
                    )
                raise AnalyzerContractError(
                    f"analyzer cache records {record['failure_code']}"
                )
            return copy.deepcopy(record["response"])

    def _store_record(self, record: dict[str, Any]) -> None:
        with self._lock:
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
        _provider_response_cache_safe(
            response_value, original_evidence_ids=[]
        )
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

    def store_rejected(
        self,
        endpoint: AnalyzerEndpoint,
        body: Mapping[str, Any],
        *,
        failure_code: str = "contract_rejected",
    ) -> None:
        if failure_code not in CACHE_REJECTION_CODES:
            raise AnalyzerError("analyzer cache rejection code is invalid")
        identity = self._request_identity(endpoint, body)
        decision = {"status": "rejected", "failure_code": failure_code}
        self._store_record(
            {
                "schema_version": ANALYZER_CACHE_SCHEMA_VERSION,
                **identity,
                "model": endpoint.model,
                "prompt_version": PROMPT_VERSION,
                "semantic_schema_version": SCHEMA_VERSION,
                "status": "rejected",
                "decision_digest": self._decision_digest(decision),
                "failure_code": failure_code,
            }
        )

    def summary(self) -> dict[str, Any]:
        with self._lock:
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
            **(
                {
                    "CF-Access-Client-Id": endpoint.cf_access_client_id,
                    "CF-Access-Client-Secret": endpoint.cf_access_client_secret,
                }
                if endpoint.cf_access_client_id
                else {}
            ),
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=endpoint.timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8"))
    except TimeoutError as exc:
        # A timeout has no usable provider response. Keep it distinguishable so
        # extraction can seal the exact failed request and bisect only at safe
        # conversation-turn boundaries; no identical request is retried.
        raise AnalyzerTimeoutError(
            f"analyzer endpoint {endpoint.name} failed: TimeoutError"
        ) from exc
    except urllib.error.HTTPError as exc:
        status_code = int(exc.code)
        if status_code in {408, 425, 429} or 500 <= status_code <= 599:
            raise AnalyzerRetryableHTTPError(
                f"analyzer endpoint {endpoint.name} failed: retryable HTTPError",
                status_code=status_code,
            ) from exc
        # Authentication and other client failures are route/configuration
        # errors, not transient request loss.
        raise AnalyzerError(
            f"analyzer endpoint {endpoint.name} failed: HTTPError"
        ) from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise AnalyzerTimeoutError(
                f"analyzer endpoint {endpoint.name} failed: TimeoutError"
            ) from exc
        # Never include request headers/body: they can contain credentials or evidence.
        raise AnalyzerTransportError(
            f"analyzer endpoint {endpoint.name} failed: URLError"
        ) from exc
    except json.JSONDecodeError as exc:
        # Never include request headers/body: they can contain credentials or evidence.
        raise AnalyzerError(f"analyzer endpoint {endpoint.name} failed: {type(exc).__name__}") from exc


def probe_endpoint(endpoint: AnalyzerEndpoint, transport: Transport = http_transport) -> dict[str, Any]:
    if endpoint.model.endswith((":cloud", "-cloud")) and not re.fullmatch(
        r"[a-f0-9]{64}", endpoint.revision
    ):
        raise AnalyzerError(
            "moving cloud model tags require an explicit 64-character release revision"
        )
    body = {
        "model": endpoint.model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": "Return JSON only."},
            {"role": "user", "content": canonical_json({"probe": PROMPT_VERSION})},
        ],
    }
    raw = transport(endpoint, body)
    response = raw if isinstance(raw, dict) and "choices" not in raw else _json_object_from_response(raw)
    return {
        "status": "ok",
        "endpoint": endpoint.name,
        "model": endpoint.model,
        "revision": endpoint.revision,
        "response": response,
    }


def _validate_review_taxonomy(
    result: Mapping[str, Any],
    taxonomy: list[dict[str, Any]],
) -> None:
    allowed = {
        (
            _one_line(row.get("project_name")),
            _one_line(row.get("prefix")),
            tuple(sorted(str(value) for value in row.get("tag_names", []))),
        )
        for row in taxonomy
        if isinstance(row, Mapping) and row.get("project_name")
    }
    for activity in result.get("activities", []):
        project = activity.get("project_recommendation") or {}
        selection = (
            _one_line(project.get("name")),
            _one_line(project.get("prefix")),
            tuple(sorted(str(value) for value in project.get("tag_names", []))),
        )
        if any(selection) and selection not in allowed:
            raise AnalyzerError(
                "semantic review selected a project or task type outside taxonomy"
            )


def _call_semantic_review_once(
    endpoint: AnalyzerEndpoint,
    events: list[dict[str, Any]],
    *,
    candidate: Mapping[str, Any],
    taxonomy: list[dict[str, Any]],
    tier: str,
    transport: Transport,
    known_evidence_ids: set[str],
    evidence_time_spans: dict[str, dict[str, str]] | None,
    cache: "AnalyzerResponseCache | None",
    before_transport: Callable[[AnalyzerEndpoint], None] | None,
    cancelled: Callable[[], bool] | None,
    repair_failure_code: str | None = None,
    transport_recovery_attempt: int | None = None,
    transport_failure_code: str | None = None,
    review_scope: str = "extraction",
    review_prompt_version: str = REVIEW_PROMPT_VERSION,
) -> dict[str, Any]:
    body = _review_body(
        events,
        candidate=candidate,
        taxonomy=taxonomy,
        model=endpoint.model,
        repair_failure_code=repair_failure_code,
        transport_recovery_attempt=transport_recovery_attempt,
        transport_failure_code=transport_failure_code,
        review_scope=review_scope,
        review_prompt_version=review_prompt_version,
    )
    if len(canonical_json(body).encode("utf-8")) > DEFAULT_MAX_BODY_BYTES:
        raise AnalyzerError("semantic review body exceeds configured request ceiling")
    _raise_if_cancelled(cancelled)
    response = cache.lookup(endpoint, body) if cache is not None else None
    cache_miss = response is None
    if response is None:
        if before_transport is not None:
            before_transport(endpoint)
        _raise_if_cancelled(cancelled)
        try:
            raw_response = transport(endpoint, body)
        except AnalyzerTimeoutError:
            _raise_if_cancelled(cancelled)
            if cache is not None and cache_miss:
                cache.store_rejected(
                    endpoint,
                    body,
                    failure_code="transport_timeout",
                )
            raise
        except AnalyzerTransportError:
            _raise_if_cancelled(cancelled)
            if cache is not None and cache_miss:
                cache.store_rejected(
                    endpoint,
                    body,
                    failure_code="transport_error",
                )
            raise
        _raise_if_cancelled(cancelled)
        try:
            response = _json_object_from_response(raw_response)
        except AnalyzerError as exc:
            _raise_if_cancelled(cancelled)
            if cache is not None and cache_miss:
                cache.store_rejected(
                    endpoint,
                    body,
                    failure_code=_contract_failure_code(exc),
                )
            raise AnalyzerContractError(str(exc)) from exc
    try:
        response = _normalize_provider_response(response, mode="extract")
        _provider_response_cache_safe(
            response,
            original_evidence_ids=known_evidence_ids,
        )
        restored = _restore_extraction_partitions(response, events=events)
        restored = bind_activity_evidence_spans(
            restored,
            evidence_time_spans=evidence_time_spans,
        )
        result = validate_result(
            restored,
            known_evidence_ids=known_evidence_ids,
            provider_model=endpoint.model,
            analyzer_tier=tier,
            provider_revision=endpoint.revision,
            evidence_time_spans=evidence_time_spans,
            semantic_validation=False,
        )
        _validate_review_taxonomy(result, taxonomy)
    except (AnalyzerTimeoutError, AnalyzerTransportError):
        raise
    except AnalyzerError as exc:
        _raise_if_cancelled(cancelled)
        if cache is not None and cache_miss:
            cache.store_rejected(
                endpoint,
                body,
                failure_code=_contract_failure_code(exc),
            )
        raise AnalyzerContractError(str(exc)) from exc
    _raise_if_cancelled(cancelled)
    if cache is not None and cache_miss:
        cache.store_accepted(endpoint, body, response)
    for activity in result["activities"]:
        activity["extractor_model"] = endpoint.model
        activity["semantic_reviewer_model"] = endpoint.model
        activity["semantic_reviewer_revision"] = endpoint.revision
        activity["review_prompt_version"] = review_prompt_version
    return result


def _call_semantic_review(
    endpoint: AnalyzerEndpoint,
    events: list[dict[str, Any]],
    *,
    candidate: Mapping[str, Any],
    taxonomy: list[dict[str, Any]],
    tier: str,
    transport: Transport,
    known_evidence_ids: set[str],
    evidence_time_spans: dict[str, dict[str, str]] | None,
    cache: "AnalyzerResponseCache | None",
    before_transport: Callable[[AnalyzerEndpoint], None] | None,
    cancelled: Callable[[], bool] | None,
    review_scope: str = "extraction",
    review_prompt_version: str = REVIEW_PROMPT_VERSION,
) -> dict[str, Any]:
    def failure(reason: str) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "prompt_version": PROMPT_VERSION,
            "activities": [],
            "exceptions": [{
                "kind": "analyzer_review_failure",
                "evidence_ids": sorted(known_evidence_ids),
                "reason": reason,
            }],
            "omissions": [],
        }

    def call_with_structural_repair(
        *,
        transport_recovery_attempt: int | None = None,
        transport_failure_code: str | None = None,
    ) -> dict[str, Any]:
        try:
            return _call_semantic_review_once(
                endpoint,
                events,
                candidate=candidate,
                taxonomy=taxonomy,
                tier=tier,
                transport=transport,
                known_evidence_ids=known_evidence_ids,
                evidence_time_spans=evidence_time_spans,
                cache=cache,
                before_transport=before_transport,
                cancelled=cancelled,
                transport_recovery_attempt=transport_recovery_attempt,
                transport_failure_code=transport_failure_code,
                review_scope=review_scope,
                review_prompt_version=review_prompt_version,
            )
        except AnalyzerContractError as exc:
            try:
                return _call_semantic_review_once(
                    endpoint,
                    events,
                    candidate=candidate,
                    taxonomy=taxonomy,
                    tier=tier,
                    transport=transport,
                    known_evidence_ids=known_evidence_ids,
                    evidence_time_spans=evidence_time_spans,
                    cache=cache,
                    before_transport=before_transport,
                    cancelled=cancelled,
                    repair_failure_code=_contract_failure_code(exc),
                    transport_recovery_attempt=transport_recovery_attempt,
                    transport_failure_code=transport_failure_code,
                    review_scope=review_scope,
                    review_prompt_version=review_prompt_version,
                )
            except AnalyzerContractError:
                return failure("Flash reviewer exhausted one structural repair")

    try:
        return call_with_structural_repair()
    except (AnalyzerTimeoutError, AnalyzerTransportError) as exc:
        failure_code = (
            "transport_timeout"
            if isinstance(exc, AnalyzerTimeoutError)
            else "transport_error"
        )
        for attempt in range(1, MAX_TIMEOUT_RECOVERY_ATTEMPTS + 1):
            try:
                return call_with_structural_repair(
                    transport_recovery_attempt=attempt,
                    transport_failure_code=failure_code,
                )
            except (AnalyzerTimeoutError, AnalyzerTransportError) as recovery_error:
                failure_code = (
                    "transport_timeout"
                    if isinstance(recovery_error, AnalyzerTimeoutError)
                    else "transport_error"
                )
                continue
        return failure(
            "Flash reviewer exhausted bounded transport recovery without rerunning extraction"
        )


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
    cancelled: Callable[[], bool] | None = None,
    repair_failure_code: str | None = None,
    repair_attempt: int | None = None,
    timeout_recovery_attempt: int | None = None,
    connection_recovery_attempt: int | None = None,
    review_taxonomy: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    body = _body_for(
        events,
        model=endpoint.model,
        mode="extract",
        corrections=corrections,
        private_text_approved=private_text_approved,
        repair_failure_code=repair_failure_code,
        repair_attempt=repair_attempt,
        timeout_recovery_attempt=timeout_recovery_attempt,
        connection_recovery_attempt=connection_recovery_attempt,
    )
    if len(canonical_json(body).encode("utf-8")) > DEFAULT_MAX_BODY_BYTES:
        raise AnalyzerError("analyzer body exceeds configured request ceiling")
    _raise_if_cancelled(cancelled)
    response = cache.lookup(endpoint, body) if cache is not None else None
    _raise_if_cancelled(cancelled)
    cache_miss = response is None
    if response is None:
        _raise_if_cancelled(cancelled)
        if before_transport is not None:
            before_transport(endpoint)
        _raise_if_cancelled(cancelled)
        try:
            raw_response = transport(endpoint, body)
        except AnalyzerTimeoutError:
            _raise_if_cancelled(cancelled)
            if cache is not None and cache_miss:
                cache.store_rejected(
                    endpoint,
                    body,
                    failure_code="transport_timeout",
                )
            raise
        except AnalyzerTransportError:
            _raise_if_cancelled(cancelled)
            if cache is not None and cache_miss:
                cache.store_rejected(
                    endpoint,
                    body,
                    failure_code="transport_error",
                )
            raise
        _raise_if_cancelled(cancelled)
        try:
            response = _json_object_from_response(raw_response)
        except AnalyzerError as exc:
            _raise_if_cancelled(cancelled)
            if cache is not None and cache_miss:
                cache.store_rejected(
                    endpoint,
                    body,
                    failure_code=_contract_failure_code(exc),
                )
            raise AnalyzerContractError(str(exc)) from exc
    try:
        response = _normalize_provider_response(response, mode="extract")
        extraction_ids = known_evidence_ids or {
            str(event.get("evidence_id")) for event in events
        }
        _provider_response_cache_safe(
            response, original_evidence_ids=extraction_ids
        )
        if review_taxonomy is not None:
            # Preserve the first-pass reasoning independently. Semantic
            # correctness is decided by a second Flash inference, not by the
            # legacy Python grammar and human-work heuristics below.
            if cache is not None and cache_miss:
                cache.store_accepted(endpoint, body, response)
                cache_miss = False
            reviewed = _call_semantic_review(
                endpoint,
                events,
                candidate=response,
                taxonomy=review_taxonomy,
                tier=f"{tier}_flash_review",
                transport=transport,
                known_evidence_ids=extraction_ids,
                evidence_time_spans=evidence_time_spans,
                cache=cache,
                before_transport=before_transport,
                cancelled=cancelled,
            )
            return _ValidatedAnalysis(
                reviewed,
                low_timing_evidence_ids=_low_model_timing_evidence_ids(reviewed),
            )
        restored_response = _restore_extraction_partitions(
            response, events=events
        )
        restored_response = bind_activity_evidence_spans(
            restored_response,
            evidence_time_spans=evidence_time_spans,
        )
        result = validate_result(
            restored_response,
            known_evidence_ids=extraction_ids,
            provider_model=endpoint.model,
            analyzer_tier=tier,
            provider_revision=endpoint.revision,
            evidence_time_spans=evidence_time_spans,
            evidence_support=_evidence_support(events),
        )
    except (AnalyzerTimeoutError, AnalyzerTransportError):
        raise
    except AnalyzerError as exc:
        _raise_if_cancelled(cancelled)
        if cache is not None and cache_miss:
            cache.store_rejected(
                endpoint,
                body,
                failure_code=_contract_failure_code(exc),
            )
        raise AnalyzerContractError(str(exc)) from exc
    _raise_if_cancelled(cancelled)
    if cache is not None and cache_miss:
        cache.store_accepted(endpoint, body, response)
    return _ValidatedAnalysis(
        result,
        low_timing_evidence_ids=_low_model_timing_evidence_ids(restored_response),
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
    cache: AnalyzerResponseCache | None = None,
    before_transport: Callable[[AnalyzerEndpoint], None] | None = None,
    repair_failure_code: str | None = None,
    repair_attempt: int | None = None,
    transport_recovery_attempt: int | None = None,
    semantic_validation: bool = True,
    review_taxonomy: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Synthesize one repeated workstream and reject any lost evidence."""
    body = _synthesis_body(
        activities,
        model=endpoint.model,
        workstream_id=workstream_id,
        repair_failure_code=repair_failure_code,
        repair_attempt=repair_attempt,
        transport_recovery_attempt=transport_recovery_attempt,
    )
    if len(canonical_json(body).encode("utf-8")) > DEFAULT_MAX_BODY_BYTES:
        raise AnalyzerError("synthesis body exceeds configured request ceiling")
    response = cache.lookup(endpoint, body) if cache is not None else None
    cache_miss = response is None
    if response is None:
        if before_transport is not None:
            before_transport(endpoint)
        try:
            raw_response = transport(endpoint, body)
        except AnalyzerTimeoutError:
            if cache is not None and cache_miss:
                cache.store_rejected(
                    endpoint,
                    body,
                    failure_code="transport_timeout",
                )
            raise
        except AnalyzerTransportError:
            if cache is not None and cache_miss:
                cache.store_rejected(
                    endpoint,
                    body,
                    failure_code="transport_error",
                )
            raise
        try:
            response = _json_object_from_response(raw_response)
        except AnalyzerError as exc:
            if cache is not None and cache_miss:
                cache.store_rejected(
                    endpoint,
                    body,
                    failure_code=_contract_failure_code(exc),
                )
            raise AnalyzerContractError(str(exc)) from exc
    try:
        response = _normalize_provider_response(response, mode="synthesize")
        restored_response = _restore_evidence_references(
            response, evidence_ids=known_evidence_ids
        )
        restored_response = bind_activity_evidence_spans(
            restored_response,
            evidence_time_spans=evidence_time_spans,
        )
        result = validate_result(
            restored_response,
            known_evidence_ids=known_evidence_ids,
            provider_model=endpoint.model,
            analyzer_tier=tier,
            provider_revision=endpoint.revision,
            evidence_time_spans=evidence_time_spans,
            semantic_validation=semantic_validation,
        )
        if review_taxonomy is not None:
            # Taxonomy membership remains a structural safety boundary.  The
            # synthesis response is not marked semantically reviewed; callers
            # must pass it through an independent evidence-backed Flash review.
            _validate_review_taxonomy(result, review_taxonomy)
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
            candidate_keys = [_synthesis_candidate_key(activity) for activity in result["activities"]]
            if len(candidate_keys) != len(set(candidate_keys)):
                raise AnalyzerError(
                    "synthesis split activities require distinct specific objects"
                )
    except AnalyzerError as exc:
        if cache is not None and cache_miss:
            cache.store_rejected(
                endpoint,
                body,
                failure_code=_contract_failure_code(exc),
            )
        raise AnalyzerContractError(str(exc)) from exc
    if cache is not None and cache_miss:
        cache.store_accepted(endpoint, body, response)
    return _ValidatedAnalysis(
        result,
        low_timing_evidence_ids=_low_model_timing_evidence_ids(restored_response),
    )


def _requires_stronger_fallback(result: dict[str, Any]) -> bool:
    return any(
        activity.get("semantic_confidence") == "low"
        for activity in result.get("activities", [])
    ) or bool(getattr(result, "low_timing_evidence_ids", ())) or any(
        str(exception.get("kind") or "").casefold() == "conflicting_evidence"
        for exception in result.get("exceptions", [])
        if isinstance(exception, dict)
    )


def _defer_unresolved_low_confidence(result: dict[str, Any]) -> dict[str, Any]:
    """Turn unresolved low-confidence claims into explicit exceptions."""
    retained: list[dict[str, Any]] = []
    exceptions = list(result.get("exceptions", []))
    low_timing_evidence_ids = set(getattr(result, "low_timing_evidence_ids", ()))
    for activity in result.get("activities", []):
        confidence_fields = [
            field
            for field, value in (
                ("semantic_confidence", activity.get("semantic_confidence")),
                (
                    "timing_confidence",
                    "low"
                    if low_timing_evidence_ids.intersection(activity.get("evidence_ids", []))
                    else activity.get("timing_confidence"),
                ),
            )
            if value == "low"
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


def _synthesis_candidate_key(activity: Mapping[str, Any]) -> str:
    """Keep broad allocation workstreams out of cross-chunk merge candidates."""
    project = activity.get("project_recommendation")
    project_name = project.get("name") if isinstance(project, Mapping) else ""
    return stable_digest(
        "sc-",
        {
            "project": _normalized_identity(project_name),
            "workstream": _normalized_identity(activity.get("workstream")),
            "object": _normalized_identity(activity.get("object")),
        },
    )


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
    max_workers: int = DEFAULT_ANALYZER_WORKERS,
    private_text_approved: bool | None = None,
    cache: AnalyzerResponseCache | None = None,
    review_taxonomy: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if max_workers <= 0:
        raise AnalyzerError("max_workers must be positive")
    original_events = sorted((dict(event) for event in events), key=_event_sort_key)
    if not original_events:
        return {
            "schema_version": SCHEMA_VERSION,
            "prompt_version": PROMPT_VERSION,
            "evidence_bundle_schema_version": EVIDENCE_BUNDLE_SCHEMA_VERSION,
            "evidence_bundle_manifest": {
                "schema_version": EVIDENCE_BUNDLE_SCHEMA_VERSION,
                "digest": stable_digest("sebm-", [], length=64),
                "bundles": [],
            },
            "ledger_event_count": 0,
            "ledger_evidence_digest": stable_digest("led-", []),
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
    # Extraction requests have no cross-chunk dependency.  Keep their output in
    # input order even though providers complete in a different order; synthesis
    # below deliberately remains sequential because it joins these results.
    probed: set[AnalyzerEndpoint] = set()
    probe_errors: dict[AnalyzerEndpoint, AnalyzerError] = {}
    probe_lock = threading.Lock()
    cancellation = threading.Event()

    def probe_once(endpoint: AnalyzerEndpoint) -> None:
        # Hold the gate through the call so concurrent workers cannot make a
        # second route probe.  A failed probe is remembered and re-raised, which
        # fails the run rather than treating an unavailable route as a contract
        # rejection.
        with probe_lock:
            prior_error = probe_errors.get(endpoint)
            if prior_error is not None:
                raise AnalyzerError(
                    f"analyzer endpoint {endpoint.name} probe previously failed"
                ) from prior_error
            if endpoint in probed:
                return
            try:
                probe_endpoint(endpoint, transport=transport)
            except AnalyzerError as exc:
                probe_errors[endpoint] = exc
                raise
            probed.add(endpoint)

    def before_extraction_transport(endpoint: AnalyzerEndpoint) -> None:
        _raise_if_cancelled(cancellation.is_set)
        probe_once(endpoint)
        _raise_if_cancelled(cancellation.is_set)

    def analyze_chunk(
        index_and_chunk: tuple[int, list[dict[str, Any]]],
        *,
        recovery_depth: int = 0,
        recovery_path: str = "root",
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        index, chunk = index_and_chunk
        _raise_if_cancelled(cancellation.is_set)
        chunk_ids = {str(event.get("evidence_id")) for event in chunk}
        chunk_spans = {
            evidence_id: span
            for evidence_id in chunk_ids
            if (span := _safe_time_span(original_by_id[evidence_id])) is not None
        }
        fallback_status = "not_needed"
        repair_status = "not_attempted"
        timeout_recovery_status = "not_attempted"
        connection_recovery_status = "not_attempted"
        primary_error: AnalyzerError | None = None
        fallback_feedback: str | None = None
        failure_digest: str | None = None

        def recover_partition(
            parent_failure_digest: str,
            *,
            trigger: str = "contract_rejection",
        ) -> tuple[dict[str, Any], dict[str, Any]] | None:
            if (
                len(chunk) <= MIN_PARTITION_RECOVERY_EVENTS
                or recovery_depth >= MAX_PARTITION_RECOVERY_DEPTH
            ):
                return None
            recovery_split = _turn_aware_recovery_split(chunk)
            if recovery_split is None:
                return None
            left_child, right_child, split_at = recovery_split
            child_outcomes = [
                analyze_chunk(
                    (index, child),
                    recovery_depth=recovery_depth + 1,
                    recovery_path=f"{recovery_path}.{label}",
                )
                for label, child in (("a", left_child), ("b", right_child))
            ]
            _raise_if_cancelled(cancellation.is_set)
            child_results = [
                child_result for child_result, _child_metadata in child_outcomes
            ]
            child_metadata = [
                {
                    key: value
                    for key, value in metadata.items()
                    if key != "_bundle_manifest"
                }
                for _child_result, metadata in child_outcomes
            ]
            child_classified_ids = [
                str(evidence_id)
                for child_result in child_results
                for collection in ("activities", "exceptions", "omissions")
                for item in child_result[collection]
                for evidence_id in item["evidence_ids"]
            ]
            if sorted(child_classified_ids) != sorted(chunk_ids):
                raise AnalyzerError(
                    "partition recovery did not preserve evidence exactly once"
                )
            recovered_activities = [
                item
                for child_result in child_results
                for item in child_result["activities"]
            ]
            if len({item["activity_id"] for item in recovered_activities}) != len(
                recovered_activities
            ):
                raise AnalyzerError(
                    "partition recovery emitted colliding activity identities"
                )
            _payload_bundles, chunk_bundle_manifest = _semantic_evidence_bundles(chunk)
            unresolved = any(
                item.get("kind") == "analyzer_failure"
                for child_result in child_results
                for item in child_result["exceptions"]
            )
            return {
                "schema_version": SCHEMA_VERSION,
                "prompt_version": PROMPT_VERSION,
                "activities": recovered_activities,
                "exceptions": [
                    item
                    for child_result in child_results
                    for item in child_result["exceptions"]
                ],
                "omissions": [
                    item
                    for child_result in child_results
                    for item in child_result["omissions"]
                ],
            }, {
                "chunk": index + 1,
                "event_count": len(chunk),
                "evidence_digest": stable_digest(
                    "ech-", sorted(event["evidence_id"] for event in chunk)
                ),
                "endpoint": "partition-recovery",
                "partition_path": recovery_path,
                "partition_depth": recovery_depth,
                "recovery_status": (
                    "recovered_by_partition" if not unresolved else "partition_exception"
                ),
                "repair_status": "partitioned",
                "evidence_bundle_schema_version": EVIDENCE_BUNDLE_SCHEMA_VERSION,
                "bundle_count": len(chunk_bundle_manifest),
                "bundle_manifest_digest": stable_digest(
                    "sebm-", chunk_bundle_manifest, length=64
                ),
                "recovery": {
                    "status": "recovered" if not unresolved else "exhausted",
                    "trigger": trigger,
                    "path": recovery_path,
                    "depth": recovery_depth,
                    "max_depth": MAX_PARTITION_RECOVERY_DEPTH,
                    "split_at_event": split_at,
                    "parent_failure_digest": parent_failure_digest,
                    "children": child_metadata,
                },
                "_bundle_manifest": chunk_bundle_manifest,
            }
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
                before_transport=before_extraction_transport,
                cancelled=cancellation.is_set,
                review_taxonomy=review_taxonomy,
            )
        except AnalyzerContractError as initial_error:
            # A sealed contract rejection receives a small, deterministic
            # corrective budget. Each attempt has its own seed and cache
            # identity. Feedback remains category-only, so neither rejected
            # prose nor raw model output enters a request or the cache.
            repair_error: AnalyzerContractError = initial_error
            for repair_attempt in range(1, MAX_CONTRACT_REPAIR_ATTEMPTS + 1):
                repair_failure_code = _contract_failure_code(repair_error)
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
                        before_transport=before_extraction_transport,
                        cancelled=cancellation.is_set,
                        repair_failure_code=repair_failure_code,
                        repair_attempt=repair_attempt,
                        review_taxonomy=review_taxonomy,
                    )
                except AnalyzerContractError as error:
                    repair_error = error
                    continue
                except AnalyzerError:
                    # The original sealed rejection remains the contract
                    # evidence; transport faults are never blindly retried.
                    primary_error = initial_error
                    fallback_feedback = _contract_failure_code(initial_error)
                    repair_status = "transport_failed"
                    break
                else:
                    repair_status = "used"
                    break
            else:
                primary_error = repair_error
                fallback_feedback = _contract_failure_code(repair_error)
                repair_status = "rejected"
        except AnalyzerError as error:
            primary_error = error
        if primary_error is not None:
            _raise_if_cancelled(cancellation.is_set)
            if fallback is None:
                if isinstance(primary_error, AnalyzerTimeoutError):
                    failure_digest = stable_digest(
                        "aer-",
                        {
                            "mode": "extract",
                            "failure": "transport_timeout",
                            "evidence_ids": sorted(chunk_ids),
                            "primary": {"name": primary.name, "model": primary.model},
                            "fallback": None,
                            "prompt_version": PROMPT_VERSION,
                            "schema_version": SCHEMA_VERSION,
                        },
                    )
                    recovered = recover_partition(
                        failure_digest,
                        trigger="transport_timeout",
                    )
                    if recovered is not None:
                        return recovered
                    timeout_recovery_status = "attempted"
                    for timeout_attempt in range(
                        1, MAX_TIMEOUT_RECOVERY_ATTEMPTS + 1
                    ):
                        # A cached failed attempt never calls the probe or
                        # transport.  A new attempt must first prove that the
                        # exact pinned route is healthy, and its attempt number
                        # gives it a distinct request/cache identity.
                        with probe_lock:
                            probed.discard(primary)
                            probe_errors.pop(primary, None)
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
                                before_transport=before_extraction_transport,
                                cancelled=cancellation.is_set,
                                timeout_recovery_attempt=timeout_attempt,
                                review_taxonomy=review_taxonomy,
                            )
                        except AnalyzerTimeoutError:
                            continue
                        except AnalyzerTransportError as recovery_error:
                            primary_error = recovery_error
                            repair_status = "timeout_recovery_transport_failed"
                            timeout_recovery_status = "transport_failed"
                            break
                        except AnalyzerContractError as recovery_error:
                            primary_error = recovery_error
                            repair_status = "timeout_recovery_rejected"
                            timeout_recovery_status = "contract_rejected"
                            break
                        except AnalyzerError as recovery_error:
                            raise AnalyzerError(
                                "primary analyzer failed during indivisible timeout "
                                f"recovery for chunk {index + 1}: {recovery_error}"
                            ) from recovery_error
                        else:
                            primary_error = None
                            used = primary
                            tier = "primary"
                            timeout_recovery_status = "used"
                            break
                    else:
                        # Exhausted retryable transport is an evidence exception,
                        # not permission to drop the evidence or abort every
                        # unrelated workstream in a month-scale run.
                        primary_error = None
                        result = {
                            "schema_version": SCHEMA_VERSION,
                            "prompt_version": PROMPT_VERSION,
                            "activities": [],
                            "exceptions": [{
                                "kind": "analyzer_failure",
                                "evidence_ids": sorted(chunk_ids),
                                "reason": (
                                    "qualified primary exhausted bounded timeout recovery"
                                ),
                            }],
                            "omissions": [],
                        }
                        used = primary
                        tier = "exception"
                        fallback_status = "primary_timeout_exception"
                        timeout_recovery_status = "exhausted_exception"
                if isinstance(primary_error, AnalyzerTransportError):
                    connection_recovery_status = "attempted"
                    for connection_attempt in range(
                        1, MAX_CONNECTION_RECOVERY_ATTEMPTS + 1
                    ):
                        # Every new transport attempt is gated by a fresh probe
                        # of the same pinned route. Cached failures remain
                        # replay-only and do not produce network traffic.
                        with probe_lock:
                            probed.discard(primary)
                            probe_errors.pop(primary, None)
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
                                before_transport=before_extraction_transport,
                                cancelled=cancellation.is_set,
                                connection_recovery_attempt=connection_attempt,
                                review_taxonomy=review_taxonomy,
                            )
                        except AnalyzerTransportError:
                            continue
                        except AnalyzerTimeoutError:
                            continue
                        except AnalyzerContractError as recovery_error:
                            primary_error = recovery_error
                            repair_status = "connection_recovery_rejected"
                            connection_recovery_status = "contract_rejected"
                            break
                        except AnalyzerError as recovery_error:
                            raise AnalyzerError(
                                "primary analyzer failed during connection recovery "
                                f"for chunk {index + 1}: {recovery_error}"
                            ) from recovery_error
                        else:
                            primary_error = None
                            used = primary
                            tier = "primary"
                            connection_recovery_status = "used"
                            break
                    else:
                        failure_digest = stable_digest(
                            "aer-",
                            {
                                "mode": "extract",
                                "failure": "transport_error",
                                "evidence_ids": sorted(chunk_ids),
                                "primary": {
                                    "name": primary.name,
                                    "model": primary.model,
                                },
                                "fallback": None,
                                "prompt_version": PROMPT_VERSION,
                                "schema_version": SCHEMA_VERSION,
                            },
                        )
                        primary_error = None
                        result = {
                            "schema_version": SCHEMA_VERSION,
                            "prompt_version": PROMPT_VERSION,
                            "activities": [],
                            "exceptions": [{
                                "kind": "analyzer_failure",
                                "evidence_ids": sorted(chunk_ids),
                                "reason": (
                                    "qualified primary exhausted bounded transport recovery"
                                ),
                            }],
                            "omissions": [],
                        }
                        used = primary
                        tier = "exception"
                        fallback_status = "primary_transport_exception"
                        connection_recovery_status = "exhausted_exception"
                if primary_error is not None and not isinstance(
                    primary_error, AnalyzerContractError
                ):
                    raise AnalyzerError(
                        f"primary analyzer failed for chunk {index + 1}: {primary_error}"
                    ) from primary_error
                if primary_error is not None:
                    failure_digest = stable_digest(
                        "aer-",
                        {
                            "mode": "extract",
                            "evidence_ids": sorted(chunk_ids),
                            "primary": {"name": primary.name, "model": primary.model},
                            "fallback": None,
                            "prompt_version": PROMPT_VERSION,
                            "schema_version": SCHEMA_VERSION,
                        },
                    )
                    recovered = recover_partition(failure_digest)
                    if recovered is not None:
                        return recovered
                    result = {
                        "schema_version": SCHEMA_VERSION,
                        "prompt_version": PROMPT_VERSION,
                        "activities": [],
                        "exceptions": [{
                            "kind": "analyzer_failure",
                            "evidence_ids": sorted(chunk_ids),
                            "reason": "qualified primary rejected this semantic chunk",
                        }],
                        "omissions": [],
                    }
                    used = primary
                    tier = "exception"
                    fallback_status = "primary_failed_exception"
            else:
                fallback_error: AnalyzerError | None = None
                fallback_attempt_start = 1 if fallback_feedback is not None else 0
                for fallback_attempt in range(
                    fallback_attempt_start, MAX_CONTRACT_REPAIR_ATTEMPTS + 1
                ):
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
                            before_transport=before_extraction_transport,
                            cancelled=cancellation.is_set,
                            repair_failure_code=fallback_feedback,
                            repair_attempt=(fallback_attempt or None),
                            review_taxonomy=review_taxonomy,
                        )
                    except AnalyzerContractError as error:
                        fallback_error = error
                        fallback_feedback = _contract_failure_code(error)
                        continue
                    except AnalyzerError as error:
                        fallback_error = error
                        break
                    else:
                        fallback_error = None
                        break
                if fallback_error is not None:
                    _raise_if_cancelled(cancellation.is_set)
                    if not all(isinstance(error, AnalyzerContractError) for error in (
                        primary_error, fallback_error
                    )):
                        raise AnalyzerError(
                            f"primary and fallback analyzers failed without dual contract "
                            f"rejection for chunk {index + 1}: primary={primary_error}; "
                            f"fallback={fallback_error}"
                        ) from fallback_error
                    failure_digest = stable_digest(
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
                    recovered = recover_partition(failure_digest)
                    if recovered is not None:
                        return recovered
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
                else:
                    used = fallback
                    tier = "fallback"
                    fallback_status = "used_after_primary_failure"
        else:
            _raise_if_cancelled(cancellation.is_set)
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
                        before_transport=before_extraction_transport,
                        cancelled=cancellation.is_set,
                        review_taxonomy=review_taxonomy,
                    )
                except AnalyzerContractError:
                    # A validated low-confidence primary decision is still useful
                    # evidence.  If the stronger route fails, retain only its
                    # review-safe portions and turn unresolved claims into
                    # explicit exceptions instead of retrying a sealed rejection.
                    result = _defer_unresolved_low_confidence(primary_result)
                    fallback_status = "failed_deferred"
                except AnalyzerError as fallback_error:
                    _raise_if_cancelled(cancellation.is_set)
                    raise AnalyzerError(
                        f"fallback analyzer failed for low-confidence chunk {index + 1}: "
                        f"{fallback_error}"
                    ) from fallback_error
                else:
                    used = fallback
                    tier = "fallback"
                    fallback_status = "used_for_low_confidence"
        _raise_if_cancelled(cancellation.is_set)
        if _requires_stronger_fallback(result):
            result = _defer_unresolved_low_confidence(result)
        _payload_bundles, chunk_bundle_manifest = _semantic_evidence_bundles(chunk)
        chunk_metadata = {
            "chunk": index + 1,
            "event_count": len(chunk),
            "evidence_digest": stable_digest(
                "ech-", sorted(event["evidence_id"] for event in chunk)
            ),
            "endpoint": used.name,
            "model": used.model,
            "tier": tier,
            "partition_path": recovery_path,
            "partition_depth": recovery_depth,
            "fallback_status": fallback_status,
            "repair_status": repair_status,
            "timeout_recovery_status": timeout_recovery_status,
            "connection_recovery_status": connection_recovery_status,
            "evidence_bundle_schema_version": EVIDENCE_BUNDLE_SCHEMA_VERSION,
            "bundle_count": len(chunk_bundle_manifest),
            "bundle_manifest_digest": stable_digest(
                "sebm-", chunk_bundle_manifest, length=64
            ),
            "_bundle_manifest": chunk_bundle_manifest,
        }
        if used.revision:
            chunk_metadata["revision"] = used.revision
        if fallback_status in {
            "failed_exception",
            "primary_failed_exception",
            "primary_timeout_exception",
            "primary_transport_exception",
        }:
            assert failure_digest is not None
            chunk_metadata["failure_digest"] = failure_digest
        if fallback_status == "failed_exception":
            chunk_metadata["fallback_endpoint"] = fallback.name
            chunk_metadata["fallback_model"] = fallback.model
            if fallback.revision:
                chunk_metadata["fallback_revision"] = fallback.revision
        return result, chunk_metadata

    if len(chunks) == 1 or max_workers == 1:
        chunk_outcomes = [analyze_chunk((index, chunk)) for index, chunk in enumerate(chunks)]
    else:
        # Submit only a bounded in-flight window.  `Executor.map` queues all
        # work immediately, which would allow a fatal chunk to be followed by
        # provider/cache side effects from queued chunks.  On failure, cancel
        # all not-yet-started futures; a currently running transport cannot be
        # interrupted, but its cancellation checks prevent fallback, cache
        # persistence, and later synthesis from using its result.
        executor = ThreadPoolExecutor(max_workers=min(max_workers, len(chunks)))
        pending: dict[Future[tuple[dict[str, Any], dict[str, Any]]], int] = {}
        outcomes: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}
        next_index = 0
        fatal_error: BaseException | None = None
        try:
            while next_index < len(chunks) and len(pending) < max_workers:
                future = executor.submit(analyze_chunk, (next_index, chunks[next_index]))
                pending[future] = next_index
                next_index += 1
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                completed = sorted((pending.pop(future), future) for future in done)
                for index, future in completed:
                    try:
                        outcomes[index] = future.result()
                    except BaseException as exc:
                        # Preserve the first failure exactly.  It is the cause
                        # callers need to diagnose, even if cancelled peers
                        # subsequently report their own cancellation errors.
                        fatal_error = exc
                        break
                if fatal_error is not None:
                    cancellation.set()
                    for future in pending:
                        future.cancel()
                    break
                # Refill every slot that actually completed before waiting
                # again.  This keeps the provider window full when one request
                # is slow, while the bounded `pending` map and cancellation
                # gate still prevent queued work from escaping after a fatal
                # peer failure is observed.
                for _index, _future in completed:
                    if next_index >= len(chunks):
                        break
                    future = executor.submit(analyze_chunk, (next_index, chunks[next_index]))
                    pending[future] = next_index
                    next_index += 1
        except BaseException as exc:
            fatal_error = exc
        finally:
            if fatal_error is not None:
                cancellation.set()
                for future in pending:
                    future.cancel()
                executor.shutdown(wait=True, cancel_futures=True)
            else:
                executor.shutdown(wait=True)
        if fatal_error is not None:
            raise fatal_error
        chunk_outcomes = [outcomes[index] for index in range(len(chunks))]
    results = [result for result, _metadata in chunk_outcomes]
    metadata = [chunk_metadata for _result, chunk_metadata in chunk_outcomes]
    bundle_manifest: list[dict[str, Any]] = []
    for chunk_metadata in metadata:
        chunk_number = int(chunk_metadata["chunk"])
        for record in chunk_metadata.pop("_bundle_manifest"):
            bundle_manifest.append({"chunk": chunk_number, **record})
    bundle_manifest.sort(key=lambda value: (value["chunk"], value["bundle_ref"]))

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
        grouped.setdefault(_synthesis_candidate_key(activity), []).append(activity)

    def synthesize_with_transport_recovery(
        provisional: list[dict[str, Any]],
        *,
        workstream_id: str,
        synthesis_ids: set[str],
        synthesis_spans: dict[str, dict[str, str]],
    ) -> dict[str, Any]:
        """Retry only sealed request-specific synthesis transport failures."""
        try:
            return _call_synthesis_validated(
                primary,
                provisional,
                workstream_id=workstream_id,
                tier="primary",
                transport=transport,
                known_evidence_ids=synthesis_ids,
                evidence_time_spans=synthesis_spans,
                cache=cache,
                before_transport=probe_once,
                semantic_validation=review_taxonomy is None,
                review_taxonomy=review_taxonomy,
            )
        except (AnalyzerTimeoutError, AnalyzerTransportError) as initial_error:
            last_error: AnalyzerError = initial_error
        for attempt in range(1, MAX_CONNECTION_RECOVERY_ATTEMPTS + 1):
            with probe_lock:
                probed.discard(primary)
                probe_errors.pop(primary, None)
            try:
                return _call_synthesis_validated(
                    primary,
                    provisional,
                    workstream_id=workstream_id,
                    tier="primary",
                    transport=transport,
                    known_evidence_ids=synthesis_ids,
                    evidence_time_spans=synthesis_spans,
                    cache=cache,
                    before_transport=probe_once,
                    transport_recovery_attempt=attempt,
                    semantic_validation=review_taxonomy is None,
                    review_taxonomy=review_taxonomy,
                )
            except (AnalyzerTimeoutError, AnalyzerTransportError) as error:
                last_error = error
                continue
        raise last_error

    synthesis_exceptions: list[dict[str, Any]] = []
    synthesis_omissions: list[dict[str, Any]] = []
    for candidate_key in sorted(grouped):
        provisional = sorted(grouped[candidate_key], key=lambda value: value["activity_id"])
        if len(provisional) < 2:
            continue
        workstream_id = provisional[0]["workstream_id"]
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
        primary_error: AnalyzerError | None = None
        fallback_feedback: str | None = None
        try:
            synthesized = synthesize_with_transport_recovery(
                provisional,
                workstream_id=workstream_id,
                synthesis_ids=synthesis_ids,
                synthesis_spans=synthesis_spans,
            )
            used = primary
            tier = "primary"
        except AnalyzerContractError as initial_error:
            repair_error: AnalyzerContractError = initial_error
            for repair_attempt in range(1, MAX_CONTRACT_REPAIR_ATTEMPTS + 1):
                repair_failure_code = _contract_failure_code(repair_error)
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
                        repair_failure_code=repair_failure_code,
                        repair_attempt=repair_attempt,
                        semantic_validation=review_taxonomy is None,
                        review_taxonomy=review_taxonomy,
                    )
                except AnalyzerContractError as error:
                    repair_error = error
                    continue
                except AnalyzerError:
                    primary_error = initial_error
                    fallback_feedback = _contract_failure_code(initial_error)
                    break
                else:
                    used = primary
                    tier = "primary"
                    break
            else:
                primary_error = repair_error
                fallback_feedback = _contract_failure_code(repair_error)
        except AnalyzerError as error:
            primary_error = error
        if primary_error is not None:
            if fallback is None:
                if isinstance(
                    primary_error, (AnalyzerTimeoutError, AnalyzerTransportError)
                ):
                    # The individual extraction activities remain valid, but an
                    # unresolved merge could duplicate billing. Keep the whole
                    # candidate workstream out of proposals and surface one
                    # evidence-bound exception instead of aborting the month.
                    for activity in provisional:
                        del activities_by_id[activity["activity_id"]]
                    synthesis_exceptions.append({
                        "kind": "analyzer_synthesis_failure",
                        "evidence_ids": sorted(synthesis_ids),
                        "reason": (
                            "qualified primary exhausted bounded synthesis "
                            "transport recovery"
                        ),
                        "failure_digest": stable_digest(
                            "aer-",
                            {
                                "mode": "synthesize",
                                "failure": "transport_error",
                                "workstream_id": workstream_id,
                                "evidence_ids": sorted(synthesis_ids),
                                "primary": {
                                    "name": primary.name,
                                    "model": primary.model,
                                },
                                "fallback": None,
                                "prompt_version": PROMPT_VERSION,
                                "schema_version": SCHEMA_VERSION,
                            },
                        ),
                        "primary_model": primary.model,
                    })
                    continue
                if not isinstance(primary_error, AnalyzerContractError):
                    raise AnalyzerError(
                        f"primary analyzer failed for synthesis {workstream_id}: {primary_error}"
                    ) from primary_error
                if _contract_failure_code(primary_error) in SYNTHESIS_INTEGRITY_FAILURE_CODES:
                    raise primary_error
                # Extraction is individually valid, but the only qualified
                # route could not decide whether these repeated workstream
                # claims should merge. Keep them out of proposals without
                # turning a sealed semantic rejection into a route outage.
                for activity in provisional:
                    del activities_by_id[activity["activity_id"]]
                synthesis_exceptions.append({
                    "kind": "analyzer_synthesis_failure",
                    "evidence_ids": sorted(synthesis_ids),
                    "reason": "qualified primary rejected workstream synthesis",
                    "failure_digest": stable_digest(
                        "aer-",
                        {
                            "mode": "synthesize",
                            "workstream_id": workstream_id,
                            "evidence_ids": sorted(synthesis_ids),
                            "primary": {"name": primary.name, "model": primary.model},
                            "fallback": None,
                            "prompt_version": PROMPT_VERSION,
                            "schema_version": SCHEMA_VERSION,
                        },
                    ),
                    "primary_model": primary.model,
                })
                continue
            fallback_error: AnalyzerError | None = None
            fallback_attempt_start = 1 if fallback_feedback is not None else 0
            for fallback_attempt in range(
                fallback_attempt_start, MAX_CONTRACT_REPAIR_ATTEMPTS + 1
            ):
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
                        repair_failure_code=fallback_feedback,
                        repair_attempt=(fallback_attempt or None),
                        semantic_validation=review_taxonomy is None,
                        review_taxonomy=review_taxonomy,
                    )
                except AnalyzerContractError as error:
                    fallback_error = error
                    fallback_feedback = _contract_failure_code(error)
                    continue
                except AnalyzerError as error:
                    fallback_error = error
                    break
                else:
                    fallback_error = None
                    break
            if fallback_error is not None:
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
        if review_taxonomy is not None:
            synthesis_events = [
                event
                for event in original_events
                if str(event.get("evidence_id")) in synthesis_ids
            ]
            synthesized = _call_semantic_review(
                used,
                synthesis_events,
                candidate=synthesized,
                taxonomy=review_taxonomy,
                tier=f"{tier}_post_synthesis_flash_review",
                transport=transport,
                known_evidence_ids=synthesis_ids,
                evidence_time_spans=synthesis_spans,
                cache=cache,
                before_transport=probe_once,
                cancelled=cancellation.is_set,
            )
            synthesis_exceptions.extend(synthesized["exceptions"])
            synthesis_omissions.extend(synthesized["omissions"])
        for activity in provisional:
            del activities_by_id[activity["activity_id"]]
        for activity in synthesized["activities"]:
            if activity["activity_id"] in activities_by_id:
                raise AnalyzerError("synthesis activity identity collides with another workstream")
            activities_by_id[activity["activity_id"]] = activity
    exceptions = [value for result in results for value in result["exceptions"]]
    exceptions.extend(synthesis_exceptions)
    omissions = [value for result in results for value in result["omissions"]]
    omissions.extend(synthesis_omissions)
    return {
        "schema_version": SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "review_prompt_version": (
            REVIEW_PROMPT_VERSION if review_taxonomy is not None else None
        ),
        "evidence_bundle_schema_version": EVIDENCE_BUNDLE_SCHEMA_VERSION,
        "evidence_bundle_manifest": {
            "schema_version": EVIDENCE_BUNDLE_SCHEMA_VERSION,
            "digest": stable_digest("sebm-", bundle_manifest, length=64),
            "bundles": bundle_manifest,
        },
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
        _fixture_payload, fixture_manifest = _semantic_evidence_bundles(events)
        result = {
            **fixture,
            "evidence_bundle_schema_version": EVIDENCE_BUNDLE_SCHEMA_VERSION,
            "evidence_bundle_manifest": {
                "schema_version": EVIDENCE_BUNDLE_SCHEMA_VERSION,
                "digest": stable_digest("sebm-", fixture_manifest, length=64),
                "bundles": fixture_manifest,
            },
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
