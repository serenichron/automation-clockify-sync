"""Validate explicit local audited equivalence against original source/POST proof.

This adapter does not discover semantic equivalence. Its declarations are a
trusted, manually source-accounted input; approval of the resulting native plan
adopts that exact declaration. Shared evidence, timing and wording imply nothing.
"""
from __future__ import annotations

import hashlib
import copy
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from scripts import evidence_ledger, review_corrections


SCHEMA = "clockify-source-accounted-adoptions/v1"
CURRENT_LIVE_SCHEMA = "clockify-source-accounted-adoptions/v2"
MANUAL_MEETING_SCHEMA = "clockify-source-accounted-adoptions/v3"
MANUAL_FIELDS = frozenset({
    "adoption_kind", "operation_anchor", "same_meeting_confirmed", "canonical_meeting_id",
    "current_review_id", "current_payload_digest", "current_proposal_digest", "source_fingerprint",
    "clockify_entry_id", "retained_entry_digest", "artifacts",
})
ARTIFACTS = frozenset({
    "prior_proposals", "source_ledger", "current_proposals", "current_source_ledger",
    "native_plan", "native_approval", "native_events",
})
FIELDS = frozenset({
    "operation_anchor", "current_review_id", "current_payload_digest", "prior_review_id",
    "clockify_entry_id", "artifacts",
})
PRIOR_ARTIFACTS = ARTIFACTS - {"current_proposals", "current_source_ledger"}


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
    before = path.stat()
    content = path.read_bytes()
    after = path.stat()
    identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    if identity(before) != identity(after) or path.resolve() != path:
        raise AdoptionError("source adoption artifact changed while being read")
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


def _source_events(proposal: Mapping[str, Any], ledger: Mapping[str, Any]) -> list[dict[str, Any]]:
    ids = set(proposal["provenance"]["evidence_ids"])
    return [copy.deepcopy(event) for event in ledger["events"] if event["evidence_id"] in ids]


def validate_prior_native_proof(proof: Mapping[str, Any]) -> Mapping[str, Any]:
    """Recheck original approved payload/target/source/event links, without IO."""
    from scripts import clockify_native_sheet_post as native
    expected = {"clockify_entry_id", "prior_review_id", "workspace_id", "member_id", "payload", "payload_digest",
                "prior_proposal", "source_events", "native_plan", "native_approval", "native_intent",
                "native_confirmed", "artifact_handles", "proof_digest"}
    if not isinstance(proof, Mapping) or set(proof) != expected or proof["proof_digest"] != native._document_digest(proof, "proof_digest"):
        raise AdoptionError("sealed prior native proof integrity differs")
    plan, approval = proof["native_plan"], proof["native_approval"]
    if (plan.get("workspace_id"), plan.get("member_id")) != (proof["workspace_id"], proof["member_id"]):
        raise AdoptionError("source adoption prior Clockify target differs")
    approval_digest = native._validate_approval(plan, approval, native.legacy._parse(approval["approved_at"]))
    rows = [row for row in plan["entries"] if row["review_id"] == proof["prior_review_id"]]
    if len(rows) != 1 or "prior_entry_credit" in rows[0]:
        raise AdoptionError("source adoption must bind one original native POST row")
    row = rows[0]
    if row["payload_digest"] != native._digest(row["payload"]) or row["payload_digest"] != proof["payload_digest"] or row["payload"] != proof["payload"]:
        raise AdoptionError("source adoption prior payload digest differs")
    _validate_source_target(proof["prior_proposal"], proof["source_events"], proof["prior_review_id"])
    if proof["prior_proposal"].get("duration_seconds") != _seconds(proof["payload"]):
        raise AdoptionError("source adoption prior exact duration differs")
    terminal, intent = proof["native_confirmed"], proof["native_intent"]
    for event in (terminal, intent):
        if (event["event_digest"] != native._document_digest(event, "event_digest")
                or event.get("approval_digest") != approval_digest or event.get("plan_digest") != plan["plan_digest"]
                or event.get("review_id") != row["review_id"] or event.get("payload_digest") != row["payload_digest"]):
            raise AdoptionError("source adoption lacks an exact native confirmed-entry binding")
    if (intent.get("event_type") != "intent" or terminal.get("event_type") != "confirmed"
            or terminal.get("clockify_entry_id") != proof["clockify_entry_id"]
            or terminal.get("disposition") not in {"created", "recovered_after_ambiguous_response"}
            or not isinstance(terminal.get("readback_digest"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", terminal["readback_digest"])):
        raise AdoptionError("source adoption lacks an exact native confirmed-entry binding")
    if not isinstance(proof["artifact_handles"], Mapping) or set(proof["artifact_handles"]) != PRIOR_ARTIFACTS:
        raise AdoptionError("sealed prior source artifact inventory differs")
    return proof


def _validate_source_target(proposal: Mapping[str, Any], events: Sequence[Mapping[str, Any]], review_id: str) -> None:
    if (not isinstance(proposal, Mapping) or type(proposal.get("allocation_segment")) is not int
            or f'{proposal.get("review_activity_key")}-s{proposal["allocation_segment"]:02d}' != review_id
            or type(proposal.get("duration_seconds")) is not int or proposal["duration_seconds"] <= 0):
        raise AdoptionError("sealed source proposal identity or duration differs")
    ids = proposal["provenance"]["evidence_ids"]
    if not isinstance(ids, list) or not ids or len(ids) != len(set(ids)):
        raise AdoptionError("sealed source proposal evidence differs")
    parsed = [evidence_ledger.EvidenceEvent.from_document(event) for event in events]
    if len(parsed) != len(ids) or set(ids) != {event.evidence_id for event in parsed}:
        raise AdoptionError("sealed source evidence membership differs")


def verify_prior_native_proof(artifact_handles: Mapping[str, Any], prior_review_id: str,
                              entry_id: str, *, workspace_id: str, member_id: str,
                              capture_cache: dict[tuple[str, str], bytes] | None = None) -> dict[str, Any]:
    from scripts import clockify_native_sheet_post as native
    if not isinstance(artifact_handles, Mapping) or set(artifact_handles) != PRIOR_ARTIFACTS:
        raise AdoptionError("source adoption is missing original native proof artifacts")
    cache = {} if capture_cache is None else capture_cache
    captures = {name: _capture(handle, cache) for name, handle in artifact_handles.items()}
    documents = {name: json.loads(raw) for name, raw in captures.items() if name != "native_events"}
    prior = _source(documents["prior_proposals"], documents["source_ledger"], prior_review_id)
    plan, approval = documents["native_plan"], documents["native_approval"]
    if (plan.get("workspace_id"), plan.get("member_id")) != (workspace_id, member_id):
        raise AdoptionError("source adoption prior Clockify target differs")
    approval_digest = native._validate_approval(plan, approval, native.legacy._parse(approval["approved_at"]))
    rows = [row for row in plan["entries"] if row["review_id"] == prior_review_id]
    if len(rows) != 1:
        raise AdoptionError("source adoption must bind one original native POST row")
    intents, _responses, confirmed = native._confirmed_by_review(native._decode_events(captures["native_events"]), approval_digest, plan["plan_digest"])
    if prior_review_id not in intents or prior_review_id not in confirmed:
        raise AdoptionError("source adoption lacks an exact native confirmed-entry binding")
    proof = dict(clockify_entry_id=entry_id, prior_review_id=prior_review_id, workspace_id=workspace_id, member_id=member_id,
                 payload=rows[0]["payload"], payload_digest=rows[0]["payload_digest"], prior_proposal=prior,
                 source_events=_source_events(prior, documents["source_ledger"]), native_plan=plan, native_approval=approval,
                 native_intent=intents[prior_review_id], native_confirmed=confirmed[prior_review_id], artifact_handles=dict(artifact_handles))
    proof["proof_digest"] = native._digest(proof)
    validate_prior_native_proof(proof)
    return copy.deepcopy(proof)


def validate_manual_meeting_credit(value: Mapping[str, Any], item: Mapping[str, Any], *,
                                   workspace_id: str, member_id: str) -> Mapping[str, Any]:
    """Recheck explicit equivalence and sealed source/native facts, without IO."""
    from scripts import clockify_native_sheet_post as native, work_accounting_pipeline as pipeline
    fields = {"verification_basis", "declaration", "declaration_digest", "clockify_entry_id", "payload",
              "readback_digest", "retained_entry", "retained_entry_digest", "current_proposal",
              "source_events", "source_manifest", "workspace_id", "member_id", "approved_current_seconds",
              "retained_native_seconds", "credit_digest"}
    try:
        if (not isinstance(value, Mapping) or set(value) != fields
                or value["verification_basis"] != "audited_manual_meeting"
                or value["credit_digest"] != native._document_digest(value, "credit_digest")):
            raise AdoptionError("manual meeting sealed credit integrity differs")
        declaration = value["declaration"]
        if not isinstance(declaration, Mapping) or set(declaration) != MANUAL_FIELDS:
            raise AdoptionError("manual meeting declaration proof shape differs")
        anchor = declaration["operation_anchor"]
        if (declaration["adoption_kind"] != "manual_meeting"
                or declaration["same_meeting_confirmed"] is not True
                or not isinstance(anchor, str) or not anchor.strip() or len(anchor) > 512 or not anchor.isprintable()
                or value["declaration_digest"] != native._digest(declaration)):
            raise AdoptionError("manual meeting requires explicit audited same-meeting authority")
        if (declaration["current_review_id"] != item["review_id"]
                or declaration["current_payload_digest"] != item["payload_digest"]
                or item["payload_digest"] != native._digest(item["payload"])
                or value["workspace_id"] != workspace_id or value["member_id"] != member_id
                or not workspace_id or not member_id
                or declaration["clockify_entry_id"] != value["clockify_entry_id"]
                or declaration["retained_entry_digest"] != value["retained_entry_digest"]
                or value["retained_entry_digest"] != native._digest(value["retained_entry"])):
            raise AdoptionError("manual meeting current review or native identity binding differs")
        retained = value["retained_entry"]
        if (not current_live_matches(value["payload"], retained, workspace_id=workspace_id,
                                     member_id=member_id, entry_id=value["clockify_entry_id"])
                or not value["clockify_entry_id"]
                or native._live_digest([retained]) != value["readback_digest"]):
            raise AdoptionError("manual meeting retained entry or target differs")
        current, payload = item["payload"], value["payload"]
        if (any(current[key] != payload[key] for key in ("projectId", "taskId", "billable"))
                or sorted(current["tagIds"]) != sorted(payload["tagIds"])
                or type(current["billable"]) is not bool
                or not (native.legacy._parse(payload["start"]) <= native.legacy._parse(current["start"])
                        < native.legacy._parse(current["end"]) <= native.legacy._parse(payload["end"]))):
            raise AdoptionError("manual meeting route or full interval coverage differs")
        proposal = value["current_proposal"]
        _validate_source_target(proposal, value["source_events"], item["review_id"])
        fingerprint = review_corrections.evidence_fingerprint(proposal["provenance"]["evidence_ids"])
        if (declaration["source_fingerprint"] != fingerprint
                or declaration["current_proposal_digest"] != recurring_proposal_digest(proposal)
                or proposal["duration_seconds"] != _seconds(current)
                or value["approved_current_seconds"] != _seconds(current)
                or value["retained_native_seconds"] != _seconds(payload)
                or native.legacy._utc(proposal["start"]) != current["start"]
                or native.legacy._utc(proposal["end"]) != current["end"]
                or not proposal.get("clockify_project_suffix")
                or not current["projectId"].endswith(proposal["clockify_project_suffix"])
                or len(current["tagIds"]) != len(proposal["tag_suffixes"])
                or len(set(proposal["tag_suffixes"])) != len(proposal["tag_suffixes"])
                or not all(suffix and sum(tag.endswith(suffix) for tag in current["tagIds"]) == 1
                           for suffix in proposal["tag_suffixes"])
                or proposal["billable"] is not current["billable"]):
            raise AdoptionError("manual meeting reviewed proposal or source fingerprint differs")
        recordings, errors = pipeline._recording_events(value["source_events"], value["source_manifest"])
        if (errors or len(recordings) != 1
                or recordings[0]["meeting"].canonical_id != declaration["canonical_meeting_id"]
                or proposal["provenance"].get("canonical_meeting_id") != declaration["canonical_meeting_id"]
                or set(recordings[0]["source_evidence_ids"]) != set(proposal["provenance"]["evidence_ids"])):
            raise AdoptionError("manual meeting canonical recording source differs")
        representative = next((event for event in recordings[0]["events"] if event["source_type"] == "fathom"), recordings[0]["events"][0])
        start, end = pipeline._canonical_meeting_span(recordings[0]["meeting"], representative)
        if not (start <= native.legacy._parse(current["start"]) < native.legacy._parse(current["end"]) <= end):
            raise AdoptionError("manual meeting approved interval exceeds recording source")
        return value
    except (ValueError, TypeError, KeyError, AttributeError, IndexError) as error:
        if isinstance(error, AdoptionError):
            raise
        raise AdoptionError("manual meeting sealed proof is invalid") from error


def _manual_meeting_credit(declaration: Mapping[str, Any], current: Mapping[str, Any], *,
                           workspace_id: str, member_id: str, capture_cache: dict[tuple[str, str], bytes],
                           live_entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    from scripts import clockify_native_sheet_post as native
    if not isinstance(declaration, Mapping) or set(declaration) != MANUAL_FIELDS:
        raise AdoptionError("manual meeting declaration proof shape differs")
    handles = declaration["artifacts"]
    if not isinstance(handles, Mapping) or set(handles) != {"current_proposals", "current_source_ledger", "retained_entry_snapshot"}:
        raise AdoptionError("manual meeting original source or retained snapshot is missing")
    documents = {key: json.loads(_capture(handle, capture_cache)) for key, handle in handles.items()}
    proposal = _source(documents["current_proposals"], documents["current_source_ledger"], current["review_id"])
    retained = documents["retained_entry_snapshot"]
    matches = [entry for entry in live_entries if entry.get("id") == declaration["clockify_entry_id"]]
    if len(matches) != 1 or native._digest(matches[0]) != native._digest(retained):
        raise AdoptionError("manual meeting fresh snapshot is missing, duplicated or drifted")
    normalized = native._normalized_live(retained)
    if normalized is None:
        raise AdoptionError("manual meeting retained native interval is invalid")
    payload = {key: normalized[key] for key in ("start", "end", "projectId", "tagIds", "taskId", "billable")}
    payload["description"] = retained.get("description")
    value = dict(verification_basis="audited_manual_meeting", declaration=dict(declaration),
                 declaration_digest=native._digest(declaration), clockify_entry_id=declaration["clockify_entry_id"],
                 payload=payload, readback_digest=native._live_digest([retained]), retained_entry=copy.deepcopy(retained),
                 retained_entry_digest=native._digest(retained), current_proposal=copy.deepcopy(proposal),
                 source_events=_source_events(proposal, documents["current_source_ledger"]),
                 source_manifest=copy.deepcopy(documents["current_source_ledger"]["manifest"]),
                 workspace_id=workspace_id, member_id=member_id,
                 approved_current_seconds=_seconds(current["payload"]), retained_native_seconds=_seconds(payload))
    value["credit_digest"] = native._digest(value)
    validate_manual_meeting_credit(value, current, workspace_id=workspace_id, member_id=member_id)
    return value


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
    present = _source(documents["current_proposals"], documents["current_source_ledger"], declaration["current_review_id"])
    proof = verify_prior_native_proof({name: handles[name] for name in PRIOR_ARTIFACTS}, declaration["prior_review_id"],
                                      declaration["clockify_entry_id"], workspace_id=workspace_id, member_id=member_id,
                                      capture_cache=capture_cache)
    prior, plan, approval = proof["prior_proposal"], proof["native_plan"], proof["native_approval"]
    row = next(row for row in plan["entries"] if row["review_id"] == declaration["prior_review_id"])
    approval_digest = native._digest(approval)
    seconds = _seconds(row["payload"])
    if (seconds != _seconds(current["payload"]) or prior.get("duration_seconds") != seconds
            or present.get("duration_seconds") != seconds
            or type(prior.get("duration_seconds")) is not int
            or type(present.get("duration_seconds")) is not int):
        raise AdoptionError("source adoption is not a one-to-one exact-duration accomplishment")
    terminal = proof["native_confirmed"]
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


def _review_id(proposal: Mapping[str, Any]) -> str:
    return f'{proposal["review_activity_key"]}-s{proposal["allocation_segment"]:02d}'


def recurring_proposal_digest(proposal: Mapping[str, Any]) -> str:
    """Bind all proposal facts except the ephemeral S/P presentation ID."""
    from scripts import clockify_native_sheet_post as native
    return native._digest({key: value for key, value in proposal.items() if key != "id"})


def _recording(event: Mapping[str, Any]) -> tuple[str, str, str] | None:
    from scripts import clockify_native_sheet_post as native
    if event.get("source_type") not in {"fathom", "calendly"}:
        return None
    ref, span = event.get("source_ref"), event.get("raw_source_span")
    if not isinstance(ref, Mapping) or not isinstance(span, Mapping) or not ref.get("source_id"):
        return None
    return (str(ref["source_id"]), native.legacy._utc(span["start"]), native.legacy._utc(span["end"]))


def validate_recurring_credit(record: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an audited coverage unit using sealed original facts only."""
    from scripts import clockify_native_sheet_post as native
    required = {"schema_version", "record_type", "verification_basis", "operation_anchor", "coverage_kind",
                "current_targets", "prior_proofs", "credit_digest"}
    try:
        if (not isinstance(record, Mapping) or set(record) != required or record["schema_version"] != 2
                or record["record_type"] != review_corrections.VERIFIED_POSTED_CREDIT
                or record["verification_basis"] != "preserved_collection_snapshot"
                or record["credit_digest"] != native._document_digest(record, "credit_digest")):
            raise AdoptionError("sealed recurring credit integrity or basis differs")
        anchor = record["operation_anchor"]
        if not isinstance(anchor, str) or not anchor.strip() or len(anchor) > 512 or not anchor.isprintable():
            raise AdoptionError("recurring credit needs an explicit audited operation anchor")
        targets, priors = record["current_targets"], record["prior_proofs"]
        if not isinstance(targets, list) or not targets or not isinstance(priors, list) or not priors:
            raise AdoptionError("recurring credit needs exact current and prior targets")
        review_ids = set()
        for target in targets:
            if set(target) != {"proposal", "proposal_digest", "source_events"} or target["proposal_digest"] != recurring_proposal_digest(target["proposal"]):
                raise AdoptionError("recurring current target digest differs")
            proposal = target["proposal"]
            review_id = _review_id(proposal)
            if review_id in review_ids or not proposal.get("activity_id") or not proposal.get("candidate_key"):
                raise AdoptionError("recurring current target identity is absent or repeated")
            review_ids.add(review_id)
            _validate_source_target(proposal, target["source_events"], review_id)
        for proof in priors:
            validate_prior_native_proof(proof)
        ids = [proof["clockify_entry_id"] for proof in priors]
        scopes = {(proof["workspace_id"], proof["member_id"]) for proof in priors}
        if len(ids) != len(set(ids)) or len(scopes) != 1:
            raise AdoptionError("recurring coverage repeats a prior entry or target scope")
        prior_seconds = sum(_seconds(proof["payload"]) for proof in priors)
        current_seconds = sum(target["proposal"]["duration_seconds"] for target in targets)
        kind = record["coverage_kind"]
        if kind == "equal_accomplishment":
            if len(priors) != 1:
                raise AdoptionError("equal recurring coverage must have one prior")
            # Explicit aliases describe the same accomplishment, not additive work.
            if any(target["proposal"]["duration_seconds"] != prior_seconds for target in targets):
                raise AdoptionError("recurring exact group duration differs")
        elif kind == "disjoint_aggregate":
            if len(targets) != 1 or prior_seconds != current_seconds:
                raise AdoptionError("recurring exact group duration differs")
            if len(priors) < 2:
                raise AdoptionError("aggregate recurring coverage must have disjoint priors")
            spans = sorted((native.legacy._parse(proof["payload"]["start"]), native.legacy._parse(proof["payload"]["end"])) for proof in priors)
            if any(right[0] < left[1] for left, right in zip(spans, spans[1:])):
                raise AdoptionError("aggregate prior coverage overlaps")
        elif kind == "whole_recording_aliases":
            if len(priors) != 1:
                raise AdoptionError("whole recording coverage must consume one prior")
            proof = priors[0]
            objects = {_recording(event) for event in proof["source_events"]}
            if None in objects or len(objects) != 1:
                raise AdoptionError("whole recording lacks exact canonical source timing")
            recording = next(iter(objects))
            if recording[1:] != (proof["payload"]["start"], proof["payload"]["end"]):
                raise AdoptionError("approved prior does not account for the whole recording")
            for target in targets:
                if {_recording(event) for event in target["source_events"]} != objects or target["proposal"]["duration_seconds"] > prior_seconds:
                    raise AdoptionError("recording alias does not bind the same whole source")
        else:
            raise AdoptionError("unsupported explicit recurring coverage kind")
        return copy.deepcopy(dict(record))
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        if isinstance(error, AdoptionError):
            raise
        raise AdoptionError("sealed recurring proof binding is invalid") from error


def build_recurring_credit(declaration: Mapping[str, Any], *, workspace_id: str, member_id: str) -> dict[str, Any]:
    """Seal one explicitly audited accomplishment; never discover aliases."""
    from scripts import clockify_native_sheet_post as native
    try:
        if set(declaration) != {"operation_anchor", "coverage_kind", "current_review_ids", "artifacts", "prior_entries"}:
            raise AdoptionError("recurring declaration proof shape differs")
        cache: dict[tuple[str, str], bytes] = {}
        handles = declaration["artifacts"]
        if set(handles) != {"current_proposals", "current_source_ledger"}:
            raise AdoptionError("recurring declaration lacks current source artifacts")
        proposals = json.loads(_capture(handles["current_proposals"], cache))
        ledger = json.loads(_capture(handles["current_source_ledger"], cache))
        targets = []
        for review_id in declaration["current_review_ids"]:
            proposal = _source(proposals, ledger, review_id)
            targets.append(dict(proposal=proposal, proposal_digest=recurring_proposal_digest(proposal), source_events=_source_events(proposal, ledger)))
        priors = [verify_prior_native_proof(prior["artifacts"], prior["prior_review_id"], prior["clockify_entry_id"],
                                           workspace_id=workspace_id, member_id=member_id, capture_cache=cache)
                  for prior in declaration["prior_entries"]]
        record = dict(schema_version=2, record_type=review_corrections.VERIFIED_POSTED_CREDIT,
                      verification_basis="preserved_collection_snapshot", operation_anchor=declaration["operation_anchor"],
                      coverage_kind=declaration["coverage_kind"], current_targets=targets, prior_proofs=priors)
        record["credit_digest"] = native._digest(record)
        return validate_recurring_credit(record)
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as error:
        if isinstance(error, AdoptionError):
            raise
        raise AdoptionError("recurring original proof validation failed") from error


def credits(snapshot: Mapping[str, Any], entries: Sequence[Mapping[str, Any]], *,
            workspace_id: str, member_id: str,
            live_entries: Sequence[Mapping[str, Any]] = ()) -> dict[str, dict[str, Any]]:
    """Validate every explicitly declared adoption; invalid proof never credits."""
    if (not isinstance(snapshot, Mapping) or set(snapshot) != {"schema_version", "declarations"}
            or snapshot.get("schema_version") not in {SCHEMA, CURRENT_LIVE_SCHEMA, MANUAL_MEETING_SCHEMA}
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
            if snapshot["schema_version"] == MANUAL_MEETING_SCHEMA:
                value = _manual_meeting_credit(declaration, by_review[review_id], workspace_id=workspace_id,
                                              member_id=member_id, capture_cache=capture_cache, live_entries=live_entries)
            else:
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
            if isinstance(value, Mapping) and value.get("verification_basis") == "audited_manual_meeting":
                validate_manual_meeting_credit(value, item, workspace_id=workspace_id, member_id=member_id)
                if value["clockify_entry_id"] in used_ids or item["review_id"] in result:
                    raise AdoptionError("manual meeting credit repeats a review or retained native entry")
                used_ids.add(value["clockify_entry_id"])
                result[item["review_id"]] = value
                continue
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
