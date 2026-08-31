from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from scripts import collector_receipts
from scripts.collector_slices import CollectionSlice


BUCHAREST = ZoneInfo("Europe/Bucharest")
REQUIRED_KINDS = {
    "run_report", "evidence_ledger", "semantic_analysis", "accounting_result",
    "quality_report", "review_snapshot",
}


class FailureReceiptTests(unittest.TestCase):
    def test_failure_receipt_contains_only_safe_identity_digests(self) -> None:
        receipt = collector_receipts.failure_receipt(
            source="fathom", slice_id="slice-safe", checkpoint_identity_digest="sha256:" + "a" * 64,
            failure_class="incomplete", retryable=True,
            resume_state_digest="sha256:" + "b" * 64,
            occurred_at="2026-08-01T00:00:00Z", cursor="private", credential="secret",
        )
        document = receipt.document()
        self.assertEqual({
            "source", "slice_id", "checkpoint_identity_digest", "failure_class",
            "retryable", "resume_state_digest", "occurred_at", "receipt_digest",
        }, set(document))
        self.assertNotIn("private", json.dumps(document))
        self.assertNotIn("secret", json.dumps(document))

    def test_failure_receipt_store_appends_verified_safe_receipts(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = collector_receipts.FailureReceiptStore(Path(tmp) / "receipts.jsonl")
            receipt = collector_receipts.failure_receipt(
                source="sessions/macbook", slice_id="slice-safe",
                checkpoint_identity_digest="sha256:" + "a" * 64,
                failure_class="offline", retryable=True,
                resume_state_digest="sha256:" + "b" * 64,
                occurred_at="2026-08-01T00:00:00Z",
            )
            store.append(receipt)

            self.assertEqual((receipt,), store.load())


class CompletionBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.slice = CollectionSlice(
            dt.datetime(2026, 8, 1, tzinfo=BUCHAREST),
            dt.datetime(2026, 8, 2, tzinfo=BUCHAREST), "slice-safe",
        )

    def _write_required_artifacts(self, run_dir: Path) -> None:
        for relative in (
            "run-report.json", "evidence/evidence-ledger.json", "semantic-analysis.json",
            "work-accounting-result.json", "quality_report.json", "review-snapshot.json",
        ):
            path = run_dir / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"artifact": relative}) + "\n", encoding="utf-8")
        (run_dir / "run-report.json").write_text(json.dumps({
            "runtime_identity": {"git_sha": "fixture"},
            "evidence_ledger": {"source_completeness": {"status": "complete", "incomplete_sources": []}},
        }) + "\n", encoding="utf-8")

    def test_bundle_binds_every_downstream_artifact_and_rejects_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            self._write_required_artifacts(run_dir)
            bundle = collector_receipts.build_completion_bundle(run_dir, slice_=self.slice)

            self.assertEqual(REQUIRED_KINDS, {item.kind for item in bundle.artifacts})
            (run_dir / "quality_report.json").write_text("{}\n", encoding="utf-8")
            with self.assertRaises(collector_receipts.CollectorReceiptError):
                collector_receipts.verify_completion_bundle(bundle)

    def test_replay_bundle_requires_replay_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            self._write_required_artifacts(run_dir)

            with self.assertRaisesRegex(collector_receipts.CollectorReceiptError, "replay-integrity"):
                collector_receipts.build_completion_bundle(run_dir, slice_=self.slice, replay=True)
            (run_dir / "replay-integrity.json").write_text("{}\n", encoding="utf-8")
            bundle = collector_receipts.build_completion_bundle(run_dir, slice_=self.slice, replay=True)
            self.assertIn("replay_integrity", {item.kind for item in bundle.artifacts})
