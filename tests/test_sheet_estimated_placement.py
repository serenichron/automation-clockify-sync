"""Estimated human timing must remain visible without blocking native review."""
import json
import unittest

from scripts import clockify_sheet_publish as publisher
from test_sheet_publish import proposal


class EstimatedPlacementPublicationTests(unittest.TestCase):
    def test_native_estimated_placement_warning_reaches_review_row(self):
        """Catches the publisher rejecting the accounting pipeline's warning."""
        candidate = proposal()
        warning = {
            "type": "estimated_session_placement",
            "reason": "Estimated effort placed within shared same-session human observations; exact outcome boundaries are not observed.",
        }
        candidate["review_warnings"] = [warning]
        try:
            row = publisher.proposal_row(candidate, "run-1")
        except publisher.PublicationError as error:
            self.fail(f"native estimated timing must reach review: {error}")
        self.assertEqual([warning], json.loads(row[12]))
        self.assertEqual("pending", row[9])
        self.assertEqual("unposted", row[13])
        self.assertEqual(10, row[3])

    def test_estimated_placement_does_not_accept_unbounded_or_extra_text(self):
        """Catches malformed warning payloads leaking into client review cells."""
        for warning in (
            {"type": "estimated_session_placement"},
            {"type": "estimated_session_placement", "reason": ""},
            {"type": "estimated_session_placement", "reason": "x" * 257},
            {"type": "estimated_session_placement", "reason": "Timing estimated", "raw_evidence": "private"},
        ):
            with self.subTest(warning=warning):
                candidate = proposal()
                candidate["review_warnings"] = [warning]
                with self.assertRaises(publisher.PublicationError):
                    publisher.proposal_row(candidate, "run-1")


if __name__ == "__main__":
    unittest.main()
