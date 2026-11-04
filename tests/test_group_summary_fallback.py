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

    def add_reviewed_source(self, index, start, end, seconds, wording):
        from test_review_batch_publication import source
        proposal = source(index, start, end, seconds)
        model, revision = auto_preparation.semantic_analyzer.CURRENT_LIVE_FLASH_ROUTE
        proposal["provenance"].update(semantic_reviewer_model=model, semantic_reviewer_revision=revision)
        self.sources.append(proposal)
        self.analysis["activities"].append({"activity_id": proposal["activity_id"],
            "evidence_ids": proposal["provenance"]["evidence_ids"], "rendered_description": wording,
            "semantic_reviewer_model": model, "semantic_reviewer_revision": revision})
        self.write(self.run / "proposals.json", self.sources)
        self.write(self.run / "semantic-analysis.json", self.analysis)
        self.write(self.run / "work-accounting-result.json", {"schema_version": 1,
            "allocation_mode": "non_overlapping_v1", "proposals": self.sources, "ambiguous": [], "skipped": []})
        return prepared.publisher.stable_review_id(proposal)

    def test_unavailable_summary_also_falls_back_for_singleton_needing_review(self):
        # A singleton is sent to the provider only because its wording needs
        # screening. When the provider fails, it must not block the delivery.
        noisy = "SC — Restored SER-1801 receipt comparison"
        singleton = self.add_reviewed_source(3, "2026-08-02T15:00:00+03:00", "2026-08-02T15:01:40+03:00", 100, noisy)
        calls = []
        outcome = self.prepare_with(self.failing(calls))
        self.assertEqual([2], calls)
        self.assertEqual("complete", outcome["status"])
        self.assertEqual(254, outcome["prepared_seconds"])
        self.assertIn(singleton, outcome["summary_fallback"]["review_ids"])
        self.assertEqual(3, len(outcome["summary_fallback"]["review_ids"]))
        projection = prepared.project(path=self.root / "prepared.json", source_dir=self.run,
            proposals=self.sources, run_id="source-run", spreadsheet_id="sheet",
            sheet_title="August 2026 review", project_allowlist={"abc123": "Example"})
        self.assertEqual(sorted(self.descriptions + [noisy]), sorted(row[8] for row in projection["rows"]))
        before = (self.root / "prepared.json").read_bytes()
        replay_calls = []
        self.assertEqual("complete", self.prepare_with(self.failing(replay_calls))["status"])
        self.assertEqual([], replay_calls)
        self.assertEqual(before, (self.root / "prepared.json").read_bytes())

    def test_unavailable_summary_falls_back_when_only_a_singleton_needs_review(self):
        noisy = "SC — Restored SER-1801 receipt comparison"
        self.sources = self.sources[:1]
        self.analysis["activities"] = self.analysis["activities"][:1]
        self.analysis["activities"][0]["rendered_description"] = noisy
        self.write(self.run / "proposals.json", self.sources)
        self.write(self.run / "semantic-analysis.json", self.analysis)
        self.write(self.run / "work-accounting-result.json", {"schema_version": 1,
            "allocation_mode": "non_overlapping_v1", "proposals": self.sources, "ambiguous": [], "skipped": []})
        calls = []
        outcome = self.prepare_with(self.failing(calls))
        self.assertEqual([1], calls)
        self.assertEqual("complete", outcome["status"])
        self.assertEqual([prepared.publisher.stable_review_id(self.sources[0])],
                         outcome["summary_fallback"]["review_ids"])
        projection = prepared.project(path=self.root / "prepared.json", source_dir=self.run,
            proposals=self.sources, run_id="source-run", spreadsheet_id="sheet",
            sheet_title="August 2026 review", project_allowlist={"abc123": "Example"})
        self.assertEqual([noisy], [row[8] for row in projection["rows"]])
        replay_calls = []
        self.assertEqual("complete", self.prepare_with(self.failing(replay_calls))["status"])
        self.assertEqual([], replay_calls)

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
