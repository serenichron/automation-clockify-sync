"""Authentic historical recovery must not replace the original collector receipt."""
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts import clockify_review_cycle as cycle, clockify_review_run as review
from ops.systemd.user import clockify_review_cycle_release as release_helper


class RecoveryHistoricalAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temporary.cleanup)
        cls.root = Path(cls.temporary.name)
        cls.release = cls.root / "releases" / ("1" * 40)
        cls.release.mkdir(parents=True)
        project = Path(__file__).resolve().parents[1]
        shutil.copytree(project / "scripts", cls.release / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
        (cls.release / "routing.json").write_text(json.dumps({"workspace_id": "workspace-1", "member_id": "member-1",
                                                             "skip_rules": {}, "session_routes": [], "meeting_routes": []}))
        (cls.release / "fleet.json").write_text(json.dumps({"machines": [{"name": "macbook", "kind": "ssh", "enabled": True}],
                                                           "ssh_options": []}))
        release_helper._make_payload_read_only(cls.release)
        cls.release.chmod(0o555)
        manifest = release_helper._tree_manifest(cls.release)
        identity = {"schema_version": release_helper.IDENTITY_SCHEMA, "git_sha": cls.release.name,
                    "root": str(cls.release), "routing_sha256": hashlib.sha256((cls.release / "routing.json").read_bytes()).hexdigest(),
                    "tree_manifest": manifest, "tree_digest": release_helper._manifest_digest(manifest)}
        cls.release.chmod(0o755)
        (cls.release / release_helper.IDENTITY_NAME).write_text(json.dumps(identity))
        (cls.release / release_helper.IDENTITY_NAME).chmod(0o444)
        cls.release.chmod(0o555)
        cls.graph = cls.root / "graph"
        cls.graph.mkdir()
        env = {"PATH": "/usr/bin:/bin", "CLOCKIFY_AUTOPILOT_COORDINATOR": "omarchy-precision",
               "CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT": str(cls.graph / "state/collector-checkpoints")}
        script = "import runpy,sys; sys.path.insert(0,sys.argv[1]); runpy.run_path(sys.argv[2])['build'](*sys.argv[1:2],*sys.argv[3:])"
        completed = subprocess.run(["/usr/bin/python3", "-B", "-c", script, str(cls.release),
            str(project / "tests/source_recovery_audit_fixture.py"), str(cls.graph),
            str(project / "tests/test_review_cycle_delivery.py")], env=env, cwd=cls.release,
            capture_output=True, text=True, timeout=30)
        if completed.returncode:
            raise AssertionError(completed.stderr)
        cls.fixture = json.loads((cls.graph / "fixture.json").read_bytes())
        cls.current_runtime = review.clockify_sync_collect.collector_runtime_identity()
        cls.saved = {path: (path.read_bytes(), path.stat().st_mode & 0o777, path.stat().st_mtime_ns)
                     for path in cls.root.rglob("*") if path.is_file()}
        cls.saved_modes = {path: path.stat().st_mode & 0o777 for path in cls.root.rglob("*") if path.is_dir()}

    def tearDown(self):
        for path, mode in self.saved_modes.items():
            path.chmod(mode)
        for path, (content, mode, mtime) in self.saved.items():
            if path.read_bytes() != content or path.stat().st_mode & 0o777 != mode:
                path.chmod(0o600)
                path.write_bytes(content)
                path.chmod(mode)
                os.utime(path, ns=(mtime, mtime))

    def audit(self, *, config_root=None, checkpoint_env=None):
        config = copy.deepcopy(self.fixture["config"])
        config["_runtime_identity"] = self.current_runtime
        if config_root is not None:
            config["root"] = str(config_root)
        with mock.patch.dict(os.environ, {"CLOCKIFY_AUTOPILOT_COORDINATOR": "omarchy-precision",
                "CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT": (str(self.graph / "state/collector-checkpoints")
                                                      if checkpoint_env is None else checkpoint_env)}):
            return cycle.source_interval_coverage_audit(config)

    def test_config_bound_receipt_does_not_require_process_checkpoint_override(self):
        """A reader must not silently use the installed collector's private default."""
        report = self.audit(checkpoint_env="")
        self.assertIn("sessions/macbook", {row["source"] for row in report["intervals"] if row["status"] == "complete"})

    def test_completed_historical_child_projects_inventory_using_exact_original_backlog(self):
        """Treating the child as original collector falsely rejects this authentic graph."""
        before = {path: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
                  for path in self.saved}
        sentinel = self.root / "sentinel"
        with mock.patch.object(review, "RUNS", sentinel):
            report = self.audit()
            self.assertEqual(sentinel, review.RUNS)
        complete = [row for row in report["intervals"] if row["status"] == "complete"]
        self.assertEqual({"clockify", "fathom", "calendly", "multica_issues", "sessions/macbook", "repositories/macbook"},
                         {row["source"] for row in complete})
        self.assertTrue(all(row["since_utc"] == "2026-09-06T21:00:00Z" and
                            row["until_utc"] == "2026-09-08T21:00:00Z" for row in complete))
        self.assertEqual(before, {path: (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
                                  for path in self.saved})

    def change(self, path, mutate):
        path = Path(path)
        value = json.loads(path.read_bytes())
        mutate(value)
        mode = path.stat().st_mode & 0o777
        path.chmod(0o600)
        path.write_text(json.dumps(value) + "\n")
        path.chmod(mode)

    def test_receipt_attempt_debt_parent_and_runtime_tampering_rejected_before_execution(self):
        """No historical code loads until exact protected authorities authenticate."""
        state = self.graph / "state/review-cycle-state.json"
        debt = self.graph / "state/source-coverage.json"
        cases = {
            "receipt_digest": (self.fixture["receipt"], lambda v: v.update(receipt_digest="sha256:" + "0" * 64)),
            "receipt_locator": (self.fixture["receipt"], lambda v: v.update(derived_run_path=str(self.graph / "runs/other"))),
            "attempt": (state, lambda v: v["slices"]["2026-09-07"]["recovery_attempts"][self.fixture["debt_id"]].update(attempt_ordinal=2)),
            "ambiguous_attempt": (state, lambda v: v["slices"]["2026-09-07"]["recovery_attempts"].update(other=copy.deepcopy(
                v["slices"]["2026-09-07"]["recovery_attempts"][self.fixture["debt_id"]]))),
            "debt": (debt, lambda v: v["events"][-1].update(completion_bundle_digest="sha256:" + "0" * 64)),
            "parent": (state, lambda v: v["slices"]["2026-09-07"]["recovery_parents"][self.fixture["debt_id"]].update(bundle_digest="sha256:" + "0" * 64)),
            "no_parent_fallback": (state, lambda v: v["slices"]["2026-09-07"].pop("recovery_parents")),
            "release_identity": (self.release / release_helper.IDENTITY_NAME, lambda v: v.update(root=str(self.graph))),
        }
        for name, (path, change) in cases.items():
            with self.subTest(name=name):
                self.change(path, change)
                with mock.patch.object(cycle.subprocess, "run", side_effect=AssertionError("unauthenticated execution")):
                    with self.assertRaises(cycle.CycleError):
                        self.audit()
                self.tearDown()
        for path, mode in ((Path(self.fixture["receipt"]), 0o600),
                           (Path(self.fixture["receipt"]).parent, 0o755), (self.release, 0o777)):
            with self.subTest(path=path, mode=mode):
                path.chmod(mode)
                with mock.patch.object(cycle.subprocess, "run", side_effect=AssertionError("unauthenticated execution")):
                    with self.assertRaises(cycle.CycleError):
                        self.audit()
                self.tearDown()
        collector_file = self.release / "scripts/clockify_sync_collect.py"
        collector_file.chmod(0o600)
        collector_file.write_bytes(collector_file.read_bytes() + b"\n# drift\n")
        collector_file.chmod(0o444)
        with mock.patch.object(cycle.subprocess, "run", side_effect=AssertionError("unauthenticated execution")):
            with self.assertRaises(cycle.CycleError):
                self.audit()

    def test_authenticated_image_outside_configured_release_store_cannot_execute(self):
        """A self-consistent arbitrary owner-created release is not execution authority."""
        other = self.root / "other-store" / ("2" * 40)
        shutil.copytree(self.release, other)
        other.chmod(0o755)
        identity_path = other / release_helper.IDENTITY_NAME
        identity_path.chmod(0o600)
        identity = json.loads(identity_path.read_bytes())
        identity.update(root=str(other), git_sha=other.name)
        other.chmod(0o555)
        manifest = release_helper._tree_manifest(other)
        identity.update(tree_manifest=manifest, tree_digest=release_helper._manifest_digest(manifest))
        identity_path.write_text(json.dumps(identity))
        identity_path.chmod(0o444)
        release_helper._identity(other, other.name)
        with mock.patch.object(cycle.subprocess, "run", side_effect=AssertionError("cross-store execution")):
            with self.assertRaises(cycle.CycleError):
                self.audit(config_root=other)

    def test_marker_snapshot_ledger_and_original_backlog_tampering_are_rejected(self):
        """Reconstructed native proofs remain mandatory, including unchanged parent receipt."""
        child = Path(self.fixture["child"])
        locator = child.name.removeprefix("source-debt-recovery-")
        marker = self.graph / "state/collector-checkpoints/source-debt-recovery" / locator / "attempt-marker.json"
        cases = ((marker, lambda v: v.update(run_dir=str(self.graph / "runs/other"))),
                 (child / "ledger-recovery.json", lambda v: v.update(parent_bundle_digest="sha256:" + "0" * 64)),
                 (child / "evidence/evidence-ledger.json", lambda v: v["manifest"].update(events_digest="0" * 64)),
                 (child / "routing.json", lambda v: v.update(member_id="other")),
                 (self.fixture["backlog"], lambda v: v["completed"][0].update(result_path=str(child / "completion-bundle.json"))))
        for path, change in cases:
            with self.subTest(path=path):
                self.change(path, change)
                with self.assertRaises(cycle.CycleError):
                    self.audit()
                self.tearDown()

    def test_fresh_validation_still_rejects_historical_runtime(self):
        """Historical reader cannot authorize an old runtime for a fresh/current stage."""
        stage = json.loads((self.graph / "state/review-cycle-state.json").read_bytes())["slices"]["2026-09-07"]["source"]
        config = {**self.fixture["config"], "_runtime_identity": self.current_runtime}
        with cycle._native_adoption_runs_config(config, None):
            with self.assertRaisesRegex(cycle.CycleError, "current runtime"):
                cycle._validate_stage(config, Path(stage["result_path"]), "2026-09-07", "2026-09-09",
                                      replay=False, expected_snapshot_digests=stage["snapshot_digests"])

    def test_historical_native_failure_malformed_extra_or_excess_output_fails_closed(self):
        """A failed child or extra protocol fields never replace native provenance."""
        for code, output in ((1, ""), (0, "{}"), (0, "x" * 4097)):
            with self.subTest(code=code, output_length=len(output)), mock.patch.object(cycle.subprocess, "run",
                    return_value=subprocess.CompletedProcess([], code, stdout=output)):
                with self.assertRaises(cycle.CycleError):
                    self.audit()
