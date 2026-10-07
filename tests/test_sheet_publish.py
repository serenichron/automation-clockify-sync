import contextlib
import hashlib
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import clockify_sheet_publish as publisher
from scripts import clockify_native_sheet_post as native_post


class FakeGateway:
    def __init__(self, rows=None, include_target=False):
        self.rows = rows or []
        self.include_target = include_target
        self.created = []
        self.prepared = []
        self.cleared = []
        self.updated = []
        self.appended = []
        self.new_rows_prepared = []

    def spreadsheet(self, _spreadsheet_id):
        sheets = [{"properties": {"title": "Proposals", "sheetId": 1}}]
        if self.include_target:
            sheets.append({"properties": {"title": "August 2026 review", "sheetId": 2}})
        return {"sheets": sheets}

    def values(self, _spreadsheet_id, _range_name):
        return self.rows

    def duplicate_sheet(self, _spreadsheet_id, source_sheet_id, title):
        self.created.append((source_sheet_id, title))
        return 2

    def prepare_sheet(self, _spreadsheet_id, sheet_id):
        self.prepared.append(sheet_id)

    def clear_values(self, _spreadsheet_id, range_name):
        self.cleared.append(range_name)

    def update_values(self, _spreadsheet_id, ranges):
        self.updated.extend(ranges)

    def append_values(self, _spreadsheet_id, range_name, rows):
        self.appended.append((range_name, list(rows)))

    def prepare_new_rows(self, _spreadsheet_id, sheet_id, start_row, end_row):
        self.new_rows_prepared.append((sheet_id, start_row, end_row))


class StatefulGateway(FakeGateway):
    """In-memory Sheet model used to verify publish outcomes, not API calls."""

    def __init__(self, rows=None, include_target=True, row_count=1000):
        super().__init__(rows=rows, include_target=include_target)
        self.row_count = row_count
        self.rows = [list(row) for row in self.rows]
        self.read_ranges = []

    def spreadsheet(self, _spreadsheet_id):
        sheets = [{"properties": {
            "title": "Proposals", "sheetId": 1,
            "gridProperties": {"rowCount": self.row_count},
        }}]
        if self.include_target:
            sheets.append({"properties": {
                "title": "August 2026 review", "sheetId": 2,
                "gridProperties": {"rowCount": self.row_count},
            }})
        return {"sheets": sheets}

    def values(self, _spreadsheet_id, range_name):
        self.read_ranges.append(range_name)
        start_text = range_name.rsplit("!A", 1)[1].split(":", 1)[0]
        start = int(start_text)
        end = int(range_name.rsplit("O", 1)[1])
        return [list(row) for row in self.rows[start - 1:end]]

    def duplicate_sheet(self, spreadsheet_id, source_sheet_id, title):
        result = super().duplicate_sheet(spreadsheet_id, source_sheet_id, title)
        self.include_target = True
        self.rows = []
        return result

    def clear_values(self, spreadsheet_id, range_name):
        super().clear_values(spreadsheet_id, range_name)
        self.rows = self.rows[:1]

    def update_values(self, spreadsheet_id, ranges):
        super().update_values(spreadsheet_id, ranges)
        for item in ranges:
            start_cell = item["range"].rsplit("!", 1)[1].split(":", 1)[0]
            column = "".join(char for char in start_cell if char.isalpha())
            start = int("".join(char for char in start_cell if char.isdigit()))
            values = item["values"]
            for offset, row in enumerate(values):
                row_index = start - 1 + offset
                while len(self.rows) <= row_index:
                    self.rows.append([])
                if column == "A" and ":I" in item["range"]:
                    self.rows[row_index][0:9] = list(row)
                elif column == "K":
                    while len(self.rows[row_index]) < 13:
                        self.rows[row_index].append("")
                    self.rows[row_index][10:13] = list(row)
                elif column == "J":
                    while len(self.rows[row_index]) < 15:
                        self.rows[row_index].append("")
                    self.rows[row_index][9:14] = list(row)
                else:
                    self.rows[row_index] = list(row)

    def append_values(self, spreadsheet_id, range_name, rows):
        super().append_values(spreadsheet_id, range_name, rows)
        self.rows.extend(list(row) for row in rows)


class CorruptingGateway(StatefulGateway):
    def update_values(self, spreadsheet_id, ranges):
        super().update_values(spreadsheet_id, ranges)
        if self.rows and self.rows[0] == publisher.HEADER and len(self.rows) > 1:
            self.rows[1][8] = "incorrect machine field"


class AppendCorruptingGateway(StatefulGateway):
    def append_values(self, spreadsheet_id, range_name, rows):
        super().append_values(spreadsheet_id, range_name, rows)
        self.rows[-1][8] = "incorrect appended machine field"


class MultiSheetGateway(StatefulGateway):
    """Stateful model with independent rows for each destination tab."""

    def __init__(self):
        super().__init__(include_target=False)
        self.sheets = {"Proposals": {"sheet_id": 1, "rows": [publisher.HEADER]}}
        self.next_sheet_id = 2

    def spreadsheet(self, _spreadsheet_id):
        return {"sheets": [{"properties": {
            "title": title,
            "sheetId": details["sheet_id"],
            "gridProperties": {"rowCount": self.row_count},
        }} for title, details in self.sheets.items()]}

    @staticmethod
    def _title(range_name):
        return range_name.split("!", 1)[0].strip("'").replace("''", "'")

    def values(self, _spreadsheet_id, range_name):
        title = self._title(range_name)
        rows = self.sheets[title]["rows"]
        start = int(range_name.rsplit("!A", 1)[1].split(":", 1)[0])
        end = int(range_name.rsplit("O", 1)[1])
        return [list(row) for row in rows[start - 1:end]]

    def duplicate_sheet(self, spreadsheet_id, source_sheet_id, title):
        sheet_id = self.next_sheet_id
        self.next_sheet_id += 1
        self.created.append((source_sheet_id, title))
        self.sheets[title] = {"sheet_id": sheet_id, "rows": [publisher.HEADER]}
        return sheet_id

    def clear_values(self, spreadsheet_id, range_name):
        self.cleared.append(range_name)
        title = self._title(range_name)
        self.sheets[title]["rows"] = self.sheets[title]["rows"][:1]

    def update_values(self, spreadsheet_id, ranges):
        self.updated.extend(ranges)
        for item in ranges:
            title = self._title(item["range"])
            rows = self.sheets[title]["rows"]
            start_cell = item["range"].rsplit("!", 1)[1].split(":", 1)[0]
            column = "".join(char for char in start_cell if char.isalpha())
            start = int("".join(char for char in start_cell if char.isdigit()))
            for offset, row in enumerate(item["values"]):
                row_index = start - 1 + offset
                while len(rows) <= row_index:
                    rows.append([])
                if column == "A" and ":I" in item["range"]:
                    rows[row_index][0:9] = list(row)
                elif column == "K":
                    while len(rows[row_index]) < 13:
                        rows[row_index].append("")
                    rows[row_index][10:13] = list(row)
                elif column == "J":
                    while len(rows[row_index]) < 15:
                        rows[row_index].append("")
                    rows[row_index][9:14] = list(row)
                else:
                    rows[row_index] = list(row)

    def append_values(self, spreadsheet_id, range_name, rows):
        self.appended.append((range_name, list(rows)))
        self.sheets[self._title(range_name)]["rows"].extend(list(row) for row in rows)


def proposal(segment=1):
    return {
        "review_activity_key": "wka-1234567890abcdef12345678",
        "allocation_segment": segment,
        "start": "2026-08-01T10:00+03:00",
        "end": "2026-08-01T10:10+03:00",
        "duration_minutes": 10,
        "client_project": "Serenichron Level 2",
        "tag_names": ["System development"],
        "activity_id": "act-123",
        "confidence": "high",
        "description": "SC — Verified guarded Sheet publication with stable review identities",
    }


def capacity_recovery_warning(
    *, requested: int, allocated: int, recovered: int, residual: int,
):
    return {
        "type": "allocation_capacity_recovery",
        "requested_minutes": requested,
        "allocator_allocated_minutes": allocated,
        "recovered_minutes": recovered,
        "residual_minutes": residual,
    }


def recovery_proposal(
    segment: int, duration: int, *, activity_id: str = "activity-recovery",
    warnings=None,
):
    candidate = proposal(segment)
    candidate["activity_id"] = activity_id
    candidate["duration_minutes"] = duration
    candidate["review_warnings"] = list(warnings or [])
    candidate["provenance"] = {"allocation_capacity_recovery": True}
    return candidate


def portfolio_document():
    return {
        "source_run": "/private/runs/run-1",
        "activities": [{
            "review_id": "pvi-1234567890abcdef12345678",
            "start": "2026-08-01T10:00+03:00",
            "end": "2026-08-01T10:10+03:00",
            "duration_minutes": 10,
            "client_project": "Serenichron Level 2",
            "tag_names": ["System development"],
            "source_activity_ids": ["act-123", "act-456"],
            "confidence": "high",
            "description": "SC — Verified guarded Sheet publication with stable review identities",
            "validation_status": "flash_validated",
        }],
        "repair": {
            "status": "complete",
            "unresolved_wording": [],
        },
    }


class SheetPublicationTests(unittest.TestCase):
    def test_readback_uses_exact_raw_minutes_instead_of_rounded_sheet_display(self):
        rows = []
        for segment, minutes in ((1, 2), (2, 43.733333333333334)):
            row = publisher.proposal_row(proposal(segment), "run-1")
            row[3] = minutes
            rows.append(row)

        def sheet_read(arguments, **_kwargs):
            params = json.loads(arguments[arguments.index("--params") + 1])
            actual = [list(row) for row in rows]
            if params.get("valueRenderOption") != "UNFORMATTED_VALUE":
                actual[0][3] = "2."
                actual[1][3] = "43.733333"
            # Genuine typed dates must retain their string representation,
            # rather than becoming serial numbers under unformatted reads.
            elif params.get("dateTimeRenderOption") != "FORMATTED_STRING":
                actual[0][1] = 46235.416666666664
            return mock.Mock(returncode=0, stderr="", stdout=json.dumps({
                "values": [publisher.HEADER, *actual],
            }))

        with mock.patch.object(publisher.subprocess, "run", side_effect=sheet_read):
            publisher._verify_readback(
                publisher.GwsSheetsGateway(), "sheet", "'August 2026 review'",
                1000, rows, new_ids=frozenset(row[0] for row in rows),
            )

    def test_raw_readback_rejects_changes_beyond_one_ulp_transport_rounding(self):
        row = publisher.proposal_row(proposal(), "run-1")
        row[3] = 43.733333333333334
        actual = list(row)
        actual[3] = 43.73333333333332
        with mock.patch.object(publisher.subprocess, "run", return_value=mock.Mock(
            returncode=0, stderr="", stdout=json.dumps({
                "values": [publisher.HEADER, actual],
            }),
        )):
            with self.assertRaisesRegex(publisher.PublicationError, "machine fields"):
                publisher._verify_readback(
                    publisher.GwsSheetsGateway(), "sheet", "'August 2026 review'",
                    1000, [row],
                )

    def test_approved_raw_integral_float_minutes_are_unchanged_without_writes(self):
        row = publisher.proposal_row(proposal(), "run-1")
        prior = list(row)
        prior[3] = 10.0
        prior[9] = "approved"
        prior[13] = "posted"
        prior[14] = "Preserve this human note"
        gateway = StatefulGateway([publisher.HEADER, prior])
        result = publisher.publish(
            gateway, spreadsheet_id="sheet", sheet_title="August 2026 review",
            template_title="Proposals", rows=[row],
        )
        self.assertEqual(1, result["unchanged"])
        self.assertEqual([], gateway.updated)
        self.assertEqual(0, result["appended"])
        self.assertEqual(prior, gateway.rows[1])

    def test_sheet_duration_format_displays_fractional_minutes_without_padding_integers(self):
        # Numeric precision is not reviewable if the emitted Sheet format hides it.
        with mock.patch.object(publisher.subprocess, "run", return_value=mock.Mock(
            returncode=0, stdout="{}", stderr="",
        )) as command:
            publisher.GwsSheetsGateway().prepare_sheet("sheet-1", 2)
        arguments = command.call_args.args[0]
        request = json.loads(arguments[arguments.index("--json") + 1])
        formatting = request["requests"][0]["repeatCell"]
        self.assertEqual(3, formatting["range"]["startColumnIndex"])
        self.assertEqual(4, formatting["range"]["endColumnIndex"])
        self.assertEqual({"type": "NUMBER", "pattern": "0.######"},
                         formatting["cell"]["userEnteredFormat"]["numberFormat"])

    def test_new_row_duration_format_is_scoped_to_only_appended_minute_cells(self):
        # Appends beyond the sheet's initial 1000-row formatting must also
        # expose precision without changing human-owned formatting elsewhere.
        with mock.patch.object(publisher.subprocess, "run", return_value=mock.Mock(
            returncode=0, stdout="{}", stderr="",
        )) as command:
            publisher.GwsSheetsGateway().prepare_new_rows("sheet-1", 2, 1001, 1002)
        arguments = command.call_args.args[0]
        request = json.loads(arguments[arguments.index("--json") + 1])
        formats = [item["repeatCell"] for item in request["requests"]
                   if item.get("repeatCell", {}).get("fields") == "userEnteredFormat.numberFormat"]
        self.assertEqual(1, len(formats))
        self.assertEqual({"sheetId": 2, "startRowIndex": 1000, "endRowIndex": 1002,
                          "startColumnIndex": 3, "endColumnIndex": 4}, formats[0]["range"])
        self.assertEqual({"type": "NUMBER", "pattern": "0.######"},
                         formats[0]["cell"]["userEnteredFormat"]["numberFormat"])

    def test_published_rows_preserve_exact_seconds_for_native_posting(self):
        # Dropping timestamp seconds or flooring duration makes the posting
        # consumer reject the row or silently changes the approved interval.
        for builder, factory in (
            (publisher.proposal_row, proposal),
            (publisher.portfolio_row, lambda: portfolio_document()["activities"][0]),
        ):
            for start, end, seconds, minutes, expected_start, expected_end in (
                ("2026-08-01T10:00:17+03:00", "2026-08-01T10:07:45+03:00",
                 448, 7.466666666666667, "2026-08-01T07:00:17Z", "2026-08-01T07:07:45Z"),
                ("2026-12-01T10:00:00+02:00", "2026-12-01T10:05:25+02:00",
                 325, 5.416666666666667, "2026-12-01T08:00:00Z", "2026-12-01T08:05:25Z"),
                ("2026-08-01T10:00:17+03:00", "2026-08-01T10:00:42+03:00",
                 25, 0.4166666666666667, "2026-08-01T07:00:17Z", "2026-08-01T07:00:42Z"),
            ):
                for exact_field in (True, False):
                    with self.subTest(builder=builder.__name__, seconds=seconds,
                                      exact_field=exact_field):
                        candidate = factory()
                        candidate.update(start=start, end=end,
                                         duration_minutes=seconds // 60 if exact_field else minutes)
                        if exact_field:
                            candidate["duration_seconds"] = seconds
                        published = builder(candidate, "run-1")
                        self.assertEqual(minutes, published[3])
                        cells = lambda values: [{"userEnteredValue": {
                            "numberValue" if isinstance(value, (int, float)) else "stringValue": value,
                        }} for value in values]
                        capture = {"structuredContent": {
                            "spreadsheetId": "sheet-1", "sheets": [{
                                "properties": {"title": "August 2026 review"},
                                "data": [{"rowData": [
                                    {"values": cells(publisher.HEADER)},
                                    {"values": cells(published)},
                                ]}],
                            }],
                        }}
                        plan = native_post.build_plan(
                            capture, capture_sha256="a" * 64, routing={"session_routes": [{
                                "project_name": "Serenichron Level 2", "project_suffix": "123456",
                                "tag_names": ["System development"], "tag_suffixes": ["654321"],
                            }]}, routing_sha256="b" * 64, timezone="Europe/Bucharest",
                            workspace_id="workspace-1", member_id="user-1",
                            projects=[{"id": "project-123456"}], tags=[{"id": "tag-654321"}],
                            live_entries=[],
                        )
                        payload = plan["entries"][0]["payload"]
                        self.assertEqual(expected_start, payload["start"])
                        self.assertEqual(expected_end, payload["end"])
                        self.assertEqual(minutes, plan["total_minutes"])

    def test_whole_minute_published_rows_keep_historical_representation(self):
        for builder, candidate in (
            (publisher.proposal_row, proposal()),
            (publisher.portfolio_row, portfolio_document()["activities"][0]),
        ):
            for exact_field in (True, False):
                with self.subTest(builder=builder.__name__, exact_field=exact_field):
                    if exact_field:
                        candidate["duration_seconds"] = 600
                    else:
                        candidate.pop("duration_seconds", None)
                    row = builder(candidate, "run-1")
                    self.assertEqual(["2026-08-01 10:00", "2026-08-01 10:10", 10], row[1:4])
                    self.assertIs(type(row[3]), int)

    def test_row_uses_stable_segment_identity_and_pending_review_fields(self):
        row = publisher.proposal_row(proposal(2), "run-1")
        self.assertEqual("wka-1234567890abcdef12345678-s02", row[0])
        self.assertEqual("pending", row[9])
        self.assertEqual("unposted", row[13])
        self.assertEqual("", row[14])

    def test_routed_tagless_project_publishes_as_pending(self):
        """A valid project route must not become unresolved only because tags are empty."""
        candidate = proposal()
        candidate["tag_names"] = []

        row = publisher.proposal_row(candidate, "run-1")

        self.assertEqual("Serenichron Level 2", row[4])
        self.assertEqual("", row[5])
        self.assertEqual("pending", row[9])
        self.assertEqual("unposted", row[13])

    def test_unresolved_routing_marker_is_validated_and_serialized_in_reason(self):
        candidate = proposal()
        candidate.update({
            "client_project": "",
            "clockify_project_suffix": "",
            "tag_suffixes": [],
            "tag_names": [],
            "billable": False,
            "routing_disposition": "unresolved-routing",
            "review_warnings": [{
                "type": "unresolved_routing",
                "disposition": "unresolved-routing",
                "reason_code": "no_deterministic_route",
            }],
        })

        row = publisher.proposal_row(candidate, "run-1")

        self.assertEqual("wka-1234567890abcdef12345678-s01", row[0])
        self.assertEqual("", row[4])
        self.assertEqual("", row[5])
        self.assertEqual("pending", row[9])
        self.assertEqual(candidate["review_warnings"], json.loads(row[12]))
        self.assertEqual("unposted", row[13])

    def test_blank_route_fields_require_exact_unresolved_nonbillable_contract(self):
        valid = proposal()
        valid.update({
            "client_project": "",
            "tag_names": [],
            "billable": False,
            "routing_disposition": "unresolved-routing",
            "review_warnings": [{
                "type": "unresolved_routing",
                "disposition": "unresolved-routing",
                "reason_code": "no_deterministic_route",
            }],
        })
        invalid = {
            "missing marker": {key: value for key, value in valid.items() if key != "routing_disposition"},
            "billable unresolved": {**valid, "billable": True},
            "missing warning": {**valid, "review_warnings": []},
            "extra unresolved warning": {**valid, "review_warnings": valid["review_warnings"] * 2},
            "missing project suffix": {key: value for key, value in valid.items() if key != "clockify_project_suffix"},
            "missing tag suffixes": {key: value for key, value in valid.items() if key != "tag_suffixes"},
            "null project suffix": {**valid, "clockify_project_suffix": None},
            "text tag suffixes": {**valid, "tag_suffixes": ""},
        }

        for name, candidate in invalid.items():
            with self.subTest(name=name), self.assertRaises(publisher.PublicationError):
                publisher.proposal_row(candidate, "run-1")

    def test_routed_proposal_rejects_any_present_unknown_routing_disposition(self):
        for value in (None, "", "unresolved_routing", "other"):
            candidate = {**proposal(), "routing_disposition": value}
            with self.subTest(value=value), self.assertRaises(publisher.PublicationError):
                publisher.proposal_row(candidate, "run-1")

    def test_proposal_review_warnings_are_published_in_machine_owned_reason(self):
        candidate = proposal()
        candidate["review_warnings"] = [
            {
                "type": "observed_capacity_cap",
                "requested_minutes": 30,
                "observed_capacity_minutes": 10,
                "proposed_minutes": 10,
            },
            {
                "type": "existing_clockify_overlap",
                "counterpart_id": "ev-" + "1" * 64,
                "overlap_start": "2026-08-01T10:00:00+03:00",
                "overlap_end": "2026-08-01T10:05:00+03:00",
                "overlap_duration_seconds": 300,
            },
            {
                "type": "meeting_proposal_overlap",
                "counterpart_id": "wks-1234567890abcdef12345678",
                "counterpart_project_suffix": "bc17f7",
                "overlap_start": "2026-08-01T10:05:00+03:00",
                "overlap_end": "2026-08-01T10:10:00+03:00",
                "overlap_duration_seconds": 300,
            },
            {
                "type": "review_proposal_overlap",
                "counterpart_id": "wks-abcdef1234567890abcdef12",
                "counterpart_project_suffix": "775f9f",
                "overlap_start": "2026-08-01T10:00:00+03:00",
                "overlap_end": "2026-08-01T10:10:00+03:00",
                "overlap_duration_seconds": 600,
            },
        ]

        row = publisher.proposal_row(candidate, "run-1", project_allowlist={
            "bc17f7": "TST Prep Level 2",
            "775f9f": "Serenichron Level 2",
        })

        self.assertEqual([
            candidate["review_warnings"][0],
            candidate["review_warnings"][1],
            {
                **{key: value for key, value in candidate["review_warnings"][2].items()
                   if key != "counterpart_project_suffix"},
                "counterpart_project": "TST Prep Level 2",
            },
            {
                **{key: value for key, value in candidate["review_warnings"][3].items()
                   if key != "counterpart_project_suffix"},
                "counterpart_project": "Serenichron Level 2",
            },
        ], json.loads(row[12]))
        self.assertEqual("", row[14])

    def test_proposal_review_warnings_reject_private_payload(self):
        candidate = proposal()
        candidate["review_warnings"] = [{
            "type": "observed_capacity_cap",
            "requested_minutes": 30,
            "observed_capacity_minutes": 10,
            "proposed_minutes": 10,
            "raw_private_transcript": "private meeting transcript",
        }]

        with self.assertRaisesRegex(
            publisher.PublicationError, "unsupported fields"
        ):
            publisher.proposal_row(candidate, "run-1")

    def test_proposal_review_warnings_reject_unknown_extra_key(self):
        candidate = proposal()
        candidate["review_warnings"] = [{
            "type": "existing_clockify_overlap",
            "counterpart_id": "ev-" + "1" * 64,
            "counterpart_project_suffix": "31b39a",
            "overlap_start": "2026-08-01T10:00:00+03:00",
            "overlap_end": "2026-08-01T10:05:00+03:00",
            "overlap_duration_seconds": 300,
            "detail": "not part of the publication contract",
        }]

        with self.assertRaisesRegex(
            publisher.PublicationError, "unsupported fields"
        ):
            publisher.proposal_row(candidate, "run-1")

    def test_proposal_review_warnings_reject_unsafe_types_and_values(self):
        base = {
            "type": "existing_clockify_overlap",
            "counterpart_id": "ev-" + "1" * 64,
            "counterpart_project_suffix": "31b39a",
            "overlap_start": "2026-08-01T10:00:00+03:00",
            "overlap_end": "2026-08-01T10:05:00+03:00",
            "overlap_duration_seconds": 300,
        }
        invalid = {
            "unknown warning type": {**base, "type": "raw_evidence"},
            "nested scalar": {**base, "counterpart_project_suffix": {"raw": "private"}},
            "oversized scalar": {**base, "counterpart_project_suffix": "x" * 257},
            "nonprinting scalar": {**base, "counterpart_project_suffix": "safe\u202esecret"},
            "invalid timestamp": {**base, "overlap_start": "yesterday"},
            "naive timestamp": {**base, "overlap_start": "2026-08-01T10:00:00"},
            "negative number": {**base, "overlap_duration_seconds": -1},
            "boolean number": {**base, "overlap_duration_seconds": True},
        }
        for name, warning in invalid.items():
            with self.subTest(name=name):
                candidate = proposal()
                candidate["review_warnings"] = [warning]
                with self.assertRaises(publisher.PublicationError):
                    publisher.proposal_row(candidate, "run-1")

    def test_overlap_warnings_require_exact_positive_timestamp_duration(self):
        base = {
            "type": "existing_clockify_overlap",
            "counterpart_id": "ev-" + "1" * 64,
            "overlap_start": "2026-08-01T10:00:00+03:00",
            "overlap_end": "2026-08-01T10:05:00+03:00",
            "overlap_duration_seconds": 300,
        }
        invalid = {
            "mismatched duration": {**base, "overlap_duration_seconds": 299},
            "zero duration": {
                **base,
                "overlap_end": "2026-08-01T10:00:00+03:00",
                "overlap_duration_seconds": 0,
            },
            "reversed duration": {
                **base,
                "overlap_end": "2026-08-01T09:59:00+03:00",
                "overlap_duration_seconds": 60,
            },
            "fractional duration": {
                **base,
                "overlap_end": "2026-08-01T10:05:00.500000+03:00",
                "overlap_duration_seconds": 300,
            },
            "fractional endpoints with whole delta": {
                **base,
                "overlap_start": "2026-08-01T10:00:00.500000+03:00",
                "overlap_end": "2026-08-01T10:05:00.500000+03:00",
                "overlap_duration_seconds": 300,
            },
        }
        for name, warning in invalid.items():
            with self.subTest(name=name):
                candidate = proposal()
                candidate["review_warnings"] = [warning]
                with self.assertRaises(publisher.PublicationError):
                    publisher.proposal_row(candidate, "run-1")

    def test_capacity_warnings_require_a_positive_actual_cap(self):
        base = {
            "type": "observed_capacity_cap",
            "requested_minutes": 30,
            "observed_capacity_minutes": 10,
            "proposed_minutes": 10,
        }
        invalid = {
            "zero requested": {**base, "requested_minutes": 0},
            "zero observed": {
                **base, "observed_capacity_minutes": 0, "proposed_minutes": 0,
            },
            "zero proposed": {**base, "proposed_minutes": 0},
            "not capped": {**base, "requested_minutes": 10},
            "requested below observed": {**base, "requested_minutes": 9},
            "proposed differs from observed": {**base, "proposed_minutes": 9},
        }
        for name, warning in invalid.items():
            with self.subTest(name=name):
                candidate = proposal()
                candidate["review_warnings"] = [warning]
                with self.assertRaises(publisher.PublicationError):
                    publisher.proposal_row(candidate, "run-1")

    def test_allocation_capacity_recovery_serializes_zero_residual_boundary(self):
        warning = {
            "type": "allocation_capacity_recovery",
            "requested_minutes": 1,
            "allocator_allocated_minutes": 0,
            "recovered_minutes": 1,
            "residual_minutes": 0,
        }
        candidate = proposal()
        candidate["review_warnings"] = [warning]

        row = publisher.proposal_row(candidate, "run-1")

        self.assertEqual([warning], json.loads(row[12]))

    def test_allocation_capacity_recovery_accounts_for_credited_minutes(self):
        warning = {
            "type": "allocation_capacity_recovery",
            "requested_minutes": 10,
            "allocator_allocated_minutes": 2,
            "recovered_minutes": 3,
            "credited_minutes": 4,
            "residual_minutes": 1,
        }
        candidate = proposal()
        candidate["review_warnings"] = [warning]

        row = publisher.proposal_row(candidate, "run-1")

        self.assertEqual([warning], json.loads(row[12]))

    def test_allocation_capacity_recovery_rejects_invalid_contract(self):
        base = {
            "type": "allocation_capacity_recovery",
            "requested_minutes": 10,
            "allocator_allocated_minutes": 2,
            "recovered_minutes": 3,
            "residual_minutes": 5,
        }
        invalid = {
            "missing field": {
                key: value for key, value in base.items() if key != "residual_minutes"
            },
            "extra field": {**base, "private_detail": "must not serialize"},
            "zero requested": {**base, "requested_minutes": 0},
            "negative requested": {**base, "requested_minutes": -1},
            "negative allocated": {**base, "allocator_allocated_minutes": -1},
            "zero recovered": {**base, "recovered_minutes": 0},
            "negative recovered": {**base, "recovered_minutes": -1},
            "negative residual": {**base, "residual_minutes": -1},
            "requested bool": {**base, "requested_minutes": True},
            "allocated bool": {**base, "allocator_allocated_minutes": False},
            "recovered bool": {**base, "recovered_minutes": True},
            "residual bool": {**base, "residual_minutes": False},
            "recovery exceeds request": {
                **base,
                "allocator_allocated_minutes": 8,
                "recovered_minutes": 3,
                "residual_minutes": 0,
            },
            "residual mismatch": {**base, "residual_minutes": 4},
        }
        for name, warning in invalid.items():
            with self.subTest(name=name):
                candidate = proposal()
                candidate["review_warnings"] = [warning]
                with self.assertRaises(publisher.PublicationError):
                    publisher.proposal_row(candidate, "run-1")

    def test_recovery_groups_accept_one_aggregate_warning_and_distinct_activities(self):
        first_warning = capacity_recovery_warning(
            requested=6, allocated=1, recovered=5, residual=0,
        )
        second_warning = capacity_recovery_warning(
            requested=5, allocated=0, recovered=4, residual=1,
        )
        proposals = [
            recovery_proposal(1, 2, warnings=[first_warning]),
            recovery_proposal(2, 3),
            {
                **proposal(4),
                "activity_id": "activity-recovery",
                "duration_minutes": 1,
                "provenance": {},
            },
            recovery_proposal(
                3, 4, activity_id="activity-independent", warnings=[second_warning],
            ),
        ]

        publisher.validate_recovery_proposal_groups(proposals)

        self.assertEqual(
            [first_warning],
            json.loads(publisher.proposal_row(proposals[0], "run-1")[12]),
        )
        self.assertEqual("", publisher.proposal_row(proposals[1], "run-1")[12])

    def test_recovery_group_rejects_claimed_allocation_not_backed_by_proposals(self):
        warning = capacity_recovery_warning(
            requested=40, allocated=30, recovered=10, residual=0,
        )
        proposals = [
            {
                **proposal(1),
                "activity_id": "activity-recovery",
                "duration_minutes": 5,
                "provenance": {},
            },
            recovery_proposal(2, 10, warnings=[warning]),
        ]

        with self.assertRaisesRegex(
            publisher.PublicationError, "allocator allocation"
        ):
            publisher.validate_recovery_proposal_groups(proposals)

    def test_recovery_groups_reject_invalid_aggregate_contracts(self):
        valid_warning = capacity_recovery_warning(
            requested=5, allocated=0, recovered=5, residual=0,
        )
        cases = {
            "duplicate aggregate warnings": [
                recovery_proposal(1, 2, warnings=[valid_warning]),
                recovery_proposal(2, 3, warnings=[valid_warning]),
            ],
            "missing aggregate warning": [
                recovery_proposal(1, 2),
                recovery_proposal(2, 3),
            ],
            "wrong recovered total": [
                recovery_proposal(1, 5, warnings=[capacity_recovery_warning(
                    requested=5, allocated=1, recovered=4, residual=0,
                )]),
            ],
            "missing activity identity": [
                recovery_proposal(1, 5, activity_id="", warnings=[valid_warning]),
            ],
            "boolean recovery duration": [
                recovery_proposal(1, True, warnings=[valid_warning]),
            ],
            "nonpositive recovery duration": [
                recovery_proposal(1, 0, warnings=[valid_warning]),
            ],
            "malformed recovery provenance": [{
                **recovery_proposal(1, 5, warnings=[valid_warning]),
                "provenance": {"allocation_capacity_recovery": 1},
            }],
            "warning on non-recovery proposal": [{
                **proposal(1),
                "activity_id": "activity-recovery",
                "duration_minutes": 5,
                "review_warnings": [valid_warning],
                "provenance": {},
            }],
        }
        for name, proposals in cases.items():
            with self.subTest(name=name), self.assertRaises(publisher.PublicationError):
                publisher.validate_recovery_proposal_groups(proposals)

    def test_warning_semantic_boundaries_are_serialized(self):
        candidate = proposal()
        candidate["review_warnings"] = [
            {
                "type": "observed_capacity_cap",
                "requested_minutes": 2,
                "observed_capacity_minutes": 1,
                "proposed_minutes": 1,
            },
            {
                "type": "review_proposal_overlap",
                "counterpart_id": "wks-abcdef1234567890abcdef12",
                "counterpart_project_suffix": "775f9f",
                "overlap_start": "2026-08-01T10:00:00+03:00",
                "overlap_end": "2026-08-01T10:00:01+03:00",
                "overlap_duration_seconds": 1,
            },
        ]

        row = publisher.proposal_row(
            candidate, "run-1", project_allowlist={"775f9f": "Serenichron Level 2"}
        )

        warnings = json.loads(row[12])
        self.assertEqual(candidate["review_warnings"][0], warnings[0])
        self.assertEqual("Serenichron Level 2", warnings[1]["counterpart_project"])
        self.assertNotIn("counterpart_project_suffix", warnings[1])

    def test_overlap_warning_rejects_free_text_project_and_malformed_counterpart_ids(self):
        base = {
            "type": "existing_clockify_overlap",
            "counterpart_id": "ev-" + "1" * 64,
            "overlap_start": "2026-08-01T10:00:00+03:00",
            "overlap_end": "2026-08-01T10:05:00+03:00",
            "overlap_duration_seconds": 300,
        }
        invalid = (
            {**base, "counterpart_project": "Untrusted Project"},
            {**base, "counterpart_id": "ev-short"},
            {**base, "type": "meeting_proposal_overlap", "counterpart_id": "ev-" + "1" * 64},
            {**base, "type": "review_proposal_overlap", "counterpart_id": "wks-XYZ"},
        )
        for warning in invalid:
            with self.subTest(warning=warning):
                candidate = proposal()
                candidate["review_warnings"] = [warning]
                with self.assertRaises(publisher.PublicationError):
                    publisher.proposal_row(candidate, "run-1")

    def test_unknown_or_missing_project_suffix_omits_project_label(self):
        base = {
            "type": "existing_clockify_overlap",
            "counterpart_id": "ev-" + "1" * 64,
            "overlap_start": "2026-08-01T10:00:00+03:00",
            "overlap_end": "2026-08-01T10:05:00+03:00",
            "overlap_duration_seconds": 300,
        }
        candidate = proposal()
        candidate["review_warnings"] = [
            base,
            {**base, "counterpart_project_suffix": "abcdef"},
        ]

        warnings = json.loads(publisher.proposal_row(
            candidate, "run-1", project_allowlist={"775f9f": "Serenichron Level 2"}
        )[12])

        self.assertTrue(all("counterpart_project" not in warning for warning in warnings))
        self.assertTrue(all("counterpart_project_suffix" not in warning for warning in warnings))

    def test_project_allowlist_rejects_suffix_collision(self):
        routing = {
            "session_routes": [{
                "project_suffix": "775f9f", "project_name": "Serenichron Level 2",
            }],
            "meeting_routes": [{
                "project_suffix": "775f9f", "project_name": "Injected Name",
            }],
            "evidence_routes": [],
        }

        with self.assertRaisesRegex(publisher.PublicationError, "collision"):
            publisher.project_allowlist(routing)

    def test_project_allowlist_resolves_lifecycle_activation_route(self):
        routing = {
            "session_routes": [],
            "meeting_routes": [],
            "evidence_routes": [],
            "client_lifecycle_routes": [{
                "activation": {"route": {
                    "project_suffix": "a1b2c3",
                    "project_name": "Lifecycle Client Level 1",
                }},
            }],
        }

        self.assertEqual(
            {"a1b2c3": "Lifecycle Client Level 1"},
            publisher.project_allowlist(routing),
        )

    def test_project_allowlist_rejects_lifecycle_cross_section_collision(self):
        routing = {
            "session_routes": [{
                "project_suffix": "775f9f", "project_name": "Serenichron Level 2",
            }],
            "meeting_routes": [],
            "evidence_routes": [],
            "client_lifecycle_routes": [{
                "activation": {"route": {
                    "project_suffix": "775f9f", "project_name": "Injected Lifecycle Name",
                }},
            }],
        }

        with self.assertRaisesRegex(publisher.PublicationError, "collision"):
            publisher.project_allowlist(routing)

    def test_portfolio_row_uses_consolidated_identity_and_sources(self):
        row = publisher.portfolio_row(portfolio_document()["activities"][0], "run-1")
        self.assertEqual("pvi-1234567890abcdef12345678", row[0])
        self.assertEqual("act-123, act-456", row[6])
        self.assertEqual("flash_validated", row[12])
        self.assertEqual("pending", row[9])
        self.assertEqual("unposted", row[13])

    def test_portfolio_gates_bind_repair_quality_and_replay(self):
        portfolio = portfolio_document()
        quality = {
            "status": "pass",
            "fragmentation": {"row_count": 1, "total_minutes": 10},
        }
        replay = {
            "status": "pass",
            "identity": {"artifacts": {
                "repair": publisher.portfolio_replay._digest(portfolio),
                "quality": publisher.portfolio_replay._digest(quality),
            }},
        }

        publisher.verify_portfolio_gates(portfolio, quality, replay, "run-1")

        replay["identity"]["artifacts"]["repair"] = "sha256:" + "0" * 64
        with self.assertRaises(publisher.PublicationError):
            publisher.verify_portfolio_gates(portfolio, quality, replay, "run-1")

    def test_portfolio_gate_accepts_verified_zero_row_slice(self):
        portfolio = portfolio_document()
        portfolio["activities"] = []
        quality = {
            "status": "pass",
            "fragmentation": {"row_count": 0, "total_minutes": 0},
        }
        replay = {
            "status": "pass",
            "identity": {"artifacts": {
                "repair": publisher.portfolio_replay._digest(portfolio),
                "quality": publisher.portfolio_replay._digest(quality),
            }},
        }

        publisher.verify_portfolio_gates(portfolio, quality, replay, "run-1")

    def test_portfolio_gate_rejects_carried_source_review(self):
        portfolio = portfolio_document()
        portfolio["activities"][0]["validation_status"] = (
            "source_semantic_review_carried_after_flash_contract_failure"
        )
        quality = {
            "status": "pass",
            "fragmentation": {"row_count": 1, "total_minutes": 10},
        }
        replay = {
            "status": "pass",
            "identity": {"artifacts": {
                "repair": publisher.portfolio_replay._digest(portfolio),
                "quality": publisher.portfolio_replay._digest(quality),
            }},
        }

        with self.assertRaisesRegex(
            publisher.PublicationError, "Flash portfolio validation"
        ):
            publisher.verify_portfolio_gates(portfolio, quality, replay, "run-1")

    def test_new_month_tab_copies_template_clears_and_writes_rows(self):
        gateway = StatefulGateway(include_target=False)
        row = publisher.proposal_row(proposal(), "run-1")
        result = publisher.publish(
            gateway,
            spreadsheet_id="sheet",
            sheet_title="August 2026 review",
            template_title="Proposals",
            rows=[row],
        )
        self.assertEqual({"created": True, "appended": 1, "updated": 0, "unchanged": 0}, result)
        self.assertEqual([(1, "August 2026 review")], gateway.created)
        self.assertEqual([2], gateway.prepared)
        self.assertIn("'August 2026 review'!A2:O1000", gateway.cleared)
        self.assertEqual(publisher.HEADER, gateway.updated[0]["values"][0])

    def test_created_tab_requires_exact_machine_field_readback_before_success(self):
        row = publisher.proposal_row(proposal(), "run-1")
        with self.assertRaisesRegex(publisher.PublicationError, "readback"):
            publisher.publish(
                CorruptingGateway(include_target=False),
                spreadsheet_id="sheet",
                sheet_title="August 2026 review",
                template_title="Proposals",
                rows=[row],
            )

    def test_existing_ids_update_machine_fields_but_preserve_human_fields(self):
        new_row = publisher.proposal_row(proposal(), "run-2")
        old_row = list(new_row)
        old_row[8] = "SC — Older description"
        old_row[9] = "pending"
        old_row[13] = "modify"
        old_row[14] = "Keep this human note"
        gateway = StatefulGateway([publisher.HEADER, old_row])
        result = publisher.publish(
            gateway,
            spreadsheet_id="sheet",
            sheet_title="August 2026 review",
            template_title="Proposals",
            rows=[new_row],
        )
        self.assertEqual(1, result["updated"])
        ranges = [item["range"] for item in gateway.updated]
        self.assertEqual(["'August 2026 review'!A2:I2", "'August 2026 review'!K2:M2"], ranges)
        self.assertTrue(all("J" not in value and "N" not in value and "O" not in value for value in ranges))
        self.assertEqual("pending", gateway.rows[1][9])
        self.assertEqual("modify", gateway.rows[1][13])
        self.assertEqual("Keep this human note", gateway.rows[1][14])

    def test_approved_matching_id_with_changed_machine_fields_aborts_before_writes(self):
        new_row = publisher.proposal_row(proposal(), "run-2")
        for status in ("Approved", "posted"):
            with self.subTest(status=status):
                approved_row = list(new_row)
                approved_row[8] = "SC — Older description"
                approved_row[13] = status
                gateway = StatefulGateway([publisher.HEADER, approved_row])

                with self.assertRaisesRegex(publisher.PublicationError, "approved or posted"):
                    publisher.publish(
                        gateway,
                        spreadsheet_id="sheet",
                        sheet_title="August 2026 review",
                        template_title="Proposals",
                        rows=[new_row],
                    )

                self.assertEqual([], gateway.updated)
                self.assertEqual([], gateway.appended)

    def test_finds_matching_id_beyond_first_thousand_rows_without_duplicate_append(self):
        target = publisher.proposal_row(proposal(), "run-1")
        filler = publisher.proposal_row(proposal(2), "run-1")
        rows = [publisher.HEADER]
        for index in range(999):
            item = list(filler)
            item[0] = f"wka-{index:024d}-s01"
            rows.append(item)
        rows.append(target)
        gateway = StatefulGateway(rows, row_count=1500)

        result = publisher.publish(
            gateway,
            spreadsheet_id="sheet",
            sheet_title="August 2026 review",
            template_title="Proposals",
            rows=[target],
        )

        self.assertEqual(1, result["unchanged"])
        self.assertEqual(1, sum(row and row[0] == target[0] for row in gateway.rows))
        self.assertIn("'August 2026 review'!A1:O1000", gateway.read_ranges)
        self.assertIn("'August 2026 review'!A1001:O1500", gateway.read_ranges)

    def test_future_interval_appends_only_new_stable_ids(self):
        first = publisher.proposal_row(proposal(1), "run-1")
        second = publisher.proposal_row(proposal(2), "run-2")
        gateway = StatefulGateway([publisher.HEADER, first])
        result = publisher.publish(
            gateway,
            spreadsheet_id="sheet",
            sheet_title="August 2026 review",
            template_title="Proposals",
            rows=[first, second],
        )
        self.assertEqual(1, result["appended"])
        self.assertEqual(second[0], gateway.appended[0][1][0][0])
        self.assertEqual([(2, 3, 3)], gateway.new_rows_prepared)

    def test_repeat_publish_does_not_reinitialize_existing_rows(self):
        row = publisher.proposal_row(proposal(), "run-1")
        gateway = StatefulGateway([publisher.HEADER, row])
        publisher.publish(gateway, spreadsheet_id="sheet", sheet_title="August 2026 review", template_title="Proposals", rows=[row])
        self.assertEqual([], gateway.new_rows_prepared)

    def test_appended_row_requires_machine_field_readback_before_success(self):
        first = publisher.proposal_row(proposal(1), "run-1")
        second = publisher.proposal_row(proposal(2), "run-2")
        with self.assertRaisesRegex(publisher.PublicationError, "readback"):
            publisher.publish(
                AppendCorruptingGateway([publisher.HEADER, first]),
                spreadsheet_id="sheet",
                sheet_title="August 2026 review",
                template_title="Proposals",
                rows=[first, second],
            )

    def test_formatted_numeric_cells_are_idempotently_unchanged(self):
        row = publisher.proposal_row(proposal(), "run-1")
        formatted = [str(value) if isinstance(value, int) else value for value in row]
        gateway = FakeGateway([publisher.HEADER, formatted], include_target=True)
        result = publisher.publish(
            gateway,
            spreadsheet_id="sheet",
            sheet_title="August 2026 review",
            template_title="Proposals",
            rows=[row],
        )
        self.assertEqual(1, result["unchanged"])
        self.assertEqual([], gateway.updated)

    def test_one_ulp_duration_transport_noise_does_not_rewrite_or_fail_readback(self):
        row = publisher.proposal_row(proposal(), "run-1")
        row[3] = 29.983333333333334
        transported = list(row)
        transported[3] = math.nextafter(row[3], math.inf)
        gateway = StatefulGateway([publisher.HEADER, transported])
        result = publisher.publish(gateway, spreadsheet_id="sheet", sheet_title="August 2026 review", template_title="Proposals", rows=[row])
        self.assertEqual(1, result["unchanged"])
        self.assertEqual([], gateway.updated)
        self.assertEqual(0, result["appended"])
        self.assertEqual(transported, gateway.rows[1])

    def test_cell_comparison_rejects_real_numeric_changes_and_nonfinite_values(self):
        value = 29.983333333333334
        two_ulps = math.nextafter(math.nextafter(value, math.inf), math.inf)
        for other in (two_ulps, value + 1 / 60, math.inf, math.nan):
            with self.subTest(other=other):
                self.assertFalse(publisher._same_cell(value, other))
        self.assertFalse(publisher._same_cell(2**53, 2**53 + 1))
        self.assertFalse(publisher._same_cell("29.983333333333334", "29.983333333333338"))

    def test_duplicate_stable_ids_fail_closed(self):
        gateway = FakeGateway()
        row = publisher.proposal_row(proposal(), "run-1")
        with self.assertRaises(publisher.PublicationError):
            publisher.publish(
                gateway,
                spreadsheet_id="sheet",
                sheet_title="August 2026 review",
                template_title="Proposals",
                rows=[row, row],
            )

    def test_quality_and_replay_are_required(self):
        rows = [proposal()]
        publisher.verify_gates(
            rows,
            {"status": "pass", "summary": {"total_proposals": 1}},
            {"status": "pass", "failures": [], "source_run_id": "run-1"},
            "run-1",
        )
        with self.assertRaises(publisher.PublicationError):
            publisher.verify_gates(
                rows,
                {"status": "blocked", "summary": {"total_proposals": 1}},
                {"status": "pass", "failures": [], "source_run_id": "run-1"},
                "run-1",
            )

    def test_zero_proposal_quality_requires_explicit_integer_count(self):
        replay = {"status": "pass", "failures": [], "source_run_id": "run-1"}
        publisher.verify_gates(
            [], {"status": "pass", "summary": {"total_proposals": 0}}, replay, "run-1"
        )
        for invalid_count in (None, False, "0", -1):
            with self.subTest(count=invalid_count), self.assertRaises(publisher.PublicationError):
                publisher.verify_gates(
                    [], {"status": "pass", "summary": {"total_proposals": invalid_count}},
                    replay, "run-1",
                )

    def test_appended_row_requires_exact_initial_decision_readback(self):
        class WrongDecisionGateway(StatefulGateway):
            def append_values(self, spreadsheet_id, range_name, rows):
                super().append_values(spreadsheet_id, range_name, rows)
                self.rows[-1][13] = "posted"

        row = publisher.proposal_row(proposal(), "run-1")
        with self.assertRaisesRegex(publisher.PublicationError, "readback"):
            publisher.publish(
                WrongDecisionGateway([publisher.HEADER]),
                spreadsheet_id="sheet", sheet_title="August 2026 review",
                template_title="Proposals", rows=[row],
            )

    def test_created_row_requires_exact_initial_decision_readback(self):
        class WrongDecisionGateway(StatefulGateway):
            def update_values(self, spreadsheet_id, ranges):
                super().update_values(spreadsheet_id, ranges)
                self.rows[-1][9] = "Approved"

        row = publisher.proposal_row(proposal(), "run-1")
        with self.assertRaisesRegex(publisher.PublicationError, "readback"):
            publisher.publish(
                WrongDecisionGateway(include_target=False),
                spreadsheet_id="sheet", sheet_title="August 2026 review",
                template_title="Proposals", rows=[row],
            )

    def test_cli_without_enable_write_never_constructs_gateway(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "proposals.json").write_text(json.dumps([proposal()]))
            routing_path = root / "routing.json"
            routing_path.write_text(json.dumps({
                "session_routes": [], "meeting_routes": [], "evidence_routes": [],
            }))
            routing_digest = "sha256:" + hashlib.sha256(routing_path.read_bytes()).hexdigest()
            (root / "quality.json").write_text(json.dumps({
                "status": "pass", "summary": {"total_proposals": 1}
            }))
            (root / "replay.json").write_text(json.dumps({
                "status": "pass", "failures": [], "source_run_id": "run-1",
                "reconciliation_binding": {"routing_sha256": routing_digest},
            }))
            output = io.StringIO()
            with mock.patch.object(
                publisher, "GwsSheetsGateway", side_effect=AssertionError("write gateway used")
            ), contextlib.redirect_stdout(output):
                result = publisher.main([
                    "--spreadsheet-id", "sheet",
                    "--sheet-title", "August 2026 review",
                    "--proposals", str(root / "proposals.json"),
                    "--quality-report", str(root / "quality.json"),
                    "--replay-integrity", str(root / "replay.json"),
                    "--routing-snapshot", str(routing_path),
                    "--run-id", "run-1",
                ])
        self.assertEqual(0, result)
        self.assertFalse(json.loads(output.getvalue())["external_writes"])

    def test_proposal_cli_requires_routing_snapshot_before_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "proposals.json").write_text(json.dumps([proposal()]))
            (root / "quality.json").write_text(json.dumps({
                "status": "pass", "summary": {"total_proposals": 1}
            }))
            (root / "replay.json").write_text(json.dumps({
                "status": "pass", "failures": [], "source_run_id": "run-1",
                "reconciliation_binding": {"routing_sha256": "sha256:" + "0" * 64},
            }))
            with mock.patch.object(
                publisher, "GwsSheetsGateway", side_effect=AssertionError("write gateway used")
            ), self.assertRaises(SystemExit):
                publisher.main([
                    "--spreadsheet-id", "sheet",
                    "--sheet-title", "August 2026 review",
                    "--proposals", str(root / "proposals.json"),
                    "--quality-report", str(root / "quality.json"),
                    "--replay-integrity", str(root / "replay.json"),
                    "--run-id", "run-1",
                    "--enable-write",
                ])

    def test_proposal_cli_rejects_routing_digest_mismatch_before_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "proposals.json").write_text(json.dumps([proposal()]))
            routing_path = root / "routing.json"
            routing_path.write_text(json.dumps({
                "session_routes": [], "meeting_routes": [], "evidence_routes": [],
            }))
            (root / "quality.json").write_text(json.dumps({
                "status": "pass", "summary": {"total_proposals": 1}
            }))
            (root / "replay.json").write_text(json.dumps({
                "status": "pass", "failures": [], "source_run_id": "run-1",
                "reconciliation_binding": {"routing_sha256": "sha256:" + "0" * 64},
            }))
            with mock.patch.object(
                publisher, "GwsSheetsGateway", side_effect=AssertionError("write gateway used")
            ), self.assertRaisesRegex(publisher.PublicationError, "routing snapshot digest"):
                publisher.main([
                    "--spreadsheet-id", "sheet",
                    "--sheet-title", "August 2026 review",
                    "--proposals", str(root / "proposals.json"),
                    "--quality-report", str(root / "quality.json"),
                    "--replay-integrity", str(root / "replay.json"),
                    "--routing-snapshot", str(routing_path),
                    "--run-id", "run-1",
                    "--enable-write",
                ])

    def test_proposal_cli_rejects_invalid_recovery_group_before_gateway(self):
        invalid = recovery_proposal(1, 5, warnings=[capacity_recovery_warning(
            requested=5, allocated=1, recovered=4, residual=0,
        )])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "proposals.json").write_text(json.dumps([invalid]))
            routing_path = root / "routing.json"
            routing_path.write_text(json.dumps({
                "session_routes": [], "meeting_routes": [], "evidence_routes": [],
            }))
            routing_digest = "sha256:" + hashlib.sha256(routing_path.read_bytes()).hexdigest()
            (root / "quality.json").write_text(json.dumps({
                "status": "pass", "summary": {"total_proposals": 1}
            }))
            (root / "replay.json").write_text(json.dumps({
                "status": "pass", "failures": [], "source_run_id": "run-1",
                "reconciliation_binding": {"routing_sha256": routing_digest},
            }))
            with mock.patch.object(
                publisher, "GwsSheetsGateway", side_effect=AssertionError("write gateway used")
            ), self.assertRaisesRegex(publisher.PublicationError, "recovery"):
                publisher.main([
                    "--spreadsheet-id", "sheet",
                    "--sheet-title", "August 2026 review",
                    "--proposals", str(root / "proposals.json"),
                    "--quality-report", str(root / "quality.json"),
                    "--replay-integrity", str(root / "replay.json"),
                    "--routing-snapshot", str(routing_path),
                    "--run-id", "run-1",
                    "--enable-write",
                ])

    def test_portfolio_cli_dry_run_never_constructs_gateway(self):
        portfolio = portfolio_document()
        quality = {
            "status": "pass",
            "fragmentation": {"row_count": 1, "total_minutes": 10},
        }
        replay = {
            "status": "pass",
            "identity": {"artifacts": {
                "repair": publisher.portfolio_replay._digest(portfolio),
                "quality": publisher.portfolio_replay._digest(quality),
            }},
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "portfolio.json").write_text(json.dumps(portfolio))
            (root / "quality.json").write_text(json.dumps(quality))
            (root / "replay.json").write_text(json.dumps(replay))
            output = io.StringIO()
            with mock.patch.object(
                publisher, "GwsSheetsGateway", side_effect=AssertionError("write gateway used")
            ), contextlib.redirect_stdout(output):
                result = publisher.main([
                    "--spreadsheet-id", "sheet",
                    "--sheet-title", "August 2026 portfolio review",
                    "--portfolio-repair", str(root / "portfolio.json"),
                    "--quality-report", str(root / "quality.json"),
                    "--replay-integrity", str(root / "replay.json"),
                    "--run-id", "run-1",
                ])
        self.assertEqual(0, result)
        self.assertEqual(1, json.loads(output.getvalue())["rows"])

    def test_mixed_proposals_publish_to_distinct_tabs_with_independent_receipts(self):
        routed = proposal(1)
        unresolved = proposal(2)
        unresolved.update({
            "client_project": "",
            "clockify_project_suffix": "",
            "tag_suffixes": [],
            "tag_names": [],
            "billable": False,
            "routing_disposition": "unresolved-routing",
            "review_warnings": [{
                "type": "unresolved_routing",
                "disposition": "unresolved-routing",
                "reason_code": "no_deterministic_route",
            }],
        })
        gateway = MultiSheetGateway()

        result = publisher.publish_proposal_partitions(
            gateway,
            spreadsheet_id="sheet",
            sheet_title="September 2026 review",
            template_title="Proposals",
            proposals=[routed, unresolved],
            run_id="run-1",
            project_allowlist={},
        )

        self.assertEqual(
            ["September 2026 review", "unresolved-evidence"],
            [item["sheet_title"] for item in result["publications"]],
        )
        self.assertEqual(
            [publisher.stable_review_id(routed)],
            [row[0] for row in gateway.sheets["September 2026 review"]["rows"][1:]],
        )
        self.assertEqual(
            [publisher.stable_review_id(unresolved)],
            [row[0] for row in gateway.sheets["unresolved-evidence"]["rows"][1:]],
        )
        self.assertTrue(all(item["receipt_id"].startswith("sheet-publication/") for item in result["publications"]))
        self.assertTrue(all(item["row_count"] == 1 for item in result["publications"]))
        self.assertEqual(0, result["clockify_writes"])

        retried = publisher.publish_proposal_partitions(
            gateway,
            spreadsheet_id="sheet",
            sheet_title="September 2026 review",
            template_title="Proposals",
            proposals=[routed, unresolved],
            run_id="run-1",
            project_allowlist={},
        )
        self.assertEqual(
            [1, 1], [item["unchanged"] for item in retried["publications"]]
        )
        self.assertEqual(
            [item["receipt_id"] for item in result["publications"]],
            [item["receipt_id"] for item in retried["publications"]],
        )

    def test_credited_tombstone_supersedes_existing_row_without_appending(self):
        prior = publisher.proposal_row(proposal(), "run-before")
        prior[14] = "human review note — preserve exactly"
        gateway = MultiSheetGateway()
        gateway.sheets["September 2026 review"] = {
            "sheet_id": 2,
            "rows": [publisher.HEADER, prior],
        }
        tombstone = {
            "review_activity_key": proposal()["review_activity_key"],
            "allocation_segment": 1,
            "reason": "proposal fully credited to existing Clockify time",
            "credited_overlap_receipt": {
                "schema_version": "clockify-overlap-credit/v1",
                "credited_seconds": 600,
                "counterparts": [],
            },
        }

        result = publisher.publish_proposal_partitions(
            gateway,
            spreadsheet_id="sheet",
            sheet_title="September 2026 review",
            template_title="Proposals",
            proposals=[],
            tombstones=[tombstone],
            run_id="run-2",
            project_allowlist={},
        )

        updated = gateway.sheets["September 2026 review"]["rows"][1]
        self.assertEqual("superseded", updated[9])
        self.assertEqual("superseded", updated[13])
        self.assertRegex(updated[12], r"^credited-overlap sha256:[a-f0-9]{64}$")
        self.assertEqual("human review note — preserve exactly", updated[14])
        self.assertEqual([], gateway.appended)
        self.assertEqual(1, result["terminal_updates"])
        self.assertEqual(0, result["clockify_writes"])

    def test_only_nonempty_proposal_partition_is_published(self):
        unresolved = proposal()
        unresolved.update({
            "client_project": "", "clockify_project_suffix": "",
            "tag_suffixes": [], "tag_names": [], "billable": False,
            "routing_disposition": "unresolved-routing",
            "review_warnings": [{
                "type": "unresolved_routing",
                "disposition": "unresolved-routing",
                "reason_code": "no_deterministic_route",
            }],
        })
        for candidate, destination, absent in (
            (proposal(), "September 2026 review", "unresolved-evidence"),
            (unresolved, "unresolved-evidence", "September 2026 review"),
        ):
            with self.subTest(destination=destination):
                gateway = MultiSheetGateway()
                result = publisher.publish_proposal_partitions(
                    gateway,
                    spreadsheet_id="sheet",
                    sheet_title="September 2026 review",
                    template_title="Proposals",
                    proposals=[candidate],
                    run_id="run-1",
                    project_allowlist={},
                )
                self.assertEqual(
                    [destination],
                    [item["sheet_title"] for item in result["publications"]],
                )
                self.assertNotIn(absent, gateway.sheets)
                self.assertEqual(0, result["clockify_writes"])

    def test_proposal_cli_routes_mixed_payload_to_actual_destination_titles(self):
        unresolved = proposal(2)
        unresolved.update({
            "client_project": "",
            "clockify_project_suffix": "",
            "tag_suffixes": [],
            "tag_names": [],
            "billable": False,
            "routing_disposition": "unresolved-routing",
            "review_warnings": [{
                "type": "unresolved_routing",
                "disposition": "unresolved-routing",
                "reason_code": "no_deterministic_route",
            }],
        })
        gateway = MultiSheetGateway()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "proposals.json").write_text(json.dumps([proposal(1), unresolved]))
            routing_path = root / "routing.json"
            routing_path.write_text(json.dumps({
                "session_routes": [], "meeting_routes": [], "evidence_routes": [],
            }))
            routing_digest = "sha256:" + hashlib.sha256(routing_path.read_bytes()).hexdigest()
            (root / "quality.json").write_text(json.dumps({
                "status": "pass", "summary": {"total_proposals": 2},
            }))
            (root / "replay.json").write_text(json.dumps({
                "status": "pass", "failures": [], "source_run_id": "run-1",
                "reconciliation_binding": {"routing_sha256": routing_digest},
            }))
            output = io.StringIO()
            with mock.patch.object(publisher, "GwsSheetsGateway", return_value=gateway), \
                 contextlib.redirect_stdout(output):
                publisher.main([
                    "--spreadsheet-id", "sheet",
                    "--sheet-title", "September 2026 review",
                    "--proposals", str(root / "proposals.json"),
                    "--quality-report", str(root / "quality.json"),
                    "--replay-integrity", str(root / "replay.json"),
                    "--routing-snapshot", str(routing_path),
                    "--run-id", "run-1",
                    "--enable-write",
                ])

        result = json.loads(output.getvalue())
        self.assertEqual(
            ["September 2026 review", "unresolved-evidence"],
            [item["sheet_title"] for item in result["publications"]],
        )
        self.assertEqual(0, result["clockify_writes"])

    def test_cli_result_artifact_is_stable_across_idempotent_retry(self):
        gateway = MultiSheetGateway()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "proposals.json").write_text(json.dumps([proposal()]))
            routing_path = root / "routing.json"
            routing_path.write_text(json.dumps({
                "session_routes": [], "meeting_routes": [], "evidence_routes": [],
            }))
            routing_digest = "sha256:" + hashlib.sha256(routing_path.read_bytes()).hexdigest()
            (root / "quality.json").write_text(json.dumps({
                "status": "pass", "summary": {"total_proposals": 1},
            }))
            (root / "replay.json").write_text(json.dumps({
                "status": "pass", "failures": [], "source_run_id": "run-1",
                "reconciliation_binding": {"routing_sha256": routing_digest},
            }))
            result_path = root / "publication" / "autopilot-result.json"
            argv = [
                "--spreadsheet-id", "sheet",
                "--sheet-title", "September 2026 review",
                "--proposals", str(root / "proposals.json"),
                "--quality-report", str(root / "quality.json"),
                "--replay-integrity", str(root / "replay.json"),
                "--routing-snapshot", str(routing_path),
                "--run-id", "run-1",
                "--result-output", str(result_path),
                "--enable-write",
            ]
            with mock.patch.object(publisher, "GwsSheetsGateway", return_value=gateway), \
                 contextlib.redirect_stdout(io.StringIO()):
                publisher.main(argv)
                first = result_path.read_bytes()
                publisher.main(argv)
            self.assertEqual(first, result_path.read_bytes())
            durable = json.loads(first)
            self.assertEqual(
                ["September 2026 review"],
                [item["sheet_title"] for item in durable["publications"]],
            )

    def test_all_partitions_validate_before_the_first_sheet_mutation(self):
        invalid_unresolved = proposal(2)
        invalid_unresolved.update({
            "client_project": "", "clockify_project_suffix": "",
            "tag_suffixes": [], "tag_names": [], "billable": False,
            "routing_disposition": "unresolved-routing",
            "review_warnings": [],
        })
        gateway = MultiSheetGateway()

        with self.assertRaisesRegex(publisher.PublicationError, "unresolved nonbillable"):
            publisher.publish_proposal_partitions(
                gateway,
                spreadsheet_id="sheet",
                sheet_title="September 2026 review",
                template_title="Proposals",
                proposals=[proposal(1), invalid_unresolved],
                run_id="run-1",
                project_allowlist={},
            )

        self.assertEqual([], gateway.created)
        self.assertEqual([], gateway.updated)
        self.assertEqual([], gateway.appended)

    def test_unresolved_existing_row_conflict_causes_zero_monthly_mutation(self):
        routed = proposal(1)
        unresolved = proposal(2)
        unresolved.update({
            "client_project": "", "clockify_project_suffix": "",
            "tag_suffixes": [], "tag_names": [], "billable": False,
            "routing_disposition": "unresolved-routing",
            "review_warnings": [{
                "type": "unresolved_routing",
                "disposition": "unresolved-routing",
                "reason_code": "no_deterministic_route",
            }],
        })
        prior = publisher.proposal_row(unresolved, "old-run", project_allowlist={})
        prior[8] = "Immutable approved description"
        prior[9] = "approved"
        gateway = MultiSheetGateway()
        gateway.sheets["unresolved-evidence"] = {
            "sheet_id": 2, "rows": [publisher.HEADER, prior],
        }
        before = {
            title: [list(row) for row in details["rows"]]
            for title, details in gateway.sheets.items()
        }

        with self.assertRaisesRegex(publisher.PublicationError, "approved or posted"):
            publisher.publish_proposal_partitions(
                gateway,
                spreadsheet_id="sheet",
                sheet_title="September 2026 review",
                template_title="Proposals",
                proposals=[routed, unresolved],
                run_id="run-1",
                project_allowlist={},
            )

        self.assertEqual(before, {
            title: details["rows"] for title, details in gateway.sheets.items()
        })
        self.assertEqual([], gateway.created)
        self.assertEqual([], gateway.updated)
        self.assertEqual([], gateway.appended)


if __name__ == "__main__":
    unittest.main()
