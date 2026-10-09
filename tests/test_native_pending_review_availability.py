"""Portable boundaries; genuine completed-source60+87 graph is tested separately."""
import copy
import importlib
import json
import unittest
from scripts import evidence_ledger
from scripts import clockify_mixed_review_availability as mixed
from scripts import clockify_pending_review_selection as pending

class AliasBoundaries(unittest.TestCase):
    def setUp(self):
        self.api=importlib.import_module('scripts.clockify_native_pending_review_availability')
        self.event=evidence_ledger.evidence_event('codex_sessions_event',
            {'source_type':'codex_sessions','source_id':'session:7','machine':'test','session_id':'session','ordinal':7},
            observed_at='2026-10-07T12:00:00Z',attributes={'role':'assistant','kind':'message','content':'Prepared the handoff specification; local commit verified.'}).document()
        self.proposal={'activity_id':'current','duration_seconds':1800,'provenance':{'evidence_ids':[self.event['evidence_id']]}}
        self.prior=copy.deepcopy(self.proposal);self.prior.update(activity_id='prior',duration_seconds=2100)
        self.activity={'activity_id':'current','action':'Prepared','object':'handoff specification','outcome':'specification and verified local commit','evidence_ids':[self.event['evidence_id']]}
        self.old=copy.deepcopy(self.activity);self.old['activity_id']='prior'
        self.alias={'source_semantic_core':{k:v for k,v in self.activity.items() if k!='activity_id'},
            'represented_semantic_core':{k:v for k,v in self.old.items() if k!='activity_id'},
            'source_activity_id':'current','represented_activity_id':'prior',
            'source_seconds':1800,'represented_seconds':2100,
            'outcome_anchors':[{'new':mixed.event_receipt(self.event),'original_posted_own':mixed.event_receipt(self.event)}],
            'review_availability_only':True,'financial_equivalence_claimed':False,'additional_credited_seconds':0}
    def invoke(self):return self.api.alias_relation(self.alias,self.proposal,self.prior,self.activity,self.old,[self.event],[self.event])
    def test_preserves_30_vs_35_pending_allocation_without_equating_credit(self):
        result=self.invoke();self.assertEqual((result['source_seconds'],result['represented_seconds']),(1800,2100))
        self.assertFalse(result['financial_equivalence_claimed']);self.assertEqual(result['additional_credited_seconds'],0)
    def test_shared_source_without_result_anchor_is_rejected(self):
        self.alias['outcome_anchors']=[]
        with self.assertRaisesRegex(ValueError,'anchor'):self.invoke()
    def test_rehashed_foreign_result_anchor_is_rejected(self):
        other=copy.deepcopy(self.event);other['attributes']['content']='Unrelated deployment completed'
        self.alias['outcome_anchors'][0]['new']=mixed.event_receipt(other)
        with self.assertRaisesRegex(ValueError,'anchor'):self.invoke()
    def test_instruction_is_not_completed_outcome(self):
        self.event['attributes']['role']='user'
        self.alias['outcome_anchors']=[{'new':mixed.event_receipt(self.event),'original_posted_own':mixed.event_receipt(self.event)}]
        with self.assertRaisesRegex(ValueError,'anchor'):self.invoke()
    def test_foreign_own_semantic_outcome_is_rejected(self):
        self.alias['represented_semantic_core']['outcome']='different work'
        with self.assertRaisesRegex(ValueError,'semantic'):self.invoke()
    def test_another_activity_cannot_supply_own_semantic_outcome(self):
        self.old['activity_id']='foreign'
        with self.assertRaisesRegex(ValueError,'activity'):self.invoke()
    def test_source_credit_cannot_be_replaced_with_retained_credit(self):
        self.alias['source_seconds']=2100
        with self.assertRaisesRegex(ValueError,'seconds'):self.invoke()
    def test_financial_equivalence_is_never_granted(self):
        self.alias['financial_equivalence_claimed']=True
        with self.assertRaisesRegex(ValueError,'credit'):self.invoke()
    def test_additional_time_is_never_granted(self):
        self.alias['additional_credited_seconds']=300
        with self.assertRaisesRegex(ValueError,'credit'):self.invoke()
    def test_full_sealed_objects_not_ids_alone_are_required(self):
        changed=copy.deepcopy(self.event);changed['attributes']['content']='changed sealed context'
        with self.assertRaisesRegex(ValueError,'sealed'):self.api.alias_relation(self.alias,self.proposal,self.prior,self.activity,self.old,[self.event],[changed])
    def test_semantic_evidence_membership_cannot_hide_context(self):
        self.old['evidence_ids'].append('foreign')
        with self.assertRaisesRegex(ValueError,'semantic'):self.invoke()

class HistoricalWarningBoundaries(unittest.TestCase):
    def setUp(self):
        self.api=importlib.import_module('scripts.clockify_native_pending_review_availability')
        self.warning={'type':'review_proposal_overlap','counterpart_id':'owned-prior','overlap_duration_seconds':60,
            'overlap_start':'2026-10-07T12:00:00Z','overlap_end':'2026-10-07T12:01:00Z'}
        self.other={**self.warning,'counterpart_id':'second-prior'}
        self.rows=[['review-id','','',1,'project','','activity','','','pending',1,'source','human presentation','unposted','']]
        self.actual={'native_review_warnings':{'review-id':[self.warning,self.warning,self.other]}}
        self.current={'native_review_warnings':{'review-id':[self.warning,self.other]}}
        self.seal(self.actual);self.seal(self.current)
    def seal(self,receipt):
        rows=copy.deepcopy(self.rows)
        for row in rows:row[12]=json.dumps(receipt['native_review_warnings'][row[0]],ensure_ascii=False,sort_keys=True)
        receipt['native_projection_rows_sha256']=pending.digest(rows)
    def invoke(self):return self.api.warning_repetition_projection(self.actual,self.current,self.rows)
    def test_only_identical_repetitions_are_retained_as_historical_warning_drift(self):
        result=self.invoke();self.assertEqual(result['original_warnings'],self.actual['native_review_warnings'])
        self.assertNotEqual(result['original_projection_sha256'],result['current_projection_sha256'])
    def test_rehashed_changed_counterpart_is_not_warning_deduplication(self):
        self.actual['native_review_warnings']['review-id'][0]={**self.warning,'counterpart_id':'foreign'};self.seal(self.actual)
        with self.assertRaisesRegex(ValueError,'warning facts'):self.invoke()
    def test_rehashed_changed_overlap_seconds_are_rejected(self):
        self.actual['native_review_warnings']['review-id'][0]={**self.warning,'overlap_duration_seconds':120};self.seal(self.actual)
        with self.assertRaisesRegex(ValueError,'warning facts'):self.invoke()
    def test_reordered_unique_warning_facts_are_rejected(self):
        self.actual['native_review_warnings']['review-id'].reverse();self.seal(self.actual)
        with self.assertRaisesRegex(ValueError,'warning facts'):self.invoke()
    def test_false_original_native_projection_digest_is_rejected(self):
        self.actual['native_projection_rows_sha256']=pending.digest({'forged':'projection'})
        with self.assertRaisesRegex(ValueError,'projection'):self.invoke()
    def test_projection_cannot_hide_interval_credit_or_route_drift(self):
        self.rows[0][3]=2
        with self.assertRaisesRegex(ValueError,'projection'):self.invoke()

if __name__=='__main__':unittest.main()
