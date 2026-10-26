"""A completed pending derivation stays bound to the release that launched it."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import clockify_review_run as review, clockify_sync_collect as collector
from scripts import clockify_review_cycle as cycle
from test_historical_release_runtime import archive_release
from test_pending_collector_derivation import pending_fixture

SINCE, UNTIL = '2026-09-01', '2026-09-02'


def runtime_of(release: Path) -> dict:
    return {'collector_path': str(release / 'scripts/clockify_sync_collect.py'),
            'canonical_root': str(release), 'git_sha': None, 'git_dirty': None}


class PendingDerivationReleaseRebindTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.runs, self.source, self.checkpoints, self.snapshots = pending_fixture(self.root)
        for context in (mock.patch.multiple(review, RUNS=self.runs),
                        mock.patch.object(collector, 'collector_checkpoint_root', return_value=self.checkpoints),
                        mock.patch.dict(os.environ, {'CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT': str(self.checkpoints)})):
            context.start(); self.addCleanup(context.stop)
        self.releases = self.root / 'releases'; self.releases.mkdir(mode=0o700)
        self.addCleanup(lambda: [p.chmod(0o700) for p in self.releases.rglob('*') if p.is_dir()])
        self.old = archive_release(self.releases, 'a' * 40, {'scripts/clockify_review_run.py': b'old'})
        self.new = archive_release(self.releases, 'b' * 40, {'scripts/clockify_review_run.py': b'new'})
        base = {'runs_dir': str(self.runs), 'state_dir': str(self.root), 'cache': str(self.root / 'cache.jsonl'),
                'timezone': 'Europe/Bucharest', 'member_id': 'user-one', 'workspace_id': 'workspace-one'}
        self.old_config = {**base, 'root': str(self.old), 'routing': str(self.snapshots['routing.json']),
                           '_runtime_identity': runtime_of(self.old)}
        # The newer release legitimately carries a different private routing.
        new_routing = self.root / 'new-routing.json'
        new_routing.write_text(json.dumps({**json.loads(self.snapshots['routing.json'].read_bytes()), 'added': 1}) + '\n')
        self.new_config = {**base, 'root': str(self.new), 'routing': str(new_routing),
                           '_runtime_identity': runtime_of(self.new)}
        self.record = {'expected_snapshot_digests': {n: cycle._digest(self.source / n) for n in self.snapshots}}
        self.launch = cycle._prepare_pending_derivation_launch(self.old_config, self.record, SINCE, UNTIL)
        self.record['fresh_input_binding'] = {
            'runtime_identity': runtime_of(self.old), 'release_root': str(self.old),
            'snapshot_digests': dict(self.launch['snapshot_digests'])}
        self.completed = {**copy.deepcopy(self.record), 'source': {'run_dir': self.launch['child_run_dir']}}

    def verify(self, record, config=None):
        return cycle._prepare_pending_derivation_launch(config or self.new_config, record, SINCE, UNTIL)

    def test_completed_launch_verifies_under_a_newer_release_without_rewriting_its_binding(self):
        before = copy.deepcopy(self.completed)
        self.assertEqual(self.launch, self.verify(self.completed))
        self.assertEqual(before, self.completed)
        self.assertIn(str(self.old), self.completed['pending_derivation_binding']['command'][1])

    def test_active_launch_still_requires_the_current_runtime(self):
        with self.assertRaisesRegex(cycle.CycleError, 'drifted'):
            self.verify(copy.deepcopy(self.record))

    def test_completed_launch_without_recorded_release_authority_requires_current_runtime(self):
        del self.completed['fresh_input_binding']
        with self.assertRaisesRegex(cycle.CycleError, 'drifted'):
            self.verify(self.completed)

    def test_recorded_authority_must_be_the_launch_executor(self):
        for mutation in ('runtime', 'release', 'routing', 'command', 'binding-digest'):
            changed = copy.deepcopy(self.completed)
            fresh, binding = changed['fresh_input_binding'], changed['pending_derivation_binding']
            if mutation == 'runtime': fresh['runtime_identity'] = runtime_of(self.new)
            elif mutation == 'release': fresh['release_root'] = str(self.new)
            elif mutation == 'routing': fresh['snapshot_digests']['routing.json'] = 'sha256:' + '0' * 64
            elif mutation == 'command': binding['command'][1] = str(self.new / 'scripts/clockify_review_run.py')
            else: binding['command_digest'] = 'sha256:' + '0' * 64
            with self.subTest(mutation=mutation), self.assertRaisesRegex(cycle.CycleError, 'drifted'):
                self.verify(changed)

    def test_recorded_release_must_authenticate_in_the_same_releases_root(self):
        target = self.old / 'scripts/clockify_review_run.py'
        target.parent.chmod(0o755); target.chmod(0o644); target.write_bytes(b'rewritten')
        with self.assertRaisesRegex(cycle.CycleError, 'drifted'):
            self.verify(copy.deepcopy(self.completed))

    def test_recorded_release_outside_the_current_releases_root_is_rejected(self):
        other = self.root / 'other-releases'; other.mkdir(mode=0o700)
        self.addCleanup(lambda: [p.chmod(0o700) for p in other.rglob('*') if p.is_dir()])
        elsewhere = archive_release(other, 'c' * 40, {'scripts/clockify_review_run.py': b'new'})
        config = {**self.new_config, 'root': str(elsewhere), '_runtime_identity': runtime_of(elsewhere)}
        with self.assertRaisesRegex(cycle.CycleError, 'drifted'):
            self.verify(copy.deepcopy(self.completed), config)


if __name__ == '__main__':
    unittest.main()
