from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from scripts import clockify_review_cycle as cycle
from scripts import clockify_review_run as review
from scripts import clockify_source_debt_recover as recovery
from scripts import clockify_sync_collect as collector
from scripts import collector_receipts, collector_slices, source_coverage
from scripts.autopilot_process import ChildResult
from test_review_cycle_delivery import (
    make_run, publisher_result_for_command, write_json,
)


class ReviewCycleSourceDebtEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.runs = self.root / "runs"
        self.state_dir = self.root / "state"
        self.cache = self.root / "cache"
        self.cache.mkdir(parents=True)
        self.checkpoints = self.state_dir / "collector-checkpoints"
        self.routing = {
            "workspace_id": "workspace-1", "member_id": "member-1",
            "skip_rules": {}, "session_routes": [], "meeting_routes": [],
        }
        self.fleet = {
            "machines": [{"name": "macbook", "enabled": True, "kind": "ssh"}],
            "ssh_options": [],
        }
        write_json(self.root / "routing.json", self.routing)
        write_json(self.root / "fleet.json", self.fleet)
        write_json(self.root / "corrections.jsonl", {})
        write_json(self.root / "acceptance.jsonl", {})
        self.config = {
            "root": str(self.root), "state_dir": str(self.state_dir),
            "cache": str(self.cache), "routing": str(self.root / "routing.json"),
            "corrections": str(self.root / "corrections.jsonl"),
            "acceptance": str(self.root / "acceptance.jsonl"),
            "workspace_id": "workspace-1", "member_id": "member-1",
            "recovery_since": "2026-09-07", "catchup_until": "2026-09-13",
            "timezone": "Europe/Bucharest", "spreadsheet_id": "sheet-1",
            "monthly_sheet_title_template": "{month_name} {year} portfolio review",
            "calendly_optional": True, "max_slices": 1,
            "total_child_budget_seconds": 7200,
        }
        patches = (
            mock.patch.object(collector, "ROOT", self.root),
            mock.patch.object(collector, "RUNS", self.runs),
            mock.patch.object(recovery, "ROOT", self.root),
            mock.patch.object(recovery, "RUNS", self.runs),
            mock.patch.object(review, "ROOT", self.root),
            mock.patch.object(review, "RUNS", self.runs),
            mock.patch.dict(
                collector.os.environ,
                {
                    "CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT": str(self.checkpoints),
                    "CLOCKIFY_AUTOPILOT_COORDINATOR": "omarchy-precision",
                },
            ),
            mock.patch.object(collector, "fetch_clockify", return_value={"status": "ok", "complete": True, "entries": []}),
            mock.patch.object(collector, "fetch_fathom", return_value={"status": "ok", "complete": True, "meetings": []}),
            mock.patch.object(collector, "fetch_multica_issues", return_value={"status": "ok", "complete": True, "issues": []}),
            mock.patch.object(collector, "machine_is_local", return_value=False),
            mock.patch.object(collector, "collector_runtime_identity", return_value={"collector_path": "/repo/collector.py", "git_sha": "fixture", "dirty": False}),
            mock.patch.object(collector, "load_env_file", return_value={"_missing": True}),
        )
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)

    @staticmethod
    def failed_peer() -> dict[str, object]:
        return {
            "machine": "macbook", "status": "error", "complete": False,
            "collector_contract": "canonical_export_v1", "claude_bursts": [],
            "hermes_sessions": [], "hermes_db_sessions": [], "codex_sessions": [],
            "repository_events": [], "repository_evidence_status": "unavailable",
            "errors": ["bounded peer timeout"],
        }

    @staticmethod
    def healthy_peer() -> dict[str, object]:
        return {
            "machine": "macbook", "status": "ok", "complete": True,
            "collector_contract": "canonical_export_v1", "claude_bursts": [],
            "hermes_sessions": [], "hermes_db_sessions": [], "codex_sessions": [],
            "repository_events": [], "repository_evidence_status": "complete", "errors": [],
        }

    @staticmethod
    def repository_only_peer() -> dict[str, object]:
        return {
            "machine": "macbook", "status": "error", "complete": False,
            "collector_contract": "canonical_export_v1", "claude_bursts": [],
            "hermes_sessions": [], "hermes_db_sessions": [], "codex_sessions": [],
            "repository_events": [], "repository_evidence_status": "complete",
            "errors": ["session export unavailable"],
        }

    @staticmethod
    def session_only_peer() -> dict[str, object]:
        return {
            "machine": "macbook", "status": "ok", "complete": True,
            "collector_contract": "canonical_export_v1", "claude_bursts": [],
            "hermes_sessions": [], "hermes_db_sessions": [], "codex_sessions": [],
            "repository_events": [], "repository_evidence_status": "unavailable",
            "errors": [],
        }

    def _write_result(
        self, run_dir: Path, bundle: collector_receipts.SliceCompletionBundle,
        *, recovery_identity: dict[str, str] | None = None, replay: bool = False,
    ) -> Path:
        coverage = collector_receipts.completion_coverage(bundle)
        result = {
            "schema_version": 1, "run_id": run_dir.name,
            "run_dir": str(run_dir.resolve()), "quality_status": "pass",
            "date_range": {"since": bundle.since_utc, "until": bundle.until_utc},
            "source_completeness": coverage,
            "completion_bundle_digest": bundle.bundle_digest,
            "completion_bundle": bundle.document(),
            "paths": {
                "quality_report": str((run_dir / "quality_report.json").resolve()),
                "evidence_ledger": str((run_dir / "evidence" / "evidence-ledger.json").resolve()),
                "semantic_analysis": str((run_dir / "semantic-analysis.json").resolve()),
                "work_accounting_result": str((run_dir / "work-accounting-result.json").resolve()),
                "review_snapshot": str((run_dir / "review-snapshot.json").resolve()),
                "replay_integrity": (
                    str((run_dir / "replay-integrity.json").resolve()) if replay else None
                ),
            },
        }
        if recovery_identity is not None:
            result["source_debt_recovery"] = recovery_identity
        path = run_dir / "autopilot-result.json"
        write_json(path, result)
        return path

    def _seed_real_parent(self, peer: dict[str, object] | None = None) -> Path:
        manifest = cycle._ensure_period(
            self.config, self.state_dir, "2026-09-07", "2026-09-09",
            bind_inputs=True,
        )
        coverage = {
            "status": "incomplete",
            "sources": {
                "sessions/macbook": {"status": "unavailable"},
                "repositories/macbook": {"status": "unavailable"},
            },
            "incomplete_sources": ["repositories/macbook", "sessions/macbook"],
        }
        template_result = make_run(
            self.root, "artifact-template", replay=False, coverage=coverage,
            since=dt.date(2026, 9, 7), until=dt.date(2026, 9, 9),
            compatibility_version="artifact-template/v1",
        )
        template = template_result.parent
        zone = ZoneInfo("Europe/Bucharest")
        since = dt.datetime(2026, 9, 7, tzinfo=zone)
        until = dt.datetime(2026, 9, 9, tzinfo=zone)
        slices = collector.plan_slices(since, until, zone=zone)
        compatibility = collector._backlog_compatibility_version(
            self.routing, self.fleet, calendly_optional=True,
            coordinator="omarchy-precision",
        )
        identity = collector.BacklogIdentity(
            since_utc=collector.iso_utc(since), until_utc=collector.iso_utc(until),
            timezone=zone.key, max_days=2, compatibility_version=compatibility,
        )
        store = collector.BacklogStore(self.checkpoints)
        backlog = store.open(identity, slices)
        parent = collector._slice_run_dir(slices[0], compatibility)
        with mock.patch.object(
            collector, "collect_remote_sessions",
            return_value=peer if peer is not None else self.failed_peer(),
        ):
            collector._collect_slice(
                argparse.Namespace(enrich=False, calendly_optional=True),
                self.routing, self.fleet, {"_missing": True}, {"_missing": True},
                since, until, "fixture",
                collector.PageCheckpointStore(backlog.directory / "source-checkpoints"),
                parent, calendly_env={"_missing": True}, coordinator="omarchy-precision",
            )
        collector._write_pending_slice_finalization(parent, identity, slices[0])
        for name, source in {
            "period-manifest.json": manifest,
            "routing.json": self.root / "routing.json",
            "review-corrections.jsonl": self.root / "corrections.jsonl",
            "review-acceptance.jsonl": self.root / "acceptance.jsonl",
        }.items():
            shutil.copyfile(source, parent / name)
        for name in (
            "semantic-analysis.json", "work-accounting-result.json", "quality_report.json",
            "review-snapshot.json", "proposals.json", "fathom-reconciliation.json",
        ):
            shutil.copyfile(template / name, parent / name)
        bundle = collector_receipts.build_completion_bundle(parent, slice_=slices[0])
        bundle_path = parent / "completion-bundle.json"
        collector_receipts.write_completion_bundle(bundle_path, bundle)
        store.record_complete(
            backlog, slices[0].slice_id, bundle_path.resolve(),
            "sha256:" + hashlib.sha256(bundle_path.read_bytes()).hexdigest(),
        )
        result_path = self._write_result(parent, bundle)
        expected = cycle._expected_snapshot_digests(self.config, manifest)
        stage = cycle._validate_stage(
            self.config, result_path, "2026-09-07", "2026-09-09", replay=False,
            expected_snapshot_digests=expected,
        )
        debts = source_coverage.SourceDebtStore()
        self.assertTrue(cycle._record_exact_debts(self.config, debts, stage))
        source_coverage.write(self.state_dir / "source-coverage.json", debts.document())
        cycle._atomic(self.state_dir / "review-cycle-state.json", {
            "schema_version": cycle.SCHEMA_VERSION, "completed_through": None,
            "scheduled_through": "2026-09-09", "next_work_class": "routine",
            "slices": {
                "2026-09-07": {
                    "until": "2026-09-09", "period_manifest": str(manifest),
                    "expected_snapshot_digests": expected, "status": "recovery_blocked",
                    "source": stage, "source_completeness": stage["coverage"],
                    "exception_ids": stage["exception_ids"], "exceptions_complete": True,
                }
            },
        })
        return parent

    def _finish_real_recovery(
        self, parent: Path, source: str, attempt_id: str,
        *, peer: dict[str, object] | None = None,
    ) -> Path:
        with mock.patch.object(
            collector, "collect_remote_sessions",
            return_value=peer if peer is not None else self.healthy_peer(),
        ):
            derived = recovery.recover(parent, source, attempt_id).run_dir
        review._snapshot_recovery_inputs(derived, parent)
        for name in (
            "semantic-analysis.json", "work-accounting-result.json", "quality_report.json",
            "review-snapshot.json", "proposals.json", "fathom-reconciliation.json",
        ):
            shutil.copyfile(parent / name, derived / name)
        bundle = review._finalize_recovery_completion(derived)
        transition = json.loads((derived / "run-report.json").read_text())["source_debt_recovery"]
        status = review._recovery_source_status(bundle, source)
        result = self._write_result(derived, bundle, recovery_identity={
            "source": source, "attempt_id": attempt_id, "status": status,
            "transition_digest": transition["transition_digest"],
        })
        recovery.seal_recovery_receipt(derived)
        return result

    def _make_replay(self, source_dir: Path, number: int) -> Path:
        source_bundle = collector_receipts.load_completion_bundle(
            source_dir / "completion-bundle.json", run_dir=source_dir
        )
        zone = ZoneInfo("Europe/Bucharest")
        since = dt.datetime.fromisoformat(source_bundle.since_utc.replace("Z", "+00:00"))
        until = dt.datetime.fromisoformat(source_bundle.until_utc.replace("Z", "+00:00"))
        provisional = make_run(
            self.root, f"replay-{number}", replay=True,
            source_name=source_dir.name, since=since.astimezone(zone).date(),
            until=until.astimezone(zone).date(), snapshots_from=source_dir,
            replay_integrity_override={},
        )
        replay_dir = provisional.parent
        shutil.copyfile(
            source_dir / "evidence" / "evidence-ledger.json",
            replay_dir / "evidence" / "evidence-ledger.json",
        )
        shutil.copyfile(source_dir / "run-report.json", replay_dir / "run-report.json")
        fixture = replay_dir / "replay-fixture" / "semantic-analysis.json"
        fixture.parent.mkdir()
        fixture.write_bytes((source_dir / "semantic-analysis.json").read_bytes())
        ledger = json.loads(
            (source_dir / "evidence" / "evidence-ledger.json").read_text()
        )
        write_json(replay_dir / "replay-source.json", {
            "schema_version": 1,
            "source_run_id": source_dir.name,
            "source_run_dir": str(source_dir.resolve()),
            "source_manifest_id": ledger["manifest"]["manifest_id"],
            "source_events_digest": ledger["manifest"]["events_digest"],
            "ledger_file_sha256": hashlib.sha256(
                (source_dir / "evidence" / "evidence-ledger.json").read_bytes()
            ).hexdigest(),
            "semantic_analysis_sha256": hashlib.sha256(
                (source_dir / "semantic-analysis.json").read_bytes()
            ).hexdigest(),
            "semantic_analysis_fixture": "replay-fixture/semantic-analysis.json",
            "work_accounting_result_sha256": hashlib.sha256(
                (source_dir / "work-accounting-result.json").read_bytes()
            ).hexdigest(),
        })
        review._verify_replay_integrity(source_dir, replay_dir)
        (replay_dir / "completion-bundle.json").unlink()
        slice_ = argparse.Namespace(
            slice_id=source_bundle.slice_id,
            since=dt.datetime.fromisoformat(source_bundle.since_utc.replace("Z", "+00:00")),
            until=dt.datetime.fromisoformat(source_bundle.until_utc.replace("Z", "+00:00")),
        )
        bundle = collector_receipts.build_completion_bundle(replay_dir, slice_=slice_, replay=True)
        collector_receipts.write_completion_bundle(replay_dir / "completion-bundle.json", bundle)
        return self._write_result(replay_dir, bundle, replay=True)

    def _prepared_promotion(self, *, published: bool = True, complete: bool = True):
        """Use real source/replay/publication and external recovery receipts."""
        parent = self._seed_real_parent()
        state_path = self.state_dir / "review-cycle-state.json"
        debt_path = self.state_dir / "source-coverage.json"
        state = json.loads(state_path.read_text())
        record = state["slices"]["2026-09-07"]
        source = record["source"]
        if published:
            replay_path = self._make_replay(parent, 1)
            replay = cycle._validate_stage(
                self.config, replay_path, "2026-09-07", "2026-09-09",
                replay=True, expected_snapshot_digests=source["snapshot_digests"],
                source_run_id=source["run_id"], source_run_dir=source["run_dir"],
            )
            command = cycle._replay_command(self.config, parent)
            returned = {
                "schema_version": "review-cycle-replay-return/v1",
                "source_digest": cycle._value_digest(source),
                "command_digest": cycle._value_digest(command),
                "result_path": str(replay_path),
                "result_digest": cycle._digest(replay_path),
            }
            returned["return_digest"] = cycle._value_digest(returned)
            receipt = cycle._delivery_document(
                self.config, "2026-09-07", "2026-09-09", source, replay,
                sheet_title="September 2026 portfolio review",
            )
            receipt_path = self.state_dir / "partial-publication-receipts" / "prior.json"
            cycle._write_delivery_receipt(receipt_path, receipt)
            record.update({
                "status": "published_with_source_gaps", "replay": replay,
                "replay_return": returned, "source_run_id": source["run_id"],
                "replay_run_id": replay["run_id"], "review_ids": source["review_ids"],
                "publication_receipt": str(receipt_path),
            })
        # An unrelated slice must never be changed by this promotion.
        state["slices"]["2026-09-09"] = {"until": "2026-09-11", "status": "incomplete", "untouched": [1, 2]}
        store = source_coverage.SourceDebtStore.from_document(source_coverage.read(debt_path))
        debt = store.active()[0]
        attempt, _command = cycle._recovery_attempt(record, debt, source, self.config)
        result = self._finish_real_recovery(
            parent, debt.interval.source, attempt["attempt_id"],
            peer=self.healthy_peer() if complete else self.repository_only_peer(),
        )
        stage, status = cycle._validate_recovery_stage(
            self.config, result, "2026-09-07", "2026-09-09", parent=source,
            debt=debt, attempt_id=attempt["attempt_id"],
        )
        attempt.update({
            "phase": "verified_" + status, "result_path": stage["result_path"],
            "result_digest": stage["result_digest"],
            "returned_bundle_digest": stage["bundle_digest"],
            "requested_source_outcome": status,
            "recovery_receipt_path": stage["recovery_receipt_path"],
            "recovery_receipt_digest": stage["recovery_receipt_digest"],
        })
        record["recovery_attempts"][debt.debt_id] = attempt
        cycle._persist_state(state_path, state, "2026-09-07", record)
        return state, record, store, debt, source, attempt, stage

    def _apply_prepared_promotion(self, prepared):
        state, record, store, debt, parent, attempt, _stage = prepared
        return cycle._apply_verified_recovery(
            self.config, state, self.state_dir / "review-cycle-state.json", record,
            store, self.state_dir / "source-coverage.json", "2026-09-07",
            "2026-09-09", debt, parent, attempt,
        )

    def test_promotion_replaces_foreign_replay_without_weakening_its_validator(self):
        """Catches promoted source retaining a replay sealed for its old source."""
        prepared = self._prepared_promotion()
        state, record, _store, _debt, parent, _attempt, stage = prepared
        prior_replay = dict(record["replay"])
        self.assertEqual("pass", review.derive_replay_integrity(
            Path(parent["run_dir"]), Path(prior_replay["run_dir"])
        )["status"])
        self.assertEqual("blocked", review.derive_replay_integrity(
            Path(stage["run_dir"]), Path(prior_replay["run_dir"])
        )["status"])
        self._apply_prepared_promotion(prepared)
        # The exact downstream validation must now see no active foreign replay.
        try:
            replay = cycle._stage_from_state(
                self.config, record, "replay", "2026-09-07", "2026-09-09", replay=True,
                expected_snapshot_digests=stage["snapshot_digests"],
                source_run_id=stage["run_id"], source_run_dir=stage["run_dir"],
            )
        except cycle.CycleError as exc:
            self.fail("promotion retained a foreign replay: " + str(exc))
        self.assertIsNone(replay)
        self.assertNotIn("replay_return", record)
        self.assertEqual({"until": "2026-09-11", "status": "incomplete", "untouched": [1, 2]}, state["slices"]["2026-09-09"])

    def test_promotion_atomically_preserves_authenticated_publication_history(self):
        """Catches losing prior publication evidence or persisting a mixed graph."""
        prepared = self._prepared_promotion()
        _state, record, _store, _debt, parent, _attempt, stage = prepared
        prior = json.loads(json.dumps(record))
        receipt_path = Path(prior["publication_receipt"])
        receipt_bytes = receipt_path.read_bytes()
        self._apply_prepared_promotion(prepared)
        persisted = json.loads((self.state_dir / "review-cycle-state.json").read_text())["slices"]["2026-09-07"]
        self.assertEqual("source_verified", persisted["status"])
        self.assertEqual(stage["run_id"], persisted["source"]["run_id"])
        history = persisted["source_recovery_history"]
        self.assertEqual(1, len(history))
        for field in ("source", "replay", "replay_return", "publication_receipt", "source_run_id", "replay_run_id", "review_ids"):
            self.assertEqual(prior[field], history[0][field])
            if field != "source":
                self.assertNotIn(field, persisted)
        self.assertEqual(receipt_bytes, receipt_path.read_bytes())
        cycle._verify_delivery_receipt(
            receipt_path, self.config, "2026-09-07", "2026-09-09", parent,
            history[0]["replay"], sheet_title="September 2026 portfolio review",
        )

    def test_promotion_write_crash_retries_once_and_retains_new_replay(self):
        """Catches partial source/history writes or repeated promotion erasing new replay."""
        prepared = self._prepared_promotion()
        state_path = self.state_dir / "review-cycle-state.json"
        original_bytes = state_path.read_bytes()
        real_atomic = cycle._atomic
        def interrupt_promotion(path, document):
            if document["slices"]["2026-09-07"].get("status") == "source_verified":
                raise RuntimeError("before atomic promotion")
            return real_atomic(path, document)
        with mock.patch.object(cycle, "_atomic", side_effect=interrupt_promotion):
            with self.assertRaisesRegex(RuntimeError, "before atomic promotion"):
                self._apply_prepared_promotion(prepared)
        self.assertEqual(original_bytes, state_path.read_bytes())
        _state, _record, _store, _debt, parent, attempt, stage = prepared
        state = json.loads(state_path.read_text())
        record = state["slices"]["2026-09-07"]
        store = source_coverage.SourceDebtStore.from_document(source_coverage.read(self.state_dir / "source-coverage.json"))
        debt = store.get(_debt.debt_id)
        resumed = state, record, store, debt, parent, attempt, stage
        self._apply_prepared_promotion(resumed)
        replay_path = self._make_replay(Path(stage["run_dir"]), 2)
        new_replay = cycle._validate_stage(
            self.config, replay_path, "2026-09-07", "2026-09-09", replay=True,
            expected_snapshot_digests=stage["snapshot_digests"],
            source_run_id=stage["run_id"], source_run_dir=stage["run_dir"],
        )
        record.update(status="replay_verified", replay=new_replay)
        cycle._persist_state(state_path, state, "2026-09-07", record)
        before_retry = state_path.read_bytes()
        self._apply_prepared_promotion(resumed)
        self.assertEqual("replay_verified", record["status"])
        self.assertEqual(new_replay, record["replay"])
        self.assertEqual(1, len(record["source_recovery_history"]))
        self.assertEqual(before_retry, state_path.read_bytes())

    def test_promotion_rejects_unauthenticated_prior_publication(self):
        """Catches sealing a forged publication as historical evidence."""
        prepared = self._prepared_promotion()
        record = prepared[1]
        receipt_path = Path(record["publication_receipt"])
        forged = json.loads(receipt_path.read_text())
        forged["spreadsheet_id"] = "different-sheet"
        write_json(receipt_path, forged)
        state_path = self.state_dir / "review-cycle-state.json"
        before = state_path.read_bytes()
        with self.assertRaisesRegex(cycle.CycleError, "delivery receipt"):
            self._apply_prepared_promotion(prepared)
        self.assertEqual(before, state_path.read_bytes())

    def test_source_only_promotion_does_not_invent_publication_history(self):
        """Catches ordinary source-only recovery acquiring unrelated derivatives."""
        prepared = self._prepared_promotion(published=False)
        self._apply_prepared_promotion(prepared)
        record = prepared[1]
        self.assertEqual("source_verified", record["status"])
        self.assertEqual(prepared[6]["run_id"], record["source"]["run_id"])
        self.assertNotIn("source_recovery_history", record)
        self.assertNotIn("replay", record)
        self.assertNotIn("publication_receipt", record)

    def test_incomplete_recovery_preserves_active_prior_publication(self):
        """Catches invalidating a publication before recovery can replace its source."""
        prepared = self._prepared_promotion(complete=False)
        record = prepared[1]
        prior = {key: record[key] for key in ("source", "replay", "replay_return", "publication_receipt")}
        self._apply_prepared_promotion(prepared)
        self.assertEqual("incomplete", record["status"])
        self.assertEqual(prior, {key: record[key] for key in prior})
        self.assertNotIn("source_recovery_history", record)

    def test_new_runtime_classifies_exhausted_generic_parent_once(self) -> None:
        """Persisted generic debt converges into exact recovery without retry loops."""
        old_runtime = {
            "collector_path": "/repo/collector.py", "git_sha": "fixture", "dirty": False,
        }
        parent = self._seed_real_parent()
        legacy_state = json.loads(
            (self.state_dir / "review-cycle-state.json").read_text(encoding="utf-8")
        )
        self.assertNotIn(
            "runtime_identity_digest", legacy_state["slices"]["2026-09-07"]["source"]
        )
        # Reproduce the deployed failure: a newer executor rewrote a derived
        # artifact in the deterministic collector directory. Raw collection,
        # backlog receipt, and the legacy bundle document remain unchanged.
        (parent / "quality_report.json").write_text(
            '{"status":"blocked","summary":{"fixture":"derived drift"}}\n',
            encoding="utf-8",
        )

        generic = cycle._generic_interval(self.config, "2026-09-07", "2026-09-09")
        debts = source_coverage.SourceDebtStore()
        debts.record_failure(
            generic, failure_class="result_unverified", retryable=True,
            resume_state_digest="sha256:generic-attempt-1",
            attempted_at="2026-09-10T00:00:00Z",
        )
        exhausted = debts.record_failure(
            generic, failure_class="result_unverified", retryable=True,
            resume_state_digest="sha256:generic-attempt-2",
            attempted_at="2026-09-11T00:00:00Z",
        )
        debts.exhaust(exhausted.debt_id, terminal_reason="retry_limit")
        debt_path = self.state_dir / "source-coverage.json"
        source_coverage.write(debt_path, debts.document())

        self.config["_runtime_identity"] = {
            **old_runtime, "git_sha": "new-release",
        }
        self.config["catchup_until"] = "2026-09-09"
        commands: list[list[str]] = []
        def publish_available(command, **_kwargs):
            command = list(command)
            commands.append(command)
            self.assertIn("--replay-from", command)
            # Raw coverage can be classified, but the deliberately drifted
            # downstream quality artifact must not be resealed or published.
            return ChildResult(2, "", "quality artifact digest mismatch", False, 1.0)
        with mock.patch.object(
            cycle, "run_child_bounded",
            side_effect=publish_available,
        ):
            first = cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 14)
            )

        self.assertEqual("failed", first["status"])
        self.assertEqual(1, len(commands))
        self.assertIn("--replay-from", commands[0])
        after_gate = source_coverage.SourceDebtStore.from_document(
            source_coverage.read(debt_path)
        )
        self.assertEqual("resolved", after_gate.get(generic.debt_id).status)
        exact = [
            item for item in after_gate.active()
            if item.interval.source != "runner/unclassified"
        ]
        self.assertEqual(
            ["peer/macbook"],
            sorted(item.interval.source for item in exact),
        )
        state = json.loads(
            (self.state_dir / "review-cycle-state.json").read_text(encoding="utf-8")
        )
        record = state["slices"]["2026-09-07"]
        self.assertIsNone(state["completed_through"])
        self.assertNotIn("delivery_receipt", record)
        self.assertEqual(
            cycle._value_digest(old_runtime),
            record["source"]["runtime_identity_digest"],
        )
        self.assertEqual(
            {item.debt_id for item in exact}, set(record["recovery_parents"]),
        )

        later_commands: list[list[str]] = []
        timed_out = ChildResult(None, "", "suppressed", True, 0.1)

        def later_child(command, **_kwargs):
            later_commands.append(list(command))
            return timed_out

        state["next_work_class"] = "exact"
        cycle._atomic(self.state_dir / "review-cycle-state.json", state)
        with mock.patch.object(cycle, "run_child_bounded", side_effect=later_child):
            second = cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 14)
            )

        self.assertEqual("incomplete", second["status"])
        self.assertEqual(1, len(later_commands))
        self.assertIn("--recover-source-debt-from", later_commands[0])
        self.assertNotIn("--since", later_commands[0])

    def test_real_recovery_reopens_incomplete_resolved_peer_before_restart(self) -> None:
        parent = self._seed_real_parent()
        commands: list[list[str]] = []
        recovery_number = 0
        replay_number = 0
        reopened_parent: Path | None = None

        def first_recovery(command, **_kwargs):
            command = list(command)
            commands.append(command)
            source = command[command.index("--recover-source") + 1]
            attempt = command[command.index("--recover-attempt-id") + 1]
            self.assertEqual("peer/macbook", source)
            result = self._finish_real_recovery(
                parent, source, attempt, peer=self.repository_only_peer()
            )
            return ChildResult(0, str(result) + "\n", "", False, 1.0)

        state = json.loads((self.state_dir / "review-cycle-state.json").read_text())
        state["next_work_class"] = "exact"
        cycle._atomic(self.state_dir / "review-cycle-state.json", state)
        with mock.patch.object(cycle, "run_child_bounded", side_effect=first_recovery):
            first = cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 14)
            )
        self.assertEqual("incomplete", first["status"])

        real_debt_write = source_coverage.write
        interrupted = False

        def write_reopened_then_interrupt(path, document):
            nonlocal interrupted
            real_debt_write(path, document)
            completed_peer = any(
                event["event"] == "complete"
                and event["interval"]["source"] == "peer/macbook"
                for event in document["events"]
            )
            if completed_peer and not interrupted:
                interrupted = True
                raise RuntimeError("after real peer reopen write")

        def second_recovery(command, **_kwargs):
            nonlocal reopened_parent
            command = list(command)
            commands.append(command)
            source = command[command.index("--recover-source") + 1]
            attempt = command[command.index("--recover-attempt-id") + 1]
            self.assertEqual("peer/macbook", source)
            recovery_parent = Path(
                command[command.index("--recover-source-debt-from") + 1]
            )
            result = self._finish_real_recovery(
                recovery_parent, source, attempt, peer=self.healthy_peer()
            )
            reopened_parent = result.parent
            return ChildResult(0, str(result) + "\n", "", False, 1.0)

        state = json.loads((self.state_dir / "review-cycle-state.json").read_text())
        state["next_work_class"] = "exact"
        cycle._atomic(self.state_dir / "review-cycle-state.json", state)
        with mock.patch.object(cycle, "run_child_bounded", side_effect=second_recovery), \
             mock.patch.object(source_coverage, "write", side_effect=write_reopened_then_interrupt):
            with self.assertRaisesRegex(RuntimeError, "after real peer reopen write"):
                cycle.run_cycle(
                    self.config, enable_sheet_write=True, today=dt.date(2026, 9, 14)
                )

        after_crash = source_coverage.SourceDebtStore.from_document(
            source_coverage.read(self.state_dir / "source-coverage.json")
        )
        self.assertEqual((), after_crash.active())

        def converging_child(command, **_kwargs):
            nonlocal recovery_number, replay_number
            command = list(command)
            commands.append(command)
            if "--recover-source-debt-from" in command:
                recovery_number += 1
                source = command[command.index("--recover-source") + 1]
                attempt = command[command.index("--recover-attempt-id") + 1]
                recovery_parent = Path(
                    command[command.index("--recover-source-debt-from") + 1]
                )
                self.assertEqual(reopened_parent, recovery_parent)
                result = self._finish_real_recovery(recovery_parent, source, attempt)
                return ChildResult(0, str(result) + "\n", "", False, 1.0)
            if "--replay-from" in command:
                replay_number += 1
                source_dir = Path(command[command.index("--replay-from") + 1])
                result = self._make_replay(source_dir, replay_number)
                return ChildResult(0, str(result) + "\n", "", False, 1.0)
            self.assertIn("clockify_sheet_publish.py", command[1])
            return publisher_result_for_command(
                self.config, command, duration=1.0,
            )

        state = json.loads((self.state_dir / "review-cycle-state.json").read_text())
        state["next_work_class"] = "exact"
        cycle._atomic(self.state_dir / "review-cycle-state.json", state)
        with mock.patch.object(cycle, "run_child_bounded", side_effect=converging_child):
            final = cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 14)
            )

        debt_store = source_coverage.SourceDebtStore.from_document(
            source_coverage.read(self.state_dir / "source-coverage.json")
        )
        recovery_commands = [
            command for command in commands
            if "--recover-source-debt-from" in command
        ]
        recovery_sources = [
            command[command.index("--recover-source") + 1]
            for command in recovery_commands
        ]
        peer_attempts = [
            command[command.index("--recover-attempt-id") + 1]
            for command in recovery_commands
            if command[command.index("--recover-source") + 1] == "peer/macbook"
        ]
        peer_events = [
            event["event"] for event in debt_store.document()["events"]
            if event["interval"]["source"] == "peer/macbook"
        ]
        self.assertEqual("delivered", final["status"])
        self.assertEqual((), debt_store.active())
        self.assertEqual(["peer/macbook", "peer/macbook"], recovery_sources)
        self.assertEqual(2, len(peer_attempts))
        self.assertEqual(2, len(set(peer_attempts)))
        self.assertEqual(
            ["failure", "failure", "complete"], peer_events
        )
        self.assertEqual(1, replay_number)
        self.assertEqual(1, len(list((self.state_dir / "delivery-receipts").glob("*.json"))))

    def test_rotating_peer_debt_uses_derived_parent_after_write_crash(self) -> None:
        original_parent = self._seed_real_parent(peer=self.repository_only_peer())
        state_path = self.state_dir / "review-cycle-state.json"
        debt_path = self.state_dir / "source-coverage.json"
        state = json.loads(state_path.read_text())
        state["next_work_class"] = "exact"
        cycle._atomic(state_path, state)

        commands: list[list[str]] = []
        first_derived: Path | None = None
        interrupted = False
        real_debt_write = source_coverage.write

        def first_child(command, **_kwargs):
            nonlocal first_derived
            command = list(command)
            commands.append(command)
            self.assertEqual("peer/macbook", command[command.index("--recover-source") + 1])
            self.assertEqual(
                original_parent,
                Path(command[command.index("--recover-source-debt-from") + 1]),
            )
            attempt = command[command.index("--recover-attempt-id") + 1]
            result = self._finish_real_recovery(
                original_parent, "peer/macbook", attempt,
                peer=self.session_only_peer(),
            )
            first_derived = result.parent
            return ChildResult(0, str(result) + "\n", "", False, 1.0)

        def write_peer_then_interrupt(path, document):
            nonlocal interrupted
            real_debt_write(path, document)
            active_sources = {
                item.interval.source
                for item in source_coverage.SourceDebtStore.from_document(document).active()
            }
            if "peer/macbook" in active_sources and not interrupted:
                interrupted = True
                raise RuntimeError("after rotated peer debt write")

        with mock.patch.object(cycle, "run_child_bounded", side_effect=first_child), \
             mock.patch.object(source_coverage, "write", side_effect=write_peer_then_interrupt):
            with self.assertRaisesRegex(RuntimeError, "after rotated peer debt write"):
                cycle.run_cycle(
                    self.config, enable_sheet_write=True, today=dt.date(2026, 9, 14)
                )

        self.assertIsNotNone(first_derived)
        crashed_state = json.loads(state_path.read_text())
        peer_debt = next(
            item for item in source_coverage.SourceDebtStore.from_document(
                source_coverage.read(debt_path)
            ).active()
            if item.interval.source == "peer/macbook"
        )
        self.assertEqual(
            str(first_derived),
            crashed_state["slices"]["2026-09-07"]["recovery_parents"][
                peer_debt.debt_id
            ]["run_dir"],
        )
        crashed_state["next_work_class"] = "exact"
        cycle._atomic(state_path, crashed_state)

        replay_number = 0

        def restarted_child(command, **_kwargs):
            nonlocal replay_number
            command = list(command)
            commands.append(command)
            if "--recover-source-debt-from" in command:
                source = command[command.index("--recover-source") + 1]
                self.assertEqual("peer/macbook", source)
                self.assertEqual(
                    first_derived,
                    Path(command[command.index("--recover-source-debt-from") + 1]),
                )
                attempt = command[command.index("--recover-attempt-id") + 1]
                result = self._finish_real_recovery(first_derived, source, attempt)
                return ChildResult(0, str(result) + "\n", "", False, 1.0)
            if "--replay-from" in command:
                replay_number += 1
                source_dir = Path(command[command.index("--replay-from") + 1])
                result = self._make_replay(source_dir, replay_number)
                return ChildResult(0, str(result) + "\n", "", False, 1.0)
            self.assertIn("clockify_sheet_publish.py", command[1])
            return publisher_result_for_command(
                self.config, command, duration=1.0,
            )

        with mock.patch.object(cycle, "run_child_bounded", side_effect=restarted_child):
            final = cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 14)
            )

        final_store = source_coverage.SourceDebtStore.from_document(
            source_coverage.read(debt_path)
        )
        recovery_commands = [
            command for command in commands if "--recover-source-debt-from" in command
        ]
        recovery_sources = [
            command[command.index("--recover-source") + 1]
            for command in recovery_commands
        ]
        peer_events = [
            event["event"] for event in final_store.document()["events"]
            if event["interval"]["source"] == "peer/macbook"
        ]
        self.assertEqual("delivered", final["status"])
        self.assertEqual((), final_store.active())
        self.assertEqual(
            ["peer/macbook", "peer/macbook"], recovery_sources
        )
        self.assertEqual(["failure", "failure", "complete"], peer_events)
        self.assertEqual(1, replay_number)
        self.assertEqual(1, len(list((self.state_dir / "delivery-receipts").glob("*.json"))))

    def test_real_overall_complete_recovery_clears_generic_and_delivers_once(self) -> None:
        config = {**self.config, "catchup_until": "2026-09-09"}
        parent = self._seed_real_parent(peer=self.session_only_peer())
        debt_path = self.state_dir / "source-coverage.json"
        debt_store = source_coverage.SourceDebtStore.from_document(
            source_coverage.read(debt_path)
        )
        generic = cycle._generic_interval(config, "2026-09-07", "2026-09-09")
        debt_store.record_failure(
            generic, failure_class="child_timeout", retryable=True,
            resume_state_digest="sha256:generic-before-exact-proof",
            attempted_at="2026-09-09T00:00:00Z",
        )
        source_coverage.write(debt_path, debt_store.document())
        state = json.loads((self.state_dir / "review-cycle-state.json").read_text())
        state["next_work_class"] = "exact"
        cycle._atomic(self.state_dir / "review-cycle-state.json", state)

        commands: list[list[str]] = []
        replay_number = 0

        def child(command, **_kwargs):
            nonlocal replay_number
            command = list(command)
            commands.append(command)
            if "--recover-source-debt-from" in command:
                source = command[command.index("--recover-source") + 1]
                attempt = command[command.index("--recover-attempt-id") + 1]
                result = self._finish_real_recovery(parent, source, attempt)
                return ChildResult(0, str(result) + "\n", "", False, 1.0)
            if "--replay-from" in command:
                replay_number += 1
                source_dir = Path(command[command.index("--replay-from") + 1])
                result = self._make_replay(source_dir, replay_number)
                return ChildResult(0, str(result) + "\n", "", False, 1.0)
            self.assertIn("clockify_sheet_publish.py", command[1])
            return publisher_result_for_command(
                self.config, command, duration=1.0,
            )

        with mock.patch.object(cycle, "run_child_bounded", side_effect=child):
            result = cycle.run_cycle(
                config, enable_sheet_write=True, today=dt.date(2026, 9, 14)
            )
            calls_after_delivery = len(commands)
            repeat = cycle.run_cycle(
                config, enable_sheet_write=True, today=dt.date(2026, 9, 14)
            )

        final_store = source_coverage.SourceDebtStore.from_document(
            source_coverage.read(debt_path)
        )
        self.assertEqual("delivered", result["status"])
        self.assertEqual((), final_store.active())
        self.assertEqual(0, sum("--since" in command for command in commands))
        self.assertEqual(1, replay_number)
        self.assertEqual(1, len(list((self.state_dir / "delivery-receipts").glob("*.json"))))
        self.assertEqual("idle", repeat["status"])
        self.assertEqual(calls_after_delivery, len(commands))

    def test_exact_source_chain_preserves_completed_unrequested_peer(self) -> None:
        parent = self._seed_real_parent()
        first = self._finish_real_recovery(
            parent, "peer/macbook", "sha256:" + "3" * 64,
            peer=self.repository_only_peer(),
        ).parent
        first_bundle = collector_receipts.load_completion_bundle(
            first / "completion-bundle.json", run_dir=first
        )
        self.assertEqual(
            ["sessions/macbook"],
            collector_receipts.completion_coverage(first_bundle)["incomplete_sources"],
        )

        second = self._finish_real_recovery(
            first, "peer/macbook", "sha256:" + "4" * 64,
            peer=self.session_only_peer(),
        ).parent
        second_bundle = collector_receipts.load_completion_bundle(
            second / "completion-bundle.json", run_dir=second
        )
        second_coverage = collector_receipts.completion_coverage(second_bundle)
        self.assertEqual(
            ["repositories/macbook"], second_coverage["incomplete_sources"]
        )

        third = self._finish_real_recovery(
            second, "peer/macbook", "sha256:" + "5" * 64,
            peer=self.healthy_peer(),
        ).parent
        third_bundle = collector_receipts.load_completion_bundle(
            third / "completion-bundle.json", run_dir=third
        )
        coverage = collector_receipts.completion_coverage(third_bundle)
        self.assertEqual([], coverage["incomplete_sources"])
        self.assertEqual("complete", coverage["sources"]["repositories/macbook"]["status"])
        self.assertEqual(second, recovery.verify_recovery_run(third).parent_run_dir)

    def test_three_slice_lifecycle_uses_real_recovery_verifier_and_converges(self) -> None:
        parent = self._seed_real_parent()
        commands: list[list[str]] = []
        recovery_calls = 0
        replay_number = 0
        debt_write_interrupted = False

        def child(command, **_kwargs):
            nonlocal recovery_calls, replay_number
            command = list(command)
            commands.append(command)
            if "--recover-source-debt-from" in command:
                recovery_calls += 1
                if recovery_calls == 1:
                    return ChildResult(None, "", "suppressed", True, 1.0)
                source = command[command.index("--recover-source") + 1]
                attempt = command[command.index("--recover-attempt-id") + 1]
                recovery_parent = Path(
                    command[command.index("--recover-source-debt-from") + 1]
                )
                result = self._finish_real_recovery(recovery_parent, source, attempt)
                return ChildResult(0, str(result) + "\n", "", False, 1.0)
            if "--replay-from" in command:
                replay_number += 1
                source_dir = Path(command[command.index("--replay-from") + 1])
                result = self._make_replay(source_dir, replay_number)
                return ChildResult(0, str(result) + "\n", "", False, 1.0)
            if "clockify_sheet_publish.py" in command[1]:
                return publisher_result_for_command(
                    self.config, command, duration=1.0,
                )
            since = dt.date.fromisoformat(command[command.index("--since") + 1])
            until = dt.date.fromisoformat(command[command.index("--until") + 1]) + dt.timedelta(days=1)
            result = make_run(
                self.root, f"routine-{since}", replay=False, since=since, until=until,
            )
            return ChildResult(0, str(result) + "\n", "", False, 1.0)

        real_debt_write = source_coverage.write

        def write_completion_then_interrupt(path, document):
            nonlocal debt_write_interrupted
            real_debt_write(path, document)
            if (
                not debt_write_interrupted
                and any(event.get("event") == "complete" for event in document.get("events", []))
            ):
                debt_write_interrupted = True
                raise RuntimeError("after exact debt completion write")

        with mock.patch.object(cycle, "run_child_bounded", side_effect=child), \
             mock.patch.object(source_coverage, "write", side_effect=write_completion_then_interrupt):
            interruptions = 0
            for _ in range(7):
                try:
                    cycle.run_cycle(self.config, enable_sheet_write=True, today=dt.date(2026, 9, 14))
                except RuntimeError as exc:
                    self.assertEqual("after exact debt completion write", str(exc))
                    interruptions += 1
            before_repeat = len(commands)
            repeat = cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 14)
            )

        state = json.loads((self.state_dir / "review-cycle-state.json").read_text())
        debt_store = source_coverage.SourceDebtStore.from_document(
            source_coverage.read(self.state_dir / "source-coverage.json")
        )
        events = debt_store.document()["events"]
        attempt_ids = [
            command[command.index("--recover-attempt-id") + 1]
            for command in commands if "--recover-attempt-id" in command
        ]
        self.assertEqual(attempt_ids[0], attempt_ids[1])
        self.assertEqual(1, interruptions)
        self.assertEqual("2026-09-13", state["scheduled_through"])
        self.assertEqual("2026-09-13", state["completed_through"])
        self.assertEqual((), debt_store.active())
        self.assertEqual(1, sum(event["event"] == "complete" for event in events))
        self.assertEqual(3, len(list((self.state_dir / "delivery-receipts").glob("*.json"))))
        self.assertEqual("idle", repeat["status"])
        self.assertEqual(before_repeat, len(commands))


if __name__ == "__main__":
    unittest.main()
