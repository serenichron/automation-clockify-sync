"""Offline historical pending proofs retain original, byte-bound runtime paths."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import shutil
import unittest
from unittest import mock

from scripts import clockify_pending_review_selection as pending
from scripts import clockify_review_cycle as cycle
from scripts import clockify_publication_presentation as display, clockify_sheet_publish as publisher
from scripts import work_accounting_pipeline as pipeline, work_allocator as allocator
import test_pending_review_selection as selection_fixtures
from test_review_cycle_delivery import write_json


class HistoricalPendingRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = selection_fixtures.PendingSelectionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.modules = {"consumer": pending, "pipeline": pipeline, "allocator": allocator}
        self.historical_paths = {}
        for role, module in self.modules.items():
            original = Path(module.__file__).resolve()
            historical = self.fixture.root / "historical-release" / "scripts" / original.name
            historical.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, historical)
            self.historical_paths[role] = historical
        current = self.verify()
        historical = self.verify_historical(self.historical_paths)
        self.expected = [cycle._publication_receipt(
            spreadsheet_id="sheet", sheet_title="August 2026 review", rows=current["rows"],
        )]
        self.expected[0]["pending_selection"] = current["receipt"]
        self.document = {
            "schema_version": "sheet-publication-result/v1", "status": "published",
            "external_writes": True, "clockify_writes": 0,
            "publications": [{**cycle._publication_receipt(
                spreadsheet_id="sheet", sheet_title="August 2026 review", rows=historical["rows"],
            ), "pending_selection": historical["receipt"]}],
        }

    def verify_historical(self, paths):
        with (
            mock.patch.object(pending, "__file__", str(paths["consumer"])),
            mock.patch.object(pipeline, "__file__", str(paths["pipeline"])),
            mock.patch.object(allocator, "__file__", str(paths["allocator"])),
        ):
            return self.verify()

    def verify(self):
        return pending.verify(
            bindings_path=self.fixture.binding_path, source_dir=self.fixture.current_dir,
            proposals=self.fixture.current, spreadsheet_id="sheet", sheet_title="August 2026 review",
            run_id=self.fixture.current_dir.name, project_allowlist={},
        )

    def validate(self, document=None, expected=None):
        return cycle._validated_publication_document(
            self.document if document is None else document,
            self.expected if expected is None else expected,
            source_dir=self.fixture.current_dir,
        )

    @staticmethod
    def reseal(proof):
        unsigned = {key: value for key, value in proof.items() if key != "acceptance_sha256"}
        proof["acceptance_sha256"] = "sha256:" + hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def test_original_runtime_paths_are_accepted_only_for_identical_verified_role_bytes(self):
        """Catches rejecting a genuine pending receipt after an identical-code release relocation."""
        receipt_path = self.fixture.root / "historical-publication.json"
        write_json(receipt_path, self.document)
        immutable = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (
            receipt_path, *self.historical_paths.values(), self.fixture.binding_path,
        )}
        expected_before = copy.deepcopy(self.expected)
        recorded_before = copy.deepcopy(self.document)
        result = self.validate()
        self.assertEqual(self.document["publications"], result)
        self.assertEqual(recorded_before, self.document)
        self.assertEqual(expected_before, self.expected)
        self.assertEqual(34, len(result[0]["row_ids"]))
        self.assertEqual({role: str(path) for role, path in self.historical_paths.items()},
                         {role: handle["path"] for role, handle in result[0]["pending_selection"]["runtime_artifacts"].items()})
        self.assertEqual(immutable, {path: (path.read_bytes(), path.stat().st_mtime_ns)
                                    for path in immutable})

    def test_runtime_inventory_paths_and_role_substitution_fail_closed(self):
        """Catches missing/tampered code or role substitution, not authentic version upgrades."""
        for mode in ("missing-role", "extra-role", "role-substitution", "missing-file",
                     "relative-file", "wrong-sha", "changed-code", "symlink", "unknown-schema"):
            with self.subTest(mode=mode):
                document = copy.deepcopy(self.document)
                proof = document["publications"][0]["pending_selection"]
                handles = proof["runtime_artifacts"]
                if mode == "missing-role":
                    del handles["allocator"]
                elif mode == "extra-role":
                    handles["unverified"] = copy.deepcopy(handles["consumer"])
                elif mode == "role-substitution":
                    handles["allocator"] = copy.deepcopy(handles["pipeline"])
                elif mode == "missing-file":
                    handles["consumer"]["path"] = str(self.fixture.root / "missing.py")
                elif mode == "relative-file":
                    handles["consumer"]["path"] = "historical-release/scripts/consumer.py"
                elif mode == "wrong-sha":
                    handles["consumer"]["sha256"] = "sha256:" + "a" * 64
                elif mode == "changed-code":
                    changed = self.fixture.root / "changed-consumer.py"
                    changed.write_bytes(self.historical_paths["consumer"].read_bytes() + b"\n# drift\n")
                    handles["consumer"] = pending.artifact_handle(changed)
                elif mode == "symlink":
                    linked = self.fixture.root / "linked-consumer.py"
                    linked.symlink_to(self.historical_paths["consumer"])
                    handles["consumer"]["path"] = str(linked)
                else:
                    proof["schema_version"] = "unknown-pending-acceptance/v1"
                self.reseal(proof)
                if mode == "changed-code":
                    self.assertEqual(document["publications"], self.validate(document))
                    continue
                with self.assertRaises(cycle.CycleError):
                    self.validate(document)

    def test_non_ascii_historical_paths_keep_pending_acceptance_digest_contract(self):
        """Catches using cycle serialization instead of the existing pending-proof digest contract."""
        paths = {}
        for role, original in self.historical_paths.items():
            path = self.fixture.root / "istoric-ș" / original.name
            path.parent.mkdir(exist_ok=True)
            shutil.copyfile(original, path)
            paths[role] = path
        document = copy.deepcopy(self.document)
        document["publications"][0]["pending_selection"] = self.verify_historical(paths)["receipt"]
        self.assertEqual(document["publications"], self.validate(document))

    def test_original_runtime_file_drift_and_missing_current_runtime_are_rejected(self):
        """Catches checking only claimed hashes rather than both original and current bytes."""
        original = self.historical_paths["consumer"]
        original.write_bytes(original.read_bytes() + b"\n# changed after receipt\n")
        with self.assertRaises(cycle.CycleError):
            self.validate()
        shutil.copyfile(Path(pending.__file__).resolve(), original)
        expected = copy.deepcopy(self.expected)
        expected[0]["pending_selection"]["runtime_artifacts"]["consumer"]["path"] = str(self.fixture.root / "missing-current.py")
        with self.assertRaises(cycle.CycleError):
            self.validate(expected=expected)

    def test_portable_paths_cannot_hide_any_other_pending_proof_or_publication_change(self):
        """Catches normalizing outputs or original source proof along with runtime location metadata."""
        original = self.document["publications"][0]["pending_selection"]
        for field in set(original) - {"runtime_artifacts", "acceptance_sha256"}:
            with self.subTest(field=field):
                document = copy.deepcopy(self.document)
                proof = document["publications"][0]["pending_selection"]
                proof[field] = {"changed": True}
                self.reseal(proof)
                with self.assertRaises(cycle.CycleError):
                    self.validate(document)
        for field, value in (("rows_sha256", "sha256:" + "a" * 64), ("row_ids", ["unrelated-review"])):
            with self.subTest(field=field):
                document = copy.deepcopy(self.document)
                document["publications"][0][field] = value
                with self.assertRaises(cycle.CycleError):
                    self.validate(document)
        document = copy.deepcopy(self.document)
        document["publications"][0]["pending_selection"]["acceptance_sha256"] = "sha256:" + "a" * 64
        with self.assertRaises(cycle.CycleError):
            self.validate(document)

    def historical_existing_publication(self):
        gateway = selection_fixtures.SelectionGateway([publisher.HEADER, *self.fixture.baseline])
        self.fixture.publish(gateway)
        operator = self.fixture.root / "operator-readback.json"
        capture = {"spreadsheetId": "sheet", "sheets": [{"properties": {
            "title": "August 2026 review", "sheetId": 2, "gridProperties": {"rowCount": 1000}},
            "data": [{"rowData": [{"values": [{"effectiveValue": {
                "numberValue" if type(cell) in (int, float) else "stringValue": cell,
            }} for cell in row]} for row in gateway.rows]}]}]}
        write_json(operator, {"readback": capture, "verification": {"clockifyWrites": 0}})
        with (
            mock.patch.object(pending, "__file__", str(self.historical_paths["consumer"])),
            mock.patch.object(pipeline, "__file__", str(self.historical_paths["pipeline"])),
            mock.patch.object(allocator, "__file__", str(self.historical_paths["allocator"])),
        ):
            result = self.fixture.publish(None, existing_publication=operator)
        return result, operator

    def test_verified_existing_operator_readback_revalidates_original_runtime_paths(self):
        """Catches the nested operator-readback reader rejecting byte-identical historical code paths."""
        document, operator = self.historical_existing_publication()
        immutable = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                     for path in (operator, *self.historical_paths.values())}
        original = copy.deepcopy(document)
        self.assertEqual("verified-existing", document["status"])
        self.assertIs(False, document["external_writes"])
        self.assertEqual(0, document["clockify_writes"])
        self.assertEqual(document["publications"], self.validate(document))
        self.assertEqual(original, document)
        self.assertEqual(immutable, {path: (path.read_bytes(), path.stat().st_mtime_ns)
                                    for path in immutable})

    def test_portable_operator_proof_still_rejects_machine_drift_and_incomplete_supersession(self):
        """Catches a runtime-location adapter bypassing original readback cells or supersession planning."""
        document, operator = self.historical_existing_publication()
        original = operator.read_bytes()
        for mode in ("machine-cell", "missing-selected-row", "incomplete-supersession"):
            with self.subTest(mode=mode):
                captured = json.loads(original)
                rows = captured["readback"]["sheets"][0]["data"][0]["rowData"]
                if mode == "machine-cell":
                    rows[-1]["values"][4] = {"effectiveValue": {"stringValue": "wrong project"}}
                elif mode == "missing-selected-row":
                    rows.pop()
                else:
                    rows[1]["values"][9] = {"effectiveValue": {"stringValue": "pending"}}
                write_json(operator, captured)
                changed = copy.deepcopy(document)
                receipt = changed["publications"][0]
                proof = receipt["existing_publication"]
                proof["operator_receipt"] = pending.artifact_handle(operator)
                error = "supersession is incomplete" if mode == "incomplete-supersession" else "captured machine cells differ"
                with self.assertRaisesRegex(ValueError, error):
                    display.verify_existing(proof, receipt)
                with self.assertRaises(cycle.CycleError):
                    self.validate(changed)

    def test_portable_operator_proof_preserves_human_review_decisions(self):
        """Catches replacing human cells while revalidating a recorded pending projection."""
        document, operator = self.historical_existing_publication()
        captured = json.loads(operator.read_bytes())
        cells = captured["readback"]["sheets"][0]["data"][0]["rowData"][-1]["values"]
        for index, value in ((9, "approved"), (13, "posted"), (14, "Human decision")):
            cells[index] = {"effectiveValue": {"stringValue": value}}
        write_json(operator, captured)
        receipt = document["publications"][0]
        receipt["existing_publication"]["operator_receipt"] = pending.artifact_handle(operator)
        self.assertEqual(document["publications"], self.validate(document))


if __name__ == "__main__":
    unittest.main()
