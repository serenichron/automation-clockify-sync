import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import clockify_post_approved_portfolio as poster
from scripts import clockify_portfolio_replay as portfolio_replay
from scripts import posting_receipts
from scripts import reconciliation_manifest


class ClockifyPortfolioPostTests(unittest.TestCase):
    def _write_posting_fixture(
        self, root: Path, *, allocation_segments: list[dict[str, object]] | None = None,
        validation_status: str = "flash_validated",
        historical_entries: list[dict[str, object]] | None = None,
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
                "validation_status": validation_status, "allocation_segments": segments,
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
        correction_path = root / "corrections.json"
        coverage_path = root / "coverage.json"
        residual_path = root / "residual-exceptions.json"
        historical_path = root / "historical-receipt.json"
        for path in (correction_path, coverage_path, residual_path):
            path.write_text("{}\n", encoding="utf-8")
        historical_path.write_text(
            json.dumps({
                "schema_version": "clockify-historical-receipt/v1",
                "entries": historical_entries or [],
            }),
            encoding="utf-8",
        )
        identity = reconciliation_manifest.PeriodIdentity(
            member_id="user-1", workspace_id="workspace-1", timezone="Europe/Bucharest",
            since=dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
            until=dt.datetime(2026, 8, 16, tzinfo=dt.timezone.utc), revision=1,
        )
        artifacts = tuple(
            reconciliation_manifest.ArtifactIdentity(
                path=path.resolve(), schema_version="fixture/v1", compatibility_version="fixture/v1",
                digest="sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
            )
            for path in (
                portfolio_path, quality_path, replay_path, routing_path,
                correction_path, coverage_path, residual_path, historical_path,
            )
        )
        events_path = root / "period-events.jsonl"
        coordinator_store = reconciliation_manifest.CoordinatorEventStore(events_path)
        event_time = dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc)
        for sequence, event_type in enumerate((
            "period_opened", "collection_complete", "reconciliation_complete", "review_approved",
        )):
            coordinator_store.append(
                identity,
                event_type,
                {"artifacts": [artifact.document() for artifact in artifacts]} if sequence == 0 else {},
                occurred_at=event_time + dt.timedelta(minutes=sequence),
            )
        manifest_path = root / "period-manifest.json"
        manifest = reconciliation_manifest.ReconciliationCoordinator(identity, coordinator_store).derive()
        manifest_path.write_text(json.dumps(manifest.document()), encoding="utf-8")
        approval_events = root / "approval-events.jsonl"
        approval_id = "approval-1"
        operation_identity = posting_receipts.derive_operation_identity(
            operation="clockify_post", period_id=identity.period_id,
            workspace_id=identity.workspace_id, member_id=identity.member_id,
        )
        digest = lambda path: "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        posting_receipts.ApprovalReceiptStore(approval_events).append(posting_receipts.ApprovalReceipt(
            approval_id=approval_id, approver="board", approved_at="2026-08-01T00:00:00Z",
            expires_at="2030-08-01T00:00:00Z", operation="clockify_post",
            operation_identity=operation_identity, period_id=identity.period_id,
            period_start=identity.document()["since_utc"], period_end=identity.document()["until_utc"],
            workspace_id=identity.workspace_id, member_id=identity.member_id,
            portfolio_digest=digest(portfolio_path), quality_digest=digest(quality_path),
            replay_digest=digest(replay_path), routing_digest=digest(routing_path),
            correction_log_digest=digest(correction_path), coverage_digest=digest(coverage_path),
            residual_exception_digest=digest(residual_path),
            manifest_digest=manifest.manifest_digest, event_history_digest=manifest.events_digest,
            historical_receipt_digest=digest(historical_path),
            max_create_count=1,
        ))
        return argparse.Namespace(
            portfolio=portfolio_path, quality_report=quality_path,
            replay_integrity=replay_path, routing=routing_path, receipt=receipt_path,
            prior_receipt=None,
            expected_portfolio_sha256=hashlib.sha256(portfolio_path.read_bytes()).hexdigest(),
            execute=False,
            approval_receipt=approval_id, approval_events=approval_events,
            post_events=root / "post-events.jsonl", period_manifest=manifest_path,
            period_events=events_path, historical_receipt=historical_path,
        )

    def _clockify_entry(
        self, entry_id: str, start: str, end: str, *, project_id: str = "project-123456",
        tag_ids: list[str] | None = None, description: str = "EX — Approved work",
    ) -> dict[str, object]:
        return {
            "id": entry_id, "timeInterval": {"start": start, "end": end},
            "projectId": project_id, "tagIds": tag_ids or ["tag-654321"],
            "description": description, "billable": True,
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

    def test_execute_requires_approval_receipt_before_credentials_or_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args = self._write_posting_fixture(Path(directory))
            args.execute = True
            args.approval_receipt = None
            args.approval_events = None
            args.post_events = None
            args.period_manifest = None
            with (
                mock.patch.object(poster, "load_env_file") as credentials,
                mock.patch.object(poster, "_request") as request,
            ):
                with self.assertRaisesRegex(poster.PortfolioPostError, "approval receipt"):
                    poster.run(args)

        credentials.assert_not_called()
        request.assert_not_called()

    def test_execute_requires_period_event_history_before_credentials_or_network(self) -> None:
        """Removing the event-history gate must never allow a standalone manifest to authorize a POST."""
        with tempfile.TemporaryDirectory() as directory:
            args = self._write_posting_fixture(Path(directory))
            args.execute = True
            args.period_events = None
            with (
                mock.patch.object(poster, "load_env_file") as credentials,
                mock.patch.object(poster, "_request") as request,
            ):
                with self.assertRaisesRegex(poster.PortfolioPostError, "period event"):
                    poster.run(args)

        credentials.assert_not_called()
        request.assert_not_called()

    def test_execute_rejects_manifest_without_matching_verified_event_history(self) -> None:
        """Replacing the immutable coordinator history must invalidate the standalone manifest."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self._write_posting_fixture(root)
            args.execute = True
            args.period_events = root / "period-events.jsonl"
            args.period_events.write_text("", encoding="utf-8")
            live = [self._clockify_entry(
                "entry-exact", "2026-08-14T10:00:00Z", "2026-08-14T10:10:00Z"
            )]
            with (
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "secret", "CLOCKIFY_WORKSPACE_ID": "workspace-1",
                }),
                mock.patch.object(poster, "_paged", side_effect=self._paged_with_live(live)),
            ):
                with self.assertRaisesRegex(poster.PortfolioPostError, "verified event history"):
                    poster.run(args)

    def test_execute_rejects_verified_history_that_changed_after_approval(self) -> None:
        """An approval must bind the exact manifest and event-chain digest, not just its artifacts."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self._write_posting_fixture(root)
            args.execute = True
            manifest = reconciliation_manifest.ReconciliationManifest.from_document(
                json.loads(args.period_manifest.read_text(encoding="utf-8"))
            )
            event_store = reconciliation_manifest.CoordinatorEventStore(args.period_events)
            event_store.append(
                manifest.identity,
                "fathom_repair_complete",
                {},
                occurred_at=dt.datetime(2026, 8, 2, tzinfo=dt.timezone.utc),
            )
            changed_manifest = reconciliation_manifest.ReconciliationCoordinator(
                manifest.identity, event_store
            ).derive()
            args.period_manifest.write_text(
                json.dumps(changed_manifest.document()), encoding="utf-8"
            )
            live = [self._clockify_entry(
                "entry-exact", "2026-08-14T10:00:00Z", "2026-08-14T10:10:00Z"
            )]
            with (
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "secret", "CLOCKIFY_WORKSPACE_ID": "workspace-1",
                }),
                mock.patch.object(poster, "_paged", side_effect=self._paged_with_live(live)),
            ):
                with self.assertRaisesRegex(poster.PortfolioPostError, "history binding"):
                    poster.run(args)

    def test_execute_rejects_loaded_workspace_drift_before_network(self) -> None:
        """Changing the environment target must fail before any Clockify discovery request."""
        with tempfile.TemporaryDirectory() as directory:
            args = self._write_posting_fixture(Path(directory))
            args.execute = True
            with (
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "secret", "CLOCKIFY_WORKSPACE_ID": "workspace-other",
                }),
                mock.patch.object(poster, "_paged") as paged,
            ):
                with self.assertRaisesRegex(poster.PortfolioPostError, "workspace"):
                    poster.run(args)

        paged.assert_not_called()

    def test_execute_consumes_single_use_approval_only_after_complete_receipt(self) -> None:
        """Removing final consumption would leave a completed approval reusable for another POST."""
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
            ):
                receipt = poster.run(args)

            self.assertEqual("complete", receipt["status"])
            approval = posting_receipts.ApprovalReceiptStore(args.approval_events)
            issued = approval._state()[0][args.approval_receipt]
            with self.assertRaisesRegex(posting_receipts.PostingReceiptError, "consumed"):
                approval.require(
                    args.approval_receipt,
                    operation_identity=issued.operation_identity,
                    now=dt.datetime(2026, 8, 2, tzinfo=dt.timezone.utc),
                )

    def test_execute_recognizes_bound_historical_entry_with_description_drift_and_31_second_shift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args = self._write_posting_fixture(Path(directory), historical_entries=[{
                "review_id": "review-a", "segment_index": 1, "clockify_entry_id": "entry-history",
            }])
            args.execute = True
            live = [self._clockify_entry(
                "entry-history", "2026-08-14T10:00:31Z", "2026-08-14T10:10:31Z",
                description="legacy August wording",
            )]
            with (
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "secret", "CLOCKIFY_WORKSPACE_ID": "workspace-1",
                }),
                mock.patch.object(poster, "_paged", side_effect=self._paged_with_live(live)),
                mock.patch.object(poster, "_request") as request,
            ):
                receipt = poster.run(args)

        self.assertEqual(["entry-history"], [item["clockify_entry_id"] for item in receipt["already_existing"]])
        request.assert_not_called()

    def test_execute_rejects_bound_historical_entry_with_30_second_shift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args = self._write_posting_fixture(Path(directory), historical_entries=[{
                "review_id": "review-a", "segment_index": 1, "clockify_entry_id": "entry-history",
            }])
            args.execute = True
            live = [self._clockify_entry(
                "entry-history", "2026-08-14T10:00:30Z", "2026-08-14T10:10:30Z",
                description="legacy August wording",
            )]
            with (
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "secret", "CLOCKIFY_WORKSPACE_ID": "workspace-1",
                }),
                mock.patch.object(poster, "_paged", side_effect=self._paged_with_live(live)),
            ):
                with self.assertRaisesRegex(poster.PortfolioPostError, "bounds or duration"):
                    poster.run(args)

    def test_execute_rejects_two_missing_blocks_before_any_create(self) -> None:
        """Removing the approval-bound one-create cap would permit partial multi-entry mutation."""
        with tempfile.TemporaryDirectory() as directory:
            args = self._write_posting_fixture(Path(directory), allocation_segments=[
                {"start": "2026-08-14T10:00:00Z", "end": "2026-08-14T10:10:00Z", "duration_minutes": 10},
                {"start": "2026-08-14T10:20:00Z", "end": "2026-08-14T10:30:00Z", "duration_minutes": 10},
            ])
            args.execute = True
            with (
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "secret", "CLOCKIFY_WORKSPACE_ID": "workspace-1",
                }),
                mock.patch.object(poster, "_paged", side_effect=self._paged_with_live([])),
                mock.patch.object(poster, "_request") as request,
            ):
                with self.assertRaisesRegex(poster.PortfolioPostError, "max_create_count"):
                    poster.run(args)

        request.assert_not_called()

    def test_execute_does_not_reopen_historical_receipt_after_approval(self) -> None:
        """Swapping the artifact after its digest check must not add a trusted historical ID."""
        with tempfile.TemporaryDirectory() as directory:
            args = self._write_posting_fixture(Path(directory))
            args.execute = True
            live = [self._clockify_entry(
                "entry-history", "2026-08-14T10:00:31Z", "2026-08-14T10:10:31Z",
                description="legacy wording",
            )]

            def paged(path: str, _api_key: str, *, timeout_seconds: int):
                if path.startswith("/workspaces/workspace-1/projects"):
                    args.historical_receipt.write_text(json.dumps({
                        "schema_version": "clockify-historical-receipt/v1",
                        "entries": [{
                            "review_id": "review-a", "segment_index": 1,
                            "clockify_entry_id": "entry-history",
                        }],
                    }), encoding="utf-8")
                    return [{"id": "project-123456"}]
                if path.startswith("/workspaces/workspace-1/tags"):
                    return [{"id": "tag-654321"}]
                return live

            with (
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "secret", "CLOCKIFY_WORKSPACE_ID": "workspace-1",
                }),
                mock.patch.object(poster, "_paged", side_effect=paged),
            ):
                with self.assertRaises(poster.PortfolioPostError):
                    poster.run(args)

    def test_exact_approval_permits_the_august_sheet_review_migration_exception(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args = self._write_posting_fixture(
                Path(directory), validation_status="sheet_reviewed_after_flash"
            )
            args.execute = True
            live = [self._clockify_entry(
                "entry-exact", "2026-08-14T10:00:00Z", "2026-08-14T10:10:00Z"
            )]
            with (
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "secret", "CLOCKIFY_WORKSPACE_ID": "workspace-1",
                }),
                mock.patch.object(poster, "_paged", side_effect=self._paged_with_live(live)),
            ):
                receipt = poster.run(args)

        self.assertEqual("complete", receipt["status"])

    def test_execute_records_exact_existing_entry_in_append_only_post_events(self) -> None:
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

            events = posting_receipts.PostEventStore(args.post_events).verify()

        self.assertEqual(["planned", "already_existing"], [event.disposition for event in events])
        self.assertEqual("entry-exact", events[-1].clockify_entry_id)
        self.assertEqual(["entry-exact"], [
            item["clockify_entry_id"] for item in receipt["already_existing"]
        ])
        request.assert_not_called()

    def test_execute_never_trusts_mutable_prior_receipt_as_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self._write_posting_fixture(root)
            args.execute = True
            args.prior_receipt = root / "forged-prior-receipt.json"
            args.prior_receipt.write_text('{"portfolio_sha256":"forged"}', encoding="utf-8")
            live = [self._clockify_entry(
                "entry-exact", "2026-08-14T10:00:00Z", "2026-08-14T10:10:00Z"
            )]
            with (
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "secret", "CLOCKIFY_WORKSPACE_ID": "workspace-1",
                }),
                mock.patch.object(poster, "_paged", side_effect=self._paged_with_live(live)),
            ):
                receipt = poster.run(args)

        self.assertEqual("complete", receipt["status"])

    def test_consumed_approval_cannot_repeat_terminal_post_events(self) -> None:
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
            ):
                poster.run(args)
                with self.assertRaisesRegex(poster.PortfolioPostError, "consumed"):
                    poster.run(args)

            events = posting_receipts.PostEventStore(args.post_events).verify()

        self.assertEqual(2, len(events))

    def test_post_terminal_event_requires_exact_live_readback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args = self._write_posting_fixture(Path(directory))
            args.execute = True
            created_live = [self._clockify_entry(
                "entry-created", "2026-08-14T10:00:00Z", "2026-08-14T10:10:00Z"
            )]
            readbacks = [[], created_live, created_live]

            def paged(path: str, _api_key: str, *, timeout_seconds: int):
                if path.startswith("/workspaces/workspace-1/projects"):
                    return [{"id": "project-123456"}]
                if path.startswith("/workspaces/workspace-1/tags"):
                    return [{"id": "tag-654321"}]
                return readbacks.pop(0)

            with (
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "secret", "CLOCKIFY_WORKSPACE_ID": "workspace-1",
                }),
                mock.patch.object(poster, "_paged", side_effect=paged),
                mock.patch.object(poster, "_request", return_value={"id": "entry-created"}),
            ):
                poster.run(args)

            events = posting_receipts.PostEventStore(args.post_events).verify()

        self.assertEqual(["planned", "created"], [event.disposition for event in events])
        self.assertEqual("entry-created", events[-1].clockify_entry_id)
        self.assertTrue(events[-1].live_readback_digest.startswith("sha256:"))

    def test_execute_emits_final_full_period_live_readback_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args = self._write_posting_fixture(Path(directory))
            args.execute = True
            live = [self._clockify_entry(
                "entry-exact", "2026-08-14T10:00:00Z", "2026-08-14T10:10:00Z"
            )]
            readbacks = [live, live]

            def paged(path: str, _api_key: str, *, timeout_seconds: int):
                if path.startswith("/workspaces/workspace-1/projects"):
                    return [{"id": "project-123456"}]
                if path.startswith("/workspaces/workspace-1/tags"):
                    return [{"id": "tag-654321"}]
                return readbacks.pop(0)

            with (
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "secret", "CLOCKIFY_WORKSPACE_ID": "workspace-1",
                }),
                mock.patch.object(poster, "_paged", side_effect=paged),
            ):
                receipt = poster.run(args)

        self.assertTrue(receipt["final_live_readback_sha256"].startswith("sha256:"))

    def test_execute_rejects_final_readback_billable_drift_for_terminal_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args = self._write_posting_fixture(Path(directory))
            args.execute = True
            initial = [self._clockify_entry(
                "entry-exact", "2026-08-14T10:00:00Z", "2026-08-14T10:10:00Z"
            )]
            final = [dict(initial[0])]
            final[0]["billable"] = False
            readbacks = [initial, final]

            def paged(path: str, _api_key: str, *, timeout_seconds: int):
                if path.startswith("/workspaces/workspace-1/projects"):
                    return [{"id": "project-123456"}]
                if path.startswith("/workspaces/workspace-1/tags"):
                    return [{"id": "tag-654321"}]
                return readbacks.pop(0)

            with (
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "secret", "CLOCKIFY_WORKSPACE_ID": "workspace-1",
                }),
                mock.patch.object(poster, "_paged", side_effect=paged),
            ):
                with self.assertRaisesRegex(poster.PortfolioPostError, "final live readback semantic"):
                    poster.run(args)

    def test_cli_requires_approval_and_post_ledger_arguments_for_execution(self) -> None:
        args = poster.parse_args([
            "portfolio.json", "--quality-report", "quality.json", "--replay-integrity", "replay.json",
            "--receipt", "receipt.json", "--expected-portfolio-sha256", "digest", "--execute",
            "--approval-receipt", "approval-1", "--approval-events", "approvals.jsonl",
            "--post-events", "posts.jsonl", "--period-manifest", "period.json",
            "--period-events", "period-events.jsonl",
            "--historical-receipt", "historical.json",
        ])

        self.assertEqual("approval-1", args.approval_receipt)
        self.assertEqual(Path("approvals.jsonl"), args.approval_events)

    def test_normalized_snapshot_digest_is_order_independent_and_covers_live_identity(self) -> None:
        entries = [
            {
                "id": "entry-b", "start": "2026-08-14T10:10:00+00:00",
                "end": "2026-08-14T10:20:00Z", "project_id": "project-b",
                "tag_ids": ["tag-z", "tag-a"], "description": " Work B ", "billable": True,
            },
            {
                "id": "entry-a", "start": "2026-08-14T10:00:00Z",
                "end": "2026-08-14T10:10:00Z", "project_id": "project-a",
                "tag_ids": ["tag-b"], "description": "Work A", "billable": True,
            },
        ]
        reordered = [
            entries[1],
            {**entries[0], "tag_ids": ["tag-a", "tag-z"]},
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
            ("billable", False),
        ):
            with self.subTest(field=field):
                mutated = [dict(entries[0]), dict(entries[1])]
                mutated[1][field] = changed
                self.assertNotEqual(baseline, poster._normalized_snapshot_sha256(mutated))

    def test_exact_rejects_non_boolean_billable(self) -> None:
        plan = {"start": "2026-08-14T10:00:00Z", "end": "2026-08-14T10:10:00Z", "project_id": "p", "tag_ids": [], "description": "work", "billable": True}
        live = {**plan, "id": "entry", "billable": 1}

        self.assertFalse(poster._exact(plan, live))

    def test_execution_failure_releases_approval_lock(self) -> None:
        """An execution error after approval validation must not leave its lock held."""
        with tempfile.TemporaryDirectory() as directory:
            args = self._write_posting_fixture(Path(directory))
            args.execute = True
            acquired_stores: list[posting_receipts.ApprovalReceiptStore] = []
            original_context = poster._approval_context

            def capture_context(*context_args: object, **context_kwargs: object):
                result = original_context(*context_args, **context_kwargs)
                acquired_stores.append(result[2])
                return result

            with (
                mock.patch.object(poster, "_approval_context", side_effect=capture_context),
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "key", "CLOCKIFY_WORKSPACE_ID": "workspace-other",
                }),
            ):
                with self.assertRaisesRegex(poster.PortfolioPostError, "does not match approval"):
                    poster.run(args)

            self.assertEqual(1, len(acquired_stores))
            retry = posting_receipts.ApprovalReceiptStore(args.approval_events)
            try:
                retry.acquire_execution_lock(args.approval_receipt)
            finally:
                retry.release_execution_lock()

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

    def test_run_retry_rederives_and_restores_aggregate_subminute_trims(self) -> None:
        """A retry excludes its provisional receipt, then restores 30+40 seconds exactly."""
        segments = [
            {"start": "2026-08-14T10:00:00Z", "end": "2026-08-14T10:10:00Z", "duration_minutes": 10},
            {"start": "2026-08-14T10:20:00Z", "end": "2026-08-14T10:30:00Z", "duration_minutes": 10},
            {"start": "2026-08-14T10:40:00Z", "end": "2026-08-14T10:50:00Z", "duration_minutes": 10},
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = self._write_posting_fixture(root, allocation_segments=segments)
            prior_path = root / "prior.json"
            prior_path.write_text(json.dumps({
                "portfolio_sha256": args.expected_portfolio_sha256,
                "created": [{
                    "review_id": "review-a", "segment_index": 1,
                    "clockify_entry_id": "entry-prior",
                    "start": "2026-08-14T10:00:30Z",
                    "end": "2026-08-14T10:10:00Z", "duration_seconds": 570,
                }], "already_existing": [],
            }), encoding="utf-8")
            args.prior_receipt = prior_path
            live = [
                self._clockify_entry(
                    "entry-prior", "2026-08-14T10:00:30Z", "2026-08-14T10:10:00Z"
                ),
                self._clockify_entry(
                    "blocker-30-before", "2026-08-14T09:59:00Z", "2026-08-14T10:00:30Z",
                    project_id="project-other", description="Unrelated blocker",
                ),
                self._clockify_entry(
                    "blocker-30-end", "2026-08-14T10:10:00Z", "2026-08-14T10:10:30Z",
                    project_id="project-other", description="Unrelated blocker",
                ),
                self._clockify_entry(
                    "blocker-40-before", "2026-08-14T10:19:00Z", "2026-08-14T10:20:40Z",
                    project_id="project-other", description="Unrelated blocker",
                ),
                self._clockify_entry(
                    "blocker-40-end", "2026-08-14T10:30:00Z", "2026-08-14T10:30:40Z",
                    project_id="project-other", description="Unrelated blocker",
                ),
            ]
            with (
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "secret", "CLOCKIFY_WORKSPACE_ID": "workspace-1",
                }),
                mock.patch.object(poster, "_paged", side_effect=self._paged_with_live(live)),
            ):
                first = poster.run(args)
                second = poster.run(args)

        self.assertEqual(["entry-prior"], [
            item["clockify_entry_id"] for item in first["already_existing"]
        ])
        self.assertEqual([
            ("2026-08-14T10:00:30Z", "2026-08-14T10:10:00Z"),
            ("2026-08-14T10:20:40Z", "2026-08-14T10:30:00Z"),
            ("2026-08-14T10:40:00Z", "2026-08-14T10:51:10Z"),
        ], [(item["posted_start"], item["posted_end"]) for item in first["boundary_adjustments"]])
        self.assertEqual(1800, first["planned_seconds"])
        self.assertEqual(first["boundary_adjustments"], second["boundary_adjustments"])
        self.assertEqual(first["boundary_adjustments_sha256"], second["boundary_adjustments_sha256"])
        self.assertEqual(first["blocker_snapshot_sha256"], second["blocker_snapshot_sha256"])

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

    def test_run_resume_never_blindly_reposts_an_ambiguous_planned_entry(self) -> None:
        segments = [
            {"start": "2026-08-14T10:00:00Z", "end": "2026-08-14T10:10:00Z", "duration_minutes": 10},
            {"start": "2026-08-14T10:20:00Z", "end": "2026-08-14T10:30:00Z", "duration_minutes": 10},
        ]
        with tempfile.TemporaryDirectory() as directory:
            args = self._write_posting_fixture(Path(directory), allocation_segments=segments)
            args.execute = True
            first_live = [self._clockify_entry(
                "entry-first", "2026-08-14T10:00:00Z", "2026-08-14T10:10:00Z"
            )]
            readbacks = [first_live, first_live]

            def paged(path: str, _api_key: str, *, timeout_seconds: int):
                if path.startswith("/workspaces/workspace-1/projects"):
                    return [{"id": "project-123456"}]
                if path.startswith("/workspaces/workspace-1/tags"):
                    return [{"id": "tag-654321"}]
                return readbacks.pop(0)

            with (
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "secret", "CLOCKIFY_WORKSPACE_ID": "workspace-1",
                }),
                mock.patch.object(poster, "_paged", side_effect=paged),
                mock.patch.object(poster, "_request", side_effect=poster.PortfolioPostError("write uncertain")),
            ):
                with self.assertRaisesRegex(poster.PortfolioPostError, "write uncertain"):
                    poster.run(args)
            interrupted = json.loads(args.receipt.read_text(encoding="utf-8"))

            with (
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "secret", "CLOCKIFY_WORKSPACE_ID": "workspace-1",
                }),
                mock.patch.object(poster, "_paged", side_effect=self._paged_with_live(first_live)),
                mock.patch.object(poster, "_request", return_value={"id": "entry-second"}) as retry_post,
            ):
                with self.assertRaisesRegex(poster.PortfolioPostError, "refusing to repeat POST"):
                    poster.run(args)

        self.assertEqual(["entry-first"], [
            item["clockify_entry_id"] for item in interrupted["already_existing"]
        ])
        retry_post.assert_not_called()

    def test_ambiguous_post_recovery_rejects_multiple_exact_live_entries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            args = self._write_posting_fixture(Path(directory))
            args.execute = True
            exact_entries = [
                self._clockify_entry(
                    "entry-first", "2026-08-14T10:00:00Z", "2026-08-14T10:10:00Z"
                ),
                self._clockify_entry(
                    "entry-second", "2026-08-14T10:00:00Z", "2026-08-14T10:10:00Z"
                ),
            ]
            readbacks = [[], exact_entries]

            def paged(path: str, _api_key: str, *, timeout_seconds: int):
                self.assertEqual(45, timeout_seconds)
                if path.startswith("/workspaces/workspace-1/projects"):
                    return [{"id": "project-123456"}]
                if path.startswith("/workspaces/workspace-1/tags"):
                    return [{"id": "tag-654321"}]
                return readbacks.pop(0)

            with (
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "secret", "CLOCKIFY_WORKSPACE_ID": "workspace-1",
                }),
                mock.patch.object(poster, "_paged", side_effect=paged),
                mock.patch.object(
                    poster, "_request", side_effect=poster.PortfolioPostError("write uncertain")
                ),
            ):
                with self.assertRaisesRegex(
                    poster.PortfolioPostError, "multiple exact Clockify entries"
                ):
                    poster.run(args)
            interrupted = json.loads(args.receipt.read_text(encoding="utf-8"))

        self.assertEqual("interrupted", interrupted["status"])
        self.assertEqual([], interrupted["created"])

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
