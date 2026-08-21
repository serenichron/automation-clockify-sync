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
    def _write_posting_fixture(
        self, root: Path, *, allocation_segments: list[dict[str, object]] | None = None
    ) -> argparse.Namespace:
        portfolio_path = root / "portfolio.json"
        quality_path = root / "quality.json"
        routing_path = root / "routing.json"
        replay_path = root / "replay.json"
        receipt_path = root / "receipt.json"
        segments = allocation_segments or [{
            "start": "2026-08-14T10:00:00Z", "end": "2026-08-14T10:10:00Z",
            "duration_minutes": 10,
        }]
        portfolio = {
            "external_writes": False,
            "repair": {"status": "complete", "unresolved_wording": []},
            "activities": [{
                "review_id": "review-a", "client_project": "Example Level 2",
                "tag_names": ["Technical development"],
                "description": "EX — Approved work", "duration_minutes": sum(
                    int(segment["duration_minutes"]) for segment in segments
                ),
                "validation_status": "flash_validated", "allocation_segments": segments,
            }],
        }
        quality = {"status": "pass"}
        portfolio_path.write_text(json.dumps(portfolio), encoding="utf-8")
        quality_path.write_text(json.dumps(quality), encoding="utf-8")
        replay_path.write_text(json.dumps({
            "status": "pass", "identity": {"artifacts": {
                "repair": portfolio_replay._digest(portfolio),
                "quality": portfolio_replay._digest(quality),
            }},
        }), encoding="utf-8")
        routing_path.write_text(json.dumps({
            "clockify_user_id": "user-1", "session_routes": [{
                "project_name": "Example Level 2",
                "tag_names": ["Technical development"], "project_suffix": "123456",
                "tag_suffixes": ["654321"],
            }],
        }), encoding="utf-8")
        return argparse.Namespace(
            portfolio=portfolio_path, quality_report=quality_path,
            replay_integrity=replay_path, routing=routing_path, receipt=receipt_path,
            prior_receipt=None,
            expected_portfolio_sha256=hashlib.sha256(portfolio_path.read_bytes()).hexdigest(),
            execute=False,
        )

    def _clockify_entry(
        self, entry_id: str, start: str, end: str, *, project_id: str = "project-123456",
        tag_ids: list[str] | None = None, description: str = "EX — Approved work",
    ) -> dict[str, object]:
        return {
            "id": entry_id, "timeInterval": {"start": start, "end": end},
            "projectId": project_id, "tagIds": tag_ids or ["tag-654321"],
            "description": description,
        }

    def _paged_with_live(self, live_entries: list[dict[str, object]]):
        def paged(path: str, _api_key: str, *, timeout_seconds: int):
            self.assertEqual(45, timeout_seconds)
            if path.startswith("/workspaces/workspace-1/projects"):
                return [{"id": "project-123456"}]
            if path.startswith("/workspaces/workspace-1/tags"):
                return [{"id": "tag-654321"}]
            return live_entries
        return paged

    def test_normalized_snapshot_digest_is_order_independent_and_covers_live_identity(self) -> None:
        entries = [
            {
                "id": "entry-b", "start": "2026-08-14T10:10:00+00:00",
                "end": "2026-08-14T10:20:00Z", "project_id": "project-b",
                "tag_ids": ["tag-z", "tag-a"], "description": " Work B ",
            },
            {
                "id": "entry-a", "start": "2026-08-14T10:00:00Z",
                "end": "2026-08-14T10:10:00Z", "project_id": "project-a",
                "tag_ids": ["tag-b"], "description": "Work A",
            },
        ]
        reordered = [
            {**entries[0], "tag_ids": ["tag-a", "tag-z"]},
            entries[1],
        ]

        baseline = poster._normalized_snapshot_sha256(entries)
        self.assertEqual(baseline, poster._normalized_snapshot_sha256(reordered))
        for field, changed in (
            ("id", "entry-other"),
            ("start", "2026-08-14T10:00:01Z"),
            ("end", "2026-08-14T10:10:01Z"),
            ("project_id", "project-other"),
            ("tag_ids", ["tag-other"]),
            ("description", "Other work"),
        ):
            with self.subTest(field=field):
                mutated = [dict(entries[0]), dict(entries[1])]
                mutated[1][field] = changed
                self.assertNotEqual(baseline, poster._normalized_snapshot_sha256(mutated))

    def test_adjustment_digest_is_bound_to_blocker_snapshot(self) -> None:
        adjustments = [{
            "review_id": "review-a", "segment_index": 1,
            "original_start": "2026-08-14T10:00:00Z",
            "original_end": "2026-08-14T10:10:00Z",
            "posted_start": "2026-08-14T10:00:30Z",
            "posted_end": "2026-08-14T10:10:30Z",
            "algorithm": poster.BOUNDARY_ADJUSTMENT_ALGORITHM,
        }]
        left = poster._adjustment_digest("portfolio-sha", "blockers-a", adjustments)
        right = poster._adjustment_digest("portfolio-sha", "blockers-b", adjustments)

        self.assertNotEqual(left, right)

    def test_run_rederives_prior_candidate_from_fresh_live_readback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self._write_posting_fixture(root)
            prior_path = root / "prior.json"
            prior_path.write_text(json.dumps({
                "portfolio_sha256": args.expected_portfolio_sha256,
                "created": [{
                    "review_id": "review-a", "segment_index": 1,
                    "clockify_entry_id": "entry-prior",
                    "start": "2026-08-14T10:00:00Z",
                    "end": "2026-08-14T10:10:00Z", "duration_seconds": 600,
                }], "already_existing": [],
            }), encoding="utf-8")
            args.prior_receipt = prior_path
            live = [self._clockify_entry(
                "entry-prior", "2026-08-14T10:00:00Z", "2026-08-14T10:10:00Z"
            )]
            with (
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "secret", "CLOCKIFY_WORKSPACE_ID": "workspace-1",
                }),
                mock.patch.object(poster, "_paged", side_effect=self._paged_with_live(live)),
            ):
                receipt = poster.run(args)

        self.assertEqual("dry_run", receipt["status"])
        self.assertEqual(["entry-prior"], [
            item["clockify_entry_id"] for item in receipt["already_existing"]
        ])

    def test_run_dry_retry_keeps_fresh_derivation_evidence_stable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args = self._write_posting_fixture(Path(directory))
            with (
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "secret", "CLOCKIFY_WORKSPACE_ID": "workspace-1",
                }),
                mock.patch.object(poster, "_paged", side_effect=self._paged_with_live([])),
            ):
                first = poster.run(args)
                second = poster.run(args)

        for field in (
            "live_snapshot_sha256", "blocker_snapshot_sha256",
            "boundary_adjustments", "boundary_adjustments_sha256",
        ):
            self.assertEqual(first[field], second[field])

    def test_run_accepts_nonreceipt_exact_entry_without_posting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args = self._write_posting_fixture(Path(directory))
            args.execute = True
            live = [self._clockify_entry(
                "entry-exact", "2026-08-14T10:00:00Z", "2026-08-14T10:10:00Z"
            )]
            with (
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "secret", "CLOCKIFY_WORKSPACE_ID": "workspace-1",
                }),
                mock.patch.object(poster, "_paged", side_effect=self._paged_with_live(live)),
                mock.patch.object(poster, "_request") as request,
            ):
                receipt = poster.run(args)

        self.assertEqual("complete", receipt["status"])
        self.assertEqual(["entry-exact"], [
            item["clockify_entry_id"] for item in receipt["already_existing"]
        ])
        request.assert_not_called()

    def test_run_rejects_multiple_exact_entries_and_reports_overlap_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args = self._write_posting_fixture(Path(directory))
            exact = self._clockify_entry(
                "entry-exact", "2026-08-14T10:00:00Z", "2026-08-14T10:10:00Z"
            )
            with (
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "secret", "CLOCKIFY_WORKSPACE_ID": "workspace-1",
                }),
                mock.patch.object(poster, "_paged", side_effect=self._paged_with_live([
                    exact, {**exact, "id": "entry-exact-2"},
                ])),
            ):
                with self.assertRaisesRegex(
                    poster.PortfolioPostError, "multiple exact Clockify entries"
                ):
                    poster.run(args)

            conflict = self._clockify_entry(
                "entry-blocker", "2026-08-14T10:01:00Z", "2026-08-14T10:02:00Z",
                description="EX — Unrelated blocker",
            )
            with (
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "secret", "CLOCKIFY_WORKSPACE_ID": "workspace-1",
                }),
                mock.patch.object(poster, "_paged", side_effect=self._paged_with_live([conflict])),
            ):
                with self.assertRaisesRegex(
                    poster.PortfolioPostError,
                    r"review-a 2026-08-14T10:00:00Z\.\.2026-08-14T10:10:00Z.*"
                    r"EX — Approved work.*entry-blocker "
                    r"2026-08-14T10:01:00Z\.\.2026-08-14T10:02:00Z.*"
                    r"EX — Unrelated blocker",
                ):
                    poster.run(args)

    def test_run_rejects_candidate_and_nonreceipt_duplicate_exact_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self._write_posting_fixture(root)
            prior_path = root / "prior.json"
            prior_path.write_text(json.dumps({
                "portfolio_sha256": args.expected_portfolio_sha256,
                "created": [{
                    "review_id": "review-a", "segment_index": 1,
                    "clockify_entry_id": "entry-prior",
                }], "already_existing": [],
            }), encoding="utf-8")
            args.prior_receipt = prior_path
            live = [
                self._clockify_entry(
                    "entry-prior", "2026-08-14T10:00:00Z", "2026-08-14T10:10:00Z"
                ),
                self._clockify_entry(
                    "entry-other", "2026-08-14T10:00:00Z", "2026-08-14T10:10:00Z"
                ),
            ]
            with (
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "secret", "CLOCKIFY_WORKSPACE_ID": "workspace-1",
                }),
                mock.patch.object(poster, "_paged", side_effect=self._paged_with_live(live)),
            ):
                with self.assertRaisesRegex(
                    poster.PortfolioPostError, "multiple exact Clockify entries"
                ):
                    poster.run(args)

    def test_run_retry_posts_only_unfulfilled_plan_after_interruption(self) -> None:
        segments = [
            {"start": "2026-08-14T10:00:00Z", "end": "2026-08-14T10:10:00Z", "duration_minutes": 10},
            {"start": "2026-08-14T10:20:00Z", "end": "2026-08-14T10:30:00Z", "duration_minutes": 10},
        ]
        with tempfile.TemporaryDirectory() as directory:
            args = self._write_posting_fixture(Path(directory), allocation_segments=segments)
            args.execute = True
            with (
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "secret", "CLOCKIFY_WORKSPACE_ID": "workspace-1",
                }),
                mock.patch.object(poster, "_paged", side_effect=self._paged_with_live([])),
                mock.patch.object(poster, "_request", side_effect=[
                    {"id": "entry-first"}, poster.PortfolioPostError("write uncertain"),
                ]),
            ):
                with self.assertRaisesRegex(poster.PortfolioPostError, "write uncertain"):
                    poster.run(args)
            interrupted = json.loads(args.receipt.read_text(encoding="utf-8"))

            first_live = [self._clockify_entry(
                "entry-first", "2026-08-14T10:00:00Z", "2026-08-14T10:10:00Z"
            )]
            with (
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "secret", "CLOCKIFY_WORKSPACE_ID": "workspace-1",
                }),
                mock.patch.object(poster, "_paged", side_effect=self._paged_with_live(first_live)),
                mock.patch.object(poster, "_request", return_value={"id": "entry-second"}) as retry_post,
            ):
                completed = poster.run(args)

        self.assertEqual(["entry-first"], [
            item["clockify_entry_id"] for item in interrupted["created"]
        ])
        self.assertEqual(1, retry_post.call_count)
        self.assertEqual("complete", completed["status"])
        self.assertEqual(2, len(completed["created"] + completed["already_existing"]))

    def test_run_rejects_prior_candidate_when_fresh_blockers_change_derived_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self._write_posting_fixture(root)
            prior_path = root / "prior.json"
            prior_path.write_text(json.dumps({
                "portfolio_sha256": args.expected_portfolio_sha256,
                "created": [{
                    "review_id": "review-a", "segment_index": 1,
                    "clockify_entry_id": "entry-prior",
                }], "already_existing": [],
            }), encoding="utf-8")
            args.prior_receipt = prior_path
            live = [
                self._clockify_entry(
                    "entry-prior", "2026-08-14T10:00:00Z", "2026-08-14T10:10:00Z"
                ),
                self._clockify_entry(
                    "entry-blocker", "2026-08-14T09:59:00Z", "2026-08-14T10:00:30Z",
                    project_id="project-other", description="EX — Unrelated blocker",
                ),
            ]
            with (
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "secret", "CLOCKIFY_WORKSPACE_ID": "workspace-1",
                }),
                mock.patch.object(poster, "_paged", side_effect=self._paged_with_live(live)),
            ):
                with self.assertRaisesRegex(
                    poster.PortfolioPostError, "freshly derived plan"
                ):
                    poster.run(args)

    def test_run_rejects_prior_receipt_that_names_an_unrelated_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self._write_posting_fixture(root)
            prior_path = root / "prior.json"
            prior_path.write_text(json.dumps({
                "portfolio_sha256": args.expected_portfolio_sha256,
                "created": [{
                    "review_id": "review-a", "segment_index": 1,
                    "clockify_entry_id": "entry-blocker",
                }], "already_existing": [],
            }), encoding="utf-8")
            args.prior_receipt = prior_path
            blocker = self._clockify_entry(
                "entry-blocker", "2026-08-14T10:01:00Z", "2026-08-14T10:02:00Z",
                project_id="project-other", description="EX — Unrelated blocker",
            )
            with (
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "secret", "CLOCKIFY_WORKSPACE_ID": "workspace-1",
                }),
                mock.patch.object(poster, "_paged", side_effect=self._paged_with_live([blocker])),
            ):
                with self.assertRaisesRegex(
                    poster.PortfolioPostError, "semantic fields differ from approval"
                ):
                    poster.run(args)

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
                "segment_index": 1,
                "start": "2026-08-14T10:00:00Z",
                "end": "2026-08-14T10:10:30Z",
                "duration_minutes": 10,
                "duration_seconds": 630,
                "project_name": "Example Level 2",
                "description": "EX — Preserve exact recorded meeting time",
            },
            {
                "review_id": "pvi-0123456789abcdef01234567",
                "segment_index": 2,
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

    def _prior_candidates(self, receipt: dict[str, object], approved: set[tuple[str, int]]):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "receipt.json"
            path.write_text(json.dumps(receipt), encoding="utf-8")
            return poster._prior_receipt_candidates(path, "approved-sha", approved)

    def test_prior_candidate_parser_rejects_duplicate_clockify_entry_id(self) -> None:
        approved = {("review-a", 1), ("review-b", 1)}
        receipt = {
            "portfolio_sha256": "approved-sha",
            "created": [
                {"review_id": "review-a", "segment_index": 1, "clockify_entry_id": "entry-1"},
                {"review_id": "review-b", "segment_index": 1, "clockify_entry_id": "entry-1"},
            ],
            "already_existing": [],
        }
        with self.assertRaisesRegex(poster.PortfolioPostError, "duplicate Clockify entry ID"):
            self._prior_candidates(receipt, approved)

    def test_prior_candidate_parser_rejects_duplicate_posting_key(self) -> None:
        receipt = {
            "portfolio_sha256": "approved-sha",
            "created": [{"review_id": "review-a", "segment_index": 1, "clockify_entry_id": "entry-1"}],
            "already_existing": [{"review_id": "review-a", "segment_index": 1, "clockify_entry_id": "entry-2"}],
        }
        with self.assertRaisesRegex(poster.PortfolioPostError, "duplicate receipt key"):
            self._prior_candidates(receipt, {("review-a", 1)})

    def test_prior_candidate_parser_rejects_unknown_key(self) -> None:
        receipt = {
            "portfolio_sha256": "approved-sha",
            "created": [{"review_id": "review-other", "segment_index": 1, "clockify_entry_id": "entry-1"}],
            "already_existing": [],
        }
        with self.assertRaisesRegex(poster.PortfolioPostError, "unknown approved key"):
            self._prior_candidates(receipt, {("review-a", 1)})

    def test_prior_candidate_parser_rejects_missing_or_blank_clockify_entry_id(self) -> None:
        for entry_id in (None, "   "):
            with self.subTest(entry_id=entry_id):
                receipt = {
                    "portfolio_sha256": "approved-sha",
                    "created": [{"review_id": "review-a", "segment_index": 1, "clockify_entry_id": entry_id}],
                    "already_existing": [],
                }
                with self.assertRaisesRegex(poster.PortfolioPostError, "lacks a Clockify entry ID"):
                    self._prior_candidates(receipt, {("review-a", 1)})

    def test_prior_candidate_parser_rejects_non_list_disposition(self) -> None:
        receipt = {"portfolio_sha256": "approved-sha", "created": {}, "already_existing": []}
        with self.assertRaisesRegex(poster.PortfolioPostError, "items must be a list"):
            self._prior_candidates(receipt, {("review-a", 1)})

    def test_prior_candidate_parser_rejects_non_object_item(self) -> None:
        receipt = {"portfolio_sha256": "approved-sha", "created": ["invalid"], "already_existing": []}
        with self.assertRaisesRegex(poster.PortfolioPostError, "invalid item"):
            self._prior_candidates(receipt, {("review-a", 1)})

    def test_prior_candidate_parser_rejects_wrong_portfolio_digest(self) -> None:
        receipt = {"portfolio_sha256": "other-sha", "created": [], "already_existing": []}
        with self.assertRaisesRegex(poster.PortfolioPostError, "does not match the approved portfolio"):
            self._prior_candidates(receipt, set())

    def test_prior_candidate_parser_wraps_invalid_optional_timestamp_as_domain_error(self) -> None:
        for field in ("start", "end"):
            with self.subTest(field=field):
                receipt = {
                    "portfolio_sha256": "approved-sha",
                    "created": [{
                        "review_id": "review-a", "segment_index": 1,
                        "clockify_entry_id": "entry-1", field: "not-a-timestamp",
                    }],
                    "already_existing": [],
                }
                with self.assertRaisesRegex(poster.PortfolioPostError, "invalid audit timestamp"):
                    self._prior_candidates(receipt, {("review-a", 1)})

    def test_prior_candidate_parser_rejects_invalid_optional_duration_seconds(self) -> None:
        for duration in (True, 0, -1, "60"):
            with self.subTest(duration=duration):
                receipt = {
                    "portfolio_sha256": "approved-sha",
                    "created": [{
                        "review_id": "review-a", "segment_index": 1, "clockify_entry_id": "entry-1",
                        "duration_seconds": duration,
                    }],
                    "already_existing": [],
                }
                with self.assertRaisesRegex(poster.PortfolioPostError, "invalid duration seconds"):
                    self._prior_candidates(receipt, {("review-a", 1)})

    def test_prior_candidate_parser_preserves_audit_fields_without_authorization(self) -> None:
        receipt = {
            "portfolio_sha256": "approved-sha",
            "created": [{
                "review_id": "review-a", "segment_index": 1, "clockify_entry_id": "entry-1",
                "start": "2026-08-14T10:00:00+03:00", "end": "2026-08-14T10:10:00+03:00",
                "duration_seconds": 600,
            }],
            "already_existing": [],
            "boundary_adjustments": "not interpreted",
        }

        candidates = self._prior_candidates(receipt, {("review-a", 1)})

        self.assertEqual((
            poster.PriorReceiptCandidate(
                "review-a", 1, "entry-1", "created",
                "2026-08-14T07:00:00Z", "2026-08-14T07:10:00Z", 600,
            ),
        ), candidates)

    def _approved_prior_candidate_plan(self) -> dict[tuple[str, int], dict[str, object]]:
        return {("review-a", 1): {
            "review_id": "review-a", "segment_index": 1,
            "project_id": "project-a", "tag_ids": ["tag-a"],
            "description": "AA — Approved work",
        }}

    def _matching_prior_candidate_live_entry(self) -> dict[str, object]:
        return {
            "id": "entry-1", "start": "2026-08-14T10:00:00Z",
            "end": "2026-08-14T10:10:00Z", "project_id": "project-a",
            "tag_ids": ["tag-a"], "description": "AA — Approved work",
        }

    def _prior_candidate(self) -> poster.PriorReceiptCandidate:
        return poster.PriorReceiptCandidate(
            "review-a", 1, "entry-1", "created", None, None, None
        )

    def test_prior_candidate_cannot_remove_unrelated_blocker_by_id(self) -> None:
        candidate = poster.PriorReceiptCandidate(
            "review-a", 1, "blocker-1", "created", None, None, None
        )
        approved = self._approved_prior_candidate_plan()
        live = [{
            "id": "blocker-1", "start": "2026-08-14T10:00:30Z",
            "end": "2026-08-14T10:01:30Z", "project_id": "project-other",
            "tag_ids": ["tag-a"], "description": "AA — Approved work",
        }]

        with self.assertRaisesRegex(poster.PortfolioPostError, "semantic fields"):
            poster._resolve_prior_candidates([candidate], live, approved)

    def test_prior_candidate_resolver_rejects_duplicate_live_id(self) -> None:
        live = [self._matching_prior_candidate_live_entry(), self._matching_prior_candidate_live_entry()]

        with self.assertRaisesRegex(poster.PortfolioPostError, "duplicate entry ID"):
            poster._resolve_prior_candidates([self._prior_candidate()], live, self._approved_prior_candidate_plan())

    def test_prior_candidate_resolver_rejects_empty_live_id(self) -> None:
        live = [self._matching_prior_candidate_live_entry()]
        live[0]["id"] = " "

        with self.assertRaisesRegex(poster.PortfolioPostError, "empty entry ID"):
            poster._resolve_prior_candidates([self._prior_candidate()], live, self._approved_prior_candidate_plan())

    def test_prior_candidate_resolver_rejects_absent_receipt_id(self) -> None:
        live = [self._matching_prior_candidate_live_entry()]
        candidate = poster.PriorReceiptCandidate(
            "review-a", 1, "missing-entry", "created", None, None, None
        )

        with self.assertRaisesRegex(poster.PortfolioPostError, "absent from fresh readback"):
            poster._resolve_prior_candidates([candidate], live, self._approved_prior_candidate_plan())

    def test_prior_candidate_resolver_rejects_semantic_mismatches(self) -> None:
        for field, changed in (
            ("project_id", "project-other"),
            ("tag_ids", ["tag-other"]),
            ("description", "AA — Other work"),
        ):
            with self.subTest(field=field):
                live = [self._matching_prior_candidate_live_entry()]
                live[0][field] = changed
                with self.assertRaisesRegex(poster.PortfolioPostError, "semantic fields"):
                    poster._resolve_prior_candidates(
                        [self._prior_candidate()], live, self._approved_prior_candidate_plan()
                    )

    def test_prior_candidate_resolver_removes_only_matching_candidate_from_blockers(self) -> None:
        matching = self._matching_prior_candidate_live_entry()
        unrelated = {
            "id": "blocker-1", "start": "2026-08-14T11:00:00Z",
            "end": "2026-08-14T11:10:00Z", "project_id": "project-other",
            "tag_ids": ["tag-other"], "description": "Unrelated blocker",
        }
        trailing = {
            "id": "blocker-2", "start": "2026-08-14T12:00:00Z",
            "end": "2026-08-14T12:10:00Z", "project_id": "project-other",
            "tag_ids": [], "description": "Second blocker",
        }

        resolved, blockers = poster._resolve_prior_candidates(
            [self._prior_candidate()], [unrelated, matching, trailing],
            self._approved_prior_candidate_plan(),
        )

        self.assertEqual({("review-a", 1): matching}, resolved)
        self.assertEqual([unrelated, trailing], blockers)

    def _derived_prior_candidate_plan(self) -> dict[tuple[str, int], dict[str, object]]:
        return {("review-a", 1): {
            "review_id": "review-a", "segment_index": 1,
            "start": "2026-08-14T10:00:00Z", "end": "2026-08-14T10:10:00Z",
            "duration_seconds": 600, "project_id": "project-a",
            "tag_ids": ["tag-a"], "description": "AA — Approved work",
        }}

    def test_prior_candidate_accepts_exact_freshly_derived_live_entry(self) -> None:
        key = ("review-a", 1)
        live = {key: self._matching_prior_candidate_live_entry()}
        receipts = {key: self._prior_candidate()}

        accepted = poster._validate_prior_candidates(
            live, self._derived_prior_candidate_plan(), receipts
        )

        self.assertEqual(live, accepted)
        self.assertIsNot(live[key], accepted[key])

    def test_prior_candidate_rejects_same_duration_relocation_after_derivation(self) -> None:
        key = ("review-a", 1)
        live = {key: self._matching_prior_candidate_live_entry()}
        live[key]["start"] = "2026-08-14T10:01:00Z"
        live[key]["end"] = "2026-08-14T10:11:00Z"
        receipts = {key: self._prior_candidate()}

        with self.assertRaisesRegex(poster.PortfolioPostError, "freshly derived plan"):
            poster._validate_prior_candidates(live, self._derived_prior_candidate_plan(), receipts)

    def test_prior_candidate_rejects_same_duration_backward_relocation_after_derivation(self) -> None:
        key = ("review-a", 1)
        live = {key: self._matching_prior_candidate_live_entry()}
        live[key]["start"] = "2026-08-14T09:59:00Z"
        live[key]["end"] = "2026-08-14T10:09:00Z"
        receipts = {key: self._prior_candidate()}

        with self.assertRaisesRegex(poster.PortfolioPostError, "freshly derived plan"):
            poster._validate_prior_candidates(live, self._derived_prior_candidate_plan(), receipts)

    def test_prior_candidate_rejects_when_changed_blocker_state_changes_derived_bounds(self) -> None:
        key = ("review-a", 1)
        live = {key: self._matching_prior_candidate_live_entry()}
        changed_derivation = self._derived_prior_candidate_plan()
        changed_derivation[key]["start"] = "2026-08-14T10:00:30Z"
        changed_derivation[key]["end"] = "2026-08-14T10:10:30Z"
        receipts = {key: self._prior_candidate()}

        with self.assertRaisesRegex(poster.PortfolioPostError, "freshly derived plan"):
            poster._validate_prior_candidates(live, changed_derivation, receipts)

    def test_prior_candidate_rejects_receipt_audit_bounds_contradicting_live_readback(self) -> None:
        key = ("review-a", 1)
        live = {key: self._matching_prior_candidate_live_entry()}
        receipts = {key: poster.PriorReceiptCandidate(
            "review-a", 1, "entry-1", "created",
            "2026-08-14T10:01:00Z", "2026-08-14T10:10:00Z", None,
        )}

        with self.assertRaisesRegex(poster.PortfolioPostError, "audit bounds contradict"):
            poster._validate_prior_candidates(live, self._derived_prior_candidate_plan(), receipts)

    def test_prior_candidate_rejects_receipt_audit_duration_contradicting_live_readback(self) -> None:
        key = ("review-a", 1)
        live = {key: self._matching_prior_candidate_live_entry()}
        receipts = {key: poster.PriorReceiptCandidate(
            "review-a", 1, "entry-1", "created", None, None, 599,
        )}

        with self.assertRaisesRegex(poster.PortfolioPostError, "audit duration contradicts"):
            poster._validate_prior_candidates(live, self._derived_prior_candidate_plan(), receipts)

    def test_prior_candidate_rejects_missing_freshly_derived_key(self) -> None:
        key = ("review-a", 1)
        live = {key: self._matching_prior_candidate_live_entry()}
        receipts = {key: self._prior_candidate()}

        with self.assertRaisesRegex(poster.PortfolioPostError, "no freshly derived plan"):
            poster._validate_prior_candidates(live, {}, receipts)

    def test_prior_candidate_rejects_different_candidate_and_receipt_identity_sets(self) -> None:
        key = ("review-a", 1)
        live = {key: self._matching_prior_candidate_live_entry()}

        with self.assertRaisesRegex(poster.PortfolioPostError, "identity sets do not match"):
            poster._validate_prior_candidates(live, self._derived_prior_candidate_plan(), {})

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

    def test_receipt_derives_planned_minutes_from_authoritative_seconds(self) -> None:
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
                    "description": "EX — Preserve exact recorded meeting time",
                    "duration_minutes": 5,
                    "duration_seconds": 301,
                    "validation_status": "flash_validated",
                    "allocation_segments": [
                        {
                            "start": "2026-08-14T10:00:00+03:00",
                            "end": "2026-08-14T10:02:31+03:00",
                            "duration_minutes": 2,
                            "duration_seconds": 151,
                        },
                        {
                            "start": "2026-08-14T10:05:00+03:00",
                            "end": "2026-08-14T10:07:30+03:00",
                            "duration_minutes": 2,
                            "duration_seconds": 150,
                        },
                    ],
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

            def paged(path: str, _api_key: str, *, timeout_seconds: int):
                if path.startswith("/workspaces/workspace-1/projects"):
                    return [{"id": "project-123456"}]
                if path.startswith("/workspaces/workspace-1/tags"):
                    return [{"id": "tag-654321"}]
                return []

            with (
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "secret",
                    "CLOCKIFY_WORKSPACE_ID": "workspace-1",
                }),
                mock.patch.object(poster, "_paged", side_effect=paged),
            ):
                receipt = poster.run(args)

        self.assertEqual(5, receipt["planned_minutes"])
        self.assertEqual(301, receipt["planned_seconds"])


if __name__ == "__main__":
    unittest.main()
