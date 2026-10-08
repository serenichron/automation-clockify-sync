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


def _credits(selected: list[dict[str, Any]], all_sources: Mapping[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Re-execute native final helpers on saved commitments, not reallocate."""
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
                if (previous.get("source_type") != canonical.get("source_type")
                        or previous["attributes"].get("tool_name") != canonical["attributes"].get("tool_name")
                        or pipeline._parse_dt(previous.get("raw_source_span", {}).get("timestamp") or previous.get("observed_at"))
                        != pipeline._parse_dt(canonical.get("raw_source_span", {}).get("timestamp") or canonical.get("observed_at"))):
                    raise ValueError("pending selection canonical aliases disagree on native human timestamp/source")
            else:
                union[key] = canonical
    segments, demands, members = [], {}, {}
    for item in selected:
        proposal, source = item["proposal"], item["source"]
        if any(event.get("source_type") in {"clockify", "existing_clockify"}
               for event in adoptions._source_events(proposal, source["ledger"])):
            raise ValueError("pending selection cannot reuse posted Clockify credit")
        activity = proposal["activity_id"]
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
        remaining = pipeline._capacity_recovery_slices(demand, segments, residual)
        recoverable = sum(int((hi - lo).total_seconds()) // 60 for lo, hi in remaining)
        if recoverable:
            raise ValueError("pending selection leaves recoverable whole-minute capacity")
        checks.append({"activity_id": activity, "credited_minutes": credited, "native_requested_minutes": demand.effort.recommended_minutes,
                       "native_residual_minutes": residual, "recoverable_minutes": recoverable})
    timing = pipeline._session_timing_contexts(union.values())
    borrowers = {}
    for activity, items in members.items():
        atoms = set().union(*(item["atoms"] for item in items))
        cited = [union[key] for key in atoms]
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
        observed = pipeline._activity_observed_intervals(cited)
        local_capacity = pipeline._interval_capacity_minutes(observed)
        pool_capacity = pipeline._interval_capacity_minutes(c["interval"] for c in contexts.values())
        requested = int(items[0]["proposal"].get("effort", {}).get("recommended_minutes", 0)) if items[0]["proposal"].get("effort") else 0
        borrowing = bool(contexts) and (not observed or (bool(cited) and all(e.get("source_type") == "codex_sessions_event" for e in cited)
                                                       and requested > local_capacity and pool_capacity > local_capacity))
        if borrowing:
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
                        "remaining_recoverable_minutes": 0, "native_residual_minutes": sum(c["native_residual_minutes"] for c in checks)}


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
                or declaration.keys() - {"review_id", "source", "disposition", "preserve_captured_routing", "preserve_captured_description"}
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
        if any((pipeline._parse_dt(row[i]) != pipeline._parse_dt(expected[i]) if i in {1, 2}
                else not publisher._same_cell(row[i], expected[i])) for i in columns):
            raise ValueError("pending selection captured native identity or duration differs")
        inactive = declaration["disposition"] == "inactive"
        if (inactive and (row[9] != "superseded" or row[13] not in {"superseded", "unposted"})) or (not inactive and (row[9] != "pending" or row[13] != "unposted")):
            raise ValueError("pending selection requires exact pending/unposted or inactive preimage")
        record.update(row=row, disposition=declaration["disposition"], verified_native_columns=[publisher.HEADER[i] for i in columns],
                      representation_basis="immutable_captured_existing_review" if declaration.get("preserve_captured_description") or declaration.get("preserve_captured_routing") else "native_projection_and_capture")
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
