"""Operator-only CLI entrypoint for an already-proven historical delivery."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import unittest
from unittest import mock

from scripts import clockify_review_cycle as cycle
import test_cycle_historical_adoption as adoption_fixtures
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
