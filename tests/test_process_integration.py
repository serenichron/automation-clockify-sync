from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import hashlib
import io
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
COMPLETE_CALENDLY = {
    "status": "ok",
    "complete": True,
    "recordings": [],
    "scheduled_without_recording": [],
}


class ProcessIntegrationTests(unittest.TestCase):
    def test_incomplete_calendly_result_never_records_a_completed_slice_receipt(self) -> None:
        """Calendly capability and pagination failures block the current slice before receipt creation."""
        incomplete_results = (
            ("missing_gateway", {"status": "capability_unavailable", "complete": False}),
            ("malformed_response", {"status": "incomplete", "complete": False}),
            ("repeated_cursor", {"status": "incomplete", "complete": False}),
            ("safety_limit", {"status": "incomplete", "complete": False}),
        )
        routing = {"skip_rules": {}, "session_routes": [], "meeting_routes": []}
        fleet = {"machines": [], "ssh_options": []}
        complete = {"status": "ok", "complete": True}

        for failure_kind, calendly_result in incomplete_results:
            with self.subTest(failure_kind=failure_kind), tempfile.TemporaryDirectory() as tmp:
                checkpoint_root = Path(tmp) / "checkpoints"
                args = argparse.Namespace(since="2026-07-01", until="2026-07-01", enrich=False)

                def config(path: Path):
                    return routing if path.name == "routing.json" else fleet

                with (
                    mock.patch.object(collector, "RUNS", Path(tmp) / "runs"),
                    mock.patch.dict(
                        collector.os.environ,
                        {"CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT": str(checkpoint_root)},
                    ),
                    mock.patch.object(collector, "load_json", side_effect=config),
                    mock.patch.object(collector, "load_env_file", return_value={"_missing": True}),
                    mock.patch.object(collector, "compute_range", return_value=(SINCE, UNTIL, "fixture")),
                    mock.patch.object(collector, "fetch_clockify", return_value={**complete, "entries": []}),
                    mock.patch.object(collector, "fetch_fathom", return_value={**complete, "meetings": []}),
                    mock.patch.object(collector, "fetch_calendly", return_value={**calendly_result, "recordings": []}),
                    mock.patch.object(collector, "fetch_multica_issues", return_value={**complete, "issues": []}),
                    mock.patch.object(
                        collector,
                        "collector_runtime_identity",
                        return_value={"collector_path": "/repo/collector.py", "git_sha": "fixture"},
                    ),
                ):
                    self.assertEqual(2, collector.run(args))

                slices = collector.plan_slices(SINCE, UNTIL, zone=collector.BUCHAREST)
                identity = collector.BacklogIdentity(
                    since_utc=collector.iso_utc(SINCE),
                    until_utc=collector.iso_utc(UNTIL),
                    timezone=collector.BUCHAREST.key,
                    max_days=2,
                    compatibility_version=collector._backlog_compatibility_version(routing, fleet),
                )
                backlog = collector.BacklogStore(checkpoint_root).open(identity, slices)
                self.assertEqual((), backlog.completed)

    def test_collect_slice_writes_calendly_evidence_and_only_aggregate_report_fields(self) -> None:
        """Calendly recordings are persisted as evidence, never embedded in the compact report."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "runs" / "bundle"
            calendly_result = {
                "status": "ok", "complete": True,
                "recordings": [{"recording_id": "recordings/private-recording"}],
                "scheduled_without_recording": [{"meeting_id": "events/scheduled-only"}],
            }
            complete = {"status": "ok", "complete": True}
            with (
                mock.patch.object(collector, "fetch_clockify", return_value={**complete, "entries": []}),
                mock.patch.object(collector, "fetch_fathom", return_value={**complete, "meetings": []}),
                mock.patch.object(collector, "fetch_calendly", return_value=calendly_result),
                mock.patch.object(collector, "fetch_multica_issues", return_value={**complete, "issues": []}),
                mock.patch.object(
                    collector, "collector_runtime_identity",
                    return_value={"collector_path": "/repo/collector.py", "git_sha": "fixture"},
                ),
            ):
                _path, report = collector._collect_slice(
                    argparse.Namespace(enrich=False),
                    {"skip_rules": {}, "session_routes": [], "meeting_routes": []},
                    {"machines": [], "ssh_options": []},
                    {"_missing": True}, {"_missing": True}, SINCE, UNTIL, "fixture",
                    collector.PageCheckpointStore(Path(tmp) / "checkpoints"), run_dir,
                    calendly_env={"_missing": ["CALENDLY_RECORDINGS_URL"]},
                )

            compact = json.loads((run_dir / "run-report.json").read_text())
            self.assertEqual("ok", compact["evidence"]["calendly"]["status"])
            self.assertEqual(1, compact["evidence"]["calendly"]["recording_count"])
            self.assertEqual(
                str(run_dir / "evidence" / "calendly-recordings.json"),
                compact["evidence"]["evidence_files"]["calendly"],
            )
            self.assertEqual(calendly_result, json.loads((run_dir / "evidence" / "calendly-recordings.json").read_text()))
            self.assertNotIn("recordings/private-recording", json.dumps(compact))
            self.assertEqual(calendly_result, report["evidence"]["calendly"])

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
            checkpoint_root = Path(tmp) / "checkpoints"
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

            with mock.patch.object(collector, "RUNS", runs), mock.patch.dict(
                collector.os.environ,
                {"CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT": str(checkpoint_root)},
            ), mock.patch.object(
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
                collector, "fetch_calendly", return_value=COMPLETE_CALENDLY
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
            self.assertEqual(
                {"status": "complete", "expected_count": 0, "observed_count": 0},
                ledger.manifest.source_inventory["calendly"],
            )
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

    def test_collector_emits_each_completed_backlog_slice(self) -> None:
        """A long recovery interval creates one independently reviewable bundle per slice."""
        seven_days_later = SINCE + dt.timedelta(days=7)

        def dated_result(
            source: str, since: dt.datetime, until: dt.datetime
        ) -> dict[str, object]:
            day = since.date().isoformat()
            if source == "clockify":
                return {
                    "status": "ok",
                    "complete": True,
                    "entries": [{"id": f"clockify-{day}", "start": since.isoformat()}],
                }
            if source == "fathom":
                return {
                    "status": "ok",
                    "complete": True,
                    "meetings": [
                        {
                            "recording_id": f"fathom-{day}",
                            "start": since.isoformat(),
                            "end": until.isoformat(),
                        }
                    ],
                }
            return {
                "status": "ok",
                "complete": True,
                "issues": [{"id": f"multica-{day}", "updated_at": since.isoformat()}],
            }

        def session_result(
            machine: dict[str, object], since: dt.datetime, until: dt.datetime, *args: object, **kwargs: object
        ) -> dict[str, object]:
            day = since.date().isoformat()
            return {
                "machine": machine["name"],
                "status": "ok",
                "collector_contract": "canonical_export_v1",
                "claude_bursts": [
                    {"session_id": f"{machine['name']}-{day}", "start": since.isoformat(), "end": until.isoformat()}
                ],
                "hermes_sessions": [],
                "hermes_db_sessions": [],
                "codex_sessions": [],
                "repository_events": [],
                "repository_evidence_status": "complete",
                "errors": [],
            }

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            checkpoint_root = Path(tmp) / "checkpoints"
            output = io.StringIO()
            args = argparse.Namespace(since="2026-07-01", until="2026-07-07", enrich=False)

            def config(path: Path):
                if path.name == "routing.json":
                    return {"skip_rules": {}, "session_routes": [], "meeting_routes": []}
                return {
                    "machines": [
                        {"name": "macbook", "enabled": True},
                        {"name": "remote", "enabled": True, "kind": "ssh"},
                    ],
                    "ssh_options": [],
                }

            with (
                mock.patch.object(collector, "RUNS", runs),
                mock.patch.dict(
                    collector.os.environ,
                    {"CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT": str(checkpoint_root)},
                ),
                mock.patch.object(collector, "load_json", side_effect=config),
                mock.patch.object(collector, "load_env_file", return_value={"_missing": True}),
                mock.patch.object(
                    collector,
                    "compute_range",
                    return_value=(SINCE, seven_days_later, "fixture"),
                ),
                mock.patch.object(
                    collector,
                    "fetch_clockify",
                    side_effect=lambda cenv, routing, since, until, **kwargs: dated_result(
                        "clockify", since, until
                    ),
                ),
                mock.patch.object(
                    collector,
                    "fetch_fathom",
                    side_effect=lambda fenv, since, until, **kwargs: dated_result(
                        "fathom", since, until
                    ),
                ),
                mock.patch.object(
                    collector, "fetch_calendly", return_value=COMPLETE_CALENDLY
                ),
                mock.patch.object(
                    collector,
                    "fetch_multica_issues",
                    side_effect=lambda since, until, **kwargs: dated_result(
                        "multica", since, until
                    ),
                ),
                mock.patch.object(
                    collector,
                    "machine_is_local",
                    side_effect=lambda machine: machine["name"] == "macbook",
                ),
                mock.patch.object(collector, "collect_local_sessions", side_effect=session_result),
                mock.patch.object(collector, "collect_remote_sessions", side_effect=session_result),
                mock.patch.object(
                    collector,
                    "collector_runtime_identity",
                    return_value={"collector_path": "/repo/collector.py", "git_sha": "fixture"},
                ),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(0, collector.run(args))

            reports = [Path(value) for value in output.getvalue().splitlines() if value]
            run_dirs = sorted(runs.iterdir())
            self.assertEqual(4, len(run_dirs))
            self.assertEqual([run_dir / "run-report.md" for run_dir in run_dirs], reports)
            self.assertEqual(4, len(set(run_dirs)))

            previous_until = None
            for run_dir, report_path in zip(run_dirs, reports):
                report = json.loads((run_dir / "run-report.json").read_text())
                evidence = json.loads(
                    (run_dir / "evidence" / "evidence-ledger.json").read_text()
                )
                since = dt.datetime.strptime(
                    report["date_range"]["since"], "%Y-%m-%d %H:%M"
                ).replace(tzinfo=TZ)
                until = dt.datetime.strptime(
                    report["date_range"]["until"], "%Y-%m-%d %H:%M"
                ).replace(tzinfo=TZ)
                if previous_until is not None:
                    self.assertEqual(previous_until, since)
                previous_until = until
                day = since.date().isoformat()
                self.assertEqual("complete", evidence["manifest"]["source_completeness"]["status"])
                self.assertEqual(
                    {f"clockify-{day}", f"fathom-{day}", f"multica-{day}", f"macbook-{day}", f"remote-{day}"},
                    {event["source_ref"]["source_id"] for event in evidence["events"]},
                )
                self.assertEqual(run_dir / "run-report.md", report_path)

            self.assertEqual(seven_days_later, previous_until)

    def test_collect_slice_rejects_an_interval_that_requires_multiple_slices(self) -> None:
        """The extracted helper cannot accidentally rebuild a whole-range bundle."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "runs" / "bundle"
            checkpoint_store = collector.PageCheckpointStore(Path(tmp) / "checkpoints")
            complete = {"status": "ok", "complete": True}
            with (
                mock.patch.object(
                    collector,
                    "fetch_clockify",
                    return_value={**complete, "entries": []},
                ),
                mock.patch.object(
                    collector,
                    "fetch_fathom",
                    return_value={**complete, "meetings": []},
                ),
                mock.patch.object(
                    collector,
                    "fetch_multica_issues",
                    return_value={**complete, "issues": []},
                ),
            ):
                with self.assertRaises(ValueError):
                    collector._collect_slice(
                        argparse.Namespace(enrich=False),
                        {"skip_rules": {}, "session_routes": [], "meeting_routes": []},
                        {"machines": [], "ssh_options": []},
                        {"_missing": True},
                        {"_missing": True},
                        SINCE,
                        SINCE + dt.timedelta(days=3),
                        "fixture",
                        checkpoint_store,
                        run_dir,
                    )
            self.assertFalse(run_dir.exists())

    def test_collect_slice_rejects_a_malformed_existing_bundle_without_overwriting_it(self) -> None:
        """A deterministic bundle collision is verified, never silently replaced."""
        with tempfile.TemporaryDirectory() as tmp:
            run_dir = Path(tmp) / "runs" / "bundle"
            ledger_path = run_dir / "evidence" / "evidence-ledger.json"
            ledger_path.parent.mkdir(parents=True)
            (run_dir / "run-report.md").write_text("diagnostic\n")
            (run_dir / "run-report.json").write_text(
                json.dumps({"run_id": run_dir.name, "date_range": []})
            )
            ledger_path.write_text(json.dumps({"manifest": {}}))

            with self.assertRaises(collector.BacklogError):
                collector._collect_slice(
                    argparse.Namespace(enrich=False),
                    {"skip_rules": {}, "session_routes": [], "meeting_routes": []},
                    {"machines": [], "ssh_options": []},
                    {"_missing": True},
                    {"_missing": True},
                    SINCE,
                    UNTIL,
                    "fixture",
                    collector.PageCheckpointStore(Path(tmp) / "checkpoints"),
                    run_dir,
                )
            self.assertEqual("diagnostic\n", (run_dir / "run-report.md").read_text())

    def test_collector_stops_at_an_incomplete_slice_and_retries_from_it(self) -> None:
        """A later source failure preserves earlier receipts and never skips its retry."""
        seven_days_later = SINCE + dt.timedelta(days=7)
        routing = {"skip_rules": {}, "session_routes": [], "meeting_routes": []}
        fleet = {"machines": [{"name": "macbook", "enabled": True}], "ssh_options": []}
        failure_enabled = [True]
        clockify_attempts: list[dt.datetime] = []

        def clockify(
            cenv: dict[str, str], routing: dict[str, object], since: dt.datetime, until: dt.datetime, **kwargs: object
        ) -> dict[str, object]:
            clockify_attempts.append(since)
            return {
                "status": "ok",
                "complete": True,
                "entries": [{"id": f"clockify-{since.date().isoformat()}", "start": since.isoformat()}],
            }

        def fathom(
            fenv: dict[str, str], since: dt.datetime, until: dt.datetime, **kwargs: object
        ) -> dict[str, object]:
            if failure_enabled[0] and since == SINCE + dt.timedelta(days=2):
                return {"status": "error", "complete": False, "meetings": []}
            return {
                "status": "ok",
                "complete": True,
                "meetings": [
                    {
                        "recording_id": f"fathom-{since.date().isoformat()}",
                        "start": since.isoformat(),
                        "end": until.isoformat(),
                    }
                ],
            }

        def multica(
            since: dt.datetime, until: dt.datetime, **kwargs: object
        ) -> dict[str, object]:
            return {
                "status": "ok",
                "complete": True,
                "issues": [{"id": f"multica-{since.date().isoformat()}", "updated_at": since.isoformat()}],
            }

        def sessions(
            machine: dict[str, object], since: dt.datetime, until: dt.datetime, *args: object, **kwargs: object
        ) -> dict[str, object]:
            return {
                "machine": machine["name"],
                "status": "ok",
                "collector_contract": "canonical_export_v1",
                "claude_bursts": [
                    {
                        "session_id": f"{machine['name']}-{since.date().isoformat()}",
                        "start": since.isoformat(),
                        "end": until.isoformat(),
                    }
                ],
                "hermes_sessions": [],
                "hermes_db_sessions": [],
                "codex_sessions": [],
                "repository_events": [],
                "repository_evidence_status": "complete",
                "errors": [],
            }

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            checkpoint_root = Path(tmp) / "checkpoints"
            args = argparse.Namespace(since="2026-07-01", until="2026-07-07", enrich=False)

            def config(path: Path):
                return routing if path.name == "routing.json" else fleet

            with (
                mock.patch.object(collector, "RUNS", runs),
                mock.patch.dict(
                    collector.os.environ,
                    {"CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT": str(checkpoint_root)},
                ),
                mock.patch.object(collector, "load_json", side_effect=config),
                mock.patch.object(collector, "load_env_file", return_value={"_missing": True}),
                mock.patch.object(
                    collector,
                    "compute_range",
                    return_value=(SINCE, seven_days_later, "fixture"),
                ),
                mock.patch.object(collector, "fetch_clockify", side_effect=clockify),
                mock.patch.object(collector, "fetch_fathom", side_effect=fathom),
                mock.patch.object(collector, "fetch_calendly", return_value=COMPLETE_CALENDLY),
                mock.patch.object(collector, "fetch_multica_issues", side_effect=multica),
                mock.patch.object(collector, "machine_is_local", return_value=True),
                mock.patch.object(collector, "collect_local_sessions", side_effect=sessions),
                mock.patch.object(
                    collector,
                    "collector_runtime_identity",
                    return_value={"collector_path": "/repo/collector.py", "git_sha": "fixture"},
                ),
            ):
                first_output = io.StringIO()
                with contextlib.redirect_stdout(first_output):
                    self.assertEqual(2, collector.run(args))

                first_reports = [Path(value) for value in first_output.getvalue().splitlines() if value]
                self.assertEqual(1, len(first_reports))
                self.assertEqual(
                    [SINCE, SINCE + dt.timedelta(days=2)], clockify_attempts
                )
                self.assertTrue(first_reports[0].is_file())

                slices = collector.plan_slices(SINCE, seven_days_later, zone=collector.BUCHAREST)
                identity = collector.BacklogIdentity(
                    since_utc=collector.iso_utc(SINCE),
                    until_utc=collector.iso_utc(seven_days_later),
                    timezone=collector.BUCHAREST.key,
                    max_days=2,
                    compatibility_version=collector._backlog_compatibility_version(routing, fleet),
                )
                backlog = collector.BacklogStore(checkpoint_root).open(identity, slices)
                self.assertEqual(1, len(backlog.completed))
                self.assertEqual(first_reports[0], backlog.completed[0].result_path)

                failure_enabled[0] = False
                clockify_attempts.clear()
                retry_output = io.StringIO()
                with contextlib.redirect_stdout(retry_output):
                    self.assertEqual(0, collector.run(args))

            retry_reports = [Path(value) for value in retry_output.getvalue().splitlines() if value]
            self.assertEqual(first_reports[0], retry_reports[0])
            self.assertEqual(4, len(retry_reports))
            self.assertEqual([slice_.since for slice_ in slices[1:]], clockify_attempts)

    def test_collector_rejects_a_tampered_ledger_before_receipt_reuse(self) -> None:
        """A completed-report digest alone cannot authorize replay after ledger tampering."""
        routing = {"skip_rules": {}, "session_routes": [], "meeting_routes": []}
        fleet = {"machines": [], "ssh_options": []}
        calls: list[str] = []

        def clockify(*args: object, **kwargs: object) -> dict[str, object]:
            calls.append("clockify")
            return {"status": "ok", "complete": True, "entries": []}

        def fathom(*args: object, **kwargs: object) -> dict[str, object]:
            calls.append("fathom")
            return {"status": "ok", "complete": True, "meetings": []}

        def multica(*args: object, **kwargs: object) -> dict[str, object]:
            calls.append("multica")
            return {"status": "ok", "complete": True, "issues": []}

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            checkpoints = Path(tmp) / "checkpoints"
            args = argparse.Namespace(since="2026-07-01", until="2026-07-01", enrich=False)

            def config(path: Path):
                return routing if path.name == "routing.json" else fleet

            with (
                mock.patch.object(collector, "RUNS", runs),
                mock.patch.dict(
                    collector.os.environ,
                    {"CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT": str(checkpoints)},
                ),
                mock.patch.object(collector, "load_json", side_effect=config),
                mock.patch.object(collector, "load_env_file", return_value={"_missing": True}),
                mock.patch.object(
                    collector, "compute_range", return_value=(SINCE, UNTIL, "fixture")
                ),
                mock.patch.object(collector, "fetch_clockify", side_effect=clockify),
                mock.patch.object(collector, "fetch_fathom", side_effect=fathom),
                mock.patch.object(collector, "fetch_calendly", return_value=COMPLETE_CALENDLY),
                mock.patch.object(collector, "fetch_multica_issues", side_effect=multica),
                mock.patch.object(
                    collector,
                    "collector_runtime_identity",
                    return_value={"collector_path": "/repo/collector.py", "git_sha": "fixture"},
                ),
            ):
                initial_output = io.StringIO()
                with contextlib.redirect_stdout(initial_output):
                    self.assertEqual(0, collector.run(args))
                report_path = Path(initial_output.getvalue().strip())
                ledger_path = report_path.parent / "evidence" / "evidence-ledger.json"
                ledger = json.loads(ledger_path.read_text())
                ledger["manifest"]["source_completeness"]["status"] = "incomplete"
                ledger["manifest"]["source_completeness"]["incomplete_sources"] = ["fathom"]
                ledger_path.write_text(json.dumps(ledger))

                calls.clear()
                replay_output = io.StringIO()
                with contextlib.redirect_stdout(replay_output):
                    self.assertEqual(2, collector.run(args))

            self.assertEqual([], calls)
            self.assertEqual("", replay_output.getvalue())

    def test_collector_rejects_a_rewritten_mutable_ledger_receipt(self) -> None:
        """The immutable Markdown receipt binds the ledger digest, not mutable JSON alone."""
        routing = {"skip_rules": {}, "session_routes": [], "meeting_routes": []}
        fleet = {"machines": [], "ssh_options": []}

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            checkpoints = Path(tmp) / "checkpoints"
            args = argparse.Namespace(since="2026-07-01", until="2026-07-01", enrich=False)

            def config(path: Path):
                return routing if path.name == "routing.json" else fleet

            with (
                mock.patch.object(collector, "RUNS", runs),
                mock.patch.dict(
                    collector.os.environ,
                    {"CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT": str(checkpoints)},
                ),
                mock.patch.object(collector, "load_json", side_effect=config),
                mock.patch.object(collector, "load_env_file", return_value={"_missing": True}),
                mock.patch.object(
                    collector, "compute_range", return_value=(SINCE, UNTIL, "fixture")
                ),
                mock.patch.object(
                    collector,
                    "fetch_clockify",
                    return_value={"status": "ok", "complete": True, "entries": []},
                ),
                mock.patch.object(
                    collector,
                    "fetch_fathom",
                    return_value={"status": "ok", "complete": True, "meetings": []},
                ),
                mock.patch.object(
                    collector, "fetch_calendly", return_value=COMPLETE_CALENDLY
                ),
                mock.patch.object(
                    collector,
                    "fetch_multica_issues",
                    return_value={"status": "ok", "complete": True, "issues": []},
                ),
                mock.patch.object(
                    collector,
                    "collector_runtime_identity",
                    return_value={"collector_path": "/repo/collector.py", "git_sha": "fixture"},
                ),
            ):
                initial_output = io.StringIO()
                with contextlib.redirect_stdout(initial_output):
                    self.assertEqual(0, collector.run(args))
                report_path = Path(initial_output.getvalue().strip())
                ledger_path = report_path.parent / "evidence" / "evidence-ledger.json"
                ledger_path.write_text(ledger_path.read_text() + "\n")
                report_json_path = report_path.parent / "run-report.json"
                report = json.loads(report_json_path.read_text())
                report["evidence_ledger"]["ledger_digest"] = "sha256:" + hashlib.sha256(
                    ledger_path.read_bytes()
                ).hexdigest()
                report_json_path.write_text(json.dumps(report))

                replay_output = io.StringIO()
                with contextlib.redirect_stdout(replay_output):
                    self.assertEqual(2, collector.run(args))

            self.assertEqual("", replay_output.getvalue())

    def test_collector_recollects_into_a_distinct_bundle_when_routing_changes(self) -> None:
        """A routing compatibility change cannot reuse a prior deterministic run ID."""
        routing = [
            {"skip_rules": {}, "session_routes": [], "meeting_routes": []}
        ]
        fleet = {"machines": [], "ssh_options": []}
        calls: list[str] = []

        def clockify(*args: object, **kwargs: object) -> dict[str, object]:
            calls.append("clockify")
            return {"status": "ok", "complete": True, "entries": []}

        def fathom(*args: object, **kwargs: object) -> dict[str, object]:
            calls.append("fathom")
            return {"status": "ok", "complete": True, "meetings": []}

        def multica(*args: object, **kwargs: object) -> dict[str, object]:
            calls.append("multica")
            return {"status": "ok", "complete": True, "issues": []}

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            checkpoints = Path(tmp) / "checkpoints"
            args = argparse.Namespace(since="2026-07-01", until="2026-07-01", enrich=False)

            def config(path: Path):
                return routing[0] if path.name == "routing.json" else fleet

            with (
                mock.patch.object(collector, "RUNS", runs),
                mock.patch.dict(
                    collector.os.environ,
                    {"CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT": str(checkpoints)},
                ),
                mock.patch.object(collector, "load_json", side_effect=config),
                mock.patch.object(collector, "load_env_file", return_value={"_missing": True}),
                mock.patch.object(
                    collector, "compute_range", return_value=(SINCE, UNTIL, "fixture")
                ),
                mock.patch.object(collector, "fetch_clockify", side_effect=clockify),
                mock.patch.object(collector, "fetch_fathom", side_effect=fathom),
                mock.patch.object(collector, "fetch_calendly", return_value=COMPLETE_CALENDLY),
                mock.patch.object(collector, "fetch_multica_issues", side_effect=multica),
                mock.patch.object(
                    collector,
                    "collector_runtime_identity",
                    return_value={"collector_path": "/repo/collector.py", "git_sha": "fixture"},
                ),
            ):
                first_output = io.StringIO()
                with contextlib.redirect_stdout(first_output):
                    self.assertEqual(0, collector.run(args))
                first_report = Path(first_output.getvalue().strip())

                calls.clear()
                routing[0] = {
                    "skip_rules": {"changed": "compatibility"},
                    "session_routes": [],
                    "meeting_routes": [],
                }
                second_output = io.StringIO()
                with contextlib.redirect_stdout(second_output):
                    self.assertEqual(0, collector.run(args))

            second_report = Path(second_output.getvalue().strip())
            self.assertEqual(["clockify", "fathom", "multica"], calls)
            self.assertNotEqual(first_report, second_report)
            self.assertEqual(2, len([path for path in runs.iterdir() if path.is_dir()]))

    def test_collector_retries_after_an_owned_claimed_directory_raises(self) -> None:
        """A collection exception relocates only this invocation's claimed directory."""
        routing = {"skip_rules": {}, "session_routes": [], "meeting_routes": []}
        fleet = {"machines": [], "ssh_options": []}
        clockify_attempts: list[str] = []

        def clockify(*args: object, **kwargs: object) -> dict[str, object]:
            clockify_attempts.append("clockify")
            if len(clockify_attempts) == 1:
                raise OSError("fixture collection failure")
            return {"status": "ok", "complete": True, "entries": []}

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            checkpoint_root = Path(tmp) / "checkpoints"
            args = argparse.Namespace(since="2026-07-01", until="2026-07-01", enrich=False)

            def config(path: Path):
                return routing if path.name == "routing.json" else fleet

            with (
                mock.patch.object(collector, "RUNS", runs),
                mock.patch.dict(
                    collector.os.environ,
                    {"CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT": str(checkpoint_root)},
                ),
                mock.patch.object(collector, "load_json", side_effect=config),
                mock.patch.object(collector, "load_env_file", return_value={"_missing": True}),
                mock.patch.object(
                    collector, "compute_range", return_value=(SINCE, UNTIL, "fixture")
                ),
                mock.patch.object(collector, "fetch_clockify", side_effect=clockify),
                mock.patch.object(
                    collector,
                    "fetch_fathom",
                    return_value={"status": "ok", "complete": True, "meetings": []},
                ),
                mock.patch.object(
                    collector, "fetch_calendly", return_value=COMPLETE_CALENDLY
                ),
                mock.patch.object(
                    collector,
                    "fetch_multica_issues",
                    return_value={"status": "ok", "complete": True, "issues": []},
                ),
                mock.patch.object(
                    collector,
                    "collector_runtime_identity",
                    return_value={"collector_path": "/repo/collector.py", "git_sha": "fixture"},
                ),
            ):
                slice_ = collector.plan_slices(SINCE, UNTIL, zone=collector.BUCHAREST)[0]
                compatibility = collector._backlog_compatibility_version(routing, fleet)
                run_dir = collector._slice_run_dir(slice_, compatibility)

                first_output = io.StringIO()
                with contextlib.redirect_stdout(first_output):
                    self.assertEqual(2, collector.run(args))

                diagnostics = list(runs.glob(f"{run_dir.name}-incomplete*"))
                self.assertFalse(run_dir.exists())
                self.assertEqual(1, len(diagnostics))
                self.assertTrue(diagnostics[0].is_dir())
                self.assertEqual("", first_output.getvalue())

                retry_output = io.StringIO()
                with contextlib.redirect_stdout(retry_output):
                    self.assertEqual(0, collector.run(args))

            report_path = Path(retry_output.getvalue().strip())
            self.assertEqual(["clockify", "clockify"], clockify_attempts)
            self.assertEqual(run_dir / "run-report.md", report_path)
            self.assertTrue(report_path.is_file())
            self.assertTrue(diagnostics[0].is_dir())

    def test_collector_never_renames_a_peer_directory_after_a_claim_collision(self) -> None:
        """A peer-owned incomplete directory is not converted into this run's diagnostic."""
        routing = {"skip_rules": {}, "session_routes": [], "meeting_routes": []}
        fleet = {"machines": [], "ssh_options": []}

        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            peer_run_dir = runs / "peer-bundle"
            peer_run_dir.mkdir(parents=True)
            marker = peer_run_dir / "peer-claim"
            marker.write_text("peer-owned\n")
            args = argparse.Namespace(since="2026-07-01", until="2026-07-01", enrich=False)

            def config(path: Path):
                return routing if path.name == "routing.json" else fleet

            with (
                mock.patch.object(collector, "RUNS", runs),
                mock.patch.dict(
                    collector.os.environ,
                    {"CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT": str(Path(tmp) / "checkpoints")},
                ),
                mock.patch.object(collector, "load_json", side_effect=config),
                mock.patch.object(collector, "load_env_file", return_value={"_missing": True}),
                mock.patch.object(
                    collector, "compute_range", return_value=(SINCE, UNTIL, "fixture")
                ),
                mock.patch.object(collector, "_slice_run_dir", return_value=peer_run_dir),
                mock.patch.object(
                    collector, "fetch_clockify", side_effect=AssertionError("must not collect")
                ),
            ):
                self.assertEqual(2, collector.run(args))

            self.assertTrue(peer_run_dir.is_dir())
            self.assertEqual("peer-owned\n", marker.read_text())
            self.assertFalse((runs / "peer-bundle-incomplete").exists())

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
