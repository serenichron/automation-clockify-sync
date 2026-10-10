"""Real dispatcher boundary for original retained-plus-appended supplements."""
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import unittest

from scripts import clockify_review_cycle as cycle, clockify_review_run as review
from scripts import clockify_selected_delivery_adoption as selected
from scripts import clockify_sheet_publish as publisher, clockify_monthly_unresolved as monthly
from scripts import collector_checkpoints, clockify_sync_collect as collector, evidence_ledger, review_corrections
import test_cycle_historical_adoption as historical
from test_cycle_selected_delivery_adoption import captured
from test_review_cycle_delivery import make_run, proposal, write_json


def handle(path):
    return {'path': str(path), 'sha256': cycle._digest(path)}


class SupplementDeliveryTests(unittest.TestCase):
    def setUp(self):
        fixture = historical.HistoricalAdoptionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root, self.config = fixture.root, fixture.config
        self.title = 'September 2026 portfolio review'
        events = [evidence_ledger.evidence_event('codex_session', {'source_id': f'own-{i}', 'machine': 'test'},
            observed_at='2026-09-07T09:00:00+03:00', attributes={'title': f'Own result {i}'}) for i in range(12)]
        ledger_root = self.root/'ledger'
        ledger = evidence_ledger.EvidenceLedger(tuple(events), timezone='Europe/Bucharest')
        write_json(ledger_root/'evidence/evidence-ledger.json', {'schema_version': evidence_ledger.SCHEMA_VERSION,
            'manifest': ledger.manifest.document(), 'events': [event.document() for event in events]})
        proposals = []
        for i, minutes in enumerate([56,5,10,15,20,15,12,14,15,10,15,43]):
            start = dt.datetime(2026,9,7,0,tzinfo=dt.timezone.utc)+dt.timedelta(minutes=90*i)
            p = {**proposal(), 'id': f'P{i+1}', 'activity_id': f'own-{i}', 'review_activity_key': f'wka-own-{i}',
                'start': start.isoformat(), 'end': (start+dt.timedelta(minutes=minutes)).isoformat(),
                'duration_minutes': minutes, 'duration_seconds': 60*minutes, 'clockify_project_suffix': '123456',
                'tag_suffixes': ['01234567'], 'evidence_ids': [events[i].evidence_id],
                'provenance': {'evidence_ids': [events[i].evidence_id]}}
            if i == 11:
                p.update(client_project='', clockify_project_suffix='', routing_disposition='unresolved-routing')
            proposals.append(p)
        source_result = make_run(self.root, 'supplement-source', replay=False, proposals=copy.deepcopy(proposals),
            ledger_from=ledger_root, record_checkpoint=False)
        self.source = source_result.parent
        replay_result = make_run(self.root, 'supplement-replay', replay=True, source_name=self.source.name,
            proposals=copy.deepcopy(proposals), snapshots_from=self.source, ledger_from=ledger_root)
        frozen = {name:cycle._digest(self.source/name) for name in fixture.request['frozen_snapshot_digests']}
        self.source_stage = cycle._validate_stage(self.config,source_result,fixture.since,fixture.until,
            replay=False,expected_snapshot_digests=frozen)
        self.replay_stage = cycle._validate_stage(self.config,replay_result,fixture.since,fixture.until,
            replay=True,expected_snapshot_digests=frozen,source_run_id=self.source.name,source_run_dir=str(self.source))
        original_result = make_run(self.root,'original-supplement',replay=False,proposals=copy.deepcopy(proposals),
            ledger_from=ledger_root,record_checkpoint=False)
        audit_root = self.root/'audit'; audit_root.mkdir()
        native_entries, decisions = [], []
        for i,p in enumerate(proposals):
            decision = {'activity_id': p['activity_id'], 'evidence_fingerprint': review_corrections.proposal_target(p)[1],
                'source_minutes': p['duration_minutes'], 'recommendation': 'review', 'automatic_credit_created': False,
                'rationale': 'Explicit own-result adjudication; temporal overlap alone is not equivalence.'}
            if 6 <= i < 11:
                entry = {'id': f'{i:024x}', 'workspaceId':'workspace-1','userId':'member-1',
                    'projectId':'project-123456','tagIds':['tag-01234567'],'taskId':None,'billable':False,
                    'description': f'Own completed result {i}', 'timeInterval': {'start':p['start'],'end':p['end'],
                    'duration': f'PT{p["duration_minutes"]}M'}}
                decision.update(recommendation='hold_represented',native_original_index=len(native_entries),
                    native_entry_id=entry['id'], native_description_sha256=hashlib.sha256(entry['description'].encode()).hexdigest(),
                    native_fully_covers_source_interval=True,effective_routing_matches=True)
                native_entries.append(entry)
            decisions.append(decision)
        since=dt.datetime(2026,9,6,21,tzinfo=dt.timezone.utc); until=dt.datetime(2026,9,8,21,tzinfo=dt.timezone.utc)
        store=collector_checkpoints.PageCheckpointStore(self.root/'native')
        state=store.open(collector._clockify_checkpoint_identity('workspace-1','member-1',since,until),
            initial_metadata={'snapshot_at':'2026-10-08T02:02:24Z'})
        state=store.append_page(state,payload=native_entries,continuation={'page':2},signature=collector._clockify_page_signature(native_entries))
        state=store.mark_complete(state)
        native=collector.fetch_clockify({'CLOCKIFY_WORKSPACE_ID':'workspace-1'},{'clockify_user_id':'member-1'},since,until,
            snapshot_at=dt.datetime(2026,10,8,2,2,24,tzinfo=dt.timezone.utc),checkpoint_store=store)
        native_path=self.root/'native-evidence.json'; write_json(native_path,native)
        normalized=[event.document() for event in evidence_ledger.normalize_collector_snapshot({'clockify':native})]
        by_index={int(e['source_ref']['source_id'].removeprefix('row-'))-1:e for e in normalized if e['source_type']=='clockify'}
        bindings=[{'native_original_index':i,'native_entry_id':entry['id'],'native_entry':entry,'evidence_event':by_index[i]}
            for i,entry in enumerate(native_entries)]
        audit={'schema_version':'sep29-native-accomplishment-duplicate-audit/v1','records':decisions,
            'native_original_index_binding':[{'native_original_index':i,'native_entry_id':entry['id'],
                'normalized_evidence_id':by_index[i]['evidence_id']} for i,entry in enumerate(native_entries)]}
        write_json(audit_root/'native-accomplishment-audit.private.json',audit)
        write_json(audit_root/'audit-metadata.json',{'schema_version':'sep29-editorial-accomplishment-audit/v1',
            'editorial_audit':[{'activity_id':p['activity_id'],'evidence_fingerprint':review_corrections.proposal_target(p)[1],
                'preserved_minutes':p['duration_minutes']} for p in proposals]})
        (audit_root/'review-corrections.jsonl').write_bytes((self.source/'review-corrections.jsonl').read_bytes())
        diagnostic_rows=monthly.project_rows(self.source)
        self.packet={'schema_version':'verified-review-publication-supplement/v2','source_run':str(self.source),
            'replay_run':str(replay_result.parent),'source_proposals_sha256':cycle._digest(self.source/'proposals.json')[7:],
            'canonical_header':publisher.HEADER,'duration_reduced_for_overlap':False,'external_writes':False,
            'rows':[publisher.proposal_row(p,self.source.name) for p in proposals[:6]],'row_proposals':proposals[:6],
            'held':[{'proposal_identity':p['activity_id'],'proposal_id_display_only':p['id'],'minutes':p['duration_minutes'],
                'native_decision':decisions[i]} for i,p in enumerate(proposals) if 6<=i<11],
            'mixed_meeting_excluded':{'activity_id':proposals[-1]['activity_id'],'full_minutes':43,
                'canonical_diagnostic_id':diagnostic_rows[0][0]},'fresh_evidence_bindings':bindings,
            'fresh_clockify_sha256':cycle._digest(native_path)[7:],'native_page_sha256':cycle._digest(state.directory/'pages/000001.json')[7:],
            'audit_artifact_sha256':{name:cycle._digest(audit_root/name)[7:] for name in ('audit-metadata.json',
                'native-accomplishment-audit.private.json','review-corrections.jsonl')}}
        self.packet_path=self.root/'packet.json'; write_json(self.packet_path,self.packet)
        retained=list(self.packet['rows'][0]); retained[14]='Preserved human note'
        def update(row,first,last,values):
            return {'updateCells':{'range':{'sheetId':1,'startRowIndex':row-1,'endRowIndex':row,
                'startColumnIndex':first,'endColumnIndex':last},'fields':'userEnteredValue',
                'rows':[{'values':[{'userEnteredValue':{'stringValue' if isinstance(v,str) else 'numberValue':v}} for v in values]}]}}
        requests=[update(853,0,15,self.packet['rows'][1])]
        requests[0]['updateCells']['range']['endRowIndex']=857
        requests[0]['updateCells']['rows']=[update(853,0,15,row)['updateCells']['rows'][0] for row in self.packet['rows'][1:]]
        requests += [update(381,i,i+1,[retained[i]]) for i in (4,5,11,12)]
        original=captured(1,self.title,self.packet['rows'][1:],start=852)
        original['structuredContent']['sheets'][0]['data'] += captured(1,self.title,[retained],start=380)['structuredContent']['sheets'][0]['data']
        self.receipt={'schema_version':'verified-sheet-publication-receipt/v1','utc':'2026-10-08 02:40:46 UTC',
            'spreadsheet_id':'sheet-1','sheet_id':1,'range':'A853:O857','rows':5,'minutes':65,
            'existing_row':381,'existing_minutes':56,'updated_columns':['E','F','L','M'],'requests':requests,
            'packet_sha256':cycle._digest(self.packet_path)[7:],'exact_readback':True,'readback':original,
            'user_dispositions_notes_preserved':True}
        self.receipt_path=self.root/'receipt.json'; write_json(self.receipt_path,self.receipt)
        live_path=self.root/'live.json'; write_json(live_path,{'captured_utc':'2026-10-10T06:26:46Z','readback':original})
        unresolved_path=self.root/'unresolved.json';write_json(unresolved_path,{'rows':diagnostic_rows,'source_run':str(self.source)})
        diagnostic_capture=captured(2,'September 2026 unresolved evidence',diagnostic_rows,start=875,header=monthly.HEADER)
        unresolved_receipt=self.root/'unresolved-receipt.json';write_json(unresolved_receipt,{'rows':len(diagnostic_rows),
            'sheet_id':2,'spreadsheet_id':'sheet-1','range':'A876:L876','readback':diagnostic_capture,
            'packet_sha256':cycle._digest(unresolved_path)[7:]})
        current=copy.deepcopy(diagnostic_capture)
        current['structuredContent']['sheets'][0]['data'][0]['rowData'][0]['values'][9]['userEnteredValue']={'stringValue':'Existing human annotation'}
        current['structuredContent']['sheets'][0]['data'][0]['rowData'][0]['values'][10]['userEnteredValue']={'stringValue':'resolved'}
        current_path=self.root/'diagnostic-current.json';write_json(current_path,{'captured_utc':'2026-10-10T06:29:47Z','readback':current})
        def inventory(root):return {str(p.relative_to(root)):{'sha256':cycle._digest(p)[7:]} for p in root.rglob('*') if p.is_file()}
        selection={'schema_version':'sep29-publication-validation/v1','source_run':str(self.source),'replay_run':str(replay_result.parent),
            'artifact_sha256':{'review-publication.private.json':cycle._digest(self.packet_path)[7:],
                'unresolved-publication.private.json':cycle._digest(unresolved_path)[7:]},
            'input_inventory':{str(path):inventory(path) for path in (self.source,replay_result.parent,original_result.parent,audit_root)},
            'review_rows':6,'review_minutes':121,'held_rows':5,'held_minutes':66,'routing_gap_rows':1,'canonical_diagnostic_rows':1,
            'repaired_original_keyed_preservation':[{'activity_id':p['activity_id'],'evidence_fingerprint':review_corrections.proposal_target(p)[1],
                'start':p['start'],'end':p['end'],'duration_minutes':p['duration_minutes'],'duration_seconds':p['duration_seconds'],
                'evidence_ids':p['provenance']['evidence_ids'],'changed_editorial_fields':[]} for p in proposals]}
        selection_path=self.root/'selection.json';write_json(selection_path,selection)
        self.proofs={name:handle(path) for name,path in {'selection':selection_path,'publication_packet':self.packet_path,
            'publication_receipt':self.receipt_path,'live_readback':live_path,'native_evidence':native_path,
            'native_checkpoint_manifest':state.directory/'manifest.json','native_checkpoint_page':state.directory/'pages/000001.json',
            'unresolved_packet':unresolved_path,'unresolved_receipt':unresolved_receipt,'header_readback':current_path}.items()}

    def verify(self):
        return selected.validate(self.config,self.source_stage,self.replay_stage,self.proofs,title=self.title)

    def rewrite(self,name,document):
        path=Path(self.proofs[name]['path']);write_json(path,document);self.proofs[name]=handle(path)

    def test_dispatcher_accepts_five_appended_and_one_retained_human_note(self):
        """Append count equality must not reject authenticated retained review availability."""
        before={Path(h['path']):Path(h['path']).read_bytes() for h in self.proofs.values()}
        try:result=self.verify()
        except ValueError as error:self.fail(f'Authentic retained-plus-appended supplement must verify: {error}')
        self.assertEqual(6,len(result['selected_review_ids']))
        self.assertEqual(5,len(result['appended_review_ids']))
        self.assertEqual(1,len(result['retained_pending_review_ids']))
        self.assertEqual(5,len(result['held_review_ids']))
        self.assertEqual(1,len(result['routing_diagnostic_review_ids']))
        self.assertEqual(0,result['posted_credits_created'])
        self.assertEqual('complete',result['diagnostics']['status'])
        self.assertFalse(result['diagnostics']['diagnostic_resolution_claimed'])
        self.assertEqual(1,len(result['diagnostics']['existing_resolved_review_ids']))
        self.assertEqual(before,{path:path.read_bytes() for path in before})

    def test_missing_or_foreign_retained_current_row_is_rejected(self):
        for foreign in (False,True):
            with self.subTest(foreign=foreign):
                current=copy.deepcopy(self.receipt['readback']);blocks=current['structuredContent']['sheets'][0]['data']
                if foreign:blocks[-1]['rowData'][0]['values'][0]['userEnteredValue']={'stringValue':'foreign-review'}
                else:blocks.pop()
                self.rewrite('live_readback',{'captured_utc':'2026-10-10T06:26:46Z','readback':current})
                with self.assertRaises(ValueError):self.verify()

    def test_retained_range_overlap_and_foreign_update_are_rejected(self):
        for column in (14,4):
            with self.subTest(column=column):
                receipt=copy.deepcopy(self.receipt)
                if column==14:receipt['requests'][-1]['updateCells']['range']['startColumnIndex']=14
                else:receipt['existing_row']=853
                self.rewrite('publication_receipt',receipt)
                with self.assertRaises(ValueError):self.verify()

    def test_current_source_machine_drift_is_not_hidden_as_human_annotation(self):
        current=copy.deepcopy(self.receipt['readback'])
        current['structuredContent']['sheets'][0]['data'][-1]['rowData'][0]['values'][3]['userEnteredValue']={'numberValue':55}
        self.rewrite('live_readback',{'captured_utc':'2026-10-10T06:26:46Z','readback':current})
        with self.assertRaises(ValueError):self.verify()

    def test_existing_annotation_links_observe_only_bounded_source_owned_rows(self):
        """A saved link must not invent an omitted native row or outcome equivalence."""
        current=json.loads(Path(self.proofs['header_readback']['path']).read_bytes())
        current['readback']['structuredContent']['sheets'][0]['data'][0]['rowData'][0]['values'][9]['userEnteredValue']={
            'stringValue':'Existing links wka-own-0-s01 and wka-intentionally-omitted-s01'}
        self.rewrite('header_readback',current)
        result=self.verify()['diagnostics']
        observations=result.get('native_primary_link_observations',[])
        self.assertEqual(['verified_source_owned_native_review_observation','not_in_bounded_native_capture'],
            [item['status'] for item in observations])
        self.assertTrue(all(item['same_outcome_alias_claimed'] is False for item in observations))
        self.assertFalse(result['diagnostic_resolution_claimed'])


@unittest.skipUnless(os.environ.get('CLOCKIFY_SEP29_NATIVE_FIXTURES')=='1', 'immutable private Sep29 fixture handles not explicitly selected')
class ActualSep29DeliveryTests(unittest.TestCase):
    def test_actual_original_graph_and_current_captures_verify_without_writes(self):
        base=Path('/tmp/clockify-sep29-publication-audit-20261008')
        fresh=Path('/tmp/clockify-sep29-fresh-readback-20261008')
        current=Path('/tmp/clockify-sep29-six-readback-20261010-4YxtQlwT')
        checkpoint=fresh/'checkpoints/446026dc6345c54d1bd6de254e3c9e96301ffdd66881484b2ca564b040232e53'
        paths={'selection':base/'validation.private.json','publication_packet':base/'review-publication.private.json',
            'publication_receipt':base/'sheet-publication-receipt.private.json','live_readback':current/'current.private.json',
            'native_evidence':fresh/'clockify-existing.private.json','native_checkpoint_manifest':checkpoint/'manifest.json',
            'native_checkpoint_page':checkpoint/'pages/000001.json','unresolved_packet':base/'unresolved-publication.private.json',
            'unresolved_receipt':base/'unresolved-sheet-receipt.private.json','header_readback':current/'diagnostics-current.private.json'}
        self.assertEqual('sha256:73fb3b28d5f7ca046acdc45b3ec7d1e37f3d1f9f52c2aa88e382d8877acc0eaa',cycle._digest(paths['selection']))
        self.assertEqual('sha256:c009bc5cda3a8962ba2287decb76da093dcfb51f64b9b38687ea2a24246c3b5d',cycle._digest(paths['live_readback']))
        self.assertEqual('sha256:dbb07bc718d948ce4a9526bc21a76366dd06382b2d406c4187f90d35c9ad58eb',cycle._digest(paths['header_readback']))
        validation=json.loads(paths['selection'].read_bytes());source=Path(validation['source_run']);replay=Path(validation['replay_run'])
        period=json.loads((source/'period-manifest.json').read_bytes())['period']
        receipt=json.loads(paths['publication_receipt'].read_bytes())
        config={'root':str(Path.cwd()),'runs_dir':str(source.parent),'timezone':period['timezone'],
            'workspace_id':period['workspace_id'],'member_id':period['member_id'],'spreadsheet_id':receipt['spreadsheet_id'],'calendly_optional':True}
        snapshots={name:cycle._digest(source/name) for name in ('period-manifest.json','routing.json','review-corrections.jsonl','review-acceptance.jsonl')}
        previous=review.RUNS;review.RUNS=source.parent
        self.addCleanup(setattr,review,'RUNS',previous)
        source_stage=cycle._validate_stage(config,source/'autopilot-result.json','2026-09-29','2026-09-30',replay=False,expected_snapshot_digests=snapshots,historical_state_validation=True)
        replay_stage=cycle._validate_stage(config,replay/'autopilot-result.json','2026-09-29','2026-09-30',replay=True,expected_snapshot_digests=snapshots,source_run_id=source.name,source_run_dir=str(source),historical_state_validation=True)
        before={path:cycle._digest(path) for path in paths.values()}
        try:result=selected.validate(config,source_stage,replay_stage,{name:handle(path) for name,path in paths.items()},title='September 2026 portfolio review')
        except ValueError as error:self.fail(f'Actual immutable Sep29 native graph must verify: {error}')
        self.assertEqual(6,len(result['selected_review_ids']))
        self.assertEqual(5,len(result['held_review_ids']))
        self.assertEqual(1,len(result['routing_diagnostic_review_ids']))
        self.assertEqual(34,len(result['diagnostics']['review_ids']))
        self.assertEqual(1,len(result['diagnostics']['existing_resolved_review_ids']))
        self.assertFalse(result['financial_coverage_claimed'])
        self.assertEqual(0,result['posted_credits_created'])
        self.assertEqual(before,{path:cycle._digest(path) for path in paths.values()})
