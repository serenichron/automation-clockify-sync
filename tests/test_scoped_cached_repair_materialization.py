"""Cache-only scoped repair must preserve source ancestry and never infer."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import clockify_review_run as review
from scripts import clockify_review_cycle as cycle
from scripts import clockify_scoped_semantic_recovery as scoped
from scripts import evidence_ledger
from scripts import semantic_analyzer as semantic
from scripts import work_accounting_pipeline as pipeline
from test_scoped_semantic_recovery import fixture
from test_semantic_analyzer import provider_members, provider_response
import test_review_run as review_tests

ROOT=Path(__file__).resolve().parents[1]

def write(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(value,sort_keys=True)+'\n')

def recovery_fixture(root):
    source=root/'source';source.mkdir()
    primary,document,events,cache,contexts,_residuals,targets=fixture(source)
    primary=semantic.AnalyzerEndpoint('clockify_analyzer_primary',primary.url,primary.model,revision=primary.revision)
    cache.path.write_bytes(b'')
    cache=semantic.AnalyzerResponseCache(cache.path,record_review_diagnostics=True)
    cache.store_rejected(primary,{'source':'sealed'},failure_code='contract_rejected_duplicate_evidence')
    document['analyzer_cache']={**cache.summary(),'snapshot':{'path':cache.path.name,'record_count':1,'sha256':hashlib.sha256(cache.path.read_bytes()).hexdigest()}}
    objects=tuple(evidence_ledger.EvidenceEvent.from_document(event) for event in events)
    ledger=evidence_ledger.EvidenceLedger(objects,timezone='Europe/Bucharest',member_identities=('member@example.com',))
    write(source/'evidence/evidence-ledger.json',{'schema_version':evidence_ledger.SCHEMA_VERSION,'events':events,'manifest':ledger.manifest.document()})
    write(source/'semantic-analysis.json',document)
    write(source/'routing.json',{'session_routes':[{'pattern':'fixture','project_name':'Serenichron Level 2','prefix':'SC','tag_names':['Processes']}],'meeting_routes':[]})
    write(source/'proposals.json',[{'id':'P001','start':'2026-09-14T08:00:00+03:00','end':'2026-09-14T08:10:00+03:00'}])
    scope=root/'scope.json';write(scope,{'evidence_ids':sum(contexts,[])})
    digests=sorted(semantic.stable_digest('frt-',list(key),length=64) for key in targets)
    argv=[str(source),'--scope-file',str(scope),'--output-dir',str(root/'recovery')]
    for digest in digests:argv.extend(['--failed-review-digest',digest])
    #21 independently cited recovered activities across6 whole contexts.
    def transport(_endpoint,body):
        payload=json.loads(body['messages'][-1]['content']);members=provider_members(payload)
        counts={4:3,5:3,9:3,18:4,20:4,34:4};count=counts[len(members)]
        activities=[]
        for index in range(count):
            response=provider_response(payload,members[index::count])
            activity=response['activities'][0];activity['object']=f'Independent outcome {index}'
            activities.append(activity)
        return {'activities':activities,'exceptions':[],'omissions':[]}
    with mock.patch.object(semantic.AnalyzerEndpoint,'from_env',return_value=primary), mock.patch.object(semantic,'http_transport',side_effect=transport), mock.patch.dict('os.environ',{'CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED':'approved'}):
        scoped.run(scoped.parse_args(argv))
    return source,root/'recovery',digests,primary


def completed_recovery_fixture(root):
    runs=root/'runs'
    source=review_tests.ReviewRunResultTests._write_real_offline_replay_source(runs,root,failed_review=True)
    document=json.loads((source/'semantic-analysis.json').read_text())
    failure=next(row for row in document['exceptions'] if row['kind']=='analyzer_review_failure')
    digest=semantic.stable_digest('frt-',sorted(failure['evidence_ids']),length=64)
    scope=root/'scope.json';write(scope,{'evidence_ids':failure['evidence_ids']})
    primary=semantic.AnalyzerResponseCache(source/'analyzer-cache-used.jsonl').sealed_endpoints()[0]
    output=root/'recovery'
    with mock.patch.object(semantic.AnalyzerEndpoint,'from_env',return_value=primary), mock.patch.object(semantic,'http_transport',side_effect=lambda _,body:provider_response(json.loads(body['messages'][-1]['content']))), mock.patch.dict('os.environ',{'CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED':'approved'}):
        scoped.run(scoped.parse_args([str(source),'--failed-review-digest',digest,'--scope-file',str(scope),'--output-dir',str(output)]))
    argv=['--repair-from',str(source),'--retry-failed-reviews','--retry-review-digest',digest,'--scoped-recovery-from',str(output),'--runs-root',str(runs),'--state',str(root/'items.json')]
    return runs,source,digest,output,argv

class ScopedCachedRepairTests(unittest.TestCase):
    def test_cycle_accepts_verified_scoped_child_and_rejects_changed_inputs(self):
        # Generic completion seals do not replace scoped recovery ancestry proof.
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);runs,source,digest,output,argv=completed_recovery_fixture(root)
            with mock.patch.object(semantic,'http_transport',side_effect=AssertionError('Must not infer')):
                self.assertEqual(0,review.main(argv))
            child=next(runs.glob('*-repair-*'))
            bundle=review.collector_receipts.load_completion_bundle(child/'completion-bundle.json',run_dir=child)
            config={'runs_dir':str(runs),'timezone':'UTC','member_id':'member-fixture','workspace_id':'workspace-fixture'}
            snapshots={name:cycle._digest(child/name) for name in ('period-manifest.json','routing.json','review-corrections.jsonl','review-acceptance.jsonl')}
            cycle._validate_stage(config,child/'autopilot-result.json','2026-08-01','2026-08-03',replay=False,expected_snapshot_digests=snapshots)
            ancestor,ancestor_bundle=cycle._collector_ancestor_from_repair(config,child,bundle)
            self.assertEqual(source,ancestor)
            self.assertEqual(source,ancestor_bundle.run_dir)
            (child/'scoped-recovery/source-scope-input.json').write_text('{}\n')
            with self.assertRaisesRegex(cycle.CycleError,'scoped'):
                cycle._collector_ancestor_from_repair(config,child,bundle)

    def test_validates_exact_six_cached_requests_without_changing_any_input(self):
        # Missing receipt validation or broader retry must not adopt arbitrary output.
        with tempfile.TemporaryDirectory() as temporary:
            source,recovery,digests,_=recovery_fixture(Path(temporary))
            before={str(p):p.read_bytes() for p in Path(temporary).rglob('*') if p.is_file()}
            validator=getattr(scoped,'validate_cached_recovery',None)
            self.assertIsNotNone(validator,'Cache-only scoped recovery validator is missing')
            with mock.patch.object(semantic,'http_transport',side_effect=AssertionError('Must not infer')):
                verified=validator(source,recovery,digests)
            self.assertEqual(90,len(verified['selected_evidence_ids']))
            self.assertEqual(6,verified['request_count'])
            self.assertEqual(26,len(verified['analysis']['activities']))
            self.assertEqual([13,47],sorted(verified['residual_event_counts']))
            self.assertEqual(before,{str(p):p.read_bytes() for p in Path(temporary).rglob('*') if p.is_file()})

    def test_rejects_scope_source_output_and_cache_tampering_without_transport(self):
        # Hash-only acceptance must not trust changed selected scope or semantic rows.
        with tempfile.TemporaryDirectory() as temporary:
            source,recovery,digests,_=recovery_fixture(Path(temporary))
            validator=getattr(scoped,'validate_cached_recovery',None)
            self.assertIsNotNone(validator,'Cache-only scoped recovery validator is missing')
            for filename in ('recovery-receipt.json','recovery-plan.json','source-scope-input.json','source-semantic-analysis.json','preserved-source-proposals.json','semantic-analysis.json','analyzer-response-cache.jsonl'):
                path=recovery/filename;original=path.read_bytes()
                with self.subTest(filename=filename):
                    path.write_bytes(b'{}\n')
                    with mock.patch.object(semantic,'http_transport',side_effect=AssertionError('Must not infer')), self.assertRaises((ValueError,pipeline.WorkAccountingError,semantic.AnalyzerError,KeyError)):
                        validator(source,recovery,digests)
                    path.write_bytes(original)

    def test_replay_reconstructs_selected_contexts_not_entire_failed_groups(self):
        # Omitting selected_evidence_ids gives wrong requests and cache misses.
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);source,recovery,digests,_=recovery_fixture(root)
            (source/'review-corrections.jsonl').write_text('')
            (source/'completion-bundle.json').write_text('{}\n')
            retry=root/'retry';retry.mkdir()
            source_semantic=source/'semantic-analysis.json';source_cache=source/'analyzer-cache-used.jsonl'
            write(retry/'repair-source.json',{'source_run_id':source.name,'source_completion_sha256':review._file_sha256(source/'completion-bundle.json',label='fixture'),'semantic_analysis_sha256':hashlib.sha256(source_semantic.read_bytes()).hexdigest(),'analyzer_cache_path':'analyzer-cache-used.jsonl','analyzer_cache_sha256':hashlib.sha256(source_cache.read_bytes()).hexdigest()})
            analysis=json.loads((recovery/'semantic-analysis.json').read_text())
            with mock.patch.object(review,'RUNS',root), mock.patch.object(semantic,'http_transport',side_effect=AssertionError('Must not infer')):
                records=review._preflight_replay_analyzer_cache(source,recovery/'analyzer-cache-used.jsonl',analysis,retry_origin=retry)
            self.assertEqual(analysis['analyzer_cache']['records'],records)
            analysis['failed_review_retry']['selected_scope_digest']='scope-'+'0'*64
            with mock.patch.object(review,'RUNS',root), self.assertRaisesRegex(ValueError,'scope'):
                review._preflight_replay_analyzer_cache(source,recovery/'analyzer-cache-used.jsonl',analysis,retry_origin=retry)

    def test_cache_only_repair_seals_and_resume_reuses_same_child_without_fixture_override(self):
        # Crash resume must not fall back to old fixture or fresh inference.
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);runs,source,digest,output,argv=completed_recovery_fixture(root)
            before={str(p):p.read_bytes() for p in source.rglob('*') if p.is_file()}
            try:
                review.parse_args(argv)
            except SystemExit:
                self.fail('Verified cache-only recovery repair CLI is missing')
            with mock.patch.object(review,'_process_run',side_effect=RuntimeError('simulated crash')), self.assertRaisesRegex(RuntimeError,'simulated crash'):
                review.main(argv)
            children=list(runs.glob('*-repair-*'));self.assertEqual(1,len(children));child=children[0]
            # Recovery external directory is no longer needed after immutable copy.
            (output/'semantic-analysis.json').write_text('{}\n')
            with mock.patch.object(semantic,'http_transport',side_effect=AssertionError('Must not infer')):
                self.assertEqual(0,review.main(['--resume-from',str(child),'--runs-root',str(runs),'--state',str(root/'items.json')]))
                self.assertEqual(0,review.main(['--replay-from',str(child),'--runs-root',str(runs),'--state',str(root/'replay-items.json')]))
            self.assertTrue((child/'completion-bundle.json').is_file())
            self.assertEqual(2,len(json.loads((child/'semantic-analysis.json').read_text())['activities']))
            self.assertEqual(before,{str(p):p.read_bytes() for p in source.rglob('*') if p.is_file()})

    def test_cache_miss_fails_closed_without_provider_or_cache_append(self):
        # A missing response must never become a new inference request.
        with tempfile.TemporaryDirectory() as temporary:
            source,recovery,digests,_=recovery_fixture(Path(temporary))
            cache=recovery/'analyzer-response-cache.jsonl'
            records=cache.read_bytes().splitlines(keepends=True)
            cache.write_bytes(b''.join(records[:-1]));before=cache.read_bytes()
            ledger,events=pipeline.load_ledger(source/'evidence/evidence-ledger.json')
            selected=json.loads((recovery/'source-scope-input.json').read_text())['evidence_ids']
            with mock.patch.object(semantic,'http_transport',side_effect=AssertionError('Must not infer')), self.assertRaisesRegex(pipeline.WorkAccountingError,'cache miss'):
                pipeline.analyze_ledger(events,analyzer_cache_path=cache,review_routing=json.loads((source/'routing.json').read_text()),review_taxonomy=[{'project_name':'Serenichron Level 2','prefix':'SC','tag_names':['Processes']}],failed_review_retry_source=source/'semantic-analysis.json',failed_review_retry_digest=digests,failed_review_retry_selected_evidence_ids=selected,failed_review_retry_cache_only=True,failed_review_retry_scoped_mode='scoped_review_v2')
            self.assertEqual(before,cache.read_bytes())

    def test_crash_after_accounting_resumes_cached_retry_not_original_fixture(self):
        # The mutable used-cache output must not replace the immutable source fixture.
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);runs,source,digest,output,argv=completed_recovery_fixture(root)
            with mock.patch.object(review,'_finalize_repair_completion',side_effect=RuntimeError('after accounting')), self.assertRaisesRegex(RuntimeError,'after accounting'):
                review.main(argv)
            child=next(runs.glob('*-repair-*'))
            self.assertEqual(2,len(json.loads((child/'semantic-analysis.json').read_text())['activities']))
            with mock.patch.object(semantic,'http_transport',side_effect=AssertionError('Must not infer')):
                self.assertEqual(0,review.main(['--resume-from',str(child),'--runs-root',str(runs),'--state',str(root/'items.json')]))
            self.assertEqual(1,len(list(runs.glob('*-repair-*'))))
            self.assertEqual(2,len(json.loads((child/'semantic-analysis.json').read_text())['activities']))

    def test_resume_rejects_stripped_scoped_binding_instead_of_old_fixture_fallback(self):
        # Losing the scoped binding must fail rather than report old work as recovered.
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);runs,source,digest,output,argv=completed_recovery_fixture(root)
            with mock.patch.object(review,'_process_run',side_effect=RuntimeError('before accounting')), self.assertRaises(RuntimeError):
                review.main(argv)
            child=next(runs.glob('*-repair-*'));lineage=child/'repair-source.json'
            document=json.loads(lineage.read_text());document.pop('scoped_recovery_artifacts');write(lineage,document)
            with mock.patch.object(semantic,'http_transport',side_effect=AssertionError('Must not infer')):
                self.assertEqual(2,review.main(['--resume-from',str(child),'--runs-root',str(runs),'--state',str(root/'items.json')]))
            self.assertFalse((child/'completion-bundle.json').exists())


if __name__=='__main__':unittest.main()
