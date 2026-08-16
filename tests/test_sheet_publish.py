import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import clockify_sheet_publish as publisher


class FakeGateway:
    def __init__(self, rows=None, include_target=False):
        self.rows = rows or []
        self.include_target = include_target
        self.created = []
        self.prepared = []
        self.cleared = []
        self.updated = []
        self.appended = []

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
    def test_row_uses_stable_segment_identity_and_pending_review_fields(self):
        row = publisher.proposal_row(proposal(2), "run-1")
        self.assertEqual("wka-1234567890abcdef12345678-s02", row[0])
        self.assertEqual("pending", row[9])
        self.assertEqual("pending", row[13])
        self.assertEqual("", row[14])

    def test_portfolio_row_uses_consolidated_identity_and_sources(self):
        row = publisher.portfolio_row(portfolio_document()["activities"][0], "run-1")
        self.assertEqual("pvi-1234567890abcdef12345678", row[0])
        self.assertEqual("act-123, act-456", row[6])
        self.assertEqual("flash_validated", row[12])
        self.assertEqual("pending", row[9])
        self.assertEqual("pending", row[13])

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
        gateway = FakeGateway()
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

    def test_existing_ids_update_machine_fields_but_preserve_human_fields(self):
        new_row = publisher.proposal_row(proposal(), "run-2")
        old_row = list(new_row)
        old_row[8] = "SC — Older description"
        old_row[9] = "approved"
        old_row[13] = "modify"
        old_row[14] = "Keep this human note"
        gateway = FakeGateway([publisher.HEADER, old_row], include_target=True)
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

    def test_future_interval_appends_only_new_stable_ids(self):
        first = publisher.proposal_row(proposal(1), "run-1")
        second = publisher.proposal_row(proposal(2), "run-2")
        gateway = FakeGateway([publisher.HEADER, first], include_target=True)
        result = publisher.publish(
            gateway,
            spreadsheet_id="sheet",
            sheet_title="August 2026 review",
            template_title="Proposals",
            rows=[first, second],
        )
        self.assertEqual(1, result["appended"])
        self.assertEqual(second[0], gateway.appended[0][1][0][0])

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

    def test_cli_without_enable_write_never_constructs_gateway(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "proposals.json").write_text(json.dumps([proposal()]))
            (root / "quality.json").write_text(json.dumps({
                "status": "pass", "summary": {"total_proposals": 1}
            }))
            (root / "replay.json").write_text(json.dumps({
                "status": "pass", "failures": [], "source_run_id": "run-1"
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
                    "--run-id", "run-1",
                ])
        self.assertEqual(0, result)
        self.assertFalse(json.loads(output.getvalue())["external_writes"])

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


if __name__ == "__main__":
    unittest.main()
