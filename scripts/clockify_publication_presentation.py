"""Optional immutable display cells; never an accounting or source override."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Sequence
import re

from scripts import clockify_source_adoptions as adoptions
from scripts.clockify_meeting_publication_alias import artifact_handle

SCHEMA = "sheet-publication-presentation/v1"
SOURCE_FILES = frozenset({"proposals.json", "work-accounting-result.json", "quality_report.json",
                          "routing.json", "evidence/evidence-ledger.json"})
ALLOWED = {"primary": frozenset({12}), "monthly": frozenset({1, 4, 5, 6, 8, 9})}


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(json.dumps(value, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


def load(path: Path) -> tuple[dict[str, Any], dict[str, str]]:
    handle = artifact_handle(path)
    document = json.loads(adoptions._capture(handle, {}))
    if (not isinstance(document, dict) or set(document) != {
            "schema_version", "spreadsheet_id", "source_run_id", "source_artifacts", "destinations"}
            or document["schema_version"] != SCHEMA
            or not isinstance(document["source_run_id"], str)
            or not isinstance(document["source_artifacts"], dict)
            or set(document["source_artifacts"]) != SOURCE_FILES
            or not isinstance(document["destinations"], dict) or not document["destinations"]):
        raise ValueError("presentation manifest contract differs")
    return document, handle


def for_source(path: Path, *, source_dir: Path, run_id: str, spreadsheet_id: str, sheet_title: str) -> Path | None:
    document, _handle = load(path)
    if document["spreadsheet_id"] != spreadsheet_id:
        raise ValueError("presentation spreadsheet differs")
    proposals_path = document["source_artifacts"]["proposals.json"]["path"]
    if document["source_run_id"] != run_id:
        return None
    if Path(proposals_path) != source_dir / "proposals.json":
        raise ValueError("presentation matching source path differs")
    if sheet_title not in document["destinations"] and sheet_title.replace(" portfolio review", " unresolved evidence") not in document["destinations"]:
        return None
    return path


def project(*, path: Path, source_dir: Path, run_id: str, spreadsheet_id: str,
            sheet_title: str, rows: Sequence[Sequence[Any]], kind: str) -> tuple[list[list[Any]], dict[str, Any] | None]:
    document, handle = load(path)
    if document["spreadsheet_id"] != spreadsheet_id or document["source_run_id"] != run_id or source_dir.name != run_id:
        raise ValueError("presentation native source or destination differs")
    for filename, artifact in document["source_artifacts"].items():
        if artifact.get("path") != str(source_dir / filename):
            raise ValueError("presentation source artifact path differs")
        adoptions._capture(artifact, {})
    declaration = document["destinations"].get(sheet_title)
    if declaration is None:
        return [list(row) for row in rows], None
    if (kind not in ALLOWED or set(declaration) != {"kind", "native_rows_sha256", "rows"}
            or declaration["kind"] != kind or declaration["native_rows_sha256"] != digest(list(rows))):
        raise ValueError("presentation native row projection differs")
    projected = json.loads(adoptions._capture(declaration["rows"], {}))
    width = 15 if kind == "primary" else 12
    if (not isinstance(projected, list) or len(projected) != len(rows)
            or any(not isinstance(row, list) or len(row) != width for row in projected)
            or any(len(row) != width for row in rows)):
        raise ValueError("presentation row shape differs")
    for native, presentation in zip(rows, projected, strict=True):
        if any(native[i] != presentation[i] for i in range(width) if i not in ALLOWED[kind]):
            raise ValueError("presentation changes native identity, provenance, or human cells")
        if any(not isinstance(presentation[i], str) for i in ALLOWED[kind]):
            raise ValueError("presentation display cells must be strings")
    proof = {"manifest": handle, "source_artifacts": document["source_artifacts"],
             "spreadsheet_id": spreadsheet_id, "sheet_title": sheet_title, "kind": kind,
             "native_rows_sha256": declaration["native_rows_sha256"], "rows": declaration["rows"],
             "presented_rows_sha256": digest(projected)}
    return projected, proof


class OperatorReadbackGateway:
    """Immutable saved operator evidence only; deliberately no write methods."""
    def __init__(self, path: Path):
        self.handle = artifact_handle(path)
        receipt = json.loads(adoptions._capture(self.handle, {}))
        capture = receipt["readback"]
        capture = capture.get("structuredContent", capture)
        if not isinstance(capture, dict) or not isinstance(capture.get("sheets"), list) or not capture.get("spreadsheetId"):
            raise ValueError("operator readback capture differs")
        self.capture = capture
        self.rows = {}
        from scripts.clockify_native_sheet_post import _cell_value
        for sheet in capture["sheets"]:
            title = sheet["properties"]["title"]
            if title in self.rows:
                raise ValueError("operator readback sheet title is ambiguous")
            rows = []
            for block in sheet.get("data", []):
                if block.get("startColumn", 0) != 0 or block.get("startRow", 0) != len(rows):
                    raise ValueError("operator readback requires contiguous full rows")
                rows.extend([_cell_value(cell) for cell in row.get("values", [])] for row in block.get("rowData", []))
            if not rows:
                raise ValueError("operator readback sheet rows are missing")
            self.rows[title] = rows

    def spreadsheet(self, spreadsheet_id: str) -> Mapping[str, Any]:
        if self.capture["spreadsheetId"] != spreadsheet_id:
            raise ValueError("operator readback spreadsheet differs")
        return self.capture

    def values(self, spreadsheet_id: str, range_name: str) -> list[list[Any]]:
        self.spreadsheet(spreadsheet_id)
        match = re.fullmatch(r"'((?:[^']|'')+)'!A(\d+):[OL](\d+)", range_name)
        if match is None:
            raise ValueError("operator readback range differs")
        title, start, end = match.groups()
        return [list(row) for row in self.rows[title.replace("''", "'")][int(start) - 1:int(end)]]


def verify_existing(proof: Mapping[str, Any], receipt: Mapping[str, Any]) -> None:
    from scripts import clockify_sheet_publish as publisher
    if set(proof) != {"verification_basis", "operator_receipt", "rows"} or proof["verification_basis"] != "immutable_operator_readback":
        raise ValueError("existing publication verification basis differs")
    adoptions._capture(proof["operator_receipt"], {})
    rows = proof["rows"]
    if publisher._publication_receipt(spreadsheet_id=receipt["spreadsheet_id"], sheet_title=receipt["sheet_title"], rows=rows) != {
            field: receipt[field] for field in ("spreadsheet_id", "sheet_title", "row_ids", "rows_sha256", "readback_id", "receipt_id")}:
        raise ValueError("existing publication projected row hash differs")
    gateway = OperatorReadbackGateway(Path(proof["operator_receipt"]["path"]))
    gateway.spreadsheet(receipt["spreadsheet_id"])
    observed = gateway.rows[receipt["sheet_title"]]
    monthly = receipt["sheet_title"].endswith(" unresolved evidence")
    if monthly:
        from scripts import clockify_monthly_unresolved as monthly_projection
        if observed[0] not in (monthly_projection.HEADER, monthly_projection.LEGACY_HEADER):
            raise ValueError("existing publication monthly header differs")
    elif observed[0] != publisher.HEADER:
        raise ValueError("existing publication primary header differs")
    width = 12 if monthly else 15
    human = {10} if monthly else publisher.HUMAN_COLUMNS
    by_id = {}
    for row in observed[1:]:
        if not row or not any(row):
            continue
        if row[0] in by_id or len(row) > width:
            raise ValueError("existing publication captured IDs or width differ")
        by_id[row[0]] = row + [""] * (width - len(row))
    if any(row[0] not in by_id or any(not publisher._same_cell(row[i], by_id[row[0]][i])
           for i in range(width) if i not in human) for row in rows):
        raise ValueError("existing publication captured machine cells differ")
    if "pending_selection" in receipt:
        from scripts import clockify_pending_review_selection as pending
        from scripts import clockify_pending_runtime_proof as pending_runtime
        acceptance = receipt["pending_selection"]
        source_artifacts = acceptance["current_source_artifacts"]
        proposals = json.loads(adoptions._capture(source_artifacts["proposals"], {}))
        source_dir = Path(source_artifacts["proposals"]["path"]).parent
        routing = json.loads(adoptions._capture(source_artifacts["routing"], {}))
        selection = pending.verify(bindings_path=Path(acceptance["selection"]["path"]), source_dir=source_dir,
            proposals=proposals, spreadsheet_id=receipt["spreadsheet_id"], sheet_title=receipt["sheet_title"],
            run_id=source_dir.name, project_allowlist=publisher.project_allowlist(routing))
        try:
            pending_runtime.verify_recorded(acceptance, selection["receipt"])
        except ValueError as exc:
            raise ValueError("existing publication pending selection differs") from exc
        presented_rows = None
        if "presentation" in receipt:
            presentation = receipt["presentation"]
            presented_rows, reconstructed = project(path=Path(presentation["manifest"]["path"]),
                source_dir=source_dir, run_id=source_dir.name, spreadsheet_id=receipt["spreadsheet_id"],
                sheet_title=receipt["sheet_title"], rows=selection["rows"], kind="primary")
            if reconstructed != presentation or presented_rows != rows:
                raise ValueError("existing publication pending presentation differs")
        plan = publisher._pending_plan_preserving_review_humans(gateway, spreadsheet_id=receipt["spreadsheet_id"],
            sheet_title=receipt["sheet_title"], selection=selection, presented_rows=presented_rows)
        if plan["updates"]:
            raise ValueError("existing publication supersession is incomplete")
