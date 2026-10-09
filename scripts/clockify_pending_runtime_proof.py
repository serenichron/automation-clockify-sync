"""Read-only revalidation of existing, byte-bound pending runtime proofs."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping

from scripts import clockify_source_adoptions as adoptions
from scripts import clockify_pending_review_selection as pending


def _native_selection(expected: Mapping[str, Any], cache: dict) -> tuple[dict, dict]:
    """Recover rows only through current native verification of original inputs."""
    from scripts import clockify_sheet_publish as publisher

    selection = json.loads(adoptions._capture(expected["selection"], cache))
    current = pending._source(selection["sources"][selection["current_source"]], cache)
    verified = pending.verify(
        bindings_path=Path(expected["selection"]["path"]),
        source_dir=Path(current["artifacts"]["proposals"]["path"]).parent,
        proposals=current["proposals"], spreadsheet_id=expected["spreadsheet_id"],
        sheet_title=expected["sheet_title"], run_id=current["run_id"],
        project_allowlist=publisher.project_allowlist(current["routing"]),
    )
    if verified["receipt"] != expected:
        raise ValueError("pending current native proof differs")
    return verified, current


def _warning_projection(actual: Mapping[str, Any], expected: Mapping[str, Any], rows: list) -> None:
    """Prove both pre-reason hashes and exact ordered facts after stable dedup."""
    old, new = actual["native_review_warnings"], expected["native_review_warnings"]
    if not isinstance(old, Mapping) or not isinstance(new, Mapping) or set(old) != set(new):
        raise ValueError("pending historical warning inventory differs")
    for review_id, warnings in old.items():
        if not isinstance(warnings, list) or any(not isinstance(warning, dict) for warning in warnings):
            raise ValueError("pending historical warning facts are invalid")
        unique, seen = [], set()
        for warning in warnings:
            identity = pending.digest(warning)
            if identity not in seen:
                unique.append(warning)
                seen.add(identity)
        if pending.digest(unique) != pending.digest(new[review_id]):
            raise ValueError("pending historical warning facts or order differ")
    for proof in (actual, expected):
        projected = copy.deepcopy(rows)
        for row in projected:
            if row[0] in proof["native_review_warnings"]:
                warnings = proof["native_review_warnings"][row[0]]
                row[12] = json.dumps(warnings, ensure_ascii=False, sort_keys=True) if warnings else ""
        if pending.digest(projected) != proof["native_projection_rows_sha256"]:
            raise ValueError("pending original/current native projection digest differs")


def _legacy_summaries(actual: Mapping[str, Any], expected: Mapping[str, Any], verified: dict, current: dict) -> set:
    """Derive omitted summaries from unchanged, verified non-recording credits."""
    from scripts import clockify_sheet_publish as publisher

    added = set(expected) - set(actual)
    if not added:
        return added
    if not added <= {"saved_credit_seconds", "fixed_recording_rows", "fixed_recording_checks"}:
        raise ValueError("pending unrecognized current proof field")
    by_id = {publisher.stable_review_id(proposal): proposal for proposal in current["proposals"]}
    originals = [by_id[review_id] for review_id in actual["selected_current_ids"]]
    originals.extend(record["proposal"] for record in verified["prior"] if record["disposition"] == "retain")
    if (any(proposal.get("provenance", {}).get("canonical_meeting_id") for proposal in originals)
            or actual.get("saved_credit_rows") != len(originals)
            or actual.get("saved_credit_minutes") != sum(proposal["duration_minutes"] for proposal in originals)):
        raise ValueError("pending original non-recording credit summary differs")
    derived = {"saved_credit_seconds": sum(proposal["duration_seconds"] for proposal in originals),
               "fixed_recording_rows": 0, "fixed_recording_checks": []}
    if any(expected[key] != derived[key] or type(expected[key]) is not type(derived[key]) for key in added):
        raise ValueError("pending derived current credit summary differs")
    return added


def verify_recorded(actual: object, expected: object) -> dict[str, Any]:
    """Preserve authentic originals only after exact native semantic revalidation."""
    schema = "pending-review-selection-acceptance/v1"
    roles = {"consumer", "pipeline", "allocator"}
    if (not isinstance(actual, Mapping) or not isinstance(expected, Mapping)
        or actual.get("schema_version") != schema or expected.get("schema_version") != schema
        or not isinstance(actual.get("runtime_artifacts"), Mapping)
        or not isinstance(expected.get("runtime_artifacts"), Mapping)
        or set(actual["runtime_artifacts"]) != roles
        or set(expected["runtime_artifacts"]) != roles):
        raise ValueError("pending runtime proof inventory differs")
    for proof in (actual, expected):
        unsigned = {key: value for key, value in proof.items() if key != "acceptance_sha256"}
        if proof.get("acceptance_sha256") != pending.digest(unsigned):
            raise ValueError("pending acceptance digest differs")
    try:
        cache: dict[tuple[str, str], bytes] = {}
        for role in roles:
            recorded = actual["runtime_artifacts"][role]
            current = expected["runtime_artifacts"][role]
            adoptions._capture(recorded, cache)
            adoptions._capture(current, cache)
        for proof in (actual, expected):
            handles = proof["runtime_artifacts"].values()
            if (len({handle["path"] for handle in handles}) != len(roles)
                    or len({handle["sha256"] for handle in handles}) != len(roles)):
                raise ValueError("pending runtime roles are not distinct")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ValueError("pending runtime artifact has drifted") from exc
    reconstructed = {**expected, "runtime_artifacts": actual["runtime_artifacts"]}
    warning_fields = {"native_review_warnings", "native_projection_rows_sha256"}
    warning_drift = any(actual.get(key) != expected.get(key) for key in warning_fields)
    runtime_drift = any(actual["runtime_artifacts"][role]["sha256"] != expected["runtime_artifacts"][role]["sha256"]
                        for role in roles)
    if warning_drift or runtime_drift or set(expected) - set(actual):
        try:
            verified, current = _native_selection(expected, cache)
            if warning_drift:
                _warning_projection(actual, expected, verified["rows"])
                for key in warning_fields:
                    reconstructed[key] = actual[key]
            for key in _legacy_summaries(actual, expected, verified, current):
                del reconstructed[key]
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ValueError("pending selection proof differs") from exc
    unsigned = {key: value for key, value in reconstructed.items() if key != "acceptance_sha256"}
    reconstructed["acceptance_sha256"] = pending.digest(unsigned)
    if actual != reconstructed:
        raise ValueError("pending selection proof differs")
    return reconstructed
