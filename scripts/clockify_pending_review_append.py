"""Read-only, missing-only append plans for genuine saved native credits.

This is not a replacement portfolio: unselected source outcomes are untouched.
Financial bindings are explicit trusted coordinator declarations, rechecked
against immutable source objects/semantic records and captured financial IDs.
They are not human approval, financial novelty, or permission to post time.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Mapping

from scripts import clockify_pending_review_selection as pending
from scripts import clockify_sheet_publish as publisher
from scripts import clockify_source_adoptions as adoptions
from scripts import evidence_ledger
from scripts import clockify_source_event_correspondence as correspondence
from scripts import clockify_financial_semantic_lineage as financial_lineage

SCHEMA = "pending-review-append/v1"
COMPARISON_SCHEMA = "pending-append-financial-comparison/v1"
WITNESS_SCHEMA = "pending-append-covered-accomplishment-witness/v1"


def _read(handle, cache):
    return json.loads(adoptions._capture(handle, cache))


def _core(activity):
    return {key: activity.get(key) for key in ("action", "object", "outcome", "lifecycle")}


def _capture_rows(document, spreadsheet_id, sheet_title):
    if (set(document) != {"spreadsheet_id", "sheet_title", "rows"}
            or (document["spreadsheet_id"], document["sheet_title"]) != (spreadsheet_id, sheet_title)
            or not isinstance(document["rows"], list)):
        raise ValueError("append-only captured destination differs")
    rows = copy.deepcopy(document["rows"])
    if (any(not isinstance(row, list) or len(row) != 15 or not isinstance(row[0], str) or not row[0] for row in rows)
            or len({row[0] for row in rows}) != len(rows)):
        raise ValueError("append-only captured rows are ambiguous")
    return rows


def _actual(capture):
    if (capture.get("schema") != "clockify-complete-month-read/v1"
            or capture.get("complete") is not True or capture.get("intervals_closed_complete") is not True
            or capture.get("terminal_empty_page") is not True
            or capture.get("open_timer_count") != 0 or capture.get("invalid_interval_count") != 0
            or not isinstance(capture.get("pages"), list) or not capture["pages"]
            or capture["pages"][-1].get("payload") != []):
        raise ValueError("append-only actual Clockify capture is incomplete")
    entries = [entry for page in capture["pages"] for entry in page["payload"]]
    ids = [entry["id"] for entry in entries]
    if len(ids) != len(set(ids)) or len(entries) != capture.get("entry_count"):
        raise ValueError("append-only actual Clockify identity/count differs")
    for entry in entries:
        interval = entry.get("timeInterval", {})
        if (not interval.get("end") or pending._time(interval["end"]) <= pending._time(interval["start"])
                or entry.get("workspaceId") != capture["workspace_id"] or entry.get("userId") != capture["user_id"]):
            raise ValueError("append-only actual Clockify target/interval differs")
    native = [{"id_suffix": entry["id"][-8:], "description": entry.get("description", ""),
               "project_id_suffix": (entry.get("projectId") or "")[-6:],
               "tag_id_suffixes": [tag[-8:] for tag in entry.get("tagIds", [])],
               "start": entry["timeInterval"]["start"], "end": entry["timeInterval"]["end"],
               "running": False, "running_snapshot": None, "duration": entry["timeInterval"]["duration"],
               "billable": entry.get("billable")} for entry in entries]
    events = [event.document() for event in evidence_ledger.normalize_collector_snapshot({"clockify": {"entries": native}})]
    return entries, {"ledger": {"events": events}}


def _comparisons(document, captured, actual, cache):
    if (set(document) != {"schema_version", "authority_boundary", "records"}
            or document["schema_version"] != COMPARISON_SCHEMA or not isinstance(document["records"], list)):
        raise ValueError("append-only financial comparison schema differs")
    validated_ledgers, ledger_timezones, semantic_documents, results = {}, {}, {}, []
    for record in document["records"]:
        if set(record) != {"surface", "id", "sheet_row", "ledger", "evidence_ids", "semantic_refs"}:
            raise ValueError("append-only financial source binding differs")
        if record["surface"] == "pending":
            row = captured.get(record["id"])
            if row is None or row[9] != "pending" or row[13] != "unposted":
                raise ValueError("append-only financial pending counterpart differs")
        elif record["surface"] == "posted":
            if record["id"] not in actual:
                raise ValueError("append-only financial posted counterpart differs")
        else:
            raise ValueError("append-only financial surface differs")
        key = tuple(sorted(record["ledger"].items()))
        if key not in validated_ledgers:
            document_ledger = _read(record["ledger"], cache)
            manifest = evidence_ledger.LedgerManifest.from_document(document_ledger["manifest"])
            events = tuple(evidence_ledger.EvidenceEvent.from_document(event) for event in document_ledger["events"])
            ledger = evidence_ledger.EvidenceLedger(events, manifest.source_inventory, manifest.timezone, manifest.member_identities)
            ledger.validate(manifest)
            validated_ledgers[key] = {event["evidence_id"]: event for event in document_ledger["events"]}
            ledger_timezones[key] = manifest.timezone
        by_id = validated_ledgers[key]
        ids = record["evidence_ids"]
        if not isinstance(ids, list) or not ids or len(set(ids)) != len(ids) or not set(ids) <= by_id.keys():
            raise ValueError("append-only financial evidence differs")
        atoms = {pending._atom(by_id[eid]) for eid in ids}
        cores = []
        # An authenticated source-only binding still supplies warning context.
        # Missing saved semantics cannot establish same work or financial novelty.
        for ref in record["semantic_refs"]:
            semantic_key = tuple(sorted(ref["artifact"].items()))
            if semantic_key not in semantic_documents:
                semantic_documents[semantic_key] = _read(ref["artifact"], cache)
            matches = [activity for activity in semantic_documents[semantic_key]["activities"] if activity["activity_id"] == ref["activity_id"]]
            if len(matches) != 1 or pending.digest(matches[0]) != ref["activity_sha256"]:
                raise ValueError("append-only financial semantic binding differs")
            cores.append(_core(matches[0]))
        results.append({**record, "atoms": atoms, "cores": cores,
                        "events": [by_id[eid] for eid in ids], "historical_timezone": ledger_timezones[key],
                        "financial_binding_sha256": pending.digest(record)})
    return results


def _selected_native_repeat(proposal, selected):
    return any(pending.digest(proposal) == pending.digest(item['proposal']) for item in selected)


def _financial_compatibility_error(proposal, own, *, require_contained_interval):
    financial = own['financial']
    if financial['project_suffix'] != proposal.get('clockify_project_suffix'):
        return 'project differs'
    if type(financial['billable']) is not bool or financial['billable'] != proposal.get('billable'):
        return 'billable differs'
    if sorted(financial['tag_suffixes']) != sorted(proposal.get('tag_suffixes', [])):
        return 'tags differ'
    if own['duration_seconds'] < proposal['duration_seconds']:
        return 'existing credit is insufficient'
    if require_contained_interval:
        lo, hi = pending._time(proposal['start']), pending._time(proposal['end'])
        if not pending._time(financial['start']) <= lo < hi <= pending._time(financial['end']):
            return 'existing interval is insufficient'
    return None


def _exact_own_native_repeat(proposal, own, atoms, record_atoms, core):
    """Only a verified identical native proposal is automatic financial coverage."""
    if own is None or not own['verified_current']:
        return False
    return (_financial_compatibility_error(proposal, own, require_contained_interval=True) is None
        and pending.digest(proposal) == pending.digest(own['proposal'])
        and atoms == record_atoms and core == own['core'])


def _witnesses(handle, declarations, cache):
    if handle is None:
        return {}
    document = _read(handle, cache)
    if (set(document) != {"schema_version", "records"} or document["schema_version"] != WITNESS_SCHEMA
            or not isinstance(document["records"], list)):
        raise ValueError("append-only same-accomplishment witness schema differs")
    result, seen = {}, set()
    for witness in document["records"]:
        if (set(witness) != {"selection", "selected_semantic_ref", "counterpart", "relation", "audit", "rationale"}
                or witness["selection"] not in declarations
                or witness["relation"] != "same-bounded-accomplishment"
                or not isinstance(witness["rationale"], str) or not witness["rationale"].strip()
                or set(witness["counterpart"]) != {"surface", "id", "financial_binding_sha256", "semantic_ref", "own_lineage_sha256"}):
            raise ValueError("append-only same-accomplishment witness binding differs")
        key = (witness["selection"]["source"], witness["selection"]["review_id"],
               witness["counterpart"]["surface"], witness["counterpart"]["id"])
        if key in seen:
            raise ValueError("append-only same-accomplishment witness repeats")
        seen.add(key)
        result.setdefault(key[1], []).append(witness)
    return result


def _covered_witness(witness, declaration, selected_ref, saved_core, proposal, row,
                     selected_events, selected_timezone, records, own_lineages, captured, actual, cache):
    """Explicit source-backed coordinator adjudication, never human approval."""
    counterpart = witness["counterpart"]
    matches = [record for record in records
               if (record["surface"], record["id"]) == (counterpart["surface"], counterpart["id"])]
    if (witness["selection"] != declaration or witness["selected_semantic_ref"] != selected_ref
            or len(matches) != 1):
        raise ValueError("append-only same-accomplishment witness selected evidence differs")
    record = matches[0]
    own = own_lineages.get((record["surface"], record["id"]))
    if own is None:
        raise ValueError("append-only same-accomplishment witness requires authenticated own financial lineage")
    if (record["financial_binding_sha256"] != counterpart["financial_binding_sha256"]
            or counterpart["own_lineage_sha256"] != own["lineage_sha256"]
            or counterpart["semantic_ref"] != own["semantic_ref"]):
        raise ValueError("append-only same-accomplishment witness financial evidence differs")
    historical_timezone = record["historical_timezone"] if selected_timezone == record["historical_timezone"] else None
    shared = correspondence.pairs(selected_events, record["events"], historical_timezone=historical_timezone)
    if len(shared) != len(selected_events):
        raise ValueError("append-only same-accomplishment witness lacks complete stable source coverage")
    audit = _read(witness["audit"], cache)
    targets = [target for target in audit.get("targets", []) if target.get("selection") == declaration]
    if (audit.get("schema_version") != "stable-source-precision-refinement-audit/v1" or len(targets) != 1
            or pending.digest(targets[0].get("proposal")) != declaration["proposal_sha256"]
            or targets[0].get("core") != saved_core):
        raise ValueError("append-only same-accomplishment witness audit target differs")
    audited = [match for match in targets[0].get("matches", [])
               if pending.digest(match.get("financial_binding")) == record["financial_binding_sha256"]]
    if (len(audited) != 1 or audited[0].get("all_selected_events_refined_to_counterpart") is not True
            or audited[0].get("selected_event_count") != len(selected_events)):
        raise ValueError("append-only same-accomplishment witness audit coverage differs")
    if not own["verified_current"]:
        raise ValueError("append-only same-accomplishment witness own financial lineage is unresolved: " + ", ".join(own["gaps"]))
    # Own current financial authority authenticates native billable/tags even
    # for pending rows. The explicit alias adjudication binds the accomplishment
    # and sufficient credit, not containment of representation-shifted clocks.
    mismatch = _financial_compatibility_error(proposal, own, require_contained_interval=False)
    if mismatch:
        raise ValueError("append-only same-accomplishment witness " + mismatch)
    raise ValueError(f"append-only covered bounded accomplishment already represented by {record['surface']} {record['id']}")


def verify(*, bindings_path: Path) -> dict[str, Any]:
    """Recheck full native authority; select only new saved review credits."""
    cache = {}
    binding_handle = pending.artifact_handle(bindings_path)
    document = _read(binding_handle, cache)
    fields = {"schema_version", "spreadsheet_id", "sheet_title", "sources", "replays", "selected",
              "sheet_capture", "clockify_capture", "financial_comparison"}
    if (set(document) - {"same_accomplishment_witnesses", "financial_lineages"} != fields or document["schema_version"] != SCHEMA):
        raise ValueError("append-only schema differs; updates/supersessions are forbidden")
    spreadsheet_id, sheet_title = document["spreadsheet_id"], document["sheet_title"]
    captured_rows = _capture_rows(_read(document["sheet_capture"], cache), spreadsheet_id, sheet_title)
    captured = {row[0]: row for row in captured_rows}
    if not document["sources"] or document["sources"].keys() != document["replays"].keys():
        raise ValueError("append-only full source/replay inventory differs")
    sources, semantics = {}, {}
    for name, record in document["sources"].items():
        if record.get("basis") != "completed-review-run" or document["replays"][name].get("basis") != "completed-review-replay":
            raise ValueError("append-only requires genuine full source and replay")
        source = pending._source(record, cache)
        replay = pending._source(document["replays"][name], cache)
        if (source["proposals"] != source["accounting"]["proposals"]
                or source["proposals"] != replay["proposals"] or source["accounting"] != replay["accounting"]
                or source["ledger"] != replay["ledger"] or source["routing"] != replay["routing"]
                or source["replay"] != replay["replay"]):
            raise ValueError("append-only full source differs from genuine replay")
        publisher.verify_gates(source["proposals"], source["quality"], replay["replay"], source["run_id"])
        routing_path = Path(source["artifacts"]["routing"]["path"])
        if publisher._routing_allowlist(routing_path, replay["replay"]) != publisher.project_allowlist(source["routing"]):
            raise ValueError("append-only routing differs from genuine replay")
        publisher.validate_recovery_proposal_groups(source["proposals"])
        # Full completion verification above already validates this native file.
        semantic_path = Path(source["artifacts"]["proposals"]["path"]).parent / "semantic-analysis.json"
        semantic_handle = pending.artifact_handle(semantic_path)
        semantic = _read(semantic_handle, cache)
        semantics[name] = {"artifact": semantic_handle, "activities": semantic["activities"]}
        sources[name] = source
    capture = _read(document["clockify_capture"], cache)
    entries, context = _actual(capture)
    actual = {entry["id"]: entry for entry in entries}
    comparisons = _comparisons(_read(document["financial_comparison"], cache), captured, actual, cache)
    own_lineages = financial_lineage.verify(document.get("financial_lineages"), records=comparisons,
        captured=captured, actual=actual, cache=cache)
    declarations = document["selected"]
    if not isinstance(declarations, list) or not declarations:
        raise ValueError("append-only selection is empty")
    witnesses = _witnesses(document.get("same_accomplishment_witnesses"), declarations, cache)
    selected, seen, partial, counterparts = [], set(), {}, {}
    for declaration in declarations:
        if set(declaration) != {"source", "review_id", "proposal_sha256"}:
            raise ValueError("append-only selected native credit binding differs")
        name, rid = declaration["source"], declaration["review_id"]
        if rid in seen or rid in captured:
            raise ValueError("append-only selected ID already exists or repeats")
        seen.add(rid)
        if name not in sources or rid not in sources[name]["by_review_id"]:
            raise ValueError("append-only selected native credit is absent")
        source = sources[name]
        proposal = source["by_review_id"][rid]
        if pending.digest(proposal) != declaration["proposal_sha256"]:
            raise ValueError("append-only selected native credit digest differs")
        row = publisher.proposal_row(proposal, source["run_id"], project_allowlist=publisher.project_allowlist(source["routing"]))
        if proposal.get("routing_disposition") == "unresolved-routing" or not row[4] or not row[8].strip():
            raise ValueError("append-only selected routing or description is unresolved")
        lo, hi = pending._time(proposal["start"]), pending._time(proposal["end"])
        if not pending._time(capture["since_utc"]) <= lo < hi <= pending._time(capture["until_utc"]):
            raise ValueError("append-only selected interval is outside actual comparison scope")
        selected_events = adoptions._source_events(proposal, source["ledger"])
        selected_timezone = source["ledger"]["manifest"].get("timezone")
        atoms = {pending._atom(event) for event in selected_events}
        matches = [activity for activity in semantics[name]["activities"] if activity["activity_id"] == proposal["activity_id"]]
        if len(matches) != 1:
            raise ValueError("append-only selected saved semantic core is ambiguous")
        saved_core = _core(matches[0])
        selected_ref = {"artifact": semantics[name]["artifact"], "activity_id": matches[0]["activity_id"],
                        "activity_sha256": pending.digest(matches[0])}
        for witness in witnesses.get(rid, []):
            _covered_witness(witness, declaration, selected_ref, saved_core, proposal, row,
                             selected_events, selected_timezone, comparisons, own_lineages, captured, actual, cache)
        if _selected_native_repeat(proposal, selected):
            raise ValueError("append-only selected same complete source and bounded accomplishment repeats")
        partial[rid] = []
        for record in comparisons:
            shared = atoms & record["atoms"]
            historical_timezone = record["historical_timezone"] if selected_timezone == record["historical_timezone"] else None
            stable_shared = correspondence.pairs(selected_events, record["events"], historical_timezone=historical_timezone)
            if not shared and not stable_shared:
                continue
            own = own_lineages.get((record["surface"], record["id"]))
            if _exact_own_native_repeat(proposal, own, atoms, record["atoms"], saved_core):
                raise ValueError("append-only same complete source and bounded accomplishment already represented")
            partial[rid].append({"surface": record["surface"], "id": record["id"], "sheet_row": record["sheet_row"],
                                 "shared_canonical_atoms": len(shared), "candidate_atoms": len(atoms),
                                 "shared_stable_source_events": len(stable_shared),
                                 "own_financial_semantics_authenticated": own is not None and own["verified_current"],
                                 "own_financial_lineage_gaps": own["gaps"] if own is not None else ["own-entry-native-lineage-not-supplied"],
                                 "counterpart_atoms": len(record["atoms"]), "warning_only": True})
        counterparts[rid] = []
        for entry in entries:
            interval = entry["timeInterval"]
            seconds = int((min(hi, pending._time(interval["end"])) - max(lo, pending._time(interval["start"]))).total_seconds())
            if seconds > 0:
                counterparts[rid].append({"id": entry["id"], "description": entry.get("description", ""),
                    "projectId": entry.get("projectId"), "start": interval["start"], "end": interval["end"],
                    "counterpart_duration_seconds": int((pending._time(interval["end"]) - pending._time(interval["start"])).total_seconds()),
                    "overlap_seconds": seconds, "same_work_not_inferred": True})
        selected.append({"proposal": proposal, "source": source, "atoms": atoms, "review_id": rid, "saved_core": saved_core})
    normalized, credits = pending._credits(selected, {**sources, "fresh_actual_clockify_comparison_context": context})
    by_key = {item["proposal"]["candidate_key"]: item for item in selected}
    rows, native_warnings = [], {}
    for proposal in normalized:
        item = by_key[proposal["candidate_key"]]
        row = publisher.proposal_row(proposal, item["source"]["run_id"], project_allowlist=publisher.project_allowlist(item["source"]["routing"]))
        rid = row[0]
        native_warnings[rid] = proposal.get("review_warnings", [])
        reason = "Proposed review item from accepted source evidence; not posted."
        if counterparts[rid]:
            reason += " Overlapping posted records (same work unproven): " + "; ".join(
                f"Clockify {pair['id']}: {' '.join(pair['description'].split())[:160]}"
                f" — {pair['counterpart_duration_seconds']} s posted, {pair['overlap_seconds']} s overlap"
                for pair in counterparts[rid]) + "."
        if partial[rid]:
            reason += f" Shared evidence with {len(partial[rid])} existing review/time entries is a warning, not proof of the same accomplishment."
            reason += " Source counterparts (accomplishment equivalence unresolved): " + "; ".join(
                f"{relation['surface']} {relation['id']} — {relation['shared_stable_source_events']} exact sealed source messages shared"
                + ("; financial ownership unresolved: " + ", ".join(relation['own_financial_lineage_gaps'])
                   if relation['own_financial_lineage_gaps'] else "")
                for relation in partial[rid]) + "."
        row[12] = reason  # Explicit presentation-only M; never alter I/native credit.
        rows.append(row)
    receipt = {"schema_version": "pending-review-append-acceptance/v1", "binding": binding_handle,
               "spreadsheet_id": spreadsheet_id, "sheet_title": sheet_title,
               "sources": document["sources"], "replays": document["replays"], "selected": declarations,
               "sheet_capture": document["sheet_capture"], "clockify_capture": document["clockify_capture"],
               "financial_comparison": document["financial_comparison"],
               "same_accomplishment_witnesses": document.get("same_accomplishment_witnesses"),
               "financial_lineages": document.get("financial_lineages"),
               "semantics": {name: value["artifact"] for name, value in semantics.items()},
               "preserved_existing_rows": len(captured_rows), "preserved_existing_all15_sha256": pending.digest(captured_rows),
               "projected_rows_sha256": pending.digest(rows), "native_review_warnings": native_warnings,
               "reason_projection_columns": ["M"], "actual_clockify_entries_compared": len(entries),
               "actual_clockify_observation_utc": capture["observation_as_of_utc"],
               "actual_clockify_counterparts": counterparts, "partial_source_relations_warning_only": partial,
               "known_financial_records_compared": len(comparisons), "full_source_coverage_claimed": False,
               "financial_semantic_binding_gaps": sum(not record["cores"] for record in comparisons),
               "financial_semantic_ownership_gaps": sum((record["surface"], record["id"]) not in own_lineages
                   or not own_lineages[(record["surface"], record["id"])]["verified_current"] for record in comparisons),
               "aggregate_semantic_cores_authority": "warning-only",
               "financial_novelty_claimed": False, "human_approval_claimed": False,
               "canonical_exact_duplicate_check": "pass", "native_normalization": "pass", **credits,
               "runtime": {"consumer": pending.artifact_handle(Path(__file__).resolve()),
                           "comparison_only_source_correspondence": pending.artifact_handle(Path(correspondence.__file__).resolve()),
                           "own_financial_semantic_lineage": pending.artifact_handle(Path(financial_lineage.__file__).resolve()),
                           "native_credit_consumer": pending.artifact_handle(Path(pending.__file__).resolve()),
                           "publisher": pending.artifact_handle(Path(publisher.__file__).resolve())},
               "updates": 0, "supersessions": 0, "clockify_writes": 0}
    receipt["acceptance_sha256"] = pending.digest(receipt)
    return {"rows": rows, "captured_rows": captured_rows, "updates": [], "receipt": receipt}


def plan(gateway: Any, *, verification: Mapping[str, Any]) -> dict[str, Any]:
    """Read live rows once; fail drift and produce only new-row appends."""
    receipt = verification["receipt"]
    if (receipt.get("acceptance_sha256") != pending.digest({key: value for key, value in receipt.items() if key != "acceptance_sha256"})
            or pending.digest(verification["rows"]) != receipt["projected_rows_sha256"]
            or pending.digest(verification["captured_rows"]) != receipt["preserved_existing_all15_sha256"]
            or verification["updates"] != []):
        raise ValueError("append-only verification drift")
    spreadsheet_id, title = receipt["spreadsheet_id"], receipt["sheet_title"]
    metadata = gateway.spreadsheet(spreadsheet_id)
    if title not in publisher._sheet_map(metadata):
        raise ValueError("append-only target sheet is missing")
    quoted = publisher._a1_title(title)
    count = publisher._sheet_row_count(metadata, title)
    _, existing = publisher._scan_rows(gateway, spreadsheet_id, quoted, count)
    live = [list(row) + [""] * (15 - len(row)) for number, row in sorted(existing.items()) if number != 1]
    if live != verification["captured_rows"]:
        raise ValueError("append-only live preservation snapshot drift")
    output = publisher._plan_publish(gateway, spreadsheet_id=spreadsheet_id, sheet_title=title,
                                     template_title=title, rows=verification["rows"])
    if output["created"] or output["updates"] or len(output["appends"]) != len(verification["rows"]):
        raise ValueError("append-only plan would update or supersede an existing row")
    start = output["existing_max_row"] + 1
    output["append_range"] = f"{quoted}!A{start}:O{start + len(output['appends']) - 1}"
    output["preserve_all_existing15cells"] = True
    output["acceptance"] = receipt
    return output
