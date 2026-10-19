"""Frozen hook evidence keeps its original semantic/cache recipe on replay."""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from scripts import evidence_ledger, work_accounting_pipeline
import test_review_run as replay_helpers


class StopHookReplayTests(unittest.TestCase):
    def test_frozen_unmarked_and_fresh_marked_hooks_replay_their_own_recipe(self):
        # Reclassifying historical text, or ignoring a stored fresh marker,
        # changes semantic selection and breaks the sealed replay recipe.
        for kind, expected_sources in (
            ("message", {"meeting-1", "noise-1"}),
            ("injected_session_message", {"meeting-1"}),
        ):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                runs = root / "runs"
                original_event = evidence_ledger.evidence_event

                def hook_event(source_type, source_ref, **kwargs):
                    if source_ref.get("source_id") == "noise-1":
                        kwargs["attributes"] = {
                            **kwargs["attributes"],
                            "role": "user",
                            "kind": kind,
                            "content": "Stop hook feedback: automated delivery status",
                        }
                    return original_event(source_type, source_ref, **kwargs)

                # Change only the fixture's system heartbeat. All ledger IDs,
                # analyzer decisions, cache seals and source artifacts are real.
                with mock.patch.object(evidence_ledger, "evidence_event", hook_event):
                    source = replay_helpers.ReviewRunResultTests._write_real_offline_replay_source(
                        runs, root, mixed_evidence=True,
                    )
                self.assertIs(original_event, evidence_ledger.evidence_event)

                ledger, events = work_accounting_pipeline.load_ledger(
                    source / "evidence" / "evidence-ledger.json"
                )
                identities = work_accounting_pipeline.meeting_reconciliation.manifest_member_identities(
                    ledger.manifest.document()
                )
                semantic_events, noise = work_accounting_pipeline._analysis_events(
                    events, identities,
                )
                self.assertEqual(expected_sources, {
                    event["source_ref"]["source_id"] for event in semantic_events
                })
                hook = next(event for event in events
                            if event["source_ref"]["source_id"] == "noise-1")
                self.assertEqual(
                    [] if kind == "message" else [{
                        "evidence_id": hook["evidence_id"], "reason": "injected_wrapper",
                    }],
                    noise,
                )
                analysis = json.loads((source / "semantic-analysis.json").read_text())
                self.assertEqual(len(expected_sources), analysis["ledger_event_count"])
                self.assertEqual(
                    kind == "message",
                    any(hook["evidence_id"] in activity["evidence_ids"]
                        for activity in analysis["activities"]),
                )
                cache_bytes = (source / "analyzer-cache-used.jsonl").read_bytes()
                self.assertTrue(cache_bytes)
                self.assertTrue(analysis["analyzer_cache"]["records"])
                source_before = replay_helpers.run_tree_snapshot(source)

                output = io.StringIO()
                with mock.patch.dict(os.environ, {
                    "CLOCKIFY_ANALYZER_PRIMARY_URL": "",
                    "CLOCKIFY_ANALYZER_PRIMARY_MODEL": "",
                    "CLOCKIFY_ANALYZER_PRIMARY_REVISION": "",
                    "CLOCKIFY_ANALYZER_FALLBACK_URL": "",
                    "CLOCKIFY_ANALYZER_FALLBACK_MODEL": "",
                    "CLOCKIFY_ANALYZER_FALLBACK_REVISION": "",
                    "CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED": "approved",
                }, clear=False), redirect_stdout(output), redirect_stderr(output):
                    code = replay_helpers.review_run.main([
                        "--replay-from", str(source),
                        "--runs-root", str(runs.resolve()),
                        "--state", str(root / "replay-items.json"),
                    ])

                self.assertEqual(0, code, output.getvalue())
                replay, = runs.glob("*-replay-source-run")
                integrity = json.loads((replay / "replay-integrity.json").read_text())
                self.assertEqual("pass", integrity["status"], integrity)
                for artifact in ("proposals.json", "work-accounting-result.json"):
                    self.assertEqual(
                        (source / artifact).read_bytes(),
                        (replay / artifact).read_bytes(),
                        artifact,
                    )
                self.assertEqual(source_before, replay_helpers.run_tree_snapshot(source))
                provenance = json.loads((replay / "replay-source.json").read_text())
                self.assertEqual(
                    analysis["analyzer_cache"]["records"],
                    provenance["analyzer_cache_reused_records"],
                )
                self.assertEqual(
                    cache_bytes,
                    (replay / provenance["analyzer_cache_fixture"]).read_bytes(),
                )
                self.assertEqual(
                    hashlib.sha256(cache_bytes).hexdigest(),
                    provenance["analyzer_cache_sha256"],
                )
                result = json.loads((replay / "work-accounting-result.json").read_text())
                self.assertFalse(result["external_writes"])


if __name__ == "__main__":
    unittest.main()
