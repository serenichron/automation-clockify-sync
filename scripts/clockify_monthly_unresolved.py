"""Pure, source-bound projection for the visible 12-column unresolved tab.

These rows are review evidence, never additional Clockify time. The projection
is shared by the writer and receipt coordinator, with no collection/inference.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from scripts import evidence_ledger
except ImportError:  # pragma: no cover
    import evidence_ledger  # type: ignore[no-redef]

PROFILE = "visible-monthly-unresolved/v1"
ALIAS_PROFILE = "visible-monthly-unresolved-source-aliases/v1"
HEADER = [
    "Stable Evidence ID", "Local Date / Slice", "Source / Machine", "Category",
    "Why No Valid Interval", "Project / Client Candidates or Routing State",
    "Concise Evidence Summary", "Fathom / Meeting Linkage",
    "Confidence / Quality State", "Recommended Human Action", "Disposition",
    "Provenance / Artifact Digest",
]


def _read(root: Path, relative: str) -> tuple[Any, str]:
    path = root / relative
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"unsafe or missing monthly projection artifact: {relative}")
    content = path.read_bytes()
    return json.loads(content), "sha256:" + hashlib.sha256(content).hexdigest()


def title_for_review(sheet_title: str) -> str:
    suffix = " portfolio review"
    if not sheet_title.endswith(suffix):
        raise ValueError("monthly unresolved projection requires a monthly portfolio review title")
    return sheet_title[:-len(suffix)] + " unresolved evidence"


def project_rows(source_dir: Path) -> list[list[str]]:
    """Render only actual accounting ambiguities and valid unrouted proposals.

    All cited IDs must resolve in the immutable ledger; duplicated projection
    identities fail rather than dropping distinct accounting records silently.
    """
    root = Path(source_dir)
    accounting, accounting_digest = _read(root, "work-accounting-result.json")
    proposals, proposals_digest = _read(root, "proposals.json")
    if proposals != accounting.get("proposals"):
        raise ValueError("monthly proposal artifact differs from accounting")
    ambiguities = accounting.get("ambiguous")
    if not isinstance(ambiguities, list):
        raise ValueError("monthly accounting ambiguities are invalid")
    candidates = [(row.get("exception_kind"), row) for row in ambiguities]
    candidates.extend(("routing_gap", row) for row in proposals
                      if row.get("routing_disposition") == "unresolved-routing")
    if not candidates:
        return []
    ambiguous_artifact, ambiguous_digest = _read(root, "ambiguous.json")
    if ambiguous_artifact != ambiguities:
        raise ValueError("monthly ambiguity artifact differs from accounting")
    document, ledger_digest = _read(root, "evidence/evidence-ledger.json")
    if document.get("schema_version") != evidence_ledger.SCHEMA_VERSION:
        raise ValueError("monthly evidence ledger schema is invalid")
    manifest = evidence_ledger.LedgerManifest.from_document(document["manifest"])
    ledger = evidence_ledger.EvidenceLedger(
        tuple(evidence_ledger.EvidenceEvent.from_document(row) for row in document["events"]),
        manifest.source_inventory, manifest.timezone, manifest.member_identities,
    )
    ledger.validate(manifest)
    events = {event.evidence_id: event.document() for event in ledger.events}
    analysis, analysis_digest = _read(root, "semantic-analysis.json")
    activities = {row.get("activity_id"): row for row in analysis.get("activities", [])
                  if row.get("activity_id")}
    report, report_digest = _read(root, "run-report.json")
    period = report["date_range"]
    start = dt.datetime.fromisoformat(period["since"].replace("Z", "+00:00"))
    end = dt.datetime.fromisoformat(period["until"].replace("Z", "+00:00"))
    zone = ZoneInfo(manifest.timezone or "Europe/Bucharest")
    # Collector reports local labels without an offset; the sealed manifest
    # carries their timezone. Never substitute the host's ambient timezone.
    if start.tzinfo is None:
        start = start.replace(tzinfo=zone)
    if end.tzinfo is None:
        end = end.replace(tzinfo=zone)
    if end <= start:
        raise ValueError("monthly projection slice interval is invalid")
    slice_label = f"slice {start.astimezone(zone).date()} to {end.astimezone(zone).date()} (end exclusive)"
    digests = {"work-accounting-result.json": accounting_digest, "proposals.json": proposals_digest,
               "ambiguous.json": ambiguous_digest, "evidence/evidence-ledger.json": ledger_digest,
               "semantic-analysis.json": analysis_digest, "run-report.json": report_digest}
    rows = []
    identities = set()
    for kind, item in candidates:
        provenance = item.get("provenance") or {}
        ids = item.get("evidence_ids", provenance.get("evidence_ids"))
        if not isinstance(kind, str) or not kind or not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i for i in ids):
            raise ValueError("monthly evidence candidate lacks kind or evidence IDs")
        ids = sorted(set(ids))
        if any(identity not in events for identity in ids):
            raise ValueError("monthly evidence reference does not resolve in ledger")
        identity = json.dumps({"kind": kind, "evidence_ids": ids}, sort_keys=True, separators=(",", ":"))
        stable_id = "uev-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        if stable_id in identities:
            raise ValueError("duplicate monthly stable evidence ID")
        identities.add(stable_id)
        evidence = [events[i] for i in ids]
        dates = set()
        for event in evidence:
            observed = dt.datetime.fromisoformat(event["observed_at"].replace("Z", "+00:00"))
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=zone)
            label = observed.astimezone(zone).date().isoformat()
            if not start <= observed < end:
                label += " (observed metadata; outside slice, not work date)"
            dates.add(label)
        activity = activities.get(item.get("activity_id") or item.get("id"), {})
        # Do not export participant names, raw titles or session content from
        # private evidence. The bounded review summary describes the accounting
        # state; detailed context stays in the private source-bound packet.
        summary = f"{len(ids)} cited evidence records; {kind} accounting exception. No confirmed additional work interval."
        if kind == "analyzer_review_failure":
            summary = f"{len(ids)} cited evidence records quarantined after structural semantic review; no accepted allocation."
        recommendation = activity.get("project_recommendation", "")
        if not isinstance(recommendation, str):
            recommendation = json.dumps(recommendation, sort_keys=True, ensure_ascii=False)
        links = sorted({event["attributes"].get("share_url", "") for event in evidence
                        if event["source_type"] == "fathom" and str(event["attributes"].get("share_url", "")).startswith("https://fathom.video/share/")})
        reason = {
            "analyzer_review_failure": "Structural semantic review did not account coherently for all cited evidence; quarantined, not evidence of zero work.",
            "insufficient_evidence": "Planning, container or session evidence does not confirm completed human-attention work and an observed interval.",
            "low_confidence": "Cited evidence does not establish duration with sufficient confidence; human confirmation required.",
            "timing_evidence": "Timestamp metadata does not establish sufficient positive observed capacity for a valid allocation.",
        }.get(kind, "Cited evidence does not establish a confirmed positive work interval; accounting exception requires human review.")
        quality = "quality pass; accounting ambiguity; no confirmed interval"
        action = "Confirm activity, project and observed interval; no automatic Clockify posting or additional time claim."
        if kind == "routing_gap":
            summary = "Recorded meeting attendance; no outcome inferred. Routing and prior representation require human review."
            reason = f"Valid interval, routing unresolved; context only: {item.get('start')} to {item.get('end')}. Not additional time; prior recording credits and overlaps require review."
            recommendation = "unresolved-routing; project/client must be confirmed"
            quality = f"quality pass; valid proposal; routing unresolved; confidence {item.get('confidence', '')}"
            action = "Resolve routing and reconcile prior recording credit; unconfirmed tails are not new time. No automatic Clockify posting."
            if provenance.get("source_type") != "recorded_meeting":
                summary = "Evidence-backed work interval; client/project routing unresolved. Prior representation requires human review."
                reason = f"Valid interval, routing unresolved; context only: {item.get('start')} to {item.get('end')}. Not additional time; prior representation and overlaps require review."
                action = "Resolve routing and reconcile prior representation; no additional time claim or automatic Clockify posting."
        lineage = json.dumps({"source_run_id": root.name, "artifacts": digests, "evidence_ids": ids},
                             sort_keys=True, separators=(",", ":"))
        sources = sorted({event["source_type"] + (" / " + str(event["source_ref"]["machine"]) if event["source_ref"].get("machine") else "") for event in evidence})
        rows.append([stable_id, slice_label + "; observed " + ", ".join(sorted(dates)),
                     ", ".join(sources), kind, reason, recommendation, str(summary)[:600],
                     "; ".join(links), quality, action, "needs_review", lineage])
    return sorted(rows, key=lambda row: row[0])


def verify_replay_artifacts(source_dir: Path, replay_dir: Path, replay: dict[str, Any]) -> None:
    """Bind the projection inputs to the exact previously validated replay."""
    provenance, _ = _read(replay_dir, "replay-source.json")
    if provenance.get("source_run_id") != source_dir.name or Path(str(provenance.get("source_run_dir"))).resolve() != source_dir.resolve():
        raise ValueError("monthly replay source binding differs")
    for relative, field in (("evidence/evidence-ledger.json", "ledger_file_sha256"),
                            ("semantic-analysis.json", "semantic_analysis_sha256"),
                            ("work-accounting-result.json", "work_accounting_result_sha256")):
        _, source_digest = _read(source_dir, relative)
        _, replay_digest = _read(replay_dir, relative)
        if source_digest != replay_digest or source_digest.removeprefix("sha256:") != provenance.get(field):
            raise ValueError("monthly frozen source/replay artifact binding differs")
    _, accounting_digest = _read(source_dir, "work-accounting-result.json")
    if replay.get("work_accounting_result", {}).get("file_sha256") != accounting_digest.removeprefix("sha256:"):
        raise ValueError("monthly accounting artifact differs from replay integrity")


def machine_digest(row: list[Any]) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(
        [*row[:10], row[11]], sort_keys=True, separators=(",", ":")
    ).encode()).hexdigest()


def _bound_document(binding: dict[str, Any]) -> tuple[Any, Path]:
    path = Path(binding["path"])
    document, digest = _read(path.parent, path.name)
    if digest != "sha256:" + binding["sha256"]:
        raise ValueError("monthly alias proof artifact digest differs")
    return document, path


def _ledger_events(root: Path) -> tuple[dict[str, Any], str]:
    document, digest = _read(root, "evidence/evidence-ledger.json")
    manifest = evidence_ledger.LedgerManifest.from_document(document["manifest"])
    ledger = evidence_ledger.EvidenceLedger(tuple(
        evidence_ledger.EvidenceEvent.from_document(event) for event in document["events"]
    ), manifest.source_inventory, manifest.timezone, manifest.member_identities)
    ledger.validate(manifest)
    return {event.evidence_id: event.document() for event in ledger.events}, digest


def load_alias_proofs(path: Path, source_dir: Path) -> dict[str, Any]:
    """Validate only explicit pinned legacy references; never discover runs.

    The packet is proof of exact evidence identity, not equivalent generated
    display text. Existing machine cells must match its saved native snapshot.
    """
    packet, packet_digest = _read(path.parent, path.name)
    current_rows = {row[0]: row for row in project_rows(source_dir)}
    current_events, _ = _ledger_events(source_dir)
    verified: dict[str, Any] = {}
    visited: set[Path] = set()

    def visit(document: dict[str, Any], locator: Path) -> list[dict[str, Any]]:
        if locator.resolve() in visited:
            raise ValueError("monthly alias proof recursion repeats a packet")
        visited.add(locator.resolve())
        if document.get("schema_version") != "clockify-unresolved-exact-source-alias-proof/v1":
            raise ValueError("monthly alias proof schema is invalid")
        proofs = document.get("proofs")
        if not isinstance(proofs, list) or document.get("source_alias_count") != len(proofs):
            raise ValueError("monthly alias proof count is invalid")
        if "prior_seven_alias_proof" in document:
            prior, prior_path = _bound_document(document["prior_seven_alias_proof"])
            prior_proofs = visit(prior, prior_path)
            remaining = [proof for proof in proofs if proof not in prior_proofs]
            if len(proofs) != len(prior_proofs) + len(remaining) or len(remaining) != 1:
                raise ValueError("combined monthly alias proof differs from its pinned prior packet")
            prefix = "additional_"
        else:
            remaining, prefix = proofs, ""
        historical, historical_path = _bound_document(document[prefix + "historical_source"])
        native, _ = _bound_document(document[prefix + "native_saved_read"])
        canonical, _ = _bound_document(document[prefix + "current_canonical_packet"])
        canonical_dir = Path(canonical["source"])
        canonical_rows = project_rows(canonical_dir)
        if canonical.get("rows") != canonical_rows:
            raise ValueError("monthly alias canonical packet differs from frozen source")
        records = {row["stable_evidence_id"]: row for row in canonical["records"]}
        canonical_events, _ = _ledger_events(canonical_dir)
        historical_events, historical_ledger_digest = _ledger_events(historical_path.parent)
        snapshot = native.get("readback", native.get("rows"))
        if snapshot.get("spreadsheetId") != canonical.get("spreadsheet_id") or not canonical.get("sheet_title") or type(canonical.get("sheet_id")) is not int:
            raise ValueError("monthly alias proof destination differs")
        native_rows: dict[int, list[Any]] = {}
        for sheet in snapshot["sheets"]:
            if sheet.get("properties", {}).get("sheetId") != canonical["sheet_id"]:
                continue
            for grid in sheet.get("data", []):
                for offset, row in enumerate(grid.get("rowData", [])):
                    cells = [cell.get("effectiveValue", {}).get("stringValue", cell.get("formattedValue", ""))
                             for cell in row.get("values", [])]
                    native_rows[grid.get("startRow", 0) + offset + 1] = cells + [""] * (12 - len(cells))
        for proof in remaining:
            identity, kind, ids = proof["stable_evidence_id"], proof["kind"], proof["evidence_ids"]
            if not isinstance(ids, list) or not ids or ids != sorted(set(ids)):
                raise ValueError("monthly alias proof citations are invalid")
            derived = "uev-" + hashlib.sha256(json.dumps({"kind": kind, "evidence_ids": ids}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]
            old = [row for row in historical if row.get("exception_kind") == kind and sorted(set(row.get("evidence_ids", []))) == ids]
            record = records.get(identity, {})
            cells = native_rows.get(proof["native_row_1based"], [])
            provenance_digest = "sha256:" + document[prefix + "historical_source"]["sha256"]
            if (derived != identity or identity in verified or len(old) != 1
                or old[0].get("id") != proof["historical_exception_id"]
                or record.get("source_item_id") != proof["current_exception_id"]
                or record.get("kind") != kind or record.get("evidence_ids") != ids
                or len(cells) != 12 or cells[0] != identity or cells[3] != kind
                or cells[11] != provenance_digest or proof["native_provenance_digest"] != provenance_digest):
                raise ValueError("monthly alias proof identity or historical provenance differs")
            if any(i not in historical_events or historical_events[i] != canonical_events.get(i) for i in ids):
                raise ValueError("monthly alias historical ledger content differs")
            verified[identity] = {"machine_row": cells, "kind": kind, "evidence_ids": ids,
                "events": {i: historical_events[i] for i in ids},
                "historical_ledger_digest": historical_ledger_digest,
                "spreadsheet_id": canonical.get("spreadsheet_id"), "sheet_title": canonical.get("sheet_title"),
                "sheet_id": canonical.get("sheet_id")}
        return proofs

    proofs = visit(packet, path)
    if len(verified) != len(proofs) or {proof["stable_evidence_id"] for proof in proofs} != set(verified):
        raise ValueError("combined monthly alias proof identities differ")
    applicable = {}
    for identity, proof in verified.items():
        if identity not in current_rows:
            continue
        row = current_rows[identity]
        lineage = json.loads(row[11])
        if row[3] != proof["kind"] or lineage["evidence_ids"] != proof["evidence_ids"]:
            raise ValueError("monthly alias current evidence identity differs")
        if any(current_events.get(i) != proof["events"][i] for i in proof["evidence_ids"]):
            raise ValueError("monthly alias current ledger citations differ")
        if machine_digest(row) != machine_digest(proof["machine_row"]):
            applicable[identity] = {**proof, "proof_digest": packet_digest}
    return applicable


def alias_metadata(rows: list[list[Any]], aliases: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"stable_evidence_id": row[0], "proof_digest": aliases[row[0]]["proof_digest"],
             "preserved_machine_digest": machine_digest(aliases[row[0]]["machine_row"]),
             "historical_ledger_digest": aliases[row[0]]["historical_ledger_digest"]}
            for row in rows if row[0] in aliases]


def canonical_source_alias(source_dir: Path, requested: list[Any], existing: list[Any]) -> dict[str, Any]:
    """Recognize only a rederivable canonical row from one pinned sibling run."""
    provenance = json.loads(existing[11])
    if not isinstance(provenance, dict) or set(provenance) != {"source_run_id", "artifacts", "evidence_ids"}:
        raise ValueError("existing row is not canonical monthly provenance")
    name = provenance["source_run_id"]
    if not isinstance(name, str) or not name or name in {".", ".."} or Path(name).name != name:
        raise ValueError("canonical alias source is not a direct sibling run")
    historical = source_dir.parent / name
    if historical.is_symlink() or not historical.is_dir() or historical.resolve().parent != source_dir.parent.resolve():
        raise ValueError("canonical alias historical source is unsafe")
    for relative, expected in provenance["artifacts"].items():
        _, digest = _read(historical, relative)
        if digest != expected:
            raise ValueError("canonical alias historical artifact digest differs")
    historical_rows = {row[0]: row for row in project_rows(historical)}
    old = historical_rows.get(requested[0])
    current_provenance = json.loads(requested[11])
    if (old is None or old[3] != requested[3] or provenance["evidence_ids"] != current_provenance["evidence_ids"]
        or old[11] != existing[11] or machine_digest(old) != machine_digest(existing)):
        raise ValueError("canonical alias identity, citations or machine cells differ")
    old_events, ledger_digest = _ledger_events(historical)
    current_events, _ = _ledger_events(source_dir)
    if any(old_events.get(i) != current_events.get(i) or i not in old_events for i in provenance["evidence_ids"]):
        raise ValueError("canonical alias cited ledger content differs")
    return {"stable_evidence_id": requested[0], "source_run_id": name,
            "historical_provenance": existing[11], "preserved_machine_digest": machine_digest(existing),
            "historical_ledger_digest": ledger_digest}


def validate_canonical_aliases(source_dir: Path, rows: list[list[Any]], aliases: Any) -> list[dict[str, Any]]:
    """Independently reconstruct publisher-reported aliases, without Sheets."""
    if not isinstance(aliases, list) or not aliases:
        raise ValueError("canonical source aliases must be a nonempty list")
    requested = {row[0]: row for row in rows}
    seen = set()
    validated = []
    for alias in aliases:
        identity = alias["stable_evidence_id"]
        if identity in seen or identity not in requested:
            raise ValueError("canonical source alias identity is unknown or duplicate")
        seen.add(identity)
        provenance = json.loads(alias["historical_provenance"])
        name = provenance.get("source_run_id")
        # Validate the name before resolving or reading any producer locator.
        if not isinstance(name, str) or not name or name in {".", ".."} or Path(name).name != name:
            raise ValueError("canonical alias source is not a direct sibling run")
        historical = source_dir.parent / name
        if historical.is_symlink() or not historical.is_dir() or historical.resolve().parent != source_dir.parent.resolve():
            raise ValueError("canonical alias historical source is unsafe")
        old_rows = project_rows(historical)
        old = next((row for row in old_rows if row[0] == identity), None)
        if old is None:
            raise ValueError("canonical alias historical identity is absent")
        proof = canonical_source_alias(source_dir, requested[identity], old)
        if proof != alias:
            raise ValueError("canonical source alias metadata differs from its frozen source")
        validated.append(proof)
    return validated
