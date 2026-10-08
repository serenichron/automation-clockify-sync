"""Immutable presentation and existing-publication proofs, entirely offline."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts import clockify_sheet_publish as publisher
from scripts import clockify_review_cycle as cycle
from test_sheet_publish import StatefulGateway, proposal
import test_pending_review_selection as pending_tests
import test_monthly_unresolved as monthly_tests
from scripts import clockify_monthly_unresolved as monthly


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return {"path": str(path), "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}


def digest(value):
    return "sha256:" + hashlib.sha256(json.dumps(value, separators=(",", ":"), sort_keys=True).encode()).hexdigest()


class PresentationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "native-run"
        self.proposals = [proposal()]
        self.native = [publisher.proposal_row(self.proposals[0], self.source.name)]
        self.readable = copy.deepcopy(self.native)
        self.readable[0][12] = "Verificați lucrarea distinctă înainte de aprobare."
        artifacts = {name: write(self.source / name, value) for name, value in {
            "proposals.json": self.proposals,
            "work-accounting-result.json": {"schema_version": 1, "allocation_mode": "non_overlapping_v1",
                "proposals": self.proposals, "skipped": [], "ambiguous": []},
            "routing.json": {}, "quality_report.json": {"status": "pass"},
            "evidence/evidence-ledger.json": {"events": []},
        }.items()}
        self.manifest = {"schema_version": "sheet-publication-presentation/v1", "spreadsheet_id": "sheet",
                         "source_run_id": self.source.name, "source_artifacts": artifacts,
                         "destinations": {"August 2026 review": {"kind": "primary",
                            "native_rows_sha256": digest(self.native),
                            "rows": write(self.root / "readable.json", self.readable)}}}
        self.path = self.root / "presentation.json"
        write(self.path, self.manifest)

    def publish(self, gateway):
        try:
            return publisher.publish_proposal_partitions(gateway, spreadsheet_id="sheet",
                sheet_title="August 2026 review", template_title="Proposals", proposals=self.proposals,
                run_id=self.source.name, project_allowlist={}, source_dir=self.source,
                presentation=self.path)
        except TypeError as exc:
            self.fail("publisher lacks immutable presentation input: " + str(exc))

    def test_readable_retry_zero_and_human_approval_preserved(self):
        row = copy.deepcopy(self.readable[0])
        row[9], row[13], row[14] = "approved", "posted", "Human note"
        gateway = StatefulGateway([publisher.HEADER, row])
        result = self.publish(gateway)
        self.assertEqual([publisher.HEADER, row], gateway.rows)
        self.assertEqual(0, result["publications"][0]["updated"])
        self.assertEqual(0, result["publications"][0]["appended"])
        self.assertEqual(digest(self.native), result["publications"][0]["presentation"]["native_rows_sha256"])

    def test_source_and_presentation_drift_rejected_before_writes(self):
        for path in (self.source / "proposals.json", self.root / "readable.json"):
            original = path.read_bytes()
            path.write_bytes(original + b" ")
            gateway = StatefulGateway([publisher.HEADER, *self.readable])
            with self.assertRaises(publisher.PublicationError):
                self.publish(gateway)
            self.assertEqual([], gateway.prepared)
            path.write_bytes(original)

    def test_presentation_cannot_change_identity_or_human_cells(self):
        for index in (0, 1, 4, 9, 13, 14):
            rows = copy.deepcopy(self.readable)
            rows[0][index] = "forged"
            self.manifest["destinations"]["August 2026 review"]["rows"] = write(self.root / "readable.json", rows)
            write(self.path, self.manifest)
            with self.assertRaises(publisher.PublicationError):
                self.publish(StatefulGateway([publisher.HEADER, *self.native]))

    def test_cycle_rederives_snapshot_and_ignores_current_optional_config_for_old_receipts(self):
        config = {"root": str(self.root), "spreadsheet_id": "sheet", "publication_presentation": str(self.path)}
        source = {"run_dir": str(self.source), "run_id": self.source.name}
        result = self.publish(StatefulGateway([publisher.HEADER, *self.readable]))
        expected = cycle._expected_publication_receipts(config, source, sheet_title="August 2026 review", publication_profile=None)
        document = {**result, "status": "published", "external_writes": True}
        self.assertEqual(expected, cycle._validated_publication_document(document, expected, source_dir=self.source))
        loaded = cycle._receipt_publication_config({}, document)
        self.assertEqual(str(self.path), loaded["publication_presentation"])
        self.assertNotIn("publication_presentation", cycle._receipt_publication_config(config, {"publications": []}))
        command = cycle._publisher_command(config, source, {"run_dir": str(self.root / "replay")},
            sheet_title="August 2026 review", result_path=self.root / "result.json")
        self.assertIn("--publication-presentation", command)
        unrelated = {"run_dir": str(self.root / "other-run"), "run_id": "other-run"}
        command = cycle._publisher_command(config, unrelated, {"run_dir": str(self.root / "replay")},
            sheet_title="August 2026 review", result_path=self.root / "other-result.json")
        self.assertNotIn("--publication-presentation", command)

    def test_existing_operator_readback_adoption_is_honest_and_snapshot_only(self):
        capture = {"spreadsheetId": "sheet", "sheets": [{"properties": {
            "title": "August 2026 review", "sheetId": 2, "gridProperties": {"rowCount": 1000}},
            "data": [{"rowData": [{"values": [{"effectiveValue": {
                "numberValue" if type(cell) in (int, float) else "stringValue": cell,
            }} for cell in row]} for row in [publisher.HEADER, *self.readable]]}]}]}
        operator = self.root / "operator.json"
        write(operator, {"readback": capture, "verification": {"clockifyWrites": 0}})
        try:
            result = publisher.publish_proposal_partitions(None, spreadsheet_id="sheet",
                sheet_title="August 2026 review", template_title="Proposals", proposals=self.proposals,
                run_id=self.source.name, project_allowlist={}, source_dir=self.source,
                presentation=self.path, existing_publication=operator)
        except TypeError as exc:
            self.fail("missing explicit existing-publication verifier: " + str(exc))
        self.assertEqual("verified-existing", result["status"])
        self.assertIs(False, result["external_writes"])
        self.assertEqual(0, result["terminal_updates"])
        expected = cycle._expected_publication_receipts({"spreadsheet_id": "sheet", "publication_presentation": str(self.path)},
            {"run_dir": str(self.source), "run_id": self.source.name}, sheet_title="August 2026 review", publication_profile=None)
        validated = cycle._validated_publication_document(result, expected, source_dir=self.source)
        self.assertEqual("immutable_operator_readback", validated[0]["existing_publication"]["verification_basis"])
        forged = copy.deepcopy(result)
        forged["publications"][0]["existing_publication"]["rows"][0][4] = "wrong project"
        with self.assertRaises(cycle.CycleError):
            cycle._validated_publication_document(forged, expected, source_dir=self.source)
        operator.write_text(operator.read_text() + " ")
        with self.assertRaises(cycle.CycleError):
            cycle._validated_publication_document(result, expected, source_dir=self.source)

    def test_pending_retry_preserves_new_and_retained_human_approval_not_supersession(self):
        fixture = pending_tests.PendingSelectionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.binding["reason_projection"] = write(fixture.root / "reasons.json", {
            review_id: "Verificați înainte de aprobare." for review_id in fixture.binding["selected_current_ids"]})
        write(fixture.binding_path, fixture.binding)
        gateway = pending_tests.SelectionGateway([publisher.HEADER, *fixture.baseline])
        fixture.publish(gateway)
        for row in (gateway.rows[17], gateway.rows[-1]):
            row[9], row[13], row[14] = "approved", "posted", "Human decision"
        before = copy.deepcopy(gateway.rows)
        result = fixture.publish(gateway)
        self.assertEqual(before, gateway.rows)
        self.assertEqual(0, result["terminal_updates"])
        self.assertEqual(0, result["publications"][0]["appended"])
        self.assertEqual(0, result["publications"][0]["updated"])
        config = {"root": str(fixture.root), "spreadsheet_id": "sheet", "pending_review_selection": str(fixture.binding_path)}
        source = {"run_dir": str(fixture.current_dir), "run_id": fixture.current_dir.name}
        command = cycle._publisher_command(config, source, {"run_dir": str(fixture.root / "current-replay")},
            sheet_title="August 2026 review", result_path=fixture.root / "result.json")
        self.assertIn("--pending-review-selection", command)
        other = {"run_dir": str(fixture.root / "other-run"), "run_id": "other-run"}
        command = cycle._publisher_command(config, other, {"run_dir": str(fixture.root / "current-replay")},
            sheet_title="August 2026 review", result_path=fixture.root / "other-result.json")
        self.assertNotIn("--pending-review-selection", command)
        restored = cycle._receipt_publication_config({}, result)
        self.assertEqual(str(fixture.binding_path), restored["pending_review_selection"])
        self.assertNotIn("pending_review_selection", cycle._receipt_publication_config(config, {"publications": []}))
        gateway.rows[1][9] = "approved"
        with self.assertRaises(publisher.PublicationError):
            fixture.publish(gateway)

    def test_monthly_display_is_rederived_and_K_is_human_owned_including_legacy(self):
        monthly_tests.frozen_run(self.source, 1)
        write(self.source / "routing.json", {})
        self.manifest["source_artifacts"] = {name: {"path": str(self.source / name),
            "sha256": "sha256:" + hashlib.sha256((self.source / name).read_bytes()).hexdigest()}
            for name in self.manifest["source_artifacts"]}
        native_rows = monthly.project_rows(self.source)
        display_rows = copy.deepcopy(native_rows)
        for row in display_rows:
            for index in (1, 4, 5, 6, 8, 9):
                row[index] = "Text românesc " + str(index)
        title = "September 2026 unresolved evidence"
        self.manifest["destinations"] = {title: {"kind": "monthly", "native_rows_sha256": digest(native_rows),
            "rows": write(self.root / "monthly.json", display_rows)}}
        write(self.path, self.manifest)
        from scripts import clockify_publication_presentation as display
        projected, proof = display.project(path=self.path, source_dir=self.source, run_id=self.source.name,
            spreadsheet_id="sheet", sheet_title=title, rows=native_rows, kind="monthly")
        for legacy in (False, True):
            rows = monthly.rows_for_layout(projected, monthly.LEGACY_LAYOUT if legacy else None)
            rows[0][10] = "approved"
            gateway = monthly_tests.MonthlyGateway([monthly.LEGACY_HEADER if legacy else monthly.HEADER, *rows])
            before = copy.deepcopy(gateway.rows)
            result = publisher.publish_monthly_unresolved(gateway, spreadsheet_id="sheet", sheet_title=title,
                rows=projected, source_dir=self.source)
            result["presentation"] = proof
            expected = cycle._expected_publication_receipts({"spreadsheet_id": "sheet", "publication_presentation": str(self.path)},
                {"run_dir": str(self.source), "run_id": self.source.name}, sheet_title="September 2026 portfolio review")
            # The unrouted source fixture has no valid primary row; the monthly
            # partition alone is expected under the monthly publication profile.
            self.assertEqual(1, len(expected))
            validated = cycle._validated_publication_document({"schema_version": "sheet-publication-result/v1",
                "status": "published", "external_writes": True, "clockify_writes": 0, "publications": [result]},
                expected, source_dir=self.source)
            self.assertEqual(proof, validated[0]["presentation"])
            self.assertEqual(before, gateway.rows)
            self.assertEqual([], gateway.writes)
        for index in (0, 2, 3, 7, 10, 11):
            forged = copy.deepcopy(display_rows)
            forged[0][index] = "forged"
            self.manifest["destinations"][title]["rows"] = write(self.root / "monthly.json", forged)
            write(self.path, self.manifest)
            with self.assertRaises(ValueError):
                display.project(path=self.path, source_dir=self.source, run_id=self.source.name,
                    spreadsheet_id="sheet", sheet_title=title, rows=native_rows, kind="monthly")


if __name__ == "__main__":
    unittest.main()
