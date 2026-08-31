from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from scripts import clockify_currency
from scripts import clockify_publication_gate as publication_gate
from scripts import publication_adapter_contract
from scripts import reconciliation_manifest
from scripts.publication_adapter_contract import (
    PublicationAdapterError,
    PublicationReceiptStore,
    SharedReportReceipt,
    SlackReceipt,
    execute_authorized_publication,
)


NOW = datetime(2026, 8, 18, 9, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(minutes=5)


def digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def authorized_publication(
    *, revision: int = 1, slack_target: str = "slack:finance-report:workspace-1:member-1",
) -> publication_gate.AuthorizedPublication:
    identity = reconciliation_manifest.PeriodIdentity(
        member_id="member-1",
        workspace_id="workspace-1",
        timezone="Europe/Bucharest",
        since=datetime(2026, 8, 1, tzinfo=timezone.utc),
        until=datetime(2026, 8, 16, tzinfo=timezone.utc),
        revision=revision,
    )
    quote = clockify_currency.FxQuoteReceipt(
        provider="ECB",
        effective_date=date(2026, 8, 18),
        fetched_at=NOW,
        base_currency="EUR",
        rates={"USD": Decimal("1.1000")},
        payload_digest=digest("quote"),
    )
    contract = publication_gate.PublicationContract(
        period_id=identity.period_id,
        workspace_id=identity.workspace_id,
        member_id=identity.member_id,
        timezone=identity.timezone,
        period_start=identity.document()["since_utc"],
        period_end=identity.document()["until_utc"],
        revision=revision,
        manifest_digest=digest("manifest"),
        event_history_digest=digest("events"),
        post_receipt_digest=digest("post"),
        api_readback_digest=digest("api"),
        shared_report_readback_digest=digest("report"),
        api_readback_refreshed_at="2026-08-18T08:56:00Z",
        shared_report_readback_refreshed_at="2026-08-18T08:57:00Z",
        duration_seconds=600,
        native_buckets={"USD": Decimal("10.00")},
        usd_buckets={"USD": Decimal("10.00")},
        usd_equivalent_total=Decimal("10.00"),
        fx_quote=quote,
        report_target="shared-report:report-1",
        slack_target=slack_target,
        idempotency_key=publication_gate.publication_idempotency_key(identity),
        contract_digest=digest("placeholder-contract"),
    )
    contract = replace(contract, contract_digest=publication_gate._digest(contract._unsigned_document()))
    authorized = publication_gate.AuthorizedPublication(
        contract=contract,
        approval_id="approval-1",
        approval_digest=digest("approval"),
        approver="board",
        expires_at="2026-08-19T09:00:00Z",
        operations=("shared_report_update", "slack_correction"),
        authorization_digest=digest("placeholder-authorization"),
    )
    return replace(authorized, authorization_digest=publication_gate._digest(authorized._unsigned_document()))


class RecordingAdapter:
    def __init__(self, *, report_duration: int = 600) -> None:
        self.calls: list[str] = []
        self.report_duration = report_duration
        self.update_report_count = 0
        self.slack_count = 0

    def update_report(self, authorized: publication_gate.AuthorizedPublication) -> None:
        self.calls.append("update_report")
        self.update_report_count += 1

    def read_report(self, authorized: publication_gate.AuthorizedPublication) -> SharedReportReceipt:
        self.calls.append("read_report")
        contract = authorized.contract
        return SharedReportReceipt(
            contract_digest=authorized.contract_digest,
            idempotency_identity=authorized.idempotency_key,
            report_target=contract.report_target,
            readback_digest=digest(f"report-{self.report_duration}"),
            duration_seconds=self.report_duration,
            native_buckets=contract.native_buckets,
            verified_at=NOW,
        )

    def upsert_slack(
        self,
        authorized: publication_gate.AuthorizedPublication,
        report_receipt: SharedReportReceipt,
    ) -> SlackReceipt:
        self.calls.append("upsert_slack")
        self.slack_count += 1
        return SlackReceipt(
            contract_digest=authorized.contract_digest,
            idempotency_identity=authorized.idempotency_key,
            slack_target=authorized.contract.slack_target,
            content_digest=digest("finance-report-content"),
            message_id="message-1",
            verified_at=NOW,
        )


class FailSlackOnceAdapter(RecordingAdapter):
    def upsert_slack(
        self,
        authorized: publication_gate.AuthorizedPublication,
        report_receipt: SharedReportReceipt,
    ) -> SlackReceipt:
        self.calls.append("upsert_slack")
        self.slack_count += 1
        if self.slack_count == 1:
            raise RuntimeError("temporary Slack transport failure")
        return SlackReceipt(
            contract_digest=authorized.contract_digest,
            idempotency_identity=authorized.idempotency_key,
            slack_target=authorized.contract.slack_target,
            content_digest=digest("finance-report-content"),
            message_id="message-1",
            verified_at=LATER,
        )


class PublicationAdapterContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.store = PublicationReceiptStore(Path(self.temporary_directory.name) / "publication-receipts.jsonl")
        self.authorized = authorized_publication()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_report_readback_passes_before_slack_call(self) -> None:
        """Skipping exact report verification would permit Slack to advertise wrong totals."""
        adapter = RecordingAdapter()

        receipt = execute_authorized_publication(self.authorized, adapter, self.store, now=NOW)

        self.assertEqual(["update_report", "read_report", "upsert_slack"], adapter.calls)
        self.assertEqual("published", receipt.state)

    def test_report_mismatch_makes_no_slack_call(self) -> None:
        """Accepting mismatched report duration would publish an inconsistent finance summary."""
        adapter = RecordingAdapter(report_duration=590)

        with self.assertRaises(PublicationAdapterError):
            execute_authorized_publication(self.authorized, adapter, self.store, now=NOW)

        self.assertEqual(["update_report", "read_report"], adapter.calls)

    def test_report_success_slack_failure_retries_only_slack(self) -> None:
        """Repeating the report refresh after a Slack-only failure would duplicate a correction."""
        adapter = FailSlackOnceAdapter()

        with self.assertRaises(PublicationAdapterError):
            execute_authorized_publication(self.authorized, adapter, self.store, now=NOW)
        receipt = execute_authorized_publication(self.authorized, adapter, self.store, now=LATER)

        self.assertEqual("published", receipt.state)
        self.assertEqual(1, adapter.update_report_count)
        self.assertEqual(2, adapter.slack_count)

    def test_same_identity_returns_the_persisted_publication_without_external_calls(self) -> None:
        """A rerun that publishes again would create duplicate client-facing Slack updates."""
        adapter = RecordingAdapter()
        first = execute_authorized_publication(self.authorized, adapter, self.store, now=NOW)

        second = execute_authorized_publication(self.authorized, adapter, self.store, now=LATER)

        self.assertEqual(first, second)
        self.assertEqual(1, adapter.update_report_count)
        self.assertEqual(1, adapter.slack_count)

    def test_receipt_store_rejects_a_rehashed_record_that_no_longer_matches_its_anchor(self) -> None:
        """Recomputing a journal line alone must not make a tampered external receipt appear valid."""
        report = RecordingAdapter().read_report(self.authorized)
        self.store.persist_report(report)
        record = json.loads(self.store.path.read_text(encoding="utf-8"))
        record["receipt"]["duration_seconds"] = 599
        record["record_digest"] = publication_adapter_contract._digest({
            key: value for key, value in record.items() if key != "record_digest"
        })
        self.store.path.write_text(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")

        with self.assertRaises(PublicationAdapterError):
            self.store.verify()

    def test_new_revision_keeps_the_prior_published_receipt(self) -> None:
        """Replacing receipt history would erase the audit trail for a corrected reporting period."""
        first_adapter = RecordingAdapter()
        first = execute_authorized_publication(self.authorized, first_adapter, self.store, now=NOW)
        correction = authorized_publication(revision=2)

        second = execute_authorized_publication(correction, RecordingAdapter(), self.store, now=LATER)

        self.assertNotEqual(first.idempotency_identity, second.idempotency_identity)
        self.assertEqual([first, second], self.store.published_receipts())

    def test_target_drift_for_the_same_identity_is_rejected_before_any_retry_call(self) -> None:
        """Reusing a receipt for a changed report target would redirect an approved correction."""
        execute_authorized_publication(self.authorized, RecordingAdapter(), self.store, now=NOW)
        contract = replace(self.authorized.contract, report_target="shared-report:other", contract_digest=digest("placeholder"))
        contract = replace(contract, contract_digest=publication_gate._digest(contract._unsigned_document()))
        altered = replace(self.authorized, contract=contract, authorization_digest=digest("placeholder"))
        altered = replace(altered, authorization_digest=publication_gate._digest(altered._unsigned_document()))
        adapter = RecordingAdapter()

        with self.assertRaises(PublicationAdapterError):
            execute_authorized_publication(altered, adapter, self.store, now=LATER)

        self.assertEqual([], adapter.calls)

    def test_prepared_contract_is_rejected_without_adapter_calls(self) -> None:
        """Treating a prepared contract as authorization would bypass the approval gate."""
        adapter = RecordingAdapter()

        with self.assertRaises(PublicationAdapterError):
            execute_authorized_publication(self.authorized.contract, adapter, self.store, now=NOW)  # type: ignore[arg-type]

        self.assertEqual([], adapter.calls)

    def test_receipt_schema_accepts_each_persisted_receipt_kind(self) -> None:
        """A schema that omits a receipt kind would make an otherwise valid audit journal unverifiable."""
        publication = execute_authorized_publication(self.authorized, RecordingAdapter(), self.store, now=NOW)
        schema = Path(__file__).parents[1] / "schemas/publication-receipt-v1.json"
        for receipt in (publication.report_receipt, publication.slack_receipt, publication):
            with self.subTest(kind=receipt.document()["kind"]):
                result = subprocess.run(
                    ["jsonschema", str(schema)], input=json.dumps(receipt.document()),
                    text=True, capture_output=True, check=False,
                )
                self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
