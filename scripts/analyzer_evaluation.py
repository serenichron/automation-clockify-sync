#!/usr/bin/env python3
"""Offline, content-addressed evaluation for captured semantic-analyzer output.

This module intentionally has no provider configuration or transport code.  It
evaluates redacted captures that were made elsewhere, emits a scorecard that
does not repeat the captured prose, and fails closed when its input corpus or
contract cannot be verified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping

try:
    from scripts import caveman_renderer, semantic_analyzer
except ModuleNotFoundError:
    import caveman_renderer  # type: ignore[no-redef]
    import semantic_analyzer  # type: ignore[no-redef]


INPUT_SCHEMA_VERSION = "clockify-analyzer-evaluation-input/v1"
SCORECARD_SCHEMA_VERSION = "clockify-analyzer-evaluation-scorecard/v1"
EVALUATOR_VERSION = "clockify-analyzer-evaluator/v2"
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ROUTE_FIELDS = frozenset({"route_id", "model", "tier"})
_RESPONSE_FIELDS = frozenset({"activities", "exceptions", "omissions"})


class EvaluationError(ValueError):
    """Raised when a captured evaluation cannot be trusted or completed."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise EvaluationError(f"{label} must be an object")
    return value


def _require_keys(value: Mapping[str, Any], required: set[str], label: str) -> None:
    missing = required - set(value)
    extra = set(value) - required
    if missing or extra:
        raise EvaluationError(f"{label} has unsupported fields")


def _span(value: Any, label: str) -> dict[str, str]:
    raw = _require_mapping(value, label)
    _require_keys(raw, {"start", "end"}, label)
    start = str(raw["start"] or "").strip()
    end = str(raw["end"] or "").strip()
    if not semantic_analyzer.SAFE_TIMESTAMP_RE.fullmatch(start) or not semantic_analyzer.SAFE_TIMESTAMP_RE.fullmatch(end):
        raise EvaluationError(f"{label} has invalid timestamp")
    if not semantic_analyzer._ordered_timestamps(start, end):
        raise EvaluationError(f"{label} ends before it starts")
    return {"start": start, "end": end}


def _corpus(document: Mapping[str, Any]) -> tuple[list[dict[str, Any]], str]:
    raw = _require_mapping(document.get("corpus"), "corpus")
    _require_keys(raw, {"records", "digest"}, "corpus")
    records = raw["records"]
    if not isinstance(records, list) or not records:
        raise EvaluationError("corpus.records must be a non-empty list")
    digest = str(raw["digest"] or "")
    if not _SHA256_RE.fullmatch(digest) or digest != sha256_hex(records):
        raise EvaluationError("corpus digest does not match its records")
    normalized: list[dict[str, Any]] = []
    evidence_ids: set[str] = set()
    for index, record in enumerate(records):
        item = _require_mapping(record, f"corpus.records[{index}]")
        _require_keys(item, {"evidence_id", "time_span"}, f"corpus.records[{index}]")
        evidence_id = str(item["evidence_id"] or "")
        if not evidence_id or evidence_id in evidence_ids:
            raise EvaluationError("corpus evidence IDs must be present and unique")
        evidence_ids.add(evidence_id)
        normalized.append({"evidence_id": evidence_id, "time_span": _span(item["time_span"], "corpus time_span")})
    return normalized, digest


def _route(document: Mapping[str, Any]) -> dict[str, str]:
    raw = _require_mapping(document.get("route"), "route")
    _require_keys(raw, set(_ROUTE_FIELDS), "route")
    route = {key: str(raw[key] or "").strip() for key in _ROUTE_FIELDS}
    if not route["route_id"] or not route["model"] or route["tier"] not in {"primary", "fallback"}:
        raise EvaluationError("route requires route_id, model, and a primary or fallback tier")
    return route


def _validate_document(document: Any) -> tuple[Mapping[str, Any], list[dict[str, Any]], str, dict[str, str]]:
    value = _require_mapping(document, "evaluation document")
    _require_keys(
        value,
        {"schema_version", "corpus", "route", "prompt_version", "semantic_schema_version", "cases"},
        "evaluation document",
    )
    if value["schema_version"] != INPUT_SCHEMA_VERSION:
        raise EvaluationError("unsupported evaluation input schema")
    if value["prompt_version"] != semantic_analyzer.PROMPT_VERSION:
        raise EvaluationError("prompt version does not match the production analyzer")
    if value["semantic_schema_version"] != semantic_analyzer.SCHEMA_VERSION:
        raise EvaluationError("semantic schema version does not match the production analyzer")
    corpus, corpus_digest = _corpus(value)
    route = _route(value)
    cases = value["cases"]
    if not isinstance(cases, list) or not cases:
        raise EvaluationError("evaluation requires at least one complete case")
    return value, corpus, corpus_digest, route


def _case_evidence(case: Mapping[str, Any], corpus_spans: Mapping[str, dict[str, str]]) -> list[str]:
    evidence_ids = case.get("evidence_ids")
    if not isinstance(evidence_ids, list) or not evidence_ids:
        raise EvaluationError("case evidence_ids must be a non-empty list")
    ids = [str(value) for value in evidence_ids]
    if len(ids) != len(set(ids)) or not set(ids) <= set(corpus_spans):
        raise EvaluationError("case references unknown or duplicate corpus evidence")
    return sorted(ids)


def _partitions(value: Any, label: str) -> list[tuple[str, ...]]:
    if not isinstance(value, list):
        raise EvaluationError(f"{label} must be a list")
    result: list[tuple[str, ...]] = []
    for part in value:
        if not isinstance(part, list) or not part:
            raise EvaluationError(f"{label} requires non-empty evidence groups")
        values = tuple(sorted(str(item) for item in part))
        if len(values) != len(set(values)):
            raise EvaluationError(f"{label} cannot repeat evidence in a group")
        result.append(values)
    if len(result) != len(set(result)):
        raise EvaluationError(f"{label} cannot repeat an activity group")
    return sorted(result)


def _validate_case_contract(case: Any, corpus_spans: Mapping[str, dict[str, str]]) -> tuple[Mapping[str, Any], list[str], list[tuple[str, ...]]]:
    value = _require_mapping(case, "case")
    _require_keys(value, {"case_id", "evidence_ids", "expected_activity_partitions", "replays"}, "case")
    case_id = str(value["case_id"] or "")
    if not _CASE_ID_RE.fullmatch(case_id):
        raise EvaluationError("case_id is invalid")
    ids = _case_evidence(value, corpus_spans)
    partitions = _partitions(value["expected_activity_partitions"], "expected_activity_partitions")
    if set(item for group in partitions for item in group) - set(ids):
        raise EvaluationError("expected activity partition cites out-of-case evidence")
    replays = value["replays"]
    if not isinstance(replays, list) or len(replays) < 2:
        raise EvaluationError("case requires at least two captured replays")
    if not all(isinstance(replay, Mapping) for replay in replays):
        raise EvaluationError("case replays must be analyzer JSON objects")
    return value, ids, partitions


def _response_shape(response: Mapping[str, Any]) -> None:
    if set(response) != _RESPONSE_FIELDS:
        raise EvaluationError("captured analyzer response does not match its response schema")
    if not all(isinstance(response[name], list) for name in _RESPONSE_FIELDS):
        raise EvaluationError("captured analyzer response collections must be lists")
    for activity in response["activities"]:
        if not isinstance(activity, Mapping):
            raise EvaluationError("captured activity must be an object")
        # Rendering is local and deterministic. A model supplied description is
        # neither trusted nor a valid basis for a Clockify entry.
        if activity.get("rendered_description") not in (None, ""):
            raise EvaluationError("captured activity must not contain a rendered description")


def _evaluate_replay(
    response: Mapping[str, Any], *, evidence_ids: list[str], corpus_spans: Mapping[str, dict[str, str]], route: Mapping[str, str]
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        _response_shape(response)
        return semantic_analyzer.validate_result(
            dict(response),
            known_evidence_ids=set(evidence_ids),
            provider_model=route["model"],
            analyzer_tier=route["tier"],
            evidence_time_spans={item: corpus_spans[item] for item in evidence_ids},
        ), None
    except (EvaluationError, semantic_analyzer.AnalyzerError):
        return None, "schema_or_citation"


def _rendering_is_valid(result: Mapping[str, Any]) -> bool:
    try:
        for activity in result["activities"]:
            if activity.get("lifecycle") in {"planned", "noise"}:
                continue
            project = activity.get("project_recommendation")
            if not isinstance(project, Mapping):
                return False
            caveman_renderer.render_caveman_description(
                {
                    "prefix": project.get("prefix"),
                    "action": activity.get("action"),
                    "object": activity.get("object"),
                    "outcome": activity.get("outcome"),
                }
            )
    except caveman_renderer.CavemanValidationError:
        return False
    return True


def _activity_partitions(result: Mapping[str, Any]) -> list[tuple[str, ...]]:
    return sorted(
        tuple(sorted(str(value) for value in activity["evidence_ids"]))
        for activity in result["activities"]
    )


def _review_decision_signature(result: Mapping[str, Any]) -> str:
    """Return the provider-wording-independent decision that affects review.

    Exact replay is guaranteed by the validated response cache.  Live route
    replays still have to agree on evidence disposition, activity partitions,
    lifecycle, effort, and confidence.  Explanatory prose and omission reasons
    are intentionally excluded because they do not create or revise review rows.
    """
    activities = sorted(
        (
            {
                "evidence_ids": sorted(str(value) for value in activity["evidence_ids"]),
                "lifecycle": activity["lifecycle"],
                "effort": activity["effort"],
                "semantic_confidence": activity["semantic_confidence"],
                "timing_confidence": activity["timing_confidence"],
            }
            for activity in result["activities"]
        ),
        key=canonical_json,
    )
    exceptions = sorted(
        (
            {
                "kind": value["kind"],
                "evidence_ids": sorted(str(item) for item in value["evidence_ids"]),
            }
            for value in result["exceptions"]
        ),
        key=canonical_json,
    )
    omissions = sorted(
        (
            {"evidence_ids": sorted(str(item) for item in value["evidence_ids"])}
            for value in result["omissions"]
        ),
        key=canonical_json,
    )
    return canonical_json(
        {"activities": activities, "exceptions": exceptions, "omissions": omissions}
    )


def _case_score(
    case: Mapping[str, Any], *, evidence_ids: list[str], expected_partitions: list[tuple[str, ...]], corpus_spans: Mapping[str, dict[str, str]], route: Mapping[str, str]
) -> dict[str, Any]:
    normalized: list[dict[str, Any]] = []
    schema_ok = True
    for replay in case["replays"]:
        result, error = _evaluate_replay(replay, evidence_ids=evidence_ids, corpus_spans=corpus_spans, route=route)
        if error or result is None:
            schema_ok = False
            continue
        normalized.append(result)
    complete = schema_ok and len(normalized) == len(case["replays"])
    replay_ok = complete and len({_review_decision_signature(item) for item in normalized}) == 1
    atomic_ok = complete and all(
        _activity_partitions(item) == expected_partitions for item in normalized
    )
    descriptions_ok = complete and all(_rendering_is_valid(item) for item in normalized)
    checks = {
        "schema_valid": schema_ok,
        "evidence_citations_valid": schema_ok,
        "atomicity_valid": atomic_ok,
        "forbidden_descriptions_rejected": descriptions_ok,
        "stable_replay": replay_ok,
    }
    input_digest = sha256_hex({"case_id": case["case_id"], "evidence_ids": evidence_ids, "expected_activity_partitions": expected_partitions, "replays": case["replays"]})
    return {
        "case_id": case["case_id"],
        "case_input_digest": input_digest,
        "replay_digests": [sha256_hex(replay) for replay in case["replays"]],
        "checks": checks,
        "passed": all(checks.values()),
    }


def _with_scorecard_digest(value: dict[str, Any]) -> dict[str, Any]:
    scorecard = dict(value)
    scorecard["scorecard_digest"] = sha256_hex(value)
    return scorecard


def evaluate(document: Any) -> dict[str, Any]:
    """Evaluate a redacted captured-output document and return a sealed scorecard.

    Structural corpus mistakes raise :class:`EvaluationError`; a model quality
    failure yields a signed scorecard with ``passed: false`` so it cannot be
    mistaken for an accepted route.
    """
    value, corpus, corpus_digest, route = _validate_document(document)
    corpus_spans = {item["evidence_id"]: item["time_span"] for item in corpus}
    seen_cases: set[str] = set()
    assigned_evidence: list[str] = []
    results: list[dict[str, Any]] = []
    for raw_case in value["cases"]:
        case, evidence_ids, partitions = _validate_case_contract(raw_case, corpus_spans)
        case_id = str(case["case_id"])
        if case_id in seen_cases:
            raise EvaluationError("case IDs must be unique")
        seen_cases.add(case_id)
        assigned_evidence.extend(evidence_ids)
        results.append(_case_score(case, evidence_ids=evidence_ids, expected_partitions=partitions, corpus_spans=corpus_spans, route=route))
    if sorted(assigned_evidence) != sorted(corpus_spans):
        raise EvaluationError("cases do not cover the complete corpus exactly once")
    results.sort(key=lambda item: item["case_id"])
    body = {
        "schema_version": SCORECARD_SCHEMA_VERSION,
        "evaluator_version": EVALUATOR_VERSION,
        "input_corpus_digest": corpus_digest,
        "input_digest": sha256_hex(value),
        "route": route,
        "prompt_version": value["prompt_version"],
        "semantic_schema_version": value["semantic_schema_version"],
        "case_count": len(results),
        "results": results,
        "passed": all(item["passed"] for item in results),
    }
    return _with_scorecard_digest(body)


def verify_scorecard(scorecard: Any) -> dict[str, Any]:
    """Verify a generated scorecard's stable content-addressed seal."""
    value = _require_mapping(scorecard, "scorecard")
    required = {
        "schema_version", "evaluator_version", "input_corpus_digest", "input_digest", "route", "prompt_version",
        "semantic_schema_version", "case_count", "results", "passed", "scorecard_digest",
    }
    _require_keys(value, required, "scorecard")
    if value["schema_version"] != SCORECARD_SCHEMA_VERSION or value["evaluator_version"] != EVALUATOR_VERSION:
        raise EvaluationError("unsupported scorecard version")
    if not _SHA256_RE.fullmatch(str(value["input_corpus_digest"])) or not _SHA256_RE.fullmatch(str(value["input_digest"])):
        raise EvaluationError("scorecard has invalid input digest")
    body = {key: value[key] for key in value if key != "scorecard_digest"}
    if str(value["scorecard_digest"]) != sha256_hex(body):
        raise EvaluationError("scorecard digest does not match scorecard content")
    if not isinstance(value["results"], list) or value["case_count"] != len(value["results"]):
        raise EvaluationError("scorecard result count is invalid")
    if value["passed"] != all(isinstance(item, Mapping) and item.get("passed") is True for item in value["results"]):
        raise EvaluationError("scorecard overall result is inconsistent")
    return dict(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path, help="redacted captured-output JSON")
    parser.add_argument("--output", required=True, type=Path, help="local scorecard JSON")
    args = parser.parse_args(argv)
    try:
        document = json.loads(args.input.read_text(encoding="utf-8"))
        scorecard = evaluate(document)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(scorecard, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, json.JSONDecodeError, EvaluationError) as exc:
        print(f"analyzer evaluation blocked: {exc}")
        return 2
    print(f"analyzer evaluation: {'passed' if scorecard['passed'] else 'failed'} ({scorecard['scorecard_digest']})")
    return 0 if scorecard["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
