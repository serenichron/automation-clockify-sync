from __future__ import annotations

import configparser
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path, PurePosixPath
import shlex
import sys
import tempfile
import unittest
from unittest import mock
from zoneinfo import ZoneInfo

from scripts import clockify_review_cycle as cycle
from scripts import clockify_review_run as review_run
from scripts.autopilot_process import ChildResult


TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))
from test_review_cycle_delivery import make_run, write_json  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
CONFIG_EXAMPLE = ROOT / "ops/systemd/clockify-review-cycle.config.example.json"
SERVICE = ROOT / "ops/systemd/clockify-review-cycle.service"


class ReviewCycleArtifactContractTests(unittest.TestCase):
    def test_example_is_complete_bounded_and_credential_free(self) -> None:
        """Catches an incomplete, unbounded, private, or credential-bearing example."""
        document = json.loads(CONFIG_EXAMPLE.read_text(encoding="utf-8"))

        self.assertEqual(
            {
                "root",
                "state_dir",
                "cache",
                "routing",
                "corrections",
                "acceptance",
                "workspace_id",
                "member_id",
                "recovery_since",
                "timezone",
                "spreadsheet_id",
                "monthly_sheet_title_template",
                "calendly_optional",
                "max_slices",
                "total_child_budget_seconds",
            },
            set(document),
        )
        self.assertEqual(1, document["max_slices"])
        self.assertEqual(7200, document["total_child_budget_seconds"])
        self.assertIs(document["calendly_optional"], True)
        self.assertEqual(
            "{month_name} {year} portfolio review",
            document["monthly_sheet_title_template"],
        )
        ZoneInfo(document["timezone"])
        for key in ("root", "state_dir", "cache", "routing", "corrections", "acceptance"):
            self.assertTrue(PurePosixPath(document[key]).is_absolute(), key)
            self.assertTrue(document[key].startswith("/srv/serenichron/"), key)
        serialized = json.dumps(document).casefold()
        self.assertNotIn("/home/", serialized)
        self.assertNotIn("blackthorne", serialized)
        self.assertFalse(
            {"password", "secret", "token", "api_key", "credential"} & {
                key.casefold() for key in document
            }
        )

    def test_service_parses_to_the_explicit_oneshot_capability_boundary(self) -> None:
        """Catches implicit write authority, legacy execution, or automatic restart."""
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        parser.optionxform = str
        parser.read_string(SERVICE.read_text(encoding="utf-8"))
        unit = parser["Unit"]
        service = parser["Service"]

        self.assertFalse(
            any(key.startswith(("Condition", "Assert")) for key in unit),
            "a pre-exec systemd gate would mask the CLI's exit-2 config failure",
        )
        self.assertNotIn("Install", parser)
        self.assertEqual("oneshot", service["Type"])
        self.assertEqual("clockify-review-cycle", service["User"])
        self.assertEqual("clockify-review-cycle", service["Group"])
        self.assertEqual(
            "/etc/serenichron/clockify-review-cycle.env",
            service["EnvironmentFile"],
        )
        self.assertEqual(
            {
                "PYTHONUNBUFFERED=1",
                "GOOGLE_WORKSPACE_CLI_CONFIG_DIR=/var/lib/serenichron/clockify-review-cycle/gws",
            },
            set(shlex.split(service["Environment"])),
        )
        self.assertEqual("no", service["Restart"])
        self.assertEqual("0077", service["UMask"])
        self.assertEqual("true", service["NoNewPrivileges"])
        self.assertEqual("control-group", service["KillMode"])
        self.assertEqual("journal", service["StandardOutput"])
        self.assertEqual("journal", service["StandardError"])
        self.assertEqual(
            [
                "/usr/bin/python3",
                "scripts/clockify_review_cycle.py",
                "--config",
                "/etc/serenichron/clockify-review-cycle.json",
                "--enable-sheet-write",
            ],
            shlex.split(service["ExecStart"]),
        )
        self.assertEqual(
            {
                "/srv/serenichron/clockify-review-cycle/runs",
                "/srv/serenichron/clockify-review-cycle/state",
                "/var/lib/serenichron/clockify-review-cycle/gws",
            },
            set(shlex.split(service["ReadWritePaths"])),
        )
        self.assertFalse((SERVICE.parent / "clockify-review-cycle.timer").exists())


class ReviewCycleEntrypointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.state_dir = self.root / "state"
        self.cache = self.state_dir / "analyzer-cache.jsonl"
        for filename, value in (
            ("routing.json", {"workspace_id": "workspace-1", "member_id": "member-1"}),
            ("corrections.jsonl", {}),
            ("acceptance.jsonl", {}),
        ):
            write_json(self.root / filename, value)
        self.config = {
            "root": str(self.root),
            "state_dir": str(self.state_dir),
            "cache": str(self.cache),
            "routing": str(self.root / "routing.json"),
            "corrections": str(self.root / "corrections.jsonl"),
            "acceptance": str(self.root / "acceptance.jsonl"),
            "workspace_id": "workspace-1",
            "member_id": "member-1",
            "recovery_since": "2026-09-07",
            "catchup_until": "2026-09-09",
            "timezone": "Europe/Bucharest",
            "spreadsheet_id": "sheet-1",
            "monthly_sheet_title_template": "{month_name} {year} portfolio review",
            "calendly_optional": True,
            "max_slices": 1,
            "total_child_budget_seconds": 7200,
        }
        runs_patch = mock.patch.object(review_run, "RUNS", self.root / "runs")
        runs_patch.start()
        self.addCleanup(runs_patch.stop)

    def write_config(self, value: object | None = None, *, name: str = "cycle.json") -> Path:
        path = self.root / name
        write_json(path, self.config if value is None else value)
        return path

    def call_main(self, *, enable_write: bool = False, config: Path | None = None):
        stdout = StringIO()
        stderr = StringIO()
        argv = ["--config", str(config or self.write_config())]
        if enable_write:
            argv.append("--enable-sheet-write")
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cycle.main(argv)
        lines = stdout.getvalue().splitlines()
        result = json.loads(lines[-1]) if lines else None
        return code, result, stderr.getvalue()

    def child_for_runs(
        self,
        commands: list[list[str]],
        *,
        coverage: dict[str, object] | None = None,
        replay_code: int = 0,
        publish_codes: list[int] | None = None,
        ambiguous: list[dict[str, object]] | None = None,
    ):
        remaining_publish_codes = list(publish_codes or [0])

        def child(command, **_kwargs):
            command = list(command)
            commands.append(command)
            if "--replay-from" in command:
                if replay_code:
                    return ChildResult(replay_code, "", "", False, 0.1)
                path = make_run(
                    self.root,
                    "replay-run",
                    replay=True,
                    snapshots_from=self.root / "runs" / "source-run",
                    coverage=coverage,
                    ambiguous=ambiguous,
                )
                return ChildResult(0, str(path) + "\n", "", False, 0.1)
            if Path(command[1]).name == "clockify_sheet_publish.py":
                return ChildResult(remaining_publish_codes.pop(0), "", "", False, 0.1)
            path = make_run(
                self.root,
                "source-run",
                replay=False,
                coverage=coverage,
                ambiguous=ambiguous,
            )
            return ChildResult(0, str(path) + "\n", "", False, 0.1)

        return child

    def test_invalid_configs_exit_two_without_spawning_a_child(self) -> None:
        """Catches configuration failures escaping the stable supervisor status."""
        missing_file = self.root / "missing.json"
        malformed = self.root / "malformed.json"
        malformed.write_text("{", encoding="utf-8")
        missing_field = {key: value for key, value in self.config.items() if key != "routing"}
        invalid_timezone = {**self.config, "timezone": "Not/AZone"}
        relative_root = {**self.config, "root": "relative/root"}
        cases = (
            missing_file,
            malformed,
            self.write_config(missing_field, name="missing-field.json"),
            self.write_config(invalid_timezone, name="invalid-timezone.json"),
            self.write_config(relative_root, name="relative-root.json"),
        )

        for number, path in enumerate(cases):
            with self.subTest(number=number):
                with mock.patch.object(cycle, "run_child_bounded") as child:
                    code, result, error = self.call_main(enable_write=True, config=path)
                self.assertEqual(2, code)
                self.assertIsNone(result)
                self.assertIn("blocked", error)
                child.assert_not_called()

    def test_unknown_result_status_prints_json_and_fails_closed(self) -> None:
        """Catches a future coordinator status being silently treated as success."""
        config_path = self.write_config()
        with mock.patch.object(
            cycle,
            "run_cycle",
            return_value={"status": "future-status", "slices": []},
        ):
            code, result, error = self.call_main(
                enable_write=True, config=config_path
            )

        self.assertEqual(2, code)
        self.assertEqual({"status": "future-status", "slices": []}, result)
        self.assertIn("unsupported result status", error)

    def test_plan_locked_delivered_exception_and_idle_are_successes(self) -> None:
        """Catches supervisor failure for successful/idle modes or plan side effects."""
        config_path = self.write_config()
        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=AssertionError("plan spawned child")
        ):
            code, result, _error = self.call_main(config=config_path)
        self.assertEqual(0, code)
        self.assertEqual("plan", result["status"])
        self.assertFalse((self.state_dir / "review-cycle-state.json").exists())
        self.assertFalse((self.state_dir / "source-coverage.json").exists())

        with cycle.single_instance(self.state_dir / "review-cycle.lock") as acquired:
            self.assertTrue(acquired)
            with mock.patch.object(
                cycle, "run_child_bounded", side_effect=AssertionError("lock spawned child")
            ):
                code, result, _error = self.call_main(
                    enable_write=True, config=config_path
                )
        self.assertEqual(0, code)
        self.assertEqual("locked", result["status"])

        commands: list[list[str]] = []
        with mock.patch.object(
            cycle,
            "run_child_bounded",
            side_effect=self.child_for_runs(commands, ambiguous=[{"id": "exception-1"}]),
        ):
            code, result, _error = self.call_main(enable_write=True, config=config_path)
            idle_code, idle, _error = self.call_main(enable_write=True, config=config_path)
        self.assertEqual(0, code)
        self.assertEqual("delivered_with_exceptions", result["status"])
        self.assertEqual(0, idle_code)
        self.assertEqual("idle", idle["status"])
        self.assertEqual(3, len(commands))
        self.assertEqual(
            {"clockify_review_run.py", "clockify_sheet_publish.py"},
            {Path(command[1]).name for command in commands},
        )

    def test_incomplete_failed_and_recovery_blocked_exit_seventy_five(self) -> None:
        """Catches logical non-delivery being hidden behind process success."""
        with self.subTest(status="incomplete"):
            config = {**self.config, "total_child_budget_seconds": 30}
            path = self.write_config(config)
            with mock.patch.object(cycle, "run_child_bounded") as child:
                code, result, _error = self.call_main(enable_write=True, config=path)
            self.assertEqual(75, code)
            self.assertEqual("incomplete", result["status"])
            self.assertEqual("total_child_budget_exhausted", result["reason"])
            child.assert_not_called()

        for status, kwargs in (
            ("failed", {"replay_code": 9}),
            (
                "recovery_blocked",
                {"coverage": {"status": "incomplete", "incomplete_sources": ["clockify"]}},
            ),
        ):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                nested = ReviewCycleEntrypointTests(methodName="runTest")
                nested.temporary = None
                nested.root = Path(directory)
                nested.state_dir = nested.root / "state"
                nested.cache = nested.state_dir / "analyzer-cache.jsonl"
                for filename, value in (
                    ("routing.json", {"workspace_id": "workspace-1", "member_id": "member-1"}),
                    ("corrections.jsonl", {}),
                    ("acceptance.jsonl", {}),
                ):
                    write_json(nested.root / filename, value)
                nested.config = {
                    **self.config,
                    "root": str(nested.root),
                    "state_dir": str(nested.state_dir),
                    "cache": str(nested.cache),
                    "routing": str(nested.root / "routing.json"),
                    "corrections": str(nested.root / "corrections.jsonl"),
                    "acceptance": str(nested.root / "acceptance.jsonl"),
                }
                commands: list[list[str]] = []
                with mock.patch.object(review_run, "RUNS", nested.root / "runs"), mock.patch.object(
                    cycle,
                    "run_child_bounded",
                    side_effect=nested.child_for_runs(commands, **kwargs),
                ):
                    code, result, _error = nested.call_main(enable_write=True)
                self.assertEqual(75, code)
                self.assertEqual(status, result["status"])

    def test_shared_budget_exhaustion_is_visible_before_replay(self) -> None:
        """Catches each child incorrectly receiving a fresh 100-second budget."""
        path = self.write_config({**self.config, "total_child_budget_seconds": 100})
        commands: list[list[str]] = []
        timeouts: list[int] = []
        ordinary = self.child_for_runs(commands)

        def consuming_child(command, *, timeout, **kwargs):
            timeouts.append(timeout.total_seconds)
            result = ordinary(command, timeout=timeout, **kwargs)
            return ChildResult(
                result.returncode,
                result.stdout,
                result.stderr,
                result.timed_out,
                70.0,
            )

        with mock.patch.object(cycle, "run_child_bounded", side_effect=consuming_child):
            code, result, _error = self.call_main(enable_write=True, config=path)
        self.assertEqual(75, code)
        self.assertEqual("incomplete", result["status"])
        self.assertEqual("total_child_budget_exhausted", result["reason"])
        self.assertEqual([100], timeouts)
        self.assertEqual(1, len(commands))

    def test_publisher_failure_retries_only_verified_publication(self) -> None:
        """Catches retry recollecting/replaying or losing failure visibility."""
        path = self.write_config()
        commands: list[list[str]] = []
        child = self.child_for_runs(commands, publish_codes=[9, 0])
        with mock.patch.object(cycle, "run_child_bounded", side_effect=child):
            first_code, first, _error = self.call_main(enable_write=True, config=path)
            before_retry = len(commands)
            second_code, second, _error = self.call_main(enable_write=True, config=path)

        self.assertEqual(75, first_code)
        self.assertEqual("failed", first["status"])
        self.assertEqual(0, second_code)
        self.assertEqual("delivered", second["status"])
        self.assertEqual(
            ["clockify_sheet_publish.py"],
            [Path(command[1]).name for command in commands[before_retry:]],
        )
        self.assertEqual(
            [
                "clockify_review_run.py",
                "clockify_review_run.py",
                "clockify_sheet_publish.py",
                "clockify_sheet_publish.py",
            ],
            [Path(command[1]).name for command in commands],
        )


if __name__ == "__main__":
    unittest.main()
