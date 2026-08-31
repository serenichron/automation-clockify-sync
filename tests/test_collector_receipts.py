from __future__ import annotations

import datetime as dt
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
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
        coverage = {"status": "complete", "incomplete_sources": []}
        (run_dir / "evidence" / "evidence-ledger.json").write_text(json.dumps({
            "manifest": {"source_completeness": coverage},
        }) + "\n", encoding="utf-8")
        (run_dir / "run-report.json").write_text(json.dumps({
            "runtime_identity": {"git_sha": "fixture"},
            "date_range": {
                "since": "2026-07-31T21:00:00Z", "until": "2026-08-01T21:00:00Z",
            },
            "evidence_ledger": {"source_completeness": coverage},
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

    def test_bundle_rejects_report_interval_or_ledger_coverage_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            self._write_required_artifacts(run_dir)
            report_path = run_dir / "run-report.json"
            report = json.loads(report_path.read_text())
            report["date_range"]["until"] = "2026-08-02T21:00:00Z"
            report_path.write_text(json.dumps(report) + "\n")
            with self.assertRaisesRegex(collector_receipts.CollectorReceiptError, "interval"):
                collector_receipts.build_completion_bundle(run_dir, slice_=self.slice)

            self._write_required_artifacts(run_dir)
            ledger_path = run_dir / "evidence" / "evidence-ledger.json"
            ledger = json.loads(ledger_path.read_text())
            ledger["manifest"]["source_completeness"] = {
                "status": "incomplete", "incomplete_sources": ["sessions/macbook"],
            }
            ledger_path.write_text(json.dumps(ledger) + "\n")
            with self.assertRaisesRegex(collector_receipts.CollectorReceiptError, "coverage"):
                collector_receipts.build_completion_bundle(run_dir, slice_=self.slice)

    def test_bundle_rejects_symlinked_artifact_and_bundle_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            self._write_required_artifacts(run_dir)
            outside = root / "outside.json"
            outside.write_text("{}\n")
            (run_dir / "quality_report.json").unlink()
            (run_dir / "quality_report.json").symlink_to(outside)
            with self.assertRaisesRegex(collector_receipts.CollectorReceiptError, "symlink"):
                collector_receipts.build_completion_bundle(run_dir, slice_=self.slice)

    def test_bundle_rejects_rehashed_coverage_digest_mismatch_and_symlinked_document(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            self._write_required_artifacts(run_dir)
            bundle = collector_receipts.build_completion_bundle(run_dir, slice_=self.slice)
            unsigned = bundle.document()
            unsigned.pop("bundle_digest")
            unsigned["source_coverage_digest"] = "sha256:" + "0" * 64
            rehashed = "sha256:" + hashlib.sha256(
                json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            tampered = replace(
                bundle, source_coverage_digest=unsigned["source_coverage_digest"], bundle_digest=rehashed,
            )
            with self.assertRaisesRegex(collector_receipts.CollectorReceiptError, "identity digest"):
                collector_receipts.verify_completion_bundle(tampered)

            external = root / "external-bundle.json"
            external.write_text(json.dumps(bundle.document()) + "\n", encoding="utf-8")
            (run_dir / "completion-bundle.json").symlink_to(external)
            with self.assertRaisesRegex(collector_receipts.CollectorReceiptError, "symlink"):
                collector_receipts.load_completion_bundle(
                    run_dir / "completion-bundle.json", run_dir=run_dir
                )

    def test_bundle_write_does_not_follow_precreated_temporary_symlink(self) -> None:
        """A predictable old temporary name cannot overwrite an outside target."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "run"
            self._write_required_artifacts(run_dir)
            bundle = collector_receipts.build_completion_bundle(run_dir, slice_=self.slice)
            outside = root / "outside.json"
            outside.write_text("outside remains intact\n", encoding="utf-8")
            attack_path = run_dir / f".completion-bundle.json.{os.getpid()}.tmp"
            attack_path.symlink_to(outside)

            collector_receipts.write_completion_bundle(run_dir / "completion-bundle.json", bundle)

            self.assertEqual("outside remains intact\n", outside.read_text(encoding="utf-8"))
            loaded = collector_receipts.load_completion_bundle(
                run_dir / "completion-bundle.json", run_dir=run_dir
            )
            self.assertEqual(bundle.bundle_digest, loaded.bundle_digest)

    def test_bucharest_local_report_interval_survives_bundle_write_and_verify(self) -> None:
        """Normal collector local date_range values remain valid after reload."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "run"
            self._write_required_artifacts(run_dir)
            report_path = run_dir / "run-report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["date_range"] = {"since": "2026-08-01 00:00", "until": "2026-08-02 00:00"}
            report_path.write_text(json.dumps(report) + "\n", encoding="utf-8")

            bundle = collector_receipts.build_completion_bundle(run_dir, slice_=self.slice)
            collector_receipts.write_completion_bundle(run_dir / "completion-bundle.json", bundle)

            loaded = collector_receipts.load_completion_bundle(
                run_dir / "completion-bundle.json", run_dir=run_dir
            )
            self.assertEqual(bundle.bundle_digest, loaded.bundle_digest)

    def test_append_rejects_malformed_or_symlinked_journal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "receipts.jsonl"
            store = collector_receipts.FailureReceiptStore(path)
            receipt = collector_receipts.failure_receipt(
                source="fathom", slice_id="slice-safe",
                checkpoint_identity_digest="sha256:" + "a" * 64,
                failure_class="offline", retryable=True,
                resume_state_digest="sha256:" + "b" * 64,
                occurred_at="2026-08-01T00:00:00Z",
            )
            path.write_text("not-json\n", encoding="utf-8")
            with self.assertRaises(collector_receipts.CollectorReceiptError):
                store.append(receipt)
            path.unlink()
            target = Path(tmp) / "target"
            target.mkdir()
            (Path(tmp) / "journal-link").symlink_to(target, target_is_directory=True)
            with self.assertRaises(collector_receipts.CollectorReceiptError):
                collector_receipts.FailureReceiptStore(
                    Path(tmp) / "journal-link" / "receipts.jsonl"
                ).append(receipt)

    def test_append_rejects_valid_unterminated_journal_without_changing_bytes(self) -> None:
        """A crash-truncated JSONL record is never silently concatenated with a receipt."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "receipts.jsonl"
            store = collector_receipts.FailureReceiptStore(path)
            receipt = collector_receipts.failure_receipt(
                source="fathom", slice_id="slice-safe",
                checkpoint_identity_digest="sha256:" + "a" * 64,
                failure_class="offline", retryable=True,
                resume_state_digest="sha256:" + "b" * 64,
                occurred_at="2026-08-01T00:00:00Z",
            )
            path.write_text(
                json.dumps(receipt.document(), sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )
            before = path.read_bytes()

            with self.assertRaisesRegex(collector_receipts.CollectorReceiptError, "newline"):
                store.append(receipt)

            self.assertEqual(before, path.read_bytes())

    def test_append_retries_short_writes_until_receipt_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "receipts.jsonl"
            store = collector_receipts.FailureReceiptStore(path)
            receipt = collector_receipts.failure_receipt(
                source="fathom", slice_id="slice-safe",
                checkpoint_identity_digest="sha256:" + "a" * 64,
                failure_class="offline", retryable=True,
                resume_state_digest="sha256:" + "b" * 64,
                occurred_at="2026-08-01T00:00:00Z",
            )
            original_write = os.write

            def short_write(descriptor: int, data: bytes) -> int:
                original_write(descriptor, data[:2])
                return min(2, len(data))

            with mock.patch.object(collector_receipts.os, "write", side_effect=short_write):
                store.append(receipt)
            self.assertEqual((receipt,), store.load())
            path.unlink()
            with mock.patch.object(collector_receipts.os, "write", return_value=0):
                with self.assertRaisesRegex(collector_receipts.CollectorReceiptError, "no progress"):
                    store.append(receipt)
