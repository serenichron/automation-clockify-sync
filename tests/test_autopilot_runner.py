import json
import datetime as dt
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts import clockify_autopilot_runner as runner
from scripts import clockify_review_run as review_run
from scripts import collector_receipts, collector_slices, source_coverage
from scripts.autopilot_process import ChildResult


class AutopilotRunnerTests(unittest.TestCase):
    @staticmethod
    def child_result(command, returncode, stdout="", stderr=""):
        return ChildResult(returncode, stdout, stderr, False, 0.01)

    def test_verified_bundle_resolves_only_matching_exact_active_debt(self):
        """Adjacent debt remains active when a verified bundle covers only its own slice."""
        with tempfile.TemporaryDirectory() as directory:
            environment, result = self.fixture(directory, "review_delta")
            run_dir = result.parent
            slice_ = collector_slices.CollectionSlice(
                dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 8, 2, tzinfo=dt.timezone.utc), "slice-1",
            )
            for relative in (
                "run-report.json", "evidence/evidence-ledger.json", "semantic-analysis.json",
                "work-accounting-result.json", "quality_report.json", "review-snapshot.json",
            ):
                path = run_dir / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"fixture": relative}) + "\n")
            canonical_coverage = {"status": "complete", "incomplete_sources": []}
            (run_dir / "evidence" / "evidence-ledger.json").write_text(json.dumps({
                "manifest": {"source_completeness": canonical_coverage},
            }) + "\n")
            (run_dir / "run-report.json").write_text(json.dumps({
                "runtime_identity": {"git_sha": "fixture"},
                "date_range": {"since": "2026-08-01T00:00:00Z", "until": "2026-08-02T00:00:00Z"},
                "evidence_ledger": {"source_completeness": canonical_coverage},
            }) + "\n")
            bundle = collector_receipts.build_completion_bundle(run_dir, slice_=slice_)
            collector_receipts.write_completion_bundle(run_dir / "completion-bundle.json", bundle)
            result.write_text(json.dumps({
                "action": "review_delta", "run_id": "run-1", "run_dir": str(run_dir),
                "slice_id": "slice-1",
                "date_range": {"since": "2026-08-01T00:00:00Z", "until": "2026-08-02T00:00:00Z"},
                # The action contract is deliberately stale; only the verified
                # run-report/ledger coverage may decide whether debt clears.
                "source_completeness": {"status": "incomplete", "incomplete_sources": ["sessions/macbook"]},
                "completion_bundle_digest": bundle.bundle_digest,
            }) + "\n")
            first = source_coverage.SourceInterval(
                source="sessions/macbook", since_utc="2026-08-01T00:00:00Z",
                until_utc="2026-08-02T00:00:00Z", slice_id="slice-1",
                compatibility_version="source-debt/v1",
            )
            second = source_coverage.SourceInterval(
                source="sessions/macbook", since_utc="2026-08-02T00:00:00Z",
                until_utc="2026-08-03T00:00:00Z", slice_id="slice-2",
                compatibility_version="source-debt/v1",
            )
            store = source_coverage.SourceDebtStore()
            for interval in (first, second):
                store.record_failure(
                    interval, failure_class="offline", retryable=True,
                    resume_state_digest="sha256:" + "a" * 64,
                    attempted_at="2026-08-01T00:00:00Z",
                )
            coverage_path = Path(directory) / "state" / "source-coverage.json"
            source_coverage.write(coverage_path, store.document())
            environment["CLOCKIFY_AUTOPILOT_COVERAGE_STATE"] = str(coverage_path)
            completed = self.child_result(["review"], 0, stdout=str(result) + "\n", stderr="")

            timed_out = ChildResult(None, "", "child stderr suppressed", True, 0.01)
            with mock.patch.object(runner, "run_child_bounded", return_value=timed_out):
                self.assertEqual(runner.TEMPORARY_COVERAGE_EXIT, runner.run(environment))
            self.assertTrue((run_dir / "completion-bundle.json").is_file())

            with mock.patch.object(runner, "run_child_bounded", return_value=completed):
                self.assertEqual(0, runner.run(environment))

            resolved = source_coverage.SourceDebtStore.from_document(source_coverage.read(coverage_path))
            self.assertEqual((second.debt_id,), tuple(item.debt_id for item in resolved.active()))
            with mock.patch.object(runner, "run_child_bounded", return_value=completed):
                self.assertEqual(0, runner.run(environment))
            replayed = source_coverage.SourceDebtStore.from_document(source_coverage.read(coverage_path))
            self.assertEqual((second.debt_id,), tuple(item.debt_id for item in replayed.active()))
            self.assertEqual(
                1,
                sum(event["event"] == "complete" for event in replayed.document()["events"]),
            )

    def test_runner_recovers_after_backlog_completion_precedes_debt_persistence(self):
        """A crash after backlog persistence converges to one debt completion on rerun."""
        with tempfile.TemporaryDirectory() as directory:
            environment, result = self.fixture(directory, "review_delta")
            root = Path(directory)
            run_dir = result.parent
            slice_ = collector_slices.plan_slices(
                dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 8, 2, tzinfo=dt.timezone.utc),
                zone=review_run.clockify_sync_collect.BUCHAREST,
            )[0]
            identity = collector_slices.BacklogIdentity(
                since_utc="2026-08-01T00:00:00Z", until_utc="2026-08-02T00:00:00Z",
                timezone="Europe/Bucharest", max_days=2, compatibility_version="fixture/v1",
            )
            for relative in (
                "run-report.json", "evidence/evidence-ledger.json", "semantic-analysis.json",
                "work-accounting-result.json", "quality_report.json", "review-snapshot.json",
            ):
                path = run_dir / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps({"fixture": relative}) + "\n", encoding="utf-8")
            coverage = {"status": "complete", "incomplete_sources": []}
            (run_dir / "evidence" / "evidence-ledger.json").write_text(json.dumps({
                "manifest": {"source_completeness": coverage},
            }) + "\n", encoding="utf-8")
            (run_dir / "run-report.json").write_text(json.dumps({
                "runtime_identity": {"git_sha": "fixture"},
                "date_range": {
                    "since": "2026-08-01T00:00:00Z", "until": "2026-08-02T00:00:00Z",
                },
                "evidence_ledger": {"source_completeness": coverage},
            }) + "\n", encoding="utf-8")
            (run_dir / "slice-finalization.json").write_text(json.dumps({
                "schema_version": "collector-slice-finalization/v1",
                "backlog_identity": identity.document(),
                "slice_id": slice_.slice_id,
                "since_utc": "2026-08-01T00:00:00Z",
                "until_utc": "2026-08-02T00:00:00Z",
            }) + "\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {
                "CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT": str(root / "checkpoints"),
            }):
                bundle = review_run._finalize_backlog_completion(run_dir)
                backlog = collector_slices.BacklogStore(root / "checkpoints").open(
                    identity, (slice_,)
                )
            self.assertEqual(1, len(backlog.completed))
            result.write_text(json.dumps({
                "action": "review_delta", "run_id": "run-1", "run_dir": str(run_dir),
                "slice_id": slice_.slice_id,
                "date_range": {
                    "since": "2026-08-01T00:00:00Z", "until": "2026-08-02T00:00:00Z",
                },
                "source_completeness": coverage,
                "completion_bundle_digest": bundle.bundle_digest,
            }) + "\n", encoding="utf-8")
            interval = source_coverage.SourceInterval(
                source="sessions/macbook", since_utc="2026-08-01T00:00:00Z",
                until_utc="2026-08-02T00:00:00Z", slice_id=slice_.slice_id,
                compatibility_version="source-debt/v1",
            )
            debt_store = source_coverage.SourceDebtStore()
            debt_store.record_failure(
                interval, failure_class="offline", retryable=True,
                resume_state_digest="sha256:" + "a" * 64,
                attempted_at="2026-08-01T00:00:00Z",
            )
            coverage_path = root / "state" / "source-coverage.json"
            source_coverage.write(coverage_path, debt_store.document())
            environment["CLOCKIFY_AUTOPILOT_COVERAGE_STATE"] = str(coverage_path)
            completed = self.child_result(
                ["review"], 0, stdout=str(result) + "\n", stderr=""
            )

            with (
                mock.patch.object(runner, "run_child_bounded", return_value=completed),
                mock.patch.object(runner.source_coverage, "write", side_effect=RuntimeError("interrupt")),
            ):
                with self.assertRaisesRegex(RuntimeError, "interrupt"):
                    runner.run(environment)
            interrupted = source_coverage.SourceDebtStore.from_document(
                source_coverage.read(coverage_path)
            )
            self.assertEqual((interval.debt_id,), tuple(item.debt_id for item in interrupted.active()))

            with mock.patch.object(runner, "run_child_bounded", return_value=completed):
                self.assertEqual(0, runner.run(environment))
            recovered = source_coverage.SourceDebtStore.from_document(
                source_coverage.read(coverage_path)
            )
            self.assertEqual((), recovered.active())
            self.assertEqual(
                1,
                sum(event["event"] == "complete" for event in recovered.document()["events"]),
            )

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

    @staticmethod
    def record_active_debt(environment: dict[str, str]) -> source_coverage.SourceInterval:
        interval = source_coverage.SourceInterval(
            source="sessions/macbook",
            since_utc="2026-08-01T00:00:00Z",
            until_utc="2026-08-02T00:00:00Z",
            slice_id="slice-timeout",
            compatibility_version="source-debt/v1",
        )
        store = source_coverage.SourceDebtStore()
        store.record_failure(
            interval,
            failure_class="coverage_incomplete",
            retryable=True,
            resume_state_digest="sha256:" + "a" * 64,
            attempted_at="2026-08-02T00:00:00Z",
        )
        path = Path(environment["CLOCKIFY_AUTOPILOT_ROOT"]) / "state" / "source-coverage.json"
        source_coverage.write(path, store.document())
        environment["CLOCKIFY_AUTOPILOT_COVERAGE_STATE"] = str(path)
        return interval

    def test_routine_run_uses_slice_timeout_budget(self):
        """Catches debt-free runs accidentally receiving the backlog budget."""
        with tempfile.TemporaryDirectory() as directory:
            environment, result = self.fixture(directory, "review_delta")
            seen = []
            completed = self.child_result(["review"], 0, stdout=str(result) + "\n")
            with mock.patch.object(
                runner,
                "run_child_bounded",
                side_effect=lambda *args, **kwargs: seen.append(kwargs["timeout"]) or completed,
            ):
                self.assertEqual(0, runner.run(environment))

        self.assertEqual(2700, seen[0].total_seconds)
        self.assertEqual(30, seen[0].grace_seconds)

    def test_existing_coverage_debt_uses_total_timeout_budget(self):
        """Catches backlog retries being stopped at the routine slice budget."""
        with tempfile.TemporaryDirectory() as directory:
            environment, result = self.fixture(directory, "review_delta")
            self.record_active_debt(environment)
            seen = []
            completed = self.child_result(["review"], 0, stdout=str(result) + "\n")
            with mock.patch.object(
                runner,
                "run_child_bounded",
                side_effect=lambda *args, **kwargs: seen.append(kwargs["timeout"]) or completed,
            ):
                self.assertEqual(0, runner.run(environment))

        self.assertEqual(7200, seen[0].total_seconds)

    def test_invalid_timeout_configuration_blocks_before_child_spawn(self):
        """Catches zero or inconsistent budgets reaching the review child."""
        with tempfile.TemporaryDirectory() as directory:
            environment, _result = self.fixture(directory, "review_delta")
            environment["CLOCKIFY_AUTOPILOT_SLICE_TIMEOUT_SECONDS"] = "0"
            spawned = []
            with mock.patch.object(
                runner,
                "run_child_bounded",
                side_effect=lambda *args, **kwargs: spawned.append(True),
            ):
                self.assertEqual(2, runner.run(environment))

        self.assertEqual([], spawned)

    def test_timeout_releases_lock_and_retains_exact_debt_for_resume(self):
        """Catches timeout paths that retain the flock or fabricate a new interval."""
        with tempfile.TemporaryDirectory() as directory:
            environment, _result = self.fixture(directory, "review_delta")
            interval = self.record_active_debt(environment)
            timed_out = ChildResult(None, "", "child stderr suppressed", True, 0.01)
            with mock.patch.object(runner, "run_child_bounded", return_value=timed_out):
                self.assertEqual(runner.TEMPORARY_COVERAGE_EXIT, runner.run(environment))
                self.assertEqual(1, runner.run(environment))
            store = source_coverage.SourceDebtStore.from_document(
                source_coverage.read(Path(environment["CLOCKIFY_AUTOPILOT_COVERAGE_STATE"]))
            )

        self.assertEqual((interval.debt_id,), tuple(item.debt_id for item in store.active()))
        self.assertEqual(3, store.active()[0].retry_count)

    def test_timeout_status_omits_private_coverage_state_path(self):
        """Catches timeout receipts exposing the private coverage-state filename."""
        with tempfile.TemporaryDirectory() as directory:
            environment, _result = self.fixture(directory, "review_delta")
            self.record_active_debt(environment)
            timed_out = ChildResult(None, "", "child stderr suppressed", True, 0.01)
            with mock.patch.object(runner, "run_child_bounded", return_value=timed_out):
                self.assertEqual(runner.TEMPORARY_COVERAGE_EXIT, runner.run(environment))
            status = json.loads(Path(environment["CLOCKIFY_AUTOPILOT_STATUS"]).read_text())

        self.assertNotIn("coverage_state", status)
        self.assertNotIn("source-coverage.json", json.dumps(status))

    def test_non_contract_child_stdout_is_not_written_to_status(self):
        """Catches a failure status that copies an arbitrary child payload."""
        with tempfile.TemporaryDirectory() as directory:
            environment, _result = self.fixture(directory, "review_delta")
            malformed = ChildResult(1, "secret-stdout=abc123\n", "", False, 0.01)
            with mock.patch.object(runner, "run_child_bounded", return_value=malformed):
                self.assertEqual(2, runner.run(environment))
            status = json.loads(Path(environment["CLOCKIFY_AUTOPILOT_STATUS"]).read_text())

        self.assertNotIn("secret-stdout", json.dumps(status))

    def test_complete_run_records_compact_status(self):
        with tempfile.TemporaryDirectory() as directory:
            environment, result = self.fixture(directory, "review_delta")
            completed = self.child_result(
                ["review"], 0, stdout=str(result) + "\n", stderr=""
            )
            with mock.patch.object(runner, "run_child_bounded", return_value=completed):
                self.assertEqual(0, runner.run(environment))
            status = json.loads(Path(environment["CLOCKIFY_AUTOPILOT_STATUS"]).read_text())
        self.assertEqual("complete", status["state"])
        self.assertEqual("review_delta", status["action"])

    def test_completed_results_are_recorded_in_order_and_reportable(self):
        with tempfile.TemporaryDirectory() as directory:
            environment, first = self.fixture(
                directory,
                "review_delta",
                date_range={"since": "2026-08-01T00:00:00Z", "until": "2026-08-02T00:00:00Z"},
                source_completeness={"status": "complete", "incomplete_sources": []},
            )
            second = first.parent.parent / "run-2" / "autopilot-result.json"
            second.parent.mkdir()
            second.write_text(json.dumps({
                "action": "coverage_warning",
                "run_id": "run-2",
                "date_range": {"since": "2026-08-02T00:00:00Z", "until": "2026-08-03T00:00:00Z"},
                "source_completeness": {"status": "complete", "incomplete_sources": []},
            }) + "\n")
            completed = self.child_result(
                ["review"], 2, stdout=f"{first}\n{second}\n", stderr="later slice incomplete"
            )
            with mock.patch.object(runner, "run_child_bounded", return_value=completed):
                self.assertEqual(
                    runner.TEMPORARY_COVERAGE_EXIT,
                    runner.run(environment),
                )
            status = json.loads(Path(environment["CLOCKIFY_AUTOPILOT_STATUS"]).read_text())

            self.assertEqual([str(first), str(second)], status["results"])
            self.assertEqual(str(second), status["result"])
            self.assertTrue(status["coverage_debts"])
            self.assertEqual(0, runner.mark_reported(environment, first))
            self.assertEqual(0, runner.mark_reported(environment, second))
            self.assertEqual(2, runner.mark_reported(environment, second.with_name("other.json")))

    def test_first_incomplete_result_controls_multi_result_retry_state(self):
        with tempfile.TemporaryDirectory() as directory:
            environment, first = self.fixture(
                directory,
                "blocked",
                date_range={"since": "2026-08-01T00:00:00Z", "until": "2026-08-02T00:00:00Z"},
                source_completeness={
                    "status": "incomplete",
                    "incomplete_sources": ["sessions/omarchy-precision"],
                },
            )
            second = first.parent.parent / "run-2" / "autopilot-result.json"
            second.parent.mkdir()
            second.write_text(json.dumps({
                "action": "coverage_warning",
                "date_range": {"since": "2026-08-01T00:00:00Z", "until": "2026-08-02T00:00:00Z"},
                "source_completeness": {
                    "status": "incomplete",
                    "incomplete_sources": ["sessions/macbook"],
                },
            }) + "\n")
            completed = self.child_result(
                ["review"], 2, stdout=f"{first}\n{second}\n", stderr="collection incomplete"
            )
            with mock.patch.object(runner, "run_child_bounded", return_value=completed):
                self.assertEqual(2, runner.run(environment))
            status = json.loads(Path(environment["CLOCKIFY_AUTOPILOT_STATUS"]).read_text())

        self.assertEqual("blocked", status["state"])

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

    def test_command_passes_exact_reconciliation_inputs(self):
        """Catches scheduled runs omitting the mandatory period snapshots."""
        with tempfile.TemporaryDirectory() as directory:
            environment, _result = self.fixture(directory, "no_comment")
            environment.update({
                "CLOCKIFY_AUTOPILOT_PERIOD_MANIFEST": "/private/period-manifest.json",
                "CLOCKIFY_AUTOPILOT_ROUTING": "/private/routing.json",
                "CLOCKIFY_AUTOPILOT_ACCEPTANCE_LEDGER": "/private/review-acceptance.jsonl",
            })
            command = runner._command(environment, Path(directory))

        self.assertIn("--period-manifest", command)
        self.assertIn("--routing", command)
        self.assertIn("--acceptance-ledger", command)
        self.assertEqual(
            "/private/period-manifest.json",
            command[command.index("--period-manifest") + 1],
        )
        self.assertEqual("/private/routing.json", command[command.index("--routing") + 1])
        self.assertEqual(
            "/private/review-acceptance.jsonl",
            command[command.index("--acceptance-ledger") + 1],
        )

    def test_coverage_warning_retries_twice_then_stops_looping(self):
        with tempfile.TemporaryDirectory() as directory:
            environment, result = self.fixture(
                directory,
                "coverage_warning",
                date_range={"since": "2026-08-01T00:00:00Z", "until": "2026-08-02T00:00:00Z"},
            )
            completed = self.child_result(
                ["review"], 0, stdout=str(result) + "\n", stderr=""
            )
            with mock.patch.object(runner, "run_child_bounded", return_value=completed):
                self.assertEqual(runner.TEMPORARY_COVERAGE_EXIT, runner.run(environment))
                self.assertEqual(runner.TEMPORARY_COVERAGE_EXIT, runner.run(environment))
                self.assertEqual(0, runner.run(environment))
            status = json.loads(Path(environment["CLOCKIFY_AUTOPILOT_STATUS"]).read_text())
        self.assertEqual("coverage_exhausted", status["state"])
        self.assertEqual(3, status["coverage_retry_attempts"])

    def test_retry_counts_are_independent_for_exact_source_debts(self):
        """Catches runner state that increments one counter for unrelated sources."""
        with tempfile.TemporaryDirectory() as directory:
            environment, result = self.fixture(
                directory,
                "coverage_warning",
                date_range={"since": "2026-08-01T00:00:00Z", "until": "2026-08-03T00:00:00Z"},
                source_completeness={
                    "status": "incomplete",
                    "incomplete_sources": ["sessions/macbook"],
                },
            )
            completed = self.child_result(
                ["review"], 0, stdout=str(result) + "\n", stderr=""
            )
            with mock.patch.object(runner, "run_child_bounded", return_value=completed):
                self.assertEqual(runner.TEMPORARY_COVERAGE_EXIT, runner.run(environment))
                result.write_text(json.dumps({
                    "action": "coverage_warning",
                    "date_range": {"since": "2026-08-01T00:00:00Z", "until": "2026-08-03T00:00:00Z"},
                    "source_completeness": {
                        "status": "incomplete",
                        "incomplete_sources": ["repositories/omarchy-desktop"],
                    },
                }) + "\n")
                self.assertEqual(runner.TEMPORARY_COVERAGE_EXIT, runner.run(environment))
            status = json.loads(Path(environment["CLOCKIFY_AUTOPILOT_STATUS"]).read_text())

        retries = {item["source"]: item["retry_count"] for item in status["coverage_debts"]}
        self.assertEqual({"sessions/macbook": 1, "repositories/omarchy-desktop": 1}, retries)

    def test_missing_utc_bounds_block_without_fabricating_epoch_debt(self):
        """Catches fallback interval construction that schedules a fabricated 1970 debt."""
        with tempfile.TemporaryDirectory() as directory:
            environment, result = self.fixture(
                directory,
                "coverage_warning",
                source_completeness={
                    "status": "incomplete",
                    "incomplete_sources": ["sessions/macbook"],
                },
            )
            completed = self.child_result(
                ["review"], 0, stdout=str(result) + "\n", stderr=""
            )
            with mock.patch.object(runner, "run_child_bounded", return_value=completed):
                self.assertEqual(2, runner.run(environment))
            status = json.loads(Path(environment["CLOCKIFY_AUTOPILOT_STATUS"]).read_text())

        self.assertEqual("blocked", status["state"])
        self.assertIn("UTC", status["reason"])
        self.assertFalse(Path(environment["CLOCKIFY_AUTOPILOT_ROOT"], "state", "source-coverage.json").exists())

    def test_invalid_exact_interval_contract_blocks_without_writing_debt(self):
        """Catches malformed UTC bounds escaping the runner as an uncaught exception."""
        with tempfile.TemporaryDirectory() as directory:
            environment, result = self.fixture(
                directory,
                "coverage_warning",
                date_range={"since": "not-a-timestampZ", "until": "2026-08-03T00:00:00Z"},
                source_completeness={
                    "status": "incomplete",
                    "incomplete_sources": ["sessions/macbook"],
                },
            )
            completed = self.child_result(
                ["review"], 0, stdout=str(result) + "\n", stderr=""
            )
            with mock.patch.object(runner, "run_child_bounded", return_value=completed):
                self.assertEqual(2, runner.run(environment))
            status = json.loads(Path(environment["CLOCKIFY_AUTOPILOT_STATUS"]).read_text())

        self.assertEqual("blocked", status["state"])
        self.assertIn("source interval", status["reason"])

    def test_next_scheduled_run_retries_debt_after_prior_retry_exhaustion(self):
        with tempfile.TemporaryDirectory() as directory:
            environment, result = self.fixture(
                directory,
                "coverage_warning",
                date_range={"since": "2026-08-01T00:00:00Z", "until": "2026-08-02T00:00:00Z"},
            )
            completed = self.child_result(
                ["review"], 0, stdout=str(result) + "\n", stderr=""
            )
            with mock.patch.object(runner, "run_child_bounded", return_value=completed):
                runner.run(environment)
                runner.run(environment)
                self.assertEqual(0, runner.run(environment))
                self.assertEqual(runner.TEMPORARY_COVERAGE_EXIT, runner.run(environment))
            status = json.loads(Path(environment["CLOCKIFY_AUTOPILOT_STATUS"]).read_text())
        self.assertEqual("retry_scheduled", status["state"])
        self.assertEqual(1, status["coverage_retry_attempts"])

    def test_first_deploy_bootstraps_prior_missed_interval_into_command(self):
        with tempfile.TemporaryDirectory() as directory:
            environment, result = self.fixture(
                directory,
                "coverage_warning",
                date_range={"since": "2026-08-12T00:00:00Z", "until": "2026-08-13T00:00:00Z"},
            )
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
            completed = self.child_result(
                ["review"], 0, stdout=str(result) + "\n", stderr=""
            )
            with mock.patch.object(
                runner, "run_child_bounded", return_value=completed
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
                date_range={"since": "2026-08-01T00:00:00Z", "until": "2026-08-02T00:00:00Z"},
                source_completeness={
                    "status": "incomplete",
                    "incomplete_sources": [
                        "sessions/macbook",
                        "repositories/omarchy-desktop",
                    ],
                },
            )
            environment["CLOCKIFY_AUTOPILOT_COORDINATOR"] = "omarchy-precision"
            completed = self.child_result(
                ["review"], 2, stdout=str(result) + "\n", stderr="coverage incomplete"
            )
            with mock.patch.object(runner, "run_child_bounded", return_value=completed):
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
            completed = self.child_result(
                ["review"], 2, stdout=str(result) + "\n", stderr="coverage incomplete"
            )
            with mock.patch.object(runner, "run_child_bounded", return_value=completed):
                self.assertEqual(2, runner.run(environment))
            status = json.loads(Path(environment["CLOCKIFY_AUTOPILOT_STATUS"]).read_text())
        self.assertEqual("blocked", status["state"])
        self.assertEqual(0, status["coverage_retry_attempts"])

    def test_mark_reported_accepts_only_current_result(self):
        with tempfile.TemporaryDirectory() as directory:
            environment, result = self.fixture(directory, "review_delta")
            completed = self.child_result(
                ["review"], 0, stdout=str(result) + "\n", stderr=""
            )
            with mock.patch.object(runner, "run_child_bounded", return_value=completed):
                self.assertEqual(0, runner.run(environment))
            self.assertEqual(0, runner.mark_reported(environment, result))
            status = json.loads(Path(environment["CLOCKIFY_AUTOPILOT_STATUS"]).read_text())
            self.assertIn("reported_at", status)
            self.assertEqual(
                2, runner.mark_reported(environment, result.with_name("other.json"))
            )


if __name__ == "__main__":
    unittest.main()
