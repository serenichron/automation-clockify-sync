"""Fresh commissioned-task contract; historical review/timing recipes stay sealed."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import semantic_analyzer as semantic
from scripts import work_accounting_pipeline as pipeline
from test_semantic_analyzer import provider_members, provider_response
import test_work_accounting_pipeline as fixtures


V9 = "clockify-semantic-review-v9"
TAXONOMY = [{"project_name": "Serenichron Level 2", "prefix": "SC", "tag_names": ["Processes"]}]


class CommissionedTaskContractTests(unittest.TestCase):
    def rows(self):
        return semantic.with_actor_context([
            fixtures.hermes_event("2026-10-18T10:00:00+03:00").document(),
            fixtures.hermes_event("2026-10-18T10:05:00+03:00", "assistant").document(),
        ])

    def test_historical_review_bodies_remain_byte_identical(self):
        # A fresh default must not rewrite sealed request/cache identities.
        for version, digest in {
            "clockify-semantic-review-v6": "d5ea36162358598f15178fd85619ab16acde5bfc3ad1db611bc0c09de920d5e5",
            "clockify-semantic-review-v7": "d5ea36162358598f15178fd85619ab16acde5bfc3ad1db611bc0c09de920d5e5",
            "clockify-semantic-review-v8": "7b8c47edc04c476b137420f6237f99dc926e89f8094bd6610b8ad3dc0ebac0f6",
        }.items():
            messages = semantic._review_messages(self.rows(), candidate={"activities": [], "exceptions": [], "omissions": []},
                                                taxonomy=TAXONOMY, review_prompt_version=version)
            self.assertEqual(digest, hashlib.sha256(semantic.canonical_json(messages).encode()).hexdigest())

    def test_fresh_actor_analysis_selects_v9_and_replay_keeps_it(self):
        # Catches fresh native analysis continuing to use the human-only v8 recipe.
        endpoint = semantic.AnalyzerEndpoint("primary", "http://fixture", "fixture")
        analyze = semantic.analyze_tiered
        seen = []
        def transport(_endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            if payload["mode"] == "review":
                seen.append(payload["review_prompt_version"])
                self.assertIn("NON-OWNING CONTEXT", body["messages"][0]["content"])
                self.assertIn("not measured agent runtime", body["messages"][0]["content"])
            return provider_response(payload)
        routing = {"semantic_actor_contract": semantic.ACTOR_CONTRACT, "session_routes": [], "meeting_routes": []}
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache.jsonl"
            with mock.patch.object(semantic.AnalyzerEndpoint, "from_env", side_effect=[endpoint, None]), \
                 mock.patch.dict("os.environ", {"CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED": "approved"}), \
                 mock.patch.object(semantic, "analyze_tiered", wraps=lambda events, **kwargs: analyze(events, transport=transport, **kwargs)):
                result = pipeline.analyze_ledger(self.rows(), analyzer_cache_path=cache, review_routing=routing,
                                                review_taxonomy=TAXONOMY, analyzer_workers=1)
            self.assertEqual([V9], seen)
            replay = analyze(self.rows(), primary=endpoint, review_taxonomy=TAXONOMY,
                             review_prompt_version=result["review_prompt_version"], private_text_approved=True,
                             cache=semantic.AnalyzerResponseCache(cache),
                             transport=lambda *_: self.fail("replay inferred"), max_workers=1)
            self.assertEqual(result["activities"], replay["activities"])

    def test_one_instruction_can_contextualize_two_exclusively_owned_results(self):
        # No shared provider field is needed; duplicate outcome ownership still fails.
        events = self.rows() + semantic.with_actor_context([
            fixtures.hermes_event("2026-10-18T10:06:00+03:00", "assistant").document()])
        self.assertEqual(V9, semantic._extraction_review_version(events, V9))
        endpoint = semantic.AnalyzerEndpoint("primary", "http://fixture", "fixture")
        def review(duplicate=False):
            def transport(_endpoint, body):
                payload = json.loads(body["messages"][1]["content"])
                members = provider_members(payload)
                first = provider_response(payload, members[:2])["activities"][0]
                second = provider_response(payload, [members[1 if duplicate else 2]])["activities"][0]
                second["object"] = "separate review artifact"
                return {"activities": [first, second], "exceptions": [], "omissions": []}
            return semantic._call_semantic_review_once(endpoint, events, candidate={}, taxonomy=TAXONOMY,
                tier="primary", transport=transport, known_evidence_ids={row["evidence_id"] for row in events},
                evidence_time_spans={row["evidence_id"]: semantic._safe_time_span(row) for row in events},
                cache=None, before_transport=None, cancelled=None, review_prompt_version=V9)
        result = review()
        self.assertEqual([1, 2], sorted(len(row["evidence_ids"]) for row in result["activities"]))
        with self.assertRaises(semantic.AnalyzerContractError):
            review(duplicate=True)


class CommissionedTaskTimingTests(unittest.TestCase):
    make_run = fixtures.WorkAccountingPipelineTests.make_run

    def analysis(self, points, version=V9, recommended=10):
        result = fixtures.analysis_for([point.evidence_id for point in points], recommended=recommended)
        result["review_prompt_version"] = version
        for row in result["activities"]:
            row.update(semantic_reviewer_model="fixture", review_prompt_version=version)
        return result

    def test_v9_hermes_result_observations_bound_estimate_without_human_relabeling(self):
        # Catches v9 still dropping actual task-owned assistant observation timing.
        points = [fixtures.hermes_event("2026-10-18T10:00:00+03:00", "assistant"),
                  fixtures.hermes_event("2026-10-18T10:03:40+03:00", "assistant")]
        _, result = self.make_run(points, self.analysis(points))
        self.assertEqual(3, sum(row["duration_minutes"] for row in result["proposals"]))
        warning = next(row for row in result["proposals"][0]["review_warnings"] if row["type"] == "estimated_task_effort")
        self.assertEqual("task_owned_message_observations/v1", warning["timing_recipe"])
        self.assertIn("task_effort_timing", result["allocation"]["deterministic_inputs"])
        self.assertTrue(any(row["type"] == "observed_capacity_cap" for row in result["proposals"][0]["review_warnings"]))
        _, historical = self.make_run(points, self.analysis(points, "clockify-semantic-review-v8"))
        self.assertEqual([], historical["proposals"])
        self.assertNotIn("task_effort_timing", historical["allocation"]["deterministic_inputs"])

    def test_v9_singletons_envelopes_and_cross_source_points_supply_no_duration(self):
        # Catches invented duration, idle bridging and cross-session capacity borrowing.
        first = fixtures.hermes_event("2026-10-18T10:00:00+03:00", "assistant")
        for other in (None,
                      fixtures.hermes_event("2026-10-18T10:31:00+03:00", "assistant"),
                      fixtures.hermes_event("2026-10-18T10:10:00+03:00", "assistant", session="other"),
                      fixtures.hermes_event("2026-10-18T10:10:00+03:00", "assistant", machine="other")):
            with self.subTest(other=other):
                points = [first] + ([other] if other else [])
                _, result = self.make_run(points, self.analysis(points))
                self.assertEqual([], result["proposals"])
                self.assertTrue(any(row.get("exception_kind") == "timing_evidence" for row in result["ambiguous"]))

    def test_v9_nonowning_context_cannot_lend_a_human_timing_pool(self):
        # Two human anchors exist in the chunk, but only the result belongs here.
        anchors = [fixtures.hermes_event("2026-10-18T10:00:00+03:00"),
                   fixtures.hermes_event("2026-10-18T10:20:00+03:00")]
        result_point = fixtures.hermes_event("2026-10-18T10:10:00+03:00", "assistant")
        analysis = self.analysis([result_point])
        analysis["omissions"] = [{"lifecycle": "noise", "evidence_ids": [p.evidence_id for p in anchors],
                                  "reason": "Non-owning commission context only"}]
        _, result = self.make_run(anchors + [result_point], analysis)
        self.assertEqual([], result["proposals"])
        self.assertTrue(any(row.get("exception_kind") == "timing_evidence" for row in result["ambiguous"]))

    def test_v9_session_envelope_and_cost_cannot_supply_capacity(self):
        point = fixtures.hermes_event("2026-10-18T10:10:00+03:00", "assistant")
        envelope = fixtures.evidence_ledger.evidence_event(
            "hermes_db_sessions", {"source_type": "hermes_db_sessions", "source_id": "summary"},
            raw_source_span={"start": "2026-10-18T00:00:00+03:00", "end": "2026-10-19T00:00:00+03:00"},
            attributes={"computed_active_minutes": 16, "token_count": 100000, "estimated_cost_usd": 50})
        _, result = self.make_run([point, envelope], self.analysis([point, envelope]))
        self.assertEqual([], result["proposals"])

    def test_v9_independent_parallel_tasks_recover_with_overlap_warnings(self):
        # Catches new task timing being collapsed into a shared human pool.
        first = [fixtures.hermes_event("2026-10-18T10:00:00+03:00", "assistant"),
                 fixtures.hermes_event("2026-10-18T10:10:00+03:00", "assistant")]
        second = [fixtures.hermes_event("2026-10-18T10:00:00+03:00", "assistant", session="other"),
                  fixtures.hermes_event("2026-10-18T10:10:00+03:00", "assistant", session="other")]
        analysis = self.analysis(first)
        other = self.analysis(second)["activities"][0]
        other["object"] = "separate commissioned deliverable"
        analysis["activities"].append(other)
        _, result = self.make_run(first + second, analysis)
        self.assertEqual(20, sum(row["duration_minutes"] for row in result["proposals"]))
        self.assertEqual(1, len(result["allocation"]["capacity_recoveries"]))
        overlaps = [w for row in result["proposals"] for w in row["review_warnings"]
                    if w["type"] == "review_proposal_overlap"]
        self.assertEqual(1, len(overlaps))
        self.assertEqual(600, overlaps[0]["overlap_duration_seconds"])


class FreshV9FeedbackReplayTests(unittest.TestCase):
    def test_fresh_v9_corrects_numeric_conflict_and_seals_cache_only_replay(self):
        # Catches repeated generic repairs and corrections that cannot replay offline.
        endpoint = semantic.AnalyzerEndpoint("primary", "http://fixture", "fixture")
        events = CommissionedTaskContractTests().rows()
        routing = {"semantic_actor_contract": semantic.ACTOR_CONTRACT, "session_routes": [], "meeting_routes": []}
        analyze = semantic.analyze_tiered
        review_payloads = []
        def transport(_endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            response = provider_response(payload)
            if payload["mode"] == "review":
                review_payloads.append(payload)
                if len(review_payloads) == 1:
                    duplicate = provider_response(payload, provider_members(payload)[1:])["activities"][0]
                    duplicate["object"] = "separate artifact"
                    response["activities"].append(duplicate)
                else:
                    self.assertEqual(1, payload["repair_feedback"].get("coverage_counts", {}).get("duplicate_members"),
                                     "fresh v9 repair needs the actual source-bound numeric conflict")
            return response
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache_path = root / "live.jsonl"
            with mock.patch.object(semantic.AnalyzerEndpoint, "from_env", side_effect=lambda prefix, **_: endpoint if prefix == "CLOCKIFY_ANALYZER_PRIMARY" else None), \
                 mock.patch.dict("os.environ", {"CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED": "approved"}), \
                 mock.patch.object(semantic, "analyze_tiered", wraps=lambda rows, **kwargs: analyze(rows, transport=transport, **kwargs)):
                result = pipeline.analyze_ledger(events, analyzer_cache_path=cache_path, review_routing=routing,
                                                review_taxonomy=TAXONOMY, analyzer_workers=1)
                diagnostic_refs = result["analyzer_cache"]["review_diagnostic_records"]
                sealed_dir = root / "sealed"
                sealed_dir.mkdir()
                pipeline._seal_analyzer_cache_snapshot(sealed_dir, cache_path, result)
            self.assertEqual(2, len(review_payloads))
            descriptor = result["analyzer_cache"]["review_diagnostics_snapshot"]
            self.assertEqual("analyzer-cache-used.jsonl.review-diagnostics.jsonl", descriptor["path"])
            diagnostic_bytes = (sealed_dir / descriptor["path"]).read_bytes()
            self.assertEqual(1, descriptor["record_count"])
            self.assertEqual(hashlib.sha256(diagnostic_bytes).hexdigest(), descriptor["sha256"])
            diagnostic = json.loads(diagnostic_bytes)
            semantic.validate_review_diagnostic_record(diagnostic)
            for bad in ({**diagnostic, "private_prose": "must never be sealed"},
                        {**diagnostic, "failure_code": "contract_rejected_invalid_effort"},
                        {**diagnostic, "cache_key": "foreign"}):
                with self.assertRaises(semantic.AnalyzerError):
                    semantic.validate_review_diagnostic_record(bad)
            # Later canonical-cache changes cannot influence the private sealed replay.
            cache_path.with_name(cache_path.name + ".review-diagnostics.jsonl").write_text("{}\n")
            sealed_cache = semantic.AnalyzerResponseCache(sealed_dir / "analyzer-cache-used.jsonl")
            used = sealed_cache.records_for_snapshot(result["analyzer_cache"]["records"], configured_endpoints=(endpoint,))
            with self.assertRaises(semantic.AnalyzerError):
                semantic.AnalyzerResponseCache(cache_path).review_diagnostics_for_snapshot(diagnostic_refs, used_cache_records=used)
            before = {path.name: path.read_bytes() for path in sealed_dir.iterdir()}
            replay = analyze(events, primary=endpoint, review_taxonomy=TAXONOMY, review_prompt_version=V9,
                             private_text_approved=True, cache=semantic.AnalyzerResponseCache(sealed_dir / "analyzer-cache-used.jsonl"),
                             transport=lambda *_: self.fail("sealed corrected v9 replay inferred"), max_workers=1)
            self.assertEqual(result["activities"], replay["activities"])
            self.assertEqual(result["exceptions"], replay["exceptions"])
            self.assertEqual(before, {path.name: path.read_bytes() for path in sealed_dir.iterdir()})

    def test_historical_v8_capture_keeps_generic_repair_bodies(self):
        endpoint = semantic.AnalyzerEndpoint("primary", "http://fixture", "fixture")
        events = CommissionedTaskContractTests().rows()
        count = 0
        def transport(_endpoint, body):
            nonlocal count
            payload = json.loads(body["messages"][1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            response = provider_response(payload)
            if payload["mode"] == "review":
                count += 1
                if count == 1:
                    response["activities"].append(provider_response(payload, provider_members(payload)[1:])["activities"][0])
                else:
                    self.assertNotIn("citation_conflicts", payload["repair_feedback"])
            return response
        with tempfile.TemporaryDirectory() as temporary:
            cache = semantic.AnalyzerResponseCache(Path(temporary) / "cache.jsonl",
                record_review_diagnostics=True, use_review_diagnostic_feedback=False)
            result = semantic.analyze_tiered(events, primary=endpoint, review_taxonomy=TAXONOMY,
                review_prompt_version="clockify-semantic-review-v8", private_text_approved=True,
                cache=cache, transport=transport, max_workers=1)
            self.assertEqual(1, len(result["activities"]))
            self.assertNotIn("review_diagnostic_records", result["analyzer_cache"])


if __name__ == "__main__":
    unittest.main()
