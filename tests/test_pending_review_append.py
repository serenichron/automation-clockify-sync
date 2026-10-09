"""Opt-in behavior tests against genuine saved six-credit recovery inputs.

CLOCKIFY_APPEND_FIXTURE supplies private immutable sources, not fake completions.
"""
import copy
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from scripts import clockify_pending_review_selection as replacement
from scripts import clockify_sheet_publish as publisher
from test_sheet_publish import StatefulGateway


class AppendGateway(StatefulGateway):
    def __init__(self, rows):
        super().__init__(rows, row_count=1539)

    def spreadsheet(self, spreadsheet_id):
        metadata = super().spreadsheet(spreadsheet_id)
        metadata["sheets"][1]["properties"]["title"] = "September 2026 portfolio review"
        return metadata


class PendingAppendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = os.environ.get("CLOCKIFY_APPEND_FIXTURE")
        if not path:
            raise unittest.SkipTest("genuine saved append inputs not supplied")
        cls.fixture = json.loads(Path(path).read_bytes())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.binding = copy.deepcopy(self.fixture["binding"])

    def verify(self):
        path = self.root / "append.json"
        path.write_text(json.dumps(self.binding))
        try:
            append = importlib.import_module("scripts.clockify_pending_review_append")
        except ModuleNotFoundError:
            # RED exercises the real historical-label failure, not import text.
            prior = self.fixture["replacement_binding"]
            legacy_path = self.root / "replacement.json"
            legacy_path.write_text(json.dumps(prior))
            source = prior["sources"][prior["current_source"]]
            proposals = json.loads(Path(source["artifacts"]["proposals"]["path"]).read_bytes())
            routing = json.loads(Path(source["artifacts"]["routing"]["path"]).read_bytes())
            try:
                return replacement.verify(bindings_path=legacy_path,
                    source_dir=Path(source["artifacts"]["proposals"]["path"]).parent,
                    proposals=proposals, spreadsheet_id=prior["spreadsheet_id"],
                    sheet_title=prior["sheet_title"], run_id=source["run_id"],
                    project_allowlist=publisher.project_allowlist(routing))
            except ValueError as exc:
                self.fail("append-only missing credits still use unrelated retained LastSeenRun: " + str(exc))
        return append.verify(bindings_path=path)

    def test_six_saved_credits_append_without_reprojecting_historical_labels(self):
        result = self.verify()
        self.assertEqual(6, len(result["rows"]))
        self.assertEqual(10, sum(row[3] for row in result["rows"]))
        self.assertEqual(1223, result["receipt"]["preserved_existing_rows"])
        self.assertEqual([], result["updates"])
        self.assertFalse(result["receipt"]["full_source_coverage_claimed"])
        capture = json.loads(Path(self.binding["sheet_capture"]["path"]).read_bytes())
        gateway = AppendGateway([publisher.HEADER, *capture["rows"]])
        before = copy.deepcopy(gateway.rows)
        append = importlib.import_module("scripts.clockify_pending_review_append")
        plan = append.plan(gateway, verification=result)
        self.assertEqual([], plan["updates"])
        self.assertEqual(6, len(plan["appends"]))
        self.assertEqual(before, gateway.rows)
        self.assertIn("A1225:O1230", plan["append_range"])
        self.assertEqual(20, sum(len(value) for value in result["receipt"]["actual_clockify_counterparts"].values()))

    def test_hash_drift_rejects(self):
        name = self.binding["selected"][0]["source"]
        self.binding["sources"][name]["artifacts"]["proposals"]["sha256"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "drift|digest"):
            self.verify()

    def test_full_source_cannot_be_relabelled_as_subset_or_wrong_replay(self):
        name = self.binding["selected"][0]["source"]
        other = next(key for key in self.binding["replays"] if key != name)
        self.binding["replays"][name] = copy.deepcopy(self.binding["replays"][other])
        with self.assertRaisesRegex(ValueError, "replay"):
            self.verify()

    def test_selected_native_credit_digest_drift_rejects(self):
        self.binding["selected"][0]["proposal_sha256"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(ValueError, "selected native credit"):
            self.verify()

    def test_blank_native_route_rejects(self):
        self.binding["selected"].append(copy.deepcopy(self.fixture["blank_route_selection"]))
        with self.assertRaisesRegex(ValueError, "routing"):
            self.verify()

    def test_already_existing_selected_id_rejects(self):
        self.binding["selected"].append(copy.deepcopy(self.fixture["existing_id_selection"]))
        with self.assertRaisesRegex(ValueError, "already exists"):
            self.verify()

    def test_aggregate_complete_source_core_is_warning_only_without_own_financial_lineage(self):
        self.binding["financial_comparison"] = self.fixture["exact_duplicate_comparison"]
        try:
            result = self.verify()
        except ValueError as exc:
            self.fail("aggregate semantic interpretations are not own financial accomplishment proof: " + str(exc))
        self.assertEqual(6, len(result["rows"]))
        self.assertEqual(1, sum(len(value) for value in result["receipt"]["partial_source_relations_warning_only"].values()))
        self.assertFalse(result["receipt"]["financial_novelty_claimed"])

    def test_source_only_financial_binding_warns_without_inventing_same_work(self):
        handle = self.fixture["exact_duplicate_comparison"]
        comparison = json.loads(Path(handle["path"]).read_bytes())
        comparison["records"][0]["semantic_refs"] = []
        path = self.root / "source-only.json"
        path.write_text(json.dumps(comparison))
        self.binding["financial_comparison"] = replacement.artifact_handle(path)
        try:
            result = self.verify()
        except ValueError as exc:
            self.fail("source-only financial evidence must remain warning-only: " + str(exc))
        self.assertEqual(6, len(result["rows"]))
        self.assertFalse(result["receipt"]["financial_novelty_claimed"])
        warnings = result["receipt"]["partial_source_relations_warning_only"]
        self.assertEqual(1, sum(len(value) for value in warnings.values()))

    def test_each_actual_counterpart_id_description_and_duration_is_sheet_visible(self):
        comparison = self.root / "no-pending-comparison.json"
        comparison.write_text(json.dumps({"schema_version": "pending-append-financial-comparison/v1",
            "authority_boundary": "test actual Clockify warning projection", "records": []}))
        self.binding["financial_comparison"] = replacement.artifact_handle(comparison)
        result = self.verify()
        rows = {row[0]: row for row in result["rows"]}
        for rid, pairs in result["receipt"]["actual_clockify_counterparts"].items():
            for pair in pairs:
                with self.subTest(rid=rid, clockify_id=pair["id"]):
                    self.assertIn(pair["id"], rows[rid][12])
                    self.assertIn(str(pair["overlap_seconds"]) + " s overlap", rows[rid][12])
                    self.assertIn(" ".join(pair["description"].split())[:160], rows[rid][12])
                    self.assertIn(str(pair["counterpart_duration_seconds"]) + " s posted", rows[rid][12])
        self.assertEqual(20, sum(len(value) for value in result["receipt"]["actual_clockify_counterparts"].values()))

    def test_update_and_supersession_requests_reject(self):
        for key in ("updates", "supersessions", "prior_rows"):
            with self.subTest(key=key):
                saved = copy.deepcopy(self.binding)
                self.binding[key] = [{"review_id": "old"}]
                with self.assertRaisesRegex(ValueError, "schema|append-only"):
                    self.verify()
                self.binding = saved

    def test_live_snapshot_drift_rejects_without_writes(self):
        result = self.verify()
        capture = json.loads(Path(self.binding["sheet_capture"]["path"]).read_bytes())
        gateway = AppendGateway([publisher.HEADER, *capture["rows"]])
        gateway.rows[1][14] = "Reviewer changed this note"
        before = copy.deepcopy(gateway.rows)
        append = importlib.import_module("scripts.clockify_pending_review_append")
        with self.assertRaisesRegex(ValueError, "snapshot drift"):
            append.plan(gateway, verification=result)
        self.assertEqual(before, gateway.rows)

    def test_modified_acceptance_or_row_cannot_make_append_plan(self):
        result = self.verify()
        result["rows"][0][3] += 1
        capture = json.loads(Path(self.binding["sheet_capture"]["path"]).read_bytes())
        gateway = AppendGateway([publisher.HEADER, *capture["rows"]])
        append = importlib.import_module("scripts.clockify_pending_review_append")
        with self.assertRaisesRegex(ValueError, "verification drift"):
            append.plan(gateway, verification=result)


if __name__ == "__main__":
    unittest.main()
