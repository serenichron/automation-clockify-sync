"""Fresh overlap collection expands the reader without rewriting legacy proof."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import clockify_sync_collect as collector
from scripts import collector_checkpoints as checkpoints
from scripts import clockify_checkpoint_snapshot as snapshots


SINCE = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
UNTIL = SINCE + dt.timedelta(days=1)
ENV = {"CLOCKIFY_WORKSPACE_ID": "workspace"}
ROUTING = {"clockify_user_id": "user"}


def entry(identifier, start, end):
    return {"id": identifier, "workspaceId": "workspace", "userId": "user",
            "description": "Synthetic work", "projectId": None, "tagIds": [],
            "taskId": None, "billable": False,
            "timeInterval": {"start": start, "end": end, "duration": "PT1H"}}


class ClockifyOverlapCheckpointTests(unittest.TestCase):
    def test_fresh_v2_pending_source_resumes_offline_with_or_without_copied_proof(self):
        from test_pending_collector_derivation import pending_fixture, inventory, SINCE as START, UNTIL as END
        from scripts import collector_receipts
        for retain_native in (False, True):
            with self.subTest(retain_native=retain_native), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                _, source, checkpoint_root, _ = pending_fixture(root, retain_native=retain_native, legacy=False)
                if retain_native:
                    # The sealed v2 identity disambiguates otherwise identical
                    # v1/v2 projections, rather than guessing by cache preference.
                    manifest = next(checkpoint_root.glob("*/source-checkpoints/*/manifest.json"))
                    store = checkpoints.PageCheckpointStore(manifest.parent.parent)
                    legacy = collector._clockify_checkpoint_identity("workspace-one", "user-one", START, END)
                    state = store.open(legacy, initial_metadata={"snapshot_at": "2026-09-03T00:00:00Z"})
                    state = store.append_page(state, payload=[], continuation={"page": 2},
                                             signature=collector._clockify_page_signature([]))
                    store.mark_complete(state)
                before = inventory(root)
                with patch.object(collector, "clockify_get", side_effect=AssertionError("offline resume only")):
                    loaded = collector_receipts.load_pending_collector_source(source, checkpoint_root=checkpoint_root)
                proof = json.loads(loaded.verified_artifact_bytes["evidence/clockify-native-checkpoint/snapshot.json"])
                self.assertEqual("clockify-overlap-pagination/v2", proof["checkpoint_identity"]["compatibility_version"])
                self.assertEqual(before, inventory(root))

    def test_unpinned_pending_source_rejects_two_valid_checkpoint_identities(self):
        from test_pending_collector_derivation import pending_fixture, inventory, SINCE as START, UNTIL as END
        from scripts import collector_receipts
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, source, checkpoint_root, _ = pending_fixture(root, legacy=False)
            manifest = next(checkpoint_root.glob("*/source-checkpoints/*/manifest.json"))
            store = checkpoints.PageCheckpointStore(manifest.parent.parent)
            legacy = collector._clockify_checkpoint_identity("workspace-one", "user-one", START, END)
            state = store.open(legacy, initial_metadata={"snapshot_at": "2026-09-03T00:00:00Z"})
            state = store.append_page(state, payload=[], continuation={"page": 2},
                                     signature=collector._clockify_page_signature([]))
            store.mark_complete(state)
            before = inventory(root)
            with patch.object(collector, "clockify_get", side_effect=AssertionError("offline resume only")):
                with self.assertRaises(collector_receipts.CollectorReceiptError):
                    collector_receipts.load_pending_collector_source(source, checkpoint_root=checkpoint_root)
            self.assertEqual(before, inventory(root))

    def test_fresh_collection_does_not_reuse_completed_start_filtered_v1_cache(self):
        carry = entry("carry-in", "2026-08-31T23:30:00Z", "2026-09-01T00:30:00Z")
        with tempfile.TemporaryDirectory() as temporary:
            store = checkpoints.PageCheckpointStore(Path(temporary))
            legacy = collector._clockify_checkpoint_identity("workspace", "user", SINCE, UNTIL)
            state = store.open(legacy, initial_metadata={"snapshot_at": "2026-09-02T00:00:00Z"})
            state = store.append_page(state, payload=[], continuation={"page": 2},
                                     signature=collector._clockify_page_signature([]))
            state = store.mark_complete(state)
            originals = {path: path.read_bytes() for path in state.directory.rglob("*") if path.is_file()}
            def fresh(path, _env):
                self.assertNotIn("start=", path)
                return [carry]
            with patch.object(collector, "clockify_get", side_effect=fresh) as backend:
                result = collector.fetch_clockify(ENV, ROUTING, SINCE, UNTIL,
                                                 snapshot_at=UNTIL, checkpoint_store=store)
            self.assertEqual(1, backend.call_count)
            self.assertTrue(result["complete"])
            self.assertEqual(["carry-in"], [row["id_suffix"] for row in result["entries"]])
            self.assertEqual(originals, {path: path.read_bytes() for path in state.directory.rglob("*") if path.is_file()})

    def test_distinct_full_filtered_pages_replay_without_false_repeat(self):
        def old_page(prefix):
            return [entry(f"{prefix}-{index}", "2016-01-01T00:00:00Z", "2016-01-01T01:00:00Z")
                    for index in range(collector.CLOCKIFY_PAGE_SIZE)]
        with tempfile.TemporaryDirectory() as temporary:
            store = checkpoints.PageCheckpointStore(Path(temporary))
            with patch.object(collector, "clockify_get", side_effect=[old_page("first"), old_page("second"), []]):
                result = collector.fetch_clockify(ENV, ROUTING, SINCE, UNTIL, checkpoint_store=store)
            self.assertTrue(result["complete"])
            self.assertEqual([], result["entries"])
            self.assertEqual(3, result["pages_fetched"])
            with patch.object(collector, "clockify_get", side_effect=AssertionError("offline replay only")):
                self.assertEqual(result, collector.fetch_clockify(ENV, ROUTING, SINCE, UNTIL, checkpoint_store=store))

    def test_invalid_wire_metadata_fails_closed_before_resume(self):
        native = entry("carry-in", "2026-08-31T23:30:00Z", "2026-09-01T00:30:00Z")
        for count in (True, -1, 0):
            with self.subTest(count=count), tempfile.TemporaryDirectory() as temporary:
                store = checkpoints.PageCheckpointStore(Path(temporary))
                identity = collector._clockify_checkpoint_identity("workspace", "user", SINCE, UNTIL, overlap_history=True)
                state = store.open(identity, initial_metadata={"snapshot_at": "2026-09-02T00:00:00Z"})
                store.append_page(state, payload=[native], continuation={"page": 2},
                    signature=collector._clockify_page_signature([native]),
                    metadata={"wire_row_count": count, "wire_signature": collector._clockify_page_signature([native])})
                with patch.object(collector, "clockify_get", side_effect=AssertionError("must reject before HTTP")):
                    result = collector.fetch_clockify(ENV, ROUTING, SINCE, UNTIL, checkpoint_store=store)
                self.assertEqual("error", result["status"])
                self.assertFalse(result["complete"])

    def test_filtered_full_wire_page_resumes_and_seals_native_snapshot(self):
        unrelated = [entry(f"old-{index}", "2016-01-01T00:00:00Z", "2016-01-01T01:00:00Z")
                     for index in range(collector.CLOCKIFY_PAGE_SIZE)]
        carry = entry("carry-in", "2026-08-31T23:30:00Z", "2026-09-01T00:30:00Z")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = checkpoints.PageCheckpointStore(root / "cache")
            with patch.object(collector, "clockify_get", side_effect=[unrelated, OSError("offline")]):
                result = collector.fetch_clockify(ENV, ROUTING, SINCE, UNTIL,
                    snapshot_at=UNTIL, checkpoint_store=store)
            self.assertFalse(result["complete"])
            manifest = next((root / "cache").glob("*/manifest.json"))
            self.assertEqual("clockify-overlap-pagination/v2",
                             json.loads(manifest.read_bytes())["identity"]["compatibility_version"])
            first_page = manifest.parent / "pages/000001.json"
            original = first_page.read_bytes()
            page = json.loads(original)
            self.assertEqual([], page["payload"])
            self.assertEqual(collector.CLOCKIFY_PAGE_SIZE, page["metadata"]["wire_row_count"])
            def resume(path, _env):
                self.assertNotIn("start=", path)
                self.assertIn("page=2&", path)
                return [carry]
            with patch.object(collector, "clockify_get", side_effect=resume):
                result = collector.fetch_clockify(ENV, ROUTING, SINCE, UNTIL, checkpoint_store=store)
            self.assertTrue(result["complete"])
            self.assertEqual(original, first_page.read_bytes())
            evidence = root / "source-run/evidence/clockify-existing.json"
            evidence.parent.mkdir(parents=True)
            evidence.write_text(json.dumps(result))
            with patch.object(collector, "clockify_get", side_effect=AssertionError("offline replay only")):
                captured = snapshots.capture_checkpoint_snapshot(checkpoint_manifest=manifest,
                    clockify_evidence=evidence, destination=root / "snapshot",
                    workspace_id="workspace", user_id="user", since=SINCE, until=UNTIL)
            self.assertEqual([carry], captured.entries)

    def test_legacy_sealed_checkpoint_replays_without_rewrite_or_network(self):
        native = entry("legacy", "2026-09-01T09:00:00Z", "2026-09-01T10:00:00Z")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = checkpoints.PageCheckpointStore(root / "cache")
            identity = collector._clockify_checkpoint_identity("workspace", "user", SINCE, UNTIL)
            self.assertEqual("clockify-pagination/v1", identity.compatibility_version)
            state = store.open(identity, initial_metadata={"snapshot_at": "2026-09-02T00:00:00Z"})
            state = store.append_page(state, payload=[native], continuation={"page": 2},
                signature=collector._clockify_page_signature([native]))
            state = store.mark_complete(state)
            originals = {path: path.read_bytes() for path in state.directory.rglob("*") if path.is_file()}
            with patch.object(collector, "clockify_get", side_effect=AssertionError("offline replay only")):
                result = collector.fetch_clockify(ENV, ROUTING, SINCE, UNTIL,
                                                 checkpoint_store=store, checkpoint_identity=identity)
                self.assertTrue(result["complete"])
                evidence = root / "source-run/evidence/clockify-existing.json"
                evidence.parent.mkdir(parents=True)
                evidence.write_text(json.dumps(result))
                captured = snapshots.capture_checkpoint_snapshot(checkpoint_manifest=state.directory / "manifest.json",
                    clockify_evidence=evidence, destination=root / "snapshot",
                    workspace_id="workspace", user_id="user", since=SINCE, until=UNTIL)
                loaded = snapshots.load_checkpoint_snapshot(root / "snapshot", workspace_id="workspace",
                    user_id="user", since=SINCE, until=UNTIL, expected_manifest_sha256=captured.manifest_sha256)
            self.assertEqual([native], loaded.entries)
            self.assertEqual(originals, {path: path.read_bytes() for path in state.directory.rglob("*") if path.is_file()})


if __name__ == "__main__":
    unittest.main()
