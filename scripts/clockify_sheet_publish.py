#!/usr/bin/env python3
"""Publish a verified Clockify proposal interval to a guarded review Sheet.

The command is deliberately separate from collection and analysis. It requires
an explicit write flag plus passing quality and immutable-replay artifacts. It
never calls Clockify and preserves human-owned decision fields on existing rows.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import subprocess
from typing import Any, Mapping, Protocol, Sequence

try:
    from scripts import clockify_portfolio_replay as portfolio_replay
except ImportError:  # pragma: no cover - direct script execution fallback
    import clockify_portfolio_replay as portfolio_replay  # type: ignore[no-redef]


HEADER = [
    "Review ID", "Start", "End", "Duration (min)", "Project", "Tags",
    "Source", "Confidence", "Description", "Disposition", "Revision",
    "Last Seen Run", "Reason", "Review Status", "Review Notes",
]
HUMAN_COLUMNS = {9, 13, 14}  # Disposition, Review Status, Review Notes.
CAPACITY_WARNING_FIELDS = frozenset({
    "type", "requested_minutes", "observed_capacity_minutes", "proposed_minutes",
})
CAPACITY_RECOVERY_WARNING_FIELDS = frozenset({
    "type", "requested_minutes", "allocator_allocated_minutes",
    "recovered_minutes", "residual_minutes",
})
OVERLAP_WARNING_FIELDS = frozenset({
    "type", "counterpart_id", "overlap_start",
    "overlap_end", "overlap_duration_seconds",
})
OVERLAP_WARNING_OPTIONAL_FIELDS = frozenset({"counterpart_project_suffix"})
OVERLAP_WARNING_TYPES = frozenset({
    "existing_clockify_overlap", "meeting_proposal_overlap",
    "review_proposal_overlap",
})
MAX_WARNING_TEXT_LENGTH = 256


class PublicationError(RuntimeError):
    """The Sheet publication cannot proceed safely."""


class SheetsGateway(Protocol):
    def spreadsheet(self, spreadsheet_id: str) -> Mapping[str, Any]: ...
    def values(self, spreadsheet_id: str, range_name: str) -> list[list[Any]]: ...
    def duplicate_sheet(
        self, spreadsheet_id: str, source_sheet_id: int, title: str
    ) -> int: ...
    def prepare_sheet(self, spreadsheet_id: str, sheet_id: int) -> None: ...
    def prepare_new_rows(self, spreadsheet_id: str, sheet_id: int, start_row: int, end_row: int) -> None: ...
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

    def prepare_new_rows(self, spreadsheet_id: str, sheet_id: int, start_row: int, end_row: int) -> None:
        if start_row < 2 or end_row < start_row:
            raise PublicationError("new row bounds are invalid")
        self._call([
            "spreadsheets", "batchUpdate", "--params", json.dumps({"spreadsheetId": spreadsheet_id}), "--json",
            json.dumps({"requests": [{"repeatCell": {"range": {
                "sheetId": sheet_id, "startRowIndex": start_row - 1, "endRowIndex": end_row,
                "startColumnIndex": 0, "endColumnIndex": len(HEADER),
            }, "cell": {"userEnteredFormat": {"backgroundColor": {"red": 1, "green": 1, "blue": 1}}},
            "fields": "userEnteredFormat.backgroundColor"}}, {"setDataValidation": {"range": {
                "sheetId": sheet_id, "startRowIndex": start_row - 1, "endRowIndex": end_row,
                "startColumnIndex": 13, "endColumnIndex": 14,
            }, "rule": {"condition": {"type": "ONE_OF_LIST", "values": [
                {"userEnteredValue": value} for value in ("pending", "ambiguous", "approved", "posted", "rejected", "superseded", "unposted")
            ]}, "showCustomUi": True, "strict": True}}}]}),
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


def _warning_text(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or (not allow_empty and not value)
        or len(value) > MAX_WARNING_TEXT_LENGTH
        or not value.isprintable()
    ):
        raise PublicationError(f"proposal review warning {field} is unsafe")
    return value


def _warning_count(value: Any, field: str, *, positive: bool = False) -> int:
    if type(value) is not int or value < (1 if positive else 0):
        qualifier = "positive" if positive else "nonnegative"
        raise PublicationError(
            f"proposal review warning {field} must be a {qualifier} integer"
        )
    return value


def _warning_timestamp(value: Any, field: str) -> dt.datetime:
    text = _warning_text(value, field)
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PublicationError(
            f"proposal review warning {field} must be an ISO timestamp"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PublicationError(
            f"proposal review warning {field} must include a timezone"
        )
    if parsed.microsecond != 0:
        raise PublicationError(
            f"proposal review warning {field} must be whole-second"
        )
    return parsed


def project_allowlist(routing: Mapping[str, Any]) -> dict[str, str]:
    if not isinstance(routing, Mapping):
        raise PublicationError("routing snapshot must be a JSON object")
    result: dict[str, str] = {}

    def add(route: Mapping[str, Any]) -> None:
        suffix = route.get("project_suffix")
        if suffix in (None, ""):
            return
        if not isinstance(suffix, str) or not re.fullmatch(r"[a-f0-9]{6}", suffix):
            raise PublicationError("routing snapshot project suffix is invalid")
        name = _warning_text(route.get("project_name"), "routing project_name")
        prior = result.get(suffix)
        if prior is not None and prior != name:
            raise PublicationError("routing snapshot project suffix collision")
        result[suffix] = name

    for section in ("session_routes", "meeting_routes", "evidence_routes"):
        routes = routing.get(section, [])
        if not isinstance(routes, list) or any(
            not isinstance(route, Mapping) for route in routes
        ):
            raise PublicationError(f"routing snapshot {section} must be a list of objects")
        for route in routes:
            add(route)
    lifecycle_routes = routing.get("client_lifecycle_routes", [])
    if not isinstance(lifecycle_routes, list) or any(
        not isinstance(rule, Mapping) for rule in lifecycle_routes
    ):
        raise PublicationError(
            "routing snapshot client_lifecycle_routes must be a list of objects"
        )
    for rule in lifecycle_routes:
        activation = rule.get("activation")
        if activation is None:
            continue
        if not isinstance(activation, Mapping):
            raise PublicationError("routing snapshot lifecycle activation is invalid")
        route = activation.get("route")
        if route is None:
            continue
        if not isinstance(route, Mapping):
            raise PublicationError("routing snapshot lifecycle route is invalid")
        add(route)
    return result


def _validate_review_warning(
    warning: Mapping[str, Any], projects: Mapping[str, str],
) -> dict[str, Any]:
    warning_type = _warning_text(warning.get("type"), "type")
    if warning_type == "observed_capacity_cap":
        expected = CAPACITY_WARNING_FIELDS
        extra = set(warning) - expected
        missing = expected - set(warning)
        if extra or missing:
            detail = "unsupported fields" if extra else "missing fields"
            raise PublicationError(f"proposal review warning has {detail}")
        requested = _warning_count(
            warning.get("requested_minutes"), "requested_minutes", positive=True
        )
        observed = _warning_count(
            warning.get("observed_capacity_minutes"),
            "observed_capacity_minutes", positive=True,
        )
        proposed = _warning_count(
            warning.get("proposed_minutes"), "proposed_minutes", positive=True
        )
        if requested <= observed or proposed != observed:
            raise PublicationError(
                "proposal review capacity warning does not describe an actual cap"
            )
        return dict(warning)
    elif warning_type == "allocation_capacity_recovery":
        extra = set(warning) - CAPACITY_RECOVERY_WARNING_FIELDS
        missing = CAPACITY_RECOVERY_WARNING_FIELDS - set(warning)
        if extra or missing:
            detail = "unsupported fields" if extra else "missing fields"
            raise PublicationError(f"proposal review warning has {detail}")
        requested = _warning_count(
            warning.get("requested_minutes"), "requested_minutes", positive=True
        )
        allocated = _warning_count(
            warning.get("allocator_allocated_minutes"),
            "allocator_allocated_minutes",
        )
        recovered = _warning_count(
            warning.get("recovered_minutes"), "recovered_minutes", positive=True
        )
        residual = _warning_count(
            warning.get("residual_minutes"), "residual_minutes"
        )
        if (
            allocated + recovered > requested
            or residual != requested - allocated - recovered
        ):
            raise PublicationError(
                "proposal review capacity recovery warning is inconsistent"
            )
        return dict(warning)
    elif warning_type in OVERLAP_WARNING_TYPES:
        extra = set(warning) - OVERLAP_WARNING_FIELDS - OVERLAP_WARNING_OPTIONAL_FIELDS
        missing = OVERLAP_WARNING_FIELDS - set(warning)
        if extra or missing:
            detail = "unsupported fields" if extra else "missing fields"
            raise PublicationError(f"proposal review warning has {detail}")
        counterpart_id = _warning_text(warning.get("counterpart_id"), "counterpart_id")
        pattern = (
            r"ev-[a-f0-9]{64}"
            if warning_type == "existing_clockify_overlap"
            else r"wks-[a-f0-9]{24}"
        )
        if not re.fullmatch(pattern, counterpart_id):
            raise PublicationError("proposal review warning counterpart_id is invalid")
        suffix = warning.get("counterpart_project_suffix")
        if suffix is not None and (
            not isinstance(suffix, str) or not re.fullmatch(r"[a-f0-9]{6}", suffix)
        ):
            raise PublicationError(
                "proposal review warning counterpart_project_suffix is invalid"
            )
        start = _warning_timestamp(warning.get("overlap_start"), "overlap_start")
        end = _warning_timestamp(warning.get("overlap_end"), "overlap_end")
        if end <= start:
            raise PublicationError("proposal review warning overlap must be positive")
        duration = _warning_count(
            warning.get("overlap_duration_seconds"), "overlap_duration_seconds",
            positive=True,
        )
        elapsed = (end - start).total_seconds()
        if not elapsed.is_integer() or duration != int(elapsed):
            raise PublicationError(
                "proposal review warning overlap duration does not match timestamps"
            )
        sanitized = {
            key: value for key, value in warning.items()
            if key != "counterpart_project_suffix"
        }
        if suffix in projects:
            sanitized["counterpart_project"] = projects[suffix]
        return sanitized
    else:
        raise PublicationError(f"unsupported proposal review warning type: {warning_type}")


def proposal_row(
    proposal: Mapping[str, Any], run_id: str, *,
    project_allowlist: Mapping[str, str] | None = None,
) -> list[Any]:
    tags = proposal.get("tag_names", [])
    if isinstance(tags, str):
        tag_text = tags
    elif isinstance(tags, list):
        tag_text = ", ".join(str(value) for value in tags)
    else:
        raise PublicationError("proposal tags must be text or a list")
    warnings = proposal.get("review_warnings", [])
    if not isinstance(warnings, list) or any(
        not isinstance(value, Mapping) for value in warnings
    ):
        raise PublicationError("proposal review warnings must be a list of objects")
    sanitized_warnings = [
        _validate_review_warning(warning, project_allowlist or {})
        for warning in warnings
    ]
    warning_text = (
        json.dumps(sanitized_warnings, ensure_ascii=False, sort_keys=True)
        if sanitized_warnings else ""
    )
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
        warning_text,
        "unposted",
        "",
    ]


def validate_recovery_proposal_groups(
    proposals: Sequence[Mapping[str, Any]],
) -> None:
    groups: dict[str, list[tuple[int, list[Mapping[str, Any]]]]] = {}
    allocated_by_activity: dict[str, int] = {}
    for proposal in proposals:
        warnings = proposal.get("review_warnings", [])
        if not isinstance(warnings, list) or any(
            not isinstance(warning, Mapping) for warning in warnings
        ):
            raise PublicationError("proposal review warnings must be a list of objects")
        recovery_warnings = [
            warning for warning in warnings
            if warning.get("type") == "allocation_capacity_recovery"
        ]
        provenance = proposal.get("provenance")
        marker: Any = None
        if isinstance(provenance, Mapping):
            marker = provenance.get("allocation_capacity_recovery")
            if (
                "allocation_capacity_recovery" in provenance
                and type(marker) is not bool
            ):
                raise PublicationError("proposal recovery provenance is malformed")
        if recovery_warnings and marker is not True:
            raise PublicationError("recovery warning appears on a non-recovery proposal")
        if marker is not True:
            activity_id = proposal.get("activity_id")
            duration = proposal.get("duration_minutes")
            if (
                isinstance(activity_id, str)
                and activity_id.strip()
                and type(duration) is int
                and duration > 0
            ):
                allocated_by_activity[activity_id] = (
                    allocated_by_activity.get(activity_id, 0) + duration
                )
            continue
        activity_id = proposal.get("activity_id")
        if not isinstance(activity_id, str) or not activity_id.strip():
            raise PublicationError("recovery proposal activity_id is missing")
        duration = proposal.get("duration_minutes")
        if type(duration) is not int or duration <= 0:
            raise PublicationError("recovery proposal duration_minutes must be positive")
        groups.setdefault(activity_id, []).append((duration, recovery_warnings))

    for activity_id, members in groups.items():
        aggregate_warnings = [
            warning for _duration, warnings in members for warning in warnings
        ]
        if len(aggregate_warnings) != 1:
            raise PublicationError(
                f"recovery activity group must have exactly one aggregate warning: {activity_id}"
            )
        warning = _validate_review_warning(aggregate_warnings[0], {})
        recovered = warning["recovered_minutes"]
        if recovered != sum(duration for duration, _warnings in members):
            raise PublicationError(
                f"recovery activity group duration does not match warning: {activity_id}"
            )
        if warning["allocator_allocated_minutes"] != allocated_by_activity.get(
            activity_id, 0
        ):
            raise PublicationError(
                f"recovery activity group allocator allocation does not match proposals: {activity_id}"
            )
        if (
            warning["allocator_allocated_minutes"]
            + recovered
            + warning["residual_minutes"]
            != warning["requested_minutes"]
        ):
            raise PublicationError(
                f"recovery activity group accounting is inconsistent: {activity_id}"
            )


def portfolio_row(activity: Mapping[str, Any], run_id: str) -> list[Any]:
    review_id = str(activity.get("review_id") or "").strip()
    if not re.fullmatch(r"pvi-[a-f0-9]{24}", review_id):
        raise PublicationError("portfolio activity lacks a stable review ID")
    tags = activity.get("tag_names")
    sources = activity.get("source_activity_ids")
    if (
        not isinstance(tags, list)
        or not tags
        or any(not isinstance(value, str) or not value.strip() for value in tags)
    ):
        raise PublicationError("portfolio activity tags are invalid")
    if (
        not isinstance(sources, list)
        or not sources
        or any(not isinstance(value, str) or not value.strip() for value in sources)
    ):
        raise PublicationError("portfolio activity sources are invalid")
    duration = int(activity.get("duration_minutes") or 0)
    if duration <= 0:
        raise PublicationError("portfolio activity duration is invalid")
    return [
        review_id,
        _timestamp(activity.get("start")),
        _timestamp(activity.get("end")),
        duration,
        str(activity.get("client_project") or ""),
        ", ".join(tags),
        ", ".join(sources),
        str(activity.get("confidence") or ""),
        str(activity.get("description") or ""),
        "pending",
        1,
        run_id,
        str(activity.get("validation_status") or ""),
        "unposted",
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
    count = summary.get("total_proposals") if isinstance(summary, Mapping) else None
    if type(count) is not int or count != len(proposals):
        raise PublicationError("quality report proposal count does not match input")
    if replay.get("status") != "pass" or replay.get("failures"):
        raise PublicationError("immutable replay has not passed cleanly")
    if str(replay.get("source_run_id") or "") != run_id:
        raise PublicationError("immutable replay does not belong to this source run")


def verify_portfolio_gates(
    portfolio: Mapping[str, Any],
    quality: Mapping[str, Any],
    replay: Mapping[str, Any],
    run_id: str,
) -> None:
    activities = portfolio.get("activities")
    repair = portfolio.get("repair")
    if not isinstance(activities, list) or not all(
        isinstance(row, Mapping) for row in activities
    ):
        raise PublicationError("portfolio repair activities are invalid")
    if (
        not isinstance(repair, Mapping)
        or repair.get("status") not in {"complete", "pass"}
        or repair.get("unresolved_wording") != []
    ):
        raise PublicationError("portfolio repair has not completed cleanly")
    if any(row.get("validation_status") != "flash_validated" for row in activities):
        raise PublicationError(
            "portfolio activity lacks successful Flash portfolio validation"
        )
    source_run = Path(str(portfolio.get("source_run") or "")).name
    if source_run != run_id:
        raise PublicationError("portfolio repair does not belong to this source run")
    if quality.get("status") != "pass":
        raise PublicationError("portfolio quality report has not passed")
    fragmentation = quality.get("fragmentation")
    total_minutes = sum(int(row.get("duration_minutes") or 0) for row in activities)
    try:
        quality_rows = int(fragmentation["row_count"])
        quality_minutes = int(fragmentation["total_minutes"])
    except (KeyError, TypeError, ValueError):
        quality_rows = quality_minutes = -1
    if (
        not isinstance(fragmentation, Mapping)
        or quality_rows != len(activities)
        or quality_minutes != total_minutes
    ):
        raise PublicationError("portfolio quality totals do not match the repair")
    identity = replay.get("identity")
    artifacts = identity.get("artifacts") if isinstance(identity, Mapping) else None
    if replay.get("status") != "pass" or not isinstance(artifacts, Mapping):
        raise PublicationError("portfolio immutable replay has not passed cleanly")
    if artifacts.get("repair") != portfolio_replay._digest(portfolio):
        raise PublicationError("portfolio replay is not bound to the repair")
    if artifacts.get("quality") != portfolio_replay._digest(quality):
        raise PublicationError("portfolio replay is not bound to the quality report")


def _sheet_map(metadata: Mapping[str, Any]) -> dict[str, int]:
    result: dict[str, int] = {}
    for sheet in metadata.get("sheets", []):
        properties = sheet.get("properties", {}) if isinstance(sheet, Mapping) else {}
        title = str(properties.get("title") or "")
        if title:
            result[title] = int(properties["sheetId"])
    return result


def _sheet_row_count(metadata: Mapping[str, Any], title: str) -> int:
    """Return the grid bound when Sheets metadata provides one.

    Older test doubles and compatible gateways may omit grid metadata, in which
    case the historical 1,000-row bound remains the safe default.
    """
    for sheet in metadata.get("sheets", []):
        properties = sheet.get("properties", {}) if isinstance(sheet, Mapping) else {}
        if str(properties.get("title") or "") != title:
            continue
        grid = properties.get("gridProperties")
        if isinstance(grid, Mapping):
            try:
                row_count = int(grid.get("rowCount") or 0)
            except (TypeError, ValueError):
                row_count = 0
            if row_count > 0:
                return row_count
        return 1000
    return 1000


def _same_cell(left: Any, right: Any) -> bool:
    """Compare API-formatted cells with equivalent raw scalar inputs."""
    if left in (None, "") and right in (None, ""):
        return True
    return str(left) == str(right)


def _scan_rows(
    gateway: SheetsGateway,
    spreadsheet_id: str,
    quoted_title: str,
    row_count: int,
) -> tuple[dict[str, int], dict[int, list[Any]]]:
    """Read the complete configured grid in bounded value ranges."""
    positions: dict[str, int] = {}
    existing: dict[int, list[Any]] = {}
    for start in range(1, row_count + 1, 1000):
        end = min(start + 999, row_count)
        chunk = gateway.values(spreadsheet_id, f"{quoted_title}!A{start}:O{end}")
        if start == 1:
            if not chunk or chunk[0][:len(HEADER)] != HEADER:
                raise PublicationError("existing Sheet header does not match the review contract")
            chunk = chunk[1:]
            first_row = 2
        else:
            first_row = start
        for row_number, row in enumerate(chunk, start=first_row):
            review_id = str(row[0] if row else "").strip()
            if not review_id:
                continue
            if review_id in positions:
                raise PublicationError(f"existing Sheet has duplicate review ID: {review_id}")
            positions[review_id] = row_number
            existing[row_number] = list(row)
    return positions, existing


def _is_approved_or_posted(row: Sequence[Any]) -> bool:
    return any(
        str(row[index] if len(row) > index else "").strip().casefold()
        in {"approved", "posted"}
        for index in (9, 13)
    )


def _verify_readback(
    gateway: SheetsGateway,
    spreadsheet_id: str,
    quoted_title: str,
    row_count: int,
    rows: Sequence[Sequence[Any]],
    *,
    new_ids: frozenset[str] = frozenset(),
) -> None:
    positions, existing = _scan_rows(gateway, spreadsheet_id, quoted_title, row_count)
    machine_columns = [index for index in range(len(HEADER)) if index not in HUMAN_COLUMNS]
    for row in rows:
        review_id = str(row[0])
        row_number = positions.get(review_id)
        if row_number is None:
            raise PublicationError(f"Sheet readback is missing review ID: {review_id}")
        actual = existing[row_number]
        actual.extend([""] * (len(HEADER) - len(actual)))
        if not all(_same_cell(actual[index], row[index]) for index in machine_columns):
            raise PublicationError(f"Sheet readback does not match machine fields: {review_id}")
        if review_id in new_ids and not all(
            _same_cell(actual[index], row[index]) for index in HUMAN_COLUMNS
        ):
            raise PublicationError(f"Sheet readback does not match initial decision fields: {review_id}")


def _prepare_appended_rows(
    gateway: SheetsGateway, spreadsheet_id: str, sheet_id: int, quoted_title: str,
    row_count: int, new_ids: frozenset[str],
) -> None:
    if not new_ids:
        return
    positions, _ = _scan_rows(gateway, spreadsheet_id, quoted_title, row_count)
    rows = sorted(positions[review_id] for review_id in new_ids if review_id in positions)
    if len(rows) != len(new_ids) or rows != list(range(rows[0], rows[-1] + 1)):
        raise PublicationError("new rows cannot be safely prepared after append")
    gateway.prepare_new_rows(spreadsheet_id, sheet_id, rows[0], rows[-1])


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

    metadata = gateway.spreadsheet(spreadsheet_id)
    sheets = _sheet_map(metadata)
    created = sheet_title not in sheets
    if created:
        if template_title not in sheets:
            raise PublicationError(f"template Sheet is missing: {template_title}")
        sheet_id = gateway.duplicate_sheet(
            spreadsheet_id, sheets[template_title], sheet_title
        )
        gateway.prepare_sheet(spreadsheet_id, sheet_id)
        quoted = _a1_title(sheet_title)
        row_count = _sheet_row_count(metadata, template_title)
        gateway.clear_values(spreadsheet_id, f"{quoted}!A2:O{row_count}")
        gateway.update_values(spreadsheet_id, [{
            "range": f"{quoted}!A1:O{len(rows) + 1}",
            "majorDimension": "ROWS",
            "values": [HEADER, *rows],
        }])
        if rows:
            gateway.prepare_new_rows(spreadsheet_id, sheet_id, 2, len(rows) + 1)
        _verify_readback(
            gateway, spreadsheet_id, quoted, max(row_count, len(rows) + 1), rows,
            new_ids=frozenset(ids),
        )
        return {"created": True, "appended": len(rows), "updated": 0, "unchanged": 0}

    gateway.prepare_sheet(spreadsheet_id, sheets[sheet_title])
    quoted = _a1_title(sheet_title)
    row_count = _sheet_row_count(metadata, sheet_title)
    positions, existing = _scan_rows(gateway, spreadsheet_id, quoted, row_count)

    updates: list[Mapping[str, Any]] = []
    appends: list[Sequence[Any]] = []
    unchanged = 0
    for row in rows:
        review_id = str(row[0])
        if review_id not in positions:
            appends.append(row)
            continue
        row_number = positions[review_id]
        prior = list(existing[row_number])
        prior.extend([""] * (len(HEADER) - len(prior)))
        machine_columns = [index for index in range(len(HEADER)) if index not in HUMAN_COLUMNS]
        if all(_same_cell(prior[index], row[index]) for index in machine_columns):
            unchanged += 1
            continue
        if _is_approved_or_posted(prior):
            raise PublicationError(
                f"approved or posted review ID cannot change machine fields: {review_id}"
            )
        updates.extend([
            {"range": f"{quoted}!A{row_number}:I{row_number}", "values": [list(row[:9])]},
            {"range": f"{quoted}!K{row_number}:M{row_number}", "values": [list(row[10:13])]},
        ])
    gateway.update_values(spreadsheet_id, updates)
    gateway.append_values(spreadsheet_id, f"{quoted}!A:O", appends)
    new_ids = frozenset(str(row[0]) for row in appends)
    _prepare_appended_rows(
        gateway, spreadsheet_id, sheets[sheet_title], quoted,
        max(row_count, max(positions.values(), default=1) + len(appends)), new_ids,
    )
    _verify_readback(
        gateway,
        spreadsheet_id,
        quoted,
        max(row_count, max(positions.values(), default=1) + len(appends)),
        rows,
        new_ids=new_ids,
    )
    return {
        "created": False,
        "appended": len(appends),
        "updated": len(updates) // 2,
        "unchanged": unchanged,
    }


def _json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _routing_allowlist(path: Path, replay: Mapping[str, Any]) -> dict[str, str]:
    try:
        content = path.read_bytes()
        routing = json.loads(content)
    except (OSError, json.JSONDecodeError) as exc:
        raise PublicationError("routing snapshot is missing or invalid") from exc
    binding = replay.get("reconciliation_binding")
    expected = binding.get("routing_sha256") if isinstance(binding, Mapping) else None
    actual = "sha256:" + hashlib.sha256(content).hexdigest()
    if not isinstance(expected, str) or expected != actual:
        raise PublicationError("routing snapshot digest does not match replay integrity")
    return project_allowlist(routing)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spreadsheet-id", required=True)
    parser.add_argument("--sheet-title", required=True)
    parser.add_argument("--template-title", default="Proposals")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--proposals", type=Path)
    source.add_argument("--portfolio-repair", type=Path)
    parser.add_argument("--quality-report", type=Path, required=True)
    parser.add_argument("--replay-integrity", type=Path, required=True)
    parser.add_argument("--routing-snapshot", type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--enable-write", action="store_true")
    args = parser.parse_args(argv)

    quality = _json(args.quality_report)
    replay = _json(args.replay_integrity)
    if args.portfolio_repair is not None:
        portfolio = _json(args.portfolio_repair)
        if not isinstance(portfolio, Mapping):
            raise PublicationError("portfolio repair input must be a JSON object")
        verify_portfolio_gates(portfolio, quality, replay, args.run_id)
        rows = [portfolio_row(row, args.run_id) for row in portfolio["activities"]]
    else:
        if args.routing_snapshot is None:
            parser.error("--routing-snapshot is required with --proposals")
        projects = _routing_allowlist(args.routing_snapshot, replay)
        proposals = _json(args.proposals)
        if not isinstance(proposals, list) or not all(
            isinstance(row, dict) for row in proposals
        ):
            raise PublicationError("proposals input must be a JSON array of objects")
        verify_gates(proposals, quality, replay, args.run_id)
        validate_recovery_proposal_groups(proposals)
        rows = [
            proposal_row(proposal, args.run_id, project_allowlist=projects)
            for proposal in proposals
        ]
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
