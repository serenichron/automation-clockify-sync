from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

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
            "include_running": True,
            "include_deleted": True,
        },
        "include_running": True,
        "include_deleted": True,
        "refreshed_at": API_REFRESH,
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
        "include_running": True,
        "include_deleted": True,
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
        fixture["entries"][0]["cost"] = {"amount": "1.00"}
        with self.assertRaisesRegex(readback.ClockifyReadbackError, "currency"):
            readback.normalize_readback(fixture)

    def test_credential_named_filter_is_rejected_without_echoing_value(self):
        fixture = _api_fixture()
        fixture["filters"]["api_key"] = "fixture-secret"
        with self.assertRaisesRegex(readback.ClockifyReadbackError, "credential") as raised:
            readback.normalize_readback(fixture)
        self.assertNotIn("fixture-secret", str(raised.exception))

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
        with self.assertRaisesRegex(readback.ClockifyReadbackError, "post receipt"):
            readback.verify_readback(api, api, post_receipt={"created": ["unknown"]})
        with self.assertRaisesRegex(readback.ClockifyReadbackError, "exactly once"):
            readback.verify_readback(api, api, post_receipt={"created": ["entry-001", "entry-001"]})
        self.assertEqual(
            "verified",
            readback.verify_readback(
                api, api,
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
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "api-fixture.json"
            output = Path(directory) / "api.json"
            report = Path(directory) / "report.json"
            routing = Path(directory) / "routing.json"
            source.write_text(json.dumps(fixture), encoding="utf-8")
            routing.write_text(json.dumps({"timezone": "Europe/Bucharest", "workspace_id": "workspace-1", "clockify_user_id": "member-1"}), encoding="utf-8")
            result = self._run(
                "capture", "--routing", str(routing), "--timezone", "Europe/Bucharest",
                "--since", START, "--until", END, "--shared-report-id", "report-1",
                "--api-fixture", str(source), "--api-output", str(output), "--report-output", str(report),
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
            result = self._run("reconcile", "--api", str(api_path), "--shared-report", str(report_path), "--output", str(output))
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
            result = self._run("reconcile", "--api", str(api_path), "--shared-report", str(report_path), "--output", str(output))
            self.assertEqual(0, result.returncode)
            self.assertEqual("verified", json.loads(output.read_text())["status"])


if __name__ == "__main__":
    unittest.main()
