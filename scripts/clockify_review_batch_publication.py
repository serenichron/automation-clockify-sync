"""Opt-in prepared review publication; no inference, Clockify credit or time edits.

The manifest retains every original source privately, including unready sources.
Only mechanically verified prepared activities become public rows. Editorial
wording and work identities are explicit caller inputs, not inferred here.
"""
from __future__ import annotations

import json
import copy
import datetime as dt
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Sequence

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
STRUCTURED_EDITORIAL_CONSOLIDATION_POLICY = "approved-daily-project-presentation/v5"
NATURAL_BATCH_CONSOLIDATION_POLICY = "approved-daily-project-presentation/v6"
ALL_DURATION_CONSOLIDATION_POLICY = "approved-daily-project-presentation/v7"
LIFECYCLE_ROUTE_POLICY = "frozen-lifecycle-editorial-route/v1"
FIELDS = {"schema_version", "run_id", "spreadsheet_id", "sheet_title", "source_proposals",
          "editorial_descriptions", "work_contexts", "protected_ids", "timezone", "package"}
STANDALONE_REASONS = {"meeting", "overlap_warning", "source_interval_overlap", "not_micro_entry",
                     "untagged_native_entry"}
AUTO_FIELDS = FIELDS | {"authority", "consolidation_policy", "standalone_review_ids",
                        "preparation_diagnostics", "preparation_outcome"}
WORDING_AUTO_FIELDS = AUTO_FIELDS | {"wording_only_ids"}
AUTHORITY_FILES = {"semantic_analysis": "semantic-analysis.json",
                   "corrections": "review-corrections.jsonl", "routing": "routing.json"}
RECORDED_ATTENDANCE_FILES = {"completion": "completion-bundle.json", "accounting": "work-accounting-result.json",
    "ledger": "evidence/evidence-ledger.json", "reconciliation": "fathom-reconciliation.json",
    "quality": "quality_report.json"}


def _recorded_attendance_descriptions(proposals: Sequence[Mapping[str, Any]], analysis: Mapping[str, Any],
        source_dir: Path, routing: Mapping[str, Any], captured: Mapping[str, Any] | None = None
        ) -> tuple[dict[str, str], dict[str, Any]]:
    """Rederive full canonical attendance facts, never semantic accomplishments."""
    from scripts import collector_receipts, review_corrections, semantic_analyzer as semantic
    from scripts import work_accounting_pipeline as pipeline
    candidates = [p for p in proposals if isinstance(p.get("provenance"), Mapping)
                  and p["provenance"].get("source_type") == "recorded_meeting"
                  and p["provenance"].get("semantic_fallback") is True]
    if not candidates:
        return {}, {}
    try:
        handles = ({name: artifact_handle(source_dir / filename) for name, filename in RECORDED_ATTENDANCE_FILES.items()}
                   if captured is None else captured["sources"])
        if not isinstance(handles, Mapping) or set(handles) != set(RECORDED_ATTENDANCE_FILES):
            raise ValueError("recorded attendance authority inventory differs")
        for name, filename in RECORDED_ATTENDANCE_FILES.items():
            if not isinstance(handles[name], Mapping) or handles[name].get("path") != str(source_dir / filename):
                raise ValueError("recorded attendance authority source differs")
        documents = {name: json.loads(adoptions._capture(handle, {})) for name, handle in handles.items()}
        collector_receipts.load_completion_bundle(source_dir / "completion-bundle.json", run_dir=source_dir)
        accounting, reconciliation = documents["accounting"], documents["reconciliation"]
        if (not isinstance(documents["quality"], Mapping) or not isinstance(accounting, Mapping)
                or documents["quality"].get("status") != "pass" or accounting.get("proposals") != list(proposals)
                or not isinstance(reconciliation, list) or accounting.get("fathom_reconciliation") != reconciliation):
            return {}, {}
        ledger, events = pipeline.load_ledger(source_dir / RECORDED_ATTENDANCE_FILES["ledger"])
        recordings, exceptions = pipeline._recording_events(events, ledger.manifest.document())
        members = pipeline.meeting_reconciliation.manifest_member_identities(ledger.manifest.document())
        descriptions = {}
        for proposal in candidates:
            provenance = proposal["provenance"]
            canonical_id = provenance.get("canonical_meeting_id")
            entries = [e for e in recordings if e["meeting"].canonical_id == canonical_id]
            if len(entries) != 1 or sum(isinstance(p.get("provenance"), Mapping)
                    and p["provenance"].get("canonical_meeting_id") == canonical_id for p in proposals) != 1:
                continue
            entry = entries[0]
            citations = entry["source_evidence_ids"]
            representative = next((e for e in entry["events"] if e.get("source_type") == "fathom"), None)
            if (representative is None or any(set(citations) & set(e["source_evidence_ids"]) for e in exceptions)
                    or not pipeline._meeting_is_eligible(representative, members)[0]
                    or pipeline._attributes(representative).get("semantic_evidence_status") == "title_only"):
                continue
            meeting = entry["meeting"]
            title = str(meeting.title or "").strip()
            activity = {"activity_id": semantic.stable_digest("act-", {"recorded_attendance": canonical_id}),
                "workstream_id": semantic.stable_digest("ws-", {"recorded_meeting": canonical_id}),
                "object": title, "effort": {}, "timing_confidence": "high",
                "split_rationale": "Authoritative canonical recording interval; factual attendance only.",
                "evidence_ids": citations}
            target = review_corrections.proposal_target(activity)
            if any(isinstance(a, Mapping) and review_corrections.proposal_target(a) == target for a in analysis["activities"]):
                continue
            start, end = pipeline._canonical_meeting_span(meeting, representative)
            expected = pipeline._proposal(activity, {}, "", start, end, citations, 1)
            expected_provenance = {"source_type": "recorded_meeting", "source_session_id": canonical_id,
                "source_machine": "cross-machine", "burst_start": expected["start"], "burst_end": expected["end"],
                "evidence_ids": citations, "canonical_meeting_id": canonical_id, "recorded_meeting_title": title,
                "recorded_meeting_start": expected["start"], "recorded_meeting_end": expected["end"],
                "meeting_precedence": pipeline._meeting_precedence(representative), "semantic_fallback": True}
            immutable = ("activity_id", "workstream_id", "candidate_key", "review_activity_key", "allocation_segment",
                "start", "end", "duration_seconds", "duration_minutes", "source", "source_label", "effort",
                "timing_confidence", "allocation_mode")
            reconciled = [r for r in reconciliation if r.get("canonical_id") == canonical_id]
            if (not title or provenance != expected_provenance
                    or any(proposal.get(field) != expected[field] for field in immutable)
                    or len(reconciled) != 1 or any(reconciled[0].get(field) != value for field, value in {
                        "status": "proposed", "activity_id": activity["activity_id"], "semantic_fallback": True,
                        "source_evidence_ids": citations, "evidence_id": citations[0]}.items())):
                continue
            description = proposal.get("description")
            prefix = description.split(" — ", 1)[0] if isinstance(description, str) else ""
            if description != f"{prefix} — Attended {title}" or (not proposal.get("client_project") and prefix != "SC"):
                continue
            # Native publication validates all warning structures and the exact
            # blank/nonbillable unresolved-route contract without clearing them.
            try:
                publisher.proposal_row(proposal, source_dir.name, project_allowlist=publisher.project_allowlist(routing))
            except publisher.PublicationError:
                continue
            descriptions[publisher.stable_review_id(proposal)] = description
        return descriptions, dict(handles)
    except (OSError, ValueError, KeyError, TypeError, pipeline.WorkAccountingError):
        return {}, {}

def bind_current_presentation(*, capture: Mapping[str, Any], proposals: Sequence[Mapping[str, Any]],
                              review_ids: Sequence[str], timezone: str) -> dict[str, Any]:
    """Seal an existing pending display, never a correction or financial approval."""
    from scripts import clockify_native_sheet_post as native
    authority = {"schema_version": "review-batch-current-presentation/v1",
        "capture": copy.deepcopy(dict(capture)), "timezone": timezone,
        "source_proposals_sha256": "sha256:" + native._digest(list(proposals)),
        "review_ids": sorted(review_ids)}
    _current_presentation_rows(authority, proposals, timezone)
    return authority


def _current_presentation_rows(authority: Mapping[str, Any], proposals: Sequence[Mapping[str, Any]],
                               timezone: str) -> tuple[str, str, dict[str, Any]]:
    """Seal display cells without treating their prose as machine warning authority.

    Description, Revision and Reason may be later presentations. Raw warnings
    remain in the immutable proposals and independently verified batch package;
    no warning is reconstructed, certified or cleared from captured Reason text.
    """
    from scripts import clockify_native_sheet_post as native
    if (not isinstance(authority, Mapping) or set(authority) != {
            "schema_version", "capture", "timezone", "source_proposals_sha256", "review_ids"}
            or authority["schema_version"] != "review-batch-current-presentation/v1"
            or authority["timezone"] != timezone
            or authority["source_proposals_sha256"] != "sha256:" + native._digest(list(proposals))):
        raise ValueError("current presentation source binding differs")
    ids = authority["review_ids"]
    if (not isinstance(ids, list) or not ids or any(not isinstance(i, str) for i in ids)
            or ids != sorted(set(ids))):
        raise ValueError("current presentation child inventory differs")
    document = json.loads(adoptions._capture(authority["capture"], {}))
    spreadsheet, title, _ = native._sheet_rows(document, pending_only=False)
    _, captured = _cell_preimages(document["structuredContent"], spreadsheet, title)
    originals = {publisher.stable_review_id(p): p for p in proposals}
    if len(originals) != len(proposals) or not set(ids) <= set(originals):
        raise ValueError("current presentation original identities differ")
    rows = {}
    for identity in ids:
        item = captured.get(identity)
        proposal = originals[identity]
        if item is None:
            raise ValueError("current presentation child is absent")
        row = item["values"]
        before = publisher.proposal_row(proposal, str(row[11]))
        if (any(str(proposal.get(field) or "").strip().casefold() not in {"", "pending", "unposted"}
                for field in ("disposition", "review_status", "status"))
                or (row[9], row[13]) != ("pending", "unposted")
                or any(not publisher._same_cell(row[i], before[i])
                       for i in [*range(8), 9, 11, 13])
                or type(row[10]) not in {int, float} or row[10] < before[10]
                or not float(row[10]).is_integer()
                or not isinstance(row[8], str) or not row[8].strip()
                or any(char in row[8] for char in "\n\r\t")):
            raise ValueError("current presentation changes source accounting or protected decision")
        rows[identity] = (item["row"], copy.deepcopy(row))
    return spreadsheet, title, rows


def _require_current_presentation_notes(authority: Mapping[str, Any], document: Mapping[str, Any]) -> None:
    """Native notes are conserved exactly, never parsed into warning authority."""
    selected = set(authority["review_ids"])
    def notes(capture):
        result = {}
        for grid in capture["structuredContent"]["sheets"][0]["data"]:
            for number, row in enumerate(grid.get("rowData", []), grid.get("startRow", 0) + 1):
                cells = list(row.get("values", []))
                identity = cells[0].get("userEnteredValue", {}).get("stringValue") if cells else None
                if identity in selected:
                    cells.extend({} for _ in range(len(publisher.HEADER) - len(cells)))
                    result[identity] = (number, [cell.get("note", "") for cell in cells])
        return result
    original = json.loads(adoptions._capture(authority["capture"], {}))
    if notes(document) != notes(original):
        raise ValueError("current presentation native notes changed or disappeared")


_INTERNAL_JOURNAL_RE = re.compile(
    r"\b(?:uncommitted|undeployed|lint\s+clean)\b|"
    r"\b\d+\s+(?:(?:locked|combined|local|unit)\s+)*(?:Composer\s+packages|PHP\s+files|tests?)\b",
    re.I,
)
_MEANINGFUL_OUTCOME_RE = re.compile(
    r"[$€£]|\b(?:EUR|USD|GBP|RON|euros?|dollars?|saved|reduced|increased|delivered|"
    r"approved|resolved|blocked|failed|unresolved|incomplete)\b|\b\d{4}-\d{2}-\d{2}\b|"
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2}\b",
    re.I,
)


def _structured_editorial_description(activity: Mapping[str, Any], proposal: Mapping[str, Any]) -> Any:
    """Omit whole clearly technical fields from an exact reviewed field render.

    This is a narrow presentation transform, not a semantic certification.
    Safe reviewed wording needs no new field contract. Ambiguous mixed outcomes
    require explicit review rather than guessing, truncating, or deleting facts.
    """
    from scripts import clockify_portfolio_repair as repair
    description = activity.get("rendered_description")
    if not isinstance(description, str) or not _INTERNAL_JOURNAL_RE.search(description):
        return description
    parts = [activity.get(field) for field in ("action", "object", "outcome")]
    if any(not isinstance(part, str) or not part or part != part.strip()
           or any(char in part for char in "\n\r\t") for part in parts):
        raise ValueError("missing_structured_editorial_authority")
    prefix, separator, _ = description.partition(" — ")
    if (not separator or f"{prefix} — {' '.join(parts)}" != description
            or proposal.get("description") != description):
        raise ValueError("structured_editorial_source_mismatch")
    action, obj, outcome = parts
    clean = f"{prefix} — {action} {obj}"
    if _INTERNAL_JOURNAL_RE.search(obj):
        if _MEANINGFUL_OUTCOME_RE.search(obj):
            raise ValueError("inseparable_internal_editorial_object")
        # Some reviewed actions already name the full accomplishment. Keep the
        # entire action, never guess a new action/object split from its words.
        clean = f"{prefix} — {action}"
    if (_INTERNAL_JOURNAL_RE.search(clean) or repair.repair_reasons(clean)
            or not _INTERNAL_JOURNAL_RE.search(outcome) or _MEANINGFUL_OUTCOME_RE.search(outcome)):
        raise ValueError("inseparable_internal_editorial_outcome")
    return clean


def frozen_lifecycle_route_authority(source_dir: Path) -> dict[str, Any]:
    """Pin optional fresh lifecycle-route evidence without changing old bindings."""
    return {"policy": LIFECYCLE_ROUTE_POLICY,
            "ledger": artifact_handle(Path(source_dir) / "evidence/evidence-ledger.json")}


def _lifecycle_route_events(authority: Mapping[str, Any], source_dir: Path) -> dict[str, dict[str, Any]]:
    from scripts import evidence_ledger
    if (not isinstance(authority, Mapping) or set(authority) != {"policy", "ledger"}
            or authority["policy"] != LIFECYCLE_ROUTE_POLICY
            or not isinstance(authority["ledger"], Mapping)
            or authority["ledger"].get("path") != str(source_dir / "evidence/evidence-ledger.json")):
        raise ValueError("frozen lifecycle route authority differs")
    document = json.loads(adoptions._capture(authority["ledger"], {}))
    if document.get("schema_version") != evidence_ledger.SCHEMA_VERSION or not isinstance(document.get("events"), list):
        raise ValueError("frozen lifecycle route ledger differs")
    manifest = evidence_ledger.LedgerManifest.from_document(document.get("manifest"))
    ledger = evidence_ledger.EvidenceLedger(tuple(evidence_ledger.EvidenceEvent.from_document(event)
        for event in document["events"]), manifest.source_inventory, manifest.timezone, manifest.member_identities)
    ledger.validate(manifest)
    return {event.evidence_id: event.document() for event in ledger.events}


def _materialized_lifecycle_route(proposal: Mapping[str, Any], prefix: str,
        cited: list[dict[str, Any]], routing: Mapping[str, Any]) -> dict[str, Any] | None:
    """Validate an exact human classification override, not infer it again."""
    from scripts import work_accounting_pipeline as pipeline
    matches = {}
    observed = [pipeline._parse_dt(event.get("observed_at")) for event in cited]
    for rule in routing.get("client_lifecycle_routes", []):
        activation = rule.get("activation") if isinstance(rule, Mapping) else None
        route = activation.get("route") if isinstance(activation, Mapping) else None
        if not isinstance(route, Mapping):
            continue
        route = pipeline._apply_prefix_override(route, cited, routing)
        if (str(route.get("project_name") or "").casefold() != str(proposal.get("client_project") or "").casefold()
                or route.get("prefix") != prefix
                or sorted(route.get("tag_names") or []) != sorted(proposal.get("tag_names") or [])):
            continue
        effective = pipeline._parse_dt(activation.get("effective_at"))
        if effective is None or not any(value is not None and value >= effective for value in observed):
            continue
        identity = (str(route.get("project_suffix") or ""), tuple(sorted(route.get("tag_suffixes") or [])),
                    route.get("prefix"), route.get("billable", True))
        matches[identity] = route
    # Two active declarations with distinct native targets are ambiguous even
    # if one happens to agree with the materialized proposal's native IDs.
    return next(iter(matches.values())) if len(matches) == 1 else None


def _derive_authority(proposals: Sequence[Mapping[str, Any]], authority: Mapping[str, Any],
                      source_dir: Path, timezone: str, *, allow_wording_consolidation: bool = False,
                      clean_structured_outcomes: bool = False,
                      allow_materialized_corrections: bool = False,
                      lifecycle_route_authority: Mapping[str, Any] | None = None,
                      allow_recorded_attendance: bool = False,
                      recorded_attendance_authority: Mapping[str, Any] | None = None) -> dict[str, Any]:
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
    if lifecycle_route_authority is not None and not allow_materialized_corrections:
        raise ValueError("frozen lifecycle routes require fresh materialized-correction policy")
    lifecycle_events = (_lifecycle_route_events(lifecycle_route_authority, source_dir)
                        if lifecycle_route_authority is not None else {})
    attendance, attendance_handles = (_recorded_attendance_descriptions(proposals, analysis, source_dir, routing,
        recorded_attendance_authority) if allow_recorded_attendance else ({}, {}))
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
    materialized_corrections = set()
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
        # This does not apply a patch or infer routing authority. Every effective
        # exact-target routing/wording replacement must already match the sealed
        # source; the route's IDs and billable state are checked below as usual.
        materialized = bool(matches) and all(
            case["decision"] == "modify"
            and set(case["correction_categories"]) <= {"routing", "wording"}
            and bool(case["expected_field_patch"])
            and set(case["expected_field_patch"]) <= {"client_project", "tag_names", "description"}
            and all(operation == {"op": "replace", "value": proposal.get(field)}
                    for field, operation in case["expected_field_patch"].items())
            for case in matches)
        group_materialized = (allow_materialized_corrections and not pure_wording and materialized
                              and pending and target not in non_wording_targets
                              and corrected is not None and proposal.get("description") == corrected)
        group_corrected = (allow_wording_consolidation and pure_wording and pending
                           and target not in non_wording_targets
                           and corrected is not None and proposal.get("description") == corrected)
        if matches or proposal.get("human_corrected"):
            if not (group_corrected or group_materialized):
                protected.add(identity)
        if allow_materialized_corrections and target in non_wording_targets:
            protected.add(identity)
        reasons = []
        factual_attendance = (identity in attendance and corrected is None and (not matches or (
            materialized and all(case["correction_categories"] == ["routing"] for case in matches))))
        if corrected is not None:
            description = corrected
            if proposal.get("description") != corrected:
                reasons.append("wording_correction_not_materialized_in_source")
            elif not (group_corrected or group_materialized):
                standalone.add(identity)
            if allow_materialized_corrections and not pure_wording and not materialized:
                reasons.append("routing_wording_correction_not_materialized_in_source")
        elif factual_attendance:
            description = attendance[identity]
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
                if clean_structured_outcomes and not reasons:
                    try:
                        description = _structured_editorial_description(activity, proposal)
                    except ValueError as error:
                        reasons.append(str(error))
        unsafe = repair.repair_reasons(description)
        if unsafe:
            reasons.append("unsafe_or_missing_reviewed_wording")
        if factual_attendance and proposal.get("routing_disposition") == "unresolved-routing":
            pass  # Exact blank route/warnings were validated against native attendance above.
        elif isinstance(description, str) and " — " in description:
            prefix = description.split(" — ", 1)[0]
            route = routes.get((str(proposal.get("client_project") or "").casefold(), prefix,
                                tuple(sorted(proposal.get("tag_names") or []))))
            if route is None and lifecycle_route_authority is not None:
                reviewed = activities.get(target, [])
                citations = review_corrections._proposal_evidence_ids(proposal)
                if len(reviewed) == 1 and citations and citations <= lifecycle_events.keys():
                    cited = [lifecycle_events[identity] for identity in sorted(citations)]
                    if group_materialized and any({"client_project", "tag_names"} <= case["expected_field_patch"].keys()
                                                  and "routing" in case["correction_categories"] for case in matches):
                        route = _materialized_lifecycle_route(proposal, prefix, cited, routing)
                    else:
                        route = pipeline._route_from_client_lifecycle(cited, routing, activity=reviewed[0])
                        if route is not None:
                            route = pipeline._apply_prefix_override(route, cited, routing)
            if (route is None or str(route.get("project_name") or "").casefold() != str(proposal.get("client_project") or "").casefold()
                    or str(route.get("prefix") or "SC") != prefix
                    or sorted(route.get("tag_names") or []) != sorted(proposal.get("tag_names") or [])
                    or str(route.get("project_suffix") or "") != str(proposal.get("clockify_project_suffix") or "")
                    or sorted(route.get("tag_suffixes") or []) != sorted(proposal.get("tag_suffixes") or [])
                    or route.get("billable", True) != proposal.get("billable")):
                reasons.append("editorial_route_not_bound_to_snapshot")
        else:
            reasons.append("editorial_route_not_bound_to_snapshot")
        if identity in protected and corrected is None and not factual_attendance:
            reasons.append("protected_correction_without_exact_wording_authority")
        if reasons:
            standalone.discard(identity)
            diagnostics.append({"review_id": identity, "reasons": sorted(set(reasons))})
            continue
        descriptions[identity] = description
        contexts[identity] = CONSOLIDATION_POLICY
        if group_corrected:
            wording_only.add(identity)
        if group_materialized:
            materialized_corrections.add(identity)
    return {"editorial_descriptions": descriptions, "work_contexts": contexts,
            "protected_ids": sorted(protected), "standalone_review_ids": sorted(standalone),
            "preparation_diagnostics": sorted(diagnostics, key=lambda item: item["review_id"]),
            "protected_existing_ids": protected_existing,
            **({"wording_only_ids": sorted(wording_only)} if allow_wording_consolidation else {}),
            **({"materialized_correction_ids": sorted(materialized_corrections)}
               if allow_materialized_corrections else {}),
            **({"lifecycle_route_authority": copy.deepcopy(dict(lifecycle_route_authority))}
               if lifecycle_route_authority is not None else {}),
            **({"recorded_attendance_authority": {"sources": attendance_handles,
                "review_ids": sorted(set(attendance) & set(descriptions) & standalone)}}
               if set(attendance) & set(descriptions) & standalone else {})}


def _publication_members(document: Mapping[str, Any]) -> set[str]:
    identities = {child for activity in document["package"]["activities"] for child in activity["child_review_ids"]}
    for retained in document["package"]["retained"]:
        allowed = STANDALONE_REASONS
        if document["schema_version"] == AUTO_SCHEMA and retained["review_id"] in document["standalone_review_ids"]:
            allowed = allowed | {"protected_review_id", "human_corrected"}
        if retained["review_id"] in document.get("recorded_attendance_authority", {}).get("review_ids", []):
            allowed = allowed | {"unresolved_routing", "invalid_route_or_activity"}
        if (set(retained["preparation_reasons"]) <= allowed
                and retained["review_id"] in document["editorial_descriptions"]
                and retained["review_id"] in document["work_contexts"]):
            identities.add(retained["review_id"])
    return identities


def _auto_fields(proposals: Sequence[Mapping[str, Any]], authority: Mapping[str, Any],
                 source_dir: Path, timezone: str, *, consolidation_policy: str = CONSOLIDATION_POLICY,
                 group_summaries: Mapping[str, Any] | None = None,
                 allow_materialized_corrections: bool = False,
                 lifecycle_route_authority: Mapping[str, Any] | None = None,
                 current_presentation: Mapping[str, Any] | None = None,
                 allow_recorded_attendance: bool = False,
                 recorded_attendance_authority: Mapping[str, Any] | None = None) -> dict[str, Any]:
    fields = _derive_authority(proposals, authority, source_dir, timezone,
        allow_wording_consolidation=consolidation_policy in {WORDING_CONSOLIDATION_POLICY, OVERLAP_CONSOLIDATION_POLICY,
                                                            CLOCKIFY_OVERLAP_CONSOLIDATION_POLICY,
                                                            STRUCTURED_EDITORIAL_CONSOLIDATION_POLICY,
                                                            NATURAL_BATCH_CONSOLIDATION_POLICY, ALL_DURATION_CONSOLIDATION_POLICY},
        clean_structured_outcomes=consolidation_policy in {STRUCTURED_EDITORIAL_CONSOLIDATION_POLICY,
                                                         NATURAL_BATCH_CONSOLIDATION_POLICY, ALL_DURATION_CONSOLIDATION_POLICY},
        allow_materialized_corrections=allow_materialized_corrections,
        lifecycle_route_authority=lifecycle_route_authority,
        allow_recorded_attendance=allow_recorded_attendance,
        recorded_attendance_authority=recorded_attendance_authority)
    protected_existing = fields.pop("protected_existing_ids")
    if current_presentation is not None:
        _, _, current = _current_presentation_rows(current_presentation, proposals, timezone)
        if set(current) - (set(fields["editorial_descriptions"]) - set(fields["protected_ids"])):
            raise ValueError("current presentation cannot grant editorial eligibility")
        fields["editorial_descriptions"].update({identity: row[8] for identity, (_, row) in current.items()})
        fields["current_presentation"] = copy.deepcopy(current_presentation)
    package = batches.package_review_batches(proposals, editorial_descriptions=fields["editorial_descriptions"],
        work_contexts=fields["work_contexts"], protected_ids=fields["protected_ids"], timezone=timezone,
        wording_only_ids=fields.get("wording_only_ids", ()),
        materialized_correction_ids=fields.get("materialized_correction_ids", ()),
        allow_review_overlaps=consolidation_policy in {OVERLAP_CONSOLIDATION_POLICY, CLOCKIFY_OVERLAP_CONSOLIDATION_POLICY,
                                                      STRUCTURED_EDITORIAL_CONSOLIDATION_POLICY, NATURAL_BATCH_CONSOLIDATION_POLICY,
                                                      ALL_DURATION_CONSOLIDATION_POLICY},
        allow_clockify_overlaps=consolidation_policy in {CLOCKIFY_OVERLAP_CONSOLIDATION_POLICY,
                                                       STRUCTURED_EDITORIAL_CONSOLIDATION_POLICY, NATURAL_BATCH_CONSOLIDATION_POLICY,
                                                       ALL_DURATION_CONSOLIDATION_POLICY},
        natural_batches=consolidation_policy in {NATURAL_BATCH_CONSOLIDATION_POLICY, ALL_DURATION_CONSOLIDATION_POLICY},
        all_durations=consolidation_policy == ALL_DURATION_CONSOLIDATION_POLICY,
        group_summaries=group_summaries)
    if group_summaries is not None:
        fields["group_summaries"] = copy.deepcopy(dict(group_summaries))
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
            sheet_title: str, timezone: str, all_durations: bool = False,
            consolidate_materialized_corrections: bool = False,
            group_summaries: Mapping[str, Any] | None = None,
            summary_provider: Callable[[Sequence[Mapping[str, Any]]], Mapping[str, Any]] | None = None,
            native_credit_directory: Path | None = None,
            current_presentation: Mapping[str, Any] | None = None,
            ) -> dict[str, Any]:
    """Create an immutable v2 binding; opt in to v7 for all source durations.

    Existing output always replays its recorded policy and summaries and is
    never edited. Fresh automatic deliveries supply a summary provider; every
    multi-description group must receive reviewed wording before a binding is
    written. Explicit maps and historical callers retain their existing API.
    The additive materialized-correction field records the separate source-bound
    opt-in; its absence preserves every historical policy's replay semantics.
    """
    from scripts import clockify_review_run
    source_dir = source_dir.resolve()
    proposals_handle = artifact_handle(source_dir / "proposals.json")
    proposals = json.loads(adoptions._capture(proposals_handle, {}))
    authority = {name: artifact_handle(source_dir / filename) for name, filename in AUTHORITY_FILES.items()}
    existing = load(output)[0] if output.exists() else None
    allow_materialized = ("materialized_correction_ids" in existing if existing is not None
                          else consolidate_materialized_corrections)
    lifecycle_authority = existing.get("lifecycle_route_authority") if existing is not None else None
    if existing is None and allow_materialized and json.loads(adoptions._capture(authority["routing"], {})).get("client_lifecycle_routes"):
        lifecycle_authority = frozen_lifecycle_route_authority(source_dir)
    policy = existing["consolidation_policy"] if existing is not None else (
        ALL_DURATION_CONSOLIDATION_POLICY if all_durations else NATURAL_BATCH_CONSOLIDATION_POLICY)
    if existing is not None and group_summaries is None:
        group_summaries = existing.get("group_summaries")
    if existing is not None:
        if current_presentation is not None and current_presentation != existing.get("current_presentation"):
            raise ValueError("immutable current presentation binding differs")
        current_presentation = existing.get("current_presentation")
    if current_presentation is not None and _current_presentation_rows(
            current_presentation, proposals, timezone)[:2] != (spreadsheet_id, sheet_title):
        raise ValueError("current presentation destination differs")
    fields = _auto_fields(proposals, authority, source_dir, timezone, consolidation_policy=policy,
                          group_summaries=group_summaries, allow_materialized_corrections=allow_materialized,
                          lifecycle_route_authority=lifecycle_authority,
                          current_presentation=current_presentation,
                          allow_recorded_attendance=existing is None or "recorded_attendance_authority" in existing,
                          recorded_attendance_authority=existing.get("recorded_attendance_authority") if existing else None)
    if existing is None and group_summaries is None and summary_provider is not None:
        from scripts import clockify_review_group_summary
        activities = fields["package"]["activities"]
        required = {item["review_id"] for item in activities
                    if len({child["description"] for child in item["prepared_descriptions"]}) > 1}
        if required or clockify_review_group_summary.needs_summary(activities):
            # The provider receives a disposable copy, never our source or
            # the authority used to derive stable identities and durations.
            # It also screens unsafe singleton wording, while healthy
            # singletons require no model call or cache write.
            group_summaries = summary_provider(copy.deepcopy(activities))
            reviewed = batches.group_summary_descriptions(activities, group_summaries)
            if not required.issubset(reviewed):
                raise ValueError("automatic group summaries are incomplete")
            fields = _auto_fields(proposals, authority, source_dir, timezone, consolidation_policy=policy,
                                  group_summaries=group_summaries, allow_materialized_corrections=allow_materialized,
                                  lifecycle_route_authority=lifecycle_authority,
                                  current_presentation=current_presentation,
                                  allow_recorded_attendance=True)
    if allow_materialized:
        _require_group_summaries(fields)
    document = {"schema_version": AUTO_SCHEMA, "run_id": run_id, "spreadsheet_id": spreadsheet_id,
        "sheet_title": sheet_title, "source_proposals": proposals_handle, "timezone": timezone,
        "authority": authority, "consolidation_policy": policy, **fields}
    if existing is not None and "native_credit_source" in existing:
        document["native_credit_source"] = existing["native_credit_source"]
    elif existing is None and native_credit_directory is not None:
        from scripts import clockify_native_credit_history as history
        document["native_credit_source"] = {"source_proposals": proposals_handle,
            "source_ledger": artifact_handle(source_dir / "evidence/evidence-ledger.json"),
            "output_directory": str(native_credit_directory)}
        history.validate_source(document["native_credit_source"], proposals=proposals)
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


def _require_group_summaries(document: Mapping[str, Any]) -> None:
    activities = document["package"]["activities"]
    required = {item["review_id"] for item in activities
                if len({child["description"] for child in item["prepared_descriptions"]}) > 1}
    if not required.issubset(batches.group_summary_descriptions(activities, document.get("group_summaries") or {})):
        raise ValueError("automatic group summaries are incomplete")


def load(path: Path) -> tuple[dict[str, Any], dict[str, str]]:
    handle = artifact_handle(path)
    document = json.loads(adoptions._capture(handle, {}))
    if (not isinstance(document, dict) or document.get("schema_version") not in {SCHEMA, AUTO_SCHEMA}
            or set(document) - {"group_summaries", "native_credit_source", "materialized_correction_ids", "current_presentation",
                                "recorded_attendance_authority", "lifecycle_route_authority"} != (FIELDS if document["schema_version"] == SCHEMA else
                WORDING_AUTO_FIELDS if document.get("consolidation_policy") in {
                    WORDING_CONSOLIDATION_POLICY, OVERLAP_CONSOLIDATION_POLICY,
                    CLOCKIFY_OVERLAP_CONSOLIDATION_POLICY, STRUCTURED_EDITORIAL_CONSOLIDATION_POLICY,
                    NATURAL_BATCH_CONSOLIDATION_POLICY, ALL_DURATION_CONSOLIDATION_POLICY} else AUTO_FIELDS)):
        raise ValueError("prepared review publication fields differ")
    if "materialized_correction_ids" in document:
        if document["schema_version"] != AUTO_SCHEMA:
            raise ValueError("materialized correction authority requires automatic preparation")
        _require_group_summaries(document)
    if "current_presentation" in document and document["schema_version"] != AUTO_SCHEMA:
        raise ValueError("current presentation requires automatic preparation")
    if "lifecycle_route_authority" in document and (document["schema_version"] != AUTO_SCHEMA
            or "materialized_correction_ids" not in document
            or not isinstance(document["lifecycle_route_authority"], Mapping)):
        raise ValueError("frozen lifecycle route authority requires materialized automatic preparation")
    if "recorded_attendance_authority" in document:
        attendance = document["recorded_attendance_authority"]
        if (document["schema_version"] != AUTO_SCHEMA or not isinstance(attendance, Mapping)
                or set(attendance) != {"sources", "review_ids"} or not isinstance(attendance["review_ids"], list)
                or not attendance["review_ids"] or attendance["review_ids"] != sorted(set(attendance["review_ids"]))):
            raise ValueError("recorded attendance authority differs")
    if "native_credit_source" in document:
        from scripts import clockify_native_credit_history as history
        history.validate_source(document["native_credit_source"])
        if document["native_credit_source"].get("source_proposals") != document["source_proposals"]:
            raise ValueError("native credit original proposal handle differs")
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
    if "group_summaries" in document:
        batches.group_summary_descriptions(document["package"]["activities"], document["group_summaries"])
    if document["schema_version"] == AUTO_SCHEMA:
        if document["consolidation_policy"] not in {CONSOLIDATION_POLICY, WORDING_CONSOLIDATION_POLICY,
                                                   OVERLAP_CONSOLIDATION_POLICY, CLOCKIFY_OVERLAP_CONSOLIDATION_POLICY,
                                                   STRUCTURED_EDITORIAL_CONSOLIDATION_POLICY, NATURAL_BATCH_CONSOLIDATION_POLICY,
                                                   ALL_DURATION_CONSOLIDATION_POLICY}:
            raise ValueError("automatic preparation consolidation policy differs")
        source_handle = document["source_proposals"]
        proposals = json.loads(adoptions._capture(source_handle, {}))
        source_dir = Path(source_handle["path"]).parent
        if "current_presentation" in document and _current_presentation_rows(
                document["current_presentation"], proposals, document["timezone"])[:2] != (
                    document["spreadsheet_id"], document["sheet_title"]):
            raise ValueError("current presentation destination differs")
        derived = _auto_fields(proposals, document["authority"], source_dir, document["timezone"],
                              consolidation_policy=document["consolidation_policy"],
                              allow_materialized_corrections="materialized_correction_ids" in document,
                              lifecycle_route_authority=document.get("lifecycle_route_authority"),
                              current_presentation=document.get("current_presentation"),
                              allow_recorded_attendance="recorded_attendance_authority" in document,
                              recorded_attendance_authority=document.get("recorded_attendance_authority"),
                              group_summaries=document.get("group_summaries"))
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
        materialized_correction_ids=document.get("materialized_correction_ids", ()),
        allow_review_overlaps=document.get("consolidation_policy") in {OVERLAP_CONSOLIDATION_POLICY, CLOCKIFY_OVERLAP_CONSOLIDATION_POLICY,
                                                                    STRUCTURED_EDITORIAL_CONSOLIDATION_POLICY, NATURAL_BATCH_CONSOLIDATION_POLICY,
                                                                    ALL_DURATION_CONSOLIDATION_POLICY},
        allow_clockify_overlaps=document.get("consolidation_policy") in {CLOCKIFY_OVERLAP_CONSOLIDATION_POLICY,
                                                                     STRUCTURED_EDITORIAL_CONSOLIDATION_POLICY, NATURAL_BATCH_CONSOLIDATION_POLICY,
                                                                     ALL_DURATION_CONSOLIDATION_POLICY},
        natural_batches=document.get("consolidation_policy") in {NATURAL_BATCH_CONSOLIDATION_POLICY, ALL_DURATION_CONSOLIDATION_POLICY},
        all_durations=document.get("consolidation_policy") == ALL_DURATION_CONSOLIDATION_POLICY,
        group_summaries=document.get("group_summaries"))
    if expected != document["package"]:
        raise ValueError("prepared review package differs from explicit editorial inputs")
    package = batches.verify_package(document["package"], proposals=frozen,
        work_contexts=document["work_contexts"], protected_ids=document["protected_ids"],
        timezone=document["timezone"], wording_only_ids=document.get("wording_only_ids", ()),
        materialized_correction_ids=document.get("materialized_correction_ids", ()),
        allow_review_overlaps=document.get("consolidation_policy") in {OVERLAP_CONSOLIDATION_POLICY, CLOCKIFY_OVERLAP_CONSOLIDATION_POLICY,
                                                                    STRUCTURED_EDITORIAL_CONSOLIDATION_POLICY, NATURAL_BATCH_CONSOLIDATION_POLICY,
                                                                    ALL_DURATION_CONSOLIDATION_POLICY},
        allow_clockify_overlaps=document.get("consolidation_policy") in {CLOCKIFY_OVERLAP_CONSOLIDATION_POLICY,
                                                                     STRUCTURED_EDITORIAL_CONSOLIDATION_POLICY, NATURAL_BATCH_CONSOLIDATION_POLICY,
                                                                     ALL_DURATION_CONSOLIDATION_POLICY},
        natural_batches=document.get("consolidation_policy") in {NATURAL_BATCH_CONSOLIDATION_POLICY, ALL_DURATION_CONSOLIDATION_POLICY},
        all_durations=document.get("consolidation_policy") == ALL_DURATION_CONSOLIDATION_POLICY,
        group_summaries=document.get("group_summaries"))
    # Exercise native warning validation even though raw wording is not exposed.
    children = {publisher.stable_review_id(item): publisher.proposal_row(item, run_id,
                project_allowlist=project_allowlist) for item in frozen
                if any(publisher.stable_review_id(item) in activity["child_review_ids"]
                       for activity in package["activities"])}
    if "current_presentation" in document:
        _, _, current = _current_presentation_rows(document["current_presentation"], frozen, document["timezone"])
        children.update({identity: row for identity, (_, row) in current.items() if identity in children})
    rows = [publisher.portfolio_row(activity, run_id, source_proposals=frozen)
            for activity in package["activities"]]
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
            **({"group_summaries": copy.deepcopy(document["group_summaries"])} if "group_summaries" in document else {}),
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


def _warning_instant(value: Any) -> dt.datetime | None:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else None
        return parsed.astimezone(dt.timezone.utc) if parsed is not None and parsed.tzinfo is not None else None
    except ValueError:
        return None


def _warning_source_counterparts(clockify: Mapping[str, Any], *, source_run: Path | None,
                                 source_ledger: Path | None) -> dict[str, dict[str, Any]]:
    """Resolve only unchanged, one-to-one native financial witnesses."""
    from scripts import evidence_ledger
    target = {}
    if source_run is not None:
        source_run = Path(source_run)
        expected = source_run / "evidence/evidence-ledger.json"
        if source_ledger is not None and Path(source_ledger) != expected:
            raise ValueError("staging source ledger differs from source run")
        source_ledger = expected
        routing = json.loads(adoptions._capture(artifact_handle(source_run / "routing.json"), {}))
        target = {"userId": routing.get("clockify_user_id"), "workspaceId": routing.get("workspace_id")}
        if any(not isinstance(value, str) or not value for value in target.values()):
            raise ValueError("staging source Clockify target is incomplete")
        if any(any(entry.get(key) != value for key, value in target.items()) for entry in clockify["entries"]):
            raise ValueError("staging Clockify capture target differs from source")
    if source_ledger is None:
        return {}
    document = json.loads(adoptions._capture(artifact_handle(Path(source_ledger)), {}))
    if document.get("schema_version") != evidence_ledger.SCHEMA_VERSION or not isinstance(document.get("events"), list):
        raise ValueError("staging source evidence ledger is invalid")
    manifest = evidence_ledger.LedgerManifest.from_document(document.get("manifest"))
    ledger = evidence_ledger.EvidenceLedger(tuple(evidence_ledger.EvidenceEvent.from_document(e)
        for e in document["events"]), manifest.source_inventory, manifest.timezone, manifest.member_identities)
    ledger.validate(manifest)
    sources = [event.document() for event in ledger.events if event.source_type == "clockify"]

    def matches(event, entry):
        attrs, ref = event["attributes"], event["source_ref"]
        native_id = ref.get("source_id")
        native_id = native_id if isinstance(native_id, str) and not re.fullmatch(r"row-\d+", native_id) else None
        suffix = attrs.get("id_suffix")
        # An available native identity must match; never fall back to geometry
        # after a native entry was replaced, even if its other facts are equal.
        if native_id and entry["id"] != native_id:
            return False
        if not native_id and suffix and not str(entry["id"]).endswith(suffix):
            return False
        member = attrs.get("userId") or attrs.get("user_id") or target.get("userId")
        if member and entry.get("userId") != member:
            return False
        workspace = attrs.get("workspaceId") or attrs.get("workspace_id") or target.get("workspaceId")
        if workspace and entry.get("workspaceId") != workspace:
            return False
        if not (native_id or suffix) and not member:
            return False
        project = attrs.get("projectId") or attrs.get("project_id")
        project_suffix = attrs.get("project_id_suffix")
        if not (project or project_suffix) or (project and entry.get("projectId") != project):
            return False
        if project_suffix and not str(entry.get("projectId") or "").endswith(project_suffix):
            return False
        if not isinstance(attrs.get("description"), str) or attrs["description"] != entry.get("description"):
            return False
        span = event["raw_source_span"]
        if not span.get("start") or not span.get("end"):
            span = attrs.get("timeInterval", {})
        interval = entry.get("timeInterval", {})
        starts = [_warning_instant(value.get("start")) for value in (span, interval)]
        ends = [_warning_instant(value.get("end")) for value in (span, interval)]
        if None in starts or None in ends or starts[0] != starts[1] or ends[0] != ends[1] or ends[0] <= starts[0]:
            return False
        if "billable" in attrs and attrs["billable"] != entry.get("billable"):
            return False
        if "tagIds" in attrs and sorted(attrs["tagIds"]) != sorted(entry.get("tagIds", [])):
            return False
        if "tag_id_suffixes" in attrs and sorted(attrs["tag_id_suffixes"]) != sorted(
                str(tag)[-8:] for tag in entry.get("tagIds", [])):
            return False
        return True

    candidates = {event["evidence_id"]: [entry["id"] for entry in clockify["entries"] if matches(event, entry)]
                  for event in sources}
    return {ids[0]: event for event in sources if len(ids := candidates[event["evidence_id"]]) == 1
            and sum(ids[0] in values for values in candidates.values()) == 1}


def _same_warning_fact(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    if any(before.get(key) != after.get(key) for key in ("type", "counterpart_id", "overlap_duration_seconds")):
        return False
    if any(_warning_instant(before.get(key)) is None or _warning_instant(before.get(key)) != _warning_instant(after.get(key))
           for key in ("overlap_start", "overlap_end")):
        return False
    return (not before.get("counterpart_project_suffix")
            or before.get("counterpart_project_suffix") == after.get("counterpart_project_suffix"))


def fresh_warning_view(proposals: Sequence[Mapping[str, Any]], clockify: Mapping[str, Any], *,
                       source_run: Path | None = None, source_ledger: Path | None = None) -> list[dict[str, Any]]:
    """Append new GET-backed overlaps, optionally anchored to a validated source ledger.

    Existing warning dictionaries are never rewritten. Without a source input,
    retain the original fresh-capture evidence identity and exact comparison.
    No warning is financial credit or authority to edit time.
    """
    from scripts import evidence_ledger, clockify_native_sheet_post as native
    if clockify.get("read_only") is not True or not isinstance(clockify.get("entries"), list):
        raise ValueError("staging requires a read-only Clockify capture")
    events = evidence_ledger.normalize_collector_snapshot({"clockify": clockify})
    counterparts = {str(event.source_ref["source_id"]): event.evidence_id for event in events}
    if (len(events) != len(clockify["entries"]) or len(counterparts) != len(events)
            or len({str(entry.get("id") or "") for entry in clockify["entries"]}) != len(events)):
        raise ValueError("staging Clockify evidence identities differ")
    anchored = source_run is not None or source_ledger is not None
    source_counterparts = _warning_source_counterparts(clockify, source_run=source_run, source_ledger=source_ledger)
    result = copy.deepcopy(list(proposals))
    for proposal in result:
        warnings = proposal.setdefault("review_warnings", [])
        for entry in clockify["entries"]:
            overlap = native._overlap(proposal, entry)
            if overlap is not None:
                source = source_counterparts.get(str(entry["id"]))
                warning = {"type": "existing_clockify_overlap", "counterpart_id":
                    source["evidence_id"] if source else counterparts[str(entry["id"])],
                    "overlap_start": overlap["start"], "overlap_end": overlap["end"],
                    "overlap_duration_seconds": overlap["overlap_seconds"]}
                project_suffix = ((source["attributes"].get("project_id_suffix") if source else None)
                                  or str(entry.get("projectId") or "")[-6:])
                if anchored and project_suffix:
                    warning["counterpart_project_suffix"] = project_suffix
                if not (any(_same_warning_fact(old, warning) for old in warnings) if anchored else warning in warnings):
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
        materialized_correction_ids=document.get("materialized_correction_ids", ()),
        allow_review_overlaps=document.get("consolidation_policy") in {OVERLAP_CONSOLIDATION_POLICY, CLOCKIFY_OVERLAP_CONSOLIDATION_POLICY,
                                                                    STRUCTURED_EDITORIAL_CONSOLIDATION_POLICY, NATURAL_BATCH_CONSOLIDATION_POLICY,
                                                                    ALL_DURATION_CONSOLIDATION_POLICY},
        allow_clockify_overlaps=document.get("consolidation_policy") in {CLOCKIFY_OVERLAP_CONSOLIDATION_POLICY,
                                                                     STRUCTURED_EDITORIAL_CONSOLIDATION_POLICY, NATURAL_BATCH_CONSOLIDATION_POLICY,
                                                                     ALL_DURATION_CONSOLIDATION_POLICY},
        natural_batches=document.get("consolidation_policy") in {NATURAL_BATCH_CONSOLIDATION_POLICY, ALL_DURATION_CONSOLIDATION_POLICY},
        all_durations=document.get("consolidation_policy") == ALL_DURATION_CONSOLIDATION_POLICY,
        group_summaries=document.get("group_summaries"))
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
