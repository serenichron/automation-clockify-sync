from __future__ import annotations

import datetime as dt
import importlib.util
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import clockify_review_cycle as cycle
from scripts import collector_receipts, collector_slices, evidence_ledger
from scripts import clockify_review_run as review_run
from scripts import source_coverage
from scripts.autopilot_process import ChildResult
from task3_scenario_contract import assert_scenario_contract
_DELIVERY_SPEC = importlib.util.spec_from_file_location(
    "review_cycle_delivery_fixtures", Path(__file__).with_name("test_review_cycle_delivery.py")
)
assert _DELIVERY_SPEC is not None and _DELIVERY_SPEC.loader is not None
_DELIVERY = importlib.util.module_from_spec(_DELIVERY_SPEC)
_DELIVERY_SPEC.loader.exec_module(_DELIVERY)
make_run = _DELIVERY.make_run
write_json = _DELIVERY.write_json


class ReviewCycleSourceDebtTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        runs_patch = mock.patch.object(review_run, "RUNS", self.root / "runs")
        runs_patch.start()
        self.addCleanup(runs_patch.stop)
        receipt_patch = mock.patch.object(
            cycle.clockify_source_debt_recover,
            "verify_recovery_receipt",
            side_effect=lambda run_dir: cycle.clockify_source_debt_recover.RecoveryReceipt(
                self.root / "checkpoints" / "source-debt-recovery-receipts"
                / (Path(run_dir).name + ".json"),
                "sha256:" + "a" * 64,
                {},
            ),
        )
        receipt_patch.start()
        self.addCleanup(receipt_patch.stop)
        self.state_dir = self.root / "state"
        self.cache = self.root / "cache"
        self.cache.mkdir()
        for filename, value in (
            ("routing.json", {"workspace_id": "workspace-1", "member_id": "member-1"}),
            ("corrections.jsonl", {}),
            ("acceptance.jsonl", {}),
        ):
            write_json(self.root / filename, value)
        self.config = {
            "root": str(self.root),
            "runs_dir": str(self.root / "runs"),
            "state_dir": str(self.state_dir),
            "cache": str(self.cache),
            "routing": str(self.root / "routing.json"),
            "corrections": str(self.root / "corrections.jsonl"),
            "acceptance": str(self.root / "acceptance.jsonl"),
            "workspace_id": "workspace-1",
            "member_id": "member-1",
            "recovery_since": "2026-09-07",
            "catchup_until": "2026-09-11",
            "timezone": "Europe/Bucharest",
            "spreadsheet_id": "sheet-1",
            "monthly_sheet_title_template": "{month_name} {year} portfolio review",
            "calendly_optional": True,
            "max_slices": 2,
        }

    def state(self) -> dict[str, object]:
        return json.loads(
            (self.state_dir / "review-cycle-state.json").read_text(encoding="utf-8")
        )

    def debts(self):
        document = source_coverage.read(self.state_dir / "source-coverage.json")
        return source_coverage.SourceDebtStore.from_document(document).active()

    def verified_audit_stage(
        self, name: str, *, since: dt.date, until: dt.date,
        calendly_status: str = "complete", calendly_optional: bool,
        fathom_status: str = "complete",
        machines: tuple[str, ...] = (),
        machine_statuses: dict[str, tuple[str, str]] | None = None,
        compatibility_version: str = "source-debt/v1",
    ) -> dict[str, object]:
        """Create a real, raw-evidence-bound collector bundle for audit tests."""
        local = cycle.ZoneInfo("Europe/Bucharest")
        since_dt = dt.datetime.combine(since, dt.time(), local)
        until_dt = dt.datetime.combine(until, dt.time(), local)
        planned = collector_slices.plan_slices(
            since_dt, until_dt, zone=local, max_days=2,
        )
        self.assertEqual(1, len(planned))
        slice_ = planned[0]
        run_dir = self.root / "runs" / name
        (run_dir / "evidence").mkdir(parents=True)
        raw = {
            "clockify": {"status": "complete", "entries": []},
            "fathom": (
                {"status": "complete", "meetings": []}
                if fathom_status == "complete"
                else {"status": "unavailable", "meetings": []}
            ),
            "calendly": (
                {"status": "complete", "recordings": []}
                if calendly_status == "complete"
                else {"status": "excluded", "complete": True, "recordings": []}
            ),
            "multica_issues": {"status": "complete", "issues": []},
            "sessions": [
                {
                    "machine": machine,
                    "status": (machine_statuses or {}).get(
                        machine, ("complete", "complete")
                    )[0],
                    "repository_evidence_status": (machine_statuses or {}).get(
                        machine, ("complete", "complete")
                    )[1],
                    "repository_events": [],
                }
                for machine in machines
            ],
        }
        filenames = {
            "clockify": "clockify-existing.json",
            "fathom": "fathom-meetings.json",
            "calendly": "calendly-recordings.json",
            "multica_issues": "multica-issues.json",
            "sessions": "sessions.json",
        }
        for key, filename in filenames.items():
            write_json(run_dir / "evidence" / filename, raw[key])
        ledger = evidence_ledger.EvidenceLedger(
            tuple(evidence_ledger.normalize_collector_snapshot(raw)),
            evidence_ledger.source_inventory_from_collector(raw),
        )
        manifest = ledger.manifest.document()
        write_json(run_dir / "evidence" / "evidence-ledger.json", {
            "schema_version": evidence_ledger.SCHEMA_VERSION,
            "manifest": manifest,
            "events": [event.document() for event in ledger.events],
        })
        since_utc = since_dt.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        until_utc = until_dt.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        write_json(run_dir / "run-report.json", {
            "runtime_identity": {"git_sha": "fixture"},
            "date_range": {"since": since_utc, "until": until_utc},
            "collection_mode": {"calendly_optional": calendly_optional},
            "evidence_ledger": {
                "source_completeness": manifest["source_completeness"],
            },
        })
        for relative in (
            "semantic-analysis.json", "work-accounting-result.json",
            "quality_report.json", "review-snapshot.json",
        ):
            write_json(run_dir / relative, {"artifact": relative})
        completion = collector_receipts.build_completion_bundle(
            run_dir, slice_=slice_,
        )
        collector_receipts.write_completion_bundle(
            run_dir / "completion-bundle.json", completion,
        )
        verified = collector_receipts.load_collector_source_bundle(
            run_dir / "completion-bundle.json", run_dir=run_dir,
        )
        return {
            "stage_kind": "collector_source",
            "run_dir": str(run_dir.resolve()),
            "bundle_digest": verified.source_bundle_digest,
            "slice_id": verified.slice_id,
            "since_utc": verified.since_utc,
            "until_utc": verified.until_utc,
            "compatibility_version": compatibility_version,
        }

    def write_audit_state(self, stages: list[dict[str, object]]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        write_json(self.state_dir / "review-cycle-state.json", {
            "schema_version": cycle.SCHEMA_VERSION,
            "completed_through": None,
            "scheduled_through": "2026-09-11",
            "next_work_class": "routine",
            "slices": {
                f"slice-{index}": {"source": stage}
                for index, stage in enumerate(stages)
            },
        })

    def test_verified_recovery_attempt_requires_external_receipt_identity(self):
        parent = {
            "run_dir": str((self.root / "runs" / "parent").resolve()),
            "bundle_digest": "sha256:" + "1" * 64,
        }
        debt_id = "sha256:" + "2" * 64
        attempt = {
            "schema_version": cycle.RECOVERY_ATTEMPT_SCHEMA_VERSION,
            "debt_id": debt_id,
            "attempt_ordinal": 1,
            "attempt_id": cycle._attempt_id(debt_id, 1),
            "parent_run_dir": parent["run_dir"],
            "parent_bundle_digest": parent["bundle_digest"],
            "command_digest": "sha256:" + "3" * 64,
            "phase": "verified_complete",
            "result_path": str((self.root / "runs" / "derived" / "autopilot-result.json").resolve()),
            "result_digest": "sha256:" + "4" * 64,
            "returned_bundle_digest": "sha256:" + "5" * 64,
            "requested_source_outcome": "complete",
            "recovery_receipt_path": str((self.root / "checkpoints" / "source-debt-recovery-receipts" / ("6" * 64 + ".json")).resolve()),
            "recovery_receipt_digest": "sha256:" + "7" * 64,
        }

        checked = cycle._validate_recovery_attempt(
            attempt, debt_id=debt_id, parent=parent
        )
        self.assertEqual(attempt["recovery_receipt_path"], checked["recovery_receipt_path"])
        self.assertEqual(attempt["recovery_receipt_digest"], checked["recovery_receipt_digest"])

        for field in ("recovery_receipt_path", "recovery_receipt_digest"):
            with self.subTest(field=field):
                damaged = dict(attempt)
                damaged.pop(field)
                with self.assertRaisesRegex(cycle.CycleError, "shape"):
                    cycle._validate_recovery_attempt(
                        damaged, debt_id=debt_id, parent=parent
                    )
        for field, value in (
            ("recovery_receipt_path", "relative/receipt.json"),
            ("recovery_receipt_digest", "sha256:not-a-digest"),
        ):
            with self.subTest(corrupt=field):
                damaged = {**attempt, field: value}
                with self.assertRaisesRegex(cycle.CycleError, "verified recovery outcome"):
                    cycle._validate_recovery_attempt(
                        damaged, debt_id=debt_id, parent=parent
                    )

    def child_with_first_gap(self, commands: list[list[str]]):
        def child(command, **_kwargs):
            command = list(command)
            commands.append(command)
            if "--recover-source-debt-from" in command:
                return ChildResult(None, "", "suppressed", True, 0.1)
            if "clockify_sheet_publish.py" in command[1]:
                return _DELIVERY.publisher_result_for_command(self.config, command)
            if "--replay-from" in command:
                source_dir = Path(command[command.index("--replay-from") + 1])
                since = dt.date.fromisoformat(source_dir.name.removeprefix("source-"))
                path = make_run(
                    self.root,
                    f"replay-{since.isoformat()}",
                    replay=True,
                    source_name=source_dir.name,
                    since=since,
                    until=since + dt.timedelta(days=2),
                    snapshots_from=source_dir,
                )
                return ChildResult(0, str(path) + "\n", "", False, 0.1)
            since = dt.date.fromisoformat(command[command.index("--since") + 1])
            coverage = {"status": "complete", "incomplete_sources": []}
            if since == dt.date(2026, 9, 7):
                coverage = {
                    "status": "incomplete",
                    "sources": {"sessions/macbook": {"status": "unavailable"}},
                    "incomplete_sources": ["sessions/macbook"],
                }
            path = make_run(
                self.root,
                f"source-{since.isoformat()}",
                replay=False,
                coverage=coverage,
                since=since,
                until=since + dt.timedelta(days=2),
            )
            return ChildResult(0, str(path) + "\n", "", False, 0.1)

        return child

    def child_complete(self, commands: list[list[str]], *, source_code: int = 0):
        def child(command, **_kwargs):
            command = list(command)
            commands.append(command)
            if "clockify_sheet_publish.py" in command[1]:
                return _DELIVERY.publisher_result_for_command(self.config, command)
            if "--replay-from" in command:
                source_dir = Path(command[command.index("--replay-from") + 1])
                since = dt.date.fromisoformat(source_dir.name.removeprefix("source-"))
                path = make_run(
                    self.root, f"replay-{since.isoformat()}", replay=True,
                    source_name=source_dir.name, since=since,
                    until=since + dt.timedelta(days=2), snapshots_from=source_dir,
                )
                return ChildResult(0, str(path) + "\n", "", False, 0.1)
            since = dt.date.fromisoformat(command[command.index("--since") + 1])
            path = make_run(
                self.root, f"source-{since.isoformat()}", replay=False,
                since=since, until=since + dt.timedelta(days=2),
            )
            return ChildResult(source_code, str(path) + "\n", "", False, 0.1)

        return child

    def test_gap_does_not_block_later_slice_but_contiguous_cursor_stays_behind(self):
        """Catches an incomplete earlier slice starving independent routine work."""
        commands: list[list[str]] = []
        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=self.child_with_first_gap(commands)
        ):
            result = cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 12)
            )

        attempts = [
            command[command.index("--since") + 1]
            for command in commands
            if "--since" in command
        ]
        self.assertEqual(["2026-09-07", "2026-09-09"], attempts)
        self.assertEqual("2026-09-11", self.state()["scheduled_through"])
        self.assertIsNone(self.state()["completed_through"])
        self.assertEqual(["peer/macbook"], [item.interval.source for item in self.debts()])
        self.assertEqual("delivered", result["status"])

    def test_trusted_exact_debt_is_blocked_without_re_adoption_or_resume(self):
        """Catches retrying a finalized incomplete bundle as if it recollected evidence."""
        commands: list[list[str]] = []
        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=self.child_with_first_gap(commands)
        ):
            cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 12)
            )
            second = cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 12)
            )

        state = self.state()
        self.assertEqual("incomplete", state["slices"]["2026-09-07"]["status"])
        self.assertEqual("incomplete", second["status"])
        self.assertFalse(any("--resume-from" in command for command in commands))
        self.assertEqual(1, sum("--since" in command and "2026-09-07" in command for command in commands))
        self.assertEqual(1, sum("--recover-source-debt-from" in command for command in commands))

    def test_timeout_records_one_generic_obligation_before_frontier_advances(self):
        """Catches child failure being forgotten while the routine frontier advances."""
        timed_out = ChildResult(None, "", "suppressed", True, 0.1)
        with mock.patch.object(cycle, "run_child_bounded", return_value=timed_out):
            cycle.run_cycle(
                {**self.config, "max_slices": 1},
                enable_sheet_write=True,
                today=dt.date(2026, 9, 12),
            )

        debt = self.debts()[0]
        self.assertEqual("runner/unclassified", debt.interval.source)
        self.assertEqual("2026-09-09", self.state()["scheduled_through"])
        self.assertIsNone(self.state()["completed_through"])

    def test_verified_unclassified_gap_restarts_through_existing_generic_debt_path(self):
        """Catches a persisted verified gap escaping debt after a pre-debt crash."""
        commands: list[list[str]] = []

        def unclassified_child(command, **_kwargs):
            command = list(command)
            commands.append(command)
            since = dt.date.fromisoformat(command[command.index("--since") + 1])
            path = make_run(
                self.root, f"unclassified-{since.isoformat()}", replay=False,
                coverage={
                    "status": "incomplete",
                    "sources": {"fathom": {"status": "unavailable"}},
                    "incomplete_sources": ["fathom"],
                },
                since=since, until=since + dt.timedelta(days=2),
            )
            return ChildResult(0, str(path) + "\n", "", False, 0.1)

        with mock.patch.object(cycle, "run_child_bounded", side_effect=unclassified_child), \
             mock.patch.object(
                 cycle.source_coverage, "write",
                 side_effect=RuntimeError("before generic debt persistence"),
             ):
            with self.assertRaisesRegex(RuntimeError, "before generic debt persistence"):
                cycle.run_cycle(
                    {**self.config, "max_slices": 1}, enable_sheet_write=True,
                    today=dt.date(2026, 9, 12),
                )

        with mock.patch.object(cycle, "run_child_bounded") as child:
            outcome = cycle.run_cycle(
                {**self.config, "max_slices": 1}, enable_sheet_write=True,
                today=dt.date(2026, 9, 12),
            )

        child.assert_not_called()
        self.assertEqual("recovery_blocked", outcome["status"])
        self.assertEqual(1, len(commands))
        self.assertEqual(
            ["runner/unclassified"],
            [item.interval.source for item in self.debts()],
        )

    def test_quality_failure_persists_exact_peer_debt_from_verified_collector_source(self):
        """Catches semantic/quality failure suppressing an already proven source gap."""
        result_path = self.root / "runs" / "derived" / "autopilot-result.json"
        result_path.parent.mkdir(parents=True)
        result_path.write_text('{"quality_status":"blocked"}\n')
        collector_source_path = result_path.parent / "collector-source.json"
        collector_source_path.write_text("{}\n")
        parent_before = {"collector-source.json": collector_source_path.read_bytes()}
        collector_stage = {
            "stage_kind": "collector_source",
            "result_path": str(result_path),
            "result_digest": "sha256:" + "1" * 64,
            "run_dir": str((self.root / "runs" / "collector-parent").resolve()),
            "run_id": "collector-parent",
            "bundle_digest": "sha256:" + "2" * 64,
            "legacy_completion_bundle_digest": "sha256:" + "3" * 64,
            "runtime_identity_digest": "sha256:" + "4" * 64,
            "collector_runtime_identity_digest": "sha256:" + "4" * 64,
            "executor_runtime_identity_digest": "sha256:" + "5" * 64,
            "snapshot_digests": {},
            "coverage": {
                "status": "incomplete",
                "sources": {
                    "sessions/macbook": {"status": "unavailable"},
                    "repositories/macbook": {"status": "unavailable"},
                },
                "incomplete_sources": [
                    "repositories/macbook", "sessions/macbook",
                ],
            },
            "slice_id": "slice-fixture",
            "since_utc": "2026-09-06T21:00:00Z",
            "until_utc": "2026-09-08T21:00:00Z",
            "compatibility_version": "collector-slice-bundles/v1:" + "a" * 64,
        }
        child = ChildResult(2, str(result_path) + "\n", "quality blocked", False, 0.1)
        real_write = source_coverage.write
        writes = 0

        def crash_after_debt(path, value):
            nonlocal writes
            real_write(path, value)
            writes += 1
            if writes == 1:
                raise RuntimeError("after exact debt persistence")

        with mock.patch.object(cycle, "run_child_bounded", return_value=child), \
             mock.patch.object(
                 cycle, "_validate_collector_source_stage", return_value=collector_stage
             ) as validated, mock.patch.object(
                 cycle.source_coverage, "write", side_effect=crash_after_debt
             ):
            with self.assertRaisesRegex(RuntimeError, "after exact debt persistence"):
                cycle.run_cycle(
                    {**self.config, "max_slices": 1},
                    enable_sheet_write=True,
                    today=dt.date(2026, 9, 12),
                )

        with mock.patch.object(cycle, "run_child_bounded", return_value=child), \
             mock.patch.object(
                 cycle, "_validate_collector_source_stage", return_value=collector_stage
             ):
            outcome = cycle.run_cycle(
                {**self.config, "max_slices": 1}, enable_sheet_write=True,
                today=dt.date(2026, 9, 12),
            )

        self.assertEqual("recovery_blocked", outcome["status"])
        self.assertEqual(["peer/macbook"], [item.interval.source for item in self.debts()])
        validated.assert_called_once()
        record = self.state()["slices"]["2026-09-07"]
        self.assertEqual("collector_source", record["source_parent"]["stage_kind"])

        later_commands: list[list[str]] = []
        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=self.child_complete(later_commands),
        ):
            cycle.run_cycle(
                {**self.config, "max_slices": 2}, enable_sheet_write=True,
                today=dt.date(2026, 9, 12),
            )
        self.assertTrue(any(
            "--since" in command and "2026-09-09" in command
            for command in later_commands
        ))
        self.assertEqual(
            ["peer/macbook"],
            [item.interval.source for item in self.debts()],
        )
        debt_document = source_coverage.read(self.state_dir / "source-coverage.json")
        assert_scenario_contract(
            self,
            stable_ids=[item.debt_id for item in self.debts()],
            parent_before=parent_before,
            parent_after={"collector-source.json": collector_source_path.read_bytes()},
            emitted_ids=[
                event["debt_id"] for event in debt_document["events"]
                if event["event"] == "failure"
            ],
            clockify_adapter_calls=sum(
                "clockify_post_approved_portfolio.py" in command[1]
                for command in later_commands
            ),
        )

    def test_v1_state_migrates_routine_frontier_from_completed_through(self):
        """Catches migration inferring progress from run-directory contents."""
        path = self.state_dir / "review-cycle-state.json"
        write_json(
            path,
            {
                "schema_version": cycle.SCHEMA_VERSION,
                "completed_through": "2026-09-09",
                "slices": {},
            },
        )

        migrated = cycle._state(path, recovery_since="2026-09-07")

        self.assertEqual("2026-09-09", migrated["scheduled_through"])

    def test_two_incomplete_sources_create_independent_exact_debts(self):
        """Catches collapsing multiple canonical source failures into one obligation."""
        commands: list[list[str]] = []
        parent_paths = (self.root / "routing.json", self.root / "corrections.jsonl")
        parent_before = {path.name: path.read_bytes() for path in parent_paths}

        def two_gap_child(command, **kwargs):
            command = list(command)
            commands.append(command)
            if "--recover-source-debt-from" in command:
                return ChildResult(None, "", "suppressed", True, 0.1)
            if "clockify_sheet_publish.py" in command[1]:
                return _DELIVERY.publisher_result_for_command(self.config, command)
            if "--replay-from" in command:
                source_dir = Path(command[command.index("--replay-from") + 1])
                since = dt.date.fromisoformat(source_dir.name.removeprefix("source-"))
                path = make_run(
                    self.root, f"replay-{since.isoformat()}", replay=True,
                    source_name=source_dir.name, since=since,
                    until=since + dt.timedelta(days=2), snapshots_from=source_dir,
                )
                return ChildResult(0, str(path) + "\n", "", False, 0.1)
            since = dt.date.fromisoformat(command[command.index("--since") + 1])
            coverage = {
                "status": "incomplete",
                "sources": {
                    "sessions/macbook": {"status": "unavailable"},
                    "repositories/desktop": {"status": "partial"},
                },
                "incomplete_sources": ["sessions/macbook", "repositories/desktop"],
            }
            if since == dt.date(2026, 9, 9):
                coverage = {"status": "complete", "incomplete_sources": []}
            path = make_run(
                self.root, f"source-{since.isoformat()}", replay=False,
                coverage=coverage, since=since,
                until=since + dt.timedelta(days=2),
            )
            return ChildResult(0, str(path) + "\n", "", False, 0.1)

        real_write = source_coverage.write
        writes = 0

        def crash_after_debt(path, value):
            nonlocal writes
            real_write(path, value)
            writes += 1
            if writes == 1:
                raise RuntimeError("after independent debt persistence")

        with mock.patch.object(cycle, "run_child_bounded", side_effect=two_gap_child), \
             mock.patch.object(cycle.source_coverage, "write", side_effect=crash_after_debt):
            with self.assertRaisesRegex(RuntimeError, "after independent debt persistence"):
                cycle.run_cycle(
                    {**self.config, "max_slices": 1}, enable_sheet_write=True,
                    today=dt.date(2026, 9, 12),
                )

        with mock.patch.object(cycle, "run_child_bounded", side_effect=two_gap_child):
            cycle.run_cycle(
                {**self.config, "max_slices": 1}, enable_sheet_write=True,
                today=dt.date(2026, 9, 12),
            )
            cycle.run_cycle(
                {**self.config, "max_slices": 2}, enable_sheet_write=True,
                today=dt.date(2026, 9, 12),
            )

        self.assertEqual(
            ["peer/desktop", "peer/macbook"],
            sorted(item.interval.source for item in self.debts()),
        )
        self.assertEqual([1, 1], sorted(item.retry_count for item in self.debts()))
        self.assertEqual(1, sum(
            "--since" in command and "2026-09-07" in command
            for command in commands
        ))
        self.assertTrue(any(
            "--since" in command and "2026-09-09" in command
            for command in commands
        ))
        debt_document = source_coverage.read(self.state_dir / "source-coverage.json")
        active = self.debts()
        assert_scenario_contract(
            self,
            stable_ids=[item.debt_id for item in active],
            parent_before=parent_before,
            parent_after={path.name: path.read_bytes() for path in parent_paths},
            emitted_ids=[
                event["debt_id"] for event in debt_document["events"]
                if event["event"] == "failure"
            ],
            clockify_adapter_calls=sum(
                "clockify_post_approved_portfolio.py" in command[1]
                for command in commands
            ),
        )

    def test_coverage_audit_uses_actual_adjacent_verified_bundles(self):
        first = self.verified_audit_stage(
            "audit-one", since=dt.date(2026, 9, 7), until=dt.date(2026, 9, 9),
            calendly_optional=False,
        )
        second = self.verified_audit_stage(
            "audit-two", since=dt.date(2026, 9, 9), until=dt.date(2026, 9, 11),
            calendly_optional=False,
        )
        self.write_audit_state([first, second])

        report = cycle.source_interval_coverage_audit({
            **self.config, "calendly_optional": False,
        })

        self.assertEqual(
            ["calendly", "clockify", "fathom", "multica_issues"],
            report["configured_sources"],
        )
        self.assertEqual(
            "2026-09-10T21:00:00Z", report["frontiers"]["fathom"]
        )
        fathom = [row for row in report["intervals"] if row["source"] == "fathom"]
        self.assertEqual(["complete", "complete"], [row["status"] for row in fathom])
        self.assertTrue(all("resume_state_digest" not in row for row in fathom))
        self.assertNotIn(str(self.root), json.dumps(report))

    def test_coverage_audit_current_config_filters_disabled_optional_and_fleet_sources(self):
        write_json(self.root / "fleet.json", {
            "machines": [
                {"name": "active", "enabled": True},
                {"name": "retired", "enabled": False},
            ],
        })
        stage = self.verified_audit_stage(
            "audit-filtered", since=dt.date(2026, 9, 7),
            until=dt.date(2026, 9, 9), calendly_optional=False,
            machines=("active", "retired"),
        )
        self.write_audit_state([stage])

        report = cycle.source_interval_coverage_audit({
            **self.config, "calendly_optional": True,
        })

        self.assertNotIn("calendly", report["configured_sources"])
        self.assertNotIn("sessions/retired", report["configured_sources"])
        self.assertNotIn("repositories/retired", report["configured_sources"])
        self.assertIn("sessions/active", report["configured_sources"])
        self.assertIn("repositories/active", report["configured_sources"])
        historical_sources = {row["source"] for row in report["intervals"]}
        self.assertIn("calendly", historical_sources)
        self.assertIn("sessions/retired", historical_sources)

    def test_coverage_audit_uses_snapshotted_optional_policy_for_exclusion(self):
        optional = self.verified_audit_stage(
            "audit-optional", since=dt.date(2026, 9, 7),
            until=dt.date(2026, 9, 9), calendly_status="excluded",
            calendly_optional=True,
        )
        self.write_audit_state([optional])
        accepted = cycle.source_interval_coverage_audit({
            **self.config, "calendly_optional": False,
        })
        calendar = [
            row for row in accepted["intervals"] if row["source"] == "calendly"
        ]
        self.assertEqual(["complete"], [row["status"] for row in calendar])

        required = self.verified_audit_stage(
            "audit-required", since=dt.date(2026, 9, 7),
            until=dt.date(2026, 9, 9), calendly_status="excluded",
            calendly_optional=False,
        )
        self.write_audit_state([required])
        with self.assertRaisesRegex(cycle.CycleError, "unbound|excluded"):
            cycle.source_interval_coverage_audit({
                **self.config, "calendly_optional": False,
            })

    def test_coverage_audit_requires_full_exact_debt_compatibility(self):
        stage = self.verified_audit_stage(
            "audit-exact-gap", since=dt.date(2026, 9, 7),
            until=dt.date(2026, 9, 9), calendly_optional=False,
            fathom_status="unavailable", compatibility_version="source-debt/v1",
        )
        self.write_audit_state([stage])
        for compatibility, accepted in (
            ("source-debt/v2", False), ("source-debt/v1", True),
        ):
            store = source_coverage.SourceDebtStore()
            interval = source_coverage.SourceInterval(
                source="fathom", since_utc=str(stage["since_utc"]),
                until_utc=str(stage["until_utc"]), slice_id=str(stage["slice_id"]),
                compatibility_version=compatibility,
            )
            store.record_failure(
                interval, failure_class="offline", retryable=True,
                resume_state_digest="sha256:" + "1" * 64,
                attempted_at="2026-09-10T00:00:00Z",
            )
            source_coverage.write(
                self.state_dir / "source-coverage.json", store.document()
            )
            if accepted:
                report = cycle.source_interval_coverage_audit(self.config)
                self.assertIn(interval.debt_id, report["active_debt_ids"])
            else:
                with self.assertRaisesRegex(cycle.CycleError, "unbound"):
                    cycle.source_interval_coverage_audit(self.config)

    def test_coverage_audit_accepts_only_generic_compatibility_version(self):
        stage = self.verified_audit_stage(
            "audit-generic-gap", since=dt.date(2026, 9, 7),
            until=dt.date(2026, 9, 9), calendly_optional=False,
            fathom_status="unavailable", compatibility_version="source-debt/v1",
        )
        self.write_audit_state([stage])
        for compatibility, accepted in (
            ("runner-unclassified/v2", False),
            (cycle.GENERIC_COMPATIBILITY_VERSION, True),
        ):
            store = source_coverage.SourceDebtStore()
            interval = source_coverage.SourceInterval(
                source="runner/unclassified", since_utc=str(stage["since_utc"]),
                until_utc=str(stage["until_utc"]), slice_id=str(stage["slice_id"]),
                compatibility_version=compatibility,
            )
            store.record_failure(
                interval, failure_class="coverage_unclassified", retryable=True,
                resume_state_digest="sha256:" + "2" * 64,
                attempted_at="2026-09-10T00:00:00Z",
            )
            source_coverage.write(
                self.state_dir / "source-coverage.json", store.document()
            )
            if accepted:
                report = cycle.source_interval_coverage_audit(self.config)
                self.assertIn(interval.debt_id, report["active_debt_ids"])
            else:
                with self.assertRaisesRegex(cycle.CycleError, "unbound"):
                    cycle.source_interval_coverage_audit(self.config)

    def test_coverage_audit_projects_exact_outcomes_and_cli_is_observer_only(self):
        first = source_coverage.SourceInterval(
            source="fathom", since_utc="2026-09-06T21:00:00Z",
            until_utc="2026-09-08T21:00:00Z", slice_id="slice-one",
            compatibility_version="source-debt/v1",
        )
        second = source_coverage.SourceInterval(
            source="fathom", since_utc="2026-09-08T21:00:00Z",
            until_utc="2026-09-10T21:00:00Z", slice_id="slice-two",
            compatibility_version="source-debt/v1",
        )
        store = source_coverage.SourceDebtStore()
        store.record_failure(
            first, failure_class="offline", retryable=True,
            resume_state_digest="sha256:" + "1" * 64,
            attempted_at="2026-09-09T00:00:00Z",
        )
        store.record_complete(
            first, completion_bundle_digest="sha256:" + "2" * 64,
            completed_at="2026-09-10T00:00:00Z",
        )
        store.record_failure(
            second, failure_class="offline", retryable=True,
            resume_state_digest="sha256:" + "3" * 64,
            attempted_at="2026-09-11T00:00:00Z",
        )
        self.write_audit_state([])
        source_coverage.write(
            self.state_dir / "source-coverage.json", store.document()
        )
        before = {
            path.name: path.read_bytes() for path in self.state_dir.iterdir()
        }

        report = cycle.source_interval_coverage_audit(self.config)

        rows = [row for row in report["intervals"] if row["source"] == "fathom"]
        self.assertEqual(["resolved", "active"], [row["status"] for row in rows])
        self.assertIn("completion_bundle_digest", rows[0])
        self.assertNotIn("resume_state_digest", rows[0])
        self.assertIn("resume_state_digest", rows[1])
        self.assertNotIn("completion_bundle_digest", rows[1])
        self.assertEqual([second.debt_id], report["active_debt_ids"])

        config_path = self.root / "cycle-config.json"
        output = self.root / "reports" / "source-interval-coverage-audit.json"
        write_json(config_path, self.config)
        with mock.patch.object(
            cycle, "run_cycle", side_effect=AssertionError("audit scheduled work")
        ), mock.patch.object(
            cycle.clockify_review_run.clockify_sync_collect,
            "collector_runtime_identity",
            side_effect=AssertionError("audit inspected runtime"),
        ):
            self.assertEqual(0, cycle.main([
                "--config", str(config_path),
                "--audit-coverage-output", str(output),
            ]))
        self.assertEqual(report, json.loads(output.read_text()))
        self.assertEqual(
            before,
            {path.name: path.read_bytes() for path in self.state_dir.iterdir()},
        )
        with self.assertRaisesRegex(cycle.CycleError, "transient reports path"):
            cycle._coverage_audit_output_path(
                self.config, self.state_dir / "source-coverage.json"
            )

    def test_two_facets_for_one_machine_coalesce_into_one_peer_debt(self):
        """One combined peer exporter must never create two transport attempts."""
        store = source_coverage.SourceDebtStore()
        coverage = {
            "status": "incomplete",
            "sources": {
                "sessions/macbook": {"status": "unavailable"},
                "repositories/macbook": {"status": "unavailable"},
            },
            "incomplete_sources": [
                "repositories/macbook", "sessions/macbook",
            ],
        }
        manifest = cycle._ensure_period(
            self.config, self.state_dir, "2026-09-07", "2026-09-09",
            bind_inputs=True,
        )
        result = make_run(
            self.root, "peer-parent", replay=False, coverage=coverage,
        )
        source = cycle._validate_stage(
            self.config, result, "2026-09-07", "2026-09-09", replay=False,
            expected_snapshot_digests=cycle._expected_snapshot_digests(
                self.config, manifest
            ),
        )

        self.assertTrue(cycle._record_exact_debts(self.config, store, source))

        debts = store.active()
        self.assertEqual(1, len(debts))
        self.assertEqual("peer/macbook", debts[0].interval.source)

    def test_peer_recovery_projects_each_missing_facet_into_audit_frontiers(self):
        """Shared transport debt must not hide either configured source facet."""
        write_json(self.root / "fleet.json", {
            "machines": [{"name": "macbook", "enabled": True}],
        })
        source = self.verified_audit_stage(
            "audit-peer-gap", since=dt.date(2026, 9, 7),
            until=dt.date(2026, 9, 9), calendly_optional=False,
            machines=("macbook",),
            machine_statuses={"macbook": ("unavailable", "unavailable")},
        )
        self.write_audit_state([source])
        store = source_coverage.SourceDebtStore()
        peer_interval = source_coverage.SourceInterval(
            source="peer/macbook",
            since_utc=str(source["since_utc"]),
            until_utc=str(source["until_utc"]),
            slice_id=str(source["slice_id"]),
            compatibility_version=str(source["compatibility_version"]),
        )
        store.record_failure(
            peer_interval,
            failure_class="coverage_incomplete",
            retryable=True,
            resume_state_digest="sha256:" + "8" * 64,
            attempted_at="2026-09-08T00:00:00Z",
        )
        peer = store.active()[0]
        store.record_complete(
            peer.interval,
            completion_bundle_digest="sha256:" + "9" * 64,
            completed_at="2026-09-09T00:00:00Z",
        )
        source_coverage.write(
            self.state_dir / "source-coverage.json", store.document()
        )

        report = cycle.source_interval_coverage_audit(self.config)

        facets = {
            row["source"]: row for row in report["intervals"]
            if row["source"] in {"sessions/macbook", "repositories/macbook"}
        }
        self.assertEqual(
            {"sessions/macbook", "repositories/macbook"}, set(facets)
        )
        self.assertEqual({"resolved"}, {row["status"] for row in facets.values()})
        self.assertEqual({peer.debt_id}, {row["operational_debt_id"] for row in facets.values()})
        self.assertEqual(
            "2026-09-08T21:00:00Z", report["frontiers"]["sessions/macbook"]
        )
        self.assertEqual(
            "2026-09-08T21:00:00Z", report["frontiers"]["repositories/macbook"]
        )

    def test_old_release_incomplete_bundle_is_recovery_parent_not_current_source(self):
        """Catches direct adoption/publication of a deterministic old-release bundle."""
        coverage = {
            "status": "incomplete",
            "sources": {
                "sessions/omarchy-desktop": {"status": "unavailable"},
                "repositories/omarchy-desktop": {"status": "partial"},
            },
            "incomplete_sources": [
                "repositories/omarchy-desktop", "sessions/omarchy-desktop",
            ],
        }
        manifest = cycle._ensure_period(
            self.config, self.state_dir, "2026-09-07", "2026-09-09",
            bind_inputs=True,
        )
        expected = cycle._expected_snapshot_digests(self.config, manifest)
        result_path = make_run(
            self.root, "old-release-source", replay=False, coverage=coverage,
            since=dt.date(2026, 9, 7), until=dt.date(2026, 9, 9),
            runtime_identity={"git_sha": "6092f453"},
        )

        with self.assertRaisesRegex(cycle.CycleError, "runtime identity"):
            cycle._validate_stage(
                {**self.config, "_runtime_identity": {"git_sha": "bab6bdf8"}},
                result_path, "2026-09-07", "2026-09-09", replay=False,
                expected_snapshot_digests=expected,
            )

        historical = cycle._validate_stage(
            {**self.config, "_runtime_identity": {"git_sha": "bab6bdf8"}},
            result_path, "2026-09-07", "2026-09-09", replay=False,
            expected_snapshot_digests=expected, allow_historical_runtime=True,
        )
        self.assertNotEqual(
            cycle._value_digest({"git_sha": "bab6bdf8"}),
            historical["runtime_identity_digest"],
        )
        self.assertEqual(
            coverage["incomplete_sources"],
            historical["coverage"]["incomplete_sources"],
        )

    def test_delivered_legacy_source_and_replay_bind_historical_runtime_once(self):
        """A runtime upgrade must not invalidate already delivered bab6 history."""
        old_runtime = {"git_sha": "bab6bdf8"}
        old_config = {
            **self.config,
            "catchup_until": "2026-09-09",
            "max_slices": 1,
            "_runtime_identity": old_runtime,
        }

        def historical_child(command, **_kwargs):
            command = list(command)
            if "clockify_sheet_publish.py" in command[1]:
                return _DELIVERY.publisher_result_for_command(self.config, command)
            if "--replay-from" in command:
                source_dir = Path(command[command.index("--replay-from") + 1])
                path = make_run(
                    self.root, "legacy-replay", replay=True,
                    source_name=source_dir.name,
                    since=dt.date(2026, 9, 7), until=dt.date(2026, 9, 9),
                    snapshots_from=source_dir, runtime_identity=old_runtime,
                )
            else:
                path = make_run(
                    self.root, "legacy-source", replay=False,
                    since=dt.date(2026, 9, 7), until=dt.date(2026, 9, 9),
                    runtime_identity=old_runtime,
                )
            return ChildResult(0, str(path) + "\n", "", False, 0.1)

        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=historical_child
        ):
            delivered = cycle.run_cycle(
                old_config, enable_sheet_write=True, today=dt.date(2026, 9, 12)
            )
        self.assertEqual("delivered", delivered["status"])

        state_path = self.state_dir / "review-cycle-state.json"
        legacy_state = self.state()
        record = legacy_state["slices"]["2026-09-07"]
        record["source"].pop("runtime_identity_digest")
        record["replay"].pop("runtime_identity_digest")
        write_json(state_path, legacy_state)

        new_config = {
            **old_config,
            "_runtime_identity": {"git_sha": "new-release"},
        }
        with mock.patch.object(
            cycle, "run_child_bounded",
            side_effect=AssertionError("delivered history must not rerun children"),
        ) as child:
            result = cycle.run_cycle(
                new_config, enable_sheet_write=True, today=dt.date(2026, 9, 12)
            )

        self.assertEqual("idle", result["status"])
        child.assert_not_called()
        migrated = self.state()["slices"]["2026-09-07"]
        expected = cycle._value_digest(old_runtime)
        self.assertEqual(expected, migrated["source"]["runtime_identity_digest"])
        self.assertEqual(expected, migrated["replay"]["runtime_identity_digest"])

    def test_offline_to_online_health_epoch_reactivates_exhausted_exact_debt_once(self):
        """Catches runtime-only retries or repeated retries while health is unchanged."""
        store = source_coverage.SourceDebtStore()
        interval = source_coverage.SourceInterval(
            source="sessions/omarchy-desktop",
            since_utc="2026-09-06T21:00:00Z",
            until_utc="2026-09-08T21:00:00Z",
            slice_id="slice-health-fixture",
            compatibility_version="collector-backlog/v1:fixture",
        )
        failed = store.record_failure(
            interval, failure_class="offline", retryable=True,
            resume_state_digest="sha256:offline-fixture",
            attempted_at="2026-09-09T00:00:00Z",
        )
        store.exhaust(failed.debt_id, terminal_reason="retry_limit")
        state: dict[str, object] = {}

        with mock.patch.object(cycle, "_probe_source_health", return_value="offline"):
            self.assertTrue(cycle._reactivate_health_transitions(self.config, state, store))
        self.assertEqual("exhausted", store.get(failed.debt_id).status)
        with mock.patch.object(cycle, "_probe_source_health", return_value="online"):
            self.assertTrue(cycle._reactivate_health_transitions(self.config, state, store))
            events_after_online = len(store.document()["events"])
            self.assertFalse(cycle._reactivate_health_transitions(self.config, state, store))

        self.assertEqual("active", store.get(failed.debt_id).status)
        self.assertEqual(events_after_online, len(store.document()["events"]))
        self.assertEqual("source_health_transition", store.get(failed.debt_id).failure_class)

        store.exhaust(failed.debt_id, terminal_reason="retry_limit")
        events_before_second_epoch = len(store.document()["events"])
        with mock.patch.object(cycle, "_probe_source_health", return_value="offline"):
            self.assertTrue(cycle._reactivate_health_transitions(self.config, state, store))
        self.assertEqual("exhausted", store.get(failed.debt_id).status)
        self.assertEqual(events_before_second_epoch, len(store.document()["events"]))
        with mock.patch.object(cycle, "_probe_source_health", return_value="online"):
            self.assertTrue(cycle._reactivate_health_transitions(self.config, state, store))
        self.assertEqual("active", store.get(failed.debt_id).status)
        self.assertEqual(events_before_second_epoch + 1, len(store.document()["events"]))

    def test_health_probe_rejects_unsafe_ssh_configuration_before_spawn(self):
        """Catches health checks accepting command-executing SSH directives."""
        cases = (
            [],
            ["-o", "BatchMode=yes"],
            ["/dev/null", "-F"],
            ["-F", "/dev/null", "-F", "/dev/null"],
            ["-F", "/tmp/config"],
            ["-F", "/dev/null", "-o", "ProxyCommand=sh -c exploit"],
            ["-F", "/dev/null", "-oLocalCommand=sh -c exploit"],
        )
        for index, options in enumerate(cases):
            with self.subTest(options=options):
                write_json(self.root / "fleet.json", {
                    "machines": [{
                        "name": "macbook", "enabled": True, "kind": "ssh",
                        "host": "macbook.example.test",
                    }],
                    "ssh_options": options,
                })
                with mock.patch.object(cycle.subprocess, "run") as spawned:
                    self.assertEqual(
                        "offline", cycle._probe_source_health(
                            self.config, "peer/macbook", timeout_seconds=1 + index,
                        )
                    )
                spawned.assert_not_called()

        with mock.patch.object(cycle.subprocess, "run") as spawned:
            self.assertEqual(
                "offline", cycle._probe_source_health(
                    self.config, "peer/not-in-fleet", timeout_seconds=1,
                )
            )
        spawned.assert_not_called()

    def test_health_probe_uses_allowlisted_host_and_suppresses_all_output(self):
        """Catches arbitrary destinations or retained probe output."""
        write_json(self.root / "fleet.json", {
            "machines": [{
                "name": "macbook", "enabled": True, "kind": "ssh",
                "host": "macbook.example.test",
            }],
            "ssh_options": ["-F", "/dev/null", "-o", "BatchMode=yes"],
        })
        completed = mock.Mock(returncode=0)
        with mock.patch.object(
            cycle.subprocess, "run", return_value=completed
        ) as spawned:
            self.assertEqual(
                "online", cycle._probe_source_health(
                    self.config, "peer/macbook", timeout_seconds=2,
                )
            )

        command = spawned.call_args.args[0]
        self.assertEqual("macbook.example.test", command[-2])
        self.assertEqual("true", command[-1])
        self.assertIs(cycle.subprocess.DEVNULL, spawned.call_args.kwargs["stdout"])
        self.assertIs(cycle.subprocess.DEVNULL, spawned.call_args.kwargs["stderr"])
        self.assertEqual(2, spawned.call_args.kwargs["timeout"])

    def test_health_probes_dedupe_machine_and_respect_aggregate_budget(self):
        """Catches per-debt probing and unbounded fleet health latency."""
        store = source_coverage.SourceDebtStore()
        sources = ["peer/macbook", "sessions/macbook", "peer/desktop", "peer/laptop"]
        for index, source in enumerate(sources):
            interval = source_coverage.SourceInterval(
                source=source,
                since_utc="2026-09-06T21:00:00Z",
                until_utc="2026-09-08T21:00:00Z",
                slice_id=f"slice-health-{index}",
                compatibility_version="collector-backlog/v1:fixture",
            )
            failed = store.record_failure(
                interval, failure_class="offline", retryable=True,
                resume_state_digest=f"sha256:offline-{index}",
                attempted_at="2026-09-09T00:00:00Z",
            )
            store.exhaust(failed.debt_id, terminal_reason="retry_limit")

        state: dict[str, object] = {}
        with mock.patch.object(
            cycle, "_probe_source_health", return_value="offline"
        ) as probe:
            cycle._reactivate_health_transitions(self.config, state, store)

        self.assertEqual(3, probe.call_count)
        self.assertEqual(
            {"peer/macbook", "peer/desktop", "peer/laptop"},
            {call.args[1] for call in probe.call_args_list},
        )
        self.assertLessEqual(
            sum(call.kwargs["timeout_seconds"] for call in probe.call_args_list),
            cycle.HEALTH_PROBE_BUDGET_SECONDS,
        )

    def test_health_transition_reconciles_debt_first_crash_without_duplicate(self):
        """Catches a crash between durable debt and epoch-state writes."""
        store = source_coverage.SourceDebtStore()
        interval = source_coverage.SourceInterval(
            source="peer/macbook",
            since_utc="2026-09-06T21:00:00Z",
            until_utc="2026-09-08T21:00:00Z",
            slice_id="slice-health-crash",
            compatibility_version="collector-backlog/v1:fixture",
        )
        failed = store.record_failure(
            interval, failure_class="offline", retryable=True,
            resume_state_digest="sha256:offline-crash",
            attempted_at="2026-09-09T00:00:00Z",
        )
        store.exhaust(failed.debt_id, terminal_reason="retry_limit")
        with mock.patch.object(cycle, "_probe_source_health", return_value="online"):
            state_before_crash: dict[str, object] = {}
            self.assertTrue(
                cycle._reactivate_health_transitions(
                    self.config, state_before_crash, store
                )
            )
        durable = store.document()
        event_count = len(durable["events"])

        restarted = source_coverage.SourceDebtStore.from_document(durable)
        restarted_state: dict[str, object] = {}
        with mock.patch.object(cycle, "_probe_source_health", return_value="online"):
            self.assertTrue(
                cycle._reactivate_health_transitions(
                    self.config, restarted_state, restarted
                )
            )

        self.assertEqual(event_count, len(restarted.document()["events"]))
        self.assertEqual(
            f"online:0",
            restarted_state["source_health_epochs"][interval.debt_id],
        )

    def test_generic_retry_resolves_only_after_verified_complete_bundle(self):
        """Catches clearing an unclassified failure without verified complete coverage."""
        timeout = ChildResult(None, "", "suppressed", True, 0.1)
        with mock.patch.object(cycle, "run_child_bounded", return_value=timeout):
            cycle.run_cycle(
                {**self.config, "max_slices": 1}, enable_sheet_write=True,
                today=dt.date(2026, 9, 12),
            )

        commands: list[list[str]] = []
        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=self.child_with_first_gap(commands)
        ):
            result = cycle.run_cycle(
                {**self.config, "max_slices": 1}, enable_sheet_write=True,
                today=dt.date(2026, 9, 12),
            )

        self.assertEqual("recovery_blocked", result["status"])
        active_sources = [item.interval.source for item in self.debts()]
        self.assertIn("runner/unclassified", active_sources)
        self.assertIn("peer/macbook", active_sources)

    def test_generic_retry_is_resolved_by_verified_complete_bundle(self):
        """Catches leaving a generic obligation active after exact complete proof."""
        timeout = ChildResult(None, "", "suppressed", True, 0.1)
        config = {**self.config, "catchup_until": "2026-09-09", "max_slices": 1}
        with mock.patch.object(cycle, "run_child_bounded", return_value=timeout):
            cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))

        commands: list[list[str]] = []
        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=self.child_complete(commands)
        ):
            result = cycle.run_cycle(
                config, enable_sheet_write=True, today=dt.date(2026, 9, 12)
            )

        self.assertEqual("delivered", result["status"])
        self.assertEqual((), self.debts())
        document = source_coverage.read(self.state_dir / "source-coverage.json")
        self.assertEqual(1, sum(event["event"] == "complete" for event in document["events"]))

    def test_overall_complete_exact_recovery_resolves_matching_generic_obligation(self):
        """Catches exact recovery leaving its interval's generic retry debt active."""
        config = {
            **self.config,
            "catchup_until": "2026-09-09",
            "max_slices": 1,
            "total_child_budget_seconds": 7200,
        }
        with mock.patch.object(
            cycle, "run_child_bounded",
            return_value=ChildResult(None, "", "suppressed", True, 0.1),
        ):
            cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))

        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=self.child_with_first_gap([])
        ):
            cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))

        parent = Path(self.state()["slices"]["2026-09-07"]["source"]["run_dir"])
        commands: list[list[str]] = []

        def recovered_child(command, **_kwargs):
            command = list(command)
            commands.append(command)
            if "--recover-source-debt-from" in command:
                with mock.patch.object(
                    collector_slices.BacklogStore, "record_complete", return_value=None
                ):
                    result_path = make_run(
                        self.root, "recovery-overall-complete", replay=False,
                        since=dt.date(2026, 9, 7), until=dt.date(2026, 9, 9),
                        snapshots_from=parent,
                    )
                return ChildResult(0, str(result_path) + "\n", "", False, 0.1)
            if "--replay-from" in command:
                source_dir = Path(command[command.index("--replay-from") + 1])
                result_path = make_run(
                    self.root, "replay-overall-complete", replay=True,
                    source_name=source_dir.name, since=dt.date(2026, 9, 7),
                    until=dt.date(2026, 9, 9), snapshots_from=source_dir,
                )
                return ChildResult(0, str(result_path) + "\n", "", False, 0.1)
            self.assertIn("clockify_sheet_publish.py", command[1])
            return _DELIVERY.publisher_result_for_command(self.config, command)

        def verified(run_dir, **_kwargs):
            bundle = cycle.collector_receipts.load_completion_bundle(
                Path(run_dir) / "completion-bundle.json", run_dir=Path(run_dir)
            )
            return bundle, "complete"

        state = self.state()
        state["next_work_class"] = "exact"
        write_json(self.state_dir / "review-cycle-state.json", state)
        with mock.patch.object(cycle, "run_child_bounded", side_effect=recovered_child), \
             mock.patch.object(
                 cycle.clockify_review_run,
                 "verify_source_debt_recovery_completion",
                 side_effect=verified,
             ):
            result = cycle.run_cycle(
                config, enable_sheet_write=True, today=dt.date(2026, 9, 12)
            )

        self.assertEqual("delivered", result["status"])
        self.assertEqual((), self.debts())
        self.assertEqual(0, sum("--since" in command for command in commands))
        self.assertEqual(
            1, sum("--recover-source-debt-from" in command for command in commands)
        )

    def test_nonzero_child_with_verified_result_can_complete_exact_slice(self):
        """Catches discarding trustworthy bundle evidence solely due to exit status."""
        commands: list[list[str]] = []
        with mock.patch.object(
            cycle, "run_child_bounded",
            side_effect=self.child_complete(commands, source_code=7),
        ):
            result = cycle.run_cycle(
                {**self.config, "catchup_until": "2026-09-09", "max_slices": 1},
                enable_sheet_write=True, today=dt.date(2026, 9, 12),
            )

        self.assertEqual("delivered", result["status"])
        self.assertEqual((), self.debts())

    def test_nonzero_child_without_safe_result_records_generic_debt(self):
        """Catches guessing a source identity from an unverified child failure."""
        failed = ChildResult(7, "not-a-safe-path\n", "suppressed", False, 0.1)
        with mock.patch.object(cycle, "run_child_bounded", return_value=failed):
            cycle.run_cycle(
                {**self.config, "max_slices": 1}, enable_sheet_write=True,
                today=dt.date(2026, 9, 12),
            )

        self.assertEqual(["runner/unclassified"], [item.interval.source for item in self.debts()])

    def test_debt_write_crash_converges_without_duplicate_exact_failure(self):
        """Catches replaying the same verified incomplete bundle as a new failure event."""
        commands: list[list[str]] = []
        parent_paths = (self.root / "routing.json", self.root / "corrections.jsonl")
        parent_before = {path.name: path.read_bytes() for path in parent_paths}
        real_write = source_coverage.write
        writes = 0

        def interrupt_after_write(path, value):
            nonlocal writes
            real_write(path, value)
            writes += 1
            if writes == 1:
                raise RuntimeError("after debt write")

        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=self.child_with_first_gap(commands)
        ), mock.patch.object(cycle.source_coverage, "write", side_effect=interrupt_after_write):
            with self.assertRaisesRegex(RuntimeError, "after debt write"):
                cycle.run_cycle(
                    {**self.config, "max_slices": 1}, enable_sheet_write=True,
                    today=dt.date(2026, 9, 12),
                )

        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=self.child_with_first_gap(commands)
        ):
            cycle.run_cycle(
                {**self.config, "max_slices": 1}, enable_sheet_write=True,
                today=dt.date(2026, 9, 12),
            )

        document = source_coverage.read(self.state_dir / "source-coverage.json")
        failures = [event for event in document["events"] if event["event"] == "failure"]
        self.assertEqual(1, len(failures))
        self.assertEqual(1, sum("--since" in command for command in commands))
        assert_scenario_contract(
            self,
            stable_ids=[item.debt_id for item in self.debts()],
            parent_before=parent_before,
            parent_after={path.name: path.read_bytes() for path in parent_paths},
            emitted_ids=[event["debt_id"] for event in failures],
            clockify_adapter_calls=sum(
                "clockify_post_approved_portfolio.py" in command[1]
                for command in commands
            ),
        )

    def test_selector_never_schedules_current_day_and_splits_at_month_boundary(self):
        """Catches partial-day work or a routine slice crossing into another month."""
        config = {**self.config, "recovery_since": "2026-09-30", "catchup_until": "2026-10-03"}
        state = {
            "schema_version": cycle.SCHEMA_VERSION,
            "completed_through": None,
            "scheduled_through": "2026-09-30",
            "slices": {},
        }

        selected = cycle._select_work(
            config, state, source_coverage.SourceDebtStore(),
            today=dt.date(2026, 10, 2),
        )

        self.assertEqual(
            [("2026-09-30", "2026-10-01"), ("2026-10-01", "2026-10-02")],
            [(since, until) for since, until, _kind, _debt in selected],
        )

    def test_real_generic_retry_advances_retry_count_then_exhausts(self):
        """Catches deterministic failure dedup suppressing every later real retry."""
        config = {**self.config, "catchup_until": "2026-09-09", "max_slices": 1}
        timed_out = ChildResult(None, "", "suppressed", True, 0.1)
        commands: list[list[str]] = []

        def child(command, **_kwargs):
            commands.append(list(command))
            return timed_out

        with mock.patch.object(cycle, "run_child_bounded", side_effect=child):
            cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))
            cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))

        debt = self.debts()[0]
        self.assertEqual(2, len(commands))
        self.assertEqual(2, debt.retry_count)
        self.assertEqual("exhausted", debt.status)
        self.assertEqual("retry_limit", debt.terminal_reason)

    def test_later_cycle_does_not_retry_exhausted_generic_without_runtime_change(self):
        """Catches unchanged exhausted generic debt looping indefinitely."""
        config = {**self.config, "catchup_until": "2026-09-09", "max_slices": 1}
        timed_out = ChildResult(None, "", "suppressed", True, 0.1)
        commands: list[list[str]] = []

        def timeout_child(command, **_kwargs):
            commands.append(list(command))
            return timed_out

        with mock.patch.object(cycle, "run_child_bounded", side_effect=timeout_child):
            cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))
            cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))

        self.assertEqual("exhausted", self.debts()[0].status)
        with mock.patch.object(cycle, "run_child_bounded") as child:
            result = cycle.run_cycle(
                config, enable_sheet_write=True, today=dt.date(2026, 9, 12)
            )

        source_commands = [command for command in commands if "--since" in command]
        self.assertEqual(2, len(source_commands))
        child.assert_not_called()
        self.assertEqual("idle", result["status"])
        self.assertEqual("exhausted", self.debts()[0].status)
        events = source_coverage.read(self.state_dir / "source-coverage.json")["events"]
        self.assertEqual(["failure", "failure", "exhausted"], [
            event["event"] for event in events
        ])

    def test_generic_debt_write_crash_replays_same_attempt_without_new_child(self):
        """Catches a persistence crash being counted as a second real retry."""
        config = {**self.config, "catchup_until": "2026-09-09", "max_slices": 1}
        timed_out = ChildResult(None, "", "suppressed", True, 0.1)
        commands: list[list[str]] = []
        real_write = source_coverage.write

        def child(command, **_kwargs):
            commands.append(list(command))
            return timed_out

        def interrupt_after_write(path, value):
            real_write(path, value)
            raise RuntimeError("after generic debt write")

        with mock.patch.object(cycle, "run_child_bounded", side_effect=child), mock.patch.object(
            cycle.source_coverage, "write", side_effect=interrupt_after_write
        ):
            with self.assertRaisesRegex(RuntimeError, "after generic debt write"):
                cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))

        with mock.patch.object(cycle, "run_child_bounded", side_effect=child):
            cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))

        debt = self.debts()[0]
        self.assertEqual(1, len(commands))
        self.assertEqual(1, debt.retry_count)
        self.assertEqual("active", debt.status)

    def seed_source_less_exhausted(self):
        """Create legacy exhausted state using real bounded failures, not receipts."""
        config = {**self.config, "catchup_until": "2026-09-09", "max_slices": 1}
        with mock.patch.object(
            cycle, "run_child_bounded",
            return_value=ChildResult(None, "", "", True, 0.1),
        ):
            for _ in range(2):
                cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))
        return {**config, "_runtime_identity": {"git_sha": "fixture-sha"}}

    def test_source_less_legacy_classification_is_fair_and_plan_is_immutable(self):
        """Catches absent-source debt starvation or planning consuming its runtime pass."""
        config = {**self.seed_source_less_exhausted(), "catchup_until": "2026-09-11"}
        paths = [self.state_dir / name for name in (
            "review-cycle-state.json", "source-coverage.json",
        )]
        before = [path.read_bytes() for path in paths]
        store = source_coverage.SourceDebtStore.from_document(source_coverage.read(paths[1]))
        state = self.state()
        recovery = cycle._select_work(
            config, {**state, "next_work_class": "exact"}, store,
            today=dt.date(2026, 9, 12),
        )
        self.assertEqual([("2026-09-07", "generic_classification")], [
            (item[0], item[2]) for item in recovery
        ])
        paired = cycle._select_work(
            {**config, "max_slices": 2}, state, store, today=dt.date(2026, 9, 12),
        )
        self.assertEqual(["generic_classification", "routine"], [item[2] for item in paired])
        with mock.patch.object(cycle, "run_child_bounded") as child:
            plan = cycle.run_cycle(config, enable_sheet_write=False, today=dt.date(2026, 9, 12))
        self.assertEqual([{"since": "2026-09-09", "until": "2026-09-11"}], plan["slices"])
        child.assert_not_called()
        self.assertEqual(before, [path.read_bytes() for path in paths])

    def test_source_less_classification_failure_is_once_per_runtime(self):
        """Catches record_failure reopening exhausted debt for a second same-runtime run."""
        config = self.seed_source_less_exhausted()
        commands = []

        def child(command, **_kwargs):
            commands.append(command)
            record = self.state()["slices"]["2026-09-07"]
            self.assertEqual("started", record["source_attempt"]["status"])
            self.assertEqual(3 if len(commands) == 1 else 4, record["runner_attempt"]["source_attempt_ordinal"])
            return ChildResult(None, "", "", True, 0.1)

        with mock.patch.object(cycle, "run_child_bounded", side_effect=child):
            first = cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))
            second = cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))
            changed = {**config, "_runtime_identity": {"git_sha": "next-fixture"}}
            third = cycle.run_cycle(changed, enable_sheet_write=True, today=dt.date(2026, 9, 12))
            fourth = cycle.run_cycle(changed, enable_sheet_write=True, today=dt.date(2026, 9, 12))
        self.assertEqual(["incomplete", "idle", "incomplete", "idle"], [
            result["status"] for result in (first, second, third, fourth)
        ])
        self.assertEqual(2, len(commands))
        self.assertEqual("exhausted", self.debts()[0].status)
        self.assertEqual("retry_limit", self.debts()[0].terminal_reason)
        events = source_coverage.read(self.state_dir / "source-coverage.json")["events"]
        self.assertEqual(["failure", "failure", "exhausted", "failure", "exhausted", "failure", "exhausted"], [
            event["event"] for event in events
        ])
        self.assertEqual({"ordinal", "command_digest", "resume_state_digest", "status", "advance_frontier"},
                         set(self.state()["slices"]["2026-09-07"]["source_attempt"]))

    def test_current_runtime_routine_exhaustion_does_not_get_extra_classification(self):
        """Catches treating a known same-runtime failure as unclassified legacy history."""
        config = {**self.config, "catchup_until": "2026-09-09", "max_slices": 1,
                  "_runtime_identity": {"git_sha": "fixture"}}
        with mock.patch.object(cycle, "run_child_bounded", return_value=ChildResult(None, "", "", True, 0.1)) as child:
            for _ in range(2):
                cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))
            result = cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))
        self.assertEqual(2, child.call_count)
        self.assertEqual("idle", result["status"])
        self.assertEqual(2, self.state()["slices"]["2026-09-07"]["runner_attempt"]["source_attempt_ordinal"])

    def test_source_less_classification_creates_verified_exact_debt(self):
        """Catches resolving generic debt without real evidence or blocking exact promotion."""
        config = self.seed_source_less_exhausted()
        generic_id = self.debts()[0].debt_id
        commands = []
        with mock.patch.object(cycle, "run_child_bounded", side_effect=self.child_with_first_gap(commands)):
            result = cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))
        self.assertEqual("recovery_blocked", result["status"])
        self.assertEqual(["peer/macbook"], [item.interval.source for item in self.debts()])
        store = source_coverage.SourceDebtStore.from_document(source_coverage.read(self.state_dir / "source-coverage.json"))
        self.assertEqual("resolved", store.get(generic_id).status)
        selected = cycle._select_work(config, self.state(), store, today=dt.date(2026, 9, 12))
        self.assertEqual(["exact"], [item[2] for item in selected])
        self.assertEqual(1, len(commands))
        self.assertEqual("2026-09-09", self.state()["scheduled_through"])
        self.assertIsNone(self.state()["completed_through"])

    def test_source_less_classification_budget_and_pre_spawn_crash_keep_same_attempt(self):
        """Catches consuming a runtime opportunity without spawning the collector."""
        config = self.seed_source_less_exhausted()
        with mock.patch.object(cycle, "run_child_bounded") as child:
            for _ in range(2):
                result = cycle.run_cycle({**config, "total_child_budget_seconds": 30},
                                         enable_sheet_write=True, today=dt.date(2026, 9, 12))
                self.assertEqual("total_child_budget_exhausted", result["reason"])
        child.assert_not_called()
        pending = self.state()["slices"]["2026-09-07"]
        self.assertEqual(3, pending["source_attempt"]["ordinal"])
        self.assertEqual("started", pending["source_attempt"]["status"])
        real_persist = cycle._persist_state

        def interrupt(path, state, since, record):
            real_persist(path, state, since, record)
            if "runner_attempt" in record:
                raise RuntimeError("before spawn")

        with mock.patch.object(cycle, "_persist_state", side_effect=interrupt), \
             mock.patch.object(cycle, "run_child_bounded") as child:
            with self.assertRaisesRegex(RuntimeError, "before spawn"):
                cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))
        child.assert_not_called()
        with mock.patch.object(cycle, "run_child_bounded", return_value=ChildResult(None, "", "", True, 0.1)) as child:
            cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))
            result = cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))
        self.assertEqual(1, child.call_count)
        self.assertEqual("idle", result["status"])
        self.assertEqual(3, self.state()["slices"]["2026-09-07"]["source_attempt"]["ordinal"])

    def test_source_less_classification_debt_write_crash_finishes_without_respawn(self):
        """Catches a durable classification failure getting rerun before its journal finishes."""
        config = self.seed_source_less_exhausted()
        real_write = source_coverage.write

        def interrupt(path, document):
            real_write(path, document)
            raise RuntimeError("after classification debt write")

        with mock.patch.object(cycle, "run_child_bounded", return_value=ChildResult(None, "", "", True, 0.1)) as child:
            with mock.patch.object(cycle.source_coverage, "write", side_effect=interrupt):
                with self.assertRaisesRegex(RuntimeError, "after classification debt write"):
                    cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))
            cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))
            result = cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))
        self.assertEqual(1, child.call_count)
        self.assertEqual("idle", result["status"])
        self.assertEqual("finished", self.state()["slices"]["2026-09-07"]["source_attempt"]["status"])
        self.assertEqual("exhausted", self.debts()[0].status)

    def test_source_less_classification_changed_runtime_after_debt_crash_runs_new_attempt(self):
        """Catches rebinding a completed old failure instead of using the new runtime pass."""
        config = self.seed_source_less_exhausted()
        real_write = source_coverage.write

        def interrupt(path, document):
            real_write(path, document)
            raise RuntimeError("after old runtime failure")

        with mock.patch.object(cycle, "run_child_bounded", return_value=ChildResult(None, "", "", True, 0.1)) as child:
            with mock.patch.object(cycle.source_coverage, "write", side_effect=interrupt):
                with self.assertRaisesRegex(RuntimeError, "after old runtime failure"):
                    cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))
            changed = {**config, "_runtime_identity": {"git_sha": "changed-after-crash"}}
            cycle.run_cycle(changed, enable_sheet_write=True, today=dt.date(2026, 9, 12))
            result = cycle.run_cycle(changed, enable_sheet_write=True, today=dt.date(2026, 9, 12))
        self.assertEqual(2, child.call_count)
        self.assertEqual("idle", result["status"])
        self.assertEqual(4, self.state()["slices"]["2026-09-07"]["source_attempt"]["ordinal"])

    def test_source_less_classification_invalid_runner_metadata_fails_closed(self):
        """Catches malformed metadata granting a fresh same-runtime classification pass."""
        config = self.seed_source_less_exhausted()
        state = self.state()
        store = source_coverage.SourceDebtStore.from_document(source_coverage.read(self.state_dir / "source-coverage.json"))
        for metadata in (None, {}, {"runtime_identity_digest": "bad", "source_attempt_ordinal": 2, "status": "finished"},
                         {"runtime_identity_digest": "sha256:" + "a" * 64, "source_attempt_ordinal": True, "status": "finished"},
                         {"runtime_identity_digest": "sha256:" + "a" * 64, "source_attempt_ordinal": 1, "status": "finished"},
                         {"runtime_identity_digest": "sha256:" + "a" * 64, "source_attempt_ordinal": 2, "status": "invalid"},
                         {"runtime_identity_digest": "sha256:" + "a" * 64, "source_attempt_ordinal": 2, "status": "pending"}):
            with self.subTest(metadata=metadata):
                state["slices"]["2026-09-07"]["runner_attempt"] = metadata
                with self.assertRaisesRegex(cycle.CycleError, "runner attempt"):
                    cycle._select_work(config, state, store, today=dt.date(2026, 9, 12))

    def test_source_less_classification_source_state_crash_resumes_exact_promotion(self):
        """Catches a verified partial source stranded before its exact debts are durable."""
        config = self.seed_source_less_exhausted()
        real_persist = cycle._persist_state

        def interrupt(path, state, since, record):
            real_persist(path, state, since, record)
            if record.get("status") == "source_verified":
                raise RuntimeError("after classification source state")

        commands = []
        with mock.patch.object(cycle, "run_child_bounded", side_effect=self.child_with_first_gap(commands)):
            with mock.patch.object(cycle, "_persist_state", side_effect=interrupt):
                with self.assertRaisesRegex(RuntimeError, "after classification source state"):
                    cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))
            result = cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))
        self.assertEqual("recovery_blocked", result["status"])
        self.assertEqual(1, len(commands))
        self.assertEqual(["peer/macbook"], [item.interval.source for item in self.debts()])
        self.assertEqual("finished", self.state()["slices"]["2026-09-07"]["runner_attempt"]["status"])

    def test_source_less_classification_does_not_duplicate_routine_interval(self):
        """Catches selecting one recovery interval twice when the frontier still overlaps."""
        config = self.seed_source_less_exhausted()
        state = self.state()
        state["scheduled_through"] = "2026-09-07"
        store = source_coverage.SourceDebtStore.from_document(source_coverage.read(self.state_dir / "source-coverage.json"))
        selected = cycle._select_work(
            {**config, "max_slices": 2}, state, store, today=dt.date(2026, 9, 12),
        )
        self.assertEqual([("2026-09-07", "generic_classification")], [
            (item[0], item[2]) for item in selected
        ])

    def test_source_less_classification_invalid_stage_fails_closed_once(self):
        """Catches invalid returned evidence leaving the runtime pass infinitely pending."""
        config = self.seed_source_less_exhausted()

        def invalid_source(_command, **_kwargs):
            result = make_run(self.root, "invalid-classification", replay=False,
                              since=dt.date(2026, 9, 7), until=dt.date(2026, 9, 9))
            document = json.loads(result.read_text())
            document["run_id"] = "wrong-run"
            write_json(result, document)
            return ChildResult(0, str(result) + "\n", "", False, 0.1)

        with mock.patch.object(cycle, "run_child_bounded", side_effect=invalid_source) as child:
            with self.assertRaises(cycle.CycleError):
                cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))
            result = cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))
        self.assertEqual(1, child.call_count)
        self.assertEqual("idle", result["status"])
        self.assertEqual("exhausted", self.debts()[0].status)
        self.assertNotIn("source", self.state()["slices"]["2026-09-07"])

    def test_exact_debt_preserves_verified_opaque_collector_compatibility(self):
        """Catches replacing the parent backlog lineage with a guessed constant."""
        commands: list[list[str]] = []

        def child(command, **_kwargs):
            commands.append(list(command))
            coverage = {
                "status": "incomplete",
                "sources": {"sessions/macbook": {"status": "unavailable"}},
                "incomplete_sources": ["sessions/macbook"],
            }
            path = make_run(
                self.root, "source-2026-09-07", replay=False, coverage=coverage,
                since=dt.date(2026, 9, 7), until=dt.date(2026, 9, 9),
                compatibility_version="collector-backlog/v1:opaque-fixture-lineage",
            )
            return ChildResult(0, str(path) + "\n", "", False, 0.1)

        with mock.patch.object(cycle, "run_child_bounded", side_effect=child):
            cycle.run_cycle(
                {**self.config, "max_slices": 1}, enable_sheet_write=True,
                today=dt.date(2026, 9, 12),
            )

        self.assertEqual(
            "collector-backlog/v1:opaque-fixture-lineage",
            self.debts()[0].interval.compatibility_version,
        )

    def test_deleted_backlog_race_never_recreates_provenance(self):
        """Catches a vanished verified backlog being recreated during validation."""
        coverage = {
            "status": "incomplete",
            "sources": {"sessions/macbook": {"status": "unavailable"}},
            "incomplete_sources": ["sessions/macbook"],
        }
        checkpoint_root = self.state_dir / "collector-checkpoints"
        real_open = collector_slices.BacklogStore.open
        real_read_existing = getattr(
            collector_slices.BacklogStore, "read_existing", None
        )
        race = {"armed": False}

        def disappear_then_open(identity, slices):
            if not race["armed"]:
                return real_open(
                    collector_slices.BacklogStore(checkpoint_root), identity, slices
                )
            shutil.rmtree(checkpoint_root)
            return real_open(
                collector_slices.BacklogStore(checkpoint_root), identity, slices
            )

        def disappear_then_read(identity, slices):
            if not race["armed"]:
                if real_read_existing is None:
                    raise collector_slices.BacklogError("read-existing API is missing")
                return real_read_existing(
                    collector_slices.BacklogStore(checkpoint_root), identity, slices
                )
            shutil.rmtree(checkpoint_root)
            if real_read_existing is None:
                raise collector_slices.BacklogError("read-existing API is missing")
            return real_read_existing(
                collector_slices.BacklogStore(checkpoint_root), identity, slices
            )

        def child(_command, **_kwargs):
            result_path = make_run(
                self.root, "source-2026-09-07", replay=False, coverage=coverage,
                since=dt.date(2026, 9, 7), until=dt.date(2026, 9, 9),
            )
            race["armed"] = True
            return ChildResult(0, str(result_path) + "\n", "", False, 0.1)

        with mock.patch.object(cycle, "run_child_bounded", side_effect=child), \
            mock.patch.object(
                collector_slices.BacklogStore, "open", side_effect=disappear_then_open
            ), mock.patch.object(
                collector_slices.BacklogStore, "read_existing", create=True,
                side_effect=disappear_then_read,
            ), self.assertRaisesRegex(cycle.CycleError, "backlog"):
            cycle.run_cycle(
                {**self.config, "max_slices": 1}, enable_sheet_write=True,
                today=dt.date(2026, 9, 12),
            )

        self.assertFalse(checkpoint_root.exists())

    def test_tampered_finalization_lineage_fails_closed(self):
        """Catches accepting opaque lineage that is not bound to the completion slice."""
        commands: list[list[str]] = []

        def child(command, **_kwargs):
            commands.append(list(command))
            coverage = {
                "status": "incomplete",
                "sources": {"sessions/macbook": {"status": "unavailable"}},
                "incomplete_sources": ["sessions/macbook"],
            }
            path = make_run(
                self.root, "source-2026-09-07", replay=False, coverage=coverage,
                since=dt.date(2026, 9, 7), until=dt.date(2026, 9, 9),
            )
            finalization = json.loads(
                (path.parent / "slice-finalization.json").read_text(encoding="utf-8")
            )
            finalization["slice_id"] = "sha256:" + "0" * 64
            write_json(path.parent / "slice-finalization.json", finalization)
            return ChildResult(0, str(path) + "\n", "", False, 0.1)

        with mock.patch.object(cycle, "run_child_bounded", side_effect=child), self.assertRaisesRegex(
            cycle.CycleError, "finalization"
        ):
            cycle.run_cycle(
                {**self.config, "max_slices": 1}, enable_sheet_write=True,
                today=dt.date(2026, 9, 12),
            )

    def test_compatibility_only_finalization_tamper_fails_closed(self):
        """Catches trusting an opaque compatibility value absent its sealed backlog."""
        commands: list[list[str]] = []

        def child(command, **_kwargs):
            commands.append(list(command))
            coverage = {
                "status": "incomplete",
                "sources": {"sessions/macbook": {"status": "unavailable"}},
                "incomplete_sources": ["sessions/macbook"],
            }
            path = make_run(
                self.root, "source-2026-09-07", replay=False, coverage=coverage,
                since=dt.date(2026, 9, 7), until=dt.date(2026, 9, 9),
            )
            finalization = json.loads(
                (path.parent / "slice-finalization.json").read_text(encoding="utf-8")
            )
            finalization["backlog_identity"]["compatibility_version"] = "attacker-lineage"
            write_json(path.parent / "slice-finalization.json", finalization)
            return ChildResult(0, str(path) + "\n", "", False, 0.1)

        with mock.patch.object(cycle, "run_child_bounded", side_effect=child), self.assertRaisesRegex(
            cycle.CycleError, "backlog"
        ):
            cycle.run_cycle(
                {**self.config, "max_slices": 1}, enable_sheet_write=True,
                today=dt.date(2026, 9, 12),
            )

    def test_partial_generic_interval_fails_closed_before_child(self):
        """Catches truncating partial UTC endpoints into a different local-date slice."""
        store = source_coverage.SourceDebtStore()
        interval = source_coverage.SourceInterval(
            source="runner/unclassified",
            since_utc="2026-09-07T10:00:00Z",
            until_utc="2026-09-08T10:00:00Z",
            slice_id="partial-interval",
            compatibility_version="runner-unclassified/v1",
        )
        store.record_failure(
            interval, failure_class="child_timeout", retryable=True,
            resume_state_digest="sha256:partial", attempted_at="2026-09-08T10:00:00Z",
        )
        state = {
            "schema_version": cycle.SCHEMA_VERSION,
            "completed_through": None,
            "scheduled_through": "2026-09-09",
            "slices": {},
        }

        with self.assertRaisesRegex(cycle.CycleError, "local midnight"):
            cycle._select_work(self.config, state, store, today=dt.date(2026, 9, 12))

    def test_future_generic_and_delivery_work_are_not_selected(self):
        """Catches recovery bypassing the closed-day boundary used by routine work."""
        store = source_coverage.SourceDebtStore()
        interval = source_coverage.SourceInterval(
            source="runner/unclassified",
            since_utc="2026-09-10T21:00:00Z",
            until_utc="2026-09-12T21:00:00Z",
            slice_id="future-interval",
            compatibility_version="runner-unclassified/v1",
        )
        store.record_failure(
            interval, failure_class="child_timeout", retryable=True,
            resume_state_digest="sha256:future", attempted_at="2026-09-11T10:00:00Z",
        )
        state = {
            "schema_version": cycle.SCHEMA_VERSION,
            "completed_through": None,
            "scheduled_through": "2026-09-11",
            "slices": {
                "2026-09-11": {
                    "until": "2026-09-13", "status": "failed",
                    "source": {"coverage": {"status": "complete", "incomplete_sources": []}},
                }
            },
        }

        selected = cycle._select_work(
            self.config, state, store, today=dt.date(2026, 9, 12)
        )

        self.assertEqual([], selected)

    def test_adjacent_generic_debts_use_exact_identity_and_bounded_order(self):
        """Catches date-only matching collapsing adjacent interval obligations."""
        store = source_coverage.SourceDebtStore()
        for since, until, suffix in (
            ("2026-09-06T21:00:00Z", "2026-09-08T21:00:00Z", "a"),
            ("2026-09-08T21:00:00Z", "2026-09-10T21:00:00Z", "b"),
        ):
            interval = source_coverage.SourceInterval(
                source="runner/unclassified", since_utc=since, until_utc=until,
                slice_id=f"slice-{suffix}", compatibility_version="runner-unclassified/v1",
            )
            store.record_failure(
                interval, failure_class="child_timeout", retryable=True,
                resume_state_digest=f"sha256:{suffix}", attempted_at="2026-09-11T10:00:00Z",
            )
        state = {
            "schema_version": cycle.SCHEMA_VERSION,
            "completed_through": None,
            "scheduled_through": "2026-09-11",
            "slices": {},
        }

        selected = cycle._select_work(
            self.config, state, store, today=dt.date(2026, 9, 12)
        )

        self.assertEqual(
            [("2026-09-07", "2026-09-09"), ("2026-09-09", "2026-09-11")],
            [(since, until) for since, until, _kind, _debt in selected],
        )

    def test_max_one_cached_delivery_precedes_new_generic_inference(self):
        """A failed publication must not strand passing source/replay behind new inference."""
        config = {**self.config, "recovery_since": "2026-09-15",
                  "catchup_until": "2026-09-19", "max_slices": 1}
        commands = []
        complete = self.child_complete(commands)

        def fail_publication(command, **kwargs):
            if "clockify_sheet_publish.py" in command[1]:
                return ChildResult(1, "", "publication failed", False, 6.0)
            return complete(command, **kwargs)

        with mock.patch.object(cycle, "run_child_bounded", side_effect=fail_publication):
            result = cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 20))
        self.assertEqual("failed", result["status"])
        state = self.state()
        cached = state["slices"]["2026-09-15"]
        self.assertEqual("complete", cached["source"]["coverage"]["status"])
        self.assertEqual([], cached["source"]["coverage"]["incomplete_sources"])
        self.assertIn("replay", cached)
        self.assertIsNone(state["completed_through"])
        self.assertNotIn("delivery_receipt", cached)
        store = source_coverage.SourceDebtStore()
        debt = source_coverage.SourceInterval(source="runner/unclassified",
            since_utc="2026-09-16T21:00:00Z", until_utc="2026-09-18T21:00:00Z",
            slice_id="next-inference", compatibility_version="runner-unclassified/v1")
        store.record_failure(debt, failure_class="child_nonzero", retryable=True,
            resume_state_digest="sha256:next", attempted_at="2026-09-19T00:00:00Z")
        before = json.loads(json.dumps(state))
        selected = cycle._select_work(config, state, store, today=dt.date(2026, 9, 20))
        self.assertEqual([("2026-09-15", "2026-09-17", "delivery", None)], selected)
        self.assertEqual(before, state)
        for variant in ("delivered", "delivered_with_exceptions", "incomplete", "invalid_coverage", "missing"):
            with self.subTest(variant=variant):
                altered = json.loads(json.dumps(state))
                record = altered["slices"]["2026-09-15"]
                if variant in {"delivered", "delivered_with_exceptions"}:
                    record["status"] = variant
                elif variant == "incomplete":
                    record["source"]["coverage"] = {"status": "incomplete", "incomplete_sources": ["fathom"]}
                elif variant == "invalid_coverage":
                    record["source"]["coverage"] = []
                else:
                    record["source"] = None
                selected = cycle._select_work(config, altered, store, today=dt.date(2026, 9, 20))
                self.assertEqual([("2026-09-17", "2026-09-19", "generic")], [row[:3] for row in selected])

    def test_later_delivery_receipt_crash_recovers_frontier_without_republishing(self):
        """Catches receipt recovery delivering a slice but stranding its routine frontier."""
        commands: list[list[str]] = []
        real_persist = cycle._persist_state
        interrupted = False

        def persist_then_interrupt(path, state, since, record):
            nonlocal interrupted
            if since == "2026-09-09" and record.get("status") == "delivered" and not interrupted:
                interrupted = True
                raise RuntimeError("after delivery receipt")
            return real_persist(path, state, since, record)

        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=self.child_with_first_gap(commands)
        ), mock.patch.object(cycle, "_persist_state", side_effect=persist_then_interrupt):
            with self.assertRaisesRegex(RuntimeError, "after delivery receipt"):
                cycle.run_cycle(
                    self.config, enable_sheet_write=True, today=dt.date(2026, 9, 12)
                )

        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=self.child_with_first_gap(commands)
        ):
            result = cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 12)
            )

        publishers = [command for command in commands if "clockify_sheet_publish.py" in command[1]]
        self.assertEqual(1, len(publishers))
        self.assertEqual("delivered", result["status"])
        self.assertEqual("2026-09-11", self.state()["scheduled_through"])
        self.assertIsNone(self.state()["completed_through"])

    def test_replay_result_state_crash_resumes_at_publisher_without_replay(self):
        """Catches a durable replay result being recomputed after a state-write crash."""
        config = {**self.config, "catchup_until": "2026-09-09", "max_slices": 1}
        commands: list[list[str]] = []
        complete = self.child_complete(commands)
        real_persist = cycle._persist_state
        interrupted = False

        def persist_replay_then_interrupt(path, state, since, record):
            nonlocal interrupted
            real_persist(path, state, since, record)
            if record.get("status") == "replay_verified" and not interrupted:
                interrupted = True
                raise RuntimeError("after replay result state")

        with mock.patch.object(cycle, "run_child_bounded", side_effect=complete), \
             mock.patch.object(
                 cycle, "_persist_state", side_effect=persist_replay_then_interrupt
             ):
            with self.assertRaisesRegex(RuntimeError, "after replay result state"):
                cycle.run_cycle(
                    config, enable_sheet_write=True, today=dt.date(2026, 9, 12)
                )

        before_restart = len(commands)
        with mock.patch.object(cycle, "run_child_bounded", side_effect=complete):
            result = cycle.run_cycle(
                config, enable_sheet_write=True, today=dt.date(2026, 9, 12)
            )
        restart_commands = commands[before_restart:]
        self.assertEqual("delivered", result["status"])
        self.assertEqual(0, sum("--since" in command for command in restart_commands))
        self.assertEqual(0, sum("--replay-from" in command for command in restart_commands))
        self.assertEqual(
            1, sum("clockify_sheet_publish.py" in command[1] for command in restart_commands)
        )

    def test_longer_closed_backlog_remains_bounded_across_cycles(self):
        """Catches a catch-up selector escaping max_slices or skipping its tail day."""
        config = {
            **self.config,
            "recovery_since": "2026-09-01",
            "catchup_until": "2026-09-06",
            "max_slices": 2,
        }
        state = {
            "schema_version": cycle.SCHEMA_VERSION,
            "completed_through": None,
            "scheduled_through": "2026-09-01",
            "slices": {},
        }
        store = source_coverage.SourceDebtStore()

        first = cycle._select_work(config, state, store, today=dt.date(2026, 9, 7))
        state["scheduled_through"] = "2026-09-05"
        second = cycle._select_work(config, state, store, today=dt.date(2026, 9, 7))

        self.assertEqual(
            [("2026-09-01", "2026-09-03"), ("2026-09-03", "2026-09-05")],
            [(since, until) for since, until, _kind, _debt in first],
        )
        self.assertEqual(
            [("2026-09-05", "2026-09-06")],
            [(since, until) for since, until, _kind, _debt in second],
        )

    def test_malformed_resumable_interval_fails_closed(self):
        """Catches silently skipping corrupt stored delivery-work boundaries."""
        state = {
            "schema_version": cycle.SCHEMA_VERSION,
            "completed_through": None,
            "scheduled_through": "2026-09-09",
            "slices": {
                "2026-09-07": {
                    "until": None, "status": "failed",
                    "source": {"coverage": {"status": "complete", "incomplete_sources": []}},
                }
            },
        }

        with self.assertRaisesRegex(cycle.CycleError, "stored slice until"):
            cycle._select_work(
                self.config, state, source_coverage.SourceDebtStore(),
                today=dt.date(2026, 9, 12),
            )

    def test_total_child_budget_config_is_positive_integer_independent_of_max_slices(self):
        config_path = self.root / "cycle.json"
        for value in (0, -1, True, 1.5, "60"):
            with self.subTest(value=value):
                write_json(config_path, {**self.config, "total_child_budget_seconds": value})
                with self.assertRaisesRegex(cycle.CycleError, "total_child_budget_seconds"):
                    cycle.load_config(config_path)
        write_json(
            config_path,
            {**self.config, "max_slices": 99, "total_child_budget_seconds": 61},
        )
        loaded = cycle.load_config(config_path)
        self.assertEqual(61, loaded["total_child_budget_seconds"])

    def test_max_one_exact_and_routine_selection_alternates_without_mutation(self):
        store = source_coverage.SourceDebtStore()
        exact = source_coverage.SourceInterval(
            source="sessions/macbook",
            since_utc="2026-09-06T21:00:00Z",
            until_utc="2026-09-08T21:00:00Z",
            slice_id="slice-exact",
            compatibility_version="fixture-lineage",
        )
        store.record_failure(
            exact, failure_class="peer_unavailable", retryable=True,
            resume_state_digest="sha256:exact", attempted_at="2026-09-09T00:00:00Z",
        )
        state = {
            "schema_version": cycle.SCHEMA_VERSION,
            "completed_through": None,
            "scheduled_through": "2026-09-09",
            "slices": {
                "2026-09-07": {
                    "until": "2026-09-09", "status": "recovery_blocked", "source": {},
                }
            },
        }
        before = json.loads(json.dumps(state))

        first = cycle._select_work(
            {**self.config, "max_slices": 1}, state, store,
            today=dt.date(2026, 9, 12),
        )
        self.assertEqual("routine", first[0][2])
        self.assertEqual(before, state)
        state["next_work_class"] = "exact"
        second = cycle._select_work(
            {**self.config, "max_slices": 1}, state, store,
            today=dt.date(2026, 9, 12),
        )
        self.assertEqual("exact", second[0][2])
        self.assertEqual(exact.debt_id, second[0][3].debt_id)
        self.assertNotIn("recovery_attempts", state["slices"]["2026-09-07"])

    def test_recovery_attempt_is_persisted_before_spawn_and_crash_reuses_identity(self):
        commands: list[list[str]] = []
        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=self.child_with_first_gap(commands)
        ):
            cycle.run_cycle(
                {**self.config, "max_slices": 1}, enable_sheet_write=True,
                today=dt.date(2026, 9, 12),
            )
        self.assertEqual("2026-09-09", self.state()["scheduled_through"])

        observed: list[tuple[str, int]] = []

        def crash(command, **_kwargs):
            state = self.state()
            debt = self.debts()[0]
            attempt = state["slices"]["2026-09-07"]["recovery_attempts"][debt.debt_id]
            observed.append((attempt["attempt_id"], attempt["attempt_ordinal"]))
            commands.append(list(command))
            return ChildResult(None, "", "suppressed", True, 1.0)

        for _ in range(2):
            state = self.state()
            state["next_work_class"] = "exact"
            write_json(self.state_dir / "review-cycle-state.json", state)
            with mock.patch.object(cycle, "run_child_bounded", side_effect=crash):
                cycle.run_cycle(
                    {**self.config, "max_slices": 1}, enable_sheet_write=True,
                    today=dt.date(2026, 9, 12),
                )

        self.assertEqual(2, len(observed))
        self.assertEqual(observed[0], observed[1])
        self.assertEqual(1, observed[0][1])
        recovery_command = commands[-1]
        self.assertEqual(
            ["--recover-source-debt-from", "--recover-source", "--recover-attempt-id"],
            [flag for flag in recovery_command if flag.startswith("--recover-")],
        )

    def test_recovery_budget_exhaustion_preserves_same_attempt_for_restart(self):
        """Catches a pre-spawn recovery budget stop losing its durable attempt."""
        config = {
            **self.config,
            "catchup_until": "2026-09-09",
            "max_slices": 1,
            "total_child_budget_seconds": 7200,
        }
        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=self.child_with_first_gap([])
        ):
            cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))

        state = self.state()
        state["next_work_class"] = "exact"
        write_json(self.state_dir / "review-cycle-state.json", state)
        calls = 0

        def unexpected_child(_command, **_kwargs):
            nonlocal calls
            calls += 1
            return ChildResult(0, "", "", False, 0.1)

        with mock.patch.object(cycle, "run_child_bounded", side_effect=unexpected_child):
            exhausted = cycle.run_cycle(
                {**config, "total_child_budget_seconds": 30},
                enable_sheet_write=True, today=dt.date(2026, 9, 12),
            )
        debt = self.debts()[0]
        attempt = self.state()["slices"]["2026-09-07"]["recovery_attempts"][debt.debt_id]
        self.assertEqual(0, calls)
        self.assertEqual("total_child_budget_exhausted", exhausted["reason"])
        self.assertEqual("started", attempt["phase"])

        observed: list[str] = []

        def timed_out(command, **_kwargs):
            observed.append(command[command.index("--recover-attempt-id") + 1])
            return ChildResult(None, "", "suppressed", True, 0.1)

        with mock.patch.object(cycle, "run_child_bounded", side_effect=timed_out):
            cycle.run_cycle(
                config, enable_sheet_write=True, today=dt.date(2026, 9, 12)
            )
        self.assertEqual([attempt["attempt_id"]], observed)

    def test_verified_terminal_incomplete_finishes_then_allocates_next_ordinal(self):
        commands: list[list[str]] = []
        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=self.child_with_first_gap(commands)
        ):
            cycle.run_cycle(
                {**self.config, "max_slices": 1}, enable_sheet_write=True,
                today=dt.date(2026, 9, 12),
            )
        parent = Path(self.state()["slices"]["2026-09-07"]["source"]["run_dir"])
        coverage = {
            "status": "incomplete",
            "sources": {"sessions/macbook": {"status": "unavailable"}},
            "incomplete_sources": ["sessions/macbook"],
        }

        observed: list[tuple[str, int]] = []
        def terminal(command, **_kwargs):
            debt = self.debts()[0]
            attempt = self.state()["slices"]["2026-09-07"]["recovery_attempts"][debt.debt_id]
            observed.append((attempt["attempt_id"], attempt["attempt_ordinal"]))
            with mock.patch.object(
                collector_slices.BacklogStore, "record_complete", return_value=None
            ):
                result_path = make_run(
                    self.root, f"recovery-{attempt['attempt_ordinal']}", replay=False,
                    coverage=coverage, since=dt.date(2026, 9, 7),
                    until=dt.date(2026, 9, 9), snapshots_from=parent,
                )
            return ChildResult(0, str(result_path) + "\n", "", False, 0.1)

        def verified(run_dir, **_kwargs):
            bundle = cycle.collector_receipts.load_completion_bundle(
                Path(run_dir) / "completion-bundle.json", run_dir=Path(run_dir)
            )
            return bundle, "incomplete"

        real_persist = cycle._persist_state
        interrupted = False

        def persist_verified_then_interrupt(path, state, since, record):
            nonlocal interrupted
            real_persist(path, state, since, record)
            attempts = record.get("recovery_attempts", {})
            if (
                not interrupted
                and any(
                    item.get("phase") == "verified_incomplete"
                    for item in attempts.values()
                    if isinstance(item, dict)
                )
            ):
                interrupted = True
                raise RuntimeError("after verified recovery result")

        for invocation in range(2):
            state = self.state()
            state["next_work_class"] = "exact"
            write_json(self.state_dir / "review-cycle-state.json", state)
            with mock.patch.object(cycle, "run_child_bounded", side_effect=terminal), \
                 mock.patch.object(
                     cycle.clockify_review_run,
                     "verify_source_debt_recovery_completion",
                     side_effect=verified,
                 ), mock.patch.object(
                     cycle, "_persist_state",
                     side_effect=(persist_verified_then_interrupt if invocation == 0 else real_persist),
                 ):
                if invocation == 0:
                    with self.assertRaisesRegex(RuntimeError, "after verified recovery result"):
                        cycle.run_cycle(
                            {**self.config, "max_slices": 1, "catchup_until": "2026-09-09"}, enable_sheet_write=True,
                            today=dt.date(2026, 9, 12),
                        )
                else:
                    cycle.run_cycle(
                        {**self.config, "max_slices": 1, "catchup_until": "2026-09-09"}, enable_sheet_write=True,
                        today=dt.date(2026, 9, 12),
                    )

        self.assertEqual([1, 2], [ordinal for _identity, ordinal in observed])
        attempt = self.state()["slices"]["2026-09-07"]["recovery_attempts"][self.debts()[0].debt_id]
        self.assertEqual("finished_incomplete", attempt["phase"])
        self.assertEqual("exhausted", self.debts()[0].status)

    def test_recovery_incomplete_debt_write_crash_finishes_before_next_ordinal(self):
        """Catches replaying a terminal-incomplete debt event after its durable write."""
        config = {**self.config, "catchup_until": "2026-09-09", "max_slices": 1}
        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=self.child_with_first_gap([])
        ):
            cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))

        debt = self.debts()[0]
        parent = Path(self.state()["slices"]["2026-09-07"]["source"]["run_dir"])
        coverage = {
            "status": "incomplete",
            "sources": {
                "sessions/macbook": {"status": "unavailable"},
                "repositories/macbook": {"status": "complete"},
            },
            "incomplete_sources": ["sessions/macbook"],
        }
        spawned_attempt_ids: list[str] = []

        def terminal(command, **_kwargs):
            attempt_id = command[command.index("--recover-attempt-id") + 1]
            spawned_attempt_ids.append(attempt_id)
            with mock.patch.object(
                collector_slices.BacklogStore, "record_complete", return_value=None
            ):
                result_path = make_run(
                    self.root, "recovery-debt-write-crash", replay=False,
                    coverage=coverage, since=dt.date(2026, 9, 7),
                    until=dt.date(2026, 9, 9), snapshots_from=parent,
                )
            return ChildResult(0, str(result_path) + "\n", "", False, 0.1)

        def verified(run_dir, **_kwargs):
            bundle = cycle.collector_receipts.load_completion_bundle(
                Path(run_dir) / "completion-bundle.json", run_dir=Path(run_dir)
            )
            return bundle, "incomplete"

        real_debt_write = source_coverage.write
        interrupted = False

        def write_recovery_failure_then_interrupt(path, document):
            nonlocal interrupted
            real_debt_write(path, document)
            if (
                not interrupted
                and any(
                    event.get("event") == "failure"
                    and event.get("failure_class") == "recovery_incomplete"
                    for event in document["events"]
                )
            ):
                interrupted = True
                raise RuntimeError("after recovery-incomplete debt write")

        state = self.state()
        state["next_work_class"] = "exact"
        write_json(self.state_dir / "review-cycle-state.json", state)
        with mock.patch.object(cycle, "run_child_bounded", side_effect=terminal), \
             mock.patch.object(
                 cycle.clockify_review_run,
                 "verify_source_debt_recovery_completion",
                 side_effect=verified,
             ), mock.patch.object(
                 cycle.source_coverage,
                 "write",
                 side_effect=write_recovery_failure_then_interrupt,
             ):
            with self.assertRaisesRegex(
                RuntimeError, "after recovery-incomplete debt write"
            ):
                cycle.run_cycle(
                    config, enable_sheet_write=True, today=dt.date(2026, 9, 12)
                )

        crashed_state = self.state()
        first_attempt = crashed_state["slices"]["2026-09-07"]["recovery_attempts"][debt.debt_id]
        self.assertEqual("verified_incomplete", first_attempt["phase"])
        crashed_document = source_coverage.read(self.state_dir / "source-coverage.json")
        recovery_failures = [
            event for event in crashed_document["events"]
            if event.get("failure_class") == "recovery_incomplete"
        ]
        self.assertEqual(1, len(recovery_failures))
        resume_digest = recovery_failures[0]["resume_state_digest"]

        restarted_state = cycle._state(
            self.state_dir / "review-cycle-state.json",
            recovery_since=config["recovery_since"],
        )
        restarted_store, _warnings = cycle._source_debt(
            self.state_dir / "source-coverage.json"
        )
        with mock.patch.object(
            cycle.clockify_review_run,
            "verify_source_debt_recovery_completion",
            side_effect=verified,
        ):
            cycle._reconcile_verified_attempts(
                config, restarted_state,
                self.state_dir / "review-cycle-state.json", restarted_store,
                self.state_dir / "source-coverage.json",
            )

        reconciled = self.state()["slices"]["2026-09-07"]["recovery_attempts"][debt.debt_id]
        self.assertEqual("finished_incomplete", reconciled["phase"])
        self.assertEqual([first_attempt["attempt_id"]], spawned_attempt_ids)
        reconciled_document = source_coverage.read(self.state_dir / "source-coverage.json")
        self.assertEqual(
            1,
            sum(
                event.get("event") == "failure"
                and event.get("resume_state_digest") == resume_digest
                for event in reconciled_document["events"]
            ),
        )

        later_attempts: list[str] = []

        def later_timeout(command, **_kwargs):
            later_attempts.append(command[command.index("--recover-attempt-id") + 1])
            return ChildResult(None, "", "suppressed", True, 0.1)

        state = self.state()
        state["next_work_class"] = "exact"
        write_json(self.state_dir / "review-cycle-state.json", state)
        with mock.patch.object(cycle, "run_child_bounded", side_effect=later_timeout), \
             mock.patch.object(cycle, "_probe_source_health", return_value="online"):
            cycle.run_cycle(
                config, enable_sheet_write=True, today=dt.date(2026, 9, 12)
            )

        second_attempt = self.state()["slices"]["2026-09-07"]["recovery_attempts"][debt.debt_id]
        self.assertEqual(2, second_attempt["attempt_ordinal"])
        self.assertEqual([second_attempt["attempt_id"]], later_attempts)
        self.assertNotEqual(first_attempt["attempt_id"], second_attempt["attempt_id"])
        after_transition = source_coverage.read(
            self.state_dir / "source-coverage.json"
        )
        self.assertEqual(
            len(reconciled_document["events"]),
            len(after_transition["events"]),
        )
        self.assertEqual(
            "recovery_incomplete",
            after_transition["events"][-1]["failure_class"],
        )

    def test_complete_recovery_resolves_only_selected_source_when_peer_debt_remains(self):
        commands: list[list[str]] = []
        coverage = {
            "status": "incomplete",
            "sources": {
                "repositories/desktop": {"status": "unavailable"},
                "sessions/macbook": {"status": "unavailable"},
            },
            "incomplete_sources": ["repositories/desktop", "sessions/macbook"],
        }
        def initial(command, **_kwargs):
            commands.append(list(command))
            result_path = make_run(
                self.root, "source-2026-09-07", replay=False, coverage=coverage,
                since=dt.date(2026, 9, 7), until=dt.date(2026, 9, 9),
            )
            return ChildResult(0, str(result_path) + "\n", "", False, 0.1)
        with mock.patch.object(cycle, "run_child_bounded", side_effect=initial):
            cycle.run_cycle(
                {**self.config, "max_slices": 1}, enable_sheet_write=True,
                today=dt.date(2026, 9, 12),
            )
        parent = Path(self.state()["slices"]["2026-09-07"]["source"]["run_dir"])
        selected_source = sorted(item.interval.source for item in self.debts())[0]
        selected_machine = selected_source.split("/", 1)[1]
        recovered_coverage = {
            "status": "incomplete",
            "sources": {
                f"sessions/{selected_machine}": {"status": "complete"},
                f"repositories/{selected_machine}": {"status": "complete"},
                "sessions/macbook": {"status": "unavailable"},
                "repositories/macbook": {"status": "complete"},
            },
            "incomplete_sources": ["sessions/macbook"],
        }
        def recovery_child(command, **_kwargs):
            self.assertEqual(selected_source, command[command.index("--recover-source") + 1])
            with mock.patch.object(
                collector_slices.BacklogStore, "record_complete", return_value=None
            ):
                result_path = make_run(
                    self.root, "recovery-complete-one", replay=False,
                    coverage=recovered_coverage, since=dt.date(2026, 9, 7),
                    until=dt.date(2026, 9, 9), snapshots_from=parent,
                )
            return ChildResult(0, str(result_path) + "\n", "", False, 0.1)
        def verified(run_dir, **_kwargs):
            bundle = cycle.collector_receipts.load_completion_bundle(
                Path(run_dir) / "completion-bundle.json", run_dir=Path(run_dir)
            )
            return bundle, "complete"
        state = self.state()
        state["next_work_class"] = "exact"
        write_json(self.state_dir / "review-cycle-state.json", state)
        with mock.patch.object(cycle, "run_child_bounded", side_effect=recovery_child), \
             mock.patch.object(
                 cycle.clockify_review_run, "verify_source_debt_recovery_completion",
                 side_effect=verified,
             ):
            cycle.run_cycle(
                {**self.config, "max_slices": 1}, enable_sheet_write=True,
                today=dt.date(2026, 9, 12),
            )
        items = {
            source_coverage.SourceDebtStore.from_document(
                source_coverage.read(self.state_dir / "source-coverage.json")
            ).get(item.debt_id).interval.source:
            source_coverage.SourceDebtStore.from_document(
                source_coverage.read(self.state_dir / "source-coverage.json")
            ).get(item.debt_id).status
            for item in self.debts()
        }
        self.assertNotIn(selected_source, [item.interval.source for item in self.debts()])
        self.assertEqual("active", items["peer/macbook"])
        self.assertEqual("recovery_blocked", self.state()["slices"]["2026-09-07"]["status"])

    def test_complete_recovery_reopens_incomplete_peer_before_promoted_state_write(self):
        """Catches a crash stranding a canonically incomplete resolved peer."""
        coverage = {
            "status": "incomplete",
            "sources": {
                "repositories/desktop": {"status": "unavailable"},
                "sessions/macbook": {"status": "unavailable"},
            },
            "incomplete_sources": ["repositories/desktop", "sessions/macbook"],
        }

        def initial(_command, **_kwargs):
            result_path = make_run(
                self.root, "source-2026-09-07", replay=False, coverage=coverage,
                since=dt.date(2026, 9, 7), until=dt.date(2026, 9, 9),
            )
            return ChildResult(0, str(result_path) + "\n", "", False, 0.1)

        config = {**self.config, "catchup_until": "2026-09-09", "max_slices": 1}
        with mock.patch.object(cycle, "run_child_bounded", side_effect=initial):
            cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))
        parent = Path(self.state()["slices"]["2026-09-07"]["source"]["run_dir"])

        def verified(run_dir, **_kwargs):
            bundle = cycle.collector_receipts.load_completion_bundle(
                Path(run_dir) / "completion-bundle.json", run_dir=Path(run_dir)
            )
            return bundle, "complete"

        def recover_with(remaining_source: str, run_name: str):
            def child(command, **_kwargs):
                selected = command[command.index("--recover-source") + 1]
                remaining_machine = (
                    remaining_source.split("/", 1)[1] if remaining_source else ""
                )
                incomplete = (
                    [f"sessions/{remaining_machine}"] if remaining_machine else []
                )
                recovered_coverage = {
                    "status": "incomplete" if incomplete else "complete",
                    "sources": {
                        "repositories/desktop": {
                            "status": "complete"
                        },
                        "sessions/desktop": {
                            "status": "unavailable" if remaining_machine == "desktop" else "complete"
                        },
                        "sessions/macbook": {
                            "status": "unavailable" if remaining_machine == "macbook" else "complete"
                        },
                        "repositories/macbook": {
                            "status": "complete"
                        },
                    },
                    "incomplete_sources": incomplete,
                }
                with mock.patch.object(
                    collector_slices.BacklogStore, "record_complete", return_value=None
                ):
                    result_path = make_run(
                        self.root, run_name, replay=False, coverage=recovered_coverage,
                        since=dt.date(2026, 9, 7), until=dt.date(2026, 9, 9),
                        snapshots_from=parent,
                    )
                self.assertNotEqual(remaining_source, selected)
                return ChildResult(0, str(result_path) + "\n", "", False, 0.1)
            return child

        first_source = sorted(item.interval.source for item in self.debts())[0]
        peer_source = next(
            item.interval.source for item in self.debts()
            if item.interval.source != first_source
        )
        state = self.state()
        state["next_work_class"] = "exact"
        write_json(self.state_dir / "review-cycle-state.json", state)
        with mock.patch.object(
            cycle, "run_child_bounded",
            side_effect=recover_with(peer_source, "recovery-first-source"),
        ), mock.patch.object(
            cycle.clockify_review_run,
            "verify_source_debt_recovery_completion",
            side_effect=verified,
        ):
            cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 12))

        state = self.state()
        state["next_work_class"] = "exact"
        write_json(self.state_dir / "review-cycle-state.json", state)
        real_debt_write = source_coverage.write
        interrupted = False

        def write_reopened_peer_then_interrupt(path, document):
            nonlocal interrupted
            real_debt_write(path, document)
            active_sources = {
                item.interval.source
                for item in source_coverage.SourceDebtStore.from_document(document).active()
            }
            if first_source in active_sources and not interrupted:
                interrupted = True
                raise RuntimeError("after reopened peer debt write")

        with mock.patch.object(
            cycle, "run_child_bounded",
            side_effect=recover_with(first_source, "recovery-peer-source"),
        ), mock.patch.object(
            cycle.clockify_review_run,
            "verify_source_debt_recovery_completion",
            side_effect=verified,
        ), mock.patch.object(
            cycle.source_coverage, "write", side_effect=write_reopened_peer_then_interrupt,
        ):
            with self.assertRaisesRegex(RuntimeError, "after reopened peer debt write"):
                cycle.run_cycle(
                    config, enable_sheet_write=True, today=dt.date(2026, 9, 12)
                )

        active = self.debts()
        self.assertEqual([first_source], [item.interval.source for item in active])
        selected = cycle._select_work(
            config, self.state(),
            source_coverage.SourceDebtStore.from_document(
                source_coverage.read(self.state_dir / "source-coverage.json")
            ),
            today=dt.date(2026, 9, 12),
        )
        self.assertEqual("exact", selected[0][2])
        self.assertEqual(first_source, selected[0][3].interval.source)

    def test_budget_caps_later_child_and_blocks_when_grace_consumes_remainder(self):
        timeouts: list[int] = []
        def child(_command, *, timeout, **_kwargs):
            timeouts.append(timeout.total_seconds)
            return ChildResult(0, "", "", False, 40.0)
        budget = [100.0]
        with mock.patch.object(cycle, "run_child_bounded", side_effect=child):
            cycle._run_budgeted_child([], root=self.root, budget=budget, cap=2700, grace=30)
            cycle._run_budgeted_child([], root=self.root, budget=budget, cap=2700, grace=30)
            with self.assertRaisesRegex(RuntimeError, "total_child_budget_exhausted"):
                cycle._run_budgeted_child([], root=self.root, budget=budget, cap=2700, grace=30)
        self.assertEqual([100, 60], timeouts)

    def test_pre_spawn_budget_exhaustion_keeps_routine_frontier_selectable(self):
        """Catches an unspawned routine interval being skipped as scheduled work."""
        config = {
            **self.config,
            "catchup_until": "2026-09-09",
            "max_slices": 1,
            "total_child_budget_seconds": 30,
        }
        calls = 0

        def child(_command, **_kwargs):
            nonlocal calls
            calls += 1
            return ChildResult(0, "", "", False, 0.1)

        with mock.patch.object(cycle, "run_child_bounded", side_effect=child):
            first = cycle.run_cycle(
                config, enable_sheet_write=True, today=dt.date(2026, 9, 12)
            )
            second = cycle.run_cycle(
                config, enable_sheet_write=True, today=dt.date(2026, 9, 12)
            )

        self.assertEqual(0, calls)
        self.assertEqual("total_child_budget_exhausted", first["reason"])
        self.assertEqual("total_child_budget_exhausted", second.get("reason"))
        self.assertEqual("2026-09-07", self.state()["scheduled_through"])
        self.assertEqual("started", self.state()["slices"]["2026-09-07"]["source_attempt"]["status"])

    def test_later_routine_slot_stays_selectable_after_earlier_children_consume_budget(self):
        """Catches later unspawned work advancing after an earlier slice uses the budget."""
        config = {
            **self.config,
            "catchup_until": "2026-09-11",
            "max_slices": 2,
            "total_child_budget_seconds": 100,
        }
        first_commands: list[list[str]] = []
        complete = self.child_complete(first_commands)

        def consuming_child(command, **kwargs):
            result = complete(command, **kwargs)
            duration = 50.0 if "clockify_sheet_publish.py" in command[1] else 10.0
            return ChildResult(
                result.returncode, result.stdout, result.stderr,
                result.timed_out, duration,
            )

        with mock.patch.object(cycle, "run_child_bounded", side_effect=consuming_child):
            first = cycle.run_cycle(
                config, enable_sheet_write=True, today=dt.date(2026, 9, 12)
            )

        self.assertEqual("total_child_budget_exhausted", first["reason"])
        self.assertEqual("2026-09-09", self.state()["scheduled_through"])
        self.assertEqual(0, sum("2026-09-09" in command for command in first_commands))

        second_commands: list[list[str]] = []
        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=self.child_complete(second_commands)
        ):
            second = cycle.run_cycle(
                config, enable_sheet_write=True, today=dt.date(2026, 9, 12)
            )

        self.assertEqual("delivered", second["status"])
        self.assertEqual("2026-09-11", self.state()["scheduled_through"])
        self.assertEqual(1, sum("2026-09-09" in command for command in second_commands))

    def test_replay_budget_exhaustion_resumes_without_recollecting_source(self):
        """Catches a verified source becoming unselectable when replay cannot spawn."""
        config = {
            **self.config,
            "catchup_until": "2026-09-09",
            "max_slices": 1,
            "total_child_budget_seconds": 100,
        }
        first_commands: list[list[str]] = []
        complete = self.child_complete(first_commands)

        def consume_before_replay(command, **kwargs):
            result = complete(command, **kwargs)
            return ChildResult(
                result.returncode, result.stdout, result.stderr,
                result.timed_out, 70.0,
            )

        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=consume_before_replay
        ):
            first = cycle.run_cycle(
                config, enable_sheet_write=True, today=dt.date(2026, 9, 12)
            )
        self.assertEqual("total_child_budget_exhausted", first["reason"])
        self.assertEqual(1, len(first_commands))

        second_commands: list[list[str]] = []
        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=self.child_complete(second_commands)
        ):
            second = cycle.run_cycle(
                config, enable_sheet_write=True, today=dt.date(2026, 9, 12)
            )

        self.assertEqual("delivered", second["status"])
        self.assertEqual(0, sum("--since" in command for command in second_commands))
        self.assertEqual(1, sum("--replay-from" in command for command in second_commands))

    def test_publisher_budget_exhaustion_resumes_without_replay(self):
        """Catches a verified replay becoming unselectable when publish cannot spawn."""
        config = {
            **self.config,
            "catchup_until": "2026-09-09",
            "max_slices": 1,
            "total_child_budget_seconds": 100,
        }
        first_commands: list[list[str]] = []
        complete = self.child_complete(first_commands)

        def consume_before_publish(command, **kwargs):
            result = complete(command, **kwargs)
            duration = 60.0 if "--replay-from" in command else 10.0
            return ChildResult(
                result.returncode, result.stdout, result.stderr,
                result.timed_out, duration,
            )

        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=consume_before_publish
        ):
            first = cycle.run_cycle(
                config, enable_sheet_write=True, today=dt.date(2026, 9, 12)
            )
        self.assertEqual("total_child_budget_exhausted", first["reason"])
        self.assertEqual(2, len(first_commands))

        second_commands: list[list[str]] = []
        with mock.patch.object(
            cycle, "run_child_bounded", side_effect=self.child_complete(second_commands)
        ):
            second = cycle.run_cycle(
                config, enable_sheet_write=True, today=dt.date(2026, 9, 12)
            )

        self.assertEqual("delivered", second["status"])
        self.assertEqual(0, sum("--since" in command for command in second_commands))
        self.assertEqual(0, sum("--replay-from" in command for command in second_commands))
        self.assertEqual(
            1, sum("clockify_sheet_publish.py" in command[1] for command in second_commands)
        )

    def test_max_two_reserves_one_exact_and_one_routine_slot(self):
        store = source_coverage.SourceDebtStore()
        debt = source_coverage.SourceInterval(
            source="sessions/macbook", since_utc="2026-09-06T21:00:00Z",
            until_utc="2026-09-08T21:00:00Z", slice_id="slice-exact",
            compatibility_version="fixture-lineage",
        )
        store.record_failure(
            debt, failure_class="offline", retryable=True,
            resume_state_digest="sha256:exact", attempted_at="2026-09-09T00:00:00Z",
        )
        selected = cycle._select_work(
            {**self.config, "max_slices": 2},
            {
                "schema_version": cycle.SCHEMA_VERSION, "completed_through": None,
                "scheduled_through": "2026-09-09", "next_work_class": "routine",
                "slices": {
                    "2026-09-07": {
                        "until": "2026-09-09", "status": "recovery_blocked", "source": {},
                    }
                },
            },
            store, today=dt.date(2026, 9, 12),
        )
        self.assertEqual(["exact", "routine"], [item[2] for item in selected])


if __name__ == "__main__":
    unittest.main()
