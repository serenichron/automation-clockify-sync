"""Sealed old delivery cells remain exact; new publication stays local-time."""
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import unittest
from unittest import mock

from scripts import clockify_review_cycle as cycle, clockify_sheet_publish as publisher
import test_cycle_historical_adoption as historical
from test_review_cycle_delivery import make_run, proposal, write_json


def seal(document):
    body={key:value for key,value in document.items() if key!='receipt_digest'}
    return {**body,'receipt_digest':'sha256:'+hashlib.sha256(json.dumps(body,ensure_ascii=False,
        sort_keys=True,separators=(',',':')).encode()).hexdigest()}


def contract(title,rows):
    body={'spreadsheet_id':'sheet-1','sheet_title':title,'row_ids':[row[0] for row in rows],
          'rows_sha256':'sha256:'+hashlib.sha256(json.dumps(rows,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
    identity=hashlib.sha256(json.dumps(body,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return {**body,'readback_id':'sheet-readback/'+identity,'receipt_id':'sheet-publication/'+identity}


class HistoricalTimestampProjectionTests(unittest.TestCase):
    def setUp(self):
        fixture=historical.HistoricalAdoptionTests();fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.root,self.config=fixture.root,fixture.config
        self.since,self.until=fixture.since,fixture.until
        self.title='September 2026 portfolio review'
        self.proposals=[]
        for index in range(8):
            hour=9+index
            item={**proposal(),'review_activity_key':f'wka-time-{index}','activity_id':f'time-{index}',
                  'start':f'2026-09-07T{hour:02}:00:00Z','end':f'2026-09-07T{hour+1:02}:00:00Z'}
            self.proposals.append(item)
        self.proposals[-1].update(client_project='',clockify_project_suffix='',tag_suffixes=[],tag_names=[],billable=False,
            routing_disposition='unresolved-routing',review_warnings=[{'type':'unresolved_routing',
                'disposition':'unresolved-routing','reason_code':'no_deterministic_route'}])
        source_result=make_run(self.root,'time-source',replay=False,proposals=self.proposals,
            snapshots_from=fixture.source_result.parent)
        replay_result=make_run(self.root,'time-replay',replay=True,source_name='time-source',
            snapshots_from=source_result.parent,proposals=self.proposals,ledger_from=source_result.parent)
        self.source_dir=source_result.parent
        snapshots={name:cycle._digest(self.source_dir/name) for name in fixture.request['adopted_snapshot_digests']}
        self.source=cycle._validate_stage(self.config,source_result,self.since,self.until,replay=False,expected_snapshot_digests=snapshots)
        self.replay=cycle._validate_stage(self.config,replay_result,self.since,self.until,replay=True,
            expected_snapshot_digests=snapshots,source_run_id=self.source['run_id'],source_run_dir=self.source['run_dir'])
        self.current=cycle._delivery_document(self.config,self.since,self.until,self.source,self.replay,
            sheet_title=self.title,publication_profile=None)
        self.old_rows=[]
        for index,item in enumerate(self.proposals):
            row=publisher.proposal_row(item,self.source['run_id'])
            # Literal hand-checked UTC wall labels emitted by the old renderer,
            # not computed with a production timestamp helper.
            row[1:3]=[f'2026-09-07 {9+index:02}:00',f'2026-09-07 {10+index:02}:00']
            self.old_rows.append(row)
        self.old=copy.deepcopy(self.current)
        self.old['publication_receipts']=[contract(self.title,self.old_rows[:7]),contract('unresolved-evidence',self.old_rows[7:])]
        self.old=seal(self.old)
        self.receipt=self.root/'state/delivery-receipts/2026-09-07.json';write_json(self.receipt,self.old)
        self.state={'slices':{self.since:{'until':self.until,'status':'delivered','source':self.source,'replay':self.replay,
            'expected_snapshot_digests':snapshots,'period_manifest':str(self.root/'state/2026-09-07.period-manifest.json'),
            'delivery_receipt':str(self.receipt)}}}

    def verify(self):
        with mock.patch.object(cycle,'run_child_bounded',side_effect=AssertionError('provider child forbidden')), \
             mock.patch.object(publisher,'publish',side_effect=AssertionError('provider publication forbidden')):
            cycle._validate_delivered_state(self.config,self.state)

    def test_sealed_september_seven_both_literal_partitions_validate_without_republication(self):
        """Catches regenerating historical receipt B/C through the current timezone renderer."""
        self.assertEqual([7,1],[len(item['row_ids']) for item in self.old['publication_receipts']])
        self.assertNotEqual(self.old['publication_receipts'],self.current['publication_receipts'])
        before={str(path):(path.read_bytes(),path.stat().st_mtime_ns) for path in self.root.rglob('*') if path.is_file()}
        try:
            self.verify();self.verify()
        except cycle.CycleError as error:
            self.fail(f'Original literal historical receipt must remain verifiable: {error}')
        self.assertEqual(before,{str(path):(path.read_bytes(),path.stat().st_mtime_ns) for path in self.root.rglob('*') if path.is_file()})

    def test_rehashed_non_time_or_unknown_timestamp_cells_are_rejected(self):
        """Catches a compatibility fallback that trusts row hashes instead of exact source projection."""
        for column,value in ((0,'wka-forged-s01'),(3,61),(4,'Other project'),(8,'Invented accomplishment'),
                             (9,'approved'),(11,'another-source'),(12,'Invented warning'),
                             (1,'2026-09-07 10:00'),(2,'2026-09-07 11:00')):
            with self.subTest(column=column):
                rows=copy.deepcopy(self.old_rows);rows[0][column]=value
                forged=copy.deepcopy(self.old)
                forged['publication_receipts']=[contract(self.title,rows[:7]),contract('unresolved-evidence',rows[7:])]
                write_json(self.receipt,seal(forged))
                before=self.receipt.read_bytes()
                with self.assertRaises(cycle.CycleError):self.verify()
                self.assertEqual(before,self.receipt.read_bytes())

    def test_receipt_cannot_mix_original_and_current_projection(self):
        """Catches accepting arbitrary per-row or per-partition combinations of timestamp renderers."""
        for split in ('partitions','one_cell'):
            with self.subTest(split=split):
                forged=copy.deepcopy(self.old)
                if split=='partitions':
                    forged['publication_receipts'][1]=self.current['publication_receipts'][1]
                else:
                    rows=copy.deepcopy(self.old_rows);rows[0][2]='2026-09-07 13:00'
                    forged['publication_receipts'][0]=contract(self.title,rows[:7])
                write_json(self.receipt,seal(forged))
                with self.assertRaises(cycle.CycleError):self.verify()

    def test_source_or_receipt_identity_drift_is_not_a_timestamp_exception(self):
        """Catches waiving sealed source/bundle identity while adapting only old time cells."""
        for field,value in (('source',{'run_id':'different'}),('receipt_digest','sha256:'+'0'*64),
                            ('schema_version','clockify-review-delivery/v999')):
            with self.subTest(field=field):
                forged=copy.deepcopy(self.old);forged[field]=value
                write_json(self.receipt,forged if field=='receipt_digest' else seal(forged))
                with self.assertRaises(cycle.CycleError):self.verify()
        write_json(self.receipt,self.old)
        path=self.source_dir/'proposals.json'
        proposals=json.loads(path.read_text());proposals[0]['description']='Changed sealed source'
        write_json(path,proposals)
        with self.assertRaises(cycle.CycleError):self.verify()

    def test_current_receipt_and_new_default_rows_keep_bucharest_rendering(self):
        """Catches leaking historical compatibility into newly generated publication cells."""
        write_json(self.receipt,self.current);self.verify()
        current_rows=[]
        for index,item in enumerate(self.proposals):
            row=publisher.proposal_row(item,self.source['run_id'])
            self.assertEqual([f'2026-09-07 {12+index:02}:00',f'2026-09-07 {13+index:02}:00'],row[1:3])
            current_rows.append(row)
        self.assertEqual([contract(self.title,current_rows[:7]),contract('unresolved-evidence',current_rows[7:])],
            cycle._expected_publication_receipts(self.config,self.source,sheet_title=self.title,publication_profile=None))

    def test_fresh_publication_readbacks_do_not_opt_into_old_projection(self):
        """Catches relaxing active publication admission while fixing sealed receipt revalidation."""
        expected=cycle._expected_publication_receipts(self.config,self.source,sheet_title=self.title,publication_profile=None)
        with self.assertRaises(cycle.CycleError):
            cycle._validated_publication_document({'schema_version':'sheet-publication-result/v1','status':'published',
                'external_writes':True,'clockify_writes':0,'publications':self.old['publication_receipts']},
                expected,source_dir=self.source_dir)

    def test_default_aware_local_naive_and_seconds_projection_is_unchanged(self):
        """Catches altering the normal timezone/default formatter to accommodate old receipts."""
        for value,want in (
            ('2026-09-07T09:00:00Z','2026-09-07 12:00'),
            ('2026-09-07T09:00:00+03:00','2026-09-07 09:00'),
            ('2026-09-07T09:00:00-04:00','2026-09-07 16:00'),
            ('2026-09-07 09:00','2026-09-07 09:00'),
            ('2026-09-07T09:00:05Z','2026-09-07 12:00:05'),
            ('2026-01-07T09:00:00Z','2026-01-07 11:00')):
            with self.subTest(value=value):self.assertEqual(want,publisher._timestamp(value))


if __name__=='__main__':unittest.main()
