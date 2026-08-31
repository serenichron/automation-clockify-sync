from __future__ import annotations

import json
from pathlib import Path
import unittest

from scripts import collector_receipts, source_coverage


FIXTURES = Path(__file__).parent / "fixtures" / "reconciliation"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name / "manifest.json").read_text(encoding="utf-8"))


class ReconciliationResilienceTests(unittest.TestCase):
    def test_routine_fixture_is_one_complete_two_day_slice_without_debt(self):
        fixture = load_fixture("routine-two-day")

        self.assertEqual("routine-two-day", fixture["scenario"])
        self.assertEqual(1, len(fixture["slices"]))
        self.assertEqual("2026-08-01T00:00:00Z", fixture["slices"][0]["since_utc"])
        self.assertEqual("2026-08-03T00:00:00Z", fixture["slices"][0]["until_utc"])
        self.assertEqual([], fixture["source_debt"])
        self.assertEqual("deferred", fixture["publication"])

    def test_exceptional_fixture_retries_only_the_exact_transient_source_debt(self):
        fixture = load_fixture("exceptional-backlog")
        transient, limitation = fixture["source_debt"]
        first, second = fixture["slices"]
        debt_store = source_coverage.SourceDebtStore()
        interval = source_coverage.SourceInterval(
            source=transient["source"], since_utc=first["since_utc"],
            until_utc=first["until_utc"], slice_id=first["slice_id"],
            compatibility_version="source-debt/v1",
        )
        adjacent = source_coverage.SourceInterval(
            source=limitation["source"], since_utc=second["since_utc"],
            until_utc=second["until_utc"], slice_id=second["slice_id"],
            compatibility_version="source-debt/v1",
        )
        debt_store.record_failure(interval, failure_class=transient["failure_class"], retryable=True,
                                  resume_state_digest="sha256:" + "a" * 64, attempted_at="2026-08-03T00:00:00Z")
        debt_store.record_failure(adjacent, failure_class=limitation["failure_class"], retryable=False,
                                  resume_state_digest="sha256:" + "b" * 64, attempted_at="2026-08-05T00:00:00Z")
        debt_store.record_complete(interval, completion_bundle_digest="sha256:" + "c" * 64,
                                   completed_at="2026-08-05T00:05:00Z")

        self.assertEqual((adjacent.debt_id,), tuple(item.debt_id for item in debt_store.active()))
        receipt = collector_receipts.failure_receipt(
            source=limitation["source"], slice_id=second["slice_id"],
            checkpoint_identity_digest="sha256:" + "d" * 64,
            failure_class=limitation["failure_class"], retryable=False,
            resume_state_digest="sha256:" + "e" * 64,
            occurred_at="2026-08-05T00:00:00Z", credential="never-retained",
        )
        self.assertNotIn("never-retained", json.dumps(receipt.document()))
        self.assertEqual("reused_after_timeout", first["completion"])
        self.assertEqual("approved_limitation_no_invented_time", limitation["resolution"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
