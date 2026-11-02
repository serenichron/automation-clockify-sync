"""A group whose reviewed summary cannot be produced publishes its children individually."""
import json
from pathlib import Path
import unittest
from unittest import mock

from scripts import clockify_review_batch_publication as prepared
from scripts import clockify_review_cycle as cycle
from scripts import clockify_review_group_summary as group_summary
import test_review_batch_auto_preparation as auto_preparation


class GroupSummaryFallbackTests(unittest.TestCase):
    setUp = auto_preparation.AutomaticPreparationTests.setUp
    write = staticmethod(auto_preparation.AutomaticPreparationTests.write)

    def prepare_with(self, provider):
        return prepared.prepare(source_dir=self.run, output=self.root / "prepared.json", run_id="source-run",
            spreadsheet_id="sheet", sheet_title="August 2026 review", timezone="Europe/Bucharest",
            consolidate_materialized_corrections=True, summary_provider=provider)

    def failing(self, calls):
        def provider(activities):
            calls.append(len(activities))
            raise group_summary.GroupSummaryError("review summary failed independent review after one targeted repair")
        return provider

    def test_unavailable_summary_publishes_children_with_their_own_reviewed_wording(self):
        calls = []
        outcome = self.prepare_with(self.failing(calls))
        self.assertEqual([1], calls)
        self.assertEqual("complete", outcome["status"])
        self.assertEqual(154, outcome["prepared_seconds"])
        self.assertEqual(0, outcome["unprepared_seconds"])
        ids = sorted(outcome["summary_fallback"]["review_ids"])
        self.assertEqual(2, len(ids))
        self.assertIn("independent review", outcome["summary_fallback"]["reason"])
        document = json.loads((self.root / "prepared.json").read_text())
        self.assertEqual([], document["package"]["activities"])
        self.assertTrue(set(ids) <= set(document["standalone_review_ids"]))
        self.assertEqual(ids, document["summary_fallback_ids"])
        projection = prepared.project(path=self.root / "prepared.json", source_dir=self.run,
            proposals=self.sources, run_id="source-run", spreadsheet_id="sheet",
            sheet_title="August 2026 review", project_allowlist={"abc123": "Example"})
        rows = sorted(projection["rows"], key=lambda row: row[1])
        self.assertEqual(self.descriptions, [row[8] for row in rows])
        self.assertEqual([71 / 60, 83 / 60], [row[3] for row in rows])

    def test_fallback_binding_replays_without_calling_the_provider_again(self):
        self.prepare_with(self.failing([]))
        before = (self.root / "prepared.json").read_bytes()
        calls = []
        outcome = self.prepare_with(self.failing(calls))
        self.assertEqual([], calls)
        self.assertEqual(before, (self.root / "prepared.json").read_bytes())
        self.assertEqual("complete", outcome["status"])

    def test_successful_summary_still_consolidates(self):
        outcome = self.prepare_with(lambda activities: {item["review_id"]: {
            "child_review_ids": item["child_review_ids"],
            "description": "SC — Prepared source publication and verified its review receipt"}
            for item in activities})
        self.assertNotIn("summary_fallback", outcome)
        document = json.loads((self.root / "prepared.json").read_text())
        self.assertEqual(1, len(document["package"]["activities"]))

    def test_non_summary_failures_still_fail_closed(self):
        def broken(_activities):
            raise RuntimeError("transport broke")
        with self.assertRaises(RuntimeError):
            self.prepare_with(broken)
        self.assertFalse((self.root / "prepared.json").exists())

    def test_forged_or_removed_fallback_authority_is_rejected(self):
        self.prepare_with(self.failing([]))
        path = self.root / "prepared.json"
        document = json.loads(path.read_text())
        for forged in ({**document, "summary_fallback_ids": document["summary_fallback_ids"][:1]},
                       {key: value for key, value in document.items() if key != "summary_fallback_ids"}):
            path.write_text(json.dumps(forged))
            with self.assertRaises(ValueError):
                prepared.load(path)

    def test_cycle_records_the_concrete_preparation_error(self):
        config = {"spreadsheet_id": "sheet", "timezone": "Europe/Bucharest", "cache": str(self.root / "cache.jsonl")}
        source_record = {"run_dir": str(self.run), "run_id": "source-run", "artifact_digests": {}}
        error = group_summary.GroupSummaryError("review summary request exceeds bounded body size")
        with mock.patch.object(prepared, "prepare", side_effect=error):
            _, outcome = cycle._prepare_new_publication(config, source_record,
                sheet_title="August 2026 review", state_dir=self.root / "state")
        self.assertEqual("editorial_authority_unavailable", outcome["reason"])
        self.assertEqual("GroupSummaryError", outcome["error_type"])
        self.assertEqual("review summary request exceeds bounded body size", outcome["error"])
        with mock.patch.object(prepared, "prepare", side_effect=ValueError("private source text")):
            _, outcome = cycle._prepare_new_publication(config, source_record,
                sheet_title="August 2026 review", state_dir=self.root / "state")
        self.assertEqual("ValueError", outcome["error_type"])
        self.assertNotIn("private source text", json.dumps(outcome))


if __name__ == "__main__":
    unittest.main()
