"""Source precision must not crash a batch or silently erase short evidence."""
from __future__ import annotations

import unittest

from scripts import work_accounting_pipeline as pipeline
import test_work_accounting_pipeline as fixtures


class SubminuteEvidenceTests(unittest.TestCase):
    def make_run(self, events, analysis):
        return fixtures.WorkAccountingPipelineTests.make_run(self, events, analysis)

    def short_events(self, end="2026-07-10T09:00:00.254000+03:00"):
        return [
            fixtures.session_event("short:1", "2026-07-10T09:00:00.247000+03:00"),
            fixtures.session_event("short:2", end),
        ]

    def assert_timing_uncertainty(self, result, evidence_ids):
        timing = [row for row in result["ambiguous"]
                  if row.get("exception_kind") == "timing_evidence"]
        self.assertEqual(1, len(timing))
        self.assertEqual(set(evidence_ids), set(timing[0]["evidence_ids"]))

    def test_observed_subsecond_bounds_keep_source_precision(self):
        """Catches serialization changing a real positive span to equal bounds."""
        intervals = pipeline._activity_observed_intervals(
            [event.document() for event in self.short_events()]
        )
        self.assertEqual([
            {"start": "2026-07-10T09:00:00.247000+03:00",
             "end": "2026-07-10T09:00:00.254000+03:00"},
        ], intervals)

    def test_subsecond_activity_is_explicit_uncertainty_not_invented_time(self):
        """Catches the original batch crash on two observations 7 ms apart."""
        events = self.short_events()
        ids = [event.evidence_id for event in events]
        try:
            _, result = self.make_run(events, fixtures.analysis_for(ids, recommended=10))
        except ValueError as error:
            self.fail(f"short source evidence must not crash accounting: {error}")
        self.assertEqual([], result["proposals"])
        self.assert_timing_uncertainty(result, ids)

    def test_subminute_activity_is_not_silently_lost_at_zero_effort(self):
        """Catches capping effort to zero then omitting the activity entirely."""
        events = self.short_events("2026-07-10T09:00:30.247000+03:00")
        ids = [event.evidence_id for event in events]
        _, result = self.make_run(events, fixtures.analysis_for(ids, recommended=10))
        self.assertEqual([], result["proposals"])
        self.assert_timing_uncertainty(result, ids)

    def test_valid_activity_survives_subsecond_activity_in_same_batch(self):
        """Catches one zero-capacity activity discarding valid rest-of-batch work."""
        short = self.short_events()
        valid = [
            fixtures.session_event("valid:1", "2026-07-10T10:00:00+03:00"),
            fixtures.session_event("valid:2", "2026-07-10T10:10:00+03:00"),
        ]
        short_ids = [event.evidence_id for event in short]
        valid_ids = [event.evidence_id for event in valid]
        analysis = fixtures.analysis_for(short_ids, recommended=10)
        other = fixtures.analysis_for(valid_ids, recommended=10)["activities"][0]
        other["object"] = "Clockify source collector"
        analysis["activities"].append(other)
        try:
            _, result = self.make_run(short + valid, analysis)
        except ValueError as error:
            self.fail(f"short source evidence must not crash a valid batch: {error}")
        self.assertEqual(1, len(result["proposals"]))
        proposal = result["proposals"][0]
        self.assertEqual(10, proposal["duration_minutes"])
        self.assertEqual("2026-07-10T10:00:00+03:00", proposal["start"])
        self.assertEqual("2026-07-10T10:10:00+03:00", proposal["end"])
        self.assert_timing_uncertainty(result, short_ids)


if __name__ == "__main__":
    unittest.main()
