from __future__ import annotations

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import clockify_sync_collect as collector
from scripts import collector_checkpoints


ROOT = Path(__file__).parents[1]


class CollectorEntrypointTests(unittest.TestCase):
    def test_top_level_entrypoint_delegates_to_canonical_module(self):
        command = [sys.executable, str(ROOT / "clockify_sync_collect.py"), "--help"]
        result = subprocess.run(command, check=False, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("Clockify sync dry-run collector", result.stdout)

    def test_only_one_collector_implementation_remains(self):
        wrapper = (ROOT / "clockify_sync_collect.py").read_text()
        self.assertIn("from scripts.clockify_sync_collect import main", wrapper)
        self.assertLess(len(wrapper.splitlines()), 20)

    @staticmethod
    def _checkpoint_identity(source: str) -> collector_checkpoints.CheckpointIdentity:
        return collector_checkpoints.CheckpointIdentity(
            source=source,
            since_utc="2026-08-01T00:00:00Z",
            until_utc="2026-08-03T00:00:00Z",
            request_fingerprint=f"sha256:{source}",
            compatibility_version="collector/v1",
        )

    @classmethod
    def _write_checkpoint(
        cls,
        root: Path,
        source: str,
        *,
        complete: bool,
        completed_at: str | None = None,
    ) -> Path:
        store = collector_checkpoints.PageCheckpointStore(root)
        state = store.open(cls._checkpoint_identity(source))
        state = store.append_page(
            state,
            payload=[{"secret_payload": "payload-secret"}],
            continuation={"cursor": "cursor-secret"},
            signature="signature-secret",
        )
        if complete:
            state = store.mark_complete(state)
            if completed_at is not None:
                manifest_path = state.directory / "manifest.json"
                manifest = json.loads(manifest_path.read_text())
                manifest["completed_at"] = completed_at
                manifest_path.write_text(json.dumps(manifest) + "\n")
        return state.directory

    @staticmethod
    def _invoke_main(*arguments: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.object(sys, "argv", ["clockify_sync_collect.py", *arguments]):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    code = collector.main()
                except SystemExit as error:
                    code = error.code
        return int(code), stdout.getvalue(), stderr.getvalue()

    def test_cleanup_removes_only_old_complete_checkpoints_and_redacts_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkpoints"
            root.mkdir()
            old = self._write_checkpoint(
                root,
                "old",
                complete=True,
                completed_at="2026-08-01T23:59:59Z",
            )
            recent = self._write_checkpoint(
                root,
                "recent",
                complete=True,
                completed_at="2026-08-02T00:00:00Z",
            )
            incomplete = self._write_checkpoint(root, "incomplete", complete=False)
            corrupt = root / ("c" * 64)
            corrupt.mkdir()
            (corrupt / "manifest.json").write_text(
                '{"credential":"credential-secret","cursor":"cursor-secret"}\n'
            )

            code, stdout, stderr = self._invoke_main(
                "cleanup-checkpoints",
                "--completed-before",
                "2026-08-02",
                "--checkpoint-root",
                str(root),
            )

            self.assertEqual(0, code, stderr)
            self.assertIn("removed=1 preserved=3", stdout)
            self.assertIn(old.name, stdout)
            self.assertNotIn(recent.name, stdout)  # preserved identities are counts only
            self.assertNotIn("payload-secret", stdout)
            self.assertNotIn("cursor-secret", stdout)
            self.assertNotIn("credential-secret", stdout)
            self.assertFalse(old.exists())
            self.assertTrue(recent.exists())
            self.assertTrue(incomplete.exists())
            self.assertTrue(corrupt.exists())

    def test_cleanup_rejects_relative_and_invalid_roots_without_deletion(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkpoints"
            root.mkdir()
            old = self._write_checkpoint(
                root,
                "old",
                complete=True,
                completed_at="2026-08-01T23:59:59Z",
            )

            relative_code, _, relative_stderr = self._invoke_main(
                "cleanup-checkpoints",
                "--completed-before",
                "2026-08-02",
                "--checkpoint-root",
                "relative-checkpoints",
            )
            invalid_code, _, invalid_stderr = self._invoke_main(
                "cleanup-checkpoints",
                "--completed-before",
                "2026-08-02",
                "--checkpoint-root",
                str(Path(directory) / "missing"),
            )

            self.assertEqual(2, relative_code, relative_stderr)
            self.assertEqual(2, invalid_code, invalid_stderr)
            self.assertTrue(old.exists())

    def test_cleanup_uses_utc_date_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkpoints"
            root.mkdir()
            boundary = self._write_checkpoint(
                root,
                "boundary",
                complete=True,
                completed_at="2026-08-02T00:00:00Z",
            )
            before_boundary = self._write_checkpoint(
                root,
                "before",
                complete=True,
                completed_at="2026-08-01T23:59:59Z",
            )

            code, stdout, stderr = self._invoke_main(
                "cleanup-checkpoints",
                "--completed-before",
                "2026-08-02",
                "--checkpoint-root",
                str(root),
            )

            self.assertEqual(0, code, stderr)
            self.assertIn("removed=1 preserved=1", stdout)
            self.assertTrue(boundary.exists())
            self.assertFalse(before_boundary.exists())


if __name__ == "__main__":
    unittest.main()
