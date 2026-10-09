"""Fixed native pending reader must reconstruct sealed bytes and reject drift."""
import copy
import json
import os
from pathlib import Path
import unittest
from scripts import clockify_pending_review_selection as pending

CONFIG = Path(os.environ['CLOCKIFY_RETAINED_COMPATIBILITY_CONFIG']) if os.environ.get('CLOCKIFY_RETAINED_COMPATIBILITY_CONFIG') else None

@unittest.skipUnless(CONFIG is not None, 'private retained compatibility graph not selected')
class RetainedPendingReaderContractTests(unittest.TestCase):
    def setUp(self):
        try:
            from scripts import clockify_historical_runtime as historical
        except ImportError:
            self.fail('fixed authenticated native pending reader is unavailable')
        self.historical = historical
        state = json.loads((Path(json.loads(CONFIG.read_bytes())['state_dir']) / 'review-cycle-state.json').read_bytes())
        record = state['slices']['2026-10-05']
        receipt = json.loads(Path(record['delivery_receipt']).read_bytes())
        self.acceptance = next(item['pending_selection'] for item in receipt['publication_receipts'] if 'pending_selection' in item)

    def test_exact_native_pending_rows_reconstruct_the_sealed_acceptance(self):
        result = self.historical.pending_selection(self.acceptance)
        self.assertEqual(self.acceptance, result['receipt'])
        self.assertEqual(self.acceptance['projected_rows_sha256'], pending.digest(result['rows']))

    def test_changed_native_role_cannot_select_an_executor(self):
        changed = copy.deepcopy(self.acceptance)
        changed['runtime_artifacts']['pipeline']['sha256'] = 'sha256:' + '0' * 64
        with self.assertRaisesRegex(ValueError, 'runtime'):
            self.historical.pending_selection(changed)

    def test_changed_native_selection_cannot_claim_original_rows(self):
        changed = copy.deepcopy(self.acceptance)
        changed['selection']['sha256'] = 'sha256:' + '0' * 64
        with self.assertRaisesRegex(ValueError, 'artifact|input'):
            self.historical.pending_selection(changed)
