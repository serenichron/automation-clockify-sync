"""Release switches must not strand started recoveries, re-hash archives, or need a human."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from scripts import clockify_review_cycle as cycle
from scripts import clockify_source_debt_recover as recovery
from scripts import source_coverage
from test_historical_release_runtime import archive_release, release_tool

OLD_SHA = "a" * 40
NEW_SHA = "b" * 40
HELPER = Path(release_tool.__file__).read_bytes()


def _releases(test: unittest.TestCase) -> Path:
    temporary = tempfile.TemporaryDirectory()
    test.addCleanup(temporary.cleanup)
    releases = Path(temporary.name).resolve() / "releases"
    releases.mkdir(mode=0o700)
    test.addCleanup(lambda: [p.chmod(0o700) for p in releases.rglob("*") if p.is_dir()])
    return releases


class StartedRecoveryAttemptReleaseSwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.releases = _releases(self)
        self.old = archive_release(self.releases, OLD_SHA, {})
        self.new = archive_release(self.releases, NEW_SHA, {})
        work = self.releases.parent / "work"
        self.config = {
            "root": str(self.new),
            "runs_dir": str(work / "runs"),
            "state_dir": str(work / "state"),
            "cache": str(work / "cache"),
        }
        interval = source_coverage.SourceInterval(
            source="peer/macbook",
            since_utc="2026-10-21T21:00:00Z",
            until_utc="2026-10-23T21:00:00Z",
            slice_id="slice-release-switch",
            compatibility_version="collector-evidence-compatibility/v2:fixture",
        )
        self.store = source_coverage.SourceDebtStore()
        self.debt = self.store.record_failure(
            interval, failure_class="peer_unavailable", retryable=True,
            resume_state_digest="sha256:" + "d" * 64,
            attempted_at="2026-10-24T00:00:00Z",
        )
        self.parent = {
            "run_dir": str(work / "runs" / "parent"),
            "bundle_digest": "sha256:" + "b" * 64,
            "snapshot_digests": {"routing.json": "sha256:" + "c" * 64},
        }
        self.attempt_id = cycle._attempt_id(self.debt.debt_id, 3)

    def command(self, config) -> list[str]:
        return cycle._recovery_command(
            config, Path(self.parent["run_dir"]), self.debt.interval.source,
            self.attempt_id, self.parent["snapshot_digests"]["routing.json"],
        )

    def started(self, command: list[str]) -> dict:
        return {
            "schema_version": cycle.RECOVERY_ATTEMPT_SCHEMA_VERSION,
            "debt_id": self.debt.debt_id,
            "attempt_ordinal": 3,
            "attempt_id": self.attempt_id,
            "parent_run_dir": self.parent["run_dir"],
            "parent_bundle_digest": self.parent["bundle_digest"],
            "command_digest": cycle._value_digest(command),
            "phase": "started",
        }

    def test_started_attempt_from_previous_release_is_rebound_to_current(self) -> None:
        stored = self.started(self.command({**self.config, "root": str(self.old)}))
        record = {"recovery_attempts": {self.debt.debt_id: dict(stored)}}

        checked, command = cycle._recovery_attempt(record, self.debt, self.parent, self.config)

        current = self.command(self.config)
        self.assertEqual(current, command)
        self.assertEqual(str(self.new / "scripts/clockify_review_run.py"), command[1])
        rebound = {**stored, "command_digest": cycle._value_digest(current)}
        self.assertEqual(rebound, checked)
        self.assertEqual(rebound, record["recovery_attempts"][self.debt.debt_id])

    def test_rebind_requires_a_sha_named_sibling_release(self) -> None:
        stray = self.releases / "scratch"
        stray.mkdir()
        stored = self.started(self.command({**self.config, "root": str(stray)}))
        record = {"recovery_attempts": {self.debt.debt_id: dict(stored)}}

        with self.assertRaisesRegex(cycle.CycleError, "command has drifted"):
            cycle._recovery_attempt(record, self.debt, self.parent, self.config)
        self.assertEqual(stored, record["recovery_attempts"][self.debt.debt_id])

    def test_rebind_rejects_a_symlinked_release(self) -> None:
        alias = self.releases / ("c" * 40)
        alias.symlink_to(self.old, target_is_directory=True)
        stored = self.started(self.command({**self.config, "root": str(alias)}))
        record = {"recovery_attempts": {self.debt.debt_id: dict(stored)}}

        with self.assertRaisesRegex(cycle.CycleError, "command has drifted"):
            cycle._recovery_attempt(record, self.debt, self.parent, self.config)

    def test_rebind_requires_an_authenticated_release(self) -> None:
        unsealed = self.releases / ("e" * 40)
        (unsealed / "scripts").mkdir(parents=True)
        (unsealed / "routing.json").write_text("{}")
        stored = self.started(self.command({**self.config, "root": str(unsealed)}))
        record = {"recovery_attempts": {self.debt.debt_id: dict(stored)}}

        with self.assertRaisesRegex(cycle.CycleError, "command has drifted"):
            cycle._recovery_attempt(record, self.debt, self.parent, self.config)
        self.assertEqual(stored, record["recovery_attempts"][self.debt.debt_id])

    def test_startup_reconciliation_admits_a_started_attempt_from_previous_release(self) -> None:
        stored = self.started(self.command({**self.config, "root": str(self.old)}))
        state = {"slices": {"2026-10-22": {
            "until": "2026-10-24",
            "recovery_attempts": {self.debt.debt_id: dict(stored)},
        }}}
        work = Path(self.config["state_dir"])
        with mock.patch.object(cycle, "_recovery_parent", return_value=self.parent), \
                mock.patch.object(cycle, "_apply_verified_recovery") as applied:
            cycle._reconcile_verified_attempts(
                self.config, state, work / "review-cycle-state.json",
                self.store, work / "source-coverage.json",
            )
        applied.assert_not_called()

    def test_startup_reconciliation_still_rejects_an_unknown_command(self) -> None:
        stored = self.started(self.command({
            **self.config, "root": str(self.old), "cache": str(self.releases.parent / "x"),
        }))
        state = {"slices": {"2026-10-22": {
            "until": "2026-10-24",
            "recovery_attempts": {self.debt.debt_id: dict(stored)},
        }}}
        work = Path(self.config["state_dir"])
        with mock.patch.object(cycle, "_recovery_parent", return_value=self.parent), \
                self.assertRaisesRegex(cycle.CycleError, "command has drifted"):
            cycle._reconcile_verified_attempts(
                self.config, state, work / "review-cycle-state.json",
                self.store, work / "source-coverage.json",
            )

    def test_rebind_never_admits_a_difference_outside_the_release_root(self) -> None:
        stored = self.started(self.command({
            **self.config, "root": str(self.old),
            "state_dir": str(self.releases.parent / "other-state"),
        }))
        record = {"recovery_attempts": {self.debt.debt_id: dict(stored)}}

        with self.assertRaisesRegex(cycle.CycleError, "command has drifted"):
            cycle._recovery_attempt(record, self.debt, self.parent, self.config)
        self.assertEqual(stored, record["recovery_attempts"][self.debt.debt_id])


class ArchivedReleaseVouchingCacheTests(unittest.TestCase):
    FILES = {
        "scripts/clockify_source_debt_recover.py": b"historical adapter",
        "scripts/clockify_sync_collect.py": b"historical collector",
        "fleet.json": b"{}",
        "ops/systemd/user/clockify_review_cycle_release.py": HELPER,
    }

    def setUp(self) -> None:
        self.releases = _releases(self)
        self.old = archive_release(self.releases, OLD_SHA, self.FILES)
        self.current = archive_release(self.releases, NEW_SHA, {
            **self.FILES, "scripts/clockify_source_debt_recover.py": b"current adapter",
        })
        for name in dir(recovery):
            function = getattr(recovery, name)
            if callable(getattr(function, "cache_clear", None)):
                function.cache_clear()
                self.addCleanup(function.cache_clear)
        patch = mock.patch.object(recovery, "ROOT", self.current)
        patch.start()
        self.addCleanup(patch.stop)
        self.transition = self.transition_for(self.old)

    @staticmethod
    def transition_for(release: Path) -> dict:
        runtime = {
            "collector_path": str(release / "scripts" / "clockify_sync_collect.py"),
            "canonical_root": str(release),
            "git_sha": None,
            "git_dirty": None,
        }
        digest = recovery._file_digest
        return {
            "recovery_adapter_sha256": digest(release / "scripts/clockify_source_debt_recover.py", label="a"),
            "current_collector_sha256": digest(release / "scripts/clockify_sync_collect.py", label="c"),
            "current_routing_sha256": digest(release / "routing.json", label="r"),
            "current_fleet_sha256": digest(release / "fleet.json", label="f"),
            "current_runtime_identity_digest": recovery._digest(runtime),
        }

    def test_only_the_matching_release_is_fully_rehashed(self) -> None:
        other = archive_release(self.releases, "c" * 40, {
            **self.FILES,
            "scripts/clockify_source_debt_recover.py": b"other adapter",
            "docs/large.bin": b"x" * 4096,
        })
        large = other / "docs" / "large.bin"
        reads = []
        original = Path.read_bytes

        def counting(path: Path) -> bytes:
            reads.append(path)
            return original(path)

        with mock.patch.object(Path, "read_bytes", counting):
            self.assertTrue(recovery._historical_release_vouches(self.transition))

        self.assertNotIn(large, reads)
        self.assertIn(self.old / "scripts/clockify_sync_collect.py", reads)

    def test_each_decision_reauthenticates_the_matching_release(self) -> None:
        self.assertTrue(recovery._historical_release_vouches(self.transition))
        self.old.chmod(0o755)
        (self.old / "scripts").chmod(0o755)
        (self.old / "scripts" / "planted.py").write_text("print('planted')\n")

        self.assertFalse(recovery._historical_release_vouches(self.transition))

    def test_release_listing_stays_fresh(self) -> None:
        self.assertTrue(recovery._historical_release_vouches(self.transition))
        later = archive_release(self.releases, "c" * 40, {
            **self.FILES, "scripts/clockify_source_debt_recover.py": b"later adapter",
        })
        self.assertTrue(recovery._historical_release_vouches(self.transition_for(later)))
        for path in [self.old, *self.old.rglob("*")]:
            if path.is_dir():
                path.chmod(0o700)
        shutil.rmtree(self.old)
        self.assertFalse(recovery._historical_release_vouches(self.transition))


def _git(cwd: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
         "-c", "commit.gpgsign=false", "-C", str(cwd), *arguments],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


class ReleaseUpdateTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        self.base = base
        self.releases = base / "releases"
        self.releases.mkdir(mode=0o700)
        self.addCleanup(lambda: [p.chmod(0o700) for p in self.releases.rglob("*") if p.is_dir()])
        self.origin = base / "origin.git"
        _git(base, "init", "--quiet", "--bare", "--initial-branch=master", str(self.origin))
        self.work = base / "work"
        _git(base, "clone", "--quiet", str(self.origin), str(self.work))
        (self.work / "scripts").mkdir()
        (self.work / "routing.json").write_text("{}\n")
        (self.work / "scripts/clockify_review_cycle.py").write_text("# v1\n")
        self.first = self.commit("v1")
        self.source = base / "source"
        _git(base, "clone", "--quiet", str(self.origin), str(self.source))
        self.config_dir = base / "config"
        self.config_dir.mkdir(mode=0o700)
        self.state = base / "state"
        self.state.mkdir(mode=0o700)
        self.record = self.state / "release-updates.json"
        self.current = release_tool.materialize(self.source, self.releases, self.first)
        self.config = self.write_config(self.first, {
            "root": str(self.current),
            "routing": str(self.current / "routing.json"),
            "state_dir": str(self.state),
        })
        self.override = self.config_dir / "override.env"
        release_tool.activate(self.current, self.first, self.config, self.override)
        self.environment = self.private(self.config_dir / "cycle.env", "")
        self.credentials = self.private(self.config_dir / "inference.env", "")
        self.gws = base / "gws"
        self.gws.mkdir(mode=0o700)
        verify = mock.patch.object(release_tool, "verify_runtime", return_value={})
        self.verify = verify.start()
        self.addCleanup(verify.stop)

    def private(self, path: Path, text: str) -> Path:
        path.write_text(text)
        path.chmod(0o600)
        return path

    def write_config(self, sha: str, document: dict) -> Path:
        return self.private(
            self.config_dir / f"clockify-review-cycle.{sha}.json",
            json.dumps(document, indent=2) + "\n",
        )

    def commit(self, message: str) -> str:
        (self.work / "scripts/clockify_review_cycle.py").write_text(f"# {message}\n")
        _git(self.work, "add", "-A")
        _git(self.work, "commit", "--quiet", "-m", message)
        _git(self.work, "push", "--quiet", "origin", "HEAD:master")
        return _git(self.work, "rev-parse", "HEAD")

    def update(self) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = release_tool.main([
                "update",
                "--source-repository", str(self.source),
                "--remote", "origin", "--branch", "master",
                "--releases-root", str(self.releases),
                "--override", str(self.override),
                "--environment", str(self.environment),
                "--credential-environment", str(self.credentials),
                "--gws", str(self.gws),
                "--record", str(self.record),
            ])
        return code, out.getvalue(), err.getvalue()

    def active(self) -> str:
        return self.override.read_text()

    def test_current_master_is_a_no_op(self) -> None:
        before = self.active()

        code, out, _err = self.update()

        self.assertEqual(0, code)
        self.assertIn(self.first, out)
        self.assertEqual(before, self.active())
        self.assertEqual([self.first], sorted(p.name for p in self.releases.iterdir()))

    def test_new_master_is_materialized_configured_and_activated(self) -> None:
        second = self.commit("v2")

        code, _out, _err = self.update()

        self.assertEqual(0, code)
        release = self.releases / second
        release_tool._identity(release, second)
        config = self.config_dir / f"clockify-review-cycle.{second}.json"
        self.assertEqual(0o600, config.stat().st_mode & 0o777)
        self.assertEqual({
            "root": str(release),
            "routing": str(release / "routing.json"),
            "state_dir": str(self.state),
        }, json.loads(config.read_text()))
        self.assertEqual(
            f"CLOCKIFY_AUTOPILOT_ROOT={release}\nCLOCKIFY_REVIEW_CYCLE_CONFIG={config}\n",
            self.active(),
        )
        record = json.loads(self.record.read_text())
        self.assertEqual(self.first, record["previous"])
        self.assertEqual(second, record["current"])
        self.assertEqual(0o600, self.record.stat().st_mode & 0o777)

    def test_private_routing_sibling_follows_the_new_release(self) -> None:
        routing = self.private(
            self.config_dir / f"clockify-review-cycle.{self.first}.routing.json",
            json.dumps({"root": str(self.current)}) + "\n",
        )
        second = self.commit("v2")

        code, _out, _err = self.update()

        self.assertEqual(0, code)
        copied = self.config_dir / f"clockify-review-cycle.{second}.routing.json"
        self.assertEqual(
            routing.read_text().replace(self.first, second), copied.read_text())
        self.assertEqual(0o600, copied.stat().st_mode & 0o777)

    def rejected(self) -> list[str]:
        if not self.record.exists():
            return []
        return [item["sha"] for item in json.loads(self.record.read_text()).get("rejected", [])]

    def test_failed_host_preflight_is_retried_not_rejected(self) -> None:
        before = self.active()
        second = self.commit("v2")
        self.verify.side_effect = ValueError("credential environment must be a regular mode-0600 file")

        code, _out, err = self.update()

        self.assertEqual(0, code)
        self.assertIn("skipped", err)
        self.assertEqual(before, self.active())
        self.assertNotIn(second, self.rejected())
        self.verify.side_effect = None
        code, _out, _err = self.update()
        self.assertEqual(0, code)
        self.assertIn(f"CLOCKIFY_AUTOPILOT_ROOT={self.releases / second}\n", self.active())

    def record_result(self, sha: str, status: int | str, *, result: str | None = None,
                      code: str = "exited") -> int:
        if result is None:
            result = "success" if status == 0 else "exit-code"
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return release_tool.main([
                "record-result", "--record", str(self.record),
                "--release", str(self.releases / sha),
                "--service-result", result, "--exit-code", code, "--exit-status", str(status),
            ])

    def test_crash_or_timeout_on_first_cycle_also_rolls_back(self) -> None:
        for result, code, status in (("exit-code", "exited", 1), ("timeout", "killed", "TERM")):
            with self.subTest(result=result):
                self.setUp()
                before = self.active()
                second = self.commit(f"v2-{result}")
                self.assertEqual(0, self.update()[0])
                for _ in range(2):
                    self.assertEqual(
                        0, self.record_result(second, status, result=result, code=code))

                self.assertEqual(0, self.update()[0])

                self.assertEqual(before, self.active())
                self.assertIn(second, self.rejected())

    def test_deliberate_stop_or_busy_lock_does_not_roll_back(self) -> None:
        second = self.commit("v2")
        self.assertEqual(0, self.update()[0])
        activated = self.active()
        self.assertEqual(0, self.record_result(second, "TERM", result="signal", code="killed"))
        self.assertEqual(0, self.record_result(second, 73))

        self.assertEqual(0, self.update()[0])

        self.assertEqual(activated, self.active())
        self.assertNotIn(second, self.rejected())

    def test_busy_lock_never_proves_a_release(self) -> None:
        before = self.active()
        second = self.commit("v2")
        self.assertEqual(0, self.update()[0])
        self.assertEqual(0, self.record_result(second, 73))
        self.assertEqual(0, self.record_result(second, 2))
        self.assertEqual(0, self.record_result(second, 2))

        self.assertEqual(0, self.update()[0])

        self.assertEqual(before, self.active())
        self.assertIn(second, self.rejected())

    def test_unavailable_rollback_target_does_not_freeze_updates(self) -> None:
        second = self.commit("v2")
        self.assertEqual(0, self.update()[0])
        activated = self.active()
        self.config.unlink()
        self.assertEqual(0, self.record_result(second, 2))
        self.assertEqual(0, self.record_result(second, 2))

        code, out, _err = self.update()

        self.assertEqual(0, code)
        self.assertEqual(activated, self.active())
        self.assertIsNone(json.loads(self.record.read_text())["previous"])
        third = self.commit("v3")
        self.assertEqual(0, self.update()[0])
        self.assertIn(f"CLOCKIFY_AUTOPILOT_ROOT={self.releases / third}\n", self.active())

    def test_leftover_candidate_from_a_skipped_update_is_refreshed(self) -> None:
        second = self.commit("v2")
        self.verify.side_effect = ValueError("gws directory must be private")
        self.assertEqual(0, self.update()[0])
        self.write_config(self.first, {
            "root": str(self.current),
            "routing": str(self.current / "routing.json"),
            "state_dir": str(self.state),
            "note": "edited after the skipped update",
        })
        self.verify.side_effect = None

        code, _out, _err = self.update()

        self.assertEqual(0, code)
        self.assertNotIn(second, self.rejected())
        config = self.config_dir / f"clockify-review-cycle.{second}.json"
        self.assertEqual("edited after the skipped update", json.loads(config.read_text())["note"])
        self.assertIn(f"CLOCKIFY_AUTOPILOT_ROOT={self.releases / second}\n", self.active())

    def test_restored_release_failing_never_reselects_the_rejected_one(self) -> None:
        before = self.active()
        second = self.commit("v2")
        self.assertEqual(0, self.update()[0])
        self.assertEqual(0, self.record_result(second, 2))
        self.assertEqual(0, self.record_result(second, 2))
        self.assertEqual(0, self.update()[0])
        self.assertEqual(before, self.active())
        self.assertEqual(0, self.record_result(self.first, 2))
        self.assertEqual(0, self.record_result(self.first, 2))

        self.assertEqual(0, self.update()[0])

        self.assertEqual(before, self.active())
        self.assertIn(second, self.rejected())

    def test_single_failed_cycle_keeps_the_new_release(self) -> None:
        second = self.commit("v2")
        self.assertEqual(0, self.update()[0])
        activated = self.active()
        self.assertEqual(0, self.record_result(second, 2))

        self.assertEqual(0, self.update()[0])

        self.assertEqual(activated, self.active())
        self.assertNotIn(second, self.rejected())

    def test_release_failing_two_cycles_in_a_row_is_rolled_back_and_rejected(self) -> None:
        before = self.active()
        second = self.commit("v2")
        self.assertEqual(0, self.update()[0])
        self.assertEqual(0, self.record_result(second, 2))
        self.assertEqual(0, self.record_result(second, 2))

        code, out, _err = self.update()

        self.assertEqual(0, code)
        self.assertIn("rolled back", out)
        self.assertEqual(before, self.active())
        self.assertIn(second, self.rejected())
        code, _out, _err = self.update()
        self.assertEqual(before, self.active())

    def test_blocked_cycle_after_a_healthy_one_keeps_the_release(self) -> None:
        second = self.commit("v2")
        self.assertEqual(0, self.update()[0])
        activated = self.active()
        self.assertEqual(0, self.record_result(second, 75))
        self.assertEqual(0, self.record_result(second, 2))

        code, _out, _err = self.update()

        self.assertEqual(0, code)
        self.assertEqual(activated, self.active())
        self.assertNotIn(second, self.rejected())

    def test_manual_rollback_keeps_the_updater_from_reselecting_the_release(self) -> None:
        before = self.active()
        second = self.commit("v2")
        self.assertEqual(0, self.update()[0])
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = release_tool.main([
                "rollback", "--release", str(self.current), "--sha", self.first,
                "--config", str(self.config), "--override", str(self.override),
                "--record", str(self.record),
            ])

        self.assertEqual(0, code)
        self.assertEqual(before, self.active())
        self.assertIn(second, self.rejected())
        self.update()
        self.assertEqual(before, self.active())

    def test_config_with_other_release_bound_fields_is_not_activated(self) -> None:
        self.write_config(self.first, {
            "root": str(self.current),
            "routing": str(self.current / "routing.json"),
            "state_dir": str(self.state),
            "cache": str(self.base / f"cache-{self.first}"),
        })
        before = self.active()
        second = self.commit("v2")

        code, _out, err = self.update()

        self.assertEqual(0, code)
        self.assertIn("skipped", err)
        self.assertEqual(before, self.active())
        self.assertIn(second, self.rejected())

    def test_unreachable_remote_keeps_current_release(self) -> None:
        before = self.active()
        _git(self.source, "remote", "set-url", "origin", str(self.base / "missing.git"))

        code, _out, err = self.update()

        self.assertEqual(0, code)
        self.assertIn("skipped", err)
        self.assertEqual(before, self.active())


class UpdateUnitOrderingTests(unittest.TestCase):
    UNITS = Path(release_tool.__file__).parent

    def test_cycle_unit_pulls_in_the_update_unit_first_without_requiring_it(self) -> None:
        cycle_unit = (self.UNITS / "clockify-review-cycle.service").read_text()
        self.assertIn("Wants=clockify-review-cycle-update.service", cycle_unit)
        self.assertIn("After=clockify-review-cycle-update.service", cycle_unit)
        self.assertNotIn("Requires=clockify-review-cycle-update.service", cycle_unit)
        self.assertIn(
            "ExecStopPost=-/usr/bin/python3 ${CLOCKIFY_AUTOPILOT_ROOT}/ops/systemd/user/"
            "clockify_review_cycle_release.py record-result",
            cycle_unit,
        )
        for variable in ("${SERVICE_RESULT}", "${EXIT_CODE}", "${EXIT_STATUS}"):
            self.assertIn(variable, cycle_unit)
        self.assertIn("flock --nonblock --conflict-exit-code 73", cycle_unit)

    def test_update_unit_runs_the_active_release_update_command(self) -> None:
        unit = (self.UNITS / "clockify-review-cycle-update.service").read_text()
        self.assertIn("Type=oneshot", unit)
        self.assertIn("clockify-review-cycle-override.env", unit)
        self.assertIn(
            "ExecStart=/usr/bin/python3 ${CLOCKIFY_AUTOPILOT_ROOT}/ops/systemd/user/"
            "clockify_review_cycle_release.py update",
            unit,
        )
        self.assertNotIn("--enable-sheet-write", unit)
        self.assertIn("--source-repository %h/Work/automation-clockify-sync-source ", unit)
        self.assertIn("ReadWritePaths=-%h/Work/automation-clockify-sync-source/.git", unit)
        self.assertNotIn("automation-clockify-sync-task-", unit)
        self.assertIn("EnvironmentFile=%h/.config/serenichron/clockify-review-cycle.env", unit)


if __name__ == "__main__":
    unittest.main()
