"""Native proof transport must not alter semantic ledger identity or reopen caches."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import clockify_checkpoint_snapshot as snapshot
from scripts import clockify_review_run as review_run
from scripts import clockify_sync_collect as collector
from scripts import collector_checkpoints as checkpoints
from scripts import collector_receipts, evidence_ledger
from scripts import reconciliation_manifest
from scripts.collector_slices import CollectionSlice
from test_clockify_checkpoint_snapshot import ENTRY, EVIDENCE, OBSERVED, SINCE, UNTIL


PREFIX = "evidence/clockify-native-checkpoint/"


class NativeCheckpointRunTransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runs = self.root / "runs"
        self.source = self.runs / "source"
        self.evidence = self.source / "evidence"
        self.evidence.mkdir(parents=True)
        self.slice = CollectionSlice(SINCE, UNTIL, "native-source")
        self.raw = {
            "clockify": EVIDENCE,
            "fathom": {"status": "complete", "meetings": []},
            "calendly": {"status": "complete", "recordings": []},
            "multica_issues": {"status": "complete", "issues": []},
            "sessions": [],
        }
        for key, filename in collector_receipts._COLLECTOR_RAW_ARTIFACTS.items():
            (self.source / filename).write_text(json.dumps(self.raw[key], indent=2) + "\n")
        ledger = evidence_ledger.EvidenceLedger(
            tuple(evidence_ledger.normalize_collector_snapshot(self.raw)),
            evidence_ledger.source_inventory_from_collector(self.raw),
        )
        self.ledger_bytes = (json.dumps({
            "schema_version": evidence_ledger.SCHEMA_VERSION,
            "manifest": ledger.manifest.document(),
            "events": [event.document() for event in ledger.events],
        }) + "\n").encode()
        (self.evidence / "evidence-ledger.json").write_bytes(self.ledger_bytes)
        self.report = {
            "runtime_identity": {"git_sha": "collector"},
            "date_range": {"since": "2026-09-01T00:00:00Z", "until": "2026-10-01T00:00:00Z"},
            "evidence_ledger": {"source_completeness": ledger.manifest.document()["source_completeness"]},
        }
        for relative in ("semantic-analysis.json", "work-accounting-result.json",
                         "quality_report.json", "review-snapshot.json"):
            (self.source / relative).write_text("{}\n")
        (self.source / "run-report.md").write_text("collector report\n")
        for name in review_run._RECONCILIATION_INPUTS.values():
            (self.source / name).write_text("{}\n")
        self.snapshot_dir = self.evidence / "clockify-native-checkpoint"
        self.bundle_path = self.source / "completion-bundle.json"

    def seal(self):
        (self.source / "run-report.json").write_text(json.dumps(self.report) + "\n")
        collector_receipts.write_completion_bundle(
            self.bundle_path,
            collector_receipts.build_completion_bundle(self.source, slice_=self.slice),
        )

    def capture(self):
        store = checkpoints.PageCheckpointStore(self.root / "original-cache")
        identity = collector._clockify_checkpoint_identity("workspace-one", "user-one", SINCE, UNTIL)
        state = store.open(identity, initial_metadata={"snapshot_at": OBSERVED})
        state = store.append_page(state, payload=[ENTRY], continuation={"page": 2},
                                  signature=collector._clockify_page_signature([ENTRY]))
        state = store.mark_complete(state)
        original = self.root / "original-run/evidence/clockify-existing.json"
        original.parent.mkdir(parents=True)
        original.write_bytes((self.evidence / "clockify-existing.json").read_bytes())
        captured = snapshot.capture_checkpoint_snapshot(
            checkpoint_manifest=state.directory / "manifest.json", clockify_evidence=original,
            destination=self.snapshot_dir, workspace_id="workspace-one", user_id="user-one",
            since=SINCE, until=UNTIL,
        )
        self.report["clockify_native_checkpoint"] = {
            "manifest_sha256": captured.manifest_sha256,
            "request": {"workspace_id": "workspace-one", "user_id": "user-one",
                        "since_utc": "2026-09-01T00:00:00Z", "until_utc": "2026-10-01T00:00:00Z"},
        }
        return captured, state.directory, original

    def load(self):
        return collector_receipts.load_collector_source_bundle(self.bundle_path, run_dir=self.source)

    def derive(self):
        with patch.object(review_run, "RUNS", self.runs):
            return review_run._prepare_collector_derivation_run(
                self.source, {name: self.source / name for name in review_run._RECONCILIATION_INPUTS.values()},
                executor_runtime_identity={"git_sha": "executor"}, environment={},
            )

    def downstream_fixture(self, *, native=True):
        captured = self.capture()[0] if native else None
        (self.source / review_run._CANONICAL_MEETING_RECONCILIATION).write_text("[]\n")
        (self.source / "work-accounting-result.json").write_text(json.dumps({
            "schema_version": 1, "allocation_mode": "non_overlapping_v1", "proposals": [],
        }) + "\n")
        (self.source / "semantic-analysis.json").write_text(json.dumps({
            "schema_version": 1, "prompt_version": "fixture-v1",
            "evidence_bundle_schema_version": "clockify-semantic-evidence-bundle/v1",
            "evidence_bundle_manifest": {
                "schema_version": "clockify-semantic-evidence-bundle/v1",
                "digest": review_run.semantic_analyzer.stable_digest("sebm-", [], length=64),
                "bundles": [],
            },
            "ledger_evidence_digest": "led-" + "a" * 16,
            "activities": [{"analyzer_model": "offline-fixture", "analyzer_tier": "fixture"}],
            "analysis_chunks": [],
        }) + "\n")
        manifest = {
            "schema_version": reconciliation_manifest.MANIFEST_COMPATIBILITY_VERSION,
            "compatibility_version": reconciliation_manifest.MANIFEST_COMPATIBILITY_VERSION,
            "period": {
                "compatibility_version": reconciliation_manifest.PERIOD_COMPATIBILITY_VERSION,
                "member_id": "member-fixture", "workspace_id": "workspace-one",
                "timezone": "Europe/Bucharest", "since_utc": "2026-09-01T00:00:00Z",
                "until_utc": "2026-10-01T00:00:00Z", "revision": 1,
            },
            "state": "collecting", "event_count": 1,
            "events_digest": "sha256:" + "d" * 64, "artifacts": [], "blockers": [],
        }
        manifest["manifest_digest"] = reconciliation_manifest._digest(manifest)
        (self.source / "period-manifest.json").write_text(json.dumps(manifest) + "\n")
        self.seal()
        return captured

    def verify_downstream_proof(self, target, captured):
        for relative, raw in captured.verified_artifact_bytes.items():
            self.assertEqual(raw, (target / PREFIX / relative).read_bytes())
        self.assertEqual((self.evidence / "clockify-existing.json").read_bytes(),
                         (target / "evidence/clockify-existing.json").read_bytes())
        loaded = snapshot.load_checkpoint_snapshot(
            target / PREFIX, workspace_id="workspace-one", user_id="user-one", since=SINCE, until=UNTIL,
            expected_manifest_sha256=captured.manifest_sha256,
        )
        self.assertEqual([ENTRY], loaded.entries)

    def test_replay_copies_native_proof_and_binds_it_to_integrity(self):
        captured = self.downstream_fixture()
        with patch.object(review_run, "RUNS", self.runs), patch.object(
            collector, "clockify_get", side_effect=AssertionError("network forbidden")
        ):
            target = review_run._prepare_replay_run(self.source)
            self.assertTrue((target / PREFIX / "snapshot.json").exists(), "native replay proof missing")
            self.verify_downstream_proof(target, captured)
            review_run._replay_analysis_fixture(self.source, target)
            for filename in ("semantic-analysis.json", "work-accounting-result.json"):
                (target / filename).write_bytes((self.source / filename).read_bytes())
            self.assertEqual("pass", review_run.derive_replay_integrity(self.source, target)["status"])
            proof = target / PREFIX / "snapshot.json"
            raw = proof.read_bytes()
            proof.write_bytes(raw + b" ")
            with self.assertRaisesRegex(review_run.ReviewRunError, "native checkpoint"):
                review_run._replay_analysis_fixture(self.source, target)
            integrity = review_run.derive_replay_integrity(self.source, target)
            self.assertEqual("blocked", integrity["status"])
            self.assertIn("replay source provenance differs", integrity["failures"])
            proof.write_bytes(raw)
            report_path = target / "run-report.json"
            report = json.loads(report_path.read_bytes())
            report["clockify_native_checkpoint"]["request"]["user_id"] = "foreign"
            report_path.write_text(json.dumps(report) + "\n")
            self.assertEqual("blocked", review_run.derive_replay_integrity(self.source, target)["status"])

    def test_repair_preserves_original_native_basis_and_rejects_tampered_copy(self):
        captured = self.downstream_fixture()
        before = {p.relative_to(self.source).as_posix(): p.read_bytes()
                  for p in self.source.rglob("*") if p.is_file()}
        with patch.object(review_run, "RUNS", self.runs):
            target = review_run._prepare_repair_run(self.source)
            self.assertTrue((target / PREFIX / "snapshot.json").exists(), "native repair proof missing")
            self.verify_downstream_proof(target, captured)
            review_run._repair_analysis_fixture(target)
            for filename in ("semantic-analysis.json", "work-accounting-result.json",
                             "quality_report.json", "review-snapshot.json"):
                (target / filename).write_bytes((self.source / filename).read_bytes())
            review_run._finalize_repair_completion(target)
            report_path = target / "run-report.json"
            raw = report_path.read_bytes()
            report = json.loads(raw)
            report["clockify_native_checkpoint"]["manifest_sha256"] = "0" * 64
            report_path.write_text(json.dumps(report) + "\n")
            with self.assertRaisesRegex(review_run.ReviewRunError, "native checkpoint"):
                review_run._repair_analysis_fixture(target)
            report_path.write_bytes(raw)
            proof = target / PREFIX / "clockify-existing.json"
            proof.write_bytes(proof.read_bytes() + b" ")
            with self.assertRaisesRegex(review_run.ReviewRunError, "native checkpoint"):
                review_run._finalize_repair_completion(target)
        self.assertEqual(before, {p.relative_to(self.source).as_posix(): p.read_bytes()
                                 for p in self.source.rglob("*") if p.is_file()})

    def test_replay_and_repair_reject_unsealed_native_metadata_before_destination(self):
        self.downstream_fixture()
        report_path = self.source / "run-report.json"
        report = json.loads(report_path.read_bytes())
        report["clockify_native_checkpoint"]["request"]["user_id"] = "unsealed"
        report_path.write_text(json.dumps(report) + "\n")
        with patch.object(review_run, "RUNS", self.runs):
            with self.assertRaises(ValueError):
                review_run._prepare_replay_run(self.source)
            with self.assertRaises(ValueError):
                review_run._prepare_repair_run(self.source)
        self.assertEqual([self.source], list(self.runs.iterdir()))

    def test_legacy_replay_and_repair_still_work_without_native_metadata(self):
        self.downstream_fixture(native=False)
        self.snapshot_dir.mkdir()
        (self.snapshot_dir / "snapshot.json").write_text("unbound legacy artifact")
        with patch.object(review_run, "RUNS", self.runs):
            replay = review_run._prepare_replay_run(self.source)
            repair = review_run._prepare_repair_run(self.source)
            review_run._replay_analysis_fixture(self.source, replay)
            review_run._repair_analysis_fixture(repair)
        for target, lineage in ((replay, "replay-source.json"), (repair, "repair-source.json")):
            self.assertFalse((target / PREFIX).exists())
            self.assertNotIn("clockify_native_checkpoint", json.loads((target / lineage).read_bytes()))

    def test_bound_native_pages_survive_source_bundle_and_derivation_without_semantic_changes(self):
        """Catches dropping full IDs/proof during derivation or normalizing them into the ledger."""
        captured, original_cache, original_evidence = self.capture()
        self.seal()
        original_cache.joinpath("manifest.json").write_text("obsolete original cache")
        original_evidence.write_text("obsolete original evidence")
        with patch.object(collector, "clockify_get", side_effect=AssertionError("network forbidden")):
            bound = self.load()
            for relative, raw in captured.verified_artifact_bytes.items():
                self.assertEqual(raw, bound.verified_artifact_bytes.get(PREFIX + relative), relative)
                self.assertEqual("sha256:" + hashlib.sha256(raw).hexdigest(),
                                 bound.verified_artifact_digests.get(PREFIX + relative))
            child = self.derive()
            with patch.object(review_run, "RUNS", self.runs):
                review_run._verified_collector_derivation(child)
        self.assertEqual(self.ledger_bytes, (child / "evidence/evidence-ledger.json").read_bytes())
        for relative, raw in captured.verified_artifact_bytes.items():
            self.assertEqual(raw, (child / PREFIX / relative).read_bytes())
        lineage = json.loads((child / "collector-source.json").read_bytes())
        self.assertTrue(set(PREFIX + name for name in captured.verified_artifact_bytes)
                        <= set(lineage["derived_artifact_digests"]))

    def test_absent_metadata_ignores_stray_directory_and_preserves_legacy_identity(self):
        self.seal()
        legacy = self.load()
        self.snapshot_dir.mkdir()
        (self.snapshot_dir / "snapshot.json").write_text("unbound junk")
        stray = self.load()
        self.assertEqual(legacy.source_bundle_digest, stray.source_bundle_digest)
        self.assertEqual(legacy.verified_artifact_digests, stray.verified_artifact_digests)
        child = self.derive()
        self.assertFalse((child / PREFIX).exists())
        with patch.object(review_run, "RUNS", self.runs):
            review_run._verified_collector_derivation(child)

    def test_source_rejects_period_outside_completion_slice(self):
        self.capture()
        self.report["clockify_native_checkpoint"]["request"]["since_utc"] = "2026-08-31T00:00:00Z"
        self.seal()
        with self.assertRaisesRegex(collector_receipts.CollectorReceiptError, "native checkpoint"):
            self.load()

    def test_source_rejects_sidecar_evidence_that_is_only_semantically_equal(self):
        self.capture()
        proof_path = self.snapshot_dir / "snapshot.json"
        proof = json.loads(proof_path.read_bytes())
        raw = (json.dumps(EVIDENCE, separators=(",", ":")) + "\n").encode()
        (self.snapshot_dir / "clockify-existing.json").write_bytes(raw)
        proof["files"]["clockify-existing.json"] = hashlib.sha256(raw).hexdigest()
        proof_bytes = (json.dumps(proof, sort_keys=True) + "\n").encode()
        proof_path.write_bytes(proof_bytes)
        self.report["clockify_native_checkpoint"]["manifest_sha256"] = hashlib.sha256(proof_bytes).hexdigest()
        self.seal()
        with self.assertRaisesRegex(collector_receipts.CollectorReceiptError, "native checkpoint"):
            self.load()

    def test_source_rejects_tampered_or_extra_native_artifacts(self):
        self.capture()
        self.seal()
        proof = self.snapshot_dir / "snapshot.json"
        raw = proof.read_bytes()
        proof.write_bytes(raw + b" ")
        with self.assertRaisesRegex(collector_receipts.CollectorReceiptError, "native checkpoint"):
            self.load()
        proof.write_bytes(raw)
        (self.snapshot_dir / "unbound.json").write_text("{}")
        with self.assertRaisesRegex(collector_receipts.CollectorReceiptError, "native checkpoint"):
            self.load()

    def test_derivation_rejects_changed_or_extra_native_copy(self):
        self.capture()
        self.seal()
        child = self.derive()
        with patch.object(review_run, "RUNS", self.runs):
            review_run._verified_collector_derivation(child)
            proof = child / PREFIX / "snapshot.json"
            self.assertTrue(proof.exists(), "native proof copy missing")
            raw = proof.read_bytes()
            proof.write_bytes(raw + b" ")
            with self.assertRaisesRegex(review_run.ReviewRunError, "derived artifact"):
                review_run._verified_collector_derivation(child)
            proof.write_bytes(raw)
            (child / PREFIX / "extra.json").write_text("{}")
            with self.assertRaisesRegex(review_run.ReviewRunError, "native checkpoint"):
                review_run._verified_collector_derivation(child)

    def test_derivation_rejects_rehashed_report_with_replaced_native_binding(self):
        """Catches executor report provenance drifting from the sealed collector report."""
        self.capture()
        self.seal()
        child = self.derive()
        report_path = child / "run-report.json"
        report = json.loads(report_path.read_bytes())
        report["clockify_native_checkpoint"]["request"]["user_id"] = "foreign-user"
        raw = (json.dumps(report) + "\n").encode()
        report_path.write_bytes(raw)
        lineage_path = child / "collector-source.json"
        lineage = json.loads(lineage_path.read_bytes())
        lineage["derived_artifact_digests"]["run-report.json"] = "sha256:" + hashlib.sha256(raw).hexdigest()
        lineage.pop("lineage_digest")
        lineage["lineage_digest"] = "sha256:" + hashlib.sha256(
            json.dumps(lineage, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        lineage_path.write_text(json.dumps(lineage) + "\n")
        with patch.object(review_run, "RUNS", self.runs):
            with self.assertRaisesRegex(review_run.ReviewRunError, "native checkpoint"):
                review_run._verified_collector_derivation(child)
