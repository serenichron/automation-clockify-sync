"""Fresh deliveries synthesize complete groups; immutable retries stay offline."""
import copy
import json
from pathlib import Path
import unittest
from unittest import mock

from scripts import clockify_review_batch_publication as prepared
from scripts import clockify_review_cycle as cycle
from scripts import semantic_analyzer as semantic
import test_review_batch_auto_preparation as preparation_fixtures


class CycleAutomaticGroupSummaryTests(unittest.TestCase):
    def setUp(self):
        self.real_summarize = cycle._summarize_review_groups
        self.fixture = preparation_fixtures.AutomaticPreparationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.path = self.fixture.root / "summary-binding.json"
        self.description = "SC — Prepared source publication and verified its review receipt"

    def summaries(self, activities):
        return {item["review_id"]: {"child_review_ids": item["child_review_ids"],
                "description": self.description} for item in activities
                if len({child["description"] for child in item["prepared_descriptions"]}) > 1}

    def prepare(self, provider):
        return prepared.prepare(source_dir=self.fixture.run, output=self.path,
            run_id="source-run", spreadsheet_id="sheet", sheet_title="August 2026 review",
            timezone="Europe/Bucharest", summary_provider=provider)

    def test_fresh_binding_summarizes_all_children_without_changing_time_or_source(self):
        before = copy.deepcopy(self.fixture.sources)
        captured = []
        def provider(activities):
            captured.extend(copy.deepcopy(activities))
            return self.summaries(activities)
        self.prepare(provider)
        document = json.loads(self.path.read_text())
        parent = document["package"]["activities"][0]
        self.assertEqual(self.description, parent["description"])
        self.assertEqual(154, parent["duration_seconds"])
        self.assertEqual(self.fixture.descriptions,
                         [item["description"] for item in captured[0]["prepared_descriptions"]])
        self.assertEqual(captured[0]["child_review_ids"], parent["child_review_ids"])
        self.assertEqual(captured[0]["review_id"], parent["review_id"])
        self.assertEqual(before, json.loads((self.fixture.run / "proposals.json").read_text()))
        projection = prepared.project(path=self.path, source_dir=self.fixture.run,
            proposals=before, run_id="source-run", spreadsheet_id="sheet",
            sheet_title="August 2026 review", project_allowlist={"abc123": "Example"})
        self.assertEqual(self.description, projection["rows"][0][8])
        self.assertEqual(154 / 60, projection["rows"][0][3])

    def test_replay_uses_saved_summary_without_provider_or_byte_changes(self):
        self.prepare(self.summaries)
        before = self.path.read_bytes()
        def forbidden(_):
            self.fail("immutable replay must not invoke inference")
        self.prepare(forbidden)
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(self.description, prepared.load(self.path)[0]["package"]["activities"][0]["description"])

    def test_incomplete_or_wrong_membership_never_writes_raw_concatenation(self):
        for provider in (lambda _: {}, lambda groups: {groups[0]["review_id"]: {
                "description": self.description, "child_review_ids": []}}):
            with self.subTest(provider=provider), self.assertRaises(ValueError):
                self.prepare(provider)
            self.assertFalse(self.path.exists())

    def test_cycle_fresh_delivery_uses_summary_for_publication_and_receipt(self):
        with mock.patch.object(cycle, "_summarize_review_groups", side_effect=lambda groups, _: self.summaries(groups),
                               create=True):
            config, outcome = self.cycle()
        self.assertEqual("complete", outcome["status"])
        document = prepared.load(Path(config["review_batch_publication"]))[0]
        self.assertEqual(self.description, document["package"]["activities"][0]["description"])
        receipts = cycle._expected_publication_receipts(config, self.source_record(),
            sheet_title="August 2026 review", publication_profile=None)
        self.assertEqual(outcome["binding"], receipts[0]["review_batches"]["binding"])
        with mock.patch.object(cycle, "_summarize_review_groups", side_effect=AssertionError("offline replay"), create=True):
            self.assertEqual((config, outcome), self.cycle())

    def source_record(self):
        return {"run_dir": str(self.fixture.run), "run_id": "source-run"}

    def cycle(self):
        return cycle._prepare_new_publication({"spreadsheet_id": "sheet", "timezone": "Europe/Bucharest",
                "cache": str(self.fixture.root / "shared-semantic-cache.jsonl")},
            self.source_record(), sheet_title="August 2026 review", state_dir=self.fixture.root / "state")

    def test_cycle_summary_failure_preserves_source_and_refuses_raw_fallback(self):
        with mock.patch.object(cycle, "_summarize_review_groups", side_effect=ValueError("private detail"), create=True):
            config, outcome = self.cycle()
        self.assertEqual({"status": "incomplete", "reason": "editorial_authority_unavailable"}, outcome)
        self.assertNotIn("review_batch_publication", config)
        self.assertFalse((self.fixture.root / "state" / "prepared-review-publications" / "source-run.json").exists())

    def test_no_distinct_group_needs_no_summary_provider(self):
        self.fixture.analysis["activities"][1]["rendered_description"] = self.fixture.descriptions[0]
        self.fixture.write(self.fixture.run / "semantic-analysis.json", self.fixture.analysis)
        def forbidden(_):
            self.fail("identical wording needs no model")
        self.prepare(forbidden)
        self.assertEqual("complete", prepared.load(self.path)[0]["preparation_outcome"]["status"])

    def test_real_cycle_producer_freezes_independently_reviewed_summary_and_reuses_cache(self):
        endpoint = semantic.AnalyzerEndpoint("fixture", "https://fixture.invalid/v1/chat/completions",
            semantic.DEFAULT_PRIMARY_MODEL, revision=semantic.DEFAULT_PRIMARY_REVISION)
        requests = []
        def transport(_endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            requests.append(payload)
            rows = []
            for group in payload["groups"]:
                row = {"group_ref": group["group_ref"],
                       "child_refs": [child["child_ref"] for child in group["children"]]}
                if payload["stage"] == "review":
                    row.update(approved=True, covers_all_children=True, no_invented_claims=True,
                               preserves_status=True, feedback="")
                else:
                    row["description"] = self.description
                rows.append(row)
            return {"choices": [{"message": {"content": json.dumps({"groups": rows})}}]}
        with mock.patch.object(cycle, "_summarize_review_groups", self.real_summarize), \
                mock.patch.object(semantic.AnalyzerEndpoint, "from_env", return_value=endpoint), \
                mock.patch.object(semantic, "http_transport", side_effect=transport):
            config, outcome = self.cycle()
        self.assertEqual("complete", outcome["status"])
        self.assertEqual(["generate", "review"], [request["stage"] for request in requests])
        for request in requests:
            self.assertEqual(self.fixture.descriptions,
                [child["description"] for child in request["groups"][0]["children"]])
        document = prepared.load(Path(config["review_batch_publication"]))[0]
        self.assertEqual(self.description, document["package"]["activities"][0]["description"])
        self.assertTrue((self.fixture.root / "shared-semantic-cache.jsonl").exists())
        self.assertFalse((self.fixture.root / "state" / "analyzer-cache-v2.jsonl").exists())
        with mock.patch.object(cycle, "_summarize_review_groups", self.real_summarize), \
                mock.patch.object(semantic.AnalyzerEndpoint, "from_env", side_effect=AssertionError("offline")):
            self.assertEqual((config, outcome), self.cycle())


if __name__ == "__main__":
    unittest.main()
