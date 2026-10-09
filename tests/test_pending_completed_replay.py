"""Opt-in authentic saved-replay integration tests, never invented completions.

Set CLOCKIFY_PENDING_NATIVE_TRACE to a read-only diagnosis artifact containing
source_records.authentic_existing_replay. The ordinary suite skips this private
artifact integration when those saved local inputs are not supplied.
"""
import copy
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from scripts import clockify_pending_review_selection as consumer, collector_receipts


class CompletedReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = os.environ.get('CLOCKIFY_PENDING_NATIVE_TRACE')
        if not path:
            raise unittest.SkipTest('authentic saved replay inputs not supplied')
        trace = json.loads(Path(path).read_bytes())
        cls.original = trace['source_records']['authentic_existing_replay']
        cls.record = {**cls.original, 'basis': 'completed-review-replay'}
        cls.root = Path(cls.record['artifacts']['receipt']['path']).parent
        cls.bundle = collector_receipts.load_completion_bundle(cls.root / 'completion-bundle.json', run_dir=cls.root)
        assert cls.bundle.replay is True

    def test_truthful_completed_replay_preserves_authentic_native_output(self):
        before = {name: Path(handle['path']).read_bytes() for name, handle in self.record['artifacts'].items()}
        try:
            source = consumer._source(self.record, {})
        except ValueError as exc:
            self.fail(f'genuine verified replay has no truthful saved-source representation: {exc}')
        self.assertEqual(60, len(source['proposals']))
        self.assertEqual(source['proposals'], source['accounting']['proposals'])
        self.assertEqual(self.root.name, source['run_id'])
        self.assertEqual(before, {name: Path(handle['path']).read_bytes() for name, handle in self.record['artifacts'].items()})

    def test_replay_mislabelled_completed_nonreplay_still_rejects(self):
        with self.assertRaisesRegex(ValueError, 'completed source identity differs'):
            consumer._source(self.original, {})

    def copy_record(self, directory):
        copied = Path(directory) / self.root.name
        shutil.copytree(self.root, copied)
        record = copy.deepcopy(self.record)
        for handle in record['artifacts'].values():
            handle['path'] = str(copied / Path(handle['path']).relative_to(self.root))
        return record, copied

    def test_tampered_replay_artifact_cannot_reuse_original_completion(self):
        with tempfile.TemporaryDirectory() as directory:
            record, copied = self.copy_record(directory)
            path = copied / 'replay-integrity.json'
            path.write_bytes(path.read_bytes() + b'\n')
            with self.assertRaisesRegex(ValueError, 'digest'):
                consumer._source(record, {})

    def test_replay_requires_own_completed_artifact_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            record, _ = self.copy_record(directory)
            record['artifacts']['ledger'] = copy.deepcopy(self.record['artifacts']['ledger'])
            with self.assertRaisesRegex(ValueError, 'completion artifacts'):
                consumer._source(record, {})

    def test_replay_cannot_use_foreign_integrity_handle(self):
        with tempfile.TemporaryDirectory() as directory:
            record, _ = self.copy_record(directory)
            record['artifacts']['replay'] = consumer.artifact_handle(self.root / 'replay-integrity.json')
            with self.assertRaisesRegex(ValueError, 'completed replay'):
                consumer._source(record, {})

    def test_replay_cannot_turn_full_completed_accounting_into_subset_source(self):
        with tempfile.TemporaryDirectory() as directory:
            record, copied = self.copy_record(directory)
            path = copied / 'proposals.json'
            proposals = json.loads(path.read_bytes())
            path.write_text(json.dumps(proposals[:1]))
            record['artifacts']['proposals'] = consumer.artifact_handle(path)
            with self.assertRaisesRegex(ValueError, 'completed replay'):
                consumer._source(record, {})


if __name__ == '__main__':
    unittest.main()
