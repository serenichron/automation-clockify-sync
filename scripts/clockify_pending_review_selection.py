"""Opt-in projection of immutable native allocations already pending review.

This is a publication consumer receipt, not a combined completed source run,
new schedule, approved decision, or posted Clockify credit. No discovery,
inference or provider access occurs here. All input paths are explicit handles.
"""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from scripts import clockify_source_adoptions as adoptions
from scripts import work_accounting_pipeline as pipeline, work_allocator as allocator

SCHEMA = "pending-review-selection/v1"


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def artifact_handle(path: Path) -> dict[str, str]:
    handle = {"path": str(Path(path).absolute()), "sha256": "sha256:" + hashlib.sha256(Path(path).read_bytes()).hexdigest()}
    adoptions._capture(handle, {})
    return handle


def _time(value: str) -> dt.datetime:
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("pending selection timestamps require a timezone")
    return parsed.astimezone(dt.timezone.utc)


def _atom(event: Mapping[str, Any]) -> tuple[Any, ...]:
    attrs, ref = event["attributes"], event["source_ref"]
    if not ref.get("machine") or not ref.get("session_id") or attrs.get("role") not in {"user", "assistant", "system", "tool"}:
        # Non-session metadata is never aliased by prose or intervals. Exact
        # source records, excluding only their evidence-ID spelling, suffice.
        return ("native-record", digest({k: v for k, v in event.items() if k not in {"evidence_id", "legacy_aliases"}}))
    content = attrs.get("content", "")
    if not isinstance(content, str):
        raise ValueError("pending selection requires exact session/content source identity")
    # Legacy session snapshots contain local naive timestamps. Use the native
    # parser's explicit Bucharest contract, never the process host timezone.
    observed = pipeline._parse_dt(event["observed_at"])
    if observed is None:
        raise ValueError("pending selection canonical observation timestamp differs")
    return (ref["machine"], ref["session_id"], observed.astimezone(dt.timezone.utc).isoformat(),
            attrs.get("role"), attrs.get("kind"), hashlib.sha256(content.encode()).hexdigest())


def _source(record: Mapping[str, Any], cache: dict) -> dict[str, Any]:
    from scripts import collector_receipts
    if set(record) != {"run_id", "basis", "artifacts"} or not isinstance(record["run_id"], str):
        raise ValueError("pending selection source record differs")
    handles = record["artifacts"]
    common = {"proposals", "ledger", "accounting", "routing", "receipt"}
    extra = {"quality", "replay", "replay_proposals", "replay_accounting", "primary_proposals"}
    if not isinstance(handles, dict) or not common <= handles.keys() or handles.keys() - common - extra:
        raise ValueError("pending selection source artifact inventory differs")
    originals = {name: json.loads(adoptions._capture(handle, cache)) for name, handle in handles.items()}
    proposals, accounting, receipt = originals["proposals"], originals["accounting"], originals["receipt"]
    if not isinstance(proposals, list) or not isinstance(accounting.get("proposals"), list) or any(p not in accounting["proposals"] for p in proposals):
        raise ValueError("pending selection saved proposals differ from native accounting output")
    if record["basis"] == "completed-review-run":
        receipt_path = Path(handles["receipt"]["path"])
        bundle = collector_receipts.load_completion_bundle(receipt_path, run_dir=receipt_path.parent)
        if receipt_path.parent.name != record["run_id"] or bundle.replay:
            raise ValueError("pending selection completed source identity differs")
        if (Path(handles["proposals"]["path"]) != receipt_path.parent / "proposals.json"
                or Path(handles["accounting"]["path"]) != receipt_path.parent / "work-accounting-result.json"
                or Path(handles["ledger"]["path"]) != receipt_path.parent / "evidence/evidence-ledger.json"):
            raise ValueError("pending selection completion artifacts differ from native run")
    elif record["basis"] in {"supplemental-native-packet", "composed-native-packet"}:
        if not {"replay_proposals", "replay_accounting"} <= handles.keys():
            raise ValueError("pending selection saved packet requires original replay artifacts")
        primary = originals.get("primary_proposals", proposals)
        if primary != originals["replay_proposals"] or accounting != originals["replay_accounting"]:
            raise ValueError("pending selection native saved replay differs")
        if not isinstance(proposals, list) or any(p not in primary for p in proposals):
            raise ValueError("pending selection published leaf differs from native packet")
        if record["basis"] == "supplemental-native-packet":
            if receipt.get("schema_version") != "macbook-only-supplemental-native-accounting/v1" or receipt.get("strict_full_source_quality_run") is not False:
                raise ValueError("pending selection supplemental lineage differs")
            replays = receipt["deterministic_accounting_replay"]
            for filename, name in (("evidence-ledger.private.json", "ledger"), ("routing.private.json", "routing")):
                if receipt["input_hashes"].get(filename) != handles[name]["sha256"][7:]:
                    raise ValueError("pending selection supplemental input receipt differs")
        else:
            if receipt.get("type") != "local_composition_not_whole_ledger_provider_acceptance":
                raise ValueError("pending selection composition lineage differs")
            replays = receipt["native_accounting_replay"]
            for filename, name in (("ledger.private.json", "ledger"), ("routing.private.json", "routing")):
                if receipt["source_bindings"][filename]["sha256"] != handles[name]["sha256"][7:]:
                    raise ValueError("pending selection composition input receipt differs")
        for filename, name in (("proposals.json", "primary_proposals" if "primary_proposals" in handles else "proposals"),
                               ("work-accounting-result.json", "accounting")):
            item = replays[filename]
            expected = handles[name]["sha256"][7:]
            replay_name = "replay_proposals" if filename == "proposals.json" else "replay_accounting"
            if handles[replay_name]["sha256"] != handles[name]["sha256"]:
                raise ValueError("pending selection native replay bytes differ")
            if item.get("byte_equal") is not True or item.get("sha256", item.get("primary_sha256")) != expected:
                raise ValueError("pending selection native packet receipt bytes differ")
            if "replay_sha256" in item and item["replay_sha256"] != expected:
                raise ValueError("pending selection native packet replay receipt differs")
    else:
        raise ValueError("pending selection source lineage basis is unsupported")
    from scripts import clockify_sheet_publish as publisher
    if not proposals:
        raise ValueError("pending selection source proposals are empty")
    # _source validates the complete original ledger/manifest once. Subsequent
    # selected rows reuse that validated inventory, never a weakened ledger.
    adoptions._source(proposals, originals["ledger"], publisher.stable_review_id(proposals[0]))
    evidence_ids = {event["evidence_id"] for event in originals["ledger"]["events"]}
    by_id = {}
    for proposal in proposals:
        review_id = publisher.stable_review_id(proposal)
        ids = proposal.get("provenance", {}).get("evidence_ids")
        if (review_id in by_id or not isinstance(ids, list) or not ids or len(set(ids)) != len(ids)
                or not set(ids) <= evidence_ids):
            raise ValueError("pending selection source review identity/evidence differs")
        adoptions.review_corrections.evidence_fingerprint(ids)
        by_id[review_id] = proposal
    return {**record, **originals, "by_review_id": by_id}


def _cited_timing_contexts(cited, timing):
    contexts = {}
    for event in cited:
        context = timing.get(event["evidence_id"])
        if context is None:
            continue
        if event.get("source_type") in {"claude_bursts_event", "codex_sessions_event"} and not any(
            other.get("source_type") == event.get("source_type")
            and pipeline.semantic_analyzer._semantic_context_key(other) == pipeline.semantic_analyzer._semantic_context_key(event)
            and pipeline._attributes(other).get("role") == "assistant"
            and pipeline._attributes(other).get("kind", "message") == "message"
            and not pipeline._attributes(other).get("tool_name")
            and str(pipeline._attributes(other).get("content") or "").strip() for other in cited):
            continue
        contexts[context["pool_id"]] = context
    return contexts


def _meeting_credit(proposal: Mapping[str, Any], source: Mapping[str, Any]) -> dict[str, Any]:
    """Authenticate a saved fixed recording allocation, not an effort demand."""
    accounting = source["accounting"]
    provenance = proposal.get("provenance") or {}
    canonical_id = provenance.get("canonical_meeting_id")
    if (not canonical_id or sum(p == proposal for p in accounting["proposals"]) != 1):
        raise ValueError("pending selection fixed meeting accounting allocation differs")
    records = [record for record in accounting.get("fathom_reconciliation", [])
               if record.get("canonical_id") == canonical_id]
    if len(records) != 1:
        raise ValueError("pending selection fixed meeting reconciliation is absent or ambiguous")
    record = records[0]
    activities = record.get("activity_ids", [record.get("activity_id")])
    if record.get("status") != "proposed" or proposal["activity_id"] not in activities:
        raise ValueError("pending selection fixed meeting reconciliation allocation differs")
    recordings, exceptions = pipeline._recording_events(source["ledger"]["events"], source["ledger"]["manifest"])
    matches = [entry for entry in recordings if entry["meeting"].canonical_id == canonical_id]
    if (len(matches) != 1 or any(set(exception["source_evidence_ids"]) & set(provenance["evidence_ids"])
                                 for exception in exceptions)):
        raise ValueError("pending selection fixed meeting canonical ledger identity differs")
    entry = matches[0]
    evidence_ids = set(entry["source_evidence_ids"])
    if (set(record.get("source_evidence_ids", [])) != evidence_ids
            or not set(provenance["evidence_ids"]) & evidence_ids):
        raise ValueError("pending selection fixed meeting canonical evidence differs")
    representative = next((event for event in entry["events"] if event.get("source_type") == "fathom"), entry["events"][0])
    lo, hi = pipeline._canonical_meeting_span(entry["meeting"], representative)
    start, end = _time(proposal["start"]), _time(proposal["end"])
    if not lo <= start < end <= hi:
        raise ValueError("pending selection fixed meeting credit exceeds canonical recording")
    return {"activity_id": proposal["activity_id"], "canonical_meeting_id": canonical_id,
            "saved_start": proposal["start"], "saved_end": proposal["end"],
            "credited_minutes": proposal["duration_minutes"], "source_evidence_ids": sorted(evidence_ids),
            "basis": "saved_native_fixed_meeting_allocation"}


def _covered_native_evidence(declaration: Mapping[str, Any], cache: dict) -> tuple[dict, dict]:
    """Authenticate the original POST and a complete current read-only GET."""
    live = json.loads(adoptions._capture(declaration["fresh_clockify_capture"], cache))
    target = live.get("verified_target", {})
    finished = _time(live["finished_utc"])
    age = dt.datetime.now(dt.timezone.utc) - finished
    if (not re.fullmatch(r"clockify(?:-[a-z]+)?-live-readonly/v1", str(live.get("schema") or ""))
            or live.get("all_pages_returned") is not True or live.get("external_mutations") is not False
            or live.get("cache_mutations") is not False or not live.get("get_requests")
            or any(request.get("method") != "GET" for request in live["get_requests"])
            or not dt.timedelta(0) <= age <= dt.timedelta(hours=1)
            or set(target) != {"workspace_id", "member_id"} or not all(target.values())):
        raise ValueError("pending selection covered recording requires fresh authenticated GET proof")
    proof = adoptions.verify_prior_native_proof(declaration["prior_proof_artifacts"], declaration["prior_review_id"],
        declaration["clockify_entry_id"], workspace_id=target["workspace_id"], member_id=target["member_id"], capture_cache=cache)
    entries = [entry for entry in live["entries"] if entry.get("id") == proof["clockify_entry_id"]]
    if len(entries) != 1:
        raise ValueError("pending selection covered recording current provider payload differs")
    return proof, entries[0]


def _covered_posted_row(proof: Mapping[str, Any], declaration: Mapping[str, Any],
                        proposal: Mapping[str, Any], captured: Mapping[str, list[Any]]) -> list[Any]:
    from scripts import clockify_native_sheet_post as native, clockify_sheet_publish as publisher
    prior = proof["prior_proposal"]
    row = captured[proof["prior_review_id"]]
    timezone = proof["native_plan"]["timezone"]
    if (row[9] != "Approved" or row[13] != "posted" or row[6] != prior["activity_id"]
            or row[11] != Path(declaration["prior_proof_artifacts"]["prior_proposals"]["path"]).parent.name
            or row[8] != proof["payload"]["description"] or row[4] != prior["client_project"]
            or row[5] != ", ".join(prior["tag_names"])
            or not publisher._same_cell(row[3], proposal["duration_seconds"] / 60)
            or any(native._utc(native._parse_sheet_time(row[index], timezone)) != proof["payload"][key]
                   for index, key in ((1, "start"), (2, "end")))):
        raise ValueError("pending selection covered recording Approved/posted Sheet representation differs")
    return row


def _legacy_serialization_graph(graph: Mapping[str, Any], proposal: Mapping[str, Any],
                                ledger: Mapping[str, Any], source_path: str, cache: dict) -> None:
    """Observe one pinned native graph without adoption or durable effects."""
    from scripts import collector_receipts, clockify_review_cycle as cycle, clockify_review_run as review
    fields = {"runs_root", "semantic_completion", "raw_completion", "raw_ancestor", "observer_stage"}
    run = Path(source_path).parent
    if (not isinstance(graph, dict) or set(graph) != fields
            or Path(graph["semantic_completion"]["path"]) != run / "completion-bundle.json"):
        raise ValueError("pending selection legacy serialization native graph differs")
    adoptions._capture(graph["semantic_completion"], cache)
    bundle = collector_receipts.load_completion_bundle(run / "completion-bundle.json", run_dir=run)
    documents = {artifact.kind: json.loads(adoptions._capture({"path": str(artifact.path), "sha256": artifact.digest}, cache))
                 for artifact in bundle.artifacts if artifact.kind in {"evidence_ledger", "accounting_result"}}
    if (bundle.replay or documents["evidence_ledger"] != ledger
            or documents["accounting_result"]["proposals"].count(proposal) != 1):
        raise ValueError("pending selection legacy serialization saved source differs")
    try:
        with cycle._selected_runs_config({}, graph["runs_root"]):
            ancestor = review._verified_replay_inference_context(run)
            stage = graph["observer_stage"]
            if (str(ancestor) != graph["raw_ancestor"] or not isinstance(stage, dict)
                    or stage.get("run_dir") != str(ancestor)
                    or Path(graph["raw_completion"]["path"]) != ancestor / "completion-bundle.json"):
                raise ValueError("pending selection legacy serialization raw ancestry differs")
            adoptions._capture(graph["raw_completion"], cache)
            cycle._audit_bundle(stage)
    except (cycle.CycleError, review.ReviewRunError) as exc:
        raise ValueError("pending selection legacy serialization native graph proof failed") from exc


def _legacy_serialization(witness: Mapping[str, Any], current: Mapping[str, Any],
                          proof: Mapping[str, Any], events: Sequence[Mapping[str, Any]], cache: dict) -> dict[str, Any]:
    """Authenticate an explicitly reviewed serialization, never tolerant atoms."""
    from zoneinfo import ZoneInfo
    mapping = witness["legacy_serialization"]
    if not isinstance(mapping, dict) or set(mapping) != {"current_graph", "prior_graph", "event_pairs", "interval_offsets_seconds"}:
        raise ValueError("pending selection legacy serialization witness differs")
    prior, proposal = proof["prior_proposal"], current["proposal"]
    offsets = {key: (_time(proposal[key]) - _time(prior[key])).total_seconds() for key in ("start", "end")}
    saved_offsets = mapping["interval_offsets_seconds"]
    if (not isinstance(saved_offsets, dict) or set(saved_offsets) != {"start", "end"}
            or any(type(value) not in {int, float} for value in saved_offsets.values())
            or saved_offsets != offsets or offsets["start"] != offsets["end"]):
        raise ValueError("pending selection legacy serialization saved interval offset differs")
    current_by_id = {e["evidence_id"]: e for e in events}
    prior_by_id = {e["evidence_id"]: e for e in proof["source_events"]}
    pairs = mapping["event_pairs"]
    fields = {"current_evidence_id", "prior_evidence_id", "current_event_sha256", "prior_event_sha256", "current_observed_at", "prior_observed_at"}
    if (not isinstance(pairs, list) or not pairs or len(pairs) != len(current_by_id) or len(pairs) != len(prior_by_id)
            or any(not isinstance(pair, dict) or set(pair) != fields for pair in pairs)
            or len({p["current_evidence_id"] for p in pairs}) != len(pairs)
            or len({p["prior_evidence_id"] for p in pairs}) != len(pairs)
            or {p["current_evidence_id"] for p in pairs} != current_by_id.keys()
            or {p["prior_evidence_id"] for p in pairs} != prior_by_id.keys()):
        raise ValueError("pending selection legacy serialization complete event pairs differ")
    for pair in pairs:
        event, old = current_by_id[pair["current_evidence_id"]], prior_by_id[pair["prior_evidence_id"]]
        attrs, old_attrs = event["attributes"], old["attributes"]
        ref = event["source_ref"]
        if (pair["current_event_sha256"] != digest(event) or pair["prior_event_sha256"] != digest(old)
                or pair["current_observed_at"] != event["observed_at"] or pair["prior_observed_at"] != old["observed_at"]
                or not all(ref.get(key) for key in ("machine", "session_id", "source_id"))
                or type(ref.get("ordinal")) is not int or ref != old["source_ref"]
                or event["source_type"] not in {"codex_sessions_event", "claude_bursts_event", "hermes_sessions_event", "hermes_db_sessions_event"}
                or event["source_type"] != old["source_type"]
                or any(attrs.get(key) != old_attrs.get(key) for key in ("role", "kind", "content", "tool_name"))
                or old["observed_at"] != _time(event["observed_at"]).astimezone(ZoneInfo("Europe/Bucharest")).strftime("%Y-%m-%d %H:%M")):
            raise ValueError("pending selection legacy serialization exact source/minute mapping differs")
    _legacy_serialization_graph(mapping["current_graph"], proposal, current["source"]["ledger"],
                                 current["source"]["artifacts"]["proposals"]["path"], cache)
    prior_ledger = json.loads(adoptions._capture(proof["artifact_handles"]["source_ledger"], cache))
    _legacy_serialization_graph(mapping["prior_graph"], prior, prior_ledger,
                                 proof["artifact_handles"]["prior_proposals"]["path"], cache)
    return {"legacy_serialization_interval_offsets_seconds": offsets, "legacy_serialization_event_pair_count": len(pairs)}


def _covered_accomplishment(declaration: Mapping[str, Any], current: Mapping[str, Any],
                            captured: Mapping[str, list[Any]], cache: dict) -> dict[str, Any]:
    """A coordinator-reviewed exact native outcome, never semantic inference."""
    from scripts import clockify_native_sheet_post as native
    fields = {"current_review_id", "prior_review_id", "clockify_entry_id", "prior_proof_artifacts",
              "fresh_clockify_capture", "semantic_adjudication", "observed_current_project"}
    if set(declaration) != fields or declaration["current_review_id"] != current["review_id"]:
        raise ValueError("pending selection covered accomplishment declaration differs")
    proposal = current["proposal"]
    if proposal.get("provenance", {}).get("canonical_meeting_id"):
        raise ValueError("pending selection covered accomplishment cannot replace recording proof")
    proof, entry = _covered_native_evidence(declaration, cache)
    prior = proof["prior_proposal"]
    witness = json.loads(adoptions._capture(declaration["semantic_adjudication"], cache))
    legacy = witness.get("schema_version") == "pending-covered-legacy-serialization-adjudication/v1"
    if (prior.get("provenance", {}).get("canonical_meeting_id")
            or type(proposal.get("duration_seconds")) is not int or proposal["duration_seconds"] <= 0
            or proposal["duration_seconds"] != prior["duration_seconds"]
            or proposal["duration_seconds"] != adoptions._seconds(proof["payload"])
            or (_time(proposal["end"]) - _time(proposal["start"])).total_seconds() != proposal["duration_seconds"]
            or any((not legacy and _time(proposal[key]) != _time(prior[key])) or _time(prior[key]) != _time(proof["payload"][key])
                   for key in ("start", "end"))):
        raise ValueError("pending selection covered accomplishment whole interval differs")
    source_events = adoptions._source_events(proposal, current["source"]["ledger"])
    atoms = {_atom(event) for event in source_events}
    prior_atoms = {_atom(event) for event in proof["source_events"]}
    if (not atoms or (not legacy and atoms != prior_atoms) or atoms != current["atoms"]
            or len(source_events) != len(atoms) or len(proof["source_events"]) != len(prior_atoms)
            or any(event.get("source_type") in {"clockify", "existing_clockify", "fathom"} for event in source_events)):
        raise ValueError("pending selection covered accomplishment complete source atoms differ")
    witness_fields = {"schema_version", "current_review_id", "prior_review_id", "current_proposal_sha256",
                      "prior_proposal_sha256", "current_semantic", "prior_semantic", "canonical_atoms_sha256",
                      "same_bounded_accomplishment", "basis", "operation_anchor", "normalized_accomplishment",
                      "adjudication_rationale"}
    if legacy:
        witness_fields.add("legacy_serialization")
    normalized = witness.get("normalized_accomplishment")
    if (set(witness) != witness_fields or witness["schema_version"] not in {"pending-covered-accomplishment-adjudication/v1", "pending-covered-legacy-serialization-adjudication/v1"}
            or witness["current_review_id"] != current["review_id"] or witness["prior_review_id"] != proof["prior_review_id"]
            or witness["current_proposal_sha256"] != digest(proposal) or witness["prior_proposal_sha256"] != digest(prior)
            or witness["canonical_atoms_sha256"] != digest(sorted(digest(atom) for atom in atoms))
            or witness["same_bounded_accomplishment"] is not True or witness["basis"] != "coordinator_source_event_review"
            or not isinstance(normalized, dict) or set(normalized) != {"action", "object", "outcome"}
            or any(not isinstance(value, str) or not value.strip() for value in normalized.values())
            or any(not isinstance(witness[name], str) or not witness[name].strip()
                   for name in ("operation_anchor", "adjudication_rationale"))):
        raise ValueError("pending selection covered accomplishment adjudication differs")
    serialization = _legacy_serialization(witness, current, proof, source_events, cache) if legacy else {}
    current_handles = current["source"]["artifacts"]
    for name, saved, source_path in (
            ("current_semantic", proposal, current_handles["proposals"]["path"]),
            ("prior_semantic", prior, declaration["prior_proof_artifacts"]["prior_proposals"]["path"])):
        binding = witness[name]
        if (not isinstance(binding, dict) or set(binding) != {"artifact", "activity"}
                or Path(binding["artifact"]["path"]) != Path(source_path).parent / "semantic-analysis.json"):
            raise ValueError("pending selection covered accomplishment native semantic source differs")
        semantic = json.loads(adoptions._capture(binding["artifact"], cache))
        matches = [activity for activity in semantic.get("activities", []) if activity.get("activity_id") == saved["activity_id"]]
        if (len(matches) != 1 or matches[0] != binding["activity"]
                or not isinstance(matches[0].get("evidence_ids"), list)
                or len(matches[0]["evidence_ids"]) != len(set(matches[0]["evidence_ids"]))
                or set(matches[0]["evidence_ids"]) != set(saved["provenance"]["evidence_ids"])
                or any(not isinstance(matches[0].get(field), str) or not matches[0][field].strip()
                       for field in ("action", "object", "outcome"))):
            raise ValueError("pending selection covered accomplishment native semantic activity differs")
    observed = declaration["observed_current_project"]
    if (not isinstance(observed, dict) or set(observed) != {"project_id", "project_name", "routing_snapshot"}
            or not isinstance(observed["project_id"], str) or not observed["project_id"]
            or not isinstance(observed["project_name"], str) or not observed["project_name"]
            or observed["routing_snapshot"] != current_handles["routing"]):
        raise ValueError("pending selection covered accomplishment observed route declaration differs")
    routing = json.loads(adoptions._capture(observed["routing_snapshot"], cache))
    routes = {(route.get("project_suffix"), route.get("project_name")) for route in native._route_values(routing)
              if isinstance(route.get("project_suffix"), str) and route["project_suffix"]
              and observed["project_id"].endswith(route["project_suffix"])}
    if len(routes) != 1 or next(iter(routes))[1] != observed["project_name"]:
        raise ValueError("pending selection covered accomplishment observed project is not uniquely configured")
    current_payload = {**proof["payload"], "projectId": observed["project_id"]}
    if not adoptions.current_live_matches(current_payload, entry, workspace_id=proof["workspace_id"],
            member_id=proof["member_id"], entry_id=proof["clockify_entry_id"]):
        raise ValueError("pending selection covered accomplishment current provider payload differs")
    row = _covered_posted_row(proof, declaration, proposal, captured)
    return {"review_id": current["review_id"], "prior_review_id": proof["prior_review_id"],
            "clockify_entry_id": proof["clockify_entry_id"], "basis": "verified_posted_source_representation_only",
            "covered_seconds": proposal["duration_seconds"], "prior_proof_artifacts": declaration["prior_proof_artifacts"],
            "prior_proof_digest": proof["proof_digest"], "captured_posted_row": row,
            "fresh_clockify_capture": declaration["fresh_clockify_capture"], "provider_entry_sha256": digest(entry),
            "semantic_adjudication": declaration["semantic_adjudication"], "semantic_adjudication_sha256": digest(witness),
            "historical_approved_project_id": proof["payload"]["projectId"], "observed_current_project": observed,
            "current_project_authorized_by_historical_approval": observed["project_id"] == proof["payload"]["projectId"],
            "authority_boundary": "trusted_coordinator_source_adjudication_not_human_financial_or_project_approval",
            "new_pending_rows": 0, "accounting_credit_mutations": 0, "clockify_writes": 0, **serialization}


def _covered_source_outcome(declaration: Mapping[str, Any], current: Mapping[str, Any],
                            captured: Mapping[str, list[Any]], cache: dict) -> dict[str, Any]:
    """Represent one whole already-posted source, without adding credit."""
    if "semantic_adjudication" in declaration:
        return _covered_accomplishment(declaration, current, captured, cache)
    if (set(declaration) != {"current_review_id", "prior_review_id", "clockify_entry_id", "prior_proof_artifacts", "fresh_clockify_capture"}
            or declaration["current_review_id"] != current["review_id"]):
        raise ValueError("pending selection covered recording declaration differs")
    proof, entry = _covered_native_evidence(declaration, cache)
    if not adoptions.current_live_matches(proof["payload"], entry,
            workspace_id=proof["workspace_id"], member_id=proof["member_id"], entry_id=proof["clockify_entry_id"]):
        raise ValueError("pending selection covered recording current provider payload differs")
    proposal = current["proposal"]
    prior = proof["prior_proposal"]
    source_events = adoptions._source_events(proposal, current["source"]["ledger"])
    recording_identity = adoptions._native_meeting_identity(source_events, proposal)
    canonical_id = proposal.get("provenance", {}).get("canonical_meeting_id")
    recordings, errors = pipeline._recording_events(current["source"]["ledger"]["events"], current["source"]["ledger"]["manifest"])
    if (errors or not canonical_id or canonical_id != prior.get("provenance", {}).get("canonical_meeting_id")
            or sum(entry["meeting"].canonical_id == canonical_id for entry in recordings) != 1
            or recording_identity != adoptions._native_meeting_identity(proof["source_events"], prior)
            or any(_time(proposal[key]) != _time(proof["payload"][key]) for key in ("start", "end"))
            or proposal["duration_seconds"] != adoptions._seconds(proof["payload"])
            or not proposal.get("clockify_project_suffix")
            or not proof["payload"]["projectId"].endswith(proposal["clockify_project_suffix"])
            or len(proposal.get("tag_suffixes", [])) != len(proof["payload"]["tagIds"])
            or len(set(proposal.get("tag_suffixes", []))) != len(proposal.get("tag_suffixes", []))
            or not all(suffix and sum(tag.endswith(suffix) for tag in proof["payload"]["tagIds"]) == 1
                       for suffix in proposal.get("tag_suffixes", []))
            or proposal["billable"] is not proof["payload"]["billable"]):
        raise ValueError("pending selection covered recording exact source, route or whole interval differs")
    row = _covered_posted_row(proof, declaration, proposal, captured)
    return {"review_id": current["review_id"], "prior_review_id": proof["prior_review_id"],
            "clockify_entry_id": proof["clockify_entry_id"], "basis": "verified_posted_source_representation_only",
            "canonical_meeting_id": canonical_id, "native_recording_identity": list(recording_identity),
            "covered_seconds": proposal["duration_seconds"], "prior_proof_artifacts": declaration["prior_proof_artifacts"],
            "prior_proof_digest": proof["proof_digest"], "captured_posted_row": row,
            "fresh_clockify_capture": declaration["fresh_clockify_capture"], "provider_entry_sha256": digest(entry),
            "new_pending_rows": 0, "accounting_credit_mutations": 0, "clockify_writes": 0}


def _credits(selected: list[dict[str, Any]], all_sources: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Re-execute native final helpers on saved commitments, not reallocate."""
    selected_atoms = set().union(*(item["atoms"] for item in selected))
    union = {}
    for source in all_sources.values():
        for event in source["ledger"]["events"]:
            if event.get("source_type") == "existing_clockify":
                raise ValueError("pending selection cannot reuse posted Clockify credit")
            # Only session atoms participate in canonical pools. Other captured
            # sources remain separate original events for native fixed blocks.
            try:
                key = _atom(event)
            except ValueError:
                key = ("other", digest(event))
            previous = union.get(key)
            canonical = copy.deepcopy(event)
            # Clockify comparison warnings must cite the immutable native
            # evidence ID, not a synthetic session-pool atom identifier.
            canonical["evidence_id"] = event["evidence_id"] if event.get("source_type") == "clockify" else "canonical-" + digest(key)[7:]
            if previous is not None:
                # Session envelope serialization and transport flags differ
                # across native captures. Neither is canonical event identity
                # nor human-pool capacity: exact human timestamp points are.
                native_drift = (previous.get("source_type") != canonical.get("source_type")
                        or pipeline._parse_dt(previous.get("raw_source_span", {}).get("timestamp") or previous.get("observed_at"))
                        != pipeline._parse_dt(canonical.get("raw_source_span", {}).get("timestamp") or canonical.get("observed_at")))
                if native_drift or previous["attributes"].get("tool_name") != canonical["attributes"].get("tool_name"):
                    if key in selected_atoms or native_drift:
                        raise ValueError("pending selection canonical aliases disagree on native human timestamp/source")
                    # Broad ledgers can contain unrelated tool-result
                    # placeholders with the same session/content/time atom.
                    # Keep conflicting uncited records in native timing/block
                    # context, without treating them as aliases or weakening
                    # any selected atom's native source/timestamp checks.
                    distinct_key = ("uncited-native-record", key, digest(event))
                    canonical["evidence_id"] = "canonical-" + digest(distinct_key)[7:]
                    union[distinct_key] = canonical
            else:
                union[key] = canonical
    segments, demands, members, meeting_checks = [], {}, {}, []
    for item in selected:
        proposal, source = item["proposal"], item["source"]
        if any(event.get("source_type") in {"clockify", "existing_clockify"}
               for event in adoptions._source_events(proposal, source["ledger"])):
            raise ValueError("pending selection cannot reuse posted Clockify credit")
        activity = proposal["activity_id"]
        matches = [d for d in source["accounting"]["allocation"]["evidence"] if d["activity_id"] == activity]
        fixed_meeting = not matches and bool(proposal.get("provenance", {}).get("canonical_meeting_id"))
        if fixed_meeting:
            meeting_checks.append(_meeting_credit(proposal, source))
        elif len(matches) != 1 or (activity in demands and matches[0] != demands[activity]):
            raise ValueError("pending selection original demand is absent or ambiguous")
        else:
            demands[activity] = matches[0]
            members.setdefault(activity, []).append(item)
        seconds = proposal["duration_seconds"]
        minutes = seconds / 60 if fixed_meeting else proposal["duration_minutes"]
        segment = allocator.AllocationSegment(proposal["candidate_key"], activity, proposal["workstream_id"],
                                            _time(proposal["start"]), _time(proposal["end"]), minutes,
                                            tuple(proposal["provenance"]["evidence_ids"]))
        if ((segment.end - segment.start).total_seconds() != segment.duration_minutes * 60
                or seconds != segment.duration_minutes * 60
                or (fixed_meeting and (type(seconds) is not int or seconds <= 0
                                       or proposal["duration_minutes"] != seconds // 60))):
            raise ValueError("pending selection saved credit exact duration differs")
        segments.append(segment)
    for activity, raw in demands.items():
        demand = allocator._as_activity(raw)
        own = [s for s in segments if s.activity_id == activity]
        if any(s.workstream_id != demand.workstream_id or not any(lo <= s.start < s.end <= hi for lo, hi in demand.allowed_intervals) for s in own):
            raise ValueError("pending selection credit exceeds original native envelope")
        if any(min(a.end, b.end) > max(a.start, b.start) for i, a in enumerate(own) for b in own[i + 1:]):
            raise ValueError("pending selection same-activity credits overlap")
        if sum(s.duration_minutes for s in own) > demand.effort.recommended_minutes:
            raise ValueError("pending selection credit exceeds original native effort")
    timing = pipeline._session_timing_contexts(union.values())
    borrowers = {}
    source_timing = {}
    estimated_activities = set()
    for activity, items in members.items():
        atoms = set().union(*(item["atoms"] for item in items))
        cited = [union[key] for key in atoms]
        contexts = _cited_timing_contexts(cited, timing)
        demand = allocator._as_activity(demands[activity])
        estimated = [item["proposal"]["provenance"].get("timing_placement") == "estimated" for item in items]
        # The saved native demand owns placement. Uncapped semantic effort or
        # a richer union alias must neither turn observed spans into borrowing
        # nor erase a genuine estimated placement's shared-pool debit.
        if not any(estimated):
            if contexts and not demand.evidence_spans:
                raise ValueError("pending selection saved native placement proof is absent")
            continue
        if not all(estimated) or demand.evidence_spans or not contexts:
            raise ValueError("pending selection saved native placement contradicts original demand")
        for item in items:
            source = item["source"]
            source_key = id(source)
            if source_key not in source_timing:
                source_timing[source_key] = pipeline._session_timing_contexts(source["ledger"]["events"])
            original_contexts = _cited_timing_contexts(adoptions._source_events(item["proposal"], source["ledger"]), source_timing[source_key])
            intervals = [original_contexts[key]["interval"] for key in sorted(original_contexts)]
            evidence_ids = sorted({value for context in original_contexts.values() for value in context["evidence_ids"]})
            provenance = item["proposal"]["provenance"]
            native_intervals = tuple(sorted((_time(pipeline._iso(_time(interval["start"]))),
                                            _time(pipeline._iso(_time(interval["end"])))) for interval in intervals))
            if (not original_contexts or provenance.get("timing_context_intervals") != intervals
                    or provenance.get("timing_context_evidence_ids") != evidence_ids
                    or demand.allowed_intervals != native_intervals):
                raise ValueError("pending selection saved native placement context differs")
        estimated_activities.add(activity)
        for key, context in contexts.items():
            borrowers.setdefault(key, {"interval": context["interval"], "activities": set()})["activities"].add(activity)
    pool_checks = []
    for key, pool in borrowers.items():
        if len(pool["activities"]) < 2:
            continue
        own = [s for s in segments if s.activity_id in pool["activities"]]
        lo, hi = (_time(pipeline._iso(_time(pool["interval"][bound]))) for bound in ("start", "end"))
        capacity = pipeline._interval_capacity_minutes([pool["interval"]])
        if (any(not lo <= s.start < s.end <= hi for s in own)
                or sum(s.duration_minutes for s in own) > capacity
                or any(min(a.end, b.end) > max(a.start, b.start) for i, a in enumerate(own) for b in own[i + 1:])):
            raise ValueError("pending selection shared human-pool debit exceeds native capacity")
        pool_checks.append({"pool_id": key, "interval": pool["interval"], "capacity_minutes": capacity,
                            "debited_minutes": sum(s.duration_minutes for s in own), "activities": sorted(pool["activities"])})
    checks = []
    for activity, raw in demands.items():
        demand = allocator._as_activity(raw)
        credited = sum(s.duration_minutes for s in segments if s.activity_id == activity)
        residual = demand.effort.recommended_minutes - credited
        # Native accounting intentionally leaves estimated shared-pool effort
        # contested: observed-only recovery excludes an activity's own slices,
        # not its siblings, and would spend their human capacity again. Only
        # the source-authenticated placement and pool debit above admit this
        # distinction; a provenance flag alone cannot bypass recovery checks.
        remaining = [] if activity in estimated_activities else pipeline._capacity_recovery_slices(demand, segments, residual)
        recoverable = sum(int((hi - lo).total_seconds()) // 60 for lo, hi in remaining)
        if recoverable:
            raise ValueError("pending selection leaves recoverable whole-minute capacity")
        checks.append({"activity_id": activity, "credited_minutes": credited, "native_requested_minutes": demand.effort.recommended_minutes,
                       "native_residual_minutes": residual, "recoverable_minutes": recoverable})
    skipped = []
    existing = pipeline._existing_blocks(union.values())
    # Captured Clockify blocks are comparison context, never accepted credit.
    # Keep every block in native normalization: exact posted matches must still
    # trim/drop a saved proposal and therefore fail the unchanged-credit gate.
    normalized = pipeline._normalize_postable_proposals([copy.deepcopy(item["proposal"]) for item in selected], existing, skipped)
    originals = {p["candidate_key"]: p for p in (item["proposal"] for item in selected)}
    if len(originals) != len(selected) or len(normalized) != len(selected) or skipped:
        raise ValueError("pending selection native normalization changed saved credits")
    for proposal in normalized:
        before = originals.get(proposal["candidate_key"])
        if before is None or {k: v for k, v in before.items() if k != "review_warnings"} != {k: v for k, v in proposal.items() if k != "review_warnings"}:
            raise ValueError("pending selection native normalization altered a saved allocation")
    return normalized, {"saved_credit_rows": len(selected), "saved_credit_minutes": sum(s.duration_minutes for s in segments),
                        "native_demand_count": len(demands), "native_credit_checks": checks, "shared_pool_debits": pool_checks,
                        "native_meeting_credit_checks": meeting_checks,
                        "remaining_recoverable_minutes": 0, "native_residual_minutes": sum(c["native_residual_minutes"] for c in checks)}


def _reviewed_routing_correction(binding: Mapping[str, Any], *, source: Mapping[str, Any],
                                 proposal: Mapping[str, Any], row: list[Any], expected: list[Any],
                                 preserve_description: bool, spreadsheet_id: str,
                                 sheet_title: str, cache: dict) -> dict[str, Any]:
    """Bind one witnessed historical representation, not a new native route.

    The coordinator supplies trusted pinned operator artifacts. Hashes bind their
    bytes, not the author's identity; this is not an arbitrary receipt discovery,
    signature framework or re-authorization of the unrelated historical batch.
    Original source/time credit and ordinary live-row/TOCTOU gates stay native.
    """
    from scripts import clockify_native_sheet_post as native, clockify_sheet_publish as publisher
    canonical = adoptions.review_corrections.canonical_digest
    if not isinstance(binding, Mapping) or set(binding) != {"operator_receipt", "correction_plan"}:
        raise ValueError("pending selection reviewed routing binding differs")
    receipt, plan = (json.loads(adoptions._capture(binding[name], cache))
                     for name in ("operator_receipt", "correction_plan"))
    if (receipt.get("schema_version") != "clockify-sheet-metadata-correction-receipt/v1"
            or plan.get("schema_version") != "source-bound-sheet-metadata-plan/v1"
            or (receipt.get("spreadsheet_id"), plan.get("spreadsheet_id"), plan.get("sheet_title"))
            != (spreadsheet_id, spreadsheet_id, sheet_title)
            or type(plan.get("sheet_id")) is not int or receipt.get("sheet_id") != plan["sheet_id"]):
        raise ValueError("pending selection reviewed routing destination differs")
    entries = [entry for entry in plan.get("rows", []) if entry.get("review_id") == row[0]]
    if len(entries) != 1:
        raise ValueError("pending selection reviewed routing target is absent or ambiguous")
    entry = entries[0]
    before, after = entry.get("expected_full_row"), entry.get("planned_full_row")
    if (entry.get("column_L_source_id") != source["run_id"]
            or entry.get("source_proposal_digest") != canonical(proposal)
            or entry.get("source_proposal") != proposal
            or entry.get("original_evidence_ids") != proposal["provenance"]["evidence_ids"]
            or not isinstance(before, list) or not isinstance(after, list)
            or len(before) != 15 or len(after) != 15
            or entry.get("expected_full_row_digest") != canonical(before)
            or before[0] != row[0] or before[11] != source["run_id"]
            or before[9] != "pending" or before[13] != "unposted"):
        raise ValueError("pending selection reviewed routing original source differs")
    native_columns = [*range(9), 10, 11]
    if preserve_description:
        native_columns.remove(8)
    if any((pipeline._parse_dt(before[i]) != pipeline._parse_dt(expected[i]) if i in {1, 2}
            else not publisher._same_cell(before[i], expected[i])) for i in native_columns):
        raise ValueError("pending selection reviewed routing preimage is not native")
    changes = entry.get("changes")
    if not isinstance(changes, list):
        raise ValueError("pending selection reviewed routing changes differ")
    rebuilt, changed = copy.deepcopy(before), set()
    for change in changes:
        column = change.get("index_zero_based")
        if (type(column) is not int or column not in {4, 5, 8} or column in changed
                or change.get("column") != {4: "E", 5: "F", 8: "I"}[column]
                or change.get("expected") != before[column]):
            raise ValueError("pending selection reviewed routing changes protected cells")
        rebuilt[column] = change.get("replacement")
        changed.add(column)
    if (not {4, 5} <= changed or (8 in changed and not preserve_description)
            or canonical(rebuilt) != canonical(after) or canonical(after) != canonical(row)
            or any(canonical(before[i]) != canonical(after[i]) for i in range(15) if i not in {4, 5, 8})):
        raise ValueError("pending selection reviewed routing postimage or current cells differ")
    routes = {(route.get("project_suffix"), tuple(route.get("tag_suffixes", [])))
              for route in native._route_values(source["routing"])
              if route.get("project_name") == after[4]
              and ", ".join(route.get("tag_names", [])) == after[5]
              and route.get("project_suffix") in publisher.project_allowlist(source["routing"])
              and len(route.get("tag_names", [])) == len(route.get("tag_suffixes", []))}
    proof = entry.get("route_proof")
    if (len(routes) != 1 or not isinstance(proof, Mapping)
            or (proof.get("project_suffix"), tuple(proof.get("tag_suffixes", []))) not in routes):
        raise ValueError("pending selection reviewed routing native project/task is absent or ambiguous")
    write_result, readback = receipt.get("write_result", {}), receipt.get("readback", {})
    if (write_result.get("isError") is not False or readback.get("isError") is not False
            or write_result.get("structuredContent", {}).get("spreadsheetId") != spreadsheet_id
            or not isinstance(write_result.get("structuredContent", {}).get("replies"), list)
            or not write_result["structuredContent"]["replies"]):
        raise ValueError("pending selection reviewed routing operator operation failed")
    captured = readback.get("structuredContent", {})
    sheets = captured.get("sheets", [])
    if (captured.get("spreadsheetId") != spreadsheet_id or len(sheets) != 1
            or (sheets[0].get("properties", {}).get("sheetId"), sheets[0].get("properties", {}).get("title"))
            != (plan["sheet_id"], sheet_title)):
        raise ValueError("pending selection reviewed routing readback destination differs")
    matches = []
    for grid in sheets[0].get("data", []):
        start = grid.get("startRow", 0)
        if type(start) is not int or start < 0 or grid.get("startColumn", 0) != 0:
            raise ValueError("pending selection reviewed routing readback grid differs")
        for offset, raw in enumerate(grid.get("rowData", [])):
            values = [native._cell_value(cell) for cell in raw.get("values", [])]
            if values and values[0] == row[0]:
                matches.append((start + offset + 1, values))
    if (len(matches) != 1 or matches[0][0] != entry.get("sheet_row_at_capture")
            or len(matches[0][1]) != 15 or canonical(matches[0][1]) != canonical(after)):
        raise ValueError("pending selection reviewed routing readback row differs")
    return {**binding, "authority_boundary": "trusted_coordinator_invocation_not_author_signature",
            "source_proposal_sha256": canonical(proposal), "preimage_sha256": canonical(before),
            "postimage_sha256": canonical(after), "current_row_sha256": canonical(row),
            "readback_row_sha256": canonical(matches[0][1]), "represented_columns": ["E", "F"],
            "native_project_suffix": proof["project_suffix"], "native_tag_suffixes": proof["tag_suffixes"]}


def verify(*, bindings_path: Path, source_dir: Path, proposals: Sequence[Mapping[str, Any]],
           spreadsheet_id: str, sheet_title: str, run_id: str, project_allowlist: Mapping[str, str]) -> dict[str, Any]:
    from scripts import clockify_sheet_publish as publisher
    cache = {}
    handle = artifact_handle(bindings_path)
    document = json.loads(adoptions._capture(handle, cache))
    required_fields = {"schema_version", "spreadsheet_id", "sheet_title", "current_source", "sources", "selected_current_ids", "prior_rows", "sheet_capture"}
    if (not required_fields <= document.keys() or document.keys() - required_fields - {"reason_projection", "covered_source_outcomes"}
            or document["schema_version"] != SCHEMA or (document["spreadsheet_id"], document["sheet_title"]) != (spreadsheet_id, sheet_title)):
        raise ValueError("pending selection schema or destination differs")
    sources = {name: _source(record, cache) for name, record in document["sources"].items()}
    current = sources[document["current_source"]]
    if (current["run_id"] != run_id or Path(source_dir).name != run_id
            or Path(current["artifacts"]["proposals"]["path"]) != Path(source_dir) / "proposals.json"
            or current["proposals"] != list(proposals)):
        raise ValueError("pending selection full current native source differs")
    publisher.verify_gates(proposals, current["quality"], current["replay"], run_id)
    if publisher._routing_allowlist(Path(current["artifacts"]["routing"]["path"]), current["replay"]) != dict(project_allowlist):
        raise ValueError("pending selection current routing differs from native replay")
    publisher.validate_recovery_proposal_groups(proposals)
    capture = json.loads(adoptions._capture(document["sheet_capture"], cache))
    if isinstance(capture, dict) and set(capture) == {"spreadsheet_id", "sheet_title", "rows"}:
        if (capture["spreadsheet_id"], capture["sheet_title"]) != (spreadsheet_id, sheet_title):
            raise ValueError("pending selection captured destination differs")
        captured = capture["rows"]
    else:
        from scripts import clockify_native_sheet_post as native
        if "structuredContent" not in capture:
            capture = {"structuredContent": capture}
        actual_id, actual_title, rows = native._sheet_rows(capture, pending_only=False)
        if (actual_id, actual_title) != (spreadsheet_id, sheet_title):
            raise ValueError("pending selection captured destination differs")
        captured = [[row[name] for name in publisher.HEADER] for _, row in rows]
    captured_by_id = {}
    for row in captured:
        if len(row) != len(publisher.HEADER) or str(row[0]) in captured_by_id:
            raise ValueError("pending selection captured row is ambiguous")
        captured_by_id[str(row[0])] = list(row)
    def item(source, review_id):
        proposal = source["by_review_id"][review_id]
        atoms = {_atom(event) for event in adoptions._source_events(proposal, source["ledger"])}
        return {"proposal": proposal, "source": source, "atoms": atoms, "review_id": review_id}
    current_items = {publisher.stable_review_id(p): item(current, publisher.stable_review_id(p)) for p in proposals}
    if len(current_items) != len(proposals):
        raise ValueError("pending selection full current review identity is ambiguous")
    ids = document["selected_current_ids"]
    if (not isinstance(ids, list) or (not ids and not document.get("covered_source_outcomes"))
            or len(set(ids)) != len(ids) or not set(ids) <= current_items.keys()):
        raise ValueError("pending selection current selection is invalid")
    reasons = None
    if "reason_projection" in document:
        reasons = json.loads(adoptions._capture(document["reason_projection"], cache))
        if (not isinstance(reasons, dict) or set(reasons) != set(ids)
                or any(not isinstance(value, str) or not value.strip() for value in reasons.values())):
            raise ValueError("pending selection readable reasons require exact selected IDs and nonempty strings")
    selected = [current_items[review_id] for review_id in ids]
    prior, seen = [], set(ids)
    for declaration in document["prior_rows"]:
        if (not {"review_id", "source", "disposition"} <= declaration.keys()
                or declaration.keys() - {"review_id", "source", "disposition", "preserve_captured_routing", "preserve_captured_description", "reviewed_routing_correction"}
                or declaration["disposition"] not in {"retain", "supersede", "inactive"}):
            raise ValueError("pending selection prior disposition differs")
        review_id = declaration["review_id"]
        if review_id in seen:
            raise ValueError("pending selection duplicate prior/current review identity")
        seen.add(review_id)
        source = sources[declaration["source"]]
        record = item(source, review_id)
        row = captured_by_id[review_id]
        expected = publisher.proposal_row(record["proposal"], source["run_id"], project_allowlist=publisher.project_allowlist(source["routing"]))
        columns = [*range(9), 10, 11]
        if "preserve_captured_description" in declaration:
            if declaration["preserve_captured_description"] is not True or not isinstance(row[8], str) or not row[8].strip():
                raise ValueError("pending selection captured description preservation differs")
            # This is a frozen historical review representation, not a fresh
            # semantic description inference. Source credits stay native.
            columns.remove(8)
        if declaration.get("preserve_captured_routing"):
            if (record["proposal"].get("routing_disposition") != "unresolved-routing"
                    or not row[4] or declaration["preserve_captured_routing"] is not True):
                raise ValueError("pending selection captured routing preservation differs")
            columns = [i for i in columns if i not in {4, 5}]
        if "reviewed_routing_correction" in declaration:
            if "preserve_captured_routing" in declaration:
                raise ValueError("pending selection routing representation mechanisms conflict")
            record["reviewed_routing_correction"] = _reviewed_routing_correction(
                declaration["reviewed_routing_correction"], source=source, proposal=record["proposal"],
                row=row, expected=expected, preserve_description=declaration.get("preserve_captured_description") is True,
                spreadsheet_id=spreadsheet_id, sheet_title=sheet_title, cache=cache)
            columns = [i for i in columns if i not in {4, 5}]
        if any((pipeline._parse_dt(row[i]) != pipeline._parse_dt(expected[i]) if i in {1, 2}
                else not publisher._same_cell(row[i], expected[i])) for i in columns):
            raise ValueError("pending selection captured native identity or duration differs")
        inactive = declaration["disposition"] == "inactive"
        if (inactive and (row[9] != "superseded" or row[13] not in {"superseded", "unposted"})) or (not inactive and (row[9] != "pending" or row[13] != "unposted")):
            raise ValueError("pending selection requires exact pending/unposted or inactive preimage")
        record.update(row=row, disposition=declaration["disposition"], verified_native_columns=[publisher.HEADER[i] for i in columns],
                      representation_basis="trusted_witnessed_routing_correction" if "reviewed_routing_correction" in record else
                      "immutable_captured_existing_review" if declaration.get("preserve_captured_description") or declaration.get("preserve_captured_routing") else "native_projection_and_capture")
        prior.append(record)
        if declaration["disposition"] == "retain":
            selected.append(record)
    covered, covered_ids, entry_ids = [], set(), set()
    declarations = document.get("covered_source_outcomes", [])
    if not isinstance(declarations, list):
        raise ValueError("pending selection covered recording declarations differ")
    for declaration in declarations:
        identity = declaration["current_review_id"]
        if identity in seen or identity in covered_ids or identity not in current_items:
            raise ValueError("pending selection covered recording identity repeats or conflicts")
        representation = _covered_source_outcome(declaration, current_items[identity], captured_by_id, cache)
        if representation["clockify_entry_id"] in entry_ids:
            raise ValueError("pending selection covered recording repeats native posted entry")
        covered_ids.add(identity)
        entry_ids.add(representation["clockify_entry_id"])
        covered.append(representation)
    accepted_atoms = set().union(*(record["atoms"] for record in selected), *(current_items[identity]["atoms"] for identity in covered_ids))
    candidates = [*current_items.values(), *(record for record in prior if record["disposition"] != "inactive")]
    if any(not record["atoms"] <= accepted_atoms for record in candidates):
        raise ValueError("pending selection would drop source outcomes")
    if any(a["proposal"]["activity_id"] != b["proposal"]["activity_id"] and a["atoms"] & b["atoms"]
           for i, a in enumerate(selected) for b in selected[i + 1:]):
        raise ValueError("pending selection distinct accepted activities duplicate canonical sources")
    normalized, acceptance = _credits(selected, sources)
    normalized_by_id = {publisher.stable_review_id(p): p for p in normalized}
    rows = []
    replacements = {}
    for review_id in ids:
        proposal = copy.deepcopy(normalized_by_id[review_id])
        # Native normalization warns only the later priority row. Retained
        # cells cannot change, so surface that same native overlap on the NEW
        # row as well when its retained counterpart would otherwise own it.
        for record in prior:
            other = record["proposal"]
            if record["disposition"] != "retain" or other["activity_id"] == proposal["activity_id"]:
                continue
            warning = pipeline._overlap_warning(_time(proposal["start"]), _time(proposal["end"]),
                                               {"block_id": other["candidate_key"], "start": _time(other["start"]),
                                                "end": _time(other["end"]), "project_id_suffix": other.get("clockify_project_suffix")},
                                               "review_proposal_overlap")
            if warning is not None and warning not in proposal.get("review_warnings", []):
                proposal["review_warnings"] = [*proposal.get("review_warnings", []), warning]
        predecessors = sorted(record["review_id"] for record in prior if record["disposition"] == "supersede"
                              and record["atoms"] & current_items[review_id]["atoms"])
        if predecessors:
            replacements[review_id] = predecessors
            proposal["review_warnings"] = [*proposal.get("review_warnings", []),
                                           {"type": "pending_review_replacement", "superseded_review_ids": predecessors,
                                            "selection_sha256": handle["sha256"]}]
        rows.append(publisher.proposal_row(proposal, run_id, project_allowlist=project_allowlist))
    rows.extend(record["row"] for record in prior if record["disposition"] == "retain")
    reason_binding = {}
    if reasons is not None:
        # Explicit immutable presentation only: source proposals and all native
        # warnings remain bound. Only M on selected NEW rows is projected.
        reason_binding = {"reason_projection": document["reason_projection"],
                          "native_projection_rows_sha256": digest(rows),
                          "native_review_warnings": {row[0]: json.loads(row[12]) if row[12] else [] for row in rows if row[0] in reasons}}
        for row in rows:
            if row[0] in reasons:
                row[12] = reasons[row[0]]
    receipt = {"schema_version": "pending-review-selection-acceptance/v1", "verification_basis": "saved_native_pending_credits",
               "selection": handle, "spreadsheet_id": spreadsheet_id, "sheet_title": sheet_title,
               "current_source_artifacts": current["artifacts"], "sources": document["sources"],
               "selected_current_ids": ids, "prior_dispositions": document["prior_rows"], "sheet_capture": document["sheet_capture"],
               "preserved_rows_sha256": digest([record["row"] for record in prior]), "replacements": replacements,
               "prior_representation_checks": [{"review_id": record["review_id"], "basis": record["representation_basis"],
                                                 "verified_native_columns": record["verified_native_columns"],
                                                 **({"reviewed_routing_correction": record["reviewed_routing_correction"]}
                                                    if "reviewed_routing_correction" in record else {}),
                                                 "all15_live_cells_must_match_capture": True} for record in prior],
               "supersession_contract": {"column": "J", "before": "pending", "after": "superseded",
                                           "review_status": "unposted", "other14_cells": "unchanged"},
               "projected_rows_sha256": digest(rows), "canonical_source_coverage": "pass", "native_normalization": "pass",
               **({"covered_source_outcomes": covered} if covered else {}),
               "runtime_artifacts": {"consumer": artifact_handle(Path(__file__).resolve()),
                                     "pipeline": artifact_handle(Path(pipeline.__file__).resolve()),
                                     "allocator": artifact_handle(Path(allocator.__file__).resolve())},
               **acceptance, **reason_binding, "clockify_writes": 0}
    receipt["acceptance_sha256"] = digest(receipt)
    return {"rows": rows, "prior": prior, "receipt": receipt, "new_ids": ids}


def plan(gateway: Any, *, spreadsheet_id: str, sheet_title: str, selection: Mapping[str, Any]) -> dict[str, Any]:
    from scripts import clockify_sheet_publish as publisher
    metadata = gateway.spreadsheet(spreadsheet_id)
    if sheet_title not in publisher._sheet_map(metadata):
        raise ValueError("pending selection target Sheet is missing")
    quoted = publisher._a1_title(sheet_title)
    count = publisher._sheet_row_count(metadata, sheet_title)
    positions, existing = publisher._scan_rows(gateway, spreadsheet_id, quoted, count)
    updates, expected, preimages = [], [], []
    for record in selection["prior"]:
        review_id, baseline = record["review_id"], record["row"]
        if review_id not in positions:
            raise ValueError("pending selection bound predecessor is missing")
        live = list(existing[positions[review_id]])
        live.extend([""] * (len(publisher.HEADER) - len(live)))
        after = list(baseline)
        if record["disposition"] == "supersede":
            after[9] = "superseded"
        same = lambda a, b: all(publisher._same_cell(x, y) for x, y in zip(a, b, strict=True))
        if same(live, after):
            expected.append(after)
        elif record["disposition"] == "supersede" and same(live, baseline):
            updates.append({"range": f"{quoted}!J{positions[review_id]}", "values": [["superseded"]]})
            expected.append(after)
        else:
            raise ValueError("pending selection live human decision or native cells drifted")
        preimages.append({"review_id": review_id, "row_number": positions[review_id], "row": live})
    for row in selection["rows"]:
        if row[0] not in selection["new_ids"] or row[0] not in positions:
            continue
        live = list(existing[positions[row[0]]])
        live.extend([""] * (len(publisher.HEADER) - len(live)))
        if len(live) != len(row) or any(not publisher._same_cell(a, b) for a, b in zip(live, row, strict=True)):
            raise ValueError("pending selection current published row changed after selection")
    return {"updates": updates, "expected": expected, "preimages": preimages, "quoted_title": quoted,
            "sheet_title": sheet_title, "sheet_id": publisher._sheet_map(metadata)[sheet_title], "row_count": count}


def apply(gateway: Any, *, spreadsheet_id: str, plan: Mapping[str, Any]) -> int:
    from scripts import clockify_sheet_publish as publisher
    # Primary/monthly publication may occur between plan and apply. Recheck
    # every bound predecessor and its physical position at the J-write boundary
    # before submitting ANY transition; a changed decision must stay untouched.
    metadata = gateway.spreadsheet(spreadsheet_id)
    if publisher._sheet_map(metadata).get(plan["sheet_title"]) != plan["sheet_id"]:
        raise publisher.PublicationError("pending selection target Sheet changed before supersession")
    row_count = publisher._sheet_row_count(metadata, plan["sheet_title"])
    positions, existing = publisher._scan_rows(gateway, spreadsheet_id, plan["quoted_title"], row_count)
    for preimage in plan["preimages"]:
        position = positions.get(preimage["review_id"])
        if position != preimage["row_number"]:
            raise publisher.PublicationError("pending selection predecessor moved before supersession")
        live = list(existing[position])
        live.extend([""] * (len(publisher.HEADER) - len(live)))
        if len(live) != len(preimage["row"]) or any(not publisher._same_cell(a, b) for a, b in zip(live, preimage["row"], strict=True)):
            raise publisher.PublicationError("pending selection predecessor cells changed before supersession")
    gateway.update_values(spreadsheet_id, plan["updates"])
    positions, existing = publisher._scan_rows(gateway, spreadsheet_id, plan["quoted_title"], row_count)
    for row in plan["expected"]:
        live = list(existing[positions[row[0]]])
        live.extend([""] * (len(publisher.HEADER) - len(live)))
        if len(live) != len(row) or any(not publisher._same_cell(a, b) for a, b in zip(live, row, strict=True)):
            raise publisher.PublicationError("pending selection predecessor readback differs")
    return len(plan["updates"])
