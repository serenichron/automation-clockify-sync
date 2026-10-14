from __future__ import annotations

import datetime as dt
import importlib.util
import json
import copy
import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

from scripts import clockify_review_cycle as cycle
from scripts import clockify_sheet_publish as publisher
from scripts import evidence_ledger
from test_monthly_unresolved import MonthlyGateway


_SPEC = importlib.util.spec_from_file_location(
    "partial_publication_delivery_fixtures",
    Path(__file__).with_name("test_review_cycle_delivery.py"),
)
assert _SPEC is not None and _SPEC.loader is not None
_FIXTURES = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_FIXTURES)


class PartialPublicationTests(unittest.TestCase):
    def test_provenance_debt_publishes_partial_and_does_not_reschedule_inference_for_later_work(self):
        self._provenance_debt_scheduling(missing_peer=False)

    def test_provenance_debt_with_missing_peer_advances_scheduling_and_preserves_source_recovery(self):
        self._provenance_debt_scheduling(missing_peer=True)

    def _provenance_debt_scheduling(self, *, missing_peer):
        # Catches publication failure, false full completion, or replay loops hiding later periods.
        fixture = self.fixture()
        commands = []
        base_child = fixture.child_for_runs(commands, source_options={"proposals": [], "ambiguous": [
            {"id": "a-1", "exception_kind": "low_confidence"},
            {"id": "a-2", "exception_kind": "low_confidence"}]})
        gateway = None
        initial = None
        original_ledger = evidence_ledger.EvidenceLedger

        def ledger_with_missing_peer(*args, **kwargs):
            if len(args) < 2 and "source_inventory" not in kwargs:
                kwargs["source_inventory"] = self.coverage()["sources"]
            return original_ledger(*args, **kwargs)

        def child(command, **options):
            nonlocal gateway, initial
            if "clockify_sheet_publish.py" not in command[1]:
                if missing_peer:
                    with mock.patch.object(evidence_ledger, "EvidenceLedger", side_effect=ledger_with_missing_peer):
                        return base_child(command, **options)
                return base_child(command, **options)
            commands.append(list(command))
            source_dir = Path(command[command.index("--proposals") + 1]).parent
            rows = cycle.clockify_monthly_unresolved.project_rows(source_dir)
            old = list(rows[0])
            provenance = json.loads(old[11])
            provenance["source_run_id"] = "missing-original"
            old[11] = json.dumps(provenance)
            old[10] = "Keep this human decision"
            gateway = MonthlyGateway([cycle.clockify_monthly_unresolved.HEADER, old])
            initial = copy.deepcopy(gateway.rows)
            try:
                with mock.patch.object(publisher, "GwsSheetsGateway", return_value=gateway), contextlib.redirect_stdout(io.StringIO()):
                    code = publisher.main(command[2:])
            except publisher.PublicationError:
                return _FIXTURES.ChildResult(2, "", "", False, 0.1)
            path = Path(command[command.index("--result-output") + 1])
            return _FIXTURES.ChildResult(code, str(path) + "\n", "", False, 0.1)
        with mock.patch.object(cycle, "run_child_bounded", side_effect=child):
            outcome = cycle.run_cycle(fixture.config, enable_sheet_write=True, today=dt.date(2026, 9, 10))
        self.assertEqual("published_with_preparation_gaps", outcome["status"])
        self.assertEqual(initial, gateway.rows[:2])
        self.assertEqual(1, len(gateway.writes))
        state = json.loads((fixture.state_dir / "review-cycle-state.json").read_text())
        record = state["slices"]["2026-09-07"]
        self.assertFalse(record["exceptions_complete"])
        self.assertIsNone(state["completed_through"])
        self.assertEqual("2026-09-09", state["scheduled_through"])
        receipt = json.loads(Path(record["publication_receipt"]).read_text())
        self.assertEqual(cycle.PARTIAL_RECEIPT_SCHEMA_VERSION, receipt["schema_version"])
        self.assertEqual(record["retained_provenance_debt"], receipt["retained_provenance_debt"])
        cycle._validate_delivered_state(fixture.config, state)
        store, _ = cycle._source_debt(fixture.state_dir / "source-coverage.json")
        if missing_peer:
            self.assertEqual("incomplete", record["source_completeness"]["status"])
            self.assertIn("peer/macbook", {debt.interval.source for debt in store.active()})
        later_config = {**fixture.config, "catchup_until": "2026-09-11"}
        selected = cycle._select_work(later_config, state, store, today=dt.date(2026, 9, 12))
        self.assertEqual(("2026-09-09", "2026-09-11", "routine"), selected[0][:3])
        if missing_peer:
            recovery_commands = []

            def offline_peer(command, **_options):
                recovery_commands.append(list(command))
                return _FIXTURES.ChildResult(None, "", "peer unavailable", True, 0.1)

            with mock.patch.object(cycle, "run_child_bounded", side_effect=offline_peer):
                repeated = cycle.run_cycle(fixture.config, enable_sheet_write=True, today=dt.date(2026, 9, 10))
            self.assertEqual("incomplete", repeated["status"])
            self.assertEqual(1, len(recovery_commands))
            self.assertIn("--recover-source-debt-from", recovery_commands[0])
            self.assertEqual("peer/macbook", recovery_commands[0][recovery_commands[0].index("--recover-source") + 1])
            self.assertNotIn("--period-manifest", recovery_commands[0])
            self.assertNotIn("--replay-from", recovery_commands[0])
            self.assertNotIn("clockify_sheet_publish.py", recovery_commands[0][1])
        else:
            with mock.patch.object(cycle, "run_child_bounded", side_effect=AssertionError("sealed partial replay launched a child")):
                repeated = cycle.run_cycle(fixture.config, enable_sheet_write=True, today=dt.date(2026, 9, 10))
            self.assertEqual("idle", repeated["status"])
        bad = copy.deepcopy(state)
        bad["slices"]["2026-09-07"]["status"] = "delivered"
        with self.assertRaises(cycle.CycleError):
            cycle._validate_delivered_state(fixture.config, bad)

    def fixture(self):
        fixture = _FIXTURES.ReviewCycleDeliveryTests(methodName="runTest")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        return fixture

    @staticmethod
    def coverage():
        return {
            "status": "incomplete",
            "incomplete_sources": ["sessions/macbook"],
            "sources": {
                "clockify": {"status": "complete", "observed_count": 0},
                "sessions/macbook": {"status": "unavailable", "observed_count": 0},
            },
        }

    def test_missing_activity_peer_does_not_hide_verified_review_rows(self):
        """Catch coverage gating publication while the Clockify baseline is complete."""
        fixture = self.fixture()
        commands: list[list[str]] = []
        coverage = {
            "status": "incomplete",
            "incomplete_sources": ["sessions/macbook"],
            "sources": {
                "clockify": {"status": "complete", "observed_count": 0},
                "sessions/macbook": {"status": "unavailable", "observed_count": 0},
            },
        }
        with mock.patch.object(
            cycle,
            "run_child_bounded",
            side_effect=fixture.child_for_runs(
                commands, source_options={"coverage": coverage}
            ),
        ):
            cycle.run_cycle(
                fixture.config, enable_sheet_write=True, today=dt.date(2026, 9, 10)
            )
        # The external-child seam writes real source/replay/publication artifacts.
        # Assert a durable publication, not the presence of a mocked function.
        publication = fixture.root / "runs/publication-source-run/autopilot-result.json"
        self.assertTrue(
            publication.is_file(),
            "available review rows must be published despite a missing activity peer",
        )
        document = json.loads(publication.read_text())
        self.assertEqual("published", document["status"])
        self.assertEqual(0, document["clockify_writes"])
        self.assertIn(
            "wka-000000000000000000000001-s01",
            document["publications"][0]["row_ids"],
        )
        state = json.loads((fixture.state_dir / "review-cycle-state.json").read_text())
        self.assertIsNone(state["completed_through"])
        self.assertEqual(
            ["sessions/macbook"],
            state["slices"]["2026-09-07"]["source_completeness"]["incomplete_sources"],
        )
        record = state["slices"]["2026-09-07"]
        self.assertEqual("published_with_source_gaps", record["status"])
        self.assertNotIn("delivery_receipt", record)
        receipt = json.loads(Path(record["publication_receipt"]).read_text())
        self.assertEqual("clockify-review-partial-publication/v1", receipt["schema_version"])
        self.assertEqual(coverage, receipt["source_completeness"])
        # Local consumer revalidates the same saved publication, not a full-period claim.
        cycle._validate_delivered_state(fixture.config, state)
        record["status"] = "delivered"
        with self.assertRaisesRegex(cycle.CycleError, "source coverage"):
            cycle._validate_delivered_state(fixture.config, state)

    def test_missing_clockify_baseline_still_blocks_proposal_publication(self):
        """Catch publishing possible duplicates without a complete native baseline."""
        fixture = self.fixture()
        coverage = self.coverage()
        coverage["incomplete_sources"].append("clockify")
        coverage["sources"]["clockify"]["status"] = "unavailable"
        commands = []
        with mock.patch.object(cycle, "run_child_bounded", side_effect=fixture.child_for_runs(
            commands, source_options={"coverage": coverage}
        )):
            result = cycle.run_cycle(fixture.config, enable_sheet_write=True, today=dt.date(2026, 9, 10))
        self.assertEqual("recovery_blocked", result["status"])
        self.assertFalse((fixture.root / "runs/publication-source-run").exists())

    def test_successful_partial_publication_recovers_only_missing_source(self):
        """Catch full recollection/publication or discarded debt after visible review."""
        fixture = self.fixture()
        commands = []
        child = fixture.child_for_runs(commands, source_options={"coverage": self.coverage()})
        with mock.patch.object(cycle, "run_child_bounded", side_effect=child):
            cycle.run_cycle(fixture.config, enable_sheet_write=True, today=dt.date(2026, 9, 10))
            first_commands = list(commands)
            first_state = json.loads((fixture.state_dir / "review-cycle-state.json").read_text())
        def offline_peer(command, **_kwargs):
            commands.append(list(command))
            from scripts.autopilot_process import ChildResult
            return ChildResult(None, "", "peer unavailable", True, 0.1)
        with mock.patch.object(cycle, "run_child_bounded", side_effect=offline_peer):
            second = cycle.run_cycle(fixture.config, enable_sheet_write=True, today=dt.date(2026, 9, 10))
        self.assertEqual("incomplete", second["status"])
        self.assertEqual(len(first_commands) + 1, len(commands))
        self.assertIn("--recover-source-debt-from", commands[-1])
        self.assertEqual("peer/macbook", commands[-1][commands[-1].index("--recover-source") + 1])
        self.assertNotIn("--period-manifest", commands[-1])
        final_state = json.loads((fixture.state_dir / "review-cycle-state.json").read_text())
        self.assertIsNone(final_state["completed_through"])
        self.assertEqual(first_state["slices"]["2026-09-07"]["publication_receipt"],
                         final_state["slices"]["2026-09-07"]["publication_receipt"])
        from scripts import source_coverage
        store = source_coverage.SourceDebtStore.from_document(
            source_coverage.read(fixture.state_dir / "source-coverage.json")
        )
        self.assertIn("peer/macbook", {debt.interval.source for debt in store.active()})

    def test_failed_partial_publication_reuses_source_and_replay_on_retry(self):
        """Catch recollection or new inference when retrying only a partial delivery."""
        fixture = self.fixture()
        commands = []
        child = fixture.child_for_runs(commands, source_options={"coverage": self.coverage()}, publish_codes=[1, 0])
        with mock.patch.object(cycle, "run_child_bounded", side_effect=child):
            first = cycle.run_cycle(fixture.config, enable_sheet_write=True, today=dt.date(2026, 9, 10))
            second = cycle.run_cycle(fixture.config, enable_sheet_write=True, today=dt.date(2026, 9, 10))
        self.assertEqual("failed", first["status"])
        self.assertEqual("published_with_source_gaps", second["status"])
        self.assertEqual(1, sum("--period-manifest" in c for c in commands))
        self.assertEqual(1, sum("--replay-from" in c for c in commands))
        self.assertEqual(2, sum("clockify_sheet_publish.py" in c[1] for c in commands))
