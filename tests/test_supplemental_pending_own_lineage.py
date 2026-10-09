"""Opt-in real immutable supplemental ownership, not fabricated completions."""
import copy
import json
import os
import shutil
from pathlib import Path
import tempfile
import unittest

from scripts import clockify_pending_review_append as append
from scripts import clockify_pending_review_selection as pending
from scripts import clockify_sheet_publish as publisher
from scripts import clockify_financial_semantic_lineage as lineage


class SupplementalPendingOwnLineageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        location = os.environ.get('CLOCKIFY_SUPPLEMENTAL_OWN_ROOT')
        comparison = os.environ.get('CLOCKIFY_SUPPLEMENTAL_COMPARISON')
        capture = os.environ.get('CLOCKIFY_SUPPLEMENTAL_SHEET_CAPTURE')
        if not all((location, comparison, capture)):
            raise unittest.SkipTest('genuine original supplemental ownership fixtures not supplied')
        cls.packet_root = Path(location)
        cls.packet = json.loads((cls.packet_root / 'supplemental-native-packet.json').read_bytes())
        cls.comparison = json.loads(Path(comparison).read_bytes())
        rows = json.loads(Path(capture).read_bytes())['rows']
        cls.captured = {row[0]: row for row in rows}
        cls.records = append._comparisons(cls.comparison, cls.captured, {}, {})
        cls.record = cls.records[0]
        cls.proposal = next(p for p in json.loads((cls.packet_root / 'primary/proposals.json').read_bytes())
                            if publisher.stable_review_id(p) == cls.record['id'])
        cls.routing = json.loads((cls.packet_root / 'inputs/routing.private.json').read_bytes())

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='supplemental-lineage-test-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def source(self):
        paths = {'proposals': self.packet_root / 'primary/proposals.json',
                 'accounting': self.packet_root / 'primary/work-accounting-result.json',
                 'ledger': self.packet_root / 'inputs/evidence-ledger.private.json',
                 'routing': self.packet_root / 'inputs/routing.private.json',
                 'receipt': self.packet_root / 'supplemental-native-packet.json',
                 'replay_proposals': self.packet_root / 'replay/proposals.json',
                 'replay_accounting': self.packet_root / 'replay/work-accounting-result.json'}
        return {'run_id': self.packet['label'], 'basis': 'supplemental-native-packet',
                'artifacts': {k: pending.artifact_handle(p) for k, p in paths.items()}}

    def own(self):
        source = self.source()
        return {'basis': 'native-pending', 'review_id': self.record['id'],
                'source': source, 'replay': copy.deepcopy(source),
                'semantic_ref': copy.deepcopy(self.record['semantic_refs'][0])}

    def authenticate(self, own=None, record=None, captured=None):
        return lineage.authenticate(own or self.own(), surface='pending', identifier=self.record['id'],
            record=record or self.record, captured=self.captured if captured is None else captured, actual={}, cache={})

    def changed_artifact(self, source, name, document):
        path = self.root / (name + '.json')
        path.write_text(json.dumps(document))
        source['artifacts'][name] = pending.artifact_handle(path)

    def test_genuine_supplemental_owner_is_authenticated_without_completed_run_impostor(self):
        # Catches the blanket completed-only basis rejection, not a mock branch.
        try:
            owner = self.authenticate()
        except ValueError as exc:
            self.fail('authentic supplemental own source must be reported separately from current mismatch: ' + str(exc))
        self.assertFalse(owner['verified_current'])
        self.assertEqual(['current-pending-machine-fields-differ-from-own-native-source'], owner['gaps'])
        self.assertEqual('P004', owner['proposal']['id'])
        self.assertEqual(360, owner['duration_seconds'])
        self.assertEqual(7, len(owner['events']))
        self.assertEqual('319d88', owner['financial']['project_suffix'])
        self.assertIs(True, owner['financial']['billable'])
        self.assertEqual(['2c078325'], owner['financial']['tag_suffixes'])
        self.assertEqual('supplemental-native-packet', owner['lineage']['source']['basis'])

    def test_exact_original_machine_fields_can_match_without_full_completion_claim(self):
        row = publisher.proposal_row(self.proposal, self.packet['label'],
                                     project_allowlist=publisher.project_allowlist(self.routing))
        owner = self.authenticate(captured={self.record['id']: row})
        self.assertTrue(owner['verified_current'])
        self.assertEqual([], owner['gaps'])

    def test_curated_description_duration_route_and_tags_do_not_become_native_authority(self):
        for column, value in ((3, '999'), (4, 'editorial route'), (5, 'editorial tags'), (8, 'curated prose')):
            with self.subTest(column=column):
                row = publisher.proposal_row(self.proposal, self.packet['label'],
                    project_allowlist=publisher.project_allowlist(self.routing))
                row[column] = value
                owner = self.authenticate(captured={self.record['id']: row})
                self.assertFalse(owner['verified_current'])
                self.assertEqual(360, owner['duration_seconds'])
                self.assertEqual('319d88', owner['financial']['project_suffix'])
                self.assertEqual(['2c078325'], owner['financial']['tag_suffixes'])

    def test_all21_actual_counterparts_keep_current_machine_field_gaps(self):
        self.assertEqual(21, len(self.records))
        for record in self.records:
            with self.subTest(review_id=record['id']):
                own = self.own()
                own['review_id'] = record['id']
                own['semantic_ref'] = copy.deepcopy(record['semantic_refs'][0])
                result = lineage.authenticate(own, surface='pending', identifier=record['id'], record=record,
                    captured=self.captured, actual={}, cache={})
                self.assertFalse(result['verified_current'])
                self.assertEqual(['current-pending-machine-fields-differ-from-own-native-source'], result['gaps'])

    def test_composed_packet_contract_is_not_broadened(self):
        own = self.own()
        own['source']['basis'] = own['replay']['basis'] = 'composed-native-packet'
        with self.assertRaises(ValueError):
            self.authenticate(own)

    def test_source_replay_and_receipt_identity_cannot_be_relabelled(self):
        for mode in ('run_id', 'different_replay', 'different_primary_path'):
            with self.subTest(mode=mode):
                own = self.own()
                if mode == 'run_id':
                    own['source']['run_id'] = own['replay']['run_id'] = 'invented-completed-source'
                elif mode == 'different_replay':
                    own['replay']['run_id'] = 'different-packet'
                else:
                    self.changed_artifact(own['source'], 'proposals', json.loads(Path(own['source']['artifacts']['proposals']['path']).read_bytes()))
                    own['replay'] = copy.deepcopy(own['source'])
                with self.assertRaises(ValueError):
                    self.authenticate(own)

    def test_modified_original_replay_bytes_and_receipt_inputs_reject(self):
        for artifact in ('replay_proposals', 'receipt'):
            with self.subTest(artifact=artifact):
                own = self.own()
                value = json.loads(Path(own['source']['artifacts'][artifact]['path']).read_bytes())
                if artifact == 'replay_proposals':
                    value[0]['duration_seconds'] += 60
                else:
                    value['input_hashes']['routing.private.json'] = '0' * 64
                self.changed_artifact(own['source'], artifact, value)
                own['replay'] = copy.deepcopy(own['source'])
                with self.assertRaises(ValueError):
                    self.authenticate(own)

    def test_malformed_receipt_and_handle_digest_drift_reject(self):
        own = self.own()
        own['source']['artifacts']['receipt']['sha256'] = 'sha256:' + '0' * 64
        own['replay'] = copy.deepcopy(own['source'])
        with self.assertRaises(ValueError):
            self.authenticate(own)
        own = self.own()
        receipt = copy.deepcopy(self.packet)
        receipt['strict_full_source_quality_run'] = True
        self.changed_artifact(own['source'], 'receipt', receipt)
        own['replay'] = copy.deepcopy(own['source'])
        with self.assertRaises(ValueError):
            self.authenticate(own)

    def test_wrong_own_activity_and_exact_sealed_event_membership_reject(self):
        own = self.own()
        own['semantic_ref']['activity_id'] = 'not-this-own-activity'
        with self.assertRaises(ValueError):
            self.authenticate(own)
        for mode in ('missing', 'modified', 'extra'):
            with self.subTest(mode=mode):
                record = copy.deepcopy(self.record)
                if mode == 'missing':
                    record['events'].pop()
                elif mode == 'modified':
                    record['events'][0]['observed_at'] = '2026-10-04T12:00:00Z'
                else:
                    record['events'].append(copy.deepcopy(record['events'][0]))
                with self.assertRaises(ValueError):
                    self.authenticate(record=record)

    def test_resealed_primary_semantics_and_changed_original_replay_semantics_reject(self):
        for mode in ('primary', 'replay'):
            with self.subTest(mode=mode):
                copied = self.root / mode
                shutil.copytree(type(self).packet_root, copied)
                receipt_path = copied / 'supplemental-native-packet.json'
                receipt = json.loads(receipt_path.read_bytes())
                receipt['native_accounting_primary'] = str(copied / 'primary')
                receipt['native_accounting_replay'] = str(copied / 'replay')
                receipt_path.write_text(json.dumps(receipt))
                self.packet_root = copied
                semantic_path = copied / mode / 'semantic-analysis.json'
                semantic = json.loads(semantic_path.read_bytes())
                activity = next(a for a in semantic['activities'] if a['activity_id'] == self.proposal['activity_id'])
                activity['outcome'] = 'Fabricated financial semantic outcome'
                semantic_path.write_text(json.dumps(semantic))
                own = self.own()
                primary_path = copied / 'primary/semantic-analysis.json'
                own['semantic_ref']['artifact'] = pending.artifact_handle(primary_path)
                primary = json.loads(primary_path.read_bytes())
                primary_activity = next(a for a in primary['activities'] if a['activity_id'] == self.proposal['activity_id'])
                own['semantic_ref']['activity_sha256'] = pending.digest(primary_activity)
                with self.assertRaisesRegex(ValueError, 'semantic|digest|drift'):
                    self.authenticate(own)

    def test_fresh_unrelated_semantic_handle_cannot_replace_receipt_pinned_own_semantics(self):
        own = self.own()
        semantic = json.loads(Path(own['semantic_ref']['artifact']['path']).read_bytes())
        semantic['activities'][0]['outcome'] = 'Unrelated substituted interpretation'
        path = self.root / 'fresh-semantic-analysis.json'
        path.write_text(json.dumps(semantic))
        own['semantic_ref']['artifact'] = pending.artifact_handle(path)
        with self.assertRaisesRegex(ValueError, 'semantic|digest|drift'):
            self.authenticate(own)


class PortableSupplementalOwnLineageTests(unittest.TestCase):
    """Reuse existing native packet builders; no private fixture or mocks."""
    def setUp(self):
        from test_pending_review_selection import PendingSelectionTests, write
        fixture = PendingSelectionTests(methodName='runTest')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.write = write
        self.source = copy.deepcopy(fixture.binding['sources']['current'])
        self.proposal = fixture.current[0]
        artifacts = self.source['artifacts']
        self.primary_path = Path(artifacts['proposals']['path']).parent / 'semantic-analysis.json'
        self.replay_path = Path(artifacts['replay_proposals']['path']).parent / 'semantic-analysis.json'
        self.activity = {'activity_id': self.proposal['activity_id'],
                         'evidence_ids': self.proposal['provenance']['evidence_ids'],
                         'action': 'Built', 'object': 'portable native fixture',
                         'outcome': 'Ownership regression proof', 'lifecycle': 'complete'}
        semantic = {'activities': [self.activity]}
        primary = write(self.primary_path, semantic)
        replay = write(self.replay_path, semantic)
        receipt_path = Path(artifacts['receipt']['path'])
        receipt = json.loads(receipt_path.read_bytes())
        receipt.update(label=self.source['run_id'], native_accounting_primary=str(self.primary_path.parent),
                       native_accounting_replay=str(self.replay_path.parent))
        receipt['deterministic_accounting_replay']['semantic-analysis.json'] = {
            'byte_equal': True, 'primary_sha256': primary['sha256'][7:], 'replay_sha256': replay['sha256'][7:]}
        artifacts['receipt'] = write(receipt_path, receipt)
        self.review_id = publisher.stable_review_id(self.proposal)
        ledger = json.loads(Path(artifacts['ledger']['path']).read_bytes())
        events = {event['evidence_id']: event for event in ledger['events']}
        self.record = {'events': [events[eid] for eid in self.proposal['provenance']['evidence_ids']]}
        self.row = publisher.proposal_row(self.proposal, self.source['run_id'], project_allowlist={})
        self.semantic_ref = {'artifact': primary, 'activity_id': self.activity['activity_id'],
                             'activity_sha256': pending.digest(self.activity)}

    def authenticate(self):
        own = {'basis': 'native-pending', 'review_id': self.review_id, 'source': self.source,
               'replay': copy.deepcopy(self.source), 'semantic_ref': self.semantic_ref}
        return lineage.authenticate(own, surface='pending', identifier=self.review_id, record=self.record,
            captured={self.review_id: self.row}, actual={}, cache={})

    def test_portable_original_supplemental_ownership_uses_real_native_validators(self):
        try:
            owner = self.authenticate()
        except ValueError as exc:
            self.fail('valid original supplemental packet did not authenticate: ' + str(exc))
        self.assertTrue(owner['verified_current'])
        self.assertEqual([], owner['gaps'])
        self.assertEqual(60, owner['duration_seconds'])
        self.assertEqual('abc123', owner['financial']['project_suffix'])
        self.assertEqual('portable native fixture', owner['core']['object'])
        self.assertEqual(1, len(owner['events']))

    def test_portable_resealed_primary_and_changed_replay_semantics_reject(self):
        for mode in ('primary', 'replay'):
            with self.subTest(mode=mode):
                original = {'activities': [self.activity]}
                self.semantic_ref['artifact'] = self.write(self.primary_path, original)
                self.write(self.replay_path, original)
                self.semantic_ref['activity_sha256'] = pending.digest(self.activity)
                changed = copy.deepcopy(original)
                changed['activities'][0]['outcome'] = 'Fabricated interpretation'
                self.write(self.primary_path if mode == 'primary' else self.replay_path, changed)
                if mode == 'primary':
                    self.semantic_ref['artifact'] = pending.artifact_handle(self.primary_path)
                    self.semantic_ref['activity_sha256'] = pending.digest(changed['activities'][0])
                with self.assertRaisesRegex(ValueError, 'semantic|digest|drift'):
                    self.authenticate()


if __name__ == '__main__':
    unittest.main()
