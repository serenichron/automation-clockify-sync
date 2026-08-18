import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import clockify_post_approved_portfolio as poster
from scripts import clockify_portfolio_replay as portfolio_replay


class ClockifyPortfolioPostTests(unittest.TestCase):
    def test_posting_plan_preserves_subminute_approved_segments(self) -> None:
        portfolio = {
            "activities": [{
                "review_id": "pvi-0123456789abcdef01234567",
                "client_project": "Example Level 2",
                "tag_names": ["Technical development"],
                "description": "EX — Preserve exact recorded meeting time",
                "duration_minutes": 30,
                "duration_seconds": 1826,
                "allocation_segments": [{
                    "start": "2026-08-14T10:00:17+03:00",
                    "end": "2026-08-14T10:30:43+03:00",
                    "duration_minutes": 30,
                    "duration_seconds": 1826,
                }],
            }],
        }
        routes = {
            ("Example Level 2", ("Technical development",)): {
                "project_id": "project-1", "tag_ids": ["tag-1"], "billable": True,
            },
        }

        plan = poster._plans(portfolio, routes)[0]

        self.assertEqual("2026-08-14T07:00:17Z", plan["start"])
        self.assertEqual("2026-08-14T07:30:43Z", plan["end"])
        self.assertEqual(1826, plan["duration_seconds"])

    def test_posting_plan_derives_merged_display_minutes_from_seconds(self) -> None:
        portfolio = {
            "activities": [{
                "review_id": "pvi-0123456789abcdef01234567",
                "client_project": "Example Level 2",
                "tag_names": ["Technical development"],
                "description": "EX — Preserve exact recorded meeting time",
                "duration_minutes": 5,
                "duration_seconds": 301,
                "allocation_segments": [
                    {
                        "start": "2026-08-14T10:00:00+03:00",
                        "end": "2026-08-14T10:02:31+03:00",
                        "duration_minutes": 2,
                        "duration_seconds": 151,
                    },
                    {
                        "start": "2026-08-14T10:02:31+03:00",
                        "end": "2026-08-14T10:05:01+03:00",
                        "duration_minutes": 2,
                        "duration_seconds": 150,
                    },
                ],
            }],
        }
        routes = {
            ("Example Level 2", ("Technical development",)): {
                "project_id": "project-1", "tag_ids": ["tag-1"], "billable": True,
            },
        }

        plan = poster._plans(portfolio, routes)

        self.assertEqual(1, len(plan))
        self.assertEqual(301, plan[0]["duration_seconds"])
        self.assertEqual(5, plan[0]["duration_minutes"])

    def test_boundary_adjustment_recomputes_exact_duration_fields(self) -> None:
        plans = [
            {
                "review_id": "pvi-0123456789abcdef01234567",
                "segment_index": 0,
                "start": "2026-08-14T10:00:00Z",
                "end": "2026-08-14T10:10:30Z",
                "duration_minutes": 10,
                "duration_seconds": 630,
                "project_name": "Example Level 2",
                "description": "EX — Preserve exact recorded meeting time",
            },
            {
                "review_id": "pvi-0123456789abcdef01234567",
                "segment_index": 1,
                "start": "2026-08-14T10:10:30Z",
                "end": "2026-08-14T10:20:30Z",
                "duration_minutes": 10,
                "duration_seconds": 600,
                "project_name": "Example Level 2",
                "description": "EX — Preserve exact recorded meeting time",
            },
        ]
        live = [
            {"start": "2026-08-14T09:59:00Z", "end": "2026-08-14T10:00:17Z"},
            {"start": "2026-08-14T10:10:30Z", "end": "2026-08-14T10:10:31Z"},
        ]

        adjusted, _changes = poster._align_subminute_boundaries(plans, live, set())

        self.assertEqual([613, 617], [item["duration_seconds"] for item in adjusted])
        self.assertEqual([10, 10], [item["duration_minutes"] for item in adjusted])
        self.assertEqual(1230, sum(item["duration_seconds"] for item in adjusted))
        receipt = poster._receipt_item(adjusted[0], "entry-1", "created")
        self.assertEqual(613, receipt["duration_seconds"])
        self.assertEqual(10, receipt["duration_minutes"])

    def test_prior_receipt_recomputes_duration_fields_from_restored_bounds(self) -> None:
        plans = [
            {
                "review_id": "pvi-0123456789abcdef01234567",
                "segment_index": 0,
                "start": "2026-08-14T10:00:00Z",
                "end": "2026-08-14T10:10:30Z",
                "duration_minutes": 10,
                "duration_seconds": 630,
            },
            {
                "review_id": "pvi-0123456789abcdef01234567",
                "segment_index": 1,
                "start": "2026-08-14T10:10:30Z",
                "end": "2026-08-14T10:20:30Z",
                "duration_minutes": 10,
                "duration_seconds": 600,
            },
        ]
        receipt = {
            "portfolio_sha256": "approved-sha",
            "created": [
                {
                    "review_id": "pvi-0123456789abcdef01234567",
                    "segment_index": 0,
                    "start": "2026-08-14T10:00:17Z",
                    "end": "2026-08-14T10:10:30Z",
                    "duration_minutes": 10,
                    "duration_seconds": 630,
                },
                {
                    "review_id": "pvi-0123456789abcdef01234567",
                    "segment_index": 1,
                    "start": "2026-08-14T10:10:30Z",
                    "end": "2026-08-14T10:20:47Z",
                    "duration_minutes": 10,
                    "duration_seconds": 600,
                },
            ],
            "already_existing": [],
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text(json.dumps(receipt), encoding="utf-8")
            restored, _adjustments = poster._apply_prior_receipt(
                plans, path, "approved-sha"
            )

        self.assertEqual([613, 617], [item["duration_seconds"] for item in restored])
        self.assertEqual([10, 10], [item["duration_minutes"] for item in restored])

    def test_prior_dry_run_adjustments_restore_adjusted_bounds(self) -> None:
        plans = [{
            "review_id": "pvi-0123456789abcdef01234567",
            "segment_index": 0,
            "start": "2026-08-14T10:00:00Z",
            "end": "2026-08-14T10:10:30Z",
            "duration_minutes": 10,
            "duration_seconds": 630,
        }]
        receipt = {
            "portfolio_sha256": "approved-sha",
            "created": [],
            "already_existing": [],
            "boundary_adjustments": [{
                "review_id": "pvi-0123456789abcdef01234567",
                "segment_index": 0,
                "original_start": "2026-08-14T10:00:00Z",
                "original_end": "2026-08-14T10:10:30Z",
                "posted_start": "2026-08-14T10:00:17Z",
                "posted_end": "2026-08-14T10:10:30Z",
            }],
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text(json.dumps(receipt), encoding="utf-8")
            restored, adjustments = poster._apply_prior_receipt(
                plans, path, "approved-sha"
            )

        self.assertEqual("2026-08-14T10:00:17Z", restored[0]["start"])
        self.assertEqual("2026-08-14T10:10:30Z", restored[0]["end"])
        self.assertEqual(613, restored[0]["duration_seconds"])
        self.assertEqual(10, restored[0]["duration_minutes"])
        self.assertEqual(receipt["boundary_adjustments"], adjustments)

    def test_prior_executed_receipt_confirms_adjusted_bounds(self) -> None:
        plans = [{
            "review_id": "pvi-0123456789abcdef01234567",
            "segment_index": 0,
            "start": "2026-08-14T10:00:00Z",
            "end": "2026-08-14T10:10:30Z",
            "duration_minutes": 10,
            "duration_seconds": 630,
        }]
        receipt = {
            "portfolio_sha256": "approved-sha",
            "created": [{
                "review_id": "pvi-0123456789abcdef01234567",
                "segment_index": 0,
                "start": "2026-08-14T10:00:17Z",
                "end": "2026-08-14T10:10:30Z",
            }],
            "already_existing": [],
            "boundary_adjustments": [{
                "review_id": "pvi-0123456789abcdef01234567",
                "segment_index": 0,
                "original_start": "2026-08-14T10:00:00Z",
                "original_end": "2026-08-14T10:10:30Z",
                "posted_start": "2026-08-14T10:00:17Z",
                "posted_end": "2026-08-14T10:10:30Z",
            }],
        }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text(json.dumps(receipt), encoding="utf-8")
            restored, _adjustments = poster._apply_prior_receipt(
                plans, path, "approved-sha"
            )

        self.assertEqual("2026-08-14T10:00:17Z", restored[0]["start"])
        self.assertEqual(613, restored[0]["duration_seconds"])

    def test_legacy_receipt_replay_rejects_changed_timestamp_seconds(self) -> None:
        portfolio = {
            "activities": [{
                "review_id": "pvi-0123456789abcdef01234567",
                "client_project": "Example Level 2",
                "tag_names": ["Technical development"],
                "description": "EX — Preserve recorded meeting time",
                "duration_minutes": 1,
                "allocation_segments": [{
                    "start": "2026-08-14T10:00:00+03:00",
                    "end": "2026-08-14T10:01:00+03:00",
                    "duration_minutes": 1,
                }],
            }],
        }
        routes = {
            ("Example Level 2", ("Technical development",)): {
                "project_id": "project-1", "tag_ids": ["tag-1"], "billable": True,
            },
        }
        plans = poster._plans(portfolio, routes)
        receipt = {
            "portfolio_sha256": "approved-sha",
            "created": [{
                "review_id": "pvi-0123456789abcdef01234567",
                "segment_index": 1,
                "start": "2026-08-14T07:00:00Z",
                "end": "2026-08-14T07:01:01Z",
            }],
            "already_existing": [],
        }

        self.assertEqual(60, plans[0]["approved_duration_seconds"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text(json.dumps(receipt), encoding="utf-8")
            with self.assertRaisesRegex(
                poster.PortfolioPostError, "approved duration"
            ):
                poster._apply_prior_receipt(plans, path, "approved-sha")

    def test_posting_plan_rejects_malformed_allocation_segment_as_domain_error(self) -> None:
        portfolio = {
            "activities": [{
                "review_id": "pvi-0123456789abcdef01234567",
                "client_project": "Example Level 2",
                "tag_names": ["Technical development"],
                "description": "EX — Preserve exact recorded meeting time",
                "duration_minutes": 5,
                "duration_seconds": 301,
                "allocation_segments": ["malformed"],
            }],
        }
        routes = {
            ("Example Level 2", ("Technical development",)): {
                "project_id": "project-1", "tag_ids": ["tag-1"], "billable": True,
            },
        }

        with self.assertRaisesRegex(
            poster.PortfolioPostError, "invalid allocation segment"
        ):
            poster._plans(portfolio, routes)

    def test_posting_plan_merges_contiguous_segments_with_equivalent_offsets(self) -> None:
        portfolio = {
            "activities": [{
                "review_id": "pvi-0123456789abcdef01234567",
                "client_project": "Example Level 2",
                "tag_names": ["Technical development"],
                "description": "EX — Preserve exact recorded meeting time",
                "duration_minutes": 5,
                "duration_seconds": 301,
                "allocation_segments": [
                    {
                        "start": "2026-08-14T10:00:00+03:00",
                        "end": "2026-08-14T10:02:31+03:00",
                        "duration_minutes": 2,
                        "duration_seconds": 151,
                    },
                    {
                        "start": "2026-08-14T07:02:31Z",
                        "end": "2026-08-14T07:05:01Z",
                        "duration_minutes": 2,
                        "duration_seconds": 150,
                    },
                ],
            }],
        }
        routes = {
            ("Example Level 2", ("Technical development",)): {
                "project_id": "project-1", "tag_ids": ["tag-1"], "billable": True,
            },
        }

        plan = poster._plans(portfolio, routes)

        self.assertEqual(1, len(plan))
        self.assertEqual(301, plan[0]["duration_seconds"])
        self.assertEqual(5, plan[0]["duration_minutes"])

    def test_post_gate_requires_clean_flash_validated_replay_bound_repair(self) -> None:
        portfolio = {
            "external_writes": False,
            "repair": {"status": "complete", "unresolved_wording": []},
            "activities": [{"validation_status": "flash_validated"}],
        }
        quality = {"status": "pass"}
        replay = {
            "status": "pass",
            "identity": {"artifacts": {
                "repair": portfolio_replay._digest(portfolio),
                "quality": portfolio_replay._digest(quality),
            }},
        }

        poster._verify_approved_artifacts(portfolio, quality, replay)

        portfolio["activities"][0]["validation_status"] = (
            "source_semantic_review_carried_after_flash_contract_failure"
        )
        replay["identity"]["artifacts"]["repair"] = portfolio_replay._digest(portfolio)
        with self.assertRaisesRegex(
            poster.PortfolioPostError, "Flash portfolio validation"
        ):
            poster._verify_approved_artifacts(portfolio, quality, replay)

        portfolio["activities"][0]["validation_status"] = "flash_validated"
        portfolio["repair"]["status"] = "complete_with_warnings"
        replay["identity"]["artifacts"]["repair"] = portfolio_replay._digest(portfolio)
        with self.assertRaisesRegex(poster.PortfolioPostError, "cleanly completed"):
            poster._verify_approved_artifacts(portfolio, quality, replay)

        portfolio["repair"]["status"] = "complete"
        replay["identity"]["artifacts"]["repair"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(poster.PortfolioPostError, "not bound"):
            poster._verify_approved_artifacts(portfolio, quality, replay)

    def test_live_conflict_query_pads_the_approved_portfolio_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            portfolio_path = root / "portfolio.json"
            quality_path = root / "quality.json"
            routing_path = root / "routing.json"
            receipt_path = root / "receipt.json"
            replay_path = root / "replay.json"
            portfolio = {
                "external_writes": False,
                "repair": {"status": "complete", "unresolved_wording": []},
                "activities": [{
                    "review_id": "pvi-0123456789abcdef01234567",
                    "client_project": "Example Level 2",
                    "tag_names": ["Technical development"],
                    "description": "EX — Delivered bounded portfolio posting coverage",
                    "duration_minutes": 30,
                    "validation_status": "flash_validated",
                    "allocation_segments": [{
                        "start": "2026-08-14T10:00:00+03:00",
                        "end": "2026-08-14T10:30:00+03:00",
                        "duration_minutes": 30,
                    }],
                }],
            }
            portfolio_path.write_text(json.dumps(portfolio), encoding="utf-8")
            quality = {"status": "pass"}
            quality_path.write_text(json.dumps(quality), encoding="utf-8")
            replay_path.write_text(json.dumps({
                "status": "pass",
                "identity": {"artifacts": {
                    "repair": portfolio_replay._digest(portfolio),
                    "quality": portfolio_replay._digest(quality),
                }},
            }), encoding="utf-8")
            routing_path.write_text(json.dumps({
                "clockify_user_id": "user-1",
                "session_routes": [{
                    "project_name": "Example Level 2",
                    "tag_names": ["Technical development"],
                    "project_suffix": "123456",
                    "tag_suffixes": ["654321"],
                }],
            }), encoding="utf-8")
            calls: list[str] = []

            def paged(path: str, _api_key: str, *, timeout_seconds: int):
                self.assertEqual(45, timeout_seconds)
                calls.append(path)
                if path.startswith("/workspaces/workspace-1/projects"):
                    return [{"id": "project-123456"}]
                if path.startswith("/workspaces/workspace-1/tags"):
                    return [{"id": "tag-654321"}]
                return []

            args = argparse.Namespace(
                portfolio=portfolio_path,
                quality_report=quality_path,
                replay_integrity=replay_path,
                routing=routing_path,
                receipt=receipt_path,
                prior_receipt=None,
                expected_portfolio_sha256=hashlib.sha256(
                    portfolio_path.read_bytes()
                ).hexdigest(),
                execute=False,
            )
            with (
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "secret",
                    "CLOCKIFY_WORKSPACE_ID": "workspace-1",
                }),
                mock.patch.object(poster, "_paged", side_effect=paged),
            ):
                poster.run(args)

            self.assertIn(
                "/workspaces/workspace-1/user/user-1/time-entries?"
                "start=2026-08-13T07%3A00%3A00Z&end=2026-08-15T07%3A30%3A00Z",
                calls,
            )


if __name__ == "__main__":
    unittest.main()
