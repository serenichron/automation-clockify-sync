"""Fresh warning views retain sealed facts across collector/API representations."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from scripts import clockify_review_batch_publication as publication
from scripts import evidence_ledger


START = "2026-10-19T07:00:00+00:00"
END = "2026-10-19T08:00:00+00:00"


class FreshWarningIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name)
        (self.run / "evidence").mkdir()
        (self.run / "routing.json").write_text(json.dumps({"clockify_user_id": "member-1",
                                                           "workspace_id": "workspace-1"}))
        self.old = {"id_suffix": "12345678", "description": "Previously recorded client work",
            "project_id_suffix": "abcdef", "start": START, "end": END,
            "billable": True, "tag_id_suffixes": ["1234abcd"], "running": False}
        self.live = {"id": "entry-12345678", "description": self.old["description"],
            "projectId": "project-abcdef", "userId": "member-1", "workspaceId": "workspace-1",
            "billable": True, "tagIds": ["tag-1234abcd"],
            "timeInterval": {"start": "2026-10-19T07:00:00Z", "end": "2026-10-19T08:00:00Z"}}
        self.proposal = {"start": START, "end": END, "review_warnings": []}
        self.seal([self.old])

    def seal(self, entries):
        events = evidence_ledger.normalize_collector_snapshot({"clockify": {"entries": entries}})
        ledger = evidence_ledger.EvidenceLedger(tuple(events))
        document = {"schema_version": evidence_ledger.SCHEMA_VERSION,
                    "manifest": ledger.manifest.document(), "events": [e.document() for e in ledger.events]}
        self.path = self.run / "evidence/evidence-ledger.json"
        self.path.write_text(json.dumps(document))
        self.old_warning = {"type": "existing_clockify_overlap", "counterpart_id": events[0].evidence_id,
            "counterpart_project_suffix": "abcdef", "overlap_start": START, "overlap_end": END,
            "overlap_duration_seconds": 3600}
        self.proposal["review_warnings"] = [copy.deepcopy(self.old_warning)]

    def view(self, entries=None, **kwargs):
        return publication.fresh_warning_view([self.proposal],
            {"read_only": True, "entries": entries if entries is not None else [self.live]},
            **(kwargs or {"source_run": self.run}))

    def test_source_shaped_identity_utc_and_project_suffix_add_no_false_warning(self):
        before = copy.deepcopy(self.proposal)
        self.assertEqual([before], self.view())
        self.assertEqual(before, self.proposal)
        self.assertEqual("abcdef", self.view()[0]["review_warnings"][0]["counterpart_project_suffix"])

    def test_explicit_validated_ledger_anchors_native_identity_without_source_run(self):
        self.assertEqual([self.proposal], self.view(source_ledger=self.path))

    def test_actual_native_id_can_anchor_representation_changes(self):
        self.seal([{**self.old, "id": self.live["id"]}])
        self.assertEqual([self.proposal], self.view())

    def test_new_native_duplicate_with_identical_financial_facts_is_not_folded(self):
        duplicate = {**self.live, "id": "new-entry-87654321"}
        warnings = self.view([self.live, duplicate])[0]["review_warnings"]
        self.assertEqual(2, len(warnings))
        self.assertEqual(self.old_warning, warnings[0])
        self.assertNotEqual(warnings[0]["counterpart_id"], warnings[1]["counterpart_id"])

    def test_new_project_fact_carries_its_own_suffix_without_changing_original(self):
        warnings = self.view([{**self.live, "projectId": "project-fedcba"}])[0]["review_warnings"]
        self.assertEqual(self.old_warning, warnings[0])
        self.assertEqual("fedcba", warnings[1].get("counterpart_project_suffix"))

    def test_explicit_ledger_retained_workspace_fact_cannot_be_ignored(self):
        self.seal([{**self.old, "workspaceId": "workspace-1"}])
        warnings = self.view([{**self.live, "workspaceId": "other-workspace"}],
                             source_ledger=self.path)[0]["review_warnings"]
        self.assertEqual(2, len(warnings))
        self.assertEqual(self.old_warning, warnings[0])

    def test_changed_native_identity_project_or_full_span_remains_new_even_for_same_overlap(self):
        changes = [{"id": "new-entry-87654321"}, {"projectId": "project-fedcba"},
                   {"timeInterval": {"start": "2026-10-19T06:00:00Z", "end": "2026-10-19T08:00:00Z"}},
                   {"description": "Different financial work"}, {"billable": False}]
        for change in changes:
            with self.subTest(fields=list(change)):
                warnings = self.view([{**self.live, **change}])[0]["review_warnings"]
                self.assertEqual(2, len(warnings))
                self.assertEqual(self.old_warning, warnings[0])

    def test_fact_fallback_requires_complete_unique_member_project_description_and_span(self):
        fallback = {k: v for k, v in self.old.items() if k != "id_suffix"}
        fallback["userId"] = "member-1"
        self.seal([fallback])
        self.assertEqual([self.proposal], self.view())
        duplicate = {**self.live, "id": "new-entry-87654321"}
        self.assertEqual(3, len(self.view([self.live, duplicate])[0]["review_warnings"]))
        fallback.pop("userId")
        self.seal([fallback])
        # A source-run target supplies the missing collector member; a bare
        # ledger with no native ID or member cannot match on geometry alone.
        self.assertEqual(2, len(self.view(source_ledger=self.path)[0]["review_warnings"]))

    def test_bad_ledger_or_wrong_capture_target_fails_closed(self):
        for change in ({"userId": "other-member"}, {"workspaceId": "other-workspace"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.view([{**self.live, **change}])
        document = json.loads(self.path.read_text())
        document["events"][0]["attributes"]["description"] = "Tampered source"
        self.path.write_text(json.dumps(document))
        with self.assertRaises(ValueError):
            self.view()

    def test_old_callers_keep_original_fresh_warning_identity_behavior(self):
        view = publication.fresh_warning_view([self.proposal], {"read_only": True, "entries": [self.live]})
        self.assertEqual(2, len(view[0]["review_warnings"]))
        self.assertEqual(self.old_warning, view[0]["review_warnings"][0])


if __name__ == "__main__":
    unittest.main()
