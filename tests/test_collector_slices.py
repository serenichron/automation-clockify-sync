from __future__ import annotations

import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from scripts import collector_slices


BUCHAREST = ZoneInfo("Europe/Bucharest")


class SlicePlanningTests(unittest.TestCase):
    def test_long_interval_slices_oldest_first_without_gaps(self) -> None:
        slices = collector_slices.plan_slices(
            dt.datetime(2026, 8, 1, tzinfo=BUCHAREST),
            dt.datetime(2026, 8, 8, tzinfo=BUCHAREST),
            zone=BUCHAREST,
        )

        self.assertEqual(4, len(slices))
        self.assertEqual(slices[0].until, slices[1].since)
        self.assertLessEqual(
            (slices[0].until.date() - slices[0].since.date()).days, 2
        )
        self.assertEqual(
            [
                dt.datetime(2026, 8, 1, tzinfo=BUCHAREST),
                dt.datetime(2026, 8, 3, tzinfo=BUCHAREST),
                dt.datetime(2026, 8, 5, tzinfo=BUCHAREST),
                dt.datetime(2026, 8, 7, tzinfo=BUCHAREST),
            ],
            [slice_.since for slice_ in slices],
        )

    def test_dst_boundaries_are_local_midnights_not_fixed_48_hours(self) -> None:
        slices = collector_slices.plan_slices(
            dt.datetime(2026, 10, 24, tzinfo=BUCHAREST),
            dt.datetime(2026, 10, 27, tzinfo=BUCHAREST),
            zone=BUCHAREST,
        )

        self.assertEqual(dt.time.min, slices[0].until.astimezone(BUCHAREST).time())
        self.assertEqual(dt.datetime(2026, 10, 26, tzinfo=BUCHAREST), slices[0].until)

    def test_repeated_fall_back_hour_preserves_utc_interval(self) -> None:
        since = dt.datetime(2026, 10, 25, 0, 30, tzinfo=dt.timezone.utc)
        until = dt.datetime(2026, 10, 25, 1, 15, tzinfo=dt.timezone.utc)

        slices = collector_slices.plan_slices(since, until, zone=BUCHAREST)

        self.assertEqual(1, len(slices))
        self.assertLess(
            slices[0].since.astimezone(dt.timezone.utc),
            slices[0].until.astimezone(dt.timezone.utc),
        )
        self.assertEqual(since, slices[0].since.astimezone(dt.timezone.utc))
        self.assertEqual(until, slices[0].until.astimezone(dt.timezone.utc))

    def test_preserves_partial_endpoints(self) -> None:
        since = dt.datetime(2026, 8, 1, 13, 45, tzinfo=BUCHAREST)
        until = dt.datetime(2026, 8, 5, 9, 15, tzinfo=BUCHAREST)

        slices = collector_slices.plan_slices(since, until, zone=BUCHAREST)

        self.assertEqual(since, slices[0].since)
        self.assertEqual(dt.datetime(2026, 8, 3, tzinfo=BUCHAREST), slices[0].until)
        self.assertEqual(until, slices[-1].until)

    def test_max_adjacent_interval_clamps_to_final_partial_slice(self) -> None:
        since = dt.datetime(9999, 12, 30, 12, tzinfo=BUCHAREST)
        until = dt.datetime(9999, 12, 31, 23, 59, tzinfo=BUCHAREST)

        slices = collector_slices.plan_slices(since, until, zone=BUCHAREST)

        self.assertEqual(1, len(slices))
        self.assertEqual(since, slices[0].since)
        self.assertEqual(until, slices[0].until)

    def test_rejects_naive_reversed_or_too_wide_intervals(self) -> None:
        aware = dt.datetime(2026, 8, 1, tzinfo=BUCHAREST)
        with self.assertRaises(collector_slices.BacklogError):
            collector_slices.plan_slices(dt.datetime(2026, 8, 1), aware, zone=BUCHAREST)
        with self.assertRaises(collector_slices.BacklogError):
            collector_slices.plan_slices(aware, aware, zone=BUCHAREST)
        with self.assertRaises(collector_slices.BacklogError):
            collector_slices.plan_slices(
                aware, aware + dt.timedelta(days=1), zone=BUCHAREST, max_days=3
            )


class BacklogReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.slices = collector_slices.plan_slices(
            dt.datetime(2026, 8, 1, tzinfo=BUCHAREST),
            dt.datetime(2026, 8, 5, tzinfo=BUCHAREST),
            zone=BUCHAREST,
        )
        self.identity = collector_slices.BacklogIdentity(
            since_utc="2026-07-31T21:00:00Z",
            until_utc="2026-08-04T21:00:00Z",
            timezone="Europe/Bucharest",
            max_days=2,
            compatibility_version="collector/v1",
        )

    def _digest(self, path: Path) -> str:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()

    def test_verified_first_receipt_leaves_next_slice_selected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = collector_slices.BacklogStore(Path(directory))
            artifact = Path(directory) / "first-result.json"
            artifact.write_bytes(b'{"slice":"first"}\n')

            state = store.record_complete(
                store.open(self.identity, self.slices),
                self.slices[0].slice_id,
                artifact,
                self._digest(artifact),
            )

            self.assertEqual(self.slices[0].slice_id, state.completed[0].slice_id)
            self.assertEqual(self.slices[1], store.next_incomplete(state))
            self.assertFalse(state.complete)

    def test_missing_completed_artifact_fails_closed_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = collector_slices.BacklogStore(Path(directory))
            artifact = Path(directory) / "first-result.json"
            artifact.write_bytes(b'{"slice":"first"}\n')
            state = store.record_complete(
                store.open(self.identity, self.slices),
                self.slices[0].slice_id,
                artifact,
                self._digest(artifact),
            )
            artifact.unlink()

            with self.assertRaises(collector_slices.BacklogError):
                store.next_incomplete(state)

    def test_digest_mismatched_completed_artifact_fails_closed_on_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = collector_slices.BacklogStore(Path(directory))
            artifact = Path(directory) / "first-result.json"
            artifact.write_bytes(b'{"slice":"first"}\n')
            state = store.record_complete(
                store.open(self.identity, self.slices),
                self.slices[0].slice_id,
                artifact,
                self._digest(artifact),
            )
            artifact.write_bytes(b'{"slice":"tampered"}\n')

            with self.assertRaises(collector_slices.BacklogError):
                store.next_incomplete(state)

    def test_parent_is_complete_only_after_every_receipt_verifies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = collector_slices.BacklogStore(Path(directory))
            state = store.open(self.identity, self.slices)
            for index, slice_ in enumerate(self.slices):
                artifact = Path(directory) / f"result-{index}.json"
                artifact.write_bytes(f'{{"slice":{index}}}\n'.encode())
                state = store.record_complete(
                    state, slice_.slice_id, artifact, self._digest(artifact)
                )
                if index < len(self.slices) - 1:
                    self.assertFalse(state.complete)

            self.assertTrue(state.complete)
            self.assertIsNone(store.next_incomplete(state))

    def test_missing_existing_manifest_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = collector_slices.BacklogStore(Path(directory))
            state = store.open(self.identity, self.slices)
            (state.directory / "backlog-manifest.json").unlink()

            with self.assertRaises(collector_slices.BacklogError):
                store.open(self.identity, self.slices)

    def test_record_complete_rejects_a_non_prefix_slice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = collector_slices.BacklogStore(Path(directory))
            artifact = Path(directory) / "later-result.json"
            artifact.write_bytes(b'{"slice":"later"}\n')

            with self.assertRaises(collector_slices.BacklogError):
                store.record_complete(
                    store.open(self.identity, self.slices),
                    self.slices[1].slice_id,
                    artifact,
                    self._digest(artifact),
                )

    def test_manifest_rejects_receipts_that_are_not_a_chronological_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = collector_slices.BacklogStore(Path(directory))
            state = store.open(self.identity, self.slices)
            second = Path(directory) / "second-result.json"
            second.write_bytes(b'{"slice":"second"}\n')
            manifest_path = state.directory / "backlog-manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["completed"] = [
                {
                    "slice_id": self.slices[1].slice_id,
                    "result_path": str(second),
                    "result_digest": self._digest(second),
                }
            ]
            manifest_path.write_text(json.dumps(manifest) + "\n")

            with self.assertRaises(collector_slices.BacklogError):
                store.open(self.identity, self.slices)
