from __future__ import annotations

from datetime import datetime
import importlib.util
from pathlib import Path
import sys
import unittest


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "work_allocator.py"
SPEC = importlib.util.spec_from_file_location("work_allocator", MODULE_PATH)
assert SPEC and SPEC.loader
allocator = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = allocator
SPEC.loader.exec_module(allocator)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 1, hour, minute)


def on(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, day, hour, minute)


def activity(
    activity_id: str,
    recommended: int,
    *,
    start: datetime = at(9),
    end: datetime = at(17),
    confidence: str = "medium",
    attention: float = 0,
):
    return {
        "activity_id": activity_id,
        "workstream_id": f"ws-{activity_id}",
        "evidence_spans": [
            {"evidence_id": f"ev-{activity_id}-one", "start": at(9), "end": at(11)},
            {"evidence_id": f"ev-{activity_id}-two", "start": at(10), "end": at(12)},
        ],
        "effort": {"min": 15, "recommended": recommended, "max": recommended + 30},
        "allowed_envelope": {"start": start, "end": end},
        "confidence": confidence,
        "attention_signal": attention,
    }


class WorkAllocatorTests(unittest.TestCase):
    def test_evidence_and_fixed_blocks_are_immutable_and_evidence_may_overlap(self):
        raw = activity("alpha", 30)
        fixed = {"id": "existing", "start": at(12), "end": at(13)}
        evidence_before = [dict(span) for span in raw["evidence_spans"]]
        fixed_before = dict(fixed)

        result = allocator.allocate_work([raw], [fixed])

        self.assertEqual(evidence_before, raw["evidence_spans"])
        self.assertEqual(fixed_before, fixed)
        self.assertEqual(2, len(result.evidence[0].evidence_spans))
        self.assertEqual(at(9), result.allocations[0].start)

    def test_allocations_are_strictly_non_overlapping_with_fixed_blocks(self):
        result = allocator.allocate_work(
            [activity("alpha", 90), activity("beta", 90, confidence="high")],
            [{"id": "meeting", "start": at(10), "end": at(11)}],
        )

        allocator.validate_strict_non_overlap(
            [
                allocator.FixedBlock("meeting", at(10), at(11)),
                *result.allocations,
            ]
        )
        self.assertFalse(result.contested_time)

    def test_overlapping_historical_fixed_blocks_reserve_their_union_without_blocking_proposals(self):
        fixed = [
            {"id": "legacy-one", "start": at(9), "end": at(10)},
            {"id": "legacy-two", "start": at(9, 30), "end": at(10, 30)},
        ]
        demand = activity("alpha", 30, start=at(9), end=at(11))

        first = allocator.allocate_work([demand], fixed)
        second = allocator.allocate_work([demand], list(reversed(fixed)))

        self.assertEqual(first.allocations, second.allocations)
        self.assertEqual([(at(10, 30), at(11))], [(row.start, row.end) for row in first.allocations])
        allocator.validate_strict_non_overlap(first.allocations)
        for proposal in first.allocations:
            for block in fixed:
                self.assertFalse(
                    allocator._overlaps(proposal.start, proposal.end, block["start"], block["end"])
                )
        self.assertFalse(first.contested_time)

    def test_empty_capacity_is_not_filled(self):
        result = allocator.allocate_work([activity("alpha", 30, start=at(9), end=at(12))])

        self.assertEqual(30, sum(row.duration_minutes for row in result.allocations))
        self.assertEqual(150, result.unallocated_capacity.total_minutes)
        self.assertEqual(((at(9, 30), at(12)),), result.unallocated_capacity.intervals)

    def test_infeasible_demand_is_contested_not_trimmed_or_dropped(self):
        result = allocator.allocate_work([activity("alpha", 120, start=at(9), end=at(10))])

        self.assertEqual(60, sum(row.duration_minutes for row in result.allocations))
        self.assertEqual(1, len(result.contested_time))
        contested = result.contested_time[0]
        self.assertEqual(120, contested.requested_minutes)
        self.assertEqual(60, contested.allocated_minutes)
        self.assertEqual(60, contested.unallocated_minutes)
        self.assertEqual(contested.requested_minutes, contested.allocated_minutes + contested.unallocated_minutes)

    def test_input_permutation_produces_the_same_schedule(self):
        rows = [
            activity("low", 60, confidence="low"),
            activity("high", 60, confidence="high"),
            activity("medium", 60, confidence="medium", attention=3),
        ]
        one = allocator.allocate_work(rows, [{"id": "fixed", "start": at(10), "end": at(11)}])
        two = allocator.allocate_work(list(reversed(rows)), [{"id": "fixed", "start": at(10), "end": at(11)}])

        self.assertEqual(one.allocations, two.allocations)
        self.assertEqual(one.contested_time, two.contested_time)

    def test_recommended_effort_splits_across_genuine_free_segments_and_conserves_duration(self):
        result = allocator.allocate_work(
            [activity("alpha", 90, start=at(9), end=at(12))],
            [
                {"id": "meeting-one", "start": at(9, 30), "end": at(10)},
                {"id": "meeting-two", "start": at(11), "end": at(11, 30)},
            ],
        )

        self.assertEqual(2, len(result.allocations))
        self.assertEqual([30, 60], [row.duration_minutes for row in result.allocations])
        self.assertEqual(90, sum(row.duration_minutes for row in result.allocations))
        self.assertFalse(result.contested_time)
        self.assertEqual(
            ["alpha:allocation:01", "alpha:allocation:02"],
            [row.allocation_id for row in result.allocations],
        )

    def test_second_precision_fixed_block_preserves_only_whole_minute_free_capacity(self):
        fixed = {
            "id": "recorded-meeting",
            "start": datetime(2026, 8, 1, 9, 10, 30),
            "end": datetime(2026, 8, 1, 9, 20, 30),
        }
        demand = activity("alpha", 49, start=at(9), end=at(10))

        first = allocator.allocate_work([demand], [fixed])
        second = allocator.allocate_work([demand], [dict(fixed)])

        self.assertEqual(first.allocations, second.allocations)
        self.assertEqual(
            [(at(9), at(9, 10)), (at(9, 21), at(10))],
            [(row.start, row.end) for row in first.allocations],
        )
        self.assertEqual(49, sum(row.duration_minutes for row in first.allocations))
        self.assertTrue(
            all(
                row.start.second == row.end.second == 0
                and row.start.microsecond == row.end.microsecond == 0
                for row in first.allocations
            )
        )
        self.assertTrue(
            all(not allocator._overlaps(row.start, row.end, fixed["start"], fixed["end"])
                for row in first.allocations)
        )
        self.assertEqual(0, first.unallocated_capacity.total_minutes)
        self.assertEqual((), first.unallocated_capacity.intervals)
        self.assertFalse(first.contested_time)

    def test_capacity_below_evidence_minimum_emits_no_token_entry(self):
        demand = activity("long-workstream", 120, start=at(9), end=at(10))
        demand["effort"] = {"min": 30, "recommended": 120, "max": 180}
        result = allocator.allocate_work(
            [demand],
            [allocator.FixedBlock("fixed", at(9, 20), at(10), "existing_clockify")],
        )
        self.assertEqual([], list(result.allocations))
        self.assertEqual(1, len(result.contested_time))
        self.assertEqual(0, result.contested_time[0].allocated_minutes)
        self.assertEqual(120, result.contested_time[0].unallocated_minutes)

    def test_only_explicit_semantic_match_is_covered_by_existing(self):
        result = allocator.allocate_work(
            [activity("alpha", 30)],
            [{"id": "existing", "start": at(9), "end": at(10), "activity_id": "alpha"}],
        )

        self.assertFalse(result.allocations)
        self.assertEqual("existing", result.covered_by_existing[0].fixed_block_id)

    def test_activity_level_evidence_id_only_binds_unambiguous_analyzer_spans(self):
        raw = activity("alpha", 30)
        raw["evidence_ids"] = ["ev-alpha"]
        for span in raw["evidence_spans"]:
            span.pop("evidence_id")

        result = allocator.allocate_work([raw])

        self.assertEqual(("ev-alpha", "ev-alpha"), result.allocations[0].evidence_ids)

    def test_multiple_allowed_intervals_do_not_bridge_an_overnight_gap(self):
        raw = activity("multi-day", 150)
        raw.pop("allowed_envelope")
        raw["allowed_intervals"] = [
            {"start": on(1, 16), "end": on(1, 17)},
            {"start": on(2, 9), "end": on(2, 10)},
        ]
        fixed = {"id": "day-two-meeting", "start": on(2, 9, 15), "end": on(2, 9, 30)}

        result = allocator.allocate_work([raw], [fixed])

        self.assertEqual(105, sum(row.duration_minutes for row in result.allocations))
        self.assertEqual(
            [(on(1, 16), on(1, 17)), (on(2, 9), on(2, 9, 15)), (on(2, 9, 30), on(2, 10))],
            [(row.start, row.end) for row in result.allocations],
        )
        self.assertTrue(all(row.end <= on(1, 17) or row.start >= on(2, 9) for row in result.allocations))
        self.assertEqual(45, result.contested_time[0].unallocated_minutes)
        allocator.validate_strict_non_overlap(
            [allocator.FixedBlock("day-two-meeting", on(2, 9, 15), on(2, 9, 30)), *result.allocations]
        )


if __name__ == "__main__":
    unittest.main()
