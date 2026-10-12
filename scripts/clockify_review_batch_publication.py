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
FIELDS = {"schema_version", "run_id", "spreadsheet_id", "sheet_title", "source_proposals",
          "editorial_descriptions", "work_contexts", "protected_ids", "timezone", "package"}
STANDALONE_REASONS = {"meeting", "overlap_warning", "source_interval_overlap", "not_micro_entry",
                     "untagged_native_entry"}
AUTO_FIELDS = FIELDS | {"authority", "consolidation_policy", "standalone_review_ids",
                        "preparation_diagnostics", "preparation_outcome"}
AUTHORITY_FILES = {"semantic_analysis": "semantic-analysis.json",
                   "corrections": "review-corrections.jsonl", "routing": "routing.json"}


def _derive_authority(proposals: Sequence[Mapping[str, Any]], authority: Mapping[str, Any],
                      source_dir: Path, timezone: str) -> dict[str, Any]:
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
    # The integrity-checked loader reads the same snapshot bytes, not a mutable
    # current configuration or corrections log outside this completed run.
    adoptions._capture(authority["corrections"], {})
    activities: dict[Any, list[Mapping[str, Any]]] = {}
    for activity in analysis["activities"]:
        if isinstance(activity, Mapping) and (target := review_corrections.proposal_target(activity)):
            activities.setdefault(target, []).append(activity)
    descriptions, contexts, protected, standalone, diagnostics = {}, {}, set(), set(), []
    protected_existing = set()
    for proposal in proposals:
        identity = publisher.stable_review_id(proposal)
        target = review_corrections.proposal_target(proposal)
        matches = [case for case in cases if (case["activity_id"], case["evidence_fingerprint"]) == target]
        if (any(case["decision"] in {"approve", "skip"} for case in matches)
                or any(str(proposal.get(field) or "").strip().casefold() in {"approved", "posted", "rejected", "superseded"}
                       for field in ("disposition", "review_status", "status"))):
            protected.add(identity)
            protected_existing.add(identity)
            continue
        corrected = pipeline._description_from_review_correction(proposal, cases)
        if matches or proposal.get("human_corrected"):
            protected.add(identity)
        reasons = []
        if corrected is not None:
            description = corrected
            if proposal.get("description") != corrected:
                reasons.append("wording_correction_not_materialized_in_source")
            else:
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
    return {"editorial_descriptions": descriptions, "work_contexts": contexts,
            "protected_ids": sorted(protected), "standalone_review_ids": sorted(standalone),
            "preparation_diagnostics": sorted(diagnostics, key=lambda item: item["review_id"]),
            "protected_existing_ids": protected_existing}


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
                 source_dir: Path, timezone: str) -> dict[str, Any]:
    fields = _derive_authority(proposals, authority, source_dir, timezone)
    protected_existing = fields.pop("protected_existing_ids")
    package = batches.package_review_batches(proposals, editorial_descriptions=fields["editorial_descriptions"],
        work_contexts=fields["work_contexts"], protected_ids=fields["protected_ids"], timezone=timezone)
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
    fields = _auto_fields(proposals, authority, source_dir, timezone)
    document = {"schema_version": AUTO_SCHEMA, "run_id": run_id, "spreadsheet_id": spreadsheet_id,
        "sheet_title": sheet_title, "source_proposals": proposals_handle, "timezone": timezone,
        "authority": authority, "consolidation_policy": CONSOLIDATION_POLICY, **fields}
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output.exists():
        existing, _handle = load(output)
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
            or set(document) != (FIELDS if document["schema_version"] == SCHEMA else AUTO_FIELDS)):
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
        if document["consolidation_policy"] != CONSOLIDATION_POLICY:
            raise ValueError("automatic preparation consolidation policy differs")
        source_handle = document["source_proposals"]
        proposals = json.loads(adoptions._capture(source_handle, {}))
        source_dir = Path(source_handle["path"]).parent
        derived = _auto_fields(proposals, document["authority"], source_dir, document["timezone"])
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
        protected_ids=document["protected_ids"], timezone=document["timezone"])
    if expected != document["package"]:
        raise ValueError("prepared review package differs from explicit editorial inputs")
    package = batches.verify_package(document["package"], proposals=frozen,
        work_contexts=document["work_contexts"], protected_ids=document["protected_ids"],
        timezone=document["timezone"])
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


def plan_children(gateway: publisher.SheetsGateway, *, spreadsheet_id: str,
                  sheet_title: str, projection: Mapping[str, Any]) -> list[dict[str, Any]]:
    metadata = gateway.spreadsheet(spreadsheet_id)
    if sheet_title not in publisher._sheet_map(metadata):
        return []
    quoted = publisher._a1_title(sheet_title)
    positions, existing = publisher._scan_rows(gateway, spreadsheet_id, quoted,
                                               publisher._sheet_row_count(metadata, sheet_title))
    plans = []
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
                      "parent_review_id": projection["parents"][child]})
    return plans


def retire_children(gateway: publisher.SheetsGateway, *, spreadsheet_id: str,
                    sheet_title: str, plans: Sequence[Mapping[str, Any]],
                    parent_rows: Sequence[Sequence[Any]]) -> int:
    """Called only after exact parent readbacks; financial tombstones are unrelated."""
    if not plans:
        return 0
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
