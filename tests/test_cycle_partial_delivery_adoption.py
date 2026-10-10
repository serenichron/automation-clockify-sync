"""Offline selected recovery of a genuinely sealed partial publication."""
import copy
import json
from pathlib import Path
import unittest
from unittest import mock

from scripts import clockify_review_cycle as cycle
import test_cycle_selected_delivery_adoption as selected_fixtures
from test_review_cycle_delivery import make_run, write_json


class PartialDeliveryAdoptionTests(unittest.TestCase):
    def setUp(self):
        fixture = selected_fixtures.SelectedHistoricalDeliveryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        frozen = fixture.request['frozen_snapshot_digests']
        coverage = {'status': 'incomplete', 'incomplete_sources': ['sessions/macbook'],
            'sources': {'clockify': {'status': 'complete', 'observed_count': 0},
                        'sessions/macbook': {'status': 'unavailable', 'observed_count': 0}}}
        result = make_run(fixture.root, 'original-partial-source', replay=False,
            coverage=coverage, record_checkpoint=False)
        source = cycle._validate_stage(fixture.config, result, fixture.since, fixture.until,
            replay=False, expected_snapshot_digests=frozen)
        replay_result = make_run(fixture.root, 'original-partial-replay', replay=True,
            source_name=source['run_id'], snapshots_from=result.parent, coverage=coverage)
        replay = cycle._validate_stage(fixture.config, replay_result, fixture.since, fixture.until,
            replay=True, expected_snapshot_digests=frozen, source_run_id=source['run_id'],
            source_run_dir=source['run_dir'])
        self.receipt_path = fixture.state_dir/'partial-publication-receipts'/'original.json'
        document = cycle._delivery_document(fixture.config, fixture.since, fixture.until,
            source, replay, sheet_title=fixture.title)
        write_json(self.receipt_path, document)
        self.state_path = fixture.state_dir/'review-cycle-state.json'
        state = json.loads(self.state_path.read_text())
        prior = {key: state['slices'][fixture.since][key] for key in (
            'until', 'period_manifest', 'expected_snapshot_digests')}
        prior.update(status='published_with_source_gaps', source=source, replay=replay,
            publication_receipt=str(self.receipt_path), source_completeness=coverage,
            source_run_id=source['run_id'], replay_run_id=replay['run_id'],
            review_ids=source['review_ids'], exception_ids=source['exception_ids'],
            exceptions_complete=True)
        state['slices'][fixture.since] = prior
        write_json(self.state_path, state)
        fixture.prior = copy.deepcopy(prior)
        fixture.request['prior_record_digest'] = cycle._value_digest(prior)
        self.original_receipt = self.receipt_path.read_bytes()
        cycle._validate_delivered_state(fixture.config, state)

    def adopt(self):
        try:
            return self.fixture.adopt()
        except cycle.CycleError as error:
            self.fail(f'Authentic partial publication must remain recoverable: {error}')

    def test_authentic_partial_graph_is_preserved_and_repeat_is_read_only(self):
        """Blanket replay rejection or loss of original publication breaks recovery."""
        fixture = self.fixture
        first = self.adopt()
        self.assertEqual('delivered_with_exceptions', first['status'])
        state = json.loads(self.state_path.read_text())
        receipt = json.loads(Path(state['slices'][fixture.since]['historical_adoption_receipt']).read_text())
        self.assertEqual(fixture.prior, receipt['superseded_record'])
        self.assertNotIn('recovery_attempts', receipt['superseded_record'])
        before = {path: path.read_bytes() for path in (
            self.state_path, fixture.state_dir/'source-coverage.json', self.receipt_path)}
        self.assertEqual(first, self.adopt())
        cycle._validate_delivered_state(fixture.config, state)
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertEqual(self.original_receipt, self.receipt_path.read_bytes())

    def test_partial_graph_drift_before_adoption_is_rejected_without_writes(self):
        """Accepting a CAS alone would launder missing original receipt proof."""
        self.receipt_path.write_bytes(b'{}')
        before = self.state_path.read_bytes()
        with self.assertRaises(cycle.CycleError):
            self.fixture.adopt()
        self.assertEqual(before, self.state_path.read_bytes())
        self.assertFalse((self.fixture.state_dir/'delivery-receipts'/f'{self.fixture.since}.json').exists())

    def test_preserved_partial_graph_is_revalidated_after_adoption(self):
        """Removing predecessor verification would allow old source bytes to drift."""
        self.adopt()
        Path(self.fixture.prior['source']['run_dir'], 'proposals.json').write_bytes(b'[]')
        state = json.loads(self.state_path.read_text())
        with self.assertRaises(cycle.CycleError):
            cycle._validate_delivered_state(self.fixture.config, state)
        with self.assertRaises(cycle.CycleError):
            self.fixture.adopt()

    def test_final_state_write_interruption_resumes_without_replacing_partial_receipt(self):
        """Receipt-before-state recovery must reuse immutable prior publication bytes."""
        original = cycle._atomic
        def interrupted(path, document):
            if path == self.state_path:
                raise OSError('simulated final state interruption')
            return original(path, document)
        before = self.state_path.read_bytes()
        with mock.patch.object(cycle, '_atomic', side_effect=interrupted):
            with self.assertRaisesRegex(OSError, 'simulated final state'):
                self.adopt()
        self.assertEqual(before, self.state_path.read_bytes())
        receipts = {path: path.read_bytes() for path in (
            self.fixture.state_dir/'delivery-receipts'/f'{self.fixture.since}.json',
            self.fixture.state_dir/'historical-adoption-receipts'/f'{self.fixture.since}.json')}
        self.assertEqual('delivered_with_exceptions', self.adopt()['status'])
        self.assertEqual(receipts, {path: path.read_bytes() for path in receipts})
        self.assertEqual(self.original_receipt, self.receipt_path.read_bytes())
