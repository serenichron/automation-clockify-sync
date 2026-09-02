from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import csv
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from scripts import collector_receipts, reconciliation_manifest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "clockify_review_run.py"
SPEC = importlib.util.spec_from_file_location("clockify_review_run", SCRIPT)
review_run = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(review_run)


def item(item_id: str, description: str) -> dict:
    return {
        "id": item_id,
        "client_project": "Serenichron Level 2",
        "description": description,
    }


def bundle_manifest() -> dict:
    return {
        "schema_version": "clockify-semantic-evidence-bundle/v1",
        "digest": review_run.semantic_analyzer.stable_digest("sebm-", [], length=64),
        "bundles": [],
    }


def accounting_result(*, proposal_id: str = "P001") -> dict:
    return {
        "schema_version": 1,
        "allocation_mode": "non_overlapping_v1",
        "ledger_manifest": {},
        "semantic_analysis": {},
        "proposals": [{"id": proposal_id}],
        "ambiguous": [],
        "skipped": [],
        "allocation": {},
        "fathom_reconciliation": [],
        "correction_regression": {},
        "external_writes": False,
    }


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


class ReviewRunResultTests(unittest.TestCase):
    def test_finalization_records_only_a_verified_downstream_bundle(self):
        """Collector output stays pending until all downstream artifacts bind one slice."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "runs" / "run-1"
            run_dir.mkdir(parents=True)
            slice_ = review_run.clockify_sync_collect.plan_slices(
                dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
                dt.datetime(2026, 8, 2, tzinfo=dt.timezone.utc),
                zone=review_run.clockify_sync_collect.BUCHAREST,
            )[0]
            identity = review_run.clockify_sync_collect.BacklogIdentity(
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
                "since_utc": review_run.clockify_sync_collect.iso_utc(slice_.since),
                "until_utc": review_run.clockify_sync_collect.iso_utc(slice_.until),
            }) + "\n", encoding="utf-8")

            with mock.patch.dict(os.environ, {"CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT": str(root / "checkpoints")}):
                bundle = review_run._finalize_backlog_completion(run_dir)
                # Simulate the interruption point before the runner persists
                # its separate source-debt completion, then replay finalization.
                replayed_bundle = review_run._finalize_backlog_completion(run_dir)
                state = review_run.clockify_sync_collect.BacklogStore(root / "checkpoints").open(
                    identity, (slice_,)
                )

            self.assertEqual(
                "sha256:" + hashlib.sha256((run_dir / "completion-bundle.json").read_bytes()).hexdigest(),
                state.completed[0].result_digest,
            )
            self.assertEqual(run_dir / "completion-bundle.json", state.completed[0].result_path)
            self.assertEqual(bundle.bundle_digest, replayed_bundle.bundle_digest)
            self.assertEqual(1, len(state.completed))

    def test_collector_run_dirs_rejects_incomplete_report_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            run_dir = runs / "20260816T120000Z"
            report_path = run_dir / "run-report.md"
            report_path.parent.mkdir(parents=True)
            report_path.write_text("# receipt\n", encoding="utf-8")
            (run_dir / "run-report.json").write_text(json.dumps({
                "evidence_ledger": {
                    "source_completeness": {"status": "incomplete"},
                },
            }) + "\n", encoding="utf-8")
            ledger_path = run_dir / "evidence" / "evidence-ledger.json"
            ledger_path.parent.mkdir()
            ledger_path.write_text(json.dumps({
                "manifest": {
                    "source_completeness": {"status": "complete"},
                },
            }) + "\n", encoding="utf-8")

            with mock.patch.object(review_run, "RUNS", runs):
                with self.assertRaisesRegex(ValueError, "not complete"):
                    review_run._collector_run_dirs(str(report_path))

    def test_collector_run_dirs_rejects_incomplete_ledger_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            run_dir = runs / "20260816T120000Z"
            report_path = run_dir / "run-report.md"
            report_path.parent.mkdir(parents=True)
            report_path.write_text("# receipt\n", encoding="utf-8")
            (run_dir / "run-report.json").write_text(json.dumps({
                "evidence_ledger": {
                    "source_completeness": {"status": "complete"},
                },
            }) + "\n", encoding="utf-8")
            ledger_path = run_dir / "evidence" / "evidence-ledger.json"
            ledger_path.parent.mkdir()
            ledger_path.write_text(json.dumps({
                "manifest": {
                    "source_completeness": {"status": "incomplete"},
                },
            }) + "\n", encoding="utf-8")

            with mock.patch.object(review_run, "RUNS", runs):
                with self.assertRaisesRegex(ValueError, "not complete"):
                    review_run._collector_run_dirs(str(report_path))

    def test_collector_run_dirs_accepts_non_coordinator_peer_coverage_debt(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            run_dir = runs / "20260816T120000Z"
            report_path = run_dir / "run-report.md"
            report_path.parent.mkdir(parents=True)
            report_path.write_text("# receipt\n", encoding="utf-8")
            completeness = {
                "status": "incomplete",
                "incomplete_sources": ["sessions/macbook", "repositories/desktop"],
            }
            (run_dir / "run-report.json").write_text(json.dumps({
                "collection_mode": {
                    "calendly_optional": True,
                    "coordinator": "omarchy-precision",
                },
                "evidence": {
                    "calendly": {"status": "excluded", "complete": True},
                },
                "evidence_ledger": {"source_completeness": completeness},
            }) + "\n", encoding="utf-8")
            ledger_path = run_dir / "evidence" / "evidence-ledger.json"
            ledger_path.parent.mkdir()
            ledger_path.write_text(json.dumps({
                "manifest": {"source_completeness": completeness},
            }) + "\n", encoding="utf-8")

            with mock.patch.object(review_run, "RUNS", runs):
                self.assertEqual((run_dir,), review_run._collector_run_dirs(str(report_path)))

    def test_parse_args_accepts_bounded_optional_calendly_override(self):
        args = review_run.parse_args(["--calendly-optional"])
        self.assertTrue(args.calendly_optional)

    def test_fresh_run_requires_period_manifest_before_collector(self):
        """Catches collection starting without an auditable period identity."""
        collected = subprocess.CompletedProcess(
            args=["collector"], returncode=0, stdout="", stderr=""
        )
        stderr = io.StringIO()
        with mock.patch.object(review_run, "_run", return_value=collected) as run, \
                redirect_stderr(stderr):
            code = review_run.main([])

        self.assertEqual(2, code)
        self.assertIn("--period-manifest", stderr.getvalue())
        run.assert_not_called()

    def test_replay_rejects_external_reconciliation_overrides_before_source_access(self):
        """Catches replay consuming mutable caller inputs instead of source snapshots."""
        for option, value in (
            ("--period-manifest", "/tmp/other-period-manifest.json"),
            ("--routing", "/tmp/other-routing.json"),
            ("--corrections", "/tmp/other-corrections.jsonl"),
            ("--acceptance-ledger", "/tmp/other-acceptance.jsonl"),
        ):
            with self.subTest(option=option), mock.patch.object(
                review_run, "_run_child", side_effect=ValueError("source accessed")
            ) as source_access, redirect_stderr(io.StringIO()):
                code = review_run.main(["--replay-from", "/tmp/source", option, value])

            self.assertEqual(2, code)
            source_access.assert_not_called()

    def test_completed_slices_are_processed_before_later_collection_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            inputs = Path(tmp) / "inputs"
            self._write_reconciliation_snapshots(inputs)
            first = runs / "20260816T120000Z"
            second = runs / "20260816T130000Z"
            for run_dir in (first, second):
                run_dir.mkdir(parents=True)
                (run_dir / "run-report.json").write_text(json.dumps({
                    "evidence": {
                        "calendly": {"status": "ok", "complete": True},
                    },
                    "evidence_ledger": {
                        "source_completeness": {
                            "status": "complete", "incomplete_sources": [],
                        },
                    },
                }) + "\n", encoding="utf-8")
                (run_dir / "run-report.md").write_text("# receipt\n", encoding="utf-8")
                ledger_path = run_dir / "evidence" / "evidence-ledger.json"
                ledger_path.parent.mkdir()
                ledger_path.write_text(json.dumps({
                    "manifest": {
                        "source_completeness": {
                            "status": "complete", "incomplete_sources": [],
                        },
                    },
                }) + "\n", encoding="utf-8")
            first_result = first / "autopilot-result.json"
            second_result = second / "autopilot-result.json"
            collected = subprocess.CompletedProcess(
                ["collector"],
                2,
                stdout=(
                    f"{first / 'run-report.md'}\n"
                    f"{second / 'run-report.md'}\n"
                ),
                stderr="third slice incomplete",
            )
            output = io.StringIO()
            with mock.patch.object(review_run, "RUNS", runs), mock.patch.object(
                review_run, "_run", return_value=collected
            ), mock.patch.object(
                review_run,
                "_process_run",
                side_effect=[(0, first_result), (0, second_result)],
            ) as process_run, redirect_stdout(output):
                code = review_run.main([
                    "--period-manifest", str(inputs / "period-manifest.json"),
                    "--routing", str(inputs / "routing.json"),
                    "--state", str(Path(tmp) / "state.json"),
                    "--corrections", str(inputs / "review-corrections.jsonl"),
                    "--acceptance-ledger", str(inputs / "review-acceptance.jsonl"),
                ])

        self.assertEqual(2, code)
        self.assertEqual(
            [first, second],
            [call.args[1] for call in process_run.call_args_list],
        )
        self.assertEqual(
            [str(first_result), str(second_result)], output.getvalue().splitlines()
        )

    def test_analysis_versions_recurses_partition_recovery_children(self):
        document = {
            "schema_version": 1,
            "prompt_version": "prompt-v1",
            "evidence_bundle_schema_version": "bundle-v1",
            "activities": [],
            "analysis_chunks": [{
                "endpoint": "partition-recovery",
                "event_count": 2,
                "partition_path": "root",
                "partition_depth": 0,
                "recovery_status": "recovered_by_partition",
                "recovery": {
                    "status": "recovered",
                    "path": "root",
                    "depth": 0,
                    "children": [
                        {"model": "model-a", "tier": "primary", "event_count": 1, "partition_path": "root.a", "partition_depth": 1},
                        {"model": "model-b", "tier": "fallback", "event_count": 1, "partition_path": "root.b", "partition_depth": 1},
                    ]
                },
            }],
        }

        versions = [json.loads(value) for value in review_run._analysis_versions(document)]

        self.assertEqual(
            [("model-a", "primary"), ("model-b", "fallback")],
            [(value["model"], value["tier"]) for value in versions],
        )

    def test_analysis_versions_rejects_malformed_partition_tree(self):
        with self.assertRaisesRegex(ValueError, "path or depth"):
            review_run._analysis_versions({
                "schema_version": 1,
                "prompt_version": "prompt-v1",
                "evidence_bundle_schema_version": "bundle-v1",
                "activities": [],
                "analysis_chunks": [{
                    "event_count": 2,
                    "partition_path": "root",
                    "partition_depth": 0,
                    "recovery_status": "recovered_by_partition",
                    "recovery": {
                        "status": "recovered",
                        "path": "root",
                        "depth": 0,
                        "children": [
                            {"model": "a", "tier": "primary", "event_count": 1, "partition_path": "wrong", "partition_depth": 1},
                            {"model": "b", "tier": "fallback", "event_count": 1, "partition_path": "root.b", "partition_depth": 1},
                        ],
                    },
                }],
            })

    def test_immutable_replay_copies_ledger_and_sealed_analysis_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            source = runs / "source-run"
            (source / "evidence").mkdir(parents=True)
            ledger = {
                "schema_version": "evidence-ledger/v1",
                "manifest": {
                    "manifest_id": "elm-" + "a" * 64,
                    "events_digest": "b" * 64,
                },
                "events": [],
            }
            (source / "evidence" / "evidence-ledger.json").write_text(
                json.dumps(ledger, sort_keys=True) + "\n", encoding="utf-8"
            )
            (source / "run-report.json").write_text(
                json.dumps({"run_id": "source-run"}) + "\n", encoding="utf-8"
            )
            (source / "run-report.md").write_text("# source\n", encoding="utf-8")
            (source / "semantic-analysis.json").write_text(
                json.dumps({
                    "schema_version": 1,
                    "prompt_version": "prompt-v1",
                    "evidence_bundle_schema_version": "clockify-semantic-evidence-bundle/v1",
                    "evidence_bundle_manifest": bundle_manifest(),
                    "ledger_evidence_digest": "sha256:" + "c" * 64,
                    "activities": [{
                        "analyzer_model": "model-a",
                        "analyzer_tier": "primary",
                    }],
                    "analysis_chunks": [],
                }) + "\n",
                encoding="utf-8",
            )
            (source / "work-accounting-result.json").write_text(
                json.dumps(accounting_result(), sort_keys=True) + "\n", encoding="utf-8"
            )
            self._write_reconciliation_snapshots(source)

            with mock.patch.object(review_run, "RUNS", runs):
                replay = review_run._prepare_replay_run(source)

            self.assertNotEqual(source, replay)
            self.assertEqual(
                (source / "evidence" / "evidence-ledger.json").read_bytes(),
                (replay / "evidence" / "evidence-ledger.json").read_bytes(),
            )
            provenance = json.loads((replay / "replay-source.json").read_text(encoding="utf-8"))
            self.assertEqual("source-run", provenance["source_run_id"])
            self.assertEqual("elm-" + "a" * 64, provenance["source_manifest_id"])
            self.assertIn("work_accounting_result_sha256", provenance)
            fixture = replay / provenance["semantic_analysis_fixture"]
            self.assertEqual(
                (source / "semantic-analysis.json").read_bytes(),
                fixture.read_bytes(),
            )
            self.assertEqual(
                provenance["semantic_analysis_sha256"],
                review_run.hashlib.sha256(fixture.read_bytes()).hexdigest(),
            )
            self.assertFalse((replay / "semantic-analysis.json").exists())

    def test_replay_fixture_drift_blocks_before_accounting(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            source = runs / "source-run"
            (source / "evidence").mkdir(parents=True)
            ledger = {
                "schema_version": "evidence-ledger/v1",
                "manifest": {
                    "manifest_id": "elm-" + "a" * 64,
                    "events_digest": "b" * 64,
                },
                "events": [],
            }
            (source / "evidence" / "evidence-ledger.json").write_text(
                json.dumps(ledger, sort_keys=True) + "\n", encoding="utf-8"
            )
            (source / "run-report.json").write_text("{}\n", encoding="utf-8")
            (source / "run-report.md").write_text("# source\n", encoding="utf-8")
            (source / "semantic-analysis.json").write_text(
                json.dumps({"schema_version": 1, "activities": []}) + "\n",
                encoding="utf-8",
            )
            (source / "work-accounting-result.json").write_text(
                json.dumps(accounting_result(), sort_keys=True) + "\n", encoding="utf-8"
            )
            self._write_reconciliation_snapshots(source)

            with mock.patch.object(review_run, "RUNS", runs):
                replay = review_run._prepare_replay_run(source)
                provenance = json.loads((replay / "replay-source.json").read_text())
                (replay / provenance["semantic_analysis_fixture"]).write_text(
                    '{"tampered":true}\n', encoding="utf-8"
                )
                with self.assertRaisesRegex(ValueError, "differs from its immutable source"):
                    review_run._replay_analysis_fixture(source, replay)

    def test_replay_automatically_passes_sealed_fixture_without_analyzer_configuration(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            source = runs / "source-run"
            (source / "evidence").mkdir(parents=True)
            ledger = {
                "schema_version": "evidence-ledger/v1",
                "manifest": {
                    "manifest_id": "elm-" + "a" * 64,
                    "events_digest": "b" * 64,
                },
                "events": [],
            }
            (source / "evidence" / "evidence-ledger.json").write_text(
                json.dumps(ledger, sort_keys=True) + "\n", encoding="utf-8"
            )
            (source / "run-report.json").write_text("{}\n", encoding="utf-8")
            (source / "run-report.md").write_text("# source\n", encoding="utf-8")
            (source / "semantic-analysis.json").write_text(
                json.dumps({"schema_version": 1, "activities": []}) + "\n",
                encoding="utf-8",
            )
            (source / "work-accounting-result.json").write_text(
                json.dumps(accounting_result(), sort_keys=True) + "\n", encoding="utf-8"
            )
            self._write_reconciliation_snapshots(source)
            blocked_after_command_capture = subprocess.CompletedProcess(
                args=["accounting"], returncode=2, stdout="", stderr="fixture test stop"
            )
            with mock.patch.object(review_run, "RUNS", runs), mock.patch.dict(
                os.environ,
                {
                    "CLOCKIFY_ANALYZER_PRIMARY_URL": "",
                    "CLOCKIFY_ANALYZER_PRIMARY_MODEL": "",
                    "CLOCKIFY_PRIVATE_TEXT_EGRESS_APPROVED": "",
                },
                clear=False,
            ), mock.patch.object(
                review_run, "_run", return_value=blocked_after_command_capture
            ) as run:
                code = review_run.main([
                    "--replay-from", str(source),
                    "--state", str(Path(tmp) / "state.json"),
                ])

            self.assertEqual(2, code)
            command = run.call_args.args[0]
            self.assertIn("--analysis-fixture", command)
            fixture = Path(command[command.index("--analysis-fixture") + 1])
            self.assertTrue(fixture.is_file())
            self.assertIn("replay-fixture", fixture.parts)

    def test_replay_rejects_caller_supplied_fixture_before_any_process_runs(self):
        with mock.patch.object(review_run, "_run") as run:
            code = review_run.main([
                "--replay-from", "/tmp/source",
                "--analysis-fixture", "/tmp/unsealed.json",
            ])

        self.assertEqual(2, code)
        run.assert_not_called()

    def test_replay_integrity_rejects_analyzer_version_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            source = runs / "source-run"
            replay = runs / "replay-run"
            ledger = {
                "schema_version": "evidence-ledger/v1",
                "manifest": {
                    "manifest_id": "elm-" + "a" * 64,
                    "events_digest": "b" * 64,
                },
                "events": [],
            }
            for run_dir, model in ((source, "model-a"), (replay, "model-b")):
                (run_dir / "evidence").mkdir(parents=True)
                (run_dir / "evidence" / "evidence-ledger.json").write_text(
                    json.dumps(ledger, sort_keys=True) + "\n", encoding="utf-8"
                )
                (run_dir / "semantic-analysis.json").write_text(
                    json.dumps({
                        "schema_version": 1,
                        "prompt_version": "prompt-v1",
                        "evidence_bundle_schema_version": "clockify-semantic-evidence-bundle/v1",
                        "evidence_bundle_manifest": bundle_manifest(),
                        "ledger_evidence_digest": "sha256:" + "c" * 64,
                        "activities": [{
                            "analyzer_model": model,
                            "analyzer_tier": "primary",
                        }],
                        "analysis_chunks": [],
                    }) + "\n",
                    encoding="utf-8",
                )
                (run_dir / "work-accounting-result.json").write_text(
                    json.dumps(accounting_result(), sort_keys=True) + "\n", encoding="utf-8"
                )

            with mock.patch.object(review_run, "RUNS", runs):
                with self.assertRaisesRegex(ValueError, "analyzer route or version differs"):
                    review_run._verify_replay_integrity(source, replay)

            report = json.loads((replay / "replay-integrity.json").read_text(encoding="utf-8"))
            self.assertEqual("blocked", report["status"])

    def test_replay_integrity_rejects_analyzer_cache_decision_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            source = runs / "source-run"
            replay = runs / "replay-run"
            ledger = {
                "schema_version": "evidence-ledger/v1",
                "manifest": {
                    "manifest_id": "elm-" + "a" * 64,
                    "events_digest": "b" * 64,
                },
                "events": [],
            }
            for run_dir, digest in ((source, "d" * 64), (replay, "e" * 64)):
                (run_dir / "evidence").mkdir(parents=True)
                (run_dir / "evidence" / "evidence-ledger.json").write_text(
                    json.dumps(ledger, sort_keys=True) + "\n", encoding="utf-8"
                )
                (run_dir / "semantic-analysis.json").write_text(
                    json.dumps({
                        "schema_version": 1,
                        "prompt_version": "prompt-v1",
                        "evidence_bundle_schema_version": "clockify-semantic-evidence-bundle/v1",
                        "evidence_bundle_manifest": bundle_manifest(),
                        "ledger_evidence_digest": "sha256:" + "c" * 64,
                        "activities": [{
                            "analyzer_model": "model-a",
                            "analyzer_tier": "primary",
                        }],
                        "analysis_chunks": [],
                        "analyzer_cache": {
                            "records": [{
                                "cache_key": "arc-" + "f" * 64,
                                "decision_digest": digest,
                            }]
                        },
                    }) + "\n",
                    encoding="utf-8",
                )
                (run_dir / "work-accounting-result.json").write_text(
                    json.dumps(accounting_result(), sort_keys=True) + "\n", encoding="utf-8"
                )

            with mock.patch.object(review_run, "RUNS", runs):
                with self.assertRaisesRegex(ValueError, "cache decisions differ"):
                    review_run._verify_replay_integrity(source, replay)

    def test_replay_integrity_rejects_accounting_result_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            source, replay = runs / "source-run", runs / "replay-run"
            ledger = {"schema_version": "evidence-ledger/v1", "manifest": {"manifest_id": "elm-" + "a" * 64, "events_digest": "b" * 64}, "events": []}
            analysis = {
                "schema_version": 1, "prompt_version": "prompt-v1",
                "evidence_bundle_schema_version": "clockify-semantic-evidence-bundle/v1",
                "evidence_bundle_manifest": bundle_manifest(),
                "ledger_evidence_digest": "sha256:" + "c" * 64,
                "activities": [{"analyzer_model": "model-a", "analyzer_tier": "primary"}],
                "analysis_chunks": [],
            }
            for run_dir, proposal_id in ((source, "P001"), (replay, "P002")):
                (run_dir / "evidence").mkdir(parents=True)
                (run_dir / "evidence" / "evidence-ledger.json").write_text(json.dumps(ledger, sort_keys=True) + "\n")
                (run_dir / "semantic-analysis.json").write_text(json.dumps(analysis, sort_keys=True) + "\n")
                (run_dir / "work-accounting-result.json").write_text(json.dumps(accounting_result(proposal_id=proposal_id), sort_keys=True) + "\n")
            with mock.patch.object(review_run, "RUNS", runs):
                with self.assertRaisesRegex(ValueError, "work accounting result differs"):
                    review_run._verify_replay_integrity(source, replay)
            self.assertEqual("blocked", json.loads((replay / "replay-integrity.json").read_text())["status"])

    def test_replay_rejects_reconciliation_input_or_slice_bundle_drift(self):
        """Catches replay accepting a changed period contract or completed slice."""
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            source, replay = self._complete_replay_fixture(runs)

            with mock.patch.object(review_run, "RUNS", runs):
                integrity = review_run._verify_replay_integrity(source, replay)
                self.assertEqual("pass", integrity["status"])
                for name in (
                    "period-manifest.json", "routing.json", "review-corrections.jsonl",
                    "review-acceptance.jsonl", "fathom-reconciliation.json",
                    "completion-bundle.json",
                ):
                    path = replay / name
                    original = path.read_bytes()
                    if name == "period-manifest.json":
                        manifest = json.loads(original)
                        manifest["period"]["revision"] = 2
                        unsigned = dict(manifest)
                        unsigned.pop("manifest_digest")
                        manifest["manifest_digest"] = "sha256:" + hashlib.sha256(
                            json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                        ).hexdigest()
                        write_json(path, manifest)
                    else:
                        path.write_bytes(original + b"\n")
                    with self.assertRaises(ValueError, msg=name):
                        review_run._verify_replay_integrity(source, replay)
                    path.write_bytes(original)

    def test_replay_rejects_drift_or_loss_of_every_manifest_artifact(self):
        """Catches binding that validates bundles but trusts other manifest references."""
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            source, replay = self._complete_replay_fixture(runs)
            for run_dir in (source, replay):
                artifact = run_dir / "safe-generic-artifact.json"
                write_json(artifact, {"kind": "safe-fixture", "version": 1})
                manifest_path = run_dir / "period-manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest["artifacts"].append({
                    "path": str(artifact.resolve()),
                    "schema_version": "safe-generic/v1",
                    "compatibility_version": "safe-generic/v1",
                    "digest": "sha256:" + hashlib.sha256(artifact.read_bytes()).hexdigest(),
                })
                unsigned = dict(manifest)
                unsigned.pop("manifest_digest")
                manifest["manifest_digest"] = "sha256:" + hashlib.sha256(
                    json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest()
                write_json(manifest_path, manifest)

            with mock.patch.object(review_run, "RUNS", runs):
                self.assertEqual("pass", review_run._verify_replay_integrity(source, replay)["status"])
                (replay / "safe-generic-artifact.json").unlink()
                with self.assertRaises(review_run.ReviewRunError):
                    review_run._verify_replay_integrity(source, replay)

    def test_normal_run_snapshots_binding_inputs_before_replay_preparation(self):
        """Catches replay requiring files that normal collection never persisted."""
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            source, _ = self._complete_replay_fixture(runs)
            (source / "run-report.md").write_text("# source\n", encoding="utf-8")
            inputs = Path(tmp) / "inputs"
            inputs.mkdir()
            mapping = {
                "period_manifest": ("period-manifest.json", "period-manifest.json"),
                "routing": ("routing.json", "routing.json"),
                "corrections": ("review-corrections.jsonl", "review-corrections.jsonl"),
                "acceptance_ledger": ("review-acceptance.jsonl", "review-acceptance.jsonl"),
            }
            for _, (source_name, target_name) in mapping.items():
                (inputs / target_name).write_bytes((source / source_name).read_bytes())
                (source / source_name).unlink()
            args = argparse.Namespace(
                period_manifest=inputs / "period-manifest.json",
                routing=inputs / "routing.json",
                corrections=inputs / "review-corrections.jsonl",
                acceptance_ledger=inputs / "review-acceptance.jsonl",
            )

            with mock.patch.object(review_run, "RUNS", runs):
                review_run._snapshot_reconciliation_inputs(source, args)
                replay = review_run._prepare_replay_run(source)

            for _, (source_name, target_name) in mapping.items():
                self.assertEqual((inputs / target_name).read_bytes(), (source / source_name).read_bytes())
                self.assertEqual((source / source_name).read_bytes(), (replay / source_name).read_bytes())

            original_routing = (source / "routing.json").read_bytes()
            (inputs / "routing.json").write_text('{"changed":true}\n', encoding="utf-8")
            with mock.patch.object(review_run, "RUNS", runs), self.assertRaisesRegex(
                review_run.ReviewRunError, "snapshot differs"
            ):
                review_run._snapshot_reconciliation_inputs(source, args)
            self.assertEqual(original_routing, (source / "routing.json").read_bytes())

    def test_failed_snapshot_write_leaves_no_partial_target(self):
        """Catches interrupted writes being mistaken for durable input snapshots."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "routing.json"
            original_write = os.write
            writes = 0

            def interrupt_after_one_byte(descriptor, content):
                nonlocal writes
                writes += 1
                if writes == 1:
                    return original_write(descriptor, content[:1])
                raise OSError("simulated interrupted write")

            with mock.patch.object(os, "write", side_effect=interrupt_after_one_byte), \
                    self.assertRaises(review_run.ReviewRunError):
                review_run._write_snapshot(
                    target, b'{"meeting_routes":[],"session_routes":[]}\n', label="routing"
                )

            self.assertFalse(target.exists())
            self.assertEqual([], list(target.parent.glob(".routing.json.*.tmp")))

    def test_artifact_hash_does_not_follow_a_swapped_symlink(self):
        """Catches path checks racing a later hash read that follows a link."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root / "outside.json"
            outside.write_text('{"private":true}\n', encoding="utf-8")
            artifact = root / "artifact.json"
            artifact.symlink_to(outside)

            with mock.patch.object(Path, "is_symlink", return_value=False), \
                    self.assertRaises(review_run.ReviewRunError):
                review_run._file_sha256(artifact, label="manifest artifact")

    def test_replay_source_requires_every_reconciliation_snapshot(self):
        """Catches replay preparation silently skipping a mandatory source snapshot."""
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            source, _ = self._complete_replay_fixture(runs)
            (source / "run-report.md").write_text("# source\n", encoding="utf-8")
            (source / "review-acceptance.jsonl").unlink()

            with mock.patch.object(review_run, "RUNS", runs), self.assertRaisesRegex(
                ValueError, "missing reconciliation snapshot"
            ):
                review_run._prepare_replay_run(source)

    @staticmethod
    def _write_reconciliation_snapshots(directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        bundle_run = directory / "period-bundle-fixture"
        coverage = {"status": "complete", "incomplete_sources": []}
        write_json(bundle_run / "run-report.json", {
            "runtime_identity": {"git_sha": "fixture"},
            "date_range": {
                "since": "2026-08-01T00:00:00Z",
                "until": "2026-08-02T00:00:00Z",
            },
            "evidence_ledger": {"source_completeness": coverage},
        })
        write_json(bundle_run / "evidence" / "evidence-ledger.json", {
            "manifest": {"source_completeness": coverage}
        })
        write_json(bundle_run / "semantic-analysis.json", {"schema_version": 1})
        write_json(bundle_run / "work-accounting-result.json", accounting_result())
        write_json(bundle_run / "quality_report.json", {"status": "pass"})
        write_json(bundle_run / "review-snapshot.json", {"summary": {}})
        slice_ = review_run.clockify_sync_collect.plan_slices(
            dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 8, 2, tzinfo=dt.timezone.utc),
            zone=review_run.clockify_sync_collect.BUCHAREST,
        )[0]
        bundle = collector_receipts.build_completion_bundle(bundle_run, slice_=slice_)
        bundle_path = bundle_run / "completion-bundle.json"
        collector_receipts.write_completion_bundle(bundle_path, bundle)
        manifest = {
            "schema_version": reconciliation_manifest.MANIFEST_COMPATIBILITY_VERSION,
            "compatibility_version": reconciliation_manifest.MANIFEST_COMPATIBILITY_VERSION,
            "period": {
                "compatibility_version": reconciliation_manifest.PERIOD_COMPATIBILITY_VERSION,
                "member_id": "member-fixture", "workspace_id": "workspace-fixture",
                "timezone": "Europe/Bucharest", "since_utc": "2026-08-01T00:00:00Z",
                "until_utc": "2026-08-02T00:00:00Z", "revision": 1,
            },
            "state": "reconciling", "event_count": 2,
            "events_digest": "sha256:" + "d" * 64,
            "artifacts": [{
                "path": str(bundle_path.resolve()),
                "schema_version": "collector-completion-bundle/v1",
                "compatibility_version": "collector-completion-bundle/v1",
                "digest": "sha256:" + hashlib.sha256(bundle_path.read_bytes()).hexdigest(),
            }],
            "blockers": [],
        }
        unsigned = dict(manifest)
        manifest["manifest_digest"] = "sha256:" + hashlib.sha256(
            json.dumps(
                unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        write_json(directory / "period-manifest.json", manifest)
        (directory / "routing.json").write_text(
            '{"meeting_routes":[],"session_routes":[]}\n', encoding="utf-8"
        )
        (directory / "review-corrections.jsonl").write_text("", encoding="utf-8")
        (directory / "review-acceptance.jsonl").write_text("", encoding="utf-8")

    @staticmethod
    def _complete_replay_fixture(runs: Path) -> tuple[Path, Path]:
        """Create two isolated, complete synthetic slices with equal identities."""
        source, replay = runs / "source-run", runs / "replay-run"
        slice_ = review_run.clockify_sync_collect.plan_slices(
            dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 8, 2, tzinfo=dt.timezone.utc),
            zone=review_run.clockify_sync_collect.BUCHAREST,
        )[0]
        ledger = {
            "schema_version": "evidence-ledger/v1",
            "manifest": {
                "manifest_id": "elm-" + "a" * 64,
                "events_digest": "b" * 64,
                "source_completeness": {"status": "complete", "incomplete_sources": []},
            },
            "events": [],
        }
        analysis = {
            "schema_version": 1,
            "prompt_version": "prompt-v1",
            "evidence_bundle_schema_version": "clockify-semantic-evidence-bundle/v1",
            "evidence_bundle_manifest": bundle_manifest(),
            "ledger_evidence_digest": "sha256:" + "c" * 64,
            "activities": [{"analyzer_model": "model-a", "analyzer_tier": "primary"}],
            "analysis_chunks": [],
        }
        manifests: dict[Path, dict] = {}
        for run_dir in (source, replay):
            write_json(run_dir / "evidence" / "evidence-ledger.json", ledger)
            write_json(run_dir / "semantic-analysis.json", analysis)
            write_json(run_dir / "work-accounting-result.json", accounting_result())
            write_json(run_dir / "quality_report.json", {"status": "pass"})
            write_json(run_dir / "review-snapshot.json", {"summary": {}})
            write_json(run_dir / "run-report.json", {
                "runtime_identity": {"git_sha": "fixture"},
                "date_range": {"since": "2026-08-01T00:00:00Z", "until": "2026-08-02T00:00:00Z"},
                "evidence_ledger": {"source_completeness": {"status": "complete", "incomplete_sources": []}},
            })
            write_json(run_dir / "fathom-reconciliation.json", [])
            bundle = collector_receipts.build_completion_bundle(run_dir, slice_=slice_)
            collector_receipts.write_completion_bundle(run_dir / "completion-bundle.json", bundle)
            manifests[run_dir] = {
                "schema_version": reconciliation_manifest.MANIFEST_COMPATIBILITY_VERSION,
                "compatibility_version": reconciliation_manifest.MANIFEST_COMPATIBILITY_VERSION,
                "period": {
                    "compatibility_version": reconciliation_manifest.PERIOD_COMPATIBILITY_VERSION,
                    "member_id": "member-fixture", "workspace_id": "workspace-fixture",
                    "timezone": "Europe/Bucharest", "since_utc": "2026-08-01T00:00:00Z",
                    "until_utc": "2026-08-02T00:00:00Z", "revision": 1,
                },
                "state": "reconciling", "event_count": 2,
                "events_digest": "sha256:" + "d" * 64,
                "artifacts": [{
                    "path": str((run_dir / "completion-bundle.json").resolve()),
                    "schema_version": "collector-completion-bundle/v1",
                    "compatibility_version": "collector-completion-bundle/v1",
                    "digest": "sha256:" + hashlib.sha256((run_dir / "completion-bundle.json").read_bytes()).hexdigest(),
                }],
                "blockers": [],
            }
            unsigned = dict(manifests[run_dir])
            manifests[run_dir]["manifest_digest"] = "sha256:" + hashlib.sha256(
                json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            write_json(run_dir / "period-manifest.json", manifests[run_dir])
            write_json(run_dir / "routing.json", {"session_routes": [], "meeting_routes": []})
            (run_dir / "review-corrections.jsonl").write_text("", encoding="utf-8")
            (run_dir / "review-acceptance.jsonl").write_text("", encoding="utf-8")
        return source, replay

    def test_replay_range_options_are_rejected_before_any_process_runs(self):
        with mock.patch.object(review_run, "_run") as run:
            code = review_run.main(
                ["--replay-from", "/tmp/source", "--since", "2026-07-01"]
            )

        self.assertEqual(2, code)
        run.assert_not_called()

    def test_exceptions_only_cannot_start_before_acceptance_gate(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(review_run, "_run") as run:
            inputs = Path(tmp) / "inputs"
            self._write_reconciliation_snapshots(inputs)
            code = review_run.main(
                [
                    "--review-mode", "exceptions_only",
                    "--period-manifest", str(inputs / "period-manifest.json"),
                    "--routing", str(inputs / "routing.json"),
                    "--corrections", str(inputs / "review-corrections.jsonl"),
                    "--acceptance-ledger", str(inputs / "review-acceptance.jsonl"),
                ]
            )

        self.assertEqual(2, code)
        run.assert_not_called()

    def test_exceptions_only_compacts_clean_rows_and_keeps_active_exceptions(self):
        snapshot = {
            "summary": {
                "new": 2,
                "changed": 0,
                "carried_pending": 2,
                "resolved_disappeared": 0,
            },
            "categories": {
                "new": [
                    {**item("rvi-clean-new", "SC — Repaired stable review wording using cited work outcomes"), "disposition": "pending"},
                    {**item("rvi-ex-new", ""), "disposition": "ambiguous", "reason": "Route is unsupported."},
                ],
                "changed": [],
                "carried_pending": [
                    {**item("rvi-clean-old", "SC — Verified replay behavior across unchanged review inputs"), "disposition": "pending"},
                    {**item("rvi-ex-old", ""), "disposition": "ambiguous", "reason": "Meeting context is insufficient."},
                ],
            },
            "coverage_warnings": [],
        }
        gate = {"status": "evaluated", "exceptions_only_eligible": True}

        result = review_run.build_result(
            Path("/tmp/run-exceptions"),
            {"status": "pass", "summary": {}},
            snapshot,
            review_mode="exceptions_only",
            acceptance_gate=gate,
        )

        self.assertEqual("review_exceptions", result["action"])
        self.assertEqual(2, result["clean_batch"]["count"])
        self.assertEqual(
            ["rvi-clean-new", "rvi-clean-old"],
            result["clean_batch"]["review_item_ids"],
        )
        self.assertRegex(result["clean_batch"]["batch_id"], r"^rbatch-[0-9a-f]{24}$")
        self.assertEqual(
            ["rvi-ex-new"],
            [row["id"] for row in result["exceptions"]],
        )
        self.assertEqual(2, result["active_exception_count"])
        self.assertNotIn("rvi-clean-new", json.dumps(result["new"]))
        self.assertNotIn("rvi-clean-old", json.dumps(result["exceptions"]))

    def test_exceptions_only_clean_delta_requests_one_batch_without_reprinting_carried_rows(self):
        snapshot = {
            "summary": {"new": 1, "changed": 0, "carried_pending": 1, "resolved_disappeared": 0},
            "categories": {
                "new": [{**item("rvi-new", "SC — Improved clean batch review using stable identities"), "disposition": "pending"}],
                "changed": [],
                "carried_pending": [{**item("rvi-old", "SC — Preserved prior clean row without repeated details"), "disposition": "pending"}],
            },
            "coverage_warnings": [],
        }

        result = review_run.build_result(
            Path("/tmp/run-batch"),
            {"status": "pass", "summary": {}},
            snapshot,
            review_mode="exceptions_only",
            acceptance_gate={"exceptions_only_eligible": True},
        )

        self.assertEqual("review_batch", result["action"])
        self.assertEqual([], result["exceptions"])
        self.assertEqual(2, result["clean_batch"]["count"])

    def test_exceptions_only_unchanged_carried_items_do_not_repeat_comment(self):
        snapshot = {
            "summary": {"new": 0, "changed": 0, "carried_pending": 2, "resolved_disappeared": 0},
            "categories": {
                "new": [],
                "changed": [],
                "carried_pending": [
                    {**item("rvi-clean", "SC — Preserved clean carried work without repeated review detail"), "disposition": "pending"},
                    {**item("rvi-ex", ""), "disposition": "ambiguous", "reason": "Existing exception."},
                ],
            },
            "coverage_warnings": [],
        }

        result = review_run.build_result(
            Path("/tmp/run-carried"),
            {"status": "pass", "summary": {}},
            snapshot,
            review_mode="exceptions_only",
            acceptance_gate={"exceptions_only_eligible": True},
        )

        self.assertEqual("no_comment", result["action"])
        self.assertFalse(result["should_comment"])
        self.assertEqual([], result["exceptions"])
        self.assertEqual(1, result["active_exception_count"])

    def test_clean_batch_id_changes_when_a_member_revision_changes(self):
        def snapshot(revision: int) -> dict:
            return {
                "summary": {"new": 1, "changed": 0, "carried_pending": 0, "resolved_disappeared": 0},
                "categories": {
                    "new": [{
                        **item("rvi-clean", "SC — Preserved exact clean batch membership across review runs"),
                        "disposition": "pending",
                        "revision": revision,
                        "evidence_fingerprint": "evfp:sha256:" + "a" * 64,
                    }],
                    "changed": [],
                    "carried_pending": [],
                },
                "coverage_warnings": [],
            }

        first = review_run.build_result(
            Path("/tmp/run-rev-one"), {"status": "pass"}, snapshot(1),
            review_mode="exceptions_only", acceptance_gate={"exceptions_only_eligible": True},
        )
        second = review_run.build_result(
            Path("/tmp/run-rev-two"), {"status": "pass"}, snapshot(2),
            review_mode="exceptions_only", acceptance_gate={"exceptions_only_eligible": True},
        )

        self.assertNotEqual(first["clean_batch"]["batch_id"], second["clean_batch"]["batch_id"])

    def test_exceptions_only_summary_contains_batch_id_not_clean_descriptions(self):
        snapshot = {
            "summary": {"new": 2, "changed": 0, "carried_pending": 0, "resolved_disappeared": 0},
            "categories": {
                "new": [
                    {**item("rvi-clean", "SC — Private clean description should stay compact"), "disposition": "pending"},
                    {**item("rvi-ex", ""), "disposition": "ambiguous", "reason": "Insufficient evidence."},
                ],
                "changed": [],
                "carried_pending": [],
            },
            "coverage_warnings": [],
        }
        result = review_run.build_result(
            Path("/tmp/run-summary"),
            {"status": "pass", "summary": {}},
            snapshot,
            review_mode="exceptions_only",
            acceptance_gate={"exceptions_only_eligible": True},
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "summary.md"
            review_run.write_summary(path, result)
            text = path.read_text(encoding="utf-8")

        self.assertIn("Clean batch: 1 rows", text)
        self.assertIn("rvi-ex", text)
        self.assertNotIn("Private clean description", text)

    def test_missing_analyzer_configuration_emits_blocked_local_action_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            inputs = Path(tmp) / "inputs"
            self._write_reconciliation_snapshots(inputs)
            run_dir = runs / "run-blocked"
            run_dir.mkdir(parents=True)
            (run_dir / "run-report.json").write_text(json.dumps({
                "evidence": {
                    "calendly": {"status": "ok", "complete": True},
                },
                "evidence_ledger": {
                    "source_completeness": {
                        "status": "complete", "incomplete_sources": [],
                    },
                },
            }) + "\n", encoding="utf-8")
            (run_dir / "run-report.md").write_text("# fixture\n", encoding="utf-8")
            ledger_path = run_dir / "evidence" / "evidence-ledger.json"
            ledger_path.parent.mkdir()
            ledger_path.write_text(json.dumps({
                "manifest": {
                    "source_completeness": {
                        "status": "complete", "incomplete_sources": [],
                    },
                },
            }) + "\n", encoding="utf-8")
            collected = subprocess.CompletedProcess(
                args=["collector"], returncode=0,
                stdout=str(run_dir / "run-report.md") + "\n", stderr="",
            )
            blocked = subprocess.CompletedProcess(
                args=["accounting"], returncode=2, stdout="",
                stderr=(
                    "work accounting blocked: semantic analyzer is not configured; "
                    "CLOCKIFY_ANALYZER_PRIMARY_URL is required"
                ),
            )
            with mock.patch.object(review_run, "RUNS", runs), mock.patch.object(
                review_run, "_run", side_effect=[collected, blocked]
            ) as run:
                result_code = review_run.main(
                    [
                        "--period-manifest", str(inputs / "period-manifest.json"),
                        "--routing", str(inputs / "routing.json"),
                        "--state", str(Path(tmp) / "state.json"),
                        "--corrections", str(inputs / "review-corrections.jsonl"),
                        "--acceptance-ledger", str(inputs / "review-acceptance.jsonl"),
                        "--analyzer-target-body-bytes", "250000",
                        "--analyzer-max-events-per-chunk", "250",
                        "--analyzer-workers", "4",
                    ]
                )
            self.assertEqual(2, result_code)
            contract = json.loads((run_dir / "autopilot-result.json").read_text(encoding="utf-8"))
            self.assertEqual("blocked", contract["action"])
            self.assertFalse(contract["external_writes"])
            self.assertIn("not configured", contract["quality_summary"]["reason"])
            self.assertTrue((run_dir / "autopilot-summary.md").is_file())
            accounting_command = run.call_args_list[1].args[0]
            self.assertEqual(
                str(run_dir / "review-corrections.jsonl"),
                accounting_command[accounting_command.index("--corrections") + 1],
            )
            self.assertEqual(
                str(run_dir / "routing.json"),
                accounting_command[accounting_command.index("--routing") + 1],
            )
            cache_index = accounting_command.index("--analyzer-cache") + 1
            self.assertEqual(
                str(Path(tmp) / "analyzer-cache-v2.jsonl"), accounting_command[cache_index]
            )
            for option, expected in (
                ("--analyzer-target-body-bytes", "250000"),
                ("--analyzer-max-events-per-chunk", "250"),
                ("--analyzer-workers", "4"),
            ):
                self.assertEqual(expected, accounting_command[accounting_command.index(option) + 1])

    def test_fresh_run_rejects_invalid_period_snapshot_before_accounting(self):
        """Catches malformed period inputs reaching semantic accounting."""
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            run_dir = runs / "run-invalid-manifest"
            run_dir.mkdir(parents=True)
            (run_dir / "run-report.md").write_text("# fixture\n", encoding="utf-8")
            write_json(run_dir / "run-report.json", {
                "evidence": {"calendly": {"status": "excluded", "complete": True}},
                "evidence_ledger": {
                    "source_completeness": {"status": "complete", "incomplete_sources": []}
                }
            })
            write_json(run_dir / "evidence" / "evidence-ledger.json", {
                "manifest": {
                    "source_completeness": {"status": "complete", "incomplete_sources": []}
                }
            })
            inputs = Path(tmp) / "inputs"
            self._write_reconciliation_snapshots(inputs)
            (inputs / "period-manifest.json").write_text("{}\n", encoding="utf-8")
            collected = subprocess.CompletedProcess(
                args=["collector"], returncode=0,
                stdout=str(run_dir / "run-report.md") + "\n", stderr="",
            )
            with mock.patch.object(review_run, "RUNS", runs), mock.patch.object(
                review_run, "_run", return_value=collected
            ) as run, mock.patch.object(
                review_run, "_process_run",
                return_value=(0, run_dir / "autopilot-result.json"),
            ) as process_run:
                code = review_run.main([
                    "--period-manifest", str(inputs / "period-manifest.json"),
                    "--routing", str(inputs / "routing.json"),
                    "--corrections", str(inputs / "review-corrections.jsonl"),
                    "--acceptance-ledger", str(inputs / "review-acceptance.jsonl"),
                ])

            self.assertEqual(2, code)
            self.assertEqual(1, run.call_count)
            process_run.assert_not_called()

    def test_healthy_carried_queue_requires_no_comment(self):
        snapshot = {
            "summary": {
                "new": 0,
                "changed": 0,
                "carried_pending": 5,
                "resolved_disappeared": 0,
            },
            "categories": {
                "new": [],
                "changed": [],
                "carried_pending": [
                    item("rvi-old", "SC — unchanged private backlog text")
                ],
            },
            "coverage_warnings": [],
        }

        result = review_run.build_result(
            Path("/tmp/run-1"), {"status": "review_required", "summary": {}}, snapshot
        )

        self.assertEqual("no_comment", result["action"])
        self.assertFalse(result["should_comment"])
        self.assertEqual([], result["new"])
        self.assertEqual([], result["changed"])

    def test_changed_item_produces_delta_without_carried_backlog(self):
        snapshot = {
            "summary": {
                "new": 0,
                "changed": 1,
                "carried_pending": 4,
                "resolved_disappeared": 0,
            },
            "categories": {
                "new": [],
                "changed": [item("rvi-change", "SC — useful changed description")],
                "carried_pending": [
                    item("rvi-old", "SC — unchanged private backlog text")
                ],
            },
            "coverage_warnings": [],
        }

        result = review_run.build_result(
            Path("/tmp/run-2"), {"status": "review_required", "summary": {}}, snapshot
        )

        self.assertEqual("review_delta", result["action"])
        self.assertTrue(result["should_comment"])
        self.assertEqual(["rvi-change"], [row["id"] for row in result["changed"]])
        self.assertNotIn("carried_pending", result)

    def test_coverage_warning_outranks_delta(self):
        snapshot = {
            "summary": {
                "new": 1,
                "changed": 0,
                "carried_pending": 0,
                "resolved_disappeared": 0,
            },
            "categories": {
                "new": [item("rvi-new", "SC — new")],
                "changed": [],
            },
            "coverage_warnings": [
                {
                    "type": "source_unavailable",
                    "source": "clockify",
                    "reason": "Collector evidence status: error.",
                }
            ],
        }

        result = review_run.build_result(
            Path("/tmp/run-3"), {"status": "pass", "summary": {}}, snapshot
        )

        self.assertEqual("coverage_warning", result["action"])
        self.assertTrue(result["should_comment"])

    def test_blocked_quality_never_claims_external_writes(self):
        result = review_run.build_result(
            Path("/tmp/run-4"),
            {"status": "blocked", "summary": {"missing_candidate_keys": 1}},
            None,
        )

        self.assertEqual("blocked", result["action"])
        self.assertFalse(result["external_writes"])
        self.assertFalse(result["should_update_issue_description"])

    def test_summary_contains_only_actionable_delta(self):
        result = review_run.build_result(
            Path("/tmp/run-5"),
            {"status": "pass", "summary": {}},
            {
                "summary": {
                    "new": 1,
                    "changed": 0,
                    "carried_pending": 1,
                    "resolved_disappeared": 0,
                },
                "categories": {
                    "new": [item("rvi-new", "SC — actionable")],
                    "changed": [],
                    "carried_pending": [
                        item("rvi-old", "SC — must not be reprinted")
                    ],
                },
                "coverage_warnings": [],
            },
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "summary.md"
            review_run.write_summary(path, result)
            text = path.read_text()

        self.assertIn("rvi-new", text)
        self.assertNotIn("rvi-old", text)
        self.assertNotIn("must not be reprinted", text)

    def test_current_review_csv_uses_stable_ids_and_includes_carried_items(self):
        snapshot = {
            "categories": {
                "new": [
                    {
                        **item("rvi-new", "SC — actionable"),
                        "duration_minutes": 20,
                        "disposition": "pending",
                        "tag_names": ["System development"],
                    }
                ],
                "changed": [],
                "carried_pending": [
                    {
                        **item("rvi-old", "SC — carried"),
                        "duration_minutes": 10,
                        "disposition": "pending",
                        "tag_names": ["Processes"],
                    }
                ],
            }
        }

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review.csv"
            review_run.write_current_review_csv(path, snapshot)
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(["rvi-new", "rvi-old"], [row["Review ID"] for row in rows])
        self.assertEqual("System development", rows[0]["Tags"])
        self.assertEqual("10", rows[1]["Duration (min)"])

    def test_current_review_csv_normalizes_ambiguous_meeting_window_and_title(self):
        for separator in ("–", " - "):
            snapshot = {
                "categories": {
                    "new": [
                        {
                            **item("rvi-meeting", ""),
                            "client_project": None,
                            "description": None,
                            "label": "Discovery call",
                            "time": (
                                f"2026-07-29 14:11{separator}2026-07-29 15:35"
                            ),
                            "duration_minutes": None,
                            "source": "fathom",
                            "disposition": "ambiguous",
                            "revision": 1,
                        }
                    ],
                    "changed": [],
                    "carried_pending": [],
                }
            }

            with self.subTest(separator=separator), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "review.csv"
                review_run.write_current_review_csv(path, snapshot)
                with path.open(newline="", encoding="utf-8") as handle:
                    row = next(csv.DictReader(handle))

                self.assertEqual("2026-07-29 14:11", row["Start"])
                self.assertEqual("2026-07-29 15:35", row["End"])
                self.assertEqual("84", row["Duration (min)"])
                self.assertEqual("Discovery call", row["Description"])


if __name__ == "__main__":
    unittest.main()
