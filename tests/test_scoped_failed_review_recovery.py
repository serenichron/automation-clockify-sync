import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import semantic_analyzer as semantic
from scripts import work_accounting_pipeline as pipeline
from test_semantic_analyzer import event, provider_response, provider_members, provider_partitions


class ScopedFailedReviewRecoveryTests(unittest.TestCase):
    endpoint = semantic.AnalyzerEndpoint(
        "primary", "http://fixture", semantic.DEFAULT_PRIMARY_MODEL,
        revision=semantic.DEFAULT_PRIMARY_REVISION,
    )
    taxonomy = [{"project_name": "Serenichron Level 2", "prefix": "SC", "tag_names": ["Processes"]}]
    source_digest = "a" * 64

    def source(self, events, cache, target_ids, *, kind="analyzer_review_partial_quarantine"):
        source = {
            "activities": [],
            "exceptions": [{
                "kind": kind,
                "evidence_ids": sorted(target_ids),
                "reason": "Unresolved citation review; no effort assigned",
            }],
            "omissions": [{
                "lifecycle": "noise",
                "evidence_ids": sorted({row["evidence_id"] for row in events} - set(target_ids)),
                "reason": "Unrelated source noise",
            }] if len(target_ids) < len(events) else [],
            "ledger_event_count": len(events),
            "ledger_evidence_digest": semantic.stable_digest(
                "led-", sorted(row["evidence_id"] for row in events)
            ),
            "analysis_chunks": [{"chunk": 1, "event_count": len(events)}],
            "evidence_bundle_manifest": {"unchanged": True},
            "analyzer_cache": cache.summary(),
        }
        content = cache.path.read_bytes() if cache.path.exists() else b""
        source["analyzer_cache"]["snapshot"] = {
            "path": "analyzer-cache-used.jsonl",
            "record_count": len(content.splitlines()),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        return source

    def test_scoped_retry_preserves_unrelated_rows_and_seals_prior_plus_new_cache(self):
        events = [event("ev-1"), event("ev-2"), event("ev-3")]
        with tempfile.TemporaryDirectory() as temporary:
            cache_path = Path(temporary) / "analyzer-cache-used.jsonl"
            cache = semantic.AnalyzerResponseCache(cache_path)
            cache.store_accepted(
                self.endpoint,
                {"model": self.endpoint.model, "messages": [{"role": "user", "content": "prior"}]},
                {"activities": [], "exceptions": [], "omissions": []},
            )
            source = self.source(events, cache, {"ev-1", "ev-2"})
            frozen_source = copy.deepcopy(source)
            target = ("ev-1", "ev-2")
            calls = []

            def transport(_endpoint, body):
                payload = json.loads(body["messages"][1]["content"])
                calls.append(payload)
                self.assertEqual(self.source_digest, payload["scoped_failed_review"]["source_semantic_sha256"])
                self.assertEqual(semantic.stable_digest("frt-", list(target), length=64), payload["scoped_failed_review"]["group_digest"])
                return provider_response(payload)

            result = pipeline.run_scoped_failed_review_retry(
                source, events, primary=self.endpoint, cache=cache,
                review_taxonomy=self.taxonomy,
                targets={target: "contract_rejected_duplicate_evidence"},
                source_semantic_sha256=self.source_digest, transport=transport,
                private_text_approved=True,
                scoped_review_mode="scoped_review_v2",
            )
            self.assertEqual(1, len(calls))
            self.assertEqual(["ev-1", "ev-2"], result["activities"][0]["evidence_ids"])
            self.assertEqual(source["omissions"], result["omissions"])
            self.assertEqual([], result["exceptions"])
            self.assertEqual(source["analysis_chunks"], result["analysis_chunks"])
            self.assertEqual(source["evidence_bundle_manifest"], result["evidence_bundle_manifest"])
            self.assertEqual(frozen_source, source)
            self.assertEqual("scoped_review_v2", result["failed_review_retry"]["mode"])
            self.assertEqual(2, len(result["analyzer_cache"]["records"]))
            sealed = cache_path.read_bytes()

            replay = pipeline.run_scoped_failed_review_retry(
                source, events, primary=self.endpoint,
                cache=semantic.AnalyzerResponseCache(cache_path),
                review_taxonomy=self.taxonomy,
                targets={target: "contract_rejected_duplicate_evidence"},
                source_semantic_sha256=self.source_digest,
                transport=lambda *_: self.fail("sealed retry must not call transport"),
                scoped_review_mode="scoped_review_v2",
            )
            for section in ("activities", "exceptions", "omissions"):
                self.assertEqual(result[section], replay[section])
            self.assertEqual(result["analyzer_cache"]["records"], replay["analyzer_cache"]["records"])
            self.assertEqual(result["failed_review_retry"], replay["failed_review_retry"])
            self.assertEqual(sealed, cache_path.read_bytes())

    def test_partial_quarantine_retry_keeps_disjoint_work_when_one_member_is_duplicated(self):
        """Catches rejecting all seven members for two singleton citations of member four."""
        events = []
        for number, role, minute in (
            (1, "assistant", "38"), (2, "user", "38"),
            (3, "assistant", "38"), (4, "assistant", "50"),
            (5, "user", "55"), (6, "user", "55"),
            (7, "assistant", "55"), (8, "assistant", "59"),
        ):
            row = event(f"ev-{number}")
            row.update({
                "source_type": "claude_bursts_event",
                "observed_at": f"2026-09-26T12:{minute}:00+03:00",
                "raw_source_span": {"timestamp": f"2026-09-26T12:{minute}:00+03:00"},
                "source_ref": {
                    "source_type": "claude_bursts", "machine": "fixture",
                    "session_id": "cleanup", "source_id": f"message-{number}",
                    "ordinal": number,
                },
                "attributes": {"role": role, "kind": "message", "content": "Cleanup work"},
            })
            row.pop("observed_start")
            row.pop("observed_end")
            events.append(row)
        target = tuple(f"ev-{number}" for number in range(1, 8))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original_cache = semantic.AnalyzerResponseCache(root / "analyzer-cache-used.jsonl")
            original_cache.store_accepted(
                self.endpoint,
                {"model": self.endpoint.model, "messages": [{"role": "user", "content": "prior"}]},
                {"activities": [], "exceptions": [], "omissions": []},
            )
            source = self.source(events, original_cache, set(target))
            source["activities"] = [{"activity_id": "prior-accepted", "evidence_ids": ["ev-8"]}]
            source["omissions"] = []
            source_path = root / "semantic-analysis.json"
            source_path.write_text(json.dumps(source))
            source_hashes = {
                path: hashlib.sha256(path.read_bytes()).hexdigest()
                for path in (source_path, original_cache.path)
            }
            frozen_source = copy.deepcopy(source)
            retry_path = root / "analyzer-cache-retry.jsonl"
            retry_path.write_bytes(original_cache.path.read_bytes())
            calls = []

            def transport(_endpoint, body):
                payload = json.loads(body["messages"][1]["content"])
                calls.append(payload)
                members = provider_members(payload)
                valid_members = [members[index - 1] for index in (2, 3, 5, 6, 7)]
                response = provider_response(payload, members=valid_members)
                response["activities"][0].update({
                    "action": "Cleaned", "object": "merged branches and worktrees",
                    "outcome": "preserved the active session",
                    "effort": {"minimum_minutes": 3, "recommended_minutes": 6, "maximum_minutes": 10},
                })
                singleton = provider_response(payload, members=[members[3]])["activities"][0]
                response["activities"].extend([singleton, copy.deepcopy(singleton)])
                response["exceptions"] = [{
                    "kind": "insufficient_evidence", "reason": "Cleanup intention only",
                    "evidence_partitions": provider_partitions([members[0]]),
                }]
                return response

            result = pipeline.run_scoped_failed_review_retry(
                source, events, primary=self.endpoint,
                cache=semantic.AnalyzerResponseCache(retry_path),
                review_taxonomy=self.taxonomy, targets={target: "citation_quarantine"},
                source_semantic_sha256=hashlib.sha256(source_path.read_bytes()).hexdigest(),
                transport=transport, private_text_approved=True,
            )
            self.assertEqual(2, len(result["activities"]))
            self.assertEqual(source["activities"][0], result["activities"][0])
            self.assertEqual(["ev-2", "ev-3", "ev-5", "ev-6", "ev-7"], result["activities"][1]["evidence_ids"])
            self.assertEqual(6, result["activities"][1]["effort"]["recommended_minutes"])
            self.assertEqual([
                ("insufficient_evidence", ["ev-1"]),
                ("analyzer_review_partial_quarantine", ["ev-4"]),
            ], [(row["kind"], row["evidence_ids"]) for row in result["exceptions"]])
            self.assertEqual([{
                "start": "2026-09-26T12:38:00+03:00", "end": "2026-09-26T12:55:00+03:00",
            }], pipeline._activity_observed_intervals([events[index - 1] for index in (2, 3, 5, 6, 7)]))
            self.assertEqual([], pipeline._activity_observed_intervals([events[3]]))
            self.assertEqual(frozen_source, source)
            self.assertEqual(source_hashes, {
                path: hashlib.sha256(path.read_bytes()).hexdigest() for path in source_hashes
            })
            self.assertEqual(1, len(calls))
            replay = pipeline.run_scoped_failed_review_retry(
                source, events, primary=self.endpoint,
                cache=semantic.AnalyzerResponseCache(retry_path),
                review_taxonomy=self.taxonomy, targets={target: "citation_quarantine"},
                source_semantic_sha256=hashlib.sha256(source_path.read_bytes()).hexdigest(),
                transport=lambda *_: self.fail("accepted partial recovery must replay offline"),
                private_text_approved=False,
                scoped_review_mode=result["failed_review_retry"]["mode"],
            )
            for section in ("activities", "exceptions", "omissions"):
                self.assertEqual(result[section], replay[section])

    def test_large_session_is_split_only_between_complete_user_turns(self):
        events = []
        for number in range(1, 41):
            for role in ("user", "assistant"):
                row = event(f"turn-{number:02d}-{role}")
                row["source_type"] = "claude_bursts_event"
                row["source_ref"] = {
                    "source_type": "claude_bursts_event", "machine": "fixture",
                    "session_id": "session-1", "source_id": f"turn-{number:02d}-{role}",
                    "ordinal": number * 2 + (role == "assistant"),
                }
                row["attributes"] = {"kind": "message", "role": role, "content": "work"}
                events.append(row)
        with tempfile.TemporaryDirectory() as temporary:
            cache = semantic.AnalyzerResponseCache(Path(temporary) / "analyzer-cache-used.jsonl")
            ids = tuple(sorted(row["evidence_id"] for row in events))
            source = self.source(events, cache, set(ids))
            requests = []
            partitions = pipeline._scoped_review_partitions(events)
            for partition in partitions:
                for number in range(1, 41):
                    pair = {f"turn-{number:02d}-user", f"turn-{number:02d}-assistant"}
                    self.assertNotEqual(1, len(pair & {row["evidence_id"] for row in partition}))

            def transport(_endpoint, body):
                payload = json.loads(body["messages"][1]["content"])
                members = [member for bundle in payload["bundles"] for member in bundle["members"]]
                requests.append(payload)
                return {
                    "activities": [], "exceptions": [],
                    "omissions": [{
                        "lifecycle": "noise", "reason": "No independently reviewable outcome",
                        "evidence_partitions": [{
                            "bundle_ref": bundle["bundle_ref"],
                            "member_ranges": [[1, bundle["member_count"]]],
                        } for bundle in payload["bundles"]],
                    }],
                }

            result = pipeline.run_scoped_failed_review_retry(
                source, events, primary=self.endpoint, cache=cache,
                review_taxonomy=self.taxonomy,
                targets={ids: "contract_rejected_omitted_evidence"},
                source_semantic_sha256=self.source_digest, transport=transport,
                private_text_approved=True,
            )
            self.assertGreaterEqual(len(requests), 2)
            self.assertTrue(all(sum(bundle["member_count"] for bundle in request["bundles"]) <= 64 for request in requests))
            self.assertEqual(80, sum(len(row["evidence_ids"]) for row in result["omissions"]))
            self.assertEqual([], result["exceptions"])

    def test_partial_quarantine_does_not_hide_invalid_project_on_duplicate_rows(self):
        """Whole-row quarantine must not sanitize invalid semantic routing."""
        events = [event("ev-1"), event("ev-2")]
        with tempfile.TemporaryDirectory() as temporary:
            cache = semantic.AnalyzerResponseCache(Path(temporary) / "analyzer-cache-used.jsonl")
            source = self.source(events, cache, {"ev-1"})

            def transport(_endpoint, body):
                payload = json.loads(body["messages"][1]["content"])
                response = provider_response(payload)
                response["activities"][0]["project_recommendation"]["name"] = "Unknown client"
                response["activities"].append(copy.deepcopy(response["activities"][0]))
                return response

            result = pipeline.run_scoped_failed_review_retry(
                source, events, primary=self.endpoint, cache=cache,
                review_taxonomy=self.taxonomy,
                targets={("ev-1",): "citation_quarantine"},
                source_semantic_sha256=self.source_digest,
                transport=transport, private_text_approved=True,
            )
            self.assertEqual([], result["activities"])
            self.assertEqual(source["omissions"], result["omissions"])
            self.assertEqual("analyzer_review_failure", result["exceptions"][0]["kind"])
            self.assertIn("contract_rejected_other", result["exceptions"][0]["reason"])

    def test_multi_target_provenance_is_sorted_by_digest_not_evidence_id(self):
        events = [event("ev-1"), event("ev-2")]
        with tempfile.TemporaryDirectory() as temporary:
            cache = semantic.AnalyzerResponseCache(Path(temporary) / "analyzer-cache-used.jsonl")
            source = self.source(events, cache, {"ev-1"})
            source["exceptions"].append({
                "kind": "analyzer_review_partial_quarantine",
                "evidence_ids": ["ev-2"], "reason": "Unresolved citation review",
            })
            source["omissions"] = []

            def transport(_endpoint, body):
                payload = json.loads(body["messages"][1]["content"])
                return provider_response(payload)

            result = pipeline.run_scoped_failed_review_retry(
                source, events, primary=self.endpoint, cache=cache,
                review_taxonomy=self.taxonomy,
                targets={
                    ("ev-1",): "contract_rejected_duplicate_evidence",
                    ("ev-2",): "contract_rejected_omitted_evidence",
                },
                source_semantic_sha256=self.source_digest, transport=transport,
                private_text_approved=True,
            )
            self.assertEqual([
                "frt-071deaf56de8ef495309b4e67b9fff2c0ed347be79f2342fe8283f0b26e157d9",
                "frt-c005bf321f7d6196805abd420b486ee8c5d33cd6896b6c8693ed138c4a49ce53",
            ], result["failed_review_retry"]["target_digests"])
            self.assertEqual(2, len(result["activities"]))

    def test_private_text_is_gated_before_any_new_transport_but_not_cache_replay(self):
        events = [event("ev-1", content="private work prose")]
        with tempfile.TemporaryDirectory() as temporary:
            cache = semantic.AnalyzerResponseCache(Path(temporary) / "analyzer-cache-used.jsonl")
            source = self.source(events, cache, {"ev-1"})
            target = {("ev-1",): "contract_rejected_duplicate_evidence"}
            with self.assertRaisesRegex(semantic.AnalyzerError, "private semantic text egress"):
                pipeline.run_scoped_failed_review_retry(
                    source, events, primary=self.endpoint, cache=cache,
                    review_taxonomy=self.taxonomy, targets=target,
                    source_semantic_sha256=self.source_digest,
                    transport=lambda *_: self.fail("private text must not be sent"),
                    private_text_approved=False,
                )
            self.assertFalse(cache.path.exists())

    def test_stale_route_cannot_send_a_new_scoped_request(self):
        events = [event("ev-1")]
        with tempfile.TemporaryDirectory() as temporary:
            cache = semantic.AnalyzerResponseCache(Path(temporary) / "analyzer-cache-used.jsonl")
            source = self.source(events, cache, {"ev-1"})
            stale = semantic.AnalyzerEndpoint("primary", "http://fixture", "flash-fixture")
            with self.assertRaisesRegex(semantic.AnalyzerError, "current exact Flash release"):
                pipeline.run_scoped_failed_review_retry(
                    source, events, primary=stale, cache=cache,
                    review_taxonomy=self.taxonomy,
                    targets={("ev-1",): "citation_quarantine"},
                    source_semantic_sha256=self.source_digest,
                    transport=lambda *_: self.fail("stale route must not send evidence"),
                    private_text_approved=True,
                )
            self.assertFalse(cache.path.exists())

    def test_oversized_indivisible_turn_stays_whole(self):
        events = []
        for number in range(70):
            row = event(f"turn-{number:02d}")
            row["source_type"] = "claude_bursts_event"
            row["source_ref"] = {
                "source_type": "claude_bursts_event", "machine": "fixture",
                "session_id": "session-1", "source_id": f"event-{number:02d}",
                "ordinal": number,
            }
            row["attributes"] = {
                "kind": "message", "role": "user" if number == 0 else "assistant",
                "content": "work",
            }
            events.append(row)
        self.assertEqual([70], [len(part) for part in pipeline._scoped_review_partitions(events)])

    def test_partial_quarantine_resolves_by_exact_members_without_invented_failure_code(self):
        events = [event("ev-1"), event("ev-2")]
        with tempfile.TemporaryDirectory() as temporary:
            cache = semantic.AnalyzerResponseCache(Path(temporary) / "analyzer-cache-used.jsonl")
            cache.path.touch()
            source = self.source(events, cache, {"ev-1"})
            digest = semantic.stable_digest("frt-", ["ev-1"], length=64)
            self.assertEqual(
                {("ev-1",): "citation_quarantine"},
                pipeline._failed_review_retry_targets(source, events, cache.path, digest),
            )

    def test_scoped_review_gives_empty_candidate_a_consumable_typed_response_contract(self):
        events = [event("ev-1")]
        marker = {
            "source_semantic_sha256": self.source_digest,
            "group_digest": "frt-" + "b" * 64,
            "subset_digest": "frt-" + "c" * 64,
            "mode": "scoped_review_v2",
        }

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            contract = payload["repair_response_contract"]
            self.assertEqual([], payload["candidate"]["activities"])
            self.assertIn("completed", contract["lifecycle_values"])
            activity = copy.deepcopy(contract["activity_example"])
            activity["evidence_spans"] = [payload["bundles"][0]["members"][0]["time_span"]]
            return {"activities": [activity], "exceptions": [], "omissions": []}

        result = semantic._call_semantic_review_once(
            self.endpoint, events,
            candidate={"activities": [], "exceptions": [], "omissions": []},
            taxonomy=self.taxonomy, tier="primary_scoped_review",
            transport=transport, known_evidence_ids={"ev-1"},
            evidence_time_spans={"ev-1": {"start": "2026-07-10 10:00", "end": "2026-07-10 10:10"}},
            cache=None, before_transport=None, cancelled=None,
            review_scope="failed_review_scoped_recovery", scoped_failed_review=marker,
        )
        self.assertEqual("completed", result["activities"][0]["lifecycle"])
        self.assertEqual(["ev-1"], result["activities"][0]["evidence_ids"])

    def test_v1_scoped_request_remains_byte_exact_and_replays_without_egress(self):
        events = [event("ev-1")]
        marker = {
            "source_semantic_sha256": self.source_digest,
            "group_digest": semantic.stable_digest("frt-", ["ev-1"], length=64),
            "subset_digest": semantic.stable_digest("frt-", ["ev-1"], length=64),
        }
        body = semantic._review_body(
            events,
            candidate={"activities": [], "exceptions": [], "omissions": []},
            taxonomy=self.taxonomy, model=self.endpoint.model,
            review_scope="failed_review_scoped_recovery",
            scoped_failed_review=marker,
        )
        self.assertEqual(
            "31ec21a87656e2b076e8433c426e7a6c470ef40fd40f05a7aaba6ac6663457e0",
            hashlib.sha256(semantic.canonical_json(body).encode("utf-8")).hexdigest(),
        )
        self.assertNotIn("repair_response_contract", json.loads(body["messages"][1]["content"]))
        with tempfile.TemporaryDirectory() as temporary:
            cache = semantic.AnalyzerResponseCache(Path(temporary) / "analyzer-cache-used.jsonl")
            cache.store_accepted(self.endpoint, body, provider_response(json.loads(body["messages"][1]["content"])))
            source = self.source(events, cache, {"ev-1"})
            result = pipeline.run_scoped_failed_review_retry(
                source, events, primary=self.endpoint, cache=semantic.AnalyzerResponseCache(cache.path),
                review_taxonomy=self.taxonomy,
                targets={("ev-1",): "citation_quarantine"},
                source_semantic_sha256=self.source_digest,
                transport=lambda *_: self.fail("v1 replay must remain cache-only"),
                private_text_approved=False,
                scoped_review_mode="scoped_review_v1",
            )
            self.assertEqual(["ev-1"], result["activities"][0]["evidence_ids"])
            self.assertEqual("scoped_review_v1", result["failed_review_retry"]["mode"])

    def test_retry_exhausted_source_resolves_exact_allowlisted_failure_codes(self):
        events = [event("ev-1")]
        with tempfile.TemporaryDirectory() as temporary:
            cache = semantic.AnalyzerResponseCache(Path(temporary) / "analyzer-cache-used.jsonl")
            cache.path.touch()
            digest = semantic.stable_digest("frt-", ["ev-1"], length=64)
            for code in ("contract_rejected_invalid_json", "contract_rejected_invalid_lifecycle"):
                with self.subTest(code=code):
                    source = self.source(events, cache, {"ev-1"}, kind="analyzer_review_failure")
                    source["exceptions"][0]["reason"] = (
                        "Flash reviewer exhausted bounded scoped retry: " + code
                    )
                    self.assertEqual(
                        {("ev-1",): code},
                        pipeline._failed_review_retry_targets(source, events, cache.path, digest),
                    )


if __name__ == "__main__":
    unittest.main()
