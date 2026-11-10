"""Behavioural tests for the run-directory retention pass.

Every test builds a private sandbox (HOME, runs/, state/, release copy and a
``systemctl`` PATH shim) and runs the real script as a subprocess, so exit
codes and the on-disk inventory are what an operator would see.
"""
from __future__ import annotations

import configparser
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
import uuid


ROOT = Path(__file__).resolve().parents[1]
USER_SYSTEMD = ROOT / "ops/systemd/user"
SCRIPT = USER_SYSTEMD / "clockify_runs_retention.py"
RELEASE_TOOL = USER_SYSTEMD / "clockify_review_cycle_release.py"
RETENTION_UNIT = USER_SYSTEMD / "clockify-runs-retention.service"
CYCLE_UNIT = USER_SYSTEMD / "clockify-review-cycle.service"
SHA_A = "a" * 40
SHA_B = "b" * 40
DAY = 86400
SLICE_HASH = "0123456789abcdef" * 4
FILLERS = ("zzzzzzzz-latest-1", "zzzzzzzz-latest-2", "zzzzzzzz-latest-3")

SHIM = r'''#!{python}
import json, os, sys
from pathlib import Path
state = json.loads(Path(os.environ["FAKE_SYSTEMCTL"]).read_text())
counter = Path(state["counter"])
calls = (int(counter.read_text() or "0") if counter.exists() else 0) + 1
counter.write_text(str(calls))
args = sys.argv[1:]
unit = next(arg for arg in args if arg.endswith(".service"))
for action in state.get("actions", []):
    if action["at"] == calls:
        if action["kind"] == "append":
            with open(action["path"], "a") as handle:
                handle.write(" changed")
        elif action["kind"] == "mkdir":
            os.mkdir(action["path"])
if state.get("transport_error"):
    sys.stderr.write("Failed to connect to bus: Operation not permitted\n")
    sys.exit(1)
invocation = state.get("invocation", "0" * 32)
if state.get("change_after") and calls > state["change_after"]:
    invocation = "f" * 32
values = {
    "ActiveState": state.get("active", {}).get(unit, "inactive"),
    "InvocationID": invocation,
    "ExecMainStartTimestampMonotonic": "100",
    "ExecMainExitTimestampMonotonic": "200",
}
for key in state.get("omit", []):
    values.pop(key, None)
for arg in args:
    if arg.startswith("--property="):
        key = arg.split("=", 1)[1]
        if key in values:
            print(f"{key}={values[key]}")
'''


def unit(path: Path) -> configparser.ConfigParser:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str
    parser.read_string(path.read_text(encoding="utf-8"))
    return parser


def repeated(path: Path, name: str) -> list[str]:
    prefix = name + "="
    return [line[len(prefix):] for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith(prefix)]


def age(path: Path, days: float) -> None:
    """Backdate a run and everything inside it (children first)."""
    moment = time.time() - days * DAY
    for entry in sorted(path.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        os.utime(entry, (moment, moment), follow_symlinks=False)
    os.utime(path, (moment, moment))


class Sandbox:
    def __init__(self, base: Path):
        self.base = base
        self.home = base / "home"
        self.live = self.home / "Work/automation-clockify-sync"
        self.runs = self.live / "runs"
        self.state = self.live / "state"
        self.extra = self.home / "extra-root"
        self.optional = self.home / "optional-root"
        self.serenichron = self.home / ".config/serenichron"
        self.config_path = self.serenichron / "clockify-runs-retention.json"
        self.override_lock = self.serenichron / "clockify-review-cycle-override.env.lock"
        self.release_lock = self.state / "release-updates.json.lock"
        self.runner_lock = self.state / "autopilot-runner.lock"
        self.coverage = self.state / "source-coverage.json"
        self.own = self.state / "runs-retention"
        self.inventory_path = self.own / "inventory.json"
        self.bin = base / "bin"
        self.control = base / "systemctl-control.json"
        self.counter = base / "systemctl-calls"
        for directory in (self.runs, self.state, self.extra, self.optional,
                          self.serenichron, self.bin):
            directory.mkdir(parents=True, exist_ok=True)
        for directory in [self.base, *self.base.rglob("*")]:
            if directory.is_dir():
                directory.chmod(0o755)
        self.serenichron.chmod(0o700)
        self.releases = {sha: self._release(sha) for sha in (SHA_A, SHA_B)}
        for lock, mode in ((self.override_lock, 0o600), (self.release_lock, 0o600),
                           (self.runner_lock, 0o600)):
            lock.touch()
            lock.chmod(mode)
        self.coverage.write_text("{}\n")
        (self.extra / "notes.txt").write_text("no run names here\n")
        shim = self.bin / "systemctl"
        shim.write_text(SHIM.replace("{python}", sys.executable))
        shim.chmod(0o755)
        self.systemctl({})
        self.config = {
            "runs_dir": str(self.runs),
            "state_dir": str(self.state),
            "reference_roots": [str(self.state), str(self.extra)],
            "optional_roots": [str(self.optional)],
            "coverage_file": str(self.coverage),
        }
        self.write_config()
        for name in FILLERS:
            self.make_run(name)

    def _release(self, sha: str) -> Path:
        target = self.base / "releases" / sha / "ops/systemd/user"
        target.mkdir(parents=True)
        shutil.copy2(SCRIPT, target / SCRIPT.name)
        shutil.copy2(RELEASE_TOOL, target / RELEASE_TOOL.name)
        return self.base / "releases" / sha

    def write_config(self, **changes) -> None:
        self.config.update(changes)
        self.config = {key: value for key, value in self.config.items() if value is not None}
        self.config_path.write_text(json.dumps(self.config))
        self.config_path.chmod(0o600)

    def systemctl(self, control: dict) -> None:
        self.control.write_text(json.dumps({"counter": str(self.counter), **control}))
        self.counter.unlink(missing_ok=True)

    def make_run(self, name: str, files: dict | None = None, *, days: float = 30) -> Path:
        path = self.runs / name
        path.mkdir()
        for relative, text in (files or {"run-report.txt": "payload\n"}).items():
            target = path / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        age(path, days)
        return path

    def run(self, sha: str = SHA_A) -> tuple[int, str, dict]:
        release = self.releases[sha]
        environment = {
            "HOME": str(self.home),
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "FAKE_SYSTEMCTL": str(self.control),
            "LANG": "C.UTF-8",
        }
        result = subprocess.run(
            [sys.executable, str(release / "ops/systemd/user" / SCRIPT.name),
             "--config", str(self.config_path), "--release", str(release)],
            env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            timeout=120,
        )
        if "Traceback" in result.stderr:
            raise AssertionError(f"script crashed:\n{result.stderr}")
        records = self.records()
        return result.returncode, result.stdout + result.stderr, records[-1] if records else {}

    def records(self) -> list:
        if not self.inventory_path.exists():
            return []
        return json.loads(self.inventory_path.read_text())["records"]

    def prime(self, sha: str = SHA_A) -> dict:
        """Satisfy the delete gate with a same-release dry-run, then enable delete."""
        self.write_config(mode="dry-run")
        code, output, record = self.run(sha)
        assert record.get("outcome") == "dry-run", (output, record)
        self.write_config(mode="delete")
        self.counter.unlink(missing_ok=True)
        return record

    def names(self) -> set:
        return {entry.name for entry in self.runs.iterdir() if entry.is_dir()}


def candidates(record: dict) -> set:
    return {item["name"] for item in record.get("remaining_candidates", [])}


class RetentionTestCase(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="runs-retention-test-")
        self.addCleanup(self._cleanup)
        self.box = Sandbox(Path(self.tmp.name))

    def _cleanup(self) -> None:
        for path in Path(self.tmp.name).rglob("*"):
            if path.is_dir() and not path.is_symlink():
                path.chmod(0o700)
        self.tmp.cleanup()

    def assertAbortedBeforeDelete(self, before: set, code: int, record: dict,
                                  reason: str = "") -> None:
        self.assertEqual("aborted-before-delete", record.get("outcome"), record.get("reason"))
        self.assertEqual(1, code)
        self.assertEqual([], record["deleted"])
        self.assertFalse(record.get("deletion_started", False))
        self.assertEqual(before, self.box.names())
        if reason:
            self.assertIn(reason, record["reason"])


class ClassificationTests(RetentionTestCase):
    def test_direct_reference_from_state_is_kept(self) -> None:
        box = self.box
        box.make_run("20260801-direct-kept")
        box.make_run("20260801-unreferenced")
        (box.state / "review-items.json").write_text('{"run": "runs/20260801-direct-kept/x"}')
        code, _, record = box.run()
        self.assertEqual(0, code)
        self.assertEqual("dry-run", record["outcome"])
        self.assertEqual({"20260801-unreferenced"}, candidates(record))
        self.assertEqual(1, record["direct"]["count"])

    def test_transitive_reference_chain_is_kept(self) -> None:
        box = self.box
        box.make_run("chain-aaaa-0001", {"ref.json": '"chain-bbbb-0002"'})
        box.make_run("chain-bbbb-0002", {"nested/ref.txt": "source=chain-cccc-0003;"})
        box.make_run("chain-cccc-0003")
        box.make_run("chain-dddd-0004")
        (box.state / "validation.json").write_text('["chain-aaaa-0001"]')
        _, _, record = box.run()
        self.assertEqual({"chain-dddd-0004"}, candidates(record))
        self.assertGreaterEqual(record["after_closure"]["rounds"], 3)

    def test_held_child_keeps_old_unreferenced_source(self) -> None:
        box = self.box
        box.make_run("collector-derivation-0001", {"source.txt": "from heldsrc-00000001"})
        box.make_run("heldsrc-00000001")
        box.make_run("heldsrc-00000002")
        _, _, record = box.run()
        self.assertEqual({"heldsrc-00000002"}, candidates(record))

    def test_young_child_keeps_old_unreferenced_source(self) -> None:
        box = self.box
        box.make_run("youngchild-0001", {"source.txt": "youngsrc-00000001"}, days=1)
        box.make_run("youngsrc-00000001")
        box.make_run("youngsrc-00000002")
        _, _, record = box.run()
        self.assertEqual({"youngsrc-00000002"}, candidates(record))
        self.assertEqual(1, record["young"]["count"])

    def test_top_level_file_reference_seeds_closure(self) -> None:
        box = self.box
        box.make_run("topref-00000001", {"next.txt": "topref-00000002"})
        box.make_run("topref-00000002")
        box.make_run("topref-00000003")
        (box.runs / "receipt-20261004.json").write_text('{"run": "topref-00000001"}')
        _, _, record = box.run()
        self.assertEqual({"topref-00000003"}, candidates(record))

    def test_prefix_matching_is_substring_of_token(self) -> None:
        box = self.box
        box.make_run("prefix-run-0001")
        box.make_run("prefix-run-0001-x")
        box.make_run("prefix-run-0002")
        box.make_run("prefix-run-0002-x")
        # Only the short name: the longer sibling must stay eligible.
        # Only the long name: the short name is a substring of the token and is kept.
        (box.state / "refs.txt").write_text("prefix-run-0001 prefix-run-0002-x\n")
        _, _, record = box.run()
        self.assertEqual({"prefix-run-0001-x"}, candidates(record))

    def test_name_without_digits_is_matched_without_abort(self) -> None:
        box = self.box
        box.make_run("alphabetical-run-kept")
        box.make_run("alphabetical-run-gone")
        (box.state / "refs.txt").write_text("see alphabetical-run-kept")
        code, _, record = box.run()
        self.assertEqual(0, code)
        self.assertEqual({"alphabetical-run-gone"}, candidates(record))

    def test_name_shorter_than_token_minimum_aborts_without_deleting(self) -> None:
        # Plan revision 2, Phase A step 3: a run name under MIN_TOKEN aborts.
        box = self.box
        box.make_run("old-eligible-0001")
        box.make_run("short")
        box.write_config(mode="dry-run")
        before = box.names()
        code, output, record = box.run()
        self.assertAbortedBeforeDelete(before, code, record, "unsupported run name")
        self.assertIn("aborted-before-delete", output)

    def test_reference_spanning_read_chunk_boundary_is_found(self) -> None:
        box = self.box
        box.make_run("boundary-run-0001")
        box.make_run("boundary-run-0002")
        (box.state / "big.txt").write_bytes(b" " * (1024 * 1024 - 7) + b"boundary-run-0001\n")
        _, _, record = box.run()
        self.assertEqual({"boundary-run-0002"}, candidates(record))

    def test_young_kept_old_eligible_and_newest_inner_mtime_counts(self) -> None:
        box = self.box
        box.make_run("age-young-0001", days=3)
        box.make_run("age-old-00002")
        inner = box.make_run("age-mixed-0003", {"a.txt": "a", "sub/b.txt": "b"})
        moment = time.time() - 2 * DAY
        os.utime(inner / "sub/b.txt", (moment, moment))
        _, _, record = box.run()
        self.assertEqual({"age-old-00002"}, candidates(record))
        self.assertEqual(2, record["young"]["count"])

    def test_min_age_days_below_seven_rejected_and_seven_accepted(self) -> None:
        box = self.box
        box.make_run("minage-run-0001", days=8)
        box.write_config(min_age_days=6)
        code, _, record = box.run()
        self.assertEqual(1, code)
        self.assertEqual("aborted-before-delete", record["outcome"])
        self.assertIn("min_age_days", record["reason"])
        box.write_config(min_age_days=7)
        code, _, record = box.run()
        self.assertEqual(0, code)
        self.assertEqual({"minage-run-0001"}, candidates(record))

    def test_nothing_eligible_outcome(self) -> None:
        code, output, record = self.box.run()
        self.assertEqual(0, code)
        self.assertEqual("nothing-eligible", record["outcome"])
        self.assertIn("nothing-eligible", output)


class HoldRuleTests(RetentionTestCase):
    def test_h1_top_level_autopilot_result_is_held(self) -> None:
        box = self.box
        box.make_run("h1-run-held-01", {"autopilot-result.json": "{}"})
        box.make_run("h1-run-nested-02", {"inner/autopilot-result.json": "{}"})
        _, _, record = box.run()
        self.assertEqual({"h1-run-nested-02"}, candidates(record))
        self.assertEqual(1, record["held"]["H1"]["count"])

    def test_h2_slice_without_completion_bundle_is_held(self) -> None:
        box = self.box
        pending = f"20261001-20261002-{SLICE_HASH}-pending"
        complete = f"20261001-20261002-{SLICE_HASH}-complete"
        box.make_run(pending)
        box.make_run(complete, {"completion-bundle.json": "{}"})
        _, _, record = box.run()
        self.assertEqual({complete}, candidates(record))
        self.assertEqual(1, record["held"]["H2"]["count"])

    def test_h3_collector_derivation_is_held(self) -> None:
        box = self.box
        box.make_run("collector-derivation-empty")
        box.make_run("collector-derivation-results", {"results/out.json": "{}"})
        box.make_run("collector-other-0001")
        _, _, record = box.run()
        self.assertEqual({"collector-other-0001"}, candidates(record))
        self.assertEqual(2, record["held"]["H3"]["count"])

    def test_h4_three_greatest_names_are_held(self) -> None:
        box = self.box
        box.make_run("zzzzzzzz-latest-0")  # fourth-greatest name
        _, _, record = box.run()
        self.assertEqual({"zzzzzzzz-latest-0"}, candidates(record))
        self.assertEqual(3, record["held"]["H4"]["count"])

    def test_h5_run_reports_held_while_coverage_absent_and_released_after(self) -> None:
        box = self.box
        box.make_run("h5-report-run-01", {"run-report.json": "{}"})
        box.make_run("h5-plain-run-002")
        box.coverage.unlink()
        _, _, record = box.run()
        self.assertEqual({"h5-plain-run-002"}, candidates(record))
        self.assertEqual(1, record["held"]["H5"]["count"])
        box.coverage.write_text("{}\n")
        _, _, record = box.run()
        self.assertEqual({"h5-report-run-01", "h5-plain-run-002"}, candidates(record))
        self.assertEqual(0, record["held"]["H5"]["count"])

    def test_held_directories_survive_a_real_delete_pass(self) -> None:
        box = self.box
        held = {
            "h1-run-held-01": {"autopilot-result.json": "{}"},
            f"20261001-20261002-{SLICE_HASH}-pending": None,
            "collector-derivation-0001": None,
        }
        for name, files in held.items():
            box.make_run(name, files)
        box.make_run("doomed-run-0001")
        box.prime()
        code, _, record = box.run()
        self.assertEqual((0, "deleted"), (code, record["outcome"]))
        self.assertEqual(set(held) | set(FILLERS), box.names())


class TopLevelEntryTests(RetentionTestCase):
    def test_top_level_regular_file_is_listed_and_never_deleted(self) -> None:
        box = self.box
        receipt = box.runs / "september-native-post-265-20261004-receipt.json"
        receipt.write_text('{"note": "nothing"}')
        age(receipt, 60)
        box.make_run("toplevel-doomed-01")
        box.prime()
        code, _, record = box.run()
        self.assertEqual((0, "deleted"), (code, record["outcome"]))
        self.assertTrue(receipt.is_file())
        self.assertEqual([{"name": receipt.name, "bytes": receipt.stat().st_size}],
                         record["top_level_files"]["files"])
        self.assertNotIn("toplevel-doomed-01", box.names())

    def test_top_level_symlink_aborts(self) -> None:
        box = self.box
        box.make_run("symlink-target-01")
        (box.runs / "link-to-run-0001").symlink_to(box.runs / "symlink-target-01")
        before = box.names()
        code, _, record = box.run()
        self.assertAbortedBeforeDelete(before, code, record, "symlink or special")

    def test_top_level_fifo_aborts(self) -> None:
        box = self.box
        box.make_run("fifo-neighbour-01")
        os.mkfifo(box.runs / "pipe-entry-0001")
        before = box.names()
        code, _, record = box.run()
        self.assertAbortedBeforeDelete(before, code, record)


class GateAndModeTests(RetentionTestCase):
    def test_default_mode_is_dry_run_and_deletes_nothing(self) -> None:
        box = self.box
        self.assertNotIn("mode", box.config)
        box.make_run("dryrun-eligible-1")
        before = box.names()
        code, _, record = box.run()
        self.assertEqual(0, code)
        self.assertEqual(("dry-run", "dry-run", "dry-run"),
                         (record["mode"], record["effective_mode"], record["outcome"]))
        self.assertEqual({"dryrun-eligible-1"}, candidates(record))
        self.assertEqual(before, box.names())
        self.assertEqual([], record["deleted"])

    def test_delete_mode_without_prior_dry_run_runs_as_dry_run(self) -> None:
        box = self.box
        box.make_run("gate-eligible-001")
        box.write_config(mode="delete")
        before = box.names()
        code, _, record = box.run()
        self.assertEqual(0, code)
        self.assertEqual(("delete", "dry-run", "dry-run"),
                         (record["mode"], record["effective_mode"], record["outcome"]))
        self.assertIn("dry-run required", record["reason"])
        self.assertEqual(before, box.names())

    def test_dry_run_from_another_release_does_not_open_the_gate(self) -> None:
        box = self.box
        box.make_run("gate-eligible-001")
        box.prime(SHA_B)
        before = box.names()
        code, _, record = box.run(SHA_A)
        self.assertEqual(("dry-run", SHA_A), (record["effective_mode"], record["release_sha"]))
        self.assertEqual(before, box.names())

    def test_nothing_eligible_record_does_not_open_the_gate(self) -> None:
        box = self.box
        box.run()
        self.assertEqual("nothing-eligible", box.records()[-1]["outcome"])
        box.make_run("gate-eligible-001")
        box.write_config(mode="delete")
        _, _, record = box.run()
        self.assertEqual("dry-run", record["effective_mode"])
        self.assertIn("gate-eligible-001", box.names())

    def test_same_release_dry_run_then_delete_removes_only_eligible(self) -> None:
        box = self.box
        box.make_run("keep-direct-00001")
        box.make_run("delete-me-000001")
        box.make_run("delete-me-000002")
        (box.state / "refs.txt").write_text("keep-direct-00001")
        box.prime()
        code, output, record = box.run()
        self.assertEqual((0, "deleted", "delete"),
                         (code, record["outcome"], record["effective_mode"]))
        self.assertEqual({"delete-me-000001", "delete-me-000002"},
                         {item["name"] for item in record["deleted"]})
        self.assertEqual([], record["remaining_candidates"])
        self.assertEqual({"keep-direct-00001", *FILLERS}, box.names())
        self.assertIn("retention deleted", output)


class AbortBeforeDeleteTests(RetentionTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.box.make_run("abort-candidate-01")
        self.box.make_run("abort-candidate-02")

    def primed_run(self, reason: str = "") -> dict:
        self.box.prime()
        before = self.box.names()
        code, _, record = self.box.run()
        self.assertAbortedBeforeDelete(before, code, record, reason)
        self.assertEqual("delete", record["effective_mode"])
        return record

    def test_missing_required_root(self) -> None:
        self.box.write_config(reference_roots=[str(self.box.state), str(self.box.home / "gone")])
        before = self.box.names()
        code, _, record = self.box.run()
        self.assertAbortedBeforeDelete(before, code, record, "required root is absent")

    def test_present_but_unreadable_optional_root(self) -> None:
        self.box.optional.chmod(0)
        before = self.box.names()
        code, _, record = self.box.run()
        self.assertAbortedBeforeDelete(before, code, record)

    def test_absent_optional_root_is_fine(self) -> None:
        self.box.optional.rmdir()
        code, _, record = self.box.run()
        self.assertEqual((0, "dry-run"), (code, record["outcome"]))

    def test_symlink_inside_candidate(self) -> None:
        (self.box.runs / "abort-candidate-01/link").symlink_to("/etc/hostname")
        before = self.box.names()
        code, _, record = self.box.run()
        self.assertAbortedBeforeDelete(before, code, record, "symlink or special")

    def _hold(self, path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.addCleanup(os.close, descriptor)

    def test_held_override_lock(self) -> None:
        self.box.prime()
        self._hold(self.box.override_lock)
        before = self.box.names()
        code, _, record = self.box.run()
        self.assertAbortedBeforeDelete(before, code, record)

    def test_held_release_record_lock(self) -> None:
        self.box.prime()
        self._hold(self.box.release_lock)
        before = self.box.names()
        code, _, record = self.box.run()
        self.assertAbortedBeforeDelete(before, code, record)

    def test_held_runner_lock(self) -> None:
        self.box.prime()
        self._hold(self.box.runner_lock)
        before = self.box.names()
        code, _, record = self.box.run()
        self.assertAbortedBeforeDelete(before, code, record)

    def test_missing_override_lock(self) -> None:
        self.box.override_lock.unlink()
        self.primed_run()

    def test_active_cycle_unit(self) -> None:
        self.box.systemctl({"active": {"clockify-review-cycle.service": "active"}})
        before = self.box.names()
        code, _, record = self.box.run()
        self.assertAbortedBeforeDelete(before, code, record, "not quiescent")

    def test_activating_update_unit(self) -> None:
        self.box.systemctl({"active": {"clockify-review-cycle-update.service": "activating"}})
        before = self.box.names()
        code, _, record = self.box.run()
        self.assertAbortedBeforeDelete(before, code, record, "not quiescent")

    def test_unit_becomes_active_between_scan_and_delete(self) -> None:
        self.box.prime()
        # Phase A makes 4 systemctl calls; flip the canary on the first Phase B call.
        control = {"actions": []}
        self.box.systemctl(control)
        script = self.box.bin / "systemctl"
        original = script.read_text()
        script.write_text(original.replace(
            'state.get("active", {}).get(unit, "inactive")',
            '("active" if calls > 4 and unit.endswith("canary.service") else "inactive")'))
        before = self.box.names()
        code, _, record = self.box.run()
        self.assertAbortedBeforeDelete(before, code, record, "not quiescent")

    def test_changed_unit_marker(self) -> None:
        self.box.prime()
        self.box.systemctl({"change_after": 4})
        self.primed_run_no_prime("unit markers changed")

    def test_systemctl_transport_error(self) -> None:
        self.box.systemctl({"transport_error": True})
        before = self.box.names()
        code, _, record = self.box.run()
        self.assertAbortedBeforeDelete(before, code, record, "systemctl show failed")

    def test_missing_systemctl_property(self) -> None:
        self.box.systemctl({"omit": ["ExecMainExitTimestampMonotonic"]})
        before = self.box.names()
        code, _, record = self.box.run()
        self.assertAbortedBeforeDelete(before, code, record, "missing systemctl properties")

    def test_file_in_state_changed_after_scan_start(self) -> None:
        refs = self.box.state / "refs.txt"
        refs.write_text("nothing")
        self.box.prime()
        self.box.systemctl({"actions": [{"at": 5, "kind": "append", "path": str(refs)}]})
        self.primed_run_no_prime("changed")

    def test_new_run_directory_after_scan_start(self) -> None:
        self.box.prime()
        self.box.systemctl({"actions": [
            {"at": 5, "kind": "mkdir", "path": str(self.box.runs / "late-arrival-0001")}]})
        record = self.primed_run_no_prime("changed", expect_extra={"late-arrival-0001"})
        self.assertIn("late-arrival-0001", self.box.names())
        del record

    def primed_run_no_prime(self, reason: str, expect_extra: set | None = None) -> dict:
        before = self.box.names() | (expect_extra or set())
        code, _, record = self.box.run()
        self.assertAbortedBeforeDelete(before, code, record, reason)
        self.assertEqual("delete", record["effective_mode"])
        return record

    def test_config_with_wrong_mode_is_rejected(self) -> None:
        self.box.config_path.chmod(0o644)
        before = self.box.names()
        code, _, record = self.box.run()
        self.assertAbortedBeforeDelete(before, code, record, "0600")


class PartialDeleteTests(RetentionTestCase):
    def test_failure_on_second_candidate_reports_partial_delete(self) -> None:
        box = self.box
        box.make_run("partial-run-0001")
        second = box.make_run("partial-run-0002", {"locked/file.txt": "x", "other.txt": "y"})
        box.make_run("partial-run-0003")
        locked = second / "locked"
        moment = (second / "other.txt").stat().st_mtime
        locked.chmod(0o500)
        os.utime(locked, (moment, moment))
        os.utime(second, (moment, moment))
        box.prime()
        code, output, record = box.run()
        self.assertEqual(1, code)
        self.assertEqual("partial-delete-failure", record["outcome"])
        self.assertEqual(["partial-run-0001"], [item["name"] for item in record["deleted"]])
        self.assertEqual(["partial-run-0002", "partial-run-0003"],
                         [item["name"] for item in record["remaining_candidates"]])
        self.assertEqual("partial-run-0002", record["possibly_partial_candidate"])
        self.assertNotIn("partial-run-0001", box.names())
        self.assertIn("partial-run-0003", box.names())
        self.assertEqual(1, record["deleted_summary"]["count"])
        self.assertEqual(len(box.names()), record["post_pass"]["directories"])
        self.assertIn("partial-delete-failure", output)


class InventoryAndCacheTests(RetentionTestCase):
    def test_inventory_mode_atomic_replace_and_record_cap(self) -> None:
        box = self.box
        box.make_run("inventory-run-001")
        box.write_config(max_records=2)
        ids, inodes = [], []
        for _ in range(3):
            code, _, record = box.run()
            self.assertEqual(0, code)
            ids.append(record["id"])
            inodes.append(box.inventory_path.stat().st_ino)
            self.assertEqual(0o600, stat.S_IMODE(box.inventory_path.stat().st_mode))
        records = box.records()
        self.assertEqual(ids[1:], [item["id"] for item in records])
        self.assertEqual(3, len(set(inodes)), "inventory must be replaced, not rewritten in place")
        self.assertEqual([], [p.name for p in box.own.iterdir() if p.name.startswith(".")])
        self.assertEqual(0o700, stat.S_IMODE(box.own.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE((box.own / "tokens.json").stat().st_mode))

    def test_inventory_records_before_and_after_counts_and_bytes(self) -> None:
        box = self.box
        box.make_run("counts-keep-00001", {"a.txt": "k" * 100})
        box.make_run("counts-gone-00001", {"a.txt": "g" * 300, "b/c.txt": "g" * 20})
        (box.runs / "receipt-top-level.json").write_text("z" * 50)
        (box.state / "refs.txt").write_text("counts-keep-00001")
        box.prime()
        code, _, record = box.run()
        self.assertEqual((0, "deleted"), (code, record["outcome"]))
        for key in ("time", "release_sha", "mode", "outcome", "reason",
                    "scan_duration_seconds", "cache", "all", "top_level_files", "direct",
                    "young", "held", "after_closure", "unreferenced", "eligible",
                    "deleted", "remaining_candidates", "post_pass", "deleted_summary"):
            self.assertIn(key, record)
        self.assertEqual(set(f"H{n}" for n in range(1, 6)), set(record["held"]))
        self.assertIn("rounds", record["after_closure"])
        self.assertIn("hit_ratio", record["cache"])
        self.assertEqual(SHA_A, record["release_sha"])
        self.assertEqual({"count": 1, "bytes": 320}, record["eligible"])
        self.assertEqual({"count": 1, "bytes": 320}, record["deleted_summary"])
        self.assertEqual({"name", "bytes", "newest_mtime_ns"}, set(record["deleted"][0]))
        self.assertEqual(record["all"]["count"] - 1, record["post_pass"]["directories"])
        self.assertEqual(1, record["post_pass"]["top_level_files"])
        self.assertEqual(
            record["all"]["bytes"] + record["top_level_files"]["bytes"] - 320,
            record["post_pass"]["bytes"],
        )

    def test_own_inventory_and_cache_are_not_references(self) -> None:
        box = self.box
        box.make_run("selfref-run-00001")
        _, _, first = box.run()
        self.assertIn("selfref-run-00001", box.inventory_path.read_text())
        _, _, second = box.run()
        self.assertEqual({"selfref-run-00001"}, candidates(first))
        self.assertEqual(candidates(first), candidates(second))
        self.assertEqual(0, second["direct"]["count"])

    def test_warm_cache_matches_cold_and_rescans_on_change(self) -> None:
        box = self.box
        box.make_run("cache-run-000001")
        box.make_run("cache-run-000002")
        refs = box.state / "refs.txt"
        refs.write_text("cache-run-000001 aaaa")
        _, _, cold = box.run()
        _, _, warm = box.run()
        self.assertEqual(candidates(cold), candidates(warm))
        self.assertEqual({"cache-run-000002"}, candidates(warm))
        self.assertGreater(cold["cache"]["misses"], 0)
        self.assertEqual(0, warm["cache"]["misses"])
        self.assertEqual(cold["cache"]["misses"], warm["cache"]["hits"])
        # Same size, new mtime: only this file is rescanned, result unchanged.
        refs.write_text("cache-run-000001 bbbb")
        _, _, touched = box.run()
        self.assertEqual(1, touched["cache"]["misses"])
        self.assertEqual(candidates(cold), candidates(touched))
        # New reference: refs.txt plus the newly retained run's file are scanned.
        refs.write_text("cache-run-000001 cache-run-000002")
        _, _, changed = box.run()
        self.assertEqual(2, changed["cache"]["misses"])
        self.assertEqual("nothing-eligible", changed["outcome"])

    def test_tokenizer_version_mismatch_discards_cache(self) -> None:
        box = self.box
        box.make_run("cache-run-000001")
        box.run()
        cache = box.own / "tokens.json"
        document = json.loads(cache.read_text())
        document["tokenizer_version"] = 999
        cache.write_text(json.dumps(document))
        _, _, record = box.run()
        self.assertEqual(0, record["cache"]["hits"])
        self.assertGreater(record["cache"]["misses"], 0)

    def test_started_record_is_durable_before_phase_b(self) -> None:
        box = self.box
        box.make_run("started-run-0001")
        box.prime()
        release = box.releases[SHA_A]
        script = release / "ops/systemd/user" / SCRIPT.name
        environment = {"HOME": str(box.home), "FAKE_SYSTEMCTL": str(box.control),
                       "PATH": f"{box.bin}:{os.environ.get('PATH', '/usr/bin')}"}
        seen = {}
        with mock.patch.dict(os.environ, environment):
            spec = importlib.util.spec_from_file_location(f"retention_{uuid.uuid4().hex}", script)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            def observe(scan, record):
                seen["records"] = json.loads(box.inventory_path.read_text())["records"]
                record["outcome"] = "deleted"

            argv = [str(script), "--config", str(box.config_path), "--release", str(release)]
            previous = os.umask(0o022)
            os.umask(previous)
            try:
                with mock.patch.object(module, "delete_candidates", observe), \
                        mock.patch.object(sys, "argv", argv), \
                        mock.patch("sys.stdout"):
                    code = module.main()
            finally:
                os.umask(previous)
        self.assertEqual(0, code)
        started = seen["records"][-1]
        self.assertEqual(("started", "delete"), (started["outcome"], started["effective_mode"]))
        self.assertEqual({"started-run-0001"}, candidates(started))
        self.assertEqual("deleted", box.records()[-1]["outcome"])
        self.assertEqual(started["id"], box.records()[-1]["id"])



SLOW_LINE = b"x" * 1000 + b" "


def slow_file(path: Path, mebibytes: int = 40) -> None:
    """Long tokens cost one index lookup per byte, so this outlasts a 1 s deadline."""
    path.write_bytes(SLOW_LINE * (mebibytes * 1024 * 1024 // len(SLOW_LINE)))


def cache_keys(box: Sandbox) -> list:
    return [json.loads(key)[0] for key in
            json.loads((box.own / "tokens.json").read_text())["entries"]]


class DeadlineAndCheckpointTests(RetentionTestCase):
    def run_in_process(self, patcher) -> tuple[int, dict]:
        """Run main() in-process so a test can inject a deadline at a chosen point."""
        box = self.box
        release = box.releases[SHA_A]
        script = release / "ops/systemd/user" / SCRIPT.name
        environment = {"HOME": str(box.home), "FAKE_SYSTEMCTL": str(box.control),
                       "PATH": f"{box.bin}:{os.environ.get('PATH', '/usr/bin')}"}
        with mock.patch.dict(os.environ, environment):
            spec = importlib.util.spec_from_file_location(f"retention_{uuid.uuid4().hex}", script)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            argv = [str(script), "--config", str(box.config_path), "--release", str(release)]
            previous = os.umask(0o022)
            os.umask(previous)
            try:
                with patcher(module), mock.patch.object(sys, "argv", argv), \
                        mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                    code = module.main()
            finally:
                os.umask(previous)
        return code, box.records()[-1]

    def test_deadline_inside_reference_scan_records_abort_and_exits_nonzero(self) -> None:
        box = self.box
        box.make_run("deadline-cand-0001")
        box.prime()
        slow_file(box.state / "zz-slow.txt")
        box.write_config(deadline_seconds=1)
        before = box.names()
        started = time.monotonic()
        code, output, record = box.run()
        elapsed = time.monotonic() - started
        self.assertAbortedBeforeDelete(before, code, record, "scan deadline exceeded")
        self.assertEqual("delete", record["mode"])  # effective mode is decided after the scan
        self.assertIn("aborted-before-delete", output)
        # The abort must land near the deadline, not after the whole file is read.
        self.assertLess(elapsed, 3.0)
        self.assertLess(record["duration_seconds"], 2.5)

    def test_partial_checkpoint_is_reused_and_excludes_unfinished_file(self) -> None:
        box = self.box
        box.make_run("checkpoint-kept-01")
        box.make_run("checkpoint-gone-01")
        refs = box.state / "aa-refs.txt"
        refs.write_text("checkpoint-kept-01")
        slow = box.state / "zz-slow.txt"
        slow_file(slow)
        box.write_config(deadline_seconds=1)
        code, _, aborted = box.run()
        self.assertEqual((1, "aborted-before-delete"), (code, aborted["outcome"]))
        self.assertIn("scan deadline exceeded", aborted["reason"])
        keys = cache_keys(box)
        self.assertIn(str(refs), keys, "completed file must be checkpointed")
        self.assertNotIn(str(slow), keys, "unfinished file must not be checkpointed")
        self.assertEqual(0o600, stat.S_IMODE((box.own / "tokens.json").stat().st_mode))
        slow.unlink()
        box.write_config(deadline_seconds=1500)
        code, _, resumed = box.run()
        self.assertEqual((0, "dry-run"), (code, resumed["outcome"]))
        self.assertGreater(resumed["cache"]["hits"], 0)
        self.assertEqual({"checkpoint-gone-01"}, candidates(resumed))

    def test_file_changed_after_checkpoint_is_rescanned_not_trusted(self) -> None:
        box = self.box
        box.make_run("checkpoint-old-001")
        box.make_run("checkpoint-new-001")
        refs = box.state / "aa-refs.txt"
        refs.write_text("checkpoint-old-001")
        slow = box.state / "zz-slow.txt"
        slow_file(slow)
        box.write_config(deadline_seconds=1)
        box.run()
        self.assertIn(str(refs), cache_keys(box))
        # Same size, new content: a stale checkpoint would wrongly free the new reference.
        refs.write_text("checkpoint-new-001")
        slow.unlink()
        box.write_config(deadline_seconds=1500)
        _, _, record = box.run()
        self.assertEqual({"checkpoint-old-001"}, candidates(record))

    def test_deadline_between_deletions_records_accurate_partial_inventory(self) -> None:
        box = self.box
        for name in ("between-del-0001", "between-del-0002", "between-del-0003"):
            box.make_run(name)
        box.prime()

        def patcher(module):
            original = module.delete_candidates

            def wrapped(scan, record):
                real = shutil.rmtree

                def rmtree(*args, **kwargs):
                    real(*args, **kwargs)
                    scan.deadline = 0  # expire right after the first removal
                rmtree.avoids_symlink_attacks = True
                with mock.patch.object(module.shutil, "rmtree", rmtree):
                    return original(scan, record)
            return mock.patch.object(module, "delete_candidates", wrapped)

        code, record = self.run_in_process(patcher)
        self.assertEqual(1, code)
        self.assertEqual("partial-delete-failure", record["outcome"])
        self.assertIn("scan deadline exceeded", record["reason"])
        self.assertEqual(["between-del-0001"], [item["name"] for item in record["deleted"]])
        self.assertEqual(["between-del-0002", "between-del-0003"],
                         [item["name"] for item in record["remaining_candidates"]])
        self.assertEqual({"between-del-0002", "between-del-0003", *FILLERS}, box.names())

    def test_deadline_after_completed_deletion_does_not_report_failure(self) -> None:
        box = self.box
        box.make_run("after-del-000001")
        box.make_run("after-del-000002")
        box.prime()

        def patcher(module):
            original = module.post_totals

            def wrapped(scan, record):
                scan.deadline = 0  # every candidate is already gone here
                return original(scan, record)
            return mock.patch.object(module, "post_totals", wrapped)

        code, record = self.run_in_process(patcher)
        self.assertEqual(set(FILLERS), box.names())
        self.assertEqual([], record["remaining_candidates"])
        self.assertEqual("deleted", record["outcome"], record["reason"])
        self.assertEqual(0, code)

    def test_deadline_after_finished_dry_run_keeps_dry_run_and_opens_gate(self) -> None:
        box = self.box
        box.make_run("after-dry-000001")

        def patcher(module):
            original = module.post_totals

            def wrapped(scan, record):
                scan.deadline = 0  # the dry-run decision is already final here
                return original(scan, record)
            return mock.patch.object(module, "post_totals", wrapped)

        code, record = self.run_in_process(patcher)
        self.assertEqual((0, "dry-run"), (code, record["outcome"]), record["reason"])
        self.assertIn("post_pass", record)
        box.write_config(mode="delete")
        code, _, record = box.run()
        self.assertEqual((0, "deleted"), (code, record["outcome"]), record["reason"])
        self.assertNotIn("after-dry-000001", box.names())

    def test_scaling_guard_hundreds_of_runs_and_thousands_of_files(self) -> None:
        box = self.box
        runs = 400
        for number in range(runs):
            name = f"scale-run-{number:06d}"
            files = {f"part-{index}.json": f'{{"n": {index}}}' for index in range(9)}
            # Each run names its predecessor, so the closure walks a long chain.
            files["link.txt"] = f"scale-run-{number - 1:06d}" if number else "start"
            box.make_run(name, files)
        (box.state / "refs.txt").write_text(f"scale-run-{runs - 1:06d}")
        started = time.monotonic()
        code, _, record = box.run()
        elapsed = time.monotonic() - started
        self.assertEqual((0, "nothing-eligible"), (code, record["outcome"]), record["reason"])
        self.assertEqual(runs + len(FILLERS), record["all"]["count"])
        self.assertGreaterEqual(record["after_closure"]["rounds"], runs)
        # Repaired scan: about 0.4 s here. The pre-repair O(runs x snapshot) metadata
        # pass took about 18 s at this size, so 8 s separates them with wide margin.
        self.assertLess(elapsed, 8.0)


class UnitArtifactTests(unittest.TestCase):
    def test_retention_unit_is_manual_oneshot_sandboxed_like_the_cycle(self) -> None:
        retention = unit(RETENTION_UNIT)
        cycle = unit(CYCLE_UNIT)
        self.assertNotIn("Install", retention)
        service = retention["Service"]
        self.assertEqual("oneshot", service["Type"])
        command = shlex.split(service["ExecStart"])
        self.assertNotIn("flock", " ".join(command))
        self.assertEqual(
            ["/usr/bin/python3",
             "${CLOCKIFY_AUTOPILOT_ROOT}/ops/systemd/user/clockify_runs_retention.py",
             "--config", "%h/.config/serenichron/clockify-runs-retention.json",
             "--release", "${CLOCKIFY_AUTOPILOT_ROOT}"],
            command,
        )
        self.assertEqual(["%h/.config/serenichron/clockify-review-cycle-override.env"],
                         repeated(RETENTION_UNIT, "EnvironmentFile"))
        for key in ("UMask", "NoNewPrivileges", "PrivateTmp", "ProtectSystem", "ProtectHome",
                    "WorkingDirectory", "Restart"):
            self.assertEqual(cycle["Service"][key], service[key], key)
        self.assertEqual({"%h/Work/automation-clockify-sync/runs",
                          "%h/Work/automation-clockify-sync/state"},
                         set(repeated(RETENTION_UNIT, "ReadWritePaths")))
        self.assertEqual(1800, int(service["TimeoutStartSec"]))
        root = [item for item in shlex.split(service["Environment"])
                if item.startswith("CLOCKIFY_AUTOPILOT_ROOT=")]
        cycle_root = [item for item in shlex.split(cycle["Service"]["Environment"])
                      if item.startswith("CLOCKIFY_AUTOPILOT_ROOT=")]
        self.assertEqual(cycle_root, root)
        self.assertFalse((USER_SYSTEMD / "clockify-runs-retention.timer").exists())

    def test_cycle_unit_starts_retention_on_success_only(self) -> None:
        cycle = unit(CYCLE_UNIT)
        self.assertEqual("clockify-runs-retention.service", cycle["Unit"]["OnSuccess"])
        self.assertNotIn("clockify-runs-retention", cycle["Service"]["ExecStopPost"])
        self.assertNotIn("clockify-runs-retention", cycle["Service"]["ExecStart"])


if __name__ == "__main__":
    unittest.main()
