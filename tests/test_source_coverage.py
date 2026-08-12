from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts import source_coverage


class SourceCoverageTests(unittest.TestCase):
    def completeness(self, peer_status: str):
        return {
            "status": "incomplete" if peer_status != "complete" else "complete",
            "sources": {
                "clockify": {"status": "complete"},
                "fathom": {"status": "complete"},
                "sessions/omarchy-precision": {"status": "complete"},
                "repositories/omarchy-precision": {"status": "complete"},
                "sessions/macbook": {"status": peer_status},
            },
            "incomplete_sources": (
                ["sessions/macbook"] if peer_status != "complete" else []
            ),
        }

    def update(self, ledger, status, since="2026-08-01", until="2026-08-12"):
        return source_coverage.update(
            ledger,
            completeness=self.completeness(status),
            interval_since=since,
            interval_until=until,
            coordinator="omarchy-precision",
            run_id="run-1",
            attempted_at="2026-08-12T10:00:00Z",
        )

    def test_unavailable_peer_creates_debt_and_extends_next_window(self):
        ledger = self.update({"schema_version": 1, "sources": {}}, "unavailable")

        self.assertEqual(
            {"sessions/macbook": "2026-08-01"},
            source_coverage.active_debt(ledger),
        )
        self.assertEqual("2026-08-01", source_coverage.effective_since("2026-08-10", ledger))

    def test_later_failure_never_advances_existing_debt(self):
        ledger = self.update({"schema_version": 1, "sources": {}}, "unavailable")
        ledger = self.update(ledger, "partial", since="2026-08-10")

        self.assertEqual("2026-08-01", source_coverage.active_debt(ledger)["sessions/macbook"])

    def test_complete_backfill_clears_debt_only_when_it_reaches_debt_start(self):
        ledger = self.update({"schema_version": 1, "sources": {}}, "unavailable")
        still_due = self.update(ledger, "complete", since="2026-08-05")
        cleared = self.update(still_due, "complete", since="2026-08-01")

        self.assertIn("sessions/macbook", source_coverage.active_debt(still_due))
        self.assertEqual({}, source_coverage.active_debt(cleared))
        self.assertEqual("2026-08-12", cleared["sources"]["sessions/macbook"]["covered_through"])

    def test_bootstrap_recovers_debt_from_predeployment_blocked_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            run = Path(directory) / "20260812T063103Z"
            run.mkdir()
            (run / "run-report.json").write_text(json.dumps({
                "run_id": run.name,
                "date_range": {
                    "since": "2026-07-30 00:00",
                    "until": "2026-08-12 09:31",
                },
                "evidence_ledger": {
                    "source_completeness": self.completeness("unavailable")
                },
            }))

            ledger = source_coverage.bootstrap_from_runs(
                Path(directory), "omarchy-precision"
            )

        self.assertEqual(
            {"sessions/macbook": "2026-07-30"},
            source_coverage.active_debt(ledger),
        )


if __name__ == "__main__":
    unittest.main()
