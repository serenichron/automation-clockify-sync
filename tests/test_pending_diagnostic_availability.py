"""Portable typed diagnostic representations using real sealed source graphs.

No historical delivery receipt, provider, or acceptance mock. The private
full-graph regression covers the actual102+12 delivery through selected.validate.
These tests isolate the source/member/kind and warning-only boundaries.
"""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts import evidence_ledger
from scripts import clockify_monthly_unresolved as monthly
from scripts import clockify_pending_diagnostic_availability as availability
from scripts import clockify_source_adoptions as artifacts


def digest(value):
    return 'sha256:'+hashlib.sha256(json.dumps(value,sort_keys=True,
        separators=(',',':'),ensure_ascii=False).encode()).hexdigest()


class RetainedDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.event = evidence_ledger.evidence_event('codex_session',
            {'source_id':'portable-observation','machine':'test'},
            observed_at='2026-10-06T12:00:00Z',
            raw_source_span={'start':'2026-10-06T12:00:00Z','end':'2026-10-06T12:05:00Z'},
            attributes={'description':'Unconfirmed source observation'})
        self.pins = {}
        self.prior = self.source('original','low_confidence')
        self.current = self.source('current','insufficient_evidence')
        self.reset_mapping()

    def source(self, name, kind):
        root = self.root/name
        ledger = evidence_ledger.EvidenceLedger((self.event,),timezone='Europe/Bucharest')
        ambiguities = [{'id':'ambiguous-observation','exception_kind':kind,
            'evidence_ids':[self.event.evidence_id]}]
        documents = {
            'work-accounting-result.json':{'proposals':[],'ambiguous':ambiguities},
            'proposals.json':[], 'ambiguous.json':ambiguities,
            'evidence/evidence-ledger.json':{'schema_version':evidence_ledger.SCHEMA_VERSION,
                'manifest':ledger.manifest.document(),'events':[self.event.document()]},
            'semantic-analysis.json':{'activities':[]},
            'run-report.json':{'date_range':{'since':'2026-10-05T21:00:00Z','until':'2026-10-06T21:00:00Z'}},
        }
        for relative,value in documents.items():
            path = root/relative
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(json.dumps(value))
            self.pins[str(path)] = {'sha256':'sha256:'+hashlib.sha256(path.read_bytes()).hexdigest()}
        return root

    def handles(self, root):
        return {name:{'path':str(root/name),'sha256':self.pins[str(root/name)]['sha256']}
            for name in availability.FILES}

    def reset_mapping(self):
        requested = monthly.project_rows(self.current)[0]
        original = monthly.project_rows(self.prior)[0]
        old = monthly.rows_for_layout([original],monthly.LEGACY_LAYOUT)[0]
        self.canonical = {requested[0]:requested}
        self.cells = {old[0]:(2,old)}
        native = {'values':[{'userEnteredValue':{'stringValue':v}} for v in old]}
        self.native_cells = {2:native}
        current_exception = json.loads((self.current/'ambiguous.json').read_bytes())[0]
        original_exception = json.loads((self.prior/'ambiguous.json').read_bytes())[0]
        same = requested[3] == old[3]
        self.mapping = {
            'current_diagnostic_id':requested[0], 'represented_diagnostic_id':old[0],
            'represented_source':str(self.prior),'current_source':str(self.current),
            'current_source_handles':self.handles(self.current),
            'represented_source_handles':{k:v for k,v in self.handles(self.prior).items()
                if k in ('evidence/evidence-ledger.json','ambiguous.json','semantic-analysis.json','work-accounting-result.json')},
            'represented_current_native_cells':copy.deepcopy(old),
            'represented_current_native_cell_data':copy.deepcopy(native),
            'represented_current_sheet_row':2, 'human_disposition_preserved':'needs_review',
            'represented_machine_digest':monthly.machine_digest(old),
            'current_canonical_row':requested,'current_canonical_row_sha256':digest(requested),
            'current_native_exception':current_exception,'current_exception_sha256':digest(current_exception),
            'represented_native_exception':original_exception,'represented_native_exception_sha256':digest(original_exception),
            'current_evidence_ids':[self.event.evidence_id],'represented_evidence_ids':[self.event.evidence_id],
            'current_kind':requested[3],'represented_native_kind':old[3],'represented_visible_kind':old[3],
            'representation_mode':'retained-same-evidence-and-kind' if same else 'retained-same-evidence-changed-kind',
            'same_kind_alias_claimed':same,'warning':availability.SAME_WARNING if same else availability.CHANGED_WARNING,
            'evidence_pairs':[{'current_evidence_id':self.event.evidence_id,
                'represented_evidence_id':self.event.evidence_id,
                'current_sealed_atom':self.event.document(),'represented_sealed_atom':self.event.document(),
                'current_object_sha256':digest(self.event.document()),'represented_object_sha256':digest(self.event.document()),
                'relation':'exact-sealed-event'}],
            'review_availability_only':True,'literal_current_canonical_delivery_claimed':False,
            'work_completed_or_posted_credit_inferred':False,'additional_minutes':0,
            'diagnostic_work_interval_status':'unknown',
        }

    def verify(self, mapping=None):
        graph = availability.SourceGraph(self.pins,
            lambda handle:json.loads(artifacts._capture(handle,{})))
        return availability.verify_retained(self.mapping if mapping is None else mapping,
            source=self.current,canonical=self.canonical,graph=graph,
            cells=self.cells,native_cells=self.native_cells)

    def rejection(self, field, value, message):
        self.verify()  # Unsupported or broken baseline is not safety coverage.
        mapping = copy.deepcopy(self.mapping)
        mapping[field] = value
        with self.assertRaisesRegex(ValueError,message):
            self.verify(mapping)

    def test_changed_kind_preserves_original_view_not_literal_current_delivery(self):
        """Collapsing distinct diagnostic kinds into aliases loses the warning."""
        result = self.verify()
        self.assertEqual('insufficient_evidence',result['current_kind'])
        self.assertEqual('low_confidence',result['represented_kind'])
        self.assertEqual('retained-same-evidence-changed-kind',result['representation_mode'])
        self.assertNotEqual(result['current_diagnostic_id'],result['represented_diagnostic_id'])
        self.assertTrue(result['review_availability_only'])
        self.assertIn('never additional work or resolution',result['warning'])

    def test_same_kind_is_distinct_from_changed_kind_representation(self):
        """Always treating retained views as kind changes misreports same-kind evidence."""
        self.current = self.source('same-kind-current','low_confidence')
        self.reset_mapping()
        result = self.verify()
        self.assertEqual('retained-same-evidence-and-kind',result['representation_mode'])
        self.assertEqual(result['current_diagnostic_id'],result['represented_diagnostic_id'])
        self.assertIn('does not prove completed outcome',result['warning'])

    def test_relabelled_kind_cannot_claim_a_same_kind_alias(self):
        self.rejection('same_kind_alias_claimed',True,'relabelled')

    def test_retained_disposition_cannot_be_changed(self):
        self.rejection('human_disposition_preserved','resolved','disposition')

    def test_credit_claim_is_not_review_availability(self):
        self.rejection('additional_minutes',8,'claims resolution, work or credit')

    def test_literal_current_delivery_is_not_retained_representation(self):
        self.rejection('literal_current_canonical_delivery_claimed',True,'claims resolution, work or credit')

    def test_duplicate_evidence_pair_does_not_count_as_exhaustive_membership(self):
        self.rejection('evidence_pairs',self.mapping['evidence_pairs']*2,'one-to-one and exhaustive')

    def test_rehashed_substituted_atom_must_match_real_sealed_ledger(self):
        self.verify()
        mapping = copy.deepcopy(self.mapping)
        pair = mapping['evidence_pairs'][0]
        pair['current_sealed_atom']['attributes']['description'] = 'Invented completed outcome'
        pair['current_object_sha256'] = digest(pair['current_sealed_atom'])
        with self.assertRaisesRegex(ValueError,'sealed source atom was substituted'):
            self.verify(mapping)

    def test_rehashed_source_bytes_cannot_replace_canonical_projection(self):
        self.verify()
        path = self.prior/'ambiguous.json'
        path.write_text('[]')
        self.pins[str(path)]['sha256'] = 'sha256:'+hashlib.sha256(path.read_bytes()).hexdigest()
        with self.assertRaisesRegex(ValueError,'ambiguity artifact differs'):
            self.verify()


class ContextWarningTests(unittest.TestCase):
    def test_temporal_overlap_is_bounded_warning_not_owned_work_or_duplicate(self):
        """Promoting shared timing metadata to financial coverage breaks this boundary."""
        actual = {'id':'actual-one','timeInterval':{
            'start':'2026-10-06T12:00:00Z','end':'2026-10-06T12:05:00Z'}}
        exception = {'timing_context_intervals':[{'start':'2026-10-06T12:04:00Z','end':'2026-10-06T12:09:00Z'}]}
        text,pairs = availability.context_warning(exception,{'actual-one':actual})
        self.assertEqual(1,len(pairs))
        self.assertEqual(60,pairs[0]['overlap_seconds'])
        self.assertEqual('2026-10-06T12:04:00+00:00',pairs[0]['overlap_start'])
        self.assertEqual('2026-10-06T12:05:00+00:00',pairs[0]['overlap_end'])
        self.assertTrue(pairs[0]['warning_only'])
        self.assertFalse(pairs[0]['owned_work_interval_or_duplicate_inferred'])
        self.assertIn('no owned work interval or additional minutes',text)
        self.assertIn('same-work/duplicate status remains unknown',text)

    def test_invalid_context_metadata_is_not_promoted_to_work_interval(self):
        text,pairs = availability.context_warning({'timing_context_intervals':[
            {'start':'2026-10-06T12:00:00','end':'2026-10-06T12:10:00'},
            {'start':'2026-10-06T12:10:00Z','end':'2026-10-06T12:00:00Z'}]},
            {'a':{'id':'a','timeInterval':{'start':'2026-10-06T12:00:00Z','end':'2026-10-06T12:05:00Z'}}})
        self.assertEqual(('',[]),(text,pairs))


if __name__ == '__main__':
    unittest.main()
