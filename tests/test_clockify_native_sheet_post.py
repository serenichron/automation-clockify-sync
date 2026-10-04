import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import clockify_native_sheet_post as native


def cell(value):
    key = "numberValue" if isinstance(value, (int, float)) else "stringValue"
    return {"userEnteredValue": {key: value}}


def capture(*rows):
    headers = [
        "Review ID", "Start", "End", "Duration (min)", "Project", "Tags",
        "Source", "Confidence", "Description", "Disposition", "Revision",
        "Last Seen Run", "Reason", "Review Status", "Review Notes",
    ]
    return {"structuredContent": {"spreadsheetId": "sheet-1", "sheets": [{
        "properties": {"title": "September 2026 portfolio review"},
        "data": [{"rowData": [
            {"values": [cell(value) for value in headers]},
            *({"values": [cell(value) for value in row]} for row in rows),
        ]}],
    }]}}


def row(review_id, start, end, minutes, project="Example", tags="Process"):
    return [review_id, start, end, minutes, project, tags, "activity", "high",
            "Approved work", "pending", 1, "run", "", "unposted", ""]


def route(project="Example", suffix="123456", section="session_routes"):
    result = {"clockify_user_id": "user-1", "session_routes": [],
              "meeting_routes": [], "evidence_routes": [], "client_lifecycle_routes": []}
    value = {"project_name": project, "tag_names": ["Process"],
             "project_suffix": suffix, "tag_suffixes": ["654321"], "billable": True}
    if section == "lifecycle":
        result["client_lifecycle_routes"] = [{"activation": {"route": value}}]
    else:
        result[section] = [value]
    return result


def live(entry_id, start, end, *, project="project-123456", description="Approved work"):
    return {"id": entry_id, "timeInterval": {"start": start, "end": end},
            "projectId": project, "tagIds": ["tag-654321"], "taskId": None,
            "description": description, "billable": True}


class FakeGateway:
    def __init__(self, entries=None, *, ambiguous=False):
        self.entries = list(entries or [])
        self.ambiguous = ambiguous
        self.posts = []

    def verify_target(self, workspace, member):
        if (workspace, member) != ("workspace-1", "user-1"):
            raise native.NativePostError("target mismatch")

    def period_entries(self, _start, _end):
        return copy.deepcopy(self.entries)

    def recovery_entries(self, _start, _end):
        return copy.deepcopy(self.entries)

    def entry_by_id(self, entry_id):
        return copy.deepcopy(next((entry for entry in self.entries if entry["id"] == entry_id), None))

    def create(self, payload):
        self.posts.append(copy.deepcopy(payload))
        entry = {"id": f"created-{len(self.posts)}", "timeInterval": {
            "start": payload["start"], "end": payload["end"]},
            "projectId": payload["projectId"], "tagIds": payload["tagIds"],
            "taskId": payload.get("taskId"), "description": payload["description"],
            "billable": payload["billable"]}
        self.entries.append(entry)
        if self.ambiguous:
            raise native.AmbiguousCreate("transport lost after create")
        return entry


class CrashAfterCreateGateway(FakeGateway):
    def create(self, payload):
        super().create(payload)
        raise KeyboardInterrupt("simulated process crash")


class DelayedDirectGetGateway(FakeGateway):
    def __init__(self):
        super().__init__()
        self.direct_gets = 0
        self.cached = []

    def period_entries(self, _start, _end):
        return copy.deepcopy(self.cached)

    def entry_by_id(self, entry_id):
        self.direct_gets += 1
        if self.direct_gets == 1:
            return None
        return super().entry_by_id(entry_id)


class NativeSheetPostTests(unittest.TestCase):
    def _plan(self, document, routing=None, existing=None):
        raw = json.dumps(document, sort_keys=True).encode()
        routing = routing or route()
        return native.build_plan(
            document, capture_sha256=hashlib.sha256(raw).hexdigest(), routing=routing,
            routing_sha256="a" * 64, timezone="Europe/Bucharest",
            workspace_id="workspace-1", member_id="user-1",
            projects=[{"id": "project-123456"}], tags=[{"id": "tag-654321"}],
            live_entries=existing or [],
        )

    def _approval(self, plan):
        return native.approval_template(
            plan, approval_id="approval-1", approver="human board user",
            approved_at="2026-10-04T10:00:00Z", expires_at="2026-10-05T10:00:00Z",
        )

    def test_second_precision_rows_preserve_exact_approved_payloads_and_totals(self):
        cases = [
            ("2026-09-03 14:10:00", 32.75, "2026-09-03T11:10:00Z"),
            ("2026-09-03 13:39:17", 2.033333333333333, "2026-09-03T10:39:17Z"),
            ("2026-09-03 14:10:20", 33.083333333333336, "2026-09-03T11:10:20Z"),
        ]
        for end, minutes, expected_end in cases:
            with self.subTest(minutes=minutes):
                plan = self._plan(capture(row("review-seconds", "2026-09-03 13:37:15", end, minutes)))
                self.assertEqual("2026-09-03T10:37:15Z", plan["entries"][0]["payload"]["start"])
                self.assertEqual(expected_end, plan["entries"][0]["payload"]["end"])
                self.assertAlmostEqual(minutes, plan["entries"][0]["duration_minutes"], places=14)
                self.assertAlmostEqual(minutes, plan["total_minutes"], places=14)

    def test_second_precision_execution_requires_unchanged_approval_and_readback(self):
        plan = self._plan(capture(row("review-seconds", "2026-09-03 13:37:15",
                                      "2026-09-03 13:39:17", 2.033333333333333)))
        approval = self._approval(plan)
        gateway = FakeGateway()
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / "events.jsonl"
            receipt_path = Path(directory) / "receipt.json"
            drifted = copy.deepcopy(plan)
            drifted["entries"][0]["payload"]["end"] = "2026-09-03T10:39:18Z"
            drifted["plan_digest"] = native._document_digest(drifted, "plan_digest")
            with self.assertRaisesRegex(native.NativePostError, "approval.*plan"):
                native.execute_plan(drifted, approval, events, receipt_path, gateway,
                                    now=dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc))
            self.assertEqual([], gateway.posts)
            receipt = native.execute_plan(plan, approval, events, receipt_path, gateway,
                                          now=dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc))
            original_bytes = receipt_path.read_bytes()
            gateway.entries[0]["timeInterval"]["duration"] = "PT2M2S"
            rerun = native.execute_plan(plan, approval, events, receipt_path, gateway,
                                        now=dt.datetime(2026, 10, 4, 11, 1, tzinfo=dt.timezone.utc))
            self.assertEqual(receipt, rerun)
            self.assertEqual(original_bytes, receipt_path.read_bytes())
            gateway.entries[0]["timeInterval"]["end"] = "2026-09-03T10:39:18Z"
            with self.assertRaisesRegex(native.NativePostError, "readback"):
                native.execute_plan(plan, approval, events, receipt_path, gateway,
                                    now=dt.datetime(2026, 10, 4, 11, 2, tzinfo=dt.timezone.utc))
            self.assertEqual(original_bytes, receipt_path.read_bytes())
        self.assertEqual(1, len(gateway.posts))
        self.assertEqual("2026-09-03T10:37:15Z", gateway.posts[0]["start"])
        self.assertEqual("2026-09-03T10:39:17Z", gateway.posts[0]["end"])

    def test_mismatched_nonfinite_or_nonnumeric_minutes_are_rejected(self):
        for minutes in (33, 32.750001, 32.75 + 1e-8, float("nan"), float("inf"),
                        -float("inf"), True, "32.75", None):
            with self.subTest(minutes=minutes):
                with self.assertRaisesRegex(native.NativePostError, "duration"):
                    self._plan(capture(row("review-seconds", "2026-09-03 13:37:15",
                                           "2026-09-03 14:10:00", minutes)))

    def test_subsecond_sheet_timestamps_are_rejected_instead_of_truncated(self):
        with self.assertRaisesRegex(native.NativePostError, "timestamps"):
            self._plan(capture(row("review-seconds", "2026-09-03 13:37:15.1",
                                   "2026-09-03 14:10:00.1", 32.75)))

    def test_zero_or_reversed_intervals_are_rejected(self):
        for end, minutes in (("2026-09-07 12:00", 0), ("2026-09-07 11:59", -1)):
            with self.subTest(end=end):
                with self.assertRaisesRegex(native.NativePostError, "duration"):
                    self._plan(capture(row("review-a", "2026-09-07 12:00", end, minutes)))

    def test_readback_duration_corresponds_to_exact_timestamp_seconds(self):
        plan = self._plan(capture(row("review-a", "2026-09-07 12:00", "2026-09-07 12:30", 30)))
        payload = plan["entries"][0]["payload"]
        for duration in ("PT30M", "PT1800S", "PT0H30M0S"):
            with self.subTest(duration=duration):
                entry = live("created", payload["start"], payload["end"])
                entry["timeInterval"]["duration"] = duration
                self.assertTrue(native._payload_matches(payload, entry))
        for duration in ("PT29M59S", "PT30M0.1S", "PT", "NaN", None, True):
            with self.subTest(duration=duration):
                entry = live("created", payload["start"], payload["end"])
                entry["timeInterval"]["duration"] = duration
                self.assertFalse(native._payload_matches(payload, entry))
        entry = live("created", "2026-09-07T09:00:00.1Z", "2026-09-07T09:30:00.1Z")
        self.assertFalse(native._payload_matches(payload, entry))

    def test_legacy_whole_minute_plan_and_receipt_hashes_are_unchanged_on_rerun(self):
        # Frozen from the pre-repair implementation with this same public input.
        cases = [
            (30, "86ed36a3d11770ffe9635f10fc6a009a24f3f509d6a14dcbd4f2eaaaf22762ae",
             "baca4e334542c3fe3d0d3fcc94e7ef836bb0cecc1b422a77987fe61332e6ee56",
             "08bd596093cdce51a989142e9af187f576e2552ee11b1a90453bd7f01ccec00a"),
            (30.0, "224ad19852f7a94ecf631805d811d8f2ad37c8c96c99014772390feabb96f412",
             "11404926cd9c670fcf64edecd49fea643ea8a96526a8d715007f38a589c47da3",
             "b2c875e1001843968375cd638801cf9f6e9d471699bd8fd1a4a0b5154e5700c0"),
        ]
        for minutes, plan_bytes_digest, plan_digest, receipt_digest in cases:
            with self.subTest(minutes=minutes):
                plan = self._plan(capture(row("review-a", "2026-09-07 12:00", "2026-09-07 12:30", minutes)))
                self.assertEqual(plan_bytes_digest, native._digest(plan))
                self.assertEqual(plan_digest, plan["plan_digest"])
                gateway = FakeGateway()
                with tempfile.TemporaryDirectory() as directory:
                    events = Path(directory) / "events.jsonl"
                    receipt_path = Path(directory) / "receipt.json"
                    receipt = native.execute_plan(plan, self._approval(plan), events, receipt_path, gateway,
                                                  now=dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc))
                    self.assertEqual(receipt_digest, native._digest(receipt))
                    original_bytes = receipt_path.read_bytes()
                    native.execute_plan(plan, self._approval(plan), events, receipt_path, gateway,
                                        now=dt.datetime(2026, 10, 4, 11, 1, tzinfo=dt.timezone.utc))
                    self.assertEqual(original_bytes, receipt_path.read_bytes())
                self.assertEqual(1, len(gateway.posts))

    def test_plan_preserves_distinct_overlaps_and_resolves_lifecycle_route(self):
        doc = capture(
            row("review-a", "2026-09-07 12:00", "2026-09-07 12:30", 30),
            row("review-b", "2026-09-07 12:10", "2026-09-07 12:20", 10),
        )
        plan = self._plan(doc, route(section="lifecycle"))

        self.assertEqual(2, len(plan["entries"]))
        self.assertEqual("2026-09-07T09:00:00Z", plan["entries"][0]["payload"]["start"])
        self.assertEqual("2026-09-07T09:30:00Z", plan["entries"][0]["payload"]["end"])
        self.assertEqual("project-123456", plan["entries"][0]["payload"]["projectId"])

    def test_gateway_preserves_raw_task_id_and_plan_materializes_live_overlap(self):
        raw = live("existing", "2026-09-07T09:10:00Z", "2026-09-07T09:20:00Z",
                   project="other", description="Other")
        raw["taskId"] = "task-live"
        gateway = native.ClockifyGateway("secret", 45)
        gateway.bind_target("workspace-1", "user-1")
        with mock.patch.object(native.legacy, "_paged", return_value=[raw]):
            actual = gateway.period_entries("2026-09-07T09:00:00Z", "2026-09-07T09:30:00Z")
        plan = self._plan(
            capture(row("review-a", "2026-09-07 12:00", "2026-09-07 12:30", 30)),
            existing=actual,
        )

        self.assertEqual("task-live", actual[0]["taskId"])
        self.assertEqual(600, plan["entries"][0]["live_overlaps"][0]["overlap_seconds"])

    def test_execute_rejects_approval_plan_drift_before_post(self):
        plan = self._plan(capture(row("review-a", "2026-09-07 12:00", "2026-09-07 12:30", 30)))
        approval = self._approval(plan)
        plan["entries"][0]["payload"]["end"] = "2026-09-07T09:29:00Z"
        gateway = FakeGateway()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(native.NativePostError, "approval.*plan"):
                native.execute_plan(plan, approval, Path(directory) / "events.jsonl",
                                    Path(directory) / "receipt.json", gateway,
                                    now=dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc))
        self.assertEqual([], gateway.posts)

    def test_live_overlap_posts_full_approved_interval_and_records_overlap(self):
        existing = [live("existing", "2026-09-07T09:10:00Z", "2026-09-07T09:20:00Z",
                         project="other", description="Other activity")]
        plan = self._plan(capture(row("review-a", "2026-09-07 12:00", "2026-09-07 12:30", 30)),
                          existing=existing)
        gateway = FakeGateway(existing)
        with tempfile.TemporaryDirectory() as directory:
            receipt = native.execute_plan(
                plan, self._approval(plan), Path(directory) / "events.jsonl",
                Path(directory) / "receipt.json", gateway,
                now=dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc))

        self.assertEqual("2026-09-07T09:00:00Z", gateway.posts[0]["start"])
        self.assertEqual("2026-09-07T09:30:00Z", gateway.posts[0]["end"])
        self.assertEqual(600, plan["entries"][0]["live_overlaps"][0]["overlap_seconds"])
        self.assertEqual("complete", receipt["status"])

    def test_preexisting_semantic_equal_entry_is_not_credited_without_receipt_identity(self):
        exact = live("unrelated-exact", "2026-09-07T09:00:00Z", "2026-09-07T09:30:00Z")
        plan = self._plan(capture(row("review-a", "2026-09-07 12:00", "2026-09-07 12:30", 30)),
                          existing=[exact])
        gateway = FakeGateway([exact])
        with tempfile.TemporaryDirectory() as directory:
            native.execute_plan(plan, self._approval(plan), Path(directory) / "events.jsonl",
                                Path(directory) / "receipt.json", gateway,
                                now=dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc))
        self.assertEqual(1, len(gateway.posts))

    def test_ambiguous_create_is_reconciled_by_unique_new_exact_id(self):
        plan = self._plan(capture(row("review-a", "2026-09-07 12:00", "2026-09-07 12:30", 30)))
        gateway = FakeGateway(ambiguous=True)
        with tempfile.TemporaryDirectory() as directory:
            receipt = native.execute_plan(plan, self._approval(plan), Path(directory) / "events.jsonl",
                                          Path(directory) / "receipt.json", gateway,
                                          now=dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc))
        self.assertEqual("recovered_after_ambiguous_response", receipt["entries"][0]["disposition"])
        self.assertEqual(1, len(gateway.posts))

    def test_completed_rerun_performs_zero_writes_and_revalidates_exact_id(self):
        plan = self._plan(capture(row("review-a", "2026-09-07 12:00", "2026-09-07 12:30", 30)))
        approval = self._approval(plan)
        gateway = FakeGateway()
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / "events.jsonl"
            receipt = Path(directory) / "receipt.json"
            native.execute_plan(plan, approval, events, receipt, gateway,
                                now=dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc))
            native.execute_plan(plan, approval, events, receipt, gateway,
                                now=dt.datetime(2026, 10, 4, 11, 1, tzinfo=dt.timezone.utc))
        self.assertEqual(1, len(gateway.posts))

    def test_restart_recovers_persisted_intent_without_second_post(self):
        plan = self._plan(capture(row("review-a", "2026-09-07 12:00", "2026-09-07 12:30", 30)))
        approval = self._approval(plan)
        gateway = CrashAfterCreateGateway()
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / "events.jsonl"
            receipt = Path(directory) / "receipt.json"
            with self.assertRaises(KeyboardInterrupt):
                native.execute_plan(plan, approval, events, receipt, gateway,
                                    now=dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc))
            result = native.execute_plan(plan, approval, events, receipt, gateway,
                                         now=dt.datetime(2026, 10, 4, 11, 1, tzinfo=dt.timezone.utc))
        self.assertEqual(1, len(gateway.posts))
        self.assertEqual("recovered_after_ambiguous_response", result["entries"][0]["disposition"])

    def test_successful_post_id_is_durable_before_direct_get_and_resume_never_reposts(self):
        plan = self._plan(capture(row("review-a", "2026-09-07 12:00", "2026-09-07 12:30", 30)))
        approval = self._approval(plan)
        gateway = DelayedDirectGetGateway()
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / "events.jsonl"
            receipt = Path(directory) / "receipt.json"
            with self.assertRaisesRegex(native.NativePostError, "direct GET"):
                native.execute_plan(plan, approval, events, receipt, gateway,
                                    now=dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc))
            self.assertEqual(["intent", "created_response"], [
                event["event_type"] for event in native._events(events)
            ])
            result = native.execute_plan(plan, approval, events, receipt, gateway,
                                         now=dt.datetime(2026, 10, 4, 11, 1, tzinfo=dt.timezone.utc))
            native.execute_plan(plan, approval, events, receipt, gateway,
                                now=dt.datetime(2026, 10, 4, 11, 2, tzinfo=dt.timezone.utc))
            confirmed = native._events(events)[-1]
        self.assertEqual(1, len(gateway.posts))
        self.assertEqual("created", result["entries"][0]["disposition"])
        self.assertEqual(native._live_digest([gateway.entries[0]]), confirmed["readback_digest"])

    def test_final_receipt_rejects_one_clockify_id_credited_to_two_reviews(self):
        plan = self._plan(capture(
            row("review-a", "2026-09-07 12:00", "2026-09-07 12:30", 30),
            row("review-b", "2026-09-07 12:00", "2026-09-07 12:30", 30),
        ))
        approval = self._approval(plan)
        approval_digest = native._digest(approval)
        gateway = FakeGateway([live("shared", "2026-09-07T09:00:00Z", "2026-09-07T09:30:00Z")])
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / "events.jsonl"
            for item in plan["entries"]:
                common = {"approval_digest": approval_digest, "plan_digest": plan["plan_digest"],
                          "review_id": item["review_id"], "payload_digest": item["payload_digest"],
                          "recorded_at": "2026-10-04T11:00:00Z"}
                native._append_event(events, {**common, "event_type": "intent", "before_entry_ids": []})
                native._append_event(events, {**common, "event_type": "confirmed",
                                              "clockify_entry_id": "shared", "disposition": "created",
                                              "readback_digest": "digest"})
            with self.assertRaisesRegex(native.NativePostError, "unique Clockify entry"):
                native.execute_plan(plan, approval, events, Path(directory) / "receipt.json", gateway,
                                    now=dt.datetime(2026, 10, 4, 11, 1, tzinfo=dt.timezone.utc))

    def test_execution_journal_lock_rejects_concurrent_writer(self):
        plan = self._plan(capture(row("review-a", "2026-09-07 12:00", "2026-09-07 12:30", 30)))
        approval = self._approval(plan)
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / "events.jsonl"
            with native.execution_lock(events):
                with self.assertRaisesRegex(native.NativePostError, "already in progress"):
                    native.execute_plan(plan, approval, events, Path(directory) / "receipt.json",
                                        FakeGateway(),
                                        now=dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc))


if __name__ == "__main__":
    unittest.main()
