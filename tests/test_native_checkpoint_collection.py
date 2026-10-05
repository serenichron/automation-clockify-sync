"""Fresh collection must retain the complete native proof without ledger churn."""
import argparse
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import clockify_sync_collect as collector, collector_checkpoints as checkpoints
from scripts import clockify_checkpoint_snapshot as snapshots


class NativeCheckpointCollectionTests(unittest.TestCase):
    def collect(self, root, *, missing=False, entries=None):
        since = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
        until = since + dt.timedelta(days=1)
        store = checkpoints.PageCheckpointStore(root / 'cache')
        routing = {'clockify_user_id': 'user-one'}
        environment = {'CLOCKIFY_WORKSPACE_ID': 'workspace-one'}
        checkpoint = None
        if not missing:
            identity = collector._clockify_checkpoint_identity('workspace-one', 'user-one', since, until)
            state = store.open(identity, initial_metadata={'snapshot_at': '2026-09-03T00:00:00Z'})
            entries = entries or []
            state = store.append_page(state, payload=entries, continuation={'page': 2},
                                      signature=collector._clockify_page_signature(entries))
            state = store.mark_complete(state)
            checkpoint = state.directory
        else:
            environment = {'_missing': ['CLOCKIFY_API_KEY']}
        originals = {path.relative_to(root): path.read_bytes() for path in root.rglob('*') if path.is_file()}
        run = root / 'runs/fresh'
        with patch.object(collector, 'collector_runtime_identity', return_value={'git_sha': 'fixture'}), \
             patch.object(collector, 'clockify_get', side_effect=AssertionError('network forbidden')), \
             patch.object(collector, 'fetch_fathom', return_value={'status': 'ok', 'complete': True, 'meetings': []}), \
             patch.object(collector, 'fetch_multica_issues', return_value={'status': 'ok', 'complete': True, 'issues': []}), \
             patch.object(collector, 'build_proposals', return_value=([], [], [])):
            collector._collect_slice(argparse.Namespace(calendly_optional=True, enrich=False),
                routing, {'machines': []}, environment, {}, since, until, 'fixture', store, run,
                coordinator='omarchy-precision')
        for relative, raw in originals.items():
            self.assertEqual(raw, (root / relative).read_bytes())
        return run, since, until, checkpoint

    def test_fresh_completed_collection_seals_native_snapshot_in_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            run, since, until, checkpoint = self.collect(Path(temporary))
            report = json.loads((run / 'run-report.json').read_bytes())
            self.assertIn('clockify_native_checkpoint', report,
                          'fresh collector discards the complete native checkpoint proof')
            proof = report['clockify_native_checkpoint']
            native_dir = run / 'evidence/clockify-native-checkpoint'
            loaded = snapshots.load_checkpoint_snapshot(native_dir,
                workspace_id='workspace-one', user_id='user-one', since=since, until=until,
                expected_manifest_sha256=proof['manifest_sha256'])
            self.assertEqual([], loaded.entries)
            self.assertEqual({'workspace_id': 'workspace-one', 'user_id': 'user-one',
                'since_utc': '2026-09-01T00:00:00Z', 'until_utc': '2026-09-02T00:00:00Z'}, proof['request'])
            self.assertEqual((run / 'evidence/clockify-existing.json').read_bytes(),
                             (native_dir / 'clockify-existing.json').read_bytes())
            self.assertEqual((checkpoint / 'manifest.json').read_bytes(),
                             (native_dir / 'checkpoint' / checkpoint.name / 'manifest.json').read_bytes())

    def test_incomplete_source_does_not_claim_native_proof(self):
        with tempfile.TemporaryDirectory() as temporary:
            run, *_ = self.collect(Path(temporary), missing=True)
            report = json.loads((run / 'run-report.json').read_bytes())
            self.assertNotIn('clockify_native_checkpoint', report)
            self.assertFalse((run / 'evidence/clockify-native-checkpoint').exists())

    def test_fresh_snapshot_retains_full_id_payload_and_seconds(self):
        entry = {'id': '0123456789abcdef01234567', 'workspaceId': 'workspace-one',
            'userId': 'user-one', 'description': 'Reviewed project work',
            'projectId': 'project-one', 'tagIds': ['tag-one'], 'taskId': None,
            'billable': True, 'timeInterval': {'start': '2026-09-01T09:00:00Z',
                'end': '2026-09-01T09:02:37Z', 'duration': 'PT2M37S'}}
        with tempfile.TemporaryDirectory() as temporary:
            run, since, until, _ = self.collect(Path(temporary), entries=[entry])
            report = json.loads((run / 'run-report.json').read_bytes())
            loaded = snapshots.load_checkpoint_snapshot(run / 'evidence/clockify-native-checkpoint',
                workspace_id='workspace-one', user_id='user-one', since=since, until=until,
                expected_manifest_sha256=report['clockify_native_checkpoint']['manifest_sha256'])
            self.assertEqual([entry], loaded.entries)


if __name__ == '__main__':
    unittest.main()
