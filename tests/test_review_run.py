from __future__ import annotations

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
from contextlib import redirect_stdout
from unittest import mock


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

    def test_completed_slices_are_processed_before_later_collection_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
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
                    "--state", str(Path(tmp) / "state.json"),
                    "--corrections", str(Path(tmp) / "corrections.jsonl"),
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
                    "--corrections", str(Path(tmp) / "corrections.jsonl"),
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

    def test_replay_range_options_are_rejected_before_any_process_runs(self):
        with mock.patch.object(review_run, "_run") as run:
            code = review_run.main(
                ["--replay-from", "/tmp/source", "--since", "2026-07-01"]
            )

        self.assertEqual(2, code)
        run.assert_not_called()

    def test_exceptions_only_cannot_start_before_acceptance_gate(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(review_run, "_run") as run:
            code = review_run.main(
                [
                    "--review-mode", "exceptions_only",
                    "--acceptance-ledger", str(Path(tmp) / "missing.jsonl"),
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
                        "--state", str(Path(tmp) / "state.json"),
                        "--corrections", str(Path(tmp) / "corrections.jsonl"),
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
