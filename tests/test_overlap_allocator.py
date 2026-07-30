"""Regression tests for deterministic proposal overlap allocation."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "clockify_sync_collect.py"
SPEC = importlib.util.spec_from_file_location("clockify_sync_collect_overlap", SCRIPT)
assert SPEC and SPEC.loader
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


def proposal(
    candidate_key: str,
    start: str,
    end: str,
    *,
    source_type: str = "codex",
    confidence: str = "medium",
    duration_minutes: int = 60,
) -> dict:
    return {
        "id": candidate_key,
        "candidate_key": candidate_key,
        "start": start,
        "end": end,
        "duration_minutes": duration_minutes,
        "source": [f"{source_type}:machine"],
        "source_label": candidate_key,
        "confidence": confidence,
        "description": f"SC — {candidate_key}",
        "rationale": "test candidate",
        "provenance": {"source_type": source_type},
    }


def overlap_minutes(left: dict, right: dict) -> float:
    start = max(collector.parse_dt(left["start"]), collector.parse_dt(right["start"]))
    end = min(collector.parse_dt(left["end"]), collector.parse_dt(right["end"]))
    return max(0.0, (end - start).total_seconds() / 60)


class CandidateOverlapAllocatorTests(unittest.TestCase):
    rules = {"soft_overlap_minutes": 15, "min_minutes": 10}

    def test_focused_high_priority_interval_trims_long_lower_priority_interval(self) -> None:
        focused = proposal(
            "meeting",
            "2026-07-21 10:30",
            "2026-07-21 11:00",
            source_type="fathom",
            confidence="high",
            duration_minutes=30,
        )
        long_running = proposal(
            "background",
            "2026-07-21 10:00",
            "2026-07-21 12:00",
            confidence="medium",
            duration_minutes=120,
        )
        skipped: list[dict] = []

        result = collector._resolve_candidate_overlaps(
            [long_running, focused], skipped, self.rules
        )

        by_key = {row["candidate_key"]: row for row in result}
        self.assertEqual("2026-07-21 10:30", by_key["meeting"]["start"])
        self.assertEqual("2026-07-21 11:00", by_key["meeting"]["end"])
        self.assertEqual("2026-07-21 11:00", by_key["background"]["start"])
        self.assertEqual("2026-07-21 12:00", by_key["background"]["end"])
        self.assertEqual(60, by_key["background"]["duration_minutes"])
        self.assertEqual(30, by_key["background"]["allocation"]["overlap_minutes_removed"])
        self.assertIn("trimmed around parallel work", by_key["background"]["description"])
        self.assertEqual([], skipped)

    def test_overlap_below_soft_threshold_is_preserved(self) -> None:
        first = proposal(
            "meeting",
            "2026-07-21 10:00",
            "2026-07-21 10:30",
            source_type="fathom",
            confidence="high",
            duration_minutes=30,
        )
        second = proposal(
            "focused-work",
            "2026-07-21 10:20",
            "2026-07-21 11:00",
            confidence="high",
            duration_minutes=40,
        )

        result = collector._resolve_candidate_overlaps(
            [second, first], [], self.rules
        )

        by_key = {row["candidate_key"]: row for row in result}
        self.assertEqual("2026-07-21 10:20", by_key["focused-work"]["start"])
        self.assertEqual("2026-07-21 11:00", by_key["focused-work"]["end"])
        self.assertNotIn("allocation", by_key["focused-work"])
        self.assertEqual(10, overlap_minutes(by_key["meeting"], by_key["focused-work"]))

    def test_fully_covered_lower_priority_candidate_is_skipped(self) -> None:
        meeting = proposal(
            "meeting",
            "2026-07-21 10:00",
            "2026-07-21 11:00",
            source_type="fathom",
            confidence="high",
            duration_minutes=60,
        )
        covered = proposal(
            "covered",
            "2026-07-21 10:10",
            "2026-07-21 10:50",
            confidence="low",
            duration_minutes=40,
        )
        skipped: list[dict] = []

        result = collector._resolve_candidate_overlaps(
            [covered, meeting], skipped, self.rules
        )

        self.assertEqual(["meeting"], [row["candidate_key"] for row in result])
        self.assertEqual("covered", skipped[0]["id"])
        self.assertIn("fully covered", skipped[0]["reason"])

    def test_result_has_no_overlap_at_or_above_soft_threshold(self) -> None:
        rows = [
            proposal(
                "meeting-a",
                "2026-07-21 09:30",
                "2026-07-21 10:00",
                source_type="fathom",
                confidence="high",
                duration_minutes=30,
            ),
            proposal(
                "meeting-b",
                "2026-07-21 11:00",
                "2026-07-21 11:30",
                source_type="fathom",
                confidence="high",
                duration_minutes=30,
            ),
            proposal(
                "work-a",
                "2026-07-21 09:00",
                "2026-07-21 12:00",
                confidence="medium",
                duration_minutes=180,
            ),
            proposal(
                "work-b",
                "2026-07-21 10:50",
                "2026-07-21 12:15",
                confidence="low",
                duration_minutes=85,
            ),
        ]

        result = collector._resolve_candidate_overlaps(rows, [], self.rules)

        for index, left in enumerate(result):
            for right in result[index + 1 :]:
                self.assertLess(
                    overlap_minutes(left, right),
                    self.rules["soft_overlap_minutes"],
                    (left["candidate_key"], right["candidate_key"]),
                )


if __name__ == "__main__":
    unittest.main()
