from __future__ import annotations

import csv
from pathlib import Path
import tempfile
import unittest

from scripts import clockify_review_run


class CurrentReviewCsvDurationTests(unittest.TestCase):
    def export_duration(self, **duration_fields):
        snapshot = {"categories": {"new": [{
            "id": "wka-recorded-tail-s01",
            "start": "2026-09-10T10:21:00Z",
            "end": "2026-09-10T10:21:59Z",
            **duration_fields,
        }]}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "current-review.csv"
            clockify_review_run.write_current_review_csv(path, snapshot)
            with path.open(newline="", encoding="utf-8") as handle:
                return next(csv.DictReader(handle))["Duration (min)"]

    def test_authoritative_subminute_seconds_do_not_become_one_minute(self):
        cases = [
            (3, 0.05), (4, 0.06666666666666667), (24, 0.4),
            (55, 0.9166666666666666), (57, 0.95), (59, 0.9833333333333333),
        ]
        for seconds, expected in cases:
            with self.subTest(seconds=seconds):
                exported = self.export_duration(duration_minutes=0, duration_seconds=seconds)
                self.assertAlmostEqual(expected, float(exported))

    def test_authoritative_seconds_override_positive_legacy_minutes(self):
        self.assertEqual("1.5", self.export_duration(duration_minutes=99, duration_seconds=90))
        self.assertEqual("2", self.export_duration(duration_minutes=99, duration_seconds=120))

    def test_timestamp_fallback_preserves_positive_seconds(self):
        for legacy_minutes in (None, 0):
            with self.subTest(legacy_minutes=legacy_minutes):
                exported = self.export_duration(duration_minutes=legacy_minutes)
                self.assertAlmostEqual(0.9833333333333333, float(exported))

    def test_whole_minutes_keep_integer_csv_values(self):
        self.assertEqual("20", self.export_duration(duration_minutes=20))
        self.assertEqual("2", self.export_duration(
            duration_minutes=None, start="2026-09-10T10:21:00Z", end="2026-09-10T10:23:00Z",
        ))


if __name__ == "__main__":
    unittest.main()
