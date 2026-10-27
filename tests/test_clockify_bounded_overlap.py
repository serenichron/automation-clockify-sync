"""Fresh Clockify overlap reads are bounded by a lookback, not the full history."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from scripts import clockify_sync_collect as collector
from scripts import collector_checkpoints as checkpoints
from scripts import clockify_checkpoint_snapshot as snapshots


SINCE = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
UNTIL = SINCE + dt.timedelta(days=2)
ENV = {"CLOCKIFY_WORKSPACE_ID": "workspace"}
ROUTING = {"clockify_user_id": "user"}
BOUNDED = {**ROUTING, "clockify_bounded_overlap": True}


def entry(identifier, start, end):
    return {"id": identifier, "workspaceId": "workspace", "userId": "user",
            "description": "Synthetic work", "projectId": None, "tagIds": [],
            "taskId": None, "billable": False,
            "timeInterval": {"start": start, "end": end, "duration": "PT1H"}}


def stamp(value):
    return value.isoformat().replace("+00:00", "Z")


class FakeClockify:
    """Newest-first paging with Clockify's start-of-entry `start`/`end` filters."""

    def __init__(self, rows):
        self.rows = sorted(rows, key=lambda row: row["timeInterval"]["start"], reverse=True)
        self.paths = []

    def __call__(self, path, _env):
        self.paths.append(path)
        query = {key: values[0] for key, values in parse_qs(urlsplit(path).query).items()}
        selected = [
            row for row in self.rows
            if ("start" not in query or row["timeInterval"]["start"] >= query["start"])
            and row["timeInterval"]["start"] <= query["end"]
        ]
        size = int(query["page-size"])
        offset = (int(query["page"]) - 1) * size
        return selected[offset:offset + size]


def history():
    old = [entry(f"old-{index}", stamp(SINCE - dt.timedelta(days=30, minutes=index)),
                 stamp(SINCE - dt.timedelta(days=30, minutes=index) + dt.timedelta(minutes=30)))
           for index in range(collector.CLOCKIFY_PAGE_SIZE * 3)]
    carry = entry("carry-in", stamp(SINCE - dt.timedelta(hours=2)), stamp(SINCE + dt.timedelta(hours=1)))
    inside = entry("inside", stamp(SINCE + dt.timedelta(hours=5)), stamp(SINCE + dt.timedelta(hours=6)))
    before = entry("ended-before", stamp(SINCE - dt.timedelta(hours=5)), stamp(SINCE - dt.timedelta(hours=4)))
    return [*old, carry, inside, before]


class BoundedOverlapTests(unittest.TestCase):
    # Catches each slice paging the full Clockify history (64 pages) for a
    # two-day overlap window.

    def test_bounded_identity_is_a_distinct_v3_contract(self):
        identity = collector._clockify_checkpoint_identity(
            "workspace", "user", SINCE, UNTIL, overlap_history=True, bounded_lookback=True)
        historical = collector._clockify_checkpoint_identity("workspace", "user", SINCE, UNTIL, overlap_history=True)
        self.assertEqual("clockify-overlap-pagination/v3", identity.compatibility_version)
        self.assertEqual("clockify-overlap-pagination/v2", historical.compatibility_version)
        self.assertNotEqual(identity.request_fingerprint, historical.request_fingerprint)

    def test_bounded_read_retains_the_identical_overlap_set_with_fewer_pages(self):
        results = {}
        for name, bounded in (("v2", False), ("v3", True)):
            with tempfile.TemporaryDirectory() as temporary:
                fake = FakeClockify(history())
                identity = collector._clockify_checkpoint_identity(
                    "workspace", "user", SINCE, UNTIL, overlap_history=True, bounded_lookback=bounded)
                with patch.object(collector, "clockify_get", side_effect=fake):
                    result = collector.fetch_clockify(
                        ENV, ROUTING, SINCE, UNTIL, snapshot_at=UNTIL,
                        checkpoint_store=checkpoints.PageCheckpointStore(Path(temporary)),
                        checkpoint_identity=identity)
                self.assertTrue(result["complete"])
                results[name] = (sorted(row["id_suffix"] for row in result["entries"]), len(fake.paths), fake.paths)
        self.assertEqual(results["v2"][0], results["v3"][0])
        self.assertEqual(2, len(results["v3"][0]))
        self.assertEqual(4, results["v2"][1])
        self.assertEqual(1, results["v3"][1])
        self.assertIn(f"start={collector.iso_utc(SINCE - collector.CLOCKIFY_OVERLAP_LOOKBACK)}&", results["v3"][2][0])

    def test_unflagged_collection_keeps_the_complete_v2_scan(self):
        fake = FakeClockify(history())
        with tempfile.TemporaryDirectory() as temporary, \
             patch.object(collector, "clockify_get", side_effect=fake):
            collector.fetch_clockify(ENV, ROUTING, SINCE, UNTIL, snapshot_at=UNTIL,
                                     checkpoint_store=checkpoints.PageCheckpointStore(Path(temporary)))
        self.assertEqual(4, len(fake.paths))
        self.assertTrue(all("start=" not in path for path in fake.paths))

    def test_non_boolean_flag_fails_closed_before_http(self):
        with patch.object(collector, "clockify_get", side_effect=AssertionError("must reject before HTTP")) as backend:
            result = collector.fetch_clockify(ENV, {**ROUTING, "clockify_bounded_overlap": "yes"}, SINCE, UNTIL)
        self.assertEqual("error", result["status"])
        self.assertEqual(0, backend.call_count)

    def test_flagged_collection_uses_v3_and_seals_a_loadable_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake = FakeClockify(history())
            with patch.object(collector, "clockify_get", side_effect=fake):
                result = collector.fetch_clockify(ENV, BOUNDED, SINCE, UNTIL, snapshot_at=UNTIL,
                                                 checkpoint_store=checkpoints.PageCheckpointStore(root / "cache"))
            self.assertEqual(1, len(fake.paths))
            manifest = next((root / "cache").glob("*/manifest.json"))
            self.assertEqual("clockify-overlap-pagination/v3",
                             json.loads(manifest.read_bytes())["identity"]["compatibility_version"])
            evidence = root / "source-run/evidence/clockify-existing.json"
            evidence.parent.mkdir(parents=True)
            evidence.write_text(json.dumps(result))
            with patch.object(collector, "clockify_get", side_effect=AssertionError("offline replay only")):
                captured = snapshots.capture_checkpoint_snapshot(
                    checkpoint_manifest=manifest, clockify_evidence=evidence, destination=root / "snapshot",
                    workspace_id="workspace", user_id="user", since=SINCE, until=UNTIL)
                loaded = snapshots.load_checkpoint_snapshot(
                    root / "snapshot", workspace_id="workspace", user_id="user", since=SINCE, until=UNTIL,
                    expected_manifest_sha256=captured.manifest_sha256)
            self.assertEqual(["carry-in", "inside"], sorted(row["id"] for row in loaded.entries))

    def test_sealed_v2_checkpoint_still_replays_offline(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = checkpoints.PageCheckpointStore(Path(temporary))
            identity = collector._clockify_checkpoint_identity(
                "workspace", "user", SINCE, UNTIL, overlap_history=True)
            with patch.object(collector, "clockify_get", side_effect=FakeClockify(history())):
                first = collector.fetch_clockify(ENV, BOUNDED, SINCE, UNTIL, snapshot_at=UNTIL,
                                                checkpoint_store=store, checkpoint_identity=identity)
            with patch.object(collector, "clockify_get", side_effect=AssertionError("offline replay only")):
                replay = collector.fetch_clockify(ENV, BOUNDED, SINCE, UNTIL,
                                                 checkpoint_store=store, checkpoint_identity=identity)
            self.assertEqual(first, replay)


if __name__ == "__main__":
    unittest.main()
