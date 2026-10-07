"""Saved collector evidence must retain structured frozen checkpoint values."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

from scripts import collector_checkpoints as checkpoints
from scripts import clockify_sync_collect as collector, evidence_ledger


class CollectorCheckpointJsonRoundtripTests(unittest.TestCase):
    def test_saved_frozen_checkpoint_objects_reconstruct_the_same_ledger(self):
        # default=str loses nested checkpoint mappings while the in-memory
        # ledger still treats them as objects. Exercise the actual store boundary.
        payload = {
            "meeting": {
                "recording_id": "meeting-one",
                "start": "2026-10-06T09:00:00Z",
                "end": "2026-10-06T10:00:00Z",
                "summary": {"text": "Reviewed café changes", "details": {"approved": True}},
                "action_items": [{"text": "Check the result", "assignee": {"name": "Reviewer"}}],
                "transcript": [{"text": "Looks correct", "speaker": {"name": "Reviewer"}}],
            },
            "issue": {
                "id": "issue-one",
                "title": "Review result",
                "updated_at": "2026-10-06T11:00:00Z",
                "labels": [{"name": "review", "style": {"color": "blue"}}],
            },
        }
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = checkpoints.PageCheckpointStore(root / "checkpoints")
            identity = checkpoints.CheckpointIdentity(
                source="fathom", since_utc="2026-10-05T21:00:00Z",
                until_utc="2026-10-06T21:00:00Z", request_fingerprint="sha256:fixture",
                compatibility_version="fixture/v1",
            )
            state = store.append_page(store.open(identity), payload=payload,
                                      continuation={}, signature="fixture-page")
            state = store.mark_complete(state)
            frozen = next(store.iter_pages(state))["payload"]
            evidence = {
                "fathom": {"status": "ok", "complete": True,
                           "meetings": [dict(frozen["meeting"])]},
                "multica_issues": {"status": "ok", "complete": True,
                                   "issues": [dict(frozen["issue"])]},
            }
            original = evidence_ledger.EvidenceLedger(
                tuple(evidence_ledger.normalize_collector_snapshot(evidence)),
                evidence_ledger.source_inventory_from_collector(evidence),
            )
            destination = root / "raw/evidence.json"
            collector.write_json(destination, evidence)
            saved = json.loads(destination.read_bytes())
            reconstructed = evidence_ledger.EvidenceLedger(
                tuple(evidence_ledger.normalize_collector_snapshot(saved)),
                evidence_ledger.source_inventory_from_collector(saved),
            )

            self.assertEqual(original.manifest.document(), reconstructed.manifest.document())
            self.assertEqual([event.document() for event in original.events],
                             [event.document() for event in reconstructed.events])
            self.assertEqual(payload["meeting"], saved["fathom"]["meetings"][0])
            self.assertEqual(payload["issue"], saved["multica_issues"]["issues"][0])

    def test_write_json_keeps_datetime_and_path_string_fallbacks(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "fallback.json"
            collector.write_json(destination, {
                "at": dt.datetime(2026, 10, 6, 9, 0, tzinfo=dt.timezone.utc),
                "path": Path("reports/review.json"),
            })

            self.assertEqual({"at": "2026-10-06 09:00:00+00:00", "path": "reports/review.json"},
                             json.loads(destination.read_bytes()))


if __name__ == "__main__":
    unittest.main()
