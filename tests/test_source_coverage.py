from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts import source_coverage


NOW = "2026-08-12T10:00:00Z"
LATER = "2026-08-12T10:05:00Z"


def interval(source: str, since: str, until: str) -> object:
    return source_coverage.SourceInterval(
        source=source,
        since_utc=f"{since}T00:00:00Z",
        until_utc=f"{until}T00:00:00Z",
        slice_id=f"slice-{since}-{until}",
        compatibility_version="source-debt/v1",
    )


class SourceCoverageTests(unittest.TestCase):
    def test_completing_one_failed_slice_does_not_clear_adjacent_debt(self):
        """Catches a source/day completion branch that clears an adjacent interval."""
        store = source_coverage.SourceDebtStore()
        first = interval("sessions/macbook", "2026-08-01", "2026-08-03")
        second = interval("sessions/macbook", "2026-08-03", "2026-08-05")

        store.record_failure(
            first,
            failure_class="offline",
            retryable=True,
            resume_state_digest="sha256:a",
            attempted_at=NOW,
        )
        store.record_failure(
            second,
            failure_class="offline",
            retryable=True,
            resume_state_digest="sha256:b",
            attempted_at=NOW,
        )
        store.record_complete(
            first, completion_bundle_digest="sha256:c", completed_at=LATER
        )

        self.assertEqual(
            (second.debt_id,), tuple(item.debt_id for item in store.active())
        )

    def test_two_sources_have_independent_retry_counts(self):
        """Catches a shared retry counter that leaks one source's failures to another."""
        store = source_coverage.SourceDebtStore()
        mac = interval("sessions/macbook", "2026-08-01", "2026-08-03")
        desktop = interval("repositories/omarchy-desktop", "2026-08-01", "2026-08-03")

        mac_item = store.record_failure(
            mac, failure_class="offline", retryable=True,
            resume_state_digest="sha256:m", attempted_at=NOW,
        )
        desktop_item = store.record_failure(
            desktop, failure_class="offline", retryable=True,
            resume_state_digest="sha256:d", attempted_at=NOW,
        )
        mac_item = store.record_failure(
            mac, failure_class="offline", retryable=True,
            resume_state_digest="sha256:m", attempted_at=LATER,
        )

        self.assertEqual(2, mac_item.retry_count)
        self.assertEqual(1, desktop_item.retry_count)

    def test_legacy_debt_is_migrated_as_an_unverifiable_oldest_day(self):
        """Catches migration code that treats a day-granular record as exact coverage."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source-coverage.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "sources": {"sessions/macbook": {"debt_since": "2026-08-01"}},
            }))
            migrated = source_coverage.read(path)

        item = source_coverage.SourceDebtStore.from_document(migrated).active()[0]
        self.assertEqual("legacy/source-coverage/v1", item.interval.compatibility_version)
        self.assertEqual({"sessions/macbook": "2026-08-01"}, source_coverage.active_debt(migrated))
        with self.assertRaises(ValueError):
            source_coverage.SourceDebtStore.from_document(migrated).record_complete(
                item.interval,
                completion_bundle_digest="sha256:verified-but-not-exact",
                completed_at=LATER,
            )

    def test_invalid_legacy_state_creates_visible_conservative_debt(self):
        """Catches corruption handling that silently discards unresolved legacy coverage."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "source-coverage.json"
            path.write_text(json.dumps({
                "schema_version": 1,
                "sources": {"sessions/macbook": {"debt_since": "not-a-date"}},
            }))
            migrated = source_coverage.read(path)

        self.assertTrue(migrated["migration_warnings"])
        self.assertTrue(source_coverage.active_debt(migrated))

    def test_eligible_returns_only_retryable_unresolved_debt(self):
        """Catches eligibility code that retries terminal or explicitly non-retryable debt."""
        store = source_coverage.SourceDebtStore()
        retryable = interval("sessions/macbook", "2026-08-01", "2026-08-03")
        terminal = interval("repositories/omarchy-desktop", "2026-08-01", "2026-08-03")
        store.record_failure(
            retryable, failure_class="offline", retryable=True,
            resume_state_digest="sha256:r", attempted_at=NOW,
        )
        store.record_failure(
            terminal, failure_class="denied", retryable=False,
            resume_state_digest="sha256:t", attempted_at=NOW,
        )

        self.assertEqual((retryable.debt_id,), tuple(item.debt_id for item in store.eligible(NOW)))
        store.record_complete(
            retryable, completion_bundle_digest="sha256:c", completed_at=LATER
        )
        self.assertEqual((), store.eligible(LATER))

    def test_exhausted_debt_stays_active_and_resets_only_after_a_later_failure(self):
        """Catches terminal retry handling that loses coverage debt or shares its next retry."""
        store = source_coverage.SourceDebtStore()
        debt = interval("sessions/macbook", "2026-08-01", "2026-08-03")
        item = store.record_failure(
            debt, failure_class="offline", retryable=True,
            resume_state_digest="sha256:r", attempted_at=NOW,
        )

        exhausted = store.exhaust(item.debt_id, terminal_reason="retry_limit")
        self.assertEqual("exhausted", exhausted.status)
        self.assertEqual("retry_limit", exhausted.terminal_reason)
        self.assertEqual((item.debt_id,), tuple(item.debt_id for item in store.active()))
        self.assertEqual((), store.eligible(LATER))
        reopened = store.record_failure(
            debt, failure_class="offline", retryable=True,
            resume_state_digest="sha256:r", attempted_at=LATER,
        )
        self.assertEqual(1, reopened.retry_count)
        self.assertIsNone(reopened.terminal_reason)

    def test_document_round_trip_verifies_only_safe_debt_fields(self):
        """Catches persistence that omits debt identity or stores raw collector evidence."""
        store = source_coverage.SourceDebtStore()
        debt = interval("sessions/macbook", "2026-08-01", "2026-08-03")
        store.record_failure(
            debt, failure_class="offline", retryable=True,
            resume_state_digest="sha256:r", attempted_at=NOW,
        )
        document = store.document()
        restored = source_coverage.SourceDebtStore.from_document(document)

        restored.verify()
        self.assertEqual((debt.debt_id,), tuple(item.debt_id for item in restored.active()))
        self.assertEqual(
            {"attempted_at", "debt_id", "event", "failure_class", "interval", "resume_state_digest", "retryable"},
            set(document["events"][0]),
        )

    def test_event_replay_rejects_a_debt_id_that_does_not_match_its_interval(self):
        """Catches replay accepting a tampered event identity while deriving a different debt."""
        store = source_coverage.SourceDebtStore()
        debt = interval("sessions/macbook", "2026-08-01", "2026-08-03")
        store.record_failure(
            debt, failure_class="offline", retryable=True,
            resume_state_digest="sha256:r", attempted_at=NOW,
        )
        document = store.document()
        document["events"][0]["debt_id"] = "source-debt/tampered"

        with self.assertRaises(ValueError):
            source_coverage.SourceDebtStore.from_document(document)

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
