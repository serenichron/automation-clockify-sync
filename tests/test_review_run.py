from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
import csv
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

from scripts import (
    collector_receipts, evidence_ledger, reconciliation_manifest,
    semantic_analyzer, work_accounting_pipeline,
)
from task3_scenario_contract import assert_scenario_contract


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "clockify_review_run.py"
ROOT = SCRIPT.parents[1]
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


def run_tree_snapshot(*roots: Path) -> dict[str, dict[str, str]]:
    """Inventory exact file membership and bytes beneath synthetic run roots."""
    return {
        root.name: {
            str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*"))
            if path.is_file()
        }
        for root in roots
    }


def analyzer_provider_response(payload: dict) -> dict:
    members = [
        {"bundle_ref": bundle["bundle_ref"], **member}
        for bundle in payload["bundles"]
        for member in bundle["members"]
    ]
    partitions = [
        {"bundle_ref": member["bundle_ref"], "member_ranges": [[member["member"], member["member"]]]}
        for member in members
    ]
    return {
        "activities": [{
            "lifecycle": "completed", "action": "Reviewed", "object": "offline replay",
            "outcome": "validated sealed analyzer cache", "evidence_partitions": partitions,
            "evidence_spans": [member["time_span"] for member in members],
            "project_recommendation": {
                "name": "Serenichron Level 2", "prefix": "SC", "tag_names": ["Processes"],
            },
            "effort": {"minimum_minutes": 10, "recommended_minutes": 10, "maximum_minutes": 10},
            "semantic_confidence": "high", "timing_confidence": "high",
            "split_rationale": "one result", "merge_rationale": "",
        }],
        "exceptions": [], "omissions": [],
    }


class ReviewRunResultTests(unittest.TestCase):
    def test_normal_inference_run_seals_used_cache_then_replays_without_mutable_state(self):
        """Removing run-cache sealing must strand a real normal run after cleanup."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / "runs"
            source = runs / "normal-source"
            source.mkdir(parents=True)
            inventory = {
                "clockify": {"status": "complete"},
                "fathom": {"status": "complete"},
                "multica_issues": {"status": "complete"},
            }
            event = evidence_ledger.evidence_event(
                "codex_sessions_event",
                {
                    "source_type": "codex_sessions", "source_id": "normal-1",
                    "machine": "fixture", "session_id": "session-normal",
                },
                observed_at="2026-08-01T10:00:00Z",
                raw_source_span={
                    "start": "2026-08-01T10:00:00Z",
                    "end": "2026-08-01T10:10:00Z",
                    "path": "/offline/normal.jsonl",
                },
                attributes={
                    "role": "user", "kind": "message",
                    "content": "Seal the normal analyzer decisions",
                },
            )
            ledger = evidence_ledger.EvidenceLedger((event,), inventory)
            write_json(source / "evidence" / "evidence-ledger.json", {
                "schema_version": ledger.manifest.schema_version,
                "manifest": ledger.manifest.document(),
                "events": [event.document()],
            })
            write_json(source / "run-report.json", {
                "run_id": source.name,
                "runtime_identity": {"git_sha": "fixture"},
                "date_range": {
                    "since": "2026-08-01T00:00:00Z",
                    "until": "2026-08-02T00:00:00Z",
                },
                "evidence_ledger": {
                    "source_completeness": ledger.manifest.document()["source_completeness"],
                },
            })
            (source / "run-report.md").write_text("# normal source\n", encoding="utf-8")
            routing = {
                "session_routes": [{
                    "pattern": "normal", "project_name": "Serenichron Level 2",
                    "prefix": "SC", "tag_names": ["Processes"], "billable": True,
                }],
                "meeting_routes": [],
            }
            write_json(source / "routing.json", routing)
            (source / "review-corrections.jsonl").write_text("", encoding="utf-8")
            (source / "review-acceptance.jsonl").write_text("", encoding="utf-8")

            mutable_cache = root / "state" / "analyzer-cache-v2.jsonl"
            cache = semantic_analyzer.AnalyzerResponseCache(mutable_cache)
            endpoint = semantic_analyzer.AnalyzerEndpoint(
                name="clockify_analyzer_primary",
                url="https://offline.invalid/v1/chat/completions",
                model=semantic_analyzer.DEFAULT_PRIMARY_MODEL,
                revision=semantic_analyzer.DEFAULT_PRIMARY_REVISION,
            )

            def transport(_endpoint, body):
                payload = json.loads(body["messages"][-1]["content"])
                return (
                    {"probe": "ok"}
                    if payload.get("probe")
                    else analyzer_provider_response(payload)
                )

            semantic_analyzer.analyze_tiered(
                work_accounting_pipeline._with_semantic_route_hints(
                    [event.document()], routing
                ),
                primary=endpoint,
                transport=transport,
                private_text_approved=True,
                cache=cache,
                max_workers=1,
                review_taxonomy=[{
                    "project_name": "Serenichron Level 2", "prefix": "SC",
                    "tag_names": ["Processes"], "billable": True,
                    "selection_guidance": ["normal"],
                }],
            )
            cache.store_rejected(
                endpoint, {"unused": True}, failure_code="contract_rejected"
            )
            legacy_records = [
                json.loads(line) for line in mutable_cache.read_text().splitlines()
            ]
            for record in legacy_records:
                record.pop("route")
            mutable_cache.write_text(
                "".join(
                    semantic_analyzer.canonical_json(record) + "\n"
                    for record in legacy_records
                ),
                encoding="utf-8",
            )
            mutable_record_count = len(legacy_records)
            args = argparse.Namespace(
                routing=source / "routing.json",
                corrections=source / "review-corrections.jsonl",
                state=root / "state" / "review-items.json",
                analyzer_cache=mutable_cache,
                analysis_fixture=None,
                analyzer_target_body_bytes=None,
                analyzer_max_events_per_chunk=None,
                analyzer_workers=1,
                review_mode="shadow_all",
            )
            environment = {
                "CLOCKIFY_ANALYZER_PRIMARY_URL": endpoint.url,
                "CLOCKIFY_ANALYZER_PRIMARY_MODEL": endpoint.model,
                "CLOCKIFY_ANALYZER_PRIMARY_REVISION": endpoint.revision,
                "CLOCKIFY_ANALYZER_FALLBACK_URL": "",
                "CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED": "approved",
            }
            with mock.patch.object(review_run, "RUNS", runs), mock.patch.dict(
                os.environ, environment, clear=False
            ):
                code, _result = review_run._process_run(args, source, {})
            self.assertEqual(0, code)

            sealed_cache = source / "analyzer-cache-used.jsonl"
            self.assertTrue(sealed_cache.is_file())
            sealed_records = [
                json.loads(line) for line in sealed_cache.read_text().splitlines()
            ]
            self.assertTrue(all(record["route"] == {
                "name": endpoint.name,
                "url": endpoint.url,
                "model": endpoint.model,
                "revision": endpoint.revision,
            } for record in sealed_records))
            analysis = json.loads((source / "semantic-analysis.json").read_text())
            used = analysis["analyzer_cache"]["records"]
            self.assertEqual(
                [record["cache_key"] for record in used],
                [record["cache_key"] for record in sealed_records],
            )
            self.assertLess(len(sealed_records), mutable_record_count)
            self.assertEqual({
                "path": "analyzer-cache-used.jsonl",
                "record_count": len(sealed_records),
                "sha256": hashlib.sha256(sealed_cache.read_bytes()).hexdigest(),
            }, analysis["analyzer_cache"]["snapshot"])

            self._write_reconciliation_snapshots(source)
            write_json(source / "routing.json", routing)
            mutable_cache.unlink()
            with mock.patch.dict(os.environ, {
                "CLOCKIFY_ANALYZER_PRIMARY_URL": "",
                "CLOCKIFY_ANALYZER_PRIMARY_MODEL": "",
                "CLOCKIFY_ANALYZER_PRIMARY_REVISION": "",
                "CLOCKIFY_ANALYZER_FALLBACK_URL": "",
                "CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED": "approved",
            }, clear=False):
                replay_code = review_run.main([
                    "--replay-from", str(source),
                    "--runs-root", str(runs),
                    "--state", str(root / "state" / "replay-items.json"),
                ])
            self.assertEqual(0, replay_code)

    def test_real_offline_replay_main_reuses_accepted_cache_and_passes_integrity(self):
        """Replay must prove accepted cache reuse before crossing the accounting child."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / "runs"
            source = self._write_real_offline_replay_source(runs, root)
            immutable_before = run_tree_snapshot(source)
            parent_before = {
                str(path.relative_to(source)): path.read_bytes()
                for path in sorted(source.rglob("*")) if path.is_file()
            }

            with mock.patch.dict(os.environ, {
                "CLOCKIFY_ANALYZER_PRIMARY_URL": "",
                "CLOCKIFY_ANALYZER_PRIMARY_MODEL": "",
                "CLOCKIFY_ANALYZER_PRIMARY_REVISION": "",
                "CLOCKIFY_ANALYZER_FALLBACK_URL": "",
                "CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED": "approved",
            }, clear=False):
                code = review_run.main([
                    "--replay-from", str(source),
                    "--runs-root", str(runs.resolve()),
                    "--state", str(root / "replay-items.json"),
                ])

            replay = next(runs.glob("*-replay-source-run"))
            self.assertEqual(
                0, code,
                (replay / "autopilot-result.json").read_text(encoding="utf-8"),
            )
            self.assertEqual(
                "pass",
                json.loads((replay / "replay-integrity.json").read_text())["status"],
            )
            self.assertEqual(immutable_before, run_tree_snapshot(source))
            self.assertEqual(
                (source / "work-accounting-result.json").read_bytes(),
                (replay / "work-accounting-result.json").read_bytes(),
            )
            provenance = json.loads((replay / "replay-source.json").read_text())
            self.assertEqual(
                json.loads((source / "semantic-analysis.json").read_text())["analyzer_cache"]["records"],
                provenance["analyzer_cache_reused_records"],
            )
            self.assertEqual(
                (source / "analyzer-cache-used.jsonl").read_bytes(),
                (replay / provenance["analyzer_cache_fixture"]).read_bytes(),
            )
            self.assertEqual(
                hashlib.sha256((source / "analyzer-cache-used.jsonl").read_bytes()).hexdigest(),
                provenance["analyzer_cache_sha256"],
            )
            replay_result = json.loads(
                (replay / "work-accounting-result.json").read_text(encoding="utf-8")
            )
            assert_scenario_contract(
                self,
                stable_ids=[
                    proposal["review_activity_key"]
                    for proposal in replay_result["proposals"]
                ],
                parent_before=parent_before,
                parent_after={
                    str(path.relative_to(source)): path.read_bytes()
                    for path in sorted(source.rglob("*")) if path.is_file()
                },
                emitted_ids=[
                    record["cache_key"]
                    for record in provenance["analyzer_cache_reused_records"]
                ],
                clockify_adapter_calls=int(bool(replay_result["external_writes"])),
            )

    def test_inference_backed_replay_requires_source_cache_before_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._write_real_offline_replay_source(root / "runs", root)
            (source / "analyzer-cache-used.jsonl").unlink()

            blocked_child = SimpleNamespace(returncode=2, stderr="unexpected child", stdout="")
            with mock.patch.object(review_run, "_run", return_value=blocked_child) as child:
                code = review_run.main([
                    "--replay-from", str(source), "--runs-root", str(root / "runs"),
                    "--state", str(root / "review-items.json"),
                ])

            self.assertEqual(2, code)
            child.assert_not_called()

    def test_inference_metadata_requires_cache_even_without_cache_summary_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = self._write_real_offline_replay_source(root / "runs", root)
            analysis_path = source / "semantic-analysis.json"
            analysis = json.loads(analysis_path.read_text())
            analysis["analyzer_cache"]["records"] = []
            write_json(analysis_path, analysis)
            (source / "analyzer-cache-used.jsonl").unlink()

            with mock.patch.object(review_run, "RUNS", root / "runs"):
                with self.assertRaisesRegex(ValueError, "sealed analyzer cache"):
                    review_run._prepare_replay_run(source)

    def test_replay_cache_binding_mismatches_block_before_accounting_child(self):
        """Evidence, prompt, model, or output drift must never reach child transport."""
        for field in ("evidence", "prompt", "model", "request", "output"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                runs = root / "runs"
                source = self._write_real_offline_replay_source(runs, root)
                if field == "evidence":
                    analysis = json.loads((source / "semantic-analysis.json").read_text())
                    analysis["ledger_evidence_digest"] = "led-" + "f" * 64
                    write_json(source / "semantic-analysis.json", analysis)
                else:
                    cache_path = source / "analyzer-cache-used.jsonl"
                    records = [json.loads(line) for line in cache_path.read_text().splitlines()]
                    record = records[-1]
                    if field == "prompt":
                        record["prompt_version"] = "clockify-semantic-v99"
                    elif field == "model":
                        record["model"] = "different-flash-route"
                    elif field == "request":
                        record["body_digest"] = "a" * 64
                        record["cache_key"] = semantic_analyzer.stable_digest(
                            "arc-", {
                                "schema_version": record["schema_version"],
                                "prompt_version": record["prompt_version"],
                                "semantic_schema_version": record["semantic_schema_version"],
                                "route_digest": record["route_digest"],
                                "body_digest": record["body_digest"],
                            }, length=64,
                        )
                    else:
                        record["response"]["activities"][0]["outcome"] = "mutated output"
                        record["decision_digest"] = semantic_analyzer.AnalyzerResponseCache._decision_digest({
                            "status": "accepted", "response": record["response"],
                        })
                    records[-1] = record
                    cache_path.write_text("".join(json.dumps(item, sort_keys=True) + "\n" for item in records))
                    if field in {"request", "output"}:
                        analysis_path = source / "semantic-analysis.json"
                        analysis = json.loads(analysis_path.read_text())
                        analysis["analyzer_cache"]["records"][-1] = {
                            "cache_key": record["cache_key"],
                            "decision_digest": record["decision_digest"],
                        }
                        write_json(analysis_path, analysis)

                with mock.patch.dict(os.environ, {
                    "CLOCKIFY_ANALYZER_PRIMARY_URL": "https://offline.invalid/v1/chat/completions",
                    "CLOCKIFY_ANALYZER_PRIMARY_MODEL": semantic_analyzer.DEFAULT_PRIMARY_MODEL,
                    "CLOCKIFY_ANALYZER_PRIMARY_REVISION": semantic_analyzer.DEFAULT_PRIMARY_REVISION,
                    "CLOCKIFY_ANALYZER_FALLBACK_URL": "",
                    "CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED": "approved",
                }, clear=False):
                    with self.assertRaises((ValueError, semantic_analyzer.AnalyzerError)):
                        review_run._prepare_replay_run(source)

    def test_collector_derivation_successfully_finalizes_verified_completion(self):
        """A passing derivation must seal and reload its complete artifact bundle."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = self._write_real_offline_replay_source(root / "runs", root)
            (run_dir / "completion-bundle.json").unlink()
            identity = SimpleNamespace(
                slice_id="slice-fixture",
                since_utc="2026-08-01T00:00:00Z",
                until_utc="2026-08-02T00:00:00Z",
            )
            with mock.patch.object(
                review_run, "_verified_collector_derivation",
                return_value=(root / "collector-source", identity, {}),
            ):
                bundle = review_run._finalize_collector_derivation_completion(run_dir)

            self.assertEqual("slice-fixture", bundle.slice_id)
            self.assertEqual(
                bundle.bundle_digest,
                collector_receipts.load_completion_bundle(
                    run_dir / "completion-bundle.json", run_dir=run_dir,
                ).bundle_digest,
            )

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

    def test_collector_run_dirs_rejects_symlink_and_lexical_result_paths(self):
        """Collector stdout cannot smuggle aliases into the trusted run root."""
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            run_dir = runs / "source"
            run_dir.mkdir(parents=True)
            report = run_dir / "run-report.md"
            report.write_text("# receipt\n", encoding="utf-8")
            alias = runs / "alias"
            alias.symlink_to(run_dir, target_is_directory=True)
            lexical = runs / "nested" / ".." / "source" / "run-report.md"
            with mock.patch.object(review_run, "RUNS", runs):
                for candidate in (alias / "run-report.md", lexical):
                    with self.subTest(candidate=candidate), self.assertRaisesRegex(
                        ValueError, "canonical"
                    ):
                        review_run._collector_run_dirs(str(candidate))

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
                "_prepare_collector_derivation_run",
                side_effect=lambda run_dir, snapshots: run_dir,
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
                        "analyzer_tier": "fixture",
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

    def test_derive_replay_integrity_passes_without_writing_report(self):
        """Catches a shared derivation API that mutates the sealed replay directory."""
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            source, replay = self._complete_replay_fixture(runs)
            report_path = replay / "replay-integrity.json"
            derive = getattr(review_run, "derive_replay_integrity", None)
            self.assertIsNotNone(derive, "non-writing replay derivation API is missing")
            before = run_tree_snapshot(source, replay)
            self.assertTrue(all(before.values()), "synthetic run trees must be nonempty")

            with mock.patch.object(review_run, "RUNS", runs):
                report = derive(source, replay)

            self.assertEqual("pass", report["status"])
            self.assertEqual([], report["failures"])
            self.assertFalse(report_path.exists())
            self.assertEqual(before, run_tree_snapshot(source, replay))

    def test_derive_replay_integrity_returns_blocked_report_without_writing(self):
        """Catches pure derivation raising or writing before its caller chooses policy."""
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            source, replay = self._complete_replay_fixture(runs)
            analysis_path = replay / "semantic-analysis.json"
            analysis = json.loads(analysis_path.read_text(encoding="utf-8"))
            analysis["activities"][0]["analyzer_model"] = "model-drifted"
            write_json(analysis_path, analysis)
            report_path = replay / "replay-integrity.json"
            derive = getattr(review_run, "derive_replay_integrity", None)
            self.assertIsNotNone(derive, "non-writing replay derivation API is missing")
            before = run_tree_snapshot(source, replay)
            self.assertTrue(all(before.values()), "synthetic run trees must be nonempty")

            with mock.patch.object(review_run, "RUNS", runs):
                report = derive(source, replay)

            self.assertEqual("blocked", report["status"])
            self.assertEqual(["analyzer route or version differs"], report["failures"])
            self.assertFalse(report_path.exists())
            self.assertEqual(before, run_tree_snapshot(source, replay))

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
                bundle_path = source / "completion-bundle.json"
                original = bundle_path.read_bytes()
                bundle_path.write_bytes(original + b"\n")
                with self.assertRaises(ValueError, msg="completion-bundle.json"):
                    review_run._verify_replay_integrity(source, replay)
                bundle_path.write_bytes(original)

    @staticmethod
    def _bootstrap_snapshots(source: Path, replay: Path, *, until: str | None = None) -> bytes:
        manifest = json.loads((source / "period-manifest.json").read_text())
        manifest.update(state="collecting", event_count=1, artifacts=[], blockers=[])
        if until:
            manifest["period"]["until_utc"] = until
        unsigned = {key: value for key, value in manifest.items() if key != "manifest_digest"}
        manifest["manifest_digest"] = reconciliation_manifest._digest(unsigned)
        write_json(source / "period-manifest.json", manifest)
        content = (source / "period-manifest.json").read_bytes()
        (replay / "period-manifest.json").write_bytes(content)
        return content

    def test_bootstrap_replay_binds_completed_source_without_rewriting_input_snapshots(self):
        """Catches the fresh-run circular requirement for pre-existing completion output."""
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            source, replay = self._complete_replay_fixture(runs)
            snapshot = self._bootstrap_snapshots(source, replay)
            # A distinct replay has not produced its own completion bundle yet.
            (replay / "completion-bundle.json").unlink()
            with mock.patch.object(review_run, "RUNS", runs):
                result = review_run._verify_replay_integrity(source, replay)
            self.assertEqual("pass", result["status"])
            self.assertEqual("1", result["reconciliation_binding"]["slice_completion_bundle_count"])
            self.assertEqual(snapshot, (source / "period-manifest.json").read_bytes())
            self.assertEqual(snapshot, (replay / "period-manifest.json").read_bytes())

    def test_bootstrap_replay_rejects_missing_or_drifted_source_completion(self):
        for failure in ("missing", "drift"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                runs = Path(tmp) / "runs"
                source, replay = self._complete_replay_fixture(runs)
                self._bootstrap_snapshots(source, replay)
                if failure == "missing":
                    (source / "completion-bundle.json").unlink()
                else:
                    write_json(source / "quality_report.json", {"status": "blocked"})
                with mock.patch.object(review_run, "RUNS", runs), self.assertRaises(ValueError):
                    review_run._verify_replay_integrity(source, replay)

    def test_bootstrap_replay_rejects_completion_for_different_period(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            source, replay = self._complete_replay_fixture(runs)
            self._bootstrap_snapshots(source, replay, until="2026-08-03T00:00:00Z")
            with mock.patch.object(review_run, "RUNS", runs), self.assertRaises(ValueError):
                review_run._verify_replay_integrity(source, replay)

    def test_replay_rejects_drift_or_loss_of_every_manifest_artifact(self):
        """Catches binding that validates bundles but trusts other manifest references."""
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            source, replay = self._complete_replay_fixture(runs)
            artifact = source / "safe-generic-artifact.json"
            write_json(artifact, {"kind": "safe-fixture", "version": 1})
            manifest_path = source / "period-manifest.json"
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
            (replay / "period-manifest.json").write_bytes(manifest_path.read_bytes())

            with mock.patch.object(review_run, "RUNS", runs):
                self.assertEqual("pass", review_run._verify_replay_integrity(source, replay)["status"])
                artifact.unlink()
                with self.assertRaises(review_run.ReviewRunError):
                    review_run._verify_replay_integrity(source, replay)

    def test_replay_binds_exact_manifest_bytes_including_artifact_paths(self):
        """Catches normalized identities accepting a different artifact location."""
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            source, replay = self._complete_replay_fixture(runs)
            original_bundle = replay / "completion-bundle.json"
            alternate_bundle = replay / "alternate-completion-bundle.json"
            alternate_bundle.write_bytes(original_bundle.read_bytes())
            manifest_path = replay / "period-manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["artifacts"][0]["path"] = str(alternate_bundle.resolve())
            unsigned = dict(manifest)
            unsigned.pop("manifest_digest")
            manifest["manifest_digest"] = "sha256:" + hashlib.sha256(
                json.dumps(
                    unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            write_json(manifest_path, manifest)

            with mock.patch.object(review_run, "RUNS", runs), self.assertRaisesRegex(
                ValueError, "reconciliation period binding differs"
            ):
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

    def test_replay_preparation_copies_the_exact_preflight_semantic_bytes(self):
        """A source-path replacement before the old copy must not change child input."""
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            source, _ = self._complete_replay_fixture(runs)
            (source / "run-report.md").write_text("# source\n", encoding="utf-8")
            semantic_path = source / "semantic-analysis.json"
            original = semantic_path.read_bytes()
            real_copyfile = shutil.copyfile

            def mutate_before_copy(src, dst, *args, **kwargs):
                if Path(src) == semantic_path:
                    semantic_path.write_text('{"schema_version":1,"mutated":true}\n')
                return real_copyfile(src, dst, *args, **kwargs)

            with mock.patch.object(review_run, "RUNS", runs), mock.patch.object(
                review_run.shutil, "copyfile", side_effect=mutate_before_copy,
            ):
                replay = review_run._prepare_replay_run(source)

            provenance = json.loads((replay / "replay-source.json").read_text())
            self.assertEqual(
                original,
                (replay / provenance["semantic_analysis_fixture"]).read_bytes(),
            )

    def test_replay_integrity_enforces_sealed_source_provenance_digests(self):
        """Final integrity must not recompute mutable equality around a broken seal."""
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            source, replay = self._complete_replay_fixture(runs)
            provenance_path = replay / "replay-source.json"
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            provenance["semantic_analysis_sha256"] = "0" * 64
            write_json(provenance_path, provenance)

            with mock.patch.object(review_run, "RUNS", runs):
                report = review_run.derive_replay_integrity(source, replay)

            self.assertEqual("blocked", report["status"])
            self.assertIn("replay source provenance differs", report["failures"])

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
            "activities": [{"analyzer_model": "model-a", "analyzer_tier": "fixture"}],
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
        (replay / "period-manifest.json").write_bytes(
            (source / "period-manifest.json").read_bytes()
        )
        semantic_fixture = replay / "replay-fixture" / "semantic-analysis.json"
        semantic_fixture.parent.mkdir()
        semantic_fixture.write_bytes((source / "semantic-analysis.json").read_bytes())
        write_json(replay / "replay-source.json", {
            "schema_version": 1,
            "source_run_id": source.name,
            "source_run_dir": str(source.resolve()),
            "source_manifest_id": ledger["manifest"]["manifest_id"],
            "source_events_digest": ledger["manifest"]["events_digest"],
            "ledger_file_sha256": hashlib.sha256(
                (source / "evidence" / "evidence-ledger.json").read_bytes()
            ).hexdigest(),
            "semantic_analysis_sha256": hashlib.sha256(
                (source / "semantic-analysis.json").read_bytes()
            ).hexdigest(),
            "semantic_analysis_fixture": "replay-fixture/semantic-analysis.json",
            "work_accounting_result_sha256": hashlib.sha256(
                (source / "work-accounting-result.json").read_bytes()
            ).hexdigest(),
        })
        return source, replay

    @staticmethod
    def _write_real_offline_replay_source(runs: Path, root: Path) -> Path:
        source = runs / "source-run"
        source.mkdir(parents=True)
        inventory = {
            "clockify": {"status": "complete"},
            "fathom": {"status": "complete"},
            "multica_issues": {"status": "complete"},
        }
        event = evidence_ledger.evidence_event(
            "codex_sessions_event",
            {"source_type": "codex_sessions", "source_id": "offline-1", "machine": "fixture", "session_id": "session-1"},
            observed_at="2026-08-01T10:00:00Z",
            raw_source_span={"start": "2026-08-01T10:00:00Z", "end": "2026-08-01T10:10:00Z", "path": "/offline/replay.jsonl"},
            attributes={"role": "user", "kind": "message", "content": "Validate offline replay"},
        )
        ledger = evidence_ledger.EvidenceLedger((event,), inventory)
        write_json(source / "evidence" / "evidence-ledger.json", {
            "schema_version": ledger.manifest.schema_version,
            "manifest": ledger.manifest.document(),
            "events": [event.document()],
        })
        write_json(source / "run-report.json", {
            "run_id": source.name,
            "runtime_identity": {"git_sha": "fixture"},
            "date_range": {
                "since": "2026-08-01T00:00:00Z",
                "until": "2026-08-02T00:00:00Z",
            },
            "evidence_ledger": {
                "source_completeness": ledger.manifest.document()["source_completeness"],
            },
        })
        (source / "run-report.md").write_text("# offline replay source\n")
        routing = {
            "session_routes": [{
                "pattern": "offline", "project_name": "Serenichron Level 2",
                "prefix": "SC", "tag_names": ["Processes"], "billable": True,
            }],
            "meeting_routes": [],
        }
        write_json(source / "routing.json", routing)
        (source / "review-corrections.jsonl").write_text("")
        (source / "review-acceptance.jsonl").write_text("")
        cache_path = source / "analyzer-cache-used.jsonl"
        cache = semantic_analyzer.AnalyzerResponseCache(cache_path)
        endpoint = semantic_analyzer.AnalyzerEndpoint(
            name="clockify_analyzer_primary", url="https://offline.invalid/v1/chat/completions",
            model=semantic_analyzer.DEFAULT_PRIMARY_MODEL,
            revision=semantic_analyzer.DEFAULT_PRIMARY_REVISION,
        )
        def transport(_endpoint, body):
            payload = json.loads(body["messages"][-1]["content"])
            return {"probe": "ok"} if payload.get("probe") else analyzer_provider_response(payload)

        analysis = semantic_analyzer.analyze_tiered(
            work_accounting_pipeline._with_semantic_route_hints([event.document()], routing),
            primary=endpoint, transport=transport,
            private_text_approved=True, cache=cache, max_workers=1,
            review_taxonomy=[{
                "project_name": "Serenichron Level 2", "prefix": "SC",
                "tag_names": ["Processes"], "billable": True,
                "selection_guidance": ["offline"],
            }],
        )
        cache_content = cache_path.read_bytes()
        analysis["analyzer_cache"]["snapshot"] = {
            "path": "analyzer-cache-used.jsonl",
            "record_count": len(cache_content.splitlines()),
            "sha256": hashlib.sha256(cache_content).hexdigest(),
        }
        fixture = root / "sealed-analysis.json"
        write_json(fixture, analysis)
        completed = subprocess.run([
            sys.executable, str(ROOT / "scripts" / "work_accounting_pipeline.py"),
            str(source), "--root", str(ROOT), "--routing", str(source / "routing.json"),
            "--corrections", str(source / "review-corrections.jsonl"),
            "--analysis-fixture", str(fixture),
        ], cwd=ROOT, text=True, capture_output=True, check=False)
        if completed.returncode:
            raise AssertionError(completed.stderr or completed.stdout)
        quality = subprocess.run([
            sys.executable, str(ROOT / "scripts" / "clockify_sync_quality.py"),
            source.name, "--runs-root", str(runs), "--root", str(ROOT),
            "--routing", str(source / "routing.json"), "--strict",
        ], cwd=ROOT, text=True, capture_output=True, check=False)
        if quality.returncode:
            raise AssertionError(quality.stderr or quality.stdout)
        state = subprocess.run([
            sys.executable, str(ROOT / "scripts" / "clockify_review_state.py"),
            str(source), "--state", str(root / "source-items.json"),
        ], cwd=ROOT, text=True, capture_output=True, check=False)
        if state.returncode:
            raise AssertionError(state.stderr or state.stdout)
        slice_ = review_run.clockify_sync_collect.plan_slices(
            dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 8, 2, tzinfo=dt.timezone.utc),
            zone=review_run.clockify_sync_collect.BUCHAREST,
        )[0]
        bundle = collector_receipts.build_completion_bundle(source, slice_=slice_)
        bundle_path = source / "completion-bundle.json"
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
        manifest["manifest_digest"] = "sha256:" + hashlib.sha256(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        write_json(source / "period-manifest.json", manifest)
        return source

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
            ) as run, mock.patch.object(
                review_run,
                "_prepare_collector_derivation_run",
                side_effect=lambda source, snapshots: source,
            ), mock.patch.object(
                review_run, "_adopt_completed_collector_derivation", return_value=None
            ):
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
                review_run,
                "_prepare_collector_derivation_run",
                side_effect=lambda source, snapshots: source,
            ), mock.patch.object(
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

    def test_fresh_run_accepts_collecting_period_without_completion_bundles(self):
        """A new period must bootstrap before its own bundles can exist."""
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            run_dir = runs / "run-collecting-manifest"
            run_dir.mkdir(parents=True)
            (run_dir / "run-report.md").write_text("# fixture\n", encoding="utf-8")
            write_json(run_dir / "run-report.json", {
                "evidence": {"calendly": {"status": "excluded", "complete": True}},
                "evidence_ledger": {
                    "source_completeness": {"status": "complete", "incomplete_sources": []}
                },
            })
            write_json(run_dir / "evidence" / "evidence-ledger.json", {
                "manifest": {
                    "source_completeness": {"status": "complete", "incomplete_sources": []}
                }
            })
            inputs = Path(tmp) / "inputs"
            self._write_reconciliation_snapshots(inputs)
            manifest = json.loads((inputs / "period-manifest.json").read_text())
            manifest.update({
                "state": "collecting",
                "event_count": 1,
                "events_digest": "sha256:" + "a" * 64,
                "artifacts": [],
                "blockers": [],
            })
            unsigned = {key: value for key, value in manifest.items() if key != "manifest_digest"}
            manifest["manifest_digest"] = "sha256:" + hashlib.sha256(
                json.dumps(
                    unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            write_json(inputs / "period-manifest.json", manifest)
            collected = subprocess.CompletedProcess(
                args=["collector"], returncode=0,
                stdout=str(run_dir / "run-report.md") + "\n", stderr="",
            )
            result_path = run_dir / "autopilot-result.json"

            with mock.patch.object(review_run, "RUNS", runs), mock.patch.object(
                review_run, "_run", return_value=collected
            ), mock.patch.object(
                review_run,
                "_prepare_collector_derivation_run",
                side_effect=lambda source, snapshots: source,
            ), mock.patch.object(
                review_run, "_process_run", return_value=(0, result_path)
            ) as process_run:
                code = review_run.main([
                    "--period-manifest", str(inputs / "period-manifest.json"),
                    "--routing", str(inputs / "routing.json"),
                    "--corrections", str(inputs / "review-corrections.jsonl"),
                    "--acceptance-ledger", str(inputs / "review-acceptance.jsonl"),
                ])

            self.assertEqual(0, code)
            process_run.assert_called_once()

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
