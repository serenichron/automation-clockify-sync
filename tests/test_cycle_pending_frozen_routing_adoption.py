"""Frozen pending collector inputs are distinct from valid adopted routing."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from scripts import clockify_review_cycle as cycle, clockify_review_run as review
from scripts import source_coverage
from test_pending_collector_derivation import pending_fixture, write_empty_actor_fixture
from test_review_cycle_delivery import write_json
import test_cycle_historical_adoption as historical_fixtures


class PendingFrozenRoutingAdoptionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runs, self.raw, self.checkpoints, snapshots = pending_fixture(self.root)
        self.since, self.until = '2026-09-01', '2026-09-02'
        self.state_dir = self.root / 'state'
        operational = self.root / 'operational-runs'
        operational.mkdir()
        self.config = {
            'root': str(self.root), 'runs_dir': str(operational),
            'state_dir': str(self.state_dir), 'cache': str(self.root / 'cache'),
            'routing': str(snapshots['routing.json']),
            'corrections': str(self.raw / 'review-corrections.jsonl'),
            'acceptance': str(self.raw / 'review-acceptance.jsonl'),
            'timezone': 'Europe/Bucharest', 'member_id': 'user-one',
            'workspace_id': 'workspace-one', 'recovery_since': self.since,
            'spreadsheet_id': 'sheet-one', 'calendly_optional': True,
            'monthly_sheet_title_template': '{month_name} {year} portfolio review',
        }
        self.manifest = cycle._ensure_period(
            self.config, self.state_dir, self.since, self.until, bind_inputs=True)
        shutil.copyfile(self.manifest, self.raw / 'period-manifest.json')
        frozen = {name: cycle._digest(self.raw / name) for name in snapshots}
        self.state_path = self.state_dir / 'review-cycle-state.json'
        write_json(self.state_path, {
            'schema_version': cycle.SCHEMA_VERSION, 'scheduled_through': self.until,
            'completed_through': None, 'next_work_class': 'routine',
            'slices': {self.since: {'until': self.until, 'status': 'incomplete',
                'period_manifest': str(self.manifest), 'expected_snapshot_digests': frozen}},
        })
        source_coverage.write(self.state_dir / 'source-coverage.json',
                              source_coverage.SourceDebtStore().document())
        (self.state_dir / 'review-cycle.lock').touch()
        fixture = self.root / 'analysis.json'
        write_empty_actor_fixture(fixture)
        environment = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                       'CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT': str(self.checkpoints)}
        def runner(*arguments):
            result = subprocess.run([sys.executable, '-B', review.__file__,
                '--runs-root', str(self.runs), '--state', str(self.root / 'review-state.json'),
                *arguments], env=environment, capture_output=True, text=True)
            self.assertEqual(0, result.returncode, result.stderr + result.stdout)
            return Path(result.stdout.strip().splitlines()[-1])
        source = runner('--derive-pending-from', str(self.raw), '--routing', self.config['routing'],
                        '--analysis-fixture', str(fixture))
        replay = runner('--replay-from', str(source.parent))
        self.derived = source.parent
        publications = cycle._expected_publication_receipts(self.config,
            {'run_dir': str(self.derived), 'run_id': self.derived.name},
            sheet_title='September 2026 portfolio review')
        publication = self.derived / 'sheet-publish-result.json'
        write_json(publication, {'schema_version': 'sheet-publication-result/v1',
            'status': 'published', 'external_writes': True, 'clockify_writes': 0,
            'publications': publications})
        bundle = json.loads((self.derived / 'completion-bundle.json').read_bytes())
        self.request = {
            'schema_version': cycle.DERIVED_ADOPTION_REQUEST_SCHEMA_VERSION,
            'since': self.since, 'until': self.until, 'runs_root': str(self.runs),
            'source_result': str(source), 'source_result_digest': cycle._digest(source),
            'replay_result': str(replay), 'replay_result_digest': cycle._digest(replay),
            'publication_result': str(publication), 'publication_result_digest': cycle._digest(publication),
            'frozen_snapshot_digests': frozen,
            'adopted_snapshot_digests': {name: cycle._digest(self.derived / name) for name in frozen},
            'runtime_identity_digest': bundle['runtime_identity_digest'],
            'source_provenance': {'kind': 'collector_derivation', 'derivation_run_dir': str(self.derived),
                'lineage_digest': cycle._digest(self.derived / 'collector-source.json')},
        }

    def durable(self):
        return {str(path.relative_to(self.state_dir)):
                (path.read_bytes(), path.stat().st_mode, path.stat().st_mtime_ns)
                for path in self.state_dir.rglob('*') if path.is_file()}

    def reject_without_writes(self, pattern):
        before = self.durable()
        with self.assertRaisesRegex(cycle.CycleError, pattern):
            cycle.adopt_historical_slice(self.config, self.request)
        self.assertEqual(before, self.durable())

    def test_current_routing_adopts_pending_original_frozen_inputs_and_repeats_without_writes(self):
        """Catches conflating valid current routing with raw collector's frozen routing."""
        graph = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                 for path in self.runs.rglob('*') if path.is_file()}
        before_config = copy.deepcopy(self.config)
        first = cycle.adopt_historical_slice(self.config, self.request)
        self.assertEqual('delivered', first['status'])
        adopted = json.loads(self.state_path.read_bytes())
        cycle._validate_delivered_state(self.config, adopted)
        self.assertEqual(self.request['frozen_snapshot_digests'],
                         adopted['slices'][self.since]['expected_snapshot_digests'])
        before_repeat = self.durable()
        self.assertEqual(first, cycle.adopt_historical_slice(self.config, self.request))
        self.assertEqual(before_repeat, self.durable())
        self.assertEqual(before_config, self.config)
        self.assertEqual(graph, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in graph})

    def test_no_current_routing_transition_retains_supported_adoption(self):
        """Catches strengthening the old current=frozen path to require adopted=current."""
        self.config['routing'] = str(self.raw / 'routing.json')
        result = cycle.adopt_historical_slice(self.config, self.request)
        self.assertEqual('delivered', result['status'])

    def test_joint_state_request_false_frozen_routing_cannot_replace_raw_authority(self):
        """Catches treating agreement between mutable state and caller as original-source proof."""
        false = 'sha256:' + 'f' * 64
        self.request['frozen_snapshot_digests']['routing.json'] = false
        state = json.loads(self.state_path.read_bytes())
        state['slices'][self.since]['expected_snapshot_digests']['routing.json'] = false
        write_json(self.state_path, state)
        self.reject_without_writes('frozen input proof')

    def test_current_routing_must_match_selected_adopted_stage(self):
        """Catches accepting valid ancestry with a different current runtime routing file."""
        routing = Path(self.config['routing'])
        routing.write_bytes(routing.read_bytes() + b' ')
        self.reject_without_writes('routing input proof')

    def test_altered_raw_snapshot_is_rejected_before_adoption_writes(self):
        """Catches stale pending bindings substituting for reverified raw bytes."""
        routing = self.raw / 'routing.json'
        routing.write_bytes(routing.read_bytes() + b' ')
        self.reject_without_writes('derivation|pending|completion')

    def test_resealed_false_pending_binding_is_rejected_before_adoption_writes(self):
        """Catches accepting a self-consistent lineage claim without authentic pending receipts."""
        path = self.derived / 'collector-source.json'
        lineage = json.loads(path.read_bytes())
        lineage['pending_source_binding']['original_snapshot_digests']['routing.json'] = 'sha256:' + 'f' * 64
        unsigned = {key: value for key, value in lineage.items() if key != 'lineage_digest'}
        lineage['lineage_digest'] = cycle._value_digest(unsigned)
        write_json(path, lineage)
        self.request['source_provenance']['lineage_digest'] = cycle._digest(path)
        self.reject_without_writes('derivation|pending|completion')

    def test_wrong_derivation_ancestor_is_rejected_before_adoption_writes(self):
        """Catches checking a genuine but different ancestor to authorize routing transition."""
        self.request['source_provenance']['derivation_run_dir'] = str(self.raw)
        self.reject_without_writes('derivation ancestry')

    def test_nonrouting_current_input_drift_is_rejected_before_adoption_writes(self):
        """Catches broadening the routing exception to current acceptance or correction inputs."""
        for key in ('acceptance', 'corrections'):
            with self.subTest(key=key):
                changed = self.root / (key + '-changed.jsonl')
                changed.write_bytes(b'changed\n')
                original = self.config[key]
                self.config[key] = str(changed)
                try:
                    self.reject_without_writes('frozen input proof')
                finally:
                    self.config[key] = original

    def test_current_period_bytes_drift_is_rejected_before_adoption_writes(self):
        """Catches exempting the period manifest from the frozen-input gate."""
        self.manifest.write_bytes(self.manifest.read_bytes() + b' ')
        self.reject_without_writes('frozen input proof')

    def test_missing_pending_binding_cannot_authorize_routing_transition(self):
        """Catches accepting a pending lineage with its authentic original binding removed."""
        path = self.derived / 'collector-source.json'
        lineage = json.loads(path.read_bytes())
        lineage.pop('pending_source_binding')
        unsigned = {key: value for key, value in lineage.items() if key != 'lineage_digest'}
        lineage['lineage_digest'] = cycle._value_digest(unsigned)
        write_json(path, lineage)
        self.request['source_provenance']['lineage_digest'] = cycle._digest(path)
        self.reject_without_writes('derivation|pending|completion')

    def test_replay_result_drift_is_rejected_before_adoption_writes(self):
        """Catches accepting source authority despite a different replay's sealed result."""
        path = Path(self.request['replay_result'])
        path.write_bytes(path.read_bytes() + b' ')
        self.reject_without_writes('replay_result_digest')

    def test_missing_period_transition_does_not_create_manifest_or_history(self):
        """Catches routing-transition rejection first recreating immutable period inputs."""
        events, manifest = cycle._period_paths(self.state_dir, self.since)
        events.rename(self.root / events.name)
        manifest.rename(self.root / manifest.name)
        self.reject_without_writes('frozen input proof')
        self.assertFalse(events.exists())
        self.assertFalse(manifest.exists())

    def test_revalidation_rejects_joint_state_receipt_false_frozen_routing(self):
        """Catches durable receipt revalidation forgetting the authentic raw frozen anchor."""
        cycle.adopt_historical_slice(self.config, self.request)
        state = json.loads(self.state_path.read_bytes())
        record = state['slices'][self.since]
        path = Path(record['historical_adoption_receipt'])
        document = json.loads(path.read_bytes())
        false = 'sha256:' + 'f' * 64
        document['frozen_snapshot_digests']['routing.json'] = false
        record['expected_snapshot_digests']['routing.json'] = false
        unsigned = {key: value for key, value in document.items() if key != 'receipt_digest'}
        document['receipt_digest'] = cycle._value_digest(unsigned)
        record['historical_adoption_receipt_digest'] = document['receipt_digest']
        write_json(path, document)
        write_json(self.state_path, state)
        with self.assertRaisesRegex(cycle.CycleError, 'frozen input proof'):
            cycle._validate_delivered_state(self.config, state)


class NonPendingRoutingCompatibilityTests(unittest.TestCase):
    def test_nonpending_derived_routing_transition_keeps_original_frozen_gate(self):
        """Catches extending pending-only frozen authority to nonpending derived lineage."""
        proof = historical_fixtures.HistoricalAdoptionTests()
        proof.setUp()
        self.addCleanup(proof.doCleanups)
        request, derived, _raw = proof._derived_adoption_request()
        config = {**proof.config, 'routing': str(derived / 'routing.json')}
        before = (proof.state_dir / 'review-cycle-state.json').read_bytes()
        with self.assertRaisesRegex(cycle.CycleError, 'frozen input proof'):
            cycle.adopt_historical_slice(config, request)
        self.assertEqual(before, (proof.state_dir / 'review-cycle-state.json').read_bytes())
        self.assertFalse((proof.state_dir / 'delivery-receipts').exists())


if __name__ == '__main__':
    unittest.main()
