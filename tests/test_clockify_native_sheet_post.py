import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.parse

from scripts import clockify_native_sheet_post as native
from scripts import clockify_review_batches as batches
from scripts import clockify_sheet_publish as publisher


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
    def test_gateway_discovers_carry_in_and_outer_overlaps_without_backend_date_filters(self):
        entries = [
            live("left", "2026-09-26T08:25:00Z", "2026-09-26T09:14:00Z"),
            live("outer", "2016-01-01T00:00:00Z", "2026-09-26T09:15:00Z"),
            live("right", "2026-09-26T09:13:30Z", "2026-09-26T09:15:00Z"),
            live("inside", "2026-09-26T09:13:10Z", "2026-09-26T09:13:20Z"),
            live("touch-left", "2026-09-26T08:00:00Z", "2026-09-26T09:13:00Z"),
            live("touch-right", "2026-09-26T09:14:00Z", "2026-09-26T10:00:00Z"),
        ]
        entries[0]["taskId"] = "original-task"
        entries[0]["customFieldValues"] = [{"customFieldId": "field", "value": "retained"}]
        gateway = native.ClockifyGateway("fixture-key", 45)
        gateway.bind_target("workspace-1", "user-1")
        for mode in ("start-range", "fully-contained"):
            with self.subTest(mode=mode):
                def request(path, key, *, timeout_seconds):
                    parsed = urllib.parse.urlparse(path)
                    self.assertEqual("/workspaces/workspace-1/user/user-1/time-entries", parsed.path)
                    self.assertEqual(("fixture-key", 45), (key, timeout_seconds))
                    query = urllib.parse.parse_qs(parsed.query)
                    rows = entries
                    if "start" in query:
                        rows = [entry for entry in rows
                                if entry["timeInterval"]["start"] >= query["start"][0]]
                    if "end" in query:
                        field = "start" if mode == "start-range" else "end"
                        rows = [entry for entry in rows
                                if entry["timeInterval"][field] < query["end"][0]]
                    page, size = int(query["page"][0]), int(query["page-size"][0])
                    return rows[(page - 1) * size:page * size]
                with mock.patch.object(native.legacy, "_request", side_effect=request):
                    actual = gateway.period_entries("2026-09-26T09:13:00Z", "2026-09-26T09:14:00Z")
                self.assertEqual(["left", "outer", "right", "inside"], [entry["id"] for entry in actual])
                self.assertIs(entries[0], actual[0])
                plan = self._plan(capture(row("review-a", "2026-09-26 12:13", "2026-09-26 12:14", 1)),
                                  existing=actual)
                self.assertEqual([60, 60, 30, 10],
                                 [value["overlap_seconds"] for value in plan["entries"][0]["live_overlaps"]])

    def test_gateway_does_not_cache_inventory_across_fresh_reads(self):
        carry_in = live("new-carry-in", "2026-09-26T08:25:00Z", "2026-09-26T09:14:00Z")
        gateway = native.ClockifyGateway("fixture-key", 45)
        gateway.bind_target("workspace-1", "user-1")
        with mock.patch.object(native.legacy, "_request", side_effect=[[], [carry_in]]):
            first = gateway.period_entries("2026-09-26T09:13:00Z", "2026-09-26T09:14:00Z")
            second = gateway.period_entries("2026-09-26T09:13:00Z", "2026-09-26T09:14:00Z")
        self.assertEqual([], first)
        self.assertEqual(["new-carry-in"], [entry["id"] for entry in second])

    def test_gateway_running_carry_in_cannot_be_declared_free_capacity(self):
        running = live("running", "2026-09-26T08:25:00Z", None)
        gateway = native.ClockifyGateway("fixture-key", 45)
        gateway.bind_target("workspace-1", "user-1")
        with mock.patch.object(native.legacy, "_request", return_value=[running]):
            with self.assertRaisesRegex(native.NativePostError, "running"):
                gateway.period_entries("2026-09-26T09:13:00Z", "2026-09-26T09:14:00Z")

    def test_native_pagination_reaches_later_thousand_entry_page(self):
        outside = live("outside", "2026-09-26T08:00:00Z", "2026-09-26T09:00:00Z")
        carry_in = live("later-carry-in", "2026-09-26T08:25:00Z", "2026-09-26T09:14:00Z")
        gateway = native.ClockifyGateway("fixture-key", 45)
        gateway.bind_target("workspace-1", "user-1")
        with mock.patch.object(native.legacy, "_request", side_effect=[[outside] * 1000, [carry_in]]) as request:
            actual = gateway.period_entries("2026-09-26T09:13:00Z", "2026-09-26T09:14:00Z")
        self.assertEqual(["later-carry-in"], [entry["id"] for entry in actual])
        self.assertEqual("/workspaces/workspace-1/user/user-1/time-entries?page=2&page-size=1000",
                         request.call_args.args[0])

    def test_legacy_pagination_default_preserves_two_hundred_entry_url(self):
        with mock.patch.object(native.legacy, "_request", return_value=[]) as request:
            self.assertEqual([], native.legacy._paged("/workspaces/workspace-1/projects?archived=false",
                                                     "fixture-key", timeout_seconds=45))
        self.assertEqual("/workspaces/workspace-1/projects?archived=false&page=1&page-size=200",
                         request.call_args.args[0])

    def test_native_inventory_rejects_malformed_items_on_full_and_final_pages(self):
        outside = live("outside", "2026-09-26T08:00:00Z", "2026-09-26T09:00:00Z")
        carry_in = live("later-carry-in", "2026-09-26T08:25:00Z", "2026-09-26T09:14:00Z")
        gateway = native.ClockifyGateway("fixture-key", 45)
        gateway.bind_target("workspace-1", "user-1")
        for first_page in ([outside] * 999 + [None], [outside, None]):
            with self.subTest(wire_page_size=len(first_page)):
                with mock.patch.object(native.legacy, "_request", side_effect=[first_page, [carry_in]]):
                    with self.assertRaisesRegex(native.legacy.PortfolioPostError, "item"):
                        gateway.period_entries("2026-09-26T09:13:00Z", "2026-09-26T09:14:00Z")

    def test_legacy_item_filtering_keeps_later_rows_after_full_wire_page(self):
        first = {"id": "first"}
        later = {"id": "later"}
        with mock.patch.object(native.legacy, "_request", side_effect=[[first] * 199 + [None], [later]]):
            actual = native.legacy._paged("/workspaces/workspace-1/projects", "fixture-key", timeout_seconds=45)
        self.assertEqual(200, len(actual))
        self.assertIs(later, actual[-1])
        self.assertNotIn(None, actual)

    def test_pagination_budget_never_returns_partial_overlap_inventory(self):
        carry_in = live("carry-in", "2026-09-26T08:25:00Z", "2026-09-26T09:14:00Z")
        gateway = native.ClockifyGateway("fixture-key", 45)
        gateway.bind_target("workspace-1", "user-1")
        with mock.patch.object(native.legacy, "_request", return_value=[carry_in] * 1000) as request:
            with self.assertRaisesRegex(native.legacy.PortfolioPostError, "pagination"):
                gateway.period_entries("2026-09-26T09:13:00Z", "2026-09-26T09:14:00Z")
        self.assertEqual(100, request.call_count)

    def test_later_contiguous_grid_block_is_not_silently_omitted(self):
        first = row("review-first", "2026-09-07 12:00", "2026-09-07 12:30", 30)
        later = row("review-later", "2026-09-07 13:00", "2026-09-07 13:30", 30)
        document = capture(first)
        document["structuredContent"]["sheets"][0]["data"].append({
            "startRow": 2, "startColumn": 0,
            "rowData": [{"values": [cell(value) for value in later]}],
        })
        plan = self._plan(document)
        self.assertEqual([("review-first", 2), ("review-later", 3)],
                         [(entry["review_id"], entry["row_number"]) for entry in plan["entries"]])
        self.assertEqual(60, plan["total_minutes"])

    def test_sparse_header_and_row_839_keep_actual_physical_row_number(self):
        current = row("review-current", "2026-09-07 12:00", "2026-09-07 12:30", 30)
        document = capture()
        document["structuredContent"]["sheets"][0]["data"].append({
            "startRow": 838, "startColumn": 0,
            "rowData": [{"values": [cell(value) for value in current]}],
        })
        plan = self._plan(document)
        self.assertEqual(1, plan["row_count"])
        self.assertEqual([839], [entry["row_number"] for entry in plan["entries"]])
        _, _, all_rows = native._sheet_rows(document, pending_only=False)
        self.assertEqual([839], [number for number, _ in all_rows])

    def test_out_of_order_blocks_preserve_physical_order_and_pending_filter(self):
        posted = row("review-posted", "2026-09-07 12:00", "2026-09-07 12:30", 30)
        posted[9], posted[13] = "Approved", "posted"
        pending = row("review-pending", "2026-09-07 13:00", "2026-09-07 13:30", 30)
        document = capture(posted)
        data = document["structuredContent"]["sheets"][0]["data"]
        data.insert(0, {"startRow": 838, "rowData": [
            {"values": [cell(value) for value in pending]},
        ]})
        _, _, pending_rows = native._sheet_rows(document)
        self.assertEqual([(839, "review-pending")],
                         [(number, value["Review ID"]) for number, value in pending_rows])
        _, _, all_rows = native._sheet_rows(document, pending_only=False)
        self.assertEqual([(2, "review-posted"), (839, "review-pending")],
                         [(number, value["Review ID"]) for number, value in all_rows])

    def test_overlapping_blocks_are_rejected_even_when_rows_match(self):
        document = capture(row("review-a", "2026-09-07 12:00", "2026-09-07 12:30", 30))
        data = document["structuredContent"]["sheets"][0]["data"]
        data.append({"startRow": 1, "rowData": [copy.deepcopy(data[0]["rowData"][1])]})
        with self.assertRaises(native.NativePostError):
            native._sheet_rows(document)

    def test_invalid_grid_positions_are_rejected_in_every_block(self):
        for block_index in (0, 1):
            for field, value in (("startRow", -1), ("startRow", True), ("startRow", 0.5),
                                 ("startRow", "0"), ("startColumn", 1),
                                 ("startColumn", -1), ("startColumn", False),
                                 ("startColumn", "0"), ("startColumn", None)):
                with self.subTest(block_index=block_index, field=field, value=value):
                    document = capture(row("review-a", "2026-09-07 12:00", "2026-09-07 12:30", 30))
                    data = document["structuredContent"]["sheets"][0]["data"]
                    data.append({"startRow": 838, "rowData": [{"values": []}]})
                    data[block_index][field] = value
                    with self.assertRaises(native.NativePostError):
                        native._sheet_rows(document)

    def test_contract_header_must_be_at_physical_row_one(self):
        document = capture(row("review-a", "2026-09-07 12:00", "2026-09-07 12:30", 30))
        document["structuredContent"]["sheets"][0]["data"][0]["startRow"] = 838
        with self.assertRaises(native.NativePostError):
            native._sheet_rows(document)

    def same_window_v3_plan(self, descriptions=None):
        from test_review_batches import proposal, identity
        descriptions = descriptions or ["SC — Prepared original guarded publication result",
                                        "SC — Verified exact receipt binding result"]
        sources = [proposal(1), proposal(2)]
        for source, description in zip(sources, descriptions):
            source.update(clockify_project_suffix="123456", tag_names=["Process"], tag_suffixes=["654321"],
                          human_corrected=True, description=description)
        ids = [identity(1), identity(2)]
        contexts = dict.fromkeys(ids, "approved-daily-project-presentation/v1")
        package = batches.package_review_batches(sources,
            editorial_descriptions=dict(zip(ids, descriptions)), work_contexts=contexts,
            wording_only_ids=ids, allow_review_overlaps=True)
        document = capture(publisher.portfolio_row(package["activities"][0], "source-run"))
        return native.build_plan(document, capture_sha256="c" * 64, routing=route(), routing_sha256="r" * 64,
            timezone="Europe/Bucharest", workspace_id="workspace-1", member_id="user-1",
            projects=[{"id": "project-123456"}], tags=[{"id": "tag-654321"}], live_entries=[],
            review_batches=package, review_batch_sources=sources, review_batch_work_contexts=contexts,
            review_batch_authority={"consolidation_policy": "approved-daily-project-presentation/v3", "wording_only_ids": ids})

    def test_distinct_same_window_children_post_and_read_back_all_142_seconds_not_payload_alias_credit(self):
        plan = self.same_window_v3_plan()
        gateway = FakeGateway()
        now = dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            events, receipt_path = Path(directory) / "events.jsonl", Path(directory) / "receipt.json"
            receipt = native.execute_plan(plan, self._approval(plan), events, receipt_path, gateway, now=now)
            self.assertEqual(2, len(gateway.posts))
            self.assertEqual(["SC — Prepared original guarded publication result", "SC — Verified exact receipt binding result"],
                             [entry["description"] for entry in gateway.posts])
            self.assertEqual(["created", "created"], [entry["disposition"] for entry in receipt["entries"]])
            self.assertEqual(["created-1", "created-2"], receipt["review_groups"][0]["clockify_entry_ids"])
            self.assertEqual(142, receipt["review_groups"][0]["duration_seconds"])
            readbacks = [gateway.entry_by_id(entry["clockify_entry_id"]) for entry in receipt["entries"]]
            self.assertEqual(142, sum(int((dt.datetime.fromisoformat(entry["timeInterval"]["end"].replace("Z", "+00:00"))
                - dt.datetime.fromisoformat(entry["timeInterval"]["start"].replace("Z", "+00:00"))).total_seconds())
                for entry in readbacks))
            retry = native.execute_plan(plan, self._approval(plan), events, receipt_path, gateway, now=now)
            self.assertEqual(receipt, retry)
            self.assertEqual(2, len(gateway.posts))

    def test_same_window_v3_distinct_claims_recover_ambiguous_creates_without_reusing_one_entry(self):
        plan = self.same_window_v3_plan()
        gateway = FakeGateway(ambiguous=True)
        now = dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            events, receipt_path = Path(directory) / "events.jsonl", Path(directory) / "receipt.json"
            receipt = native.execute_plan(plan, self._approval(plan), events, receipt_path, gateway, now=now)
            self.assertEqual(2, len(gateway.posts))
            self.assertEqual(["created-1", "created-2"], [entry["clockify_entry_id"] for entry in receipt["entries"]])
            self.assertEqual(["recovered_after_ambiguous_response"] * 2,
                             [entry["disposition"] for entry in receipt["entries"]])
            self.assertEqual(142, receipt["review_groups"][0]["duration_seconds"])
            self.assertEqual(receipt, native.execute_plan(plan, self._approval(plan), events, receipt_path, gateway, now=now))
            self.assertEqual(2, len(gateway.posts))

    def test_identical_native_payloads_for_distinct_v3_claims_remain_unready_until_source_equivalence_verified(self):
        with self.assertRaisesRegex(native.NativePostError, "identical native payloads; verify source equivalence"):
            self.same_window_v3_plan(["SC — Same reviewed accomplishment description"] * 2)

    def test_v3_independent_claims_cannot_alias_or_share_one_native_credit_identity(self):
        plan = self.same_window_v3_plan()
        independent = native._independent_review_ids(plan)
        entries = copy.deepcopy(plan["entries"])
        entries[1]["payload"] = copy.deepcopy(entries[0]["payload"])
        self.assertEqual({}, native._batch_alias_leaders(entries, independent_review_ids=independent))
        with self.assertRaisesRegex(native.NativePostError, "distinct Clockify entries"):
            native._check_unique_native_ids(entries, ["one-entry", "one-entry"], independent_review_ids=independent)

    def test_frozen_presentation_authority_plans_corrected_overlapping_children_without_posts(self):
        from test_review_batches import proposal, identity
        sources = [proposal(1), proposal(2, "2026-09-29T10:00:30+03:00", "2026-09-29T10:01:53+03:00", 83)]
        descriptions = ["SC — Prepared original guarded publication result", "SC — Verified exact receipt binding result"]
        for source, description in zip(sources, descriptions):
            source.update(clockify_project_suffix="123456", tag_names=["Process"], tag_suffixes=["654321"],
                          human_corrected=True, description=description)
        ids = [identity(1), identity(2)]
        contexts = dict.fromkeys(ids, "approved-daily-project-presentation/v1")
        package = batches.package_review_batches(sources,
            editorial_descriptions=dict(zip(ids, descriptions)), work_contexts=contexts,
            wording_only_ids=ids, allow_review_overlaps=True)
        document = capture(publisher.portfolio_row(package["activities"][0], "source-run"))
        inputs = dict(capture_sha256="c" * 64, routing=route(), routing_sha256="r" * 64,
            timezone="Europe/Bucharest", workspace_id="workspace-1", member_id="user-1",
            projects=[{"id": "project-123456"}], tags=[{"id": "tag-654321"}], live_entries=[],
            review_batches=package, review_batch_sources=sources, review_batch_work_contexts=contexts)
        authority = {"consolidation_policy": "approved-daily-project-presentation/v3", "wording_only_ids": ids}
        plan = native.build_plan(document, **inputs, review_batch_authority=authority)
        self.assertEqual(1, plan["row_count"])
        self.assertEqual(154 / 60, plan["total_minutes"])
        self.assertEqual(ids, [entry["review_id"] for entry in plan["entries"]])
        self.assertEqual(["2026-09-29T07:00:07Z", "2026-09-29T07:00:30Z"],
                         [entry["payload"]["start"] for entry in plan["entries"]])
        self.assertEqual(["2026-09-29T07:01:18Z", "2026-09-29T07:01:53Z"],
                         [entry["payload"]["end"] for entry in plan["entries"]])
        self.assertEqual(descriptions, [entry["payload"]["description"] for entry in plan["entries"]])
        self.assertEqual(authority, plan["review_batches_binding"]["preparation_authority"])
        native._validate_review_batch_plan(plan)
        with self.assertRaises(native.NativePostError):
            native.build_plan(document, **inputs)
        tampered = copy.deepcopy(plan)
        tampered["review_batches_binding"]["preparation_authority"]["wording_only_ids"] = []
        with self.assertRaises(native.NativePostError):
            native._validate_review_batch_plan(tampered)

    def _plan(self, document, routing=None, existing=None, projects=None, tags=None):
        raw = json.dumps(document, sort_keys=True).encode()
        routing = routing or route()
        return native.build_plan(
            document, capture_sha256=hashlib.sha256(raw).hexdigest(), routing=routing,
            routing_sha256="a" * 64, timezone="Europe/Bucharest",
            workspace_id="workspace-1", member_id="user-1",
            projects=projects or [{"id": "project-123456"}],
            tags=tags or [{"id": "tag-654321"}],
            live_entries=existing or [],
        )

    def _approval(self, plan):
        return native.approval_template(
            plan, approval_id="approval-1", approver="human board user",
            approved_at="2026-10-04T10:00:00Z", expires_at="2026-10-05T10:00:00Z",
        )

    def test_posted_canonical_source_credits_renamed_review_without_native_write(self):
        prior = row("review-old", "2026-09-07 12:00", "2026-09-07 12:30", 30)
        prior[6], prior[9], prior[13] = "act-0123456789abcdef01234567", "Approved", "posted"
        current = row("review-new", "2026-09-07 12:00", "2026-09-07 12:30", 30)
        current[6], current[8] = "act-0123456789abcdef01234567", "Renamed client accomplishment"
        existing = live("prior-native", "2026-09-07T09:00:00Z", "2026-09-07T09:30:00Z")
        plan = self._plan(capture(prior, current), existing=[existing])
        gateway = FakeGateway([existing])
        with tempfile.TemporaryDirectory() as directory:
            events, receipt_path = Path(directory) / "events.jsonl", Path(directory) / "receipt.json"
            receipt = native.execute_plan(plan, self._approval(plan), events, receipt_path, gateway,
                                          now=dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc))
            original_bytes = receipt_path.read_bytes()
            rerun = native.execute_plan(plan, self._approval(plan), events, receipt_path, gateway,
                                        now=dt.datetime(2026, 10, 4, 11, 1, tzinfo=dt.timezone.utc))
            self.assertEqual(receipt, rerun)
            self.assertEqual(original_bytes, receipt_path.read_bytes())
        self.assertEqual([], gateway.posts)
        self.assertEqual(1, plan["row_count"])
        self.assertEqual(30, plan["total_minutes"])
        self.assertEqual("Renamed client accomplishment", plan["entries"][0]["payload"]["description"])
        credit = plan["entries"][0]["sheet_source_credit"]
        self.assertEqual("act-0123456789abcdef01234567", credit["source"])
        self.assertEqual("review-old", credit["prior_row"]["Review ID"])
        self.assertEqual(2, credit["prior_row_number"])
        self.assertEqual("Approved work", credit["payload"]["description"])
        result = receipt["entries"][0]
        self.assertEqual("prior-native", result["clockify_entry_id"])
        self.assertEqual("existing_credit", result["disposition"])
        self.assertEqual("existing_source_retained_current_payload_not_posted", result["posting_semantics"])
        self.assertEqual(0, receipt["posted_minutes"])
        self.assertEqual(30, receipt["existing_credit_minutes"])
        self.assertEqual("Approved work", gateway.entries[0]["description"])

    def test_posted_source_credit_requires_exact_verified_identity_not_overlap(self):
        cases = [
            ("different source", 6, "act-ffffffffffffffffffffffff"),
            ("blank source", 6, ""),
            ("noncanonical source", 6, "activity"),
            ("source suffix", 6, "act-0123456789abcdef01234567-s00"),
            ("different start", 1, "2026-09-07 12:10"),
            ("different end", 2, "2026-09-07 12:20"),
            ("unapproved", 9, "pending"),
            ("unposted", 13, "unposted"),
            ("invalid prior duration", 3, 29),
        ]
        for label, index, value in cases:
            with self.subTest(case=label):
                prior = row("review-old", "2026-09-07 12:00", "2026-09-07 12:30", 30)
                prior[6], prior[9], prior[13] = "act-0123456789abcdef01234567", "Approved", "posted"
                prior[index] = value
                if index in (1, 2):
                    prior[3] = 20
                current = row("review-new", "2026-09-07 12:00", "2026-09-07 12:30", 30)
                current[6], current[8] = "act-0123456789abcdef01234567", "Renamed work"
                if label in {"blank source", "noncanonical source", "source suffix"}:
                    current[6] = value  # Equal unsupported strings must not become identity.
                existing = live("prior-native", "2026-09-07T09:00:00Z", "2026-09-07T09:30:00Z")
                plan = self._plan(capture(prior, current), existing=[existing])
                gateway = FakeGateway([existing])
                with tempfile.TemporaryDirectory() as directory:
                    receipt = native.execute_plan(plan, self._approval(plan), Path(directory) / "events.jsonl",
                                                  Path(directory) / "receipt.json", gateway,
                                                  now=dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc))
                self.assertEqual(1, len(gateway.posts))
                self.assertEqual("created", receipt["entries"][0]["disposition"])

    def test_same_source_changed_resolved_route_is_not_credited(self):
        for field, value in (("project_suffix", "999999"), ("task_id", "different-task"),
                             ("tag_suffixes", ["888888"]), ("billable", False)):
            with self.subTest(field=field):
                prior = row("review-old", "2026-09-07 12:00", "2026-09-07 12:30", 30)
                prior[6], prior[9], prior[13] = "act-0123456789abcdef01234567", "Approved", "posted"
                current = row("review-new", "2026-09-07 12:00", "2026-09-07 12:30", 30, project="Changed")
                current[6], current[8] = "act-0123456789abcdef01234567", "Renamed work"
                routing = route()
                changed = copy.deepcopy(routing["session_routes"][0])
                changed["project_name"], changed[field] = "Changed", value
                routing["session_routes"].append(changed)
                existing = live("prior-native", "2026-09-07T09:00:00Z", "2026-09-07T09:30:00Z")
                plan = self._plan(capture(prior, current), routing, [existing],
                                  projects=[{"id": "project-123456"}, {"id": "project-999999"}],
                                  tags=[{"id": "tag-654321"}, {"id": "tag-888888"}])
                gateway = FakeGateway([existing])
                with tempfile.TemporaryDirectory() as directory:
                    receipt = native.execute_plan(plan, self._approval(plan), Path(directory) / "events.jsonl",
                                                  Path(directory) / "receipt.json", gateway,
                                                  now=dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc))
                self.assertEqual(1, len(gateway.posts))
                self.assertEqual("created", receipt["entries"][0]["disposition"])

    def test_unverified_posted_identity_does_not_invent_credit(self):
        for case in ("missing", "description mismatch", "task mismatch"):
            with self.subTest(case=case):
                prior = row("review-old", "2026-09-07 12:00", "2026-09-07 12:30", 30)
                prior[6], prior[9], prior[13] = "act-0123456789abcdef01234567", "Approved", "posted"
                current = row("review-new", "2026-09-07 12:00", "2026-09-07 12:30", 30)
                current[6], current[8] = "act-0123456789abcdef01234567", "Renamed work"
                existing = [live("prior-native", "2026-09-07T09:00:00Z", "2026-09-07T09:30:00Z")]
                priors = [prior]
                if case == "missing":
                    existing = []
                elif case == "description mismatch":
                    existing[0]["description"] = "Unrelated accomplishment"
                elif case == "task mismatch":
                    existing[0]["taskId"] = "wrong-task"
                plan = self._plan(capture(*priors, current), existing=existing)
                gateway = FakeGateway(existing)
                with tempfile.TemporaryDirectory() as directory:
                    receipt = native.execute_plan(plan, self._approval(plan), Path(directory) / "events.jsonl",
                                                  Path(directory) / "receipt.json", gateway,
                                                  now=dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc))
                self.assertEqual(1, len(gateway.posts))
                self.assertEqual("created", receipt["entries"][0]["disposition"])

    def test_successive_posted_source_aliases_credit_one_verified_native_identity(self):
        for alias_description in ("Approved work", "Previously renamed work"):
            with self.subTest(description=alias_description):
                prior = row("review-old", "2026-09-07 12:00", "2026-09-07 12:30", 30)
                prior[6], prior[9], prior[13] = "act-0123456789abcdef01234567", "Approved", "posted"
                alias = ["review-old-alias", *prior[1:]]
                alias[8] = alias_description
                current = row("review-latest", "2026-09-07 12:00", "2026-09-07 12:30", 30)
                current[6], current[8] = "act-0123456789abcdef01234567", "Newest client wording"
                existing = live("prior-native", "2026-09-07T09:00:00Z", "2026-09-07T09:30:00Z")
                plan = self._plan(capture(prior, alias, current), existing=[existing])
                gateway = FakeGateway([existing])
                with tempfile.TemporaryDirectory() as directory:
                    events, receipt_path = Path(directory) / "events.jsonl", Path(directory) / "receipt.json"
                    receipt = native.execute_plan(plan, self._approval(plan), events, receipt_path, gateway,
                                                  now=dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc))
                    rerun = native.execute_plan(plan, self._approval(plan), events, receipt_path, gateway,
                                                now=dt.datetime(2026, 10, 4, 11, 1, tzinfo=dt.timezone.utc))
                    self.assertEqual(receipt, rerun)
                self.assertEqual([], gateway.posts)
                self.assertEqual("prior-native", receipt["entries"][0]["clockify_entry_id"])
                self.assertEqual("existing_credit", receipt["entries"][0]["disposition"])

    def test_multiple_verified_native_matches_for_same_source_fail_closed(self):
        prior = row("review-old", "2026-09-07 12:00", "2026-09-07 12:30", 30)
        prior[6], prior[9], prior[13] = "act-0123456789abcdef01234567", "Approved", "posted"
        current = row("review-new", "2026-09-07 12:00", "2026-09-07 12:30", 30)
        current[6], current[8] = "act-0123456789abcdef01234567", "Renamed work"
        existing = [live("prior-native-a", "2026-09-07T09:00:00Z", "2026-09-07T09:30:00Z"),
                    live("prior-native-b", "2026-09-07T09:00:00Z", "2026-09-07T09:30:00Z")]
        with self.assertRaisesRegex(native.NativePostError, "source.*ambiguous"):
            self._plan(capture(prior, current), existing=existing)

    def test_source_credit_requires_untampered_source_and_fresh_direct_get_on_resume(self):
        prior = row("review-old", "2026-09-07 12:00", "2026-09-07 12:30", 30)
        prior[6], prior[9], prior[13] = "act-0123456789abcdef01234567", "Approved", "posted"
        current = row("review-new", "2026-09-07 12:00", "2026-09-07 12:30", 30)
        current[6], current[8] = "act-0123456789abcdef01234567", "Renamed work"
        existing = live("prior-native", "2026-09-07T09:00:00Z", "2026-09-07T09:30:00Z")
        plan = self._plan(capture(prior, current), existing=[existing])
        # An internally inconsistent credit remains invalid even if reapproved.
        tampered = copy.deepcopy(plan)
        self.assertIn("sheet_source_credit", tampered["entries"][0])
        credit = tampered["entries"][0]["sheet_source_credit"]
        credit["source"] = "act-ffffffffffffffffffffffff"
        credit["credit_digest"] = native._document_digest(credit, "credit_digest")
        tampered["plan_digest"] = native._document_digest(tampered, "plan_digest")
        with tempfile.TemporaryDirectory() as directory:
            gateway = FakeGateway([existing])
            with self.assertRaisesRegex(native.NativePostError, "source credit"):
                native.execute_plan(tampered, self._approval(tampered), Path(directory) / "tampered.jsonl",
                                    Path(directory) / "tampered.json", gateway,
                                    now=dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc))
            self.assertEqual([], gateway.posts)

            events, receipt_path = Path(directory) / "events.jsonl", Path(directory) / "receipt.json"
            approval = self._approval(plan)
            with mock.patch.object(gateway, "entry_by_id", return_value=None):
                with self.assertRaisesRegex(native.NativePostError, "direct GET readback"):
                    native.execute_plan(plan, approval, events, receipt_path, gateway,
                                        now=dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc))
            self.assertFalse(events.exists())
            self.assertEqual([], gateway.posts)
            # A crash after durable credit intent must resume via the sealed ID,
            # never through ambiguous-create recovery or a fresh POST.
            append_event = native._append_event
            def interrupt_after_intent(path, event):
                append_event(path, event)
                raise KeyboardInterrupt("credit intent persisted")
            with mock.patch.object(native, "_append_event", side_effect=interrupt_after_intent):
                with self.assertRaises(KeyboardInterrupt):
                    native.execute_plan(plan, approval, events, receipt_path, gateway,
                                        now=dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc))
            self.assertEqual(["intent"], [event["event_type"] for event in native._events(events)])
            native.execute_plan(plan, approval, events, receipt_path, gateway,
                                now=dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc))
            original_bytes = receipt_path.read_bytes()
            for field, value in (("taskId", "wrong-task"), ("description", "Changed native entry")):
                gateway.entries = [{**existing, field: value}]
                with self.subTest(field=field), self.assertRaisesRegex(native.NativePostError, "direct GET readback"):
                    native.execute_plan(plan, approval, events, receipt_path, gateway,
                                        now=dt.datetime(2026, 10, 4, 11, 1, tzinfo=dt.timezone.utc))
                self.assertEqual(original_bytes, receipt_path.read_bytes())
            self.assertEqual([], gateway.posts)

    def test_two_exact_pending_source_aliases_share_existing_credit_without_post(self):
        prior = row("review-old", "2026-09-07 12:00", "2026-09-07 12:30", 30)
        prior[6], prior[9], prior[13] = "act-0123456789abcdef01234567", "Approved", "posted"
        aliases = []
        for review_id, description in (("review-new-a", "Renamed work A"), ("review-new-b", "Renamed work B")):
            current = row(review_id, "2026-09-07 12:00", "2026-09-07 12:30", 30)
            current[6], current[8] = "act-0123456789abcdef01234567", description
            aliases.append(current)
        existing = live("prior-native", "2026-09-07T09:00:00Z", "2026-09-07T09:30:00Z")
        plan = self._plan(capture(prior, *aliases), existing=[existing])
        gateway = FakeGateway([existing])
        with tempfile.TemporaryDirectory() as directory:
            events, receipt_path = Path(directory) / "events.jsonl", Path(directory) / "receipt.json"
            receipt = native.execute_plan(plan, self._approval(plan), events, receipt_path, gateway,
                                          now=dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc))
            original_bytes = receipt_path.read_bytes()
            rerun = native.execute_plan(plan, self._approval(plan), events, receipt_path, gateway,
                                        now=dt.datetime(2026, 10, 4, 11, 1, tzinfo=dt.timezone.utc))
            self.assertEqual(receipt, rerun)
            self.assertEqual(original_bytes, receipt_path.read_bytes())
        self.assertEqual([], gateway.posts)
        self.assertEqual(["prior-native", "prior-native"], [item["clockify_entry_id"] for item in receipt["entries"]])
        self.assertEqual(["existing_credit", "existing_credit"], [item["disposition"] for item in receipt["entries"]])
        self.assertEqual(60, receipt["total_minutes"])
        self.assertEqual(0, receipt["posted_minutes"])
        self.assertEqual(30, receipt["existing_credit_minutes"])

    def test_different_canonical_sources_cannot_share_one_native_credit(self):
        rows = []
        for number, source in enumerate(("act-0123456789abcdef01234567", "act-ffffffffffffffffffffffff")):
            prior = row(f"review-old-{number}", "2026-09-07 12:00", "2026-09-07 12:30", 30)
            prior[6], prior[9], prior[13] = source, "Approved", "posted"
            current = row(f"review-new-{number}", "2026-09-07 12:00", "2026-09-07 12:30", 30)
            current[6], current[8] = source, "Renamed work"
            rows.extend((prior, current))
        existing = live("prior-native", "2026-09-07T09:00:00Z", "2026-09-07T09:30:00Z")
        plan = self._plan(capture(*rows), existing=[existing])
        gateway = FakeGateway([existing])
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(native.NativePostError, "unique Clockify entry"):
                native.execute_plan(plan, self._approval(plan), Path(directory) / "events.jsonl",
                                    Path(directory) / "receipt.json", gateway,
                                    now=dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc))
        self.assertEqual([], gateway.posts)

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

    def test_resealed_plan_with_inconsistent_row_payload_digest_blocks_every_post(self):
        plan = self._plan(capture(
            row("review-a", "2026-09-07 12:00", "2026-09-07 12:30", 30),
            row("review-b", "2026-09-07 13:00", "2026-09-07 13:30", 30),
        ))
        plan["entries"][1]["payload_digest"] = "0" * 64
        plan["plan_digest"] = native._document_digest(plan, "plan_digest")
        gateway = FakeGateway()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(native.NativePostError, "payload digest"):
                native.execute_plan(plan, self._approval(plan), Path(directory) / "events.jsonl",
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

    def test_preexisting_exact_entry_without_identity_never_creates_another_copy(self):
        exact = live("unrelated-exact", "2026-09-07T09:00:00Z", "2026-09-07T09:30:00Z")
        plan = self._plan(capture(row("review-a", "2026-09-07 12:00", "2026-09-07 12:30", 30)),
                          existing=[exact])
        gateway = FakeGateway([exact])
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(native.NativePostError, "existing exact payload"):
                native.execute_plan(plan, self._approval(plan), Path(directory) / "events.jsonl",
                                    Path(directory) / "receipt.json", gateway,
                                    now=dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc))
            self.assertFalse((Path(directory) / "events.jsonl").exists())
        self.assertEqual([], gateway.posts)

    def test_unbound_exact_match_in_later_row_prevents_partial_batch_posting(self):
        exact = live("existing-exact", "2026-09-07T09:00:00Z", "2026-09-07T09:30:00Z")
        plan = self._plan(capture(
            row("new-work", "2026-09-07 11:00", "2026-09-07 11:10", 10),
            row("duplicate-work", "2026-09-07 12:00", "2026-09-07 12:30", 30),
        ), existing=[exact])
        gateway = FakeGateway([exact])
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(native.NativePostError, "duplicate-work"):
                native.execute_plan(plan, self._approval(plan), Path(directory) / "events.jsonl",
                                    Path(directory) / "receipt.json", gateway,
                                    now=dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc))
        self.assertEqual([], gateway.posts)

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

    def test_identical_batch_payloads_create_once_and_receipt_preserves_all_reviews(self):
        # Missing within-batch equivalence must produce two POSTs and fail this test.
        plan = self._plan(capture(
            row("review-a", "2026-09-07 12:00", "2026-09-07 12:30", 30),
            row("review-b", "2026-09-07 12:00", "2026-09-07 12:30", 30),
        ))
        gateway = FakeGateway()
        with tempfile.TemporaryDirectory() as directory:
            result = native.execute_plan(plan, self._approval(plan), Path(directory) / "events.jsonl",
                                         Path(directory) / "receipt.json", gateway,
                                         now=dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc))
        self.assertEqual(1, len(gateway.posts))
        self.assertEqual(["review-a", "review-b"], [item["review_id"] for item in result["entries"]])
        self.assertEqual(["created-1", "created-1"], [item["clockify_entry_id"] for item in result["entries"]])
        self.assertEqual("same_batch_payload_credit", result["entries"][1]["disposition"])
        self.assertEqual("review-a", result["entries"][1]["alias_of_review_id"])
        self.assertEqual(30, result["posted_minutes"])
        self.assertEqual(30, result["same_batch_alias_minutes"])
        self.assertEqual(60, result["total_minutes"])

    def test_identical_batch_payload_alias_resumes_after_leader_crash_without_repost(self):
        # A restart that resumes by review ID alone would post the alias again.
        plan = self._plan(capture(
            row("review-a", "2026-09-07 12:00", "2026-09-07 12:30", 30),
            row("review-b", "2026-09-07 12:00", "2026-09-07 12:30", 30),
        ))
        gateway = CrashAfterCreateGateway()
        with tempfile.TemporaryDirectory() as directory:
            events, receipt = Path(directory) / "events.jsonl", Path(directory) / "receipt.json"
            approval = self._approval(plan)
            with self.assertRaises(KeyboardInterrupt):
                native.execute_plan(plan, approval, events, receipt, gateway,
                                    now=dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc))
            gateway.create = FakeGateway.create.__get__(gateway)
            result = native.execute_plan(plan, approval, events, receipt, gateway,
                                         now=dt.datetime(2026, 10, 4, 11, 1, tzinfo=dt.timezone.utc))
            native.execute_plan(plan, approval, events, receipt, gateway,
                                now=dt.datetime(2026, 10, 4, 11, 2, tzinfo=dt.timezone.utc))
        self.assertEqual(1, len(gateway.posts))
        self.assertEqual("same_batch_payload_credit", result["entries"][1]["disposition"])

    def test_batch_alias_marked_intent_resumes_after_confirmation_interruption(self):
        plan = self._plan(capture(
            row("review-a", "2026-09-07 12:00", "2026-09-07 12:30", 30),
            row("review-b", "2026-09-07 12:00", "2026-09-07 12:30", 30),
        ))
        gateway = FakeGateway()
        append = native._append_event

        def crash_before_alias_confirm(path, event):
            if event.get("disposition") == "same_batch_payload_credit":
                raise KeyboardInterrupt("crash after durable alias intent")
            append(path, event)

        with tempfile.TemporaryDirectory() as directory:
            events, receipt = Path(directory) / "events.jsonl", Path(directory) / "receipt.json"
            with mock.patch.object(native, "_append_event", side_effect=crash_before_alias_confirm):
                with self.assertRaises(KeyboardInterrupt):
                    native.execute_plan(plan, self._approval(plan), events, receipt, gateway,
                                        now=dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc))
            result = native.execute_plan(plan, self._approval(plan), events, receipt, gateway,
                                         now=dt.datetime(2026, 10, 4, 11, 1, tzinfo=dt.timezone.utc))
        self.assertEqual(1, len(gateway.posts))
        self.assertEqual("review-a", result["entries"][1]["alias_of_review_id"])

    def test_batch_alias_cannot_reinterpret_an_old_unmarked_intent(self):
        plan = self._plan(capture(
            row("review-a", "2026-09-07 12:00", "2026-09-07 12:30", 30),
            row("review-b", "2026-09-07 12:00", "2026-09-07 12:30", 30),
        ))
        approval = self._approval(plan)
        gateway = FakeGateway()
        with tempfile.TemporaryDirectory() as directory:
            events = Path(directory) / "events.jsonl"
            item = plan["entries"][1]
            native._append_event(events, {"event_type": "intent", "approval_digest": native._digest(approval),
                                "plan_digest": plan["plan_digest"], "review_id": item["review_id"],
                                "payload_digest": item["payload_digest"], "before_entry_ids": [],
                                "recorded_at": "2026-10-04T11:00:00Z"})
            with self.assertRaisesRegex(native.NativePostError, "historical alias intent"):
                native.execute_plan(plan, approval, events, Path(directory) / "receipt.json", gateway,
                                    now=dt.datetime(2026, 10, 4, 11, 1, tzinfo=dt.timezone.utc))
        self.assertEqual([], gateway.posts)

    def test_batch_alias_changed_description_remains_a_separate_approved_entry(self):
        first = row("review-a", "2026-09-07 12:00", "2026-09-07 12:30", 30)
        second = row("review-b", "2026-09-07 12:00", "2026-09-07 12:30", 30)
        second[8] = "Different approved work"
        plan = self._plan(capture(first, second))
        gateway = FakeGateway()
        with tempfile.TemporaryDirectory() as directory:
            result = native.execute_plan(plan, self._approval(plan), Path(directory) / "events.jsonl",
                                         Path(directory) / "receipt.json", gateway,
                                         now=dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc))
        self.assertEqual(2, len(gateway.posts))
        self.assertEqual(["created-1", "created-2"], [item["clockify_entry_id"] for item in result["entries"]])

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
