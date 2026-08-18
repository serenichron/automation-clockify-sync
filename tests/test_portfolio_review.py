import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from scripts import clockify_portfolio_quality as quality
from scripts import clockify_portfolio_replay as replay
from scripts import clockify_post_approved_portfolio as poster
from scripts import evidence_ledger


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

    def test_package_review_keeps_one_minute_legacy_source_fully_legacy(self):
        source_activities = [{"activity_id": "act-one", "evidence_ids": ["ev-one"]}]
        source_proposals = [{
            "activity_id": "act-one",
            "start": "2026-07-10T09:00:00+03:00",
            "end": "2026-07-10T09:01:00+03:00",
            "duration_minutes": 1,
        }]
        reviewed = {
            "activities": [{
                "activity_id": "act-one",
                "evidence_ids": ["ev-one"],
                "action": "Reviewed",
                "object": "legacy portfolio row",
                "outcome": "retained one minute",
                "effort": {"recommended_minutes": 1},
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
        accounting = portfolio._group_accounting(
            source_activities, source_proposals, rows, exceptions, omissions
        )

        self.assertEqual(1, rows[0]["duration_minutes"])
        self.assertNotIn("duration_seconds", rows[0])
        self.assertNotIn("duration_seconds", rows[0]["allocation_segments"][0])
        self.assertEqual(1, accounting["review_minutes"])
        self.assertEqual(0, accounting["excluded_minutes"])

    def test_legacy_review_omits_all_exact_second_aggregates(self):
        source_activities = [{"activity_id": "act-one", "evidence_ids": ["ev-one"]}]
        analysis = {
            "activities": [{
                "activity_id": "act-one",
                "evidence_ids": ["ev-one"],
                "action": "Reviewed",
                "object": "legacy portfolio row",
                "outcome": "retained one minute",
                "effort": {"recommended_minutes": 1},
                "semantic_confidence": "high",
                "project_recommendation": {
                    "name": "Serenichron Level 2",
                    "prefix": "SC",
                    "tag_names": ["Processes"],
                },
            }],
        }
        proposal = {
            "activity_id": "act-one",
            "start": "2026-07-10T09:00:00+03:00",
            "end": "2026-07-10T09:01:00+03:00",
            "duration_minutes": 1,
        }
        event = {
            "evidence_id": "ev-one",
            "source_type": "fathom",
            "raw_source_span": {
                "start": proposal["start"], "end": proposal["end"],
            },
        }
        routing = {
            "session_routes": [],
            "meeting_routes": [{
                "project_name": "Serenichron Level 2",
                "prefix": "SC",
                "tag_names": ["Processes"],
            }],
        }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run"
            (run_dir / "evidence").mkdir(parents=True)
            (run_dir / "evidence" / "evidence-ledger.json").write_text(
                json.dumps({"events": [event]}), encoding="utf-8"
            )
            (run_dir / "proposals.json").write_text(
                json.dumps([proposal]), encoding="utf-8"
            )
            analysis_path = root / "analysis.json"
            analysis_path.write_text(json.dumps(analysis), encoding="utf-8")
            routing_path = root / "routing.json"
            routing_path.write_text(json.dumps(routing), encoding="utf-8")
            args = argparse.Namespace(
                run_dir=run_dir,
                analysis_fixture=analysis_path,
                routing=routing_path,
                output_dir=root / "review",
                cache=root / "cache.json",
                since="2026-07-01",
                until="2026-07-31",
                max_activities=20,
                workers=1,
            )
            endpoint = SimpleNamespace(model="deepseek-v4-flash:cloud", revision="rev")
            with mock.patch.object(
                portfolio.semantic_analyzer.AnalyzerEndpoint, "from_env", return_value=endpoint
            ):
                result = portfolio.run(args)

        self.assertNotIn("source_seconds", result)
        self.assertNotIn("review_seconds", result)
        self.assertNotIn("excluded_seconds", result)
        self.assertTrue(all(
            "source_seconds" not in group
            and "review_seconds" not in group
            and "excluded_seconds" not in group
            for group in result["groups"]
        ))

    def test_legacy_precision_survives_review_quality_replay_and_posting(self):
        event = evidence_ledger.evidence_event(
            "fathom",
            {"source_type": "fathom", "source_id": "legacy-meeting"},
            observed_at="2026-07-10T09:00:00+03:00",
            raw_source_span={
                "start": "2026-07-10T09:00:00+03:00",
                "end": "2026-07-10T09:01:00+03:00",
            },
            attributes={
                "title": "Legacy client review",
                "recorded_by_email": "vlad@serenichron.com",
                "meeting_id": "events/legacy-meeting",
            },
        )
        proposal = {
            "activity_id": "act-one",
            "start": "2026-07-10T09:00:00+03:00",
            "end": "2026-07-10T09:01:00+03:00",
            "duration_minutes": 1,
            "client_project": "Serenichron Level 2",
            "tag_names": ["Processes"],
        }
        analysis = {
            "activities": [{
                "activity_id": "act-one",
                "evidence_ids": [event.evidence_id],
                "action": "Reviewed",
                "object": "legacy portfolio row",
                "outcome": "retained one minute",
                "effort": {"recommended_minutes": 1},
                "semantic_confidence": "high",
                "semantic_reviewer_model": quality.REQUIRED_MODEL,
                "semantic_reviewer_revision": quality.REQUIRED_REVISION,
                "project_recommendation": {
                    "name": "Serenichron Level 2",
                    "prefix": "SC",
                    "tag_names": ["Processes"],
                },
            }],
        }
        routing = {
            "session_routes": [],
            "meeting_routes": [{
                "project_name": "Serenichron Level 2",
                "prefix": "SC",
                "tag_names": ["Processes"],
            }],
        }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "run"
            immutable = evidence_ledger.EvidenceLedger(
                (event,),
                {"fathom": {
                    "status": "complete", "expected_count": 1, "observed_count": 1,
                }},
            )
            ledger_document = {
                "schema_version": immutable.manifest.schema_version,
                "manifest": immutable.manifest.document(),
                "events": [event.document()],
            }
            (run_dir / "evidence").mkdir(parents=True)
            (run_dir / "evidence" / "evidence-ledger.json").write_text(
                json.dumps(ledger_document), encoding="utf-8"
            )
            (run_dir / "proposals.json").write_text(
                json.dumps([proposal]), encoding="utf-8"
            )
            analysis_path = root / "analysis.json"
            analysis_path.write_text(json.dumps(analysis), encoding="utf-8")
            routing_path = root / "routing.json"
            routing_path.write_text(json.dumps(routing), encoding="utf-8")
            args = argparse.Namespace(
                run_dir=run_dir,
                analysis_fixture=analysis_path,
                routing=routing_path,
                output_dir=root / "review",
                cache=root / "cache.json",
                since="2026-07-01",
                until="2026-07-31",
                max_activities=20,
                workers=1,
            )
            endpoint = SimpleNamespace(
                model=quality.REQUIRED_MODEL, revision=quality.REQUIRED_REVISION,
            )
            with (
                mock.patch.object(
                    portfolio.semantic_analyzer.AnalyzerEndpoint, "from_env", return_value=endpoint
                ),
                mock.patch.object(portfolio.semantic_analyzer, "_require_private_text_approval"),
            ):
                review = portfolio.run(args)

            quality_report = quality.audit(
                review, ledger_document, source_proposals=[proposal], routing=routing,
            )
            (run_dir / "semantic-analysis.json").write_text(
                json.dumps({"analyzer_cache": {"records": []}}), encoding="utf-8"
            )
            (run_dir / "work-accounting-result.json").write_text(
                json.dumps({"proposals": []}), encoding="utf-8"
            )
            (run_dir / "fathom-reconciliation.json").write_text("[]", encoding="utf-8")
            review_path = root / "portfolio-review.json"
            review_path.write_text(json.dumps(review), encoding="utf-8")
            quality_path = root / "portfolio-quality.json"
            quality_path.write_text(json.dumps(quality_report), encoding="utf-8")
            repair_path = root / "portfolio-repair.json"
            repair_path.write_text(json.dumps({"repair": {
                "model": quality.REQUIRED_MODEL, "revision": quality.REQUIRED_REVISION,
            }}), encoding="utf-8")
            seal = replay.seal(
                run_dir=run_dir,
                review=review_path,
                repair=repair_path,
                quality=quality_path,
                routing=routing_path,
            )
            replay_report = replay.verify(
                seal,
                run_dir=run_dir,
                review=review_path,
                repair=repair_path,
                quality=quality_path,
                routing=routing_path,
            )
            posting_plans = poster._plans(review, {
                ("Serenichron Level 2", ("Processes",)): {
                    "project_id": "project-1", "tag_ids": ["tag-1"], "billable": True,
                },
            })

        self.assertNotIn("source_seconds", review)
        self.assertEqual("pass", quality_report["status"])
        self.assertEqual("pass", replay_report["status"])
        self.assertEqual(60, posting_plans[0]["duration_seconds"])
        self.assertEqual(1, posting_plans[0]["duration_minutes"])

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

    def test_exact_second_split_does_not_create_floor_minute_exclusion(self):
        accounting = portfolio._group_accounting(
            [{"activity_id": "act-one", "evidence_ids": ["ev-one"]}],
            [{
                "start": "2026-07-10T09:00:00+03:00",
                "end": "2026-07-10T09:05:01+03:00",
                "duration_minutes": 5,
                "duration_seconds": 301,
            }],
            [
                {"duration_minutes": 2, "duration_seconds": 151},
                {"duration_minutes": 2, "duration_seconds": 150},
            ],
            [],
            [],
        )

        self.assertEqual(301, accounting["source_seconds"])
        self.assertEqual(301, accounting["review_seconds"])
        self.assertEqual(0, accounting["excluded_seconds"])
        self.assertEqual(5, accounting["source_minutes"])
        self.assertEqual(5, accounting["review_minutes"])
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
