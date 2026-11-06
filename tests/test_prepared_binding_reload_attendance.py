"""A prepared binding must reload under the same attendance policy that produced it.

Regression (2026-11-06, slice 2026-11-05..06): preparation derived with recorded
attendance enabled, but recorded `recorded_attendance_authority` only when some
attendance row was adopted. When the attendance candidate was withheld instead,
`load()` re-derived without attendance, got different diagnostics, and the cycle
failed on every run ("row contract", then "preparation drifted").
"""
from pathlib import Path
import json
import tempfile
import unittest
from unittest import mock

import test_recorded_attendance_repair_integration as attendance
from scripts import clockify_review_batch_publication as prepared
from scripts import clockify_sheet_publish as publisher

REAL_ATTENDANCE = prepared._recorded_attendance_descriptions
CANDIDATES = []


def _withheld_attendance(*args, **kwargs):
    # Same canonical attendance facts, but wording the safety contract rejects:
    # the attendance candidate is derived and then withheld with a diagnostic.
    descriptions, handles = REAL_ATTENDANCE(*args, **kwargs)
    CANDIDATES.append(len(descriptions))
    return {identity: "SC — I attended the meeting" for identity in descriptions}, handles


class PreparedBindingReloadAttendanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source, _corrections, self.proposals = attendance.RecordedAttendanceRepairTests().source(self.root)

    def prepare(self, output):
        return prepared.prepare(source_dir=self.source, output=output, run_id=self.source.name,
            spreadsheet_id="sheet", sheet_title="August review", timezone="Europe/Bucharest", all_durations=True,
            summary_provider=lambda *_: self.fail("no semantic wording is needed"))

    def test_incomplete_binding_with_withheld_attendance_reloads_and_replays(self):
        output = self.root / "prepared.json"
        CANDIDATES.clear()
        with mock.patch.object(prepared, "_recorded_attendance_descriptions", side_effect=_withheld_attendance):
            result = self.prepare(output)
            self.assertTrue(CANDIDATES and max(CANDIDATES) > 0, "fixture must yield an attendance candidate")
            self.assertNotIn("recorded_attendance_authority", json.loads(output.read_text()))
            self.assertEqual("incomplete", result["status"])
            withheld = {publisher.stable_review_id(p) for p in self.proposals}
            self.assertTrue(withheld & set(result["unprepared_review_ids"]))

            document, _ = prepared.load(output)  # must not raise "derived maps differ"
            frozen = output.read_bytes()
            self.prepare(output)  # replaying the immutable binding is a no-op, not drift
            self.assertEqual(frozen, output.read_bytes())
        self.assertEqual(result["unprepared_review_ids"], document["preparation_outcome"]["unprepared_review_ids"])

    def test_key_absent_binding_with_altered_diagnostics_is_still_rejected(self):
        output = self.root / "prepared.json"
        with mock.patch.object(prepared, "_recorded_attendance_descriptions", side_effect=_withheld_attendance):
            self.prepare(output)
            forged = json.loads(output.read_text())
            forged["preparation_diagnostics"][0]["reasons"] = ["forged_reason"]
            output.write_text(json.dumps(forged))
            with self.assertRaisesRegex(ValueError, "derived maps differ"):
                prepared.load(output)


if __name__ == "__main__":
    unittest.main()
