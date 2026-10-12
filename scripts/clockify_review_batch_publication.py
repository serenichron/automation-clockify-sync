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
FIELDS = {"schema_version", "run_id", "spreadsheet_id", "sheet_title", "source_proposals",
          "editorial_descriptions", "work_contexts", "protected_ids", "timezone", "package"}
STANDALONE_REASONS = {"meeting", "overlap_warning", "source_interval_overlap", "not_micro_entry"}


def load(path: Path) -> tuple[dict[str, Any], dict[str, str]]:
    handle = artifact_handle(path)
    document = json.loads(adoptions._capture(handle, {}))
    if not isinstance(document, dict) or set(document) != FIELDS or document["schema_version"] != SCHEMA:
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
    for retained in package["retained"]:
        if not set(retained["preparation_reasons"]) <= STANDALONE_REASONS:
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
            "receipt": {"binding": handle, "source_proposals": document["source_proposals"]}}


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
