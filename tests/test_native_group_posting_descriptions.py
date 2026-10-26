"""Fresh client-facing summaries never rewrite old approval-bound payloads."""
import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import clockify_native_sheet_post as native
from scripts import clockify_review_batches as batches
from scripts import clockify_sheet_publish as publisher
from test_clockify_native_sheet_post import FakeGateway, capture, route
from test_review_batches import proposal, identity


SUMMARY = "SC — Reconciled the review ledger while preserving original time"
CHILD_CAPTIONS = ["SC — Verified 265 rows with 61h29m retained",
                  "SC — Confirmed 35 cells and 28 minutes"]
NOW = dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc)
LEGACY_PLAN_DIGEST = "1cbe16318edd8e1cff33c2d5e41f741fde71f1f8adbfb2daaf7f635a415f7dab"


class NativeGroupPostingDescriptionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sources = [proposal(1), proposal(2, "2026-09-29T10:10:00+03:00",
                                               "2026-09-29T10:11:23+03:00", 83)]
        for number, source in enumerate(self.sources, 1):
            source.update(activity_id=f"act-{number:024x}", clockify_project_suffix="123456",
                          tag_names=["Process"], tag_suffixes=["654321"])
        self.contexts = {identity(1): "explicit-work", identity(2): "explicit-work"}
        options = dict(editorial_descriptions=dict(zip(self.contexts, CHILD_CAPTIONS)),
                       work_contexts=self.contexts, natural_batches=True, all_durations=True)
        first = batches.package_review_batches(self.sources, **options)["activities"][0]
        self.summaries = {first["review_id"]: {"child_review_ids": first["child_review_ids"], "description": SUMMARY}}
        self.package = batches.package_review_batches(self.sources, group_summaries=self.summaries, **options)
        self.group = self.package["activities"][0]
        self.parent = publisher.portfolio_row(self.group, "source-run")

    def plan(self, *, extra_rows=(), live_entries=()):
        return native.build_plan(capture(self.parent, *extra_rows), capture_sha256="c" * 64,
            routing=route(), routing_sha256="r" * 64, timezone="Europe/Bucharest",
            workspace_id="workspace-1", member_id="user-1", projects=[{"id": "project-123456"}],
            tags=[{"id": "tag-654321"}], live_entries=live_entries,
            review_batches=self.package, review_batch_sources=self.sources,
            review_batch_work_contexts=self.contexts, review_batch_group_summaries=self.summaries,
            review_batch_authority={"consolidation_policy": "approved-daily-project-presentation/v7",
                                    "wording_only_ids": []})

    @staticmethod
    def approval(plan):
        return native.approval_template(plan, approval_id="historical-board-fixture", approver="test fixture",
            approved_at="2026-10-04T10:00:00Z", expires_at="2026-10-05T10:00:00Z")

    def historical_plan(self):
        # This exact legacy document's digest was frozen against the pre-fix
        # implementation. Absence of a basis must retain its old child payloads.
        plan = self.plan()
        plan["review_batches_binding"].pop("posting_description_basis", None)
        for entry, caption in zip(plan["entries"], CHILD_CAPTIONS, strict=True):
            entry["payload"]["description"] = caption
            entry["payload_digest"] = native._digest(entry["payload"])
        plan["plan_digest"] = native._document_digest(plan, "plan_digest")
        return json.loads(json.dumps(plan))

    def execute_twice(self, plan, gateway=None):
        gateway = gateway or FakeGateway()
        approval = self.approval(plan)
        events, receipt_path = self.root / "events.jsonl", self.root / "receipt.json"
        receipt = native.execute_plan(plan, approval, events, receipt_path, gateway, now=NOW)
        original_bytes = receipt_path.read_bytes()
        replay = native.execute_plan(plan, approval, events, receipt_path, gateway, now=NOW)
        self.assertEqual(receipt, replay)
        self.assertEqual(original_bytes, receipt_path.read_bytes())
        return receipt, gateway

    def same_window(self, captions=CHILD_CAPTIONS):
        self.sources[1].update(start=self.sources[0]["start"], end=self.sources[0]["end"],
                               duration_seconds=71, duration_minutes=1)
        options = dict(editorial_descriptions=dict(zip(self.contexts, captions)),
                       work_contexts=self.contexts, natural_batches=True, all_durations=True,
                       allow_review_overlaps=True, allow_clockify_overlaps=True)
        first = batches.package_review_batches(self.sources, **options)["activities"][0]
        self.summaries = {first["review_id"]: {"child_review_ids": first["child_review_ids"], "description": SUMMARY}}
        self.package = batches.package_review_batches(self.sources, group_summaries=self.summaries, **options)
        self.group = self.package["activities"][0]
        self.parent = publisher.portfolio_row(self.group, "source-run")

    def test_equal_parent_projection_preserves_distinct_same_window_source_claims(self):
        self.same_window()
        plan = self.plan()
        self.assertEqual([SUMMARY, SUMMARY], [e["payload"]["description"] for e in plan["entries"]])
        receipt, gateway = self.execute_twice(plan)
        self.assertEqual(["created", "created"], [e["disposition"] for e in receipt["entries"]])
        self.assertEqual(["created-1", "created-2"], [e["clockify_entry_id"] for e in receipt["entries"]])
        self.assertEqual(142, receipt["review_groups"][0]["duration_seconds"])
        self.assertEqual(2, len(gateway.posts))

    def test_same_window_ambiguous_posts_recover_distinct_ids_with_parent_projection(self):
        self.same_window()
        receipt, gateway = self.execute_twice(self.plan(), FakeGateway(ambiguous=True))
        self.assertEqual(["recovered_after_ambiguous_response"] * 2,
                         [e["disposition"] for e in receipt["entries"]])
        self.assertEqual(["created-1", "created-2"], [e["clockify_entry_id"] for e in receipt["entries"]])
        self.assertEqual(142, receipt["review_groups"][0]["duration_seconds"])
        self.assertEqual(2, len(gateway.posts))

    def interrupt_after_first_confirmation(self, plan, gateway):
        append = native._append_event
        def interrupted(path, event):
            append(path, event)
            if event["event_type"] == "confirmed" and event["review_id"] == identity(1):
                raise KeyboardInterrupt("first source claim durably confirmed")
        with mock.patch.object(native, "_append_event", side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            native.execute_plan(plan, self.approval(plan), self.root / "events.jsonl",
                self.root / "receipt.json", gateway, now=NOW)
        self.assertEqual(1, len(gateway.posts))

    def test_resume_between_equal_display_claims_does_not_treat_confirmed_sibling_as_external(self):
        self.same_window()
        plan, gateway = self.plan(), FakeGateway()
        self.interrupt_after_first_confirmation(plan, gateway)
        receipt, gateway = self.execute_twice(plan, gateway)
        self.assertEqual(["created-1", "created-2"], [e["clockify_entry_id"] for e in receipt["entries"]])
        self.assertEqual(2, len(gateway.posts))

    def test_reserved_sibling_requires_its_exact_id_and_payload_before_any_new_intent(self):
        self.same_window()
        plan, gateway = self.plan(), FakeGateway()
        self.interrupt_after_first_confirmation(plan, gateway)
        journal = (self.root / "events.jsonl").read_bytes()
        for field, value in (("id", "foreign-native"), ("description", "Changed native work")):
            changed = copy.deepcopy(gateway.entries[0])
            changed[field] = value
            with self.subTest(field=field), mock.patch.object(gateway, "entry_by_id", return_value=changed):
                with self.assertRaisesRegex(native.NativePostError, "sibling entry failed exact direct GET"):
                    native.execute_plan(plan, self.approval(plan), self.root / "events.jsonl",
                        self.root / "receipt.json", gateway, now=NOW)
            self.assertEqual(journal, (self.root / "events.jsonl").read_bytes())
            self.assertEqual(1, len(gateway.posts))

    def test_ambiguous_resume_never_reuses_claimed_sibling_when_before_inventory_omits_it(self):
        self.same_window()
        plan, gateway = self.plan(), FakeGateway()
        self.interrupt_after_first_confirmation(plan, gateway)
        second = plan["entries"][1]
        native._append_event(self.root / "events.jsonl", {"event_type": "intent",
            "approval_digest": native._validate_approval(plan, self.approval(plan), NOW),
            "plan_digest": plan["plan_digest"], "review_id": second["review_id"],
            "payload_digest": second["payload_digest"], "before_entry_ids": [],
            "recorded_at": "2026-10-04T11:00:00Z"})
        with self.assertRaisesRegex(native.NativePostError, "unique exact GET recovery"):
            native.execute_plan(plan, self.approval(plan), self.root / "events.jsonl",
                self.root / "receipt.json", gateway, now=NOW)
        self.assertFalse(any(e["event_type"] == "confirmed" and e["review_id"] == identity(2)
                             for e in native._events(self.root / "events.jsonl")))
        self.assertEqual(1, len(gateway.posts))

    def test_unbound_external_identical_display_still_refuses_before_any_post(self):
        self.same_window()
        external = {"id": "foreign-native", "timeInterval": {"start": "2026-09-29T07:00:07Z", "end": "2026-09-29T07:01:18Z"},
            "projectId": "project-123456", "tagIds": ["tag-654321"], "taskId": None,
            "description": SUMMARY, "billable": True}
        plan, gateway = self.plan(live_entries=[external]), FakeGateway([external])
        with self.assertRaisesRegex(native.NativePostError, "verify source identity before posting"):
            native.execute_plan(plan, self.approval(plan), self.root / "events.jsonl",
                self.root / "receipt.json", gateway, now=NOW)
        self.assertEqual([], gateway.posts)
        self.assertFalse((self.root / "events.jsonl").exists())

    def test_indistinguishable_original_source_payloads_cannot_hide_behind_parent_projection(self):
        self.same_window(["SC — Same original source wording"] * 2)
        with self.assertRaisesRegex(native.NativePostError, "identical native payloads; verify source equivalence"):
            self.plan()

    def test_fresh_segments_use_verified_parent_summary_not_internal_child_counts(self):
        original_sources = copy.deepcopy(self.sources)
        plan = self.plan()
        self.assertEqual([SUMMARY, SUMMARY], [e["payload"]["description"] for e in plan["entries"]])
        self.assertEqual("reviewed_parent_summary/v1", plan["review_batches_binding"]["posting_description_basis"])
        self.assertEqual(CHILD_CAPTIONS, [d["description"] for d in plan["review_batches_binding"]["package"]["activities"][0]["prepared_descriptions"]])
        self.assertEqual(original_sources, plan["review_batches_binding"]["source_proposals"])
        self.assertEqual([identity(1), identity(2)], [e["review_id"] for e in plan["entries"]])
        self.assertEqual([71 / 60, 83 / 60], [e["duration_minutes"] for e in plan["entries"]])
        self.assertEqual(["2026-09-29T07:00:07Z", "2026-09-29T07:10:00Z"],
                         [e["payload"]["start"] for e in plan["entries"]])
        self.assertEqual(["2026-09-29T07:01:18Z", "2026-09-29T07:11:23Z"],
                         [e["payload"]["end"] for e in plan["entries"]])
        self.assertTrue(all({key: e["payload"][key] for key in ("projectId", "tagIds", "taskId", "billable")}
            == {"projectId": "project-123456", "tagIds": ["tag-654321"], "taskId": None, "billable": True}
            for e in plan["entries"]))

    def test_frozen_legacy_plan_approval_and_receipt_replay_remain_unchanged(self):
        legacy = self.historical_plan()
        self.assertEqual(LEGACY_PLAN_DIGEST, legacy["plan_digest"])
        native._validate_approval(legacy, self.approval(legacy), NOW)
        receipt, gateway = self.execute_twice(legacy)
        self.assertEqual(CHILD_CAPTIONS, [entry["description"] for entry in gateway.posts])
        self.assertEqual(["created", "created"], [e["disposition"] for e in receipt["entries"]])

    def test_parent_summary_does_not_alias_independently_supported_segments(self):
        plan = self.plan()
        self.assertEqual([SUMMARY, SUMMARY], [e["payload"]["description"] for e in plan["entries"]])
        self.assertEqual({identity(1), identity(2)}, native._independent_review_ids(plan))
        self.assertEqual({}, native._batch_alias_leaders(plan["entries"],
            independent_review_ids=native._independent_review_ids(plan)))
        receipt, gateway = self.execute_twice(plan)
        self.assertEqual(2, len(gateway.posts))
        self.assertEqual(["created", "created"], [e["disposition"] for e in receipt["entries"]])

    def test_unknown_basis_unbound_caption_and_parent_row_drift_refuse(self):
        plan = self.plan()
        for basis in ("unknown/v1", "", None):
            changed = copy.deepcopy(plan)
            changed["review_batches_binding"]["posting_description_basis"] = basis
            with self.subTest(basis=basis), self.assertRaises(native.NativePostError):
                native._validate_review_batch_plan(changed)
        changed = copy.deepcopy(plan)
        changed["entries"][0]["payload"]["description"] = CHILD_CAPTIONS[0]
        changed["entries"][0]["payload_digest"] = native._digest(changed["entries"][0]["payload"])
        with self.assertRaises(native.NativePostError):
            native._validate_review_batch_plan(changed)
        self.parent[8] = "SC — Unbound different parent wording"
        with self.assertRaises(native.NativePostError):
            self.plan()

    def test_previously_posted_source_keeps_exact_verified_credit_after_summary_change(self):
        historical = publisher.proposal_row(self.sources[0], "prior-run")
        historical[0], historical[8], historical[9], historical[13] = (
            "prior-source-row", "SC — Historical approved wording", "approved", "posted")
        live = {"id": "prior-native", "timeInterval": {"start": "2026-09-29T07:00:07Z", "end": "2026-09-29T07:01:18Z"},
            "projectId": "project-123456", "tagIds": ["tag-654321"], "taskId": None,
            "description": "SC — Historical approved wording", "billable": True}
        plan = self.plan(extra_rows=[historical], live_entries=[live])
        self.assertEqual(SUMMARY, plan["entries"][0]["payload"]["description"])
        self.assertEqual("prior-native", plan["entries"][0]["sheet_source_credit"]["clockify_entry_id"])
        receipt, gateway = self.execute_twice(plan, FakeGateway([live]))
        self.assertEqual(["existing_credit", "created"], [e["disposition"] for e in receipt["entries"]])
        self.assertEqual(1, len(gateway.posts))
        self.assertEqual(SUMMARY, gateway.posts[0]["description"])


if __name__ == "__main__":
    unittest.main()
