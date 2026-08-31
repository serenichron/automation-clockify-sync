from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest

from scripts import clockify_currency
from scripts import clockify_finance_report_adapter as scheduled_adapter
from scripts import clockify_period_readback
from scripts import clockify_publication_gate as publication_gate
from scripts import posting_receipts
from scripts import reconciliation_manifest
from scripts.publication_adapter_contract import (
    SharedReportReceipt,
    SlackReceipt,
    expected_slack_content_digest,
)


NOW = datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)
FIXTURES = Path(__file__).parent / "fixtures" / "reconciliation"


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def _artifact(path: Path) -> reconciliation_manifest.ArtifactIdentity:
    return reconciliation_manifest.ArtifactIdentity(
        path=path.resolve(), schema_version="fixture/v1", compatibility_version="fixture/v1",
        digest=_digest_bytes(path.read_bytes()),
    )


@dataclass
class RecordingTransport:
    fail_first_slack: bool = False
    update_report_count: int = 0
    slack_count: int = 0

    def update_report(self, authorized: publication_gate.AuthorizedPublication) -> None:
        self.update_report_count += 1

    def read_report(self, authorized: publication_gate.AuthorizedPublication) -> SharedReportReceipt:
        contract = authorized.contract
        return SharedReportReceipt(
            contract_digest=authorized.contract_digest,
            authorization_digest=authorized.authorization_digest,
            idempotency_identity=authorized.idempotency_key,
            report_target=contract.report_target,
            readback_digest=_digest_bytes(b"fixture-shared-report-readback"),
            duration_seconds=contract.duration_seconds,
            native_buckets=contract.native_buckets,
            verified_at=NOW,
        )

    def upsert_slack(
        self, authorized: publication_gate.AuthorizedPublication, report: SharedReportReceipt,
    ) -> SlackReceipt:
        self.slack_count += 1
        if self.fail_first_slack and self.slack_count == 1:
            raise RuntimeError("transient fixture Slack failure")
        return SlackReceipt(
            contract_digest=authorized.contract_digest,
            idempotency_identity=authorized.idempotency_key,
            slack_target=authorized.contract.slack_target,
            content_digest=expected_slack_content_digest(authorized),
            message_id="fixture-slack-message-1",
            verified_at=NOW,
        )


@dataclass(frozen=True)
class FixtureResult:
    state: str
    reason: str | None
    adapter: RecordingTransport
    currency: clockify_currency.CurrencySummary | None


def load_fixture(name: str) -> dict[str, object]:
    value = json.loads((FIXTURES / name / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("fixture manifest must be an object")
    return value


def _identity(fixture: dict[str, object]) -> reconciliation_manifest.PeriodIdentity:
    period = fixture["period"]
    if not isinstance(period, dict):
        raise ValueError("fixture period is invalid")
    return reconciliation_manifest.PeriodIdentity(
        member_id=str(period["member_id"]), workspace_id=str(period["workspace_id"]),
        timezone=str(period["timezone"]), since=datetime.fromisoformat(str(period["since"])),
        until=datetime.fromisoformat(str(period["until"])), revision=int(period["revision"]),
    )


def _coverage(
    fixture: dict[str, object], identity: reconciliation_manifest.PeriodIdentity, stage: str,
) -> dict[str, object]:
    specification = fixture["coverage"]
    if not isinstance(specification, dict):
        raise ValueError("fixture coverage is invalid")
    required = specification["required_slices"]
    if not isinstance(required, list):
        raise ValueError("fixture required slices are invalid")
    if stage == "transient_debt":
        return {
            "status": "complete",
            "source_completeness": {"status": "complete", "incomplete_sources": []},
            "limitations": [], "required_slices": required,
        }
    if stage == "coverage_complete":
        return {
            "status": "incomplete",
            "source_completeness": {"status": "incomplete", "incomplete_sources": ["desktop"]},
            "limitations": [], "required_slices": required,
        }
    limitation = specification.get("desktop_limitation")
    if limitation is None:
        return {
            "status": "complete",
            "source_completeness": {"status": "complete", "incomplete_sources": []},
            "limitations": [], "required_slices": required,
        }
    if not isinstance(limitation, dict):
        raise ValueError("approved fixture requires desktop limitation")
    period = identity.document()
    return {
        "status": "incomplete",
        "source_completeness": {"status": "incomplete", "incomplete_sources": ["desktop"]},
        "limitations": [{
            "approval_id": limitation["approval_id"], "approver": limitation["approver"],
            "approved_at": limitation["approved_at"], "period_id": identity.period_id,
            "period_start": period["since_utc"], "period_end": period["until_utc"],
            "source": limitation["source"], "coverage_digest": "sha256:" + "d" * 64,
        }],
        "required_slices": required,
    }


def _slice_bundle(
    fixture: dict[str, object], identity: reconciliation_manifest.PeriodIdentity, slice_specification: dict[str, object],
) -> dict[str, object]:
    return {
        "run_id": "fixture-" + str(fixture["scenario"]),
        "slice_id": slice_specification["slice_id"],
        "target": {
            "period_id": identity.period_id, "workspace_id": identity.workspace_id,
            "member_id": identity.member_id,
        },
        "date_range": {"since": slice_specification["since"], "until": slice_specification["until"]},
        "evidence_ledger": {"source_completeness": {"status": "complete", "incomplete_sources": []}},
        "evidence": {"calendly": {"status": "ok", "complete": True, "recordings": fixture.get("recordings", [])}},
        "meeting_accounting": {"status": "complete", "unresolved_exceptions": []},
        "semantic_exceptions": [], "routing_exceptions": [], "overlap_exceptions": [], "billability_exceptions": [],
    }


def _authorization(contract: publication_gate.PublicationContract) -> dict[str, object]:
    return {
        "schema_version": "publication-approval/v1", "approval_id": "fixture-publication-approval-1",
        "approver": "board", "approved_at": "2026-08-18T08:00:00Z",
        "expires_at": "2026-08-19T08:00:00Z",
        "operations": ["shared_report_update", "slack_correction"],
        "report_target": contract.report_target, "slack_target": contract.slack_target,
        "period_id": contract.period_id, "workspace_id": contract.workspace_id,
        "member_id": contract.member_id, "period_start": contract.period_start,
        "period_end": contract.period_end, "revision": contract.revision,
        "contract_digest": contract.contract_digest, "idempotency_key": contract.idempotency_key,
    }


def _run_scheduler(
    events_path: Path, manifest_path: Path, authorization_path: Path,
    receipts_path: Path, adapter: RecordingTransport,
) -> tuple[int, dict[str, object]]:
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = scheduled_adapter.main([
            "execute", "--period-manifest", str(manifest_path), "--events", str(events_path),
            "--authorized", str(authorization_path), "--receipts", str(receipts_path),
        ], now=NOW, adapter_factory=lambda _: adapter)
    return code, json.loads(output.getvalue())


def run_fixture(name: str, *, stage: str = "approved") -> FixtureResult:
    """Drive actual coordinator, receipt, readback, currency, gate, and adapter contracts."""
    fixture = load_fixture(name)
    identity = _identity(fixture)
    coverage = _coverage(fixture, identity, stage)
    required = coverage["required_slices"]
    if not isinstance(required, list):
        raise ValueError("coverage required slices are invalid")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        paths = {
            "quality": root / "quality.json", "replay": root / "replay.json",
            "coverage": root / "coverage.json", "quote": root / "fx-quote.json",
            "approval": root / "approval-events.jsonl", "post_events": root / "post-events.jsonl",
            "post": root / "post-receipt.json", "api": root / "clockify-api-after.json",
            "report": root / "shared-report-after.json", "events": root / "period-events.jsonl",
        }
        slice_specs = required[:1] if stage == "transient_debt" else required
        slice_paths: list[Path] = []
        for index, value in enumerate(slice_specs):
            if not isinstance(value, dict):
                raise ValueError("fixture slice is invalid")
            path = root / f"slice-{index}.json"
            _write_json(path, _slice_bundle(fixture, identity, value))
            slice_paths.append(path)
        quality = {"status": "pass"}
        replay = {"status": "pass", "identity": {"artifacts": {"quality": "bound"}}}
        quote = clockify_currency.FxQuoteReceipt(
            provider="ECB", effective_date=date(2026, 8, 18), fetched_at=NOW,
            base_currency="EUR", rates={"USD": Decimal("1.1000")}, payload_digest="sha256:" + "e" * 64,
        )
        _write_json(paths["quality"], quality)
        _write_json(paths["replay"], replay)
        _write_json(paths["coverage"], coverage)
        _write_json(paths["quote"], quote.to_dict())

        events = reconciliation_manifest.CoordinatorEventStore(paths["events"])
        initial_artifacts = [_artifact(path) for path in (*slice_paths, paths["quality"], paths["replay"], paths["coverage"], paths["quote"])]
        events.append(identity, "period_opened", {"artifacts": [item.document() for item in initial_artifacts]}, occurred_at=NOW - timedelta(minutes=10))
        for limitation in coverage["limitations"]:
            events.append(identity, "coverage_limitation_approved", limitation, occurred_at=NOW - timedelta(minutes=9, seconds=30))
        for offset, event_type in zip((8, 7, 6), ("collection_complete", "reconciliation_complete", "review_approved")):
            events.append(identity, event_type, {}, occurred_at=NOW - timedelta(minutes=offset))

        operation_identity = posting_receipts.derive_operation_identity(
            operation="clockify_post", period_id=identity.period_id,
            workspace_id=identity.workspace_id, member_id=identity.member_id,
        )
        approval_store = posting_receipts.ApprovalReceiptStore(paths["approval"])
        period = identity.document()
        approved_prefix = reconciliation_manifest.ReconciliationCoordinator(identity, events).derive()
        post_approval = posting_receipts.ApprovalReceipt(
            approval_id="fixture-post-approval-1", approver="board", approved_at="2026-08-18T08:00:00Z",
            expires_at="2026-08-19T08:00:00Z", operation="clockify_post", operation_identity=operation_identity,
            period_id=identity.period_id, period_start=period["since_utc"], period_end=period["until_utc"],
            workspace_id=identity.workspace_id, member_id=identity.member_id,
            portfolio_digest=_digest_bytes(b"portfolio"), quality_digest=_digest_bytes(b"quality"),
            replay_digest=_digest_bytes(b"replay"), routing_digest=_digest_bytes(b"routing"),
            correction_log_digest=_digest_bytes(b"retained-correction-revision"), coverage_digest=_digest_bytes(b"coverage"),
            residual_exception_digest=_digest_bytes(b"exceptions"),
            manifest_digest=approved_prefix.manifest_digest, event_history_digest=approved_prefix.events_digest,
            historical_receipt_digest=_digest_bytes(b"historical"), max_create_count=2,
        )
        approval_store.append(post_approval)
        approval_store.consume(post_approval.approval_id, operation_identity=operation_identity, consumed_at="2026-08-18T08:01:00Z")
        post_store = posting_receipts.PostEventStore(paths["post_events"])
        posting = fixture.get("posting", {})
        recovery = posting.get("ambiguous_post_recovery", {}) if isinstance(posting, dict) else {}
        recovered_disposition = recovery.get("disposition", "created") if isinstance(recovery, dict) else "created"
        for index, disposition in enumerate(("created", recovered_disposition), start=1):
            review_id = f"review-{name}-{index}"
            post_store.append(posting_receipts.PostEvent(
                disposition="planned", operation="clockify_post", operation_identity=operation_identity,
                period_id=identity.period_id, workspace_id=identity.workspace_id, member_id=identity.member_id,
                review_id=review_id, segment_index=0, recorded_at=f"2026-08-18T08:0{index}:00Z",
            ))
            post_store.append(posting_receipts.PostEvent(
                disposition=disposition, operation="clockify_post", operation_identity=operation_identity,
                period_id=identity.period_id, workspace_id=identity.workspace_id, member_id=identity.member_id,
                review_id=review_id, segment_index=0, recorded_at=f"2026-08-18T08:1{index}:00Z",
                clockify_entry_id=f"fixture-entry-{index}", live_readback_digest="sha256:" + str(index) * 64,
            ))
        api = clockify_period_readback.normalize_readback({
            "schema_version": "clockify-period-readback/v1", "evidence_kind": "ledger",
            "workspace_id": identity.workspace_id, "member_id": identity.member_id, "timezone": identity.timezone,
            "period_start": period["since_utc"], "period_end": period["until_utc"],
            "filters": {"workspace_id": identity.workspace_id, "member_id": identity.member_id,
                        "timezone": identity.timezone, "include_running": False, "include_deleted": False},
            "refreshed_at": "2026-08-18T08:55:00Z", "include_running": False, "include_deleted": False,
            "entry_ids": ["fixture-entry-1", "fixture-entry-2"], "entry_count": 2,
            "duration_seconds": 1800, "entry_durations": {"fixture-entry-1": 600, "fixture-entry-2": 1200},
            "native_costs": {"USD": "10.00", "EUR": "5.47"}, "final_live_readback_sha256": "sha256:" + "a" * 64,
        })
        post_receipt = {"schema_version": "clockify-portfolio-post/v1", "status": "complete",
                        "final_live_readback_sha256": api.final_live_readback_sha256,
                        "post_events": post_store.derive_receipt(operation_identity)}
        raw_report = {"totals": [{"entriesCount": 2, "totalTime": 1800, "amounts": [{"amountByCurrency": [
            {"currency": "USD", "amount": "1000"}, {"currency": "EUR", "amount": "547"},
        ]}]}]}
        publication = fixture["publication"]
        if not isinstance(publication, dict):
            raise ValueError("fixture publication is invalid")
        report_receipt = {"workspace_id": identity.workspace_id, "member_id": identity.member_id,
                          "period_start": period["since_utc"], "period_end": period["until_utc"],
                          "filters": dict(api.filters), "shared_report_id": str(publication["report_target"]).split(":", 1)[1],
                          "raw_response_digest": clockify_period_readback._digest(clockify_period_readback._normalize_report_projection(raw_report))}
        report = clockify_period_readback.normalize_readback({
            **api.document(), "evidence_kind": "report", "refreshed_at": "2026-08-18T08:56:00Z",
            "request_receipt": report_receipt, "raw_response": raw_report,
        })
        _write_json(paths["post"], post_receipt)
        _write_json(paths["api"], api.to_dict())
        _write_json(paths["report"], report.to_dict())
        events.append(identity, "posting_started", {}, occurred_at=NOW - timedelta(minutes=5))
        posting_artifacts = [_artifact(paths[key]) for key in ("approval", "post_events", "post", "api", "report")]
        events.append(identity, "posting_complete", {"artifacts": [item.document() for item in posting_artifacts]}, occurred_at=NOW - timedelta(minutes=4))
        events.append(identity, "clockify_readback_verified", {}, occurred_at=NOW - timedelta(minutes=3))
        manifest = reconciliation_manifest.ReconciliationCoordinator(identity, events).derive()
        summary = clockify_currency.convert_native_buckets(report.native_costs, quote, publication_date=NOW.date())
        try:
            contract = publication_gate.prepare_publication(
                manifest, events=paths["events"], approval_receipt_id=post_approval.approval_id,
                approval_events=paths["approval"], post_events=paths["post_events"], post_receipt=post_receipt,
                api_readback=api, shared_report_readback=report, currency_summary=summary, fx_quote=quote,
                quality=quality, replay=replay, coverage=coverage,
                slack_target=str(publication["slack_target"]), now=NOW,
            )
        except publication_gate.PublicationGateError as error:
            return FixtureResult("publication_deferred", str(error), RecordingTransport(), None)

        binding = {"contract_digest": contract.contract_digest, "idempotency_identity": contract.idempotency_key}
        events.append(identity, "publication_prepared", binding, occurred_at=NOW - timedelta(minutes=2))
        authorized = publication_gate.authorize_publication(contract, _authorization(contract), now=NOW)
        events.append(identity, "publication_authorized", binding, occurred_at=NOW - timedelta(minutes=1))
        manifest_path = root / "period-manifest.json"
        authorization_path = root / "publication-authorized.json"
        receipts_path = root / "publication-receipts.jsonl"
        _write_json(manifest_path, reconciliation_manifest.ReconciliationCoordinator(identity, events).derive().document())
        _write_json(authorization_path, authorized.document())
        adapter = RecordingTransport(fail_first_slack=bool(publication.get("report_success_slack_failure_retry")))
        first_code, first = _run_scheduler(paths["events"], manifest_path, authorization_path, receipts_path, adapter)
        if adapter.fail_first_slack:
            if first_code == 0 or first.get("action") != "publication_incomplete":
                raise AssertionError("fixture Slack failure must retain the report receipt for retry")
            _, result = _run_scheduler(paths["events"], manifest_path, authorization_path, receipts_path, adapter)
        else:
            result = first
        _, replay_result = _run_scheduler(paths["events"], manifest_path, authorization_path, receipts_path, adapter)
        if replay_result.get("action") != "published":
            raise AssertionError("published fixture must be idempotently readable")
        return FixtureResult(str(result["action"]), None, adapter, summary)


def run_until_coverage_complete(fixture: dict[str, object]) -> FixtureResult:
    return run_fixture(str(fixture["scenario"]), stage="coverage_complete")


def run_after_approved_desktop_limitation(fixture: dict[str, object]) -> FixtureResult:
    return run_fixture(str(fixture["scenario"]), stage="approved")


class PublicationEndToEndTests(unittest.TestCase):
    def test_routine_two_day_period_publishes_after_exact_receipts(self) -> None:
        result = run_fixture("publication-routine")
        self.assertEqual("published", result.state)
        self.assertEqual(1, result.adapter.slack_count)
        self.assertEqual(1, result.adapter.update_report_count)

    def test_exceptional_backlog_defers_until_all_slices_and_limitations_are_approved(self) -> None:
        fixture = load_fixture("publication-backlog")
        recordings = fixture["recordings"]
        self.assertEqual(["calendly"], recordings[0]["sources"])
        self.assertEqual(["fathom", "calendly"], recordings[1]["sources"])
        self.assertEqual(2, fixture["posting"]["retained_correction_revision"])
        self.assertEqual("publication_deferred", run_fixture("publication-backlog", stage="transient_debt").state)
        deferred = run_until_coverage_complete(fixture)
        self.assertEqual("publication_deferred", deferred.state)
        self.assertIn("coverage is incomplete", deferred.reason or "")
        final = run_after_approved_desktop_limitation(fixture)
        self.assertEqual("published", final.state)
        self.assertEqual({"USD", "EUR"}, set(final.currency.native_buckets))
        self.assertEqual(1, final.adapter.update_report_count)
        self.assertEqual(2, final.adapter.slack_count)


if __name__ == "__main__":
    unittest.main()
