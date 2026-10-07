"""Real sealed-cache replay across an accounting-only routing repair."""
import json
import socket
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_review_run as fixtures
from test_review_run_chained_repair_replay import validate_repair
from scripts import work_accounting_pipeline as accounting
from scripts import semantic_analyzer as semantic


review = fixtures.review_run


class RoutingRepairReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.runs = cls.root / "runs"
        cls.original = fixtures.ReviewRunResultTests._write_real_offline_replay_source(cls.runs, cls.root)
        cls.original_routing = (cls.original / "routing.json").read_bytes()
        routing = json.loads(cls.original_routing)
        routing["session_routes"][0].update(project_suffix="654321", confidence="high")
        cls.override = cls.root / "new-accounting-routing.json"
        fixtures.write_json(cls.override, routing)

    def repaired(self):
        with mock.patch.object(review, "RUNS", self.runs):
            repair = review._prepare_repair_run(self.original, routing_override=self.override)
            fixture = review._repair_analysis_fixture(repair)
            accounting.run_accounting(repair, root=fixtures.ROOT, routing_path=repair / "routing.json", corrections_path=repair / "review-corrections.jsonl", analysis_fixture=fixture)
            validate_repair(repair, self.runs, self.root / (repair.name + "-state.json"))
            review._finalize_repair_completion(repair)
        return repair

    def test_changed_accounting_route_replays_exact_old_cache_without_transport(self):
        """Catches reconstructing inference bodies using changed accounting hints."""
        repair = self.repaired()
        before = fixtures.run_tree_snapshot(self.original, repair)
        with mock.patch.object(review, "RUNS", self.runs), mock.patch.object(review, "_sealed_replay_transport", side_effect=AssertionError("network/inference forbidden")):
            replay = review._prepare_replay_run(repair)
            accounting.run_accounting(replay, root=fixtures.ROOT, routing_path=replay / "routing.json", corrections_path=replay / "review-corrections.jsonl", analysis_fixture=review._replay_analysis_fixture(repair, replay), analyzer_cache_path=review._replay_analyzer_cache(replay))
            self.assertEqual("pass", review._verify_replay_integrity(repair, replay)["status"])
        self.assertEqual(before, fixtures.run_tree_snapshot(self.original, repair))
        self.assertEqual(self.override.read_bytes(), (replay / "routing.json").read_bytes())
        self.assertEqual((repair / "proposals.json").read_bytes(), (replay / "proposals.json").read_bytes())
        proposals = json.loads((replay / "proposals.json").read_bytes())
        self.assertEqual("654321", proposals[0]["clockify_project_suffix"])
        self.assertEqual((self.original / "analyzer-cache-used.jsonl").read_bytes(), (repair / "analyzer-cache-used.jsonl").read_bytes())

    def test_tampered_original_inference_routing_is_rejected(self):
        """Catches replay trusting a changed ancestor's inference route snapshot."""
        repair = self.repaired()
        path = self.original / "routing.json"
        original = path.read_bytes()
        try:
            modified = json.loads(original)
            modified["session_routes"][0]["confidence"] = "medium"
            fixtures.write_json(path, modified)
            with mock.patch.object(review, "RUNS", self.runs), self.assertRaisesRegex(ValueError, "inference.*routing|routing provenance"):
                review._prepare_replay_run(repair)
        finally:
            path.write_bytes(original)

    def test_tampered_original_completion_is_rejected(self):
        """Catches inference-context ancestry whose original artifacts no longer seal."""
        repair = self.repaired()
        path = self.original / "completion-bundle.json"
        original = path.read_bytes()
        try:
            path.write_bytes(original + b" ")
            with mock.patch.object(review, "RUNS", self.runs), self.assertRaisesRegex(ValueError, "inference.*completion|source completion"):
                review._prepare_replay_run(repair)
        finally:
            path.write_bytes(original)

    def test_normal_replay_still_uses_its_own_snapshot(self):
        """Catches changed repair handling disturbing the ordinary sealed replay."""
        original_before = fixtures.run_tree_snapshot(self.original)
        with mock.patch.object(review, "RUNS", self.runs), mock.patch.object(review, "_sealed_replay_transport", side_effect=AssertionError("network/inference forbidden")):
            replay = review._prepare_replay_run(self.original)
            accounting.run_accounting(replay, root=fixtures.ROOT, routing_path=replay / "routing.json", corrections_path=replay / "review-corrections.jsonl", analysis_fixture=review._replay_analysis_fixture(self.original, replay), analyzer_cache_path=review._replay_analyzer_cache(replay))
            self.assertEqual("pass", review._verify_replay_integrity(self.original, replay)["status"])
        self.assertEqual(original_before, fixtures.run_tree_snapshot(self.original))
        self.assertEqual(self.original_routing, (replay / "routing.json").read_bytes())

    def test_tampered_repair_cache_binding_is_rejected_before_cache_reconstruction(self):
        """Catches a forged repair lineage silently choosing older inference inputs."""
        repair = self.repaired()
        path = repair / "repair-source.json"
        lineage = json.loads(path.read_bytes())
        lineage["analyzer_cache_sha256"] = "0" * 64
        fixtures.write_json(path, lineage)
        with mock.patch.object(review, "RUNS", self.runs), self.assertRaisesRegex(ValueError, "inference sealed cache provenance changed"):
            review._prepare_replay_run(repair)

    def test_tampered_repair_ledger_binding_is_rejected(self):
        """Catches using old inference routing for a different immutable ledger."""
        repair = self.repaired()
        path = repair / "repair-source.json"
        lineage = json.loads(path.read_bytes())
        lineage["ledger_identity"]["file_sha256"] = "0" * 64
        fixtures.write_json(path, lineage)
        with mock.patch.object(review, "RUNS", self.runs), self.assertRaisesRegex(ValueError, "inference immutable evidence changed"):
            review._prepare_replay_run(repair)

    def test_broken_lineage_symlink_cannot_hide_inference_ancestry(self):
        """Catches treating an invalid lineage link as an original source."""
        for name in ("repair-source.json", "replay-source.json"):
            with self.subTest(name=name):
                source = self.runs / ("broken-" + name)
                source.mkdir()
                (source / name).symlink_to(source / "missing-lineage.json")
                with mock.patch.object(review, "RUNS", self.runs), self.assertRaises(ValueError):
                    review._verified_replay_inference_context(source)


class ActorContractReplayTests(unittest.TestCase):
    def test_native_entrypoint_replays_sealed_actor_binding_and_corrected_hints_without_transport(self):
        # Catches reconstruction silently using v17 projection or frozen hints.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            binding = {"source_type": "multica", "server_origin": "https://offline.invalid", "workspace_id": "private-workspace", "author_id": "private-subject"}
            source = fixtures.ReviewRunResultTests._write_real_offline_replay_source(runs, root, actor_contract=True, actor_subject_binding=binding)
            source_analysis = json.loads((source / "semantic-analysis.json").read_bytes())
            self.assertEqual("clockify-semantic-actors/v1", source_analysis["actor_contract"])
            before = fixtures.run_tree_snapshot(source)
            sealed_transport = review._sealed_replay_transport
            def refuse_provider(endpoint, body):
                if json.loads(body["messages"][-1]["content"]) == {"probe": semantic.PROMPT_VERSION}:
                    return sealed_transport(endpoint, body)
                self.fail("Actor replay inference forbidden")
            with mock.patch.object(review, "RUNS", runs), mock.patch.object(review, "_sealed_replay_transport", side_effect=refuse_provider), mock.patch.object(socket.socket, "connect", side_effect=AssertionError("Actor replay network forbidden")):
                replay = review._prepare_replay_run(source)
                accounting.run_accounting(replay, root=fixtures.ROOT, routing_path=replay / "routing.json", corrections_path=replay / "review-corrections.jsonl", analysis_fixture=review._replay_analysis_fixture(source, replay), analyzer_cache_path=review._replay_analyzer_cache(replay))
                self.assertEqual("pass", review._verify_replay_integrity(source, replay)["status"])
            self.assertEqual(before, fixtures.run_tree_snapshot(source))
            self.assertEqual((source / "proposals.json").read_bytes(), (replay / "proposals.json").read_bytes())
            self.assertEqual((source / "work-accounting-result.json").read_bytes(), (replay / "work-accounting-result.json").read_bytes())

    def test_native_entrypoint_rejects_unsupported_actor_contract_before_transport(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            source = fixtures.ReviewRunResultTests._write_real_offline_replay_source(runs, root)
            analysis = json.loads((source / "semantic-analysis.json").read_bytes())
            analysis["actor_contract"] = "unsupported"
            with mock.patch.object(review, "RUNS", runs), mock.patch.object(review, "_sealed_replay_transport", side_effect=AssertionError("Unsupported contract inferred")), self.assertRaisesRegex(ValueError, "actor contract"):
                review._preflight_replay_analyzer_cache(source, source / "analyzer-cache-used.jsonl", analysis)


if __name__ == "__main__":
    unittest.main()
