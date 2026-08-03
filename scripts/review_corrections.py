#!/usr/bin/env python3
"""Immutable, evidence-bound review corrections for Clockify proposals.

The decision log is local JSONL.  Each line is self-authenticating and linked
to its predecessor, so a changed, reordered, or conflicting decision blocks
reuse rather than silently altering the review record.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping


SCHEMA_VERSION = 1
DECISIONS = {"approve", "skip", "modify"}
CATEGORIES = {"wording", "routing", "split", "omission", "allocation"}
PATCH_FIELDS = {
    "description",
    "client_project",
    "tag_names",
    "start",
    "end",
    "duration_minutes",
    "billable",
}


class ReviewDecisionError(ValueError):
    """Raised when a correction cannot be trusted or safely generalized."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def canonical_digest(value: Any, *, prefix: str = "sha256:") -> str:
    return prefix + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def evidence_fingerprint(evidence_ids: Iterable[Any]) -> str:
    values = sorted({str(value).strip() for value in evidence_ids if str(value).strip()})
    if not values:
        raise ReviewDecisionError("evidence_ids are required to bind a review decision")
    return canonical_digest({"evidence_ids": values}, prefix="evfp:sha256:")


def _evidence_ids(item: Mapping[str, Any]) -> list[Any]:
    current = item.get("current") if isinstance(item.get("current"), Mapping) else item
    provenance = current.get("provenance") if isinstance(current.get("provenance"), Mapping) else {}
    values = current.get("evidence_ids", provenance.get("evidence_ids", ()))
    return list(values) if isinstance(values, (list, tuple, set)) else []


def _one_line(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip() or "\n" in value or "\r" in value:
        raise ReviewDecisionError(f"{field} must be a non-empty, trimmed one-line string")
    return value


def _normal_categories(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ReviewDecisionError("correction_categories must be a non-empty list")
    values = sorted({str(item).strip().lower() for item in value})
    if not set(values) <= CATEGORIES:
        raise ReviewDecisionError("correction_categories contains an unsupported category")
    return values


def _normal_patch(value: Any, decision: str, *, allow_empty_modify: bool = False) -> dict[str, dict[str, Any]]:
    if decision != "modify":
        if value not in (None, {}, []):
            raise ReviewDecisionError("only modify decisions may contain a field_patch")
        return {}
    if value in (None, {}) and allow_empty_modify:
        return {}
    if not isinstance(value, Mapping) or not value:
        raise ReviewDecisionError("modify decisions require a non-empty structured field_patch")
    patch: dict[str, dict[str, Any]] = {}
    for field, operation in value.items():
        if field not in PATCH_FIELDS or not isinstance(operation, Mapping):
            raise ReviewDecisionError("field_patch contains an unsupported or opaque replacement")
        if set(operation) != {"op", "value"} or operation.get("op") != "replace":
            raise ReviewDecisionError("field_patch operations must be explicit replace operations")
        patch[str(field)] = {"op": "replace", "value": copy.deepcopy(operation["value"])}
    return {field: patch[field] for field in sorted(patch)}


def _normal_evidence_id_groups(value: Any) -> list[list[str]]:
    """Normalize an unordered, non-overlapping evidence partition."""
    if not isinstance(value, list) or not value:
        raise ReviewDecisionError("expected_child_evidence_ids must be a non-empty list")
    groups: list[list[str]] = []
    seen: set[str] = set()
    for group in value:
        if not isinstance(group, list) or not group:
            raise ReviewDecisionError("every split child must have non-empty evidence IDs")
        normalized = sorted({_one_line(item, "split child evidence ID") for item in group})
        if len(normalized) != len(group):
            raise ReviewDecisionError("split child evidence IDs must not repeat")
        overlap = seen.intersection(normalized)
        if overlap:
            raise ReviewDecisionError("split child evidence must form a non-overlapping partition")
        seen.update(normalized)
        groups.append(normalized)
    return sorted(groups, key=lambda group: tuple(group))


def _normal_split_expectation(
    value: Any,
    *,
    decision: str,
    categories: Iterable[str],
    parent_evidence_ids: Iterable[Any] | None,
    required: bool,
) -> dict[str, Any] | None:
    """Validate the local-only, evidence-bound contract for a split correction."""
    is_split = "split" in set(categories)
    if value is None:
        if is_split and required:
            raise ReviewDecisionError("split corrections require an executable split_expectation")
        return None
    if not is_split or decision != "modify":
        raise ReviewDecisionError("split_expectation is only valid for modify decisions in the split category")
    if not isinstance(value, Mapping) or set(value) != {"expected_child_count", "expected_child_evidence_ids"}:
        raise ReviewDecisionError("split_expectation must define only child count and child evidence IDs")
    count = value.get("expected_child_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 2:
        raise ReviewDecisionError("expected_child_count must be an integer of at least two")
    groups = _normal_evidence_id_groups(value.get("expected_child_evidence_ids"))
    if count != len(groups):
        raise ReviewDecisionError("expected_child_count must equal the number of child evidence groups")
    expected_ids = {item for group in groups for item in group}
    if parent_evidence_ids is not None:
        parent_ids = {str(item).strip() for item in parent_evidence_ids if str(item).strip()}
        if expected_ids != parent_ids:
            raise ReviewDecisionError("split child evidence must exactly partition the reviewed parent evidence")
    return {
        "expected_child_count": count,
        "expected_child_evidence_ids": groups,
    }


def review_target(item: Mapping[str, Any]) -> dict[str, str]:
    review_item_id = _one_line(item.get("id"), "review_item_id")
    current = item.get("current") if isinstance(item.get("current"), Mapping) else item
    activity_id = _one_line(current.get("activity_id"), "activity_id")
    return {
        "review_item_id": review_item_id,
        "activity_id": activity_id,
        "evidence_fingerprint": evidence_fingerprint(_evidence_ids(item)),
    }


def build_decision(
    item: Mapping[str, Any],
    *,
    decision: str,
    reviewer: str,
    reviewed_at: str,
    correction_categories: list[str],
    rationale: str,
    field_patch: Mapping[str, Any] | None = None,
    split_expectation: Mapping[str, Any] | None = None,
    _allow_legacy_split_without_expectation: bool = False,
) -> dict[str, Any]:
    """Build a decision from a current state item, retaining its evidence binding."""
    target = review_target(item)
    decision = str(decision).strip().lower()
    if decision not in DECISIONS:
        raise ReviewDecisionError("decision must be approve, skip, or modify")
    categories = _normal_categories(correction_categories)
    patch = _normal_patch(
        field_patch,
        decision,
        allow_empty_modify=(decision == "modify" and "split" in categories),
    )
    split = _normal_split_expectation(
        split_expectation,
        decision=decision,
        categories=categories,
        parent_evidence_ids=_evidence_ids(item),
        required=not _allow_legacy_split_without_expectation,
    )
    if split is not None and patch:
        raise ReviewDecisionError("split corrections cannot use one parent field_patch; record child corrections separately")
    record = {
        "schema_version": SCHEMA_VERSION,
        **target,
        "decision": decision,
        "field_patch": patch,
        "correction_categories": categories,
        "rationale": _one_line(rationale, "rationale"),
        "reviewer": _one_line(reviewer, "reviewer"),
        "reviewed_at": _one_line(reviewed_at, "reviewed_at"),
    }
    if split is not None:
        record["split_expectation"] = split
    record["decision_id"] = "rdec-" + canonical_digest(record)[7:31]
    return record


def _without_integrity(record: Mapping[str, Any]) -> dict[str, Any]:
    value = {str(key): copy.deepcopy(item) for key, item in record.items() if key not in {"canonical_digest", "previous_digest"}}
    return value


def validate_decision(record: Mapping[str, Any], *, item: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if not isinstance(record, Mapping) or record.get("schema_version") != SCHEMA_VERSION:
        raise ReviewDecisionError("unsupported review decision schema")
    required = {
        "schema_version", "decision_id", "review_item_id", "activity_id", "evidence_fingerprint",
        "decision", "field_patch", "correction_categories", "rationale", "reviewer", "reviewed_at",
    }
    missing = required - set(record)
    if missing:
        raise ReviewDecisionError(f"review decision is missing fields: {', '.join(sorted(missing))}")
    split_expectation = record.get("split_expectation")
    split_evidence_ids = (
        [evidence_id for group in split_expectation.get("expected_child_evidence_ids", []) for evidence_id in group]
        if isinstance(split_expectation, Mapping) else ["bound"]
    )
    result = build_decision(
        {"id": record["review_item_id"], "current": {"activity_id": record["activity_id"], "evidence_ids": split_evidence_ids}},
        decision=record["decision"],
        reviewer=record["reviewer"],
        reviewed_at=record["reviewed_at"],
        correction_categories=list(record["correction_categories"]),
        rationale=record["rationale"],
        field_patch=record["field_patch"],
        split_expectation=split_expectation,
        _allow_legacy_split_without_expectation=True,
    )
    # The temporary evidence above only verifies shape. Restore and verify the
    # supplied fingerprint independently; it must be a canonical evfp digest.
    fingerprint = _one_line(record["evidence_fingerprint"], "evidence_fingerprint")
    if not re.fullmatch(r"evfp:sha256:[0-9a-f]{64}", fingerprint):
        raise ReviewDecisionError("evidence_fingerprint is not canonical")
    if split_expectation is not None:
        split = result.get("split_expectation")
        assert isinstance(split, Mapping)
        split_ids = [evidence_id for group in split["expected_child_evidence_ids"] for evidence_id in group]
        if evidence_fingerprint(split_ids) != fingerprint:
            raise ReviewDecisionError("split child evidence does not match the reviewed evidence fingerprint")
    result["evidence_fingerprint"] = fingerprint
    unsigned = {key: result[key] for key in result if key != "decision_id"}
    # decision_id is derived from the unsigned record in build_decision; verify
    # with the exact same construction to reject copied or altered records.
    expected_id = "rdec-" + canonical_digest(unsigned)[7:31]
    if record["decision_id"] != expected_id:
        raise ReviewDecisionError("review decision_id does not match canonical content")
    result["decision_id"] = record["decision_id"]
    if item is not None and review_target(item) != {
        "review_item_id": result["review_item_id"],
        "activity_id": result["activity_id"],
        "evidence_fingerprint": result["evidence_fingerprint"],
    }:
        raise ReviewDecisionError("review decision is stale for the current activity evidence")
    return result


def _read_log(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    previous: str | None = None
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            raise ReviewDecisionError(f"blank line in immutable decision log at line {number}")
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ReviewDecisionError(f"invalid decision JSON at line {number}") from exc
        normalized = validate_decision(record)
        digest = canonical_digest(_without_integrity(record))
        if record.get("canonical_digest") != digest or record.get("previous_digest") != previous:
            raise ReviewDecisionError(f"decision log integrity failure at line {number}")
        normalized["canonical_digest"] = digest
        normalized["previous_digest"] = previous
        records.append(normalized)
        previous = digest
    return records


def load_decisions(path: Path) -> list[dict[str, Any]]:
    """Read and integrity-check an immutable correction JSONL file."""
    return _read_log(path)


def append_decision(path: Path, record: Mapping[str, Any], *, item: Mapping[str, Any] | None = None) -> bool:
    """Append exactly once, rejecting stale evidence, conflict, and tampering.

    Returns ``True`` only when a new immutable line was appended.
    """
    normalized = validate_decision(record, item=item)
    existing = _read_log(path)
    target = tuple(normalized[key] for key in ("review_item_id", "activity_id", "evidence_fingerprint"))
    for prior in existing:
        prior_target = tuple(prior[key] for key in ("review_item_id", "activity_id", "evidence_fingerprint"))
        if prior_target != target:
            continue
        if canonical_json(_without_integrity(prior)) == canonical_json(_without_integrity(normalized)):
            return False
        raise ReviewDecisionError("conflicting immutable decision already exists for this evidence-bound target")
    previous = existing[-1]["canonical_digest"] if existing else None
    line = dict(normalized)
    line["previous_digest"] = previous
    line["canonical_digest"] = canonical_digest(_without_integrity(line))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(canonical_json(line) + "\n")
    return True


_RULES = {
    "wording": "Render concise wording that states only the cited accomplishment.",
    "routing": "Route work only to the project supported by cited evidence.",
    "split": "Split only when cited evidence proves independent accomplishments.",
    "omission": "Omit work when cited evidence does not support a loggable accomplishment.",
    "allocation": "Allocate cited active effort without overlap or gap filling.",
}


def derive_learning_cases(decisions: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Create context-safe, generalized regression rules without raw review text."""
    cases: list[dict[str, Any]] = []
    for record in decisions:
        normalized = validate_decision(record)
        patch_fields = sorted(normalized["field_patch"])
        for category in normalized["correction_categories"]:
            case = {
                "schema_version": SCHEMA_VERSION,
                "category": category,
                "decision": normalized["decision"],
                "patched_fields": patch_fields,
                "instruction": _RULES[category],
            }
            case["learning_case_id"] = "lcase-" + canonical_digest(case)[7:31]
            cases.append(case)
    return sorted(cases, key=lambda value: value["learning_case_id"])


def derive_regression_cases(decisions: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Create exact local-only behavioral expectations for corrected evidence.

    Unlike generalized learning rules, these cases may contain an explicitly
    approved replacement value. They must remain local and are never projected
    into an analyzer request.
    """
    cases: list[dict[str, Any]] = []
    for record in decisions:
        normalized = validate_decision(record)
        case = {
            "schema_version": SCHEMA_VERSION,
            "local_only": True,
            "activity_id": normalized["activity_id"],
            "evidence_fingerprint": normalized["evidence_fingerprint"],
            "decision": normalized["decision"],
            "correction_categories": list(normalized["correction_categories"]),
            "expected_presence": normalized["decision"] != "skip",
            "expected_field_patch": copy.deepcopy(normalized["field_patch"]),
        }
        if "split" in normalized["correction_categories"]:
            if "split_expectation" in normalized:
                case["expected_split"] = copy.deepcopy(normalized["split_expectation"])
            else:
                # Preserve old immutable records, but fail their replay rather
                # than pretending that a generic split rule is executable.
                case["split_contract_missing"] = True
        case["regression_case_id"] = "rcase-" + canonical_digest(case)[7:31]
        cases.append(case)
    return sorted(cases, key=lambda value: value["regression_case_id"])


def proposal_target(proposal: Mapping[str, Any]) -> tuple[str, str] | None:
    """Return the stable activity/evidence target shared by all allocation segments."""
    activity_id = str(proposal.get("activity_id") or "").strip()
    provenance = proposal.get("provenance") if isinstance(proposal.get("provenance"), Mapping) else {}
    values = proposal.get("evidence_ids", provenance.get("evidence_ids", ()))
    if not activity_id or not isinstance(values, (list, tuple, set)) or not values:
        return None
    return activity_id, evidence_fingerprint(values)


def _proposal_evidence_ids(proposal: Mapping[str, Any]) -> set[str]:
    provenance = proposal.get("provenance") if isinstance(proposal.get("provenance"), Mapping) else {}
    values = proposal.get("evidence_ids", provenance.get("evidence_ids", ()))
    if not isinstance(values, (list, tuple, set)):
        return set()
    return {str(value).strip() for value in values if str(value).strip()}


def _split_failures(case: Mapping[str, Any], proposals: Iterable[Mapping[str, Any]]) -> list[str]:
    """Verify a reviewed parent was replaced by the exact child partition.

    This deliberately looks at evidence overlap rather than the parent activity
    identity: a real split creates new activity IDs and each child gets a
    subset of the parent evidence.  Exact correction prose never enters this
    contract or any analyzer request.
    """
    if case.get("split_contract_missing"):
        return ["split correction has no executable structural contract"]
    raw = case.get("expected_split")
    try:
        split = _normal_split_expectation(
            raw,
            decision=str(case.get("decision") or ""),
            categories=case.get("correction_categories", ()),
            parent_evidence_ids=None,
            required=True,
        )
    except ReviewDecisionError as exc:
        return [f"split correction contract is invalid: {exc}"]
    assert split is not None
    expected_groups = [set(group) for group in split["expected_child_evidence_ids"]]
    expected_union = set().union(*expected_groups)
    try:
        expected_fingerprint = evidence_fingerprint(expected_union)
    except ReviewDecisionError:
        return ["split correction contract has no evidence"]
    if expected_fingerprint != str(case.get("evidence_fingerprint") or ""):
        return ["split correction evidence does not match its reviewed target"]

    actual: dict[str, set[str]] = {}
    missing_identity = False
    for position, proposal in enumerate(proposals):
        evidence_ids = _proposal_evidence_ids(proposal)
        if not evidence_ids.intersection(expected_union):
            continue
        activity_id = str(proposal.get("activity_id") or "").strip()
        if not activity_id:
            missing_identity = True
            activity_id = f"<missing:{position}>"
        actual.setdefault(activity_id, set()).update(evidence_ids)
    actual_groups = list(actual.values())
    failures: list[str] = []
    if missing_identity:
        failures.append("split child proposal is missing an activity_id")
    if len(actual_groups) != split["expected_child_count"]:
        failures.append("split child count differs from reviewed value")
    occurrences: dict[str, int] = {}
    for group in actual_groups:
        for evidence_id in group.intersection(expected_union):
            occurrences[evidence_id] = occurrences.get(evidence_id, 0) + 1
    if any(count > 1 for count in occurrences.values()):
        failures.append("split child evidence is duplicated across activities")
    actual_union = set().union(*actual_groups) if actual_groups else set()
    if actual_union != expected_union:
        failures.append("split child evidence union differs from reviewed value")
    normalized_actual = sorted((tuple(sorted(group)) for group in actual_groups))
    normalized_expected = sorted((tuple(sorted(group)) for group in expected_groups))
    if normalized_actual != normalized_expected:
        failures.append("split child evidence partition differs from reviewed value")
    return failures


def evaluate_regression_cases(
    cases: Iterable[Mapping[str, Any]],
    proposals: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Evaluate current proposals against exact evidence-bound corrections."""
    proposal_rows = list(proposals)
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = {}
    for proposal in proposal_rows:
        if target := proposal_target(proposal):
            grouped.setdefault(target, []).append(proposal)
    results: list[dict[str, Any]] = []
    for raw in cases:
        case = dict(raw)
        target = (str(case.get("activity_id") or ""), str(case.get("evidence_fingerprint") or ""))
        rows = grouped.get(target, [])
        decision = str(case.get("decision") or "")
        failures: list[str] = []
        is_split = "split" in case.get("correction_categories", ())
        if is_split:
            failures.extend(_split_failures(case, proposal_rows))
            status = "pass" if not failures else "fail"
        elif decision == "skip":
            if rows:
                failures.append("previously skipped activity reappeared")
            status = "pass" if not failures else "fail"
        elif not rows:
            if case.get("expected_presence", True):
                failures.append("reviewed activity is missing")
                status = "fail"
            else:
                status = "not_applicable"
        else:
            patch = case.get("expected_field_patch") if isinstance(case.get("expected_field_patch"), Mapping) else {}
            for field, operation in patch.items():
                expected = operation.get("value") if isinstance(operation, Mapping) else None
                if field == "duration_minutes":
                    actual = sum(int(row.get(field) or 0) for row in rows)
                    if actual != expected:
                        failures.append(f"{field} differs from reviewed value")
                elif field == "start":
                    actual = min(str(row.get(field) or "") for row in rows)
                    if actual != expected:
                        failures.append(f"{field} differs from reviewed value")
                elif field == "end":
                    actual = max(str(row.get(field) or "") for row in rows)
                    if actual != expected:
                        failures.append(f"{field} differs from reviewed value")
                elif any(row.get(field) != expected for row in rows):
                    failures.append(f"{field} differs from reviewed value")
            status = "pass" if not failures else "fail"
        results.append(
            {
                "regression_case_id": case.get("regression_case_id"),
                "activity_id": target[0],
                "evidence_fingerprint": target[1],
                "decision": decision,
                "status": status,
                "failures": failures,
            }
        )
    results.sort(key=lambda value: str(value.get("regression_case_id") or ""))
    return {
        "schema_version": SCHEMA_VERSION,
        "results": results,
        "summary": {
            status: sum(result["status"] == status for result in results)
            for status in ("pass", "fail", "not_applicable")
        },
    }
