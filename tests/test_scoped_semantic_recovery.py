"""Behavior proofs for source-bound, semantic-only context recovery."""
import copy
import hashlib
import importlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import evidence_ledger
from scripts import semantic_analyzer as semantic
from scripts import work_accounting_pipeline as pipeline
from test_semantic_analyzer import provider_response, valid_response


TAXONOMY = [{"project_name": "Serenichron Level 2", "prefix": "SC", "tag_names": ["Processes"]}]


def fixture(root):
    """Six whole human contexts90, residual47+13, and14 unaffected members."""
    primary = semantic.AnalyzerEndpoint("primary", "http://fixture", semantic.CURRENT_LIVE_FLASH_ROUTE[0], revision=semantic.CURRENT_LIVE_FLASH_ROUTE[1])
    contexts = []
    event_objects = []
    for number, count in enumerate((18, 34, 5, 9, 20, 4)):
        context = []
        for ordinal in range(count):
            event = evidence_ledger.evidence_event("codex_sessions_event", {"source_type": "codex_sessions", "source_id": f"context{number}:{ordinal}", "machine": "fixture", "session_id": f"context{number}", "ordinal": ordinal + 1}, observed_at=f"2026-09-14T10:{ordinal // 4:02}:{ordinal % 4 * 15:02}+03:00", raw_source_span={"timestamp": f"2026-09-14T10:{ordinal // 4:02}:{ordinal % 4 * 15:02}+03:00"}, attributes={"role": "user" if ordinal == 0 else "assistant", "kind": "message", "content": "Requested investigation" if ordinal == 0 else "Completed investigation"})
            event_objects.append(event)
            context.append(event.evidence_id)
        contexts.append(context)
    residuals = []
    for group, count in enumerate((47, 13)):
        ids = []
        for index in range(count):
            event = evidence_ledger.evidence_event("multica", {"source_id": f"residual{group}:{index}"}, observed_at="2026-09-14T09:00:00+03:00", attributes={"title": "Issue metadata"})
            event_objects.append(event)
            ids.append(event.evidence_id)
        residuals.append(ids)
    accepted = []
    for index in range(5):
        event = evidence_ledger.evidence_event("multica", {"source_id": f"accepted:{index}"}, observed_at="2026-09-14T09:00:00+03:00", attributes={"title": "Reviewed issue"})
        event_objects.append(event)
        activity = valid_response(event.evidence_id)["activities"][0]
        activity.update(activity_id=f"accepted-{index}", semantic_reviewer_model=primary.model, semantic_reviewer_revision=primary.revision)
        accepted.append(activity)
    other_ids = []
    for index in range(9):
        event = evidence_ledger.evidence_event("context_snapshot", {"source_id": f"unaffected:{index}"}, observed_at="2026-09-14T09:00:00+03:00", attributes={"title": "Neutral source"})
        event_objects.append(event)
        other_ids.append(event.evidence_id)
    events = [event.document() for event in event_objects]
    groups = [sorted(contexts[0] + residuals[0]), sorted(sum(contexts[1:], []) + residuals[1])]
    cache_path = root / "analyzer-cache-used.jsonl"
    cache = semantic.AnalyzerResponseCache(cache_path, record_review_diagnostics=True)
    cache.store_rejected(primary, {"source": "sealed"}, failure_code="contract_rejected_duplicate_evidence")
    source = {"activities": accepted, "omissions": [{"lifecycle": "noise", "evidence_ids": other_ids, "reason": "Unchanged neutral evidence"}], "exceptions": [{"kind": "analyzer_review_failure", "evidence_ids": ids, "reason": "Flash reviewer exhausted bounded structural repair: " + code} for ids, code in zip(groups, ("contract_rejected_invalid_evidence_ids", "contract_rejected_duplicate_evidence"))], "ledger_event_count": 164, "ledger_evidence_digest": semantic.stable_digest("led-", sorted(event["evidence_id"] for event in events)), "analyzer_cache": {**cache.summary(), "snapshot": {"path": cache_path.name, "record_count": 1, "sha256": hashlib.sha256(cache_path.read_bytes()).hexdigest()}}}
    targets = {tuple(ids): code for ids, code in zip(groups, ("contract_rejected_invalid_evidence_ids", "contract_rejected_duplicate_evidence"))}
    return primary, source, events, cache, contexts, residuals, targets


class ScopedSemanticRecoveryTests(unittest.TestCase):
    def run_scoped(self, source, events, primary, cache, targets, selected, transport, **extra):
        try:
            return pipeline.run_scoped_failed_review_retry(source, events, primary=primary, cache=cache, review_taxonomy=TAXONOMY, targets=targets, source_semantic_sha256="a" * 64, selected_evidence_ids=selected, private_text_approved=True, transport=transport, **extra)
        except TypeError as exc:
            if "selected_evidence_ids" in str(exc):
                self.fail("Source-bound selected-ID recovery is not implemented")
            raise

    def test_exact_six_context_membership_original_digests_and_residual_quarantines(self):
        # Catches whole-group reruns, manufactured source digests, and dropped residuals.
        with tempfile.TemporaryDirectory() as temporary:
            primary, source, events, cache, contexts, residuals, targets = fixture(Path(temporary))
            original = copy.deepcopy(source)
            prefix = cache.path.read_bytes()
            selected = sum(contexts, [])
            payloads = []
            def transport(endpoint, body):
                payload = json.loads(body["messages"][1]["content"])
                payloads.append(payload)
                return provider_response(payload)
            result = self.run_scoped(source, events, primary, cache, targets, selected, transport)
            self.assertEqual([4, 5, 9, 18, 20, 34], sorted(sum(len(b["members"]) for b in payload["bundles"]) for payload in payloads))
            self.assertEqual(6, len(payloads))
            self.assertEqual(90, sum(len(a["evidence_ids"]) for a in result["activities"][5:]))
            self.assertEqual({frozenset(ids) for ids in contexts}, {frozenset(a["evidence_ids"]) for a in result["activities"][5:]})
            self.assertEqual(original["activities"], result["activities"][:5])
            self.assertEqual(original["omissions"], result["omissions"])
            self.assertEqual([set(ids) for ids in residuals], [set(row["evidence_ids"]) for row in result["exceptions"]])
            digests = {semantic.stable_digest("frt-", list(key), length=64) for key in targets}
            self.assertEqual(digests, {payload["scoped_failed_review"]["group_digest"] for payload in payloads})
            self.assertEqual(digests, set(result["failed_review_retry"]["target_digests"]))
            cited = [eid for section in ("activities", "exceptions", "omissions") for row in result[section] for eid in row["evidence_ids"]]
            self.assertEqual(164, len(cited))
            self.assertEqual(164, len(set(cited)))
            self.assertEqual(original, source)
            self.assertTrue(cache.path.read_bytes().startswith(prefix))
            before = cache.path.read_bytes()
            replay = self.run_scoped(source, events, primary, cache, targets, selected, lambda *_: self.fail("Cached recovery must not infer"))
            self.assertEqual(result["activities"], replay["activities"])
            self.assertEqual(before, cache.path.read_bytes())

    def test_scope_rejects_unknown_unaffected_duplicate_and_incomplete_context_before_transport(self):
        # Catches selection boundary bypass and borrowed human/result context.
        with tempfile.TemporaryDirectory() as temporary:
            primary, source, events, cache, contexts, _, targets = fixture(Path(temporary))
            selected = sum(contexts, [])
            for invalid in ([], ["unknown"], [source["activities"][0]["evidence_ids"][0]], selected + selected[:1], selected[1:]):
                with self.subTest(length=len(invalid)), self.assertRaisesRegex(pipeline.WorkAccountingError, "scope|context"):
                    self.run_scoped(source, events, primary, cache, targets, invalid, lambda *_: self.fail("Invalid scope must not infer"))

    def test_plan_constructs_exact_context_jobs_without_transport_or_cache_writes(self):
        # Catches planning that performs live inference or silently broadens membership.
        with tempfile.TemporaryDirectory() as temporary:
            primary, source, events, cache, contexts, _, targets = fixture(Path(temporary))
            before = cache.path.read_bytes()
            plan = self.run_scoped(source, events, primary, cache, targets, sum(contexts, []), lambda *_: self.fail("Plan must not infer"), plan_only=True)
            self.assertEqual(90, plan["selected_event_count"])
            self.assertEqual(6, len(plan["requests"]))
            self.assertEqual([4, 5, 9, 18, 20, 34], sorted(row["event_count"] for row in plan["requests"]))
            self.assertEqual([13, 47], sorted(plan["residual_event_counts"]))
            self.assertEqual(before, cache.path.read_bytes())
            self.assertEqual("47927851505b9213851d60fdcc3d54433673e19b1522578ce3b17ddc4a2d1557",
                             hashlib.sha256(semantic.canonical_json(plan).encode()).hexdigest())

    def test_actor_contract_is_orthogonal_to_scoped_repair_modes_and_replays(self):
        # Catches actor awareness replacing the established effort/citation repair.
        for mode in ("scoped_review_v2", "scoped_review_v3_invalid_effort", "scoped_review_v4_citation_quarantine"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                primary, source, events, cache, contexts, _, targets = fixture(Path(temporary))
                if mode == "scoped_review_v3_invalid_effort":
                    targets = {key: "contract_rejected_invalid_effort" for key in targets}
                elif mode == "scoped_review_v4_citation_quarantine":
                    targets = {key: "contract_rejected_duplicate_evidence" for key in targets}
                self.assertIn("actor_contract", __import__("inspect").signature(pipeline.run_scoped_failed_review_retry).parameters,
                              "Scoped actor-contract boundary is missing")
                options = {"scoped_review_mode": mode, "actor_contract": "clockify-semantic-actors/v1"}
                before = cache.path.read_bytes()
                plan = self.run_scoped(source, events, primary, cache, targets, sum(contexts, []), lambda *_: self.fail("Plan inferred"), plan_only=True, **options)
                self.assertEqual(mode, plan["mode"])
                self.assertEqual("clockify-semantic-actors/v1", plan["actor_contract"])
                self.assertEqual(before, cache.path.read_bytes())
                payloads = []
                def transport(_endpoint, body):
                    payload = json.loads(body["messages"][1]["content"])
                    payloads.append(payload)
                    return provider_response(payload)
                result = self.run_scoped(source, events, primary, cache, targets, sum(contexts, []), transport, **options)
                self.assertEqual(mode, result["failed_review_retry"]["mode"])
                self.assertEqual("clockify-semantic-actors/v1", result["failed_review_retry"]["actor_contract"])
                self.assertEqual(source["activities"], result["activities"][:5])
                self.assertTrue(all(p["scoped_failed_review"]["mode"] == mode for p in payloads))
                self.assertTrue(all(p["review_prompt_version"] == "clockify-semantic-review-v7" for p in payloads))
                after = cache.path.read_bytes()
                replay = self.run_scoped(source, events, primary, cache, targets, sum(contexts, []), lambda *_: self.fail("Replay inferred"), **options)
                self.assertEqual(result["activities"], replay["activities"])
                self.assertEqual(after, cache.path.read_bytes())

    def test_unknown_actor_contract_fails_before_scoped_cache_or_transport(self):
        with tempfile.TemporaryDirectory() as temporary:
            primary, source, events, cache, contexts, _, targets = fixture(Path(temporary))
            self.assertIn("actor_contract", __import__("inspect").signature(pipeline.run_scoped_failed_review_retry).parameters,
                          "Scoped actor-contract boundary is missing")
            before = cache.path.read_bytes()
            with self.assertRaisesRegex(pipeline.WorkAccountingError, "actor contract"):
                self.run_scoped(source, events, primary, cache, targets, sum(contexts, []), lambda *_: self.fail("Unknown contract inferred"), actor_contract="unsupported")
            self.assertEqual(before, cache.path.read_bytes())

    def test_command_forwards_new_actor_contract_and_reconstructs_sealed_cache(self):
        # Catches standalone recovery dropping the actor contract during replay.
        command = importlib.import_module("scripts.clockify_scoped_semantic_recovery")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_dir = root / "source"
            source_dir.mkdir()
            primary, source, events, cache, contexts, _, targets = fixture(source_dir)
            ledger = evidence_ledger.EvidenceLedger(tuple(evidence_ledger.EvidenceEvent.from_document(row) for row in events),
                                                   timezone="Europe/Bucharest", member_identities=("member@example.com",))
            (source_dir / "evidence").mkdir()
            files = {"evidence/evidence-ledger.json": {"schema_version": evidence_ledger.SCHEMA_VERSION, "events": events, "manifest": ledger.manifest.document()},
                     "semantic-analysis.json": source,
                     "proposals.json": [],
                     "routing.json": {"semantic_actor_contract": "clockify-semantic-actors/v1",
                                      "session_routes": [{"pattern": "fixture", "project_name": "Serenichron Level 2", "prefix": "SC", "tag_names": ["Processes"]}], "meeting_routes": []}}
            for name, value in files.items():
                (source_dir / name).write_text(json.dumps(value))
            scope = root / "scope.json"
            scope.write_text(json.dumps({"evidence_ids": sum(contexts, [])}))
            argv = [str(source_dir), "--scope-file", str(scope), "--output-dir", str(root / "output")]
            digests = [semantic.stable_digest("frt-", list(key), length=64) for key in targets]
            for digest in digests:
                argv += ["--failed-review-digest", digest]
            before = {path: path.read_bytes() for path in source_dir.rglob("*") if path.is_file()}
            def transport(_endpoint, body):
                payload = json.loads(body["messages"][1]["content"])
                self.assertEqual("clockify-semantic-actors/v1", payload.get("actor_contract"))
                return provider_response(payload)
            with mock.patch.object(semantic.AnalyzerEndpoint, "from_env", return_value=primary), mock.patch.object(semantic, "http_transport", side_effect=transport), mock.patch.dict("os.environ", {"CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED": "approved"}):
                plan = command.run(command.parse_args(argv + ["--plan"]))
                self.assertEqual("clockify-semantic-actors/v1", plan.get("actor_contract"))
                command.run(command.parse_args(argv))
            sealed = {path: path.read_bytes() for path in (root / "output").rglob("*") if path.is_file()}
            with mock.patch.object(semantic, "http_transport", side_effect=AssertionError("Sealed recovery inferred")):
                replay = command.validate_cached_recovery(source_dir, root / "output", digests)
            self.assertEqual(source["activities"], replay["analysis"]["activities"][:5])
            self.assertEqual("clockify-semantic-actors/v1", replay["analysis"]["failed_review_retry"]["actor_contract"])
            self.assertEqual(before, {path: path.read_bytes() for path in source_dir.rglob("*") if path.is_file()})
            self.assertEqual(sealed, {path: path.read_bytes() for path in (root / "output").rglob("*") if path.is_file()})

    def test_semantic_only_command_preserves_meetings_and_never_emits_proposals(self):
        # Catches routing through accounting/allocation or modifying source proposals.
        try:
            command = importlib.import_module("scripts.clockify_scoped_semantic_recovery")
        except ImportError:
            self.fail("Dedicated semantic-only recovery command is not implemented")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source_dir = root / "source"
            source_dir.mkdir()
            primary, source, events, cache, contexts, _, targets = fixture(source_dir)
            objects = [evidence_ledger.EvidenceEvent.from_document(e) for e in events]
            ledger = evidence_ledger.EvidenceLedger(tuple(objects), timezone="Europe/Bucharest", member_identities=("member@example.com",))
            (source_dir / "evidence").mkdir()
            def write(path, value):
                path.write_text(json.dumps(value))
            write(source_dir / "evidence/evidence-ledger.json", {"schema_version": evidence_ledger.SCHEMA_VERSION, "events": events, "manifest": ledger.manifest.document()})
            write(source_dir / "semantic-analysis.json", source)
            write(source_dir / "routing.json", {"session_routes": [{"pattern": "fixture", "project_name": "Serenichron Level 2", "prefix": "SC", "tag_names": ["Processes"]}], "meeting_routes": []})
            meeting_bytes = b'[{"id":"P001","start":"2026-09-14T10:00:00+03:00","end":"2026-09-14T10:05:00+03:00"},{"id":"P002","start":"2026-09-14T12:00:00+03:00","end":"2026-09-14T13:00:00+03:00"}]\n'
            (source_dir / "proposals.json").write_bytes(meeting_bytes)
            scope_file = root / "scope.json"
            write(scope_file, {"evidence_ids": sum(contexts, [])})
            argv = [str(source_dir), "--scope-file", str(scope_file), "--output-dir", str(root / "output")]
            for key in targets:
                argv += ["--failed-review-digest", semantic.stable_digest("frt-", list(key), length=64)]
            snapshots = {p: p.read_bytes() for p in source_dir.rglob("*") if p.is_file()}
            def transport(endpoint, body):
                return provider_response(json.loads(body["messages"][1]["content"]))
            with mock.patch.object(semantic.AnalyzerEndpoint, "from_env", return_value=primary), mock.patch.object(semantic, "http_transport", side_effect=transport), mock.patch.object(pipeline, "run_accounting", side_effect=AssertionError("Semantic-only must not allocate")), mock.patch.dict("os.environ", {"CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED": "approved"}):
                plan = command.run(command.parse_args(argv + ["--plan"]))
                self.assertIn("scope_file_sha256", plan)
                self.assertEqual(hashlib.sha256(scope_file.read_bytes()).hexdigest(), plan["scope_file_sha256"])
                self.assertEqual("evidence_ids", plan["scope_key"])
                self.assertFalse((root / "output").exists())
                result = command.run(command.parse_args(argv))
            self.assertEqual(0, result["new_proposal_count"])
            self.assertEqual(plan["scope_file_sha256"], result["scope_file_sha256"])
            self.assertEqual(meeting_bytes, (root / "output/preserved-source-proposals.json").read_bytes())
            self.assertEqual(scope_file.read_bytes(), (root / "output/source-scope-input.json").read_bytes())
            self.assertEqual((source_dir / "semantic-analysis.json").read_bytes(), (root / "output/source-semantic-analysis.json").read_bytes())
            self.assertFalse((root / "output/proposals.json").exists())
            self.assertFalse((root / "output/work-accounting-result.json").exists())
            self.assertTrue((root / "output/semantic-analysis.json").exists())
            self.assertTrue((root / "output/timing-overlaps.json").exists())
            timing = json.loads((root / "output/timing-overlaps.json").read_text())
            contested = next(row for row in timing["activities"] if set(row["evidence_ids"]) == set(contexts[0]))
            self.assertEqual(4, contested["observed_whole_minute_capacity"])
            self.assertEqual(0, contested["unoccupied_whole_minute_capacity"])
            self.assertEqual(255, contested["overlaps"][0]["seconds"])
            self.assertTrue(timing["temporal_overlap_is_not_financial_credit"])
            self.assertTrue(timing["capacities_are_not_effort_estimates"])
            recovered = json.loads((root / "output/semantic-analysis.json").read_text())
            used_cache = root / "output/analyzer-cache-used.jsonl"
            pipeline._failed_review_retry_targets(recovered, events, used_cache, semantic.stable_digest("frt-", sorted(recovered["exceptions"][0]["evidence_ids"]), length=64))
            self.assertEqual(snapshots, {p: p.read_bytes() for p in source_dir.rglob("*") if p.is_file()})

    def test_normalized_point_report_preserves_positive_clusters_and_recorded_overlap(self):
        # Catches substituting point bounds for the actual allocator's clustering.
        command = importlib.import_module("scripts.clockify_scoped_semantic_recovery")
        timestamps = ["2026-09-14T14:12:17.739000+03:00", "2026-09-14T14:57:06.263000+03:00", "2026-09-14T16:41:55.081000+03:00", "2026-09-14T16:42:54.873000+03:00"]
        # The first interval needs intervening points to satisfy the exact1800s gap policy.
        timestamps.insert(1, "2026-09-14T14:35:00.000000+03:00")
        events = [evidence_ledger.evidence_event("codex_sessions_event", {"source_type": "codex_sessions", "source_id": f"point:{i}", "machine": "fixture", "session_id": "normalized-points", "ordinal": i + 1}, observed_at=timestamp, raw_source_span={"timestamp": timestamp}, attributes={"role": "user" if i == 0 else "assistant", "kind": "message", "content": "Human-directed work"}).document() for i, timestamp in enumerate(timestamps)]
        ids = [event["evidence_id"] for event in events]
        existing = evidence_ledger.evidence_event("clockify", {"source_id": "occupied"}, raw_source_span={"start": "2026-09-14T16:42:00+03:00", "end": "2026-09-14T16:42:30+03:00"}, attributes={"description": "Existing time"}).document()
        activity = {"activity_id": "normalized-point-activity", "evidence_ids": ids}
        proposal = {"id": "P001", "start": "2026-09-14T13:35:19+03:00", "end": "2026-09-14T14:37:18+03:00"}
        report = command._timing_report([activity], events + [existing], [proposal])
        row = report["activities"][0]
        self.assertEqual(44, row["observed_whole_minute_capacity"])
        self.assertEqual(19, row["unoccupied_whole_minute_capacity"])
        self.assertEqual([{"start": timestamps[0], "end": timestamps[2]}, {"start": timestamps[3], "end": timestamps[4]}], row["observed_intervals"])
        self.assertEqual(["source_proposal", "existing_clockify"], [overlap["block_kind"] for overlap in row["overlaps"]])
        self.assertAlmostEqual(1500.261, row["overlaps"][0]["seconds"])
        self.assertEqual(30, row["overlaps"][1]["seconds"])
        self.assertTrue(report["capacities_are_not_effort_estimates"])
        self.assertTrue(report["temporal_overlap_is_not_financial_credit"])


if __name__ == "__main__":
    unittest.main()
