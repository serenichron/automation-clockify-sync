"""Plan-only cycle calls must leave durable review and debt state untouched."""
from __future__ import annotations

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import clockify_review_cycle as cycle
from scripts import source_coverage
from test_review_cycle_delivery import make_run, write_json


class PlanOnlyImmutabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state_dir = self.root / "state"
        (self.root / "cache").mkdir()
        for filename, content in (
            ("routing.json", {"workspace_id": "workspace-1", "member_id": "member-1"}),
            ("corrections.jsonl", {}),
            ("acceptance.jsonl", {}),
        ):
            write_json(self.root / filename, content)
        self.config = {
            "root": str(self.root), "state_dir": str(self.state_dir),
            "cache": str(self.root / "cache"),
            "routing": str(self.root / "routing.json"),
            "corrections": str(self.root / "corrections.jsonl"),
            "acceptance": str(self.root / "acceptance.jsonl"),
            "workspace_id": "workspace-1", "member_id": "member-1",
            "recovery_since": "2026-09-07", "timezone": "Europe/Bucharest",
            "spreadsheet_id": "sheet-1",
            "monthly_sheet_title_template": "{month_name} {year} portfolio review",
            "calendly_optional": True,
            "_runtime_identity": {"git_sha": "fixture-sha"},
        }
        self.state_path = self.state_dir / "review-cycle-state.json"
        self.debt_path = self.state_dir / "source-coverage.json"

    def state(self) -> dict:
        return {
            "schema_version": cycle.SCHEMA_VERSION,
            "completed_through": None,
            "scheduled_through": "2026-09-07",
            "next_work_class": "routine",
            "slices": {},
        }

    def test_plan_does_not_persist_legacy_runtime_migration(self) -> None:
        """Catches the pre-plan stage migration writing a validated state update."""
        manifest = cycle._ensure_period(
            self.config, self.state_dir, "2026-09-07", "2026-09-09",
            bind_inputs=True,
        )
        result_path = make_run(self.root, "source-run", replay=False)
        snapshots = cycle._expected_snapshot_digests(self.config, manifest)
        stage = cycle._validate_stage(
            self.config, result_path, "2026-09-07", "2026-09-09",
            replay=False, expected_snapshot_digests=snapshots,
        )
        stage.pop("runtime_identity_digest")
        state = self.state()
        state["slices"] = {"2026-09-07": {
            "until": "2026-09-09", "status": "incomplete",
            "period_manifest": str(manifest),
            "expected_snapshot_digests": snapshots,
            "source": stage,
        }}
        write_json(self.state_path, state)
        before = self.state_path.read_bytes()

        result = cycle.run_cycle(
            self.config, enable_sheet_write=False, today=dt.date(2026, 9, 7),
        )
        self.assertEqual("plan", result["status"])
        self.assertEqual(before, self.state_path.read_bytes())

        cycle.run_cycle(
            self.config, enable_sheet_write=True, today=dt.date(2026, 9, 7),
        )
        migrated = json.loads(self.state_path.read_text())
        self.assertNotEqual(before, self.state_path.read_bytes())
        self.assertIn(
            "runtime_identity_digest", migrated["slices"]["2026-09-07"]["source"],
        )

    def test_plan_does_not_persist_health_reactivation(self) -> None:
        """Catches pre-plan peer recovery advancing the debt ledger and epoch."""
        write_json(self.state_path, self.state())
        interval = source_coverage.SourceInterval(
            source="sessions/peer", since_utc="2026-09-06T21:00:00Z",
            until_utc="2026-09-08T21:00:00Z", slice_id="slice-fixture",
            compatibility_version="collector-slice-bundles/v1",
        )
        store = source_coverage.SourceDebtStore()
        debt = store.record_failure(
            interval, failure_class="source_unavailable", retryable=True,
            resume_state_digest="sha256:" + "a" * 64,
            attempted_at="2026-09-09T00:00:00Z",
        )
        store.exhaust(debt.debt_id, terminal_reason="retry_limit")
        source_coverage.write(self.debt_path, store.document())
        state_before = self.state_path.read_bytes()
        debt_before = self.debt_path.read_bytes()

        with mock.patch.object(cycle, "_probe_source_health", return_value="online"):
            result = cycle.run_cycle(
                self.config, enable_sheet_write=False, today=dt.date(2026, 9, 7),
            )
            self.assertEqual("plan", result["status"])
            self.assertEqual(state_before, self.state_path.read_bytes())
            self.assertEqual(debt_before, self.debt_path.read_bytes())
            cycle.run_cycle(
                self.config, enable_sheet_write=True, today=dt.date(2026, 9, 7),
            )

        self.assertNotEqual(state_before, self.state_path.read_bytes())
        self.assertNotEqual(debt_before, self.debt_path.read_bytes())
        self.assertEqual(
            "online:0",
            json.loads(self.state_path.read_text())["source_health_epochs"][debt.debt_id],
        )
        events = source_coverage.read(self.debt_path)["events"]
        self.assertEqual(3, len(events))


if __name__ == "__main__":
    unittest.main()
