"""A fresh citation-repair contract survives completed repair ancestry and replay."""
from __future__ import annotations

import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_review_run as fixtures
from test_review_run_chained_repair_replay import validate_repair
from scripts import clockify_review_cycle, semantic_analyzer, work_accounting_pipeline


run = fixtures.review_run


class CitationV4ReplayTests(unittest.TestCase):
    def test_fresh_v4_duplicate_repair_verifies_ancestry_and_replays_offline(self):
        """Catches either consumer discarding the sealed v4 retry mode."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs = root / "runs"
            source = fixtures.ReviewRunResultTests._write_real_offline_replay_source(
                runs, root, failed_review=True,
            )
            endpoint = semantic_analyzer.AnalyzerEndpoint(
                "clockify_analyzer_primary", "https://offline.invalid/v1/chat/completions",
                semantic_analyzer.DEFAULT_PRIMARY_MODEL,
                revision=semantic_analyzer.DEFAULT_PRIMARY_REVISION,
            )
            original_analyze = semantic_analyzer.analyze_tiered
            original_scoped = work_accounting_pipeline.run_scoped_failed_review_retry
            original_provider = fixtures.analyzer_provider_response
            scoped_calls = []

            def duplicate_transport(_endpoint, body):
                payload = json.loads(body["messages"][-1]["content"])
                response = original_provider(payload)
                response["activities"].append(copy.deepcopy(response["activities"][0]))
                if payload.get("scoped_failed_review"):
                    scoped_calls.append(payload)
                return response

            def valid_transport(_endpoint, body):
                payload = json.loads(body["messages"][-1]["content"])
                scoped_calls.append(payload)
                return original_provider(payload)

            endpoint_patch = lambda name, **_kw: (
                endpoint if name == "CLOCKIFY_ANALYZER_PRIMARY" else None
            )

            def retry(parent: Path, target: str, *, scoped: bool, transport):
                child = run._prepare_repair_run(parent)
                cache = child / "analyzer-cache-retry.jsonl"
                cache.write_bytes((child / "analyzer-cache-used.jsonl").read_bytes())
                if scoped:
                    replacement = mock.patch.object(
                        work_accounting_pipeline, "run_scoped_failed_review_retry",
                        side_effect=lambda *args, **kwargs: original_scoped(
                            *args, **{**kwargs, "transport": transport,
                                     "private_text_approved": True},
                        ),
                    )
                else:
                    replacement = mock.patch.object(
                        semantic_analyzer, "analyze_tiered",
                        side_effect=lambda events, **kwargs: original_analyze(
                            events, transport=transport,
                            private_text_approved=True, **kwargs,
                        ),
                    )
                with (
                    mock.patch.object(
                        semantic_analyzer.AnalyzerEndpoint, "from_env",
                        side_effect=endpoint_patch,
                    ),
                    replacement,
                ):
                    work_accounting_pipeline.run_accounting(
                        child, root=fixtures.ROOT,
                        routing_path=child / "routing.json",
                        corrections_path=child / "review-corrections.jsonl",
                        analyzer_cache_path=cache,
                        failed_review_retry_source=run._repair_analysis_fixture(child),
                        failed_review_retry_digest=target,
                        analyzer_workers=1,
                    )
                validate_repair(child, runs, root / f"{child.name}-items.json")
                bundle = run._finalize_repair_completion(child)
                return child, bundle

            def exception(parent: Path, kind: str):
                analysis = json.loads((parent / "semantic-analysis.json").read_text())
                return next(row for row in analysis["exceptions"] if row["kind"] == kind)

            def digest(row):
                return semantic_analyzer.stable_digest(
                    "frt-", row["evidence_ids"], length=64,
                )

            with mock.patch.object(run, "RUNS", runs):
                structural = exception(source, "analyzer_review_failure")
                self.assertIn("contract_rejected_duplicate_evidence", structural["reason"])
                first, _ = retry(
                    source, digest(structural), scoped=False,
                    transport=duplicate_transport,
                )
                quarantine = exception(first, "analyzer_review_partial_quarantine")
                second, _ = retry(
                    first, digest(quarantine), scoped=True,
                    transport=duplicate_transport,
                )
                residual = exception(second, "analyzer_review_partial_quarantine")
                self.assertFalse(any(
                    row["kind"] == "analyzer_review_failure"
                    for row in json.loads((second / "semantic-analysis.json").read_text())["exceptions"]
                ))
                self.assertEqual(
                    "scoped_review_v4_citation_quarantine",
                    json.loads((second / "semantic-analysis.json").read_text())[
                        "failed_review_retry"
                    ]["mode"],
                )
                third, bundle = retry(
                    second, digest(residual), scoped=True,
                    transport=valid_transport,
                )
                recovered = json.loads((third / "semantic-analysis.json").read_text())
                self.assertEqual(
                    "scoped_review_v4_citation_quarantine",
                    recovered["failed_review_retry"]["mode"],
                )
                self.assertEqual(
                    "scoped_review_v4_citation_quarantine",
                    scoped_calls[-1]["scoped_failed_review"]["mode"],
                )
                self.assertTrue(recovered["activities"])
                self.assertFalse(any(
                    row["kind"] == "analyzer_review_failure"
                    for row in recovered["exceptions"]
                ))
                ancestor, _ = clockify_review_cycle._collector_ancestor_from_repair(
                    {"runs_dir": str(runs)}, third, bundle,
                )
                self.assertEqual(source, ancestor)
                with (
                    mock.patch.dict(os.environ, {
                        "CLOCKIFY_ANALYZER_PRIMARY_URL": "",
                        "CLOCKIFY_ANALYZER_FALLBACK_URL": "",
                    }),
                    mock.patch.object(
                        run, "_sealed_replay_transport",
                        side_effect=AssertionError("sealed replay called inference"),
                    ),
                    contextlib.redirect_stdout(io.StringIO()),
                ):
                    code = run.main([
                        "--replay-from", str(third), "--runs-root", str(runs),
                        "--state", str(root / "replay-items.json"),
                    ])
            self.assertEqual(0, code)
            replays = list(runs.glob(f"*-replay-{third.name}*"))
            self.assertEqual(1, len(replays))
            self.assertEqual(
                "pass", json.loads((replays[0] / "replay-integrity.json").read_text())["status"],
            )
            self.assertEqual(
                (third / "work-accounting-result.json").read_bytes(),
                (replays[0] / "work-accounting-result.json").read_bytes(),
            )


if __name__ == "__main__":
    unittest.main()
