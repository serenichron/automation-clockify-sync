"""Read-only portability checks for existing, byte-bound pending runtime proofs."""
from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
from typing import Any, Mapping

from scripts import clockify_source_adoptions as adoptions
from scripts import clockify_pending_review_selection as pending


@contextmanager
def _authenticated_graph(path: Path, *, pinned_digest: str, source_dir: Path,
                         spreadsheet_id: str, sheet_title: str):
    """Cycle-only scope after its independent native state/adoption pin checks."""
    document = json.loads(adoptions._capture({"path": str(path), "sha256": pinned_digest}, {}))
    if document.get("schema_version") == "sheet-publication-result/v1":
        if (document.get("status") not in {"published", "verified-existing"}
                or document.get("clockify_writes") != 0
                or document.get("external_writes") is not (document["status"] == "published")):
            raise ValueError("historical pending publication graph differs")
        publications = document.get("publications")
    elif document.get("schema_version") in {"clockify-review-delivery/v1", "clockify-review-partial-publication/v1"}:
        from scripts import clockify_review_cycle as cycle
        if (document.get("receipt_digest") != cycle._value_digest({key: value for key, value in document.items()
                                                                  if key != "receipt_digest"})
                or document.get("target") != {"spreadsheet_id": spreadsheet_id, "sheet_title": sheet_title}
                or document.get("source", {}).get("run_id") != source_dir.name):
            raise ValueError("historical pending delivery graph differs")
        publications = document.get("publication_receipts")
    else:
        raise ValueError("historical pending native graph schema differs")
    if not isinstance(publications, list):
        raise ValueError("historical pending native graph publications differ")
    admissions = {}
    for publication in publications:
        if not isinstance(publication, Mapping):
            raise ValueError("historical pending publication inventory differs")
        receipt = publication.get("pending_selection")
        if receipt is None:
            continue
        if (not isinstance(receipt, Mapping) or receipt.get("schema_version") != "pending-review-selection-acceptance/v1"
                or publication.get("spreadsheet_id") != spreadsheet_id or publication.get("sheet_title") != sheet_title
                or receipt.get("spreadsheet_id") != spreadsheet_id or receipt.get("sheet_title") != sheet_title
                or receipt.get("current_source_artifacts", {}).get("proposals", {}).get("path") != str(source_dir / "proposals.json")
                or receipt.get("projected_rows_sha256") != publication.get("rows_sha256") and "presentation" not in publication):
            raise ValueError("historical pending exact publication identity differs")
        if not receipt.get("covered_source_outcomes"):
            continue
        pending._validate_native_admission(receipt)
        key = pending._admission_key(receipt["selection"], source_dir, spreadsheet_id, sheet_title)
        if key in admissions:
            raise ValueError("historical pending admission repeats")
        admissions[key] = receipt
    token = pending._RECORDED_ADMISSIONS.set(admissions)
    try:
        yield
    finally:
        pending._RECORDED_ADMISSIONS.reset(token)


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
    if "native_admission" in actual or "native_admission" in expected:
        pending._validate_native_admission(actual)
        pending._validate_native_admission(expected)
        if actual["native_admission"]["acceptance_bindings_sha256"] != expected["native_admission"]["acceptance_bindings_sha256"]:
            raise ValueError("pending native admission immutable bindings differ")
        reconstructed["native_admission"] = actual["native_admission"]
    unsigned = {key: value for key, value in reconstructed.items() if key != "acceptance_sha256"}
    reconstructed["acceptance_sha256"] = pending.digest(unsigned)
    if actual != reconstructed:
        raise ValueError("pending selection proof differs")
    return reconstructed
