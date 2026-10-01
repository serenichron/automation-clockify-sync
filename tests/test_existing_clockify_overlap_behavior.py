import datetime as dt
import unittest

from scripts import evidence_ledger
from scripts import work_accounting_pipeline as pipeline


class ExistingClockifyOverlapBehaviorTests(unittest.TestCase):
    def proposal(self, *, description="SC — Completed evidence review", project="abc123"):
        start = dt.datetime.fromisoformat("2026-09-28T09:00:00+03:00")
        return pipeline._proposal(
            {"activity_id": "act-review", "workstream_id": "ws-review"},
            {"project_name": "Serenichron", "project_suffix": project},
            description, start, start + dt.timedelta(hours=1), ["ev-review"], 1,
        )

    def existing(self, start, end, *, description="SC — Completed evidence review", project="abc123"):
        event = evidence_ledger.evidence_event(
            "clockify", {"source_type": "clockify", "source_id": "existing-1"},
            observed_at=start, raw_source_span={"start": start, "end": end},
            attributes={"description": description, "project_id_suffix": project},
        )
        return pipeline._existing_blocks([event.document()])[0]

    def test_exact_interval_project_and_description_credits_same_accomplishment(self):
        proposal = self.proposal()
        block = self.existing(proposal["start"], proposal["end"])
        skipped = []

        survivors = pipeline._normalize_postable_proposals([proposal], [block], skipped)

        self.assertEqual([], survivors)
        self.assertEqual(1, len(skipped))
        self.assertEqual(3600, skipped[0]["credited_overlap_receipt"]["credited_seconds"])

    def test_partial_overlap_preserves_full_proposal_with_exact_warning(self):
        proposal = self.proposal()
        block = self.existing("2026-09-28T09:30:00+03:00", "2026-09-28T10:30:00+03:00")
        skipped = []

        survivors = pipeline._normalize_postable_proposals([proposal], [block], skipped)

        self.assertEqual([], skipped)
        self.assertEqual(1, len(survivors))
        self.assertEqual((proposal["start"], proposal["end"], 3600), (
            survivors[0]["start"], survivors[0]["end"], survivors[0]["duration_seconds"],
        ))
        self.assertEqual([{
            "type": "existing_clockify_overlap", "counterpart_id": block["block_id"],
            "overlap_start": "2026-09-28T09:30:00+03:00",
            "overlap_end": "2026-09-28T10:00:00+03:00",
            "overlap_duration_seconds": 1800,
            "counterpart_project_suffix": "abc123",
        }], survivors[0]["review_warnings"])
        self.assertNotIn("credited_overlap_receipt", survivors[0]["provenance"])

    def test_same_interval_but_distinct_description_or_project_remains_reviewable(self):
        proposal = self.proposal()
        for description, project in (
            ("SC — Completed another deliverable", "abc123"),
            ("SC — Completed evidence review", "other1"),
        ):
            with self.subTest(description=description, project=project):
                block = self.existing(proposal["start"], proposal["end"], description=description, project=project)
                skipped = []
                survivors = pipeline._normalize_postable_proposals([proposal], [block], skipped)
                self.assertEqual([], skipped)
                self.assertEqual(1, len(survivors))
                self.assertEqual(3600, survivors[0]["duration_seconds"])
                self.assertEqual("existing_clockify_overlap", survivors[0]["review_warnings"][0]["type"])


if __name__ == "__main__":
    unittest.main()
