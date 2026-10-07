"""Review timestamps use Bucharest wall time regardless of source timezone."""
import unittest

from scripts import clockify_sheet_publish as publisher
from test_sheet_publish import portfolio_document, proposal


class SheetTimestampTimezoneTests(unittest.TestCase):
    def test_aware_rows_use_bucharest_summer_and_winter_offsets(self):
        """Catches dropping source offsets or hardcoding summer's UTC+03."""
        cases = (
            ("2026-08-01T07:00:17Z", "2026-08-01T07:07:45Z",
             "2026-08-01 10:00:17", "2026-08-01 10:07:45"),
            ("2026-12-01T08:00:00+00:00", "2026-12-01T08:05:25+00:00",
             "2026-12-01 10:00", "2026-12-01 10:05:25"),
        )
        for builder, fixture in (
            (publisher.proposal_row, proposal),
            (publisher.portfolio_row, lambda: portfolio_document()["activities"][0]),
        ):
            for start, end, want_start, want_end in cases:
                with self.subTest(builder=builder.__name__, start=start):
                    candidate = fixture()
                    candidate.update(start=start, end=end)
                    row = builder(candidate, "timezone-test")
                    self.assertEqual([want_start, want_end], row[1:3])

    def test_aware_conversion_rolls_over_the_sheet_date(self):
        """Catches converting the hour without carrying into the local date."""
        candidate = proposal()
        candidate.update(start="2026-08-01T22:55:00Z", end="2026-08-01T23:05:00Z")
        row = publisher.proposal_row(candidate, "timezone-test")
        self.assertEqual(["2026-08-02 01:55", "2026-08-02 02:05"], row[1:3])

    def test_equivalent_aware_instants_produce_identical_review_rows(self):
        """Catches source-specific time display or changes outside time cells."""
        for builder, fixture in (
            (publisher.proposal_row, proposal),
            (publisher.portfolio_row, lambda: portfolio_document()["activities"][0]),
        ):
            with self.subTest(builder=builder.__name__):
                rows = []
                for start, end in (
                    ("2026-08-01T07:00:17Z", "2026-08-01T07:10:17Z"),
                    ("2026-08-01T10:00:17+03:00", "2026-08-01T10:10:17+03:00"),
                    ("2026-08-01T03:00:17-04:00", "2026-08-01T03:10:17-04:00"),
                ):
                    candidate = fixture()
                    candidate.update(start=start, end=end)
                    rows.append(builder(candidate, "timezone-test"))
                self.assertEqual(["2026-08-01 10:00:17", "2026-08-01 10:10:17"], rows[0][1:3])
                self.assertEqual(rows[0], rows[1])
                self.assertEqual(rows[0], rows[2])

    def test_naive_rows_keep_existing_local_time_and_precision(self):
        """Catches interpreting a legacy local timestamp as UTC or adding seconds."""
        for builder, fixture in (
            (publisher.proposal_row, proposal),
            (publisher.portfolio_row, lambda: portfolio_document()["activities"][0]),
        ):
            with self.subTest(builder=builder.__name__):
                candidate = fixture()
                candidate.update(start="2026-08-01T10:00:00", end="2026-08-01T10:07:45")
                row = builder(candidate, "timezone-test")
                self.assertEqual(["2026-08-01 10:00", "2026-08-01 10:07:45"], row[1:3])

    def test_invalid_timestamps_keep_the_publication_error(self):
        """Catches silently substituting or accepting a malformed timestamp."""
        candidate = proposal()
        candidate["start"] = "not-a-timestamp"
        with self.assertRaisesRegex(publisher.PublicationError, "invalid proposal timestamp: not-a-timestamp"):
            publisher.proposal_row(candidate, "timezone-test")

    def test_missing_timestamps_keep_the_publication_error(self):
        """Catches silently defaulting missing time values during conversion."""
        for missing in (None, "", "   "):
            with self.subTest(missing=missing):
                candidate = proposal()
                candidate["start"] = missing
                with self.assertRaisesRegex(publisher.PublicationError, "proposal is missing a timestamp"):
                    publisher.proposal_row(candidate, "timezone-test")


if __name__ == "__main__":
    unittest.main()
