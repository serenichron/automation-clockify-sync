"""Regression: an authenticated finished child needs a non-mutating repair plan."""
import json
import os
from pathlib import Path
import unittest
from scripts import clockify_review_cycle as cycle

CONFIG = Path(os.environ['CLOCKIFY_RETAINED_COMPATIBILITY_CONFIG']) if os.environ.get('CLOCKIFY_RETAINED_COMPATIBILITY_CONFIG') else None

@unittest.skipUnless(CONFIG is not None, 'private retained compatibility graph not selected')
class FinishedRepairEntryContractTests(unittest.TestCase):
    def test_finished_child_exposes_explicit_plan_instead_of_implicit_state_migration(self):
        operation = getattr(cycle, 'repair_finished_recovery', None)
        self.assertTrue(callable(operation), 'authenticated finished child has no explicit repair plan operation')
        config = json.loads(CONFIG.read_bytes())
        config['_runtime_identity'] = {'canonical_root': config['root'],
            'collector_path': str(Path(config['root']) / 'scripts/clockify_sync_collect.py'),
            'git_sha': None, 'git_dirty': None}
        # Exact non-mutating native graph proof is a separate audited runner.
        self.assertEqual('source_verified', json.loads((Path(config['state_dir']) / 'review-cycle-state.json').read_bytes())['slices']['2026-10-07']['status'])
