from __future__ import annotations

import json
from pathlib import Path
import plistlib
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
            "CLOCKIFY_ANALYZER_PRIMARY_MODEL": "deepseek-v4-flash:0731-cloud",
            "CLOCKIFY_ANALYZER_PRIMARY_REVISION": runner.APPROVED_FLASH_REVISION,
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
        self.assertEqual(
            "deepseek-v4-flash:0731-cloud",
            status["analyzer_route"]["model"],
        )
        self.assertEqual(
            runner.APPROVED_FLASH_REVISION,
            status["analyzer_route"]["revision"],
        )

    def test_non_flash_or_wrong_revision_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root, run_dir, cache = self._layout(directory)
            for name, value in (
                ("CLOCKIFY_ANALYZER_PRIMARY_MODEL", "deepseek-v4-pro:cloud"),
                ("CLOCKIFY_ANALYZER_PRIMARY_REVISION", "0" * 64),
            ):
                environment = self._environment(root, run_dir, cache)
                environment[name] = value
                with self.subTest(name=name):
                    self.assertEqual(2, runner.run(environment))

    def test_sealed_cache_rejects_model_tag_drift_and_mixing(self):
        with tempfile.TemporaryDirectory() as directory:
            root, run_dir, cache = self._layout(directory)
            cache.parent.mkdir(parents=True)
            environment = self._environment(root, run_dir, cache)
            cache.write_text(
                json.dumps({"model": "deepseek-v4-flash:cloud"}) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(2, runner.run(environment))

            cache.write_text(
                "\n".join([
                    json.dumps({"model": "deepseek-v4-flash:0731-cloud"}),
                    json.dumps({"model": "deepseek-v4-flash:cloud"}),
                ]) + "\n",
                encoding="utf-8",
            )
            self.assertEqual(2, runner.run(environment))

    def test_sealed_cache_accepts_its_exact_model_tag(self):
        with tempfile.TemporaryDirectory() as directory:
            root, run_dir, cache = self._layout(directory)
            cache.parent.mkdir(parents=True)
            cache.write_text(
                json.dumps({"model": "deepseek-v4-flash:0731-cloud"}) + "\n",
                encoding="utf-8",
            )
            completed = subprocess.CompletedProcess(["fixture"], 2)
            with mock.patch.object(runner.subprocess, "run", return_value=completed):
                self.assertEqual(2, runner.run(self._environment(root, run_dir, cache)))
            status = json.loads((cache.parent / "status.json").read_text())
            self.assertEqual("blocked", status["state"])

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

    def test_systemd_templates_target_the_current_desktop_checkout(self):
        root = Path(__file__).resolve().parents[1]
        service = (root / "ops/systemd/clockify-work-accounting.service").read_text()
        environment = (
            root / "ops/systemd/clockify-work-accounting.env.example"
        ).read_text()

        self.assertIn("%h/Work/automation-clockify-sync", service)
        self.assertIn(
            "CLOCKIFY_ACCOUNTING_ROOT=/home/blackthorne/Work/automation-clockify-sync",
            environment,
        )
        self.assertNotIn("Work/serenichron/automation/clockify-sync", service)
        self.assertNotIn("Work/serenichron/automation/clockify-sync", environment)

    def test_launchd_templates_use_guarded_runner_without_embedded_secrets(self):
        root = Path(__file__).resolve().parents[1]
        wrapper_path = root / "ops/launchd/clockify-work-accounting.sh"
        environment_path = root / "ops/launchd/clockify-work-accounting.env.example"
        plist_path = root / "ops/launchd/com.serenichron.clockify-work-accounting.plist"

        wrapper = wrapper_path.read_text()
        environment = environment_path.read_text()
        with plist_path.open("rb") as handle:
            launch_agent = plistlib.load(handle)

        self.assertEqual(
            [
                "/Users/blackthorne/Work/automation-clockify-sync/ops/launchd/"
                "clockify-work-accounting.sh"
            ],
            launch_agent["ProgramArguments"],
        )
        self.assertTrue(launch_agent["RunAtLoad"])
        self.assertEqual({"SuccessfulExit": False}, launch_agent["KeepAlive"])
        self.assertEqual(60, launch_agent["ThrottleInterval"])
        self.assertIn("clockify_accounting_runner.py", wrapper)
        self.assertIn('[[ "${runner_exit}" -eq 2 ]] && exit 0', wrapper)
        self.assertIn("unset CLOCKIFY_ANALYZER_FALLBACK_URL", wrapper)
        self.assertIn(
            "CLOCKIFY_ANALYZER_PRIMARY_MODEL=deepseek-v4-flash:cloud",
            environment,
        )
        self.assertIn("CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED=", environment)
        self.assertNotIn("API_KEY=", environment)


if __name__ == "__main__":
    unittest.main()
