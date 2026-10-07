"""The systemd direct-script import path must support optional native credits."""
import os
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import test_cycle_native_credit_producer as fixtures
from test_semantic_analyzer import valid_response
from test_scoped_cached_repair_materialization import completed_recovery_fixture
from scripts import clockify_review_cycle as cycle
from scripts import clockify_review_run as review
from scripts import work_accounting_pipeline as pipeline


ROOT = Path(__file__).resolve().parents[1]


class StandaloneNativeCreditTests(unittest.TestCase):
    def test_standalone_native_credit_repair_runs_real_accounting_child(self):
        # A parent process's bootstrap must not conceal child import failures.
        fixture = fixtures.CycleNativeCreditProducerTests()
        self.addCleanup(fixture.doCleanups)
        transport, config, source, *_ = fixture.fixture()
        analysis=json.loads((transport.source/'semantic-analysis.json').read_text())
        ledger=json.loads((transport.source/'evidence/evidence-ledger.json').read_text())
        meetings=[row for row in ledger['events'] if row['source_type'] in ('fathom','calendly')]
        activity=valid_response(meetings[0]['evidence_id'])['activities'][0]
        activity.update(id='meeting-fixture',lifecycle='meeting',action='Discuss',
            object='recorded meeting',outcome='decisions aligned',
            evidence_ids=[row['evidence_id'] for row in meetings],
            evidence_spans=[row['raw_source_span'] for row in meetings])
        analysis['activities']=[activity]
        analysis_path=transport.root/'valid-semantic-fixture.json'
        fixtures.write(analysis_path,analysis)
        pipeline.run_accounting(transport.source,root=ROOT,routing_path=transport.source/'routing.json',
            corrections_path=transport.source/'review-corrections.jsonl',analysis_fixture=analysis_path)
        transport.seal()
        result_doc=review.build_result(transport.source,{'status':'pass'},{})
        completion=json.loads(transport.bundle_path.read_text())
        result_doc.update(completion_bundle=completion,completion_bundle_digest=completion['bundle_digest'])
        fixtures.write(transport.source/'autopilot-result.json',result_doc)
        source=cycle._validate_stage(config,transport.source/'autopilot-result.json','2026-09-01','2026-10-01',
            replay=False,expected_snapshot_digests=source['snapshot_digests'])
        record={'source':source,'expected_snapshot_digests':source['snapshot_digests']}
        state={'slices':{'2026-09-01':record}}
        with mock.patch.object(cycle,'_run_budgeted_child',side_effect=InterruptedError('before launch')):
            with self.assertRaises(InterruptedError):
                cycle._adopt_native_credit(config,state,transport.root/'state/state.json',record,
                    '2026-09-01','2026-10-01',source,[120])
        child=Path(record['native_credit_transition']['child_run_dir'])
        env=dict(os.environ);env.pop('PYTHONPATH',None);env['PYTHONDONTWRITEBYTECODE']='1'
        result=subprocess.run(['/usr/bin/python3','-B',str(ROOT/'scripts/clockify_review_run.py'),
            '--resume-from',str(child),'--runs-root',str(transport.runs),
            '--state',str(transport.root/'review-state.json')],cwd='/',env=env,
            text=True,capture_output=True,timeout=30)
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        accounting=json.loads((child/'work-accounting-result.json').read_text())
        self.assertEqual([837],[row['verified_posted_credit']['covered_seconds'] for row in accounting['skipped']])
        self.assertEqual([4],[row['duration_seconds'] for row in accounting['proposals']])

    def test_standalone_scoped_cached_repair_and_replay(self):
        # Scoped recovery's lazy imports must survive both actual child launches.
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            runs,_source,_digest,_output,argv=completed_recovery_fixture(root)
            env=dict(os.environ);env.pop('PYTHONPATH',None);env['PYTHONDONTWRITEBYTECODE']='1'
            command=['/usr/bin/python3','-B',str(ROOT/'scripts/clockify_review_run.py')]
            repaired=subprocess.run(command+argv,cwd='/',env=env,text=True,capture_output=True,timeout=30)
            self.assertEqual(0,repaired.returncode,repaired.stdout+repaired.stderr)
            child=next(runs.glob('*-repair-*'))
            self.assertTrue((child/'completion-bundle.json').is_file())
            self.assertEqual(2,len(json.loads((child/'semantic-analysis.json').read_text())['activities']))
            replayed=subprocess.run(command+['--replay-from',str(child),'--runs-root',str(runs),
                '--state',str(root/'replay-state.json')],cwd='/',env=env,text=True,capture_output=True,timeout=30)
            self.assertEqual(0,replayed.returncode,replayed.stdout+replayed.stderr)

    def standalone(self, body):
        # Match Python's direct-script search path, not this test runner's path.
        code = (
            "import runpy,sys,pathlib\n"
            f"script=pathlib.Path({str(ROOT / 'scripts/clockify_review_cycle.py')!r})\n"
            "sys.path[0]=str(script.parent)\n"
            "cycle=runpy.run_path(str(script))\n" + body
        )
        env = dict(os.environ)
        env.pop('PYTHONPATH', None)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        return subprocess.run(['/usr/bin/python3', '-B', '-c', code], cwd='/', env=env,
                              text=True, capture_output=True, timeout=30)

    def test_no_credit_is_noop_under_service_import_conditions(self):
        # Lazy package imports must not break even when credit is disabled.
        result = self.standalone(
            "source={'run_id':'unchanged'}\n"
            "assert cycle['_adopt_native_credit']({}, {}, pathlib.Path('/unused'), {}, "
            "'2026-09-01', '2026-09-03', source, [1.0]) == source\n"
        )
        self.assertEqual(0, result.returncode, result.stderr)

    def test_native_plan_resolves_nested_package_imports_under_service_conditions(self):
        # Use actual sealed finite proof, including adoptions' lazy imports.
        fixture = fixtures.CycleNativeCreditProducerTests()
        self.addCleanup(fixture.doCleanups)
        _transport, config, source, *_ = fixture.fixture()
        result = self.standalone(
            f"config={config!r}\nsource={source!r}\n"
            "plan=cycle['_native_credit_plan'](config,source)\n"
            "assert len(plan['credits']) == 1, plan\n"
            "print('verified-native-plan')\n"
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual('verified-native-plan', result.stdout.strip())


if __name__ == '__main__':
    unittest.main()
