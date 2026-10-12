"""Native approval/posting of grouped rows must preserve every original window."""
import copy
import datetime as dt
import inspect
from pathlib import Path
import tempfile
import unittest

from scripts import clockify_native_sheet_post as native
from scripts import clockify_review_batches as batches
from scripts import clockify_sheet_publish as publisher
from test_clockify_native_sheet_post import FakeGateway, capture, route, row
from test_review_batches import proposal, identity


NOW = dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc)


class LateReadbackFailure(FakeGateway):
    fail_second = True

    def entry_by_id(self, entry_id):
        if self.fail_second and entry_id == "created-2":
            return None
        return super().entry_by_id(entry_id)


class NativeReviewBatchTests(unittest.TestCase):
    def setUp(self):
        self.sources = [proposal(1), proposal(2, "2026-09-29T10:10:00+03:00",
                                                   "2026-09-29T10:11:23+03:00", 83)]
        for source in self.sources:
            source.update(clockify_project_suffix="123456", tag_names=["Process"], tag_suffixes=["654321"])
        self.contexts = {identity(1): "explicit-work", identity(2): "explicit-work"}
        self.package = batches.package_review_batches(self.sources,
            editorial_descriptions={identity(1): "SC — Prepared one", identity(2): "SC — Prepared two"},
            work_contexts=self.contexts)
        self.group = self.package["activities"][0]
        self.sheet_row = publisher.portfolio_row(self.group, "source-run")

    def plan(self, sheet_row=None, package=None, sources=None, contexts=None, extra_rows=()):
        self.assertIn("review_batches", inspect.signature(native.build_plan).parameters,
                      "Native consumer must accept source-bound review batches")
        return native.build_plan(capture(sheet_row or self.sheet_row, *extra_rows),
            capture_sha256="c" * 64, routing=route(), routing_sha256="r" * 64,
            timezone="Europe/Bucharest", workspace_id="workspace-1", member_id="user-1",
            projects=[{"id": "project-123456"}], tags=[{"id": "tag-654321"}], live_entries=[],
            review_batches=self.package if package is None else package,
            review_batch_sources=self.sources if sources is None else sources,
            review_batch_work_contexts=self.contexts if contexts is None else contexts)

    def approval(self, plan):
        return native.approval_template(plan, approval_id="board-approval", approver="human board user",
                                       approved_at="2026-10-04T10:00:00Z", expires_at="2026-10-05T10:00:00Z")

    def test_multitag_batch_routes_by_tag_identity_not_config_order(self):
        for source in self.sources:
            source.update(tag_names=["Process", "Development"], tag_suffixes=["654321", "987654"])
        package = batches.package_review_batches(self.sources,
            editorial_descriptions={identity(1): "SC — Prepared one", identity(2): "SC — Prepared two"},
            work_contexts=self.contexts)
        row = publisher.portfolio_row(package["activities"][0], "source-run")
        routing = route()
        routing["session_routes"][0].update(tag_names=["Process", "Development"], tag_suffixes=["654321", "987654"])
        plan = native.build_plan(capture(row), capture_sha256="c" * 64,
            routing=routing, routing_sha256="r" * 64, timezone="Europe/Bucharest",
            workspace_id="workspace-1", member_id="user-1", projects=[{"id": "project-123456"}],
            tags=[{"id": "tag-654321"}, {"id": "tag-987654"}], live_entries=[],
            review_batches=package, review_batch_sources=self.sources, review_batch_work_contexts=self.contexts)
        self.assertEqual(1, plan["row_count"])
        self.assertEqual([["tag-654321", "tag-987654"]] * 2, [entry["payload"]["tagIds"] for entry in plan["entries"]])

    def test_native_planning_rejects_removed_child_warning_from_parent_review(self):
        self.sources[0]["review_warnings"] = [{"type": "estimated_session_placement",
                                               "reason": "Observed work; estimated placement."}]
        self.package = batches.package_review_batches(self.sources,
            editorial_descriptions={identity(1): "SC — Prepared one", identity(2): "SC — Prepared two"},
            work_contexts=self.contexts)
        self.group = self.package["activities"][0]
        self.sheet_row = publisher.portfolio_row(self.group, "source-run")
        self.assertEqual(1, self.plan()["row_count"])
        removed_warning = list(self.sheet_row)
        removed_warning[12] = ""
        with self.assertRaises(native.NativePostError):
            self.plan(sheet_row=removed_warning)

    def test_disjoint_group_posts_only_original_windows_and_all_child_receipt_mapping(self):
        plan = self.plan()
        self.assertEqual(1, plan["row_count"])
        self.assertEqual([identity(1), identity(2)], [entry["review_id"] for entry in plan["entries"]])
        self.assertEqual([self.group["review_id"]] * 2, [entry["sheet_review_id"] for entry in plan["entries"]])
        self.assertEqual([2, 2], [entry["row_number"] for entry in plan["entries"]])
        gateway = FakeGateway()
        with tempfile.TemporaryDirectory() as directory:
            receipt = native.execute_plan(plan, self.approval(plan), Path(directory) / "events.jsonl",
                                          Path(directory) / "receipt.json", gateway, now=NOW)
        self.assertEqual(["2026-09-29T07:00:07Z", "2026-09-29T07:10:00Z"], [p["start"] for p in gateway.posts])
        self.assertEqual(["2026-09-29T07:01:18Z", "2026-09-29T07:11:23Z"], [p["end"] for p in gateway.posts])
        self.assertEqual(154 / 60, receipt["total_minutes"])
        self.assertEqual(1, receipt["row_count"])
        group_receipt = receipt["review_groups"][0]
        self.assertEqual(self.group["review_id"], group_receipt["sheet_review_id"])
        self.assertEqual([identity(1), identity(2)], group_receipt["child_review_ids"])
        self.assertEqual(["created-1", "created-2"], group_receipt["clockify_entry_ids"])
        self.assertEqual("complete", group_receipt["status"])

    def test_repeat_reads_every_child_and_does_not_create_again(self):
        plan = self.plan()
        gateway = FakeGateway()
        with tempfile.TemporaryDirectory() as directory:
            events, receipt_path = Path(directory) / "events.jsonl", Path(directory) / "receipt.json"
            first = native.execute_plan(plan, self.approval(plan), events, receipt_path, gateway, now=NOW)
            second = native.execute_plan(plan, self.approval(plan), events, receipt_path, gateway, now=NOW)
            self.assertEqual(first, second)
            gateway.entries[1]["timeInterval"]["end"] = "2026-09-29T07:12:23Z"
            with self.assertRaises(native.NativePostError):
                native.execute_plan(plan, self.approval(plan), events, receipt_path, gateway, now=NOW)
        self.assertEqual(2, len(gateway.posts))

    def test_late_child_failure_has_no_complete_group_receipt_and_retries_without_reposting(self):
        plan = self.plan()
        gateway = LateReadbackFailure()
        with tempfile.TemporaryDirectory() as directory:
            events, receipt_path = Path(directory) / "events.jsonl", Path(directory) / "receipt.json"
            with self.assertRaises(native.NativePostError):
                native.execute_plan(plan, self.approval(plan), events, receipt_path, gateway, now=NOW)
            self.assertFalse(receipt_path.exists())
            self.assertEqual(1, sum(event["event_type"] == "confirmed" for event in native._events(events)))
            gateway.fail_second = False
            receipt = native.execute_plan(plan, self.approval(plan), events, receipt_path, gateway, now=NOW)
            self.assertEqual("complete", receipt["review_groups"][0]["status"])
        self.assertEqual(2, len(gateway.posts))

    def test_changed_native_row_rejects_before_any_execution(self):
        for index, value in ((0, "pvi-" + "f" * 24), (1, "2026-09-29 10:00:00"),
                             (2, "2026-09-29 10:12:23"), (3, 3), (4, "Other"),
                             (5, "Different tag"), (8, "Changed clean wording")):
            with self.subTest(column=index):
                changed = list(self.sheet_row)
                changed[index] = value
                with self.assertRaises(native.NativePostError):
                    self.plan(sheet_row=changed)

    def test_missing_unknown_duplicate_or_changed_children_are_not_authority(self):
        changes = [
            lambda group: group["child_review_ids"].pop(),
            lambda group: group["child_review_ids"].__setitem__(1, identity(29)),
            lambda group: group["child_review_ids"].__setitem__(1, identity(1)),
            lambda group: group["allocation_segments"][1].__setitem__("start", "2026-09-29T10:10:01+03:00"),
            lambda group: group.__setitem__("duration_seconds", 155),
        ]
        for change in changes:
            package = copy.deepcopy(self.package)
            change(package["activities"][0])
            with self.assertRaises(native.NativePostError):
                self.plan(package=package)
        for sources in ([self.sources[0]], [*self.sources, copy.deepcopy(self.sources[0])]):
            with self.assertRaises(native.NativePostError):
                self.plan(sources=sources)

    def test_plan_seals_original_sources_contexts_and_package_without_mutable_rereads(self):
        plan = self.plan()
        original_digest = plan["plan_digest"]
        self.sources[0]["start"] = "2026-09-29T01:00:00+03:00"
        self.contexts[identity(1)] = "different work"
        self.package["activities"][0]["allocation_segments"][0]["start"] = "bad"
        self.assertEqual(original_digest, native._document_digest(plan, "plan_digest"))
        gateway = FakeGateway()
        with tempfile.TemporaryDirectory() as directory:
            receipt = native.execute_plan(plan, self.approval(plan), Path(directory) / "events.jsonl",
                                          Path(directory) / "receipt.json", gateway, now=NOW)
        self.assertEqual("complete", receipt["status"])
        self.assertEqual("2026-09-29T07:00:07Z", gateway.posts[0]["start"])

    def test_changed_context_or_approval_bound_source_cannot_execute(self):
        with self.assertRaises(native.NativePostError):
            self.plan(contexts={identity(1): "another", identity(2): "explicit-work"})
        plan = self.plan()
        approval = self.approval(plan)
        plan["review_batches_binding"]["source_proposals"][0]["duration_seconds"] = 70
        with tempfile.TemporaryDirectory() as directory:
            gateway = FakeGateway()
            with self.assertRaises(native.NativePostError):
                native.execute_plan(plan, approval, Path(directory) / "events.jsonl",
                                    Path(directory) / "receipt.json", gateway, now=NOW)
            self.assertEqual([], gateway.posts)

    def test_child_cannot_also_be_posted_as_an_ordinary_pending_row(self):
        duplicate_row = row(identity(1), "2026-09-29 10:00:07", "2026-09-29 10:01:18", 71 / 60)
        with self.assertRaises(native.NativePostError):
            self.plan(extra_rows=[duplicate_row])

    def test_captured_approved_or_posted_child_cannot_be_regrouped(self):
        for column, status in ((9, "approved"), (13, "posted")):
            historical = row(identity(1), "2026-09-29 10:00:07", "2026-09-29 10:01:18", 71 / 60)
            historical[column] = status
            with self.assertRaises(native.NativePostError):
                self.plan(extra_rows=[historical])

    def test_overlap_with_retained_original_source_blocks_group(self):
        overlapping = proposal(3, "2026-09-29T10:01:00+03:00", "2026-09-29T10:02:00+03:00", 60)
        overlapping["human_corrected"] = True
        with self.assertRaises(native.NativePostError):
            self.plan(sources=[*self.sources, overlapping])

    def test_disjoint_group_without_prepared_artifact_stays_rejected(self):
        with self.assertRaises(native.NativePostError):
            native.build_plan(capture(self.sheet_row), capture_sha256="c" * 64, routing=route(),
                routing_sha256="r" * 64, timezone="Europe/Bucharest", workspace_id="workspace-1",
                member_id="user-1", projects=[{"id": "project-123456"}], tags=[{"id": "tag-654321"}], live_entries=[])

    def test_historical_contiguous_pvi_without_artifact_preserves_ordinary_native_posting(self):
        historical = row("pvi-" + "a" * 24, "2026-08-29 10:00:07", "2026-08-29 10:01:18", 71 / 60)
        try:
            plan = native.build_plan(capture(historical), capture_sha256="c" * 64, routing=route(),
                routing_sha256="r" * 64, timezone="Europe/Bucharest", workspace_id="workspace-1",
                member_id="user-1", projects=[{"id": "project-123456"}], tags=[{"id": "tag-654321"}], live_entries=[])
        except native.NativePostError as error:
            self.fail(f"A historical contiguous pvi row is still an ordinary valid native interval: {error}")
        self.assertNotIn("review_batches_binding", plan)
        self.assertNotIn("sheet_review_id", plan["entries"][0])
        gateway = FakeGateway()
        with tempfile.TemporaryDirectory() as directory:
            receipt = native.execute_plan(plan, self.approval(plan), Path(directory) / "events.jsonl",
                                          Path(directory) / "receipt.json", gateway, now=NOW)
        self.assertEqual(1, len(gateway.posts))
        self.assertEqual("2026-08-29T07:00:07Z", gateway.posts[0]["start"])
        self.assertEqual("2026-08-29T07:01:18Z", gateway.posts[0]["end"])
        self.assertEqual("pvi-" + "a" * 24, receipt["entries"][0]["review_id"])
        self.assertNotIn("review_groups", receipt)


if __name__ == "__main__":
    unittest.main()
