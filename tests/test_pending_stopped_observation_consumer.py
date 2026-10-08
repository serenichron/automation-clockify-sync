"""Explicit old stopped-only admission must survive real pending derivation replay."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts import clockify_checkpoint_snapshot as native
from scripts import collector_checkpoints, collector_receipts, evidence_ledger
from scripts import clockify_review_run as review, clockify_sync_collect as collector
from test_pending_collector_derivation import pending_fixture, write_empty_actor_fixture, SINCE, UNTIL


ENTRY = {
    "id": "0123456789abcdef01234567", "workspaceId": "workspace-one",
    "userId": "user-one", "description": "Stopped historical work",
    "projectId": "project-123456", "tagIds": ["tag-01234567"],
    "taskId": None, "billable": False,
    "timeInterval": {"start": "2026-09-01T09:00:00Z",
                     "end": "2026-09-01T09:50:38Z", "duration": "PT50M38S"},
}
COMPATIBILITY = {"mode": "stopped-only-legacy-observation-variance/v1",
                 "checkpoint_observed_at": "2026-09-03T00:00:00Z",
                 "source_observed_at": "2026-09-05T00:00:00Z"}


def immutable_inventory(root):
    return {str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob("*") if p.is_file()}


def stopped_fixture(root):
    """Construct then copy a fully receipted old raw graph; no production I/O."""
    original = root / "original"
    original.mkdir()
    append_page = collector_checkpoints.PageCheckpointStore.append_page

    def append_stopped(store, state, *, payload, continuation, signature):
        payload = [copy.deepcopy(ENTRY)]
        return append_page(store, state, payload=payload, continuation=continuation,
                           signature=collector._clockify_page_signature(payload))

    with mock.patch.object(collector_checkpoints.PageCheckpointStore, "append_page", append_stopped):
        runs, source, checkpoints, snapshots = pending_fixture(original)
    evidence_path = source / "evidence/clockify-existing.json"
    clockify = json.loads(evidence_path.read_bytes())
    clockify["entries"][0]["start"] = "2026-09-01 12:00"
    clockify["entries"][0]["end"] = "2026-09-01 12:50"
    clockify["collection_snapshot"] = {"observed_at": "2026-09-05T00:00:00Z",
        "boundary": "2026-09-01T21:00:00Z", "requested_until": "2026-09-01T21:00:00Z"}
    evidence_path.write_text(json.dumps(clockify) + "\n")
    report_path = source / "run-report.json"
    report = json.loads(report_path.read_bytes())
    old_digest = report["evidence_ledger"]["ledger_digest"]
    report["evidence"]["clockify"] = clockify
    raw = {key: json.loads((source / relative).read_bytes())
           for key, relative in collector_receipts._COLLECTOR_RAW_ARTIFACTS.items()}
    ledger = evidence_ledger.EvidenceLedger(
        tuple(evidence_ledger.normalize_collector_snapshot(raw)),
        evidence_ledger.source_inventory_from_collector(raw),
        "Europe/Bucharest", ("member@example.invalid",))
    ledger_path = source / "evidence/evidence-ledger.json"
    ledger_path.write_text(json.dumps({"schema_version": ledger.manifest.schema_version,
        "manifest": ledger.manifest.document(), "events": [e.document() for e in ledger.events]}) + "\n")
    new_digest = "sha256:" + hashlib.sha256(ledger_path.read_bytes()).hexdigest()
    report["evidence_ledger"] = {"manifest_id": ledger.manifest.manifest_id,
        "event_count": ledger.manifest.event_count, "events_digest": ledger.manifest.events_digest,
        "source_completeness": ledger.manifest.document()["source_completeness"], "ledger_digest": new_digest}
    report_path.write_text(json.dumps(report) + "\n")
    markdown = source / "run-report.md"
    markdown.write_text(markdown.read_text().replace(old_digest, new_digest))
    copied = root / "copied"
    shutil.copytree(original, copied)
    return copied / "runs", copied / "runs" / source.name, copied / "checkpoints", {
        name: copied / path.relative_to(original) for name, path in snapshots.items()}


class PendingStoppedObservationConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runs, self.source, self.checkpoints, self.snapshots = stopped_fixture(self.root)
        self.runs_patch = mock.patch.object(review, "RUNS", self.runs)
        self.runs_patch.start()
        self.addCleanup(self.runs_patch.stop)

    def prepare(self, **kwargs):
        return review._prepare_collector_derivation_run(self.source, self.snapshots,
            pending_checkpoint_root=self.checkpoints, executor_runtime_identity={"git_sha": "fixture"},
            environment={}, **kwargs)

    def test_opt_in_admits_immutable_old_source_and_replays_native_child(self):
        before = {"source": immutable_inventory(self.source), "checkpoints": immutable_inventory(self.checkpoints)}
        with self.assertRaises(ValueError):
            self.prepare()
        try:
            child = self.prepare(allow_stopped_only_legacy_observation_variance=True)
        except (TypeError, ValueError) as error:
            self.fail(f"explicit pending admission must accept stopped-only observation variance: {error}")
        source, identity, lineage = review._verified_collector_derivation(child)
        self.assertEqual(self.source, source)
        self.assertTrue(identity.pending_binding["allow_stopped_only_legacy_observation_variance"])
        self.assertEqual(COMPATIBILITY, identity.pending_binding["observation_compatibility"])
        self.assertEqual(identity.pending_binding, lineage["pending_source_binding"])
        report = json.loads((child / "run-report.json").read_bytes())
        loaded = native.load_checkpoint_snapshot(child / "evidence/clockify-native-checkpoint",
            workspace_id="workspace-one", user_id="user-one", since=SINCE, until=UNTIL,
            expected_manifest_sha256=report["clockify_native_checkpoint"]["manifest_sha256"])
        self.assertEqual([ENTRY], loaded.entries)
        self.assertEqual("2026-09-03T00:00:00Z", loaded.manifest["snapshot_at"])
        self.assertEqual("clockify-native-checkpoint-snapshot/v2", loaded.manifest["schema_version"])
        self.assertEqual(COMPATIBILITY, loaded.manifest["observation_compatibility"])
        self.assertEqual(before, {"source": immutable_inventory(self.source), "checkpoints": immutable_inventory(self.checkpoints)})
        self.assertEqual(child, self.prepare(allow_stopped_only_legacy_observation_variance=True))

    def test_cli_option_is_explicit_and_only_valid_for_pending_derivation(self):
        flag = "--allow-stopped-only-legacy-observation-variance"
        try:
            args = review.parse_args(["--derive-pending-from", str(self.source),
                                      "--routing", str(self.snapshots["routing.json"]), flag])
        except SystemExit as error:
            self.fail(f"explicit pending compatibility CLI flag must be recognized: {error}")
        self.assertTrue(args.allow_stopped_only_legacy_observation_variance)
        self.assertEqual(2, review.main([flag]))

    def test_malformed_opt_in_and_nonpending_use_fail_before_child_creation(self):
        for value in ("true", 1, None):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    self.prepare(allow_stopped_only_legacy_observation_variance=value)
                with self.assertRaises(ValueError):
                    collector_receipts.load_pending_collector_source(self.source, checkpoint_root=self.checkpoints,
                        allow_stopped_only_legacy_observation_variance=value)
        with self.assertRaises(ValueError):
            review._prepare_collector_derivation_run(self.source, self.snapshots,
                allow_stopped_only_legacy_observation_variance=True)
        self.assertEqual([], list(self.runs.glob("collector-derivation-*")))

    def test_recomputed_lineage_cannot_change_compatibility_intent_or_metadata(self):
        child = self.prepare(allow_stopped_only_legacy_observation_variance=True)
        path = child / "collector-source.json"
        original = path.read_bytes()
        for change in ("remove", "string", "checkpoint", "source", "mode", "missing", "extra"):
            with self.subTest(change=change):
                lineage = json.loads(original)
                binding = lineage["pending_source_binding"]
                if change == "remove":
                    binding.pop("allow_stopped_only_legacy_observation_variance")
                    binding.pop("observation_compatibility")
                elif change == "string":
                    binding["allow_stopped_only_legacy_observation_variance"] = "true"
                elif change == "missing":
                    binding.pop("observation_compatibility")
                elif change == "extra":
                    binding["observation_compatibility"]["extra"] = "ignored?"
                else:
                    key = {"checkpoint": "checkpoint_observed_at", "source": "source_observed_at", "mode": "mode"}[change]
                    binding["observation_compatibility"][key] = "forged"
                lineage.pop("lineage_digest")
                lineage["lineage_digest"] = "sha256:" + hashlib.sha256(
                    json.dumps(lineage, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                path.write_text(json.dumps(lineage))
                with self.assertRaises(ValueError):
                    review._verified_collector_derivation(child)
                path.write_bytes(original)
        review._verified_collector_derivation(child)

    def test_cli_derives_and_seals_only_child_then_replays_offline(self):
        before = {"source": immutable_inventory(self.source), "checkpoints": immutable_inventory(self.checkpoints)}
        fixture = self.root / "analysis.json"
        write_empty_actor_fixture(fixture)
        environment = {**os.environ, "CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT": str(self.checkpoints),
            "PYTHONDONTWRITEBYTECODE": "1", "CLOCKIFY_ANALYZER_PRIMARY_URL": "http://127.0.0.1:1/forbidden"}
        result = subprocess.run([sys.executable, "-B", str(Path(review.__file__)),
            "--runs-root", str(self.runs), "--derive-pending-from", str(self.source),
            "--allow-stopped-only-legacy-observation-variance",
            "--routing", str(self.snapshots["routing.json"]), "--analysis-fixture", str(fixture),
            "--state", str(self.root / "review-state.json")],
            env=environment, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr + result.stdout)
        child = Path(result.stdout.strip().splitlines()[-1]).parent
        self.assertTrue((child / "completion-bundle.json").is_file())
        self.assertFalse((self.source / "completion-bundle.json").exists())
        _, identity, _ = review._verified_collector_derivation(child)
        self.assertEqual(COMPATIBILITY, identity.pending_binding["observation_compatibility"])
        replay = subprocess.run([sys.executable, "-B", str(Path(review.__file__)),
            "--runs-root", str(self.runs), "--replay-from", str(child),
            "--state", str(self.root / "review-state.json")],
            env=environment, capture_output=True, text=True)
        self.assertEqual(0, replay.returncode, replay.stderr + replay.stdout)
        replay_dir = Path(replay.stdout.strip().splitlines()[-1]).parent
        self.assertEqual("pass", json.loads((replay_dir / "replay-integrity.json").read_bytes())["status"])
        self.assertEqual(before, {"source": immutable_inventory(self.source), "checkpoints": immutable_inventory(self.checkpoints)})

    def test_compatibility_replay_rejects_original_and_child_native_byte_drift(self):
        child = self.prepare(allow_stopped_only_legacy_observation_variance=True)
        paths = [self.source / "evidence/clockify-existing.json"]
        paths += list(self.checkpoints.rglob("*.json"))
        paths += list((child / "evidence/clockify-native-checkpoint").rglob("*.json"))
        for path in paths:
            with self.subTest(path=str(path)):
                original = path.read_bytes()
                try:
                    path.write_bytes(original + b" ")
                    with self.assertRaises(ValueError):
                        review._verified_collector_derivation(child)
                finally:
                    path.write_bytes(original)
        review._verified_collector_derivation(child)


if __name__ == "__main__":
    unittest.main()
