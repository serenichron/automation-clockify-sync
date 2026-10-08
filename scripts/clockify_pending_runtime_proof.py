"""Read-only portability checks for existing, byte-bound pending runtime proofs."""
from __future__ import annotations

from typing import Any, Mapping

from scripts import clockify_source_adoptions as adoptions
from scripts import clockify_pending_review_selection as pending


def verify_recorded(actual: object, expected: object) -> dict[str, Any]:
    """Keep original locations only when every role and the whole native proof match."""
    schema = "pending-review-selection-acceptance/v1"
    roles = {"consumer", "pipeline", "allocator"}
    if (not isinstance(actual, Mapping) or not isinstance(expected, Mapping)
        or actual.get("schema_version") != schema or expected.get("schema_version") != schema
        or not isinstance(actual.get("runtime_artifacts"), Mapping)
        or not isinstance(expected.get("runtime_artifacts"), Mapping)
        or set(actual["runtime_artifacts"]) != roles
        or set(expected["runtime_artifacts"]) != roles):
        raise ValueError("pending runtime proof inventory differs")
    try:
        cache: dict[tuple[str, str], bytes] = {}
        for role in roles:
            recorded = actual["runtime_artifacts"][role]
            current = expected["runtime_artifacts"][role]
            adoptions._capture(recorded, cache)
            adoptions._capture(current, cache)
            if recorded["sha256"] != current["sha256"]:
                raise ValueError("pending runtime role bytes differ")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise ValueError("pending runtime artifact has drifted") from exc
    reconstructed = {**expected, "runtime_artifacts": actual["runtime_artifacts"]}
    unsigned = {key: value for key, value in reconstructed.items() if key != "acceptance_sha256"}
    reconstructed["acceptance_sha256"] = pending.digest(unsigned)
    if actual != reconstructed:
        raise ValueError("pending selection proof differs")
    return reconstructed
