"""Automatic summaries use all reviewed children and independent semantic review."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import semantic_analyzer as semantic
try:
    from scripts import clockify_review_group_summary as summary
except ImportError:
    summary = None


def group(number=1, descriptions=None):
    descriptions = descriptions or ["SC — Completed receipt verification.",
                                    "SC — Planned reconciliation repair; deployment remains blocked."]
    ids = [f"wka-{number:020d}{index:04d}-s01" for index in range(len(descriptions))]
    return {"review_id": f"pvi-{number:024d}", "child_review_ids": ids,
            "prepared_descriptions": [{"review_id": identity, "description": text}
                                      for identity, text in zip(ids, descriptions)],
            "client_project": "Serenichron", "description": "; ".join(descriptions),
            "evidence_ids": ["private-evidence-identifier"],
            "allocation_segments": [{"start": "private-timestamp", "duration_seconds": 211}],
            "private_source": "raw private source text"}


class FixtureTransport:
    def __init__(self, change=None):
        self.requests = []
        self.change = change

    def __call__(self, endpoint, body):
        payload = json.loads(body["messages"][1]["content"])
        self.requests.append(copy.deepcopy(payload))
        rows = []
        for item in payload["groups"]:
            row = {"group_ref": item["group_ref"],
                   "child_refs": [child["child_ref"] for child in item["children"]]}
            if payload["stage"] == "review":
                row.update(approved=True, covers_all_children=True, no_invented_claims=True,
                           preserves_status=True, feedback="")
            else:
                row["description"] = "SC — Verified receipts and planned reconciliation repair while deployment remains blocked."
            rows.append(row)
        response = {"groups": rows}
        if self.change:
            self.change(payload, response)
        return {"choices": [{"message": {"content": json.dumps(response)}}]}


class AutomaticGroupSummaryTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(summary, "automatic grouped summary producer is missing")
        self.temp = tempfile.TemporaryDirectory(prefix="group-summary-", dir="/tmp")
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "analyzer-cache-v2.jsonl"
        self.cache = semantic.AnalyzerResponseCache(self.path)
        self.endpoint = semantic.AnalyzerEndpoint("fixture", "https://fixture.invalid/v1/chat/completions",
            semantic.DEFAULT_PRIMARY_MODEL, revision=semantic.DEFAULT_PRIMARY_REVISION)

    def summarize(self, groups, transport):
        return summary.summarize_groups(groups, cache=self.cache, endpoint=self.endpoint, transport=transport)

    def test_all_children_reviewed_input_unchanged_and_private_fields_never_projected(self):
        value = group(descriptions=["SC — Completed receipt verification.", "SC — Planned repair.",
                                    "SC — Deployment remains blocked."])
        before = copy.deepcopy(value)
        transport = FixtureTransport()
        result = self.summarize([value], transport)
        self.assertEqual(before, value)
        self.assertEqual({value["review_id"]: {"child_review_ids": value["child_review_ids"],
            "description": "SC — Verified receipts and planned reconciliation repair while deployment remains blocked."}}, result)
        self.assertEqual(["generate", "review"], [request["stage"] for request in transport.requests])
        for request in transport.requests:
            self.assertEqual(["G1"], [item["group_ref"] for item in request["groups"]])
            self.assertEqual(["C1", "C2", "C3"], [item["child_ref"] for item in request["groups"][0]["children"]])
            self.assertEqual([item["description"] for item in value["prepared_descriptions"]],
                             [item["description"] for item in request["groups"][0]["children"]])
            serialized = json.dumps(request)
            for private in [value["review_id"], *value["child_review_ids"], "private-evidence-identifier",
                            "private-timestamp", "raw private source text", "duration_seconds"]:
                self.assertNotIn(private, serialized)
            self.assertIn("prompt_version", request)
            self.assertEqual("Serenichron", request["groups"][0]["project"])
            self.assertEqual("SC", request["groups"][0]["prefix"])
        self.assertIn("summary", transport.requests[1]["groups"][0])

    def test_cache_replay_uses_no_duplicate_calls_and_new_child_invalidates_cache(self):
        value = group()
        transport = FixtureTransport()
        first = self.summarize([value], transport)
        self.cache = semantic.AnalyzerResponseCache(self.path)
        self.assertEqual(first, self.summarize([value], transport))
        self.assertEqual(2, len(transport.requests))
        value["prepared_descriptions"][-1]["description"] = "SC — Planned documentation; publication blocked."
        self.summarize([value], transport)
        self.assertEqual(4, len(transport.requests))

    def test_mixed_child_prefixes_keep_first_native_prefix_and_all_reviewed_work(self):
        value = group(descriptions=["SC — Completed receipt verification.",
                                    "ES — Planned reconciliation repair; deployment remains blocked."])
        before = copy.deepcopy(value)
        transport = FixtureTransport()
        try:
            result = self.summarize([value], transport)
        except ValueError as error:
            self.fail(f"native grouping does not require matching presentation prefixes: {error}")
        self.assertEqual(before, value)
        self.assertTrue(result[value["review_id"]]["description"].startswith("SC — "))
        self.assertEqual(["generate", "review"], [request["stage"] for request in transport.requests])
        for request in transport.requests:
            projected = request["groups"][0]
            self.assertEqual("SC", projected["prefix"])
            self.assertEqual(["SC — Completed receipt verification.",
                              "ES — Planned reconciliation repair; deployment remains blocked."],
                             [child["description"] for child in projected["children"]])

    def test_single_or_identical_wording_requires_no_endpoint_or_cache_write(self):
        groups = [group(1, ["SC — Verified receipt."]), group(2, ["SC — Verified receipt."] * 3)]
        self.assertEqual({}, summary.summarize_groups(groups, cache=self.cache))
        self.assertFalse(self.path.exists())

    def test_batches_all_groups_without_sending_healthy_single_wording_groups(self):
        groups = [group(index) for index in range(1, 44)]
        groups.insert(1, group(100, ["SC — Verified receipt."]))
        groups.append(group(101, ["SC — Verified receipt."] * 2))
        transport = FixtureTransport()
        result = self.summarize(groups, transport)
        self.assertEqual(43, len(result))
        self.assertEqual([20, 20, 3], [len(request["groups"]) for request in transport.requests
                                      if request["stage"] == "generate"])
        self.assertEqual(6, len(transport.requests))

    def test_first_child_only_candidate_is_rejected_then_repaired_once_using_all_children(self):
        def reject_first(payload, response):
            if payload["stage"] == "generate":
                response["groups"][0]["description"] = "SC — Completed receipt verification."
            if payload["stage"] == "review" and payload["groups"][0]["summary"] == "SC — Completed receipt verification.":
                response["groups"][0].update(approved=False, covers_all_children=False,
                    feedback="Include the planned repair and blocked deployment.")
        transport = FixtureTransport(reject_first)
        result = self.summarize([group()], transport)
        self.assertIn("deployment remains blocked", next(iter(result.values()))["description"])
        self.assertEqual(["generate", "review", "repair", "review"], [item["stage"] for item in transport.requests])
        self.assertEqual(2, len(transport.requests[2]["groups"][0]["children"]))
        self.assertIn("feedback", transport.requests[2]["groups"][0])
        self.summarize([group()], transport)
        self.assertEqual(4, len(transport.requests))

    def test_persistent_rejection_never_returns_concat_or_first_child_fallback(self):
        def reject(payload, response):
            if payload["stage"] == "review":
                for item in response["groups"]:
                    item.update(approved=False, covers_all_children=False, feedback="Missing reviewed work.")
        transport = FixtureTransport(reject)
        with self.assertRaises(ValueError):
            self.summarize([group()], transport)
        # Identical rejected repair wording reuses its already sealed independent
        # review rather than issuing an identical provider request a second time.
        self.assertEqual(["generate", "review", "repair"], [item["stage"] for item in transport.requests])

    def test_targeted_repair_excludes_already_accepted_groups(self):
        def reject_one(payload, response):
            if payload["stage"] == "review" and len(payload["groups"]) == 2:
                response["groups"][1].update(approved=False, covers_all_children=False, feedback="Include blocked work.")
        transport = FixtureTransport(reject_one)
        self.assertEqual(2, len(self.summarize([group(1), group(2)], transport)))
        self.assertEqual([2, 2, 1, 1], [len(item["groups"]) for item in transport.requests])

    def test_scope_stale_membership_and_fake_aliases_fail_closed_before_cache(self):
        changes = [lambda row: row.update(group_ref="G999"),
                   lambda row: row.update(child_refs=["C999"]),
                   lambda row: row.update(child_refs=["C2", "C1"]),
                   lambda row: row.update(child_refs=["C1"])]
        for change in changes:
            with self.subTest(change=change):
                transport = FixtureTransport(lambda payload, response: change(response["groups"][0]))
                with self.assertRaises(ValueError):
                    self.summarize([group()], transport)
                self.assertFalse(self.path.exists())

    def test_bad_review_membership_is_not_cached_or_accepted(self):
        def tamper(payload, response):
            if payload["stage"] == "review":
                response["groups"][0]["child_refs"] = ["C1"]
        with self.assertRaises(ValueError):
            self.summarize([group()], FixtureTransport(tamper))
        self.assertEqual(1, len(self.path.read_text().splitlines()))

    def test_wrong_current_route_rejected_even_with_injected_transport(self):
        self.endpoint = semantic.AnalyzerEndpoint("retired", "https://fixture.invalid", "deepseek-v4-flash:cloud",
            revision="6ca9e29c41ded618e527ee40e305ed5e4d8319b571d5b6695a30e1df65f103cc")
        transport = FixtureTransport()
        with self.assertRaises(ValueError):
            self.summarize([group()], transport)
        self.assertEqual([], transport.requests)

    def test_input_mismatched_children_rejected_without_network(self):
        value = group()
        value["child_review_ids"].reverse()
        with self.assertRaises(ValueError):
            self.summarize([value], FixtureTransport())

    def test_transport_failure_is_bounded_and_does_not_echo_private_text(self):
        def fail(endpoint, body):
            raise semantic.AnalyzerTransportError("raw private source text")
        with self.assertRaises(ValueError) as caught:
            self.summarize([group()], fail)
        self.assertNotIn("raw private source text", str(caught.exception))
        self.assertFalse(self.path.exists())

    def test_real_native_package_children_keep_their_segment_suffixes(self):
        import test_native_review_batches as native_fixtures
        fixture = native_fixtures.NativeReviewBatchTests()
        fixture.setUp()
        native_group = fixture.package["activities"][0]
        result = self.summarize([native_group], FixtureTransport())
        self.assertEqual(native_group["child_review_ids"], result[native_group["review_id"]]["child_review_ids"])

    def test_native_binding_changes_invalidate_cache_even_with_same_reviewed_wording(self):
        value = group()
        transport = FixtureTransport()
        self.summarize([value], transport)
        value["allocation_segments"][0]["duration_seconds"] += 1
        self.summarize([value], transport)
        self.assertEqual(4, len(transport.requests))

    def test_missing_endpoint_and_inconsistent_review_approval_fail_closed(self):
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ValueError):
                summary.summarize_groups([group()], cache=self.cache, transport=FixtureTransport())
        def inconsistent(payload, response):
            if payload["stage"] == "review":
                response["groups"][0]["no_invented_claims"] = False
        with self.assertRaises(ValueError):
            self.summarize([group()], FixtureTransport(inconsistent))

    def test_environment_uses_native_primary_endpoint_without_fallback(self):
        transport = FixtureTransport()
        with mock.patch.dict("os.environ", {"CLOCKIFY_ANALYZER_PRIMARY_URL": self.endpoint.url,
                "CLOCKIFY_ANALYZER_PRIMARY_MODEL": semantic.DEFAULT_PRIMARY_MODEL,
                "CLOCKIFY_ANALYZER_PRIMARY_REVISION": semantic.DEFAULT_PRIMARY_REVISION,
                "CLOCKIFY_ANALYZER_FALLBACK_URL": "https://forbidden.invalid"}, clear=True):
            result = summary.summarize_groups([group()], cache=self.cache, transport=transport)
        self.assertEqual(1, len(result))
        self.assertEqual(2, len(transport.requests))

    def test_large_native_group_reviews_the_late_child_without_total_child_cap(self):
        value = group(descriptions=[f"SC — Reviewed distinct included task {index}." for index in range(34)])
        transport = FixtureTransport()
        result = self.summarize([value], transport)
        self.assertEqual(34, len(result[value["review_id"]]["child_review_ids"]))
        self.assertEqual("SC — Reviewed distinct included task 33.",
                         transport.requests[1]["groups"][0]["children"][-1]["description"])

    def test_native_stable_id_inside_reviewed_prose_never_reaches_transport(self):
        value = group()
        value["prepared_descriptions"][0]["description"] += " " + value["child_review_ids"][1]
        transport = FixtureTransport()
        with self.assertRaises(ValueError):
            self.summarize([value], transport)
        self.assertEqual([], transport.requests)

    def test_oversized_single_group_fails_without_truncation_or_transport(self):
        value = group(descriptions=["SC — " + "A" * 260000, "SC — Planned followup."])
        transport = FixtureTransport()
        with self.assertRaises(ValueError):
            self.summarize([value], transport)
        self.assertEqual([], transport.requests)


if __name__ == "__main__":
    unittest.main()
