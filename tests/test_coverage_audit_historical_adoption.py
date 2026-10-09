"""Observer scopes are authenticated by native adoption witnesses, not paths."""
import copy
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from scripts import clockify_review_cycle as cycle, clockify_review_run as review
import test_cycle_historical_adoption as historical
import test_cycle_selected_delivery_adoption as selected
import test_cycle_pending_frozen_routing_adoption as pending
from test_review_cycle_delivery import write_json


class HistoricalCoverageAuditTests(unittest.TestCase):
    def fixture(self, kind='native'):
        fixture = (selected.SelectedHistoricalDeliveryTests() if kind == 'selected'
                   else pending.PendingFrozenRoutingAdoptionTests() if kind == 'pending'
                   else historical.HistoricalAdoptionTests())
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        if kind == 'native':
            config, request, derived, raw = fixture._external_graph_request()
        elif kind == 'selected':
            operational = fixture.root / 'operational-runs'
            operational.mkdir()
            config = {**fixture.config, 'runs_dir': str(operational)}
            request, derived, raw = fixture.request, fixture.source, None
        else:
            config, request, derived, raw = fixture.config, fixture.request, fixture.derived, fixture.raw
        cycle.adopt_historical_slice(config, request)
        fixture.audit_config, fixture.audit_request = config, request
        fixture.audit_derived, fixture.audit_raw = derived, raw
        return fixture

    @staticmethod
    def inventory(root):
        return {str(p): (p.read_bytes(), p.stat().st_mtime_ns)
                for p in root.rglob('*') if p.is_file()}

    def audit(self, fixture):
        config = fixture.audit_config
        before = self.inventory(fixture.root)
        original = copy.deepcopy(config)
        previous = review.RUNS
        with mock.patch.object(cycle, '_atomic', side_effect=AssertionError('observer write')), \
             mock.patch.object(cycle, '_ensure_period', side_effect=AssertionError('observer period mutation')), \
             mock.patch.object(cycle, 'run_child_bounded', side_effect=AssertionError('observer child')):
            report = cycle.source_interval_coverage_audit(config)
        self.assertEqual(before, self.inventory(fixture.root))
        self.assertEqual(original, config)
        self.assertEqual(previous, review.RUNS)
        return report

    def test_observes_native_receipt_pinned_external_graph_without_writes(self):
        fixture = self.fixture()
        try:
            report = self.audit(fixture)
        except cycle.CycleError as error:
            self.fail('authentic historical native coverage must be observable: '+str(error))
        self.assertEqual('2026-09-08T21:00:00Z', report['frontiers']['fathom'])

    def test_observes_selected_receipt_pinned_external_graph_without_writes(self):
        fixture = self.fixture('selected')
        try:
            report = self.audit(fixture)
        except cycle.CycleError as error:
            self.fail('authentic historical selected coverage must be observable: '+str(error))
        self.assertEqual('2026-09-08T21:00:00Z', report['frontiers']['fathom'])

    def test_observes_native_pending_raw_without_inventing_raw_completion(self):
        fixture = self.fixture('pending')
        self.assertFalse((fixture.audit_raw / 'completion-bundle.json').exists())
        try:
            report = self.audit(fixture)
        except cycle.CycleError as error:
            self.fail('authentic pending raw ancestry must remain observable: '+str(error))
        self.assertEqual('2026-09-01T21:00:00Z', report['frontiers']['fathom'])
        self.assertFalse((fixture.audit_raw / 'completion-bundle.json').exists())

    def test_unwitnessed_external_stage_and_receipt_drift_stay_rejected(self):
        fixture = self.fixture()
        state_path = fixture.state_dir / 'review-cycle-state.json'
        original = json.loads(state_path.read_bytes())
        for kind in ('missing', 'wrong-digest', 'wrong-stage', 'wrong-interval'):
            state = copy.deepcopy(original)
            record = state['slices'][fixture.since]
            if kind == 'missing':
                record.pop('historical_adoption_receipt')
                record.pop('historical_adoption_receipt_digest')
            elif kind == 'wrong-digest': record['historical_adoption_receipt_digest'] = 'sha256:'+'f'*64
            elif kind == 'wrong-stage': record['source']['result_digest'] = 'sha256:'+'f'*64
            else: record['until'] = '2026-09-10'
            write_json(state_path, state)
            before = self.inventory(fixture.root)
            previous = review.RUNS
            with self.subTest(kind=kind), self.assertRaises(cycle.CycleError):
                cycle.source_interval_coverage_audit(fixture.audit_config)
            self.assertEqual(before, self.inventory(fixture.root))
            self.assertEqual(previous, review.RUNS)

    def test_selected_delivery_proof_and_stored_replay_drift_stay_rejected(self):
        fixture = self.fixture('selected')
        proof = Path(fixture.audit_request['selected_delivery_proofs']['live_readback']['path'])
        before = proof.read_bytes()
        proof.write_bytes(before+b' ')
        try:
            with self.assertRaises(cycle.CycleError):
                cycle.source_interval_coverage_audit(fixture.audit_config)
        finally: proof.write_bytes(before)
        state_path = fixture.state_dir/'review-cycle-state.json'
        state = json.loads(state_path.read_bytes())
        state['slices'][fixture.since]['replay']['result_digest'] = 'sha256:'+'f'*64
        write_json(state_path,state)
        before_inventory = self.inventory(fixture.root)
        previous = review.RUNS
        with self.assertRaisesRegex(cycle.CycleError,'historical stage identity drifted'):
            cycle.source_interval_coverage_audit(fixture.audit_config)
        self.assertEqual(before_inventory,self.inventory(fixture.root))
        self.assertEqual(previous,review.RUNS)

    def test_native_parent_bytes_publication_and_root_drift_stay_rejected(self):
        fixture = self.fixture()
        for path in (fixture.audit_raw/'evidence/clockify-existing.json',
                     Path(fixture.audit_request['publication_result']),
                     fixture.audit_derived/'collector-source.json'):
            before = path.read_bytes()
            path.write_bytes(before+b' ')
            try:
                with self.subTest(path=path), self.assertRaises(cycle.CycleError):
                    cycle.source_interval_coverage_audit(fixture.audit_config)
            finally: path.write_bytes(before)
        state_path = fixture.state_dir/'review-cycle-state.json'
        state = json.loads(state_path.read_bytes())
        record = state['slices'][fixture.since]
        path = Path(record['historical_adoption_receipt'])
        document = json.loads(path.read_bytes())
        document['runs_root'] = fixture.audit_config['runs_dir']
        document['receipt_digest'] = cycle._value_digest({k:v for k,v in document.items() if k!='receipt_digest'})
        record['historical_adoption_receipt_digest'] = document['receipt_digest']
        write_json(path, document); write_json(state_path, state)
        with self.assertRaisesRegex(cycle.CycleError, 'escapes its bounded run'):
            cycle.source_interval_coverage_audit(fixture.audit_config)

    def test_general_stage_validator_does_not_accept_external_stage_even_with_historical_flag(self):
        fixture = self.fixture()
        with self.assertRaisesRegex(cycle.CycleError,'escapes its bounded run'):
            cycle._validate_stage(fixture.audit_config,Path(fixture.audit_request['source_result']),
                fixture.since,fixture.until,replay=False,
                expected_snapshot_digests=fixture.audit_request['adopted_snapshot_digests'],
                historical_state_validation=True,allow_historical_runtime=True,
                expected_runtime_digest=fixture.audit_request['runtime_identity_digest'])

@unittest.skipUnless(os.environ.get('CLOCKIFY_RETAINED_COMPATIBILITY_CONFIG'),
                     'private retained recovery graph not selected')
class NativeRecoveryCoverageAuditTests(unittest.TestCase):
    """Authentic retained native proof, without copying or editing live artifacts."""
    def setUp(self):
        self.config = json.loads(Path(os.environ['CLOCKIFY_RETAINED_COMPATIBILITY_CONFIG']).read_bytes())
        self.state_path = Path(self.config['state_dir'])/'review-cycle-state.json'
        self.state = cycle._state(self.state_path,recovery_since=self.config['recovery_since'])
        self.since = '2026-10-07'
        self.record = self.state['slices'][self.since]
        self.assertEqual('2026-10-08',self.record['until'])
        # Filter observation scope, never bypass any graph or witness validator.
        self.state['slices'] = {self.since:self.record}

    def audit(self):
        state_bytes = self.state_path.read_bytes()
        previous = review.RUNS
        with mock.patch.object(cycle,'_state',return_value=copy.deepcopy(self.state)), \
             mock.patch.object(cycle,'_atomic',side_effect=AssertionError('observer write')), \
             mock.patch.object(cycle,'_ensure_period',side_effect=AssertionError('observer period mutation')), \
             mock.patch.object(cycle,'run_child_bounded',side_effect=AssertionError('observer provider child')):
            try: return cycle.source_interval_coverage_audit(self.config)
            finally:
                self.assertEqual(state_bytes,self.state_path.read_bytes())
                self.assertEqual(previous,review.RUNS)

    def test_native_recovery_uses_parent_backlog_and_exact_child_receipt(self):
        report = self.audit()
        stage = self.record['source']
        child = Path(stage['run_dir'])
        collector = cycle.collector_receipts.load_collector_source_bundle(child/'completion-bundle.json',run_dir=child)
        intervals = [row for row in report['intervals']
                     if row['source']=='fathom' and row['since_utc']=='2026-10-06T21:00:00Z']
        self.assertEqual(1,len(intervals))
        self.assertEqual(collector.source_bundle_digest,intervals[0]['completion_bundle_digest'])
        self.assertEqual('2026-10-07T21:00:00Z',intervals[0]['until_utc'])

    def test_native_recovery_receipt_drift_is_rejected(self):
        attempt = next(iter(self.record['recovery_attempts'].values()))
        receipt = json.loads(Path(attempt['recovery_receipt_path']).read_bytes())
        receipt['artifacts']['completion_bundle_sha256'] = 'sha256:'+'f'*64
        with mock.patch.object(cycle.clockify_source_debt_recover,'_read_secure_receipt',return_value=receipt):
            with self.assertRaisesRegex(cycle.CycleError,'historical recovery proof is invalid'):
                self.audit()

    def test_native_recovery_child_drift_is_rejected(self):
        self.record['source']['result_digest'] = 'sha256:'+'f'*64
        with self.assertRaisesRegex(cycle.CycleError,'stored source identity drifted'):
            self.audit()

    def test_native_recovery_parent_drift_is_rejected(self):
        next(iter(self.record['recovery_parents'].values()))['result_digest'] = 'sha256:'+'f'*64
        with self.assertRaises(cycle.CycleError): self.audit()

if __name__ == '__main__': unittest.main()
