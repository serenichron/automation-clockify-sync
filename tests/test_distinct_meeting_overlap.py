import datetime as dt
import unittest

from scripts import work_accounting_pipeline as pipeline


class DistinctMeetingOverlapTests(unittest.TestCase):
    def proposal(self, activity_id, start_minute, duration_minutes, meeting_id=None):
        start = dt.datetime.fromisoformat("2026-09-28T09:00:00+03:00") + dt.timedelta(minutes=start_minute)
        proposal = pipeline._proposal(
            {"activity_id": activity_id, "workstream_id": activity_id},
            {"project_name": "Serenichron", "project_suffix": "sc0001"},
            f"SC — {activity_id}", start, start + dt.timedelta(minutes=duration_minutes),
            [f"ev-{activity_id}"], 1,
        )
        if meeting_id:
            proposal["provenance"]["canonical_meeting_id"] = meeting_id
        return proposal

    def assert_reciprocal_review_warnings(self, rows):
        self.assertEqual(2, len(rows))
        for row, counterpart in ((rows[0], rows[1]), (rows[1], rows[0])):
            self.assertEqual([{
                "type": "review_proposal_overlap",
                "counterpart_id": counterpart["candidate_key"],
                "overlap_start": "2026-09-28T09:30:00+03:00",
                "overlap_end": "2026-09-28T10:00:00+03:00",
                "overlap_duration_seconds": 1800,
                "counterpart_project_suffix": "sc0001",
            }], row["review_warnings"])

    def test_distinct_overlapping_meetings_retain_ninety_minutes_with_warning(self):
        first = self.proposal("meeting-one", 0, 60, "meeting-1")
        second = self.proposal("meeting-two", 30, 30, "meeting-2")
        skipped = []

        rows = pipeline._normalize_postable_proposals([first, second], [], skipped)

        self.assertEqual([], skipped)
        self.assertEqual({"meeting-one": 3600, "meeting-two": 1800}, {
            row["activity_id"]: row["duration_seconds"] for row in rows
        })
        self.assertEqual(5400, sum(row["duration_seconds"] for row in rows))
        self.assert_reciprocal_review_warnings(rows)

    def test_meeting_and_unrelated_work_retain_full_duration_in_either_order(self):
        meeting = self.proposal("meeting", 0, 60, "meeting-1")
        work = self.proposal("work", 30, 30)
        for incoming in ([meeting, work], [work, meeting]):
            with self.subTest(order=[row["activity_id"] for row in incoming]):
                skipped = []
                rows = pipeline._normalize_postable_proposals(incoming, [], skipped)
                self.assertEqual([], skipped)
                self.assertEqual({"meeting": 3600, "work": 1800}, {
                    row["activity_id"]: row["duration_seconds"] for row in rows
                })
                self.assert_reciprocal_review_warnings(rows)

    def test_same_canonical_meeting_from_two_sources_is_credited(self):
        first = self.proposal("fathom-source", 0, 60, "meeting-1")
        duplicate = self.proposal("calendar-source", 30, 30, "meeting-1")
        skipped = []

        rows = pipeline._normalize_postable_proposals([first, duplicate], [], skipped)

        self.assertEqual(1, len(rows))
        self.assertEqual(3600, rows[0]["duration_seconds"])
        self.assertEqual([], rows[0]["review_warnings"])
        self.assertEqual(1, len(skipped))
        self.assertEqual(1800, skipped[0]["credited_overlap_receipt"]["credited_seconds"])
        self.assertEqual("meeting_proposal_overlap", skipped[0]["credited_overlap_receipt"]["counterparts"][0]["type"])

    def test_distinct_meeting_identity_outweighs_reused_activity_id(self):
        first = self.proposal("shared-activity", 0, 60, "meeting-1")
        second = self.proposal("shared-activity", 30, 30, "meeting-2")
        second["candidate_key"] = "distinct-meeting-segment"
        skipped = []

        rows = pipeline._normalize_postable_proposals([first, second], [], skipped)

        self.assertEqual([], skipped)
        self.assertEqual(5400, sum(row["duration_seconds"] for row in rows))
        self.assert_reciprocal_review_warnings(rows)

    def test_same_nonmeeting_activity_segments_are_still_credited(self):
        first = self.proposal("same-activity", 0, 60)
        duplicate = self.proposal("same-activity", 30, 30)
        duplicate["candidate_key"] = "distinct-segment"
        skipped = []

        rows = pipeline._normalize_postable_proposals([first, duplicate], [], skipped)

        self.assertEqual(1, len(rows))
        self.assertEqual(3600, rows[0]["duration_seconds"])
        self.assertEqual(1, len(skipped))
        self.assertEqual(1800, skipped[0]["credited_overlap_receipt"]["credited_seconds"])


if __name__ == "__main__":
    unittest.main()
