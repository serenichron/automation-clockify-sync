"""Offline replay must preserve the semantic partition used by a sealed source."""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import semantic_analyzer
import test_review_run as replay_fixtures


class TunedReplayTests(unittest.TestCase):
    def test_max_events_one_source_replays_from_cache_without_transport(self):
        """Default repartitioning would miss the sealed per-event cache decisions."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            original_analyze = semantic_analyzer.analyze_tiered

            def tuned_analyze(*args, **kwargs):
                return original_analyze(*args, **{**kwargs, "max_events_per_chunk": 1})

            with mock.patch.object(semantic_analyzer, "analyze_tiered", side_effect=tuned_analyze):
                source = replay_fixtures.ReviewRunResultTests._write_real_offline_replay_source(
                    runs, root, failed_review=True,
                )

            source_analysis = json.loads((source / "semantic-analysis.json").read_text())
            self.assertEqual(2, len(source_analysis["analysis_chunks"]))
            self.assertEqual(
                [{"target_body_bytes": 250_000, "max_events_per_chunk": 1}] * 2,
                [chunk["chunking"] for chunk in source_analysis["analysis_chunks"]],
            )

            offline_environment = {
                "CLOCKIFY_ANALYZER_PRIMARY_URL": "",
                "CLOCKIFY_ANALYZER_FALLBACK_URL": "",
                "CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED": "approved",
            }
            with mock.patch.dict(os.environ, offline_environment, clear=False), mock.patch.object(
                replay_fixtures.review_run, "_sealed_replay_transport",
                side_effect=AssertionError("replay must not call network transport"),
            ):
                exit_code = replay_fixtures.review_run.main([
                    "--replay-from", str(source),
                    "--runs-root", str(runs.resolve()),
                    "--state", str(root / "replay-items.json"),
                ])

            replay = next(runs.glob("*-replay-source-run"))
            self.assertEqual(0, exit_code, (replay / "autopilot-result.json").read_text())
            self.assertEqual(
                (source / "work-accounting-result.json").read_bytes(),
                (replay / "work-accounting-result.json").read_bytes(),
            )
            self.assertEqual(
                "pass", json.loads((replay / "replay-integrity.json").read_text())["status"],
            )


if __name__ == "__main__":
    unittest.main()
