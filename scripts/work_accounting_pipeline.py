#!/usr/bin/env python3
"""Evidence-grounded semantic work accounting for one Clockify run bundle.

This stage replaces legacy burst proposals with semantic activities, fixed
meeting blocks, and strict non-overlapping effort allocations.  It writes only
inside the selected local run directory.
"""
from __future__ import annotations

import argparse
import base64
import copy
import dataclasses
import datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Iterable, Mapping, Sequence

try:
    from scripts import caveman_renderer
    from scripts import clockify_sync_collect as collector
    from scripts import evidence_ledger
    from scripts import meeting_reconciliation
    from scripts import review_corrections
    from scripts import semantic_analyzer
    from scripts import work_allocator
except ModuleNotFoundError:
    import caveman_renderer  # type: ignore[no-redef]
    import clockify_sync_collect as collector  # type: ignore[no-redef]
    import evidence_ledger  # type: ignore[no-redef]
    import meeting_reconciliation  # type: ignore[no-redef]
    import review_corrections  # type: ignore[no-redef]
    import semantic_analyzer  # type: ignore[no-redef]
    import work_allocator  # type: ignore[no-redef]


SCHEMA_VERSION = 1
ALLOCATION_MODE = "non_overlapping_v1"
MEETING_RECONCILIATION_MIN_RATIO = 0.8
POINT_OBSERVATION_GAP_THRESHOLDS_SECONDS = {
    "claude_bursts_event": collector.BURST_GAP_SECONDS,
    "codex_sessions_event": collector.BURST_GAP_SECONDS,
}
POINT_OBSERVATION_CLUSTERING_INPUT = {
    "configuration_source": "scripts.clockify_sync_collect.BURST_GAP_SECONDS",
    "max_consecutive_gap_seconds": collector.BURST_GAP_SECONDS,
    "source_types": sorted(POINT_OBSERVATION_GAP_THRESHOLDS_SECONDS),
    "user_anchor_source_types": ["hermes_db_sessions_event", "hermes_sessions_event"],
}
NOISE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("heartbeat", re.compile(r"^\s*(?:heartbeat|health[- ]?check)(?:\s*[:—-].*)?\s*$", re.I)),
    ("standing_by", re.compile(r"^\s*(?:standing by|still waiting|no change(?: yet)?)(?:[.!])?\s*$", re.I)),
    ("tool_transport", re.compile(r"^\s*\[(?:tool_result|tool_ref|thinking)(?::[^]]+)?]\s*$", re.I)),
    ("session_control", re.compile(r"^\s*/(?:goal|review|compact|status)\b", re.I)),
    ("approval_wait", re.compile(r"^\s*(?:approval request|awaiting board approval|waiting for approval)(?:\s*[:—-].*)?\s*$", re.I)),
    ("polling", re.compile(r"^\s*(?:polling|checking again|still running|download(?:s)? running)(?:\s*[:—-].*)?\s*$", re.I)),
    ("injected_wrapper", re.compile(r"<(?:codex_internal_context|command-message|local-command)", re.I)),
)
AUTONOMOUS_MULTICA_SESSION_RE = re.compile(
    r"\byou are running as (?:a )?(?:local coding )?agent for a multica workspace\b|"
    r"\byour assigned issue id is\b",
    re.I,
)


class WorkAccountingError(RuntimeError):
    """Invalid or incomplete local accounting input."""


def _failed_review_retry_targets(
    source: Mapping[str, Any],
    events: list[dict[str, Any]],
    cache_path: Path,
    selected_digest: str | Sequence[str],
) -> dict[tuple[str, ...], str]:
    """Bind selected failed reviewer groups to one exact source ledger and cache."""
    selected = _canonical_retry_digests(selected_digest)
    ids = sorted(str(event["evidence_id"]) for event in events)
    if (
        source.get("ledger_event_count") != len(ids)
        or source.get("ledger_evidence_digest") != semantic_analyzer.stable_digest("led-", ids)
    ):
        raise WorkAccountingError("failed-review retry source ledger binding differs")
    cache_summary = source.get("analyzer_cache")
    snapshot = cache_summary.get("snapshot") if isinstance(cache_summary, Mapping) else None
    if (
        not isinstance(snapshot, Mapping)
        or snapshot.get("path") != "analyzer-cache-used.jsonl"
        or not cache_path.is_file()
    ):
        raise WorkAccountingError("failed-review retry source cache binding is missing")
    content = cache_path.read_bytes()
    record_count = snapshot.get("record_count")
    if not isinstance(record_count, int) or isinstance(record_count, bool) or record_count < 0:
        raise WorkAccountingError("failed-review retry source cache binding differs")
    lines = content.splitlines(keepends=True)
    source_prefix = b"".join(lines[:record_count])
    if (
        len(lines) < record_count
        or snapshot.get("sha256") != hashlib.sha256(source_prefix).hexdigest()
    ):
        raise WorkAccountingError("failed-review retry source cache binding differs")
    semantic_analyzer.AnalyzerResponseCache(cache_path)
    targets: dict[str, tuple[tuple[str, ...], str]] = {}
    for row in source.get("exceptions", []):
        if not isinstance(row, Mapping) or row.get("kind") not in {
            "analyzer_review_failure", "analyzer_review_partial_quarantine"
        }:
            continue
        evidence_ids = row.get("evidence_ids")
        if not isinstance(evidence_ids, list) or not evidence_ids:
            raise WorkAccountingError("failed-review retry source failure is unsupported")
        key = tuple(sorted(str(value) for value in evidence_ids))
        if len(set(key)) != len(key) or not set(key) <= set(ids):
            raise WorkAccountingError("failed-review retry source evidence is invalid")
        digest = semantic_analyzer.stable_digest("frt-", list(key), length=64)
        if row["kind"] == "analyzer_review_partial_quarantine":
            # The accepted quarantine may be only a subset of the earlier
            # failed request. Its original failure code is not derivable from
            # this row and must not be guessed.
            code = "citation_quarantine"
        else:
            match = re.fullmatch(
                r"Flash reviewer exhausted bounded (?:structural repair|failed-review retry|scoped retry): "
                r"(contract_rejected(?:_[a-z_]+)?)",
                str(row.get("reason") or ""),
            )
            code = match.group(1) if match else None
        if code != "citation_quarantine" and code not in semantic_analyzer.CONTRACT_FAILURE_CODES:
            raise WorkAccountingError("failed-review retry source failure is unsupported")
        if digest in targets:
            raise WorkAccountingError("failed-review retry source has duplicate targets")
        targets[digest] = (key, code)
    if any(digest not in targets for digest in selected):
        raise WorkAccountingError("failed-review retry target is absent from source")
    chosen = [targets[digest] for digest in selected]
    selected_ids: set[str] = set()
    for key, _code in chosen:
        if selected_ids.intersection(key):
            raise WorkAccountingError("failed-review retry targets overlap")
        selected_ids.update(key)
    return {key: code for key, code in chosen}


def _scoped_review_partitions(
    events: list[dict[str, Any]], *, maximum_members: int = 64,
) -> list[list[dict[str, Any]]]:
    """Bound focused requests without dividing an instruction from its result."""
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for event in events:
        grouped.setdefault(semantic_analyzer._semantic_context_key(event), []).append(event)
    units: list[list[dict[str, Any]]] = []
    for context in sorted(grouped):
        source_order = context[0] == "session" and all(
            isinstance(row.get("source_ref"), Mapping)
            and type(row["source_ref"].get("ordinal")) is int
            for row in grouped[context]
        )
        ordered = sorted(
            grouped[context],
            key=lambda row: (
                # Ordinals restart for each captured burst. Keep burst bounds
                # ahead of source order so later instructions cannot borrow
                # an earlier burst's reply; preserve ordinal order within it.
                str((row.get("raw_source_span") or {}).get("session_start") or "") if source_order else "",
                str((row.get("raw_source_span") or {}).get("session_end") or "") if source_order else "",
                row["source_ref"]["ordinal"] if source_order else 0,
                semantic_analyzer._event_sort_key(row)[:2],
                str(row["evidence_id"]),
            ),
        )
        units.extend(semantic_analyzer._context_turn_units(context, ordered))
    partitions: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for unit in units:
        # The limit is soft for an indivisible source turn. The hard request
        # byte ceiling is checked for every partition before any transport.
        if current and len(current) + len(unit) > maximum_members:
            partitions.append(current)
            current = []
        current.extend(unit)
    if current:
        partitions.append(current)
    return partitions


def run_scoped_failed_review_retry(
    source: Mapping[str, Any],
    events: list[dict[str, Any]],
    *,
    primary: semantic_analyzer.AnalyzerEndpoint,
    cache: semantic_analyzer.AnalyzerResponseCache,
    review_taxonomy: list[dict[str, Any]],
    targets: Mapping[tuple[str, ...], str],
    source_semantic_sha256: str,
    transport: semantic_analyzer.Transport = semantic_analyzer.http_transport,
    private_text_approved: bool | None = None,
    scoped_review_mode: str = "fresh",
) -> dict[str, Any]:
    """Review only selected sealed failures, retaining every other decision."""
    if scoped_review_mode not in {
        "fresh", "scoped_review_v1", "scoped_review_v2",
        "scoped_review_v3_invalid_effort", "scoped_review_v4_citation_quarantine",
    }:
        raise WorkAccountingError("scoped failed-review mode is invalid")
    request_mode = scoped_review_mode
    if scoped_review_mode == "fresh":
        if targets and all(code == "contract_rejected_invalid_effort" for code in targets.values()):
            request_mode = "scoped_review_v3_invalid_effort"
        elif targets and all(code in {
            "contract_rejected_duplicate_evidence", "citation_quarantine",
        } for code in targets.values()):
            request_mode = "scoped_review_v4_citation_quarantine"
        else:
            request_mode = "scoped_review_v2"
    if request_mode == "scoped_review_v4_citation_quarantine" and any(
        code not in {"contract_rejected_duplicate_evidence", "citation_quarantine"}
        for code in targets.values()
    ):
        raise WorkAccountingError("scoped citation quarantine requires duplicate-evidence targets")
    if re.fullmatch(r"[a-f0-9]{64}", source_semantic_sha256) is None or not targets:
        raise WorkAccountingError("scoped failed-review source identity is invalid")
    ids = {str(event.get("evidence_id")) for event in events}
    if (
        len(ids) != len(events)
        or source.get("ledger_event_count") != len(events)
        or source.get("ledger_evidence_digest")
        != semantic_analyzer.stable_digest("led-", sorted(ids))
    ):
        raise WorkAccountingError("scoped failed-review source ledger binding differs")
    selected = {evidence_id for key in targets for evidence_id in key}
    if sum(len(key) for key in targets) != len(selected) or not selected <= ids:
        raise WorkAccountingError("scoped failed-review targets overlap or leave source")
    source_exceptions = source.get("exceptions")
    if not isinstance(source_exceptions, list):
        raise WorkAccountingError("scoped failed-review source exceptions are invalid")
    source_groups = {
        tuple(sorted(str(value) for value in row.get("evidence_ids", [])))
        for row in source_exceptions
        if isinstance(row, Mapping)
        and row.get("kind") in {"analyzer_review_failure", "analyzer_review_partial_quarantine"}
    }
    if any(
        not key or tuple(sorted(set(key))) != key or key not in source_groups
        or (
            code != "citation_quarantine"
            and code not in semantic_analyzer.CONTRACT_FAILURE_CODES
        )
        for key, code in targets.items()
    ):
        raise WorkAccountingError("scoped failed-review target is not an exact source group")
    for section in ("activities", "exceptions", "omissions"):
        rows = source.get(section)
        if not isinstance(rows, list):
            raise WorkAccountingError("scoped failed-review source rows are invalid")
        for row in rows:
            if not isinstance(row, Mapping) or not isinstance(row.get("evidence_ids"), list):
                raise WorkAccountingError("scoped failed-review source row is invalid")
            cited = set(str(value) for value in row["evidence_ids"])
            if cited & selected and not (
                section == "exceptions"
                and row.get("kind") in {"analyzer_review_failure", "analyzer_review_partial_quarantine"}
                and tuple(sorted(cited)) in targets
            ):
                raise WorkAccountingError("scoped failed-review source overlaps a non-target row")
    cache_summary = source.get("analyzer_cache")
    snapshot = cache_summary.get("snapshot") if isinstance(cache_summary, Mapping) else None
    if not isinstance(snapshot, Mapping) or snapshot.get("path") != "analyzer-cache-used.jsonl":
        raise WorkAccountingError("scoped failed-review source cache binding is missing")
    count = snapshot.get("record_count")
    references = cache_summary.get("records")
    if not isinstance(references, list):
        raise WorkAccountingError("scoped failed-review source cache references are invalid")
    sealed = cache.records_for_snapshot(references, configured_endpoints=(primary,))
    source_snapshot = b"".join(
        (semantic_analyzer.canonical_json(record) + "\n").encode("utf-8")
        for record in sealed
    )
    if (
        not isinstance(count, int) or isinstance(count, bool)
        or count != len(sealed)
        or hashlib.sha256(source_snapshot).hexdigest() != snapshot.get("sha256")
    ):
        raise WorkAccountingError("scoped failed-review source cache binding differs")
    cache.used.update({record["cache_key"]: record["decision_digest"] for record in sealed})
    result = copy.deepcopy(dict(source))
    result["exceptions"] = [
        row for row in result["exceptions"]
        if tuple(sorted(str(value) for value in row["evidence_ids"])) not in targets
    ]
    events_by_id = {str(event["evidence_id"]): event for event in events}
    jobs: list[tuple[list[dict[str, Any]], set[str], dict[str, dict[str, str]], dict[str, str]]] = []
    for key in sorted(targets):
        group_digest = semantic_analyzer.stable_digest("frt-", list(key), length=64)
        for subset in _scoped_review_partitions([events_by_id[value] for value in key]):
            subset_ids = {str(event["evidence_id"]) for event in subset}
            subset_digest = semantic_analyzer.stable_digest(
                "frt-", sorted(subset_ids), length=64
            )
            spans = {
                evidence_id: span
                for evidence_id in subset_ids
                if (span := semantic_analyzer._safe_time_span(events_by_id[evidence_id])) is not None
            }
            marker = {
                "source_semantic_sha256": source_semantic_sha256,
                "group_digest": group_digest,
                "subset_digest": subset_digest,
            }
            if request_mode != "scoped_review_v1":
                marker["mode"] = request_mode
            body = semantic_analyzer._review_body(
                subset,
                candidate={"activities": [], "exceptions": [], "omissions": []},
                taxonomy=review_taxonomy, model=primary.model,
                review_scope="failed_review_scoped_recovery",
                scoped_failed_review=marker,
                local_coverage_repair=request_mode == "scoped_review_v4_citation_quarantine",
            )
            if len(semantic_analyzer.canonical_json(body).encode("utf-8")) > semantic_analyzer.DEFAULT_MAX_BODY_BYTES:
                raise WorkAccountingError("scoped failed-review request exceeds analyzer ceiling")
            jobs.append((subset, subset_ids, spans, marker))
    for subset, subset_ids, spans, marker in jobs:
        def authorize_transport(
            endpoint: semantic_analyzer.AnalyzerEndpoint,
            rows: list[dict[str, Any]] = subset,
        ) -> None:
            semantic_analyzer.require_current_live_flash_route(endpoint)
            semantic_analyzer._require_private_text_approval(rows, private_text_approved)

        try:
            reviewed = semantic_analyzer._call_semantic_review_once(
                primary, subset,
                candidate={"activities": [], "exceptions": [], "omissions": []},
                taxonomy=review_taxonomy, tier="primary_scoped_review",
                transport=transport, known_evidence_ids=subset_ids,
                evidence_time_spans=spans, cache=cache,
                before_transport=authorize_transport,
                cancelled=None,
                review_scope="failed_review_scoped_recovery",
                scoped_failed_review=marker,
                local_coverage_repair=request_mode == "scoped_review_v4_citation_quarantine",
            )
        except semantic_analyzer.AnalyzerContractError as exc:
            reviewed = {
                "activities": [], "omissions": [],
                "exceptions": [{
                    "kind": "analyzer_review_failure",
                    "evidence_ids": sorted(subset_ids),
                    "reason": "Flash reviewer exhausted bounded scoped retry: "
                    + semantic_analyzer._contract_failure_code(exc),
                }],
            }
        for section in ("activities", "exceptions", "omissions"):
            result[section].extend(reviewed[section])
    result["analyzer_cache"] = cache.summary()
    failure_codes = {
        semantic_analyzer.stable_digest("frt-", list(key), length=64): code
        for key, code in targets.items()
    }
    digests = sorted(failure_codes)
    provenance: dict[str, Any] = {
        "mode": request_mode,
        "source_semantic_sha256": source_semantic_sha256,
        "source_cache_sha256": snapshot["sha256"],
    }
    if len(digests) == 1:
        provenance["target_digest"] = digests[0]
        provenance["failure_code"] = failure_codes[digests[0]]
    else:
        provenance["target_digests"] = digests
        provenance["failure_codes"] = {digest: failure_codes[digest] for digest in digests}
    result["failed_review_retry"] = provenance
    return result


def _canonical_retry_digests(value: str | Sequence[str]) -> tuple[str, ...]:
    selected = (value,) if isinstance(value, str) else tuple(value)
    if (
        not selected
        or any(not isinstance(digest, str) or re.fullmatch(r"frt-[a-f0-9]{64}", digest) is None for digest in selected)
        or len(set(selected)) != len(selected)
    ):
        raise WorkAccountingError("failed-review retry requires distinct exact target digests")
    return tuple(sorted(selected))


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_bytes(path: Path, content: bytes) -> None:
    """Atomically replace one run-local immutable snapshot."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _seal_analyzer_cache_snapshot(
    run_dir: Path, analyzer_cache_path: Path, analysis: Mapping[str, Any]
) -> dict[str, Any]:
    cache_summary = analysis.get("analyzer_cache")
    if not isinstance(cache_summary, Mapping) or not isinstance(
        cache_summary.get("records"), list
    ):
        raise WorkAccountingError("semantic analysis lacks used analyzer cache records")
    primary = semantic_analyzer.AnalyzerEndpoint.from_env(
        "CLOCKIFY_ANALYZER_PRIMARY",
        default_model=semantic_analyzer.DEFAULT_PRIMARY_MODEL,
    )
    fallback = semantic_analyzer.AnalyzerEndpoint.from_env(
        "CLOCKIFY_ANALYZER_FALLBACK"
    )
    configured_endpoints = tuple(
        endpoint for endpoint in (primary, fallback) if endpoint is not None
    )
    records = semantic_analyzer.AnalyzerResponseCache(
        analyzer_cache_path
    ).records_for_snapshot(
        cache_summary["records"], configured_endpoints=configured_endpoints
    )
    content = b"".join(
        (semantic_analyzer.canonical_json(record) + "\n").encode("utf-8")
        for record in records
    )
    target = run_dir / "analyzer-cache-used.jsonl"
    _write_bytes(target, content)
    return {
        "path": target.name,
        "record_count": len(records),
        "sha256": hashlib.sha256(content).hexdigest(),
    }


def _parse_dt(value: Any) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=collector.BUCHAREST)
    return parsed


def _iso(value: dt.datetime) -> str:
    return value.isoformat(timespec="seconds")


def _minutes(start: dt.datetime, end: dt.datetime) -> int:
    return max(0, int((end - start).total_seconds() // 60))


def _seconds(start: dt.datetime, end: dt.datetime) -> int:
    return max(0, int((end - start).total_seconds()))


def _drop_failed_allocations(
    allocation: work_allocator.AllocationResult,
    failed_activity_ids: set[str],
) -> work_allocator.AllocationResult:
    """Remove rejected activities without moving accepted allocations into freed time."""
    if not failed_activity_ids:
        return allocation
    removed = [
        (row.start, row.end)
        for row in allocation.allocations
        if row.activity_id in failed_activity_ids
    ]
    free = [*allocation.unallocated_capacity.intervals, *removed]
    free.sort(key=lambda value: (value[0], value[1]))
    merged: list[tuple[dt.datetime, dt.datetime]] = []
    for start, end in free:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return work_allocator.AllocationResult(
        evidence=tuple(
            row for row in allocation.evidence
            if row.activity_id not in failed_activity_ids
        ),
        allocations=tuple(
            row for row in allocation.allocations
            if row.activity_id not in failed_activity_ids
        ),
        unallocated_capacity=work_allocator.UnallocatedCapacity(
            sum(_minutes(start, end) for start, end in merged),
            tuple(merged),
        ),
        covered_by_existing=tuple(
            row for row in allocation.covered_by_existing
            if row.activity_id not in failed_activity_ids
        ),
        contested_time=tuple(
            row for row in allocation.contested_time
            if row.activity_id not in failed_activity_ids
        ),
    )


def _span(event: Mapping[str, Any]) -> tuple[dt.datetime | None, dt.datetime | None]:
    raw = event.get("raw_source_span") if isinstance(event.get("raw_source_span"), Mapping) else {}
    start = _parse_dt(
        raw.get("start")
        or raw.get("timestamp")
        or event.get("observed_at")
        or raw.get("session_start")
    )
    end = _parse_dt(raw.get("end") or raw.get("timestamp") or raw.get("session_end"))
    if start and not end:
        end = start + dt.timedelta(minutes=1)
    if start and end and end <= start:
        end = start + dt.timedelta(minutes=1)
    return start, end


def _observed_span(event: Mapping[str, Any]) -> tuple[dt.datetime | None, dt.datetime | None]:
    """Return only source-recorded bounds; an observed instant is not duration."""
    raw = event.get("raw_source_span") if isinstance(event.get("raw_source_span"), Mapping) else {}
    if raw.get("start"):
        start = _parse_dt(raw.get("start"))
        end = _parse_dt(raw.get("end"))
    elif raw.get("timestamp"):
        start = _parse_dt(raw.get("timestamp"))
        end = None
    elif raw.get("session_start"):
        start = _parse_dt(raw.get("session_start"))
        end = _parse_dt(raw.get("session_end"))
    else:
        start = _parse_dt(event.get("observed_at"))
        end = None
    return (start, end) if start and end and end > start else (start, None)


def _attributes(event: Mapping[str, Any]) -> Mapping[str, Any]:
    value = event.get("attributes")
    return value if isinstance(value, Mapping) else {}


def classify_noise(event: Mapping[str, Any]) -> str | None:
    """High-precision deterministic noise classification; uncertain text stays."""
    attrs = _attributes(event)
    content = str(attrs.get("content") or "").strip()
    role = str(attrs.get("role") or "").lower()
    kind = str(attrs.get("kind") or "").lower()
    if role == "system":
        return "system_message"
    if (
        role == "tool"
        or kind == "tool"
        or kind == "tool_transport"
        or kind in semantic_analyzer.TOOL_KINDS
    ):
        return "tool_transport"
    for reason, pattern in NOISE_PATTERNS:
        if content and pattern.search(content):
            return reason
    return None


def load_ledger(path: Path) -> tuple[evidence_ledger.EvidenceLedger, list[dict[str, Any]]]:
    document = _read_json(path)
    if document.get("schema_version") != evidence_ledger.SCHEMA_VERSION:
        raise WorkAccountingError("unsupported evidence ledger schema")
    events = tuple(
        evidence_ledger.EvidenceEvent.from_document(value)
        for value in document.get("events", [])
    )
    manifest_document = document.get("manifest") or {}
    manifest = evidence_ledger.LedgerManifest.from_document(manifest_document)
    ledger = evidence_ledger.EvidenceLedger(
        events, manifest.source_inventory, manifest.timezone, manifest.member_identities
    )
    ledger.validate(manifest)
    return ledger, [event.document() for event in ledger.events]


def _analysis_events(
    events: Iterable[dict[str, Any]],
    member_identities: frozenset[str] = frozenset({"vlad@serenichron.com"}),
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    event_list = list(events)
    retained: list[dict[str, Any]] = []
    noise: list[dict[str, str]] = []
    has_message_events = any(
        str(event.get("source_type", "")).endswith("_event")
        for event in event_list
    )
    canonical_session_keys = {
        (
            str(source.get("source_type") or ""),
            str(source.get("machine") or ""),
            str(source.get("session_id") or ""),
        )
        for event in event_list
        for source in (
            event.get("source_ref")
            if isinstance(event.get("source_ref"), Mapping)
            else {},
        )
        if (
            source.get("source_type")
            and source.get("machine")
            and source.get("session_id")
            and str(event.get("source_type") or "")
            == f"{source.get('source_type')}_event"
        )
    }
    autonomous_session_keys = {
        (
            str(source.get("source_type") or ""),
            str(source.get("machine") or ""),
            str(source.get("session_id") or ""),
        )
        for event in event_list
        for source in (
            event.get("source_ref")
            if isinstance(event.get("source_ref"), Mapping)
            else {},
        )
        if (
            str(event.get("source_type") or "")
            in {"codex_sessions", "hermes_db_sessions", "claude_bursts"}
            and source.get("source_type")
            and source.get("machine")
            and source.get("session_id")
            and AUTONOMOUS_MULTICA_SESSION_RE.search(
                str(_attributes(event).get("first_user_message") or "")
            )
        )
    }
    for event in event_list:
        source_type = str(event.get("source_type") or "")
        if source_type == "clockify":
            continue
        if source_type in {"fathom", "calendly"}:
            eligible, exclusion = _meeting_is_eligible(event, member_identities)
            semantic_status = str(_attributes(event).get("semantic_evidence_status") or "")
            if not eligible or semantic_status == "title_only":
                noise.append({
                    "evidence_id": str(event.get("evidence_id")),
                    "reason": f"recording_preclassified:{exclusion or semantic_status}",
                })
                continue
        source = (
            event.get("source_ref")
            if isinstance(event.get("source_ref"), Mapping)
            else {}
        )
        session_key = (
            str(source.get("source_type") or source_type),
            str(source.get("machine") or ""),
            str(source.get("session_id") or ""),
        )
        if session_key in autonomous_session_keys:
            noise.append({
                "evidence_id": str(event.get("evidence_id")),
                "reason": "autonomous_background_session",
            })
            continue
        if has_message_events and source_type.startswith("enriched_"):
            # Canonical message events are richer; old enriched snippets would
            # duplicate and bias the semantic model.
            continue
        if source_type in {"codex_sessions", "hermes_db_sessions", "claude_bursts"}:
            if session_key in canonical_session_keys:
                noise.append({
                    "evidence_id": str(event.get("evidence_id")),
                    "reason": "duplicate_session_summary",
                })
                continue
        reason = classify_noise(event)
        if reason:
            noise.append({"evidence_id": str(event.get("evidence_id")), "reason": reason})
            continue
        retained.append(event)
    return retained, sorted(noise, key=lambda value: value["evidence_id"])


def _load_corrections(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    try:
        decisions = review_corrections.load_decisions(path)
        return review_corrections.derive_learning_cases(decisions)
    except review_corrections.ReviewDecisionError as exc:
        raise WorkAccountingError(f"review correction log is invalid: {exc}") from exc


def _load_regression_cases(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    try:
        return review_corrections.derive_regression_cases(
            review_corrections.load_decisions(path)
        )
    except review_corrections.ReviewDecisionError as exc:
        raise WorkAccountingError(f"review correction log is invalid: {exc}") from exc


def _load_verified_posted_credits(path: Path | None) -> list[dict[str, Any]]:
    if path is None or not path.exists():
        return []
    try:
        return review_corrections.load_verified_posted_credits(path)
    except review_corrections.ReviewDecisionError as exc:
        raise WorkAccountingError(f"review correction log is invalid: {exc}") from exc


def analyze_ledger(
    events: list[dict[str, Any]],
    *,
    analysis_fixture: Path | None = None,
    corrections: list[dict[str, Any]] | None = None,
    analyzer_cache_path: Path | None = None,
    analyzer_target_body_bytes: int | None = None,
    analyzer_max_events_per_chunk: int | None = None,
    analyzer_workers: int | None = None,
    review_taxonomy: list[dict[str, Any]] | None = None,
    review_routing: Mapping[str, Any] | None = None,
    failed_review_retry_source: Path | None = None,
    failed_review_retry_digest: str | Sequence[str] | None = None,
) -> dict[str, Any]:
    known = {str(event.get("evidence_id")) for event in events}
    if (failed_review_retry_source is None) != (failed_review_retry_digest is None):
        raise WorkAccountingError("failed-review retry source and target must be paired")
    if failed_review_retry_source is not None and analysis_fixture is not None:
        raise WorkAccountingError("failed-review retry cannot use an analysis fixture")
    if analysis_fixture:
        fixture = _read_json(analysis_fixture)
        raw_activities = fixture.get("activities", [])
        reviewed = bool(raw_activities) and all(
            isinstance(activity, Mapping)
            and activity.get("semantic_reviewer_model")
            for activity in raw_activities
        )
        result = semantic_analyzer.validate_result(
            fixture,
            known_evidence_ids=known,
            provider_model="fixture",
            analyzer_tier="fixture",
            semantic_validation=not reviewed,
        )
        # Live scoped recovery retains source rows and appends reviewed rows.
        # Validation sorts them by identity, which changes the order of the
        # allocator's serialized evidence during an otherwise identical replay.
        for section in ("activities", "exceptions", "omissions"):
            source_rows = fixture.get(section, [])
            order = {
                tuple(sorted(str(value) for value in row["evidence_ids"])): index
                for index, row in enumerate(source_rows)
            }
            if len(order) != len(source_rows):
                raise WorkAccountingError("analysis fixture has duplicate evidence groups")
            result[section].sort(
                key=lambda row: order[tuple(row["evidence_ids"])]
            )
        if reviewed:
            semantic_analyzer._validate_review_taxonomy(
                result,
                review_taxonomy or [],
            )
            provenance = {
                tuple(sorted(str(value) for value in activity.get("evidence_ids", []))): {
                    key: activity.get(key)
                    for key in (
                        "analyzer_model",
                        "analyzer_tier",
                        "analyzer_revision",
                        "extractor_model",
                        "semantic_reviewer_model",
                        "semantic_reviewer_revision",
                        "review_prompt_version",
                    )
                    if key != "extractor_model" or key in activity
                }
                for activity in raw_activities
            }
            for activity in result["activities"]:
                activity.update(
                    provenance.get(tuple(activity["evidence_ids"]), {})
                )
        # A completed semantic run is also a valid offline fixture. Preserve
        # its replay identity metadata after revalidating the semantic rows;
        # validate_result intentionally returns only the provider contract.
        for key in (
            "review_prompt_version",
            "evidence_bundle_schema_version",
            "evidence_bundle_manifest",
            "ledger_event_count",
            "ledger_evidence_digest",
            "analysis_chunks",
            "analyzer_cache",
            "failed_review_retry",
        ):
            if key in fixture:
                result[key] = copy.deepcopy(fixture[key])
        return result
    primary = semantic_analyzer.AnalyzerEndpoint.from_env(
        "CLOCKIFY_ANALYZER_PRIMARY",
        default_model=semantic_analyzer.DEFAULT_PRIMARY_MODEL,
    )
    if primary is None:
        raise WorkAccountingError(
            "semantic analyzer is not configured; CLOCKIFY_ANALYZER_PRIMARY_URL is required"
        )
    retry_targets = None
    retry_cache_sha256 = None
    retry_source_document = None
    if failed_review_retry_source is not None:
        if analyzer_cache_path is None:
            raise WorkAccountingError("failed-review retry requires a bound analyzer cache")
        semantic_analyzer.require_current_live_flash_route(primary)
        retry_source_document = _read_json(failed_review_retry_source)
        retry_targets = _failed_review_retry_targets(
            retry_source_document, events,
            analyzer_cache_path, failed_review_retry_digest,
        )
        retry_cache_sha256 = retry_source_document["analyzer_cache"]["snapshot"]["sha256"]
    fallback = semantic_analyzer.AnalyzerEndpoint.from_env("CLOCKIFY_ANALYZER_FALLBACK")
    cache = (
        semantic_analyzer.AnalyzerResponseCache(
            analyzer_cache_path, record_review_diagnostics=retry_targets is not None,
        )
        if analyzer_cache_path is not None
        else None
    )
    tuning = {
        key: value
        for key, value in {
            "target_body_bytes": analyzer_target_body_bytes,
            "max_events_per_chunk": analyzer_max_events_per_chunk,
            "max_workers": analyzer_workers,
        }.items()
        if value is not None
    }
    routing = dict(review_routing or {"session_routes": [], "meeting_routes": []})
    if review_routing is None:
        for choice in review_taxonomy or []:
            for pattern in choice.get("selection_guidance", []):
                routing["session_routes"].append({
                    "pattern": pattern,
                    "project_name": choice.get("project_name"),
                    "prefix": choice.get("prefix"),
                    "tag_names": choice.get("tag_names", []),
                    "confidence": "medium",
                })
    hinted_events = _with_semantic_route_hints(events, routing)
    scoped_retry = retry_targets is not None and any(
        isinstance(row, Mapping)
        and tuple(sorted(str(value) for value in row.get("evidence_ids", []))) in retry_targets
        and (
            row.get("kind") == "analyzer_review_partial_quarantine"
            or str(row.get("reason") or "").startswith(
                "Flash reviewer exhausted bounded structural repair:"
            )
            or str(row.get("reason") or "").startswith(
                "Flash reviewer exhausted bounded failed-review retry:"
            )
            or str(row.get("reason") or "").startswith(
                "Flash reviewer exhausted bounded scoped retry:"
            )
        )
        for row in retry_source_document.get("exceptions", [])
    )
    if scoped_retry:
        result = run_scoped_failed_review_retry(
            retry_source_document, hinted_events, primary=primary, cache=cache,
            review_taxonomy=review_taxonomy or [], targets=retry_targets,
            source_semantic_sha256=hashlib.sha256(
                failed_review_retry_source.read_bytes()
            ).hexdigest(),
        )
    else:
        result = semantic_analyzer.analyze_tiered(
            hinted_events,
            primary=primary,
            fallback=fallback,
            corrections=corrections,
            cache=cache,
            review_taxonomy=review_taxonomy,
            **({"failed_review_retry_targets": retry_targets} if retry_targets is not None else {}),
            **tuning,
        )
    if retry_targets is not None and not scoped_retry:
        target_ids = set().union(*retry_targets)
        for section in ("activities", "exceptions", "omissions"):
            source_rows = retry_source_document.get(section, [])
            result_rows = result.get(section, [])
            if not isinstance(source_rows, list) or not isinstance(result_rows, list):
                raise WorkAccountingError("failed-review retry semantic output is invalid")
            for row in result_rows:
                cited = set(str(value) for value in row.get("evidence_ids", []))
                if cited & target_ids and not cited <= target_ids:
                    raise WorkAccountingError("failed-review retry crossed target evidence boundary")
            def unaffected(rows: list[dict[str, Any]]) -> list[str]:
                return sorted(
                    semantic_analyzer.canonical_json(
                        {key: value for key, value in row.items() if key != "rendered_description"}
                        if section == "activities" else row
                    )
                    for row in rows
                    if not set(str(value) for value in row.get("evidence_ids", [])) & target_ids
                )
            if unaffected(source_rows) != unaffected(result_rows):
                raise WorkAccountingError("failed-review retry changed non-target semantic output")
        selected = _canonical_retry_digests(failed_review_retry_digest)
        provenance = {
            "source_semantic_sha256": hashlib.sha256(failed_review_retry_source.read_bytes()).hexdigest(),
            "source_cache_sha256": retry_cache_sha256,
        }
        if len(selected) == 1:
            provenance["target_digest"] = selected[0]
            provenance["failure_code"] = next(iter(retry_targets.values()))
        else:
            provenance["target_digests"] = list(selected)
            provenance["failure_codes"] = {
                semantic_analyzer.stable_digest("frt-", list(key), length=64): code
                for key, code in retry_targets.items()
            }
        result["failed_review_retry"] = provenance
    return result


def _semantic_review_taxonomy(routing: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Expose only valid Clockify project/task choices to semantic review."""
    choices: dict[tuple[str, str, tuple[str, ...]], dict[str, Any]] = {}
    for section in ("session_routes", "meeting_routes"):
        for route in routing.get(section, []):
            if not isinstance(route, Mapping) or not route.get("project_name"):
                continue
            tag_names = tuple(sorted(str(value) for value in route.get("tag_names", [])))
            key = (
                str(route.get("project_name") or ""),
                str(route.get("prefix") or "SC"),
                tag_names,
            )
            choice = choices.setdefault(key, {
                "project_name": key[0],
                "prefix": key[1],
                "tag_names": list(tag_names),
                "billable": bool(route.get("billable", True)),
                "selection_guidance": [],
            })
            guidance = choice["selection_guidance"]
            for field in ("pattern", "email_domain", "title_regex"):
                value = str(route.get(field) or "").strip()
                if value and value not in guidance:
                    guidance.append(value)
    # Prefix overrides are billing-identification aliases, not new Clockify
    # projects.  Expose every existing task type for the matched project so the
    # Flash reviewer can select both the correct task and the required prefix.
    base_choices = list(choices.values())
    for override in routing.get("prefix_overrides", []):
        if not isinstance(override, Mapping):
            continue
        project_prefix = str(override.get("project_name_prefix") or "").casefold()
        prefix = str(override.get("prefix") or "").strip()
        patterns = [str(value) for value in override.get("patterns", []) if str(value)]
        if not project_prefix or not prefix or not patterns:
            continue
        for base in base_choices:
            if not str(base["project_name"]).casefold().startswith(project_prefix):
                continue
            key = (
                str(base["project_name"]),
                prefix,
                tuple(base["tag_names"]),
            )
            choices.setdefault(key, {
                **base,
                "prefix": prefix,
                "selection_guidance": patterns,
            })
    for choice in choices.values():
        choice["selection_guidance"].sort()
    return [choices[key] for key in sorted(choices)]


def _semantic_route_hint(
    event: Mapping[str, Any], routing: Mapping[str, Any]
) -> dict[str, Any] | None:
    record = _event_record(event)
    if event.get("source_type") in {"fathom", "calendly"}:
        candidate = collector.route_meeting(record, dict(routing))
    else:
        candidate = _route_session_record(record, routing)
    action = str(candidate.get("action") or "")
    if action == "ambiguous":
        return None
    if action == "skip":
        return {
            "action": "skip",
            "reason": str(candidate.get("reason") or "local route excludes source"),
        }
    project_name = str(candidate.get("project_name") or "")
    if not project_name:
        return None
    candidate = _apply_prefix_override(candidate, [event], routing)
    return {
        "action": "route",
        "project_name": project_name,
        "prefix": str(candidate.get("prefix") or "SC"),
        "tag_names": sorted(str(value) for value in candidate.get("tag_names", [])),
        "confidence": str(candidate.get("confidence") or "low"),
    }


def _with_semantic_route_hints(
    events: list[dict[str, Any]], routing: Mapping[str, Any]
) -> list[dict[str, Any]]:
    hinted: list[dict[str, Any]] = []
    for event in events:
        copied = dict(event)
        if hint := _semantic_route_hint(event, routing):
            copied["semantic_route_hint"] = hint
        hinted.append(copied)
    return hinted


def _route_session_record(
    record: Mapping[str, Any], routing: Mapping[str, Any]
) -> dict[str, Any]:
    context = str(record.get("cwd") or record.get("path") or "")
    normalized_context = re.sub(r"[^a-z0-9]+", "-", context.casefold()).strip("-")
    labels = [
        str(record.get("label") or ""),
        str(record.get("title") or ""),
        normalized_context,
    ]
    return collector.route_session(
        {"label": " ".join(value for value in labels if value), "path": context},
        dict(routing),
    )


def _routes_by_name(routing: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for route in routing.get("session_routes", []):
        if not isinstance(route, dict) or not route.get("project_name"):
            continue
        result.setdefault(str(route["project_name"]).casefold(), route)
    for route in routing.get("meeting_routes", []):
        if not isinstance(route, dict) or not route.get("project_name"):
            continue
        result.setdefault(str(route["project_name"]).casefold(), route)
    return result


def _routes_by_selection(
    routing: Mapping[str, Any],
) -> dict[tuple[str, str, tuple[str, ...]], dict[str, Any]]:
    result: dict[tuple[str, str, tuple[str, ...]], dict[str, Any]] = {}
    for section in ("session_routes", "meeting_routes", "evidence_routes"):
        for route in routing.get(section, []):
            if not isinstance(route, dict) or not route.get("project_name"):
                continue
            key = (
                str(route["project_name"]).casefold(),
                str(route.get("prefix") or "SC"),
                tuple(sorted(str(value) for value in route.get("tag_names", []))),
            )
            result.setdefault(key, route)
            for override in routing.get("prefix_overrides", []):
                if not isinstance(override, Mapping):
                    continue
                project_prefix = str(
                    override.get("project_name_prefix") or ""
                ).casefold()
                prefix = str(override.get("prefix") or "").strip()
                if project_prefix and prefix and key[0].startswith(project_prefix):
                    result.setdefault(
                        (key[0], prefix, key[2]),
                        {
                            **route,
                            "base_prefix": str(route.get("prefix") or "SC"),
                            "prefix": prefix,
                        },
                    )
    return result


def _description_from_review_correction(
    activity: Mapping[str, Any], regression_cases: Iterable[Mapping[str, Any]],
) -> str | None:
    """Apply only an exact local human wording replacement, never provider hints."""
    target = review_corrections.proposal_target(activity)
    if target is None:
        return None
    replacements: set[str] = set()
    for case in regression_cases:
        if (str(case.get("activity_id") or ""), str(case.get("evidence_fingerprint") or "")) != target:
            continue
        if case.get("decision") != "modify" or "wording" not in case.get("correction_categories", ()):
            continue
        patch = case.get("expected_field_patch")
        operation = patch.get("description") if isinstance(patch, Mapping) else None
        if not isinstance(operation, Mapping) or operation.get("op") != "replace":
            continue
        value = operation.get("value")
        if not isinstance(value, str) or not value.strip() or "\n" in value or "\r" in value:
            return None
        replacements.add(value)
    # Conflicting exact targets stay visible to the regression gate.
    return next(iter(replacements)) if len(replacements) == 1 else None


def _route_from_review_correction(
    activity: Mapping[str, Any],
    regression_cases: Iterable[Mapping[str, Any]],
    routing: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Resolve only an exact evidence-bound routing correction."""
    evidence_ids = activity.get("evidence_ids", [])
    if not isinstance(evidence_ids, list) or not evidence_ids:
        return None
    target = (
        str(activity.get("activity_id") or ""),
        review_corrections.evidence_fingerprint(evidence_ids),
    )
    for case in regression_cases:
        if (
            str(case.get("activity_id") or ""),
            str(case.get("evidence_fingerprint") or ""),
        ) != target:
            continue
        if case.get("decision") != "modify":
            return None
        patch = case.get("expected_field_patch")
        if not isinstance(patch, Mapping):
            return None
        project = patch.get("client_project")
        tags = patch.get("tag_names")
        project_name = project.get("value") if isinstance(project, Mapping) else None
        tag_names = tags.get("value") if isinstance(tags, Mapping) else None
        if not isinstance(project_name, str) or not isinstance(tag_names, list):
            return None
        # Exact corrections select a project/task, not a universal SC prefix.
        # Use its canonical configured route and refuse ambiguous native targets.
        expected_tags = tuple(sorted(str(value) for value in tag_names))
        configured_routes = [
            route
            for section in ("session_routes", "meeting_routes", "evidence_routes")
            for route in routing.get(section, [])
            if isinstance(route, dict) and route.get("project_name")
            and not route.get("base_prefix")
        ]
        # Explicit human selections may name a configured lifecycle route even
        # when the activity does not trigger automatic lifecycle/cutover routing.
        # Keep both declarations so ordinary/lifecycle native conflicts fail closed.
        for rule in routing.get("client_lifecycle_routes", []):
            activation = rule.get("activation") if isinstance(rule, Mapping) else None
            route = activation.get("route") if isinstance(activation, Mapping) else None
            if isinstance(route, Mapping) and route.get("project_name"):
                configured_routes.append(dict(route))
        matches = {
            (
                str(route.get("project_suffix") or ""),
                tuple(sorted(str(value) for value in route.get("tag_suffixes", []))),
                str(route.get("prefix") or "SC"),
                bool(route.get("billable", True)),
            ): route
            for route in configured_routes
            if str(route["project_name"]).casefold() == project_name.casefold()
            and tuple(sorted(str(value) for value in route.get("tag_names", []))) == expected_tags
        }
        return next(iter(matches.values())) if len(matches) == 1 else None
    return None


def _route_from_client_lifecycle(
    cited_events: list[Mapping[str, Any]], routing: Mapping[str, Any],
    *, activity: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Apply a configured client route only on/after its explicit cutover."""
    for rule in routing.get("client_lifecycle_routes", []):
        if not isinstance(rule, Mapping):
            continue
        pattern = str(rule.get("pattern") or "")
        if not pattern:
            continue
        outcome_scope = _activity_routing_text(activity)
        if outcome_scope and re.search(pattern, outcome_scope, flags=re.IGNORECASE) is None:
            continue
        activation = rule.get("activation")
        if not isinstance(activation, Mapping):
            return None
        effective = _parse_dt(activation.get("effective_at"))
        route = activation.get("route")
        if effective is None or not isinstance(route, Mapping):
            return None
        activated = False
        for event in cited_events:
            searchable = _substantive_evidence_text((event,))
            observed = _parse_dt(event.get("observed_at"))
            if (
                observed is not None
                and observed >= effective
                and re.search(pattern, searchable, flags=re.IGNORECASE) is not None
            ):
                activated = True
                break
        if not activated:
            continue
        selection = (
            str(route.get("project_name") or "").casefold(),
            str(route.get("prefix") or "SC"),
            tuple(sorted(str(value) for value in route.get("tag_names", []))),
        )
        return _routes_by_selection(routing).get(selection) or dict(route)
    return None


def _route_from_explicit_evidence(
    cited_events: list[Mapping[str, Any]], routing: Mapping[str, Any],
    *, activity: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    searchable = _substantive_evidence_text(cited_events)
    for rule in routing.get("evidence_routes", []):
        if not isinstance(rule, Mapping):
            continue
        pattern = str(rule.get("pattern") or "")
        if not pattern or re.search(pattern, searchable, flags=re.IGNORECASE) is None:
            continue
        outcome_scope = _activity_routing_text(activity)
        if outcome_scope and re.search(pattern, outcome_scope, flags=re.IGNORECASE) is None:
            continue
        selection = (
            str(rule.get("project_name") or "").casefold(),
            str(rule.get("prefix") or "SC"),
            tuple(sorted(str(value) for value in rule.get("tag_names", []))),
        )
        return _routes_by_selection(routing).get(selection) or dict(rule)
    return None


def _activity_routing_text(activity: Mapping[str, Any] | None) -> str:
    """Scope client-name rules to the actual outcome, not shared chat history."""
    if not activity:
        return ""
    recommendation = activity.get("project_recommendation") or {}
    return " ".join(
        str(value or "") for value in (
            activity.get("action"), activity.get("object"), activity.get("outcome"),
            recommendation.get("name") if isinstance(recommendation, Mapping) else None,
        )
    ).strip()


def _substantive_evidence_text(cited_events: Iterable[Mapping[str, Any]]) -> str:
    """Return human-authored evidence text without paths or source metadata."""
    return " ".join(
        " ".join(
            str(_attributes(event).get(field) or "")
            for field in ("content", "title", "description", "summary")
        )
        for event in cited_events
    )


def _apply_prefix_override(
    route: Mapping[str, Any],
    cited_events: list[Mapping[str, Any]],
    routing: Mapping[str, Any],
) -> dict[str, Any]:
    selected = dict(route)
    selected["prefix"] = str(
        selected.pop("base_prefix", None) or selected.get("prefix") or "SC"
    )
    project_name = str(selected.get("project_name") or "").casefold()
    searchable = " ".join(
        json.dumps(_event_record(event), ensure_ascii=False, sort_keys=True)
        for event in cited_events
    ).casefold()
    for override in routing.get("prefix_overrides", []):
        if not isinstance(override, Mapping):
            continue
        project_prefix = str(override.get("project_name_prefix") or "").casefold()
        patterns = [str(value).casefold() for value in override.get("patterns", [])]
        if (
            project_prefix
            and project_name.startswith(project_prefix)
            and any(pattern and pattern in searchable for pattern in patterns)
        ):
            selected["prefix"] = str(override.get("prefix") or selected["prefix"])
            break
    return selected


def _event_record(event: Mapping[str, Any]) -> dict[str, Any]:
    attrs = dict(_attributes(event))
    raw = event.get("raw_source_span") if isinstance(event.get("raw_source_span"), Mapping) else {}
    source = event.get("source_ref") if isinstance(event.get("source_ref"), Mapping) else {}
    record = {
        **attrs,
        "start": raw.get("start") or raw.get("timestamp") or event.get("observed_at"),
        "end": raw.get("end") or raw.get("timestamp"),
        "path": raw.get("path"),
        "cwd": raw.get("cwd"),
        "machine": source.get("machine"),
        "session_id": source.get("session_id"),
    }
    if event.get("source_type") == "calendly":
        record["calendar_invitees"] = list(attrs.get("participants") or [])
    return record


def resolve_route(
    activity: Mapping[str, Any],
    cited_events: list[dict[str, Any]],
    routing: Mapping[str, Any],
) -> tuple[dict[str, Any] | None, str | None]:
    if lifecycle := _route_from_client_lifecycle(cited_events, routing, activity=activity):
        return _apply_prefix_override(lifecycle, cited_events, routing), None
    deterministic_routes: list[dict[str, Any]] = []
    skipped_routes: list[str] = []
    for event in cited_events:
        record = _event_record(event)
        if event.get("source_type") in {"fathom", "calendly"}:
            candidate = collector.route_meeting(record, dict(routing))
        else:
            candidate = _route_session_record(record, routing)
        if candidate.get("action") == "skip":
            skipped_routes.append(str(candidate.get("reason") or "deterministic route excluded source"))
            continue
        if candidate.get("action") != "ambiguous":
            deterministic_routes.append(candidate)

    route_identities = {
        (
            str(route.get("project_suffix") or ""),
            str(route.get("project_name") or "").casefold(),
            tuple(sorted(str(value) for value in route.get("tag_suffixes", []))),
            bool(route.get("billable", True)),
        )
        for route in deterministic_routes
    }
    if skipped_routes and deterministic_routes:
        return None, "cited evidence mixes excluded and billable sources; semantic split required"
    if skipped_routes and not deterministic_routes:
        return None, sorted(skipped_routes)[0]
    if len(route_identities) > 1:
        names = sorted({str(route.get("project_name") or "unknown") for route in deterministic_routes})
        return None, f"cited evidence spans multiple deterministic routes; semantic split required: {', '.join(names)}"
    deterministic = deterministic_routes[0] if deterministic_routes else None

    recommended = activity.get("project_recommendation") or {}
    recommended_name = str(recommended.get("name") or "").casefold()
    recommended_tags = tuple(
        sorted(str(value) for value in recommended.get("tag_names", []))
    )
    if activity.get("semantic_reviewer_model") and recommended_name:
        recommended_prefix = str(recommended.get("prefix") or "SC")
        reviewed_route = _routes_by_selection(routing).get(
            (recommended_name, recommended_prefix, recommended_tags)
        )
        if reviewed_route is None:
            return None, "Flash review selected an unavailable Clockify project/task type"
        return _apply_prefix_override(reviewed_route, cited_events, routing), None
    named = _routes_by_name(routing).get(recommended_name) if recommended_name else None
    route = deterministic or named
    explicit = _route_from_explicit_evidence(cited_events, routing, activity=activity)
    route_is_broad_sc = (
        str((route or {}).get("project_name") or "").casefold().startswith("serenichron")
        and (
            str((route or {}).get("confidence") or "") != "high"
            or not _activity_routing_text(activity)
        )
    )
    if explicit is not None and (route is None or route_is_broad_sc):
        route = explicit
    if route is None:
        return None, "no deterministic Clockify project route"
    if deterministic and recommended_name:
        deterministic_name = str(deterministic.get("project_name") or "").casefold()
        if recommended_name != deterministic_name:
            return None, (
                f"semantic project recommendation conflicts with deterministic route: "
                f"{recommended.get('name')} vs {deterministic.get('project_name')}"
            )
    return _apply_prefix_override(route, cited_events, routing), None


def _unresolved_route() -> tuple[dict[str, Any], dict[str, str]]:
    disposition = "unresolved-routing"
    return (
        {
            "project_name": "",
            "project_suffix": "",
            "tag_suffixes": [],
            "tag_names": [],
            "billable": False,
            "prefix": "SC",
            "routing_disposition": disposition,
        },
        {
            "type": "unresolved_routing",
            "disposition": disposition,
            "reason_code": "no_deterministic_route",
        },
    )


_MEETING_IDENTITY_FIELDS = (
    "canonical_meeting_id",
    "cross_provider_meeting_id",
    "meeting_id",
    "calendar_event_id",
)


def _normalized_meeting_title(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def _participant_identity_set(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    identities = []
    for person in value:
        if not isinstance(person, Mapping):
            continue
        for field in ("email", "id", "name"):
            identity = str(person.get(field) or "").strip().casefold()
            if identity:
                identities.append(f"{field}:{identity}")
                break
    return tuple(sorted(set(identities)))


def _derived_meeting_identity(
    start: dt.datetime,
    end: dt.datetime,
    title: Any,
    participants: Any,
) -> str | None:
    normalized_title = _normalized_meeting_title(title)
    participant_ids = _participant_identity_set(participants)
    if not normalized_title or not participant_ids:
        return None
    return json.dumps(
        {
            "start": start.astimezone(dt.timezone.utc).isoformat(),
            "end": end.astimezone(dt.timezone.utc).isoformat(),
            "title": normalized_title,
            "participants": participant_ids,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def _existing_meeting_identity_keys(
    event: Mapping[str, Any], start: dt.datetime, end: dt.datetime
) -> list[str]:
    attrs = _attributes(event)
    keys = {
        f"explicit:{identity}"
        for field in _MEETING_IDENTITY_FIELDS
        if (identity := str(attrs.get(field) or "").strip().casefold())
    }
    derived = _derived_meeting_identity(
        start,
        end,
        attrs.get("meeting_title") or attrs.get("title"),
        attrs.get("participants", attrs.get("calendar_invitees")),
    )
    if derived:
        keys.add(f"derived:{derived}")
    return sorted(keys)


def _canonical_meeting_identity_keys(
    meeting: meeting_reconciliation.CanonicalMeeting,
    entry: Mapping[str, Any],
    start: dt.datetime,
    end: dt.datetime,
) -> set[str]:
    keys = {f"explicit:{meeting.canonical_id.casefold()}"}
    for source_id in meeting.source_ids:
        keys.add(f"explicit:{str(source_id).casefold()}")
        if ":" in str(source_id):
            keys.add(f"explicit:{str(source_id).split(':', 1)[1].casefold()}")
    for event in entry.get("events", []):
        attrs = _attributes(event)
        source = event.get("source_ref") if isinstance(event.get("source_ref"), Mapping) else {}
        for field in _MEETING_IDENTITY_FIELDS:
            identity = str(attrs.get(field) or source.get(field) or "").strip().casefold()
            if identity:
                keys.add(f"explicit:{identity}")
    derived = _derived_meeting_identity(
        start, end, meeting.title, list(meeting.participants)
    )
    if derived:
        keys.add(f"derived:{derived}")
    return keys


def _existing_blocks(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks = []
    for event in events:
        if event.get("source_type") != "clockify":
            continue
        start, end = _span(event)
        if not start or not end:
            continue
        attributes = _attributes(event)
        blocks.append(
            {
                "block_id": str(event["evidence_id"]),
                "start": start,
                "end": end,
                "kind": "existing_clockify",
                "project_id_suffix": str(
                    attributes.get("project_id_suffix") or ""
                ),
                "description": str(attributes.get("description") or "").strip(),
                "meeting_identity_keys": _existing_meeting_identity_keys(
                    event, start, end
                ),
                **({"tag_suffixes": list(attributes["tag_suffixes"])}
                   if isinstance(attributes.get("tag_suffixes"), list) else
                   {"tag_suffixes": list(attributes["tag_id_suffixes"])}
                   if isinstance(attributes.get("tag_id_suffixes"), list) else {}),
                **({"billable": attributes["billable"]}
                   if type(attributes.get("billable")) is bool else {}),
            }
        )
    return blocks


def _recording_events(
    events: Iterable[dict[str, Any]],
    manifest: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Bind source evidence to the one canonical meeting it represents."""
    recording_sources = [
        event for event in events if event.get("source_type") in {"fathom", "calendly"}
    ]
    by_source_id = {
        f"{event['source_type']}:{(event.get('source_ref') or {}).get('source_id')}": event
        for event in recording_sources
    }
    reconciliation_sources = meeting_reconciliation.normalize_ledger_recordings(
        recording_sources, manifest
    )
    reconciliation = meeting_reconciliation.reconcile_meetings(
        [event for event in reconciliation_sources if event.get("source_type") == "fathom"],
        [event for event in reconciliation_sources if event.get("source_type") == "calendly"],
        vlad_identities=meeting_reconciliation.manifest_member_identities(manifest),
    )
    result = []
    for meeting in reconciliation.meetings:
        source_events = [by_source_id[source_id] for source_id in meeting.source_ids]
        result.append({
            "meeting": meeting,
            "events": source_events,
            "source_evidence_ids": [str(event["evidence_id"]) for event in source_events],
        })
    exceptions = []
    for exception in reconciliation.exceptions:
        source_evidence_ids = sorted({
            str(by_source_id[source_id]["evidence_id"])
            for source_id in (
                *exception.get("source_ids", []),
                *exception.get("candidate_source_ids", []),
            )
            if source_id in by_source_id
        })
        exceptions.append({
            "reason": str(exception.get("reason") or "canonical reconciliation exception"),
            "source_evidence_ids": source_evidence_ids,
        })
    return result, exceptions


def _meeting_is_eligible(
    event: Mapping[str, Any], member_identities: frozenset[str] = frozenset({"vlad@serenichron.com"}),
) -> tuple[bool, str | None]:
    attrs = _attributes(event)
    start, end = _span(event)
    if not start or not end:
        return False, "invalid_meeting_window"
    if _minutes(start, end) < 5 and not attrs.get("transcript"):
        return False, "short_recording_without_transcript"
    recorded_by = str(attrs.get("recorded_by_email") or "").strip().casefold()
    organizer = attrs.get("organizer")
    if not recorded_by and isinstance(organizer, Mapping):
        recorded_by = str(organizer.get("email") or "").strip().casefold()
    invitees = attrs.get("calendar_invitees", attrs.get("participants"))
    invitee_emails = {
        str(value.get("email") or "").strip().casefold()
        for value in invitees
        if isinstance(value, Mapping)
    } if isinstance(invitees, list) else set()
    member_attended = bool(member_identities.intersection(invitee_emails))
    if recorded_by in member_identities or member_attended:
        return True, None
    if recorded_by:
        return False, "not_vlads_meeting"
    return False, "unknown_meeting_ownership"


def _meeting_precedence(event: Mapping[str, Any]) -> dict[str, Any]:
    """Classify only the evidence needed for deterministic overlap precedence."""
    attrs = _attributes(event)
    title = str(attrs.get("title") or "").strip()
    participants = attrs.get("calendar_invitees", attrs.get("participants"))
    external = any(
        isinstance(person, Mapping) and person.get("is_external") is True
        for person in participants
    ) if isinstance(participants, list) else False
    generic_internal = re.search(
        r"\b(?:daily(?:\s+meet(?:ing)?)?|internal(?:\s+meet(?:ing)?)?)\b",
        title,
        flags=re.IGNORECASE,
    ) is not None
    specific_named = re.search(
        r"\b(?:BNI|Mazilu(?:\s*&\s*Partners)?|client)\b",
        title,
        flags=re.IGNORECASE,
    ) is not None
    if external or specific_named:
        return {"rank": 0, "class": "specific_external_meeting"}
    if generic_internal:
        return {"rank": 2, "class": "generic_internal_meeting"}
    return {"rank": 1, "class": "meeting"}


def _overlap_ratio(
    start: dt.datetime,
    end: dt.datetime,
    other_start: dt.datetime,
    other_end: dt.datetime,
) -> float:
    overlap = max(dt.timedelta(), min(end, other_end) - max(start, other_start))
    duration = end - start
    return overlap.total_seconds() / duration.total_seconds() if duration.total_seconds() else 0.0


def _meeting_matches_existing_block(
    meeting: meeting_reconciliation.CanonicalMeeting,
    entry: Mapping[str, Any],
    start: dt.datetime,
    end: dt.datetime,
    block: Mapping[str, Any],
) -> bool:
    """Match only explicit identity or exact schedule/title/participant identity."""
    canonical_keys = _canonical_meeting_identity_keys(meeting, entry, start, end)
    block_keys = {
        str(value) for value in block.get("meeting_identity_keys", [])
    }
    return bool(canonical_keys & block_keys)


def _canonical_meeting_span(
    meeting: meeting_reconciliation.CanonicalMeeting,
    representative: Mapping[str, Any],
) -> tuple[dt.datetime, dt.datetime]:
    """Use canonical instants while retaining the source's stable display zone."""
    start, end = _parse_dt(meeting.start), _parse_dt(meeting.end)
    source_start, _source_end = _span(representative)
    assert start and end
    if source_start is not None:
        start, end = start.astimezone(source_start.tzinfo), end.astimezone(source_start.tzinfo)
    return start, end


def _meeting_split_candidate(
    meeting: meeting_reconciliation.CanonicalMeeting,
    activity: Mapping[str, Any],
    route: Mapping[str, Any],
    evidence_ids: list[str],
    source_evidence_ids: list[str],
    index: int,
) -> tuple[meeting_reconciliation.MeetingSplit, dt.datetime, dt.datetime]:
    """Turn one timestamped semantic activity into a split-validation input."""
    spans = [
        span for span in activity.get("evidence_spans", [])
        if isinstance(span, Mapping)
        and (
            str(span.get("evidence_id") or "") in source_evidence_ids
            or (
                not span.get("evidence_id")
                and len(evidence_ids) == 1
                and evidence_ids[0] in source_evidence_ids
            )
        )
    ]
    if len(spans) != 1:
        raise meeting_reconciliation.MeetingReconciliationError(
            "meeting split requires exactly one canonical source timestamped evidence span"
        )
    start, end = _parse_dt(spans[0].get("start")), _parse_dt(spans[0].get("end"))
    if start is None or end is None:
        raise meeting_reconciliation.MeetingReconciliationError(
            "meeting split requires timestamped boundary evidence"
        )
    meeting_start = _parse_dt(meeting.start)
    assert meeting_start is not None
    start_offset = int((start - meeting_start).total_seconds())
    end_offset = int((end - meeting_start).total_seconds())
    evidence_id = str(spans[0].get("evidence_id") or evidence_ids[0])
    if evidence_id not in source_evidence_ids:
        raise meeting_reconciliation.MeetingReconciliationError(
            "meeting split boundary must cite a canonical source evidence ID"
        )
    task_name = ", ".join(sorted(str(value) for value in route.get("tag_names", [])))
    return (
        meeting_reconciliation.MeetingSplit(
            canonical_id=meeting.canonical_id,
            index=index,
            start=start.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            end=end.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            route={"project_name": route.get("project_name"), "task_name": task_name},
            evidence_ids=(f"{evidence_id}:{start_offset}-{end_offset}",),
        ),
        start,
        end,
    )


def _workstream_daily_envelopes(
    activities: Iterable[Mapping[str, Any]],
    events_by_id: Mapping[str, dict[str, Any]],
) -> dict[str, dict[str, tuple[dt.datetime, dt.datetime]]]:
    """Bound flexible placement to observed human spans in one workstream.

    Evidence can justify moving an allocation inside the span of related work,
    but unrelated activity elsewhere that day must never widen its capacity.
    """
    grouped: dict[str, dict[str, list[tuple[dt.datetime, dt.datetime]]]] = {}
    for activity in activities:
        workstream_id = str(activity.get("workstream_id") or "")
        if not workstream_id:
            continue
        for evidence_id in activity.get("evidence_ids", []):
            event = events_by_id.get(str(evidence_id))
            if not event:
                continue
            attrs = _attributes(event)
            source_type = str(event.get("source_type") or "")
            role = str(attrs.get("role") or "").lower()
            if source_type.endswith("_event") and role not in {"user", "human"}:
                continue
            if source_type in {"clockify", "fathom"}:
                continue
            start, end = _span(event)
            if not start or not end:
                continue
            grouped.setdefault(workstream_id, {}).setdefault(
                start.date().isoformat(), []
            ).append((start, end))
    return {
        workstream_id: {
            day: (min(start for start, _ in intervals), max(end for _, end in intervals))
            for day, intervals in days.items()
        }
        for workstream_id, days in grouped.items()
    }


def _authoritative_spans(cited_events: list[dict[str, Any]]) -> list[dict[str, str]]:
    spans = []
    for event in cited_events:
        start, end = _span(event)
        if start and end:
            spans.append(
                {
                    "evidence_id": str(event["evidence_id"]),
                    "start": _iso(start),
                    "end": _iso(end),
                }
            )
    return spans


def _allowed_intervals(
    cited_events: list[dict[str, Any]],
    workstream_daily: Mapping[str, tuple[dt.datetime, dt.datetime]],
) -> list[dict[str, str]]:
    days: set[str] = set()
    for event in cited_events:
        start, _ = _span(event)
        if start:
            days.add(start.date().isoformat())
    return [
        {"start": _iso(workstream_daily[day][0]), "end": _iso(workstream_daily[day][1])}
        for day in sorted(days)
        if day in workstream_daily and workstream_daily[day][1] > workstream_daily[day][0]
    ]


def _activity_observed_intervals(
    cited_events: list[dict[str, Any]],
) -> list[dict[str, str]]:
    """Union actual bounds and authoritative per-source point clusters."""
    candidates: list[tuple[dt.datetime, dt.datetime]] = []
    point_groups: dict[tuple[str, str, str, str], list[dt.datetime]] = {}
    hermes_groups: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    for event in cited_events:
        source_type = str(event.get("source_type") or "")
        if (
            source_type in POINT_OBSERVATION_GAP_THRESHOLDS_SECONDS
            and collector.is_injected_session_message(str(_attributes(event).get("content") or ""))
        ):
            # Legacy direct citations cannot regain timing from machine messages.
            continue
        if source_type in {"hermes_db_sessions", "hermes_sessions"}:
            # Hermes session_start/end describe an unattended envelope. Only
            # its timestamped direct-user messages can establish capacity.
            continue
        if source_type in {"hermes_db_sessions_event", "hermes_sessions_event"}:
            source = event.get("source_ref") if isinstance(event.get("source_ref"), Mapping) else {}
            raw = event.get("raw_source_span") if isinstance(event.get("raw_source_span"), Mapping) else {}
            if (
                _attributes(event).get("role") == "user"
                and source.get("machine") and source.get("session_id")
                and source.get("source_type") == source_type.removesuffix("_event")
            ):
                group = (source_type, str(source["machine"]), str(source["session_id"]))
                hermes_groups.setdefault(group, []).append({
                    "role": "user",
                    "kind": str(_attributes(event).get("kind") or "message"),
                    "tool_name": str(_attributes(event).get("tool_name") or ""),
                    "content": str(_attributes(event).get("content") or ""),
                    "timestamp": str(raw.get("timestamp") or event.get("observed_at") or ""),
                })
            continue
        start, end = _observed_span(event)
        if start is None:
            continue
        if end is not None:
            candidates.append((start, end))
            continue
        if source_type not in POINT_OBSERVATION_GAP_THRESHOLDS_SECONDS:
            continue
        source_ref = (
            event.get("source_ref")
            if isinstance(event.get("source_ref"), Mapping)
            else {}
        )
        group = (
            start.date().isoformat(),
            source_type,
            str(source_ref.get("machine") or ""),
            str(source_ref.get("session_id") or ""),
        )
        point_groups.setdefault(group, []).append(start)
    for group in sorted(hermes_groups):
        for interval in collector.hermes_user_observed_intervals(hermes_groups[group]):
            candidates.append((_parse_dt(interval["start"]), _parse_dt(interval["end"])))
    for group in sorted(point_groups):
        points = sorted(set(point_groups[group]))
        threshold = POINT_OBSERVATION_GAP_THRESHOLDS_SECONDS[group[1]]
        cluster: list[dt.datetime] = []
        for point in points:
            if cluster and (point - cluster[-1]).total_seconds() > threshold:
                if len(cluster) >= 2:
                    candidates.append((cluster[0], cluster[-1]))
                cluster = []
            cluster.append(point)
        if len(cluster) >= 2:
            candidates.append((cluster[0], cluster[-1]))
    merged: list[tuple[dt.datetime, dt.datetime]] = []
    for start, end in sorted(candidates):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return [
        # Source observations can differ only by milliseconds. Keep their
        # precision rather than collapsing a positive span to equal seconds.
        {"start": start.isoformat(), "end": end.isoformat()}
        for start, end in merged
    ]


def _session_timing_contexts(
    events: Iterable[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Index bounded human pools separately from atomic outcome citations.

    Pools are source-bound placement capacity, never per-outcome observed spans.
    Mirror the collector's direct-human filter so automated user-role wrappers
    cannot borrow a surrounding genuine-human pool.
    """
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for event in events:
        source_type = str(event.get("source_type") or "")
        source = event.get("source_ref") or {}
        attrs = _attributes(event)
        if (
            source_type not in {"hermes_db_sessions_event", "hermes_sessions_event", "claude_bursts_event"}
            or source.get("source_type") != source_type.removesuffix("_event")
            or not source.get("machine") or not source.get("session_id")
            or attrs.get("role") != "user"
            or attrs.get("kind", "message") != "message" or attrs.get("tool_name")
            or collector.is_injected_session_message(str(attrs.get("content") or ""))
        ):
            continue
        groups.setdefault((source_type, str(source["machine"]), str(source["session_id"])), []).append(event)
    contexts: dict[str, dict[str, Any]] = {}
    for members in groups.values():
        # Use only actual human timestamps, not assistant points or source
        # envelopes. The existing collector helper bounds these exact points
        # by local day and the established idle threshold for each source group.
        for interval in collector.hermes_user_observed_intervals([
            {**_attributes(event), "timestamp": str(
                (event.get("raw_source_span") or {}).get("timestamp")
                or event.get("observed_at") or ""
            )} for event in members
        ]):
            start, end = _parse_dt(interval["start"]), _parse_dt(interval["end"])
            ids = sorted(
                str(event["evidence_id"])
                for event in members
                if (point := _parse_dt(
                    (event.get("raw_source_span") or {}).get("timestamp")
                    or event.get("observed_at")
                )) is not None
                and start <= point <= end
            )
            context = {
                "pool_id": semantic_analyzer.stable_digest("htp-", ids),
                "interval": interval,
                "evidence_ids": ids,
            }
            for evidence_id in ids:
                contexts[evidence_id] = context
    return contexts


def _interval_capacity_minutes(intervals: Iterable[Mapping[str, Any]]) -> int:
    parsed = [
        (_parse_dt(interval.get("start")), _parse_dt(interval.get("end")))
        for interval in intervals
    ]
    valid = sorted((start, end) for start, end in parsed if start and end and end > start)
    merged: list[tuple[dt.datetime, dt.datetime]] = []
    for start, end in valid:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return sum(int((end - start).total_seconds() // 60) for start, end in merged)


def _subtract_intervals(
    intervals: Iterable[tuple[dt.datetime, dt.datetime]],
    exclusions: Iterable[tuple[dt.datetime, dt.datetime]],
) -> list[tuple[dt.datetime, dt.datetime]]:
    """Subtract only this activity's accepted segments from observed intervals."""
    remaining = list(intervals)
    for excluded_start, excluded_end in sorted(exclusions):
        next_remaining: list[tuple[dt.datetime, dt.datetime]] = []
        for start, end in remaining:
            if excluded_end <= start or excluded_start >= end:
                next_remaining.append((start, end))
                continue
            if start < excluded_start:
                next_remaining.append((start, excluded_start))
            if excluded_end < end:
                next_remaining.append((excluded_end, end))
        remaining = next_remaining
    return remaining


def _capacity_recovery_slices(
    demand: work_allocator.ActivityDemand,
    accepted: Iterable[work_allocator.AllocationSegment],
    requested_minutes: int,
) -> list[tuple[dt.datetime, dt.datetime]]:
    """Place review-only residuals inside observed time, excluding own allocations."""
    if requested_minutes <= 0:
        return []
    own_intervals = [
        (segment.start, segment.end)
        for segment in accepted
        if segment.activity_id == demand.activity_id
    ]
    available = _subtract_intervals(demand.allowed_intervals, own_intervals)
    remaining_seconds = requested_minutes * 60
    result: list[tuple[dt.datetime, dt.datetime]] = []
    for start, end in available:
        if remaining_seconds <= 0:
            break
        take_seconds = min(int((end - start).total_seconds()), remaining_seconds)
        if take_seconds < 60:
            continue
        take_seconds -= take_seconds % 60
        recovered_end = start + dt.timedelta(seconds=take_seconds)
        result.append((start, recovered_end))
        remaining_seconds -= take_seconds
    return result


def _refresh_capacity_recovery_warnings(
    proposals: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    recovery_records: list[dict[str, Any]],
) -> None:
    """Rebind one aggregate recovery warning after overlap normalization."""
    surviving_by_activity: dict[str, list[dict[str, Any]]] = {}
    for proposal in proposals:
        provenance = proposal.get("provenance")
        if not (
            isinstance(provenance, Mapping)
            and provenance.get("allocation_capacity_recovery") is True
        ):
            continue
        warnings = proposal.get("review_warnings", [])
        if isinstance(warnings, list):
            proposal["review_warnings"] = [
                warning for warning in warnings
                if not (
                    isinstance(warning, Mapping)
                    and warning.get("type") == "allocation_capacity_recovery"
                )
            ]
        activity_id = str(proposal.get("activity_id") or "")
        surviving_by_activity.setdefault(activity_id, []).append(proposal)

    for record in recovery_records:
        activity_id = str(record["activity_id"])
        survivors = sorted(
            surviving_by_activity.get(activity_id, []),
            key=lambda row: (
                str(row.get("start") or ""),
                str(row.get("end") or ""),
                str(row.get("candidate_key") or ""),
            ),
        )
        recovered_minutes = sum(
            int(proposal.get("duration_seconds") or 0) // 60
            for proposal in survivors
        )
        overlap_credited_minutes = sum(
            int((row.get("credited_overlap_receipt") or {}).get("credited_seconds") or 0) // 60
            for row in skipped
            if row.get("activity_id") == activity_id
            and (row.get("provenance") or {}).get("allocation_capacity_recovery")
        )
        posted_credited_minutes = sum(
            int((row.get("verified_posted_credit") or {}).get("covered_seconds") or 0) // 60
            for row in skipped
            if row.get("activity_id") == activity_id
            and row.get("verification_basis") == "preserved_collection_snapshot"
            and (row.get("verified_posted_credit") or {}).get("allocation_capacity_recovery") is True
        )
        credited_minutes = overlap_credited_minutes + posted_credited_minutes
        residual_minutes = max(
            0,
            int(record["requested_minutes"])
            - int(record["allocator_allocated_minutes"])
            - recovered_minutes
            - credited_minutes,
        )
        record.update({
            "recovered_minutes": recovered_minutes,
            "credited_minutes": credited_minutes,
            "residual_minutes": residual_minutes,
        })
        if not survivors:
            continue
        survivors[0].setdefault("review_warnings", []).append({
            "type": "allocation_capacity_recovery",
            "requested_minutes": int(record["requested_minutes"]),
            "allocator_allocated_minutes": int(record["allocator_allocated_minutes"]),
            "recovered_minutes": recovered_minutes,
            "credited_minutes": credited_minutes,
            "residual_minutes": residual_minutes,
        })


def _proposal(
    activity: Mapping[str, Any],
    route: Mapping[str, Any],
    description: str,
    start: dt.datetime,
    end: dt.datetime,
    evidence_ids: list[str],
    segment: int,
    *,
    review_warnings: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    seconds = int((end - start).total_seconds())
    if seconds <= 0:
        raise WorkAccountingError("proposal bounds must have positive duration")
    activity_id = str(activity["activity_id"])
    review_activity_key = semantic_analyzer.stable_digest(
        "wka-",
        {
            "activity_id": activity_id,
            "workstream_id": str(activity.get("workstream_id") or ""),
            "evidence_ids": sorted(str(value) for value in evidence_ids),
        },
    )
    candidate_key = semantic_analyzer.stable_digest(
        "wks-",
        {
            "review_activity_key": review_activity_key,
            "segment": segment,
            "allocation_mode": ALLOCATION_MODE,
        },
    )
    proposal = {
        "id": f"S{segment:03d}",
        "candidate_key": candidate_key,
        "review_activity_key": review_activity_key,
        "allocation_segment": segment,
        "activity_id": activity_id,
        "workstream_id": activity.get("workstream_id"),
        "start": _iso(start),
        "end": _iso(end),
        "duration_minutes": seconds // 60,
        "duration_seconds": seconds,
        "client_project": route.get("project_name"),
        "clockify_project_suffix": route.get("project_suffix"),
        "tag_suffixes": list(route.get("tag_suffixes", [])),
        "tag_names": list(route.get("tag_names", [])),
        "billable": route.get("billable", True),
        "source": [f"evidence:{value}" for value in evidence_ids],
        "source_label": activity.get("object"),
        "confidence": activity.get("semantic_confidence"),
        "timing_confidence": activity.get("timing_confidence"),
        "description": description,
        "rendered_description": description,
        "rationale": activity.get("split_rationale") or activity.get("merge_rationale"),
        "allocation_mode": ALLOCATION_MODE,
        "effort": activity.get("effort"),
        "review_warnings": copy.deepcopy(review_warnings or []),
        "provenance": {
            "source_type": "semantic_activity",
            "source_session_id": activity_id,
            "source_machine": "cross-machine",
            "burst_start": _iso(start),
            "burst_end": _iso(end),
            "evidence_ids": evidence_ids,
            "analyzer_model": activity.get("analyzer_model"),
            "semantic_reviewer_model": activity.get("semantic_reviewer_model"),
            "semantic_reviewer_revision": activity.get("semantic_reviewer_revision"),
            "review_prompt_version": activity.get("review_prompt_version"),
            "prompt_version": activity.get("prompt_version"),
            "schema_version": activity.get("schema_version"),
        },
    }
    if route.get("routing_disposition") == "unresolved-routing":
        proposal["routing_disposition"] = "unresolved-routing"
    return proposal


def _overlap_warning(
    proposal_start: dt.datetime,
    proposal_end: dt.datetime,
    counterpart: Mapping[str, Any],
    warning_type: str,
) -> dict[str, Any] | None:
    overlap_start = max(proposal_start, counterpart["start"])
    overlap_end = min(proposal_end, counterpart["end"])
    if overlap_end <= overlap_start:
        return None
    warning = {
        "type": warning_type,
        "counterpart_id": str(counterpart["block_id"]),
        "overlap_start": overlap_start.isoformat(),
        "overlap_end": overlap_end.isoformat(),
        "overlap_duration_seconds": int(
            (overlap_end - overlap_start).total_seconds()
        ),
    }
    if counterpart.get("project_id_suffix"):
        warning["counterpart_project_suffix"] = str(
            counterpart["project_id_suffix"]
        )
    return warning


def _credited_overlap_receipt(
    original_start: dt.datetime,
    original_end: dt.datetime,
    warnings: list[dict[str, Any]],
) -> dict[str, Any]:
    overlap_intervals = [
        (_parse_dt(warning["overlap_start"]), _parse_dt(warning["overlap_end"]))
        for warning in warnings
    ]
    merged: list[tuple[dt.datetime, dt.datetime]] = []
    for start, end in sorted(
        (start, end)
        for start, end in overlap_intervals
        if start is not None and end is not None
    ):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return {
        "schema_version": "clockify-overlap-credit/v1",
        "original_start": original_start.isoformat(),
        "original_end": original_end.isoformat(),
        "credited_seconds": sum(
            int((end - start).total_seconds()) for start, end in merged
        ),
        "counterparts": copy.deepcopy(warnings),
    }


def _slice_proposal_around_credits(
    proposal: Mapping[str, Any],
    counterparts: Iterable[Mapping[str, Any]],
    warning_type: str,
    skipped: list[dict[str, Any]],
    *,
    fully_credited_reason: str,
    precedence: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return only uncredited proposal spans and bind an audit receipt."""
    start = _parse_dt(proposal.get("start"))
    end = _parse_dt(proposal.get("end"))
    if start is None or end is None:
        return [dict(proposal)]
    warnings = [
        warning
        for counterpart in counterparts
        if (warning := _overlap_warning(start, end, counterpart, warning_type))
        is not None
    ]
    if not warnings:
        return [dict(proposal)]
    free = _subtract_intervals(
        [(start, end)],
        [
            (_parse_dt(warning["overlap_start"]), _parse_dt(warning["overlap_end"]))
            for warning in warnings
            if _parse_dt(warning["overlap_start"]) is not None
            and _parse_dt(warning["overlap_end"]) is not None
        ],
    )
    receipt = _credited_overlap_receipt(start, end, warnings)
    if precedence is not None:
        receipt["precedence"] = copy.deepcopy(dict(precedence))
    if not free:
        skipped.append({
            "id": str(proposal.get("candidate_key") or proposal.get("id") or ""),
            "candidate_key": str(proposal.get("candidate_key") or ""),
            "review_activity_key": str(proposal.get("review_activity_key") or ""),
            "allocation_segment": int(proposal.get("allocation_segment") or 0),
            "activity_id": str(proposal.get("activity_id") or ""),
            "reason": fully_credited_reason,
            "evidence_ids": list(
                (proposal.get("provenance") or {}).get("evidence_ids", [])
            ),
            "provenance": copy.deepcopy(proposal.get("provenance") or {}),
            "credited_overlap_receipt": receipt,
        })
        return []

    sliced: list[dict[str, Any]] = []
    for index, (free_start, free_end) in enumerate(free):
        row = copy.deepcopy(proposal)
        seconds = int((free_end - free_start).total_seconds())
        row.update({
            "start": _iso(free_start),
            "end": _iso(free_end),
            "duration_minutes": seconds // 60,
            "duration_seconds": seconds,
        })
        row["review_warnings"] = [*row.get("review_warnings", []), *warnings]
        row["provenance"]["burst_start"] = _iso(free_start)
        row["provenance"]["burst_end"] = _iso(free_end)
        row["provenance"]["credited_overlap_receipt"] = copy.deepcopy(receipt)
        if index:
            row["allocation_segment"] = int(proposal["allocation_segment"]) + index
            row["candidate_key"] = semantic_analyzer.stable_digest(
                "wks-",
                {
                    "parent_candidate_key": str(proposal["candidate_key"]),
                    "start": row["start"],
                    "end": row["end"],
                    "allocation_mode": ALLOCATION_MODE,
                },
            )
        sliced.append(row)
    return sliced


def _exact_existing_accomplishment_match(
    proposal: Mapping[str, Any], block: Mapping[str, Any],
    start: dt.datetime | None, end: dt.datetime | None,
) -> bool:
    """Require reciprocal time plus explicit meeting identity or exact work labels."""
    if start is None or end is None:
        return False
    provenance = proposal.get("provenance")
    meeting_id = str(
        provenance.get("canonical_meeting_id") or ""
        if isinstance(provenance, Mapping) else ""
    ).casefold()
    if meeting_id and f"explicit:{meeting_id}" in block.get("meeting_identity_keys", []):
        if start == block.get("start") and end == block.get("end"):
            return True  # Historical exact-full-identity contract is unchanged.
        block_start, block_end = block.get("start"), block.get("end")
        return bool(
            block_start and block_end and start < block_end and block_start < end
            and proposal.get("clockify_project_suffix")
            and proposal["clockify_project_suffix"] == block.get("project_id_suffix")
            and "tag_suffixes" in block
            and sorted(proposal.get("tag_suffixes", [])) == sorted(block["tag_suffixes"])
            and type(block.get("billable")) is bool
            and proposal.get("billable") is block["billable"]
        )
    if start != block.get("start") or end != block.get("end"):
        return False
    project = str(proposal.get("clockify_project_suffix") or "")
    description = str(proposal.get("description") or "").strip()
    return bool(
        project and description
        and project == block.get("project_id_suffix")
        and description == block.get("description")
    )


def _normalize_postable_proposals(
    proposals: list[dict[str, Any]],
    existing: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Credit exact existing work; keep other overlaps visible for review."""
    existing_blocks = [
        block for block in existing if block.get("kind") == "existing_clockify"
    ]
    live_normalized = []
    for proposal in proposals:
        start = _parse_dt(proposal.get("start"))
        end = _parse_dt(proposal.get("end"))
        exact = [
            block for block in existing_blocks
            if _exact_existing_accomplishment_match(proposal, block, start, end)
        ]
        if exact:
            live_normalized.extend(_slice_proposal_around_credits(
                proposal, exact, "existing_clockify_overlap", skipped,
                fully_credited_reason="proposal fully credited to existing Clockify time",
            ))
            continue
        retained = copy.deepcopy(proposal)
        if start is not None and end is not None:
            retained["review_warnings"] = [
                *retained.get("review_warnings", []),
                *(
                    warning
                    for block in existing_blocks
                    if (warning := _overlap_warning(
                        start, end, block, "existing_clockify_overlap"
                    )) is not None
                ),
            ]
        live_normalized.append(retained)

    def priority(row: Mapping[str, Any]) -> tuple[int, str, str]:
        provenance = row.get("provenance") or {}
        meeting_precedence = provenance.get("meeting_precedence")
        return (
            (
                int(meeting_precedence.get("rank", 1))
                if isinstance(meeting_precedence, Mapping)
                else 3
            ),
            str(row.get("start") or ""),
            str(row.get("candidate_key") or ""),
        )

    accepted: list[dict[str, Any]] = []
    for proposal in sorted(live_normalized, key=priority):
        proposal_meeting_id = str(
            (proposal.get("provenance") or {}).get("canonical_meeting_id") or ""
        )
        proposal_is_meeting = bool(proposal_meeting_id)
        blocks = [
            {
                "block_id": str(row.get("candidate_key") or ""),
                "start": _parse_dt(row.get("start")),
                "end": _parse_dt(row.get("end")),
                "project_id_suffix": row.get("clockify_project_suffix"),
                "activity_id": str(row.get("activity_id") or ""),
                "canonical_meeting_id": str(
                    (row.get("provenance") or {}).get("canonical_meeting_id") or ""
                ),
            }
            for row in accepted
        ]
        overlapping = [
            block
            for block in blocks
            if block["start"] is not None and block["end"] is not None
            and _parse_dt(proposal.get("start")) < block["end"]
            and block["start"] < _parse_dt(proposal.get("end"))
        ]
        activity_id = str(proposal.get("activity_id") or "")
        credited = [
            block for block in overlapping
            if (
                proposal_meeting_id
                and proposal_meeting_id == block["canonical_meeting_id"]
            ) or (
                not proposal_meeting_id
                and not block["canonical_meeting_id"]
                and activity_id
                and activity_id == block["activity_id"]
            )
        ]
        distinct = [block for block in overlapping if block not in credited]
        warning_type = (
            "meeting_proposal_overlap"
            if proposal_is_meeting and any(block["canonical_meeting_id"] for block in credited)
            else "review_proposal_overlap"
        )
        winner = credited[0] if credited else None
        winner_precedence = None
        if winner is not None:
            winner_row = next(
                row for row in accepted
                if row.get("candidate_key") == winner["block_id"]
            )
            winner_class = str(
                ((winner_row.get("provenance") or {}).get("meeting_precedence") or {}).get("class")
                or "incidental_activity"
            )
            loser_class = str(
                ((proposal.get("provenance") or {}).get("meeting_precedence") or {}).get("class")
                or "incidental_activity"
            )
            rule = (
                "specific_external_meeting_over_generic_internal_meeting"
                if winner_class == "specific_external_meeting"
                and loser_class == "generic_internal_meeting"
                else "deterministic_precedence_then_stable_key"
            )
            winner_precedence = {
                "rule": rule,
                "winner_candidate_key": str(winner["block_id"]),
                "loser_candidate_key": str(proposal.get("candidate_key") or ""),
            }
        survivors = _slice_proposal_around_credits(
            proposal,
            credited,
            warning_type,
            skipped,
            fully_credited_reason="proposal fully credited to higher-priority review time",
            precedence=winner_precedence,
        )
        for row in survivors:
            start = _parse_dt(row.get("start"))
            end = _parse_dt(row.get("end"))
            if start is not None and end is not None:
                row["review_warnings"] = [
                    *row.get("review_warnings", []),
                    *(
                        warning
                        for block in distinct
                        if (warning := _overlap_warning(start, end, block, "review_proposal_overlap"))
                        is not None
                    ),
                ]
                survivor_block = {
                    "block_id": str(row.get("candidate_key") or ""),
                    "start": start,
                    "end": end,
                    "project_id_suffix": row.get("clockify_project_suffix"),
                }
                for accepted_row, block in zip(accepted, blocks):
                    if block not in distinct or not block["canonical_meeting_id"]:
                        continue
                    warning = _overlap_warning(
                        block["start"], block["end"], survivor_block,
                        "review_proposal_overlap",
                    )
                    if warning is not None and warning not in accepted_row["review_warnings"]:
                        accepted_row["review_warnings"].append(warning)
        accepted.extend(survivors)
    return sorted(
        accepted,
        key=lambda row: (str(row.get("start") or ""), str(row.get("candidate_key") or "")),
    )


def _apply_verified_posted_credits(
    proposals: list[dict[str, Any]],
    existing_blocks: list[dict[str, Any]],
    credits: Iterable[Mapping[str, Any]],
    *, collection_snapshot: Any = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Credit only a fully proved posted accomplishment, never a temporal overlap."""
    def fingerprint(proposal: Mapping[str, Any]) -> str | None:
        provenance = proposal.get("provenance")
        evidence = provenance.get("evidence_ids") if isinstance(provenance, Mapping) else None
        if not isinstance(evidence, list) or not evidence:
            return None
        try:
            return review_corrections.evidence_fingerprint(evidence)
        except review_corrections.ReviewDecisionError:
            return None

    def digest(description: Any) -> str | None:
        if not isinstance(description, str) or not description.strip():
            return None
        return "sha256:" + hashlib.sha256(description.encode("utf-8")).hexdigest()

    def sheet_minutes(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return None
        try:
            number = Decimal(str(value).strip())
        except InvalidOperation:
            return None
        if not number.is_finite() or number <= 0 or number != number.to_integral_value():
            return None
        return int(number)

    def proves(current_segments: list[Mapping[str, Any]], credit: Mapping[str, Any]) -> bool:
        try:
            content = base64.b64decode(credit["prior_proposals_base64"], validate=True)
            if "sha256:" + hashlib.sha256(content).hexdigest() != credit["prior_proposals_sha256"]:
                return False
            prior_proposals = json.loads(content)
            if not isinstance(prior_proposals, list):
                return False
            by_id: dict[str, list[Mapping[str, Any]]] = {}
            for prior in prior_proposals:
                if not isinstance(prior, Mapping):
                    return False
                key = prior.get("review_activity_key")
                segment = prior.get("allocation_segment")
                if not isinstance(key, str) or not key.startswith("wka-") or type(segment) is not int or segment < 1:
                    return False
                by_id.setdefault(f"{key}-s{segment:02d}", []).append(prior)
            total_seconds = 0
            for posted in credit["posted_rows"]:
                sheet = posted["sheet_row"]
                matches = by_id.get(sheet[0], [])
                if len(matches) != 1:
                    return False
                prior = matches[0]
                start, end = _parse_dt(prior.get("start")), _parse_dt(prior.get("end"))
                if start is None or end is None or end <= start:
                    return False
                if (
                    fingerprint(prior) != credit["evidence_fingerprint"]
                    or prior.get("clockify_project_suffix") != credit["project_suffix"]
                    or sheet[1] != start.strftime("%Y-%m-%d %H:%M")
                    or sheet[2] != end.strftime("%Y-%m-%d %H:%M")
                    or sheet_minutes(sheet[3]) != prior.get("duration_minutes")
                    or sheet[4] != prior.get("client_project")
                    or sheet[8] != prior.get("description")
                    or str(sheet[9]).strip().lower() not in {"approved", "posted"}
                    or sheet[11] != credit["sheet_publication_run_id"]
                    or str(sheet[13]).strip().lower() != "posted"
                ):
                    return False
                exact = [
                    block for block in existing_blocks
                    if block.get("kind") == "existing_clockify"
                    and block.get("start") == start
                    and block.get("end") == end
                    and block.get("project_id_suffix") == credit["project_suffix"]
                    and block.get("description") == prior.get("description")
                ]
                if len(exact) != 1 or exact[0].get("block_id") != posted["clockify_block_id"]:
                    return False
                seconds = int((end - start).total_seconds())
                if (
                    seconds <= 0
                    or type(prior.get("duration_minutes")) is not int
                    or seconds // 60 != prior["duration_minutes"]
                    or (
                        "duration_seconds" in prior
                        and (type(prior["duration_seconds"]) is not int or seconds != prior["duration_seconds"])
                    )
                ):
                    return False
                total_seconds += seconds
            current_seconds = [segment.get("duration_seconds") for segment in current_segments]
            return (
                all(type(seconds) is int and seconds > 0 for seconds in current_seconds)
                and total_seconds == sum(current_seconds)
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return False

    credits = list(credits)
    recurring = [raw for raw in credits if raw.get("schema_version") == 2]
    survivors = list(proposals)
    skipped: list[dict[str, Any]] = []
    if recurring and collection_snapshot is not None:
        from scripts import clockify_source_adoptions as adoptions
        try:
            sealed = [review_corrections.validate_verified_posted_credit(raw) for raw in recurring]
            prior_ids = [proof["clockify_entry_id"] for credit in sealed for proof in credit["prior_proofs"]]
            target_ids = [target["proposal"]["candidate_key"] for credit in sealed for target in credit["current_targets"]]
            if len(prior_ids) != len(set(prior_ids)) or len(target_ids) != len(set(target_ids)):
                sealed = []  # Repeated coverage must not cause partial suppression.
            for credit in sealed:
                matched = []
                for target in credit["current_targets"]:
                    rows = [row for row in survivors if row.get("candidate_key") == target["proposal"]["candidate_key"]
                            and adoptions.recurring_proposal_digest(row) == target["proposal_digest"]]
                    if len(rows) != 1:
                        break
                    matched.extend(rows)
                else:
                    for proof in credit["prior_proofs"]:
                        entries = [entry for entry in collection_snapshot.entries if entry.get("id") == proof["clockify_entry_id"]]
                        request = collection_snapshot.manifest["request"]
                        if (len(entries) != 1 or request["workspace_id"] != proof["workspace_id"]
                                or request["user_id"] != proof["member_id"]
                                or not adoptions.current_live_matches(proof["payload"], entries[0], workspace_id=proof["workspace_id"],
                                                                     member_id=proof["member_id"], entry_id=proof["clockify_entry_id"])):
                            break
                    else:
                        for row in matched:
                            survivors.remove(row)
                            covered_seconds = row["duration_seconds"]
                            intersection_receipt = None
                            fully_credited_intersection = False
                            if credit["coverage_kind"] == "source_native_meeting_intersection":
                                counterparts = [{"block_id": proof["clockify_entry_id"],
                                                 "start": _parse_dt(proof["payload"]["start"]),
                                                 "end": _parse_dt(proof["payload"]["end"])}
                                                for proof in credit["prior_proofs"]]
                                sliced = _slice_proposal_around_credits(
                                    row, counterparts, "existing_clockify_overlap", [],
                                    fully_credited_reason="verified previously posted recording interval",
                                )
                                for residual in sliced:
                                    # Native entry IDs are receipt identities, not
                                    # ev-* warning identities. Rebind the original
                                    # ledger-backed warnings to each residual;
                                    # native counterparts stay in the sealed receipt.
                                    residual["review_warnings"] = copy.deepcopy(row.get("review_warnings", []))
                                    rebound = []
                                    for warning in residual["review_warnings"]:
                                        blocks = [block for block in existing_blocks
                                                  if isinstance(warning, dict)
                                                  and block.get("kind") == "existing_clockify"
                                                  and block.get("block_id") == warning.get("counterpart_id")]
                                        if (len(blocks) == 1 and type(warning.get("overlap_duration_seconds")) is int
                                                and warning == _overlap_warning(_parse_dt(row["start"]),
                                                    _parse_dt(row["end"]), blocks[0], "existing_clockify_overlap")):
                                            warning = _overlap_warning(_parse_dt(residual["start"]),
                                                _parse_dt(residual["end"]), blocks[0], "existing_clockify_overlap")
                                            if warning is None:
                                                continue
                                        # Unrecognized or invalid warnings stay intact,
                                        # so splitting cannot weaken quality validation.
                                        rebound.append(warning)
                                    residual["review_warnings"] = rebound
                                survivors.extend(sliced)
                                fully_credited_intersection = not sliced
                                intersection_receipt = _credited_overlap_receipt(
                                    _parse_dt(row["start"]), _parse_dt(row["end"]),
                                    [warning for block in counterparts if (warning := _overlap_warning(
                                        _parse_dt(row["start"]), _parse_dt(row["end"]), block,
                                        "existing_clockify_overlap")) is not None],
                                )
                                covered_seconds = intersection_receipt["credited_seconds"]
                            skipped.append({"id": row["candidate_key"], "activity_id": row["activity_id"],
                                            "candidate_key": row["candidate_key"], "review_activity_key": row["review_activity_key"],
                                            "allocation_segment": row["allocation_segment"], "evidence_ids": list(row["provenance"]["evidence_ids"]),
                                            "reason": "verified previously posted accomplishment",
                                            "verification_basis": "preserved_collection_snapshot", "operation_anchor": credit["operation_anchor"],
                                            "coverage_kind": credit["coverage_kind"], "credit_digest": credit["credit_digest"],
                                            "collection_snapshot_sha256": collection_snapshot.manifest_sha256,
                                            "verified_posted_credit": {
                                                "covered_seconds": covered_seconds,
                                                "allocation_capacity_recovery": row["provenance"].get("allocation_capacity_recovery") is True,
                                                **({"intersection_receipt": intersection_receipt}
                                                   if intersection_receipt is not None else {}),
                                            },
                                            "clockify_entry_ids": [proof["clockify_entry_id"] for proof in credit["prior_proofs"]],
                                            **({"credited_overlap_receipt": intersection_receipt}
                                               if fully_credited_intersection else {})})
        except (ValueError, TypeError, KeyError, AttributeError):
            # An invalid recurring group must never hide a reviewable proposal.
            survivors, skipped = list(proposals), []
    for raw in credits:
        if raw.get("schema_version") == 2:
            continue
        try:
            credit = review_corrections.validate_verified_posted_credit(raw)
        except review_corrections.ReviewDecisionError:
            continue
        matches = [
            proposal for proposal in survivors
            if fingerprint(proposal) == credit["evidence_fingerprint"]
            and proposal.get("clockify_project_suffix") == credit["project_suffix"]
            and digest(proposal.get("description")) == credit["current_description_sha256"]
        ]
        activity_ids = {str(row.get("activity_id") or "") for row in matches}
        review_keys = {str(row.get("review_activity_key") or "") for row in matches}
        segments = [row.get("allocation_segment") for row in matches]
        candidate_keys = [str(row.get("candidate_key") or "") for row in matches]
        if (
            not matches
            or len(activity_ids) != 1 or "" in activity_ids
            or len(review_keys) != 1 or "" in review_keys
            or any(type(segment) is not int or segment < 1 for segment in segments)
            or len(set(segments)) != len(segments)
            or "" in candidate_keys or len(set(candidate_keys)) != len(candidate_keys)
            or not proves(matches, credit)
        ):
            continue
        for current in matches:
            survivors.remove(current)
            skipped.append({
                "id": str(current.get("candidate_key") or current.get("id") or ""),
                "activity_id": current.get("activity_id"),
                "candidate_key": current.get("candidate_key"),
                "review_activity_key": current.get("review_activity_key"),
                "allocation_segment": current.get("allocation_segment"),
                "reason": "verified previously posted accomplishment",
                "evidence_fingerprint": credit["evidence_fingerprint"],
                "evidence_ids": list((current.get("provenance") or {}).get("evidence_ids", [])),
                "project_suffix": credit["project_suffix"],
                "prior_run_id": credit["prior_run_id"],
                "sheet_publication_run_id": credit["sheet_publication_run_id"],
                "publication_artifact_provenance": (
                    "matching_publication_run_snapshot"
                    if credit["prior_run_id"] == credit["sheet_publication_run_id"]
                    else "later_matching_snapshot_original_not_reconstructed"
                ),
                "prior_proposals_sha256": credit["prior_proposals_sha256"],
                "posted_review_ids": [entry["sheet_row"][0] for entry in credit["posted_rows"]],
                "clockify_block_ids": [entry["clockify_block_id"] for entry in credit["posted_rows"]],
            })
    return survivors, skipped


def _accounting_collection_snapshot(run_dir: Path, events: list[dict[str, Any]]) -> Any:
    """Load active-run preserved proof, binding its projection to this ledger.

    Accounting runs before completion sealing. Never substitute a live GET or
    infer native approved fields from the sanitized ledger. Absent or invalid
    proof leaves every recurring target reviewable.
    """
    from scripts import clockify_checkpoint_snapshot, collector_receipts
    try:
        report_path = collector_receipts._safe_path(run_dir / "run-report.json", run_dir=run_dir)
        report = json.loads(collector_receipts._safe_read_bytes_and_digest(report_path)[0])
        metadata = report.get("clockify_native_checkpoint")
        if not isinstance(metadata, Mapping) or set(metadata) != {"manifest_sha256", "request"}:
            return None
        request = metadata["request"]
        if not isinstance(request, Mapping) or set(request) != {"workspace_id", "user_id", "since_utc", "until_utc"}:
            return None
        since, until = _parse_dt(report["date_range"]["since"]), _parse_dt(report["date_range"]["until"])
        if since is None or until is None or (collector.iso_utc(since), collector.iso_utc(until)) != (request["since_utc"], request["until_utc"]):
            return None
        snapshot = clockify_checkpoint_snapshot.load_checkpoint_snapshot(
            run_dir / collector_receipts.NATIVE_CHECKPOINT_PREFIX,
            workspace_id=request["workspace_id"], user_id=request["user_id"], since=since, until=until,
            expected_manifest_sha256=metadata["manifest_sha256"],
        )
        if snapshot is None or snapshot.manifest["request"] != request:
            return None
        inventory = {collector_receipts.NATIVE_CHECKPOINT_PREFIX + name for name in snapshot.verified_artifact_bytes}
        if collector_receipts.native_checkpoint_inventory(run_dir) != inventory:
            return None
        evidence_path = collector_receipts._safe_path(run_dir / "evidence/clockify-existing.json", run_dir=run_dir)
        evidence = collector_receipts._safe_read_bytes_and_digest(evidence_path)[0]
        if evidence != snapshot.verified_artifact_bytes["clockify-existing.json"]:
            return None
        normalized = [event.document() for event in evidence_ledger.normalize_collector_snapshot({"clockify": json.loads(evidence)})]
        ledger_clockify = [event for event in events if event.get("source_type") == "clockify"]
        if sorted(normalized, key=lambda event: event["evidence_id"]) != sorted(ledger_clockify, key=lambda event: event["evidence_id"]):
            return None
        return snapshot
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def run_accounting(
    run_dir: Path,
    *,
    root: Path,
    routing_path: Path | None = None,
    analysis_fixture: Path | None = None,
    corrections_path: Path | None = None,
    analyzer_cache_path: Path | None = None,
    analyzer_target_body_bytes: int | None = None,
    analyzer_max_events_per_chunk: int | None = None,
    analyzer_workers: int | None = None,
    failed_review_retry_source: Path | None = None,
    failed_review_retry_digest: str | Sequence[str] | None = None,
) -> dict[str, Any]:
    ledger_path = run_dir / "evidence" / "evidence-ledger.json"
    ledger, all_events = load_ledger(ledger_path)
    completeness = ledger.manifest.document().get("source_completeness", {})
    incomplete = [str(value) for value in completeness.get("incomplete_sources", [])]
    coordinator = os.environ.get(
        "CLOCKIFY_AUTOPILOT_COORDINATOR", "omarchy-precision"
    ).strip()
    required = {"clockify", "fathom", "multica_issues"}
    hard_missing = [
        source
        for source in incomplete
        if source in required
        or source in {f"sessions/{coordinator}", f"repositories/{coordinator}"}
        or not source.startswith(("sessions/", "repositories/"))
    ]
    if hard_missing:
        missing = ", ".join(hard_missing) or "unknown"
        raise WorkAccountingError(
            f"required evidence is incomplete; semantic accounting is blocked: {missing}"
        )
    try:
        member_identities = meeting_reconciliation.manifest_member_identities(
            ledger.manifest.document()
        )
    except meeting_reconciliation.MeetingReconciliationError as exc:
        raise WorkAccountingError(f"ledger member identities are invalid: {exc}") from exc
    analysis_events, noise = _analysis_events(all_events, member_identities)
    routing = _read_json(routing_path or (root / "routing.json"))
    corrections = _load_corrections(corrections_path)
    regression_cases = _load_regression_cases(corrections_path)
    verified_posted_credits = _load_verified_posted_credits(corrections_path)
    _write_json(run_dir / "review-learning-cases.json", corrections)
    _write_json(run_dir / "review-regression-cases.json", regression_cases)
    analysis = analyze_ledger(
        analysis_events,
        analysis_fixture=analysis_fixture,
        corrections=corrections,
        analyzer_cache_path=analyzer_cache_path,
        analyzer_target_body_bytes=analyzer_target_body_bytes,
        analyzer_max_events_per_chunk=analyzer_max_events_per_chunk,
        analyzer_workers=analyzer_workers,
        review_taxonomy=_semantic_review_taxonomy(routing),
        review_routing=routing,
        failed_review_retry_source=failed_review_retry_source,
        failed_review_retry_digest=failed_review_retry_digest,
    )
    if analysis_fixture is None and analyzer_cache_path is not None:
        analysis["analyzer_cache"]["snapshot"] = _seal_analyzer_cache_snapshot(
            run_dir, analyzer_cache_path, analysis
        )
    analysis.setdefault("ledger_event_count", len(analysis_events))
    analysis.setdefault("ledger_evidence_digest", semantic_analyzer.stable_digest(
        "led-", sorted(event["evidence_id"] for event in analysis_events)
    ))
    analysis.setdefault("analysis_chunks", [])
    analysis.setdefault(
        "analyzer_cache",
        {
            "schema_version": semantic_analyzer.ANALYZER_CACHE_SCHEMA_VERSION,
            "status": "disabled",
        },
    )
    analysis["noise_classifications"] = noise
    _write_json(run_dir / "semantic-analysis.json", analysis)

    events_by_id = {str(event["evidence_id"]): event for event in all_events}
    existing = _existing_blocks(all_events)
    try:
        recordings, recording_exceptions = _recording_events(
            all_events, ledger.manifest.document()
        )
    except meeting_reconciliation.MeetingReconciliationError as exc:
        raise WorkAccountingError(f"canonical meeting reconciliation failed: {exc}") from exc
    quarantined_evidence_ids = {
        evidence_id
        for exception in recording_exceptions
        for evidence_id in exception["source_evidence_ids"]
    }
    recordings_by_id = {
        entry["meeting"].canonical_id: entry for entry in recordings
    }
    recording_by_evidence_id = {
        evidence_id: entry
        for entry in recordings
        for evidence_id in entry["source_evidence_ids"]
    }
    eligible_recordings = []
    fathom_manifest: dict[str, dict[str, Any]] = {}
    for entry in recordings:
        meeting = entry["meeting"]
        source_events = entry["events"]
        representative = next(
            (event for event in source_events if event.get("source_type") == "fathom"),
            source_events[0],
        )
        eligible, exclusion = _meeting_is_eligible(representative, member_identities)
        meeting_id = meeting.canonical_id
        manifest_base = {
            "canonical_id": meeting_id,
            "source_evidence_ids": entry["source_evidence_ids"],
        }
        if any(evidence_id in quarantined_evidence_ids for evidence_id in entry["source_evidence_ids"]):
            fathom_manifest[meeting_id] = {
                **manifest_base,
                "status": "exception",
                "reason": "canonical_reconciliation_exception",
            }
        elif eligible:
            eligible_recordings.append(entry)
            if _attributes(representative).get("semantic_evidence_status") == "title_only":
                fathom_manifest[meeting_id] = {
                    **manifest_base,
                    "status": "exception",
                    "reason": "title_only",
                }
            else:
                fathom_manifest[meeting_id] = {**manifest_base, "status": "unresolved"}
        else:
            fathom_manifest[meeting_id] = {
                **manifest_base, "status": "excluded", "reason": exclusion
            }

    fixed = list(existing)
    meeting_overlap_blocks: dict[str, list[dict[str, Any]]] = {}
    for entry in eligible_recordings:
        meeting = entry["meeting"]
        representative = next(
            (event for event in entry["events"] if event.get("source_type") == "fathom"),
            entry["events"][0],
        )
        start, end = _canonical_meeting_span(meeting, representative)
        meeting_id = meeting.canonical_id
        overlapping_blocks = [
            block
            for block in fixed
            if _overlap_ratio(start, end, block["start"], block["end"]) > 0
        ]
        matching_existing = [
            block
            for block in overlapping_blocks
            if block["kind"] == "existing_clockify"
            and _meeting_matches_existing_block(meeting, entry, start, end, block)
        ]
        if (
            len(matching_existing) == 1
            and matching_existing[0]["start"] <= start
            and end <= matching_existing[0]["end"]
        ):
            matching_block = matching_existing[0]
            unrelated_blocks = [
                block for block in overlapping_blocks if block is not matching_block
            ]
            fixed.append({
                "block_id": meeting_id,
                "start": start,
                "end": end,
                "kind": "fathom_meeting",
            })
            fathom_manifest[meeting_id].update({
                "status": "reconciled",
                "reason": "existing_clockify_meeting_match",
                "fixed_block_ids": [matching_block["block_id"], meeting_id],
            })
            if unrelated_blocks:
                fathom_manifest[meeting_id]["overlap_diagnostics"] = [
                    {
                        "type": "existing_clockify_overlap",
                        "counterpart_id": str(block["block_id"]),
                        **({
                            "counterpart_project_suffix": str(
                                block.get("project_id_suffix")
                            ),
                        } if block.get("project_id_suffix") else {}),
                        "overlap_start": _iso(max(start, block["start"])),
                        "overlap_end": _iso(min(end, block["end"])),
                        "overlap_duration_seconds": int((
                            min(end, block["end"]) - max(start, block["start"])
                        ).total_seconds()),
                    }
                    for block in unrelated_blocks
                ]
            continue
        fixed.append({
            "block_id": meeting_id,
            "start": start,
            "end": end,
            "kind": "fathom_meeting",
        })
        if overlapping_blocks:
            block_ids = [str(block["block_id"]) for block in overlapping_blocks]
            fathom_manifest[meeting_id].update({
                "fixed_block_ids": block_ids,
            })
            meeting_overlap_blocks[meeting_id] = overlapping_blocks
            continue
        fathom_manifest[meeting_id].update({"fixed_block_ids": [meeting_id]})

    workstream_envelopes = _workstream_daily_envelopes(
        analysis.get("activities", []), events_by_id
    )
    session_timing_contexts = _session_timing_contexts(analysis_events)
    shared_timing_pool_ids: set[str] = set()
    activity_context: dict[str, dict[str, Any]] = {}
    allocation_demands = []
    ambiguous: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    meeting_proposals: list[dict[str, Any]] = []
    correction_observations: list[dict[str, Any]] = []
    meeting_activities: dict[str, list[dict[str, Any]]] = {}
    meeting_attempts: dict[str, list[dict[str, Any]]] = {}

    ambiguous.extend({
        "id": exception["source_evidence_ids"][0] if exception["source_evidence_ids"] else "canonical-reconciliation",
        "reason": exception["reason"],
        "exception_kind": "canonical_meeting_reconciliation",
        "evidence_ids": exception["source_evidence_ids"],
    } for exception in recording_exceptions)

    for meeting_id, status in fathom_manifest.items():
        if status.get("status") == "exception" and status.get("reason") == "title_only":
            ambiguous.append({
                "id": meeting_id,
                "reason": "Fathom meeting lacks transcript, summary, or action items",
                "exception_kind": "insufficient_meeting_evidence",
                "evidence_ids": status["source_evidence_ids"],
            })

    for exception in analysis.get("exceptions", []):
        ambiguous.append({
            "id": semantic_analyzer.stable_digest("semx-", exception),
            "reason": exception.get("reason") or "semantic analyzer exception",
            "exception_kind": exception.get("kind") or "semantic_exception",
            "evidence_ids": list(exception.get("evidence_ids", [])),
        })
    for omission in analysis.get("omissions", []):
        skipped.append({
            "id": semantic_analyzer.stable_digest("omit-", omission),
            "reason": omission.get("reason") or "semantic analyzer omission",
            "lifecycle": omission.get("lifecycle"),
            "evidence_ids": list(omission.get("evidence_ids", [])),
        })

    for activity in analysis.get("activities", []):
        activity_id = str(activity.get("activity_id") or "")
        evidence_ids = [str(value) for value in activity.get("evidence_ids", [])]
        cited = [events_by_id[value] for value in evidence_ids if value in events_by_id]
        if len(cited) != len(evidence_ids):
            ambiguous.append({"id": activity_id, "reason": "activity cites missing evidence", "exception_kind": "invalid_evidence"})
            continue
        lifecycle = str(activity.get("lifecycle") or "")
        if lifecycle in {"planned", "noise"}:
            skipped.append({"id": activity_id, "reason": f"semantic lifecycle: {lifecycle}", "evidence_ids": evidence_ids})
            continue
        meeting_events = [
            event for event in cited if event.get("source_type") in {"fathom", "calendly"}
        ]
        canonical_ids = {
            entry["meeting"].canonical_id
            for event in meeting_events
            if (entry := recording_by_evidence_id.get(str(event["evidence_id"])))
        }
        meeting_id = next(iter(canonical_ids)) if len(canonical_ids) == 1 else None
        attempt = None
        if lifecycle == "meeting" or meeting_events:
            if meeting_id is None:
                ambiguous.append({"id": activity_id, "reason": "meeting activity requires exactly one canonical recording", "exception_kind": "meeting_evidence", "evidence_ids": evidence_ids})
                continue
            attempt = {"activity_id": activity_id, "evidence_ids": evidence_ids, "failures": []}
            meeting_attempts.setdefault(meeting_id, []).append(attempt)
        corrected_route = _route_from_review_correction(
            activity, regression_cases, routing
        )
        if corrected_route is not None:
            route, route_error = (
                _apply_prefix_override(corrected_route, cited, routing),
                None,
            )
        else:
            route, route_error = resolve_route(activity, cited, routing)
        routing_warnings: list[dict[str, Any]] = []
        if route_error or route is None:
            route, warning = _unresolved_route()
            routing_warnings.append(warning)
        corrected_description = _description_from_review_correction(activity, regression_cases)
        if corrected_description is not None:
            description = corrected_description
        elif activity.get("semantic_reviewer_model"):
            # The independent Flash reviewer owns semantic clarity and useful
            # wording. Python only assembles its reviewed fields with the
            # authoritative route prefix; it does not overrule the review with
            # grammar heuristics.
            description = (
                f"{str(route.get('prefix') or 'SC')} — "
                f"{str(activity.get('action') or '')} "
                f"{str(activity.get('object') or '')} "
                f"{str(activity.get('outcome') or '')}"
            ).strip()
        else:
            rendered = caveman_renderer.try_render(
                {
                    "prefix": str(route.get("prefix") or "SC"),
                    "action": activity.get("action"),
                    "object": activity.get("object"),
                    "outcome": activity.get("outcome"),
                }
            )
            if not rendered.ok:
                if attempt is not None:
                    attempt["failures"].append(str(rendered.error))
                else:
                    ambiguous.append({
                        "id": activity_id,
                        "reason": str(rendered.error),
                        "exception_kind": "description_contract",
                        "evidence_ids": evidence_ids,
                    })
                continue
            description = str(rendered.description)
        activity["rendered_description"] = description
        if attempt is not None:
            assert meeting_id is not None
            entry = recordings_by_id[meeting_id]
            meeting = entry["meeting"]
            representative = next(
                (event for event in entry["events"] if event.get("source_type") == "fathom"),
                entry["events"][0],
            )
            attrs = _attributes(representative)
            if attrs.get("semantic_evidence_status") == "title_only":
                ambiguous.append({"id": activity_id, "reason": "title-only Fathom evidence cannot support a meeting outcome", "exception_kind": "insufficient_meeting_evidence", "evidence_ids": evidence_ids})
                fathom_manifest[meeting_id].update({"status": "exception", "reason": "title_only"})
                attempt["failures"].append("title-only meeting evidence")
                continue
            start, end = _canonical_meeting_span(meeting, representative)
            meeting_status = fathom_manifest.get(meeting_id, {}).get("status")
            if meeting_status == "reconciled":
                skipped.append({"id": activity_id, "reason": "meeting already reconciled by existing Clockify entry", "evidence_ids": evidence_ids})
                attempt["failures"].append("meeting already reconciled")
                continue
            if meeting_status == "exception":
                skipped.append({"id": activity_id, "reason": "meeting has a fixed-block conflict", "evidence_ids": evidence_ids})
                attempt["failures"].append("meeting has a fixed-block conflict")
                continue
            meeting_activities.setdefault(meeting_id, []).append({
                "activity": activity,
                "route": route,
                "description": description,
                "evidence_ids": evidence_ids,
                "entry": entry,
                "review_warnings": routing_warnings,
            })
            attempt["candidate"] = True
            continue

        timing_contexts = {}
        for event in cited:
            evidence_id = str(event["evidence_id"])
            if evidence_id not in session_timing_contexts:
                continue
            if event.get("source_type") == "claude_bursts_event" and not any(
                other.get("source_type") == "claude_bursts_event"
                and semantic_analyzer._semantic_context_key(other) == semantic_analyzer._semantic_context_key(event)
                and _attributes(other).get("role") == "assistant"
                and _attributes(other).get("kind", "message") == "message"
                and not _attributes(other).get("tool_name")
                and str(_attributes(other).get("content") or "").strip()
                for other in cited
            ):
                # Historical accepted user-only reports do not become new
                # work merely because surrounding human activity was observed.
                continue
            context = session_timing_contexts[evidence_id]
            timing_contexts[context["pool_id"]] = context
        intervals = _activity_observed_intervals(cited)
        borrowed_timing_context = not intervals and bool(timing_contexts)
        if borrowed_timing_context:
            intervals = sorted(
                (dict(context["interval"]) for context in timing_contexts.values()),
                key=lambda interval: (interval["start"], interval["end"]),
            )
            shared_timing_pool_ids.update(timing_contexts)
        observed_capacity = _interval_capacity_minutes(intervals)
        if not intervals or observed_capacity == 0:
            reason = (
                "cited evidence has timestamps but no positive observed interval"
                if not intervals else
                "cited evidence has no whole-minute observed capacity"
            )
            ambiguous.append({"id": activity_id, "reason": reason, "exception_kind": "timing_evidence", "evidence_ids": evidence_ids})
            if corrected_route is not None:
                correction_observations.append({
                    "activity_id": activity_id,
                    "client_project": route.get("project_name"),
                    "tag_names": list(route.get("tag_names", [])),
                    "description": description,
                    "provenance": {"evidence_ids": evidence_ids},
                })
            continue
        # Shared human pool bounds must not be relabelled as observed spans of
        # an arbitrary outcome evidence ID. Allowed intervals own placement.
        spans = [] if borrowed_timing_context else [
            {
                "evidence_id": evidence_ids[min(index, len(evidence_ids) - 1)],
                "start": interval["start"],
                "end": interval["end"],
            }
            for index, interval in enumerate(intervals)
        ]
        requested_effort = dict(activity.get("effort") or {})
        requested_minutes = int(requested_effort.get("recommended_minutes") or 0)
        review_warnings: list[dict[str, Any]] = list(routing_warnings)
        demand_effort = requested_effort
        if requested_minutes > observed_capacity:
            demand_effort = {
                "minimum_minutes": observed_capacity,
                "recommended_minutes": observed_capacity,
                "maximum_minutes": observed_capacity,
            }
            review_warnings.append({
                "type": "observed_capacity_cap",
                "requested_minutes": requested_minutes,
                "observed_capacity_minutes": observed_capacity,
                "proposed_minutes": observed_capacity,
            })
        demand = {
            **activity,
            "evidence_spans": spans,
            "allowed_intervals": intervals,
            "effort": demand_effort,
            "confidence": activity.get("semantic_confidence"),
            "attention_signal": sum(2 if str(_attributes(event).get("role")).lower() == "user" else 1 for event in cited),
        }
        allocation_demands.append(demand)
        activity_context[activity_id] = {
            "activity": activity,
            "route": route,
            "description": description,
            "evidence_ids": evidence_ids,
            "review_warnings": review_warnings,
            "session_timing_contexts": timing_contexts,
        }

    for meeting_id, attempts in meeting_attempts.items():
        failures = sorted({
            failure for attempt in attempts for failure in attempt["failures"]
        })
        if len(attempts) <= 1 or not failures:
            continue
        if fathom_manifest[meeting_id].get("status") != "unresolved":
            continue
        meeting_activities.pop(meeting_id, None)
        fathom_manifest[meeting_id].update({
            "status": "exception", "reason": "invalid_meeting_split",
        })
        ambiguous.append({
            "id": meeting_id,
            "reason": "; ".join(failures),
            "exception_kind": "invalid_meeting_split",
            "evidence_ids": fathom_manifest[meeting_id]["source_evidence_ids"],
        })

    # Attendance is a source fact, not an inferred outcome. Preserve an eligible
    # canonical recording when semantic extraction/review supplied no usable
    # activity, without adding synthetic rows to the analyzer artifact. Existing
    # quarantines and rejected multi-part splits remain exceptions.
    for entry in eligible_recordings:
        meeting = entry["meeting"]
        meeting_id = meeting.canonical_id
        if meeting_id in meeting_activities or fathom_manifest[meeting_id]["status"] != "unresolved":
            continue
        representative = next(
            (event for event in entry["events"] if event.get("source_type") == "fathom"),
            entry["events"][0],
        )
        title = str(meeting.title or "").strip()
        if not title:
            continue
        attendance = {
            "activity_id": semantic_analyzer.stable_digest("act-", {"recorded_attendance": meeting_id}),
            "workstream_id": semantic_analyzer.stable_digest("ws-", {"recorded_meeting": meeting_id}),
            "object": title,
            "effort": {},
            "timing_confidence": "high",
            "split_rationale": "Authoritative canonical recording interval; factual attendance only.",
            "evidence_ids": entry["source_evidence_ids"],
        }
        corrected_route = _route_from_review_correction(attendance, regression_cases, routing)
        if corrected_route is not None:
            route, route_error = _apply_prefix_override(corrected_route, entry["events"], routing), None
        else:
            route, route_error = resolve_route(attendance, entry["events"], routing)
        warnings = [{
            "type": "semantic_meeting_fallback",
            "reason": "No usable semantic activity; recorded attendance only, no outcome inferred.",
        }]
        if route_error or route is None:
            route, warning = _unresolved_route()
            warnings.append(warning)
        start, end = _canonical_meeting_span(meeting, representative)
        proposal = _proposal(
            attendance,
            route, f"{route.get('prefix') or 'SC'} — Attended {title}",
            start, end, entry["source_evidence_ids"], 1,
            review_warnings=warnings,
        )
        proposal["provenance"] = {
            "source_type": "recorded_meeting",
            "source_session_id": meeting_id,
            "source_machine": "cross-machine",
            "burst_start": _iso(start),
            "burst_end": _iso(end),
            "evidence_ids": entry["source_evidence_ids"],
            "canonical_meeting_id": meeting_id,
            "recorded_meeting_title": title,
            "recorded_meeting_start": _iso(start),
            "recorded_meeting_end": _iso(end),
            "meeting_precedence": _meeting_precedence(representative),
            "semantic_fallback": True,
        }
        meeting_proposals.append(proposal)
        fathom_manifest[meeting_id].update({
            "status": "proposed", "activity_id": proposal["activity_id"],
            "semantic_fallback": True,
        })

    for meeting_id, candidates in meeting_activities.items():
        entry = recordings_by_id[meeting_id]
        meeting = entry["meeting"]
        representative = next(
            (event for event in entry["events"] if event.get("source_type") == "fathom"),
            entry["events"][0],
        )
        if len(candidates) == 1:
            candidate = candidates[0]
            start, end = _canonical_meeting_span(meeting, representative)
            proposal = _proposal(
                candidate["activity"], candidate["route"], candidate["description"],
                start, end, entry["source_evidence_ids"], 1,
                review_warnings=candidate["review_warnings"],
            )
            proposal["provenance"]["canonical_meeting_id"] = meeting_id
            proposal["provenance"]["meeting_precedence"] = _meeting_precedence(
                representative
            )
            meeting_proposals.append(proposal)
            fathom_manifest[meeting_id].update({
                "status": "proposed", "activity_id": str(candidate["activity"].get("activity_id") or ""),
            })
            continue
        try:
            split_candidates = [
                (candidate, *_meeting_split_candidate(
                    meeting, candidate["activity"], candidate["route"],
                    candidate["evidence_ids"], entry["source_evidence_ids"], index,
                ))
                for index, candidate in enumerate(candidates)
            ]
            split_candidates.sort(key=lambda value: value[1].start)
            splits = tuple(
                dataclasses.replace(value[1], index=index)
                for index, value in enumerate(split_candidates)
            )
            validated = meeting_reconciliation.validate_meeting_splits(meeting, splits)
        except meeting_reconciliation.MeetingReconciliationError as exc:
            fathom_manifest[meeting_id].update({"status": "exception", "reason": "invalid_meeting_split"})
            ambiguous.append({
                "id": meeting_id,
                "reason": str(exc),
                "exception_kind": "invalid_meeting_split",
                "evidence_ids": entry["source_evidence_ids"],
            })
            continue
        for segment, (candidate, _split, start, end) in zip(validated, split_candidates):
            proposal = _proposal(
                candidate["activity"], candidate["route"], candidate["description"],
                start, end, candidate["evidence_ids"], segment.index + 1,
                review_warnings=candidate["review_warnings"],
            )
            proposal["provenance"]["canonical_meeting_id"] = meeting_id
            proposal["provenance"]["meeting_precedence"] = _meeting_precedence(
                representative
            )
            proposal["provenance"]["timestamped_split_evidence_ids"] = list(segment.evidence_ids)
            meeting_proposals.append(proposal)
        fathom_manifest[meeting_id].update({
            "status": "proposed",
            "activity_ids": [str(candidate["activity"].get("activity_id") or "") for candidate in candidates],
        })

    for context in activity_context.values():
        pools = context["session_timing_contexts"]
        if not shared_timing_pool_ids.intersection(pools):
            continue
        context["shared_timing_context"] = {
            "timing_context_evidence_ids": sorted({
                value for pool in pools.values() for value in pool["evidence_ids"]
            }),
            "timing_context_intervals": [pools[key]["interval"] for key in sorted(pools)],
            "timing_placement": "estimated",
        }
        context["review_warnings"].append({
            "type": "estimated_session_placement",
            "reason": "Estimated effort placed within shared same-session human observations; exact outcome boundaries are not observed.",
        })
    allocation = work_allocator.allocate_work(allocation_demands, fixed)
    proposals = list(meeting_proposals)
    activity_segment_counts: dict[str, int] = {}
    for segment in allocation.allocations:
        context = activity_context[segment.activity_id]
        activity_segment_counts[segment.activity_id] = activity_segment_counts.get(segment.activity_id, 0) + 1
        proposal = _proposal(
            context["activity"],
            context["route"],
            context["description"],
            segment.start,
            segment.end,
            context["evidence_ids"],
            activity_segment_counts[segment.activity_id],
            review_warnings=context["review_warnings"],
        )
        proposal["provenance"].update(copy.deepcopy(context.get("shared_timing_context") or {}))
        proposals.append(proposal)

    demands_by_activity = {
        demand.activity_id: demand for demand in allocation.evidence
    }
    recovery_proposal_ids: set[str] = set()
    recovery_identities = {
        (
            str(proposal.get("activity_id") or ""),
            str(proposal.get("start") or ""),
            str(proposal.get("end") or ""),
        )
        for proposal in proposals
    }
    recovery_records: list[dict[str, Any]] = []
    residual_conflicts: list[work_allocator.ContestedTime] = []
    for conflict in allocation.contested_time:
        demand = demands_by_activity[conflict.activity_id]
        context = activity_context[conflict.activity_id]
        if context.get("shared_timing_context"):
            # The allocator debits the occupied union once. Recovery deliberately
            # allows distinct observed activity overlap, but cannot re-spend a
            # shared human pool for outcomes with unknown individual boundaries.
            residual_conflicts.append(conflict)
            continue
        slices = _capacity_recovery_slices(
            demand,
            allocation.allocations,
            conflict.unallocated_minutes,
        )
        unique_slices: list[tuple[dt.datetime, dt.datetime]] = []
        for start, end in slices:
            identity = (conflict.activity_id, _iso(start), _iso(end))
            if identity in recovery_identities:
                continue
            recovery_identities.add(identity)
            unique_slices.append((start, end))
        recovered_minutes = sum(
            _minutes(start, end) for start, end in unique_slices
        )
        residual_minutes = max(
            0,
            conflict.requested_minutes
            - conflict.allocated_minutes
            - recovered_minutes,
        )
        if recovered_minutes:
            recovery_warning = {
                "type": "allocation_capacity_recovery",
                "requested_minutes": conflict.requested_minutes,
                "allocator_allocated_minutes": conflict.allocated_minutes,
                "recovered_minutes": recovered_minutes,
                "residual_minutes": residual_minutes,
            }
            for recovery_index, (start, end) in enumerate(unique_slices):
                activity_segment_counts[conflict.activity_id] = (
                    activity_segment_counts.get(conflict.activity_id, 0) + 1
                )
                proposal = _proposal(
                    context["activity"],
                    context["route"],
                    context["description"],
                    start,
                    end,
                    context["evidence_ids"],
                    activity_segment_counts[conflict.activity_id],
                    review_warnings=[
                        *context["review_warnings"],
                        *([recovery_warning] if recovery_index == 0 else []),
                    ],
                )
                proposal["provenance"]["allocation_capacity_recovery"] = True
                proposals.append(proposal)
                recovery_proposal_ids.add(proposal["candidate_key"])
            recovery_records.append({
                "activity_id": conflict.activity_id,
                "requested_minutes": conflict.requested_minutes,
                "allocator_allocated_minutes": conflict.allocated_minutes,
                "recovered_minutes": recovered_minutes,
                "residual_minutes": residual_minutes,
            })
        if residual_minutes:
            residual_conflicts.append(dataclasses.replace(
                conflict,
                allocated_minutes=(
                    conflict.allocated_minutes + recovered_minutes
                ),
                unallocated_minutes=residual_minutes,
            ))
    allocation = dataclasses.replace(
        allocation,
        contested_time=tuple(residual_conflicts),
    )

    proposals = _normalize_postable_proposals(proposals, existing, skipped)
    # Match sealed credits against the same recovery warning shape as finalized proposals.
    _refresh_capacity_recovery_warnings(proposals, skipped, recovery_records)
    collection_snapshot = (_accounting_collection_snapshot(run_dir, all_events)
                           if any(credit.get("schema_version") == 2 for credit in verified_posted_credits) else None)
    proposals, posted_skipped = _apply_verified_posted_credits(
        proposals, existing, verified_posted_credits, collection_snapshot=collection_snapshot
    )
    skipped.extend(posted_skipped)
    _refresh_capacity_recovery_warnings(proposals, skipped, recovery_records)
    proposed_meeting_ids = {
        str((proposal.get("provenance") or {}).get("canonical_meeting_id") or "")
        for proposal in proposals
    }
    for meeting_id, status in fathom_manifest.items():
        if status.get("status") == "proposed" and meeting_id not in proposed_meeting_ids:
            status.update({
                "status": "reconciled",
                "reason": "fully_credited_existing_clockify_overlap",
            })
    for conflict in allocation.contested_time:
        context = activity_context[conflict.activity_id]
        shared_context = context.get("shared_timing_context") or {}
        ambiguous.append({
            "id": conflict.activity_id,
            "activity_id": conflict.activity_id,
            "workstream_id": conflict.workstream_id,
            "reason": (
                "Estimated outcomes share one observed human window; remaining effort is not separately timed."
                if shared_context else conflict.reason
            ),
            "exception_kind": "contested_time",
            "requested_minutes": conflict.requested_minutes,
            "allocated_minutes": conflict.allocated_minutes,
            "unallocated_minutes": conflict.unallocated_minutes,
            **({"evidence_ids": list(context["evidence_ids"]), **copy.deepcopy(shared_context)}
               if shared_context else {}),
        })

    for meeting_id, status in fathom_manifest.items():
        if status["status"] == "unresolved":
            status.update({"status": "exception", "reason": "no semantic meeting activity"})
            ambiguous.append({"id": meeting_id, "reason": "eligible Fathom meeting has no semantic activity", "exception_kind": "missing_meeting_activity", "evidence_ids": status["source_evidence_ids"]})

    proposals.sort(key=lambda value: (value["start"], value["candidate_key"]))
    correction_regression = review_corrections.evaluate_regression_cases(
        regression_cases, [*proposals, *correction_observations]
    )
    failed_targets: dict[tuple[str, str], dict[str, Any]] = {}
    for regression in correction_regression["results"]:
        if regression["status"] != "fail":
            continue
        target = (regression["activity_id"], regression["evidence_fingerprint"])
        existing_failure = failed_targets.get(target)
        if existing_failure is None:
            failed_targets[target] = {
                **regression,
                "failures": list(regression["failures"]),
                "regression_case_ids": [regression["regression_case_id"]],
            }
            continue
        existing_failure["failures"] = sorted(set(
            [*existing_failure["failures"], *regression["failures"]]
        ))
        existing_failure["regression_case_ids"].append(regression["regression_case_id"])
    if failed_targets:
        proposals_by_target: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for proposal in proposals:
            if target := review_corrections.proposal_target(proposal):
                proposals_by_target.setdefault(target, []).append(proposal)
        proposals = [
            proposal for proposal in proposals
            if review_corrections.proposal_target(proposal) not in failed_targets
        ]
        failed_activity_ids = {target[0] for target in failed_targets}
        allocation = _drop_failed_allocations(allocation, failed_activity_ids)
        for target, failure in sorted(failed_targets.items()):
            related = proposals_by_target.get(target, [])
            evidence_ids = sorted({
                str(evidence_id)
                for proposal in related
                for evidence_id in (proposal.get("provenance") or {}).get("evidence_ids", [])
            })
            for proposal in related:
                for evidence_id in (proposal.get("provenance") or {}).get("evidence_ids", []):
                    recording = recording_by_evidence_id.get(str(evidence_id))
                    meeting_key = (
                        recording["meeting"].canonical_id
                        if recording is not None
                        else str(evidence_id)
                    )
                    meeting_status = fathom_manifest.get(meeting_key)
                    if not meeting_status or meeting_status.get("activity_id") != target[0]:
                        continue
                    if failure["decision"] == "skip":
                        meeting_status.update({
                            "status": "excluded",
                            "reason": "review_correction_skip",
                        })
                    else:
                        meeting_status.update({
                            "status": "exception",
                            "reason": "correction_regression",
                        })
            if failure["decision"] == "skip":
                skipped.append({
                    "id": target[0],
                    "reason": "preserved evidence-bound skip decision",
                    "evidence_ids": evidence_ids,
                    "evidence_fingerprint": target[1],
                    "regression_case_ids": sorted(failure["regression_case_ids"]),
                })
            else:
                ambiguous.append({
                    "id": target[0],
                    "activity_id": target[0],
                    "reason": "; ".join(failure["failures"]),
                    "exception_kind": "correction_regression",
                    "evidence_ids": evidence_ids,
                    "evidence_fingerprint": target[1],
                    "regression_case_ids": sorted(failure["regression_case_ids"]),
                })
    for index, proposal in enumerate(proposals, 1):
        proposal["id"] = f"P{index:03d}"
    ambiguous.sort(key=lambda value: (str(value.get("exception_kind")), str(value.get("id"))))
    for index, value in enumerate(ambiguous, 1):
        value.setdefault("activity_id", value.get("id"))
        value["id"] = f"A{index:03d}"

    def serialize(value: Any) -> Any:
        if dataclasses.is_dataclass(value):
            return {key: serialize(item) for key, item in dataclasses.asdict(value).items()}
        if isinstance(value, dt.datetime):
            return _iso(value)
        if isinstance(value, tuple):
            return [serialize(item) for item in value]
        if isinstance(value, list):
            return [serialize(item) for item in value]
        if isinstance(value, dict):
            return {str(key): serialize(item) for key, item in value.items()}
        return value

    serialized_allocation = serialize(allocation)
    serialized_allocation["capacity_recoveries"] = copy.deepcopy(
        recovery_records
    )
    serialized_allocation["deterministic_inputs"] = {
        "point_observation_clustering": copy.deepcopy(
            POINT_OBSERVATION_CLUSTERING_INPUT
        )
    }
    review_tombstones = [
        copy.deepcopy(row)
        for row in skipped
        if isinstance(row.get("credited_overlap_receipt"), Mapping)
    ]
    result = {
        "schema_version": SCHEMA_VERSION,
        "allocation_mode": ALLOCATION_MODE,
        "ledger_manifest": ledger.manifest.document(),
        "member_identities": sorted(member_identities),
        "semantic_analysis": {
            "prompt_version": analysis.get("prompt_version"),
            "activity_count": len(analysis.get("activities", [])),
            "exception_count": len(analysis.get("exceptions", [])),
            "omission_count": len(analysis.get("omissions", [])),
            "noise_count": len(noise),
            "learning_case_count": len(corrections),
        },
        "proposals": proposals,
        "ambiguous": ambiguous,
        "skipped": skipped,
        "review_tombstones": review_tombstones,
        "allocation": serialized_allocation,
        "fathom_reconciliation": [
            {"evidence_id": value["source_evidence_ids"][0], **value}
            for key, value in sorted(fathom_manifest.items())
        ],
        "correction_regression": correction_regression,
        "external_writes": False,
        "coverage_warnings": [
            {
                "source": source,
                "reason": "peer evidence unavailable; interval retained for later backfill",
            }
            for source in incomplete
            if source not in hard_missing
        ],
    }
    # The initial analyzer artifact is written before deterministic routing and
    # rendering so failures remain inspectable. Rewrite it with the final
    # rendered_description values for activities that became proposals.
    _write_json(run_dir / "semantic-analysis.json", analysis)
    _write_json(run_dir / "allocation-report.json", result["allocation"])
    _write_json(run_dir / "fathom-reconciliation.json", result["fathom_reconciliation"])
    _write_json(run_dir / "review-regression-results.json", correction_regression)
    _write_json(run_dir / "proposals.json", proposals)
    _write_json(run_dir / "ambiguous.json", ambiguous)
    _write_json(run_dir / "skipped.json", skipped)
    _write_json(run_dir / "review-tombstones.json", review_tombstones)
    # This is the durable completion marker consumed by the service runner.
    # Publish it only after every required artifact has been atomically replaced.
    _write_json(run_dir / "work-accounting-result.json", result)
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--routing", type=Path)
    parser.add_argument("--analysis-fixture", type=Path)
    parser.add_argument("--corrections", type=Path)
    parser.add_argument("--analyzer-cache", type=Path)
    parser.add_argument("--analyzer-target-body-bytes", type=int)
    parser.add_argument("--analyzer-max-events-per-chunk", type=int)
    parser.add_argument("--analyzer-workers", type=int)
    parser.add_argument("--failed-review-retry-source", type=Path)
    parser.add_argument("--failed-review-retry-digest", action="append")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run_accounting(
            args.run_dir.resolve(),
            root=args.root.resolve(),
            routing_path=args.routing,
            analysis_fixture=args.analysis_fixture,
            corrections_path=args.corrections,
            analyzer_cache_path=args.analyzer_cache,
            analyzer_target_body_bytes=args.analyzer_target_body_bytes,
            analyzer_max_events_per_chunk=args.analyzer_max_events_per_chunk,
            analyzer_workers=args.analyzer_workers,
            failed_review_retry_source=args.failed_review_retry_source,
            failed_review_retry_digest=args.failed_review_retry_digest,
        )
    except (WorkAccountingError, semantic_analyzer.AnalyzerError, work_allocator.AllocationError, ValueError) as exc:
        print(f"work accounting blocked: {exc}", file=sys.stderr)
        return 2
    print((args.run_dir / "work-accounting-result.json").resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
