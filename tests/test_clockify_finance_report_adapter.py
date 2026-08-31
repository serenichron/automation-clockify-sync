from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from scripts import clockify_publication_gate as publication_gate
from scripts import reconciliation_manifest
from scripts import clockify_finance_report_adapter as scheduled_adapter
from scripts.publication_adapter_contract import PublicationReceiptStore
from tests.test_publication_adapter_contract import (
    FailSlackOnceAdapter,
    NOW,
    RecordingAdapter,
    authorized_publication,
)


SCHEDULE_NOW = datetime(2026, 8, 18, 9, 0, tzinfo=ZoneInfo("Europe/Bucharest"))


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


class ScheduledFinanceReportAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.authorized = authorized_publication()
        self.events_path = self.root / "period-events.jsonl"
        self.manifest_path = self.root / "period-manifest.json"
        self.authorization_path = self.root / "publication-authorized.json"
        self.receipts_path = self.root / "publication-receipts.jsonl"
        self._write_authorized_manifest()
        write_json(self.authorization_path, self.authorized.document())

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _write_authorized_manifest(self) -> None:
        identity = self.authorized.contract
        period = reconciliation_manifest.PeriodIdentity(
            member_id=identity.member_id,
            workspace_id=identity.workspace_id,
            timezone=identity.timezone,
            since=datetime.fromisoformat(identity.period_start.replace("Z", "+00:00")),
            until=datetime.fromisoformat(identity.period_end.replace("Z", "+00:00")),
            revision=identity.revision,
        )
        store = reconciliation_manifest.CoordinatorEventStore(self.events_path)
        events = (
            "period_opened", "collection_complete", "reconciliation_complete", "review_approved",
            "posting_started", "posting_complete", "clockify_readback_verified",
        )
        event_start = SCHEDULE_NOW - timedelta(minutes=2)
        for offset, event_type in enumerate(events):
            store.append(period, event_type, {}, occurred_at=event_start + timedelta(seconds=offset))
        binding = {
            "contract_digest": self.authorized.contract_digest,
            "idempotency_identity": self.authorized.idempotency_key,
        }
        store.append(period, "publication_prepared", binding, occurred_at=event_start + timedelta(seconds=10))
        store.append(period, "publication_authorized", binding, occurred_at=event_start + timedelta(seconds=11))
        write_json(self.manifest_path, reconciliation_manifest.ReconciliationCoordinator(period, store).derive().document())

    def _run(self, *, adapter: RecordingAdapter | None = None) -> tuple[int, dict[str, object]]:
        stdout = io.StringIO()
        factory = (lambda _: adapter) if adapter is not None else None
        with contextlib.redirect_stdout(stdout):
            code = scheduled_adapter.main([
                "execute", "--period-manifest", str(self.manifest_path), "--events", str(self.events_path),
                "--authorized", str(self.authorization_path), "--receipts", str(self.receipts_path),
            ], now=SCHEDULE_NOW, adapter_factory=factory)
        return code, json.loads(stdout.getvalue())

    def _derived_manifest(self) -> reconciliation_manifest.ReconciliationManifest:
        return reconciliation_manifest.ReconciliationManifest.from_document(json.loads(self.manifest_path.read_text(encoding="utf-8")))

    def test_unready_inputs_defer_without_constructing_an_external_adapter(self) -> None:
        """Constructing a gateway before authorization could leak a report or Slack mutation."""
        cases = {
            "missing": None,
            "prepared": self.authorized.contract.document(),
            "expired": self._expired_authorization(),
            "digest_drifted": self._digest_drifted_authorization(),
        }
        for name, document in cases.items():
            with self.subTest(name=name):
                if document is None:
                    self.authorization_path.unlink()
                else:
                    write_json(self.authorization_path, document)
                constructed = 0

                def factory(_: publication_gate.AuthorizedPublication) -> RecordingAdapter:
                    nonlocal constructed
                    constructed += 1
                    return RecordingAdapter()

                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    code = scheduled_adapter.main([
                        "execute", "--period-manifest", str(self.manifest_path), "--events", str(self.events_path),
                        "--authorized", str(self.authorization_path), "--receipts", str(self.receipts_path),
                    ], now=SCHEDULE_NOW, adapter_factory=factory)
                result = json.loads(stdout.getvalue())

                self.assertEqual(0, code)
                self.assertEqual("publication_deferred", result["action"])
                self.assertEqual(0, constructed)
                self.assertIn("publication_deferred", self._derived_manifest().blockers)
                self.events_path.unlink()
                self.manifest_path.unlink()
                self._write_authorized_manifest()
                write_json(self.authorization_path, self.authorized.document())

    def _expired_authorization(self) -> dict[str, object]:
        document = self.authorized.document()
        document["expires_at"] = "2026-08-18T05:59:00Z"
        document["authorization_digest"] = publication_gate._digest({
            key: value for key, value in document.items() if key != "authorization_digest"
        })
        return document

    def _digest_drifted_authorization(self) -> dict[str, object]:
        document = self.authorized.document()
        contract = dict(document["contract"])
        contract["duration_seconds"] = 599
        document["contract"] = contract
        return document

    def test_report_mismatch_records_blocker_and_never_calls_slack(self) -> None:
        """Calling Slack after a wrong report readback would publish incorrect financial totals."""
        adapter = RecordingAdapter(report_duration=590)

        code, result = self._run(adapter=adapter)

        self.assertNotEqual(0, code)
        self.assertEqual("report_mismatch", result["action"])
        self.assertEqual(["update_report", "read_report"], adapter.calls)
        self.assertIn("report_mismatch", self._derived_manifest().blockers)

    def test_report_success_then_slack_failure_retries_only_slack(self) -> None:
        """A retry that refreshes the report again could create a duplicate external correction."""
        adapter = FailSlackOnceAdapter()

        first_code, _ = self._run(adapter=adapter)
        second_code, result = self._run(adapter=adapter)

        self.assertNotEqual(0, first_code)
        self.assertEqual(0, second_code)
        self.assertEqual("published", result["action"])
        self.assertEqual(1, adapter.update_report_count)
        self.assertEqual(2, adapter.slack_count)

    def test_valid_authorization_resumes_after_a_deferred_attempt(self) -> None:
        """Leaving the deferred blocker active would make a later board-approved retry impossible."""
        self.authorization_path.unlink()
        deferred_code, _ = self._run()
        write_json(self.authorization_path, self.authorized.document())

        published_code, result = self._run(adapter=RecordingAdapter())

        self.assertEqual(0, deferred_code)
        self.assertEqual(0, published_code)
        self.assertEqual("published", result["action"])
        self.assertEqual((), self._derived_manifest().blockers)

    def test_production_invocation_without_exact_transport_configuration_defers_before_external_work(self) -> None:
        """Creating a live gateway without its reviewed transport configuration could send an unapproved publication."""
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = scheduled_adapter.main([
                "execute", "--period-manifest", str(self.manifest_path), "--events", str(self.events_path),
                "--authorized", str(self.authorization_path), "--receipts", str(self.receipts_path),
            ], now=SCHEDULE_NOW, environment={})

        self.assertEqual(0, code)
        self.assertEqual("publication_deferred", json.loads(stdout.getvalue())["action"])
        self.assertFalse(self.receipts_path.exists())

    def test_configured_but_unimplemented_production_transport_still_defers_without_a_call(self) -> None:
        """A placeholder provider must not turn credentials into an attempted external mutation."""
        environment = {
            "CLOCKIFY_API_KEY": "clockify-test",
            "SLACK_BOT_TOKEN": "xoxb-test",
            "CLOCKIFY_FINANCE_REPORT_TARGET": self.authorized.contract.report_target,
            "CLOCKIFY_FINANCE_SLACK_TARGET": self.authorized.contract.slack_target,
        }
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = scheduled_adapter.main([
                "execute", "--period-manifest", str(self.manifest_path), "--events", str(self.events_path),
                "--authorized", str(self.authorization_path), "--receipts", str(self.receipts_path),
            ], now=SCHEDULE_NOW, environment=environment)

        self.assertEqual(0, code)
        result = json.loads(stdout.getvalue())
        self.assertEqual("publication_deferred", result["action"])
        self.assertEqual("transport_unavailable", result["reason"])
        self.assertFalse(self.receipts_path.exists())

    def test_success_appends_bound_events_and_derives_published(self) -> None:
        """Unbound completion events could claim publication without persisted external proof."""
        adapter = RecordingAdapter()

        code, result = self._run(adapter=adapter)

        self.assertEqual(0, code)
        self.assertEqual("published", result["action"])
        events = reconciliation_manifest.CoordinatorEventStore(self.events_path).verify(self._derived_manifest().identity)
        self.assertEqual(["shared_report_verified", "publication_complete"], [event.event_type for event in events[-2:]])
        publication = PublicationReceiptStore(self.receipts_path).publication_receipt(self.authorized.idempotency_key)
        assert publication is not None
        self.assertEqual(publication.report_receipt.document(), events[-2].payload["report_receipt"])
        self.assertEqual(publication.report_receipt.document(), events[-1].payload["report_receipt"])
        self.assertEqual(publication.slack_receipt.document(), events[-1].payload["slack_receipt"])
        self.assertEqual("published", self._derived_manifest().state)

    def test_published_period_returns_its_receipt_without_constructing_another_adapter(self) -> None:
        """A recurring runner that republishes a completed period would duplicate an approved client-facing correction."""
        first_code, _ = self._run(adapter=RecordingAdapter())
        constructed = 0

        def factory(_: publication_gate.AuthorizedPublication) -> RecordingAdapter:
            nonlocal constructed
            constructed += 1
            return RecordingAdapter()

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = scheduled_adapter.main([
                "execute", "--period-manifest", str(self.manifest_path), "--events", str(self.events_path),
                "--authorized", str(self.authorization_path), "--receipts", str(self.receipts_path),
            ], now=SCHEDULE_NOW, adapter_factory=factory)

        self.assertEqual(0, first_code)
        self.assertEqual(0, code)
        self.assertEqual("published", json.loads(stdout.getvalue())["action"])
        self.assertEqual(0, constructed)






if __name__ == "__main__":
    unittest.main()
