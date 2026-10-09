"""A replacement source must not inherit a superseded publication graph."""
import copy
import datetime as dt
import json
from pathlib import Path
import unittest
from unittest import mock

import test_review_cycle_delivery as fixtures
from scripts import clockify_review_cycle as cycle, source_coverage


class RecoveryPromotionHistoryTests(unittest.TestCase):
    def setUp(self):
        fixtures.ReviewCycleDeliveryTests.setUp(self)

    def publisher_child_result(self, command, *, code=0):
        return fixtures.publisher_result_for_command(self.config, command, code=code)

    def test_promotion_retires_authenticated_prior_graph_and_repeat_preserves_new_graph(self):
        child = fixtures.ReviewCycleDeliveryTests.child_for_runs(self, [])
        with mock.patch.object(cycle, 'run_child_bounded', side_effect=child):
            cycle.run_cycle(self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10))
        state_path = self.state_dir / 'review-cycle-state.json'
        state = json.loads(state_path.read_bytes())
        record = state['slices']['2026-09-07']
        prior = copy.deepcopy(record)
        parent = record['source']
        interval = cycle._interval_from_stage(self.config, 'peer/desktop', parent)
        store = source_coverage.SourceDebtStore()
        debt = store.record_failure(interval, failure_class='peer_unavailable', retryable=True,
            resume_state_digest='sha256:' + 'a' * 64, attempted_at='2026-09-09T00:00:00Z')
        attempt, _command = cycle._recovery_attempt(record, debt, parent, self.config)
        with mock.patch.object(fixtures.collector_slices.BacklogStore, 'record_complete', return_value=None):
            recovered = fixtures.make_run(self.root, 'recovered-source', replay=False,
                                         snapshots_from=Path(parent['run_dir']))
        stage = cycle._validate_stage(self.config, recovered, '2026-09-07', '2026-09-09',
            replay=False, expected_snapshot_digests=parent['snapshot_digests'])
        stage.update(recovery_receipt_path=str(self.root / 'recovery-receipt.json'),
                     recovery_receipt_digest='sha256:' + 'b' * 64)
        attempt = {**attempt, 'phase': 'verified_complete', 'result_path': stage['result_path'],
            'result_digest': stage['result_digest'], 'returned_bundle_digest': stage['bundle_digest'],
            'requested_source_outcome': 'complete', 'recovery_receipt_path': stage['recovery_receipt_path'],
            'recovery_receipt_digest': stage['recovery_receipt_digest']}
        record['recovery_attempts'][debt.debt_id] = attempt
        # Only the external recovery transport/receipt boundary is substituted;
        # prior source, replay, integrity and publication authentication are real.
        with mock.patch.object(cycle, '_validate_recovery_stage', return_value=(stage, 'complete')):
            cycle._apply_verified_recovery(self.config, state, state_path, record, store,
                self.state_dir / 'source-coverage.json', '2026-09-07', '2026-09-09',
                debt, parent, attempt)
            self.assertNotIn('replay', record)
            self.assertNotIn('delivery_receipt', record)
            self.assertEqual(1, len(record['source_recovery_history']))
            history = record['source_recovery_history'][0]
            self.assertEqual(prior['source'], history['source'])
            self.assertEqual(prior['replay'], history['replay'])
            self.assertEqual(prior['delivery_receipt'], history['delivery_receipt'])
            record['replay'] = {'newer': 'verified elsewhere'}
            record['publication_receipt'] = 'newer-publication'
            record['status'] = 'delivered_with_exceptions'
            repeated_before = copy.deepcopy(record)
            cycle._apply_verified_recovery(self.config, state, state_path, record, store,
                self.state_dir / 'source-coverage.json', '2026-09-07', '2026-09-09',
                debt, parent, attempt)
            self.assertEqual(repeated_before, record)


if __name__ == '__main__':
    unittest.main()
