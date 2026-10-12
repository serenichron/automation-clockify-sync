"""Opt-in prepared review publication; no inference, Clockify credit or time edits.

The manifest retains every original source privately, including unready sources.
Only mechanically verified prepared activities become public rows. Editorial
wording and work identities are explicit caller inputs, not inferred here.
"""
from __future__ import annotations

import json
import copy
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from scripts import clockify_review_batches as batches
from scripts import clockify_source_adoptions as adoptions
from scripts.clockify_meeting_publication_alias import artifact_handle
from scripts import clockify_sheet_publish as publisher

SCHEMA = "review-batch-publication/v1"
AUTO_SCHEMA = "review-batch-publication/v2"
CONSOLIDATION_POLICY = "approved-daily-project-presentation/v1"
WORDING_CONSOLIDATION_POLICY = "approved-daily-project-presentation/v2"
OVERLAP_CONSOLIDATION_POLICY = "approved-daily-project-presentation/v3"
CLOCKIFY_OVERLAP_CONSOLIDATION_POLICY = "approved-daily-project-presentation/v4"
FIELDS = {"schema_version", "run_id", "spreadsheet_id", "sheet_title", "source_proposals",
          "editorial_descriptions", "work_contexts", "protected_ids", "timezone", "package"}
STANDALONE_REASONS = {"meeting", "overlap_warning", "source_interval_overlap", "not_micro_entry",
                     "untagged_native_entry"}
AUTO_FIELDS = FIELDS | {"authority", "consolidation_policy", "standalone_review_ids",
                        "preparation_diagnostics", "preparation_outcome"}
WORDING_AUTO_FIELDS = AUTO_FIELDS | {"wording_only_ids"}
AUTHORITY_FILES = {"semantic_analysis": "semantic-analysis.json",
                   "corrections": "review-corrections.jsonl", "routing": "routing.json"}


def _derive_authority(proposals: Sequence[Mapping[str, Any]], authority: Mapping[str, Any],
                      source_dir: Path, timezone: str, *, allow_wording_consolidation: bool = False) -> dict[str, Any]:
    """Derive editorial maps from sealed reviewed sources, not raw descriptions.

    Daily-project context is an approved presentation policy identity. It does
    not claim that distinct accomplishments are semantically identical.
    """
    from scripts import clockify_portfolio_repair as repair, review_corrections, work_accounting_pipeline as pipeline
    if not isinstance(authority, Mapping) or set(authority) != set(AUTHORITY_FILES):
        raise ValueError("automatic preparation authority inventory differs")
    for name, filename in AUTHORITY_FILES.items():
        if not isinstance(authority[name], Mapping) or authority[name].get("path") != str(source_dir / filename):
            raise ValueError("automatic preparation authority source differs")
    captures = {name: adoptions._capture(handle, {}) for name, handle in authority.items()}
    analysis = json.loads(captures["semantic_analysis"])
    routing = json.loads(captures["routing"])
    if not isinstance(analysis, Mapping) or not isinstance(analysis.get("activities"), list):
        raise ValueError("automatic preparation semantic authority differs")
    routes = pipeline._routes_by_selection(routing)
    decisions = review_corrections.load_decisions(Path(authority["corrections"]["path"]))
    cases = review_corrections.derive_regression_cases(decisions)
    # Historical timing metadata is intentionally not an executable regression
    # decision, but its presence must not become wording-only grouping consent.
    non_wording_targets = {(record.get("activity_id"), record.get("evidence_fingerprint"))
        for record in review_corrections._read_log(Path(authority["corrections"]["path"]))
        if record.get("record_type")} if allow_wording_consolidation else set()
    # The integrity-checked loader reads the same snapshot bytes, not a mutable
    # current configuration or corrections log outside this completed run.
    adoptions._capture(authority["corrections"], {})
    activities: dict[Any, list[Mapping[str, Any]]] = {}
    for activity in analysis["activities"]:
        if isinstance(activity, Mapping) and (target := review_corrections.proposal_target(activity)):
            activities.setdefault(target, []).append(activity)
    descriptions, contexts, protected, standalone, diagnostics = {}, {}, set(), set(), []
    wording_only = set()
    protected_existing = set()
    for proposal in proposals:
        identity = publisher.stable_review_id(proposal)
        target = review_corrections.proposal_target(proposal)
        matches = [case for case in cases if (case["activity_id"], case["evidence_fingerprint"]) == target]
        terminal = {"approved", "posted", "rejected", "superseded"}
        if allow_wording_consolidation:
            terminal |= {"approve", "skip", "skipped"}
        if (any(case["decision"] in {"approve", "skip"} for case in matches)
                or any(str(proposal.get(field) or "").strip().casefold() in terminal
                       for field in ("disposition", "review_status", "status"))):
            protected.add(identity)
            protected_existing.add(identity)
            continue
        corrected = pipeline._description_from_review_correction(proposal, cases)
        # Every effective exact-target correction must be description-only.
        # Wording supersession deliberately leaves older routing cases active.
        pure_wording = bool(matches) and all(
            case["decision"] == "modify" and case["correction_categories"] == ["wording"]
            and set(case["expected_field_patch"]) == {"description"}
            and case["expected_field_patch"]["description"] == {"op": "replace", "value": corrected}
            for case in matches)
        pending = all(str(proposal.get(field) or "").strip().casefold() in {"", "pending", "unposted"}
                      for field in ("disposition", "review_status", "status"))
        group_corrected = (allow_wording_consolidation and pure_wording and pending
                           and target not in non_wording_targets
                           and corrected is not None and proposal.get("description") == corrected)
        if matches or proposal.get("human_corrected"):
            if not group_corrected:
                protected.add(identity)
        reasons = []
        if corrected is not None:
            description = corrected
            if proposal.get("description") != corrected:
                reasons.append("wording_correction_not_materialized_in_source")
            elif not group_corrected:
                standalone.add(identity)
        else:
            reviewed = activities.get(target, [])
            if len(reviewed) != 1:
                description = None
                reasons.append("missing_unique_reviewed_activity_authority")
            else:
                activity = reviewed[0]
                model = activity.get("semantic_reviewer_model")
                revision = activity.get("semantic_reviewer_revision")
                provenance = proposal.get("provenance") or {}
                if not repair.is_approved_flash_route(model, revision) or (
                        provenance.get("semantic_reviewer_model"), provenance.get("semantic_reviewer_revision")) != (model, revision):
                    reasons.append("unapproved_or_mismatched_editorial_reviewer")
                description = activity.get("rendered_description")
        unsafe = repair.repair_reasons(description)
        if unsafe:
            reasons.append("unsafe_or_missing_reviewed_wording")
        if isinstance(description, str) and " — " in description:
            prefix = description.split(" — ", 1)[0]
            route = routes.get((str(proposal.get("client_project") or "").casefold(), prefix,
                                tuple(sorted(proposal.get("tag_names") or []))))
            if (route is None or str(route.get("project_suffix") or "") != str(proposal.get("clockify_project_suffix") or "")
                    or sorted(route.get("tag_suffixes") or []) != sorted(proposal.get("tag_suffixes") or [])
                    or route.get("billable", True) != proposal.get("billable")):
                reasons.append("editorial_route_not_bound_to_snapshot")
        else:
            reasons.append("editorial_route_not_bound_to_snapshot")
        if identity in protected and corrected is None:
            reasons.append("protected_correction_without_exact_wording_authority")
        if reasons:
            standalone.discard(identity)
            diagnostics.append({"review_id": identity, "reasons": sorted(set(reasons))})
            continue
        descriptions[identity] = description
        contexts[identity] = CONSOLIDATION_POLICY
        if group_corrected:
            wording_only.add(identity)
    return {"editorial_descriptions": descriptions, "work_contexts": contexts,
            "protected_ids": sorted(protected), "standalone_review_ids": sorted(standalone),
            "preparation_diagnostics": sorted(diagnostics, key=lambda item: item["review_id"]),
            "protected_existing_ids": protected_existing,
            **({"wording_only_ids": sorted(wording_only)} if allow_wording_consolidation else {})}


def _publication_members(document: Mapping[str, Any]) -> set[str]:
    identities = {child for activity in document["package"]["activities"] for child in activity["child_review_ids"]}
    for retained in document["package"]["retained"]:
        allowed = STANDALONE_REASONS
        if document["schema_version"] == AUTO_SCHEMA and retained["review_id"] in document["standalone_review_ids"]:
            allowed = allowed | {"protected_review_id", "human_corrected"}
        if (set(retained["preparation_reasons"]) <= allowed
                and retained["review_id"] in document["editorial_descriptions"]
                and retained["review_id"] in document["work_contexts"]):
            identities.add(retained["review_id"])
    return identities


def _auto_fields(proposals: Sequence[Mapping[str, Any]], authority: Mapping[str, Any],
                 source_dir: Path, timezone: str, *, consolidation_policy: str = CONSOLIDATION_POLICY) -> dict[str, Any]:
    fields = _derive_authority(proposals, authority, source_dir, timezone,
        allow_wording_consolidation=consolidation_policy in {WORDING_CONSOLIDATION_POLICY, OVERLAP_CONSOLIDATION_POLICY,
                                                            CLOCKIFY_OVERLAP_CONSOLIDATION_POLICY})
    protected_existing = fields.pop("protected_existing_ids")
    package = batches.package_review_batches(proposals, editorial_descriptions=fields["editorial_descriptions"],
        work_contexts=fields["work_contexts"], protected_ids=fields["protected_ids"], timezone=timezone,
        wording_only_ids=fields.get("wording_only_ids", ()),
        allow_review_overlaps=consolidation_policy in {OVERLAP_CONSOLIDATION_POLICY, CLOCKIFY_OVERLAP_CONSOLIDATION_POLICY},
        allow_clockify_overlaps=consolidation_policy == CLOCKIFY_OVERLAP_CONSOLIDATION_POLICY)
    fields["package"] = package
    members = _publication_members({**fields, "schema_version": AUTO_SCHEMA})
    withheld = [item for item in proposals if publisher.stable_review_id(item) not in members | protected_existing]
    source_seconds = sum(item["duration_seconds"] for item in proposals)
    withheld_seconds = sum(item["duration_seconds"] for item in withheld)
    diagnostic_ids = {item["review_id"] for item in fields["preparation_diagnostics"]}
    for retained in package["retained"]:
        identity = retained["review_id"]
        if identity is None:
            try:
                # A diagnostic may cite the source's computable original ID,
                # even when it fails the helper's stricter native-ID contract.
                # This does not make that ID eligible for a payable/public row.
                identity = publisher.stable_review_id(retained["proposal"])
            except (publisher.PublicationError, TypeError, ValueError):
                identity = None
        if identity not in members | protected_existing | diagnostic_ids:
            fields["preparation_diagnostics"].append({"review_id": identity,
                "reasons": retained["preparation_reasons"]})
            diagnostic_ids.add(identity)
    fields["preparation_diagnostics"].sort(key=lambda item: item["review_id"] or "")
    fields["preparation_outcome"] = {"status": "incomplete" if withheld else "complete",
        "source_seconds": source_seconds, "prepared_seconds": sum(item["duration_seconds"] for item in proposals
            if publisher.stable_review_id(item) in members),
        "preserved_seconds": source_seconds - withheld_seconds - sum(item["duration_seconds"] for item in proposals
            if publisher.stable_review_id(item) in members), "unprepared_seconds": withheld_seconds,
        "unprepared_review_ids": sorted(publisher.stable_review_id(item) for item in withheld)}
    return fields


def prepare(*, source_dir: Path, output: Path, run_id: str, spreadsheet_id: str,
            sheet_title: str, timezone: str) -> dict[str, Any]:
    """Create one immutable v2 binding; existing output is verified, never edited."""
    from scripts import clockify_review_run
    source_dir = source_dir.resolve()
    proposals_handle = artifact_handle(source_dir / "proposals.json")
    proposals = json.loads(adoptions._capture(proposals_handle, {}))
    authority = {name: artifact_handle(source_dir / filename) for name, filename in AUTHORITY_FILES.items()}
    existing = load(output)[0] if output.exists() else None
    policy = existing["consolidation_policy"] if existing is not None else CLOCKIFY_OVERLAP_CONSOLIDATION_POLICY
    fields = _auto_fields(proposals, authority, source_dir, timezone, consolidation_policy=policy)
    document = {"schema_version": AUTO_SCHEMA, "run_id": run_id, "spreadsheet_id": spreadsheet_id,
        "sheet_title": sheet_title, "source_proposals": proposals_handle, "timezone": timezone,
        "authority": authority, "consolidation_policy": policy, **fields}
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output.exists():
        if existing != document:
            raise ValueError("immutable automatic preparation binding differs")
    else:
        clockify_review_run._write_snapshot(output,
            (json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(),
            label="automatic review preparation")
    return {**fields["preparation_outcome"], "binding": artifact_handle(output),
            "diagnostics": fields["preparation_diagnostics"]}


def load(path: Path) -> tuple[dict[str, Any], dict[str, str]]:
    handle = artifact_handle(path)
    document = json.loads(adoptions._capture(handle, {}))
    if (not isinstance(document, dict) or document.get("schema_version") not in {SCHEMA, AUTO_SCHEMA}
            or set(document) != (FIELDS if document["schema_version"] == SCHEMA else
                WORDING_AUTO_FIELDS if document.get("consolidation_policy") in {
                    WORDING_CONSOLIDATION_POLICY, OVERLAP_CONSOLIDATION_POLICY,
                    CLOCKIFY_OVERLAP_CONSOLIDATION_POLICY} else AUTO_FIELDS)):
        raise ValueError("prepared review publication fields differ")
    for field in ("run_id", "spreadsheet_id", "sheet_title", "timezone"):
        if not isinstance(document[field], str) or not document[field].strip():
            raise ValueError("prepared review publication identity differs")
    for field in ("editorial_descriptions", "work_contexts"):
        if not isinstance(document[field], dict) or any(
                not isinstance(key, str) or not isinstance(value, str) or not value.strip()
                or "\n" in value or "\r" in value for key, value in document[field].items()):
            raise ValueError("prepared review publication editorial input differs")
    protected = document["protected_ids"]
    if (not isinstance(protected, list) or any(not isinstance(item, str)
            or not re.fullmatch(r"(?:wka-[a-f0-9]{24}-s[0-9]{2,}|pvi-[a-f0-9]{24})", item) for item in protected)
            or len(protected) != len(set(protected))):
        raise ValueError("prepared review protected identities differ")
    if document["schema_version"] == AUTO_SCHEMA:
        if document["consolidation_policy"] not in {CONSOLIDATION_POLICY, WORDING_CONSOLIDATION_POLICY,
                                                   OVERLAP_CONSOLIDATION_POLICY, CLOCKIFY_OVERLAP_CONSOLIDATION_POLICY}:
            raise ValueError("automatic preparation consolidation policy differs")
        source_handle = document["source_proposals"]
        proposals = json.loads(adoptions._capture(source_handle, {}))
        source_dir = Path(source_handle["path"]).parent
        derived = _auto_fields(proposals, document["authority"], source_dir, document["timezone"],
                              consolidation_policy=document["consolidation_policy"])
        if any(document[key] != value for key, value in derived.items()):
            raise ValueError("automatic preparation editorial authority or derived maps differ")
    return document, handle


def project(*, path: Path, source_dir: Path, proposals: Sequence[Mapping[str, Any]],
            run_id: str, spreadsheet_id: str, sheet_title: str,
            project_allowlist: Mapping[str, str]) -> dict[str, Any]:
    document, handle = load(path)
    if ((document["run_id"], document["spreadsheet_id"], document["sheet_title"])
            != (run_id, spreadsheet_id, sheet_title)
            or source_dir.name != run_id
            or document["source_proposals"].get("path") != str(source_dir.resolve() / "proposals.json")):
        raise ValueError("prepared review source or destination differs")
    frozen = json.loads(adoptions._capture(document["source_proposals"], {}))
    if frozen != list(proposals):
        raise ValueError("prepared review original proposal content differs")
    expected = batches.package_review_batches(frozen,
        editorial_descriptions=document["editorial_descriptions"], work_contexts=document["work_contexts"],
        protected_ids=document["protected_ids"], timezone=document["timezone"],
        wording_only_ids=document.get("wording_only_ids", ()),
        allow_review_overlaps=document.get("consolidation_policy") in {OVERLAP_CONSOLIDATION_POLICY, CLOCKIFY_OVERLAP_CONSOLIDATION_POLICY},
        allow_clockify_overlaps=document.get("consolidation_policy") == CLOCKIFY_OVERLAP_CONSOLIDATION_POLICY)
    if expected != document["package"]:
        raise ValueError("prepared review package differs from explicit editorial inputs")
    package = batches.verify_package(document["package"], proposals=frozen,
        work_contexts=document["work_contexts"], protected_ids=document["protected_ids"],
        timezone=document["timezone"], wording_only_ids=document.get("wording_only_ids", ()),
        allow_review_overlaps=document.get("consolidation_policy") in {OVERLAP_CONSOLIDATION_POLICY, CLOCKIFY_OVERLAP_CONSOLIDATION_POLICY},
        allow_clockify_overlaps=document.get("consolidation_policy") == CLOCKIFY_OVERLAP_CONSOLIDATION_POLICY)
    # Exercise native warning validation even though raw wording is not exposed.
    children = {publisher.stable_review_id(item): publisher.proposal_row(item, run_id,
                project_allowlist=project_allowlist) for item in frozen
                if any(publisher.stable_review_id(item) in activity["child_review_ids"]
                       for activity in package["activities"])}
    rows = [publisher.portfolio_row(activity, run_id) for activity in package["activities"]]
    # Group-ineligible is not necessarily publication-ineligible: full meeting,
    # long session and honestly warned overlap rows retain their exact native
    # identities, durations and warnings, with explicit cleaned wording only.
    publishable = _publication_members(document)
    for retained in package["retained"]:
        if retained["review_id"] not in publishable:
            continue
        identity = retained["review_id"]
        description = document["editorial_descriptions"].get(identity)
        context = document["work_contexts"].get(identity)
        if not description or not context:
            continue
        standalone = copy.deepcopy(retained["proposal"])
        standalone["description"] = description
        rows.append(publisher.proposal_row(standalone, run_id, project_allowlist=project_allowlist))
    rows.sort(key=lambda row: (row[1], row[0]))
    parents = {child: activity["review_id"] for activity in package["activities"]
               for child in activity["child_review_ids"]}
    return {"rows": rows, "children": children, "parents": parents,
            "editorial_descriptions": document["editorial_descriptions"],
            "receipt": {"binding": handle, "source_proposals": document["source_proposals"],
                **({"preparation_outcome": document["preparation_outcome"]} if document["schema_version"] == AUTO_SCHEMA else {})}}


def _retired(row: Sequence[Any], parent: str) -> list[Any]:
    result = list(row)
    result[9] = "superseded"
    result[12] = (str(row[12]) + "\n" if row[12] else "") + "Prepared review parent: " + parent + "; original time preserved, not financial credit."
    result[13] = "unposted"
    return result


def _cell_preimages(capture: Mapping[str, Any], spreadsheet_id: str, sheet_title: str) -> tuple[int, dict[str, Any]]:
    if capture.get("spreadsheetId") != spreadsheet_id:
        raise ValueError("staging spreadsheet differs")
    sheets = [sheet for sheet in capture.get("sheets", []) if sheet["properties"]["title"] == sheet_title]
    if len(sheets) != 1:
        raise ValueError("staging target sheet differs")
    sheet, result, offset = sheets[0], {}, 0
    for block in sheet.get("data", []):
        if block.get("startRow", 0) != offset or block.get("startColumn", 0) != 0:
            raise ValueError("staging requires contiguous full CellData")
        for record in block.get("rowData", []):
            cells = record.get("values", [])
            if len(cells) > len(publisher.HEADER):
                raise ValueError("staging row width differs")
            row = [cell.get("userEnteredValue", {}).get("stringValue",
                cell.get("userEnteredValue", {}).get("numberValue", "")) for cell in cells]
            row.extend([""] * (len(publisher.HEADER) - len(row)))
            offset += 1
            if offset == 1:
                if row != publisher.HEADER:
                    raise ValueError("staging header differs")
            elif row[0]:
                if row[0] in result:
                    raise ValueError("staging native identity is duplicated")
                result[row[0]] = {"row": offset, "values": row,
                    "has_native_note": any(cell.get("note") for cell in cells)}
    return sheet["properties"]["sheetId"], result


def fresh_warning_view(proposals: Sequence[Mapping[str, Any]], clockify: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Append GET-backed overlap warnings only; never allocate or credit time."""
    from scripts import evidence_ledger, clockify_native_sheet_post as native
    if clockify.get("read_only") is not True or not isinstance(clockify.get("entries"), list):
        raise ValueError("staging requires a read-only Clockify capture")
    events = evidence_ledger.normalize_collector_snapshot({"clockify": clockify})
    counterparts = {str(event.source_ref["source_id"]): event.evidence_id for event in events}
    if (len(events) != len(clockify["entries"]) or len(counterparts) != len(events)
            or len({str(entry.get("id") or "") for entry in clockify["entries"]}) != len(events)):
        raise ValueError("staging Clockify evidence identities differ")
    result = copy.deepcopy(list(proposals))
    for proposal in result:
        warnings = proposal.setdefault("review_warnings", [])
        for entry in clockify["entries"]:
            overlap = native._overlap(proposal, entry)
            if overlap is not None:
                warning = {"type": "existing_clockify_overlap", "counterpart_id": counterparts[str(entry["id"])],
                    "overlap_start": overlap["start"], "overlap_end": overlap["end"],
                    "overlap_duration_seconds": overlap["overlap_seconds"]}
                if warning not in warnings:
                    warnings.append(warning)
    return result


def stage_projection(path: Path) -> dict[str, Any]:
    """Bind presentation-only preimages to native sources and qualified wording.

    This optional operator stage is not a completed native run or financial
    receipt. I/L/M may be historical display cells; all financial identity,
    timing, routing and revision cells remain source-exact. L needs an actual
    matching native source witness, never an invented or renamed run label.
    """
    stage_handle = artifact_handle(path)
    stage = json.loads(adoptions._capture(stage_handle, {}))
    if set(stage) != {"schema_version", "source_index", "projection_binding", "qualified_materialization",
                      "sheet_capture", "clockify_capture", "historical_sources"} or stage["schema_version"] != "review-batch-staging/v1":
        raise ValueError("publication stage fields differ")
    materialization = json.loads(adoptions._capture(stage["qualified_materialization"], {}))
    if materialization.get("schema_version") != "isolated-unsealed-editorial-projection-qualification/v1":
        raise ValueError("qualified editorial materialization differs")
    matches = [source for source in materialization["sources"] if source["index"] == stage["source_index"]]
    if len(matches) != 1:
        raise ValueError("staging native source is ambiguous")
    source = matches[0]
    original = json.loads(adoptions._capture(source["source_handles"]["proposals.json"], {}))
    qualified = json.loads(adoptions._capture(source["projection_proposals"], {}))
    if len(original) != len(qualified) or any(
        {k: v for k, v in before.items() if k not in {"description", "rendered_description"}}
        != {k: v for k, v in after.items() if k not in {"description", "rendered_description"}}
        for before, after in zip(original, qualified, strict=True)):
        raise ValueError("qualified wording changes native accounting")
    clockify = json.loads(adoptions._capture(stage["clockify_capture"], {}))
    warning_view = fresh_warning_view(qualified, clockify)
    document, binding_handle = load(Path(stage["projection_binding"]["path"]))
    if binding_handle != stage["projection_binding"]:
        raise ValueError("staging preparation binding drifted")
    proposals = json.loads(adoptions._capture(document["source_proposals"], {}))
    if proposals != warning_view:
        raise ValueError("staging preparation is not the exact qualified warning view")
    source_dir = Path(document["source_proposals"]["path"]).parent
    routing = json.loads(adoptions._capture(source["source_handles"]["routing.json"], {}))
    allowlist = publisher.project_allowlist(routing)
    projection = project(path=Path(binding_handle["path"]), source_dir=source_dir, proposals=proposals,
        run_id=document["run_id"], spreadsheet_id=document["spreadsheet_id"], sheet_title=document["sheet_title"],
        project_allowlist=allowlist)
    capture = json.loads(adoptions._capture(stage["sheet_capture"], {}))
    sheet_id, captured = _cell_preimages(capture, document["spreadsheet_id"], document["sheet_title"])
    origins = {publisher.stable_review_id(p): p for p in original}
    if len(origins) != len(original):
        raise ValueError("staging original native identity is duplicated")
    original_run = Path(source["source_handles"]["proposals.json"]["path"]).parent.name
    witnesses = [(original_run, origins, allowlist)]
    for witness in stage["historical_sources"]:
        if set(witness) != {"proposals", "routing"}:
            raise ValueError("staging historical source fields differ")
        run = Path(witness["proposals"]["path"]).parent.name
        routing_handle = witness["routing"]
        if Path(routing_handle["path"]).parent.name != run:
            raise ValueError("staging historical native run differs")
        values = json.loads(adoptions._capture(witness["proposals"], {}))
        by_id = {publisher.stable_review_id(p): p for p in values}
        if len(by_id) != len(values):
            raise ValueError("staging historical native identity is duplicated")
        witnesses.append((run, by_id, publisher.project_allowlist(json.loads(adoptions._capture(routing_handle, {})))))
    eligible, diagnostics, authority = set(), [], {}
    for identity, proposal in origins.items():
        item = captured.get(identity)
        reason = None
        if item is None:
            reason = "missing_current_child"
        elif item["has_native_note"] or item["values"][14]:
            reason = "protected_native_or_review_note"
        elif (item["values"][9], item["values"][13]) != ("pending", "unposted"):
            reason = "protected_current_decision"
        else:
            row = item["values"]
            native = publisher.proposal_row(proposal, original_run, project_allowlist=allowlist)
            if any(not publisher._same_cell(row[i], native[i]) for i in [*range(8), 10]):
                reason = "native_financial_identity_or_route_differs"
            elif row[8] not in {proposal.get("description"), proposal.get("rendered_description"),
                    projection["editorial_descriptions"].get(identity),
                    next((p.get("description") for p in qualified if publisher.stable_review_id(p) == identity), None),
                    *(values[identity].get("description") for run, values, _projects in witnesses
                        if run == row[11] and identity in values)}:
                reason = "captured_wording_not_source_or_qualified_editorial_authority"
            elif not any(run == row[11] and identity in values and all(
                    publisher._same_cell(row[i], publisher.proposal_row(values[identity], run,
                        project_allowlist=projects)[i]) for i in [*range(8), 10, 11])
                    for run, values, projects in witnesses):
                reason = "missing_exact_last_seen_native_source_witness"
            else:
                authority[identity] = {"source_proposal": proposal, "captured_row": item["row"],
                    "display_difference_columns": [publisher.HEADER[i] for i in (8, 11, 12)
                        if not publisher._same_cell(row[i], native[i])]}
        if reason:
            diagnostics.append({"review_id": identity, "reason": reason})
        else:
            eligible.add(identity)
    # Restrict at child scope, so one missing/noted sibling cannot prevent safe
    # peers from receiving a usable presentation. Every original still survives
    # in the scoped package as a child or explicitly protected retained record.
    package = batches.package_review_batches(proposals,
        editorial_descriptions=document["editorial_descriptions"], work_contexts=document["work_contexts"],
        protected_ids=set(document["protected_ids"]) | (set(origins) - eligible),
        wording_only_ids=document.get("wording_only_ids", ()), timezone=document["timezone"],
        allow_review_overlaps=document.get("consolidation_policy") in {OVERLAP_CONSOLIDATION_POLICY, CLOCKIFY_OVERLAP_CONSOLIDATION_POLICY},
        allow_clockify_overlaps=document.get("consolidation_policy") == CLOCKIFY_OVERLAP_CONSOLIDATION_POLICY)
    # A lone safe peer is already a usable native row: do not create a new
    # parent merely to move the same micro-entry under another identity.
    singles = [activity for activity in package["activities"] if len(activity["child_review_ids"]) == 1]
    package["activities"] = [activity for activity in package["activities"] if len(activity["child_review_ids"]) > 1]
    by_id = {publisher.stable_review_id(p): p for p in proposals}
    for activity in singles:
        identity = activity["child_review_ids"][0]
        package["retained"].append({"review_id": identity, "proposal": copy.deepcopy(by_id[identity]),
            "preparation_reasons": ["single_current_source_preserved_natively"]})
    selected = [publisher.portfolio_row(activity, document["run_id"]) for activity in package["activities"]]
    publishable = _publication_members({**document, "package": package}) & eligible
    publishable |= {activity["child_review_ids"][0] for activity in singles}
    for retained in package["retained"]:
        identity = retained["review_id"]
        if identity in publishable:
            proposal = copy.deepcopy(by_id[identity])
            proposal["description"] = document["editorial_descriptions"][identity]
            selected.append(publisher.proposal_row(proposal, document["run_id"], project_allowlist=allowlist))
    selected.sort(key=lambda row: (row[1], row[0]))
    parents = {child: activity["review_id"] for activity in package["activities"] for child in activity["child_review_ids"]}
    selected_ids = {row[0] for row in selected}
    children = {child: captured[child]["values"] for child, parent in parents.items() if parent in selected_ids}
    # Standalone rows keep their authentic historical run marker. Only reviewed
    # description/reason cells may change; routes, time and notes stay untouched.
    for row in selected:
        if row[0] in captured:
            row[8] = captured[row[0]]["values"][8]
            row[11] = captured[row[0]]["values"][11]
            candidate = next(p for p in proposals if publisher.stable_review_id(p) == row[0])
            prior_warnings = next(p for p in qualified if publisher.stable_review_id(p) == row[0]).get("review_warnings", [])
            fresh = [warning for warning in candidate.get("review_warnings", []) if warning not in prior_warnings]
            row[12] = captured[row[0]]["values"][12]
            if fresh:
                row[12] += ("\n" if row[12] else "") + json.dumps(fresh, ensure_ascii=False, sort_keys=True)
    selected_originals = set(children) | {row[0] for row in selected if row[0] in captured}
    return {**projection, "rows": selected, "children": children,
        "parents": {child: parent for child, parent in parents.items() if child in children},
        "staged_package": package,
        "staged_preimages": {identity: captured[identity]["values"] for identity in selected_originals},
        "staging_receipt": {"stage": stage_handle, "sheet_id": sheet_id,
            "sheet_capture": stage["sheet_capture"], "clockify_capture": stage["clockify_capture"],
            "preimage_authority": authority, "diagnostics": diagnostics}}


def _check_staged_cell_data(gateway: Any, spreadsheet_id: str, sheet_title: str,
                            expected: Mapping[str, Any], sheet_id: int, *,
                            postimages: Mapping[str, Any] | None = None,
                            parents: Mapping[str, str] | None = None,
                            parent_rows: Mapping[str, Any] | None = None,
                            positions: Mapping[str, int] | None = None) -> None:
    reader = getattr(gateway, "cell_data", None)
    if reader is None:
        raise publisher.PublicationError("staged publication requires fresh full CellData readback")
    actual_sheet, actual = _cell_preimages(reader(spreadsheet_id, sheet_title), spreadsheet_id, sheet_title)
    if actual_sheet != sheet_id:
        raise publisher.PublicationError("staged target sheet changed")
    for identity, row in (parent_rows or {}).items():
        if identity in actual and (actual[identity]["has_native_note"] or actual[identity]["values"] != row):
            raise publisher.PublicationError("staged parent changed or gained a native note")
    for identity, before in expected.items():
        item = actual.get(identity)
        after = (postimages or {}).get(identity)
        matches = item is not None and (item["values"] == before or after is not None and item["values"] == after)
        if matches and item["values"] != before and identity in (parents or {}):
            parent = actual.get(parents[identity])
            matches = parent is not None and not parent["has_native_note"] and parent["values"] == (parent_rows or {}).get(parents[identity])
        if not matches or item["has_native_note"] or positions is not None and item["row"] != positions[identity]:
            raise publisher.PublicationError("staged child CellData changed or gained a native note")


def check_staged_preimages(gateway: Any, *, projection: Mapping[str, Any],
                           spreadsheet_id: str, sheet_title: str) -> None:
    """Fresh full-value/native-note guard for grouped AND standalone changes."""
    parents = projection["parents"]
    postimages = {child: _retired(before, parents[child]) for child, before in projection["children"].items()}
    postimages.update({row[0]: row for row in projection["rows"] if row[0] in projection["staged_preimages"]})
    _check_staged_cell_data(gateway, spreadsheet_id, sheet_title, projection["staged_preimages"],
        projection["staging_receipt"]["sheet_id"], postimages=postimages, parents=parents,
        parent_rows={row[0]: row for row in projection["rows"] if row[0] in set(parents.values())},
        positions={identity: projection["staging_receipt"]["preimage_authority"][identity]["captured_row"]
            for identity in projection["staged_preimages"]})


def publish_staged_projection(gateway: Any, *, projection: Mapping[str, Any],
                              spreadsheet_id: str, sheet_title: str) -> dict[str, Any]:
    """Value-only operator publication; exact parent readback before retirement."""
    if "staging_receipt" not in projection:
        raise publisher.PublicationError("staged publication lacks its source-bound stage")
    check_staged_preimages(gateway, projection=projection, spreadsheet_id=spreadsheet_id, sheet_title=sheet_title)
    child_plans = plan_children(gateway, spreadsheet_id=spreadsheet_id, sheet_title=sheet_title, projection=projection)
    plan = publisher._plan_publish(gateway, spreadsheet_id=spreadsheet_id, sheet_title=sheet_title,
        template_title="Proposals", rows=projection["rows"])
    if plan["created"]:
        raise publisher.PublicationError("staged publication requires an existing target sheet")
    updates = []
    positions, existing = publisher._scan_rows(gateway, spreadsheet_id, plan["quoted_title"], plan["row_count"])
    for row in projection["rows"]:
        if row[0] not in projection["staged_preimages"]:
            continue
        before = list(existing[positions[row[0]]])
        before.extend([""] * (15-len(before)))
        number = projection["staging_receipt"]["preimage_authority"][row[0]]["captured_row"]
        updates.extend({"range": f"{plan['quoted_title']}!{chr(65+i)}{number}", "values": [[row[i]]]}
            for i in range(15) if row[i] != before[i])
    # Recheck immediately before any value write; never rewrite existing formats,
    # validations or native notes. The caller must authorize external effects.
    check_staged_preimages(gateway, projection=projection, spreadsheet_id=spreadsheet_id, sheet_title=sheet_title)
    if updates:
        gateway.update_values(spreadsheet_id, updates)
    if plan["appends"]:
        gateway.append_values(spreadsheet_id, f"{plan['quoted_title']}!A:O", plan["appends"])
    publisher._verify_readback(gateway, spreadsheet_id, plan["quoted_title"],
        max(plan["row_count"], plan["existing_max_row"] + len(plan["appends"])), projection["rows"],
        new_ids=frozenset(row[0] for row in projection["rows"]))
    terminal_updates = retire_children(gateway, spreadsheet_id=spreadsheet_id, sheet_title=sheet_title,
        plans=child_plans, parent_rows=projection["rows"])
    return {"appended": len(plan["appends"]), "value_updates": len(updates), "terminal_updates": terminal_updates,
        "retired_child_rows": [plan["row"] for plan in child_plans], "staging": projection["staging_receipt"],
        "publication": publisher._publication_receipt(spreadsheet_id=spreadsheet_id, sheet_title=sheet_title,
            rows=projection["rows"]), "clockify_writes": 0}


def plan_children(gateway: publisher.SheetsGateway, *, spreadsheet_id: str,
                  sheet_title: str, projection: Mapping[str, Any]) -> list[dict[str, Any]]:
    metadata = gateway.spreadsheet(spreadsheet_id)
    if sheet_title not in publisher._sheet_map(metadata):
        return []
    quoted = publisher._a1_title(sheet_title)
    positions, existing = publisher._scan_rows(gateway, spreadsheet_id, quoted,
                                               publisher._sheet_row_count(metadata, sheet_title))
    plans = []
    staging = projection.get("staging_receipt")
    if staging is not None:
        check_staged_preimages(gateway, projection=projection, spreadsheet_id=spreadsheet_id, sheet_title=sheet_title)
    for child, original in projection["children"].items():
        if child not in positions:
            continue
        number = positions[child]
        prior = list(existing[number])
        prior.extend([""] * (len(publisher.HEADER) - len(prior)))
        clean = list(original)
        clean[8] = projection["editorial_descriptions"][child]
        variants = [original, clean]
        if len(prior) != len(original):
            raise publisher.PublicationError("prepared review child width differs")
        equal = lambda row: all(publisher._same_cell(a, b) for a, b in zip(prior, row))
        if any(equal(_retired(row, projection["parents"][child])) for row in variants):
            continue
        if not any(equal(row) for row in variants):
            raise publisher.PublicationError("prepared review child changed or has a protected human decision")
        plans.append({"row": number, "before": prior, "after": _retired(prior, projection["parents"][child]),
                      "parent_review_id": projection["parents"][child],
                      **({"staging_sheet_id": staging["sheet_id"]} if staging is not None else {})})
    return plans


def retire_children(gateway: publisher.SheetsGateway, *, spreadsheet_id: str,
                    sheet_title: str, plans: Sequence[Mapping[str, Any]],
                    parent_rows: Sequence[Sequence[Any]]) -> int:
    """Called only after exact parent readbacks; financial tombstones are unrelated."""
    if not plans:
        return 0
    staged = [plan for plan in plans if "staging_sheet_id" in plan]
    if staged:
        _check_staged_cell_data(gateway, spreadsheet_id, sheet_title,
            {plan["before"][0]: plan["before"] for plan in staged}, staged[0]["staging_sheet_id"])
    quoted = publisher._a1_title(sheet_title)
    metadata = gateway.spreadsheet(spreadsheet_id)
    positions, existing = publisher._scan_rows(gateway, spreadsheet_id, quoted,
        publisher._sheet_row_count(metadata, sheet_title))
    expected_parents = {row[0]: list(row) for row in parent_rows}
    for parent_id in {plan["parent_review_id"] for plan in plans}:
        expected = expected_parents.get(parent_id)
        actual = list(existing.get(positions.get(parent_id), []))
        actual.extend([""] * (len(publisher.HEADER) - len(actual)))
        if (expected is None or len(actual) != len(expected)
                or any(not publisher._same_cell(a, b) for a, b in zip(actual, expected))):
            raise publisher.PublicationError("prepared review parent changed or has a protected human decision")
    for plan in plans:
        before = plan["before"]
        prior = list(existing.get(plan["row"], []))
        prior.extend([""] * (len(publisher.HEADER) - len(prior)))
        if positions.get(before[0]) != plan["row"] or prior != before:
            raise publisher.PublicationError("prepared review child full preimage drifted before retirement")
    gateway.update_values(spreadsheet_id, [
        {"range": f"{quoted}!J{plan['row']}:N{plan['row']}", "values": [plan["after"][9:14]]}
        for plan in plans])
    _, existing = publisher._scan_rows(gateway, spreadsheet_id, quoted,
        publisher._sheet_row_count(metadata, sheet_title))
    for plan in plans:
        actual = list(existing.get(plan["row"], []))
        actual.extend([""] * (len(publisher.HEADER) - len(actual)))
        if actual != plan["after"]:
            raise publisher.PublicationError("prepared review child retirement readback differs")
    return len(plans)
