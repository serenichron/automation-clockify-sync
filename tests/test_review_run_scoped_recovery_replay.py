"""Scoped quarantine recovery must remain reproducible without inference."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import semantic_analyzer, work_accounting_pipeline as pipeline
import test_review_run as fixtures
from test_review_run_chained_repair_replay import validate_repair

review = fixtures.review_run


class ScopedRecoveryReplayTests(unittest.TestCase):
    def test_recovered_quarantine_replays_with_no_network_and_unchanged_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / 'runs'
            original = fixtures.ReviewRunResultTests._write_real_offline_replay_source(
                runs, root, failed_review=True,
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
            original_analyze = semantic_analyzer.analyze_tiered

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
                    mock.patch.object(semantic_analyzer, 'analyze_tiered', side_effect=lambda events, **kw:
                                      original_analyze(events, transport=duplicate_transport,
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
                self.assertEqual('scoped_review_v2', recovered['failed_review_retry']['mode'])
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
