"""Offline regression for a retry whose sealed source predates its repair parent."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from scripts import semantic_analyzer, work_accounting_pipeline
import test_review_run as fixtures


review_run = fixtures.review_run


def validate_repair(repair: Path, runs: Path, state: Path) -> None:
    quality = subprocess.run([
        sys.executable, str(fixtures.ROOT / "scripts" / "clockify_sync_quality.py"),
        repair.name, "--runs-root", str(runs), "--root", str(fixtures.ROOT),
        "--routing", str(repair / "routing.json"), "--strict",
    ], cwd=fixtures.ROOT, text=True, capture_output=True, check=False)
    if quality.returncode:
        raise AssertionError(quality.stderr or quality.stdout)
    review = subprocess.run([
        sys.executable, str(fixtures.ROOT / "scripts" / "clockify_review_state.py"),
        str(repair), "--state", str(state),
    ], cwd=fixtures.ROOT, text=True, capture_output=True, check=False)
    if review.returncode:
        raise AssertionError(review.stderr or review.stdout)


class ChainedRepairReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.runs = cls.root / "runs"
        cls.original = fixtures.ReviewRunResultTests._write_real_offline_replay_source(
            cls.runs, cls.root, failed_review=True,
        )
        analysis = json.loads((cls.original / "semantic-analysis.json").read_text())
        failure = next(
            row for row in analysis["exceptions"]
            if row["kind"] == "analyzer_review_failure"
        )
        digest = semantic_analyzer.stable_digest(
            "frt-", failure["evidence_ids"], length=64,
        )
        endpoint = semantic_analyzer.AnalyzerEndpoint(
            "clockify_analyzer_primary",
            "https://offline.invalid/v1/chat/completions",
            semantic_analyzer.DEFAULT_PRIMARY_MODEL,
            revision=semantic_analyzer.DEFAULT_PRIMARY_REVISION,
        )
        original_scoped = work_accounting_pipeline.run_scoped_failed_review_retry

        def retry_transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            return fixtures.analyzer_provider_response(payload)

        def forbidden_transport(*_args):
            raise AssertionError("second repair must use the sealed cache")

        with mock.patch.object(review_run, "RUNS", cls.runs):
            cls.parent = review_run._prepare_repair_run(cls.original)
            original_fixture = review_run._repair_analysis_fixture(cls.parent)
            parent_cache = cls.parent / "analyzer-cache-retry.jsonl"
            parent_cache.write_bytes((cls.parent / "analyzer-cache-used.jsonl").read_bytes())
            with (
                mock.patch.object(
                    semantic_analyzer.AnalyzerEndpoint, "from_env",
                    side_effect=lambda name, **_kw: endpoint
                    if name == "CLOCKIFY_ANALYZER_PRIMARY" else None,
                ),
                mock.patch.object(
                    work_accounting_pipeline, "run_scoped_failed_review_retry",
                    side_effect=lambda *args, **kw: original_scoped(
                        *args, transport=retry_transport,
                        private_text_approved=True, **kw,
                    ),
                ),
            ):
                work_accounting_pipeline.run_accounting(
                    cls.parent, root=fixtures.ROOT,
                    routing_path=cls.parent / "routing.json",
                    corrections_path=cls.parent / "review-corrections.jsonl",
                    analyzer_cache_path=parent_cache,
                    failed_review_retry_source=original_fixture,
                    failed_review_retry_digest=digest,
                    analyzer_workers=1,
                )
            validate_repair(cls.parent, cls.runs, cls.root / "parent-items.json")
            review_run._finalize_repair_completion(cls.parent)

            cls.child = review_run._prepare_repair_run(cls.parent)
            child_cache = cls.child / "analyzer-cache-retry.jsonl"
            child_cache.write_bytes(parent_cache.read_bytes())
            with (
                mock.patch.object(
                    semantic_analyzer.AnalyzerEndpoint, "from_env",
                    side_effect=lambda name, **_kw: endpoint
                    if name == "CLOCKIFY_ANALYZER_PRIMARY" else None,
                ),
                mock.patch.object(
                    work_accounting_pipeline, "run_scoped_failed_review_retry",
                    side_effect=lambda *args, **kw: original_scoped(
                        *args, transport=forbidden_transport,
                        private_text_approved=True, **kw,
                    ),
                ),
            ):
                work_accounting_pipeline.run_accounting(
                    cls.child, root=fixtures.ROOT,
                    routing_path=cls.child / "routing.json",
                    corrections_path=cls.child / "review-corrections.jsonl",
                    analyzer_cache_path=child_cache,
                    failed_review_retry_source=original_fixture,
                    failed_review_retry_digest=digest,
                    analyzer_workers=1,
                )
            validate_repair(cls.child, cls.runs, cls.root / "child-items.json")
            review_run._finalize_repair_completion(cls.child)

    def test_replay_uses_bound_original_retry_source_without_transport(self):
        root, runs, original, parent, child = (
            self.root, self.runs, self.original, self.parent, self.child,
        )
        with mock.patch.object(review_run, "RUNS", runs):
            provenance = json.loads((child / "semantic-analysis.json").read_text())[
                "failed_review_retry"
            ]
            self.assertEqual(
                hashlib.sha256((original / "semantic-analysis.json").read_bytes()).hexdigest(),
                provenance["source_semantic_sha256"],
            )
            self.assertNotEqual(
                hashlib.sha256((parent / "semantic-analysis.json").read_bytes()).hexdigest(),
                provenance["source_semantic_sha256"],
            )
            with (
                mock.patch.dict(os.environ, {
                    "CLOCKIFY_ANALYZER_PRIMARY_URL": "",
                    "CLOCKIFY_ANALYZER_FALLBACK_URL": "",
                }),
                mock.patch.object(
                    review_run, "_sealed_replay_transport",
                    side_effect=AssertionError("replay must not use transport"),
                ),
            ):
                replay_code = review_run.main([
                    "--replay-from", str(child),
                    "--runs-root", str(runs),
                    "--state", str(root / "replay-items.json"),
                ])
            replays = [
                path for path in runs.glob(f"*-replay-{child.name}*")
                if (path / "replay-integrity.json").is_file()
            ]
            self.assertEqual(0, replay_code)
            self.assertEqual(1, len(replays))
            self.assertEqual(
                "pass", json.loads((replays[0] / "replay-integrity.json").read_text())["status"],
            )
            self.assertEqual(
                (child / "work-accounting-result.json").read_bytes(),
                (replays[0] / "work-accounting-result.json").read_bytes(),
            )

    def test_replay_rejects_tampered_repair_hop_hashes(self):
        lineage_path = self.parent / "repair-source.json"
        original_bytes = lineage_path.read_bytes()
        original_lineage = json.loads(original_bytes)
        try:
            for field in (
                "source_completion_sha256", "semantic_analysis_sha256",
                "analyzer_cache_sha256",
            ):
                with self.subTest(field=field):
                    tampered = dict(original_lineage)
                    tampered[field] = "sha256:" + "0" * 64 if field == "source_completion_sha256" else "0" * 64
                    lineage_path.write_text(json.dumps(tampered) + "\n")
                    with mock.patch.object(review_run, "RUNS", self.runs):
                        with self.assertRaisesRegex(ValueError, "source lineage differs"):
                            review_run._prepare_replay_run(self.child)
        finally:
            lineage_path.write_bytes(original_bytes)

    def test_replay_rejects_repair_lineage_loop(self):
        lineage_path = self.parent / "repair-source.json"
        original_bytes = lineage_path.read_bytes()
        lineage = json.loads(original_bytes)
        lineage.update({
            "source_run_id": self.child.name,
            "source_completion_sha256": review_run._file_sha256(
                self.child / "completion-bundle.json", label="child completion",
            ),
            "semantic_analysis_sha256": hashlib.sha256(
                (self.child / "semantic-analysis.json").read_bytes()
            ).hexdigest(),
            "analyzer_cache_sha256": hashlib.sha256(
                (self.child / "analyzer-cache-used.jsonl").read_bytes()
            ).hexdigest(),
        })
        try:
            lineage_path.write_text(json.dumps(lineage) + "\n")
            with mock.patch.object(review_run, "RUNS", self.runs):
                with self.assertRaisesRegex(ValueError, "source lineage loops"):
                    review_run._prepare_replay_run(self.child)
        finally:
            lineage_path.write_bytes(original_bytes)

    def test_replay_rejects_symlinked_ancestor_lineage(self):
        lineage_path = self.parent / "repair-source.json"
        backup = self.parent / "repair-source.backup.json"
        lineage_path.rename(backup)
        lineage_path.symlink_to(backup.name)
        try:
            with mock.patch.object(review_run, "RUNS", self.runs):
                with self.assertRaisesRegex(ValueError, "source lineage is missing or unsafe"):
                    review_run._prepare_replay_run(self.child)
        finally:
            lineage_path.unlink()
            backup.rename(lineage_path)


if __name__ == "__main__":
    unittest.main()
