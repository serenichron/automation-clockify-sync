"""Availability of factual attendance is neither allocation nor posted credit."""
import copy
import contextlib
import datetime as dt
import importlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest

from scripts import collector_checkpoints as checkpoints, clockify_sync_collect as collector
from scripts import clockify_meeting_publication_alias as aliases
from scripts import clockify_sheet_publish as publisher, clockify_source_adoptions as artifacts
from scripts import clockify_native_sheet_post as native
from scripts import clockify_selected_delivery_adoption as selected
from unittest import mock
import test_work_accounting_pipeline as evidence


class RecordedAttendanceAvailabilityTests(unittest.TestCase):
    def module(self):
        # A missing implementation is an explicit feature failure, not a test import error.
        try:
            return importlib.import_module('scripts.clockify_recorded_attendance_availability')
        except ModuleNotFoundError:
            self.fail('recorded attendance availability consumer is not implemented')

    def test_precise_partition_rejects_equal_sum_gap_and_overlap(self):
        available = self.module()
        start, end = '2026-10-01T09:00:17Z', '2026-10-01T09:57:47Z'
        parts = [(start, '2026-10-01T09:04:00Z'),
                 ('2026-10-01T09:04:00Z', '2026-10-01T09:48:37Z'),
                 ('2026-10-01T09:48:37Z', end)]
        self.assertEqual(3450, available.exact_partition(start, end, parts))
        shifted = [(start, '2026-10-01T09:04:01Z'),
                   ('2026-10-01T09:04:00Z', '2026-10-01T09:48:36Z'),
                   ('2026-10-01T09:48:37Z', end)]
        with self.assertRaisesRegex(ValueError, 'gap|overlap'):
            available.exact_partition(start, end, shifted)
        for parts in ([], [(start, end), (start, end)],
                      [('2026-10-01T09:00:16Z', end)], [(end, start)]):
            with self.subTest(parts=parts), self.assertRaises(ValueError):
                available.exact_partition(start, end, parts)

    def checkpoint(self, root):
        store = checkpoints.PageCheckpointStore(root / 'checkpoint')
        since = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
        until = dt.datetime(2026, 10, 3, tzinfo=dt.UTC)
        identity = collector._fathom_checkpoint_identity(since, until)
        items = [{'recording_id': 'recording-1', 'share_url': 'https://fathom.video/share/one',
                  'recording_start_time': '2026-10-01T09:00:17Z',
                  'recording_end_time': '2026-10-01T09:57:47Z'}]
        state = store.open(identity)
        state = store.append_page(state, payload={'items': items}, continuation={'cursor': None},
            signature=collector._fathom_page_signature(items), metadata={'request_cursor': None})
        state = store.mark_complete(state)
        handle = aliases.artifact_handle
        return {'manifest': handle(state.directory / 'manifest.json'),
                'pages': [handle(state.directory / 'pages/000001.json')]}, items

    def test_original_complete_checkpoint_requires_all_pages_and_provider_signature(self):
        available = self.module()
        with tempfile.TemporaryDirectory() as directory:
            proof, items = self.checkpoint(Path(directory))
            self.assertEqual(items, available.recording_checkpoint(proof, {}))
            with self.assertRaises(ValueError):
                available.recording_checkpoint({**proof, 'pages': []}, {})
            page_path = Path(proof['pages'][0]['path'])
            page = json.loads(page_path.read_bytes())
            page['signature'] = 'wrong-provider-signature'
            page_path.write_text(json.dumps(page))
            manifest_path = Path(proof['manifest']['path'])
            manifest = json.loads(manifest_path.read_bytes())
            manifest['pages'][0]['page_digest'] = checkpoints._digest(page)
            manifest['pages_digest'] = checkpoints._digest(manifest['pages'])
            manifest_path.write_text(json.dumps(manifest))
            # Even a coherently rehashed generic checkpoint is not a valid Fathom page.
            changed = {'manifest': aliases.artifact_handle(manifest_path),
                       'pages': [aliases.artifact_handle(page_path)]}
            with self.assertRaisesRegex(ValueError, 'signature'):
                available.recording_checkpoint(changed, {})

    def test_financial_or_source_resolution_claims_are_rejected(self):
        available = self.module()
        scope = {'review_availability_only': True, 'financial_coverage_claimed': False,
                 'financial_equivalence_claimed': False, 'additional_credited_seconds': 0,
                 'diagnostic_resolution_claimed': False, 'source_debt_resolution_claimed': False}
        available.availability_scope(scope)
        for key in scope:
            value = copy.deepcopy(scope)
            value[key] = 1 if key == 'additional_credited_seconds' else not value[key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                available.availability_scope(value)

    def recording(self):
        start, end = '2026-10-01T09:00:17Z', '2026-10-01T09:57:47Z'
        event = evidence.fathom_event('2026-10-01T09:00:00Z', '2026-10-01T09:57:00Z', 'available')
        event = event.document()
        event['attributes']['title'] = 'Daily Meet'
        event['attributes']['share_url'] = 'https://fathom.video/share/one'
        # Rebuild identity digest after changing source facts.
        event = evidence.evidence_ledger.evidence_event(event['source_type'], event['source_ref'],
            observed_at=event['observed_at'], raw_source_span=event['raw_source_span'],
            attributes=event['attributes']).document()
        ledger = evidence.evidence_ledger.EvidenceLedger(tuple([
            evidence.evidence_ledger.EvidenceEvent.from_document(event)]), {'fathom': {'status': 'complete'}})
        document = {'schema_version': evidence.evidence_ledger.SCHEMA_VERSION,
                    'manifest': ledger.manifest.document(), 'events': [event]}
        from scripts import work_accounting_pipeline as pipeline
        entries, errors = pipeline._recording_events([event], document['manifest'])
        self.assertEqual([], errors)
        proposal = {'review_activity_key': 'wka-original', 'allocation_segment': 1,
            'activity_id': 'activity-original', 'start': '2026-10-01T09:00:00Z',
            'end': '2026-10-01T09:57:00Z', 'duration_seconds': 3420,
            'provenance': {'evidence_ids': [event['evidence_id']],
                           'canonical_meeting_id': entries[0]['meeting'].canonical_id}}
        item = {'recording_id': event['source_ref']['source_id'],
                'share_url': event['attributes']['share_url'], 'title': 'Daily Meet',
                'recording_start_time': start, 'recording_end_time': end}
        return proposal, document, item

    def test_same_recording_identity_accepts_only_exact_or_exact_historical_minute_projection(self):
        available = self.module()
        self.assertTrue(hasattr(available, 'recording_identity'), 'recording identity consumer missing')
        proposal, ledger, item = self.recording()
        self.assertEqual(('2026-10-01T09:00:17Z', '2026-10-01T09:57:47Z'),
                         available.recording_identity(proposal, ledger, item))
        for key, value in (('recording_id', 'foreign'), ('share_url', 'https://fathom.video/share/foreign'),
                           ('title', 'Unrelated meeting'), ('recording_start_time', '2026-10-01T09:01:17Z')):
            changed = {**item, key: value}
            with self.subTest(key=key), self.assertRaises(ValueError):
                available.recording_identity(proposal, ledger, changed)

    def test_current_rows_bind_old_activity_but_never_invent_second_archived_proposal(self):
        available = self.module()
        self.assertTrue(hasattr(available, 'pending_intervals'), 'physical pending partition consumer missing')
        proposal, ledger, item = self.recording()
        row = ['wka-original-s01', '2026-10-01 12:00:17', '2026-10-01 12:04:00',
               223/60, 'Daily meetings', '', 'activity-original', '', 'Daily Meet',
               'pending', 1, 'old-run', 'Original human warning', 'unposted', 'Keep my decision']
        second = copy.deepcopy(row)
        second[0], second[1], second[2], second[3] = ('wka-original-s02', '2026-10-01 12:48:37',
                                                     '2026-10-01 12:57:47', 550/60)
        values = {'wka-original-s01': (2, row), 'wka-original-s02': (3, second)}
        self.assertEqual([('2026-10-01T09:00:17Z', '2026-10-01T09:04:00Z'),
                          ('2026-10-01T09:48:37Z', '2026-10-01T09:57:47Z')],
                         available.pending_intervals(proposal, list(values), values))
        for index, value in ((0, 'unrelated-s01'), (6, 'wrong-activity'), (9, 'approved'),
                             (13, 'posted'), (3, 4)):
            altered = copy.deepcopy(values)
            altered['wka-original-s01'][1][index] = value
            with self.subTest(index=index), self.assertRaises(ValueError):
                available.pending_intervals(proposal, list(values), altered)

    def manual(self, root):
        proposal, ledger, item = self.recording()
        entry = {'id': 'entry-1', 'workspaceId': 'workspace-1', 'userId': 'member-1',
            'description': 'Daily Meet', 'projectId': 'project-1', 'taskId': None,
            'billable': True, 'tagIds': [], 'type': 'REGULAR',
            'timeInterval': {'start': '2026-10-01T09:04:00Z', 'end': '2026-10-01T09:48:37Z',
                             'duration': 'PT44M37S'}}
        segments = [{'start': item['recording_start_time'], 'end': entry['timeInterval']['start'], 'seconds': 223},
                    {'start': entry['timeInterval']['end'], 'end': item['recording_end_time'], 'seconds': 550}]
        assessment = {'recording_id': item['recording_id'], 'canonical_id': proposal['provenance']['canonical_meeting_id'],
            'clockify_entry_id': 'entry-1', 'native_interval': entry['timeInterval'],
            'native_entry_digest': native._digest(entry), 'same_meeting_credit_seconds': 2677,
            'exact_recording_seconds': 3450, 'residual_seconds': 773, 'residual_segments': segments,
            'recommendation': 'Preserve existing same-meeting credit and pending residual.'}
        documents = {'assessment': {'schema': 'october-unresolved-review-packet/v1',
                                    'meeting_duplicate_assessment': {'daily': assessment}},
            'wire': {'workspace_id': 'workspace-1', 'user_id': 'member-1', 'pages': [
                {'page': 1, 'page_size': 1000, 'request_path': '/native/member', 'payload': [entry]},
                {'page': 2, 'page_size': 1000, 'request_path': '/native/member', 'payload': []}]},
            'current': {'read_only': True, 'workspace_id': 'workspace-1', 'user_id': 'member-1',
                        'captured_at': '2026-10-22T10:00:00Z', 'entries': [entry]}}
        def save(name, value):
            path = root / (name + '.json')
            path.write_text(json.dumps(value))
            return aliases.artifact_handle(path)
        handles = {name: save(name, value) for name, value in documents.items()}
        receipt = {'schema': 'clockify-complete-month-read/v1', 'clockify_writes': 0,
            'complete': True, 'intervals_closed_complete': True, 'invalid_interval_count': 0,
            'open_timer_count': 0, 'terminal_empty_page': True,
            'observation_as_of_utc': '2026-10-22T09:00:00Z', 'since_utc': '2026-10-01T00:00:00Z',
            'until_utc': '2026-11-01T00:00:00Z', 'workspace_id': 'workspace-1', 'user_id': 'member-1',
            'wire_evidence_sha256': handles['wire']['sha256'], 'entry_count': 1,
            'pages': [{'page': 1, 'wire_payload_count': 1}, {'page': 2, 'wire_payload_count': 0}]}
        handles['read_receipt'] = save('read_receipt', receipt)
        return handles, proposal, item, segments, documents, save

    def test_saved_same_meeting_assessment_authenticates_existing_get_without_post_authority(self):
        available = self.module()
        self.assertTrue(hasattr(available, 'manual_intersection'), 'manual assessment consumer missing')
        with tempfile.TemporaryDirectory() as directory:
            handles, proposal, item, segments, documents, save = self.manual(Path(directory))
            config = {'workspace_id': 'workspace-1', 'member_id': 'member-1'}
            partition = [(s['start'], s['end']) for s in segments]
            got = available.manual_intersection(handles, proposal, item, partition, config, {})
            self.assertEqual(('2026-10-01T09:04:00Z', '2026-10-01T09:48:37Z'), got)
            for field, value in (('same_meeting_credit_seconds', 2676), ('recording_id', 'foreign'),
                                 ('native_entry_digest', '0'*64), ('canonical_id', 'foreign')):
                changed = copy.deepcopy(documents['assessment'])
                changed['meeting_duplicate_assessment']['daily'][field] = value
                bad = {**handles, 'assessment': save('changed', changed)}
                with self.subTest(field=field), self.assertRaises(ValueError):
                    available.manual_intersection(bad, proposal, item, partition, config, {})

    def test_observation_microseconds_are_preserved_but_interval_microseconds_rejected(self):
        available = self.module()
        with tempfile.TemporaryDirectory() as directory:
            handles, proposal, item, segments, documents, save = self.manual(Path(directory))
            receipt = json.loads(Path(handles['read_receipt']['path']).read_bytes())
            receipt['observation_as_of_utc'] = '2026-10-22T09:00:00.123456Z'
            current = copy.deepcopy(documents['current'])
            current['captured_at'] = '2026-10-22T10:00:00.654321Z'
            proof = {**handles, 'read_receipt': save('fractional-observed', receipt),
                     'current': save('fractional-current', current)}
            partition = [(s['start'], s['end']) for s in segments]
            config = {'workspace_id':'workspace-1', 'member_id':'member-1'}
            self.assertEqual(('2026-10-01T09:04:00Z','2026-10-01T09:48:37Z'),
                available.manual_intersection(proof, proposal, item, partition, config, {}))
            changed = {**item, 'recording_start_time': '2026-10-01T09:00:17.123456Z'}
            with self.assertRaisesRegex(ValueError, 'exact seconds'):
                available.manual_intersection(proof, proposal, changed, partition, config, {})
            for field, value in (('description', 'Changed work'), ('workspaceId', 'foreign'),
                                 ('billable', False)):
                changed = copy.deepcopy(documents['current'])
                changed['entries'][0][field] = value
                bad = {**handles, 'current': save('changed-current', changed)}
                with self.subTest(field=field), self.assertRaises(ValueError):
                    available.manual_intersection(bad, proposal, item, partition, config, {})

    def test_raw_submicrosecond_recording_and_pending_endpoints_are_not_silently_truncated(self):
        available = self.module()
        with self.assertRaisesRegex(ValueError, 'exact seconds'):
            available.exact_partition('2026-10-01T09:00:17.0000001Z', '2026-10-01T09:01:17Z',
                [('2026-10-01T09:00:17Z', '2026-10-01T09:01:17Z')])
        self.assertEqual(60, available.exact_partition('2026-10-01T09:00:17.0000000Z',
            '2026-10-01T09:01:17Z', [('2026-10-01T09:00:17Z', '2026-10-01T09:01:17.0000000Z')]))
        proposal, ledger, item = self.recording()
        changed = {**item, 'recording_start_time': '2026-10-01T09:00:17.0000001Z'}
        with self.assertRaisesRegex(ValueError, 'exact seconds'):
            available.recording_identity(proposal, ledger, changed)
        row = ['wka-original-s01', '2026-10-01 12:00:17', '2026-10-01 12:57:47',
               57.5, 'Daily meetings', '', 'activity-original', '', 'Daily Meet',
               'pending', 1, 'old-run', 'Original human warning', 'unposted', '']
        for index in (1, 2):
            changed_row = copy.deepcopy(row)
            changed_row[index] += '.0000001'
            with self.subTest(index=index), self.assertRaisesRegex(ValueError, 'exact seconds'):
                available.pending_intervals(proposal, [row[0]], {row[0]: (2, changed_row)})

    def test_manual_native_raw_endpoints_and_current_regular_type_are_enforced(self):
        available = self.module()
        with tempfile.TemporaryDirectory() as directory:
            handles, proposal, item, segments, documents, save = self.manual(Path(directory))
            config = {'workspace_id':'workspace-1', 'member_id':'member-1'}
            partition = [(s['start'], s['end']) for s in segments]
            for endpoint in ('start', 'end'):
                changed = copy.deepcopy(documents['current'])
                changed['entries'][0]['timeInterval'][endpoint] = changed['entries'][0]['timeInterval'][endpoint].replace('Z', '.0000001Z')
                proof = {**handles, 'current': save('fractional-live-'+endpoint, changed)}
                with self.subTest(surface='current', endpoint=endpoint), self.assertRaisesRegex(ValueError, 'exact seconds'):
                    available.manual_intersection(proof, proposal, item, partition, config, {})
                wire = copy.deepcopy(documents['wire'])
                entry = wire['pages'][0]['payload'][0]
                entry['timeInterval'][endpoint] = entry['timeInterval'][endpoint].replace('Z', '.0000001Z')
                assessment = copy.deepcopy(documents['assessment'])
                assessment['meeting_duplicate_assessment']['daily']['native_interval'] = entry['timeInterval']
                assessment['meeting_duplicate_assessment']['daily']['native_entry_digest'] = native._digest(entry)
                receipt = json.loads(Path(handles['read_receipt']['path']).read_bytes())
                wire_handle = save('fractional-wire-'+endpoint, wire)
                receipt['wire_evidence_sha256'] = wire_handle['sha256']
                proof = {**handles, 'wire': wire_handle, 'assessment': save('fractional-assessment-'+endpoint, assessment),
                         'read_receipt': save('fractional-receipt-'+endpoint, receipt)}
                with self.subTest(surface='original', endpoint=endpoint), self.assertRaisesRegex(ValueError, 'exact seconds'):
                    available.manual_intersection(proof, proposal, item, partition, config, {})
            changed = copy.deepcopy(documents['current'])
            changed['entries'][0]['type'] = 'TIME_OFF'
            proof = {**handles, 'current': save('time-off-live', changed)}
            with self.assertRaisesRegex(ValueError, 'REGULAR|type'):
                available.manual_intersection(proof, proposal, item, partition, config, {})

    def test_dispatcher_selects_new_availability_contract_not_legacy_publication(self):
        available = self.module()
        self.assertTrue(hasattr(available, 'verify'), 'top-level availability consumer missing')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'selection.json'
            path.write_text(json.dumps({'schema_version': 'recorded-attendance-review-availability/v1'}))
            proofs = {'selection': aliases.artifact_handle(path), 'live_readback': {}}
            # Missing source and scope is rejected by the new validator, not by
            # an unrelated historical-publication inventory requirement.
            with self.assertRaisesRegex(ValueError, 'recorded attendance availability'):
                selected.validate({}, {}, {}, proofs, title='October 2026 portfolio review')

    def test_real_native_monthly_result_preserves_retained_debt_and_human_cells(self):
        available = self.module()
        self.assertTrue(hasattr(available, 'verify_diagnostics'), 'native diagnostic availability consumer missing')
        from scripts import clockify_monthly_unresolved as monthly
        from test_monthly_unresolved import frozen_run, MonthlyGateway
        from test_cycle_selected_delivery_adoption import captured
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'current'
            frozen_run(source, 1)
            rows = monthly.project_rows(source)
            old = copy.deepcopy(rows[0])
            provenance = json.loads(old[11])
            provenance['source_run_id'] = 'missing-original'
            old[11] = json.dumps(provenance)
            old[10] = 'My human decision stays'
            gateway = MonthlyGateway([monthly.HEADER, old])
            before = copy.deepcopy(gateway.rows)
            result = publisher.publish_monthly_unresolved(gateway, spreadsheet_id='test',
                sheet_title='September 2026 unresolved evidence', rows=rows, source_dir=source,
                retain_provenance_debt=True)
            def save(name, value):
                path = root / (name + '.json')
                path.write_text(json.dumps(value))
                return aliases.artifact_handle(path)
            before_doc = captured(7, 'September 2026 unresolved evidence', before)
            after_doc = captured(7, 'September 2026 unresolved evidence', gateway.rows)
            # Existing fixture uses sheet-1; target identity is not inferred.
            for doc in (before_doc, after_doc):
                doc['structuredContent']['spreadsheetId'] = 'test'
            proof = {'publication_result': save('result', result), 'before': save('before', before_doc),
                     'after': save('after', after_doc), 'live_readback': save('live', after_doc)}
            load = lambda h: json.loads(artifacts._capture(h, {}))
            got = available.verify_diagnostics(proof, source, {'spreadsheet_id': 'test'},
                'September 2026 portfolio review', load)
            self.assertEqual('complete', got['status'])
            self.assertFalse(got['diagnostic_resolution_claimed'])
            self.assertEqual(1, got['retained_provenance_debt_count'])
            changed = copy.deepcopy(after_doc)
            changed['structuredContent']['sheets'][0]['data'][0]['rowData'][1]['values'][10] = {
                'userEnteredValue': {'stringValue': 'Overwritten human decision'}}
            bad = {**proof, 'live_readback': save('changed-live', changed)}
            with self.assertRaises(ValueError):
                available.verify_diagnostics(bad, source, {'spreadsheet_id': 'test'},
                    'September 2026 portfolio review', load)

    def test_explicit_availability_boundary_does_not_resolve_other_source_debt(self):
        from test_cycle_selected_delivery_adoption import SelectedHistoricalDeliveryTests
        fixture = SelectedHistoricalDeliveryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        debt_path = fixture.state_dir / 'source-coverage.json'
        before = debt_path.read_bytes()
        real_validate = selected.validate
        def availability(*args, **kwargs):
            result = real_validate(*args, **kwargs)
            result['source_debt_resolution_claimed'] = False
            return result
        with mock.patch.object(selected, 'validate', side_effect=availability):
            self.assertEqual('delivered_with_exceptions', fixture.adopt()['status'])
        self.assertEqual(before, debt_path.read_bytes())

    def test_completed_activity_free_source_replay_and_uncompleted_old_archive_full_wrapper(self):
        self._completed_wrapper()

    def test_three_recording_wrapper_combines_saved_manual_intersection_and_retained_diagnostic_debt(self):
        self._completed_wrapper(with_daily=True)

    def _completed_wrapper(self, with_daily=False):
        from test_recorded_attendance_repair_integration import RecordedAttendanceRepairTests, run
        from test_review_run_chained_repair_replay import validate_repair
        from test_cycle_selected_delivery_adoption import captured
        from scripts import work_accounting_pipeline as accounting, collector_receipts
        available = self.module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original_fathom = evidence.fathom_event
            def fathom(*args, **kwargs):
                event = original_fathom(*args, **kwargs)
                return evidence.evidence_ledger.evidence_event(event.source_type, event.source_ref,
                    observed_at=event.observed_at, raw_source_span=event.raw_source_span,
                    attributes={**event.attributes, 'share_url': 'https://fathom.video/share/offline'})
            with mock.patch.object(evidence, 'fathom_event', side_effect=fathom):
                original, corrections, _ = RecordedAttendanceRepairTests().source(root)
            if with_daily:
                # Extend only this synthetic source, then genuinely rerun its
                # accounting/quality/completion before preparing the replay.
                import argparse
                import hashlib
                seal = collector_receipts.load_completion_bundle(original/'completion-bundle.json', run_dir=original)
                old_ledger = json.loads((original/'evidence/evidence-ledger.json').read_bytes())
                event = fathom('2026-08-01T10:00:17Z', '2026-08-01T10:57:47Z', 'available')
                daily = evidence.evidence_ledger.evidence_event('fathom',
                    {'source_type':'fathom', 'source_id':'daily-recording'}, observed_at=event.observed_at,
                    raw_source_span=event.raw_source_span, attributes={**event.attributes, 'title':'Daily Meet'})
                events = tuple(evidence.evidence_ledger.EvidenceEvent.from_document(e) for e in old_ledger['events']) + (daily,)
                new_ledger = evidence.evidence_ledger.EvidenceLedger(events,
                    evidence.evidence_ledger.LedgerManifest.from_document(old_ledger['manifest']).source_inventory)
                (original/'evidence/evidence-ledger.json').write_text(json.dumps({
                    'schema_version':evidence.evidence_ledger.SCHEMA_VERSION,
                    'manifest':new_ledger.manifest.document(), 'events':[e.document() for e in events]}))
                analysis = json.loads((root/'attendance-analysis.json').read_bytes())
                analysis['exceptions'][0]['evidence_ids'].append(daily.evidence_id)
                (root/'attendance-analysis.json').write_text(json.dumps(analysis))
                accounting.run_accounting(original, root=Path(run.ROOT), routing_path=original/'routing.json',
                    corrections_path=original/'review-corrections.jsonl', analysis_fixture=root/'attendance-analysis.json')
                validate_repair(original, root/'runs', root/'source-items.json')
                slice_ = argparse.Namespace(slice_id=seal.slice_id,
                    since=dt.datetime.fromisoformat(seal.since_utc.replace('Z','+00:00')),
                    until=dt.datetime.fromisoformat(seal.until_utc.replace('Z','+00:00')))
                collector_receipts.write_completion_bundle(original/'completion-bundle.json',
                    collector_receipts.build_completion_bundle(original, slice_=slice_))
                manifest = json.loads((original/'period-manifest.json').read_bytes())
                manifest['artifacts'][0]['digest'] = 'sha256:'+hashlib.sha256((original/'completion-bundle.json').read_bytes()).hexdigest()
                del manifest['manifest_digest']
                manifest['manifest_digest'] = 'sha256:'+hashlib.sha256(json.dumps(
                    manifest, ensure_ascii=False, sort_keys=True, separators=(',',':')).encode()).hexdigest()
                (original/'period-manifest.json').write_text(json.dumps(manifest))
            with mock.patch.object(run, 'RUNS', root / 'runs'):
                source = run._prepare_repair_run(original, corrections_override=corrections)
                result = accounting.run_accounting(source, root=Path(run.ROOT), routing_path=source/'routing.json',
                    corrections_path=source/'review-corrections.jsonl', analysis_fixture=run._repair_analysis_fixture(source))
                proposals = result['proposals']
                validate_repair(source, root/'runs', root/'repair-items.json')
                run._finalize_repair_completion(source)
            with mock.patch.object(run, 'RUNS', root / 'runs'), \
                 mock.patch.dict(os.environ, {'CLOCKIFY_ANALYZER_PRIMARY_URL': '', 'CLOCKIFY_ANALYZER_FALLBACK_URL': ''}), \
                 mock.patch.object(run, '_sealed_replay_transport', side_effect=AssertionError('network forbidden')), \
                 contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, run.main(['--replay-from', str(source), '--runs-root', str(root/'runs'),
                                             '--state', str(root/'replay-items.json')]))
            replay = next((root/'runs').glob('*-replay-*'))
            handle = aliases.artifact_handle
            names = {'proposals': 'proposals.json', 'accounting': 'work-accounting-result.json',
                     'ledger': 'evidence/evidence-ledger.json', 'routing': 'routing.json',
                     'quality': 'quality_report.json', 'receipt': 'completion-bundle.json'}
            def record(directory, basis):
                return {'run_id': directory.name, 'basis': basis,
                    'artifacts': {**{k: handle(directory/v) for k, v in names.items()},
                                  'replay': handle(replay/'replay-integrity.json')}}
            pair = {'source': record(source, 'completed-review-run'),
                    'replay': record(replay, 'completed-review-replay')}
            def stage(directory):
                return {'run_dir': str(directory), 'bundle_digest': json.loads(
                    (directory/'completion-bundle.json').read_bytes())['bundle_digest'],
                    'proposals_digest': handle(directory/'proposals.json')['sha256'],
                    'review_ids': [publisher.stable_review_id(p) for p in proposals]}
            def save(name, value):
                path = root/(name+'.json')
                path.write_text(json.dumps(value))
                return handle(path)
            # This historical factual witness deliberately has no proposals.json
            # and no completion/publication receipt.
            archived = {'accounting': save('archived-accounting', json.loads((source/'work-accounting-result.json').read_bytes())),
                        'ledger': save('archived-ledger', json.loads((source/'evidence/evidence-ledger.json').read_bytes()))}
            ledger = json.loads((source/'evidence/evidence-ledger.json').read_bytes())
            items = []
            for proposal in proposals:
                event = artifacts._source_events(proposal, ledger)[0]
                items.append({'recording_id': event['source_ref']['source_id'], 'title': event['attributes']['title'],
                    'share_url': event['attributes']['share_url'], 'recording_start_time': proposal['start'],
                    'recording_end_time': proposal['end']})
            store = checkpoints.PageCheckpointStore(root/'fathom')
            checkpoint = store.open(collector._fathom_checkpoint_identity(
                dt.datetime(2026,8,1,tzinfo=dt.UTC), dt.datetime(2026,8,2,tzinfo=dt.UTC)))
            checkpoint = store.append_page(checkpoint, payload={'items': items}, continuation={'cursor': None},
                signature=collector._fathom_page_signature(items), metadata={'request_cursor': None})
            checkpoint = store.mark_complete(checkpoint)
            title = 'August 2026 portfolio review'
            rows = [publisher.proposal_row(p, source.name) for p in proposals]
            manual_proof = None
            if with_daily:
                daily_index = next(i for i,p in enumerate(proposals)
                    if p['provenance'].get('recorded_meeting_title') == 'Daily Meet')
                proposal, item = proposals[daily_index], items[daily_index]
                _, _, _, _, documents, save_manual = self.manual(root)
                wire = documents['wire']
                entry = wire['pages'][0]['payload'][0]
                entry['timeInterval'] = {'start':'2026-08-01T10:04:00Z','end':'2026-08-01T10:48:37Z','duration':'PT44M37S'}
                assessment = documents['assessment']['meeting_duplicate_assessment']['daily']
                assessment.update(recording_id=item['recording_id'], canonical_id=proposal['provenance']['canonical_meeting_id'],
                    native_interval=entry['timeInterval'], native_entry_digest=native._digest(entry),
                    residual_segments=[{'start':item['recording_start_time'],'end':entry['timeInterval']['start'],'seconds':223},
                        {'start':entry['timeInterval']['end'],'end':item['recording_end_time'],'seconds':550}])
                current = documents['current']
                current['entries'] = [entry]
                manual_proof = {name:save_manual('combined-'+name, value) for name,value in documents.items()}
                receipt = json.loads((root/'read_receipt.json').read_bytes())
                receipt.update(since_utc='2026-08-01T00:00:00Z',until_utc='2026-09-01T00:00:00Z',
                    wire_evidence_sha256=manual_proof['wire']['sha256'])
                manual_proof['read_receipt'] = save_manual('combined-receipt',receipt)
                row = rows[daily_index]
                first, second = copy.deepcopy(row), copy.deepcopy(row)
                first[1:4] = ['2026-08-01 13:00:17','2026-08-01 13:04:00',223/60]
                second[0] = proposal['review_activity_key']+'-s02'
                second[1:4] = ['2026-08-01 13:48:37','2026-08-01 13:57:47',550/60]
                rows[daily_index:daily_index+1] = [first,second]
            capture = captured(1, title, [publisher.HEADER, *rows])
            selection = {'schema_version': available.SCHEMA, **available.SCOPE, 'source_pair': pair,
                'source_semantic_analysis': handle(source/'semantic-analysis.json'),
                'attendance_authority': {k:handle(source/v) for k,v in available.batches.RECORDED_ATTENDANCE_FILES.items()},
                'fathom_checkpoint': {'manifest': handle(checkpoint.directory/'manifest.json'),
                                     'pages': [handle(checkpoint.directory/'pages/000001.json')]},
                'represented_sources': {'archive': archived}, 'recordings': [],
                'spreadsheet_id': 'sheet-1', 'sheet_title': title, 'sheet_id': 1,
                'primary_capture': save('primary', capture), 'diagnostics': None}
            for proposal, item in zip(proposals, items, strict=True):
                rid = publisher.stable_review_id(proposal)
                is_daily = with_daily and proposal['provenance'].get('recorded_meeting_title') == 'Daily Meet'
                selection['recordings'].append({'source_review_id': rid, 'source_proposal_sha256': available.pending.digest(proposal),
                    'recording_id': item['recording_id'], 'provider_item_sha256': available.pending.digest(item),
                    'represented_source': 'archive', 'original_review_id': rid,
                    'original_proposal_sha256': available.pending.digest(proposal),
                    'pending_review_ids': [rid,proposal['review_activity_key']+'-s02'] if is_daily else [rid],
                    'manual_assessment': manual_proof if is_daily else None})
            if with_daily:
                from scripts import clockify_monthly_unresolved as monthly
                from test_monthly_unresolved import MonthlyGateway
                diagnostic_rows = monthly.project_rows(source)
                self.assertTrue(diagnostic_rows, 'combined fixture needs genuine diagnostic rows')
                retained = copy.deepcopy(diagnostic_rows[0])
                provenance = json.loads(retained[11])
                provenance['source_run_id'] = 'unavailable-original'
                retained[11], retained[10] = json.dumps(provenance), 'Keep human diagnostic decision'
                gateway = MonthlyGateway([monthly.HEADER,retained])
                # The fixture gateway exposes this title; bind to that real
                # synthetic surface rather than teaching production a fake one.
                diagnostic_title = monthly.title_for_review(title)
                gateway.spreadsheet = lambda _: {'sheets':[{'properties':{'sheetId':7,'title':diagnostic_title,
                    'gridProperties':{'rowCount':1000,'columnCount':12}}}]}
                before = copy.deepcopy(gateway.rows)
                result = publisher.publish_monthly_unresolved(gateway,spreadsheet_id='sheet-1',
                    sheet_title=diagnostic_title,rows=diagnostic_rows,source_dir=source,retain_provenance_debt=True)
                selection['diagnostics'] = {'publication_result':save('combined-diagnostic-result',result),
                    'before':save('combined-diagnostic-before',captured(7,diagnostic_title,before)),
                    'after':save('combined-diagnostic-after',captured(7,diagnostic_title,gateway.rows)),
                    'live_readback':save('combined-diagnostic-live',captured(7,diagnostic_title,gateway.rows))}
            proofs = {'selection': save('selection', selection), 'live_readback': save('live', capture)}
            original_bytes = {path: path.read_bytes() for path in source.rglob('*') if path.is_file()}
            config = {'spreadsheet_id':'sheet-1','workspace_id':'workspace-1','member_id':'member-1'}
            got = selected.validate(config,stage(source),stage(replay),proofs,title=title)
            self.assertEqual('complete', got['source_review_availability']['status'])
            self.assertEqual('complete' if with_daily else 'missing', got['diagnostics']['status'])
            self.assertEqual([3507, 5115,3450] if with_daily else [3507,5115],
                [r['precise_recording_seconds'] for r in got['recordings']])
            if with_daily:
                self.assertEqual(2677,sum(r['existing_same_meeting_intersection_seconds'] for r in got['recordings']))
                self.assertEqual(1,got['diagnostics']['retained_provenance_debt_count'])
                self.assertFalse(got['diagnostics']['diagnostic_resolution_claimed'])
            self.assertFalse(got['historical_allocation_or_publication_equivalence_claimed'])
            self.assertEqual(original_bytes, {path:path.read_bytes() for path in original_bytes})
            selection['recordings'].pop()
            proofs['selection'] = save('incomplete', selection)
            with self.assertRaisesRegex(ValueError, 'exhaustive'):
                selected.validate(config,stage(source),stage(replay),proofs,title=title)


if __name__ == '__main__':
    unittest.main()
