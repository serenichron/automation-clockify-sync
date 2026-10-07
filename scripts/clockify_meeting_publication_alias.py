"""Prospective recorded-meeting representation proofs from genuine captures.

These optional, explicitly selected aliases preserve existing review rows.
They prove neither historical publication authority nor posted Clockify credit.
No directory discovery, collection, inference, or producer identity changes.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts import clockify_source_adoptions as adoptions
from scripts import clockify_native_sheet_post as native
from scripts import work_accounting_pipeline as accounting

SCHEMA = "meeting-publication-bindings/v1"
ARTIFACTS = {"proposals", "source_ledger", "routing", "sheet_capture"}


def artifact_handle(path: Path) -> dict[str, str]:
    # _capture enforces original absolute, non-symlink, immutable bytes.
    absolute = Path(path).absolute()
    handle = {"path": str(absolute), "sha256": "sha256:" + hashlib.sha256(absolute.read_bytes()).hexdigest()}
    adoptions._capture(handle, {})
    return handle


def _utc(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("meeting representation timestamp is invalid")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.microsecond:
        raise ValueError("meeting representation timestamp requires exact aware seconds")
    return parsed.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _identity(proposals: list[Mapping[str, Any]], ledger: Mapping[str, Any], review_id: str) -> dict[str, Any]:
    proposal = adoptions._source(proposals, ledger, review_id)
    provenance = proposal.get("provenance") or {}
    canonical = provenance.get("canonical_meeting_id")
    if not isinstance(canonical, str) or not canonical:
        raise ValueError("selected review is not a canonical recorded meeting")
    events = adoptions._source_events(proposal, ledger)
    recordings, errors = accounting._recording_events(events, ledger["manifest"])
    if errors or len(recordings) != 1:
        raise ValueError("meeting representation source is ambiguous")
    entry = recordings[0]
    meeting = entry["meeting"]
    if (meeting.canonical_id != canonical
            or set(entry["source_evidence_ids"]) != set(provenance["evidence_ids"])):
        raise ValueError("meeting representation canonical source differs")
    start, end = _utc(proposal.get("start")), _utc(proposal.get("end"))
    if not (_utc(meeting.start) <= start < end <= _utc(meeting.end)):
        raise ValueError("meeting representation interval exceeds its recording")
    seconds = int((dt.datetime.fromisoformat(end.replace("Z", "+00:00"))
                   - dt.datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds())
    if type(proposal.get("duration_seconds")) is not int or proposal["duration_seconds"] != seconds:
        raise ValueError("meeting representation exact duration differs")
    siblings = [p for p in proposals if (p.get("provenance") or {}).get("canonical_meeting_id") == canonical]
    return {"recording_ids": sorted(meeting.source_ids), "start": start, "end": end,
            "segment": proposal["allocation_segment"], "segment_count": len(siblings)}


def project(
    *, bindings_path: Path, source_dir: Path, proposals: Sequence[Mapping[str, Any]],
    rows: Sequence[Sequence[Any]], spreadsheet_id: str, sheet_title: str,
) -> tuple[list[list[Any]], list[dict[str, Any]]]:
    """Validate selected prior representations, then project exact aliases.

    Current source validation stays with the native quality/replay consumer;
    this additionally binds each matching proposal to its own ledger events.
    Only immutable prior artifacts are configured, so future runs need no new
    current-run pin. The timestamp is trusted connector/operator metadata.
    """
    from scripts import clockify_sheet_publish as publisher

    cache: dict[tuple[str, str], bytes] = {}
    binding_handle = artifact_handle(bindings_path)
    document = json.loads(adoptions._capture(binding_handle, cache))
    if (not isinstance(document, dict) or set(document) != {
            "schema_version", "spreadsheet_id", "sheet_title", "bindings"}
            or document["schema_version"] != SCHEMA
            or document["spreadsheet_id"] != spreadsheet_id or document["sheet_title"] != sheet_title
            or not isinstance(document["bindings"], list) or not document["bindings"]):
        raise ValueError("meeting representation binding destination or schema differs")
    prior_candidates = []
    seen = set()
    for binding in document["bindings"]:
        if (not isinstance(binding, dict) or set(binding) != {
                "prior_run_id", "review_ids", "representation_verified_at", "artifacts"}
                or not isinstance(binding["prior_run_id"], str) or not binding["prior_run_id"].strip()
                or not isinstance(binding["review_ids"], list) or not binding["review_ids"]
                or any(not isinstance(i, str) or not i for i in binding["review_ids"])
                or len(set(binding["review_ids"])) != len(binding["review_ids"])
                or not isinstance(binding["artifacts"], dict) or set(binding["artifacts"]) != ARTIFACTS):
            raise ValueError("meeting representation prior binding is invalid")
        verified_at = _utc(binding["representation_verified_at"])
        artifacts = binding["artifacts"]
        originals = {name: json.loads(adoptions._capture(handle, cache)) for name, handle in artifacts.items()}
        capture = originals["sheet_capture"]
        # Connector exports may retain the result envelope or structuredContent
        # alone. Both describe the same captured grid; neither is a new receipt.
        if isinstance(capture, dict) and "structuredContent" not in capture:
            capture = {"structuredContent": capture}
        actual_sheet, actual_title, captured = native._sheet_rows(capture, pending_only=False)
        if (actual_sheet, actual_title) != (spreadsheet_id, sheet_title):
            raise ValueError("meeting representation capture target differs")
        old_proposals, ledger = originals["proposals"], originals["source_ledger"]
        projects = publisher.project_allowlist(originals["routing"])
        for review_id in binding["review_ids"]:
            if review_id in seen:
                raise ValueError("meeting representation prior review identity is ambiguous")
            seen.add(review_id)
            proposal = adoptions._source(old_proposals, ledger, review_id)
            matches = [row for _, row in captured if row["Review ID"] == review_id]
            if len(matches) != 1:
                raise ValueError("meeting representation capture review identity is missing or duplicated")
            row = [matches[0][name] for name in publisher.HEADER]
            expected = publisher.proposal_row(proposal, binding["prior_run_id"], project_allowlist=projects)
            # M is captured review context, not a reproducible historical warning
            # serialization. J/N/O are human-owned. All are preserved, not inferred.
            identity_columns = [*range(9), 10, 11]
            if (str(row[9]).strip().lower() != "pending" or str(row[13]).strip().lower() != "unposted"
                    or any(not publisher._same_cell(row[i], expected[i]) for i in identity_columns)):
                raise ValueError("meeting representation captured cells or prior run differ")
            prior_candidates.append({"row": row, "identity": _identity(old_proposals, ledger, review_id),
                                     "prior_run_id": binding["prior_run_id"], "artifacts": artifacts,
                                     "representation_verified_at": verified_at})

    current_handle = artifact_handle(Path(source_dir) / "proposals.json")
    source_proposals = json.loads(adoptions._capture(current_handle, cache))
    ledger_handle = artifact_handle(Path(source_dir) / "evidence/evidence-ledger.json")
    current_ledger = json.loads(adoptions._capture(ledger_handle, cache))
    if not isinstance(source_proposals, list) or any(p not in source_proposals for p in proposals):
        raise ValueError("meeting representation current proposals differ from their source")
    by_id = {publisher.stable_review_id(p): p for p in proposals}
    projected, aliases, retained = [], [], set()
    for raw in rows:
        row = list(raw)
        proposal = by_id.get(str(row[0]))
        if proposal is None:
            raise ValueError("meeting representation row is absent from current proposals")
        if not (proposal.get("provenance") or {}).get("canonical_meeting_id"):
            projected.append(row)
            continue
        identity = _identity(source_proposals, current_ledger, str(row[0]))
        matches = [prior for prior in prior_candidates if (
            set(identity["recording_ids"]) & set(prior["identity"]["recording_ids"])
            and all(identity[key] == prior["identity"][key] for key in ("start", "end", "segment", "segment_count"))
        )]
        if len(matches) > 1:
            raise ValueError("meeting representation recording interval is ambiguous")
        if not matches:
            projected.append(row)
            continue
        prior = matches[0]
        old = prior["row"]
        if old[0] in retained:
            raise ValueError("meeting representation aliases are not one-to-one")
        retained.add(old[0])
        projected.append(list(old))
        aliases.append({
            "verification_basis": "current_sheet_capture",
            "current_review_id": row[0], "retained_review_id": old[0],
            "prior_run_id": prior["prior_run_id"],
            "representation_verified_at": prior["representation_verified_at"],
            "recording_ids": sorted(set(identity["recording_ids"]) & set(prior["identity"]["recording_ids"])),
            "start": identity["start"], "end": identity["end"],
            "allocation_segment": identity["segment"], "segment_count": identity["segment_count"],
            "bindings": binding_handle, "artifacts": prior["artifacts"],
            "current_artifacts": {"proposals": current_handle, "source_ledger": ledger_handle},
            "preserved_machine_digest": "sha256:" + hashlib.sha256(json.dumps(
                [old[i] for i in range(len(publisher.HEADER)) if i not in publisher.HUMAN_COLUMNS],
                separators=(",", ":"), sort_keys=True,
            ).encode()).hexdigest(),
        })
    return projected, aliases
