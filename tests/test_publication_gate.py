from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

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
    ) -> dict[str, object]:
        self.fixture_counter += 1
        fixture_root = self.root / f"fixture-{self.fixture_counter}"
        fixture_root.mkdir()
        period = self.identity.document()
        slice_bundle = {
            "run_id": "fixture-slice",
            "date_range": {"since": period["since_utc"], "until": period["until_utc"]},
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
        }
        operation_identity = posting_receipts.derive_operation_identity(
            operation="clockify_post",
            period_id=self.identity.period_id,
            workspace_id=self.identity.workspace_id,
            member_id=self.identity.member_id,
        )
        post_receipt = {
            "schema_version": "clockify-portfolio-post/v1",
            "status": "complete",
            "final_live_readback_sha256": "sha256:" + "b" * 64,
            "post_events": {
                "operation_identity": operation_identity,
                "entries": [{
                    "review_id": "review-1", "segment_index": 0, "disposition": "created",
                    "clockify_entry_id": "entry-1", "live_readback_digest": "sha256:" + "c" * 64,
                }],
            },
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
            "entry_ids": ["entry-1"], "entry_count": 1, "duration_seconds": 600,
            "entry_durations": {"entry-1": 600}, "native_costs": {"USD": "983.70", "EUR": "7.31"},
        })
        quote = clockify_currency.FxQuoteReceipt(
            provider="ECB", effective_date=quote_date, fetched_at=NOW, base_currency="EUR",
            rates={"USD": Decimal("1.1000")}, payload_digest="sha256:" + "d" * 64,
        )
        currency_summary = clockify_currency.convert_native_buckets(
            readback.native_costs, quote, publication_date=NOW.date(),
        ) if quote_date >= NOW.date() - timedelta(days=4) else None

        paths = {
            "slice": fixture_root / "run-report.json", "quality": fixture_root / "quality.json",
            "replay": fixture_root / "replay.json", "coverage": fixture_root / "coverage.json",
            "post": fixture_root / "post-receipt.json", "readback": fixture_root / "clockify-api-after.json",
            "quote": fixture_root / "fx-quote.json",
        }
        write_json(paths["slice"], slice_bundle)
        write_json(paths["quality"], quality)
        write_json(paths["replay"], replay)
        write_json(paths["coverage"], coverage)
        write_json(paths["post"], post_receipt)
        write_json(paths["readback"], readback.to_dict())
        write_json(paths["quote"], quote.to_dict())
        artifacts = [artifact(path) for key, path in paths.items() if key != "slice" or include_slice]
        events = reconciliation_manifest.CoordinatorEventStore(fixture_root / "period-events.jsonl")
        for sequence, event_type in enumerate((
            "period_opened", "collection_complete", "reconciliation_complete", "review_approved",
            "posting_started", "posting_complete", "clockify_readback_verified",
        )):
            payload: dict[str, object] = {}
            if sequence == 0:
                payload["artifacts"] = [item.document() for item in artifacts]
            events.append(self.identity, event_type, payload, occurred_at=NOW + timedelta(minutes=sequence))
        manifest = reconciliation_manifest.ReconciliationCoordinator(self.identity, events).derive()
        return {
            "manifest": manifest, "post_receipt": post_receipt, "clockify_readback": readback,
            "currency_summary": currency_summary, "fx_quote": quote, "quality": quality,
            "replay": replay, "coverage": coverage, "paths": paths,
        }

    def prepare(self, inputs: dict[str, object]) -> publication_gate.PublicationContract:
        return publication_gate.prepare_publication(
            inputs["manifest"], post_receipt=inputs["post_receipt"],
            clockify_readback=inputs["clockify_readback"], currency_summary=inputs["currency_summary"],
            fx_quote=inputs["fx_quote"], quality=inputs["quality"], replay=inputs["replay"],
            coverage=inputs["coverage"], now=NOW,
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
            "limitations": [{"source": "desktop", "reason": "unavailable", "approved": False}],
        })

        with self.assertRaisesRegex(publication_gate.PublicationGateError, "coverage"):
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

        with self.assertRaisesRegex(publication_gate.PublicationGateError, "manifest state"):
            publication_gate.prepare_publication(
                stale_manifest, post_receipt=inputs["post_receipt"], clockify_readback=inputs["clockify_readback"],
                currency_summary=inputs["currency_summary"], fx_quote=inputs["fx_quote"], quality=inputs["quality"],
                replay=inputs["replay"], coverage=inputs["coverage"], now=NOW,
            )

    def test_forged_manifest_object_digest_blocks_prepare(self) -> None:
        inputs = self.ready_inputs()
        manifest = inputs["manifest"]
        assert isinstance(manifest, reconciliation_manifest.ReconciliationManifest)
        forged = reconciliation_manifest.ReconciliationManifest(
            identity=manifest.identity, state=manifest.state, event_count=manifest.event_count,
            events_digest=manifest.events_digest, artifacts=manifest.artifacts, blockers=manifest.blockers,
            manifest_digest="sha256:" + "e" * 64,
        )

        with self.assertRaisesRegex(publication_gate.PublicationGateError, "manifest digest"):
            publication_gate.prepare_publication(
                forged, post_receipt=inputs["post_receipt"], clockify_readback=inputs["clockify_readback"],
                currency_summary=inputs["currency_summary"], fx_quote=inputs["fx_quote"], quality=inputs["quality"],
                replay=inputs["replay"], coverage=inputs["coverage"], now=NOW,
            )

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

        with self.assertRaisesRegex(publication_gate.PublicationGateError, "readback"):
            self.prepare(inputs)

    def test_stale_fx_quote_blocks_prepare(self) -> None:
        inputs = self.ready_inputs(quote_date=date(2026, 8, 13))
        quote = inputs["fx_quote"]
        readback = inputs["clockify_readback"]
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

    def test_cli_prepare_and_authorize_write_only_successful_contracts(self) -> None:
        inputs = self.ready_inputs()
        paths = inputs["paths"]
        assert isinstance(paths, dict)
        prepared_path = self.root / "publication-prepared.json"
        prepared_path.write_text("existing\n", encoding="utf-8")
        failure = publication_gate.main([
            "prepare", "--manifest", str(self.root / "missing-manifest.json"),
            "--post-receipt", str(paths["post"]), "--clockify-readback", str(paths["readback"]),
            "--fx-quote", str(paths["quote"]), "--quality", str(paths["quality"]),
            "--replay", str(paths["replay"]), "--coverage", str(paths["coverage"]),
            "--output", str(prepared_path),
        ], now=NOW)
        self.assertNotEqual(0, failure)
        self.assertEqual("existing\n", prepared_path.read_text(encoding="utf-8"))

        manifest_path = self.root / "period-manifest.json"
        write_json(manifest_path, inputs["manifest"].document())
        success = publication_gate.main([
            "prepare", "--manifest", str(manifest_path), "--post-receipt", str(paths["post"]),
            "--clockify-readback", str(paths["readback"]), "--fx-quote", str(paths["quote"]),
            "--quality", str(paths["quality"]), "--replay", str(paths["replay"]),
            "--coverage", str(paths["coverage"]), "--output", str(prepared_path),
        ], now=NOW)
        self.assertEqual(0, success)
        prepared = publication_gate.PublicationContract.from_document(json.loads(prepared_path.read_text(encoding="utf-8")))
        self.assertEqual("publication_prepared", prepared.state)

        approval_path = self.root / "publication-approval.json"
        authorized_path = self.root / "publication-authorized.json"
        write_json(approval_path, self.approval(prepared))
        self.assertEqual(0, publication_gate.main([
            "authorize", "--prepared", str(prepared_path), "--approval", str(approval_path),
            "--output", str(authorized_path),
        ], now=NOW))
        authorized = publication_gate.AuthorizedPublication.from_document(json.loads(authorized_path.read_text(encoding="utf-8")))
        self.assertEqual("publication_authorized", authorized.state)


if __name__ == "__main__":
    unittest.main()
