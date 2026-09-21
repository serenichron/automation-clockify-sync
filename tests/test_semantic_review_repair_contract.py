import copy
import json
from pathlib import Path
import tempfile
import unittest

from scripts import semantic_analyzer as semantic
from test_semantic_analyzer import event, provider_response


class RepairContractTests(unittest.TestCase):
    events = [event("ev-1")]
    candidate = {"activities": [], "exceptions": [], "omissions": []}
    taxonomy = [{"project_name": "Serenichron Level 2", "prefix": "SC", "tag_names": ["Processes"]}]
    endpoint = semantic.AnalyzerEndpoint("primary", "http://fixture", "flash-fixture")
    code = "contract_rejected_invalid_evidence_ids"

    def review(self, transport, cache=None):
        return semantic._call_semantic_review(
            self.endpoint, self.events, candidate=self.candidate, taxonomy=self.taxonomy,
            tier="primary", transport=transport, known_evidence_ids={"ev-1"},
            evidence_time_spans={"ev-1": {"start": "2026-07-10 10:00", "end": "2026-07-10 10:10"}},
            cache=cache, before_transport=None, cancelled=None,
        )

    def body(self, **kwargs):
        return semantic._review_body(
            self.events, candidate=self.candidate, taxonomy=self.taxonomy,
            model=self.endpoint.model, **kwargs,
        )

    def test_repair_example_is_consumable_by_actual_provider_contract(self):
        calls = []
        def transport(_endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            calls.append(payload)
            if len(calls) == 1:
                response = provider_response(payload)
                response["activities"][0]["evidence_partitions"][0]["bundle_ref"] = "b-9999"
                return response
            contract = payload["repair_response_contract"]
            activity = copy.deepcopy(contract["activity_example"])
            activity["evidence_partitions"] = [{"bundle_ref": "b-0001", "member_ranges": [[1, 1]]}]
            activity["evidence_spans"] = [{"start": "2026-07-10 10:00", "end": "2026-07-10 10:10"}]
            return {"activities": [activity], "exceptions": [], "omissions": []}
        result = self.review(transport)
        self.assertEqual(2, len(calls))
        self.assertEqual(["ev-1"], result["activities"][0]["evidence_ids"])
        self.assertEqual([], result["exceptions"])

    def test_initial_review_body_is_identical_with_or_without_repair_addendum(self):
        self.assertEqual(self.body(), self.body(include_repair_contract=False))

    def test_old_accepted_repair_cache_is_reused_without_transport_or_cache_rewrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.jsonl"
            cache = semantic.AnalyzerResponseCache(path)
            initial = self.body()
            old = self.body(repair_failure_code=self.code, include_repair_contract=False)
            cache.store_rejected(self.endpoint, initial, failure_code=self.code)
            cache.store_accepted(self.endpoint, old, provider_response(json.loads(old["messages"][1]["content"])))
            before = path.read_bytes()
            def forbidden(*_args):
                self.fail("accepted historical repair must not call transport")
            result = self.review(forbidden, cache)
            self.assertEqual(["ev-1"], result["activities"][0]["evidence_ids"])
            self.assertEqual(before, path.read_bytes())

    def test_rejected_legacy_repair_gets_one_new_request_then_cached_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = semantic.AnalyzerResponseCache(Path(tmp) / "cache.jsonl")
            cache.store_rejected(self.endpoint, self.body(), failure_code=self.code)
            cache.store_rejected(self.endpoint, self.body(repair_failure_code=self.code, include_repair_contract=False), failure_code=self.code)
            calls = []
            def transport(_endpoint, body):
                payload = json.loads(body["messages"][1]["content"])
                calls.append(payload)
                self.assertIn("repair_response_contract", payload)
                return provider_response(payload)
            first = self.review(transport, cache)
            second = self.review(transport, cache)
            self.assertEqual(1, len(calls))
            self.assertEqual(first["activities"], second["activities"])

    def test_bad_coverage_still_fails_after_bounded_repairs_with_sanitized_reason(self):
        for corruption in ("unknown", "duplicate", "omitted"):
            with self.subTest(corruption=corruption):
                calls = []
                def transport(_endpoint, body):
                    calls.append(body)
                    response = provider_response(json.loads(body["messages"][1]["content"]))
                    if corruption == "unknown":
                        response["activities"][0]["evidence_partitions"][0]["bundle_ref"] = "b-9999"
                    elif corruption == "duplicate":
                        response["activities"].append(copy.deepcopy(response["activities"][0]))
                    else:
                        response["activities"] = []
                    return response
                result = self.review(transport)
                self.assertEqual(3, len(calls))
                self.assertEqual([], result["activities"])
                self.assertIn("contract_rejected_", result["exceptions"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
