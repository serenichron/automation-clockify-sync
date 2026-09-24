import argparse
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from scripts import clockify_review_run as review
from task3_scenario_contract import assert_scenario_contract


class ResumeTests(unittest.TestCase):
    def source(self, root):
        run = root / "runs" / "source"
        (run / "evidence").mkdir(parents=True)
        for name in ("run-report.json", "evidence/evidence-ledger.json", "period-manifest.json", "routing.json", "review-corrections.jsonl", "review-acceptance.jsonl"):
            (run / name).write_text("{}")
        return run

    def test_resume_rejects_missing_snapshot_before_accounting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "runs" / "source"
            (run / "evidence").mkdir(parents=True)
            (run / "run-report.json").write_text("{}")
            (run / "evidence" / "evidence-ledger.json").write_text("{}")
            with mock.patch.object(review, "RUNS", root / "runs"):
                with self.assertRaisesRegex(ValueError, "snapshot"):
                    review._resume_source(run)

    def test_resume_rejects_replay_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "runs" / "source"
            (run / "evidence").mkdir(parents=True)
            for name in ("run-report.json", "evidence/evidence-ledger.json", "period-manifest.json", "routing.json", "review-corrections.jsonl", "review-acceptance.jsonl"):
                path = run / name
                path.write_text("{}")
            (run / "replay-source.json").write_text("{}")
            with mock.patch.object(review, "RUNS", root / "runs"):
                with self.assertRaisesRegex(ValueError, "replay"):
                    review._resume_source(run)

    def test_resume_rejects_symlink_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "runs" / "source"
            (target / "evidence").mkdir(parents=True)
            for name in ("run-report.json", "evidence/evidence-ledger.json", "period-manifest.json", "routing.json", "review-corrections.jsonl", "review-acceptance.jsonl"):
                (target / name).write_text("{}")
            alias = root / "runs" / "alias"
            alias.symlink_to(target, target_is_directory=True)
            with mock.patch.object(review, "RUNS", root / "runs"):
                with self.assertRaises(ValueError):
                    review._resume_source(alias)

    def test_configured_runs_root_bounds_resume_and_replay_sources(self):
        """Catches replay/resume accepting a sibling release checkout's artifacts."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "operational" / "runs"
            source = self.source(root / "operational")
            outside = self.source(root / "release")
            with mock.patch.object(review, "RUNS", runs):
                self.assertEqual(source.resolve(), review._run_child(source, label="resume source"))
                with self.assertRaisesRegex(ValueError, "direct child"):
                    review._run_child(outside, label="replay source")

    def test_run_child_rejects_symlink_and_lexical_alias_inside_runs_root(self):
        """Catches replay/resume normalizing unsafe aliases before containment."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            source = self.source(root)
            alias = runs / "alias"
            alias.symlink_to(source, target_is_directory=True)
            lexical = runs / "nested" / ".." / "source"
            with mock.patch.object(review, "RUNS", runs):
                for candidate in (alias, lexical):
                    with self.subTest(candidate=candidate):
                        with self.assertRaisesRegex(ValueError, "canonical"):
                            review._run_child(candidate, label="resume source")

    def test_collector_derivation_verifier_rejects_symlinked_attempt(self):
        """A matching attempt symlink must not escape the canonical runs root."""
        with tempfile.TemporaryDirectory() as directory:
            runs = Path(directory) / "runs"
            target = runs / "collector-derivation-target"
            target.mkdir(parents=True)
            alias = runs / "collector-derivation-alias"
            alias.symlink_to(target, target_is_directory=True)
            with mock.patch.object(review, "RUNS", runs):
                with self.assertRaisesRegex(ValueError, "canonical"):
                    review._verified_collector_derivation(alias)

    def test_collector_derivation_discovery_rejects_broken_symlink_candidate(self):
        """Discovery must validate an attempt directory before probing its files."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            source = self.source(root)
            (source / "run-report.md").write_text("collector report\n")
            for name in review._COLLECTOR_EVIDENCE_FILES:
                (source / "evidence" / name).write_text("{}\n")
            collector_runtime = {"git_sha": "a" * 40, "git_dirty": False}
            executor_runtime = {"git_sha": "b" * 40, "git_dirty": False}
            snapshots = {
                name: source / name for name in review._RECONCILIATION_INPUTS.values()
            }
            artifact_paths = {
                "run-report.json": source / "run-report.json",
                "evidence/evidence-ledger.json": source / "evidence/evidence-ledger.json",
                **{
                    f"evidence/{name}": source / "evidence" / name
                    for name in review._COLLECTOR_EVIDENCE_FILES
                },
            }
            artifact_bytes = {
                name: path.read_bytes() for name, path in artifact_paths.items()
            }
            artifact_digests = {
                name: "sha256:" + hashlib.sha256(content).hexdigest()
                for name, content in artifact_bytes.items()
            }
            source_identity = SimpleNamespace(
                source_bundle_digest="sha256:" + "1" * 64,
                slice_id="slice-fixture",
                since_utc="2026-08-31T21:00:00Z",
                until_utc="2026-09-02T21:00:00Z",
                collector_runtime_identity=collector_runtime,
                verified_artifact_bytes=artifact_bytes,
                verified_artifact_digests=artifact_digests,
            )
            lineage = {
                "schema_version": "collector-derivation/v1",
                "source_run_id": source.name,
                "source_bundle_digest": source_identity.source_bundle_digest,
                "collector_runtime_identity": collector_runtime,
                "executor_runtime_identity": executor_runtime,
                "source_artifact_digests": dict(sorted(artifact_digests.items())),
                "snapshot_digests": {
                    name: "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
                    for name, path in sorted(snapshots.items())
                },
            }
            locator = hashlib.sha256(json.dumps(
                lineage, sort_keys=True, separators=(",", ":")
            ).encode()).hexdigest()
            candidate = runs / f"collector-derivation-{locator}-attempt-1"
            candidate.symlink_to(root / "missing-attempt", target_is_directory=True)
            with mock.patch.object(review, "RUNS", runs), mock.patch.object(
                review.collector_receipts,
                "load_collector_source_bundle",
                return_value=source_identity,
            ):
                with self.assertRaisesRegex(ValueError, "canonical"):
                    review._prepare_collector_derivation_run(
                        source,
                        snapshots,
                        executor_runtime_identity=executor_runtime,
                        environment={},
                    )

    def test_collector_derivation_verifier_rejects_symlinked_lineage_file(self):
        """The internal lineage document must be read without following symlinks."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            attempt = runs / "collector-derivation-attempt"
            attempt.mkdir(parents=True)
            outside = root / "outside-lineage.json"
            outside.write_text("{}\n")
            (attempt / "collector-source.json").symlink_to(outside)
            with mock.patch.object(review, "RUNS", runs):
                with self.assertRaisesRegex(ValueError, "symlink"):
                    review._verified_collector_derivation(attempt)

    def test_runs_root_option_is_forwarded_to_collector(self):
        """Catches fresh review collection falling back to the release checkout's runs/."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "operational" / "runs"
            runs.mkdir(parents=True)
            args = review.parse_args([
                "--runs-root", str(runs),
                "--period-manifest", str(root / "manifest.json"),
            ])
            self.assertEqual(runs, args.runs_root)

    def test_reused_collector_bundle_derives_in_immutable_attempt_with_split_runtime_provenance(self):
        """Catches current execution overwriting an older deterministic collector run."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            source = self.source(root)
            (source / "run-report.md").write_text("collector report\n")
            (source / "evidence" / "evidence-ledger.json").write_text(json.dumps({
                "schema_version": "evidence-ledger/v1",
                "manifest": {
                    "manifest_id": "elm-" + "c" * 64,
                    "events_digest": "d" * 64,
                },
                "events": [],
            }) + "\n")
            for name in (
                "clockify-existing.json", "fathom-meetings.json",
                "calendly-recordings.json", "multica-issues.json", "sessions.json",
            ):
                (source / "evidence" / name).write_text("{}\n")
            collector_runtime = {"git_sha": "a" * 40, "git_dirty": False}
            report = {
                "run_id": source.name,
                "runtime_identity": collector_runtime,
                "date_range": {
                    "since": "2026-09-01 00:00", "until": "2026-09-03 00:00",
                    "reason": "fixture",
                },
            }
            (source / "run-report.json").write_text(json.dumps(report) + "\n")
            immutable_before = {
                path.relative_to(source): path.read_bytes()
                for path in source.rglob("*") if path.is_file()
            }
            snapshots = {
                name: source / name for name in review._RECONCILIATION_INPUTS.values()
            }
            artifact_paths = {
                "run-report.json": source / "run-report.json",
                "evidence/evidence-ledger.json": source / "evidence/evidence-ledger.json",
                **{
                    f"evidence/{name}": source / "evidence" / name
                    for name in review._COLLECTOR_EVIDENCE_FILES
                },
            }
            artifact_bytes = {
                name: path.read_bytes() for name, path in artifact_paths.items()
            }
            artifact_digests = {
                name: "sha256:" + hashlib.sha256(content).hexdigest()
                for name, content in artifact_bytes.items()
            }
            source_identity = SimpleNamespace(
                source_bundle_digest="sha256:" + "1" * 64,
                slice_id="slice-fixture",
                since_utc="2026-08-31T21:00:00Z",
                until_utc="2026-09-02T21:00:00Z",
                collector_runtime_identity=collector_runtime,
                verified_artifact_bytes=artifact_bytes,
                verified_artifact_digests=artifact_digests,
            )
            executor_runtime = {"git_sha": "b" * 40, "git_dirty": False}
            invocation = "0123456789abcdef0123456789abcdef"
            with mock.patch.object(review, "RUNS", runs), \
                 mock.patch.object(
                     review.collector_receipts,
                     "load_collector_source_bundle",
                     return_value=source_identity,
                 ):
                first = review._prepare_collector_derivation_run(
                    source,
                    snapshots,
                    executor_runtime_identity=executor_runtime,
                    environment={"INVOCATION_ID": invocation},
                )
                second = review._prepare_collector_derivation_run(
                    source,
                    snapshots,
                    executor_runtime_identity=executor_runtime,
                    environment={"INVOCATION_ID": invocation},
                )
                (first / "quality_report.json").write_text(
                    '{"status":"blocked"}\n'
                )
                (first / "autopilot-result.json").write_text(json.dumps({
                    "run_id": first.name,
                    "run_dir": str(first),
                    "quality_status": "blocked",
                }) + "\n")
                resumed = review._prepare_collector_derivation_run(
                    source,
                    snapshots,
                    executor_runtime_identity=executor_runtime,
                    environment={"INVOCATION_ID": "fedcba9876543210fedcba9876543210"},
                )

            self.assertNotEqual(source, first)
            self.assertNotEqual(first, second)
            self.assertEqual(first, resumed)
            self.assertEqual(
                immutable_before,
                {
                    path.relative_to(source): path.read_bytes()
                    for path in source.rglob("*") if path.is_file()
                },
            )
            lineage = json.loads((first / "collector-source.json").read_text())
            self.assertEqual(collector_runtime, lineage["collector_runtime_identity"])
            self.assertEqual(executor_runtime, lineage["executor_runtime_identity"])
            self.assertEqual(
                "sha256:" + hashlib.sha256(invocation.encode()).hexdigest(),
                lineage["systemd_invocation_digest"],
            )
            derived_report = json.loads((first / "run-report.json").read_text())
            self.assertEqual(collector_runtime, derived_report["collector_runtime_identity"])
            self.assertEqual(executor_runtime, derived_report["executor_runtime_identity"])
            self.assertEqual(executor_runtime, derived_report["runtime_identity"])

            raw_copy = first / "evidence" / "clockify-existing.json"
            raw_bytes = raw_copy.read_bytes()
            raw_copy.write_text('{"mutated":true}\n')
            with mock.patch.object(review, "RUNS", runs), mock.patch.object(
                review.collector_receipts,
                "load_collector_source_bundle",
                return_value=source_identity,
            ):
                with self.assertRaisesRegex(ValueError, "derived artifact"):
                    review._verified_collector_derivation(first)
            raw_copy.write_bytes(raw_bytes)

            final_components = (
                ("autopilot-result.json", review._adopt_completed_collector_derivation),
                ("quality_report.json", review._adopt_completed_collector_derivation),
                ("evidence/evidence-ledger.json", review._verified_collector_derivation),
                ("run-report.json", review._verified_collector_derivation),
            )
            for relative, verifier in final_components:
                with self.subTest(relative=relative):
                    component = first / relative
                    original = component.read_bytes()
                    outside = root / (relative.replace("/", "-") + ".outside")
                    outside.write_bytes(original)
                    component.unlink()
                    component.symlink_to(outside)
                    try:
                        with mock.patch.object(review, "RUNS", runs), mock.patch.object(
                            review.collector_receipts,
                            "load_collector_source_bundle",
                            return_value=source_identity,
                        ):
                            with self.assertRaisesRegex(ValueError, "unsafe|symlink"):
                                verifier(first)
                    finally:
                        component.unlink()
                        component.write_bytes(original)

    def test_fresh_collection_processes_derivation_child_not_collector_source(self):
        """Catches the normal runner writing derived artifacts into the collector bundle."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            source = self.source(root)
            (source / "run-report.md").write_text("collector report\n")
            derived = runs / "derived-attempt"
            derived.mkdir()
            manifest = root / "manifest.json"
            manifest.write_text("{}\n")
            result = derived / "autopilot-result.json"
            snapshots = {
                name: source / name for name in review._RECONCILIATION_INPUTS.values()
            }
            collected = SimpleNamespace(
                returncode=0,
                stdout=str(source / "run-report.md") + "\n",
                stderr="",
            )
            with mock.patch.object(review, "RUNS", runs), \
                 mock.patch.object(review, "_run", return_value=collected), \
                 mock.patch.object(review, "_collector_run_dirs", return_value=(source,)), \
                 mock.patch.object(
                     review, "_snapshot_reconciliation_inputs", return_value=snapshots
                 ), \
                 mock.patch.object(
                     review, "_prepare_collector_derivation_run", return_value=derived
                 ) as prepared, \
                 mock.patch.object(review, "_validated_period_manifest"), \
                 mock.patch.object(review, "_acceptance_gate", return_value={}), \
                 mock.patch.object(review, "_process_run", return_value=(0, result)) as processed:
                code = review.main([
                    "--runs-root", str(runs),
                    "--period-manifest", str(manifest),
                    "--routing", str(source / "routing.json"),
                    "--corrections", str(source / "review-corrections.jsonl"),
                    "--acceptance-ledger", str(source / "review-acceptance.jsonl"),
                ])

            self.assertEqual(0, code)
            prepared.assert_called_once_with(source, snapshots)
            self.assertEqual(derived, processed.call_args.args[1])

    def test_fresh_collection_adopts_terminal_derivation_without_overwrite(self):
        """Catches crash/resume rerunning a terminal derivation attempt in place."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            source = self.source(root)
            derived = runs / "derived-attempt"
            derived.mkdir()
            result = derived / "autopilot-result.json"
            result.write_text('{"quality_status":"blocked"}\n')
            parent_paths = (*sorted(source.rglob("*")), result)
            parent_before = {
                str(path.relative_to(root)): path.read_bytes()
                for path in parent_paths if path.is_file()
            }
            manifest = root / "manifest.json"
            manifest.write_text("{}\n")
            snapshots = {
                name: source / name for name in review._RECONCILIATION_INPUTS.values()
            }
            collected = SimpleNamespace(returncode=0, stdout="ignored", stderr="")
            with mock.patch.object(review, "RUNS", runs), \
                 mock.patch.object(review, "_run", return_value=collected), \
                 mock.patch.object(review, "_collector_run_dirs", return_value=(source,)), \
                 mock.patch.object(
                     review, "_snapshot_reconciliation_inputs", return_value=snapshots
                 ), \
                 mock.patch.object(
                     review, "_prepare_collector_derivation_run", return_value=derived
                 ), \
                 mock.patch.object(
                     review, "_adopt_completed_collector_derivation", return_value=result
                 ) as adopted, \
                 mock.patch.object(review, "_process_run") as processed:
                code = review.main([
                    "--runs-root", str(runs),
                    "--period-manifest", str(manifest),
                    "--routing", str(source / "routing.json"),
                    "--corrections", str(source / "review-corrections.jsonl"),
                    "--acceptance-ledger", str(source / "review-acceptance.jsonl"),
                ])

            self.assertEqual(0, code)
            adopted.assert_called_once_with(derived)
            processed.assert_not_called()
            assert_scenario_contract(
                self,
                stable_ids=[source.name, derived.name],
                parent_before=parent_before,
                parent_after={
                    str(path.relative_to(root)): path.read_bytes()
                    for path in parent_paths if path.is_file()
                },
                emitted_ids=[str(result.relative_to(root))],
                clockify_adapter_calls=processed.call_count,
            )

    def test_forbidden_resume_overrides_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "runs" / "source"
            run.mkdir(parents=True)
            for option in ("--since", "--until", "--analysis-fixture", "--period-manifest", "--routing", "--corrections", "--acceptance-ledger"):
                args = ["--resume-from", str(run), option, "value"]
                self.assertEqual(2, review.main(args), option)

    def test_resume_processes_source_with_its_exact_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self.source(root)
            result = run / "autopilot-result.json"
            with mock.patch.object(review, "RUNS", root / "runs"), \
                 mock.patch.object(review, "_validated_period_manifest"), \
                 mock.patch.object(review, "_acceptance_gate", return_value={}), \
                 mock.patch.object(review, "_process_run", return_value=(0, result)) as processed:
                self.assertEqual(0, review.main(["--resume-from", str(run)]))
            args = processed.call_args.args[0]
            self.assertEqual(run / "period-manifest.json", args.period_manifest)
            self.assertEqual(run / "routing.json", args.routing)
            self.assertEqual(run / "review-corrections.jsonl", args.corrections)
            self.assertEqual(run / "review-acceptance.jsonl", args.acceptance_ledger)

    def test_resume_rejects_boolean_and_replay_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "runs" / "source"
            run.mkdir(parents=True)
            for option in ("--calendly-optional", "--no-enrich", "--replay-from"):
                args = ["--resume-from", str(run), option]
                if option == "--replay-from":
                    args.append(str(run))
                self.assertEqual(2, review.main(args), option)

    def test_passing_result_with_drifted_completion_bundle_rejects(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self.source(root)
            (run / "autopilot-result.json").write_text(json.dumps({"quality_status": "pass"}))
            (run / "review-snapshot.json").write_text("{}")
            with mock.patch.object(review.collector_receipts, "load_completion_bundle", side_effect=ValueError("drift")):
                with self.assertRaisesRegex(ValueError, "cannot be verified"):
                    review._adopt_completed_resume(run)

    def test_verified_adoption_skips_accounting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = self.source(root)
            result = run / "autopilot-result.json"
            result.write_text(json.dumps({"quality_status": "pass"}))
            (run / "review-snapshot.json").write_text("{}")
            bundle = SimpleNamespace(replay=False)
            with mock.patch.object(review, "RUNS", root / "runs"), \
                 mock.patch.object(review.collector_receipts, "load_completion_bundle", return_value=bundle), \
                 mock.patch.object(review.collector_receipts, "completion_coverage", return_value={"status": "complete", "incomplete_sources": []}), \
                 mock.patch.object(review, "_process_run") as processed:
                self.assertEqual(0, review.main(["--resume-from", str(run)]))
            processed.assert_not_called()

    def test_source_debt_recovery_requires_all_three_identity_options(self):
        """Dropping any caller-persisted recovery identity must fail before work."""
        parent = "/tmp/source-parent"
        attempt = "sha256:" + "a" * 64
        complete = [
            "--recover-source-debt-from", parent,
            "--recover-source", "sessions/macbook",
            "--recover-attempt-id", attempt,
        ]
        parsed = review.parse_args(complete)
        self.assertEqual(Path(parent), parsed.recover_source_debt_from)
        self.assertEqual("sessions/macbook", parsed.recover_source)
        self.assertEqual(attempt, parsed.recover_attempt_id)

        for omitted in range(0, len(complete), 2):
            argv = complete[:omitted] + complete[omitted + 2:]
            with self.subTest(omitted=complete[omitted]):
                self.assertEqual(2, review.main(argv))

    def test_source_debt_recovery_processes_derived_run_with_parent_snapshots(self):
        """Recovery must enter its own adapter branch, never fresh/resume collection."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "runs" / "parent"
            derived = root / "runs" / "derived"
            parent.mkdir(parents=True)
            derived.mkdir(parents=True)
            snapshots = {
                name: derived / name for name in review._RECONCILIATION_INPUTS.values()
            }
            for path in snapshots.values():
                path.write_text("{}")
            result = derived / "autopilot-result.json"
            recovery_result = SimpleNamespace(run_dir=derived, parent_run_dir=parent)
            argv = [
                "--recover-source-debt-from", str(parent),
                "--recover-source", "sessions/macbook",
                "--recover-attempt-id", "sha256:" + "a" * 64,
            ]
            with mock.patch.object(review, "RUNS", root / "runs"), \
                 mock.patch.object(review.clockify_source_debt_recover, "recover", return_value=recovery_result) as recovered, \
                 mock.patch.object(review, "_snapshot_recovery_inputs", return_value=snapshots), \
                 mock.patch.object(review, "_adopt_completed_recovery", return_value=None), \
                 mock.patch.object(review, "_validated_period_manifest"), \
                 mock.patch.object(review, "_acceptance_gate", return_value={}), \
                 mock.patch.object(review, "_process_run", return_value=(0, result)) as processed:
                self.assertEqual(0, review.main(argv))
            recovered.assert_called_once_with(parent, "sessions/macbook", "sha256:" + "a" * 64)
            args, actual_run, _gate = processed.call_args.args
            self.assertEqual(derived, actual_run)
            self.assertEqual(derived / "routing.json", args.routing)

    def test_source_debt_recovery_reuses_verified_terminal_result(self):
        """A repeated durable attempt ID must not rerun downstream processing."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            parent = root / "runs" / "parent"
            derived = root / "runs" / "derived"
            parent.mkdir(parents=True)
            derived.mkdir(parents=True)
            result = derived / "autopilot-result.json"
            recovery_result = SimpleNamespace(run_dir=derived, parent_run_dir=parent)
            argv = [
                "--recover-source-debt-from", str(parent),
                "--recover-source", "sessions/macbook",
                "--recover-attempt-id", "sha256:" + "b" * 64,
            ]
            with mock.patch.object(review.clockify_source_debt_recover, "recover", return_value=recovery_result), \
                 mock.patch.object(review, "_snapshot_recovery_inputs", return_value={}), \
                 mock.patch.object(review, "_adopt_completed_recovery", return_value=result), \
                 mock.patch.object(review, "_process_run") as processed:
                self.assertEqual(0, review.main(argv))
            processed.assert_not_called()

    def test_source_debt_recovery_rejects_other_modes_and_overrides(self):
        """Range, replay/resume/repair and reconciliation overrides cannot alter lineage."""
        base = [
            "--recover-source-debt-from", "/tmp/parent",
            "--recover-source", "sessions/macbook",
            "--recover-attempt-id", "sha256:" + "c" * 64,
        ]
        conflicts = (
            ["--resume-from", "/tmp/parent"], ["--replay-from", "/tmp/parent"],
            ["--repair-from", "/tmp/parent"], ["--since", "2026-07-01"],
            ["--until", "2026-07-02"], ["--no-enrich"], ["--calendly-optional"],
            ["--analysis-fixture", "/tmp/fixture"], ["--routing", "/tmp/routing"],
        )
        for conflict in conflicts:
            with self.subTest(conflict=conflict):
                self.assertEqual(2, review.main(base + conflict))

    def test_recovery_required_source_cannot_be_excluded_or_missing(self):
        """Optional-source semantics must never discharge required source debt."""
        bundle = SimpleNamespace()
        coverages = (
            {"incomplete_sources": ["sessions/macbook"], "sources": {"sessions/macbook": {"status": "excluded"}}},
            {"incomplete_sources": ["sessions/macbook"], "sources": {}},
        )
        for coverage in coverages:
            with self.subTest(coverage=coverage), \
                 mock.patch.object(review.collector_receipts, "completion_coverage", return_value=coverage), \
                 self.assertRaises(review.ReviewRunError):
                review._recovery_source_status(bundle, "sessions/macbook")


if __name__ == "__main__":
    unittest.main()
