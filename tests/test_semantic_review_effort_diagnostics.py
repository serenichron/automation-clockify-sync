from __future__ import annotations

import json
import hashlib
import stat
import tempfile
import unittest
from pathlib import Path

from scripts import semantic_analyzer as semantic
from scripts import work_accounting_pipeline as pipeline
from test_semantic_analyzer import event, provider_members, provider_response


class SemanticReviewEffortDiagnosticsTests(unittest.TestCase):
    endpoint = semantic.AnalyzerEndpoint("primary", "http://fixture", "flash-fixture")
    marker = {
        "source_semantic_sha256": "a" * 64,
        "group_digest": "frt-" + "b" * 64,
        "subset_digest": "frt-" + "c" * 64,
        "mode": "scoped_review_v2",
    }

    def body(self, events):
        return semantic._review_body(
            events, candidate={"activities": [], "exceptions": [], "omissions": []},
            taxonomy=[], model=self.endpoint.model,
            review_scope="failed_review_scoped_recovery",
            scoped_failed_review=self.marker,
        )

    def test_private_sidecar_classifies_effort_without_values_or_prose(self):
        body = self.body([event("ev-1", content="Private evidence sentinel")])
        partition = {"bundle_ref": "b-0001", "member_ranges": [[1, 1]]}
        rows = [
            ("noise", None, "missing_or_nonobject"),
            ("planned", [], "missing_or_nonobject"),
            ("completed", {"minimum_minutes": True, "recommended_minutes": 5, "maximum_minutes": 10}, "missing_or_nonintegral_field"),
            ("completed", {"minimum_minutes": "7", "recommended_minutes": 10, "maximum_minutes": 15}, "valid"),
            ("completed", {"minimum_minutes": 7.9, "recommended_minutes": 10, "maximum_minutes": 15}, "valid"),
            ("completed", {"minimum_minutes": "not a number", "recommended_minutes": 10, "maximum_minutes": 15}, "missing_or_nonintegral_field"),
            ("completed", {"minimum_minutes": 0, "recommended_minutes": 5, "maximum_minutes": 10}, "nonpositive"),
            ("completed", {"minimum_minutes": 10, "recommended_minutes": 5, "maximum_minutes": 15}, "inverted"),
            ("Private lifecycle sentinel", {"minimum_minutes": 5, "recommended_minutes": 10, "maximum_minutes": 15}, "valid"),
        ]
        response = {
            "activities": [
                {
                    "lifecycle": lifecycle,
                    "effort": effort,
                    "evidence_partitions": [partition],
                    "action": "Private activity sentinel",
                    "evidence_ids": ["private-evidence-id"],
                }
                for lifecycle, effort, _category in rows
            ],
            "exceptions": [], "omissions": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            cache = semantic.AnalyzerResponseCache(
                Path(temporary) / "cache.jsonl", record_review_diagnostics=True,
            )
            cache.record_rejected_review(
                self.endpoint, body, failure_code="contract_rejected_invalid_effort",
                review_scope="failed_review_scoped_recovery", response=response,
            )
            sidecar = Path(temporary) / "cache.jsonl.review-diagnostics.jsonl"
            self.assertEqual(0o600, stat.S_IMODE(sidecar.stat().st_mode))
            diagnostic = json.loads(sidecar.read_text())
            self.assertEqual({
                "cache_key", "body_digest", "failure_code", "review_scope",
                "coverage_contract", "activities", "exceptions", "omissions",
            }, set(diagnostic))
            self.assertEqual([
                {
                    "row_index": index,
                    "lifecycle": lifecycle if lifecycle in semantic.LIFECYCLES else None,
                    "effort_category": category,
                    "evidence_partitions": [partition],
                }
                for index, (lifecycle, _effort, category) in enumerate(rows)
            ], diagnostic["activities"])
            serialized = sidecar.read_text()
            for forbidden in ("Private evidence sentinel", "Private activity sentinel", "Private lifecycle sentinel", "private-evidence-id", "minimum_minutes", "recommended_minutes", "maximum_minutes"):
                self.assertNotIn(forbidden, serialized)

    def test_invalid_effort_still_rejects_whole_review_and_redacts_cache(self):
        events = [event("ev-1"), event("ev-2")]
        with tempfile.TemporaryDirectory() as temporary:
            cache = semantic.AnalyzerResponseCache(
                Path(temporary) / "cache.jsonl", record_review_diagnostics=True,
            )

            def transport(_endpoint, body):
                payload = json.loads(body["messages"][1]["content"])
                members = provider_members(payload)
                valid = provider_response(payload, [members[0]])["activities"][0]
                valid["effort"] = {
                    "minimum_minutes": "10",
                    "recommended_minutes": "20",
                    "maximum_minutes": "30",
                }
                invalid = provider_response(payload, [members[1]])["activities"][0]
                invalid["effort"]["minimum_minutes"] = 0
                return {"activities": [valid, invalid], "exceptions": [], "omissions": []}

            with self.assertRaises(semantic.AnalyzerContractError):
                semantic._call_semantic_review_once(
                    self.endpoint, events,
                    candidate={"activities": [], "exceptions": [], "omissions": []},
                    taxonomy=[], tier="primary_scoped_review", transport=transport,
                    known_evidence_ids={"ev-1", "ev-2"},
                    evidence_time_spans={
                        "ev-1": {"start": "2026-07-10 10:00", "end": "2026-07-10 10:10"},
                        "ev-2": {"start": "2026-07-10 10:00", "end": "2026-07-10 10:10"},
                    }, cache=cache,
                    before_transport=None, cancelled=None,
                    review_scope="failed_review_scoped_recovery",
                    scoped_failed_review=self.marker,
                )
            cache_rows = [json.loads(line) for line in cache.path.read_text().splitlines()]
            self.assertEqual(1, len(cache_rows))
            self.assertEqual("rejected", cache_rows[0]["status"])
            self.assertEqual("contract_rejected_invalid_effort", cache_rows[0]["failure_code"])
            self.assertNotIn("response", cache_rows[0])
            diagnostic = json.loads(
                (Path(temporary) / "cache.jsonl.review-diagnostics.jsonl").read_text()
            )
            self.assertEqual(
                ["valid", "nonpositive"],
                [row["effort_category"] for row in diagnostic["activities"]],
            )

    def test_other_rejections_keep_citation_only_diagnostics(self):
        body = self.body([event("ev-1")])
        response = {
            "activities": [{
                "lifecycle": "completed",
                "effort": {"minimum_minutes": 5, "recommended_minutes": 10, "maximum_minutes": 15},
                "evidence_partitions": [{"bundle_ref": "b-0001", "member_ranges": [[1, 1]]}],
            }],
            "exceptions": [], "omissions": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            cache = semantic.AnalyzerResponseCache(
                Path(temporary) / "cache.jsonl", record_review_diagnostics=True,
            )
            cache.record_rejected_review(
                self.endpoint, body, failure_code="contract_rejected_duplicate_evidence",
                review_scope="failed_review_scoped_recovery", response=response,
            )
            diagnostic = json.loads(
                (Path(temporary) / "cache.jsonl.review-diagnostics.jsonl").read_text()
            )
            self.assertEqual([{
                "evidence_partitions": [{"bundle_ref": "b-0001", "member_ranges": [[1, 1]]}],
            }], diagnostic["activities"])

    def test_scoped_v2_reuses_legacy_accepted_cache_without_transport(self):
        events = [event("ev-1")]
        taxonomy = [{
            "project_name": "Serenichron Level 2", "prefix": "SC",
            "tag_names": ["Processes"],
        }]
        old_body = semantic._review_body(
            events, candidate={"activities": [], "exceptions": [], "omissions": []},
            taxonomy=taxonomy, model=self.endpoint.model,
            review_scope="failed_review_scoped_recovery",
            scoped_failed_review=self.marker,
        )
        old_payload = json.loads(old_body["messages"][1]["content"])
        old_payload["repair_response_contract"]["effort_rule"] = (
            "positive integer minutes: minimum <= recommended <= maximum, supported by evidence"
        )
        old_body["messages"][1]["content"] = semantic.canonical_json(old_payload)
        with tempfile.TemporaryDirectory() as temporary:
            cache = semantic.AnalyzerResponseCache(Path(temporary) / "cache.jsonl")
            cache.store_accepted(self.endpoint, old_body, provider_response(old_payload))
            result = semantic._call_semantic_review_once(
                self.endpoint, events,
                candidate={"activities": [], "exceptions": [], "omissions": []},
                taxonomy=taxonomy, tier="primary_scoped_review",
                transport=lambda *_: self.fail("sealed scoped v2 response must be reused"),
                known_evidence_ids={"ev-1"},
                evidence_time_spans={"ev-1": {
                    "start": "2026-07-10 10:00", "end": "2026-07-10 10:10",
                }},
                cache=cache, before_transport=None, cancelled=None,
                review_scope="failed_review_scoped_recovery",
                scoped_failed_review=self.marker,
            )
            self.assertEqual(["ev-1"], result["activities"][0]["evidence_ids"])

    def test_bound_invalid_effort_uses_distinct_scoped_contract_once(self):
        events = [event("ev-1")]
        endpoint = semantic.AnalyzerEndpoint(
            "primary", "http://fixture", semantic.CURRENT_LIVE_FLASH_ROUTE[0],
            revision=semantic.CURRENT_LIVE_FLASH_ROUTE[1],
        )
        taxonomy = [{
            "project_name": "Serenichron Level 2", "prefix": "SC",
            "tag_names": ["Processes"],
        }]
        with tempfile.TemporaryDirectory() as temporary:
            cache = semantic.AnalyzerResponseCache(Path(temporary) / "cache.jsonl")
            source = {
                "activities": [],
                "exceptions": [{
                    "kind": "analyzer_review_failure",
                    "evidence_ids": ["ev-1"],
                    "reason": "Flash reviewer exhausted bounded scoped retry: contract_rejected_invalid_effort",
                }],
                "omissions": [],
                "ledger_event_count": 1,
                "ledger_evidence_digest": semantic.stable_digest("led-", ["ev-1"]),
                "analyzer_cache": {
                    "records": [],
                    "snapshot": {
                        "path": "analyzer-cache-used.jsonl",
                        "record_count": 0,
                        "sha256": hashlib.sha256(b"").hexdigest(),
                    },
                },
            }
            payloads = []

            def transport(_endpoint, body):
                payload = json.loads(body["messages"][1]["content"])
                payloads.append(payload)
                return provider_response(payload)

            result = pipeline.run_scoped_failed_review_retry(
                source, events, primary=endpoint, cache=cache,
                review_taxonomy=taxonomy,
                targets={("ev-1",): "contract_rejected_invalid_effort"},
                source_semantic_sha256="a" * 64,
                transport=transport, private_text_approved=True,
            )
            self.assertEqual(1, len(payloads))
            self.assertEqual("scoped_review_v3_invalid_effort", payloads[0]["scoped_failed_review"]["mode"])
            rule = payloads[0]["repair_response_contract"]["effort_rule"].lower()
            for required in ("positive", "ordered", "omission", "exception", "no effort", "invent"):
                self.assertIn(required, rule)
            self.assertEqual("scoped_review_v3_invalid_effort", result["failed_review_retry"]["mode"])
            self.assertEqual(["ev-1"], result["activities"][0]["evidence_ids"])
            replayed = pipeline.run_scoped_failed_review_retry(
                source, events, primary=endpoint,
                cache=semantic.AnalyzerResponseCache(cache.path),
                review_taxonomy=taxonomy,
                targets={("ev-1",): "contract_rejected_invalid_effort"},
                source_semantic_sha256="a" * 64,
                scoped_review_mode="scoped_review_v3_invalid_effort",
                transport=lambda *_: self.fail("sealed v3 response must be reused"),
            )
            self.assertEqual(result["activities"], replayed["activities"])
            self.assertEqual("scoped_review_v3_invalid_effort", replayed["failed_review_retry"]["mode"])

    def test_explicit_v2_invalid_effort_reuses_historical_cache(self):
        events = [event("ev-1")]
        endpoint = semantic.AnalyzerEndpoint(
            "primary", "http://fixture", semantic.CURRENT_LIVE_FLASH_ROUTE[0],
            revision=semantic.CURRENT_LIVE_FLASH_ROUTE[1],
        )
        taxonomy = [{
            "project_name": "Serenichron Level 2", "prefix": "SC",
            "tag_names": ["Processes"],
        }]
        target_digest = semantic.stable_digest("frt-", ["ev-1"], length=64)
        marker = {
            "source_semantic_sha256": "a" * 64,
            "group_digest": target_digest,
            "subset_digest": target_digest,
            "mode": "scoped_review_v2",
        }
        old_body = semantic._review_body(
            events, candidate={"activities": [], "exceptions": [], "omissions": []},
            taxonomy=taxonomy, model=endpoint.model,
            review_scope="failed_review_scoped_recovery", scoped_failed_review=marker,
        )
        with tempfile.TemporaryDirectory() as temporary:
            cache = semantic.AnalyzerResponseCache(Path(temporary) / "cache.jsonl")
            cache.store_accepted(
                endpoint, old_body,
                provider_response(json.loads(old_body["messages"][1]["content"])),
            )
            content = cache.path.read_bytes()
            source = {
                "activities": [],
                "exceptions": [{
                    "kind": "analyzer_review_failure",
                    "evidence_ids": ["ev-1"],
                    "reason": "Flash reviewer exhausted bounded scoped retry: contract_rejected_invalid_effort",
                }],
                "omissions": [],
                "ledger_event_count": 1,
                "ledger_evidence_digest": semantic.stable_digest("led-", ["ev-1"]),
                "analyzer_cache": {
                    **cache.summary(),
                    "snapshot": {
                        "path": "analyzer-cache-used.jsonl",
                        "record_count": 1,
                        "sha256": hashlib.sha256(content).hexdigest(),
                    },
                },
            }
            replayed = pipeline.run_scoped_failed_review_retry(
                source, events, primary=endpoint,
                cache=semantic.AnalyzerResponseCache(cache.path),
                review_taxonomy=taxonomy,
                targets={("ev-1",): "contract_rejected_invalid_effort"},
                source_semantic_sha256="a" * 64,
                scoped_review_mode="scoped_review_v2",
                transport=lambda *_: self.fail("historical v2 response must be reused"),
            )
            self.assertEqual(["ev-1"], replayed["activities"][0]["evidence_ids"])
            self.assertEqual("scoped_review_v2", replayed["failed_review_retry"]["mode"])


if __name__ == "__main__":
    unittest.main()
