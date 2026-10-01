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

    def test_fresh_duplicate_review_records_only_private_partition_diagnostic(self):
        events = [event("ev-1"), event("ev-2")]
        secret = "private-source-prose-DO-NOT-STORE"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.jsonl"
            cache = semantic.AnalyzerResponseCache(path, record_review_diagnostics=True)

            def transport(_endpoint, body):
                payload = json.loads(body["messages"][1]["content"])
                response = provider_response(payload)
                repeated = copy.deepcopy(response["activities"][0])
                repeated["evidence_partitions"] = [
                    {"bundle_ref": "b-0001", "member_ranges": [[2, 2]]}
                ]
                response["activities"].append(repeated)
                response["private_note"] = secret
                return response

            result = semantic._call_semantic_review(
                self.endpoint, events, candidate=self.candidate,
                taxonomy=self.taxonomy, tier="primary", transport=transport,
                known_evidence_ids={"ev-1", "ev-2"},
                evidence_time_spans={
                    value: {"start": "2026-07-10 10:00", "end": "2026-07-10 10:10"}
                    for value in ("ev-1", "ev-2")
                },
                cache=cache, before_transport=None, cancelled=None,
            )
            self.assertEqual([], result["activities"])
            diagnostics = path.with_name(path.name + ".review-diagnostics.jsonl")
            records = [json.loads(line) for line in diagnostics.read_text().splitlines()]
            self.assertEqual(3, len(records))
            self.assertEqual(0o600, diagnostics.stat().st_mode & 0o777)
            self.assertEqual(
                {"cache_key", "body_digest", "failure_code", "review_scope",
                 "coverage_contract", "activities", "exceptions", "omissions"},
                set(records[0]),
            )
            self.assertEqual("contract_rejected_duplicate_evidence", records[0]["failure_code"])
            self.assertEqual("extraction", records[0]["review_scope"])
            self.assertEqual(
                [{"bundle_ref": "b-0001", "allowed_member_range": [1, 2]}],
                records[0]["coverage_contract"],
            )
            self.assertEqual(
                [
                    {"evidence_partitions": [{"bundle_ref": "b-0001", "member_ranges": [[1, 2]]}]},
                    {"evidence_partitions": [{"bundle_ref": "b-0001", "member_ranges": [[2, 2]]}]},
                ],
                records[0]["activities"],
            )
            self.assertNotIn(secret, diagnostics.read_text())
            self.assertNotIn("ev-1", diagnostics.read_text())
            self.assertNotIn("ev-2", diagnostics.read_text())

            sealed_diagnostics = diagnostics.read_bytes()
            replay_cache = semantic.AnalyzerResponseCache(path, record_review_diagnostics=True)
            def forbidden(*_args):
                self.fail("sealed rejection must not use transport")
            replayed = semantic._call_semantic_review(
                self.endpoint, events, candidate=self.candidate,
                taxonomy=self.taxonomy, tier="primary", transport=forbidden,
                known_evidence_ids={"ev-1", "ev-2"},
                evidence_time_spans={
                    value: {"start": "2026-07-10 10:00", "end": "2026-07-10 10:10"}
                    for value in ("ev-1", "ev-2")
                },
                cache=replay_cache, before_transport=None, cancelled=None,
            )
            self.assertEqual([], replayed["activities"])
            self.assertEqual(sealed_diagnostics, diagnostics.read_bytes())

    def test_missing_member_and_malformed_partition_are_safely_recorded(self):
        events = [event("ev-1"), event("ev-2")]
        secret = "secret=private-provider-prose"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.jsonl"
            cache = semantic.AnalyzerResponseCache(path, record_review_diagnostics=True)
            calls = 0

            def transport(_endpoint, body):
                nonlocal calls
                calls += 1
                payload = json.loads(body["messages"][1]["content"])
                response = provider_response(payload)
                partition = response["activities"][0]["evidence_partitions"][0]
                if calls == 1:
                    partition["member_ranges"] = [[1, 1]]
                else:
                    partition["bundle_ref"] = secret
                    partition["member_ranges"] = [[secret, 2]]
                response["private_note"] = secret
                return response

            result = semantic._call_semantic_review(
                self.endpoint, events, candidate=self.candidate,
                taxonomy=self.taxonomy, tier="primary", transport=transport,
                known_evidence_ids={"ev-1", "ev-2"},
                evidence_time_spans={
                    value: {"start": "2026-07-10 10:00", "end": "2026-07-10 10:10"}
                    for value in ("ev-1", "ev-2")
                },
                cache=cache, before_transport=None, cancelled=None,
            )
            self.assertEqual([], result["activities"])
            diagnostics = path.with_name(path.name + ".review-diagnostics.jsonl")
            records = [json.loads(line) for line in diagnostics.read_text().splitlines()]
            self.assertEqual("contract_rejected_omitted_evidence", records[0]["failure_code"])
            self.assertEqual(
                [{"evidence_partitions": [{"bundle_ref": "b-0001", "member_ranges": [[1, 1]]}]}],
                records[0]["activities"],
            )
            self.assertEqual(
                [{"evidence_partitions": [{"bundle_ref": None, "member_ranges": []}]}],
                records[1]["activities"],
            )
            self.assertNotIn(secret, diagnostics.read_text())

    def test_accepted_and_cache_only_reviews_never_write_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.jsonl"
            cache = semantic.AnalyzerResponseCache(path, record_review_diagnostics=True)
            diagnostics = path.with_name(path.name + ".review-diagnostics.jsonl")

            def accepted(_endpoint, body):
                return provider_response(json.loads(body["messages"][1]["content"]))

            self.review(accepted, cache)
            self.assertFalse(diagnostics.exists())

            replay_cache = semantic.AnalyzerResponseCache(path, record_review_diagnostics=True)
            def forbidden(*_args):
                self.fail("sealed review must not use transport")
            self.review(forbidden, replay_cache)
            self.assertFalse(diagnostics.exists())

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.jsonl"
            cache = semantic.AnalyzerResponseCache(path, record_review_diagnostics=True)
            for body in (
                self.body(),
                self.body(repair_failure_code="contract_rejected_duplicate_evidence", repair_attempt=1),
                self.body(repair_failure_code="contract_rejected_duplicate_evidence", repair_attempt=2),
            ):
                cache.store_rejected(
                    self.endpoint, body, failure_code="contract_rejected_duplicate_evidence"
                )
            self.review(forbidden, cache)
            self.assertFalse(path.with_name(path.name + ".review-diagnostics.jsonl").exists())

    def test_diagnostics_remain_opt_in_for_fresh_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.jsonl"
            cache = semantic.AnalyzerResponseCache(path)

            def invalid(_endpoint, body):
                response = provider_response(json.loads(body["messages"][1]["content"]))
                response["activities"].append(copy.deepcopy(response["activities"][0]))
                return response

            result = self.review(invalid, cache)
            self.assertEqual([], result["activities"])
            self.assertEqual(3, len(path.read_text().splitlines()))
            self.assertFalse(path.with_name(path.name + ".review-diagnostics.jsonl").exists())

    def test_failed_review_retry_uses_new_append_only_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.jsonl"
            cache = semantic.AnalyzerResponseCache(path)
            for body in (
                self.body(),
                self.body(repair_failure_code="contract_rejected_duplicate_evidence", repair_attempt=1),
                self.body(repair_failure_code="contract_rejected_duplicate_evidence", repair_attempt=2),
            ):
                cache.store_rejected(self.endpoint, body, failure_code="contract_rejected_duplicate_evidence")
            original = path.read_bytes()
            calls = []
            def transport(_endpoint, body):
                payload = json.loads(body["messages"][1]["content"])
                calls.append(payload)
                return provider_response(payload)
            result = semantic._call_semantic_review(
                self.endpoint, self.events, candidate=self.candidate, taxonomy=self.taxonomy,
                tier="primary", transport=transport, known_evidence_ids={"ev-1"},
                evidence_time_spans={"ev-1": {"start": "2026-07-10 10:00", "end": "2026-07-10 10:10"}},
                cache=cache, before_transport=None, cancelled=None,
                failed_review_retry_targets={("ev-1",): "contract_rejected_duplicate_evidence"},
            )
            self.assertEqual(["ev-1"], result["activities"][0]["evidence_ids"])
            self.assertEqual(1, len(calls))
            self.assertEqual({"attempt": 1, "maximum_attempts": 1,
                              "failure_code": "contract_rejected_duplicate_evidence"},
                             calls[0]["failed_review_retry"])
            self.assertEqual("contract_rejected_duplicate_evidence", calls[0]["repair_feedback"]["failure_code"])
            self.assertIn("repair_response_contract", calls[0])
            self.assertTrue(path.read_bytes().startswith(original))
            self.assertEqual(4, len(path.read_text().splitlines()))
            replay_cache = semantic.AnalyzerResponseCache(path)
            def forbidden(*_args):
                self.fail("sealed failed-review retry must replay without transport")
            replayed = semantic._call_semantic_review(
                self.endpoint, self.events, candidate=self.candidate, taxonomy=self.taxonomy,
                tier="primary", transport=forbidden, known_evidence_ids={"ev-1"},
                evidence_time_spans={"ev-1": {"start": "2026-07-10 10:00", "end": "2026-07-10 10:10"}},
                cache=replay_cache, before_transport=None, cancelled=None,
                failed_review_retry_targets={("ev-1",): "contract_rejected_duplicate_evidence"},
            )
            self.assertEqual(result["activities"], replayed["activities"])
            self.assertEqual(4, len(path.read_text().splitlines()))

    def test_retry_mode_fails_closed_on_unrelated_extraction_cache_miss(self):
        calls = []
        def transport(_endpoint, body):
            calls.append(body)
            return provider_response(json.loads(body["messages"][1]["content"]))
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(semantic.AnalyzerError, "failed-review retry forbids"):
                semantic.analyze_tiered(
                    self.events,
                    primary=semantic.AnalyzerEndpoint("primary", "http://fixture", semantic.CURRENT_LIVE_FLASH_ROUTE[0], revision=semantic.CURRENT_LIVE_FLASH_ROUTE[1]),
                    transport=transport,
                    cache=semantic.AnalyzerResponseCache(Path(tmp) / "cache.jsonl"),
                    review_taxonomy=self.taxonomy,
                    failed_review_retry_targets={("ev-1",): "contract_rejected_duplicate_evidence"},
                    max_workers=1, private_text_approved=True,
                )
        self.assertEqual([], calls)


if __name__ == "__main__":
    unittest.main()
