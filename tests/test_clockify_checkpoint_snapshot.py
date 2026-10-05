from __future__ import annotations

import copy
import datetime as dt
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import collector_checkpoints as checkpoints
from scripts import clockify_sync_collect as collector

try:
    snapshot = importlib.import_module("scripts.clockify_checkpoint_snapshot")
except ModuleNotFoundError:
    snapshot = None


SINCE = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
UNTIL = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
OBSERVED = "2026-10-02T12:00:00Z"
ENTRY = {
    "id": "0123456789abcdef01234567",
    "workspaceId": "workspace-one",
    "userId": "user-one",
    "description": "Existing work",
    "projectId": "project-123456",
    "tagIds": ["tag-01234567"],
    "taskId": None,
    "billable": False,
    "timeInterval": {
        "start": "2026-09-10T09:00:00Z",
        "end": "2026-09-10T10:00:00Z",
        "duration": "PT1H",
    },
}
EVIDENCE = {
    "status": "ok",
    "entries": [{
        "id_suffix": "01234567", "description": "Existing work",
        "project_id_suffix": "123456", "tag_id_suffixes": ["01234567"],
        "start": "2026-09-10 12:00", "end": "2026-09-10 13:00",
        "running": False, "running_snapshot": None, "duration": "PT1H",
        "billable": False,
    }],
    "pages_fetched": 1, "running_entry_count": 0,
    "running_entry_snapshot_count": 0,
    "collection_snapshot": {
        "observed_at": OBSERVED, "boundary": "2026-10-01T00:00:00Z",
        "requested_until": "2026-10-01T00:00:00Z",
    },
    "complete": True,
}


class ClockifyCheckpointSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(snapshot, "immutable checkpoint transport is missing")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = checkpoints.PageCheckpointStore(self.root / "source-cache")
        self.identity = collector._clockify_checkpoint_identity(
            "workspace-one", "user-one", SINCE, UNTIL
        )
        self.source_evidence = self.root / "source-run/evidence/clockify-existing.json"
        self.source_evidence.parent.mkdir(parents=True)
        self.source_evidence.write_text(json.dumps(EVIDENCE, indent=2) + "\n")
        self.destination = self.root / "fresh-run/evidence/clockify-native-checkpoint"

    def checkpoint(self, pages=None, *, complete=True, continuation=None, metadata=None):
        state = self.store.open(self.identity, initial_metadata=metadata or {"snapshot_at": OBSERVED})
        for number, entries in enumerate(pages or [[copy.deepcopy(ENTRY)]], 1):
            state = self.store.append_page(
                state, payload=entries,
                continuation=continuation if continuation is not None else {"page": number + 1},
                signature=collector._clockify_page_signature(entries),
            )
        if complete:
            state = self.store.mark_complete(state)
        return state.directory / "manifest.json"

    def capture(self, manifest=None, **overrides):
        arguments = dict(
            checkpoint_manifest=manifest or self.checkpoint(),
            clockify_evidence=self.source_evidence,
            destination=self.destination, workspace_id="workspace-one", user_id="user-one",
            since=SINCE, until=UNTIL,
        )
        arguments.update(overrides)
        return snapshot.capture_checkpoint_snapshot(**arguments)

    def load(self, captured, **overrides):
        arguments = dict(workspace_id="workspace-one", user_id="user-one", since=SINCE, until=UNTIL,
                         expected_manifest_sha256=captured.manifest_sha256)
        arguments.update(overrides)
        return snapshot.load_checkpoint_snapshot(self.destination, **arguments)

    def test_capture_preserves_full_native_bytes_and_uses_existing_projection_without_network(self):
        manifest = self.checkpoint()
        original = {"manifest.json": manifest.read_bytes(),
                    "pages/000001.json": (manifest.parent / "pages/000001.json").read_bytes()}
        with patch.object(collector, "clockify_get", side_effect=AssertionError("network forbidden")):
            captured = self.capture(manifest)
            loaded = self.load(captured)
        self.assertEqual([ENTRY], loaded.entries)
        self.assertEqual(OBSERVED, loaded.manifest["snapshot_at"])
        self.assertEqual(1, loaded.manifest["entry_count"])
        self.assertEqual(hashlib.sha256((self.destination / "snapshot.json").read_bytes()).hexdigest(),
                         captured.manifest_sha256)
        copied = self.destination / "checkpoint" / self.identity_directory_name()
        for relative, raw in original.items():
            self.assertEqual(raw, (copied / relative).read_bytes())
            self.assertEqual(raw, (manifest.parent / relative).read_bytes())
        self.assertEqual(self.source_evidence.read_bytes(), (self.destination / "clockify-existing.json").read_bytes())

    def identity_directory_name(self):
        return self.store._directory_for(self.identity).name

    def test_replay_never_reopens_changed_source_cache_or_original_evidence(self):
        manifest = self.checkpoint()
        captured = self.capture(manifest)
        manifest.write_text("changed source cache")
        (manifest.parent / "pages/000001.json").write_text("changed source page")
        self.source_evidence.write_text("changed source evidence")
        loaded = self.load(captured)
        self.assertEqual([ENTRY], loaded.entries)

    def test_verified_artifact_bytes_are_the_exact_bytes_consumed_by_replay(self):
        captured = self.capture()
        loaded = self.load(captured)
        raw = (self.destination / "clockify-existing.json").read_bytes()
        (self.destination / "clockify-existing.json").write_text("later changed bytes")
        self.assertTrue(hasattr(loaded, "verified_artifact_bytes"), "verified replay bytes are missing")
        self.assertEqual(raw, loaded.verified_artifact_bytes["clockify-existing.json"])
        self.assertEqual(loaded.manifest_sha256,
                         hashlib.sha256(loaded.verified_artifact_bytes["snapshot.json"]).hexdigest())
        self.assertEqual(set(loaded.manifest["files"]) | {"snapshot.json"}, set(loaded.verified_artifact_bytes))

    def test_derived_destination_cannot_write_into_original_cache_or_old_source_run(self):
        manifest = self.checkpoint()
        for destination in (manifest.parent / "derived", self.store.root / "derived",
                            self.source_evidence.parent / "clockify-native-checkpoint"):
            with self.subTest(destination=destination):
                with self.assertRaisesRegex(ValueError, "original"):
                    self.capture(manifest, destination=destination)
                self.assertFalse(destination.exists())

    def test_changed_while_read_source_is_rejected_before_copy(self):
        manifest = self.checkpoint()
        from scripts import collector_receipts
        original_read = collector_receipts.os.read
        with patch.object(collector_receipts.os, "read", wraps=collector_receipts.os.read) as read:
            changed = False

            def change_during_read(descriptor, size):
                nonlocal changed
                data = original_read(descriptor, size)
                if data and not changed:
                    manifest.write_bytes(manifest.read_bytes() + b" ")
                    changed = True
                return data

            read.side_effect = change_during_read
            with self.assertRaisesRegex(ValueError, "changed while being read"):
                self.capture(manifest)
        self.assertFalse(self.destination.exists())

    def test_projection_mismatch_fails_before_creating_destination(self):
        evidence = copy.deepcopy(EVIDENCE)
        evidence["entries"][0]["description"] = "Different source run"
        self.source_evidence.write_text(json.dumps(evidence))
        with self.assertRaisesRegex(ValueError, "projection"):
            self.capture()
        self.assertFalse(self.destination.exists())

    def test_duplicate_full_native_ids_fail_even_when_suffix_projection_matches(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.capture(self.checkpoint([[copy.deepcopy(ENTRY), copy.deepcopy(ENTRY)]]))
        self.assertFalse(self.destination.exists())

    def test_missing_full_native_fields_fail(self):
        entry = copy.deepcopy(ENTRY)
        del entry["taskId"]
        with self.assertRaisesRegex(ValueError, "native"):
            self.capture(self.checkpoint([[entry]]))

    def test_foreign_workspace_or_user_rows_fail(self):
        for field in ("workspaceId", "userId"):
            with self.subTest(field=field):
                with tempfile.TemporaryDirectory() as directory:
                    self.store = checkpoints.PageCheckpointStore(Path(directory))
                    entry = copy.deepcopy(ENTRY)
                    entry[field] = "foreign"
                    with self.assertRaisesRegex(ValueError, "workspace|user"):
                        self.capture(self.checkpoint([[entry]]))

    def test_foreign_trusted_request_identity_fails(self):
        manifest = self.checkpoint()
        for changes in ({"workspace_id": "foreign"}, {"user_id": "foreign"},
                        {"since": SINCE + dt.timedelta(days=1)}, {"until": UNTIL + dt.timedelta(days=1)}):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, "identity|locator"):
                    self.capture(manifest, **changes)

    def test_incomplete_checkpoint_fails(self):
        with self.assertRaisesRegex(ValueError, "complete"):
            self.capture(self.checkpoint(complete=False))

    def test_bad_page_continuation_fails(self):
        with self.assertRaisesRegex(ValueError, "continuation"):
            self.capture(self.checkpoint(continuation={"page": 99}))

    def test_short_nonterminal_page_is_not_complete_pagination(self):
        second = copy.deepcopy(ENTRY)
        second["id"] = "fedcba987654321001234568"
        with self.assertRaisesRegex(ValueError, "nonterminal"):
            self.capture(self.checkpoint([[ENTRY], [second]]))

    def test_complete_checkpoint_requires_short_terminal_page(self):
        entries = []
        for index in range(collector.CLOCKIFY_PAGE_SIZE):
            entry = copy.deepcopy(ENTRY)
            entry["id"] = f"{index:024x}"
            entries.append(entry)
        with self.assertRaisesRegex(ValueError, "final page"):
            self.capture(self.checkpoint([entries]))

    def test_invalid_snapshot_at_fails(self):
        with self.assertRaisesRegex(ValueError, "snapshot_at"):
            self.capture(self.checkpoint(metadata={"snapshot_at": "2026-10-02T12:00:00"}))

    def test_snapshot_tampering_is_rejected(self):
        captured = self.capture()
        targets = [self.destination / "snapshot.json", self.destination / "clockify-existing.json",
                   self.destination / "checkpoint" / self.identity_directory_name() / "manifest.json",
                   self.destination / "checkpoint" / self.identity_directory_name() / "pages/000001.json"]
        for target in targets:
            with self.subTest(target=target.name):
                original = target.read_bytes()
                target.write_bytes(original + b" ")
                with self.assertRaisesRegex(ValueError, "hash|digest"):
                    self.load(captured)
                target.write_bytes(original)

    def test_replay_requires_external_proof_hash_and_matching_identity(self):
        captured = self.capture()
        for changes in ({"expected_manifest_sha256": None}, {"expected_manifest_sha256": "0" * 64},
                        {"workspace_id": "foreign"}, {"user_id": "foreign"},
                        {"until": UNTIL + dt.timedelta(days=1)}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.load(captured, **changes)

    def test_symlinked_source_component_fails(self):
        manifest = self.checkpoint()
        link = self.root / "cache-link"
        link.symlink_to(self.store.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.capture(link / manifest.parent.name / "manifest.json")

    def test_symlinked_snapshot_component_fails(self):
        captured = self.capture()
        page = self.destination / "checkpoint" / self.identity_directory_name() / "pages/000001.json"
        outside = self.root / "outside.json"
        outside.write_bytes(page.read_bytes())
        page.unlink()
        page.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.load(captured)

    def test_destination_must_be_fresh_and_not_symlinked(self):
        manifest = self.checkpoint()
        self.destination.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "fresh"):
            self.capture(manifest)
        link = self.root / "destination-link"
        link.symlink_to(self.root / "fresh-run", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.capture(manifest, destination=link / "other")

    def test_renamed_or_ambiguous_checkpoint_locator_fails(self):
        manifest = self.checkpoint()
        renamed = manifest.parent.with_name("unbound-checkpoint")
        manifest.parent.rename(renamed)
        with self.assertRaisesRegex(ValueError, "locator"):
            self.capture(renamed / "manifest.json")

    def test_absent_optional_snapshot_preserves_compatibility(self):
        self.assertIsNone(snapshot.load_checkpoint_snapshot(
            None, workspace_id="workspace-one", user_id="user-one", since=SINCE, until=UNTIL,
            expected_manifest_sha256=None))

    def test_transport_imports_for_direct_script_consumer(self):
        scripts = Path(__file__).resolve().parents[1] / "scripts"
        result = subprocess.run(
            [sys.executable, "-c", "import clockify_checkpoint_snapshot"],
            cwd=self.root, env={**os.environ, "PYTHONPATH": str(scripts), "PYTHONDONTWRITEBYTECODE": "1"},
            text=True, capture_output=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
