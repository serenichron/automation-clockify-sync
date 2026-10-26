"""Native source validation reuses only a call-local fully checked ledger."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import clockify_native_credit_history as history
from scripts import clockify_source_adoptions as adoptions, evidence_ledger


class NativeCreditSourceValidationReuseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.events = [evidence_ledger.evidence_event("multica", {"id": str(index)})
                       for index in range(4)]
        ledger = evidence_ledger.EvidenceLedger(tuple(self.events))
        self.ledger = {"schema_version": evidence_ledger.SCHEMA_VERSION,
                       "manifest": ledger.manifest.document(),
                       "events": [event.document() for event in self.events]}
        self.proposals = [{"review_activity_key": f"wka-{index}", "allocation_segment": 1,
                           "start": "2026-10-22T08:00:00Z", "end": "2026-10-22T08:01:00Z",
                           "duration_seconds": 60,
                           "provenance": {"evidence_ids": [self.events[index].evidence_id]}}
                          for index in range(3)]
        self.binding = {"output_directory": str(self.root / "output")}
        self.seal()

    def seal(self):
        for field, document in (("source_proposals", self.proposals), ("source_ledger", self.ledger)):
            content = json.dumps(document).encode()
            path = self.root / f"{field}.json"
            path.write_bytes(content)
            self.binding[field] = {"path": str(path), "sha256": "sha256:" + hashlib.sha256(content).hexdigest()}

    def plan(self):
        return {"native_credit_source": self.binding, "entries": [
            {"review_id": f"wka-{index}-s01", "payload": {
                "start": "2026-10-22T08:00:00Z", "end": "2026-10-22T08:01:00Z"}}
            for index in range(3)]}

    def validation_spy(self):
        return mock.patch.object(evidence_ledger.EvidenceLedger, "validate", autospec=True,
                                 side_effect=evidence_ledger.EvidenceLedger.validate)

    def test_source_validates_whole_ledger_once_and_checks_every_selected_target(self):
        with self.validation_spy() as validation, mock.patch.object(
                adoptions, "_validate_source_target", wraps=adoptions._validate_source_target) as targets:
            originals, ledger = history.validate_source(self.binding, proposals=self.proposals)
        self.assertEqual(1, validation.call_count)
        self.assertEqual(["wka-0-s01", "wka-1-s01", "wka-2-s01"],
                         [call.args[2] for call in targets.call_args_list])
        for index, call in enumerate(targets.call_args_list):
            self.assertIs(originals[index], call.args[0])
        self.assertEqual(self.proposals, originals)
        self.assertEqual(self.ledger, ledger)

    def test_plan_reuses_validation_and_preserves_original_payload_checks(self):
        plan = self.plan()
        with self.validation_spy() as validation:
            originals, ledger = history.validate_plan_source(plan)
        self.assertEqual(1, validation.call_count)
        self.assertEqual(self.proposals, originals)
        self.assertEqual(self.ledger, ledger)
        plan["entries"][-1]["payload"]["end"] = "2026-10-22T08:02:00Z"
        with self.assertRaisesRegex(ValueError, "original interval"):
            history.validate_plan_source(plan)

    def test_tampered_unrelated_event_rejects_even_when_handle_is_resealed(self):
        self.ledger["events"][-1]["attributes"]["title"] = "changed unrelated evidence"
        self.seal()
        with self.assertRaisesRegex(ValueError, "Evidence ID"):
            history.validate_source(self.binding, review_ids=["wka-0-s01"])

    def test_changed_unrelated_event_with_valid_id_still_requires_matching_manifest(self):
        self.ledger["events"][-1] = evidence_ledger.evidence_event(
            "multica", {"id": "replacement-unrelated"}).document()
        self.seal()
        with self.assertRaisesRegex(ValueError, "manifest validation"):
            history.validate_source(self.binding, review_ids=["wka-0-s01"])

    def test_each_call_revalidates_resealed_changed_source(self):
        with self.validation_spy() as validation:
            history.validate_source(self.binding)
            self.events[-1] = evidence_ledger.evidence_event("multica", {"id": "new-unrelated"})
            ledger = evidence_ledger.EvidenceLedger(tuple(self.events))
            self.ledger["events"] = [event.document() for event in self.events]
            self.ledger["manifest"] = ledger.manifest.document()
            self.seal()
            _, actual = history.validate_source(self.binding)
        self.assertEqual(2, validation.call_count)
        self.assertEqual("new-unrelated", actual["events"][-1]["source_ref"]["id"])

    def test_empty_selection_still_does_not_validate_ledger(self):
        self.ledger = {"not": "a ledger"}
        self.seal()
        with self.validation_spy() as validation:
            originals, ledger = history.validate_source(self.binding, review_ids=[])
        self.assertEqual(0, validation.call_count)
        self.assertEqual(self.proposals, originals)
        self.assertEqual({"not": "a ledger"}, ledger)

    def test_missing_or_duplicate_selected_evidence_rejects_later_target(self):
        original = copy.deepcopy(self.proposals)
        for ids in (["ev:sha256:" + "0" * 64],
                    [self.events[1].evidence_id, self.events[1].evidence_id], []):
            with self.subTest(ids=ids):
                self.proposals = copy.deepcopy(original)
                self.proposals[1]["provenance"]["evidence_ids"] = ids
                self.seal()
                with self.assertRaises(ValueError):
                    history.validate_source(self.binding)

    def test_duplicate_raw_selected_events_still_reject_exact_membership(self):
        self.ledger["events"].append(copy.deepcopy(self.ledger["events"][1]))
        self.seal()
        with self.assertRaisesRegex(ValueError, "membership"):
            history.validate_source(self.binding)

    def test_native_identity_coercions_are_not_accepted_for_later_target(self):
        for segment in ("1", True):
            with self.subTest(segment=segment):
                self.proposals[1]["allocation_segment"] = segment
                self.seal()
                with self.assertRaisesRegex(ValueError, "review identity"):
                    history.validate_source(self.binding)

    def test_duplicate_or_absent_selected_review_ids_reject(self):
        for identities in (["wka-0-s01", "wka-0-s01"], ["wka-missing-s01"]):
            with self.subTest(identities=identities), self.assertRaisesRegex(ValueError, "membership"):
                history.validate_source(self.binding, review_ids=identities)


if __name__ == "__main__":
    unittest.main()
