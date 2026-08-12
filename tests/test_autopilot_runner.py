import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts import clockify_autopilot_runner as runner


class AutopilotRunnerTests(unittest.TestCase):
    def fixture(
        self,
        directory: str,
        action: str,
        **result_fields,
    ) -> tuple[dict[str, str], Path]:
        root = Path(directory)
        (root / "scripts").mkdir()
        (root / "scripts" / "clockify_review_run.py").write_text("# fixture\n")
        run = root / "runs" / "run-1"
        run.mkdir(parents=True)
        result = run / "autopilot-result.json"
        result.write_text(
            json.dumps({"action": action, **result_fields}) + "\n"
        )
        environment = {
            "CLOCKIFY_AUTOPILOT_ROOT": str(root),
            "CLOCKIFY_AUTOPILOT_STATUS": str(root / "state" / "status.json"),
            "CLOCKIFY_AUTOPILOT_LOCK": str(root / "state" / "runner.lock"),
            "CLOCKIFY_AUTOPILOT_MAX_COVERAGE_RETRIES": "2",
        }
        return environment, result

    def test_complete_run_records_compact_status(self):
        with tempfile.TemporaryDirectory() as directory:
            environment, result = self.fixture(directory, "review_delta")
            completed = subprocess.CompletedProcess(
                ["review"], 0, stdout=str(result) + "\n", stderr=""
            )
            with mock.patch.object(runner.subprocess, "run", return_value=completed):
                self.assertEqual(0, runner.run(environment))
            status = json.loads(Path(environment["CLOCKIFY_AUTOPILOT_STATUS"]).read_text())
        self.assertEqual("complete", status["state"])
        self.assertEqual("review_delta", status["action"])

    def test_command_passes_explicit_analyzer_tuning(self):
        with tempfile.TemporaryDirectory() as directory:
            environment, _result = self.fixture(directory, "no_comment")
            environment.update({
                "CLOCKIFY_AUTOPILOT_ANALYZER_TARGET_BODY_BYTES": "250000",
                "CLOCKIFY_AUTOPILOT_ANALYZER_MAX_EVENTS": "250",
                "CLOCKIFY_AUTOPILOT_ANALYZER_WORKERS": "8",
            })
            command = runner._command(environment, Path(directory))
        self.assertIn("--analyzer-target-body-bytes", command)
        self.assertIn("--analyzer-max-events-per-chunk", command)
        self.assertEqual("8", command[command.index("--analyzer-workers") + 1])

    def test_coverage_warning_retries_twice_then_stops_looping(self):
        with tempfile.TemporaryDirectory() as directory:
            environment, result = self.fixture(directory, "coverage_warning")
            completed = subprocess.CompletedProcess(
                ["review"], 0, stdout=str(result) + "\n", stderr=""
            )
            with mock.patch.object(runner.subprocess, "run", return_value=completed):
                self.assertEqual(runner.TEMPORARY_COVERAGE_EXIT, runner.run(environment))
                self.assertEqual(runner.TEMPORARY_COVERAGE_EXIT, runner.run(environment))
                self.assertEqual(0, runner.run(environment))
            status = json.loads(Path(environment["CLOCKIFY_AUTOPILOT_STATUS"]).read_text())
        self.assertEqual("coverage_exhausted", status["state"])
        self.assertEqual(3, status["coverage_retry_attempts"])

    def test_next_scheduled_run_retries_debt_after_prior_retry_exhaustion(self):
        with tempfile.TemporaryDirectory() as directory:
            environment, result = self.fixture(directory, "coverage_warning")
            completed = subprocess.CompletedProcess(
                ["review"], 0, stdout=str(result) + "\n", stderr=""
            )
            with mock.patch.object(runner.subprocess, "run", return_value=completed):
                runner.run(environment)
                runner.run(environment)
                self.assertEqual(0, runner.run(environment))
                self.assertEqual(runner.TEMPORARY_COVERAGE_EXIT, runner.run(environment))
            status = json.loads(Path(environment["CLOCKIFY_AUTOPILOT_STATUS"]).read_text())
        self.assertEqual("retry_scheduled", status["state"])
        self.assertEqual(1, status["coverage_retry_attempts"])

    def test_first_deploy_bootstraps_prior_missed_interval_into_command(self):
        with tempfile.TemporaryDirectory() as directory:
            environment, result = self.fixture(directory, "coverage_warning")
            prior = Path(directory) / "runs" / "20260812T063103Z"
            prior.mkdir()
            (prior / "run-report.json").write_text(json.dumps({
                "run_id": prior.name,
                "date_range": {
                    "since": "2026-07-30 00:00",
                    "until": "2026-08-12 09:31",
                },
                "evidence_ledger": {"source_completeness": {
                    "status": "incomplete",
                    "incomplete_sources": ["sessions/macbook"],
                    "sources": {
                        "clockify": {"status": "complete"},
                        "fathom": {"status": "complete"},
                        "sessions/omarchy-precision": {"status": "complete"},
                        "sessions/macbook": {"status": "unavailable"},
                    },
                }},
            }))
            completed = subprocess.CompletedProcess(
                ["review"], 0, stdout=str(result) + "\n", stderr=""
            )
            with mock.patch.object(
                runner.subprocess, "run", return_value=completed
            ) as invoked:
                runner.run(environment)

            command = invoked.call_args.args[0]
            status = json.loads(Path(environment["CLOCKIFY_AUTOPILOT_STATUS"]).read_text())

        self.assertEqual("2026-07-30", command[command.index("--since") + 1])
        self.assertEqual("2026-07-30", status["effective_since"])

    def test_blocked_peer_only_coverage_schedules_delayed_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            environment, result = self.fixture(
                directory,
                "blocked",
                source_completeness={
                    "status": "incomplete",
                    "incomplete_sources": [
                        "sessions/macbook",
                        "repositories/omarchy-desktop",
                    ],
                },
            )
            environment["CLOCKIFY_AUTOPILOT_COORDINATOR"] = "omarchy-precision"
            completed = subprocess.CompletedProcess(
                ["review"], 2, stdout=str(result) + "\n", stderr="coverage incomplete"
            )
            with mock.patch.object(runner.subprocess, "run", return_value=completed):
                self.assertEqual(runner.TEMPORARY_COVERAGE_EXIT, runner.run(environment))
            status = json.loads(Path(environment["CLOCKIFY_AUTOPILOT_STATUS"]).read_text())
        self.assertEqual("retry_scheduled", status["state"])
        self.assertEqual(1, status["coverage_retry_attempts"])

    def test_blocked_coordinator_coverage_remains_hard_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            environment, result = self.fixture(
                directory,
                "blocked",
                source_completeness={
                    "status": "incomplete",
                    "incomplete_sources": ["sessions/omarchy-precision"],
                },
            )
            environment["CLOCKIFY_AUTOPILOT_COORDINATOR"] = "omarchy-precision"
            completed = subprocess.CompletedProcess(
                ["review"], 2, stdout=str(result) + "\n", stderr="coverage incomplete"
            )
            with mock.patch.object(runner.subprocess, "run", return_value=completed):
                self.assertEqual(2, runner.run(environment))
            status = json.loads(Path(environment["CLOCKIFY_AUTOPILOT_STATUS"]).read_text())
        self.assertEqual("blocked", status["state"])
        self.assertEqual(0, status["coverage_retry_attempts"])

    def test_mark_reported_accepts_only_current_result(self):
        with tempfile.TemporaryDirectory() as directory:
            environment, result = self.fixture(directory, "review_delta")
            completed = subprocess.CompletedProcess(
                ["review"], 0, stdout=str(result) + "\n", stderr=""
            )
            with mock.patch.object(runner.subprocess, "run", return_value=completed):
                self.assertEqual(0, runner.run(environment))
            self.assertEqual(0, runner.mark_reported(environment, result))
            status = json.loads(Path(environment["CLOCKIFY_AUTOPILOT_STATUS"]).read_text())
            self.assertIn("reported_at", status)
            self.assertEqual(
                2, runner.mark_reported(environment, result.with_name("other.json"))
            )


if __name__ == "__main__":
    unittest.main()
