"""Fresh malformed wording gets one repair, never weaker membership checks."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from scripts import clockify_review_group_summary as summary
from scripts import semantic_analyzer as semantic
import test_automatic_review_group_summary as fixtures


GOOD = "SC — Verified receipts and planned reconciliation repair while deployment remains blocked."


class StructuralWordingRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "cache.jsonl"
        self.cache = semantic.AnalyzerResponseCache(self.path)
        self.endpoint = semantic.AnalyzerEndpoint("fixture", "https://fixture.invalid",
            semantic.DEFAULT_PRIMARY_MODEL, revision=semantic.DEFAULT_PRIMARY_REVISION)
        self.requests = []

    def transport(self, first, repaired=GOOD, mutate=None):
        def send(endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            self.requests.append(copy.deepcopy(payload))
            rows = []
            for group in payload["groups"]:
                row = {"group_ref": group["group_ref"],
                       "child_refs": [child["child_ref"] for child in group["children"]]}
                if payload["stage"] == "review":
                    row.update(approved=True, covers_all_children=True,
                               no_invented_claims=True, preserves_status=True, feedback="")
                else:
                    row["description"] = first if payload["stage"] == "generate" else repaired
                rows.append(row)
            response = {"groups": rows}
            if mutate:
                mutate(payload, response)
            return {"choices": [{"message": {"content": json.dumps(response)}}]}
        return send

    def run_summary(self, transport, activities=None):
        return summary.summarize_groups(activities or [fixtures.group()], cache=self.cache,
                                        endpoint=self.endpoint, transport=transport)

    def test_malformed_candidate_gets_one_repair_then_full_child_review(self):
        malformed = ["XX — Rejected private candidate.", "SC — Rejected\nprivate candidate.",
                     "SC — " + "x" * 1000, " SC — Rejected private candidate.",
                     "SC — Rejected — private candidate.", None, 12, {"bad": "private candidate"}]
        activity = fixtures.group(descriptions=["SC — Completed receipt verification.",
            "SC — Planned reconciliation repair.", "SC — Deployment remains blocked."])
        before = copy.deepcopy(activity)
        for index, bad in enumerate(malformed):
            with self.subTest(candidate=index):
                self.requests.clear()
                self.path = Path(self.temp.name) / f"cache-{index}.jsonl"
                self.cache = semantic.AnalyzerResponseCache(self.path)
                result = self.run_summary(self.transport(bad), [activity])
                self.assertEqual({activity["review_id"]: {
                    "child_review_ids": activity["child_review_ids"], "description": GOOD}}, result)
                self.assertEqual(before, activity)
                self.assertEqual(["generate", "repair", "review"], [r["stage"] for r in self.requests])
                for request in self.requests[1:]:
                    self.assertEqual(["C1", "C2", "C3"],
                        [c["child_ref"] for c in request["groups"][0]["children"]])
                    self.assertEqual("SC — Deployment remains blocked.",
                        request["groups"][0]["children"][-1]["description"])
                self.assertIn("feedback", self.requests[1]["groups"][0])
                self.assertNotIn("feedback", self.requests[-1]["groups"][0])
                self.assertEqual(GOOD, self.requests[-1]["groups"][0]["summary"])
                records = [json.loads(line) for line in self.path.read_text().splitlines()]
                self.assertEqual(2, len(records), "invalid generation must not be cached")
                self.assertNotIn("private candidate", json.dumps(records))

    def test_persistently_malformed_repair_stops_after_two_calls_without_cache(self):
        bad = "XX — Rejected private candidate."
        with self.assertRaises(summary.GroupSummaryError) as caught:
            self.run_summary(self.transport(bad, repaired=bad))
        self.assertEqual(["generate", "repair"], [r["stage"] for r in self.requests])
        self.assertNotIn("private candidate", str(caught.exception))
        self.assertFalse(self.path.exists())

    def test_invalid_wording_never_masks_later_alias_or_membership_failure(self):
        changes = [lambda row: row.update(group_ref="G999"),
                   lambda row: row.update(child_refs=["C1"]),
                   lambda row: row.update(child_refs=["C1", "C999"]),
                   lambda row: row.update(child_refs=["C2", "C1"])]
        for change in changes:
            with self.subTest(change=change):
                self.requests.clear()
                def tamper(payload, response):
                    change(response["groups"][-1])
                with self.assertRaises(summary.GroupSummaryError):
                    self.run_summary(self.transport("XX — Invalid wording.", mutate=tamper),
                                     [fixtures.group(1), fixtures.group(2)])
                self.assertEqual(["generate"], [r["stage"] for r in self.requests])
                self.assertFalse(self.path.exists())

    def test_malformed_cached_candidate_is_not_reclassified_as_fresh_wording(self):
        activity = fixtures.group()
        projected, _ = summary._project([activity])
        self.cache.store_accepted(self.endpoint, summary._body(projected, self.endpoint, "generate"),
            response={"groups": [{"group_ref": "G1", "child_refs": ["C1", "C2"],
                                   "description": "XX — Invalid cached wording."}]})
        before = self.path.read_bytes()
        with self.assertRaises(summary.GroupSummaryError):
            self.run_summary(self.transport(GOOD), [activity])
        self.assertEqual([], self.requests)
        self.assertEqual(before, self.path.read_bytes())


if __name__ == "__main__":
    unittest.main()
