"""Operator-only CLI entrypoint for an already-proven historical delivery."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import unittest
from unittest import mock

from scripts import clockify_review_cycle as cycle
from scripts import clockify_review_run as review
from scripts import clockify_sheet_publish as publisher, clockify_sync_collect as collector
import test_cycle_historical_adoption as adoption_fixtures
import test_cycle_selected_delivery_adoption as selected_fixtures
from test_review_cycle_delivery import write_json


class HistoricalAdoptionCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.proof = adoption_fixtures.HistoricalAdoptionTests(
            methodName="test_adopts_exact_historical_delivery_once_without_children_or_republication"
        )
        self.proof.setUp()
        self.addCleanup(self.proof.doCleanups)
        self.config_path = self.proof.root / "cycle-config.json"
        self.request_path = self.proof.root / "historical-adoption-request.json"
        write_json(self.config_path, self.proof.config)
        write_json(self.request_path, self.proof.request)

    def invoke(self, *extra: str) -> tuple[int, dict | None, str]:
        stdout, stderr = StringIO(), StringIO()
        with (
            mock.patch.object(cycle, "_validate_runtime_root", return_value=self.proof.root),
            mock.patch.object(cycle, "run_cycle", side_effect=AssertionError("scheduled work")),
            mock.patch.object(cycle, "run_child_bounded", side_effect=AssertionError("child")),
            mock.patch.object(collector, "clockify_get", side_effect=AssertionError("Clockify provider")),
            mock.patch.object(publisher, "publish", side_effect=AssertionError("Sheet publication")),
            mock.patch.object(publisher, "publish_monthly_unresolved", side_effect=AssertionError("diagnostic publication")),
            mock.patch.object(publisher, "publish_proposal_partitions", side_effect=AssertionError("partition publication")),
            redirect_stdout(stdout), redirect_stderr(stderr),
        ):
            try:
                code = cycle.main([
                    "--config", str(self.config_path),
                    "--adopt-historical-request", str(self.request_path),
                    *extra,
                ])
            except SystemExit as exc:
                code = int(exc.code)
        lines = stdout.getvalue().splitlines()
        return code, json.loads(lines[-1]) if lines else None, stderr.getvalue()

    def test_exact_request_adopts_once_and_repeated_invocation_is_idempotent(self) -> None:
        first_code, first, first_error = self.invoke()
        self.assertEqual(0, first_code, first_error)
        self.assertEqual("delivered", first["status"])
        state_path = self.proof.state_dir / "review-cycle-state.json"
        state = json.loads(state_path.read_text())
        self.assertEqual(self.proof.until, state["completed_through"])
        receipt = Path(state["slices"][self.proof.since]["delivery_receipt"])
        adoption = Path(state["slices"][self.proof.since]["historical_adoption_receipt"])
        before = (state_path.read_bytes(), receipt.read_bytes(), adoption.read_bytes())

        second_code, second, second_error = self.invoke()
        self.assertEqual(0, second_code, second_error)
        self.assertEqual(first, second)
        self.assertEqual(before, (state_path.read_bytes(), receipt.read_bytes(), adoption.read_bytes()))

    def test_derived_adoption_cli_binds_operational_runs_before_native_ancestry_validation(self) -> None:
        """Catches CLI adoption retaining the immutable code-root/runs default."""
        request, source, _ = self.proof._derived_adoption_request()
        write_json(self.request_path, request)
        with mock.patch.object(review, "RUNS", review.ROOT / "runs"):
            code, result, error = self.invoke()
            self.assertEqual(0, code, error)
            self.assertEqual("delivered", result["status"])
            state_path = self.proof.state_dir / "review-cycle-state.json"
            state = json.loads(state_path.read_bytes())
            record = state["slices"][self.proof.since]
            self.assertEqual(str(source), record["source"]["run_dir"])
            self.assertEqual(self.proof.until, state["completed_through"])
            receipts = [Path(record[key]) for key in (
                "delivery_receipt", "historical_adoption_receipt")]
            before = [(path.read_bytes(), path.stat().st_mtime_ns)
                      for path in [state_path, *receipts]]
            # Each invocation starts with the actual imported release default,
            # not a test fixture's preconfigured operational runs root.
            review.RUNS = review.ROOT / "runs"
            repeat_code, repeat, repeat_error = self.invoke()
            self.assertEqual(0, repeat_code, repeat_error)
            self.assertEqual(result, repeat)
            self.assertEqual(before, [(path.read_bytes(), path.stat().st_mtime_ns)
                                     for path in [state_path, *receipts]])

    def test_selected_adoption_cli_starts_from_release_default_without_providers(self) -> None:
        """Catches startup binding breaking selected graph context or repeat."""
        self.proof = selected_fixtures.SelectedHistoricalDeliveryTests()
        self.proof.setUp()
        self.addCleanup(self.proof.doCleanups)
        self.config_path = self.proof.root / "cycle-config.json"
        self.request_path = self.proof.root / "historical-adoption-request.json"
        write_json(self.config_path, self.proof.config)
        write_json(self.request_path, self.proof.request)
        with mock.patch.object(review, "RUNS", review.ROOT / "runs"):
            code, result, error = self.invoke()
            self.assertEqual(0, code, error)
            self.assertEqual("delivered_with_exceptions", result["status"])
            state_path = self.proof.state_dir / "review-cycle-state.json"
            state = json.loads(state_path.read_bytes())
            record = state["slices"][self.proof.since]
            receipt_paths = [Path(record[key]) for key in (
                "delivery_receipt", "historical_adoption_receipt")]
            sealed = [(path.read_bytes(), path.stat().st_mtime_ns)
                      for path in [state_path, *receipt_paths]]
            review.RUNS = review.ROOT / "runs"
            repeat_code, repeat, repeat_error = self.invoke()
            self.assertEqual(0, repeat_code, repeat_error)
            self.assertEqual(result, repeat)
            self.assertEqual(sealed, [(path.read_bytes(), path.stat().st_mtime_ns)
                                     for path in [state_path, *receipt_paths]])

    def test_unbound_source_path_is_still_rejected_after_cli_startup(self) -> None:
        """Catches startup binding admitting a source outside operational runs."""
        request, _, _ = self.proof._derived_adoption_request()
        request["source_result"] = str(self.proof.root / "outside-runs/autopilot-result.json")
        write_json(self.request_path, request)
        state_path = self.proof.state_dir / "review-cycle-state.json"
        before = state_path.read_bytes()
        with mock.patch.object(review, "RUNS", review.ROOT / "runs"):
            code, result, error = self.invoke()
        self.assertEqual(2, code)
        self.assertIsNone(result)
        self.assertIn("escapes its bounded run", error)
        self.assertEqual(before, state_path.read_bytes())
        self.assertFalse((self.proof.state_dir / "delivery-receipts").exists())

    def test_malformed_request_leaves_cycle_state_untouched(self) -> None:
        before = (self.proof.state_dir / "review-cycle-state.json").read_bytes()
        write_json(self.request_path, {"schema_version": "wrong"})
        code, result, error = self.invoke()
        self.assertEqual(2, code)
        self.assertIsNone(result)
        self.assertIn("historical adoption request is invalid", error)
        self.assertEqual(before, (self.proof.state_dir / "review-cycle-state.json").read_bytes())
        self.assertFalse((self.proof.state_dir / "delivery-receipts").exists())

    def test_adoption_rejects_scheduling_and_audit_modes(self) -> None:
        before = (self.proof.state_dir / "review-cycle-state.json").read_bytes()
        for extra in (
            ("--enable-sheet-write",),
            ("--audit-coverage-output", str(self.proof.root / "audit.json")),
        ):
            with self.subTest(extra=extra):
                code, result, error = self.invoke(*extra)
                self.assertEqual(2, code)
                self.assertIsNone(result)
                self.assertIn("mutually exclusive", error)
                self.assertEqual(before, (self.proof.state_dir / "review-cycle-state.json").read_bytes())


if __name__ == "__main__":
    unittest.main()
