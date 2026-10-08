"""Historical selection is coverage proof, never a new provider publication."""
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import unittest
from unittest import mock

from scripts import clockify_review_cycle as cycle, clockify_review_run as review
from scripts import collector_checkpoints, clockify_sync_collect as collector, source_coverage
from scripts import clockify_sheet_publish as publisher, review_corrections
from scripts import clockify_selected_delivery_adoption as selected, clockify_monthly_unresolved as monthly, evidence_ledger
from scripts import collector_receipts, clockify_source_adoptions
import test_cycle_historical_adoption as historical
from test_review_cycle_delivery import make_run, proposal, write_json


def captured(sheet_id, title, rows, start=0, header=None):
    cells = lambda row: {'values': [{'userEnteredValue': {'numberValue' if type(v) in (int,float) else 'stringValue': v}} for v in row]}
    data = [{'startRow': start, 'rowData': [cells(row) for row in rows]}]
    if header is not None:
        data.append({'startRow': 0, 'rowData': [cells(header)]})
    return {'structuredContent': {'spreadsheetId': 'sheet-1', 'sheets': [
        {'properties': {'sheetId': sheet_id, 'title': title}, 'data': data}]}}


class SelectedHistoricalDeliveryTests(unittest.TestCase):
    def setUp(self):
        fixture = historical.HistoricalAdoptionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root, self.config = fixture.root, fixture.config
        self.state_dir = fixture.state_dir
        self.since, self.until = fixture.since, fixture.until
        rows = []
        for i, minutes in enumerate([4]*11+[9]+[5]*21+[9]):
            start = dt.datetime(2026,9,7,9,tzinfo=dt.timezone.utc)+dt.timedelta(minutes=15*i)
            row = {**proposal(), 'id': f'P{i+1:03}', 'review_activity_key': f'wka-target-{i}',
                'activity_id': f'activity-{i}', 'start': start.isoformat(),
                'end': (start+dt.timedelta(minutes=minutes)).isoformat(),
                'duration_minutes': minutes, 'duration_seconds': 60*minutes,
                'clockify_project_suffix': '123456', 'tag_suffixes': ['01234567'],
                'provenance': {'evidence_ids': ['ev-source']}}
            rows.append(row)
        make_run(self.root, 'historical-source', replay=False, proposals=rows,
                 snapshot_overrides={'routing.json': json.loads((fixture.source_result.parent/'routing.json').read_text())})
        request, derived, collector_source = fixture._derived_adoption_request()
        request,derived=self.with_source_diagnostics(request,derived,collector_source)
        self.source = derived
        self.proposals = rows
        # Independent prior incomplete stage must survive supersession intact.
        previous = make_run(self.root, 'previous-incomplete', replay=False,
            snapshots_from=derived, coverage={'status':'incomplete','incomplete_sources':['sessions/macbook','repositories/macbook']},
            record_checkpoint=False)
        state_path = self.state_dir/'review-cycle-state.json'
        state = json.loads(state_path.read_text())
        prior = state['slices'][self.since]
        prior.update(status='recovery_blocked', source=cycle._validate_stage(self.config, previous, self.since, self.until,
            replay=False, expected_snapshot_digests=request['adopted_snapshot_digests']),
            source_attempt={'ordinal':2,'status':'finished'}, source_attempt_history=[{'legacy':'keep'}],
            recovery_parents={'peer/macbook': {'original_parent':'keep'}},
            fresh_input_binding={'old_binding':'keep'}, fresh_native_source_digest='sha256:'+'f'*64)
        self.prior = copy.deepcopy(prior)
        write_json(state_path,state)
        self.packet_dir = self.root/'publication'; self.packet_dir.mkdir()
        self.title = 'September 2026 portfolio review'
        self.selected = [publisher.proposal_row(p, derived.name) for p in rows[12:]]
        held, entries = [], []
        for i,p in enumerate(rows[:12]):
            entry = {'id':f'{i:024x}', 'workspaceId':'workspace-1','userId':'member-1',
                'projectId':'project-123456','tagIds':['tag-01234567'],'taskId':None,
                'billable':False,'description':f'Original reviewed accomplishment {i}',
                'timeInterval': {'start':p['start'],'end':p['end'],'duration':f'PT{p["duration_minutes"]}M'}}
            entries.append(entry)
            decision = {'activity_id':p['activity_id'],'evidence_fingerprint':review_corrections.proposal_target(p)[1],
                'recommendation':'hold_represented','automatic_credit_created':False,
                'native_entry_id':entry['id'],'native_original_index':i,
                'native_id_sha256':hashlib.sha256(entry['id'].encode()).hexdigest(),
                'native_description_sha256':hashlib.sha256(entry['description'].encode()).hexdigest(),
                'source_minutes':p['duration_minutes'],'represented_minutes':p['duration_minutes'],
                'native_fully_covers_source_interval':True,'effective_routing_matches':True,
                'rationale':f'Reviewed exact source target {i}.'}
            held.append({'proposal_id':p['id'],'activity_id':p['activity_id'],'minutes':p['duration_minutes'],
                         'decision':'hold_represented','native_decision':decision})
        self.packet = {'schema_version':'verified-review-publication-supplement/v2','source_run':str(derived),
            'replay_run':request['replay_result'].removesuffix('/autopilot-result.json'),
            'rows':self.selected,'held':held,'external_writes':False,'duration_reduced_for_overlap':False}
        packet_path = self.packet_dir/'packet.json'; write_json(packet_path,self.packet)
        selection_path = self.packet_dir/'selection.json'
        write_json(selection_path, {'schema_version':'sep25-repaired-publication-selection/v1',
            'source_run':str(derived),'replay_run':self.packet['replay_run'],
            'source_proposals_sha256':hashlib.sha256((derived/'proposals.json').read_bytes()).hexdigest(),
            'held':held, 'remaining':[{'proposal_id':p['id'],'activity_id':p['activity_id'],'minutes':p['duration_minutes']} for p in rows[12:]]})
        self.packet['selection_sha256']=hashlib.sha256(selection_path.read_bytes()).hexdigest()
        write_json(packet_path,self.packet)
        receipt_path=self.packet_dir/'receipt.json'
        write_json(receipt_path, {'schema_version':'verified-sheet-publication-receipt/v1','utc':'2026-10-08 02:22:09 UTC',
            'spreadsheet_id':'sheet-1','sheet_id':1,'range':'A831:O852','rows':22,'minutes':114,
            'packet_sha256':hashlib.sha256(packet_path.read_bytes()).hexdigest(),'exact_readback':True,
            'readback':captured(1,self.title,self.selected,start=830)})
        # Real checkpoint storage and read-only collector projection.
        store=collector_checkpoints.PageCheckpointStore(self.root/'fresh-native')
        since=dt.datetime(2026,9,6,21,tzinfo=dt.timezone.utc); until=dt.datetime(2026,9,8,21,tzinfo=dt.timezone.utc)
        identity=collector._clockify_checkpoint_identity('workspace-1','member-1',since,until)
        native_state=store.open(identity,initial_metadata={'snapshot_at':'2026-10-08T02:02:24Z'})
        native_state=store.append_page(native_state,payload=entries,continuation={'page':2},signature=collector._clockify_page_signature(entries))
        native_state=store.mark_complete(native_state)
        evidence=collector.fetch_clockify({'CLOCKIFY_WORKSPACE_ID':'workspace-1'},{'clockify_user_id':'member-1'},since,until,
            snapshot_at=dt.datetime(2026,10,8,2,2,24,tzinfo=dt.timezone.utc),checkpoint_store=store)
        evidence_path=self.packet_dir/'native-evidence.json';write_json(evidence_path,evidence)
        self.packet['fresh_clockify_sha256']=hashlib.sha256(evidence_path.read_bytes()).hexdigest()
        self.packet['native_page_sha256']=hashlib.sha256((native_state.directory/'pages/000001.json').read_bytes()).hexdigest()
        write_json(packet_path,self.packet)
        receipt=json.loads(receipt_path.read_text()); receipt['packet_sha256']=hashlib.sha256(packet_path.read_bytes()).hexdigest();write_json(receipt_path,receipt)
        live_path=self.packet_dir/'live.json'
        write_json(live_path, {'schema_version':'clockify-consumer-live-readback/v1','captured_utc':'2026-10-08T03:34:45Z',
            'identity_and_portfolio_readback':captured(1,self.title,self.selected,start=830),'external_writes':False})
        handle=lambda p: {'path':str(p),'sha256':cycle._digest(p)}
        self.proofs={name:handle(path) for name,path in {'publication_packet':packet_path,'publication_receipt':receipt_path,
            'selection':selection_path,'live_readback':live_path,'native_evidence':evidence_path,
            'native_checkpoint_manifest':native_state.directory/'manifest.json',
            'native_checkpoint_page':native_state.directory/'pages/000001.json'}.items()}
        self.request={k:v for k,v in request.items() if k not in {'publication_result','publication_result_digest'}}
        self.request.update(schema_version='clockify-historical-selected-adoption-request/v1',
            runs_root=str(derived.parent),prior_record_digest=cycle._value_digest(self.prior),selected_delivery_proofs=self.proofs)
        self.attach_diagnostic_proofs()
        self.config.pop('monthly_unresolved_alias_proof',None)
        debt_path=self.state_dir/'source-coverage.json'
        debt=source_coverage.SourceDebtStore.from_document(source_coverage.read(debt_path))
        bundle=json.loads((derived/'completion-bundle.json').read_text())
        self.peer_debt_ids=[]
        for peer in ('macbook','unrelated'):
            interval=source_coverage.SourceInterval(source=f'peer/{peer}',since_utc=bundle['since_utc'],
                until_utc=bundle['until_utc'],slice_id=bundle['slice_id'],compatibility_version='peer/v1')
            item=debt.record_failure(interval,failure_class='peer_unavailable',retryable=True,
                resume_state_digest='sha256:'+'a'*64,attempted_at='2026-09-10T00:00:00Z')
            self.peer_debt_ids.append(item.debt_id)
        source_coverage.write(debt_path,debt.document())
        self.before_artifacts={str(p):(p.read_bytes(),p.stat().st_mtime_ns) for p in derived.parent.rglob('*') if p.is_file()}

    def with_source_diagnostics(self,request,previous,source):
        """Construct a genuine new collector derivation with 51 native atoms."""
        sessions=[{'machine':'macbook','status':'ok','repository_evidence_status':'complete','repository_events':[],
            'codex_sessions':[{'session_id':f'diagnostic-{i}','start':'2026-09-07 12:00',
                               'title':f'Original diagnostic {i}'} for i in range(51)]}]
        write_json(source/'evidence/sessions.json',sessions)
        raw={key:json.loads((source/path).read_text()) for key,path in collector_receipts._COLLECTOR_RAW_ARTIFACTS.items()}
        ledger=evidence_ledger.EvidenceLedger(tuple(evidence_ledger.normalize_collector_snapshot(raw)),
            evidence_ledger.source_inventory_from_collector(raw))
        write_json(source/'evidence/evidence-ledger.json',{'schema_version':evidence_ledger.SCHEMA_VERSION,
            'manifest':ledger.manifest.document(),'events':[e.document() for e in ledger.events]})
        ambiguities=[{'id':f'exception-{i}','exception_kind':'insufficient_evidence','activity_id':f'diagnostic-{i}',
            'reason':'No bounded work interval','evidence_ids':[event.evidence_id]} for i,event in enumerate(ledger.events)]
        write_json(source/'ambiguous.json',ambiguities)
        accounting=json.loads((source/'work-accounting-result.json').read_text());accounting['ambiguous']=ambiguities
        write_json(source/'work-accounting-result.json',accounting)
        report=json.loads((source/'run-report.json').read_text());report['evidence_ledger']['source_completeness']=ledger.manifest.document()['source_completeness']
        write_json(source/'run-report.json',report)
        original=json.loads((source/'completion-bundle.json').read_text())
        slice_=type('Slice',(),{'slice_id':original['slice_id'],
            'since':dt.datetime.fromisoformat(original['since_utc'].replace('Z','+00:00')),
            'until':dt.datetime.fromisoformat(original['until_utc'].replace('Z','+00:00'))})()
        write_json(source/'completion-bundle.json',collector_receipts.build_completion_bundle(source,slice_=slice_).document())
        derived=review._prepare_collector_derivation_run(source,
            {name:source/name for name in review._RECONCILIATION_INPUTS.values()},executor_runtime_identity={'git_sha':'fixture-sha'},environment={})
        files=('semantic-analysis.json','work-accounting-result.json','quality_report.json','review-snapshot.json','proposals.json','ambiguous.json','fathom-reconciliation.json')
        for name in files:(derived/name).write_bytes((source/name).read_bytes())
        analysis=json.loads((derived/'semantic-analysis.json').read_text());analysis['activities'][0]['analyzer_tier']='fixture'
        write_json(derived/'semantic-analysis.json',analysis)
        bundle=review._finalize_collector_derivation_completion(derived)
        document=json.loads((previous/'autopilot-result.json').read_text())
        def result_for(target,bundle,old):
            result=copy.deepcopy(document);result.update(run_id=target.name,run_dir=str(target),completion_bundle_digest=bundle.bundle_digest,completion_bundle=bundle.document())
            result['paths']={k:(v.replace(str(old),str(target),1) if v else v) for k,v in document['paths'].items()}
            return result
        document=result_for(derived,bundle,previous);write_json(derived/'autopilot-result.json',document)
        replay=review._prepare_replay_run(derived)
        for name in files:(replay/name).write_bytes((derived/name).read_bytes())
        review._verify_replay_integrity(derived,replay)
        rb=review._finalize_replay_completion(derived,replay)
        replay_document=result_for(replay,rb,derived);replay_document['paths']['replay_integrity']=str(replay/'replay-integrity.json')
        write_json(replay/'autopilot-result.json',replay_document)
        request={**request,'source_result':str(derived/'autopilot-result.json'),
            'replay_result':str(replay/'autopilot-result.json'),
            'source_result_digest':cycle._digest(derived/'autopilot-result.json'),'replay_result_digest':cycle._digest(replay/'autopilot-result.json'),
            'runtime_identity_digest':bundle.runtime_identity_digest,
            'source_provenance':{'kind':'collector_derivation','derivation_run_dir':str(derived),'lineage_digest':cycle._digest(derived/'collector-source.json')}}
        return request,derived

    def attach_diagnostic_proofs(self):
        source,expected,actual,aliases,_load=self.diagnostic_fixture(self.source)
        rows=[actual[row[0]][1] for row in expected]
        p=self.packet_dir/'unresolved.json';write_json(p,{'rows':expected,'source_run':str(source)})
        title='September 2026 unresolved evidence'
        ur=self.packet_dir/'unresolved-receipt.json';write_json(ur,{'spreadsheet_id':'sheet-1','sheet_id':2,
            'packet_sha256':hashlib.sha256(p.read_bytes()).hexdigest(),
            'readback':captured(2,title,rows[10:],start=285)})
        alias_path=self.packet_dir/'aliases.json';write_json(alias_path,aliases)
        hp=self.packet_dir/'header.json';write_json(hp,captured(2,title,[monthly.LEGACY_HEADER]))
        live=json.loads(Path(self.proofs['live_readback']['path']).read_text())
        live['diagnostic_readback']=captured(2,title,rows,start=275)
        self.rewrite_proof('live_readback',live)
        for name,path in {'unresolved_packet':p,'unresolved_receipt':ur,'diagnostic_aliases':alias_path,'header_readback':hp}.items():
            self.proofs[name]={'path':str(path),'sha256':cycle._digest(path)}

    def adopt(self, request=None):
        with mock.patch.object(cycle,'run_child_bounded',side_effect=AssertionError('provider child forbidden')), \
             mock.patch.object(collector,'clockify_get',side_effect=AssertionError('Clockify provider forbidden')):
            return cycle.adopt_historical_slice(self.config,request or self.request)

    def test_adopts_exhaustive_selected_delivery_preserving_previous_history_once(self):
        """Removing predecessor preservation or launching publication breaks this proof."""
        try:
            first=self.adopt()
        except cycle.CycleError as error:
            self.fail(f'Consumer must adopt selected historical recovery, without republishing: {error}')
        self.assertEqual('delivered_with_exceptions',first['status'])
        state=json.loads((self.state_dir/'review-cycle-state.json').read_text())
        record=state['slices'][self.since]
        receipt=json.loads(Path(record['historical_adoption_receipt']).read_text())
        self.assertEqual(self.prior,receipt['superseded_record'])
        self.assertEqual(34,len(record['review_ids']))
        self.assertEqual(22,len(receipt['selected_delivery']['selected_review_ids']))
        self.assertEqual(12,len(receipt['selected_delivery']['held_review_ids']))
        self.assertEqual(51,len(receipt['selected_delivery']['diagnostics']['review_ids']))
        self.assertEqual(10,len(receipt['selected_delivery']['diagnostics']['historical_alias_ids']))
        self.assertEqual(self.until,state['completed_through'])
        state_bytes=(self.state_dir/'review-cycle-state.json').read_bytes()
        debt_bytes=(self.state_dir/'source-coverage.json').read_bytes()
        self.assertEqual(first,self.adopt())
        self.assertEqual(state_bytes,(self.state_dir/'review-cycle-state.json').read_bytes())
        self.assertEqual(debt_bytes,(self.state_dir/'source-coverage.json').read_bytes())
        self.assertEqual(self.before_artifacts,{str(p):(p.read_bytes(),p.stat().st_mtime_ns) for p in self.source.parent.rglob('*') if p.is_file()})
        cycle._validate_delivered_state(self.config,state)
        debt=source_coverage.SourceDebtStore.from_document(source_coverage.read(self.state_dir/'source-coverage.json'))
        self.assertEqual('resolved',debt.get(self.peer_debt_ids[0]).status)
        self.assertEqual('active',debt.get(self.peer_debt_ids[1]).status)

    def test_receipts_and_completed_debts_resume_after_final_state_write_interrupt(self):
        state_path=self.state_dir/'review-cycle-state.json'
        before=state_path.read_bytes()
        original=cycle._atomic
        def interrupted(path,value):
            if path==state_path:raise OSError('simulated final state write interruption')
            return original(path,value)
        with mock.patch.object(cycle,'_atomic',side_effect=interrupted):
            with self.assertRaisesRegex(OSError,'simulated final state'):self.adopt()
        self.assertEqual(before,state_path.read_bytes())
        debt_bytes=(self.state_dir/'source-coverage.json').read_bytes()
        debts=json.loads(debt_bytes)
        completed=[event for event in debts['events'] if event['event']=='complete']
        self.assertEqual(2,len(completed))
        receipts={path:path.read_bytes() for path in (
            self.state_dir/'delivery-receipts'/f'{self.since}.json',
            self.state_dir/'historical-adoption-receipts'/f'{self.since}.json')}
        self.assertEqual(self.prior,json.loads(list(receipts.values())[1])['superseded_record'])
        self.assertEqual('delivered_with_exceptions',self.adopt()['status'])
        self.assertEqual(debt_bytes,(self.state_dir/'source-coverage.json').read_bytes())
        self.assertEqual(receipts,{path:path.read_bytes() for path in receipts})
        self.assertEqual('delivered_with_exceptions',self.adopt()['status'])
        self.assertEqual(debt_bytes,(self.state_dir/'source-coverage.json').read_bytes())

    def test_missing_diagnostic_proof_returns_review_proof_without_local_mutation(self):
        before={path:path.read_bytes() for path in (self.state_dir/'review-cycle-state.json',self.state_dir/'source-coverage.json')}
        request=copy.deepcopy(self.request)
        request['selected_delivery_proofs'].pop('header_readback')
        result=self.adopt(request)
        self.assertEqual('diagnostic_proof_missing',result['status'])
        self.assertEqual(22,len(result['selected_delivery']['selected_review_ids']))
        self.assertEqual(12,len(result['selected_delivery']['held_review_ids']))
        self.assertEqual(before,{path:path.read_bytes() for path in before})
        self.assertFalse((self.state_dir/'delivery-receipts'/f'{self.since}.json').exists())

    def test_missing_original_graph_after_adoption_fails_closed(self):
        self.adopt()
        state=json.loads((self.state_dir/'review-cycle-state.json').read_text())
        Path(self.proofs['selection']['path']).unlink()
        with self.assertRaises(cycle.CycleError):cycle._validate_delivered_state(self.config,state)
        with self.assertRaises(cycle.CycleError):self.adopt()

    def test_earlier_undelivered_slice_is_not_skipped(self):
        self.config['recovery_since']='2026-09-06'
        path=self.state_dir/'review-cycle-state.json'
        state=json.loads(path.read_text())
        state['slices']['2026-09-06']={'until':self.since,'status':'recovery_blocked'}
        write_json(path,state)
        self.adopt()
        self.assertIsNone(json.loads(path.read_text())['completed_through'])

    def test_delivered_metadata_cannot_drift_from_sealed_source(self):
        self.adopt()
        state=json.loads((self.state_dir/'review-cycle-state.json').read_text())
        for field,value in (('status','delivered'),('review_ids',[]),('exception_ids',[]),
                            ('source_run_id','other'),('exceptions_complete',True)):
            with self.subTest(field=field):
                altered=copy.deepcopy(state);altered['slices'][self.since][field]=value
                with self.assertRaises(cycle.CycleError):cycle._validate_delivered_state(self.config,altered)

    def test_rehashed_live_target_or_retained_source_atom_drift_is_rejected(self):
        live=json.loads(Path(self.proofs['live_readback']['path']).read_text())
        aliases=json.loads(Path(self.proofs['diagnostic_aliases']['path']).read_text())
        before={path:path.read_bytes() for path in (self.state_dir/'review-cycle-state.json',self.state_dir/'source-coverage.json')}
        for change in ('spreadsheet','title','sheet_id','old_capture','row','diagnostic_id','header','source_atom'):
            with self.subTest(change=change):
                altered=copy.deepcopy(live);alias_packet=copy.deepcopy(aliases)
                capture=altered['identity_and_portfolio_readback']['structuredContent']
                sheet=capture['sheets'][0]
                if change=='spreadsheet':capture['spreadsheetId']='other'
                elif change=='title':sheet['properties']['title']='other'
                elif change=='sheet_id':sheet['properties']['sheetId']=9
                elif change=='old_capture':altered['captured_utc']='2026-10-08T02:00:00Z'
                elif change=='row':sheet['data'][0]['rowData'][0]['values'][8]['userEnteredValue']['stringValue']='Drift'
                elif change=='diagnostic_id':altered['diagnostic_readback']['structuredContent']['sheets'][0]['data'][0]['rowData'][0]['values'][0]['userEnteredValue']['stringValue']='other'
                elif change=='header':self.rewrite_proof('header_readback',captured(2,'September 2026 unresolved evidence',[['wrong']]))
                else:alias_packet['aliases'][0]['source_atoms'][0]['canonical_event_sha256']='sha256:'+'0'*64
                self.rewrite_proof('live_readback',altered);self.rewrite_proof('diagnostic_aliases',alias_packet)
                with self.assertRaises(cycle.CycleError):self.adopt()
                self.assertEqual(before,{path:path.read_bytes() for path in before})
                self.assertFalse((self.state_dir/'delivery-receipts'/f'{self.since}.json').exists())
                self.rewrite_proof('header_readback',captured(2,'September 2026 unresolved evidence',[monthly.LEGACY_HEADER]))
        self.rewrite_proof('live_readback',live);self.rewrite_proof('diagnostic_aliases',aliases)

    def test_selected_runs_root_cannot_escape_or_be_symlinked(self):
        link=self.root/'runs-link';link.symlink_to(self.source.parent,target_is_directory=True)
        before=(self.state_dir/'review-cycle-state.json').read_bytes()
        for root in (link,self.packet_dir):
            with self.subTest(root=str(root)):
                with self.assertRaises(cycle.CycleError):self.adopt({**self.request,'runs_root':str(root)})
                self.assertEqual(before,(self.state_dir/'review-cycle-state.json').read_bytes())

    def test_stale_prior_binding_is_rejected_without_state_changes(self):
        before=(self.state_dir/'review-cycle-state.json').read_bytes()
        with self.assertRaises(cycle.CycleError):
            self.adopt({**self.request,'prior_record_digest':'sha256:'+'0'*64})
        self.assertEqual(before,(self.state_dir/'review-cycle-state.json').read_bytes())

    def test_forged_or_omitted_partition_never_creates_delivery_receipt(self):
        before=(self.state_dir/'review-cycle-state.json').read_bytes()
        path=Path(self.proofs['publication_packet']['path'])
        for mutate in ('omit','duplicate','wrong_native'):
            with self.subTest(mutate=mutate):
                packet=copy.deepcopy(self.packet)
                if mutate=='omit':packet['held'].pop()
                elif mutate=='duplicate':packet['rows'][0]=packet['rows'][1]
                else:packet['held'][0]['native_decision']['native_entry_id']='f'*24
                write_json(path,packet)
                with self.assertRaises(cycle.CycleError):self.adopt()
                self.assertEqual(before,(self.state_dir/'review-cycle-state.json').read_bytes())
                self.assertFalse((self.state_dir/'delivery-receipts'/f'{self.since}.json').exists())
        write_json(path,self.packet)

    def rewrite_proof(self,name,value):
        path=Path(self.proofs[name]['path']);write_json(path,value)
        self.proofs[name]['sha256']=cycle._digest(path)

    def repin_selection(self,packet,selection):
        self.rewrite_proof('selection',selection)
        packet['selection_sha256']=self.proofs['selection']['sha256'].removeprefix('sha256:')
        self.rewrite_proof('publication_packet',packet)
        receipt=json.loads(Path(self.proofs['publication_receipt']['path']).read_text())
        receipt['packet_sha256']=self.proofs['publication_packet']['sha256'].removeprefix('sha256:')
        self.rewrite_proof('publication_receipt',receipt)

    def test_recomputed_receipts_cannot_forge_exhaustive_source_partition(self):
        """Even internally rehashed packets cannot hide a source target."""
        for change in ('omitted','overlapping','wrong_fingerprint','wrong_native','altered_row'):
            with self.subTest(change=change):
                packet=copy.deepcopy(self.packet)
                selection=json.loads(Path(self.proofs['selection']['path']).read_text())
                if change=='omitted':packet['held'].pop()
                elif change=='overlapping':packet['held'][0]=copy.deepcopy(packet['held'][1])
                elif change=='wrong_fingerprint':packet['held'][0]['native_decision']['evidence_fingerprint']='evfp:sha256:'+'0'*64
                elif change=='wrong_native':packet['held'][0]['native_decision']['native_entry_id']='f'*24
                else:
                    packet['rows'][0][8]='Invented accomplishment'
                    receipt=json.loads(Path(self.proofs['publication_receipt']['path']).read_text())
                    receipt['readback']=captured(1,self.title,packet['rows'],start=830)
                    self.rewrite_proof('publication_receipt',receipt)
                    live=json.loads(Path(self.proofs['live_readback']['path']).read_text())
                    live['identity_and_portfolio_readback']=captured(1,self.title,packet['rows'],start=830)
                    self.rewrite_proof('live_readback',live)
                selection['held']=packet['held']
                self.repin_selection(packet,selection)
                before=(self.state_dir/'review-cycle-state.json').read_bytes()
                with self.assertRaises(cycle.CycleError):self.adopt()
                self.assertEqual(before,(self.state_dir/'review-cycle-state.json').read_bytes())

    def diagnostic_fixture(self,source=None):
        """51 actual source-bound diagnostics and ten legacy representations."""
        ambiguities=[{'id':f'old-{i}','exception_kind':'insufficient_evidence','activity_id':f'diagnostic-{i}',
                      'reason':'No trustworthy work interval','evidence_ids':[f'fixture-{i}']} for i in range(51)]
        make_source=source is None
        if make_source:
            result=make_run(self.root,'diagnostic-source',replay=False,proposals=copy.deepcopy(self.proposals),
                ambiguous=ambiguities,snapshots_from=self.source,record_checkpoint=False)
            source=result.parent
        # Legacy collector observations are local labels; current/old events
        # must remain byte-identical, not be relabeled with an invented offset.
        events=[evidence_ledger.evidence_event('codex_session',{'source_id':f'fixture-{i}','machine':'test'},
            observed_at='2026-09-07 12:00',attributes={'title':f'Original diagnostic {i}'}) for i in range(51)]
        if make_source:
            ledger=evidence_ledger.EvidenceLedger(tuple(events),timezone='Europe/Bucharest')
            write_json(source/'evidence/evidence-ledger.json',{'schema_version':evidence_ledger.SCHEMA_VERSION,
                'manifest':ledger.manifest.document(),'events':[e.document() for e in events]})
            for i,item in enumerate(ambiguities):item['evidence_ids']=[events[i].evidence_id]
            write_json(source/'ambiguous.json',ambiguities)
            accounting=json.loads((source/'work-accounting-result.json').read_text());accounting['ambiguous']=ambiguities
            write_json(source/'work-accounting-result.json',accounting)
        else:
            ambiguities=json.loads((source/'ambiguous.json').read_text())
            events=[evidence_ledger.EvidenceEvent.from_document(e) for e in json.loads((source/'evidence/evidence-ledger.json').read_text())['events']]
        expected=monthly.project_rows(source)
        old=self.root/('old-diagnostics' if make_source else 'initial-old-diagnostics');old.mkdir()
        old_ambiguous=copy.deepcopy(ambiguities)
        for item in old_ambiguous:item['reason']='Original historical reason'
        write_json(old/'ambiguous.json',old_ambiguous)
        write_json(old/'evidence/evidence-ledger.json',json.loads((source/'evidence/evidence-ledger.json').read_text()))
        write_json(old/'semantic-analysis.json',{'activities':[]})
        handle=lambda p:{'path':str(p),'sha256':cycle._digest(p)}
        old_handles={name:handle(old/name) for name in ('ambiguous.json','evidence/evidence-ledger.json','semantic-analysis.json')}
        current_handles={name:handle(source/name) for name in old_handles}
        by_ids={tuple(a['evidence_ids']):i for i,a in enumerate(ambiguities)}
        actual=[];aliases=[]
        for i,row in enumerate(expected):
            if i>=10:actual.append(list(row));continue
            ids=json.loads(row[11])['evidence_ids'];index=by_ids[tuple(ids)];event=events[index].document()
            cells=[row[0],'2026-09-07',event['source_type'],row[3],'Historical reason','Old routing',
                event['attributes']['title'],'','Historical confidence','Original human action','needs_review',old_handles['ambiguous.json']['sha256']]
            actual.append(cells)
            alias={'existing_row_number':i+276,'existing_stable_id':row[0],
                'existing_live_cells':cells,'existing_live_cells_sha256':selected._digest(cells),'preserve_existing_cells':True,
                'old_source_artifacts':old_handles,'current_source_artifacts':current_handles,
                'old_exception_json_pointer':f'/{index}','current_exception_json_pointer':f'/{index}',
                'old_exception':old_ambiguous[index],'current_exception':ambiguities[index],
                'old_exception_sha256':selected._digest(old_ambiguous[index]),'current_exception_sha256':selected._digest(ambiguities[index]),
                'canonical_diagnostic_identity':{'kind':row[3],'evidence_ids':ids},
                'source_atoms':[{'evidence_id':ids[0],'canonical_event_sha256':selected._digest(event)}],
                'current_canonical_diagnostic_row':row,'current_canonical_diagnostic_row_sha256':selected._digest(row)}
            aliases.append(alias)
        alias_packet={'schema_version':'retained-diagnostic-source-provenance-aliases/v1','spreadsheet_id':'sheet-1',
            'sheet_title':'September 2026 unresolved evidence','sheet_id':2,'source_bindings':{},'aliases':aliases}
        old_load=lambda handle:json.loads(clockify_source_adoptions._capture(handle,{}))
        live={row[0]:(i+276,row) for i,row in enumerate(actual)}
        return source,expected,live,alias_packet,old_load

    def test_fifty_one_diagnostic_representations_preserve_old_local_observations(self):
        """Rejecting native local observations loses genuine historical aliases."""
        source,rows,live,packet,load=self.diagnostic_fixture()
        self.assertEqual(51,len(rows))
        try:
            aliases=selected._legacy_aliases(packet,source=source,expected={r[0]:r for r in rows},live=live,
                spreadsheet='sheet-1',title='September 2026 unresolved evidence',sheet_id=2,load=load)
        except ValueError as error:
            self.fail(f'Genuine legacy collector timestamps must remain valid source observations: {error}')
        self.assertEqual(10,len(aliases))
        self.assertEqual([live[a['existing_stable_id']][1] for a in packet['aliases']],list(aliases.values()))


if __name__=='__main__':unittest.main()
