#!/usr/bin/env python3
"""Publish a verified Clockify proposal interval to a guarded review Sheet.

The command is deliberately separate from collection and analysis. It requires
an explicit write flag plus passing quality and immutable-replay artifacts. It
never calls Clockify and preserves human-owned decision fields on existing rows.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import subprocess
from typing import Any, Mapping, Protocol, Sequence


HEADER = [
    "Review ID", "Start", "End", "Duration (min)", "Project", "Tags",
    "Source", "Confidence", "Description", "Disposition", "Revision",
    "Last Seen Run", "Reason", "Review Status", "Review Notes",
]
HUMAN_COLUMNS = {9, 13, 14}  # Disposition, Review Status, Review Notes.


class PublicationError(RuntimeError):
    """The Sheet publication cannot proceed safely."""


class SheetsGateway(Protocol):
    def spreadsheet(self, spreadsheet_id: str) -> Mapping[str, Any]: ...
    def values(self, spreadsheet_id: str, range_name: str) -> list[list[Any]]: ...
    def duplicate_sheet(
        self, spreadsheet_id: str, source_sheet_id: int, title: str
    ) -> int: ...
    def prepare_sheet(self, spreadsheet_id: str, sheet_id: int) -> None: ...
    def clear_values(self, spreadsheet_id: str, range_name: str) -> None: ...
    def update_values(
        self, spreadsheet_id: str, ranges: Sequence[Mapping[str, Any]]
    ) -> None: ...
    def append_values(
        self, spreadsheet_id: str, range_name: str, rows: Sequence[Sequence[Any]]
    ) -> None: ...


def _a1_title(title: str) -> str:
    return "'" + title.replace("'", "''") + "'"


class GwsSheetsGateway:
    """Small adapter around the authenticated Google Workspace CLI."""

    @staticmethod
    def _call(arguments: Sequence[str]) -> dict[str, Any]:
        completed = subprocess.run(
            ["gws", "sheets", *arguments],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode:
            message = completed.stderr.strip() or completed.stdout.strip()
            raise PublicationError(f"Google Sheets request failed: {message[:500]}")
        try:
            value = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise PublicationError("Google Sheets returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise PublicationError("Google Sheets returned an invalid response")
        return value

    def spreadsheet(self, spreadsheet_id: str) -> Mapping[str, Any]:
        return self._call([
            "spreadsheets", "get", "--params",
            json.dumps({"spreadsheetId": spreadsheet_id, "includeGridData": False}),
        ])

    def values(self, spreadsheet_id: str, range_name: str) -> list[list[Any]]:
        response = self._call([
            "spreadsheets", "values", "get", "--params",
            json.dumps({"spreadsheetId": spreadsheet_id, "range": range_name}),
        ])
        rows = response.get("values", [])
        return rows if isinstance(rows, list) else []

    def duplicate_sheet(
        self, spreadsheet_id: str, source_sheet_id: int, title: str
    ) -> int:
        response = self._call([
            "spreadsheets", "batchUpdate", "--params",
            json.dumps({"spreadsheetId": spreadsheet_id}), "--json",
            json.dumps({"requests": [{"duplicateSheet": {
                "sourceSheetId": source_sheet_id,
                "newSheetName": title,
            }}]}),
        ])
        try:
            return int(response["replies"][0]["duplicateSheet"]["properties"]["sheetId"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise PublicationError("Google Sheets did not confirm the duplicated tab") from exc

    def prepare_sheet(self, spreadsheet_id: str, sheet_id: int) -> None:
        self._call([
            "spreadsheets", "batchUpdate", "--params",
            json.dumps({"spreadsheetId": spreadsheet_id}), "--json",
            json.dumps({"requests": [
                {"repeatCell": {
                    "range": {
                        "sheetId": sheet_id,
                        "startRowIndex": 1,
                        "endRowIndex": 1000,
                        "startColumnIndex": 3,
                        "endColumnIndex": 4,
                    },
                    "cell": {"userEnteredFormat": {
                        "numberFormat": {"type": "NUMBER", "pattern": "0"}
                    }},
                    "fields": "userEnteredFormat.numberFormat",
                }},
                {"setBasicFilter": {"filter": {"range": {
                    "sheetId": sheet_id,
                    "startRowIndex": 0,
                    "endRowIndex": 1000,
                    "startColumnIndex": 0,
                    "endColumnIndex": len(HEADER),
                }}}},
            ]}),
        ])

    def clear_values(self, spreadsheet_id: str, range_name: str) -> None:
        self._call([
            "spreadsheets", "values", "clear", "--params",
            json.dumps({"spreadsheetId": spreadsheet_id, "range": range_name}),
            "--json", "{}",
        ])

    def update_values(
        self, spreadsheet_id: str, ranges: Sequence[Mapping[str, Any]]
    ) -> None:
        if not ranges:
            return
        self._call([
            "spreadsheets", "values", "batchUpdate", "--params",
            json.dumps({"spreadsheetId": spreadsheet_id}), "--json",
            json.dumps({
                "valueInputOption": "RAW",
                "includeValuesInResponse": False,
                "data": list(ranges),
            }, ensure_ascii=False),
        ])

    def append_values(
        self, spreadsheet_id: str, range_name: str, rows: Sequence[Sequence[Any]]
    ) -> None:
        if not rows:
            return
        self._call([
            "spreadsheets", "values", "append", "--params",
            json.dumps({
                "spreadsheetId": spreadsheet_id,
                "range": range_name,
                "valueInputOption": "RAW",
                "insertDataOption": "INSERT_ROWS",
            }),
            "--json", json.dumps({"majorDimension": "ROWS", "values": list(rows)}, ensure_ascii=False),
        ])


def _timestamp(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise PublicationError("proposal is missing a timestamp")
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PublicationError(f"invalid proposal timestamp: {text}") from exc
    return parsed.strftime("%Y-%m-%d %H:%M")


def stable_review_id(proposal: Mapping[str, Any]) -> str:
    activity_key = str(proposal.get("review_activity_key") or "").strip()
    try:
        segment = int(proposal.get("allocation_segment") or 0)
    except (TypeError, ValueError) as exc:
        raise PublicationError("proposal has an invalid allocation segment") from exc
    if not activity_key.startswith("wka-") or segment < 1:
        raise PublicationError("proposal lacks a stable activity key and segment")
    return f"{activity_key}-s{segment:02d}"


def proposal_row(proposal: Mapping[str, Any], run_id: str) -> list[Any]:
    tags = proposal.get("tag_names", [])
    if isinstance(tags, str):
        tag_text = tags
    elif isinstance(tags, list):
        tag_text = ", ".join(str(value) for value in tags)
    else:
        raise PublicationError("proposal tags must be text or a list")
    return [
        stable_review_id(proposal),
        _timestamp(proposal.get("start")),
        _timestamp(proposal.get("end")),
        int(proposal.get("duration_minutes") or 0),
        str(proposal.get("client_project") or ""),
        tag_text,
        str(proposal.get("activity_id") or ""),
        str(proposal.get("confidence") or ""),
        str(proposal.get("description") or ""),
        "pending",
        1,
        run_id,
        "",
        "pending",
        "",
    ]


def verify_gates(
    proposals: Sequence[Mapping[str, Any]],
    quality: Mapping[str, Any],
    replay: Mapping[str, Any],
    run_id: str,
) -> None:
    if quality.get("status") != "pass":
        raise PublicationError("quality report has not passed")
    summary = quality.get("summary")
    if not isinstance(summary, Mapping) or int(summary.get("total_proposals") or -1) != len(proposals):
        raise PublicationError("quality report proposal count does not match input")
    if replay.get("status") != "pass" or replay.get("failures"):
        raise PublicationError("immutable replay has not passed cleanly")
    if str(replay.get("source_run_id") or "") != run_id:
        raise PublicationError("immutable replay does not belong to this source run")


def _sheet_map(metadata: Mapping[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for sheet in metadata.get("sheets", []):
        properties = sheet.get("properties", {}) if isinstance(sheet, Mapping) else {}
        title = str(properties.get("title") or "")
        if title:
            result[title] = int(properties["sheetId"])
    return result


def _same_cell(left: Any, right: Any) -> bool:
    """Compare API-formatted cells with equivalent raw scalar inputs."""
    if left in (None, "") and right in (None, ""):
        return True
    return str(left) == str(right)


def publish(
    gateway: SheetsGateway,
    *,
    spreadsheet_id: str,
    sheet_title: str,
    template_title: str,
    rows: Sequence[Sequence[Any]],
) -> dict[str, Any]:
    ids = [str(row[0]) for row in rows]
    if len(ids) != len(set(ids)):
        raise PublicationError("proposal input contains duplicate stable review IDs")

    sheets = _sheet_map(gateway.spreadsheet(spreadsheet_id))
    created = sheet_title not in sheets
    if created:
        if template_title not in sheets:
            raise PublicationError(f"template Sheet is missing: {template_title}")
        sheet_id = gateway.duplicate_sheet(
            spreadsheet_id, sheets[template_title], sheet_title
        )
        gateway.prepare_sheet(spreadsheet_id, sheet_id)
        quoted = _a1_title(sheet_title)
        gateway.clear_values(spreadsheet_id, f"{quoted}!A2:O1000")
        gateway.update_values(spreadsheet_id, [{
            "range": f"{quoted}!A1:O{len(rows) + 1}",
            "majorDimension": "ROWS",
            "values": [HEADER, *rows],
        }])
        return {"created": True, "appended": len(rows), "updated": 0, "unchanged": 0}

    gateway.prepare_sheet(spreadsheet_id, sheets[sheet_title])
    quoted = _a1_title(sheet_title)
    existing = gateway.values(spreadsheet_id, f"{quoted}!A1:O1000")
    if not existing or existing[0][:len(HEADER)] != HEADER:
        raise PublicationError("existing Sheet header does not match the review contract")
    positions: dict[str, int] = {}
    for row_number, row in enumerate(existing[1:], start=2):
        review_id = str(row[0] if row else "").strip()
        if not review_id:
            continue
        if review_id in positions:
            raise PublicationError(f"existing Sheet has duplicate review ID: {review_id}")
        positions[review_id] = row_number

    updates: list[Mapping[str, Any]] = []
    appends: list[Sequence[Any]] = []
    unchanged = 0
    for row in rows:
        review_id = str(row[0])
        if review_id not in positions:
            appends.append(row)
            continue
        row_number = positions[review_id]
        prior = list(existing[row_number - 1])
        prior.extend([""] * (len(HEADER) - len(prior)))
        machine_columns = [index for index in range(len(HEADER)) if index not in HUMAN_COLUMNS]
        if all(_same_cell(prior[index], row[index]) for index in machine_columns):
            unchanged += 1
            continue
        updates.extend([
            {"range": f"{quoted}!A{row_number}:I{row_number}", "values": [list(row[:9])]},
            {"range": f"{quoted}!K{row_number}:M{row_number}", "values": [list(row[10:13])]},
        ])
    gateway.update_values(spreadsheet_id, updates)
    gateway.append_values(spreadsheet_id, f"{quoted}!A:O", appends)
    return {
        "created": False,
        "appended": len(appends),
        "updated": len(updates) // 2,
        "unchanged": unchanged,
    }


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spreadsheet-id", required=True)
    parser.add_argument("--sheet-title", required=True)
    parser.add_argument("--template-title", default="Proposals")
    parser.add_argument("--proposals", type=Path, required=True)
    parser.add_argument("--quality-report", type=Path, required=True)
    parser.add_argument("--replay-integrity", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--enable-write", action="store_true")
    args = parser.parse_args(argv)

    proposals = _json(args.proposals)
    quality = _json(args.quality_report)
    replay = _json(args.replay_integrity)
    if not isinstance(proposals, list) or not all(isinstance(row, dict) for row in proposals):
        raise PublicationError("proposals input must be a JSON array of objects")
    verify_gates(proposals, quality, replay, args.run_id)
    rows = [proposal_row(proposal, args.run_id) for proposal in proposals]
    if not args.enable_write:
        print(json.dumps({
            "status": "dry_run",
            "external_writes": False,
            "sheet_title": args.sheet_title,
            "rows": len(rows),
        }, sort_keys=True))
        return 0
    result = publish(
        GwsSheetsGateway(),
        spreadsheet_id=args.spreadsheet_id,
        sheet_title=args.sheet_title,
        template_title=args.template_title,
        rows=rows,
    )
    print(json.dumps({"status": "published", "external_writes": True, **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
