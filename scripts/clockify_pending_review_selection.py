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
    if record["basis"] in {"completed-review-run", "completed-review-replay"}:
        receipt_path = Path(handles["receipt"]["path"])
        bundle = collector_receipts.load_completion_bundle(receipt_path, run_dir=receipt_path.parent)
        is_replay = record["basis"] == "completed-review-replay"
        if receipt_path.parent.name != record["run_id"] or bundle.replay is not is_replay:
            raise ValueError("pending selection completed source identity differs")
        if (Path(handles["proposals"]["path"]) != receipt_path.parent / "proposals.json"
                or Path(handles["accounting"]["path"]) != receipt_path.parent / "work-accounting-result.json"
                or Path(handles["ledger"]["path"]) != receipt_path.parent / "evidence/evidence-ledger.json"):
            raise ValueError("pending selection completion artifacts differ from native run")
        if is_replay:
            # Keep the genuine replay completion identity; never relabel it as
            # a provider-completed source or synthesize a replacement receipt.
            bound = {artifact.kind: artifact for artifact in bundle.artifacts}
            proof = json.loads(adoptions._capture({"path": str(bound["replay_integrity"].path),
                "sha256": bound["replay_integrity"].digest}, cache))
            quality = json.loads(adoptions._capture({"path": str(bound["quality_report"].path),
                "sha256": bound["quality_report"].digest}, cache))
            from scripts import review_acceptance, clockify_sheet_publish as publisher
            if (proposals != accounting["proposals"]
                    or proof.get("schema_version") != 1
                    or proof.get("replay_run_id") != record["run_id"]
                    or not isinstance(proof.get("source_run_id"), str) or not proof["source_run_id"]
                    or proof.get("integrity_digest") != review_acceptance.digest({k: v for k, v in proof.items() if k != "integrity_digest"})
                    or proof.get("work_accounting_result", {}).get("file_sha256") != handles["accounting"]["sha256"][7:]
                    or proof.get("ledger_identity", {}).get("file_sha256") != handles["ledger"]["sha256"][7:]
                    or proof.get("reconciliation_binding", {}).get("routing_sha256") != handles["routing"]["sha256"]
                    or any(name in handles and Path(handles[name]["path"]) != receipt_path.parent / filename
                           for name, filename in (("replay", "replay-integrity.json"), ("quality", "quality_report.json")))):
                raise ValueError("pending selection completed replay binding differs")
            publisher.verify_gates(proposals, quality, proof, proof["source_run_id"])
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


def _fixed_recording_credit(item: Mapping[str, Any]) -> dict[str, Any]:
    """Validate saved fixed source time, not inferred effort or financial credit.

    Native meeting proposals deliberately precede the session allocator. A
    canonical ID dispatches validation but is never sufficient authority: the
    sealed original ledger, reconciliation and full saved partition must agree.
    Historical projections and posted-credit residuals are not this contract.
    """
    from scripts import clockify_sheet_publish as publisher, meeting_reconciliation as meetings
    proposal, source = item["proposal"], item["source"]
    try:
        accounting, ledger = source["accounting"], source["ledger"]
        saved = accounting["proposals"]
        original = adoptions._source(saved, ledger, publisher.stable_review_id(proposal))
        if original != proposal:
            raise ValueError("selected proposal differs from saved native output")
        source_keys = [(e["source_type"], e["source_ref"].get("source_id"))
                       for e in ledger["events"] if e["source_type"] in {"fathom", "calendly"}]
        if len(source_keys) != len(set(source_keys)):
            raise ValueError("recording source identity is ambiguous")
        recordings, exceptions = pipeline._recording_events(ledger["events"], ledger["manifest"])
        canonical_id = proposal["provenance"]["canonical_meeting_id"]
        matches = [entry for entry in recordings if entry["meeting"].canonical_id == canonical_id]
        if len(matches) != 1:
            raise ValueError("canonical recording identity is absent or ambiguous")
        entry = matches[0]
        source_ids = entry["source_evidence_ids"]
        if any(set(error["source_evidence_ids"]) & set(source_ids) for error in exceptions):
            raise ValueError("canonical recording is quarantined")
        representative = next((e for e in entry["events"] if e["source_type"] == "fathom"), entry["events"][0])
        member_ids = meetings.manifest_member_identities(ledger["manifest"])
        if (not pipeline._meeting_is_eligible(representative, member_ids)[0]
                or pipeline._attributes(representative).get("semantic_evidence_status") == "title_only"):
            raise ValueError("canonical recording is not eligible")
        reconciliation = [row for row in accounting["fathom_reconciliation"] if row["canonical_id"] == canonical_id]
        if (len(reconciliation) != 1 or reconciliation[0].get("status") != "proposed"
                or reconciliation[0].get("source_evidence_ids") != source_ids):
            raise ValueError("saved native reconciliation differs")
        siblings = sorted((p for p in saved if p.get("provenance", {}).get("canonical_meeting_id") == canonical_id),
                          key=lambda p: _time(p["start"]))
        activities = [p["activity_id"] for p in siblings]
        native_activities = reconciliation[0].get("activity_ids", [reconciliation[0].get("activity_id")])
        if (not siblings or len(set(activities)) != len(activities)
                or not isinstance(native_activities, list) or len(activities) != len(native_activities)
                or set(activities) != set(native_activities)
                or any(d["activity_id"] in activities for d in accounting["allocation"]["evidence"])):
            raise ValueError("fixed recording activity membership differs")
        meeting = entry["meeting"]
        start, end = pipeline._canonical_meeting_span(meeting, representative)
        splits = []
        for index, sibling in enumerate(siblings):
            provenance = sibling["provenance"]
            ids = provenance["evidence_ids"]
            seconds = (_time(sibling["end"]) - _time(sibling["start"])).total_seconds()
            if (type(sibling.get("duration_seconds")) is not int or seconds <= 0
                    or seconds != sibling["duration_seconds"]
                    or type(sibling.get("duration_minutes")) is not int
                    or sibling["duration_minutes"] != sibling["duration_seconds"] // 60
                    or not ids or len(ids) != len(set(ids)) or not set(ids) <= set(source_ids)
                    or "credited_overlap_receipt" in provenance or "verified_posted_credit" in provenance):
                raise ValueError("exact saved recording duration or source differs")
            # Fathom binds its immutable RecordingID/share URL explicitly.
            # Calendly's strict native EvidenceEvent schema already validates
            # its recording/meeting IDs, source digest and exact source span.
            if representative["source_type"] == "fathom":
                adoptions._native_meeting_identity([representative], sibling)
            if len(siblings) == 1:
                if (set(ids) != set(source_ids) or _time(sibling["start"]) != start
                        or _time(sibling["end"]) != end
                        or provenance.get("timestamped_split_evidence_ids")):
                    raise ValueError("whole recording does not conserve canonical span")
            else:
                boundaries = provenance.get("timestamped_split_evidence_ids")
                lo = int((_time(sibling["start"]) - start).total_seconds())
                hi = int((_time(sibling["end"]) - start).total_seconds())
                if (not isinstance(boundaries, list) or len(boundaries) != 1
                        or boundaries[0] not in {f"{evidence_id}:{lo}-{hi}" for evidence_id in ids}):
                    raise ValueError("native timestamped split source differs")
                splits.append(meetings.MeetingSplit(canonical_id, index, sibling["start"], sibling["end"],
                    {"project_name": sibling["client_project"],
                     "task_name": ", ".join(sorted(sibling["tag_names"]))}, tuple(boundaries)))
        if splits:
            meetings.validate_meeting_splits(meeting, splits)
        return {"canonical_meeting_id": canonical_id, "activity_id": proposal["activity_id"],
                "source_evidence_ids": source_ids, "canonical_duration_seconds": int((end - start).total_seconds()),
                "saved_duration_seconds": proposal["duration_seconds"]}
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError("pending selection fixed recording proof differs: " + str(exc)) from exc


def _tool_pool_locator(event: Mapping[str, Any]) -> tuple[Any, ...] | None:
    """Distinguish captured tool results without changing outcome atoms."""
    attrs, ref = event["attributes"], event["source_ref"]
    source_type = event.get("source_type")
    if (attrs.get("role") != "tool" or attrs.get("kind") != "tool_result"
            or source_type not in {"hermes_db_sessions_event", "hermes_sessions_event", "claude_bursts_event", "codex_sessions_event"}
            or ref.get("source_type") != source_type.removesuffix("_event")
            or any(not isinstance(ref.get(name), str) or not ref[name].strip()
                   for name in ("machine", "session_id", "source_id"))
            or type(ref.get("ordinal")) is not int or ref["ordinal"] < 0):
        return None
    # Source type, tool name and native time are checked as alias agreement,
    # not discriminators that could hide drift at one captured location.
    return (ref["machine"], ref["session_id"], ref["source_id"], ref["ordinal"])


def _pool_event_key(event: Mapping[str, Any]) -> tuple[Any, ...]:
    try:
        atom = _atom(event)
    except ValueError:
        atom = ("other", digest(event))
    locator = _tool_pool_locator(event)
    return ("tool-pool-record", atom, locator) if locator is not None else atom


def _canonical_pool_events(all_sources: Mapping[str, Any]) -> dict[tuple[Any, ...], dict[str, Any]]:
    union = {}
    old_aliases, tool_sources, tool_ordinals = {}, {}, {}
    for source in all_sources.values():
        for event in source["ledger"]["events"]:
            if event.get("source_type") == "existing_clockify":
                raise ValueError("pending selection cannot reuse posted Clockify credit")
            # Only session atoms participate in canonical pools. Other captured
            # sources remain separate original events for native fixed blocks.
            key = _pool_event_key(event)
            locator = _tool_pool_locator(event)
            atom = key[1] if locator is not None else key
            earlier = old_aliases.get(atom)
            if (earlier is not None and _pool_event_key(earlier) != key
                    and (locator is None or _tool_pool_locator(earlier) is None)):
                raise ValueError("pending selection canonical aliases disagree on native human timestamp/source")
            old_aliases.setdefault(atom, event)
            if locator is not None:
                # Claude ordinals are local to historical bursts. Validate the
                # locator only within the unchanged exact observation atom;
                # different observed points must retain their native identity.
                source_key = (atom, *locator[:3])
                ordinal_key = (atom, locator[0], locator[1], locator[3])
                prior_source, prior_ordinal = tool_sources.get(source_key), tool_ordinals.get(ordinal_key)
                if ((prior_source is not None and _pool_event_key(prior_source) != key)
                        or (prior_ordinal is not None and _tool_pool_locator(prior_ordinal) != locator)):
                    raise ValueError("pending selection canonical aliases disagree on native human timestamp/source")
                tool_sources.setdefault(source_key, event)
                tool_ordinals.setdefault(ordinal_key, event)
            previous = union.get(key)
            canonical = copy.deepcopy(event)
            # Clockify comparison warnings must cite the immutable native
            # evidence ID, not a synthetic session-pool atom identifier.
            canonical["evidence_id"] = event["evidence_id"] if event.get("source_type") == "clockify" else "canonical-" + digest(key)[7:]
            if previous is not None:
                # Session envelope serialization and transport flags differ
                # across native captures. Neither is canonical event identity
                # nor human-pool capacity: exact human timestamp points are.
                if (previous.get("source_type") != canonical.get("source_type")
                        or previous["attributes"].get("tool_name") != canonical["attributes"].get("tool_name")
                        or pipeline._parse_dt(previous.get("raw_source_span", {}).get("timestamp") or previous.get("observed_at"))
                        != pipeline._parse_dt(canonical.get("raw_source_span", {}).get("timestamp") or canonical.get("observed_at"))):
                    raise ValueError("pending selection canonical aliases disagree on native human timestamp/source")
            else:
                union[key] = canonical
    return union


def _credits(selected: list[dict[str, Any]], all_sources: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Re-execute native final helpers on saved commitments, not reallocate."""
    union = _canonical_pool_events(all_sources)
    segments, demands, members, fixed_checks = [], {}, {}, []
    for item in selected:
        proposal, source = item["proposal"], item["source"]
        if any(event.get("source_type") in {"clockify", "existing_clockify"}
               for event in adoptions._source_events(proposal, source["ledger"])):
            raise ValueError("pending selection cannot reuse posted Clockify credit")
        activity = proposal["activity_id"]
        if proposal.get("provenance", {}).get("canonical_meeting_id"):
            fixed_checks.append(_fixed_recording_credit(item))
            continue
        matches = [d for d in source["accounting"]["allocation"]["evidence"] if d["activity_id"] == activity]
        if len(matches) != 1 or (activity in demands and matches[0] != demands[activity]):
            raise ValueError("pending selection original demand is absent or ambiguous")
        demands[activity] = matches[0]
        members.setdefault(activity, []).append(item)
        segment = allocator.AllocationSegment(proposal["candidate_key"], activity, proposal["workstream_id"],
                                            _time(proposal["start"]), _time(proposal["end"]), proposal["duration_minutes"],
                                            tuple(proposal["provenance"]["evidence_ids"]))
        if ((segment.end - segment.start).total_seconds() != segment.duration_minutes * 60
                or proposal["duration_seconds"] != segment.duration_minutes * 60):
            raise ValueError("pending selection saved credit exact duration differs")
        segments.append(segment)
    checks = []
    for activity, raw in demands.items():
        demand = allocator._as_activity(raw)
        own = [s for s in segments if s.activity_id == activity]
        if any(s.workstream_id != demand.workstream_id or not any(lo <= s.start < s.end <= hi for lo, hi in demand.allowed_intervals) for s in own):
            raise ValueError("pending selection credit exceeds original native envelope")
        if any(min(a.end, b.end) > max(a.start, b.start) for i, a in enumerate(own) for b in own[i + 1:]):
            raise ValueError("pending selection same-activity credits overlap")
        credited = sum(s.duration_minutes for s in own)
        residual = demand.effort.recommended_minutes - credited
        if residual < 0:
            raise ValueError("pending selection credit exceeds original native effort")
        checks.append({"activity_id": activity, "credited_minutes": credited, "native_requested_minutes": demand.effort.recommended_minutes,
                       "native_residual_minutes": residual, "recoverable_minutes": 0})
    timing = pipeline._session_timing_contexts(union.values())
    borrowers = {}
    source_timing = {}
    estimated_activities = set()
    for activity, items in members.items():
        pool_keys = {_pool_event_key(event) for item in items
                     for event in adoptions._source_events(item["proposal"], item["source"]["ledger"])}
        cited = [union[key] for key in pool_keys]
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
    for check in checks:
        # The native producer explicitly forbids recovery for shared-context
        # estimated placement: own-activity free time is not unused human-pool
        # credit. Preserve that saved residual only after original placement and
        # context validation above; the ordinary recovery and pool guards stay.
        if check["activity_id"] in estimated_activities:
            continue
        demand = allocator._as_activity(demands[check["activity_id"]])
        remaining = pipeline._capacity_recovery_slices(demand, segments, check["native_residual_minutes"])
        recoverable = sum(int((hi - lo).total_seconds()) // 60 for lo, hi in remaining)
        if recoverable:
            raise ValueError("pending selection leaves recoverable whole-minute capacity")
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
        # Re-running the native helper appends identical captured-context
        # warnings. Preserve the saved list and only add genuinely new facts.
        warnings = copy.deepcopy(before.get("review_warnings", []))
        for warning in proposal.get("review_warnings", []):
            if warning not in warnings:
                warnings.append(warning)
        proposal["review_warnings"] = warnings
    return normalized, {"saved_credit_rows": len(selected),
                        "saved_credit_minutes": sum(item["proposal"]["duration_minutes"] for item in selected),
                        "saved_credit_seconds": sum(item["proposal"]["duration_seconds"] for item in selected),
                        "fixed_recording_rows": len(fixed_checks), "fixed_recording_checks": fixed_checks,
                        "native_demand_count": len(demands), "native_credit_checks": checks, "shared_pool_debits": pool_checks,
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


def _historical_last_seen_run(binding: Mapping[str, Any], *, source: Mapping[str, Any],
                              proposal: Mapping[str, Any], row: list[Any], expected: list[Any],
                              cache: dict) -> dict[str, Any]:
    """Witness only a legacy display label for unchanged native session credit.

    The explicit pinned packet is a trusted coordinator input, not a completion
    or financial receipt. Its input manifest must bind the genuine completed
    source bytes. Recordings, residuals, routes and time cannot use this path.
    """
    from scripts import clockify_sheet_publish as publisher
    if (not isinstance(binding, Mapping) or set(binding) != {"packet", "input_manifest"}
            or source["basis"] not in {"completed-review-run", "completed-review-replay"}
            or proposal.get("provenance", {}).get("canonical_meeting_id")
            or any(e["source_type"] in {"fathom", "calendly", "clockify", "existing_clockify"}
                   for e in adoptions._source_events(proposal, source["ledger"]))):
        raise ValueError("pending selection historical label requires an unchanged native session")
    packet = json.loads(adoptions._capture(binding["packet"], cache))
    manifest = json.loads(adoptions._capture(binding["input_manifest"], cache))
    if (packet.get("proposal_header") != publisher.HEADER
            or any("sha256:" + str(manifest.get(filename)) != source["artifacts"][name]["sha256"]
                   for filename, name in (("proposals.json", "proposals"), ("routing.json", "routing")))):
        raise ValueError("pending selection historical packet source binding differs")
    rows, declarations = packet.get("proposal_rows"), packet.get("proposal_bindings")
    if (not isinstance(rows, list) or not isinstance(declarations, list)
            or packet.get("proposal_count") != len(rows) or len(rows) != len(declarations)
            or any(not isinstance(r, list) or len(r) != 15 for r in rows)
            or len({r[0] for r in rows}) != len(rows)
            or len({d["review_id"] for d in declarations}) != len(declarations)):
        raise ValueError("pending selection historical packet row identity is ambiguous")
    matches = [r for r in rows if r[0] == row[0]]
    links = [d for d in declarations if d["review_id"] == row[0]]
    if len(matches) != 1 or len(links) != 1:
        raise ValueError("pending selection historical session binding is absent")
    original_row, link = matches[0], links[0]
    same = lambda a, b, i: (pipeline._parse_dt(a) == pipeline._parse_dt(b) if i in {1, 2}
                            else publisher._same_cell(a, b))
    if (set(link) != {"review_id", "parent_review_id", "native_recording_id", "seconds"}
            or link["parent_review_id"] != publisher.stable_review_id(proposal)
            or link["native_recording_id"] is not None or link["seconds"] != proposal["duration_seconds"]
            or not isinstance(original_row[11], str) or not original_row[11].strip()
            or any(not same(original_row[i], expected[i], i) for i in [*range(8), 10])
            or any(not same(original_row[i], row[i], i) for i in [*range(8), 10, 11])):
        raise ValueError("pending selection historical session identity, credit or label differs")
    return {**binding, "represented_columns": ["L"],
            "authority_boundary": "trusted_coordinator_packet_not_completion_or_financial_proof",
            "authentic_source_run_id": source["run_id"], "captured_display_run_id": row[11],
            "source_proposal_sha256": digest(proposal), "captured_all15_sha256": digest(row),
            "historical_packet_row_sha256": digest(original_row)}


def verify(*, bindings_path: Path, source_dir: Path, proposals: Sequence[Mapping[str, Any]],
           spreadsheet_id: str, sheet_title: str, run_id: str, project_allowlist: Mapping[str, str]) -> dict[str, Any]:
    from scripts import clockify_sheet_publish as publisher
    cache = {}
    handle = artifact_handle(bindings_path)
    document = json.loads(adoptions._capture(handle, cache))
    required_fields = {"schema_version", "spreadsheet_id", "sheet_title", "current_source", "sources", "selected_current_ids", "prior_rows", "sheet_capture"}
    if (not required_fields <= document.keys() or document.keys() - required_fields - {"reason_projection"}
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
    if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids) or not set(ids) <= current_items.keys():
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
                or declaration.keys() - {"review_id", "source", "disposition", "preserve_captured_routing", "preserve_captured_description", "reviewed_routing_correction", "historical_last_seen_run"}
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
        if "historical_last_seen_run" in declaration:
            if declaration.get("preserve_captured_routing") or "reviewed_routing_correction" in declaration:
                raise ValueError("pending selection historical session label cannot project routing")
            record["historical_last_seen_run"] = _historical_last_seen_run(
                declaration["historical_last_seen_run"], source=source, proposal=record["proposal"],
                row=row, expected=expected, cache=cache)
            columns.remove(11)
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
    accepted_atoms = set().union(*(record["atoms"] for record in selected))
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
                                                 **({"historical_last_seen_run": record["historical_last_seen_run"]}
                                                    if "historical_last_seen_run" in record else {}),
                                                 "all15_live_cells_must_match_capture": True} for record in prior],
               "supersession_contract": {"column": "J", "before": "pending", "after": "superseded",
                                           "review_status": "unposted", "other14_cells": "unchanged"},
               "projected_rows_sha256": digest(rows), "canonical_source_coverage": "pass", "native_normalization": "pass",
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
