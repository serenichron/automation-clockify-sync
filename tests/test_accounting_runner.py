from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts import clockify_accounting_runner as runner


class AccountingRunnerTests(unittest.TestCase):
    def _environment(self, root: Path, run_dir: Path, cache: Path) -> dict[str, str]:
        return {
            "CLOCKIFY_ACCOUNTING_ROOT": str(root),
            "CLOCKIFY_ACCOUNTING_RUN_DIR": str(run_dir),
            "CLOCKIFY_ACCOUNTING_CACHE": str(cache),
            "CLOCKIFY_ACCOUNTING_STATUS": str(cache.parent / "status.json"),
            "CLOCKIFY_ACCOUNTING_LOCK": str(cache.parent / "runner.lock"),
            "CLOCKIFY_ACCOUNTING_TARGET_BODY_BYTES": "250000",
            "CLOCKIFY_ACCOUNTING_MAX_EVENTS": "250",
            "CLOCKIFY_ACCOUNTING_WORKERS": "4",
        }

    def _layout(self, directory: str) -> tuple[Path, Path, Path]:
        root = Path(directory) / "repo"
        scripts = root / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "work_accounting_pipeline.py").write_text("# fixture\n")
        run_dir = root / "runs" / "run-one"
        run_dir.mkdir(parents=True)
        cache = root / "state" / "validation" / "cache.jsonl"
        return root, run_dir, cache

    def _complete_result(self, run_dir: Path) -> None:
        for name in runner.REQUIRED_RESULT_ARTIFACTS:
            (run_dir / name).write_text("{}\n")
        (run_dir / "work-accounting-result.json").write_text(
            json.dumps({"schema_version": 1, "external_writes": False}) + "\n"
        )

    def test_build_command_uses_only_explicit_absolute_paths_and_tuning(self):
        with tempfile.TemporaryDirectory() as directory:
            root, run_dir, cache = self._layout(directory)
            command, returned_run, returned_cache, status, lock = runner.build_command(
                self._environment(root, run_dir, cache),
                python_executable="/usr/bin/python3",
            )

        self.assertEqual(run_dir.resolve(), returned_run)
        self.assertEqual(cache.resolve(), returned_cache)
        self.assertEqual((cache.parent / "status.json").resolve(), status)
        self.assertEqual((cache.parent / "runner.lock").resolve(), lock)
        self.assertEqual("/usr/bin/python3", command[0])
        self.assertIn("--analyzer-workers", command)
        self.assertNotIn("CLOCKIFY_ANALYZER_PRIMARY_API_KEY", " ".join(command))

    def test_runner_records_complete_result_without_environment_dump(self):
        with tempfile.TemporaryDirectory() as directory:
            root, run_dir, cache = self._layout(directory)
            environment = self._environment(root, run_dir, cache)

            def complete(command, *, cwd, check):
                self.assertEqual(root.resolve(), cwd)
                self.assertFalse(check)
                self._complete_result(run_dir)
                return subprocess.CompletedProcess(command, 0)

            with mock.patch.object(runner.subprocess, "run", side_effect=complete):
                self.assertEqual(0, runner.run(environment))
            status = json.loads((cache.parent / "status.json").read_text())

        self.assertEqual("complete", status["state"])
        self.assertEqual(0, status["exit_code"])
        self.assertNotIn("environment", status)
        self.assertNotIn("api_key", json.dumps(status).casefold())

    def test_known_block_is_not_reported_as_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            root, run_dir, cache = self._layout(directory)
            environment = self._environment(root, run_dir, cache)
            completed = subprocess.CompletedProcess(["fixture"], 2)
            with mock.patch.object(runner.subprocess, "run", return_value=completed):
                self.assertEqual(2, runner.run(environment))
            status = json.loads((cache.parent / "status.json").read_text())

        self.assertEqual("blocked", status["state"])
        self.assertEqual(2, status["exit_code"])

    def test_completed_run_does_not_start_pipeline_again(self):
        with tempfile.TemporaryDirectory() as directory:
            root, run_dir, cache = self._layout(directory)
            self._complete_result(run_dir)
            with mock.patch.object(runner.subprocess, "run") as child:
                self.assertEqual(0, runner.run(self._environment(root, run_dir, cache)))
            child.assert_not_called()

    def test_relative_paths_fail_closed(self):
        environment = {
            "CLOCKIFY_ACCOUNTING_ROOT": "relative",
            "CLOCKIFY_ACCOUNTING_RUN_DIR": "/tmp/run",
            "CLOCKIFY_ACCOUNTING_CACHE": "/tmp/cache",
        }
        self.assertEqual(2, runner.run(environment))

    def test_relative_optional_control_paths_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root, run_dir, cache = self._layout(directory)
            for name in ("CLOCKIFY_ACCOUNTING_STATUS", "CLOCKIFY_ACCOUNTING_LOCK"):
                environment = self._environment(root, run_dir, cache)
                environment[name] = "relative/path"
                self.assertEqual(2, runner.run(environment))

    def test_invalid_completion_marker_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as directory:
            root, run_dir, cache = self._layout(directory)
            (run_dir / "work-accounting-result.json").write_text("{}\n")
            with mock.patch.object(
                runner.subprocess,
                "run",
                return_value=subprocess.CompletedProcess(["fixture"], 2),
            ) as child:
                self.assertEqual(2, runner.run(self._environment(root, run_dir, cache)))
            child.assert_called_once()

    def test_zero_exit_without_complete_artifacts_restarts_as_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root, run_dir, cache = self._layout(directory)
            completed = subprocess.CompletedProcess(["fixture"], 0)
            with mock.patch.object(runner.subprocess, "run", return_value=completed):
                self.assertEqual(1, runner.run(self._environment(root, run_dir, cache)))
            status = json.loads((cache.parent / "status.json").read_text())

        self.assertEqual("failed", status["state"])
        self.assertEqual(1, status["exit_code"])


if __name__ == "__main__":
    unittest.main()
