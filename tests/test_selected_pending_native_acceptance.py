"""Portable receipt-compatibility checks using the real native pending verifier.

No pretend Sep25 receipt or provider mock: the fixture emits the documented
pending acceptance schema through pending.verify. A frozen receipt exercises
only its older runtime/additive-field representation, not historical delivery.
The private original Oct6 graph separately covers complete source/replay and
incremental1+retained32/current native/editorial delivery.
"""
import copy
import json
from pathlib import Path
import unittest

from scripts import clockify_pending_review_selection as pending
from scripts import clockify_selected_delivery_adoption as selected
from scripts import clockify_source_adoptions as artifacts
import test_pending_review_selection as fixtures


class NativePendingAcceptanceTests(unittest.TestCase):
    def setUp(self):
        fixture = fixtures.PendingSelectionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root = fixture.root
        verified = pending.verify(bindings_path=fixture.binding_path,source_dir=fixture.current_dir,
            proposals=fixture.current,spreadsheet_id='sheet',sheet_title='August 2026 review',
            run_id=fixture.current_dir.name,project_allowlist={})
        self.assertEqual(34,len(verified['rows']))
        self.assertEqual(34,verified['receipt']['saved_credit_minutes'])
        self.expected = verified['receipt']
        self.actual = copy.deepcopy(self.expected)
        for key in ('fixed_recording_checks','fixed_recording_rows','saved_credit_seconds'):
            self.actual.pop(key)
        for role in ('consumer','pipeline','allocator'):
            path = self.root/(role+'-recorded.py')
            # Different, byte-bound recorded context is not a credit override.
            path.write_text('# recorded '+role+' runtime fixture\n')
            self.actual['runtime_artifacts'][role] = pending.artifact_handle(path)
        self.reseal()

    def reseal(self):
        self.actual['acceptance_sha256'] = pending.digest({
            k:v for k,v in self.actual.items() if k != 'acceptance_sha256'})

    def verify(self):
        return selected._pending_acceptance(self.actual,self.expected,
            lambda handle:artifacts._capture(handle,{}))

    def test_known_additive_checks_do_not_replace_recorded_authority(self):
        """Removing runtime portability rejects unchanged native pending credit."""
        before_actual,before_expected = copy.deepcopy(self.actual),copy.deepcopy(self.expected)
        self.assertEqual({'fixed_recording_checks':[],'fixed_recording_rows':0,'saved_credit_seconds':2040},
                         self.verify())
        self.assertEqual(before_actual,self.actual)
        self.assertEqual(before_expected,self.expected)

    def test_resealed_credit_inflation_still_fails_native_comparison(self):
        """Checking only historic digest would accept an invented extra minute."""
        self.actual['saved_credit_minutes'] = 35
        self.reseal()
        with self.assertRaisesRegex(ValueError,'semantic proof'):
            self.verify()

    def test_recorded_runtime_bytes_must_still_match_original_handle(self):
        """Ignoring recorded runtime handles silently loses original-byte proof."""
        path = Path(self.actual['runtime_artifacts']['consumer']['path'])
        path.write_text('# drifted runtime bytes\n')
        with self.assertRaises(ValueError):
            self.verify()

    def test_unknown_new_validation_fields_are_not_silently_ignored(self):
        """Broad actual-subset comparison would hide a new authority field."""
        self.expected = {**self.expected,'unreviewed_authority_extension':True}
        with self.assertRaisesRegex(ValueError,'unrecognized'):
            self.verify()

    def test_historic_native_validation_field_cannot_be_omitted(self):
        """A resealed subset must not erase original allocation verification."""
        self.actual.pop('native_credit_checks')
        self.reseal()
        with self.assertRaisesRegex(ValueError,'unrecognized'):
            self.verify()

    def test_original_receipt_self_digest_cannot_be_ignored(self):
        """Comparing only semantic fields would accept an unsealed original."""
        self.actual['acceptance_sha256'] = 'sha256:'+'0'*64
        with self.assertRaisesRegex(ValueError,'digest'):
            self.verify()


if __name__ == '__main__':
    unittest.main()
