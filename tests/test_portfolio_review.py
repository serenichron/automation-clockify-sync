import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "clockify_portfolio_review.py"
SPEC = importlib.util.spec_from_file_location("clockify_portfolio_review", SCRIPT)
portfolio = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(portfolio)


class PortfolioReviewTests(unittest.TestCase):
    def test_split_minutes_preserves_total_and_weighting(self):
        result = portfolio._split_minutes(60, [10, 20, 30])

        self.assertEqual([10, 20, 30], result)
        self.assertEqual(60, sum(result))

    def test_package_merged_activity_preserves_source_allocated_minutes(self):
        source_activities = [
            {"activity_id": "act-one", "evidence_ids": ["ev-one"]},
            {"activity_id": "act-two", "evidence_ids": ["ev-two"]},
        ]
        source_proposals = [
            {
                "activity_id": "act-one",
                "start": "2026-07-10T09:00+03:00",
                "end": "2026-07-10T09:20+03:00",
                "duration_minutes": 20,
            },
            {
                "activity_id": "act-two",
                "start": "2026-07-10T10:00+03:00",
                "end": "2026-07-10T10:40+03:00",
                "duration_minutes": 40,
            },
        ]
        reviewed = {
            "activities": [{
                "activity_id": "act-merged",
                "evidence_ids": ["ev-one", "ev-two"],
                "action": "Rebuilt",
                "object": "Clockify review workflow",
                "outcome": "produced invoice-ready July entries",
                "effort": {"recommended_minutes": 60},
                "semantic_confidence": "high",
                "project_recommendation": {
                    "name": "Serenichron Level 2",
                    "prefix": "SC",
                    "tag_names": ["Processes"],
                },
            }],
            "exceptions": [],
            "omissions": [],
        }

        rows, exceptions, omissions = portfolio._package_review(
            reviewed, source_activities, source_proposals, {}
        )

        self.assertEqual(1, len(rows))
        self.assertEqual(60, rows[0]["duration_minutes"])
        self.assertEqual(2, len(rows[0]["allocation_segments"]))
        self.assertEqual(["act-one", "act-two"], rows[0]["source_activity_ids"])
        self.assertEqual([], exceptions)
        self.assertEqual([], omissions)

    def test_package_review_preserves_subminute_source_boundaries_and_seconds(self):
        source_activities = [{"activity_id": "act-one", "evidence_ids": ["ev-one"]}]
        source_proposals = [{
            "activity_id": "act-one",
            "start": "2026-07-10T09:00:17+03:00",
            "end": "2026-07-10T09:30:43+03:00",
            "duration_minutes": 30,
            "duration_seconds": 1826,
        }]
        reviewed = {
            "activities": [{
                "activity_id": "act-one", "evidence_ids": ["ev-one"],
                "action": "Rebuilt", "object": "review contract",
                "outcome": "preserved exact time", "effort": {"recommended_minutes": 30},
                "semantic_confidence": "high",
                "project_recommendation": {
                    "name": "Serenichron Level 2", "prefix": "SC",
                    "tag_names": ["Processes"],
                },
            }],
            "exceptions": [], "omissions": [],
        }

        rows, _exceptions, _omissions = portfolio._package_review(
            reviewed, source_activities, source_proposals, {}
        )

        self.assertEqual("2026-07-10T09:00:17+03:00", rows[0]["start"])
        self.assertEqual("2026-07-10T09:30:43+03:00", rows[0]["end"])
        self.assertEqual(1826, rows[0]["duration_seconds"])
        self.assertEqual(1826, rows[0]["allocation_segments"][0]["duration_seconds"])

    def test_allocate_from_pool_never_fills_gap(self):
        parse = portfolio._parse
        allocations = portfolio._allocate_from_pool(
            [
                (parse("2026-07-10T09:00+03:00"), parse("2026-07-10T09:20+03:00")),
                (parse("2026-07-10T10:00+03:00"), parse("2026-07-10T10:40+03:00")),
            ],
            [60],
        )

        self.assertEqual(2, len(allocations[0]))
        self.assertEqual("2026-07-10T09:20:00+03:00", allocations[0][0]["end"])
        self.assertEqual("2026-07-10T10:00:00+03:00", allocations[0][1]["start"])

    def test_group_accounting_all_retained(self):
        accounting = portfolio._group_accounting(
            [{"activity_id": "act-one", "evidence_ids": ["ev-one"]}],
            [{"duration_minutes": 30}],
            [{"duration_minutes": 30}],
            [],
            [],
        )

        self.assertEqual(30, accounting["source_minutes"])
        self.assertEqual(30, accounting["review_minutes"])
        self.assertEqual(0, accounting["excluded_minutes"])
        self.assertEqual([], accounting["exclusion_reasons"])

    def test_group_accounting_all_excluded_with_flash_reasons(self):
        accounting = portfolio._group_accounting(
            [{"activity_id": "act-one", "evidence_ids": ["ev-one", "ev-two"]}],
            [{"duration_minutes": 30}],
            [],
            [{"evidence_ids": ["ev-one"], "reason": "conflicting evidence"}],
            [{"evidence_ids": ["ev-two"], "reason": "autonomous background work"}],
        )

        self.assertEqual(30, accounting["excluded_minutes"])
        self.assertEqual([
            {"disposition": "exception", "reason": "conflicting evidence", "evidence_count": 1},
            {"disposition": "omission", "reason": "autonomous background work", "evidence_count": 1},
        ], accounting["exclusion_reasons"])

    def test_group_accounting_blocks_unreasoned_exclusion(self):
        with self.assertRaisesRegex(portfolio.PortfolioReviewError, "nonempty exception/omission reason"):
            portfolio._group_accounting(
                [{"activity_id": "act-one", "evidence_ids": ["ev-one"]}],
                [{"duration_minutes": 30}],
                [],
                [{"evidence_ids": ["ev-one"], "reason": ""}],
                [],
            )

    def test_group_accounting_blocks_analyzer_failure_as_exclusion(self):
        with self.assertRaisesRegex(portfolio.PortfolioReviewError, "cannot be counted"):
            portfolio._group_accounting(
                [{"activity_id": "act-one", "evidence_ids": ["ev-one"]}],
                [{"duration_minutes": 30}],
                [],
                [{
                    "kind": "analyzer_review_failure",
                    "evidence_ids": ["ev-one"],
                    "reason": "transport retries exhausted",
                }],
                [],
            )

    def test_failed_parent_bisects_and_does_not_repeat_successful_children(self):
        activities = [
            {"activity_id": "act-one", "evidence_ids": []},
            {"activity_id": "act-two", "evidence_ids": []},
        ]
        calls = []

        def reviewer(group):
            ids = tuple(row["activity_id"] for row in group)
            calls.append(ids)
            if len(group) == 2:
                return {
                    "activities": [],
                    "exceptions": [{
                        "kind": "analyzer_review_failure",
                        "evidence_ids": [],
                        "reason": "structural retry exhausted",
                    }],
                    "omissions": [],
                }
            return {"activities": [{"activity_id": ids[0]}], "exceptions": [], "omissions": []}

        partitions = portfolio._review_with_bisection(
            activities, reviewer, events_by_id={}
        )

        self.assertEqual([("act-one", "act-two"), ("act-one",), ("act-two",)], calls)
        self.assertEqual([(1,), (2,)], [path for path, _, _ in partitions])
        self.assertEqual([("act-one",), ("act-two",)], [
            tuple(row["activity_id"] for row in group) for _, group, _ in partitions
        ])

    def test_single_activity_analyzer_failure_blocks_loudly(self):
        failure = {
            "activities": [],
            "exceptions": [{
                "kind": "analyzer_review_failure",
                "evidence_ids": ["ev-one"],
                "reason": "transport retries exhausted",
            }],
            "omissions": [],
        }

        with self.assertRaisesRegex(portfolio.PortfolioReviewError, "single source activity"):
            portfolio._review_with_bisection(
                [{"activity_id": "act-one", "evidence_ids": ["ev-one"]}],
                lambda group: failure,
                events_by_id={},
            )

    def test_single_activity_failure_uses_flash_recovery_before_blocking(self):
        failure = {
            "activities": [],
            "exceptions": [{
                "kind": "analyzer_review_failure",
                "evidence_ids": ["ev-one"],
                "reason": "structural retries exhausted",
            }],
            "omissions": [],
        }
        recovered = {
            "activities": [{"activity_id": "act-recovered"}],
            "exceptions": [],
            "omissions": [],
        }
        recovery_calls = []

        partitions = portfolio._review_with_bisection(
            [{"activity_id": "act-one", "evidence_ids": ["ev-one"]}],
            lambda group: failure,
            events_by_id={},
            single_activity_reviewer=lambda group: (
                recovery_calls.append(tuple(row["activity_id"] for row in group))
                or recovered
            ),
        )

        self.assertEqual([("act-one",)], recovery_calls)
        self.assertEqual(recovered, partitions[0][2])

    def test_single_activity_failure_can_carry_reviewed_source_after_recovery(self):
        failure = {
            "activities": [],
            "exceptions": [{
                "kind": "analyzer_review_failure",
                "evidence_ids": ["ev-one"],
                "reason": "opaque evidence IDs were copied incorrectly",
            }],
            "omissions": [],
        }

        partitions = portfolio._review_with_bisection(
            [{"activity_id": "act-one", "evidence_ids": ["ev-one"]}],
            lambda group: failure,
            events_by_id={},
            single_activity_reviewer=lambda group: failure,
            single_activity_fallback=lambda group: {
                "activities": [{
                    **group[0],
                    "portfolio_validation_status": "source_review_carried",
                }],
                "exceptions": [],
                "omissions": [],
            },
        )

        self.assertEqual(
            "source_review_carried",
            partitions[0][2]["activities"][0]["portfolio_validation_status"],
        )

    def test_portfolio_accounting_requires_top_level_equation(self):
        portfolio._portfolio_accounting(60, 45, 15)

        with self.assertRaisesRegex(portfolio.PortfolioReviewError, "exceed"):
            portfolio._portfolio_accounting(60, 61, -1)
        with self.assertRaisesRegex(portfolio.PortfolioReviewError, "does not balance"):
            portfolio._portfolio_accounting(60, 45, 0)


if __name__ == "__main__":
    unittest.main()
