"""Pending raw provenance must not masquerade as old downstream completion."""
import argparse
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import subprocess
import sys
import os
import shutil
from unittest import mock

from scripts import clockify_review_run as review, clockify_sync_collect as collector
from scripts import collector_slices, collector_checkpoints, reconciliation_manifest, semantic_analyzer
from scripts import clockify_review_cycle as cycle, collector_receipts, source_coverage
from scripts.autopilot_process import ChildResult
from ops.systemd.user import clockify_review_cycle_release as release_helper

SINCE = dt.datetime(2026, 8, 31, 21, tzinfo=dt.timezone.utc)
UNTIL = SINCE + dt.timedelta(days=1)
ACTOR_ROUTING = {'clockify_user_id':'user-one', 'member_identities':['member@example.invalid'],
    'workspace_id':'workspace-one','member_id':'user-one',
    'session_routes':[], 'meeting_routes':[], 'semantic_actor_contract':'clockify-semantic-actors/v1',
    'semantic_subject_binding':{'source_type':'multica', 'server_origin':'https://multica.example.invalid',
        'workspace_id':'workspace-one','author_id':'member-one'}}

def inventory(path):
    return {str(p.relative_to(path)):p.read_bytes() for p in path.rglob('*') if p.is_file()}

def write_empty_actor_fixture(path):
    path.write_text(json.dumps({'schema_version':1,'activities':[],'exceptions':[],'omissions':[],
        'actor_contract':'clockify-semantic-actors/v1','prompt_version':'clockify-semantic-v18',
        'evidence_bundle_schema_version':'clockify-semantic-evidence-bundle/v2',
        'evidence_bundle_manifest':{'schema_version':'clockify-semantic-evidence-bundle/v2',
            'digest':semantic_analyzer.stable_digest('sebm-',[],length=64),'bundles':[]},
        'ledger_event_count':0,'ledger_evidence_digest':semantic_analyzer.stable_digest('led-',[]),
        'analysis_chunks':[{'model':'offline-fixture','tier':'fixture'}]})+'\n')

def pending_fixture(root, *, retain_native=False, multica=None):
    runs = root/'runs'; runs.mkdir(exist_ok=True)
    checkpoint_root = root/'checkpoints'
    identity = collector_slices.BacklogIdentity(collector.iso_utc(SINCE),collector.iso_utc(UNTIL),
        'Europe/Bucharest',2,'collector-evidence-compatibility/v2:fixture')
    plan = collector.plan_slices(SINCE,UNTIL,zone=collector.BUCHAREST)
    backlog = collector_slices.BacklogStore(checkpoint_root).open(identity,plan)
    checkpoint_root.chmod(0o700)
    pages = collector_checkpoints.PageCheckpointStore(backlog.directory/'source-checkpoints')
    ci = collector._clockify_checkpoint_identity('workspace-one','user-one',SINCE,UNTIL)
    state = pages.open(ci,initial_metadata={'snapshot_at':'2026-09-03T00:00:00Z'})
    state = pages.append_page(state,payload=[],continuation={'page':2},signature=collector._clockify_page_signature([]))
    pages.mark_complete(state)
    source = collector._slice_run_dir(plan[0],identity.compatibility_version).name
    source = runs/source
    with mock.patch.object(collector,'collector_runtime_identity',return_value={'git_sha':'original'}), \
         mock.patch.object(collector,'clockify_get',side_effect=AssertionError('network forbidden')), \
         mock.patch.object(collector,'fetch_fathom',return_value={'status':'ok','complete':True,'meetings':[]}), \
         mock.patch.object(collector,'fetch_multica_issues',return_value=multica or {'status':'ok','complete':True,'issues':[]}):
        collector._collect_slice(argparse.Namespace(calendly_optional=True,enrich=False),
            {'clockify_user_id':'user-one','member_identities':['member@example.invalid']},{'machines':[]},
            {'CLOCKIFY_WORKSPACE_ID':'workspace-one'},{},SINCE,UNTIL,'explicit --since',pages,source,
            coordinator='omarchy-precision')
    # Historical sources lacked the new copied native proof; the actual original
    # checkpoint stays available, complete and bound to sanitized evidence.
    if not retain_native:
        shutil.rmtree(source/'evidence/clockify-native-checkpoint')
        report = json.loads((source/'run-report.json').read_bytes())
        report.pop('clockify_native_checkpoint')
        (source/'run-report.json').write_text(json.dumps(report)+'\n')
    collector._write_pending_slice_finalization(source,identity,plan[0])
    period = reconciliation_manifest.PeriodIdentity('user-one','workspace-one','Europe/Bucharest',SINCE,UNTIL,1)
    unsigned={'schema_version':reconciliation_manifest.MANIFEST_COMPATIBILITY_VERSION,
        'compatibility_version':reconciliation_manifest.MANIFEST_COMPATIBILITY_VERSION,'period':period.document(),
        'state':'collecting','event_count':1,'events_digest':'sha256:'+'0'*64,'artifacts':[],'blockers':[]}
    (source/'period-manifest.json').write_text(json.dumps({**unsigned,'manifest_digest':reconciliation_manifest._digest(unsigned)})+'\n')
    (source/'routing.json').write_text(json.dumps({'session_routes':[],'meeting_routes':[]})+'\n')
    (source/'review-corrections.jsonl').write_text('')
    (source/'review-acceptance.jsonl').write_text('')
    routing = root/'actor-routing.json'; routing.write_text(json.dumps(ACTOR_ROUTING)+'\n')
    snapshots={n:source/n for n in review._RECONCILIATION_INPUTS.values()}
    snapshots['routing.json']=routing
    return runs,source,checkpoint_root,snapshots

class PendingDerivationTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.runs,self.source,self.checkpoints,self.snapshots=pending_fixture(self.root)
        self.executor={'git_sha':'045-fixture','git_dirty':False}
        self.context=mock.patch.multiple(review,RUNS=self.runs)
        self.context.start(); self.addCleanup(self.context.stop)
        self.cp=mock.patch.object(collector,'collector_checkpoint_root',return_value=self.checkpoints)
        self.cp.start(); self.addCleanup(self.cp.stop)

    def prepare(self):
        return review._prepare_collector_derivation_run(self.source,self.snapshots,
            executor_runtime_identity=self.executor,environment={},pending_checkpoint_root=self.checkpoints)

    def cycle_config(self):
        return {'root':str(self.root),'runs_dir':str(self.runs),'state_dir':str(self.root),
            'cache':str(self.root/'cache.jsonl'),'routing':str(self.snapshots['routing.json']),
            'corrections':str(self.source/'review-corrections.jsonl'),
            'acceptance':str(self.source/'review-acceptance.jsonl'),
            'timezone':'Europe/Bucharest','member_id':'user-one','workspace_id':'workspace-one',
            '_runtime_identity':self.executor}

    def test_admitted_child_rejects_original_and_native_byte_drift(self):
        child=self.prepare()
        original_paths=[self.source/name for name in (
            'run-report.json','run-report.md','slice-finalization.json','period-manifest.json',
            'routing.json','review-corrections.jsonl','review-acceptance.jsonl',
            'evidence/evidence-ledger.json','evidence/sessions.json','evidence/clockify-existing.json',
            'evidence/fathom-meetings.json','evidence/calendly-recordings.json','evidence/multica-issues.json')]
        original_paths += list(self.checkpoints.rglob('manifest.json'))+list(self.checkpoints.rglob('backlog-manifest.json'))+list(self.checkpoints.rglob('pages/*.json'))
        for path in original_paths:
            with self.subTest(path=str(path)):
                before=path.read_bytes()
                try:
                    path.write_bytes(before+b' ')
                    with self.assertRaises(ValueError):
                        review._verified_collector_derivation(child)
                finally:
                    path.write_bytes(before)
        review._verified_collector_derivation(child)

    def test_actor_runtime_and_lineage_drift_never_create_second_child(self):
        child=self.prepare(); children=sorted(self.runs.glob('collector-derivation-*'))
        before=self.snapshots['routing.json'].read_bytes()
        try:
            self.snapshots['routing.json'].write_text(json.dumps({**ACTOR_ROUTING,
                'semantic_subject_binding':{**ACTOR_ROUTING['semantic_subject_binding'],'author_id':'other-member'}}))
            with self.assertRaises(ValueError): self.prepare()
        finally:
            self.snapshots['routing.json'].write_bytes(before)
        self.executor['git_sha']='different-runtime'
        with self.assertRaises(ValueError): self.prepare()
        self.executor['git_sha']='045-fixture'
        self.assertEqual(children,sorted(self.runs.glob('collector-derivation-*')))
        (child/'collector-source.json').write_bytes((child/'collector-source.json').read_bytes()+b' ')
        # JSON whitespace is not semantic lineage drift; changed identity is.
        lineage=json.loads((child/'collector-source.json').read_bytes()); lineage['source_run_id']='wrong-source'
        (child/'collector-source.json').write_text(json.dumps(lineage))
        with self.assertRaises(ValueError): review._verified_collector_derivation(child)

    def test_invalid_actor_subject_binding_fails_closed_before_child_creation(self):
        for binding in ({'author_id':'member-one'},
            {**ACTOR_ROUTING['semantic_subject_binding'],'source_type':'clockify'}):
            with self.subTest(binding=binding):
                self.snapshots['routing.json'].write_text(json.dumps({**ACTOR_ROUTING,'semantic_subject_binding':binding}))
                with self.assertRaises(ValueError): self.prepare()
        self.assertEqual([],list(self.runs.glob('collector-derivation-*')))

    def test_sealed_invalid_and_partial_sources_cannot_enter_pending_bridge(self):
        seal=self.source/'completion-bundle.json'; seal.write_text('{}')
        with self.assertRaises(ValueError): self.prepare()
        with self.assertRaises(ValueError): collector_receipts.load_pending_collector_source(self.source,checkpoint_root=self.checkpoints)
        config=self.cycle_config(); record={'expected_snapshot_digests':{n:cycle._digest(self.source/n) for n in self.snapshots}}
        with mock.patch.dict(os.environ,{'CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT':str(self.checkpoints)}):
            self.assertIsNone(cycle._prepare_pending_derivation_launch(config,record,'2026-09-01','2026-09-02'))
        seal.unlink()
        path=self.source/'evidence/evidence-ledger.json'; ledger=json.loads(path.read_bytes())
        ledger['manifest']['source_completeness']['status']='incomplete'
        ledger['manifest']['source_completeness']['incomplete_sources']=['fathom']
        path.write_text(json.dumps(ledger))
        with self.assertRaises(ValueError): self.prepare()
        with mock.patch.dict(os.environ,{'CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT':str(self.checkpoints)}):
            self.assertIsNone(cycle._prepare_pending_derivation_launch(config,record,'2026-09-01','2026-09-02'))
        self.assertEqual([],list(self.runs.glob('collector-derivation-*')))

    def test_cycle_rejects_multiple_exact_complete_sources_without_child(self):
        duplicate=self.source.with_name(self.source.name+'-duplicate'); shutil.copytree(self.source,duplicate)
        report=json.loads((duplicate/'run-report.json').read_bytes()); report['run_id']=duplicate.name
        (duplicate/'run-report.json').write_text(json.dumps(report))
        (duplicate/'run-report.md').write_text((duplicate/'run-report.md').read_text().replace(self.source.name,duplicate.name))
        config=self.cycle_config(); record={'expected_snapshot_digests':{n:cycle._digest(self.source/n) for n in self.snapshots}}
        with mock.patch.dict(os.environ,{'CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT':str(self.checkpoints)}):
            with self.assertRaisesRegex(cycle.CycleError,'ambiguous'):
                cycle._prepare_pending_derivation_launch(config,record,'2026-09-01','2026-09-02')
        self.assertEqual([],list(self.runs.glob('collector-derivation-*')))

    def test_cycle_persists_exact_pending_launch_and_resumes_same_child_after_interrupt(self):
        """Exercises real release validation and state persistence before the process boundary."""
        config={**self.cycle_config(),'monthly_sheet_title_template':'{month_name} {year} portfolio review'}
        manifest=cycle._ensure_period(config,self.root,'2026-09-01','2026-09-02',bind_inputs=True)
        shutil.copyfile(manifest,self.source/'period-manifest.json')
        release=self.root/'releases'/('a'*40); release.mkdir(parents=True)
        shutil.copyfile(self.snapshots['routing.json'],release/'routing.json')
        release_helper._make_payload_read_only(release); release.chmod(0o555)
        tree=release_helper._tree_manifest(release)
        identity={'schema_version':'clockify-user-release/v1','git_sha':'a'*40,'root':str(release),
            'tree_manifest':tree,'tree_digest':release_helper._manifest_digest(tree),
            'routing_sha256':hashlib.sha256((release/'routing.json').read_bytes()).hexdigest()}
        release.chmod(0o755); (release/'.clockify-release.json').write_text(json.dumps(identity)+'\n')
        (release/'.clockify-release.json').chmod(0o444); release.chmod(0o555)
        runtime={'canonical_root':str(release),'git_sha':None,'git_dirty':None}
        config.update(root=str(release),routing=str(release/'routing.json'),_runtime_identity=runtime)
        record={'expected_snapshot_digests':{n:cycle._digest(self.source/n) for n in self.snapshots}}
        interval=cycle._generic_interval(config,'2026-09-01','2026-09-02')
        old=cycle._source_attempt(record,['old-unbound-command'],interval,advance_frontier=True)
        cycle._finish_attempt(record,old)
        initial_record=copy.deepcopy(record)
        original_attempt=copy.deepcopy(record['source_attempt']); original_inputs=copy.deepcopy(record['expected_snapshot_digests'])
        state=cycle._state(self.root/'absent.json',recovery_since='2026-09-01'); state['slices']['2026-09-01']=record
        state_path=self.root/'review-cycle-state.json'
        debts=source_coverage.SourceDebtStore()
        failure=debts.record_failure(interval,failure_class='result_unverified',retryable=True,
            resume_state_digest=old['resume_state_digest'],attempted_at='2026-09-03T00:00:00Z')
        debts.exhaust(failure.debt_id,terminal_reason='retry_limit')
        generic=debts.active()[0]; commands=[]
        before=inventory(self.source); checkpoints_before=inventory(self.checkpoints)
        def stopped(command,**kwargs):
            saved=json.loads(state_path.read_bytes())['slices']['2026-09-01']
            launch=saved['pending_derivation_binding']; commands.append(command)
            self.assertEqual(command,launch['command'])
            self.assertEqual(launch['command_digest'],saved['source_attempt']['command_digest'])
            self.assertEqual(saved['source_attempt']['ordinal'],saved['runner_attempt']['source_attempt_ordinal'])
            self.assertEqual(cycle._value_digest(runtime),saved['runner_attempt']['runtime_identity_digest'])
            self.assertEqual(original_inputs,saved['expected_snapshot_digests'])
            self.assertEqual(original_attempt,saved['source_attempt_history'][0]['source_attempt'])
            self.assertEqual(launch['snapshot_digests'],saved['fresh_input_binding']['snapshot_digests'])
            self.assertTrue((Path(launch['child_run_dir'])/'collector-source.json').is_file())
            self.assertIn('--resume-from',command); self.assertNotIn('--since',command)
            if len(commands)==2:
                return ChildResult(75,'','synthetic interruption',True,0.1)
            raise cycle._BudgetExhausted('synthetic pre-launch budget stop')
        with mock.patch.dict(os.environ,{'CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT':str(self.checkpoints)}), \
             mock.patch.object(cycle,'_run_budgeted_child',side_effect=stopped), \
             mock.patch.object(collector,'_collect_slice',side_effect=AssertionError('recollection forbidden')):
            for _ in range(4):
                result=cycle._run_slice(config,state,state_path,self.root,release,'2026-09-01','2026-09-02',
                    debts,self.root/'source-coverage.json',generic=generic,advance_frontier=True,budget=[3600])
                self.assertEqual('incomplete',result['status'])
        self.assertEqual(commands[0],commands[1]); self.assertEqual(commands[1],commands[2])
        self.assertEqual(commands[2],commands[3])
        saved=json.loads(state_path.read_bytes())['slices']['2026-09-01']
        self.assertEqual(3,saved['source_attempt']['ordinal'])
        self.assertEqual(2,len(saved['source_attempt_history']))
        self.assertEqual(2,saved['source_attempt_history'][1]['source_attempt']['ordinal'])
        self.assertEqual('finished',saved['source_attempt_history'][1]['source_attempt']['status'])
        self.assertEqual(1,len(list(self.runs.glob('collector-derivation-*'))))
        self.assertEqual(before,inventory(self.source)); self.assertEqual(checkpoints_before,inventory(self.checkpoints))
        fixture=self.root/'completed-analysis.json'; write_empty_actor_fixture(fixture)
        with mock.patch.object(collector,'collector_runtime_identity',return_value=runtime):
            self.assertEqual(0,review.main(['--runs-root',str(self.runs),'--derive-pending-from',str(self.source),
                '--routing',config['routing'],'--analysis-fixture',str(fixture),'--state',str(self.root/'review-state.json')]))
        state['slices']['2026-09-01']=initial_record
        def replay_only(command,**kwargs):
            self.assertIn('--replay-from',command)
            saved=json.loads(state_path.read_bytes())['slices']['2026-09-01']
            self.assertEqual(saved['pending_derivation_binding']['child_run_dir'],saved['source']['run_dir'])
            raise cycle._BudgetExhausted('stop before replay')
        with mock.patch.dict(os.environ,{'CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT':str(self.checkpoints)}), \
             mock.patch.object(cycle,'_run_budgeted_child',side_effect=replay_only):
            cycle._run_slice(config,state,state_path,self.root,release,'2026-09-01','2026-09-02',
                debts,self.root/'source-coverage.json',generic=generic,advance_frontier=True,budget=[3600])

    def test_existing_native_proof_is_preserved_and_verified_on_pending_admission(self):
        modern=self.root/'modern'; modern.mkdir()
        runs,source,checkpoints,snapshots=pending_fixture(modern,retain_native=True)
        before=inventory(source); original_metadata=json.loads((source/'run-report.json').read_bytes())['clockify_native_checkpoint']
        with mock.patch.object(review,'RUNS',runs):
            child=review._prepare_collector_derivation_run(source,snapshots,
                executor_runtime_identity=self.executor,pending_checkpoint_root=checkpoints)
            review._verified_collector_derivation(child)
            self.assertEqual(original_metadata,json.loads((child/'run-report.json').read_bytes())['clockify_native_checkpoint'])
            self.assertEqual(inventory(source/'evidence/clockify-native-checkpoint'),inventory(child/'evidence/clockify-native-checkpoint'))
            proof=source/'evidence/clockify-native-checkpoint/snapshot.json'; proof.write_bytes(proof.read_bytes()+b' ')
            with self.assertRaises(ValueError): review._verified_collector_derivation(child)
        self.assertEqual({k:v for k,v in before.items() if k!='evidence/clockify-native-checkpoint/snapshot.json'},
            {k:v for k,v in inventory(source).items() if k!='evidence/clockify-native-checkpoint/snapshot.json'})

    def test_child_uses_new_actor_request_identity_without_changing_author_provenance(self):
        authored=self.root/'authored'; authored.mkdir()
        multica={'status':'ok','complete':True,'issues':[],
            'source_version':'multica-issues-with-comment-history/v1','comments':[
            {'id':f'comment-{index}','issue_id':'issue-one','created_at':'2026-09-01T09:00:00Z',
             'content':'Completed implementation','author_id':author,'author_type':kind,
             'workspace_id':'workspace-one','server_origin':'https://multica.example.invalid'}
            for index,(author,kind) in enumerate([('member-one','member'),('member-two','member'),('agent-one','agent')])]}
        runs,source,checkpoints,snapshots=pending_fixture(authored,multica=multica)
        before=inventory(source)
        with mock.patch.object(review,'RUNS',runs):
            child=review._prepare_collector_derivation_run(source,snapshots,
                executor_runtime_identity=self.executor,pending_checkpoint_root=checkpoints)
        rows=json.loads((child/'evidence/evidence-ledger.json').read_bytes())['events']
        self.assertEqual(['agent-one','member-one','member-two'],sorted(row['attributes']['author_id'] for row in rows))
        legacy=semantic_analyzer._body_for(rows,model='fixture',mode='extract',private_text_approved=True)
        annotated=semantic_analyzer.with_actor_context(rows,
            subject_binding=json.loads((child/'routing.json').read_bytes())['semantic_subject_binding'])
        current=semantic_analyzer._body_for(annotated,model='fixture',mode='extract',private_text_approved=True)
        payload=json.loads(current['messages'][1]['content'])
        members=[member for bundle in payload['bundles'] for member in bundle['members']]
        self.assertEqual('clockify-semantic-v18',payload['prompt_version'])
        self.assertEqual('clockify-semantic-actors/v1',payload['actor_contract'])
        self.assertEqual(['other','other','subject'],sorted(member['actor']['subject_relation'] for member in members))
        self.assertEqual(['agent','human_member','human_member'],sorted(member['actor']['kind'] for member in members))
        self.assertNotEqual(cycle._value_digest(legacy),cycle._value_digest(current))
        self.assertEqual(before,inventory(source))

    def test_complete_pending_source_derives_actor_child_without_old_completion(self):
        """Catches refusing actual complete raw proof just because old accounting never sealed."""
        before=inventory(self.source); checkpoints_before=inventory(self.checkpoints)
        with mock.patch.object(collector,'clockify_get',side_effect=AssertionError('network forbidden')), \
             mock.patch.object(collector,'_collect_slice',side_effect=AssertionError('collector forbidden')), \
             mock.patch.object(semantic_analyzer,'http_transport',side_effect=AssertionError('provider forbidden')):
            try:
                child=self.prepare()
            except ValueError as error:
                self.fail(f'complete pending source must derive without old completion: {error}')
            ancestor,identity,lineage=review._verified_collector_derivation(child)
            self.assertEqual(self.source,ancestor)
            self.assertEqual(child,self.prepare())
        self.assertNotEqual(self.source,child)
        self.assertEqual(ACTOR_ROUTING,json.loads((child/'routing.json').read_bytes()))
        self.assertTrue((child/'evidence/clockify-native-checkpoint/snapshot.json').is_file())
        self.assertFalse((self.source/'completion-bundle.json').exists())
        self.assertEqual(before,inventory(self.source))
        self.assertEqual(checkpoints_before,inventory(self.checkpoints))
        self.assertEqual('collector-derivation/pending-v1',lineage['schema_version'])

    def test_standalone_pending_cli_finalizes_only_child_and_replays_offline(self):
        """Catches admission accidentally entering normal collectors or fake old finalization."""
        before=inventory(self.source); checkpoint_before=inventory(self.checkpoints)
        fixture=self.root/'analysis.json'
        write_empty_actor_fixture(fixture)
        env={**os.environ,'CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT':str(self.checkpoints),
            'PYTHONDONTWRITEBYTECODE':'1','CLOCKIFY_ANALYZER_PRIMARY_URL':'http://127.0.0.1:1/forbidden'}
        result=subprocess.run([sys.executable,'-B',str(Path(review.__file__)),
            '--runs-root',str(self.runs),'--derive-pending-from',str(self.source),
            '--routing',str(self.snapshots['routing.json']),'--analysis-fixture',str(fixture),
            '--state',str(self.root/'review-state.json')],env=env,capture_output=True,text=True)
        self.assertEqual(0,result.returncode,result.stderr+result.stdout)
        result_path=Path(result.stdout.strip().splitlines()[-1]); child=result_path.parent
        self.assertTrue((child/'completion-bundle.json').is_file())
        self.assertFalse((self.source/'completion-bundle.json').exists())
        config={'runs_dir':str(self.runs),'timezone':'Europe/Bucharest',
            'member_id':'user-one','workspace_id':'workspace-one'}
        stage=cycle._validate_collector_source_stage(config,child/'autopilot-result.json',
            '2026-09-01','2026-09-02',expected_snapshot_digests={n:cycle._digest(child/n) for n in self.snapshots})
        audit_identity,inventory_details,digest,optional=cycle._audit_bundle(stage)
        self.assertEqual(stage['bundle_digest'],digest)
        self.assertEqual(stage['slice_id'],audit_identity['slice_id'])
        self.assertIsNone(stage['legacy_completion_bundle_digest'])
        executor_stage=cycle._validate_stage(config,child/'autopilot-result.json','2026-09-01','2026-09-02',
            replay=False,expected_snapshot_digests=stage['snapshot_digests'])
        interval=cycle._interval_from_derived_stage(config,'2026-09-01','2026-09-02',executor_stage,
            {'kind':'collector_derivation','derivation_run_dir':str(child),
             'lineage_digest':cycle._digest(child/'collector-source.json')})
        self.assertEqual(stage['slice_id'],interval.slice_id)
        replay=subprocess.run([sys.executable,'-B',str(Path(review.__file__)),
            '--runs-root',str(self.runs),'--replay-from',str(child),
            '--state',str(self.root/'review-state.json')],env=env,capture_output=True,text=True)
        replay_path=Path(replay.stdout.strip().splitlines()[-1])
        self.assertEqual(0,replay.returncode,replay.stderr+replay.stdout+replay_path.read_text())
        self.assertEqual('pass',json.loads((Path(replay.stdout.strip().splitlines()[-1]).parent/'replay-integrity.json').read_bytes())['status'])
        self.assertEqual(before,inventory(self.source))
        self.assertEqual(checkpoint_before,inventory(self.checkpoints))

    def test_cycle_binds_unique_pending_child_and_actual_resume_command(self):
        """Catches launching recollection/guessing an old failed command for retained raw."""
        config={'root':str(self.root),'runs_dir':str(self.runs),'state_dir':str(self.root),
            'cache':str(self.root/'cache.jsonl'),'routing':str(self.snapshots['routing.json']),
            'timezone':'Europe/Bucharest','member_id':'user-one','workspace_id':'workspace-one',
            '_runtime_identity':self.executor}
        record={'expected_snapshot_digests':{n:cycle._digest(self.source/n) for n in self.snapshots}}
        with mock.patch.dict(os.environ,{'CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT':str(self.checkpoints)}):
            launch=cycle._prepare_pending_derivation_launch(config,record,'2026-09-01','2026-09-02')
            self.assertEqual(launch,cycle._prepare_pending_derivation_launch(config,record,'2026-09-01','2026-09-02'))
        self.assertEqual(str(self.source),launch['source_run_dir'])
        self.assertEqual(launch['child_run_dir'],launch['command'][launch['command'].index('--resume-from')+1])
        self.assertNotIn('--since',launch['command'])
        self.assertEqual(cycle._value_digest(launch['command']),launch['command_digest'])
        self.assertEqual(launch,record['pending_derivation_binding'])
        self.assertFalse((self.source/'completion-bundle.json').exists())
        for mutation in ('runtime','frozen-input','checkpoint-root','command'):
            changed=copy.deepcopy(record); changed_config=copy.deepcopy(config)
            environment={'CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT':str(self.checkpoints)}
            if mutation=='runtime': changed_config['_runtime_identity']['git_sha']='different-runtime'
            elif mutation=='frozen-input': changed['expected_snapshot_digests']['review-corrections.jsonl']='sha256:'+'0'*64
            elif mutation=='checkpoint-root': environment['CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT']=str(self.root/'different-checkpoints')
            else: changed['pending_derivation_binding']['command'].append('--since')
            with self.subTest(mutation=mutation),mock.patch.dict(os.environ,environment):
                with self.assertRaises(cycle.CycleError):
                    cycle._prepare_pending_derivation_launch(changed_config,changed,'2026-09-01','2026-09-02')

if __name__=='__main__': unittest.main()
