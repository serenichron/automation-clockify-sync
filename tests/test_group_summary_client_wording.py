import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts import clockify_review_group_summary as summary
from scripts import semantic_analyzer as semantic
import test_automatic_review_group_summary as fixtures
from test_client_wording_execution_metadata import TELEMETRY


GOOD = "SC — Restored receipt comparison and clarified reconciliation recovery readiness."


class ClientWordingTests(unittest.TestCase):
    def endpoint(self):
        return semantic.AnalyzerEndpoint("fixture", "https://fixture.invalid",
            semantic.DEFAULT_PRIMARY_MODEL, revision=semantic.DEFAULT_PRIMARY_REVISION)

    def invoke(self, root, activities, first, repaired=GOOD):
        requests = []

        def transport(endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            requests.append(copy.deepcopy(payload))
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
            return {"choices": [{"message": {"content": json.dumps({"groups": rows})}}]}

        result = summary.summarize_groups(activities,
            cache=semantic.AnalyzerResponseCache(root / "cache.jsonl"),
            endpoint=self.endpoint(), transport=transport)
        return result, requests

    def test_internal_ids_hashes_and_execution_chatter_get_one_targeted_repair(self):
        bad = ["SC — Restored SER-1801 receipt comparison.",
               "SC — Reconciled Jira task TST-42 with receipt verification.",
               "SC — Verified receipt changes at commit a8bf012.",
               "SC — Verified receipt comparison with 47 tests passing.",
               "SC — Restored receipt comparison after build passed."]
        for wording in bad:
            with self.subTest(wording=wording), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                activity = fixtures.group()
                result, requests = self.invoke(root, [activity], wording)
                self.assertEqual(GOOD, result[activity["review_id"]]["description"])
                self.assertEqual(["generate", "repair", "review"], [r["stage"] for r in requests])
                self.assertEqual(["C1", "C2"], requests[-1]["groups"][0]["child_refs"] if "child_refs" in requests[-1]["groups"][0]
                                 else [c["child_ref"] for c in requests[-1]["groups"][0]["children"]])
                records = [json.loads(line) for line in (root / "cache.jsonl").read_text().splitlines()]
                self.assertNotIn(wording, json.dumps([r.get("response") for r in records]))

    def test_literal_child_concatenation_is_repaired_without_dropping_children(self):
        activity = fixtures.group(descriptions=["SC — Verified receipts.", "SC — Restored comparison."])
        with tempfile.TemporaryDirectory() as directory:
            result, requests = self.invoke(Path(directory), [activity],
                "SC — Verified receipts; Restored comparison.")
        self.assertEqual(GOOD, result[activity["review_id"]]["description"])
        self.assertEqual(["generate", "repair", "review"], [r["stage"] for r in requests])
        self.assertEqual(activity["prepared_descriptions"][-1]["description"],
                         requests[-1]["groups"][0]["children"][-1]["description"])

    def test_execution_metadata_gets_one_repair_without_mutating_native_accounting(self):
        clean = "SC — Restored receipt comparison locally; deployment remains blocked."
        for wording in TELEMETRY:
            with self.subTest(wording=wording), tempfile.TemporaryDirectory() as directory:
                activity = fixtures.group()
                before = copy.deepcopy(activity)
                result, requests = self.invoke(Path(directory), [activity], wording, clean)
                self.assertEqual(clean, result[activity["review_id"]]["description"])
                self.assertEqual(["generate", "repair", "review"], [r["stage"] for r in requests])
                self.assertEqual(before, activity)
                self.assertEqual(activity["child_review_ids"], result[activity["review_id"]]["child_review_ids"])
                for request in requests:
                    self.assertEqual([row["description"] for row in activity["prepared_descriptions"]],
                                     [row["description"] for row in request["groups"][0]["children"]])
                records = [json.loads(line) for line in (Path(directory) / "cache.jsonl").read_text().splitlines()]
                self.assertNotIn(wording, json.dumps([record.get("response") for record in records]))

    def test_execution_metadata_after_repair_fails_without_caching_dirty_candidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(summary.GroupSummaryError):
                self.invoke(root, [fixtures.group()], TELEMETRY[0], TELEMETRY[1])
            records = ([json.loads(line) for line in (root / "cache.jsonl").read_text().splitlines()]
                       if (root / "cache.jsonl").exists() else [])
            self.assertEqual([], records)

    def test_single_and_same_wording_telemetry_gets_reviewed_overlay_without_source_repair(self):
        clean = "SC — Restored receipt comparison locally; deployment remains blocked."
        for count in (1, 2):
            with self.subTest(children=count), tempfile.TemporaryDirectory() as directory:
                activity = fixtures.group(descriptions=[TELEMETRY[0]] * count)
                before = copy.deepcopy(activity)
                self.assertTrue(summary.needs_summary([activity]))
                result, requests = self.invoke(Path(directory), [activity], clean)
                self.assertEqual(clean, result[activity["review_id"]]["description"])
                self.assertEqual(["generate", "review"], [r["stage"] for r in requests])
                self.assertEqual(before, activity)

    def test_v2_prompt_bodies_remain_exact_and_fresh_calls_do_not_reuse_dirty_decisions(self):
        groups = [{"group_ref": "G1", "binding_sha256": "a" * 64, "project": "Serenichron", "prefix": "SC",
                   "children": [{"child_ref": "C1", "description": "SC — Verified receipts."},
                                {"child_ref": "C2", "description": "SC — Restored comparison."}]}]
        expected = {"generate": "9906c07000c3ab91c2aef3cad4bc10c4cf6360ec088a6d2aeb3083665db4c909",
                    "review": "c9e2b03d14546431b452b7fd0be1e2b0e2785ab266e4e51e3c4a88effbbdc1ef",
                    "repair": "267db42398d9f7c5e1a02bd31a7030fb93186db46a43964c59d58e60af24c865"}
        for stage, digest in expected.items():
            self.assertEqual(digest, hashlib.sha256(semantic.canonical_json(
                summary._body(groups, self.endpoint(), stage, contract_version=2)).encode()).hexdigest())
        activity = fixtures.group()
        projected, _ = summary._project([activity])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = semantic.AnalyzerResponseCache(root / "cache.jsonl")
            cache.store_accepted(self.endpoint(), summary._body(projected, self.endpoint(), "generate", contract_version=2),
                response={"groups": [{"group_ref": "G1", "child_refs": ["C1", "C2"], "description": TELEMETRY[0]}]})
            before = (root / "cache.jsonl").read_bytes()
            result, requests = self.invoke(root, [activity], GOOD)
            self.assertEqual(GOOD, result[activity["review_id"]]["description"])
            self.assertEqual(["generate", "review"], [r["stage"] for r in requests])
            self.assertTrue((root / "cache.jsonl").read_bytes().startswith(before))
            self.assertEqual("clockify-review-group-summary-v3", requests[0]["prompt_version"])

    def test_bad_second_candidate_fails_after_two_generation_attempts(self):
        calls = []
        def transport(endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            calls.append(payload["stage"])
            return {"choices": [{"message": {"content": json.dumps({"groups": [{
                "group_ref": "G1", "child_refs": ["C1", "C2"],
                "description": "SC — Verified SER-1801 reconciliation changes."}]})}}]}
        with tempfile.TemporaryDirectory() as directory, self.assertRaises(ValueError):
            summary.summarize_groups([fixtures.group()], cache=semantic.AnalyzerResponseCache(Path(directory) / "cache"),
                                     endpoint=self.endpoint(), transport=transport)
        self.assertEqual(["generate", "repair"], calls)

    def test_unsafe_singleton_is_rewritten_but_material_business_quantities_survive(self):
        activity = fixtures.group(descriptions=["SC — Expanded SER-1802 onboarding coverage."])
        clean = "SC — Expanded onboarding to 20 markets and improved conversion by 32 percent."
        before = copy.deepcopy(activity)
        with tempfile.TemporaryDirectory() as directory:
            result, requests = self.invoke(Path(directory), [activity], clean)
        self.assertEqual(clean, result[activity["review_id"]]["description"])
        self.assertEqual(before, activity)
        self.assertEqual(["generate", "review"], [r["stage"] for r in requests])

    def test_v1_request_bodies_remain_exact_and_fresh_calls_do_not_reuse_them(self):
        groups = [{"group_ref": "G1", "binding_sha256": "a" * 64, "project": "Serenichron", "prefix": "SC",
                   "children": [{"child_ref": "C1", "description": "SC — Verified receipts."},
                                {"child_ref": "C2", "description": "SC — Restored comparison."}]}]
        expected = {"generate": "b050d275c154b9e592ac1139694233154c258fd02ff4eea413aed4d7d4c1f3ee",
                    "review": "30463470f98d37b6f43c3e960f78397787c660d54f02361fba8ec91e4c5c1dc2",
                    "repair": "5dec931666664e70ff5d7f71c6cc51125b12bf21f1942741ec27080c50443090"}
        for stage, digest in expected.items():
            body = summary._body(groups, self.endpoint(), stage, contract_version=1)
            self.assertEqual(digest, hashlib.sha256(semantic.canonical_json(body).encode()).hexdigest())
        activity = fixtures.group()
        projected, _ = summary._project([activity])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = semantic.AnalyzerResponseCache(root / "cache.jsonl")
            legacy_body = summary._body(projected, self.endpoint(), "generate", contract_version=1)
            cache.store_accepted(self.endpoint(), legacy_body, response={"groups": [{
                "group_ref": "G1", "child_refs": ["C1", "C2"], "description": "SC — Verified SER-1801 changes."}]})
            original = (root / "cache.jsonl").read_bytes()
            result, requests = self.invoke(root, [activity], GOOD)
            self.assertEqual(GOOD, result[activity["review_id"]]["description"])
            self.assertEqual(["generate", "review"], [r["stage"] for r in requests])
            self.assertTrue((root / "cache.jsonl").read_bytes().startswith(original))
            self.assertEqual("clockify-review-group-summary-v3", requests[0]["prompt_version"])


if __name__ == "__main__":
    unittest.main()
