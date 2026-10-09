"""Saved native shared-pool debt is not new recoverable publication credit."""
import copy
import unittest

from scripts import clockify_pending_review_selection as consumer, evidence_ledger
import test_pending_review_selection as placement_tests


class NativeResidualTests(unittest.TestCase):
    def fixture(self):
        records, sources = placement_tests.SavedNativePlacementTests().fixture(estimated=True)
        demand = sources['native']['accounting']['allocation']['evidence'][0]
        demand['effort'] = {'min': 3, 'recommended': 5, 'max': 5}
        return records, sources

    def add_posted(self, records, sources, *, same_work):
        proposal = records[0]['proposal']
        posted = evidence_ledger.evidence_event('clockify', {'source_id': 'actual-posted-context'},
            observed_at=proposal['start'], raw_source_span={'start': proposal['start'], 'end': proposal['end']},
            attributes={'project_id_suffix': 'abc123',
                'description': proposal['description'] if same_work else 'Independent posted accomplishment'}).document()
        sources['native']['ledger']['events'].append(posted)
        return posted

    def test_valid_estimated_pool_debt_preserves_original_saved_minutes(self):
        records, sources = self.fixture()
        before = copy.deepcopy((records, sources))
        try:
            normalized, proof = consumer._credits(records, sources)
        except ValueError as exc:
            self.fail(f'producer-forbidden estimated pool recovery wrongly demanded new credit: {exc}')
        self.assertEqual(4, proof['saved_credit_minutes'])
        self.assertEqual(2, proof['native_residual_minutes'])
        self.assertEqual(0, proof['remaining_recoverable_minutes'])
        self.assertEqual({'activity_id': 'first', 'credited_minutes': 3, 'native_requested_minutes': 5,
            'native_residual_minutes': 2, 'recoverable_minutes': 0}, proof['native_credit_checks'][0])
        self.assertEqual([3, 1], [p['duration_minutes'] for p in normalized])
        self.assertEqual(before, (records, sources))

    def test_ordinary_nonshared_recoverable_demand_is_still_rejected(self):
        records, sources = placement_tests.SavedNativePlacementTests().fixture()
        demand = sources['native']['accounting']['allocation']['evidence'][0]
        demand['effort'] = {'min': 3, 'recommended': 5, 'max': 5}
        demand['allowed_intervals'][0][1] = '2026-08-01T09:33:00+00:00'
        with self.assertRaisesRegex(ValueError, 'recoverable whole-minute'):
            consumer._credits(records, sources)

    def test_pool_residual_does_not_waive_original_placement_proof(self):
        for drift in ('human-id', 'interval', 'evidence-span', 'mixed-marker'):
            with self.subTest(drift=drift):
                records, sources = self.fixture()
                provenance = records[0]['proposal']['provenance']
                demand = sources['native']['accounting']['allocation']['evidence'][0]
                if drift == 'human-id':
                    provenance['timing_context_evidence_ids'].append('uncaptured-human')
                elif drift == 'interval':
                    provenance['timing_context_intervals'][0]['end'] = '2026-08-01T09:29:00+00:00'
                elif drift == 'evidence-span':
                    demand['evidence_spans'] = [{'evidence_id': provenance['evidence_ids'][0],
                        'start': records[0]['proposal']['start'], 'end': records[0]['proposal']['end']}]
                else:
                    second = copy.deepcopy(records[0])
                    second['proposal']['provenance'].pop('timing_placement')
                    second['proposal'].update(start='2026-08-01T09:10:00+00:00', end='2026-08-01T09:11:00+00:00',
                        duration_minutes=1, duration_seconds=60, candidate_key='separate-part')
                    records.append(second)
                with self.assertRaisesRegex(ValueError, 'saved native placement'):
                    consumer._credits(records, sources)

    def test_residual_does_not_waive_shared_pool_capacity_guard(self):
        records, sources = self.fixture()
        for record, start in zip(records, (0, 12), strict=True):
            record['proposal'].update(start=f'2026-08-01T09:{start:02d}:00+00:00',
                end=f'2026-08-01T09:{start+16:02d}:00+00:00', duration_minutes=16, duration_seconds=960)
        for demand in sources['native']['accounting']['allocation']['evidence']:
            demand['effort'] = {'min': 16, 'recommended': 20, 'max': 20}
        with self.assertRaisesRegex(ValueError, 'shared human-pool debit'):
            consumer._credits(records, sources)

    def test_time_only_posted_overlap_remains_a_warning_not_suppression(self):
        records, sources = self.fixture()
        posted = self.add_posted(records, sources, same_work=False)
        try:
            normalized, proof = consumer._credits(records, sources)
        except ValueError as exc:
            self.fail(f'time-only posted overlap blocked genuine saved pool credit: {exc}')
        self.assertEqual(4, proof['saved_credit_minutes'])
        self.assertEqual(2, len(normalized))
        warnings = [w for p in normalized for w in p.get('review_warnings', [])
            if w.get('type') == 'existing_clockify_overlap']
        self.assertTrue(warnings)
        self.assertTrue(any(posted['evidence_id'] in str(w) for w in warnings))

    def test_same_work_posted_overlap_still_rejects_unchanged_saved_credit(self):
        records, sources = self.fixture()
        self.add_posted(records, sources, same_work=True)
        with self.assertRaisesRegex(ValueError, 'native normalization changed saved credits'):
            consumer._credits(records, sources)


if __name__ == '__main__':
    unittest.main()
