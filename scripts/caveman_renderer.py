"""Fail-closed renderer for concise, evidence-backed Clockify descriptions."""
from dataclasses import dataclass
import re
from typing import Any, Mapping


class CavemanRenderError(ValueError):
    """Base class for unsafe or incomplete Caveman description input."""


class CavemanValidationError(CavemanRenderError):
    """Raised when rendering would produce unsafe, invented, or malformed text."""


@dataclass(frozen=True)
class CavemanParts:
    prefix: str
    action: str
    object: str
    outcome: str


@dataclass(frozen=True)
class RenderResult:
    description: str | None
    error: CavemanValidationError | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


_TRUNCATION_RE = re.compile(r"(?:\.{2,}|[\u2024\u2025\u2026\u22ef])|(?:\.\s+){1,}\.")
_BARE_DOMAIN_RE = re.compile(
    r"(?<![@A-Za-z0-9_-])(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+(?:com|net|org|io|ai|app|dev|ro|agency|co|uk|eu|info|biz|me|tv|edu|gov)(?![A-Za-z0-9_-])",
    re.I,
)
_SHELL_COMMAND_RE = re.compile(
    r"(?:^|\s)(?:\$\s*)?(?:sudo\s+)?(?:git\s+(?:status|diff|log|commit|push|pull|checkout|switch)|curl\s+|wget\s+|ssh\s+|scp\s+|rsync\s+|pytest(?:\s|$)|py\.test(?:\s|$)|python(?:3(?:\.\d+)?)?\s+-m\s+pytest\b|npm\s+(?:run|test|ci|install)\b|npx\s+|node\s+|docker\s+|kubectl\s+|terraform\s+|make\s+(?:test|check|build)\b|bash\s+|zsh\s+)",
    re.I,
)
_PROMPT_OR_RULE_RE = re.compile(
    r"\b(?:system|user|assistant)\s+(?:prompt|message)\b|\btool\s+call\b|\bprompt\b|\b(?:follow|obey)\s+(?:these\s+)?(?:rules?|instructions?)\b|\b(?:must|should)\s+(?:not\s+)?(?:always|never)\b",
    re.I,
)
_STATUS_DUMP_RE = re.compile(
    r"\b(?:needs[\s_-]*review|in[\s_-]*progress|wip|queued|pending|awaiting|standing\s+by|still\s+(?:running|waiting)|run(?:ning)?\s+status|status\s*[:—-]\s*(?:running|waiting|pending|queued|complete|completed)|all\s+(?:done|complete)|task\s+(?:is\s+)?(?:done|complete)|agent\s+(?:is\s+)?(?:running|completed|waiting)|heartbeat\s+(?:agent|run|status|is|was|received|running))\b",
    re.I,
)
_EVIDENCE_DUMP_RE = re.compile(
    r"\b(?:evidence\s+(?:dump|ids?|records?)|provenance\s+(?:dump|ids?)|session\s+ids?|"
    r"trace\s+ids?|log\s+dump|artifact\s+(?:ids?|paths?)|source\s+ids?)\b",
    re.I,
)

_FORBIDDEN_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("NEEDS REVIEW marker", re.compile(r"\bneeds[\s\u00a0\u2000-\u200b\u202f\u205f\u3000_-]+review\b", re.I)),
    ("Markdown", re.compile(r"(?:`|\*|_|\[|\]|\{|\}|#|^\s*(?:>|[-+]|\d+[.)])\s*|\|)", re.M)),
    ("truncation", _TRUNCATION_RE),
    ("first-person narration", re.compile(r"\b(?:i|me|my|mine|we|us|our|ours|i'm|we're)\b", re.I)),
    ("URL", re.compile(r"\b(?:(?:https?|ftp|file|mailto):|www\.)", re.I)),
    ("domain", _BARE_DOMAIN_RE),
    ("path", re.compile(r"(?:~[/\\]|(?:[A-Za-z]:)?[/\\][^\s]+|\\)")),
    ("hash", re.compile(r"\b[0-9a-f]{7,}\b", re.I)),
    ("email", re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")),
    ("command", _SHELL_COMMAND_RE),
    ("prompt or rule", _PROMPT_OR_RULE_RE),
    ("agent status", _STATUS_DUMP_RE),
    ("evidence dump", _EVIDENCE_DUMP_RE),
)


def _part(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise CavemanValidationError(f"{name} must be a non-empty, already-trimmed string")
    if "\n" in value or "\r" in value or "\t" in value:
        raise CavemanValidationError(f"{name} must be one line")
    return value


def _parts(value: CavemanParts | Mapping[str, Any]) -> CavemanParts:
    if isinstance(value, CavemanParts):
        return value
    if not isinstance(value, Mapping):
        raise CavemanValidationError("renderer input must be CavemanParts or a mapping")
    return CavemanParts(
        _part(value.get("prefix"), "prefix"),
        _part(value.get("action", value.get("verb")), "action"),
        _part(value.get("object"), "object"),
        _part(value.get("outcome", value.get("bounded_outcome")), "outcome"),
    )


def validate_description(description: str) -> str:
    """Validate an already-rendered description without rewriting it."""
    if not isinstance(description, str) or not description:
        raise CavemanValidationError("description must be a non-empty string")
    if description != description.strip() or "\n" in description or "\r" in description or "\t" in description:
        raise CavemanValidationError("description must be one trimmed line")
    if description.count(" — ") != 1:
        raise CavemanValidationError("description must use exactly one Prefix — body separator")
    prefix, body = description.split(" — ", 1)
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9 &-]{0,24}", prefix):
        raise CavemanValidationError("prefix contains unsupported characters")
    if not body or not re.match(r"^[A-Z]", body):
        raise CavemanValidationError("body must begin with a capitalized verb")
    for label, pattern in _FORBIDDEN_PATTERNS:
        if pattern.search(description):
            raise CavemanValidationError(f"description contains forbidden {label}")
    body_words = re.findall(r"\b[\w'-]+\b", body)
    if any(left.casefold() == right.casefold() for left, right in zip(body_words, body_words[1:])):
        raise CavemanValidationError("description contains adjacent repeated words")
    word_count = len(re.findall(r"\b[\w'-]+\b", description))
    if not 5 <= word_count <= 14:
        raise CavemanValidationError(
            f"description has {word_count} words; expected hard bounds 5–14 "
            "with an 8–14 word target"
        )
    return description


def render_caveman_description(value: CavemanParts | Mapping[str, Any]) -> str:
    """Render exact provided semantics or fail; never truncate or embellish them."""
    parts = _parts(value)
    description = f"{parts.prefix} — {parts.action} {parts.object} {parts.outcome}"
    return validate_description(description)


def try_render_caveman_description(value: CavemanParts | Mapping[str, Any]) -> RenderResult:
    """Typed result variant for callers that route invalid records to exceptions."""
    try:
        return RenderResult(render_caveman_description(value))
    except CavemanValidationError as exc:
        return RenderResult(None, exc)


# Compact aliases for pipeline integrations.
render = render_caveman_description
try_render = try_render_caveman_description
