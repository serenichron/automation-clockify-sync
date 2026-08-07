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
EVALUATOR_VERSION = "clockify-analyzer-evaluator/v5"
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")
_CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REQUIRED_ROUTE_FIELDS = frozenset({"route_id", "model", "tier"})
_ROUTE_FIELDS = _REQUIRED_ROUTE_FIELDS | {"revision"}
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
    if not _REQUIRED_ROUTE_FIELDS <= set(raw) or not set(raw) <= _ROUTE_FIELDS:
        raise EvaluationError("route has unsupported fields")
    route = {key: str(raw.get(key) or "").strip() for key in _ROUTE_FIELDS}
    if not route["route_id"] or not route["model"] or route["tier"] not in {"primary", "fallback"}:
        raise EvaluationError("route requires route_id, model, and a primary or fallback tier")
    if route["model"].endswith((":cloud", "-cloud")) and not re.fullmatch(
        r"[a-f0-9]{64}", route["revision"]
    ):
        raise EvaluationError("moving cloud model routes require a release revision")
    return route


def _validate_document(document: Any) -> tuple[Mapping[str, Any], list[dict[str, Any]], str, dict[str, str]]:
    value = _require_mapping(document, "evaluation document")
    _require_keys(
        value,
        {
            "schema_version", "corpus", "route", "prompt_version",
            "semantic_schema_version", "evidence_bundle_schema_version", "cases",
        },
        "evaluation document",
    )
    if value["schema_version"] != INPUT_SCHEMA_VERSION:
        raise EvaluationError("unsupported evaluation input schema")
    if value["prompt_version"] != semantic_analyzer.PROMPT_VERSION:
        raise EvaluationError("prompt version does not match the production analyzer")
    if value["semantic_schema_version"] != semantic_analyzer.SCHEMA_VERSION:
        raise EvaluationError("semantic schema version does not match the production analyzer")
    if value["evidence_bundle_schema_version"] != semantic_analyzer.EVIDENCE_BUNDLE_SCHEMA_VERSION:
        raise EvaluationError("evidence bundle schema version does not match the production analyzer")
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


def _concepts(value: Any) -> dict[tuple[str, ...], tuple[str, ...]]:
    if not isinstance(value, list):
        raise EvaluationError("expected_activity_concepts must be a list")
    result: dict[tuple[str, ...], tuple[str, ...]] = {}
    for item in value:
        raw = _require_mapping(item, "expected_activity_concept")
        _require_keys(raw, {"evidence_ids", "required_terms"}, "expected_activity_concept")
        groups = _partitions([raw["evidence_ids"]], "expected_activity_concept.evidence_ids")
        terms = raw["required_terms"]
        if not isinstance(terms, list) or not terms:
            raise EvaluationError("expected_activity_concept requires terms")
        normalized_terms = tuple(sorted({str(term).casefold().strip() for term in terms}))
        if any(not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,31}", term) for term in normalized_terms):
            raise EvaluationError("expected activity concept term is invalid")
        group = groups[0]
        if group in result:
            raise EvaluationError("expected activity concept repeats a partition")
        result[group] = normalized_terms
    return result


def _nonactivity_expectations(
    value: Any, *, classification: str
) -> dict[tuple[str, ...], tuple[str, ...]]:
    if not isinstance(value, list):
        raise EvaluationError(f"expected_{classification} must be a list")
    field = "kind" if classification == "exceptions" else "lifecycles"
    result: dict[tuple[str, ...], tuple[str, ...]] = {}
    for item in value:
        raw = _require_mapping(item, f"expected_{classification} item")
        _require_keys(
            raw,
            {"evidence_ids", field},
            f"expected_{classification} item",
        )
        group = _partitions(
            [raw["evidence_ids"]],
            f"expected_{classification}.evidence_ids",
        )[0]
        raw_decisions = [raw[field]] if classification == "exceptions" else raw[field]
        if not isinstance(raw_decisions, list) or not raw_decisions:
            raise EvaluationError(f"expected_{classification} {field} is invalid")
        decisions = tuple(sorted({str(item or "").strip() for item in raw_decisions}))
        if any(not re.fullmatch(r"[a-z][a-z_]{1,63}", item) for item in decisions):
            raise EvaluationError(f"expected_{classification} {field} is invalid")
        if group in result:
            raise EvaluationError(f"expected_{classification} repeats a partition")
        result[group] = decisions
    return result


def _validate_case_contract(
    case: Any,
    corpus_spans: Mapping[str, dict[str, str]],
) -> tuple[
    Mapping[str, Any],
    list[str],
    list[tuple[str, ...]],
    dict[tuple[str, ...], tuple[str, ...]],
    dict[tuple[str, ...], tuple[str, ...]],
    dict[tuple[str, ...], tuple[str, ...]],
]:
    value = _require_mapping(case, "case")
    _require_keys(
        value,
        {
            "case_id", "evidence_ids", "expected_activity_partitions",
            "expected_activity_concepts", "expected_exceptions",
            "expected_omissions", "replays",
        },
        "case",
    )
    case_id = str(value["case_id"] or "")
    if not _CASE_ID_RE.fullmatch(case_id):
        raise EvaluationError("case_id is invalid")
    ids = _case_evidence(value, corpus_spans)
    partitions = _partitions(value["expected_activity_partitions"], "expected_activity_partitions")
    concepts = _concepts(value["expected_activity_concepts"])
    expected_exceptions = _nonactivity_expectations(
        value["expected_exceptions"], classification="exceptions"
    )
    expected_omissions = _nonactivity_expectations(
        value["expected_omissions"], classification="omissions"
    )
    if set(item for group in partitions for item in group) - set(ids):
        raise EvaluationError("expected activity partition cites out-of-case evidence")
    if set(concepts) != set(partitions):
        raise EvaluationError("expected activity concepts must cover every expected partition")
    expected_groups = [
        *partitions,
        *expected_exceptions,
        *expected_omissions,
    ]
    expected_ids = [item for group in expected_groups for item in group]
    if sorted(expected_ids) != ids or len(expected_ids) != len(set(expected_ids)):
        raise EvaluationError(
            "expected activity, exception, and omission partitions must cover case evidence exactly once"
        )
    replays = value["replays"]
    if not isinstance(replays, list) or len(replays) < 2:
        raise EvaluationError("case requires at least two captured replays")
    if not all(isinstance(replay, Mapping) for replay in replays):
        raise EvaluationError("case replays must be analyzer JSON objects")
    return (
        value,
        ids,
        partitions,
        concepts,
        expected_exceptions,
        expected_omissions,
    )


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
        response = semantic_analyzer.bind_activity_evidence_spans(
            response,
            evidence_time_spans={item: corpus_spans[item] for item in evidence_ids},
        )
        return semantic_analyzer.validate_result(
            dict(response),
            known_evidence_ids=set(evidence_ids),
            provider_model=route["model"],
            provider_revision=route.get("revision", ""),
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
            caveman_renderer.render_caveman_description(
                {
                    # Deterministic routing owns the prefix; the analyzer route
                    # is evaluated only on action, object, and bounded outcome.
                    "prefix": "SC",
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


def _semantic_content_is_valid(
    result: Mapping[str, Any],
    concepts: Mapping[tuple[str, ...], tuple[str, ...]],
) -> bool:
    activities = {
        tuple(sorted(str(value) for value in activity["evidence_ids"])): activity
        for activity in result["activities"]
    }
    if set(activities) != set(concepts):
        return False
    for partition, terms in concepts.items():
        activity = activities[partition]
        text = " ".join(
            str(activity.get(field) or "").casefold()
            for field in ("action", "object", "outcome")
        )
        if any(term not in text for term in terms):
            return False
    return True


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
            {
                "disposition": "omitted",
                "evidence_ids": sorted(str(item) for item in value["evidence_ids"]),
            }
            for value in result["omissions"]
        ),
        key=canonical_json,
    )
    return canonical_json(
        {"activities": activities, "exceptions": exceptions, "omissions": omissions}
    )


def _nonactivity_dispositions_are_valid(
    result: Mapping[str, Any],
    *,
    expected_exceptions: Mapping[tuple[str, ...], tuple[str, ...]],
    expected_omissions: Mapping[tuple[str, ...], tuple[str, ...]],
) -> bool:
    actual_exceptions = {
        tuple(sorted(str(item) for item in value["evidence_ids"])): str(
            value.get("kind") or ""
        )
        for value in result["exceptions"]
    }
    actual_omissions = {
        tuple(sorted(str(item) for item in value["evidence_ids"])): str(
            value.get("lifecycle") or ""
        )
        for value in result["omissions"]
    }
    return (
        set(actual_exceptions) == set(expected_exceptions)
        and all(
            actual_exceptions[group] in expected_exceptions[group]
            for group in actual_exceptions
        )
        and set(actual_omissions) == set(expected_omissions)
        and all(
            actual_omissions[group] in expected_omissions[group]
            for group in actual_omissions
        )
    )


def _case_score(
    case: Mapping[str, Any], *, evidence_ids: list[str], expected_partitions: list[tuple[str, ...]], expected_concepts: Mapping[tuple[str, ...], tuple[str, ...]], expected_exceptions: Mapping[tuple[str, ...], tuple[str, ...]], expected_omissions: Mapping[tuple[str, ...], tuple[str, ...]], corpus_spans: Mapping[str, dict[str, str]], route: Mapping[str, str]
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
    semantic_content_ok = complete and all(
        _semantic_content_is_valid(item, expected_concepts) for item in normalized
    )
    dispositions_ok = complete and all(
        _nonactivity_dispositions_are_valid(
            item,
            expected_exceptions=expected_exceptions,
            expected_omissions=expected_omissions,
        )
        for item in normalized
    )
    checks = {
        "schema_valid": schema_ok,
        "evidence_citations_valid": schema_ok,
        "atomicity_valid": atomic_ok,
        "semantic_content_valid": semantic_content_ok,
        "nonactivity_dispositions_valid": dispositions_ok,
        "forbidden_descriptions_rejected": descriptions_ok,
        "stable_replay": replay_ok,
    }
    input_digest = sha256_hex({"case_id": case["case_id"], "evidence_ids": evidence_ids, "expected_activity_partitions": expected_partitions, "expected_activity_concepts": case["expected_activity_concepts"], "expected_exceptions": case["expected_exceptions"], "expected_omissions": case["expected_omissions"], "replays": case["replays"]})
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
        (
            case,
            evidence_ids,
            partitions,
            concepts,
            expected_exceptions,
            expected_omissions,
        ) = _validate_case_contract(raw_case, corpus_spans)
        case_id = str(case["case_id"])
        if case_id in seen_cases:
            raise EvaluationError("case IDs must be unique")
        seen_cases.add(case_id)
        assigned_evidence.extend(evidence_ids)
        results.append(_case_score(case, evidence_ids=evidence_ids, expected_partitions=partitions, expected_concepts=concepts, expected_exceptions=expected_exceptions, expected_omissions=expected_omissions, corpus_spans=corpus_spans, route=route))
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
        "evidence_bundle_schema_version": value["evidence_bundle_schema_version"],
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
        "semantic_schema_version", "evidence_bundle_schema_version", "case_count",
        "results", "passed", "scorecard_digest",
    }
    _require_keys(value, required, "scorecard")
    if value["schema_version"] != SCORECARD_SCHEMA_VERSION or value["evaluator_version"] != EVALUATOR_VERSION:
        raise EvaluationError("unsupported scorecard version")
    _route(value)
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
