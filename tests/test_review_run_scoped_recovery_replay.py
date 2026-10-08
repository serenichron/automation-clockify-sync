"""Scoped quarantine recovery must remain reproducible without inference."""
import copy
import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import semantic_analyzer, work_accounting_pipeline as pipeline
from scripts import clockify_scoped_semantic_recovery as recovery
import test_review_run as fixtures
from test_review_run_chained_repair_replay import validate_repair

review = fixtures.review_run


class ScopedRecoveryReplayTests(unittest.TestCase):
    def test_actor_bound_cached_recovery_is_consumed_by_accounting_cli_offline(self):
        self.assert_cached_recovery_cli(actor_contract=True)

    def test_actorless_cached_recovery_remains_consumable_by_accounting_cli(self):
        self.assert_cached_recovery_cli(actor_contract=False)

    def test_copied_actor_recovery_tamper_is_rejected_without_transport(self):
        self.assert_cached_recovery_cli(actor_contract=True, tamper='provenance')

    def test_injected_actor_routing_is_rejected_without_transport(self):
        self.assert_cached_recovery_cli(actor_contract=True, tamper='routing')

    def assert_cached_recovery_cli(self, *, actor_contract, tamper=False):
        # Catches dropping sealed actor context at the accounting CLI boundary.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / 'runs'
            source = fixtures.ReviewRunResultTests._write_real_offline_replay_source(
                runs, root, failed_review=True, actor_contract=actor_contract,
            )
            original = {p: p.read_bytes() for p in source.rglob('*') if p.is_file()}
            analysis = json.loads((source / 'semantic-analysis.json').read_text())
            failure = next(row for row in analysis['exceptions'] if row['kind'] == 'analyzer_review_failure')
            digest = semantic_analyzer.stable_digest('frt-', failure['evidence_ids'], length=64)
            scope = root / 'scope.json'
            scope.write_text(json.dumps({'evidence_ids': failure['evidence_ids']}))
            endpoint = semantic_analyzer.AnalyzerEndpoint(
                'clockify_analyzer_primary', 'https://offline.invalid/v1/chat/completions',
                semantic_analyzer.DEFAULT_PRIMARY_MODEL,
                revision=semantic_analyzer.DEFAULT_PRIMARY_REVISION,
            )
            output = root / 'recovery'
            with (
                mock.patch.object(semantic_analyzer.AnalyzerEndpoint, 'from_env', return_value=endpoint),
                mock.patch.object(semantic_analyzer, 'http_transport', side_effect=lambda _endpoint, body:
                                  fixtures.analyzer_provider_response(json.loads(body['messages'][-1]['content']))),
                mock.patch.dict(os.environ, {'CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED': 'approved'}),
            ):
                recovery.run(recovery.parse_args([str(source), '--scope-file', str(scope),
                             '--output-dir', str(output), '--failed-review-digest', digest]))
            with (
                mock.patch.object(review, 'RUNS', runs),
                mock.patch.object(semantic_analyzer, 'http_transport', side_effect=AssertionError('cache consume inferred')),
                mock.patch.dict(os.environ, {'CLOCKIFY_ANALYZER_PRIMARY_URL': '', 'CLOCKIFY_ANALYZER_FALLBACK_URL': ''}),
            ):
                verified = recovery.validate_cached_recovery(source, output, [digest])
                child = review._prepare_repair_run(source, scoped_recovery=verified)
                cache = child / 'analyzer-cache-retry.jsonl'
                cache.write_bytes(verified['files']['analyzer-response-cache.jsonl'])
                argv = [str(child), '--root', str(fixtures.ROOT), '--routing', str(child / 'routing.json'),
                        '--corrections', str(child / 'review-corrections.jsonl'), '--analyzer-cache', str(cache),
                        '--failed-review-retry-source', str(review._repair_analysis_fixture(child)),
                        '--failed-review-retry-digest', digest, '--failed-review-retry-cache-only',
                        '--failed-review-retry-scoped-mode', verified['mode']]
                for identity in verified['selected_evidence_ids']:
                    argv += ['--failed-review-retry-selected-evidence-id', identity]
                if tamper == 'provenance':
                    copied = child / 'scoped-recovery/semantic-analysis.json'
                    document = json.loads(copied.read_text())
                    document['failed_review_retry'].pop('actor_contract')
                    copied.write_text(json.dumps(document))
                elif tamper == 'routing':
                    path = child / 'routing.json'
                    document = json.loads(path.read_text())
                    document['semantic_subject_binding'] = {'injected': True}
                    path.write_text(json.dumps(document))
                else:
                    # Exercise the actual standalone child entrypoint, not only main().
                    result = subprocess.run([sys.executable, str(Path(pipeline.__file__).resolve()), *argv],
                                            cwd=root, capture_output=True, text=True)
                    self.assertEqual(0, result.returncode, result.stderr)
                stderr = io.StringIO()
                with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                    code = pipeline.main(argv)
                if tamper:
                    self.assertEqual(2, code)
                    self.assertIn('immutable input changed' if tamper == 'provenance' else
                                  'differs from immutable recovery inputs', stderr.getvalue())
                    self.assertFalse((child / 'work-accounting-result.json').exists())
                    return
                self.assertEqual(0, code, stderr.getvalue())
                recovered = json.loads((child / 'semantic-analysis.json').read_text())
                self.assertEqual(semantic_analyzer.ACTOR_CONTRACT if actor_contract else None,
                                 recovered['failed_review_retry'].get('actor_contract'))
                self.assertEqual([row['evidence_ids'] for row in verified['analysis']['activities']],
                                 [row['evidence_ids'] for row in recovered['activities']])
                self.assertEqual([], recovered['exceptions'])
                self.assertEqual(original, {p: p.read_bytes() for p in original})

    def test_recovered_quarantine_replays_with_no_network_and_unchanged_parent(self):
        self.assert_recovered_quarantine_replay(actor_contract=False)

    def test_actor_recovered_quarantine_replays_through_native_entrypoint(self):
        self.assert_recovered_quarantine_replay(actor_contract=True)

    def assert_recovered_quarantine_replay(self, *, actor_contract):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / 'runs'
            original = fixtures.ReviewRunResultTests._write_real_offline_replay_source(
                runs, root, failed_review=True, actor_contract=actor_contract,
            )
            endpoint = semantic_analyzer.AnalyzerEndpoint(
                'clockify_analyzer_primary', 'https://offline.invalid/v1/chat/completions',
                semantic_analyzer.DEFAULT_PRIMARY_MODEL,
                revision=semantic_analyzer.DEFAULT_PRIMARY_REVISION,
            )
            source_analysis = json.loads((original / 'semantic-analysis.json').read_text())
            failure = next(row for row in source_analysis['exceptions']
                           if row['kind'] == 'analyzer_review_failure')
            digest = semantic_analyzer.stable_digest('frt-', failure['evidence_ids'], length=64)
            original_scoped = pipeline.run_scoped_failed_review_retry

            def duplicate_transport(_endpoint, body):
                payload = json.loads(body['messages'][-1]['content'])
                result = fixtures.analyzer_provider_response(payload)
                result['activities'].append(copy.deepcopy(result['activities'][0]))
                return result

            endpoint_patch = lambda name, **_kw: endpoint if name == 'CLOCKIFY_ANALYZER_PRIMARY' else None
            with mock.patch.object(review, 'RUNS', runs):
                parent = review._prepare_repair_run(original)
                parent_cache = parent / 'analyzer-cache-retry.jsonl'
                parent_cache.write_bytes((parent / 'analyzer-cache-used.jsonl').read_bytes())
                with (
                    mock.patch.object(semantic_analyzer.AnalyzerEndpoint, 'from_env', side_effect=endpoint_patch),
                    mock.patch.object(pipeline, 'run_scoped_failed_review_retry', side_effect=lambda *args, **kw:
                                      original_scoped(*args, transport=duplicate_transport,
                                                       private_text_approved=True, **kw)),
                ):
                    pipeline.run_accounting(
                        parent, root=fixtures.ROOT, routing_path=parent / 'routing.json',
                        corrections_path=parent / 'review-corrections.jsonl',
                        analyzer_cache_path=parent_cache,
                        failed_review_retry_source=review._repair_analysis_fixture(parent),
                        failed_review_retry_digest=digest, analyzer_workers=1,
                    )
                validate_repair(parent, runs, root / 'parent-items.json')
                review._finalize_repair_completion(parent)
                parent_before = {name: (parent / name).read_bytes() for name in
                                 ('semantic-analysis.json', 'analyzer-cache-used.jsonl', 'proposals.json')}
                partial = json.loads(parent_before['semantic-analysis.json'])
                quarantine = next(row for row in partial['exceptions']
                                  if row['kind'] == 'analyzer_review_partial_quarantine')
                target = semantic_analyzer.stable_digest('frt-', quarantine['evidence_ids'], length=64)
                child = review._prepare_repair_run(parent)
                child_cache = child / 'analyzer-cache-retry.jsonl'
                child_cache.write_bytes((child / 'analyzer-cache-used.jsonl').read_bytes())
                scoped = pipeline.run_scoped_failed_review_retry
                calls = []

                def repaired_transport(_endpoint, body):
                    calls.append(body)
                    return fixtures.analyzer_provider_response(json.loads(body['messages'][-1]['content']))

                with (
                    mock.patch.object(semantic_analyzer.AnalyzerEndpoint, 'from_env', side_effect=endpoint_patch),
                    mock.patch.object(pipeline, 'run_scoped_failed_review_retry', side_effect=lambda *args, **kw:
                                      scoped(*args, **{**kw, 'transport': repaired_transport,
                                                       'private_text_approved': True})),
                ):
                    pipeline.run_accounting(
                        child, root=fixtures.ROOT, routing_path=child / 'routing.json',
                        corrections_path=child / 'review-corrections.jsonl',
                        analyzer_cache_path=child_cache,
                        failed_review_retry_source=review._repair_analysis_fixture(child),
                        failed_review_retry_digest=target, analyzer_workers=1,
                    )
                self.assertEqual(1, len(calls))
                recovered = json.loads((child / 'semantic-analysis.json').read_text())
                self.assertEqual('scoped_review_v4_citation_quarantine', recovered['failed_review_retry']['mode'])
                if actor_contract:
                    self.assertEqual(semantic_analyzer.ACTOR_CONTRACT, recovered['failed_review_retry']['actor_contract'])
                self.assertTrue(recovered['activities'])
                self.assertFalse(any(row['kind'] == 'analyzer_review_partial_quarantine'
                                     for row in recovered['exceptions']))
                validate_repair(child, runs, root / 'child-items.json')
                review._finalize_repair_completion(child)
                with (
                    mock.patch.dict(os.environ, {'CLOCKIFY_ANALYZER_PRIMARY_URL': '',
                                                 'CLOCKIFY_ANALYZER_FALLBACK_URL': ''}),
                    mock.patch.object(review, '_sealed_replay_transport',
                                      side_effect=AssertionError('replay must not call inference')),
                ):
                    code = review.main(['--replay-from', str(child), '--runs-root', str(runs),
                                        '--state', str(root / 'replay-items.json')])
                self.assertEqual(0, code)
                replays = list(runs.glob(f'*-replay-{child.name}*'))
                self.assertEqual(1, len(replays))
                self.assertEqual((child / 'work-accounting-result.json').read_bytes(),
                                 (replays[0] / 'work-accounting-result.json').read_bytes())
                self.assertEqual(parent_before, {name: (parent / name).read_bytes()
                                                 for name in parent_before})
