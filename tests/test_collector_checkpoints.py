from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest

from scripts import collector_checkpoints as checkpoints


class PageCheckpointStoreTests(unittest.TestCase):
    def identity(self):
        return checkpoints.CheckpointIdentity(
            source="clockify",
            since_utc="2026-08-01T00:00:00Z",
            until_utc="2026-08-03T00:00:00Z",
            request_fingerprint="sha256:request",
            compatibility_version="collector/v1",
        )

    def test_append_publishes_page_before_manifest_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            store = checkpoints.PageCheckpointStore(Path(directory))
            state = store.open(self.identity(), initial_metadata={"snapshot_at": "fixed"})
            state = store.append_page(
                state,
                payload=[{"id": "one"}],
                continuation={"page": 2},
                signature="sha256:page-one",
            )
            manifest = json.loads((state.directory / "manifest.json").read_text())
            page = state.directory / manifest["pages"][0]["path"]
            self.assertTrue(page.is_file())
            self.assertEqual({"page": 2}, state.continuation)
            self.assertFalse(state.complete)

    def test_identity_document_never_contains_secret_fields(self):
        document = self.identity().document()
        self.assertEqual(
            {
                "source",
                "since_utc",
                "until_utc",
                "request_fingerprint",
                "compatibility_version",
            },
            set(document),
        )

    def test_open_rejects_a_mutated_referenced_page(self):
        with tempfile.TemporaryDirectory() as directory:
            store = checkpoints.PageCheckpointStore(Path(directory))
            state = store.append_page(
                store.open(self.identity()),
                payload=[{"id": "one"}],
                continuation={},
                signature="sha256:page-one",
            )
            page_path = state.directory / "pages/000001.json"
            page = json.loads(page_path.read_text())
            page["payload"] = [{"id": "changed"}]
            page_path.write_text(json.dumps(page))

            with self.assertRaises(checkpoints.CheckpointError):
                store.open(self.identity())

    def test_open_rejects_a_missing_referenced_page(self):
        with tempfile.TemporaryDirectory() as directory:
            store = checkpoints.PageCheckpointStore(Path(directory))
            state = store.append_page(
                store.open(self.identity()),
                payload=[{"id": "one"}],
                continuation={},
                signature="sha256:page-one",
            )
            (state.directory / "pages/000001.json").unlink()

            with self.assertRaises(checkpoints.CheckpointError):
                store.open(self.identity())

    def test_open_ignores_an_unreferenced_page_file(self):
        with tempfile.TemporaryDirectory() as directory:
            store = checkpoints.PageCheckpointStore(Path(directory))
            state = store.append_page(
                store.open(self.identity()),
                payload=[{"id": "one"}],
                continuation={},
                signature="sha256:page-one",
            )
            orphan = state.directory / "pages/999999.json"
            orphan.write_text('{"not":"a checkpoint page"}\n')

            reopened = store.open(self.identity())

            self.assertEqual(1, len(reopened.pages))

    def test_open_validates_pages_without_retaining_payload_bodies(self):
        with tempfile.TemporaryDirectory() as directory:
            store = checkpoints.PageCheckpointStore(Path(directory))
            state = store.append_page(
                store.open(self.identity()),
                payload=[{"id": "one", "detail": "must not remain in state"}],
                continuation={},
                signature="sha256:page-one",
            )

            reopened = store.open(self.identity())

            self.assertEqual(
                {"index", "path", "payload_digest", "page_digest"},
                set(reopened.pages[0]),
            )
            page_path = state.directory / "pages/000001.json"
            page = json.loads(page_path.read_text())
            page["payload"] = [{"id": "changed"}]
            page_path.write_text(json.dumps(page))
            with self.assertRaises(checkpoints.CheckpointError):
                store.open(self.identity())

    def test_open_rejects_manifest_for_a_different_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            store = checkpoints.PageCheckpointStore(Path(directory))
            original = store.open(self.identity())
            different = checkpoints.CheckpointIdentity(
                source="clockify",
                since_utc="2026-08-04T00:00:00Z",
                until_utc="2026-08-05T00:00:00Z",
                request_fingerprint="sha256:request",
                compatibility_version="collector/v1",
            )
            different_directory = store.root / checkpoints._digest(different.document())[7:]
            different_directory.mkdir()
            (different_directory / "manifest.json").write_text(
                (original.directory / "manifest.json").read_text()
            )

            with self.assertRaises(checkpoints.CheckpointError):
                store.open(different)

    def test_completed_checkpoint_rejects_another_page(self):
        with tempfile.TemporaryDirectory() as directory:
            store = checkpoints.PageCheckpointStore(Path(directory))
            state = store.append_page(
                store.open(self.identity()),
                payload=[{"id": "one"}],
                continuation={},
                signature="sha256:page-one",
            )
            completed = store.mark_complete(state)

            with self.assertRaises(checkpoints.CheckpointError):
                store.append_page(
                    completed,
                    payload=[{"id": "two"}],
                    continuation={},
                    signature="sha256:page-two",
                )

    def test_open_rejects_completion_with_an_incorrect_aggregate_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            store = checkpoints.PageCheckpointStore(Path(directory))
            state = store.append_page(
                store.open(self.identity()),
                payload=[{"id": "one"}],
                continuation={},
                signature="sha256:page-one",
            )
            completed = store.mark_complete(state)
            manifest_path = completed.directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["pages_digest"] = "sha256:incorrect"
            manifest_path.write_text(json.dumps(manifest))

            with self.assertRaises(checkpoints.CheckpointError):
                store.open(self.identity())

    def test_open_rejects_a_manifest_with_an_unsafe_page_path(self):
        with tempfile.TemporaryDirectory() as directory:
            store = checkpoints.PageCheckpointStore(Path(directory))
            state = store.append_page(
                store.open(self.identity()),
                payload=[{"id": "one"}],
                continuation={},
                signature="sha256:page-one",
            )
            manifest_path = state.directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["pages"][0]["path"] = "pages/../../elsewhere.json"
            manifest_path.write_text(json.dumps(manifest))

            with self.assertRaises(checkpoints.CheckpointError):
                store.open(self.identity())

    def test_open_rejects_a_manifest_with_a_non_mapping_continuation(self):
        with tempfile.TemporaryDirectory() as directory:
            store = checkpoints.PageCheckpointStore(Path(directory))
            state = store.open(self.identity())
            manifest_path = state.directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["continuation"] = None
            manifest_path.write_text(json.dumps(manifest))

            with self.assertRaises(checkpoints.CheckpointError):
                store.open(self.identity())

    def test_open_rejects_a_boolean_page_index(self):
        with tempfile.TemporaryDirectory() as directory:
            store = checkpoints.PageCheckpointStore(Path(directory))
            state = store.append_page(
                store.open(self.identity()),
                payload=[{"id": "one"}],
                continuation={},
                signature="sha256:page-one",
            )
            manifest_path = state.directory / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["pages"][0]["index"] = True
            manifest_path.write_text(json.dumps(manifest))

            with self.assertRaises(checkpoints.CheckpointError):
                store.open(self.identity())

    def test_cleanup_removes_only_old_verified_complete_checkpoints(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = checkpoints.PageCheckpointStore(root)
            complete = store.mark_complete(store.open(self.identity()))
            incomplete_identity = checkpoints.CheckpointIdentity(
                source="clockify",
                since_utc="2026-08-04T00:00:00Z",
                until_utc="2026-08-05T00:00:00Z",
                request_fingerprint="sha256:request",
                compatibility_version="collector/v1",
            )
            incomplete = store.open(incomplete_identity)
            malformed = root / "malformed"
            malformed.mkdir()
            (malformed / "manifest.json").write_text("{not json}\n")

            removed = store.remove_completed_before(
                datetime.now(timezone.utc) + timedelta(seconds=1)
            )

            self.assertEqual((complete.directory,), removed)
            self.assertFalse(complete.directory.exists())
            self.assertTrue(incomplete.directory.exists())
            self.assertTrue(malformed.exists())


if __name__ == "__main__":
    unittest.main()
