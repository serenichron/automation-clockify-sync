"""Repair credits must use sealed collection proof, never original locators."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from scripts import clockify_checkpoint_snapshot as snapshot
from scripts import clockify_review_run as run, collector_checkpoints as checkpoints
from scripts import clockify_sync_collect as collector, evidence_ledger, review_corrections
import test_native_checkpoint_run_transport as transport_fixtures
import test_recurring_native_credit as credit_fixtures
from test_clockify_checkpoint_snapshot import SINCE, UNTIL, OBSERVED


class RecurringCreditRepairTransitionTests(unittest.TestCase):
    def fixture(self, *, prior=(30,), current=(30,), kind="equal_accomplishment"):
        transport = transport_fixtures.NativeCheckpointRunTransportTests()
        transport.setUp()
        self.addCleanup(transport.temporary.cleanup)
        transport.downstream_fixture(native=False)
        original = transport.root / "original-proof"
        original.mkdir()
        helper = credit_fixtures.RecurringNativeCreditTests()
        proposals, declaration, synthetic = helper.fixture(
            original, prior_minutes=prior, current_minutes=current, kind=kind,
        )
        credit = helper.seal(declaration)
        store = checkpoints.PageCheckpointStore(transport.root / "cache")
        identity = collector._clockify_checkpoint_identity("workspace-1", "user-1", SINCE, UNTIL)
        state = store.open(identity, initial_metadata={"snapshot_at": OBSERVED})
        state = store.append_page(state, payload=synthetic.entries, continuation={"page": 2},
                                  signature=collector._clockify_page_signature(synthetic.entries))
        state = store.mark_complete(state)
        raw = collector.fetch_clockify(
            {"CLOCKIFY_WORKSPACE_ID": "workspace-1"}, {"clockify_user_id": "user-1"}, SINCE, UNTIL,
            snapshot_at=dt.datetime.fromisoformat(OBSERVED.replace("Z", "+00:00")), checkpoint_store=store,
        )
        evidence = transport.evidence / "clockify-existing.json"
        evidence.write_text(json.dumps(raw) + "\n")
        original_evidence = transport.root / "original-run/evidence/clockify-existing.json"
        original_evidence.parent.mkdir(parents=True)
        original_evidence.write_bytes(evidence.read_bytes())
        captured = snapshot.capture_checkpoint_snapshot(
            checkpoint_manifest=state.directory / "manifest.json", clockify_evidence=original_evidence,
            destination=transport.snapshot_dir, workspace_id="workspace-1", user_id="user-1",
            since=SINCE, until=UNTIL,
        )
        ledger = evidence_ledger.EvidenceLedger(tuple(evidence_ledger.normalize_collector_snapshot({"clockify": raw})))
        transport.report["evidence_ledger"]["source_completeness"] = ledger.manifest.document()["source_completeness"]
        (transport.evidence / "evidence-ledger.json").write_text(json.dumps({
            "schema_version": ledger.manifest.schema_version, "manifest": ledger.manifest.document(),
            "events": [event.document() for event in ledger.events],
        }) + "\n")
        transport.report["clockify_native_checkpoint"] = {
            "manifest_sha256": captured.manifest_sha256,
            "request": {"workspace_id": "workspace-1", "user_id": "user-1",
                        "since_utc": "2026-09-01T00:00:00Z", "until_utc": "2026-10-01T00:00:00Z"},
        }
        parent = transport.source / "review-corrections.jsonl"
        parent.write_bytes(b"")
        (transport.source / "proposals.json").write_text(json.dumps(proposals) + "\n")
        transport.bundle_path.unlink()
        transport.seal()
        child = transport.root / "child-corrections.jsonl"
        review_corrections.append_verified_posted_credit(
            child, credit, runs_root=transport.runs, current_proposals=proposals,
            existing_blocks=[], collection_snapshot=captured,
        )
        return transport, child, original

    def test_repair_accepts_exact_native_group_without_reopening_original_artifacts(self):
        """Rejecting record2 prevents recovery; reopening handles breaks frozen replay."""
        for prior, current, kind in (
            ((30,), (30,), "equal_accomplishment"),
            ((2, 1), (3,), "disjoint_aggregate"),
            ((86,), (15, 6), "whole_recording_aliases"),
        ):
            with self.subTest(kind=kind):
                transport, child, original = self.fixture(prior=prior, current=current, kind=kind)
                for path in original.iterdir():
                    path.unlink()
                expected = ("sha256:" + hashlib.sha256(b"").hexdigest(),
                            "sha256:" + hashlib.sha256(child.read_bytes()).hexdigest())
                self.assertEqual(expected, run._validate_repair_credit_transition(
                    transport.source, child, runs_root=transport.runs,
                ))

    def test_repair_rejects_missing_collection_proof(self):
        transport, child, _original = self.fixture()
        (transport.snapshot_dir / "snapshot.json").unlink()
        with self.assertRaises(run.ReviewRunError):
            run._validate_repair_credit_transition(transport.source, child, runs_root=transport.runs)

    def test_repair_child_keeps_credit_and_native_basis_offline(self):
        """A validator-only fix is insufficient if the actual repair drops proof."""
        transport, corrections, original = self.fixture()
        before = {path.relative_to(transport.source): path.read_bytes()
                  for path in transport.source.rglob("*") if path.is_file()}
        for path in original.iterdir():
            path.unlink()
        with patch.object(run, "RUNS", transport.runs), patch.object(
            collector, "clockify_get", side_effect=AssertionError("network forbidden"),
        ):
            child = run._prepare_repair_run(transport.source, corrections_override=corrections)
            self.assertEqual(corrections.read_bytes(), (child / "review-corrections.jsonl").read_bytes())
            run._repair_analysis_fixture(child)
        self.assertEqual(before, {path.relative_to(transport.source): path.read_bytes()
                                  for path in transport.source.rglob("*") if path.is_file()})

    def test_repair_rejects_partially_matching_declared_targets(self):
        transport, child, _original = self.fixture(prior=(86,), current=(15, 6), kind="whole_recording_aliases")
        proposals = json.loads((transport.source / "proposals.json").read_text())
        proposals[1]["description"] += " changed"
        (transport.source / "proposals.json").write_text(json.dumps(proposals) + "\n")
        with self.assertRaises(run.ReviewRunError):
            run._validate_repair_credit_transition(transport.source, child, runs_root=transport.runs)

    def test_repair_rejects_prior_reuse_across_parent_and_appended_unit(self):
        transport, child, _original = self.fixture()
        parent_bytes = child.read_bytes()
        first = json.loads(child.read_text())
        (transport.source / "review-corrections.jsonl").write_bytes(parent_bytes)
        second = dict(first)
        second["previous_digest"] = first["canonical_digest"]
        second["canonical_digest"] = review_corrections.canonical_digest(review_corrections._without_integrity(second))
        child.write_bytes(parent_bytes + (json.dumps(second) + "\n").encode())
        with self.assertRaises(run.ReviewRunError) as rejected:
            run._validate_repair_credit_transition(transport.source, child, runs_root=transport.runs)
        self.assertIn("reuses a native", str(rejected.exception.__cause__))
