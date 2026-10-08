"""Explicit repair must retire only authenticated stale recovery derivatives."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import shutil
import unittest
from unittest import mock

from scripts import clockify_review_cycle as cycle, clockify_review_run as review
import test_review_cycle_source_debt_end_to_end as native
import test_source_audit_recovery_history as historical


class FinishedRecoveryRepairTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Reuse the immutable producer/native recovery builder, not its tests.
        historical.RecoveryHistoricalAuditTests.setUpClass.__func__(cls)
        cls.config = copy.deepcopy(cls.fixture["config"])
        cls.config.update(spreadsheet_id="sheet-1", monthly_sheet_title_template="{month_name} {year} portfolio review",
                          _runtime_identity=cls.current_runtime)
        cls.state_path = cls.graph / "state/review-cycle-state.json"
        cls.debt_path = cls.graph / "state/source-coverage.json"
        state = json.loads(cls.state_path.read_bytes())
        record = state["slices"]["2026-09-07"]
        parent = record["source_parent"]
        cls.replay_helper = native.ReviewCycleSourceDebtEndToEndTests("test_three_slice_lifecycle_uses_real_recovery_verifier_and_converges")
        cls.replay_helper.root = cls.graph
        cls.replay_helper.state_dir = cls.graph / "state"
        with cycle._native_adoption_runs_config(cls.config, None):
            replay_path = cls.make_replay(Path(parent["run_dir"]), 1)
            replay = cycle._validate_stage(cls.config, replay_path, "2026-09-07", "2026-09-09",
                replay=True, expected_snapshot_digests=parent["snapshot_digests"],
                source_run_id=parent["run_id"], source_run_dir=parent["run_dir"],
                allow_historical_runtime=True, historical_state_validation=True,
                expected_runtime_digest=parent["runtime_identity_digest"])
            returned = {"schema_version": "review-cycle-replay-return/v1", "source_digest": cycle._value_digest(parent),
                        "command_digest": cycle._value_digest(cycle._replay_command(cls.config, Path(parent["run_dir"]))),
                        "result_path": str(replay_path), "result_digest": cycle._digest(replay_path)}
            returned["return_digest"] = cycle._value_digest(returned)
            receipt = cycle._delivery_document(cls.config, "2026-09-07", "2026-09-09", parent, replay,
                                              sheet_title="September 2026 portfolio review")
            publication = cls.graph / "state/partial-publication-receipts/prior.json"
            cycle._write_delivery_receipt(publication, receipt)
        source = record["source"]
        record.update(status="source_verified", replay=replay, replay_return=returned,
                      source_run_id=parent["run_id"], replay_run_id=replay["run_id"], review_ids=parent["review_ids"],
                      publication_receipt=str(publication), source_completeness=source["coverage"],
                      exception_ids=source["exception_ids"], exceptions_complete=not source["exception_ids"],
                      period_manifest=str(cls.graph / "state/2026-09-07.period-manifest.json"),
                      expected_snapshot_digests=source["snapshot_digests"])
        state["slices"]["2026-09-09"] = {"until": "2026-09-11", "status": "incomplete", "preserve": {"arbitrary": [1, 2]}}
        cycle._atomic(cls.state_path, state)
        cls.initial_state = state
        cls.saved = {path: (path.read_bytes(), path.stat().st_mode & 0o777, path.stat().st_mtime_ns)
                     for path in cls.root.rglob("*") if path.is_file()}
        cls.receipt_root = cls.graph / "state/source-recovery-repair-receipts"

    @classmethod
    def make_replay(cls, source, number):
        real_template = native.make_run
        def exact_template(*args, **kwargs):
            kwargs["proposals"] = json.loads((source / "proposals.json").read_bytes())
            kwargs["ledger_from"] = source
            return real_template(*args, **kwargs)
        # Match the real source's empty work and full native inventory instead
        # of this older helper's one-row/complete-coverage defaults.
        with mock.patch.object(native, "make_run", side_effect=exact_template):
            return cls.replay_helper._make_replay(source, number)

    def setUp(self):
        for path, (content, mode, mtime) in self.saved.items():
            if path.read_bytes() != content or path.stat().st_mode & 0o777 != mode:
                path.chmod(0o600)
                path.write_bytes(content)
                path.chmod(mode)
                os.utime(path, ns=(mtime, mtime))
        if self.receipt_root.exists():
            shutil.rmtree(self.receipt_root)
        self.request = {"schema_version": "review-cycle-finished-recovery-repair-request/v1",
                        "since": "2026-09-07", "until": "2026-09-09",
                        "attempt_id": self.initial_state["slices"]["2026-09-07"]["recovery_attempts"][self.fixture["debt_id"]]["attempt_id"],
                        "source_stage_digest": cycle._value_digest(self.initial_state["slices"]["2026-09-07"]["source"]),
                        "expected_state_sha256": cycle._digest(self.state_path),
                        "expected_debt_sha256": cycle._digest(self.debt_path)}

    def repair(self, *, apply=False, request=None):
        operation = getattr(cycle, "repair_finished_recovery", None)
        self.assertTrue(callable(operation), "native explicit finished-recovery repair operation is missing")
        with mock.patch.dict(os.environ, {"CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT": "", "CLOCKIFY_AUTOPILOT_COORDINATOR": "omarchy-precision"}):
            return operation(self.config, self.request if request is None else request, apply=apply)

    def immutable(self):
        return {path: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
                for path in self.saved}

    def test_default_plan_authenticates_real_foreign_replay_without_any_write(self):
        """Catches plan mode creating a lock/receipt or failing to prove the old graph."""
        record = self.initial_state["slices"]["2026-09-07"]
        with cycle._native_adoption_runs_config(self.config, None):
            self.assertEqual("blocked", review.derive_replay_integrity(Path(record["source"]["run_dir"]), Path(record["replay"]["run_dir"]))["status"])
        before = self.immutable()
        with mock.patch.object(cycle, "single_instance", side_effect=AssertionError("plan created a lock")):
            result = self.repair()
        self.assertEqual("plan", result["status"])
        self.assertEqual(before, self.immutable())
        self.assertFalse(self.receipt_root.exists())
        receipt = result["receipt"]
        self.assertEqual(self.request["expected_state_sha256"], receipt["before_state_sha256"])
        self.assertEqual(record["source_parent"], receipt["prior_history"]["source"])
        for field in ("replay", "replay_return", "publication_receipt", "source_run_id", "replay_run_id", "review_ids"):
            self.assertEqual(record[field], receipt["prior_history"][field])

    def test_apply_archives_exact_old_publication_preserving_source_debt_and_other_state(self):
        """Catches repair altering the finished child, debt, exceptions or another slice."""
        before = self.immutable()
        result = self.repair(apply=True)
        self.assertEqual("repaired", result["status"])
        state = json.loads(self.state_path.read_bytes())
        prior, current = self.initial_state["slices"]["2026-09-07"], state["slices"]["2026-09-07"]
        for field in ("source", "source_parent", "source_completeness", "exception_ids", "exceptions_complete", "recovery_attempts", "expected_snapshot_digests"):
            self.assertEqual(prior[field], current[field])
        for field in ("replay", "replay_return", "publication_receipt", "source_run_id", "replay_run_id", "review_ids"):
            self.assertNotIn(field, current)
        self.assertEqual("source_verified", current["status"])
        self.assertEqual([result["receipt"]["prior_history"]], current["source_recovery_history"])
        for key in self.initial_state:
            if key != "slices":
                self.assertEqual(self.initial_state[key], state[key])
        self.assertEqual(self.initial_state["slices"]["2026-09-09"], state["slices"]["2026-09-09"])
        self.assertEqual({p:v for p,v in before.items() if p != self.state_path},
                         {p:v for p,v in self.immutable().items() if p != self.state_path})
        receipt_path = Path(result["receipt_path"])
        self.assertEqual(0o444, receipt_path.stat().st_mode & 0o777)
        self.assertEqual(0o700, receipt_path.parent.stat().st_mode & 0o777)
        self.assertEqual(cycle._digest(self.state_path), result["receipt"]["after_state_sha256"])

    def test_exact_repeat_is_noop_and_never_rewrites_receipt(self):
        """Catches a duplicate explicit repair appending history or rewriting evidence."""
        first = self.repair(apply=True)
        path = Path(first["receipt_path"])
        before = (self.immutable(), path.read_bytes(), path.stat().st_mtime_ns)
        second = self.repair(apply=True)
        self.assertEqual("already_repaired", second["status"])
        self.assertEqual(before, (self.immutable(), path.read_bytes(), path.stat().st_mtime_ns))

    def test_state_debt_and_source_cas_fail_before_historical_execution(self):
        """Catches an unconstrained request authenticating or changing a different state."""
        for field in ("expected_state_sha256", "expected_debt_sha256", "source_stage_digest"):
            request = {**self.request, field: "sha256:" + "0" * 64}
            before = self.immutable()
            with mock.patch.object(cycle.subprocess, "run", side_effect=AssertionError("CAS did not gate execution")):
                with self.assertRaises(cycle.CycleError):
                    self.repair(apply=True, request=request)
            self.assertEqual(before, self.immutable())
            self.assertFalse(self.receipt_root.exists())

    def test_newer_active_replay_is_never_reclassified_as_stale(self):
        """Catches repairing a newer replay simply because old publication fields remain."""
        state = copy.deepcopy(self.initial_state)
        record = state["slices"]["2026-09-07"]
        source = record["source"]
        with cycle._native_adoption_runs_config(self.config, None):
            path = self.make_replay(Path(source["run_dir"]), 2)
            record["replay"] = cycle._validate_stage(self.config, path, "2026-09-07", "2026-09-09", replay=True,
                expected_snapshot_digests=source["snapshot_digests"], source_run_id=source["run_id"], source_run_dir=source["run_dir"],
                allow_historical_runtime=True, historical_state_validation=True, expected_runtime_digest=source["runtime_identity_digest"])
        cycle._atomic(self.state_path, state)
        request = {**self.request, "expected_state_sha256": cycle._digest(self.state_path)}
        before = self.immutable()
        with self.assertRaisesRegex(cycle.CycleError, "exact source run"):
            self.repair(apply=True, request=request)
        self.assertEqual(before, self.immutable())

    def test_atomic_state_crash_retry_uses_same_immutable_receipt(self):
        """Catches a receipt/state crash losing history or requiring a fresh request."""
        real_atomic = cycle._atomic
        def stop_before_state(path, value):
            if path == self.state_path:
                raise RuntimeError("before repair state commit")
            return real_atomic(path, value)
        before = self.state_path.read_bytes()
        with mock.patch.object(cycle, "_atomic", side_effect=stop_before_state):
            with self.assertRaisesRegex(RuntimeError, "before repair state commit"):
                self.repair(apply=True)
        self.assertEqual(before, self.state_path.read_bytes())
        receipt_path, = self.receipt_root.glob("*.json")
        receipt_before = (receipt_path.read_bytes(), receipt_path.stat().st_mtime_ns)
        self.repair(apply=True)
        self.assertEqual(receipt_before, (receipt_path.read_bytes(), receipt_path.stat().st_mtime_ns))

    def test_crash_after_atomic_commit_is_exact_repeat_noop(self):
        """Catches a completed state write being applied twice after process interruption."""
        real_atomic = cycle._atomic
        def stop_after_state(path, value):
            real_atomic(path, value)
            if path == self.state_path:
                raise RuntimeError("after repair state commit")
        with mock.patch.object(cycle, "_atomic", side_effect=stop_after_state):
            with self.assertRaisesRegex(RuntimeError, "after repair state commit"):
                self.repair(apply=True)
        before = self.immutable()
        self.assertEqual("already_repaired", self.repair(apply=True)["status"])
        self.assertEqual(before, self.immutable())

    def test_receipt_seal_failure_cannot_commit_state(self):
        """Catches state becoming repaired before its immutable receipt is durable."""
        before = self.immutable()
        with mock.patch.object(cycle.os, "link", side_effect=OSError("receipt seal interrupted")):
            with self.assertRaisesRegex(OSError, "receipt seal interrupted"):
                self.repair(apply=True)
        self.assertEqual(before, self.immutable())
        self.assertEqual([], list(self.receipt_root.glob("*.json")))
        self.assertEqual("repaired", self.repair(apply=True)["status"])

    def test_crash_after_receipt_link_retry_fsyncs_receipt_directory_before_state(self):
        """A surviving link must be made durable before retry commits state."""
        real_fsync = cycle.os.fsync
        before = self.state_path.read_bytes()
        def fail_receipt_directory(descriptor):
            if self.receipt_root.exists() and os.fstat(descriptor).st_ino == self.receipt_root.stat().st_ino:
                raise OSError("receipt directory durability interrupted")
            return real_fsync(descriptor)
        with mock.patch.object(cycle.os, "fsync", side_effect=fail_receipt_directory):
            with self.assertRaisesRegex(OSError, "receipt directory durability interrupted"):
                self.repair(apply=True)
        self.assertEqual(before, self.state_path.read_bytes())
        receipt, = self.receipt_root.glob("*.json")
        sealed = (receipt.read_bytes(), receipt.stat().st_mtime_ns)
        receipt_synced = []
        def record_sync(descriptor):
            if os.fstat(descriptor).st_ino == self.receipt_root.stat().st_ino:
                receipt_synced.append(True)
            return real_fsync(descriptor)
        real_atomic = cycle._atomic
        def require_durable_receipt(path, value):
            if path == self.state_path:
                self.assertTrue(receipt_synced, "retry must fsync the surviving receipt link before state")
            return real_atomic(path, value)
        with mock.patch.object(cycle.os, "fsync", side_effect=record_sync), mock.patch.object(cycle, "_atomic", side_effect=require_durable_receipt):
            self.assertEqual("repaired", self.repair(apply=True)["status"])
        self.assertEqual(sealed, (receipt.read_bytes(), receipt.stat().st_mtime_ns))

    def test_cas_is_rechecked_after_authentication(self):
        """Catches committing a plan after concurrent state changed during validation."""
        original = cycle._audit_recovered_source
        def change_after_proof(*args, **kwargs):
            value = original(*args, **kwargs)
            changed = copy.deepcopy(self.initial_state)
            changed["slices"]["2026-09-09"]["newer"] = True
            cycle._atomic(self.state_path, changed)
            return value
        with mock.patch.object(cycle, "_audit_recovered_source", side_effect=change_after_proof):
            with self.assertRaisesRegex(cycle.CycleError, "CAS"):
                self.repair(apply=True)
        self.assertTrue(json.loads(self.state_path.read_bytes())["slices"]["2026-09-09"]["newer"])
        self.assertFalse(self.receipt_root.exists())

    def test_apply_requires_same_native_lock(self):
        """Catches an explicit repair racing the ordinary cycle's single-instance lock."""
        before = self.immutable()
        with cycle.single_instance(self.graph / "state/review-cycle.lock") as acquired:
            self.assertTrue(acquired)
            self.assertEqual("locked", self.repair(apply=True)["status"])
        self.assertEqual(before, self.immutable())

    def test_receipt_tamper_or_writable_mode_blocks_exact_repeat(self):
        """Catches a repeat trusting an edited or unsealed local receipt."""
        result = self.repair(apply=True)
        path = Path(result["receipt_path"])
        original = path.read_bytes()
        path.chmod(0o600)
        before = self.immutable()
        with self.assertRaisesRegex(cycle.CycleError, "unsafe"):
            self.repair(apply=True)
        self.assertEqual(before, self.immutable())
        edited = json.loads(original)
        edited["source_bundle_digest"] = "sha256:" + "0" * 64
        path.write_text(json.dumps(edited))
        path.chmod(0o444)
        with self.assertRaisesRegex(cycle.CycleError, "integrity"):
            self.repair(apply=True)
        self.assertEqual(before, self.immutable())

    def test_newer_state_after_repair_blocks_old_request_before_execution(self):
        """Catches exact repeat overwriting unrelated state updated after repair."""
        result = self.repair(apply=True)
        path = Path(result["receipt_path"])
        receipt_before = (path.read_bytes(), path.stat().st_mtime_ns)
        state = json.loads(self.state_path.read_bytes())
        state["slices"]["2026-09-09"]["newer"] = True
        cycle._atomic(self.state_path, state)
        before = self.immutable()
        with mock.patch.object(cycle.subprocess, "run", side_effect=AssertionError("newer state reached historical execution")):
            with self.assertRaisesRegex(cycle.CycleError, "CAS"):
                self.repair(apply=True)
        self.assertEqual(before, self.immutable())
        self.assertEqual(receipt_before, (path.read_bytes(), path.stat().st_mtime_ns))

    def test_cli_defaults_to_plan_and_rejects_mixed_scheduling_modes(self):
        """Catches explicit repair invoking ordinary scheduling or requiring apply by default."""
        self.assertTrue(callable(getattr(cycle, "repair_finished_recovery", None)), "native explicit finished-recovery repair operation is missing")
        config_path, request_path = self.graph / "config.json", self.graph / "request.json"
        config_path.write_text(json.dumps({k:v for k,v in self.config.items() if k != "_runtime_identity"}))
        request_path.write_text(json.dumps(self.request))
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.object(cycle, "run_cycle", side_effect=AssertionError("ordinary cycle invoked")):
            self.assertEqual(0, cycle.main(["--config", str(config_path), "--repair-finished-recovery-request", str(request_path)]))
        self.assertEqual("plan", json.loads(output.getvalue())["status"])
        self.assertFalse(self.receipt_root.exists())
        for extra in (["--enable-sheet-write"], ["--audit-coverage-output", str(self.graph / "audit.json")],
                      ["--adopt-historical-request", str(request_path)]):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(2, cycle.main(["--config", str(config_path), "--repair-finished-recovery-request", str(request_path), *extra]))
