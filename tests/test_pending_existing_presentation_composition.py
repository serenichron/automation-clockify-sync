"""Authenticated display cells compose with native pending readback adoption."""
import copy
import json
from pathlib import Path
import unittest

from scripts import clockify_pending_review_selection as pending
from scripts import clockify_publication_presentation as display
from scripts import clockify_review_cycle as cycle
from scripts import clockify_sheet_publish as publisher
import test_pending_review_selection as fixtures


class PendingExistingPresentationCompositionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture = fixtures.PendingSelectionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        artifacts = fixture.binding["sources"]["current"]["artifacts"]
        accounting = json.loads(Path(artifacts["accounting"]["path"]).read_text())
        accounting.update(schema_version=1, allocation_mode="non_overlapping_v1", ambiguous=[], skipped=[])
        for key in ("accounting", "replay_accounting"):
            artifacts[key] = fixtures.write(Path(artifacts[key]["path"]), accounting)
        packet = json.loads(Path(artifacts["receipt"]["path"]).read_text())
        packet["deterministic_accounting_replay"]["work-accounting-result.json"] = {
            "byte_equal": True, "primary_sha256": artifacts["accounting"]["sha256"][7:],
            "replay_sha256": artifacts["replay_accounting"]["sha256"][7:]}
        artifacts["receipt"] = fixtures.write(Path(artifacts["receipt"]["path"]), packet)
        fixtures.write(fixture.binding_path, fixture.binding)
        selection = pending.verify(bindings_path=fixture.binding_path, source_dir=fixture.current_dir,
            proposals=fixture.current, spreadsheet_id="sheet", sheet_title="August 2026 review",
            run_id=fixture.current_dir.name, project_allowlist={})
        self.presented = copy.deepcopy(selection["rows"])
        self.presented[0][12] = "Review current Clockify overlap: exact entry; 60 seconds."
        self.presented[-1][12] = "Preserved retained review explanation."
        self.manifest = {"schema_version": display.SCHEMA, "spreadsheet_id": "sheet",
            "source_run_id": fixture.current_dir.name,
            "source_artifacts": {name: artifacts[key] for name, key in (
                ("proposals.json", "proposals"), ("work-accounting-result.json", "accounting"),
                ("quality_report.json", "quality"), ("routing.json", "routing"),
                ("evidence/evidence-ledger.json", "ledger"))},
            "destinations": {"August 2026 review": {"kind": "primary",
                "native_rows_sha256": display.digest(selection["rows"]),
                "rows": fixtures.write(fixture.root / "presented.json", self.presented)}}}
        self.presentation_path = fixture.root / "presentation.json"
        fixtures.write(self.presentation_path, self.manifest)
        self.observed = copy.deepcopy(fixture.baseline)
        for row in self.observed[:16]:
            row[9] = "superseded"
        by_id = {row[0]: row for row in self.observed}
        for row in self.presented:
            if row[0] in by_id:
                by_id[row[0]][12] = row[12]
            else:
                self.observed.append(copy.deepcopy(row))
        for review_id in (self.presented[0][0], self.presented[-1][0]):
            row = next(row for row in self.observed if row[0] == review_id)
            row[9], row[13], row[14] = "approved", "posted", "Preserved human decision"
        self.operator_path = fixture.root / "operator.json"
        self.write_operator()

    def write_operator(self):
        fixtures.write(self.operator_path, {"readback": {"spreadsheetId": "sheet", "sheets": [{
            "properties": {"title": "August 2026 review", "sheetId": 2,
                           "gridProperties": {"rowCount": 1000}},
            "data": [{"startRow": 0, "startColumn": 0, "rowData": [{"values": [{
                "effectiveValue": {"numberValue" if type(cell) in (int, float) else "stringValue": cell}}
                for cell in row]} for row in [publisher.HEADER, *self.observed]]}]}]}})

    def publish(self, *, presentation=True):
        return self.fixture.publish(None, existing_publication=self.operator_path,
                                    presentation=self.presentation_path if presentation else None)

    def adopt(self):
        try:
            return self.publish()
        except publisher.PublicationError as exc:
            self.fail("Authenticated displayed pending rows must be adoptable: " + str(exc))

    def test_authenticated_m_projection_adopts_real_pending_readback_and_cycle_accepts(self):
        result = self.adopt()
        self.assertEqual("verified-existing", result["status"])
        self.assertFalse(result["external_writes"])
        self.assertEqual(0, result["clockify_writes"])
        self.assertEqual(0, result["terminal_updates"])
        expected = cycle._expected_publication_receipts({"spreadsheet_id": "sheet",
            "pending_review_selection": str(self.fixture.binding_path),
            "publication_presentation": str(self.presentation_path)},
            {"run_dir": str(self.fixture.current_dir), "run_id": self.fixture.current_dir.name},
            sheet_title="August 2026 review", publication_profile=None)
        validated = cycle._validated_publication_document(result, expected, source_dir=self.fixture.current_dir)
        self.assertEqual(34, len(validated[0]["row_ids"]))
        self.assertEqual(self.presented, validated[0]["existing_publication"]["rows"])

    def test_no_presentation_still_rejects_changed_machine_m(self):
        with self.assertRaises(publisher.PublicationError):
            self.publish(presentation=False)

    def test_presented_capture_drift_cannot_change_financial_route_or_source_cells(self):
        original = copy.deepcopy(self.observed)
        for index in (1, 2, 3, 4, 5, 6, 7, 8, 11, 12):
            with self.subTest(column=index):
                self.observed = copy.deepcopy(original)
                self.observed[-13][index] = "forged"
                self.write_operator()
                with self.assertRaises(publisher.PublicationError):
                    self.publish()

    def test_presentation_cannot_repin_changed_financial_cell(self):
        self.presented[0][3] = 999
        self.manifest["destinations"]["August 2026 review"]["rows"] = fixtures.write(
            self.fixture.root / "presented.json", self.presented)
        fixtures.write(self.presentation_path, self.manifest)
        with self.assertRaises(publisher.PublicationError):
            self.publish()

    def test_display_does_not_relax_supersession_human_decision(self):
        self.observed[0][9] = "approved"
        self.write_operator()
        with self.assertRaises(publisher.PublicationError):
            self.publish()

    def test_direct_existing_proof_requires_authenticated_presentation(self):
        result = self.adopt()
        receipt = result["publications"][0]
        forged = copy.deepcopy(receipt)
        forged["presentation"]["presented_rows_sha256"] = "sha256:" + "0" * 64
        with self.assertRaises(ValueError):
            display.verify_existing(forged["existing_publication"], forged)


if __name__ == "__main__":
    unittest.main()
