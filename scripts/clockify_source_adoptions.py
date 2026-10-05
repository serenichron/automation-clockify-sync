"""Validate explicit local audited equivalence against original source/POST proof.

This adapter does not discover semantic equivalence. Its declarations are a
trusted, manually source-accounted input; approval of the resulting native plan
adopts that exact declaration. Shared evidence, timing and wording imply nothing.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from scripts import evidence_ledger, review_corrections


SCHEMA = "clockify-source-accounted-adoptions/v1"
CURRENT_LIVE_SCHEMA = "clockify-source-accounted-adoptions/v2"
ARTIFACTS = frozenset({
    "prior_proposals", "source_ledger", "current_proposals", "current_source_ledger",
    "native_plan", "native_approval", "native_events",
})
FIELDS = frozenset({
    "operation_anchor", "current_review_id", "current_payload_digest", "prior_review_id",
    "clockify_entry_id", "artifacts",
})


class AdoptionError(ValueError):
    pass


def _capture(handle: Mapping[str, Any], cache: dict[tuple[str, str], bytes]) -> bytes:
    if not isinstance(handle, Mapping) or set(handle) != {"path", "sha256"}:
        raise AdoptionError("source adoption artifact handle is invalid")
    if not isinstance(handle["path"], str) or not isinstance(handle["sha256"], str):
        raise AdoptionError("source adoption artifact handle is invalid")
    key = (handle["path"], handle["sha256"])
    if key in cache:
        return cache[key]
    path = Path(handle["path"])
    if not path.is_absolute() or path.resolve() != path or not path.is_file():
        raise AdoptionError("source adoption artifact must be an absolute original file")
    content = path.read_bytes()
    if handle["sha256"] != "sha256:" + hashlib.sha256(content).hexdigest():
        raise AdoptionError("source adoption artifact bytes drifted")
    cache[key] = content
    return content


def _source(proposals: Any, ledger_document: Any, review_id: str) -> Mapping[str, Any]:
    if not isinstance(proposals, list) or not all(isinstance(value, Mapping) for value in proposals):
        raise AdoptionError("source adoption original proposals are invalid")
    matches = [value for value in proposals if (
        isinstance(value.get("review_activity_key"), str)
        and type(value.get("allocation_segment")) is int
        and f'{value["review_activity_key"]}-s{value["allocation_segment"]:02d}' == review_id
    )]
    if len(matches) != 1:
        raise AdoptionError("source adoption review identity is not unique in original proposals")
    proposal = matches[0]
    provenance = proposal.get("provenance")
    ids = provenance.get("evidence_ids") if isinstance(provenance, Mapping) else None
    if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids):
        raise AdoptionError("source adoption proposal lacks exact source evidence")
    review_corrections.evidence_fingerprint(ids)
    if not isinstance(ledger_document, Mapping) or ledger_document.get("schema_version") != evidence_ledger.SCHEMA_VERSION:
        raise AdoptionError("source adoption ledger schema is invalid")
    manifest = evidence_ledger.LedgerManifest.from_document(ledger_document["manifest"])
    events = tuple(evidence_ledger.EvidenceEvent.from_document(value) for value in ledger_document["events"])
    ledger = evidence_ledger.EvidenceLedger(events, manifest.source_inventory, manifest.timezone, manifest.member_identities)
    ledger.validate(manifest)
    if not set(ids).issubset({event.evidence_id for event in ledger.events}):
        raise AdoptionError("source adoption evidence is absent from the original ledger")
    return proposal


def _seconds(payload: Mapping[str, Any]) -> int:
    from scripts import clockify_native_sheet_post as native
    start, end = native.legacy._parse(payload["start"]), native.legacy._parse(payload["end"])
    seconds = (end - start).total_seconds()
    if seconds <= 0 or not seconds.is_integer():
        raise AdoptionError("source adoption duration must be positive exact seconds")
    return int(seconds)


def current_live_matches(payload: Mapping[str, Any], entry: Mapping[str, Any], *,
                         workspace_id: str, member_id: str, entry_id: str) -> bool:
    """Exact approved fields and target, without historical-recipe inference."""
    from scripts import clockify_native_sheet_post as native
    return (entry.get("id") == entry_id and entry.get("workspaceId") == workspace_id
            and entry.get("userId") == member_id and native._payload_matches(payload, entry)
            and entry.get("description") == payload["description"]
            and entry.get("projectId") == payload["projectId"]
            and entry.get("taskId") == payload.get("taskId"))


def _credit(declaration: Mapping[str, Any], current: Mapping[str, Any], *,
            workspace_id: str, member_id: str,
            capture_cache: dict[tuple[str, str], bytes],
            current_live: bool = False,
            live_entries: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    from scripts import clockify_native_sheet_post as native
    if not isinstance(declaration, Mapping) or set(declaration) != FIELDS:
        raise AdoptionError("source adoption declaration has an invalid proof shape")
    anchor = declaration["operation_anchor"]
    if not isinstance(anchor, str) or not anchor.strip() or len(anchor) > 512 or not anchor.isprintable():
        raise AdoptionError("source adoption needs an explicit audited operation anchor")
    if (declaration["current_review_id"] != current["review_id"]
            or declaration["current_payload_digest"] != current["payload_digest"]
            or current["payload_digest"] != native._digest(current["payload"])):
        raise AdoptionError("source adoption current review/payload binding differs")
    handles = declaration["artifacts"]
    if not isinstance(handles, Mapping) or set(handles) != ARTIFACTS:
        raise AdoptionError("source adoption is missing original source or confirmed POST artifacts")
    captures = {name: _capture(handle, capture_cache) for name, handle in handles.items()}
    documents = {name: json.loads(content) for name, content in captures.items() if name != "native_events"}
    prior = _source(documents["prior_proposals"], documents["source_ledger"], declaration["prior_review_id"])
    present = _source(documents["current_proposals"], documents["current_source_ledger"], declaration["current_review_id"])
    plan, approval = documents["native_plan"], documents["native_approval"]
    if (plan.get("workspace_id"), plan.get("member_id")) != (workspace_id, member_id):
        raise AdoptionError("source adoption prior Clockify target differs")
    # Historical approval is checked at its actual validity boundary, not now.
    approval_digest = native._validate_approval(plan, approval, native.legacy._parse(approval["approved_at"]))
    rows = [row for row in plan["entries"] if row["review_id"] == declaration["prior_review_id"]]
    if len(rows) != 1 or "prior_entry_credit" in rows[0]:
        raise AdoptionError("source adoption must bind one original native POST row")
    row = rows[0]
    if row["payload_digest"] != native._digest(row["payload"]):
        raise AdoptionError("source adoption prior payload digest differs")
    seconds = _seconds(row["payload"])
    if (seconds != _seconds(current["payload"]) or prior.get("duration_seconds") != seconds
            or present.get("duration_seconds") != seconds
            or type(prior.get("duration_seconds")) is not int
            or type(present.get("duration_seconds")) is not int):
        raise AdoptionError("source adoption is not a one-to-one exact-duration accomplishment")
    records = native._decode_events(captures["native_events"])
    intents, _responses, confirmed = native._confirmed_by_review(records, approval_digest, plan["plan_digest"])
    terminal, intent = confirmed.get(row["review_id"]), intents.get(row["review_id"])
    if (terminal is None or intent is None
            or terminal.get("clockify_entry_id") != declaration["clockify_entry_id"]
            or terminal.get("payload_digest") != row["payload_digest"]
            or intent.get("payload_digest") != row["payload_digest"]
            or terminal.get("disposition") not in {"created", "recovered_after_ambiguous_response"}
            or not isinstance(terminal.get("readback_digest"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", terminal["readback_digest"])):
        raise AdoptionError("source adoption lacks an exact native confirmed-entry binding")
    result = {
        "declaration": dict(declaration), "declaration_digest": native._digest(declaration),
        "clockify_entry_id": terminal["clockify_entry_id"], "payload": dict(row["payload"]),
        "readback_digest": terminal["readback_digest"],
        "prior_source_fingerprint": review_corrections.evidence_fingerprint(prior["provenance"]["evidence_ids"]),
        "current_source_fingerprint": review_corrections.evidence_fingerprint(present["provenance"]["evidence_ids"]),
        "confirmed_binding": {
            "workspace_id": workspace_id, "member_id": member_id,
            "prior_plan_digest": plan["plan_digest"], "prior_approval_digest": approval_digest,
            "prior_review_id": row["review_id"], "prior_payload_digest": row["payload_digest"],
            "clockify_entry_id": terminal["clockify_entry_id"],
            "readback_digest": terminal["readback_digest"], "event_digest": terminal["event_digest"],
        },
    }
    if current_live:
        matches = [entry for entry in live_entries if entry.get("id") == terminal["clockify_entry_id"]]
        if len(matches) != 1:
            raise AdoptionError("source adoption current live entry is missing or duplicated")
        if not current_live_matches(row["payload"], matches[0], workspace_id=workspace_id,
                                    member_id=member_id, entry_id=terminal["clockify_entry_id"]):
            raise AdoptionError("source adoption current live target or approved payload differs")
        # The original confirmed digest remains historical, not reproduced.
        result.update(verification_basis="current_live_snapshot",
                      historical_readback_digest=terminal["readback_digest"],
                      readback_digest=native._live_digest(matches))
    result["credit_digest"] = native._digest(result)
    return result


def credits(snapshot: Mapping[str, Any], entries: Sequence[Mapping[str, Any]], *,
            workspace_id: str, member_id: str,
            live_entries: Sequence[Mapping[str, Any]] = ()) -> dict[str, dict[str, Any]]:
    """Validate every explicitly declared adoption; invalid proof never credits."""
    if (not isinstance(snapshot, Mapping) or set(snapshot) != {"schema_version", "declarations"}
            or snapshot.get("schema_version") not in {SCHEMA, CURRENT_LIVE_SCHEMA}
            or not isinstance(snapshot["declarations"], list)):
        raise AdoptionError("source adoption snapshot schema is invalid")
    by_review = {entry["review_id"]: entry for entry in entries}
    result: dict[str, dict[str, Any]] = {}
    used_ids: set[str] = set()
    capture_cache: dict[tuple[str, str], bytes] = {}
    try:
        for declaration in snapshot["declarations"]:
            review_id = declaration["current_review_id"]
            if review_id not in by_review or review_id in result:
                raise AdoptionError("source adoption current review identity is absent or repeated")
            value = _credit(declaration, by_review[review_id], workspace_id=workspace_id,
                            member_id=member_id, capture_cache=capture_cache,
                            current_live=snapshot["schema_version"] == CURRENT_LIVE_SCHEMA,
                            live_entries=live_entries)
            if value["clockify_entry_id"] in used_ids:
                raise AdoptionError("source adoption reuses one prior entry for different current rows")
            used_ids.add(value["clockify_entry_id"])
            result[review_id] = value
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        if isinstance(error, AdoptionError):
            raise
        raise AdoptionError("source adoption proof validation failed") from error
    return result


def validate_sealed_credits(entries: Sequence[Mapping[str, Any]], *,
                            workspace_id: str, member_id: str) -> dict[str, Mapping[str, Any]]:
    """Consume only approved plan facts; original handles are provenance only.

The plan/approval digest is the adoption authority. This validates the internal
bindings that the plan-time artifact consumer sealed, without replaying IO.
"""
    from scripts import clockify_native_sheet_post as native
    result: dict[str, Mapping[str, Any]] = {}
    used_ids: set[str] = set()
    try:
        for item in entries:
            if "prior_entry_credit" not in item:
                continue
            value = item["prior_entry_credit"]
            expected_fields = {
                "declaration", "declaration_digest", "clockify_entry_id", "payload", "readback_digest",
                "prior_source_fingerprint", "current_source_fingerprint", "confirmed_binding", "credit_digest",
            }
            current_live = isinstance(value, Mapping) and "verification_basis" in value
            if current_live:
                expected_fields |= {"verification_basis", "historical_readback_digest"}
            if (not isinstance(value, Mapping) or set(value) != expected_fields
                    or value["credit_digest"] != native._document_digest(value, "credit_digest")):
                raise AdoptionError("sealed source adoption credit integrity differs")
            declaration, binding = value["declaration"], value["confirmed_binding"]
            if current_live and (
                value["verification_basis"] != "current_live_snapshot"
                or not isinstance(value["readback_digest"], str)
                or not re.fullmatch(r"[0-9a-f]{64}", value["readback_digest"])
                or value["historical_readback_digest"] != binding["readback_digest"]
            ):
                raise AdoptionError("sealed source adoption verification basis differs")
            if (not isinstance(declaration, Mapping) or set(declaration) != FIELDS
                    or value["declaration_digest"] != native._digest(declaration)
                    or declaration["current_review_id"] != item["review_id"]
                    or declaration["current_payload_digest"] != item["payload_digest"]
                    or item["payload_digest"] != native._digest(item["payload"])
                    or binding["workspace_id"] != workspace_id or binding["member_id"] != member_id
                    or binding["prior_review_id"] != declaration["prior_review_id"]
                    or binding["prior_payload_digest"] != native._digest(value["payload"])
                    or binding["clockify_entry_id"] != value["clockify_entry_id"]
                    or value["clockify_entry_id"] != declaration["clockify_entry_id"]
                    or (not current_live and binding["readback_digest"] != value["readback_digest"])
                    or _seconds(value["payload"]) != _seconds(item["payload"])):
                raise AdoptionError("sealed source adoption proof binding differs")
            if value["clockify_entry_id"] in used_ids or item["review_id"] in result:
                raise AdoptionError("sealed source adoption repeats a review or prior entry")
            used_ids.add(value["clockify_entry_id"])
            result[item["review_id"]] = value
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        if isinstance(error, AdoptionError):
            raise
        raise AdoptionError("sealed source adoption proof is invalid") from error
    return result
