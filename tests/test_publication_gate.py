from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from dataclasses import replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import clockify_currency
from scripts import clockify_period_readback
from scripts import clockify_publication_gate as publication_gate
from scripts import posting_receipts
from scripts import reconciliation_manifest


NOW = datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)


def digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def artifact(path: Path) -> reconciliation_manifest.ArtifactIdentity:
    return reconciliation_manifest.ArtifactIdentity(
        path=path.resolve(),
        schema_version="fixture/v1",
        compatibility_version="fixture/v1",
        digest=digest_bytes(path.read_bytes()),
    )


def copy_ledger(source: Path, destination: Path) -> None:
    """Copy an anchored receipt ledger so path binding, not validity, is tested."""
    destination.write_bytes(source.read_bytes())
    source_anchor = source.with_name(source.name + ".anchor.jsonl")
    destination.with_name(destination.name + ".anchor.jsonl").write_bytes(source_anchor.read_bytes())


class PublicationGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.identity = reconciliation_manifest.PeriodIdentity(
            member_id="member-1",
            workspace_id="workspace-1",
            timezone="Europe/Bucharest",
            since=datetime(2026, 8, 1, tzinfo=timezone.utc),
            until=datetime(2026, 8, 16, tzinfo=timezone.utc),
            revision=1,
        )
        self.fixture_counter = 0

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def ready_inputs(
        self,
        *,
        calendly_complete: bool = True,
        include_slice: bool = True,
        quality_status: str = "pass",
        replay_status: str = "pass",
        coverage: dict[str, object] | None = None,
        quote_date: date = date(2026, 8, 18),
        exception_key: str | None = None,
        slice_target: dict[str, str] | None = None,
        slice_dates: dict[str, str] | None = None,
        post_entries: bool = True,
        approval_manifest_digest: str | None = None,
        approval_event_history_digest: str | None = None,
        approval_prefix_state: str = "approved",
    ) -> dict[str, object]:
        self.fixture_counter += 1
        fixture_root = self.root / f"fixture-{self.fixture_counter}"
        fixture_root.mkdir()
        period = self.identity.document()
        slice_bundle = {
            "run_id": "fixture-slice",
            "slice_id": "fixture-slice",
            "target": slice_target or {
                "period_id": self.identity.period_id,
                "workspace_id": self.identity.workspace_id,
                "member_id": self.identity.member_id,
            },
            "date_range": slice_dates or {"since": period["since_utc"], "until": period["until_utc"]},
            "evidence_ledger": {"source_completeness": {"status": "complete", "incomplete_sources": []}},
            "evidence": {"calendly": {"status": "ok", "complete": calendly_complete}},
            "meeting_accounting": {"status": "complete", "unresolved_exceptions": []},
        }
        if exception_key is not None:
            slice_bundle[exception_key] = [{"reason": "requires review"}]
        quality = {"status": quality_status}
        replay = {"status": replay_status, "identity": {"artifacts": {"quality": "bound"}}}
        coverage = coverage or {
            "status": "complete",
            "source_completeness": {"status": "complete", "incomplete_sources": []},
            "limitations": [],
            "required_slices": [{
                "slice_id": "fixture-slice", "since": period["since_utc"], "until": period["until_utc"],
            }],
        }
        quote = clockify_currency.FxQuoteReceipt(
            provider="ECB", effective_date=quote_date, fetched_at=NOW, base_currency="EUR",
            rates={"USD": Decimal("1.1000")}, payload_digest="sha256:" + "d" * 64,
        )
        paths = {
            "slice": fixture_root / "run-report.json", "quality": fixture_root / "quality.json",
            "replay": fixture_root / "replay.json", "coverage": fixture_root / "coverage.json",
            "quote": fixture_root / "fx-quote.json",
        }
        write_json(paths["slice"], slice_bundle)
        write_json(paths["quality"], quality)
        write_json(paths["replay"], replay)
        write_json(paths["coverage"], coverage)
        write_json(paths["quote"], quote.to_dict())
        initial_artifacts = [
            artifact(path) for key, path in paths.items() if key != "slice" or include_slice
        ]
        events = reconciliation_manifest.CoordinatorEventStore(fixture_root / "period-events.jsonl")
        events.append(
            self.identity, "period_opened", {"artifacts": [item.document() for item in initial_artifacts]}, occurred_at=NOW,
        )
        for limitation in coverage.get("limitations", []):
            if isinstance(limitation, dict) and limitation.get("approved") is not False:
                events.append(
                    self.identity, "coverage_limitation_approved", limitation,
                    occurred_at=NOW + timedelta(seconds=30),
                )
        for sequence, event_type in enumerate((
            "collection_complete", "reconciliation_complete", "review_approved",
        ), start=1):
            events.append(self.identity, event_type, {}, occurred_at=NOW + timedelta(minutes=sequence))
        approved_manifest = reconciliation_manifest.ReconciliationCoordinator(self.identity, events).derive()
        if approval_prefix_state == "approved":
            approval_manifest = approved_manifest
        elif approval_prefix_state == "awaiting_review":
            approval_manifest = reconciliation_manifest.derive_manifest_from_verified_events(
                self.identity, events.verify(self.identity)[:-1],
            )
        else:
            raise ValueError("unsupported approval prefix fixture state")
        operation_identity = posting_receipts.derive_operation_identity(
            operation="clockify_post",
            period_id=self.identity.period_id,
            workspace_id=self.identity.workspace_id,
            member_id=self.identity.member_id,
        )
        approval_events_path = fixture_root / "approval-events.jsonl"
        approval_store = posting_receipts.ApprovalReceiptStore(approval_events_path)
        approval_receipt = posting_receipts.ApprovalReceipt(
            approval_id="post-approval-1", approver="board", approved_at="2026-08-18T08:00:00Z",
            expires_at="2026-08-19T08:00:00Z", operation="clockify_post", operation_identity=operation_identity,
            period_id=self.identity.period_id, period_start=period["since_utc"], period_end=period["until_utc"],
            workspace_id=self.identity.workspace_id, member_id=self.identity.member_id,
            portfolio_digest=digest_bytes(b"portfolio"), quality_digest=digest_bytes(b"quality"),
            replay_digest=digest_bytes(b"replay"), routing_digest=digest_bytes(b"routing"),
            correction_log_digest=digest_bytes(b"correction-log"), coverage_digest=digest_bytes(b"coverage"),
            residual_exception_digest=digest_bytes(b"exceptions"),
            manifest_digest=approval_manifest_digest or approval_manifest.manifest_digest,
            event_history_digest=approval_event_history_digest or approval_manifest.events_digest,
            historical_receipt_digest=digest_bytes(b"historical"),
            max_create_count=1,
        )
        approval_store.append(approval_receipt)
        approval_store.consume(
            approval_receipt.approval_id, operation_identity=operation_identity,
            consumed_at="2026-08-18T08:01:00Z",
        )
        post_events_path = fixture_root / "post-events.jsonl"
        post_store = posting_receipts.PostEventStore(post_events_path)
        if post_entries:
            post_store.append(posting_receipts.PostEvent(
                disposition="planned", operation="clockify_post", operation_identity=operation_identity,
                period_id=self.identity.period_id, workspace_id=self.identity.workspace_id, member_id=self.identity.member_id,
                review_id="review-1", segment_index=0, recorded_at="2026-08-18T08:02:00Z",
            ))
            post_store.append(posting_receipts.PostEvent(
                disposition="created", operation="clockify_post", operation_identity=operation_identity,
                period_id=self.identity.period_id, workspace_id=self.identity.workspace_id, member_id=self.identity.member_id,
                review_id="review-1", segment_index=0, recorded_at="2026-08-18T08:03:00Z",
                clockify_entry_id="entry-1", live_readback_digest=digest_bytes(b"post-readback"),
            ))
        else:
            post_events_path.touch()
        post_receipt = {
            "schema_version": "clockify-portfolio-post/v1",
            "status": "complete",
            "final_live_readback_sha256": "sha256:" + "b" * 64,
            "post_events": post_store.derive_receipt(operation_identity),
        }
        readback = clockify_period_readback.normalize_readback({
            "schema_version": "clockify-period-readback/v1", "evidence_kind": "ledger",
            "workspace_id": self.identity.workspace_id, "member_id": self.identity.member_id,
            "timezone": self.identity.timezone, "period_start": period["since_utc"], "period_end": period["until_utc"],
            "filters": {
                "workspace_id": self.identity.workspace_id, "member_id": self.identity.member_id,
                "timezone": self.identity.timezone, "include_running": False, "include_deleted": False,
            },
            "refreshed_at": "2026-08-18T08:55:00Z", "include_running": False, "include_deleted": False,
            "entry_ids": ["entry-1"] if post_entries else ["entry-zero"], "entry_count": 1,
            "duration_seconds": 600 if post_entries else 0,
            "entry_durations": {"entry-1": 600} if post_entries else {"entry-zero": 0}, "native_costs": {"USD": "983.70", "EUR": "7.31"},
        })
        currency_summary = clockify_currency.convert_native_buckets(
            readback.native_costs, quote, publication_date=NOW.date(),
        ) if quote_date >= NOW.date() - timedelta(days=4) else None
        raw_report = {"totals": [{
            "entriesCount": 1, "totalTime": 600 if post_entries else 0,
            "amounts": [{"amountByCurrency": [
                {"currency": "USD", "amount": "98370"}, {"currency": "EUR", "amount": "731"},
            ]}],
        }]}
        report_receipt = {
            "workspace_id": self.identity.workspace_id, "member_id": self.identity.member_id,
            "period_start": period["since_utc"], "period_end": period["until_utc"],
            "filters": dict(readback.filters), "shared_report_id": "shared-report-fixture-1",
            "raw_response_digest": clockify_period_readback._digest(
                clockify_period_readback._normalize_report_projection(raw_report)
            ),
        }
        shared_report = clockify_period_readback.normalize_readback({
            **readback.document(), "evidence_kind": "report", "refreshed_at": "2026-08-18T08:56:00Z",
            "request_receipt": report_receipt, "raw_response": raw_report,
        })

        paths.update({
            "post": fixture_root / "post-receipt.json", "api": fixture_root / "clockify-api-after.json",
            "report": fixture_root / "shared-report-after.json", "approval_events": approval_events_path,
            "post_events": post_events_path,
        })
        write_json(paths["post"], post_receipt)
        write_json(paths["api"], readback.to_dict())
        write_json(paths["report"], shared_report.to_dict())
        events.append(self.identity, "posting_started", {}, occurred_at=NOW + timedelta(minutes=4))
        posting_artifacts = [
            artifact(paths[key]) for key in ("approval_events", "post_events", "post", "api", "report")
        ]
        events.append(
            self.identity, "posting_complete", {"artifacts": [item.document() for item in posting_artifacts]},
            occurred_at=NOW + timedelta(minutes=5),
        )
        events.append(self.identity, "clockify_readback_verified", {}, occurred_at=NOW + timedelta(minutes=6))
        manifest = reconciliation_manifest.ReconciliationCoordinator(self.identity, events).derive()
        return {
            "manifest": manifest, "events": events.path, "post_receipt": post_receipt,
            "approval_receipt_id": approval_receipt.approval_id, "approval_events": approval_events_path,
            "post_events": post_events_path, "api_readback": readback, "shared_report_readback": shared_report,
            "currency_summary": currency_summary, "fx_quote": quote, "quality": quality,
            "replay": replay, "coverage": coverage, "paths": paths,
            "slack_target": "slack:finance-report:workspace-1:member-1",
        }

    def prepare(self, inputs: dict[str, object]) -> publication_gate.PublicationContract:
        return publication_gate.prepare_publication(
            inputs["manifest"], events=inputs["events"], approval_receipt_id=inputs["approval_receipt_id"],
            approval_events=inputs["approval_events"], post_events=inputs["post_events"],
            post_receipt=inputs["post_receipt"], api_readback=inputs["api_readback"],
            shared_report_readback=inputs["shared_report_readback"], currency_summary=inputs["currency_summary"],
            fx_quote=inputs["fx_quote"], quality=inputs["quality"], replay=inputs["replay"],
            coverage=inputs["coverage"], slack_target=inputs["slack_target"], now=NOW,
        )

    def approval(self, contract: publication_gate.PublicationContract, **changes: object) -> dict[str, object]:
        approval: dict[str, object] = {
            "schema_version": "publication-approval/v1", "approval_id": "publication-approval-1",
            "approver": "board", "approved_at": "2026-08-18T08:00:00Z", "expires_at": "2026-08-19T08:00:00Z",
            "operations": ["shared_report_update", "slack_correction"],
            "report_target": contract.report_target, "slack_target": contract.slack_target,
            "period_id": contract.period_id, "workspace_id": contract.workspace_id,
            "member_id": contract.member_id, "period_start": contract.period_start,
            "period_end": contract.period_end, "revision": contract.revision,
            "contract_digest": contract.contract_digest, "idempotency_key": contract.idempotency_key,
        }
        approval.update(changes)
        return approval

    def test_prepare_requires_verified_manifest_post_readback_and_fx(self) -> None:
        contract = self.prepare(self.ready_inputs())

        self.assertEqual("publication_prepared", contract.state)
        self.assertEqual("finance-report:workspace-1:member-1:2026-08-01:2026-08-16:1", contract.idempotency_key)
        self.assertEqual({"USD": Decimal("983.70"), "EUR": Decimal("7.31")}, contract.native_buckets)
        self.assertEqual(Decimal("991.74"), contract.usd_equivalent_total)
        self.assertEqual("shared-report:shared-report-fixture-1", contract.report_target)

    def test_prepare_rederives_the_supplied_manifest_from_canonical_period_events(self) -> None:
        inputs = self.ready_inputs()
        events = reconciliation_manifest.CoordinatorEventStore(inputs["events"])
        events.append(self.identity, "coverage_limitation_approved", {}, occurred_at=NOW + timedelta(hours=1))

        with self.assertRaisesRegex(publication_gate.PublicationGateError, "event history"):
            self.prepare(inputs)

    def test_prepare_rejects_posting_ledgers_not_bound_to_the_manifest(self) -> None:
        """Removing public API path binding would admit a valid alternate posting history."""
        inputs = self.ready_inputs()
        for field in ("approval_events", "post_events"):
            with self.subTest(field=field):
                alternate = self.root / f"alternate-{field}.jsonl"
                copy_ledger(inputs[field], alternate)  # type: ignore[arg-type]
                substituted = dict(inputs)
                substituted[field] = alternate

                with self.assertRaisesRegex(publication_gate.PublicationGateError, "artifact"):
                    self.prepare(substituted)

    def test_prepare_rejects_post_receipt_not_bound_to_the_manifest(self) -> None:
        """Changing a receipt-only digest must not survive preparation as a self-consistent claim."""
        inputs = self.ready_inputs()
        post_receipt = dict(inputs["post_receipt"])
        post_receipt["final_live_readback_sha256"] = digest_bytes(b"unbound-final-readback")
        inputs["post_receipt"] = post_receipt

        with self.assertRaisesRegex(publication_gate.PublicationGateError, "post receipt artifact"):
            self.prepare(inputs)

    def test_prepare_rejects_a_forged_in_memory_quality_result(self) -> None:
        inputs = self.ready_inputs(quality_status="fail")
        inputs["quality"] = {"status": "pass"}

        with self.assertRaisesRegex(publication_gate.PublicationGateError, "quality artifact"):
            self.prepare(inputs)

    def test_prepare_rejects_a_forged_in_memory_replay_result(self) -> None:
        inputs = self.ready_inputs(replay_status="fail")
        inputs["replay"] = {"status": "pass", "identity": {"artifacts": {"quality": "bound"}}}

        with self.assertRaisesRegex(publication_gate.PublicationGateError, "replay artifact"):
            self.prepare(inputs)

    def test_prepare_rejects_post_approval_without_a_manifest_prefix(self) -> None:
        with self.assertRaisesRegex(publication_gate.PublicationGateError, "approval history prefix"):
            self.prepare(self.ready_inputs(approval_event_history_digest=digest_bytes(b"absent-prefix")))

    def test_prepare_rejects_post_approval_with_wrong_prefix_manifest_digest(self) -> None:
        with self.assertRaisesRegex(publication_gate.PublicationGateError, "approval manifest"):
            self.prepare(self.ready_inputs(approval_manifest_digest=digest_bytes(b"wrong-prefix-manifest")))

    def test_prepare_rejects_post_approval_with_a_non_approved_prefix(self) -> None:
        with self.assertRaisesRegex(publication_gate.PublicationGateError, "approved state"):
            self.prepare(self.ready_inputs(approval_prefix_state="awaiting_review"))

    def test_post_approval_prefix_rejects_ambiguous_history(self) -> None:
        inputs = self.ready_inputs()
        approval = posting_receipts.ApprovalReceiptStore(inputs["approval_events"]).require_consumed(
            inputs["approval_receipt_id"],
            operation_identity=posting_receipts.derive_operation_identity(
                operation="clockify_post", period_id=self.identity.period_id,
                workspace_id=self.identity.workspace_id, member_id=self.identity.member_id,
            ),
            now=NOW,
        )
        history = reconciliation_manifest.CoordinatorEventStore(inputs["events"]).verify(self.identity)
        approved_prefix = reconciliation_manifest.derive_manifest_from_verified_events(self.identity, history[:4])

        with mock.patch.object(
            reconciliation_manifest, "derive_manifest_from_verified_events", return_value=approved_prefix,
        ), self.assertRaisesRegex(publication_gate.PublicationGateError, "ambiguous"):
            publication_gate._approved_manifest_prefix(self.identity, history, approval)

    def test_complete_slice_bundle_for_another_target_blocks_prepare(self) -> None:
        with self.assertRaisesRegex(publication_gate.PublicationGateError, "slice target"):
            self.prepare(self.ready_inputs(slice_target={
                "period_id": self.identity.period_id, "workspace_id": self.identity.workspace_id,
                "member_id": "other-member",
            }))

    def test_idempotency_key_accepts_a_serialized_reconciliation_manifest(self) -> None:
        inputs = self.ready_inputs()
        manifest = inputs["manifest"]
        assert isinstance(manifest, reconciliation_manifest.ReconciliationManifest)

        self.assertEqual(
            "finance-report:workspace-1:member-1:2026-08-01:2026-08-16:1",
            publication_gate.publication_idempotency_key(manifest.document()),
        )

    def test_unapproved_coverage_exception_blocks_prepare(self) -> None:
        inputs = self.ready_inputs(coverage={
            "status": "incomplete", "source_completeness": {"status": "incomplete", "incomplete_sources": ["desktop"]},
            "required_slices": [{
                "slice_id": "fixture-slice", "since": self.identity.document()["since_utc"],
                "until": self.identity.document()["until_utc"],
            }],
            "limitations": [{"source": "desktop", "approved": True}],
        })

        with self.assertRaisesRegex(publication_gate.PublicationGateError, "coverage"):
            self.prepare(inputs)

    def test_coverage_limitation_requires_an_immutable_event_backed_approval(self) -> None:
        period = self.identity.document()
        limitation = {
            "approval_id": "coverage-approval-1", "approver": "board", "approved_at": "2026-08-18T08:30:00Z",
            "period_id": self.identity.period_id, "period_start": period["since_utc"], "period_end": period["until_utc"],
            "source": "desktop", "coverage_digest": digest_bytes(b"desktop-coverage"),
        }
        contract = self.prepare(self.ready_inputs(coverage={
            "status": "incomplete", "source_completeness": {"status": "incomplete", "incomplete_sources": ["desktop"]},
            "required_slices": [{"slice_id": "fixture-slice", "since": period["since_utc"], "until": period["until_utc"]}],
            "limitations": [limitation],
        }))

        self.assertEqual("publication_prepared", contract.state)

    def test_coverage_input_must_match_immutable_manifest_evidence(self) -> None:
        """Removing manifest binding would let callers replace coverage evidence in memory."""
        inputs = self.ready_inputs()
        coverage = dict(inputs["coverage"])
        coverage["untrusted_override"] = True
        inputs["coverage"] = coverage

        with self.assertRaisesRegex(publication_gate.PublicationGateError, "coverage artifact"):
            self.prepare(inputs)

    def test_missing_slice_bundle_blocks_prepare(self) -> None:
        with self.assertRaisesRegex(publication_gate.PublicationGateError, "slice"):
            self.prepare(self.ready_inputs(include_slice=False))

    def test_artifact_drift_blocks_prepare(self) -> None:
        inputs = self.ready_inputs()
        cast_paths = inputs["paths"]
        assert isinstance(cast_paths, dict)
        write_json(cast_paths["quality"], {"status": "pass", "drift": True})

        with self.assertRaisesRegex(publication_gate.PublicationGateError, "artifact"):
            self.prepare(inputs)

    def test_incomplete_calendly_blocks_prepare(self) -> None:
        with self.assertRaisesRegex(publication_gate.PublicationGateError, "Calendly"):
            self.prepare(self.ready_inputs(calendly_complete=False))

    def test_stale_runner_state_cannot_substitute_for_verified_manifest(self) -> None:
        inputs = self.ready_inputs()
        manifest = inputs["manifest"]
        assert isinstance(manifest, reconciliation_manifest.ReconciliationManifest)
        stale_document = manifest.document()
        stale_document["state"] = "approved"
        stale_document["manifest_digest"] = reconciliation_manifest._digest({
            key: value for key, value in stale_document.items() if key != "manifest_digest"
        })
        stale_manifest = reconciliation_manifest.ReconciliationManifest.from_document(stale_document)

        inputs["manifest"] = stale_manifest
        with self.assertRaisesRegex(publication_gate.PublicationGateError, "manifest state"):
            self.prepare(inputs)

    def test_forged_manifest_object_digest_blocks_prepare(self) -> None:
        inputs = self.ready_inputs()
        manifest = inputs["manifest"]
        assert isinstance(manifest, reconciliation_manifest.ReconciliationManifest)
        forged = reconciliation_manifest.ReconciliationManifest(
            identity=manifest.identity, state=manifest.state, event_count=manifest.event_count,
            events_digest=manifest.events_digest, artifacts=manifest.artifacts, blockers=manifest.blockers,
            manifest_digest="sha256:" + "e" * 64,
        )

        inputs["manifest"] = forged
        with self.assertRaisesRegex(publication_gate.PublicationGateError, "manifest digest"):
            self.prepare(inputs)

    def test_quality_and_replay_failures_block_prepare(self) -> None:
        for label, inputs in (("quality", self.ready_inputs(quality_status="fail")), ("replay", self.ready_inputs(replay_status="fail"))):
            with self.subTest(label=label), self.assertRaisesRegex(publication_gate.PublicationGateError, label):
                self.prepare(inputs)

    def test_unresolved_semantic_routing_overlap_or_billability_exception_blocks_prepare(self) -> None:
        for exception_key, expected in (
            ("semantic_exceptions", "semantic"), ("routing_exceptions", "routing"),
            ("overlap_exceptions", "overlap"), ("billability_exceptions", "billability"),
        ):
            with self.subTest(exception_key=exception_key), self.assertRaisesRegex(publication_gate.PublicationGateError, expected):
                self.prepare(self.ready_inputs(exception_key=exception_key))

    def test_post_readback_mismatch_blocks_prepare(self) -> None:
        inputs = self.ready_inputs()
        receipt = inputs["post_receipt"]
        assert isinstance(receipt, dict)
        post_events = receipt["post_events"]
        assert isinstance(post_events, dict)
        entries = post_events["entries"]
        assert isinstance(entries, list)
        entry = entries[0]
        assert isinstance(entry, dict)
        entry["clockify_entry_id"] = "missing-entry"

        with self.assertRaisesRegex(publication_gate.PublicationGateError, "post_events"):
            self.prepare(inputs)

    def test_unconsumed_or_unrelated_post_approval_blocks_prepare(self) -> None:
        inputs = self.ready_inputs()
        inputs["approval_receipt_id"] = "missing-approval"

        with self.assertRaisesRegex(publication_gate.PublicationGateError, "approval"):
            self.prepare(inputs)

    def test_nonzero_period_requires_at_least_one_terminal_post_entry(self) -> None:
        """Changing the guard to use duration rather than period boundaries would admit empty posting history."""
        with self.assertRaisesRegex(publication_gate.PublicationGateError, "terminal"):
            self.prepare(self.ready_inputs(post_entries=False))

    def test_stale_or_future_dual_readback_blocks_prepare(self) -> None:
        for label, key, refreshed_at in (
            ("stale", "api_readback", "2026-08-18T08:44:00Z"),
            ("future", "shared_report_readback", "2026-08-18T09:01:00Z"),
        ):
            with self.subTest(label=label):
                inputs = self.ready_inputs()
                readback = inputs[key]
                assert isinstance(readback, clockify_period_readback.ClockifyPeriodReadback)
                document = readback.to_dict()
                document["refreshed_at"] = refreshed_at
                document.pop("digest")
                inputs[key] = document
                with self.assertRaisesRegex(publication_gate.PublicationGateError, "fresh"):
                    self.prepare(inputs)

    def test_stale_fx_quote_blocks_prepare(self) -> None:
        inputs = self.ready_inputs(quote_date=date(2026, 8, 13))
        quote = inputs["fx_quote"]
        readback = inputs["api_readback"]
        assert isinstance(quote, clockify_currency.FxQuoteReceipt)
        assert isinstance(readback, clockify_period_readback.ClockifyPeriodReadback)
        inputs["currency_summary"] = clockify_currency.CurrencySummary(
            native_buckets=readback.native_costs,
            usd_buckets={"USD": Decimal("983.70"), "EUR": Decimal("8.04")},
            usd_equivalent_total=Decimal("991.74"),
        )

        with self.assertRaisesRegex(publication_gate.PublicationGateError, "currency"):
            self.prepare(inputs)

    def test_authorization_names_exact_report_and_slack_bundle(self) -> None:
        prepared = self.prepare(self.ready_inputs())

        authorized = publication_gate.authorize_publication(prepared, self.approval(prepared), now=NOW)

        self.assertEqual("publication_authorized", authorized.state)
        self.assertEqual(prepared.contract_digest, authorized.contract_digest)
        self.assertEqual(("shared_report_update", "slack_correction"), authorized.operations)

    def test_expired_or_wrong_target_publication_approval_blocks_authorization(self) -> None:
        prepared = self.prepare(self.ready_inputs())
        for label, approval in (
            ("expired", self.approval(prepared, expires_at="2026-08-18T08:59:00Z")),
            ("target", self.approval(prepared, report_target="different-report")),
        ):
            with self.subTest(label=label), self.assertRaisesRegex(publication_gate.PublicationGateError, label):
                publication_gate.authorize_publication(prepared, approval, now=NOW)

    def test_authorization_normalizes_contract_and_rechecks_fx_freshness(self) -> None:
        prepared = self.prepare(self.ready_inputs())
        tampered = replace(prepared, duration_seconds=0)
        with self.assertRaisesRegex(publication_gate.PublicationGateError, "contract digest"):
            publication_gate.authorize_publication(tampered, self.approval(prepared), now=NOW)

        future = NOW + timedelta(days=5)
        approval = self.approval(prepared, expires_at="2026-08-25T08:00:00Z")
        with self.assertRaisesRegex(publication_gate.PublicationGateError, "currency evidence"):
            publication_gate.authorize_publication(prepared, approval, now=future)

    def test_cli_prepare_and_authorize_write_only_successful_contracts(self) -> None:
        inputs = self.ready_inputs()
        paths = inputs["paths"]
        assert isinstance(paths, dict)
        prepared_path = self.root / "publication-prepared.json"
        prepared_path.write_text("existing\n", encoding="utf-8")
        failure = publication_gate.main([
            "prepare", "--manifest", str(self.root / "missing-manifest.json"),
            "--events", str(inputs["events"]), "--post-receipt", str(paths["post"]),
            "--approval-receipt-id", str(inputs["approval_receipt_id"]),
            "--approval-events", str(paths["approval_events"]), "--post-events", str(paths["post_events"]),
            "--api-readback", str(paths["api"]), "--shared-report-readback", str(paths["report"]),
            "--fx-quote", str(paths["quote"]), "--quality", str(paths["quality"]),
            "--replay", str(paths["replay"]), "--coverage", str(paths["coverage"]),
            "--slack-target", str(inputs["slack_target"]),
            "--output", str(prepared_path),
        ], now=NOW)
        self.assertNotEqual(0, failure)
        self.assertEqual("existing\n", prepared_path.read_text(encoding="utf-8"))

        manifest_path = self.root / "period-manifest.json"
        write_json(manifest_path, inputs["manifest"].document())
        success = publication_gate.main([
            "prepare", "--manifest", str(manifest_path), "--post-receipt", str(paths["post"]),
            "--events", str(inputs["events"]), "--approval-receipt-id", str(inputs["approval_receipt_id"]),
            "--approval-events", str(paths["approval_events"]), "--post-events", str(paths["post_events"]),
            "--api-readback", str(paths["api"]), "--shared-report-readback", str(paths["report"]),
            "--fx-quote", str(paths["quote"]),
            "--quality", str(paths["quality"]), "--replay", str(paths["replay"]),
            "--coverage", str(paths["coverage"]), "--slack-target", str(inputs["slack_target"]),
            "--output", str(prepared_path),
        ], now=NOW)
        self.assertEqual(0, success)
        prepared = publication_gate.PublicationContract.from_document(json.loads(prepared_path.read_text(encoding="utf-8")))
        self.assertEqual("publication_prepared", prepared.state)

        approval_path = self.root / "publication-approval.json"
        authorized_path = self.root / "publication-authorized.json"
        authorized_path.write_text("existing authorization\n", encoding="utf-8")
        write_json(approval_path, self.approval(prepared))
        self.assertEqual(0, publication_gate.main([
            "authorize", "--prepared", str(prepared_path), "--approval", str(approval_path),
            "--output", str(authorized_path),
        ], now=NOW))
        authorized = publication_gate.AuthorizedPublication.from_document(json.loads(authorized_path.read_text(encoding="utf-8")))
        self.assertEqual("publication_authorized", authorized.state)

        authorized_path.write_text("preserve this authorization\n", encoding="utf-8")
        write_json(approval_path, self.approval(prepared, report_target="other-report"))
        self.assertNotEqual(0, publication_gate.main([
            "authorize", "--prepared", str(prepared_path), "--approval", str(approval_path),
            "--output", str(authorized_path),
        ], now=NOW))
        self.assertEqual("preserve this authorization\n", authorized_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
