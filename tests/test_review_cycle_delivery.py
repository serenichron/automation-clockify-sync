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
from scripts.autopilot_process import ChildResult


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
) -> Path:
    run_dir = (runs_dir or root / "runs") / name
    run_dir.mkdir(parents=True, exist_ok=True)
    proposals = [proposal()] if proposals is None else proposals
    coverage = (
        {"status": "complete", "incomplete_sources": []}
        if coverage is None
        else coverage
    )
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
                {"analyzer_model": "fixture-model", "analyzer_tier": "primary"}
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
            with mock.patch.object(review_run, "RUNS", active_runs):
                review_run._verify_replay_integrity(active_runs / source_name, run_dir)
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
    if not replay:
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
    return run_dir / "autopilot-result.json"


class ReviewCycleDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        runs_patch = mock.patch.object(review_run, "RUNS", self.root / "runs")
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
                return ChildResult(code, "", "", False, 0.1)
            path = make_run(
                self.root, "source-run", replay=False, **(source_options or {})
            )
            return ChildResult(0, str(path) + "\n", "", False, 0.1)

        return child

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
                return ChildResult(0, "", "", False, 0.1)
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
                commands, source_options={"ambiguous": [{"id": "ambiguous-1"}]}
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
            commands, replay_code=7, source_options={"accounting_remove": ("schema_version",)}
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
        self.assertEqual(cycle._value_digest(expected_row), receipt["expected_row_contract_digest"])
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
                return ChildResult(0, "", "", False, 0.1)
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
