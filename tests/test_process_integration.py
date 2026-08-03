from __future__ import annotations

import argparse
import datetime as dt
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts import clockify_sync_collect as collector
from scripts import evidence_ledger
from scripts import work_accounting_pipeline


TZ = dt.timezone(dt.timedelta(hours=3))
SINCE = dt.datetime(2026, 7, 1, tzinfo=TZ)
UNTIL = dt.datetime(2026, 7, 2, tzinfo=TZ)


class ProcessIntegrationTests(unittest.TestCase):
    def test_versioned_schemas_cover_the_emitted_document_wrappers(self) -> None:
        root = Path(__file__).resolve().parents[1]
        evidence = json.loads((root / "schemas" / "evidence-ledger-v1.json").read_text())
        semantic = json.loads((root / "schemas" / "semantic-analysis-v1.json").read_text())
        accounting = json.loads((root / "schemas" / "work-accounting-result-v1.json").read_text())
        self.assertEqual(
            {"schema_version", "manifest", "events"},
            set(evidence["$defs"]["ledgerDocument"]["required"]),
        )
        self.assertTrue(
            {
                "schema_version", "prompt_version", "ledger_event_count",
                "ledger_evidence_digest", "activities", "exceptions", "omissions",
                "analysis_chunks",
            }
            <= set(semantic["required"])
        )
        self.assertTrue(
            {"omit_rationale", "rendered_description"}
            <= set(semantic["$defs"]["activity"]["required"])
        )
        self.assertEqual(
            {
                "schema_version", "allocation_mode", "ledger_manifest",
                "semantic_analysis", "proposals", "ambiguous", "skipped",
                "allocation", "fathom_reconciliation", "correction_regression",
                "external_writes",
            },
            set(accounting["required"]),
        )
        proposal = accounting["$defs"]["proposal"]
        self.assertEqual("^wks-[a-f0-9]{24}$", proposal["properties"]["candidate_key"]["pattern"])
        self.assertEqual("^wka-[a-f0-9]{24}$", proposal["properties"]["review_activity_key"]["pattern"])
        self.assertTrue(
            {"candidate_key", "review_activity_key", "allocation_segment", "rendered_description"}
            <= set(proposal["required"])
        )
        correction = accounting["$defs"]["correctionRegression"]
        self.assertEqual(
            {"schema_version", "results", "summary"},
            set(correction["required"]),
        )
        self.assertEqual(
            {
                "regression_case_id", "activity_id", "evidence_fingerprint",
                "decision", "status", "failures",
            },
            set(correction["properties"]["results"]["items"]["required"]),
        )

    def test_collector_run_emits_a_manifest_valid_evidence_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            machine_result = {
                "machine": "macbook",
                "status": "ok",
                "collector_contract": "canonical_export_v1",
                "claude_bursts": [],
                "hermes_sessions": [],
                "hermes_db_sessions": [],
                "codex_sessions": [],
                "repository_events": [],
                "repository_evidence_status": "complete",
                "errors": [],
            }
            args = argparse.Namespace(
                since="2026-07-01", until="2026-07-01", enrich=False
            )

            def config(path: Path):
                if path.name == "routing.json":
                    return {"skip_rules": {}, "session_routes": [], "meeting_routes": []}
                return {"machines": [{"name": "macbook", "enabled": True}], "ssh_options": []}

            with mock.patch.object(collector, "RUNS", runs), mock.patch.object(
                collector, "load_json", side_effect=config
            ), mock.patch.object(
                collector, "load_env_file", return_value={"_missing": True}
            ), mock.patch.object(
                collector, "compute_range", return_value=(SINCE, UNTIL, "fixture")
            ), mock.patch.object(
                collector, "fetch_clockify", return_value={"status": "ok", "entries": []}
            ), mock.patch.object(
                collector,
                "fetch_fathom",
                return_value={"status": "ok", "complete": True, "meetings": []},
            ), mock.patch.object(
                collector, "fetch_multica_issues", return_value={"status": "ok", "issues": []}
            ), mock.patch.object(
                collector, "machine_is_local", return_value=True
            ), mock.patch.object(
                collector, "collect_local_sessions", return_value=machine_result
            ), mock.patch.object(
                collector,
                "collector_runtime_identity",
                return_value={"collector_path": "/repo/collector.py", "git_sha": "fixture"},
            ), mock.patch.object(
                collector,
                "build_proposals",
                return_value=([{"description": "[NEEDS REVIEW] copied status"}], [], []),
            ):
                self.assertEqual(0, collector.run(args))

            run_dir = next(runs.iterdir())
            ledger, events = work_accounting_pipeline.load_ledger(
                run_dir / "evidence" / "evidence-ledger.json"
            )
            ledger.validate(ledger.manifest)
            self.assertEqual([], events)
            self.assertEqual("complete", ledger.manifest.document()["source_completeness"]["status"])
            self.assertEqual([], json.loads((run_dir / "proposals.json").read_text()))
            self.assertEqual([], json.loads((run_dir / "ambiguous.json").read_text()))
            self.assertEqual([], json.loads((run_dir / "skipped.json").read_text()))
            self.assertIn(
                "[NEEDS REVIEW] copied status",
                (run_dir / "legacy-proposals.json").read_text(),
            )
            self.assertNotIn(
                "[NEEDS REVIEW] copied status",
                (run_dir / "run-report.md").read_text(),
            )

    def test_canonical_remote_export_is_preferred_without_legacy_execution(self) -> None:
        machine = {
            "name": "precision",
            "host": "precision.example.test",
            "collector_root": "/work/clockify",
        }
        exported = {
            "machine": "precision",
            "status": "ok",
            "claude_bursts": [],
            "hermes_sessions": [],
            "hermes_db_sessions": [],
            "codex_sessions": [],
            "repository_events": [],
            "repository_evidence_status": "complete",
            "errors": [],
            "canonical_export_attestation": {
                "collector_script_sha256": "fixture-digest",
                "runtime_identity": {"git_sha": "fixture-sha", "git_dirty": False},
            },
        }
        completed = subprocess.CompletedProcess(
            args=["ssh"], returncode=0, stdout=json.dumps(exported) + "\n", stderr=""
        )
        with (
            mock.patch.object(collector, "collector_script_sha256", return_value="fixture-digest"),
            mock.patch.object(collector.subprocess, "run", return_value=completed) as run,
        ):
            result = collector.collect_remote_sessions(
                machine,
                SINCE,
                UNTIL,
                [],
                coordinator_identity={"git_sha": "fixture-sha", "git_dirty": False},
            )
        self.assertEqual("canonical_export_v1", result["collector_contract"])
        self.assertEqual("git_worktree", result["canonical_export"]["bundle_provenance"])
        self.assertEqual(1, run.call_count)
        self.assertNotIn("<<'PY'", " ".join(str(value) for value in run.call_args.args[0]))

    def test_legacy_remote_fallback_is_partial_and_warns_repository_coverage(self) -> None:
        machine = {
            "name": "precision",
            "host": "precision.example.test",
            "collector_root": "/work/clockify",
        }
        failed = subprocess.CompletedProcess(
            args=["ssh"], returncode=1, stdout="", stderr="missing canonical exporter"
        )
        legacy_payload = {
            "machine": "precision",
            "status": "ok",
            "claude_bursts": [],
            "hermes_sessions": [],
            "hermes_db_sessions": [],
            "codex_sessions": [],
            "errors": [],
        }
        legacy = subprocess.CompletedProcess(
            args=["ssh"], returncode=0, stdout=json.dumps(legacy_payload) + "\n", stderr=""
        )
        with mock.patch.object(collector.subprocess, "run", side_effect=[failed, legacy]):
            result = collector.collect_remote_sessions(
                machine,
                SINCE,
                UNTIL,
                [],
                coordinator_identity={"git_sha": "fixture-sha", "git_dirty": False},
            )
        self.assertEqual("partial", result["status"])
        self.assertEqual("legacy_metadata_fallback", result["collector_contract"])
        inventory = evidence_ledger.source_inventory_from_collector(
            {
                "clockify": {"status": "ok", "entries": []},
                "fathom": {"status": "ok", "meetings": []},
                "multica_issues": {"status": "ok", "issues": []},
                "sessions": [result],
            }
        )
        self.assertEqual("unavailable", inventory["repositories/precision"]["status"])
        self.assertEqual("incomplete", evidence_ledger.source_completeness(inventory)["status"])


if __name__ == "__main__":
    unittest.main()
