"""Canonical attendance remains reviewable without invented semantic authority."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_recorded_attendance_repair_integration as attendance
from scripts import clockify_review_batch_publication as prepared
from scripts import clockify_sheet_publish as publisher
from scripts import collector_receipts


class RecordedFallbackReviewPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source, self.corrections, self.proposals = attendance.RecordedAttendanceRepairTests().source(self.root)

    def prepare(self, source=None, output=None):
        source = source or self.source
        return prepared.prepare(source_dir=source, output=output or self.root / "prepared.json",
            run_id=source.name, spreadsheet_id="sheet", sheet_title="August review",
            timezone="Europe/Bucharest", all_durations=True,
            summary_provider=lambda *_: self.fail("Factual attendance must not request semantic wording"))

    def project(self, source, output, proposals):
        return prepared.project(path=output, source_dir=source, proposals=proposals,
            run_id=source.name, spreadsheet_id="sheet", sheet_title="August review",
            project_allowlist=publisher.project_allowlist(json.loads((source / "routing.json").read_text())))

    def test_unrouted_canonical_attendance_projects_standalone_and_replays_immutably(self):
        # Catches dropping genuine time solely because there is no semantic outcome or known client.
        before = {p: p.read_bytes() for p in self.source.rglob("*") if p.is_file()}
        result = self.prepare()
        self.assertEqual("complete", result["status"])
        self.assertEqual(8622, result["prepared_seconds"])
        output = self.root / "prepared.json"
        document, _ = prepared.load(output)
        self.assertEqual([], document["package"]["activities"])
        self.assertEqual(2, len(document["recorded_attendance_authority"]["review_ids"]))
        projection = self.project(self.source, output, self.proposals)
        self.assertEqual({}, projection["parents"])
        self.assertEqual(2, len(projection["rows"]))
        expected = {publisher.stable_review_id(p): p for p in self.proposals}
        for row in projection["rows"]:
            proposal = expected[row[0]]
            self.assertEqual(proposal["duration_seconds"] / 60, row[3])
            self.assertEqual(("", "", "pending", "unposted"), (row[4], row[5], row[9], row[13]))
            self.assertEqual(proposal["description"], row[8])
            self.assertEqual(proposal["activity_id"], row[6])
            self.assertEqual(proposal["review_warnings"], json.loads(row[12]))
        frozen = output.read_bytes()
        self.prepare()
        self.assertEqual(frozen, output.read_bytes())
        self.assertEqual(before, {p: p.read_bytes() for p in self.source.rglob("*") if p.is_file()})
        # The new authority must bind the evidence it consulted, not only proposal wording.
        ledger = self.source / "evidence/evidence-ledger.json"
        ledger.write_bytes(ledger.read_bytes() + b" ")
        with self.assertRaises((ValueError, publisher.PublicationError)):
            prepared.load(output)

    def test_routing_only_corrected_canonical_attendance_stays_factual_and_standalone(self):
        # A legitimate routing decision must not require fabricated wording/reviewer provenance.
        run = attendance.run
        with mock.patch.object(run, "RUNS", self.root / "runs"):
            child = run._prepare_repair_run(self.source, corrections_override=self.corrections)
            accounting = attendance.accounting.run_accounting(child, root=attendance.fixtures.ROOT,
                routing_path=child / "routing.json", corrections_path=child / "review-corrections.jsonl",
                analysis_fixture=run._repair_analysis_fixture(child))
            attendance.validate_repair(child, self.root / "runs", self.root / "repair-items.json")
            run._finalize_repair_completion(child)
        output = self.root / "routed.json"
        result = self.prepare(child, output)
        self.assertEqual("complete", result["status"])
        document, _ = prepared.load(output)
        self.assertEqual([], document["package"]["activities"])
        self.assertEqual(2, len(document["standalone_review_ids"]))
        projection = self.project(child, output, accounting["proposals"])
        self.assertEqual(2, len(projection["rows"]))
        for proposal, row in zip(accounting["proposals"], projection["rows"]):
            self.assertEqual(proposal["description"], row[8])
            self.assertEqual(proposal["client_project"], row[4])
            self.assertEqual(proposal["duration_seconds"] / 60, row[3])
        self.assertEqual([], json.loads((child / "semantic-analysis.json").read_text())["activities"])

    def test_resealed_forged_untimed_and_nonrecording_lookalikes_are_withheld(self):
        # Even a fresh matching accounting receipt cannot manufacture canonical attendance.
        paths = [self.source / name for name in ("proposals.json", "work-accounting-result.json", "completion-bundle.json")]
        baseline = {p: p.read_bytes() for p in paths}
        variants = [
            {"provenance": {**self.proposals[0]["provenance"], "semantic_fallback": False}},
            {"provenance": {**self.proposals[0]["provenance"], "source_type": "codex"}},
            {"provenance": {**self.proposals[0]["provenance"], "canonical_meeting_id": "cm-" + "0" * 64}},
            {"duration_seconds": 3506}, {"start": None},
            {"description": "SC — Claimed an unsupported delivered project outcome"},
        ]
        original_bundle = collector_receipts.load_completion_bundle(paths[2], run_dir=self.source)
        for index, patch in enumerate(variants):
            with self.subTest(index=index):
                proposals = copy.deepcopy(self.proposals)
                proposals[0].update(patch)
                attendance.fixtures.write_json(paths[0], proposals)
                result = json.loads(baseline[paths[1]])
                result["proposals"] = proposals
                attendance.fixtures.write_json(paths[1], result)
                slice_ = attendance.fixtures.argparse.Namespace(slice_id=original_bundle.slice_id,
                    since=attendance.fixtures.dt.datetime(2026, 8, 1, tzinfo=attendance.fixtures.dt.UTC),
                    until=attendance.fixtures.dt.datetime(2026, 8, 2, tzinfo=attendance.fixtures.dt.UTC))
                collector_receipts.write_completion_bundle(paths[2], collector_receipts.build_completion_bundle(self.source, slice_=slice_))
                result = self.prepare(output=self.root / f"invalid-{index}.json")
                self.assertEqual("incomplete", result["status"])
                self.assertIn(publisher.stable_review_id(proposals[0]), result["unprepared_review_ids"])
                for path, content in baseline.items():
                    path.write_bytes(content)

    def test_historical_binding_without_fallback_opt_in_keeps_original_bytes(self):
        authority = {name: prepared.artifact_handle(self.source / filename)
                     for name, filename in prepared.AUTHORITY_FILES.items()}
        fields = prepared._auto_fields(self.proposals, authority, self.source, "Europe/Bucharest",
            consolidation_policy=prepared.ALL_DURATION_CONSOLIDATION_POLICY)
        historical = {"schema_version": prepared.AUTO_SCHEMA, "run_id": self.source.name,
            "spreadsheet_id": "sheet", "sheet_title": "August review", "timezone": "Europe/Bucharest",
            "source_proposals": prepared.artifact_handle(self.source / "proposals.json"),
            "authority": authority, "consolidation_policy": prepared.ALL_DURATION_CONSOLIDATION_POLICY, **fields}
        output = self.root / "historical.json"
        output.write_text(json.dumps(historical))
        before = output.read_bytes()
        self.prepare(output=output)
        self.assertEqual(before, output.read_bytes())
        self.assertEqual([], self.project(self.source, output, self.proposals)["rows"])


if __name__ == "__main__":
    unittest.main()
