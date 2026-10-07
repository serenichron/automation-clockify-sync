from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from scripts import clockify_review_cycle as cycle
from scripts import collector_receipts, collector_slices
from scripts import clockify_review_run as review_run
from scripts import semantic_analyzer
from scripts import evidence_ledger, source_coverage
from ops.systemd.user import clockify_review_cycle_release as release_helper
from scripts.autopilot_process import ChildResult
from task3_scenario_contract import assert_scenario_contract


SINCE_UTC = "2026-09-06T21:00:00Z"
UNTIL_UTC = "2026-09-08T21:00:00Z"


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def proposal() -> dict[str, object]:
    return {
        "review_activity_key": "wka-alpha",
        "allocation_segment": 1,
        "start": "2026-09-07T09:00:00+03:00",
        "end": "2026-09-07T10:00:00+03:00",
        "duration_minutes": 60,
        "client_project": "Serenichron Level 2",
        "tag_names": ["Delivery"],
        "activity_id": "activity-alpha",
        "confidence": "high",
        "description": "SC — review-cycle delivery",
    }


def publisher_result_for_command(
    config: dict[str, object], command: list[str], *, code: int = 0,
    duration: float = 0.1,
) -> ChildResult:
    if code:
        return ChildResult(code, "", "", False, duration)
    source_dir = Path(command[command.index("--proposals") + 1]).parent
    source = {
        "run_dir": str(source_dir),
        "run_id": command[command.index("--run-id") + 1],
    }
    title = command[command.index("--sheet-title") + 1]
    publications = cycle._expected_publication_receipts(
        config, source, sheet_title=title,
    )
    path = Path(command[command.index("--result-output") + 1])
    write_json(path, {
        "schema_version": "sheet-publication-result/v1",
        "status": "published",
        "external_writes": True,
        "clockify_writes": 0,
        "publications": publications,
    })
    return ChildResult(0, str(path) + "\n", "", False, duration)


def make_run(
    root: Path,
    name: str,
    *,
    runs_dir: Path | None = None,
    replay: bool,
    source_name: str = "source-run",
    proposals: list[dict[str, object]] | None = None,
    ambiguous: list[dict[str, object]] | None = None,
    coverage: dict[str, object] | None = None,
    since: dt.date = dt.date(2026, 9, 7),
    until: dt.date = dt.date(2026, 9, 9),
    snapshots_from: Path | None = None,
    snapshot_overrides: dict[str, object] | None = None,
    replay_integrity_override: dict[str, object] | None = None,
    accounting_overrides: dict[str, object] | None = None,
    accounting_remove: tuple[str, ...] = (),
    compatibility_version: str = "fixture-collector-lineage/v1",
    runtime_identity: dict[str, object] | None = None,
    collector_source_marker: bool = False,
    analyzer_tier: str = "primary",
    ledger_from: Path | None = None,
    record_checkpoint: bool = True,
) -> Path:
    run_dir = (runs_dir or root / "runs") / name
    run_dir.mkdir(parents=True, exist_ok=True)
    proposals = [proposal()] if proposals is None else proposals
    # Monthly evidence fixtures must cite real content-addressed ledger events;
    # older routed-only scenarios intentionally keep their minimal fixture.
    candidates = [*(ambiguous or []), *(p for p in proposals if p.get("routing_disposition") == "unresolved-routing")]
    monthly_events = []
    if candidates and ledger_from is None:
        for index, candidate in enumerate(candidates):
            event = evidence_ledger.evidence_event(
                "codex_session", {"source_id": f"fixture-{index}", "machine": "test"},
                observed_at="2026-09-07T09:00:00+03:00",
                raw_source_span={"start": "2026-09-07T09:00:00+03:00", "end": "2026-09-07T10:00:00+03:00"},
                attributes={"title": "Synthetic review evidence"},
            )
            monthly_events.append(event)
            candidate.setdefault("evidence_ids", [event.evidence_id])
    coverage = (
        {"status": "complete", "incomplete_sources": []}
        if coverage is None
        else coverage
    )
    if ledger_from is not None:
        coverage = json.loads((ledger_from / "evidence/evidence-ledger.json").read_text())["manifest"]["source_completeness"]
    local = ZoneInfo("Europe/Bucharest")
    since_dt = dt.datetime.combine(since, dt.time(), local)
    until_dt = dt.datetime.combine(until, dt.time(), local)
    since_utc = since_dt.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    until_utc = until_dt.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    write_json(
        run_dir / "run-report.json",
        {
            "runtime_identity": runtime_identity or {"git_sha": "fixture-sha"},
            "date_range": {"since": since_utc, "until": until_utc},
            "evidence_ledger": {"source_completeness": coverage},
        },
    )
    (run_dir / "run-report.md").write_text("# synthetic report\n", encoding="utf-8")
    write_json(
        run_dir / "evidence" / "evidence-ledger.json",
        {
            "schema_version": "evidence-ledger/v1",
            "manifest": {
                "manifest_id": "elm-" + "a" * 64,
                "events_digest": "b" * 64,
                "source_completeness": coverage,
            },
            "events": [],
        },
    )
    if ledger_from is not None:
        shutil.copyfile(ledger_from / "evidence/evidence-ledger.json", run_dir / "evidence/evidence-ledger.json")
    elif monthly_events:
        ledger = evidence_ledger.EvidenceLedger(tuple(monthly_events), timezone="Europe/Bucharest")
        write_json(run_dir / "evidence/evidence-ledger.json", {
            "schema_version": "evidence-ledger/v1", "manifest": ledger.manifest.document(),
            "events": [event.document() for event in ledger.events],
        })
        report_path = run_dir / "run-report.json"
        report = json.loads(report_path.read_text())
        report["evidence_ledger"]["source_completeness"] = ledger.manifest.document()["source_completeness"]
        write_json(report_path, report)
    bundle_manifest = {
        "schema_version": "clockify-semantic-evidence-bundle/v1",
        "digest": semantic_analyzer.stable_digest("sebm-", [], length=64),
        "bundles": [],
    }
    write_json(
        run_dir / "semantic-analysis.json",
        {
            "schema_version": 1,
            "prompt_version": "prompt-v1",
            "evidence_bundle_schema_version": "clockify-semantic-evidence-bundle/v1",
            "evidence_bundle_manifest": bundle_manifest,
            "ledger_evidence_digest": "sha256:" + "c" * 64,
            "activities": [
                {"analyzer_model": "fixture-model", "analyzer_tier": analyzer_tier}
            ],
            "analysis_chunks": [],
            "analyzer_cache": {"records": []},
        },
    )
    accounting = {
        "schema_version": 1,
        "allocation_mode": "non_overlapping_v1",
        "ledger_manifest": {},
        "semantic_analysis": {},
        "proposals": proposals,
        "ambiguous": ambiguous or [],
        "skipped": [],
        "allocation": {},
        "fathom_reconciliation": [],
        "correction_regression": {},
        "external_writes": False,
    }
    accounting.update(accounting_overrides or {})
    for field in accounting_remove:
        accounting.pop(field, None)
    write_json(run_dir / "work-accounting-result.json", accounting)
    write_json(run_dir / "ambiguous.json", accounting.get("ambiguous", []))
    write_json(
        run_dir / "quality_report.json",
        {"status": "pass", "summary": {"total_proposals": len(proposals)}},
    )
    write_json(run_dir / "review-snapshot.json", {"fixture": "review"})
    write_json(run_dir / "proposals.json", proposals)
    write_json(run_dir / "fathom-reconciliation.json", [])
    snapshot_sources = {
        "period-manifest.json": root / "state" / f"{since.isoformat()}.period-manifest.json",
        "routing.json": root / "routing.json",
        "review-corrections.jsonl": root / "corrections.jsonl",
        "review-acceptance.jsonl": root / "acceptance.jsonl",
    }
    if snapshots_from is not None:
        snapshot_sources = {
            filename: snapshots_from / filename for filename in snapshot_sources
        }
    for filename, source in snapshot_sources.items():
        shutil.copyfile(source, run_dir / filename)
    for filename, value in (snapshot_overrides or {}).items():
        write_json(run_dir / filename, value)
    if replay:
        if replay_integrity_override is not None:
            write_json(run_dir / "replay-integrity.json", replay_integrity_override)
        else:
            active_runs = runs_dir or root / "runs"
            source_dir = active_runs / source_name
            fixture = run_dir / "replay-fixture" / "semantic-analysis.json"
            fixture.parent.mkdir()
            fixture.write_bytes((source_dir / "semantic-analysis.json").read_bytes())
            ledger = json.loads(
                (source_dir / "evidence" / "evidence-ledger.json").read_text()
            )
            write_json(run_dir / "replay-source.json", {
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
            with mock.patch.object(review_run, "RUNS", active_runs):
                review_run._verify_replay_integrity(source_dir, run_dir)
    slice_ = collector_slices.plan_slices(
        since_dt, until_dt, zone=local, max_days=2,
    )
    if len(slice_) != 1:
        raise AssertionError("review-cycle fixture must describe one bounded slice")
    slice_ = slice_[0]
    write_json(
        run_dir / "slice-finalization.json",
        {
            "schema_version": "collector-slice-finalization/v1",
            "backlog_identity": {
                "since_utc": since_utc,
                "until_utc": until_utc,
                "timezone": "Europe/Bucharest",
                "max_days": 2,
                "compatibility_version": compatibility_version,
            },
            "slice_id": slice_.slice_id,
            "since_utc": since_utc,
            "until_utc": until_utc,
        },
    )
    bundle = collector_receipts.build_completion_bundle(
        run_dir, slice_=slice_, replay=replay
    )
    collector_receipts.write_completion_bundle(run_dir / "completion-bundle.json", bundle)
    if not replay and record_checkpoint:
        identity = collector_slices.BacklogIdentity(
            since_utc=since_utc,
            until_utc=until_utc,
            timezone="Europe/Bucharest",
            max_days=2,
            compatibility_version=compatibility_version,
        )
        checkpoint_root = root / "state" / "collector-checkpoints"
        checkpoint_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        checkpoint_root.chmod(0o700)
        store = collector_slices.BacklogStore(checkpoint_root)
        backlog = store.open(identity, (slice_,))
        bundle_file_digest = "sha256:" + hashlib.sha256(
            (run_dir / "completion-bundle.json").read_bytes()
        ).hexdigest()
        store.record_complete(
            backlog, slice_.slice_id,
            (run_dir / "completion-bundle.json").resolve(), bundle_file_digest,
        )
    result = {
        "schema_version": 1,
        "run_id": name,
        "run_dir": str(run_dir.resolve()),
        "quality_status": "pass",
        "date_range": {"since": since_utc, "until": until_utc},
        "source_completeness": coverage,
        "completion_bundle_digest": bundle.bundle_digest,
        "completion_bundle": bundle.document(),
        "paths": {
            "quality_report": str((run_dir / "quality_report.json").resolve()),
            "evidence_ledger": str(
                (run_dir / "evidence" / "evidence-ledger.json").resolve()
            ),
            "semantic_analysis": str((run_dir / "semantic-analysis.json").resolve()),
            "work_accounting_result": str(
                (run_dir / "work-accounting-result.json").resolve()
            ),
            "review_snapshot": str((run_dir / "review-snapshot.json").resolve()),
            "replay_integrity": (
                str((run_dir / "replay-integrity.json").resolve()) if replay else None
            ),
        },
    }
    write_json(run_dir / "autopilot-result.json", result)
    if collector_source_marker:
        write_json(run_dir / "collector-source.json", {})
    return run_dir / "autopilot-result.json"


class ReviewCycleDeliveryTests(unittest.TestCase):
    def routing_transition_fixture(self, *, create_source=True, release_collector=False):
        """Real completed source plus a sealed immutable synthetic release."""
        self.state_dir.mkdir()
        manifest = cycle._ensure_period(self.config, self.state_dir, "2026-09-07", "2026-09-09", bind_inputs=True)
        old = cycle._expected_snapshot_digests(self.config, manifest)
        routing = json.loads((self.root / "routing.json").read_text())
        routing["session_routes"][0]["project_suffix"] = "775f9e"
        write_json(self.root / "routing.json", routing)
        release = self.root / "releases" / ("a" * 40)
        release.mkdir(parents=True)
        shutil.copyfile(self.root / "routing.json", release / "routing.json")
        if release_collector:
            (release / "scripts").mkdir()
            shutil.copyfile(Path(cycle.__file__).parent / "clockify_sync_collect.py", release / "scripts/clockify_sync_collect.py")
            write_json(release / "fleet.json", {"machines":[{"name":"macbook", "enabled":True}]})
        release_helper._make_payload_read_only(release)
        release.chmod(0o555)
        tree = release_helper._tree_manifest(release)
        identity = {
            "schema_version": "clockify-user-release/v1", "git_sha": "a" * 40,
            "root": str(release), "tree_manifest": tree,
            "tree_digest": release_helper._manifest_digest(tree),
            "routing_sha256": hashlib.sha256((release / "routing.json").read_bytes()).hexdigest(),
        }
        release.chmod(0o755)
        write_json(release / ".clockify-release.json", identity)
        (release / ".clockify-release.json").chmod(0o444)
        release.chmod(0o555)
        runtime = {"canonical_root": str(release), "collector_path": str(release / "scripts" / "clockify_sync_collect.py"), "git_sha": None, "git_dirty": None}
        config = {**self.config, "root": str(release), "routing": str(release / "routing.json"), "runs_dir": str(self.root / "runs"), "_runtime_identity": runtime}
        command = cycle._review_command(config, "2026-09-07", "2026-09-09")
        record = {"expected_snapshot_digests": old, "period_manifest": str(manifest), "until": "2026-09-09", "status": "incomplete"}
        attempt = cycle._source_attempt(record, command, cycle._generic_interval(config, "2026-09-07", "2026-09-09"), advance_frontier=True)
        record["runner_attempt"] = {"runtime_identity_digest": cycle._value_digest(runtime), "source_attempt_ordinal": attempt["ordinal"], "status": "pending"}
        source = make_run(self.root, "source-run", replay=False, runtime_identity=runtime, analyzer_tier="fixture") if create_source else None
        return config, record, manifest, source

    def fresh_routing_fixture(self):
        config, record, manifest, _ = self.routing_transition_fixture(create_source=False, release_collector=True)
        record.pop("runner_attempt")
        record["source_attempt"]["status"] = "finished"
        state = cycle._state(self.state_dir / "absent.json", recovery_since="2026-09-07")
        state["slices"]["2026-09-07"] = record
        write_json(self.state_dir / "review-cycle-state.json", state)
        return config, record, manifest

    def make_modern_raw_source(self, config, *, name="source-run", attest=True, incomplete_peer=False):
        result = make_run(self.root, name, replay=False, runtime_identity=config["_runtime_identity"], analyzer_tier="fixture", record_checkpoint=False)
        run = result.parent
        runtime = config["_runtime_identity"]
        collector_digest = hashlib.sha256(Path(runtime["collector_path"]).read_bytes()).hexdigest()
        host = {"machine": "macbook", "status": "ok", "complete": True,
            "collector_contract": "canonical_export_v1", "claude_bursts": [], "hermes_sessions": [],
            "hermes_db_sessions": [], "codex_sessions": [], "repository_events": [],
            "repository_evidence_status": "complete", "errors": []}
        if attest:
            host["canonical_export_attestation"] = {"collector_script_sha256": collector_digest, "runtime_identity": runtime}
            host["canonical_export"] = {"collector_script_sha256": collector_digest, "provenance": "full_context_remote_export"}
        if incomplete_peer:
            host.update(status="error", complete=False, errors=["synthetic unavailable sessions"])
        raw = {"sessions": [host], "clockify": {"status": "ok", "complete": True, "entries": []},
            "fathom": {"status": "ok", "complete": True, "meetings": []},
            "calendly": {"status": "excluded", "complete": False, "recordings": []},
            "multica_issues": {"status": "ok", "complete": True, "issues": []}}
        for key, filename in {"sessions":"sessions.json", "clockify":"clockify-existing.json", "fathom":"fathom-meetings.json", "calendly":"calendly-recordings.json", "multica_issues":"multica-issues.json"}.items():
            write_json(run / "evidence" / filename, raw[key])
        ledger = evidence_ledger.EvidenceLedger(tuple(evidence_ledger.normalize_collector_snapshot(raw)), evidence_ledger.source_inventory_from_collector(raw), "Europe/Bucharest", ("member-1",))
        write_json(run / "evidence/evidence-ledger.json", {"schema_version":"evidence-ledger/v1", "manifest":ledger.manifest.document(), "events":[event.document() for event in ledger.events]})
        report = json.loads((run / "run-report.json").read_text())
        report["evidence_ledger"] = {"source_completeness": ledger.manifest.document()["source_completeness"]}
        report["collection_mode"] = {"coordinator":"omarchy-precision"}
        write_json(run / "run-report.json", report)
        finalization = json.loads((run / "slice-finalization.json").read_text())
        slice_ = type("Slice", (), {"slice_id":finalization["slice_id"], "since":dt.datetime.fromisoformat(SINCE_UTC.replace("Z","+00:00")), "until":dt.datetime.fromisoformat(UNTIL_UTC.replace("Z","+00:00"))})()
        bundle = collector_receipts.build_completion_bundle(run, slice_=slice_)
        collector_receipts.write_completion_bundle(run / "completion-bundle.json", bundle)
        doc = json.loads(result.read_text())
        doc.update(completion_bundle_digest=bundle.bundle_digest, completion_bundle=bundle.document(), source_completeness=collector_receipts.completion_coverage(bundle))
        write_json(result, doc)
        backlog_identity = collector_slices.BacklogIdentity(**finalization["backlog_identity"])
        planned = collector_slices.plan_slices(slice_.since,slice_.until,zone=ZoneInfo("Europe/Bucharest"),max_days=2)
        checkpoint = self.state_dir / "collector-checkpoints"
        checkpoint.mkdir(parents=True,exist_ok=True,mode=0o700)
        checkpoint.chmod(0o700)
        store=collector_slices.BacklogStore(checkpoint)
        backlog=store.open(backlog_identity,planned)
        store.record_complete(backlog,finalization["slice_id"],(run/"completion-bundle.json").resolve(),"sha256:"+hashlib.sha256((run/"completion-bundle.json").read_bytes()).hexdigest())
        return result

    def test_fresh_routing_attempt_binds_before_child_and_preserves_legacy_history(self):
        """Catches stale no-runner legacy bindings stopping the next modern collection."""
        config, original, _ = self.fresh_routing_fixture()
        original_attempt = dict(original["source_attempt"])
        original_inputs = dict(original["expected_snapshot_digests"])
        # Actual legacy debt entrypoint: exhausted generic recovery, not a
        # freshly scheduled slice, must select and resolve the new attempt.
        state_path = self.state_dir / "review-cycle-state.json"
        state = json.loads(state_path.read_text())
        state["scheduled_through"] = "2026-09-09"
        write_json(state_path, state)
        debts = source_coverage.SourceDebtStore()
        interval = cycle._generic_interval(config, "2026-09-07", "2026-09-09")
        failure = debts.record_failure(interval, failure_class="result_unverified", retryable=True, resume_state_digest=original_attempt["resume_state_digest"], attempted_at="2026-09-10T00:00:00Z")
        debts.exhaust(failure.debt_id, terminal_reason="retry_limit")
        source_coverage.write(self.state_dir / "source-coverage.json", debts.document())
        def child(command, **_kwargs):
            if "--replay-from" in command:
                path = make_run(self.root, "replay-run", replay=True, snapshots_from=self.root / "runs/source-run", runtime_identity=config["_runtime_identity"], analyzer_tier="fixture", ledger_from=self.root / "runs/source-run")
                return ChildResult(0, str(path) + "\n", "", False, 0.1)
            if "clockify_sheet_publish.py" in command[1]:
                return publisher_result_for_command(config, command)
            persisted = json.loads((self.state_dir / "review-cycle-state.json").read_text())["slices"]["2026-09-07"]
            self.assertEqual(original_inputs, persisted["expected_snapshot_digests"])
            self.assertEqual(original_attempt, persisted["source_attempt_history"][0]["source_attempt"])
            self.assertEqual(2, persisted["source_attempt"]["ordinal"])
            self.assertEqual("started", persisted["source_attempt"]["status"])
            self.assertEqual("pending", persisted["runner_attempt"]["status"])
            self.assertNotEqual(original_inputs["routing.json"], persisted["fresh_input_binding"]["snapshot_digests"]["routing.json"])
            path = self.make_modern_raw_source(config)
            return ChildResult(0, str(path) + "\n", "", False, 0.1)
        try:
            with mock.patch.object(cycle, "run_child_bounded", side_effect=child):
                result = cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 10))
        except cycle.CycleError as exc:
            self.fail(f"modern fresh attempt blocked by legacy routing: {exc}")
        self.assertEqual("delivered", result["status"])
        recovered, _ = cycle._source_debt(self.state_dir / "source-coverage.json")
        self.assertEqual((), recovered.active())
        with mock.patch.object(cycle, "run_child_bounded", side_effect=AssertionError("child on repeat")):
            self.assertEqual("idle", cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 10))["status"])
        write_json(Path(config["corrections"]), {"later":"unrelated live correction"})
        with mock.patch.object(cycle, "run_child_bounded", side_effect=AssertionError("child after live drift")):
            self.assertEqual("idle", cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 10))["status"])

    def test_fresh_routing_attempt_crash_reuses_completed_native_source(self):
        """Catches retry after completion spawning another collection/inference process."""
        config, original, _ = self.fresh_routing_fixture()
        legacy = self.root / "cache/legacy-decisions.jsonl"
        legacy.write_bytes(b'{"sealed":"legacy decision"}\n')
        def crash(command, **_kwargs):
            self.make_modern_raw_source(config)
            raise RuntimeError("synthetic coordinator crash after completed child")
        with mock.patch.object(cycle, "run_child_bounded", side_effect=crash), self.assertRaisesRegex(RuntimeError, "synthetic coordinator crash"):
            cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 10))
        def resume(command, **_kwargs):
            if "--replay-from" in command:
                path = make_run(self.root, "replay-run", replay=True, snapshots_from=self.root / "runs/source-run", runtime_identity=config["_runtime_identity"], analyzer_tier="fixture", ledger_from=self.root / "runs/source-run")
                return ChildResult(0, str(path) + "\n", "", False, 0.1)
            if "clockify_sheet_publish.py" in command[1]:
                return publisher_result_for_command(config, command)
            self.fail("new collection/inference after completed fresh attempt crash")
        with mock.patch.object(cycle, "run_child_bounded", side_effect=resume):
            result = cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 10))
        self.assertEqual("delivered", result["status"])
        saved = json.loads((self.state_dir / "review-cycle-state.json").read_text())["slices"]["2026-09-07"]
        self.assertEqual(2, saved["source_attempt"]["ordinal"])
        self.assertEqual(1, len(saved["source_attempt_history"]))
        self.assertEqual(b'{"sealed":"legacy decision"}\n', legacy.read_bytes())

    def test_fresh_routing_attempt_unresolved_launch_is_not_duplicated(self):
        """Catches restarting an active/detached attempt with no verified completion."""
        config, _, _ = self.fresh_routing_fixture()
        with mock.patch.object(cycle, "run_child_bounded", side_effect=RuntimeError("detached child")), self.assertRaises(RuntimeError):
            cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 10))
        with mock.patch.object(cycle, "run_child_bounded", side_effect=AssertionError("duplicate child")), self.assertRaisesRegex(cycle.CycleError, "no duplicate child"):
            cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 10))

    def test_fresh_routing_attempt_budget_before_spawn_can_resume_once(self):
        """Catches a no-spawn budget guard permanently marking a child as active."""
        config, _, _ = self.fresh_routing_fixture()
        limited = {**config, "total_child_budget_seconds":30}
        with mock.patch.object(cycle, "run_child_bounded", side_effect=AssertionError("child with no budget")):
            result = cycle.run_cycle(limited, enable_sheet_write=True, today=dt.date(2026,9,10))
        self.assertEqual("incomplete", result["status"])
        def resume(command, **_kwargs):
            if "--replay-from" in command:
                path = make_run(self.root,"replay-run", replay=True,snapshots_from=self.root/"runs/source-run",runtime_identity=config["_runtime_identity"],analyzer_tier="fixture",ledger_from=self.root/"runs/source-run")
            elif "clockify_sheet_publish.py" in command[1]:
                return publisher_result_for_command(config,command)
            else:
                path = self.make_modern_raw_source(config)
            return ChildResult(0,str(path)+"\n","",False,0.1)
        try:
            with mock.patch.object(cycle,"run_child_bounded",side_effect=resume):
                result=cycle.run_cycle(config,enable_sheet_write=True,today=dt.date(2026,9,10))
        except cycle.CycleError as exc:
            self.fail(f"no-spawn attempt permanently blocked: {exc}")
        self.assertEqual("delivered",result["status"])
    def test_fresh_routing_attempt_rejects_nonrouting_and_active_runtime_drift(self):
        """Catches routing adoption silently changing corrections or an active runtime."""
        config, _, _ = self.fresh_routing_fixture()
        write_json(Path(config["corrections"]), {"unapproved":"change"})
        with mock.patch.object(cycle, "run_child_bounded", side_effect=AssertionError("child")), self.assertRaisesRegex(cycle.CycleError, "terminal legacy"):
            cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 10))
        write_json(Path(config["corrections"]), {})
        with mock.patch.object(cycle, "run_child_bounded", side_effect=RuntimeError("detached child")), self.assertRaises(RuntimeError):
            cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 10))
        changed = {**config, "_runtime_identity": {**config["_runtime_identity"], "git_sha":"b"*40}}
        with mock.patch.object(cycle, "run_child_bounded", side_effect=AssertionError("child")), self.assertRaisesRegex(cycle.CycleError, "active fresh attempt"):
            cycle.run_cycle(changed, enable_sheet_write=True, today=dt.date(2026, 9, 10))

    def test_fresh_routing_attempt_rejects_unattested_complete_zero_native_source(self):
        """Catches the legacy all-zero coverage illusion passing as modern completion."""
        config, _, _ = self.fresh_routing_fixture()
        def child(command, **_kwargs):
            self.assertNotIn("--replay-from", command)
            path = self.make_modern_raw_source(config, attest=False)
            return ChildResult(0, str(path)+"\n", "", False, 0.1)
        with mock.patch.object(cycle, "run_child_bounded", side_effect=child), self.assertRaisesRegex(cycle.CycleError, "native coverage"):
            cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 10))

    def test_fresh_routing_attempt_generic_native_failure_never_replays_or_publishes(self):
        """Catches generic classification retaining a source after native proof fails."""
        config, original, _ = self.fresh_routing_fixture()
        state_path = self.state_dir / "review-cycle-state.json"
        state = json.loads(state_path.read_text())
        state["scheduled_through"] = "2026-09-09"
        write_json(state_path,state)
        debts=source_coverage.SourceDebtStore()
        interval=cycle._generic_interval(config,"2026-09-07","2026-09-09")
        item=debts.record_failure(interval,failure_class="result_unverified",retryable=True,resume_state_digest=original["source_attempt"]["resume_state_digest"],attempted_at="2026-09-10T00:00:00Z")
        debts.exhaust(item.debt_id,terminal_reason="retry_limit")
        source_coverage.write(self.state_dir/"source-coverage.json",debts.document())
        def child(command, **_kwargs):
            self.assertNotIn("--replay-from",command,"invalid native source reached replay")
            self.assertNotIn("clockify_sheet_publish.py",command[1],"invalid native source reached publisher")
            path=self.make_modern_raw_source(config,attest=False)
            return ChildResult(0,str(path)+"\n","",False,0.1)
        with mock.patch.object(cycle,"run_child_bounded",side_effect=child), self.assertRaisesRegex(cycle.CycleError,"native coverage"):
            cycle.run_cycle(config,enable_sheet_write=True,today=dt.date(2026,9,10))
        record=json.loads(state_path.read_text())["slices"]["2026-09-07"]
        self.assertNotIn("source",record)
        persisted,_=cycle._source_debt(self.state_dir/"source-coverage.json")
        self.assertTrue(persisted.active())

    def test_fresh_routing_attempt_refuses_current_orphan_without_legacy_launch_binding(self):
        """Catches fresh fallback hiding ambiguous or already completed modern results."""
        config, _, _ = self.fresh_routing_fixture()
        self.make_modern_raw_source(config)
        with mock.patch.object(cycle, "run_child_bounded", side_effect=AssertionError("child")), self.assertRaisesRegex(cycle.CycleError, "orphan.*fresh attempt forbidden"):
            cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 10))

    def test_fresh_routing_attempt_rejects_historical_incomplete_runtime_parent(self):
        """Catches the old actionable-peer fallback bypassing exact fresh launch identity."""
        config, _, _ = self.fresh_routing_fixture()
        old = {**config,"_runtime_identity":{**config["_runtime_identity"],"git_sha":"b"*40}}
        def child(command, **_kwargs):
            self.assertNotIn("--replay-from",command)
            path=self.make_modern_raw_source(old,incomplete_peer=True)
            return ChildResult(0,str(path)+"\n","",False,0.1)
        try:
            with mock.patch.object(cycle,"run_child_bounded",side_effect=child):
                cycle.run_cycle(config,enable_sheet_write=True,today=dt.date(2026,9,10))
        except cycle.CycleError:
            pass
        record=json.loads((self.state_dir/"review-cycle-state.json").read_text())["slices"]["2026-09-07"]
        self.assertNotIn("source",record,"historical runtime source was promoted as fresh native parent")

    def test_fresh_routing_attempt_partial_peer_cannot_claim_unattested_repository_coverage(self):
        """Catches incomplete sessions hiding an unproven complete repository source."""
        config, _, _ = self.fresh_routing_fixture()
        def child(command, **_kwargs):
            path=self.make_modern_raw_source(config,incomplete_peer=True,attest=False)
            return ChildResult(0,str(path)+"\n","",False,0.1)
        with mock.patch.object(cycle,"run_child_bounded",side_effect=child), self.assertRaisesRegex(cycle.CycleError,"native coverage"):
            cycle.run_cycle(config,enable_sheet_write=True,today=dt.date(2026,9,10))

    def test_approved_routing_transition_adopts_completed_source_preserving_original_inputs(self):
        """Catches stale routing bindings forcing new collection after verified completion."""
        config, record, manifest, result = self.routing_transition_fixture()
        original = dict(record["expected_snapshot_digests"])
        source = cycle._routing_transition_source(config, record, "2026-09-07", "2026-09-09", manifest)
        self.assertEqual(str(result), source["result_path"])
        self.assertEqual(original, record["expected_snapshot_digests"])
        self.assertNotEqual(original["routing.json"], source["snapshot_digests"]["routing.json"])
        self.assertEqual("unique verified completed legacy-attempt result", record["routing_transition"]["eligibility_basis"])

    def test_routing_transition_rejects_ambiguous_completed_sources(self):
        """Catches selecting an arbitrary orphan when launch records do not name a run."""
        config, record, manifest, _ = self.routing_transition_fixture()
        make_run(self.root, "second-source", replay=False, runtime_identity=config["_runtime_identity"], compatibility_version="other-compatible-lineage/v1", analyzer_tier="fixture")
        with self.assertRaisesRegex(cycle.CycleError, "unique"):
            cycle._routing_transition_source(config, record, "2026-09-07", "2026-09-09", manifest)

    def test_routing_transition_ignores_malformed_unrelated_sibling(self):
        """Catches malformed sibling report preventing adoption of the one valid source."""
        config, record, manifest, result = self.routing_transition_fixture()
        sibling = self.root / "runs" / "malformed-sibling"
        write_json(sibling / "autopilot-result.json", {})
        write_json(sibling / "run-report.json", [])
        try:
            source = cycle._routing_transition_source(config, record, "2026-09-07", "2026-09-09", manifest)
        except AttributeError as exc:
            self.fail(f"malformed unrelated sibling blocked valid orphan: {exc}")
        self.assertEqual(str(result), source["result_path"])

    def test_routing_transition_rejects_release_tree_mutation(self):
        """Catches trusting current config/source agreement without immutable release proof."""
        config, record, manifest, _ = self.routing_transition_fixture()
        route = Path(config["routing"])
        route.chmod(0o644)
        write_json(route, {"workspace_id": "workspace-1", "member_id": "member-1"})
        with self.assertRaisesRegex(cycle.CycleError, "release"):
            cycle._routing_transition_source(config, record, "2026-09-07", "2026-09-09", manifest)

    def test_routing_transition_resume_delivers_and_repeats_without_fresh_review(self):
        """Catches the coordinator ignoring a verified orphan and launching inference again."""
        config, record, manifest, source = self.routing_transition_fixture()
        state = cycle._state(self.state_dir / "absent.json", recovery_since="2026-09-07")
        state["slices"]["2026-09-07"] = record
        write_json(self.state_dir / "review-cycle-state.json", state)
        original_bytes = source.read_bytes()
        original_inputs = dict(record["expected_snapshot_digests"])

        def child(command, **_kwargs):
            if "--replay-from" in command:
                path = make_run(self.root, "replay-run", replay=True, snapshots_from=source.parent, runtime_identity=config["_runtime_identity"], analyzer_tier="fixture")
                return ChildResult(0, str(path) + "\n", "", False, 0.1)
            if "clockify_sheet_publish.py" in command[1]:
                return publisher_result_for_command(config, command)
            self.fail("fresh collection/inference invoked despite completed orphan")

        with mock.patch.object(cycle, "run_child_bounded", side_effect=child):
            first = cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 10))
        self.assertEqual("delivered", first["status"])
        with mock.patch.object(cycle, "run_child_bounded", side_effect=AssertionError("child invoked on repeat")):
            repeated = cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 10))
        self.assertEqual("idle", repeated["status"])
        saved = json.loads((self.state_dir / "review-cycle-state.json").read_text())["slices"]["2026-09-07"]
        self.assertEqual(original_inputs, saved["expected_snapshot_digests"])
        self.assertEqual(original_bytes, source.read_bytes())
        write_json(Path(config["corrections"]), {"later": "unrelated live correction"})
        with mock.patch.object(cycle, "run_child_bounded", side_effect=AssertionError("child invoked on later input drift")):
            self.assertEqual("idle", cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 10))["status"])

    def test_routing_transition_new_fix_runtime_adopts_exact_old_launch(self):
        """Catches deployment of the fix itself making the sealed old launch unusable."""
        config, record, manifest, result = self.routing_transition_fixture()
        old_root = Path(config["root"])
        new_root = old_root.parent / ("b" * 40)
        new_root.mkdir()
        shutil.copyfile(old_root / "routing.json", new_root / "routing.json")
        release_helper._make_payload_read_only(new_root)
        new_root.chmod(0o555)
        tree = release_helper._tree_manifest(new_root)
        identity = {"schema_version": "clockify-user-release/v1", "git_sha": "b" * 40, "root": str(new_root), "tree_manifest": tree, "tree_digest": release_helper._manifest_digest(tree), "routing_sha256": hashlib.sha256((new_root / "routing.json").read_bytes()).hexdigest()}
        new_root.chmod(0o755)
        write_json(new_root / ".clockify-release.json", identity)
        (new_root / ".clockify-release.json").chmod(0o444)
        new_root.chmod(0o555)
        runtime = {**config["_runtime_identity"], "canonical_root": str(new_root), "collector_path": str(new_root / "scripts/clockify_sync_collect.py")}
        config.update(root=str(new_root), routing=str(new_root / "routing.json"), _runtime_identity=runtime)
        try:
            source = cycle._routing_transition_source(config, record, "2026-09-07", "2026-09-09", manifest)
        except cycle.CycleError as exc:
            self.fail(f"approved fix deployment rejected exact sealed launch: {exc}")
        self.assertEqual(str(result), source["result_path"])
        self.assertEqual(record["runner_attempt"]["runtime_identity_digest"], source["runtime_identity_digest"])
        state = cycle._state(self.state_dir / "absent.json", recovery_since="2026-09-07")
        state["slices"]["2026-09-07"] = record
        write_json(self.state_dir / "review-cycle-state.json", state)
        launch_runtime = json.loads((result.parent / "run-report.json").read_text())["runtime_identity"]
        def child(command, **_kwargs):
            if "--replay-from" in command:
                path = make_run(self.root, "replay-run", replay=True, snapshots_from=result.parent, runtime_identity=launch_runtime, analyzer_tier="fixture")
                return ChildResult(0, str(path) + "\n", "", False, 0.1)
            if "clockify_sheet_publish.py" in command[1]:
                return publisher_result_for_command(config, command)
            self.fail("fresh collection/inference after fix deployment")
        try:
            with mock.patch.object(cycle, "run_child_bounded", side_effect=child):
                outcome = cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 10))
        except cycle.CycleError as exc:
            self.fail(f"sealed old source/replay rejected by fix deployment: {exc}")
        self.assertEqual("delivered", outcome["status"])
        with mock.patch.object(cycle, "run_child_bounded", side_effect=AssertionError("child invoked")):
            self.assertEqual("idle", cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 10))["status"])
        # A subsequent approved runtime does not rewrite the sealed slice's
        # original routing/correction authority.
        later = {**config, "root": str(old_root), "routing": str(old_root / "routing.json"), "_runtime_identity": launch_runtime}
        write_json(Path(config["corrections"]), {"later": "correction"})
        with mock.patch.object(cycle, "run_child_bounded", side_effect=AssertionError("child invoked after later release")):
            self.assertEqual("idle", cycle.run_cycle(later, enable_sheet_write=True, today=dt.date(2026, 9, 10))["status"])

    def test_routing_transition_rejects_nonrouting_drift_and_wrong_launch(self):
        """Catches using routing adoption to relax other inputs or runtime provenance."""
        config, record, manifest, _ = self.routing_transition_fixture()
        write_json(Path(config["corrections"]), {"unauthorized": "change"})
        with self.assertRaisesRegex(cycle.CycleError, "immutable inputs"):
            cycle._routing_transition_source(config, record, "2026-09-07", "2026-09-09", manifest)
        write_json(Path(config["corrections"]), {})
        record["runner_attempt"]["runtime_identity_digest"] = "sha256:" + "f" * 64
        with self.assertRaisesRegex(cycle.CycleError, "unique"):
            cycle._routing_transition_source(config, record, "2026-09-07", "2026-09-09", manifest)

    def test_routing_transition_recovers_finalized_legacy_attempt_without_relaunch(self):
        """Catches terminal classification finalizing an attempt before source adoption."""
        config, record, _, result = self.routing_transition_fixture()
        record["source_attempt"]["status"] = "finished"
        record["runner_attempt"]["status"] = "finished"
        record["status"] = "incomplete"
        original_attempt = dict(record["source_attempt"])
        original_runner = dict(record["runner_attempt"])
        original_inputs = dict(record["expected_snapshot_digests"])
        state = cycle._state(self.state_dir / "absent.json", recovery_since="2026-09-07")
        state["slices"]["2026-09-07"] = record
        write_json(self.state_dir / "review-cycle-state.json", state)
        def child(command, **_kwargs):
            if "--replay-from" in command:
                path = make_run(self.root, "replay-run", replay=True, snapshots_from=result.parent, runtime_identity=config["_runtime_identity"], analyzer_tier="fixture")
                return ChildResult(0, str(path) + "\n", "", False, 0.1)
            if "clockify_sheet_publish.py" in command[1]:
                return publisher_result_for_command(config, command)
            self.fail("fresh review launched for finalized completed legacy attempt")
        try:
            with mock.patch.object(cycle, "run_child_bounded", side_effect=child):
                outcome = cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 10))
        except cycle.CycleError as exc:
            self.fail(f"finalized valid legacy attempt could not resume: {exc}")
        self.assertEqual("delivered", outcome["status"])
        saved = json.loads((self.state_dir / "review-cycle-state.json").read_text())["slices"]["2026-09-07"]
        self.assertEqual(original_attempt, saved["source_attempt"])
        self.assertEqual(original_runner, saved["runner_attempt"])
        self.assertEqual(original_inputs, saved["expected_snapshot_digests"])
        with mock.patch.object(cycle, "run_child_bounded", side_effect=AssertionError("child invoked on repeat")):
            self.assertEqual("idle", cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 10))["status"])

    def test_routing_transition_crash_resume_pins_candidate_and_rejects_tampering(self):
        """Catches a crash after sealing choosing another run or accepting receipt drift."""
        config, record, manifest, result = self.routing_transition_fixture()
        first = cycle._routing_transition_source(config, record, "2026-09-07", "2026-09-09", manifest)
        write_json(self.state_dir / "sealed-crash-state.json", record)
        resumed = json.loads((self.state_dir / "sealed-crash-state.json").read_text())
        make_run(self.root, "later-completed-source", replay=False, runtime_identity=config["_runtime_identity"], compatibility_version="later-lineage/v1", analyzer_tier="fixture")
        self.assertEqual(first, cycle._routing_transition_source(config, resumed, "2026-09-07", "2026-09-09", manifest))
        resumed["routing_transition"]["source"]["result_digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(cycle.CycleError, "receipt has drifted"):
            cycle._routing_transition_source(config, resumed, "2026-09-07", "2026-09-09", manifest)
        resumed = json.loads((self.state_dir / "sealed-crash-state.json").read_text())
        write_json(result, {"tampered": True})
        with self.assertRaises((cycle.CycleError, ValueError)):
            cycle._routing_transition_source(config, resumed, "2026-09-07", "2026-09-09", manifest)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        runs_patch = mock.patch.object(review_run, "RUNS", self.root / "runs")
        self.runs_patch = runs_patch
        runs_patch.start()
        self.addCleanup(runs_patch.stop)
        self.state_dir = self.root / "state"
        self.cache = self.root / "cache"
        self.cache.mkdir()
        for filename, value in (
            ("routing.json", {
                "workspace_id": "workspace-1",
                "member_id": "member-1",
                "session_routes": [{
                    "project_suffix": "775f9f",
                    "project_name": "Serenichron Level 2",
                }],
                "meeting_routes": [],
                "evidence_routes": [],
            }),
            ("corrections.jsonl", {}),
            ("acceptance.jsonl", {}),
        ):
            write_json(self.root / filename, value)
        self.config = {
            "root": str(self.root),
            "state_dir": str(self.state_dir),
            "cache": str(self.cache),
            "routing": str(self.root / "routing.json"),
            "corrections": str(self.root / "corrections.jsonl"),
            "acceptance": str(self.root / "acceptance.jsonl"),
            "workspace_id": "workspace-1",
            "member_id": "member-1",
            "recovery_since": "2026-09-07",
            "catchup_until": "2026-09-09",
            "timezone": "Europe/Bucharest",
            "spreadsheet_id": "sheet-1",
            "monthly_sheet_title_template": "{month_name} {year} portfolio review",
            "calendly_optional": True,
            "max_slices": 1,
        }

    def test_configured_durable_runs_root_is_used_by_real_replay_consumer(self):
        """Catches the service consumer retaining immutable-release/runs internally."""
        self.runs_patch.stop()
        previous = review_run.RUNS
        self.addCleanup(review_run._configure_runs_root, previous)
        durable = self.root / "durable-runs"
        config = {**self.config, "runs_dir":str(durable)}
        manifest = cycle._ensure_period(config, self.state_dir, "2026-09-07", "2026-09-09", bind_inputs=True)
        source = make_run(self.root, "source-run", runs_dir=durable, replay=False)
        review_run._configure_runs_root(durable)
        replay = make_run(self.root, "replay-run", runs_dir=durable, replay=True, snapshots_from=source.parent)
        review_run._configure_runs_root(previous)
        expected = cycle._expected_snapshot_digests(config, manifest)
        source_stage = cycle._validate_stage(config, source, "2026-09-07", "2026-09-09", replay=False, expected_snapshot_digests=expected)
        state = cycle._state(self.state_dir / "absent.json", recovery_since="2026-09-07")
        state["slices"]["2026-09-07"] = {"until":"2026-09-09", "period_manifest":str(manifest), "expected_snapshot_digests":expected, "status":"source_verified", "source":source_stage}
        write_json(self.state_dir / "review-cycle-state.json", state)
        def child(command, **_kwargs):
            if "--replay-from" in command:
                return ChildResult(0, str(replay)+"\n", "", False, 0.1)
            if "clockify_sheet_publish.py" in command[1]:
                return publisher_result_for_command(config, command)
            self.fail("fresh review for verified source")
        try:
            with mock.patch.object(cycle, "run_child_bounded", side_effect=child):
                result = cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026,9,10))
        except cycle.CycleError as exc:
            self.fail(f"valid durable source rejected by replay consumer: {exc}")
        self.assertEqual("delivered", result["status"])
        outside = self.root / "not-configured-runs/source-run"
        outside.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "direct child"):
            review_run.derive_replay_integrity(outside, replay.parent)

    def test_returned_exact_source_replay_is_persisted_before_validation_and_reused(self):
        """Catches a crash before replay validation discarding the exact returned locator."""
        manifest = cycle._ensure_period(self.config, self.state_dir, "2026-09-07", "2026-09-09", bind_inputs=True)
        source = make_run(self.root, "source-run", replay=False)
        replay = make_run(self.root, "completed-replay", replay=True, snapshots_from=source.parent)
        expected = cycle._expected_snapshot_digests(self.config, manifest)
        stage = cycle._validate_stage(self.config, source, "2026-09-07", "2026-09-09", replay=False, expected_snapshot_digests=expected)
        state = cycle._state(self.state_dir / "absent.json", recovery_since="2026-09-07")
        state["slices"]["2026-09-07"] = {"until":"2026-09-09", "period_manifest":str(manifest), "expected_snapshot_digests":expected, "status":"source_verified", "source":stage}
        write_json(self.state_dir / "review-cycle-state.json", state)
        def child(command, **_kwargs):
            if "--replay-from" in command:
                return ChildResult(0, str(replay)+"\n", "", False, 0.1)
            if "clockify_sheet_publish.py" in command[1]:
                return publisher_result_for_command(self.config, command)
            self.fail("fresh review was invoked")
        persist = cycle._persist_state
        def crash_after_locator(*args, **kwargs):
            persist(*args, **kwargs)
            if "replay_return" in args[3] and "replay" not in args[3]:
                raise RuntimeError("crash after returned locator persisted")
        with mock.patch.object(cycle, "run_child_bounded", side_effect=child), mock.patch.object(cycle, "_persist_state", side_effect=crash_after_locator), self.assertRaisesRegex(RuntimeError, "returned locator"):
            cycle.run_cycle(self.config, enable_sheet_write=True, today=dt.date(2026,9,10))
        def resume(command, **_kwargs):
            if "clockify_sheet_publish.py" in command[1]:
                return publisher_result_for_command(self.config, command)
            self.fail("exact returned replay was rerun after locator persistence")
        with mock.patch.object(cycle, "run_child_bounded", side_effect=resume):
            outcome = cycle.run_cycle(self.config, enable_sheet_write=True, today=dt.date(2026,9,10))
        self.assertEqual("delivered", outcome["status"])
        saved = json.loads((self.state_dir / "review-cycle-state.json").read_text())["slices"]["2026-09-07"]
        self.assertEqual(str(replay), saved["replay"]["result_path"])

    def test_checkpoint_root_defaults_to_durable_state_and_is_injected_into_child(self):
        """Catches collector checkpoints falling back under an immutable release."""
        release = self.root / "immutable-release"
        release.mkdir()
        release.chmod(0o555)
        state_dir = self.root / "durable-state"
        state_dir.mkdir()
        config = {**self.config, "root": str(release), "state_dir": str(state_dir)}
        captured: dict[str, object] = {}

        def child(command, **kwargs):
            captured.update(kwargs)
            checkpoint = Path(kwargs["environment"]["CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT"])
            self.assertTrue(checkpoint.is_dir())
            return ChildResult(0, "", "", False, 0.25)

        with mock.patch.object(cycle, "run_child_bounded", side_effect=child):
            result = cycle._run_budgeted_child(
                ["fixture"], root=release, budget=[10.0], cap=9, grace=1,
                runs_dir=self.root / "runs", checkpoint_root=cycle._collector_checkpoint_root(
                    config, {}
                ),
            )

        expected = state_dir / "collector-checkpoints"
        self.assertEqual(expected, cycle._collector_checkpoint_root(config, {}))
        self.assertEqual(str(expected), captured["environment"]["CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT"])
        self.assertTrue(expected.is_dir())
        self.assertEqual(0o700, stat.S_IMODE(expected.stat().st_mode))
        self.assertEqual(0, result.returncode)
        self.assertEqual([], list(release.iterdir()))

    def test_existing_checkpoint_root_rejects_group_or_other_access(self):
        """Catches collector provenance stored in a directory accessible by other users."""
        state_dir = self.root / "durable-state"
        checkpoint = state_dir / "collector-checkpoints"
        checkpoint.mkdir(parents=True)
        checkpoint.chmod(0o777)
        config = {**self.config, "state_dir": str(state_dir)}

        with self.assertRaisesRegex(cycle.CycleError, "0700"):
            cycle._collector_checkpoint_root(config, {})

    def test_existing_checkpoint_root_rejects_wrong_owner(self):
        """Catches trusting collector provenance owned by another user."""
        state_dir = self.root / "durable-state"
        checkpoint = state_dir / "collector-checkpoints"
        checkpoint.mkdir(parents=True)
        checkpoint.chmod(0o700)
        config = {**self.config, "state_dir": str(state_dir)}

        with mock.patch.object(cycle.os, "getuid", return_value=os.getuid() + 1), \
                self.assertRaisesRegex(cycle.CycleError, "owned"):
            cycle._collector_checkpoint_root(config, {})

    def test_checkpoint_override_must_be_canonical_and_within_durable_state(self):
        """Catches symlinked, noncanonical, relative, or wrong-root checkpoint overrides."""
        state_dir = self.root / "durable-state"
        state_dir.mkdir()
        state_dir.chmod(0o700)
        allowed = state_dir / "alternate-checkpoints"
        config = {**self.config, "state_dir": str(state_dir)}
        self.assertEqual(
            allowed,
            cycle._collector_checkpoint_root(
                config, {"CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT": str(allowed)}
            ),
        )

        outside = self.root / "outside-checkpoints"
        alias = state_dir / "checkpoint-alias"
        outside.mkdir()
        alias.symlink_to(outside, target_is_directory=True)
        for override in (
            "relative/checkpoints",
            str(outside),
            str(alias),
            str(state_dir / "nested" / ".." / "alternate-checkpoints"),
        ):
            with self.subTest(override=override), self.assertRaisesRegex(
                cycle.CycleError, "checkpoint root"
            ):
                cycle._collector_checkpoint_root(
                    config, {"CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT": override}
                )

    def test_absent_checkpoint_override_requires_an_existing_secure_parent(self):
        """Catches accepting an override that cannot be created without unsafe recursion."""
        state_dir = self.root / "durable-state"
        state_dir.mkdir(mode=0o700)
        config = {**self.config, "state_dir": str(state_dir)}
        missing_parent = state_dir / "missing" / "checkpoints"
        with self.assertRaisesRegex(cycle.CycleError, "parent"):
            cycle._collector_checkpoint_root(
                config, {"CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT": str(missing_parent)}
            )

        unsafe_parent = state_dir / "unsafe"
        unsafe_parent.mkdir(mode=0o700)
        unsafe_parent.chmod(0o755)
        with self.assertRaisesRegex(cycle.CycleError, "0700"):
            cycle._collector_checkpoint_root(
                config,
                {"CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT": str(unsafe_parent / "checkpoints")},
            )

        safe_parent = state_dir / "safe"
        safe_parent.mkdir(mode=0o700)
        expected = safe_parent / "checkpoints"
        self.assertEqual(
            expected,
            cycle._collector_checkpoint_root(
                config, {"CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT": str(expected)}
            ),
        )

    def child_for_runs(
        self,
        commands: list[list[str]],
        *,
        source_options: dict[str, object] | None = None,
        replay_code: int = 0,
        publish_codes: list[int] | None = None,
    ):
        remaining_publish_codes = list(publish_codes or [0])

        def child(command, **_kwargs):
            command = list(command)
            commands.append(command)
            if "--replay-from" in command:
                if replay_code:
                    return ChildResult(replay_code, "", "", False, 0.1)
                replay_options = dict(source_options or {})
                replay_options.pop("snapshot_overrides", None)
                replay_options["snapshots_from"] = self.root / "runs" / "source-run"
                path = make_run(
                    self.root, "replay-run", replay=True, **replay_options
                )
                return ChildResult(0, str(path) + "\n", "", False, 0.1)
            if "clockify_sheet_publish.py" in command[1]:
                code = remaining_publish_codes.pop(0)
                return self.publisher_child_result(command, code=code)
            path = make_run(
                self.root, "source-run", replay=False, **(source_options or {})
            )
            return ChildResult(0, str(path) + "\n", "", False, 0.1)

        return child

    def publisher_child_result(self, command: list[str], *, code: int = 0) -> ChildResult:
        return publisher_result_for_command(self.config, command, code=code)

    def test_first_success_delivers_once_and_repeat_validates_without_children(self):
        """Catches stopping after review or publishing the same verified slice twice."""
        commands: list[list[str]] = []

        def child(command, **_kwargs):
            command = list(command)
            commands.append(command)
            if "--replay-from" in command:
                path = make_run(
                    self.root,
                    "replay-run",
                    replay=True,
                    snapshots_from=self.root / "runs" / "source-run",
                )
                return ChildResult(0, str(path) + "\n", "", False, 0.1)
            if "clockify_sheet_publish.py" in command[1]:
                return self.publisher_child_result(command)
            path = make_run(self.root, "source-run", replay=False)
            return ChildResult(0, str(path) + "\n", "", False, 0.1)

        with mock.patch.object(cycle, "run_child_bounded", side_effect=child):
            first = cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )
            second = cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )

        self.assertEqual("delivered", first["status"])
        self.assertEqual("idle", second["status"])
        self.assertEqual(3, len(commands))
        self.assertIn("2026-09-08", commands[0])
        self.assertIn("--replay-from", commands[1])
        self.assertIn(str((self.root / "runs" / "source-run").resolve()), commands[1])
        self.assertIn("--enable-write", commands[2])
        self.assertIn("September 2026 portfolio review", commands[2])
        routing_index = commands[2].index("--routing-snapshot")
        self.assertEqual(
            str((self.root / "runs" / "source-run" / "routing.json").resolve()),
            commands[2][routing_index + 1],
        )

        state = json.loads(
            (self.state_dir / "review-cycle-state.json").read_text(encoding="utf-8")
        )
        delivered = state["slices"]["2026-09-07"]
        self.assertEqual("2026-09-09", state["completed_through"])
        self.assertEqual("delivered", delivered["status"])
        self.assertEqual("source-run", delivered["source_run_id"])
        self.assertEqual("replay-run", delivered["replay_run_id"])
        self.assertEqual(["wka-alpha-s01"], delivered["review_ids"])
        receipt = Path(delivered["delivery_receipt"])
        self.assertTrue(receipt.is_file())
        self.assertEqual(
            "September 2026 portfolio review",
            json.loads(receipt.read_text(encoding="utf-8"))["target"]["sheet_title"],
        )

        events = self.state_dir / "2026-09-07.period-events.jsonl"
        manifest = self.state_dir / "2026-09-07.period-manifest.json"
        self.assertEqual(1, len(events.read_text(encoding="utf-8").splitlines()))
        self.assertEqual("collecting", json.loads(manifest.read_text())["state"])

    def test_plan_only_rejects_no_template_and_runs_no_children(self):
        """Catches plan mode invoking collection, replay, or publication."""
        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=AssertionError("child invoked")
        ):
            result = cycle.run_cycle(
                self.config, enable_sheet_write=False, today=dt.date(2026, 9, 10)
            )
        self.assertEqual(
            {"status": "plan", "slices": [{"since": "2026-09-07", "until": "2026-09-09"}]},
            result,
        )

    def test_publish_failure_reuses_verified_source_and_replay(self):
        """Catches a publisher retry recollecting or rerunning inference/replay."""
        commands: list[list[str]] = []
        child = self.child_for_runs(commands, publish_codes=[9, 0])
        with mock.patch.object(cycle, "run_child_bounded", side_effect=child):
            failed = cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )
            retried = cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )
        self.assertEqual("failed", failed["status"])
        self.assertEqual("delivered", retried["status"])
        self.assertEqual(4, len(commands))
        self.assertEqual(2, sum("clockify_sheet_publish.py" in item[1] for item in commands))
        self.assertEqual(1, sum("--replay-from" in item for item in commands))

    def test_crash_after_publish_before_receipt_retries_same_stable_rows(self):
        """Catches treating an unreceipted successful write as locally complete."""
        commands: list[list[str]] = []
        child = self.child_for_runs(commands, publish_codes=[0, 0])
        real_write = cycle._write_delivery_receipt
        with mock.patch.object(cycle, "run_child_bounded", side_effect=child), mock.patch.object(
            cycle, "_write_delivery_receipt", side_effect=RuntimeError("synthetic crash")
        ):
            with self.assertRaisesRegex(RuntimeError, "synthetic crash"):
                cycle.run_cycle(
                    self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
                )
        source = self.root / "runs" / "source-run"
        parent_before = {
            str(path.relative_to(source)): path.read_bytes()
            for path in sorted(source.rglob("*")) if path.is_file()
        }
        with mock.patch.object(cycle, "run_child_bounded", side_effect=child), mock.patch.object(
            cycle, "_write_delivery_receipt", side_effect=real_write
        ):
            result = cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )
        self.assertEqual("delivered", result["status"])
        publisher_commands = [item for item in commands if "clockify_sheet_publish.py" in item[1]]
        self.assertEqual(2, len(publisher_commands))
        self.assertEqual(publisher_commands[0], publisher_commands[1])
        receipt = json.loads(
            (self.state_dir / "delivery-receipts" / "2026-09-07.json").read_text()
        )
        assert_scenario_contract(
            self,
            stable_ids=receipt["review_ids"],
            parent_before=parent_before,
            parent_after={
                str(path.relative_to(source)): path.read_bytes()
                for path in sorted(source.rglob("*")) if path.is_file()
            },
            emitted_ids=receipt["review_ids"],
            clockify_adapter_calls=sum(
                "clockify_post_approved_portfolio.py" in command[1]
                for command in commands
            ),
        )

    def test_mixed_publication_receipt_binds_both_destinations_on_restart(self):
        """A monthly-only receipt must not hide unresolved-tab durability."""
        routed = proposal()
        unresolved = {**proposal(),
            "review_activity_key": "wka-unresolved",
            "allocation_segment": 2,
            "client_project": "",
            "clockify_project_suffix": "",
            "tag_suffixes": [],
            "tag_names": [],
            "billable": False,
            "routing_disposition": "unresolved-routing",
            "review_warnings": [{
                "type": "unresolved_routing",
                "disposition": "unresolved-routing",
                "reason_code": "no_deterministic_route",
            }],
        }
        commands: list[list[str]] = []
        with mock.patch.object(
            cycle, "run_child_bounded",
            side_effect=self.child_for_runs(
                commands, source_options={"proposals": [routed, unresolved]},
            ),
        ):
            result = cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )
        self.assertEqual("delivered", result["status"])
        receipt_path = self.state_dir / "delivery-receipts" / "2026-09-07.json"
        receipt = json.loads(receipt_path.read_text())
        self.assertEqual(
            ["September 2026 portfolio review", "September 2026 unresolved evidence"],
            [item["sheet_title"] for item in receipt["publication_receipts"]],
        )
        self.assertTrue(all(item["row_ids"] for item in receipt["publication_receipts"]))
        self.assertEqual("visible-monthly-unresolved/v1", receipt["publication_profile"])
        self.assertTrue(any("--monthly-unresolved" in command for command in commands))
        self.assertTrue(all(item["readback_id"].startswith("sheet-readback/") for item in receipt["publication_receipts"]))

        # Missing profile means the exact old hidden 15-column contract, not a
        # request to migrate or rewrite a historical delivered receipt.
        state = json.loads((self.state_dir / "review-cycle-state.json").read_text())
        record = state["slices"]["2026-09-07"]
        legacy = cycle._delivery_document(
            self.config, "2026-09-07", "2026-09-09", record["source"], record["replay"],
            sheet_title="September 2026 portfolio review", publication_profile=None,
        )
        self.assertNotIn("publication_profile", legacy)
        self.assertEqual("unresolved-evidence", legacy["publication_receipts"][1]["sheet_title"])
        historical = self.state_dir / "historical-delivery.json"
        write_json(historical, legacy)
        original = historical.read_bytes()
        with mock.patch.object(cycle.clockify_monthly_unresolved, "project_rows", side_effect=AssertionError("legacy projection invoked")):
            cycle._verify_delivery_receipt(
                historical, self.config, "2026-09-07", "2026-09-09", record["source"], record["replay"],
                sheet_title="September 2026 portfolio review",
            )
        self.assertEqual(original, historical.read_bytes())
        unknown = {**receipt, "publication_profile": "unknown/v99"}
        write_json(historical, unknown)
        with self.assertRaises(cycle.CycleError):
            cycle._verify_delivery_receipt(
                historical, self.config, "2026-09-07", "2026-09-09", record["source"], record["replay"],
                sheet_title="September 2026 portfolio review",
            )

        receipt["publication_receipts"][1]["readback_id"] = "sheet-readback/tampered"
        write_json(receipt_path, receipt)
        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=AssertionError("child invoked")
        ), self.assertRaisesRegex(cycle.CycleError, "receipt"):
            cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )

    def test_mixed_publisher_readback_mismatch_blocks_durable_receipt(self):
        """The scheduler must parse both publisher readbacks, not infer success."""
        unresolved = {**proposal(),
            "review_activity_key": "wka-unresolved",
            "allocation_segment": 2,
            "client_project": "", "clockify_project_suffix": "",
            "tag_suffixes": [], "tag_names": [], "billable": False,
            "routing_disposition": "unresolved-routing",
            "review_warnings": [{
                "type": "unresolved_routing",
                "disposition": "unresolved-routing",
                "reason_code": "no_deterministic_route",
            }],
        }
        ordinary = self.child_for_runs(
            [], source_options={"proposals": [proposal(), unresolved]},
        )

        def child(command, **kwargs):
            result = ordinary(command, **kwargs)
            if "clockify_sheet_publish.py" in list(command)[1]:
                path = Path(result.stdout.strip())
                document = json.loads(path.read_text())
                document["publications"][1]["readback_id"] = "sheet-readback/tampered"
                write_json(path, document)
            return result

        with mock.patch.object(cycle, "run_child_bounded", side_effect=child), \
             self.assertRaisesRegex(cycle.CycleError, "readbacks differ"):
            cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )
        self.assertFalse(
            (self.state_dir / "delivery-receipts" / "2026-09-07.json").exists()
        )

    def test_replay_failure_blocks_before_publisher(self):
        """Catches publication continuing without a passing distinct replay."""
        commands: list[list[str]] = []
        with mock.patch.object(
            cycle, "run_child_bounded",
            side_effect=self.child_for_runs(commands, replay_code=7),
        ):
            result = cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )
        self.assertEqual("failed", result["status"])
        self.assertEqual(2, len(commands))
        self.assertFalse(any("clockify_sheet_publish.py" in item[1] for item in commands))

    def test_incomplete_required_source_retains_recovery_obligation(self):
        """Catches incomplete source coverage advancing into replay or delivery."""
        commands: list[list[str]] = []
        coverage = {"status": "incomplete", "incomplete_sources": ["clockify"]}
        with mock.patch.object(
            cycle,
            "run_child_bounded",
            side_effect=self.child_for_runs(
                commands, source_options={"coverage": coverage}
            ),
        ):
            result = cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )
        self.assertEqual("recovery_blocked", result["status"])
        self.assertEqual(1, len(commands))
        state = json.loads(
            (self.state_dir / "review-cycle-state.json").read_text(encoding="utf-8")
        )
        self.assertIsNone(state["completed_through"])
        self.assertEqual("2026-09-09", state["scheduled_through"])
        self.assertEqual(["clockify"], state["slices"]["2026-09-07"]["source_completeness"]["incomplete_sources"])

    def test_empty_proposals_are_delivered_as_a_verified_zero_row_slice(self):
        """Catches treating a verified empty proposal list as missing output."""
        commands: list[list[str]] = []
        with mock.patch.object(
            cycle,
            "run_child_bounded",
            side_effect=self.child_for_runs(
                commands, source_options={"proposals": []}
            ),
        ):
            result = cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )
        self.assertEqual("delivered", result["status"])
        receipt = json.loads(
            (self.state_dir / "delivery-receipts" / "2026-09-07.json").read_text()
        )
        self.assertEqual([], receipt["review_ids"])

    def test_semantic_exceptions_remain_visible_after_delivery_advances(self):
        """Catches delivery erasing unresolved semantic exception state."""
        commands: list[list[str]] = []
        with mock.patch.object(
            cycle,
            "run_child_bounded",
            side_effect=self.child_for_runs(
                commands, source_options={"ambiguous": [{"id": "ambiguous-1", "exception_kind": "low_confidence"}]}
            ),
        ):
            result = cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )
        self.assertEqual("delivered_with_exceptions", result["status"])
        state = json.loads(
            (self.state_dir / "review-cycle-state.json").read_text(encoding="utf-8")
        )
        record = state["slices"]["2026-09-07"]
        self.assertEqual("2026-09-09", state["completed_through"])
        self.assertEqual(["ambiguous-1"], record["exception_ids"])
        self.assertFalse(record["exceptions_complete"])

    def test_ambiguous_accounting_without_identity_fails_closed(self):
        """Catches malformed ambiguity being silently dropped from exception state."""
        commands: list[list[str]] = []
        with mock.patch.object(
            cycle,
            "run_child_bounded",
            side_effect=self.child_for_runs(
                commands, source_options={"ambiguous": [{"reason": "unclear"}]}
            ),
        ):
            with self.assertRaisesRegex(cycle.CycleError, "durable identity"):
                cycle.run_cycle(
                    self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
                )

    def test_malformed_title_templates_fail_before_any_child(self):
        """Catches unsafe format expansion reaching collection or publication."""
        for template in ("{unknown}", "{year!r}", "{month:02d}", "{year"):
            with self.subTest(template=template):
                config = {**self.config, "monthly_sheet_title_template": template}
                with mock.patch.object(
                    cycle, "run_child_bounded", side_effect=AssertionError("child invoked")
                ), self.assertRaises(cycle.CycleError):
                    cycle.run_cycle(
                        config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
                    )

    def test_receipt_target_drift_blocks_repeat_without_children(self):
        """Catches a delivered receipt being reused for a different sheet target."""
        commands: list[list[str]] = []
        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=self.child_for_runs(commands)
        ):
            cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )
        changed = {**self.config, "spreadsheet_id": "sheet-2"}
        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=AssertionError("child invoked")
        ), self.assertRaisesRegex(cycle.CycleError, "drifted"):
            cycle.run_cycle(
                changed, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )

    def test_drifted_source_input_blocks_repeat_without_children(self):
        """Catches mutation of a delivered source artifact after receipt binding."""
        commands: list[list[str]] = []
        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=self.child_for_runs(commands)
        ):
            cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )
        write_json(self.root / "runs" / "source-run" / "proposals.json", [])
        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=AssertionError("child invoked")
        ), self.assertRaises(cycle.CycleError):
            cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )

    def test_drifted_receipt_blocks_repeat_without_children(self):
        """Catches local receipt mutation being accepted as a delivered slice."""
        commands: list[list[str]] = []
        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=self.child_for_runs(commands)
        ):
            cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )
        receipt = self.state_dir / "delivery-receipts" / "2026-09-07.json"
        document = json.loads(receipt.read_text())
        document["review_ids"] = []
        write_json(receipt, document)
        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=AssertionError("child invoked")
        ), self.assertRaisesRegex(cycle.CycleError, "drifted"):
            cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )

    def test_wrong_source_interval_blocks_before_replay(self):
        """Catches a successful child result for another interval being adopted."""
        commands: list[list[str]] = []

        def child(command, **_kwargs):
            commands.append(list(command))
            path = make_run(self.root, "source-run", replay=False)
            document = json.loads(path.read_text())
            document["date_range"] = {
                "since": "2026-09-07T21:00:00Z",
                "until": UNTIL_UTC,
            }
            write_json(path, document)
            return ChildResult(0, str(path) + "\n", "", False, 0.1)

        with mock.patch.object(cycle, "run_child_bounded", side_effect=child), self.assertRaisesRegex(
            cycle.CycleError, "interval"
        ):
            cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )
        self.assertEqual(1, len(commands))

    def assert_identity_rejected_before_child_or_sheet(
        self, config: dict[str, object]
    ) -> None:
        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=AssertionError("child invoked")
        ), mock.patch.object(
            cycle, "_publisher_command", side_effect=AssertionError("publisher invoked")
        ), self.assertRaisesRegex(cycle.CycleError, "identity"):
            cycle.run_cycle(
                config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )

    def test_workspace_mismatch_blocks_before_child_or_sheet_action(self):
        """Catches a config workspace that disagrees with immutable routing."""
        self.assert_identity_rejected_before_child_or_sheet(
            {**self.config, "workspace_id": "workspace-2"}
        )

    def test_workspace_missing_blocks_before_child_or_sheet_action(self):
        """Catches release routing without a pinned workspace identity."""
        routing_path = self.root / "routing.json"
        routing = json.loads(routing_path.read_text(encoding="utf-8"))
        routing.pop("workspace_id")
        write_json(routing_path, routing)
        self.assert_identity_rejected_before_child_or_sheet(self.config)

    def test_member_mismatch_blocks_before_child_or_sheet_action(self):
        """Catches a config member that disagrees with immutable routing."""
        self.assert_identity_rejected_before_child_or_sheet(
            {**self.config, "member_id": "member-2"}
        )

    def test_member_missing_blocks_before_child_or_sheet_action(self):
        """Catches release routing without a pinned member identity."""
        routing_path = self.root / "routing.json"
        routing = json.loads(routing_path.read_text(encoding="utf-8"))
        routing.pop("member_id")
        write_json(routing_path, routing)
        self.assert_identity_rejected_before_child_or_sheet(self.config)

    def test_manifest_history_drift_blocks_delivered_repeat(self):
        """Catches an altered append-only period contract being silently reused."""
        commands: list[list[str]] = []
        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=self.child_for_runs(commands)
        ):
            cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )
        manifest = self.state_dir / "2026-09-07.period-manifest.json"
        document = json.loads(manifest.read_text())
        document["state"] = "reconciling"
        write_json(manifest, document)
        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=AssertionError("child invoked")
        ), self.assertRaisesRegex(cycle.CycleError, "manifest"):
            cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )

    def test_input_drift_during_publisher_blocks_receipt(self):
        """Catches binding a receipt after publisher-time immutable input drift."""
        commands: list[list[str]] = []
        ordinary = self.child_for_runs(commands)

        def child(command, **kwargs):
            result = ordinary(command, **kwargs)
            if "clockify_sheet_publish.py" in list(command)[1]:
                write_json(self.root / "runs" / "source-run" / "proposals.json", [])
            return result

        with mock.patch.object(cycle, "run_child_bounded", side_effect=child), self.assertRaises(
            cycle.CycleError
        ):
            cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )
        self.assertFalse(
            (self.state_dir / "delivery-receipts" / "2026-09-07.json").exists()
        )

    def test_fresh_source_snapshots_must_match_initial_expected_inputs(self):
        """Catches a fresh child substituting routing bytes before source adoption."""
        commands: list[list[str]] = []
        child = self.child_for_runs(
            commands,
            source_options={
                "snapshot_overrides": {
                    "routing.json": {
                        "workspace_id": "workspace-substituted",
                        "member_id": "member-1",
                    }
                }
            },
        )
        with mock.patch.object(cycle, "run_child_bounded", side_effect=child), self.assertRaisesRegex(
            cycle.CycleError, "snapshot"
        ):
            cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )
        self.assertEqual(1, len(commands))

    def test_replay_snapshots_must_equal_immutable_source_snapshots(self):
        """Catches replay snapshot substitution hidden behind a stale integrity report."""
        commands: list[list[str]] = []

        def child(command, **_kwargs):
            command = list(command)
            commands.append(command)
            if "--replay-from" in command:
                path = make_run(
                    self.root,
                    "replay-run",
                    replay=True,
                    snapshots_from=self.root / "runs" / "source-run",
                )
                write_json(
                    self.root / "runs" / "replay-run" / "routing.json",
                    {"workspace_id": "workspace-substituted", "member_id": "member-1"},
                )
                return ChildResult(0, str(path) + "\n", "", False, 0.1)
            if "clockify_sheet_publish.py" in command[1]:
                return ChildResult(0, "", "", False, 0.1)
            path = make_run(self.root, "source-run", replay=False)
            return ChildResult(0, str(path) + "\n", "", False, 0.1)

        with mock.patch.object(cycle, "run_child_bounded", side_effect=child), self.assertRaisesRegex(
            cycle.CycleError, "snapshot"
        ):
            cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )
        self.assertEqual(2, len(commands))

    def test_fabricated_replay_integrity_is_rejected(self):
        """Catches status-only replay evidence lacking its real self/binding digests."""
        commands: list[list[str]] = []

        def child(command, **_kwargs):
            command = list(command)
            commands.append(command)
            if "--replay-from" in command:
                path = make_run(
                    self.root,
                    "replay-run",
                    replay=True,
                    snapshots_from=self.root / "runs" / "source-run",
                    replay_integrity_override={
                        "status": "pass",
                        "source_run_id": "source-run",
                        "replay_run_id": "replay-run",
                        "failures": [],
                        "integrity_digest": "sha256:" + "d" * 64,
                    },
                )
                return ChildResult(0, str(path) + "\n", "", False, 0.1)
            path = make_run(self.root, "source-run", replay=False)
            return ChildResult(0, str(path) + "\n", "", False, 0.1)

        with mock.patch.object(cycle, "run_child_bounded", side_effect=child), self.assertRaisesRegex(
            cycle.CycleError, "replay integrity"
        ):
            cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )

    def test_accounting_requires_schema_version(self):
        """Catches a partial accounting object being accepted as a completion marker."""
        commands: list[list[str]] = []
        child = self.child_for_runs(
            commands,
            replay_code=7,
            source_options={
                "accounting_remove": ("schema_version",),
                "collector_source_marker": True,
            },
        )
        with mock.patch.object(cycle, "run_child_bounded", side_effect=child), self.assertRaisesRegex(
            cycle.CycleError, "schema"
        ):
            cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )

    def test_accounting_requires_non_overlapping_allocation_mode(self):
        """Catches an incompatible accounting algorithm being published."""
        commands: list[list[str]] = []
        child = self.child_for_runs(
            commands,
            replay_code=7,
            source_options={"accounting_overrides": {"allocation_mode": "legacy"}},
        )
        with mock.patch.object(cycle, "run_child_bounded", side_effect=child), self.assertRaisesRegex(
            cycle.CycleError, "allocation mode"
        ):
            cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )

    def test_retry_uses_persisted_source_snapshots_not_mutable_master_inputs(self):
        """Catches a publisher retry rebinding a verified source to changed master files."""
        commands: list[list[str]] = []
        child = self.child_for_runs(commands, publish_codes=[8, 0])
        with mock.patch.object(cycle, "run_child_bounded", side_effect=child):
            first = cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )
            write_json(
                self.root / "routing.json",
                {"workspace_id": "changed-later", "member_id": "changed-later"},
            )
            second = cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )
        self.assertEqual("failed", first["status"])
        self.assertEqual("delivered", second["status"])
        self.assertEqual(4, len(commands))

    def test_receipt_row_contract_uses_same_immutable_routing_snapshot_as_publisher(self):
        """Catches receipt labels being resolved from mutable master routing."""
        candidate = proposal()
        candidate["review_warnings"] = [{
            "type": "review_proposal_overlap",
            "counterpart_id": "wks-" + "b" * 24,
            "counterpart_project_suffix": "775f9f",
            "overlap_start": "2026-09-07T09:00:00+03:00",
            "overlap_end": "2026-09-07T09:05:00+03:00",
            "overlap_duration_seconds": 300,
        }]
        commands: list[list[str]] = []
        ordinary = self.child_for_runs(commands, source_options={"proposals": [candidate]})

        def child(command, **kwargs):
            result = ordinary(command, **kwargs)
            if "--replay-from" not in command and "clockify_sheet_publish.py" not in command[1]:
                write_json(self.root / "routing.json", {
                    "workspace_id": "workspace-1",
                    "member_id": "member-1",
                    "session_routes": [{
                        "project_suffix": "775f9f", "project_name": "Injected Mutable Name",
                    }],
                    "meeting_routes": [],
                    "evidence_routes": [],
                })
            return result

        with mock.patch.object(cycle, "run_child_bounded", side_effect=child):
            result = cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )

        self.assertEqual("delivered", result["status"])
        receipt = json.loads(
            (self.state_dir / "delivery-receipts" / "2026-09-07.json").read_text()
        )
        expected_warning = json.dumps([{
            "counterpart_id": "wks-" + "b" * 24,
            "counterpart_project": "Serenichron Level 2",
            "overlap_duration_seconds": 300,
            "overlap_end": "2026-09-07T09:05:00+03:00",
            "overlap_start": "2026-09-07T09:00:00+03:00",
            "type": "review_proposal_overlap",
        }], ensure_ascii=False, sort_keys=True)
        expected_row = [[
            "wka-alpha-s01", "2026-09-07 09:00", "2026-09-07 10:00", 60,
            "Serenichron Level 2", "Delivery", "activity-alpha", "high",
            "SC — review-cycle delivery", "pending", 1, "source-run",
            expected_warning, "unposted", "",
        ]]
        self.assertEqual(
            cycle._publication_receipt(
                spreadsheet_id="sheet-1",
                sheet_title="September 2026 portfolio review",
                rows=expected_row,
            ),
            receipt["publication_receipts"][0],
        )
        publisher = commands[-1]
        self.assertEqual(
            str((self.root / "runs" / "source-run" / "routing.json").resolve()),
            publisher[publisher.index("--routing-snapshot") + 1],
        )

    def test_enabled_cycle_processes_two_selected_slices_contiguously(self):
        """Catches max_slices selection being truncated to selected[0]."""
        config = {
            **self.config,
            "catchup_until": "2026-09-11",
            "max_slices": 2,
        }
        commands: list[list[str]] = []

        def child(command, **_kwargs):
            command = list(command)
            commands.append(command)
            if "clockify_sheet_publish.py" in command[1]:
                return self.publisher_child_result(command)
            if "--replay-from" in command:
                source_dir = Path(command[command.index("--replay-from") + 1])
                since = dt.date.fromisoformat(source_dir.name.removeprefix("source-"))
                until = since + dt.timedelta(days=2)
                path = make_run(
                    self.root,
                    f"replay-{since.isoformat()}",
                    replay=True,
                    source_name=source_dir.name,
                    since=since,
                    until=until,
                    snapshots_from=source_dir,
                )
                return ChildResult(0, str(path) + "\n", "", False, 0.1)
            since = dt.date.fromisoformat(command[command.index("--since") + 1])
            inclusive_until = dt.date.fromisoformat(command[command.index("--until") + 1])
            until = inclusive_until + dt.timedelta(days=1)
            path = make_run(
                self.root,
                f"source-{since.isoformat()}",
                replay=False,
                since=since,
                until=until,
            )
            return ChildResult(0, str(path) + "\n", "", False, 0.1)

        with mock.patch.object(cycle, "run_child_bounded", side_effect=child):
            result = cycle.run_cycle(
                config, enable_sheet_write=True, today=dt.date(2026, 9, 12)
            )
        self.assertEqual("delivered", result["status"])
        self.assertEqual(
            [
                {"since": "2026-09-07", "until": "2026-09-09", "exception_ids": []},
                {"since": "2026-09-09", "until": "2026-09-11", "exception_ids": []},
            ],
            result["slices"],
        )
        self.assertEqual(6, len(commands))
        state = json.loads(
            (self.state_dir / "review-cycle-state.json").read_text(encoding="utf-8")
        )
        self.assertEqual("2026-09-11", state["completed_through"])


if __name__ == "__main__":
    unittest.main()
