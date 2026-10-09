"""Recovery adoption retains the native source and separately bounded replay."""
import argparse
import copy
import datetime as dt
import json
from pathlib import Path
import shutil
import unittest
from unittest import mock

from ops.systemd.user import clockify_review_cycle_release as release_helper
from scripts import clockify_review_cycle as cycle, clockify_review_run as review
from scripts import clockify_sync_collect as collector, collector_receipts
from scripts import clockify_source_debt_recover as recovery, source_coverage
from scripts import clockify_sheet_publish as publisher
import test_cycle_selected_delivery_adoption as selected_tests
import test_source_debt_recovery as recovery_tests
from test_review_cycle_delivery import write_json


class RecoveredSelectedDeliveryTests(unittest.TestCase):
    def setUp(self):
        selection = selected_tests.SelectedHistoricalDeliveryTests()
        selection.setUp()
        self.addCleanup(selection.doCleanups)
        self.selection = selection
        self.root, self.config = selection.root, dict(selection.config)
        self.since, self.until = selection.since, selection.until
        native = recovery_tests.SourceDebtRecoveryTests()
        native.setUp()
        self.addCleanup(native.doCleanups)
        self.addCleanup(native.tearDown)
        self.native = native
        native.root = self.root
        native.runs = self.root / 'runs'
        native.checkpoints = selection.state_dir / 'collector-checkpoints'
        native.routing = json.loads((selection.source / 'routing.json').read_text())
        self.release = self.root / 'releases' / ('a' * 40)
        repository = Path(cycle.__file__).resolve().parents[1]
        shutil.copytree(repository / 'scripts', self.release / 'scripts',
                        ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(repository / 'ops', self.release / 'ops',
                        ignore=shutil.ignore_patterns('__pycache__'))
        write_json(self.release / 'routing.json', native.routing)
        write_json(self.release / 'fleet.json', native.fleet)
        runtime = {'canonical_root': str(self.release),
            'collector_path': str(self.release / 'scripts/clockify_sync_collect.py'),
            'git_sha': None, 'git_dirty': None}
        release_helper._make_payload_read_only(self.release)
        self.release.chmod(0o555)
        tree = release_helper._tree_manifest(self.release)
        self.release.chmod(0o755)
        write_json(self.release / '.clockify-release.json', {
            'schema_version': 'clockify-user-release/v1', 'git_sha': self.release.name,
            'root': str(self.release), 'tree_manifest': tree,
            'tree_digest': release_helper._manifest_digest(tree),
            'routing_sha256': cycle._digest(self.release / 'routing.json').removeprefix('sha256:')})
        (self.release / '.clockify-release.json').chmod(0o444)
        self.release.chmod(0o555)
        self.config.update(root=str(self.release), _runtime_identity=runtime)
        for module in (collector, recovery, review):
            patcher = mock.patch.object(module, 'ROOT', self.release)
            patcher.start(); self.addCleanup(patcher.stop)
            patcher = mock.patch.object(module, 'RUNS', native.runs)
            patcher.start(); self.addCleanup(patcher.stop)
        patcher = mock.patch.object(collector, 'collector_runtime_identity', return_value=runtime)
        patcher.start(); self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(collector.os.environ,
            {'CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT': str(native.checkpoints)})
        patcher.start(); self.addCleanup(patcher.stop)

        since = dt.datetime.fromisoformat(self.since).replace(tzinfo=collector.BUCHAREST)
        until = dt.datetime.fromisoformat(self.until).replace(tzinfo=collector.BUCHAREST)
        slices = collector.plan_slices(since, until, zone=collector.BUCHAREST)
        compatibility = collector._backlog_compatibility_version(native.routing, native.fleet,
            calendly_optional=True, coordinator='omarchy-precision')
        identity = collector.BacklogIdentity(since_utc=collector.iso_utc(since),
            until_utc=collector.iso_utc(until), timezone=collector.BUCHAREST.key,
            max_days=2, compatibility_version=compatibility)
        backlog = collector.BacklogStore(native.checkpoints)
        checkpoint = backlog.open(identity, slices)
        parent = collector._slice_run_dir(slices[0], compatibility)
        with mock.patch.object(collector, 'collect_remote_sessions', return_value=native.failed_peer()):
            collector._collect_slice(argparse.Namespace(enrich=False, calendly_optional=True),
                native.routing, native.fleet, {'_missing': True}, {'_missing': True}, since, until,
                'fixture', collector.PageCheckpointStore(checkpoint.directory / 'source-checkpoints'),
                parent, calendly_env={'_missing': True}, coordinator='omarchy-precision')
        collector._write_pending_slice_finalization(parent, identity, slices[0])
        for name in review._RECONCILIATION_INPUTS.values():
            shutil.copyfile(selection.source / name, parent / name)
        self.copy_analysis(selection.source, parent)
        pb = collector_receipts.build_completion_bundle(parent, slice_=slices[0])
        collector_receipts.write_completion_bundle(parent / 'completion-bundle.json', pb)
        self.result(parent, pb)
        backlog.record_complete(checkpoint, slices[0].slice_id,
            (parent / 'completion-bundle.json').resolve(), cycle._digest(parent / 'completion-bundle.json'))
        snapshots = {name: cycle._digest(parent / name) for name in self.selection.request['adopted_snapshot_digests']}
        parent_stage = cycle._validate_stage(self.config, parent / 'autopilot-result.json', self.since, self.until,
            replay=False, expected_snapshot_digests=snapshots)
        interval = cycle._interval_from_stage(self.config, 'peer/macbook', parent_stage)
        store = source_coverage.SourceDebtStore()
        debt = store.record_failure(interval, failure_class='peer_unavailable', retryable=True,
            resume_state_digest='sha256:' + 'b'*64, attempted_at='2026-09-10T00:00:00Z')
        prior = copy.deepcopy(selection.prior)
        prior.update(source=parent_stage, recovery_parents={debt.debt_id: parent_stage})
        attempt, command = cycle._recovery_attempt(prior, debt, parent_stage, self.config)
        # The native verifier pins /usr/bin/python3, as the installed executor does.
        command[0] = '/usr/bin/python3'
        attempt['command_digest'] = cycle._value_digest(command)
        peer = native.healthy_peer()
        peer['codex_sessions'] = [{'session_id':f'diagnostic-{i}', 'start':'2026-09-07 12:00',
                                  'title':f'Original diagnostic {i}'} for i in range(51)]
        with mock.patch.object(collector, 'collect_remote_sessions', return_value=peer):
            self.source = recovery.recover(parent, 'peer/macbook', attempt['attempt_id']).run_dir
        review._snapshot_recovery_inputs(self.source, parent)
        self.copy_analysis(selection.source, self.source)
        sb = review._finalize_recovery_completion(self.source)
        self.result(self.source, sb)
        receipt = recovery.seal_recovery_receipt(self.source)
        self.receipt_path = receipt.path
        source_stage = cycle._validate_stage(self.config, self.source / 'autopilot-result.json', self.since, self.until,
            replay=False, expected_snapshot_digests=snapshots)
        attempt.update(phase='finished_complete', result_path=source_stage['result_path'],
            result_digest=source_stage['result_digest'], returned_bundle_digest=sb.bundle_digest,
            requested_source_outcome='complete', recovery_receipt_path=str(receipt.path), recovery_receipt_digest=receipt.digest)
        store.record_complete(interval, completion_bundle_digest=sb.bundle_digest, completed_at='2026-09-10T00:01:00Z')
        source_coverage.write(selection.state_dir / 'source-coverage.json', store.document())
        prior['source_recovery_history'] = [cycle._recovery_source_history(self.config,
            {**prior,'source_run_id':parent_stage['run_id'],'review_ids':parent_stage['review_ids']},
            self.since,self.until,source_stage,attempt)]
        prior.update(source=source_stage, status='source_verified')
        self.prior = prior
        state_path = selection.state_dir / 'review-cycle-state.json'
        state = json.loads(state_path.read_text()); state['slices'][self.since] = prior; write_json(state_path, state)
        # Produce the replay in its original second root, never move/relabel it.
        replay_root = self.root / 'offline' / 'runs'; replay_root.mkdir(parents=True)
        self.cached_source = replay_root / self.source.name
        shutil.copytree(self.source, self.cached_source)
        with mock.patch.object(review, 'RUNS', replay_root):
            self.replay = review._prepare_replay_run(self.cached_source)
            self.copy_analysis(self.cached_source, self.replay)
            review._verify_replay_integrity(self.cached_source, self.replay)
            rb = review._finalize_replay_completion(self.cached_source, self.replay)
            self.result(self.replay, rb, replay=True)
        self.request = {**selection.request, 'source_result':str(self.source / 'autopilot-result.json'),
            'replay_result':str(self.replay / 'autopilot-result.json'), 'runs_root':str(native.runs),
            'replay_runs_root':str(replay_root), 'source_result_digest':cycle._digest(self.source / 'autopilot-result.json'),
            'replay_result_digest':cycle._digest(self.replay / 'autopilot-result.json'),
            'runtime_identity_digest':sb.runtime_identity_digest, 'prior_record_digest':cycle._value_digest(prior),
            'source_provenance':{'kind':'source_debt_recovery', 'debt_id':debt.debt_id,
                'attempt_id':attempt['attempt_id'], 'recovery_receipt_path':str(receipt.path),
                'recovery_receipt_digest':receipt.digest,
                'cached_source_result':str(self.cached_source/'autopilot-result.json'),
                'cached_source_result_digest':cycle._digest(self.cached_source/'autopilot-result.json')}}
        selection.source = self.source
        selection.selected = [publisher.proposal_row(p,self.source.name) for p in selection.proposals[12:]]
        packet = copy.deepcopy(selection.packet)
        packet.update(source_run=str(self.source),replay_run=str(self.replay),rows=selection.selected)
        declared = json.loads(Path(selection.proofs['selection']['path']).read_text())
        declared.update(source_run=str(self.source),replay_run=str(self.replay),
            source_proposals_sha256=cycle._digest(self.source/'proposals.json').removeprefix('sha256:'))
        selection.rewrite_proof('selection',declared)
        packet['selection_sha256']=selection.proofs['selection']['sha256'].removeprefix('sha256:')
        selection.rewrite_proof('publication_packet',packet)
        receipt_doc=json.loads(Path(selection.proofs['publication_receipt']['path']).read_text())
        receipt_doc.update(packet_sha256=selection.proofs['publication_packet']['sha256'].removeprefix('sha256:'))
        from test_cycle_selected_delivery_adoption import captured
        receipt_doc['readback']=captured(1,selection.title,selection.selected,start=830)
        selection.rewrite_proof('publication_receipt',receipt_doc)
        live=json.loads(Path(selection.proofs['live_readback']['path']).read_text())
        live['identity_and_portfolio_readback']=captured(1,selection.title,selection.selected,start=830)
        selection.rewrite_proof('live_readback',live)
        (self.root/'initial-old-diagnostics').rename(self.root/'retained-initial-old-diagnostics')
        selection.attach_diagnostic_proofs()
        self.before = {str(p):(p.read_bytes(), p.stat().st_mtime_ns)
            for root in (self.source, self.cached_source, self.replay) for p in root.rglob('*') if p.is_file()}
        # Restore the real read-only checkpoint projection for delivery proof;
        # only the collector transport was replaced during synthetic collection.
        next(p for p in native.patches if getattr(p,'attribute',None)=='fetch_clockify').stop()

    @staticmethod
    def copy_analysis(source, target):
        for name in ('semantic-analysis.json','work-accounting-result.json','quality_report.json',
                     'review-snapshot.json','proposals.json','ambiguous.json','fathom-reconciliation.json'):
            shutil.copyfile(source / name, target / name)

    def result(self, target, bundle, replay=False):
        original = self.selection.source
        result = json.loads((original / 'autopilot-result.json').read_text())
        result.update(run_id=target.name, run_dir=str(target), completion_bundle_digest=bundle.bundle_digest,
                      completion_bundle=bundle.document())
        result['paths'] = {k:(v.replace(str(original),str(target),1) if v else v) for k,v in result['paths'].items()}
        if replay: result['paths']['replay_integrity'] = str(target / 'replay-integrity.json')
        transition = json.loads((target / 'run-report.json').read_text()).get('source_debt_recovery')
        if transition and not replay:
            result['source_debt_recovery'] = {'source':'peer/macbook', 'attempt_id':transition['attempt_id'],
                'status':'complete', 'transition_digest':transition['transition_digest']}
        write_json(target / 'autopilot-result.json', result)

    def test_native_recovered_source_and_original_separate_replay_are_authenticated(self):
        # Exact source/replay paths remain original: the cache is evidence, not
        # a replacement operational source or a relabeled recovery receipt.
        with mock.patch.object(cycle, 'run_child_bounded', side_effect=AssertionError('inference forbidden')), \
             mock.patch.object(collector,'clockify_get',side_effect=AssertionError('provider forbidden')):
            result = cycle.adopt_historical_slice(self.config,self.request)
            repeated = cycle.adopt_historical_slice(self.config,self.request)
        self.assertEqual('delivered_with_exceptions',result['status'])
        self.assertEqual(result,repeated)
        state=json.loads((self.selection.state_dir/'review-cycle-state.json').read_text())
        record=state['slices'][self.since]
        self.assertEqual(str(self.source),record['source']['run_dir'])
        self.assertEqual(str(self.replay),record['replay']['run_dir'])
        adoption=json.loads(Path(record['historical_adoption_receipt']).read_text())
        self.assertEqual(self.prior,adoption['superseded_record'])
        cycle._validate_delivered_state(self.config,state)
        self.assertEqual(self.prior['source_recovery_history'],record['source_recovery_history'])
        self.assertEqual(self.prior['recovery_attempts'],record['recovery_attempts'])
        self.assertEqual(self.before, {str(p):(p.read_bytes(),p.stat().st_mtime_ns)
            for root in (self.source,self.cached_source,self.replay) for p in root.rglob('*') if p.is_file()})

    def assert_rejected_without_writes(self, request):
        state_paths = (self.selection.state_dir/'review-cycle-state.json',
                       self.selection.state_dir/'source-coverage.json')
        before = [(p.read_bytes(),p.stat().st_mtime_ns) for p in state_paths]
        with mock.patch.object(cycle,'run_child_bounded',side_effect=AssertionError('inference forbidden')), \
             mock.patch.object(collector,'clockify_get',side_effect=AssertionError('provider forbidden')), \
             self.assertRaises(cycle.CycleError):
            cycle.adopt_historical_slice(self.config,request)
        self.assertEqual(before,[(p.read_bytes(),p.stat().st_mtime_ns) for p in state_paths])
        self.assertFalse((self.selection.state_dir/'delivery-receipts'/f'{self.since}.json').exists())
        self.assertFalse((self.selection.state_dir/'historical-adoption-receipts'/f'{self.since}.json').exists())

    def test_unpinned_roots_provenance_receipt_attempt_and_result_are_rejected(self):
        cases = []
        for key in ('source_result_digest','replay_result_digest','runtime_identity_digest'):
            cases.append((key,{**self.request,key:'sha256:'+'f'*64}))
        for key in ('attempt_id','recovery_receipt_digest','cached_source_result_digest'):
            cases.append((key,{**self.request,'source_provenance':{
                **self.request['source_provenance'],key:'sha256:'+'f'*64}}))
        cases.extend([
            ('broad source root',{**self.request,'runs_root':str(self.root)}),
            ('broad replay root',{**self.request,'replay_runs_root':str(self.replay.parent.parent)}),
            ('different receipt locator',{**self.request,'source_provenance':{
                **self.request['source_provenance'],'recovery_receipt_path':str(self.root/'foreign.json')}}),
            ('different cache locator',{**self.request,'source_provenance':{
                **self.request['source_provenance'],'cached_source_result':str(self.source/'autopilot-result.json')}}),
            ('wrong provenance',{**self.request,'source_provenance':self.selection.request['source_provenance']}),
        ])
        for label,request in cases:
            with self.subTest(label=label):self.assert_rejected_without_writes(request)

    def test_original_cache_bytes_inventory_inputs_and_symlinks_are_not_relaxed(self):
        for relative in ('autopilot-result.json','completion-bundle.json','evidence/evidence-ledger.json',
                         'run-report.json','semantic-analysis.json','work-accounting-result.json',
                         'quality_report.json','proposals.json','routing.json','review-corrections.jsonl',
                         'review-acceptance.jsonl','period-manifest.json'):
            path=self.cached_source/relative; original=path.read_bytes(); mode=path.stat().st_mode & 0o777
            with self.subTest(relative=relative):
                try:
                    path.chmod(0o600);path.write_bytes(original+b'\n')
                    self.assert_rejected_without_writes(self.request)
                finally:path.write_bytes(original);path.chmod(mode)
        extra=self.cached_source/'foreign-artifact.json'
        try:
            extra.write_text('{}\n');self.assert_rejected_without_writes(self.request)
        finally:extra.unlink()
        original_path=self.cached_source/'proposals.json';original=original_path.read_bytes()
        original_path.unlink();original_path.symlink_to(self.source/'proposals.json')
        try:self.assert_rejected_without_writes(self.request)
        finally:original_path.unlink();original_path.write_bytes(original)
        link=self.root/'replay-root-alias';link.symlink_to(self.replay.parent,target_is_directory=True)
        self.assert_rejected_without_writes({**self.request,'replay_runs_root':str(link)})

    def test_coherent_but_foreign_receipt_history_attempt_and_debt_fail_native_audit(self):
        state_path=self.selection.state_dir/'review-cycle-state.json'
        original_state=state_path.read_bytes()
        for label,alter in (
            ('attempt phase',lambda record:record['recovery_attempts'][self.request['source_provenance']['debt_id']].update(phase='verified_complete')),
            ('history digest',lambda record:record['source_recovery_history'][0].update(history_digest='sha256:'+'f'*64)),
            ('rehashed history',lambda record:record['source_recovery_history'][0].update(replacement_source_digest='sha256:'+'f'*64)),
        ):
            try:
                state=json.loads(original_state);record=state['slices'][self.since];alter(record)
                if label=='rehashed history':
                    item=record['source_recovery_history'][0]
                    item['history_digest']=cycle._value_digest({k:v for k,v in item.items() if k!='history_digest'})
                write_json(state_path,state)
                with self.subTest(label=label):
                    self.assert_rejected_without_writes({**self.request,'prior_record_digest':cycle._value_digest(record)})
            finally:state_path.write_bytes(original_state)
        receipt=self.receipt_path;original=receipt.read_bytes();mode=receipt.stat().st_mode & 0o777
        try:
            document=json.loads(original);document['artifacts']['result_sha256']='sha256:'+'f'*64
            document['receipt_digest']=cycle._value_digest({k:v for k,v in document.items() if k!='receipt_digest'})
            receipt.chmod(0o600);write_json(receipt,document);receipt.chmod(mode)
            state=json.loads(original_state);record=state['slices'][self.since]
            record['recovery_attempts'][self.request['source_provenance']['debt_id']]['recovery_receipt_digest']=document['receipt_digest']
            write_json(state_path,state)
            request={**self.request,'prior_record_digest':cycle._value_digest(record),'source_provenance':{
                **self.request['source_provenance'],'recovery_receipt_digest':document['receipt_digest']}}
            self.assert_rejected_without_writes(request)
        finally:
            receipt.chmod(0o600);receipt.write_bytes(original);receipt.chmod(mode);state_path.write_bytes(original_state)
        debt_path=self.selection.state_dir/'source-coverage.json';original_debt=debt_path.read_bytes()
        try:
            source_coverage.write(debt_path,source_coverage.SourceDebtStore().document())
            self.assert_rejected_without_writes(self.request)
        finally:debt_path.write_bytes(original_debt)

    def test_rehashed_earlier_history_is_native_authenticated_with_last_valid(self):
        state_path=self.selection.state_dir/'review-cycle-state.json'
        state=json.loads(state_path.read_text());record=state['slices'][self.since]
        earlier=copy.deepcopy(record['source_recovery_history'][0])
        earlier.update(recovery_attempt_id='sha256:'+'e'*64,
                       replacement_source_digest='sha256:'+'d'*64,
                       source_run_id='foreign-parent')
        earlier['history_digest']=cycle._value_digest({k:v for k,v in earlier.items() if k!='history_digest'})
        record['source_recovery_history'].insert(0,earlier)
        write_json(state_path,state)
        self.assert_rejected_without_writes({**self.request,'prior_record_digest':cycle._value_digest(record)})

    def test_earlier_authenticated_history_preserves_original_transition_identities(self):
        state_path=self.selection.state_dir/'review-cycle-state.json'
        state=json.loads(state_path.read_text());record=state['slices'][self.since]
        earlier=copy.deepcopy(record['source_recovery_history'][0])
        earlier.update(recovery_attempt_id='sha256:'+'e'*64,replacement_source_digest='sha256:'+'d'*64)
        earlier['history_digest']=cycle._value_digest({k:v for k,v in earlier.items() if k!='history_digest'})
        record['source_recovery_history'].insert(0,earlier)
        retained=copy.deepcopy(record['source_recovery_history']);write_json(state_path,state)
        request={**self.request,'prior_record_digest':cycle._value_digest(record)}
        with mock.patch.object(cycle,'run_child_bounded',side_effect=AssertionError('inference forbidden')), \
             mock.patch.object(collector,'clockify_get',side_effect=AssertionError('provider forbidden')):
            first=cycle.adopt_historical_slice(self.config,request)
            self.assertEqual(first,cycle.adopt_historical_slice(self.config,request))
        adopted=json.loads(state_path.read_text())['slices'][self.since]
        self.assertEqual(retained,adopted['source_recovery_history'])


if __name__ == '__main__': unittest.main()
