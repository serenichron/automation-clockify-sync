from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from unittest import mock

from scripts import clockify_period_readback as readback


TZ = "+03:00"
START = "2026-08-01T00:00:00+03:00"
END = "2026-08-16T00:00:00+03:00"
API_REFRESH = "2026-08-31T12:00:00+03:00"


def _entry(number: int, seconds: int = 600, *, currency: str = "USD", amount: str = "0.00") -> dict:
    start = f"2026-08-{(number % 14) + 1:02d}T09:00:00+03:00"
    return {
        "id": f"entry-{number:03d}",
        "userId": "member-1",
        "workspaceId": "workspace-1",
        "timeInterval": {"start": start, "end": start, "duration": seconds},
        "duration_seconds": seconds,
        "cost": {"amount": amount, "currency": currency},
    }


def _api_fixture() -> dict:
    entries = [_entry(index, 1217 if index == 0 else 780 if index == 171 else 766) for index in range(172)]
    entries[0]["cost"] = {"amount": "983.70", "currency": "USD"}
    entries[1]["cost"] = {"amount": "7.31", "currency": "EUR"}
    # The interval values are intentionally not used for duration arithmetic;
    # a valid fixture needs concrete end times for the contract.
    for index, entry in enumerate(entries):
        day = (index % 14) + 1
        entry["timeInterval"]["start"] = f"2026-08-{day:02d}T09:00:00+03:00"
        end_seconds = entry["duration_seconds"]
        end_minutes, end_remainder = divmod(end_seconds, 60)
        entry["timeInterval"]["end"] = f"2026-08-{day:02d}T{9 + end_minutes // 60:02d}:{end_minutes % 60:02d}:{end_remainder:02d}+03:00"
    return {
        "schema_version": "clockify-period-readback/v1",
        "workspace_id": "workspace-1",
        "member_id": "member-1",
        "timezone": "Europe/Bucharest",
        "period_start": START,
        "period_end": END,
        "filters": {
            "workspace_id": "workspace-1",
            "member_id": "member-1",
            "timezone": "Europe/Bucharest",
            "include_running": True,
            "include_deleted": True,
        },
        "include_running": True,
        "include_deleted": True,
        "refreshed_at": API_REFRESH,
        "native_costs": {"USD": "983.70", "EUR": "7.31"},
        "entries": entries,
    }


def _report_from(api: dict, *, seconds: int | None = None, **changes: object) -> dict:
    result = {
        "schema_version": "clockify-period-readback/v1",
        "workspace_id": api["workspace_id"],
        "member_id": api["member_id"],
        "timezone": api["timezone"],
        "period_start": api["period_start"],
        "period_end": api["period_end"],
        "filters": dict(api["filters"]),
        "include_running": api.get("include_running", False),
        "include_deleted": api.get("include_deleted", False),
        "refreshed_at": api["refreshed_at"],
        "entry_ids": [entry["id"] for entry in api["entries"]],
        "entry_count": 172,
        "duration_seconds": seconds if seconds is not None else 132217,
        "native_costs": {"USD": "983.70", "EUR": "7.31"},
    }
    result.update(changes)
    return result


class ClockifyReadbackContractTests(unittest.TestCase):
    def test_readback_binds_entries_duration_filters_and_native_currency_buckets(self):
        result = readback.normalize_readback(_api_fixture())
        self.assertEqual(172, result.entry_count)
        self.assertEqual(132217, result.duration_seconds)
        self.assertEqual(
            {"USD": Decimal("983.70"), "EUR": Decimal("7.31")}, result.native_costs
        )
        self.assertEqual(("entry-000", "entry-001"), result.entry_ids[:2])
        self.assertEqual("Europe/Bucharest", result.timezone)
        self.assertTrue(result.filters["include_running"])
        self.assertTrue(result.filters["include_deleted"])
        self.assertTrue(result.digest.startswith("sha256:"))

    def test_august_sixteenth_is_outside_half_open_period(self):
        fixture = _api_fixture()
        fixture["entries"].append({
            **_entry(999),
            "id": "august-16",
            "timeInterval": {
                "start": "2026-08-16T00:00:00+03:00",
                "end": "2026-08-16T00:10:00+03:00",
                "duration": 600,
            },
        })
        result = readback.normalize_readback(fixture)
        self.assertNotIn("august-16", result.entry_ids)
        self.assertEqual(172, result.entry_count)

    def test_ten_minute_report_difference_is_a_blocker_not_rounding(self):
        api = readback.normalize_readback(_api_fixture())
        report = readback.normalize_readback(_report_from(_api_fixture(), seconds=api.duration_seconds - 600))
        with self.assertRaisesRegex(readback.ClockifyReadbackError, "duration mismatch: 600 seconds"):
            readback.verify_readback(api, report)

    def test_summary_report_may_prove_totals_without_entry_ids(self):
        api = readback.normalize_readback(_api_fixture())
        report_document = _report_from(_api_fixture())
        report_document.pop("entry_ids")
        report_document["entry_count"] = api.entry_count
        report = readback.normalize_readback(report_document)
        self.assertEqual("verified", readback.verify_readback(api, report)["status"])

    def test_wrong_filter_member_period_and_stale_report_fail_closed(self):
        api = _api_fixture()
        for changes, message in (
            ({"member_id": "other-member"}, "member mismatch"),
            ({"period_start": "2026-08-02T00:00:00+03:00"}, "period mismatch"),
            ({"filters": {**api["filters"], "include_deleted": False}}, "filter mismatch"),
            ({"refreshed_at": "2026-08-30T12:00:00+03:00"}, "stale report refresh"),
        ):
            with self.subTest(message=message):
                report = _report_from(api, **changes)
                with self.assertRaisesRegex(readback.ClockifyReadbackError, message):
                    readback.verify_readback(api, report)

    def test_missing_native_currency_code_fails_closed(self):
        fixture = _api_fixture()
        fixture["native_costs"] = {"": "1.00"}
        with self.assertRaisesRegex(readback.ClockifyReadbackError, "currency"):
            readback.normalize_readback(fixture)

    def test_nonfinite_native_cost_fails_closed(self):
        fixture = _api_fixture()
        fixture["native_costs"] = {"USD": "NaN"}
        with self.assertRaisesRegex(readback.ClockifyReadbackError, "invalid amount"):
            readback.normalize_readback(fixture)

    def test_explicit_summary_entry_count_binds_even_when_entry_ids_are_empty(self):
        fixture = _report_from(_api_fixture())
        fixture["evidence_kind"] = "ledger"
        fixture["entry_ids"] = []
        fixture["entry_count"] = 1
        with self.assertRaisesRegex(readback.ClockifyReadbackError, "entry count|entry_durations"):
            readback.normalize_readback(fixture)

    def test_credential_named_filter_is_rejected_without_echoing_value(self):
        fixture = _api_fixture()
        fixture["filters"]["api_key"] = "fixture-secret"
        with self.assertRaisesRegex(readback.ClockifyReadbackError, "credential") as raised:
            readback.normalize_readback(fixture)
        self.assertNotIn("fixture-secret", str(raised.exception))

    def test_nested_credential_named_filter_is_rejected_without_echoing_value(self):
        fixture = _api_fixture()
        fixture["filters"]["nested"] = {"transport": {"token": "fixture-secret"}}
        with self.assertRaisesRegex(readback.ClockifyReadbackError, "credential") as raised:
            readback.normalize_readback(fixture)
        self.assertNotIn("fixture-secret", str(raised.exception))

    def test_cost_rate_on_time_entries_is_not_native_cost_evidence(self):
        fixture = _api_fixture()
        fixture["native_costs"] = {"USD": "983.70"}
        fixture["entries"][0]["costRate"] = {"amount": 999999, "currency": "USD"}
        result = readback.normalize_readback(fixture)
        self.assertEqual({"USD": Decimal("983.70")}, result.native_costs)

    def test_running_and_deleted_semantics_are_not_asserted_when_gateway_cannot_filter_them(self):
        gateway = readback.ClockifyApiGateway("fixture-key")
        with self.assertRaisesRegex(readback.ClockifyReadbackError, "running/deleted"):
            gateway.read_period(
                workspace_id="workspace-1", member_id="member-1",
                start=datetime.fromisoformat(START), end=datetime.fromisoformat(END),
                filters={"include_running": True, "include_deleted": True},
            )

    def test_running_and_deleted_entries_follow_explicit_inclusion_filters(self):
        fixture = _api_fixture()
        fixture["entries"][0]["running"] = True
        fixture["entries"][1]["deleted"] = True
        fixture["include_running"] = False
        fixture["include_deleted"] = False
        fixture["filters"]["include_running"] = False
        fixture["filters"]["include_deleted"] = False
        result = readback.normalize_readback(fixture)
        self.assertNotIn("entry-000", result.entry_ids)
        self.assertNotIn("entry-001", result.entry_ids)
        self.assertEqual(170, result.entry_count)

        fixture["include_running"] = True
        fixture["include_deleted"] = True
        fixture["filters"]["include_running"] = True
        fixture["filters"]["include_deleted"] = True
        result = readback.normalize_readback(fixture)
        self.assertEqual(172, result.entry_count)

    def test_post_receipt_ids_must_exist_exactly_once_in_final_ledger(self):
        api = readback.normalize_readback(_api_fixture())
        report = readback.normalize_readback(_report_from(_api_fixture()))
        with self.assertRaisesRegex(readback.ClockifyReadbackError, "post receipt"):
            readback.verify_readback(api, report, post_receipt={"created": ["unknown"]})
        with self.assertRaisesRegex(readback.ClockifyReadbackError, "exactly once"):
            readback.verify_readback(api, report, post_receipt={"created": ["entry-001", "entry-001"]})
        self.assertEqual(
            "verified",
            readback.verify_readback(
                api, report,
                {"entries": [{"disposition": "created", "clockify_entry_id": "entry-001"}]},
            )["status"],
        )


class ClockifyReadbackCliTests(unittest.TestCase):
    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "scripts/clockify_period_readback.py", *args],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_capture_fixture_is_read_only_and_writes_normalized_api(self):
        fixture = _api_fixture()
        fixture["include_running"] = fixture["include_deleted"] = False
        fixture["filters"]["include_running"] = fixture["filters"]["include_deleted"] = False
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "api-fixture.json"
            report_source = Path(directory) / "report-fixture.json"
            output = Path(directory) / "api.json"
            report = Path(directory) / "report.json"
            routing = Path(directory) / "routing.json"
            source.write_text(json.dumps(fixture), encoding="utf-8")
            report_document = _report_from(fixture)
            report_document["evidence_kind"] = "report"
            raw_response = {"entriesCount": 172, "totalTime": 132217, "totals": [{"amounts": [{"amountByCurrency": [{"currency": "USD", "amount": 98370}, {"currency": "EUR", "amount": 731}]}]}]}
            report_document["raw_response"] = raw_response
            report_document["request_receipt"] = {"workspace_id": "workspace-1", "member_id": "member-1", "period_start": START, "period_end": END, "filters": dict(fixture["filters"]), "shared_report_id": "report-1", "raw_response_digest": readback._digest(raw_response)}
            report_source.write_text(json.dumps(report_document), encoding="utf-8")
            routing.write_text(json.dumps({"timezone": "Europe/Bucharest", "workspace_id": "workspace-1", "clockify_user_id": "member-1"}), encoding="utf-8")
            result = self._run(
                "capture", "--routing", str(routing), "--timezone", "Europe/Bucharest",
                "--since", START, "--until", END, "--shared-report-id", "report-1",
                "--api-fixture", str(source), "--report-fixture", str(report_source), "--api-output", str(output), "--report-output", str(report), "--output-root", directory,
            )
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(172, json.loads(output.read_text())["entry_count"])
            self.assertNotIn("credential", result.stdout.lower())
            self.assertNotIn("api_key", result.stdout.lower())
            self.assertFalse((Path(directory) / "mutation.marker").exists())

    def test_reconcile_identifies_exact_entry_delta(self):
        api = _api_fixture()
        with tempfile.TemporaryDirectory() as directory:
            api_path, report_path, output = [Path(directory) / name for name in ("api.json", "report.json", "reconcile.json")]
            api_path.write_text(json.dumps(api), encoding="utf-8")
            report_path.write_text(json.dumps(_report_from(api, seconds=132217 - 600, entry_ids=[entry["id"] for entry in api["entries"][:-1]], entry_count=171)), encoding="utf-8")
            result = self._run("reconcile", "--api", str(api_path), "--shared-report", str(report_path), "--output", str(output), "--output-root", directory)
            self.assertEqual(2, result.returncode)
            data = json.loads(output.read_text())
            self.assertEqual("readback_mismatch", data["status"])
            self.assertEqual(600, data["duration_delta_seconds"])
            self.assertEqual(["entry-171"], data["api_only_entry_ids"])
            self.assertNotIn("CLOCKIFY_API_KEY", result.stdout + result.stderr)

    def test_reconcile_accepts_totals_only_shared_report_when_exact(self):
        api = _api_fixture()
        report = _report_from(api)
        report.pop("entry_ids")
        with tempfile.TemporaryDirectory() as directory:
            api_path, report_path, output = [Path(directory) / name for name in ("api.json", "report.json", "reconcile.json")]
            api_path.write_text(json.dumps(api), encoding="utf-8")
            report_path.write_text(json.dumps(report), encoding="utf-8")
            result = self._run("reconcile", "--api", str(api_path), "--shared-report", str(report_path), "--output", str(output), "--output-root", directory)
            self.assertEqual(0, result.returncode)
            self.assertEqual("verified", json.loads(output.read_text())["status"])

    def test_reconcile_filter_only_mismatch_calls_gate_and_returns_nonzero(self):
        api = _api_fixture()
        report = _report_from(api, filters={**api["filters"], "include_deleted": False})
        with tempfile.TemporaryDirectory() as directory:
            api_path, report_path, output = [Path(directory) / name for name in ("api.json", "report.json", "reconcile.json")]
            api_path.write_text(json.dumps(api), encoding="utf-8")
            report_path.write_text(json.dumps(report), encoding="utf-8")
            result = self._run("reconcile", "--api", str(api_path), "--shared-report", str(report_path), "--output", str(output), "--output-root", directory)
            self.assertEqual(2, result.returncode)
            self.assertEqual("readback_mismatch", json.loads(output.read_text())["status"])

    def test_output_writer_rejects_symlink_and_traversal_and_writes_atomically(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "root"
            root.mkdir()
            outside = Path(directory) / "outside.json"
            link = root / "link.json"
            link.symlink_to(outside)
            with self.assertRaisesRegex(readback.ClockifyReadbackError, "symlink"):
                readback._write(link, {"ok": True}, root=root)
            with self.assertRaisesRegex(readback.ClockifyReadbackError, "root"):
                readback._write(root / ".." / "escape.json", {"ok": True}, root=root)
            destination = root / "safe.json"
            readback._write(destination, {"ok": True}, root=root)
            self.assertEqual({"ok": True}, json.loads(destination.read_text()))


class ClockifyGatewayTests(unittest.TestCase):
    def test_gateway_uses_separate_reports_api_host(self):
        gateway = readback.ClockifyApiGateway("fixture-key")
        self.assertEqual("https://api.clockify.me/api/v1", gateway._base_url)
        self.assertEqual("https://reports.api.clockify.me/v1", gateway._reports_base_url)

    def test_true_inclusion_requests_are_rejected_without_environment_bypass(self):
        gateway = readback.ClockifyApiGateway("fixture-key")
        with self.assertRaisesRegex(readback.ClockifyReadbackError, "inclusion"):
            gateway.read_period(
                workspace_id="workspace-1", member_id="member-1",
                start=datetime.fromisoformat(START), end=datetime.fromisoformat(END),
                filters={"include_running": True, "include_deleted": True},
            )

    def test_realistic_report_result_parses_totals_and_cent_amounts(self):
        gateway = readback.ClockifyApiGateway("fixture-key")
        gateway._post_report = mock.Mock(return_value={
            "entriesCount": 172,
            "totalTime": 132217,
            "totals": [{"amounts": [{"amountByCurrency": [
                {"currency": "USD", "amount": 98370},
                {"currency": "EUR", "amount": 731},
            ]}]}],
        })
        result = gateway.read_report_result(
            report_id="report-1", workspace_id="workspace-1", member_id="member-1",
            start=datetime.fromisoformat(START), end=datetime.fromisoformat(END),
            filters={"timezone": "Europe/Bucharest", "include_running": False, "include_deleted": False},
        )
        self.assertEqual("report", result["evidence_kind"])
        self.assertEqual(172, result["entry_count"])
        self.assertEqual("983.70", result["native_costs"]["USD"])
        self.assertEqual("report-1", result["request_receipt"]["shared_report_id"])

    def test_report_persists_only_allowlisted_sanitized_projection(self):
        gateway = readback.ClockifyApiGateway("fixture-key")
        gateway._post_report = mock.Mock(return_value={
            "totals": [{"entriesCount": 7, "totalTime": 420, "description": "drop", "amounts": [{"amountByCurrency": [{"currency": "USD", "amount": 100, "private_key": "drop", "metadata": {"access_token": "drop"}}]}]}],
            "description": "drop", "unknown": {"nested": "drop"}, "access_token": "drop",
        })
        envelope = gateway.read_report_result(report_id="report-1", workspace_id="workspace-1", member_id="member-1", start=datetime.fromisoformat(START), end=datetime.fromisoformat(END), filters={"timezone": "Europe/Bucharest", "include_running": False, "include_deleted": False})
        projection = envelope["raw_response"]
        self.assertEqual({"totals"}, set(projection))
        self.assertEqual({"entriesCount", "totalTime", "amounts"}, set(projection["totals"][0]))
        self.assertNotIn("access_token", json.dumps(projection))
        self.assertNotIn("description", json.dumps(projection))

    def test_report_metrics_are_read_from_totals_item(self):
        gateway = readback.ClockifyApiGateway("fixture-key")
        gateway._post_report = mock.Mock(return_value={
            "totals": [{"entriesCount": 7, "totalTime": 420, "amounts": [{"amountByCurrency": [{"currency": "USD", "amount": 100}]}]}],
        })
        result = gateway.read_report_result(
            report_id="report-1", workspace_id="workspace-1", member_id="member-1",
            start=datetime.fromisoformat(START), end=datetime.fromisoformat(END),
            filters={"timezone": "Europe/Bucharest", "include_running": False, "include_deleted": False},
        )
        self.assertEqual(7, result["entry_count"])
        self.assertEqual(420, result["duration_seconds"])

    def test_repeated_same_currency_totals_are_aggregated(self):
        gateway = readback.ClockifyApiGateway("fixture-key")
        gateway._post_report = mock.Mock(return_value={
            "entriesCount": 1, "totalTime": 60,
            "totals": [
                {"amounts": [{"amountByCurrency": [{"currency": "USD", "amount": 100}]}]},
                {"amounts": [{"amountByCurrency": [{"currency": "USD", "amount": 250}]}]},
            ],
        })
        result = gateway.read_report_result(
            report_id="report-1", workspace_id="workspace-1", member_id="member-1",
            start=datetime.fromisoformat(START), end=datetime.fromisoformat(END),
            filters={"timezone": "Europe/Bucharest", "include_running": False, "include_deleted": False},
        )
        self.assertEqual("3.50", result["native_costs"]["USD"])

    def test_report_result_rejects_missing_costs(self):
        gateway = readback.ClockifyApiGateway("fixture-key")
        gateway._post_report = mock.Mock(return_value={"entriesCount": 1, "totalTime": 60, "totals": [{"amounts": []}]})
        with self.assertRaisesRegex(readback.ClockifyReadbackError, "cost"):
            gateway.read_report_result(
                report_id="report-1", workspace_id="workspace-1", member_id="member-1",
                start=datetime.fromisoformat(START), end=datetime.fromisoformat(END), filters={"include_running": False, "include_deleted": False},
            )


class ClockifyEvidenceKindTests(unittest.TestCase):
    def test_ledger_requires_entry_durations_and_matching_sum(self):
        ledger = _api_fixture()
        ledger["evidence_kind"] = "ledger"
        ledger.pop("native_costs")
        ledger.pop("entries")
        ledger["entry_ids"] = ["entry-1"]
        ledger["entry_count"] = 1
        ledger["duration_seconds"] = 60
        with self.assertRaisesRegex(readback.ClockifyReadbackError, "entry_durations"):
            readback.normalize_readback(ledger)

    def test_report_receipt_digest_drift_fails_closed(self):
        report = _report_from(_api_fixture())
        report["evidence_kind"] = "report"
        report["request_receipt"] = {"workspace_id": "workspace-1", "member_id": "member-1", "period_start": START, "period_end": END, "filters": dict(report["filters"]), "shared_report_id": "report-1", "raw_response_digest": "sha256:" + "0" * 64}
        report["raw_response"] = {"entriesCount": 172, "totalTime": 132217, "totals": [{"amounts": [{"amountByCurrency": [{"currency": "USD", "amount": 98370}]}]}]}
        with self.assertRaisesRegex(readback.ClockifyReadbackError, "digest"):
            readback.normalize_readback(report)

    def test_evidence_metadata_is_bound_into_canonical_digest(self):
        report = _report_from(_api_fixture())
        report["evidence_kind"] = "report"
        report["raw_response"] = {"totals": [{"entriesCount": 172, "totalTime": 132217, "amounts": []}]}
        report["request_receipt"] = {"workspace_id": "workspace-1", "member_id": "member-1", "period_start": START, "period_end": END, "filters": dict(report["filters"]), "shared_report_id": "report-1", "raw_response_digest": readback._digest(report["raw_response"])}
        normalized = readback.normalize_readback(report)
        document = normalized.to_dict()
        document["request_receipt"]["shared_report_id"] = "tampered"
        document["digest"] = normalized.digest
        with self.assertRaisesRegex(readback.ClockifyReadbackError, "digest"):
            readback.normalize_readback(document)

    def test_report_costs_verify_without_ledger_costs(self):
        api = _api_fixture()
        api.pop("native_costs")
        report = _report_from(_api_fixture())
        report["evidence_kind"] = "report"
        report["raw_response"] = {"totals": [{"entriesCount": 172, "totalTime": 132217, "amounts": []}]}
        report["request_receipt"] = {"workspace_id": "workspace-1", "member_id": "member-1", "period_start": START, "period_end": END, "filters": dict(report["filters"]), "shared_report_id": "report-1", "raw_response_digest": readback._digest(report["raw_response"])}
        verified = readback.verify_readback(api, report)
        self.assertEqual({"USD": "983.70", "EUR": "7.31"}, verified["native_costs"])

    def test_verify_requires_ledger_and_report_evidence_roles(self):
        api = _api_fixture()
        report = replace(readback.normalize_readback(_report_from(api)), evidence_kind="ledger", request_receipt={"shared_report_id": "report-1"})
        with self.assertRaisesRegex(readback.ClockifyReadbackError, "evidence kind"):
            readback.verify_readback(api, report)

    def test_verify_rejects_second_ledger_without_report_receipt(self):
        api = readback.normalize_readback(_api_fixture())
        with self.assertRaisesRegex(readback.ClockifyReadbackError, "evidence kind"):
            readback.verify_readback(api, api)

    def test_report_round_trip_preserves_sanitized_raw_response(self):
        gateway = readback.ClockifyApiGateway("fixture-key")
        raw = {"totals": [{"entriesCount": 172, "totalTime": 132217, "amounts": [{"amountByCurrency": [{"currency": "USD", "amount": 98370}]}]}]}
        gateway._post_report = mock.Mock(return_value=raw)
        envelope = gateway.read_report_result(report_id="report-1", workspace_id="workspace-1", member_id="member-1", start=datetime.fromisoformat(START), end=datetime.fromisoformat(END), filters={"timezone": "Europe/Bucharest", "include_running": False, "include_deleted": False})
        normalized = readback.normalize_readback(envelope)
        persisted = normalized.to_dict()
        self.assertEqual({"totals"}, set(persisted["raw_response"]))
        self.assertNotIn("description", json.dumps(persisted["raw_response"]))
        self.assertEqual("report", readback.normalize_readback(persisted).evidence_kind)
        persisted.pop("raw_response")
        with self.assertRaisesRegex(readback.ClockifyReadbackError, "raw response"):
            readback.normalize_readback(persisted)

    def test_raw_report_private_fields_are_rejected_before_persistence(self):
        gateway = readback.ClockifyApiGateway("fixture-key")
        raw = {"totals": [{"entriesCount": 1, "totalTime": 60, "amounts": [{"amountByCurrency": [{"currency": "USD", "amount": 100}]}]}], "nested": {"token": "secret"}}
        gateway._post_report = mock.Mock(return_value=raw)
        with self.assertRaisesRegex(readback.ClockifyReadbackError, "credential"):
            gateway.read_report_result(report_id="report-1", workspace_id="workspace-1", member_id="member-1", start=datetime.fromisoformat(START), end=datetime.fromisoformat(END), filters={"timezone": "Europe/Bucharest", "include_running": False, "include_deleted": False})
    def test_period_gateway_paginates_beyond_two_hundred_and_stops_at_exhaustion(self):
        gateway = readback.ClockifyApiGateway("fixture-key")
        def page_entry(index: int) -> dict:
            entry = {**_entry(index), "cost": None}
            entry["timeInterval"] = {"start": "2026-08-01T09:00:00+03:00", "end": "2026-08-01T09:10:00+03:00", "duration": 600}
            return entry
        pages = [[page_entry(index) for index in range(200)], [page_entry(200)], []]
        gateway._get = mock.Mock(side_effect=pages)
        result = gateway.read_period(
            workspace_id="workspace-1", member_id="member-1",
            start=datetime.fromisoformat(START), end=datetime.fromisoformat(END),
            filters={"timezone": "Europe/Bucharest", "include_running": False, "include_deleted": False},
        )
        self.assertEqual(201, result.entry_count)
        self.assertEqual(3, gateway._get.call_count)
        self.assertLessEqual(gateway._get.call_args_list[0].args[0].count("page-size=200"), 1)

    def test_period_gateway_rejects_duplicate_page_ids(self):
        gateway = readback.ClockifyApiGateway("fixture-key")
        page = [{**_entry(1), "cost": None, "timeInterval": {"start": "2026-08-01T09:00:00+03:00", "end": "2026-08-01T09:10:00+03:00", "duration": 600}}]
        gateway._get = mock.Mock(side_effect=[page, page])
        with self.assertRaisesRegex(readback.ClockifyReadbackError, "duplicate"):
            gateway.read_period(
            workspace_id="workspace-1", member_id="member-1",
            start=datetime.fromisoformat(START), end=datetime.fromisoformat(END),
                filters={"timezone": "Europe/Bucharest", "include_running": False, "include_deleted": False},
            )

    def test_report_config_without_results_is_not_report_evidence(self):
        gateway = readback.ClockifyApiGateway("fixture-key")
        gateway._post_report = mock.Mock(return_value={"name": "config", "id": "report-1"})
        with self.assertRaisesRegex(readback.ClockifyReadbackError, "results"):
            gateway.read_report_result(
                report_id="report-1", workspace_id="workspace-1", member_id="member-1",
                start=datetime.fromisoformat(START), end=datetime.fromisoformat(END),
                filters={"timezone": "Europe/Bucharest", "include_running": True, "include_deleted": True},
            )


if __name__ == "__main__":
    unittest.main()
