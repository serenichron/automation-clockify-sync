"""Portable source-anchor and exact native availability boundaries.

No fabricated completed run, Sep25 receipt or acceptance mock. The private
genuine53+14 fixture separately exercises selected.validate end to end.
"""
import copy
import unittest

from scripts import evidence_ledger
from scripts import clockify_mixed_review_availability as mixed


def event(content='Outcome: bounded draft prepared', role='assistant'):
    return evidence_ledger.evidence_event('claude_bursts_event',
        {'source_type':'claude_bursts','source_id':'session:event:2','machine':'test',
         'session_id':'session','ordinal':2},observed_at='2026-09-30T12:00:20Z',
        attributes={'role':role,'kind':'message','content':content}).document()


class OutcomeAnchorTests(unittest.TestCase):
    def setUp(self):
        self.current=event();self.original=event()
        self.pair={'new':mixed.event_receipt(self.current),
            'original_posted_own':mixed.event_receipt(self.original)}

    def test_exact_own_assistant_outcome_anchor_is_authenticated(self):
        """Rejecting authentic shared completion objects loses review representation."""
        self.assertIsNone(mixed.verify_anchors([self.pair],[self.current],[self.original]))

    def test_shared_source_without_declared_result_anchor_is_not_own_result(self):
        """Inferring outcome equivalence from source intersection promotes context."""
        with self.assertRaisesRegex(ValueError,'anchor is missing'):
            mixed.verify_anchors([],[self.current],[self.original])

    def test_rehashed_other_completion_cannot_replace_original_owned_anchor(self):
        """Trusting declaration hashes instead of original source membership is unsafe."""
        fabricated=event('Outcome: unrelated completed production deployment')
        pair=copy.deepcopy(self.pair);pair['new']=mixed.event_receipt(fabricated)
        with self.assertRaisesRegex(ValueError,'anchor differs'):
            mixed.verify_anchors([pair],[self.current],[self.original])

    def test_user_instruction_is_not_an_assistant_completion_anchor(self):
        """Treating desired work as original completed work fabricates a result."""
        instruction=event('Prepare this bounded draft','user')
        pair={'new':mixed.event_receipt(instruction),'original_posted_own':mixed.event_receipt(instruction)}
        with self.assertRaisesRegex(ValueError,'anchor differs'):
            mixed.verify_anchors([pair],[instruction],[instruction])

    def test_duplicate_anchor_does_not_create_two_owned_results(self):
        with self.assertRaisesRegex(ValueError,'repeats'):
            mixed.verify_anchors([self.pair,self.pair],[self.current],[self.original])

    def test_identical_recording_object_needs_no_fabricated_assistant_message(self):
        recording=evidence_ledger.evidence_event('fathom',{'source_id':123},
            observed_at='2026-09-30T12:00:00Z',
            raw_source_span={'start':'2026-09-30T12:00:00Z','end':'2026-09-30T12:05:00Z'},
            attributes={'share_url':'https://fathom.video/share/portable'}).document()
        self.assertIsNone(mixed.verify_anchors([],[recording],[recording],same_recording=True))

    def test_recording_id_alone_does_not_authenticate_original_recording_object(self):
        recording=evidence_ledger.evidence_event('fathom',{'source_id':123},
            observed_at='2026-09-30T12:00:00Z',attributes={'title':'original'}).document()
        other=evidence_ledger.evidence_event('fathom',{'source_id':123},
            observed_at='2026-09-30T12:00:00Z',attributes={'title':'other'}).document()
        with self.assertRaisesRegex(ValueError,'recording outcome witness differs'):
            mixed.verify_anchors([],[other],[recording],same_recording=True)


class NativeAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.document={'structuredContent':{'spreadsheetId':'sheet','sheets':[
            {'properties':{'sheetId':7,'title':'Review'},'data':[
                {'startRow':0,'rowData':[{'values':[{'userEnteredValue':{'stringValue':'Identity'}}]}]},
                {'startRow':99,'rowData':[{'values':[
                    {'userEnteredValue':{'stringValue':'current-id'},'note':'human note'},
                    {'userEnteredValue':{'numberValue':4},'userEnteredFormat':{'numberFormat':{'type':'NUMBER'}}}]}]}]}]}}

    def read(self):
        return mixed.native_rows(self.document,'sheet','Review',7,2,header=['Identity',''])

    def test_sparse_native_capture_preserves_typed_values_and_full_cell_data(self):
        """String normalization or dropping notes loses native CAS evidence."""
        rows,raw=self.read()
        self.assertEqual({'current-id':(100,['current-id',4])},rows)
        self.assertIs(type(rows['current-id'][1][1]),int)
        self.assertEqual('human note',raw[100]['values'][0]['note'])
        self.assertEqual({'type':'NUMBER'},raw[100]['values'][1]['userEnteredFormat']['numberFormat'])

    def test_header_is_not_a_review_identity(self):
        self.assertEqual(['current-id'],list(self.read()[0]))

    def test_formula_cannot_masquerade_as_literal_pending_value(self):
        self.document['structuredContent']['sheets'][0]['data'][1]['rowData'][0]['values'][1]['userEnteredValue']={'formulaValue':'=4'}
        with self.assertRaisesRegex(ValueError,'formula'):
            self.read()

    def test_duplicate_native_identity_is_not_an_exhaustive_partition(self):
        block=copy.deepcopy(self.document['structuredContent']['sheets'][0]['data'][1]);block['startRow']=100
        self.document['structuredContent']['sheets'][0]['data'].append(block)
        with self.assertRaisesRegex(ValueError,'duplicates an identity'):
            self.read()

    def test_foreign_sheet_metadata_does_not_prove_current_target(self):
        self.document['structuredContent']['spreadsheetId']='foreign'
        with self.assertRaisesRegex(ValueError,'spreadsheet differs'):
            self.read()


class ScopeTests(unittest.TestCase):
    def test_availability_contract_cannot_expand_to_financial_or_resolution_credit(self):
        document={'schema_version':'mixed-review-availability/v1','preparation':{},'comparison':{},
            'review_availability_only':True,'financial_coverage_claimed':False,
            'diagnostic_resolution_claimed':False,'additional_credited_seconds':0}
        mixed.verify_scope(document)
        for field,value in [('financial_coverage_claimed',True),('diagnostic_resolution_claimed',True),
            ('additional_credited_seconds',240),('additional_credited_seconds',False),('unreviewed_authority',True)]:
            with self.subTest(field=field,value=value),self.assertRaisesRegex(ValueError,'availability-only'):
                mixed.verify_scope({**document,field:value})


if __name__=='__main__':unittest.main()
