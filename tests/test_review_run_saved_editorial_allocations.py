"""Editorial source derivation must never allocate time again."""
import copy
import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_review_run as fixtures
from scripts import review_corrections, work_accounting_pipeline as accounting
from test_review_run_chained_repair_replay import validate_repair

run = fixtures.review_run


class SavedEditorialAllocationTests(unittest.TestCase):
    def test_external_editorial_input_overrides_cannot_bypass_pinned_snapshots(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);runs=root/'runs'
            source=fixtures.ReviewRunResultTests._write_real_offline_replay_source(runs,root)
            p=json.loads((source/'proposals.json').read_text())[0]
            logs=[]
            for number,description in enumerate(['SC — Clarified image generation work','SC — Reviewed image generation work']):
                path=root/f'corrections-{number}.jsonl'
                record=review_corrections.build_decision({'id':'rvi-source','current':p},decision='modify',reviewer='agent',reviewed_at='2026-10-08T14:00:00Z',correction_categories=['wording'],rationale='Exact local review target.',field_patch={'description':{'op':'replace','value':description}})
                review_corrections.append_decision(path,record);logs.append(path)
            with mock.patch.object(run,'RUNS',runs):
                child=run._prepare_repair_run(source,corrections_override=logs[0],preserve_editorial_allocations=True)
            with self.assertRaisesRegex(accounting.WorkAccountingError,'editorial correction snapshot differs'):
                accounting.run_accounting(child,root=fixtures.ROOT,routing_path=child/'routing.json',corrections_path=logs[1])
            other_routing=root/'other-routing.json'
            other_routing.write_text('{}')
            with self.assertRaisesRegex(accounting.WorkAccountingError,'editorial configured routing snapshot differs'):
                accounting.run_accounting(child,root=fixtures.ROOT,routing_path=other_routing,corrections_path=child/'review-corrections.jsonl')

    def test_native_configured_route_choice_does_not_approve_posting(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);runs=root/'runs'
            write_json=fixtures.write_json
            def write_configured_fixture(path,value):
                if path.name=='routing.json':
                    value=copy.deepcopy(value)
                    value['session_routes'][0].update(project_suffix='project2',tag_suffixes=['processes'])
                write_json(path,value)
            with mock.patch.object(fixtures,'write_json',side_effect=write_configured_fixture):
                source=fixtures.ReviewRunResultTests._write_real_offline_replay_source(runs,root)
            original=json.loads((source/'work-accounting-result.json').read_text())
            p=original['proposals'][0]
            log=root/'corrections.jsonl'
            record=review_corrections.build_decision({'id':'rvi-source','current':p},decision='modify',reviewer='agent editorial review',reviewed_at='2026-10-08T14:00:00Z',correction_categories=['routing'],rationale='Select this exact configured pending classification.',field_patch={'client_project':{'op':'replace','value':'Serenichron Level 2'},'tag_names':{'op':'replace','value':['Processes']}})
            review_corrections.append_decision(log,record)
            with mock.patch.object(run,'RUNS',runs):
                child=run._prepare_repair_run(source,corrections_override=log,preserve_editorial_allocations=True)
                with mock.patch.object(accounting.work_allocator,'allocate_work',side_effect=AssertionError('editorial repair must not allocate')):
                    result=accounting.run_accounting(child,root=fixtures.ROOT,routing_path=child/'routing.json',corrections_path=child/'review-corrections.jsonl')
            self.assertEqual(original['allocation'],result['allocation'])
            self.assertEqual('Serenichron Level 2',result['proposals'][0]['client_project'])
            self.assertEqual(['Processes'],result['proposals'][0]['tag_names'])
            self.assertEqual('project2',result['proposals'][0]['clockify_project_suffix'])
            self.assertEqual(['processes'],result['proposals'][0]['tag_suffixes'])
            self.assertTrue(result['proposals'][0]['billable'])
            self.assertFalse(result['editorial_derivation']['posting_approval'])
            self.assertTrue(all(not choice['posting_approval'] for choice in result['editorial_derivation']['route_choices']))

    def test_flag_cannot_enter_fresh_collection_or_retry(self):
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(2,run.main(['--preserve-editorial-allocations']))

    def test_skip_and_effort_corrections_are_rejected_before_derivation(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);runs=root/'runs'
            source=fixtures.ReviewRunResultTests._write_real_offline_replay_source(runs,root)
            p=json.loads((source/'proposals.json').read_text())[0]
            for decision,categories,patch in [('skip',['allocation'],{}),('modify',['allocation'],{'duration_minutes':{'op':'replace','value':1}})]:
                with self.subTest(decision=decision):
                    log=root/(decision+'.jsonl')
                    r=review_corrections.build_decision({'id':'rvi-source','current':p},decision=decision,reviewer='agent',reviewed_at='2026-10-08T14:00:00Z',correction_categories=categories,rationale='Exact local review target.',field_patch=patch)
                    review_corrections.append_decision(log,r)
                    with mock.patch.object(run,'RUNS',runs),self.assertRaises(run.ReviewRunError):
                        run._prepare_repair_run(source,corrections_override=log,preserve_editorial_allocations=True)

    def test_tampered_saved_accounting_is_never_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);runs=root/'runs'
            source=fixtures.ReviewRunResultTests._write_real_offline_replay_source(runs,root)
            p=json.loads((source/'proposals.json').read_text())[0]
            log=root/'corrections.jsonl'
            r=review_corrections.build_decision({'id':'rvi-source','current':p},decision='modify',reviewer='agent',reviewed_at='2026-10-08T14:00:00Z',correction_categories=['wording'],rationale='Exact local review target.',field_patch={'description':{'op':'replace','value':'SC — Clarified image generation work'}})
            review_corrections.append_decision(log,r)
            with mock.patch.object(run,'RUNS',runs):
                child=run._prepare_repair_run(source,corrections_override=log,preserve_editorial_allocations=True)
                path=child/'editorial-source/work-accounting-result.json'
                document=json.loads(path.read_text());document['proposals'][0]['duration_seconds']+=60
                path.write_text(json.dumps(document))
                with self.assertRaisesRegex(accounting.WorkAccountingError,'hash differs'):
                    accounting.run_accounting(child,root=fixtures.ROOT,routing_path=child/'routing.json',corrections_path=child/'review-corrections.jsonl')

    def test_completed_parent_wording_edit_preserves_every_saved_allocation(self):
        # Calling the normal allocator or changing a saved segment must fail.
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);runs=root/'runs'
            source=fixtures.ReviewRunResultTests._write_real_offline_replay_source(runs,root)
            before=fixtures.run_tree_snapshot(source)
            original=json.loads((source/'work-accounting-result.json').read_text())
            p=original['proposals'][0]
            record=review_corrections.build_decision({'id':'rvi-source','current':p},decision='modify',reviewer='agent editorial review',reviewed_at='2026-10-08T14:00:00Z',correction_categories=['wording'],rationale='Clarify this exact source outcome without new time.',field_patch={'description':{'op':'replace','value':'SC — Clarified image generation work'}})
            corrections=root/'corrections.jsonl'
            review_corrections.append_decision(corrections,record)
            with mock.patch.object(run,'RUNS',runs):
                try:
                    child=run._prepare_repair_run(source,corrections_override=corrections,preserve_editorial_allocations=True)
                except TypeError as exc:
                    self.fail(f'saved-allocation editorial derivation is unavailable: {exc}')
                with mock.patch.object(accounting.work_allocator,'allocate_work',side_effect=AssertionError('editorial repair must not allocate')):
                    result=accounting.run_accounting(child,root=fixtures.ROOT,routing_path=child/'routing.json',corrections_path=child/'review-corrections.jsonl',analysis_fixture=run._repair_analysis_fixture(child))
                self.assertEqual(original['allocation'],result['allocation'])
                self.assertEqual(len(original['proposals']),len(result['proposals']))
                self.assertEqual('SC — Clarified image generation work',result['proposals'][0]['description'])
                for old,new in zip(original['proposals'],result['proposals']):
                    self.assertEqual({k:v for k,v in old.items() if k not in {'description','rendered_description'}},{k:v for k,v in new.items() if k not in {'description','rendered_description'}})
                self.assertEqual('pass',result['correction_regression']['results'][0]['status'])
                validate_repair(child,runs,root/'scratch-review-state.json')
                run._finalize_repair_completion(child)
                replay=run._prepare_replay_run(child)
                second=accounting.run_accounting(replay,root=fixtures.ROOT,routing_path=replay/'routing.json',corrections_path=replay/'review-corrections.jsonl',analysis_fixture=run._replay_analysis_fixture(child,replay))
                self.assertEqual(result,second)
                integrity=run.derive_replay_integrity(child,replay)
                self.assertEqual('pass',integrity['status'],integrity['failures'])
            self.assertEqual(before,fixtures.run_tree_snapshot(source))


if __name__=='__main__':unittest.main()
