"""Provider quota/rate limits defer analyzer work instead of sealing it.

A usage-limit response is not evidence about the request: the identical request
succeeds once the provider window resets. Sealing it as a replayable
``transport_error`` turned a temporary quota hit into a permanent
``analyzer_failure`` exception on every later replay.
"""
from __future__ import annotations

import argparse
from contextlib import redirect_stderr
import datetime as dt
import email.utils
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock
import urllib.error

from scripts import clockify_review_cycle as cycle
from scripts import semantic_analyzer as semantic
from scripts import work_accounting_pipeline
from scripts.autopilot_process import ChildResult
import test_review_cycle_source_debt as cycle_fixtures
from test_review_run import review_run
from test_semantic_analyzer import event, provider_response


def _payload(body: dict) -> dict:
    return json.loads(body["messages"][-1]["content"])


def _kind(payload: dict) -> str:
    if payload.get("probe"):
        return "probe"
    if payload.get("connection_recovery"):
        return "recovery"
    return "extract"


def _records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


class QuotaDeferralTests(unittest.TestCase):
    def setUp(self) -> None:
        self.endpoint = semantic.AnalyzerEndpoint("primary", "http://primary", "qualified")
        self.waits: list[float] = []
        patcher = mock.patch.object(
            semantic, "_wait_for_rate_limit",
            side_effect=lambda _stop, seconds: self.waits.append(seconds),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        environment = mock.patch.dict(os.environ, {}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop(semantic.CHILD_DEADLINE_ENV, None)

    def analyze(self, transport, path: Path, events=None, **options):
        return semantic.analyze_tiered(
            events or [event("ev-a")],
            primary=self.endpoint,
            transport=transport,
            private_text_approved=True,
            cache=semantic.AnalyzerResponseCache(path),
            **options,
        )

    def test_quota_exhausted_mid_run_defers_then_recovers_after_reset(self):
        calls: list[str] = []
        extract_bodies: list[str] = []

        def quota_exhausted(_endpoint, body):
            payload = _payload(body)
            calls.append(_kind(payload))
            if payload.get("probe"):
                return {"probe": "ok"}
            extract_bodies.append(semantic.canonical_json(body))
            raise semantic.AnalyzerRetryableHTTPError("usage limit", status_code=429)

        def healthy(_endpoint, body):
            payload = _payload(body)
            calls.append("after_reset:" + _kind(payload))
            return {"probe": "ok"} if payload.get("probe") else provider_response(payload)

        with tempfile.TemporaryDirectory() as directory:
            baseline = self.analyze(healthy, Path(directory) / "baseline.jsonl")
            calls.clear()
            path = Path(directory) / "cache.jsonl"

            # Run 1: the probe succeeds, then the quota is exhausted mid-run.
            with self.assertRaises(semantic.AnalyzerQuotaDeferred) as raised:
                self.analyze(quota_exhausted, path)
            self.assertNotIsInstance(raised.exception, semantic.AnalyzerError)
            self.assertEqual(429, raised.exception.status_code)
            retries = len(semantic.RATE_LIMIT_BACKOFF_SECONDS)
            self.assertEqual(["probe"] + ["extract"] * (retries + 1), calls)
            # Same request, same model, no recovery-attempt variants.
            self.assertEqual(1, len(set(extract_bodies)))
            self.assertEqual(list(semantic.RATE_LIMIT_BACKOFF_SECONDS), self.waits)
            self.assertEqual([], _records(path))

            # Run 2: after the reset the identical request is sent once and
            # every activity is recovered exactly once.
            calls.clear()
            second = self.analyze(healthy, path)
        self.assertEqual(["after_reset:probe", "after_reset:extract"], calls)
        self.assertEqual([], second["exceptions"])
        self.assertEqual(baseline["activities"], second["activities"])
        self.assertEqual(1, len(second["activities"]))
        self.assertEqual("not_attempted", second["analysis_chunks"][0]["connection_recovery_status"])

    def test_retry_after_is_honored_within_one_run(self):
        responses = iter([
            semantic.AnalyzerRetryableHTTPError(
                "rate limited", status_code=429, retry_after_seconds=7.0
            ),
            None,
        ])

        def transport(_endpoint, body):
            payload = _payload(body)
            if payload.get("probe"):
                return {"probe": "ok"}
            error = next(responses)
            if error is not None:
                raise error
            return provider_response(payload)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.jsonl"
            result = self.analyze(transport, path)
            records = _records(path)
        self.assertEqual([7.0], self.waits)
        self.assertEqual([], result["exceptions"])
        self.assertEqual(1, len(result["activities"]))
        self.assertEqual(["accepted"], [record["status"] for record in records])

    def test_wait_that_outlasts_child_deadline_defers_without_waiting(self):
        os.environ[semantic.CHILD_DEADLINE_ENV] = str(
            time.time() + semantic.RATE_LIMIT_DEADLINE_MARGIN_SECONDS + 10
        )

        def transport(_endpoint, body):
            if _payload(body).get("probe"):
                return {"probe": "ok"}
            raise semantic.AnalyzerRetryableHTTPError(
                "rate limited", status_code=429, retry_after_seconds=30.0
            )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.jsonl"
            with self.assertRaises(semantic.AnalyzerQuotaDeferred) as raised:
                self.analyze(transport, path)
            self.assertEqual([], _records(path))
        self.assertEqual([], self.waits)
        self.assertEqual(30.0, raised.exception.retry_after_seconds)

    def test_retry_after_beyond_bounded_delay_defers_immediately(self):
        def transport(_endpoint, body):
            if _payload(body).get("probe"):
                return {"probe": "ok"}
            raise semantic.AnalyzerRetryableHTTPError(
                "usage limit", status_code=429,
                retry_after_seconds=semantic.MAX_RATE_LIMIT_DELAY_SECONDS + 1.0,
            )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.jsonl"
            with self.assertRaises(semantic.AnalyzerQuotaDeferred):
                self.analyze(transport, path)
            self.assertEqual([], _records(path))
        self.assertEqual([], self.waits)

    def test_quota_during_contract_repair_is_not_an_analyzer_failure(self):
        # The repair loop's catch-all must not turn a quota hit into a
        # rejected-chunk exception; only the genuine rejection stays sealed.
        def transport(_endpoint, body):
            payload = _payload(body)
            if payload.get("probe"):
                return {"probe": "ok"}
            if payload.get("repair_feedback") is None:
                return provider_response(payload, members=[])
            raise semantic.AnalyzerRetryableHTTPError("usage limit", status_code=429)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.jsonl"
            with self.assertRaises(semantic.AnalyzerQuotaDeferred):
                self.analyze(transport, path)
            records = _records(path)
        self.assertEqual(["rejected"], [record["status"] for record in records])
        self.assertTrue(records[0]["failure_code"].startswith("contract_rejected"))

    def test_quota_on_one_chunk_defers_whole_run_with_parallel_workers(self):
        def transport(_endpoint, body):
            payload = _payload(body)
            if payload.get("probe"):
                return {"probe": "ok"}
            # Evidence IDs are opaque to the provider; select the chunk by text.
            if "beta work" in json.dumps(payload):
                raise semantic.AnalyzerRetryableHTTPError("usage limit", status_code=429)
            return provider_response(payload)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.jsonl"
            with self.assertRaises(semantic.AnalyzerQuotaDeferred):
                self.analyze(
                    transport, path,
                    events=[
                        event("ev-a", content="alpha work"),
                        event("ev-b", day="2026-07-11", content="beta work"),
                    ],
                    max_events_per_chunk=1, max_workers=2,
                )
            failure_codes = {record.get("failure_code") for record in _records(path)}
        self.assertNotIn("transport_error", failure_codes)

    def test_probe_quota_defers_without_sealing(self):
        def transport(_endpoint, _body):
            raise semantic.AnalyzerRetryableHTTPError("usage limit", status_code=429)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.jsonl"
            with self.assertRaises(semantic.AnalyzerQuotaDeferred):
                self.analyze(transport, path)
            self.assertEqual([], _records(path))

    def test_connection_loss_keeps_sealed_transport_error_semantics(self):
        calls: list[str] = []

        def transport(_endpoint, body):
            payload = _payload(body)
            calls.append(_kind(payload))
            if payload.get("probe"):
                return {"probe": "ok"}
            raise semantic.AnalyzerRetryableHTTPError("unavailable", status_code=503)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.jsonl"
            result = self.analyze(transport, path)
            codes = [record["failure_code"] for record in _records(path)]
        self.assertEqual([], self.waits)
        self.assertEqual(
            ["transport_error"] * (semantic.MAX_CONNECTION_RECOVERY_ATTEMPTS + 1), codes
        )
        self.assertEqual(["analyzer_failure"], [row["kind"] for row in result["exceptions"]])
        self.assertEqual(
            "exhausted_exception", result["analysis_chunks"][0]["connection_recovery_status"]
        )

    def test_historical_sealed_transport_error_still_replays(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.jsonl"
            self.analyze(
                lambda _e, body: (
                    {"probe": "ok"} if _payload(body).get("probe")
                    else (_ for _ in ()).throw(semantic.AnalyzerTransportError("lost"))
                ),
                path,
            )
            sealed = path.read_bytes()
            replay = self.analyze(
                lambda _e, _body: self.fail("sealed failures must replay offline"), path
            )
            self.assertEqual(sealed, path.read_bytes())
        self.assertEqual(["analyzer_failure"], [row["kind"] for row in replay["exceptions"]])

    def test_invalid_child_deadline_fails_closed(self):
        os.environ[semantic.CHILD_DEADLINE_ENV] = "soon"
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(semantic.AnalyzerError):
                self.analyze(lambda _e, _b: {"probe": "ok"}, Path(directory) / "c.jsonl")


class UngatedSealSiteTests(unittest.TestCase):
    """Scoped retry and portfolio repair call the seal sites without a gate."""

    endpoint = semantic.AnalyzerEndpoint("primary", "http://fixture", "flash-fixture")
    spans = {"ev-1": {"start": "2026-07-10 10:00", "end": "2026-07-10 10:10"}}

    @staticmethod
    def limited(_endpoint, _body):
        raise semantic.AnalyzerRetryableHTTPError("usage limit", status_code=429)

    def test_extraction_seal_site_defers_rate_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.jsonl"
            with self.assertRaises(semantic.AnalyzerQuotaDeferred):
                semantic._call_validated(
                    self.endpoint, [event("ev-1")], tier="primary",
                    transport=self.limited, known_evidence_ids={"ev-1"},
                    evidence_time_spans=self.spans, private_text_approved=True,
                    cache=semantic.AnalyzerResponseCache(path),
                )
            self.assertEqual([], _records(path))

    def test_semantic_review_seal_site_defers_rate_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.jsonl"
            with self.assertRaises(semantic.AnalyzerQuotaDeferred):
                semantic._call_semantic_review_once(
                    self.endpoint, [event("ev-1")],
                    candidate={"activities": [], "exceptions": [], "omissions": []},
                    taxonomy=[{
                        "project_name": "Serenichron Level 2", "prefix": "SC",
                        "tag_names": ["Processes"],
                    }],
                    tier="primary", transport=self.limited, known_evidence_ids={"ev-1"},
                    evidence_time_spans=self.spans,
                    cache=semantic.AnalyzerResponseCache(path),
                    before_transport=None, cancelled=None,
                )
            self.assertEqual([], _records(path))

    def test_extraction_seal_site_still_seals_connection_loss(self):
        def lost(_endpoint, _body):
            raise semantic.AnalyzerRetryableHTTPError("unavailable", status_code=502)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.jsonl"
            with self.assertRaises(semantic.AnalyzerTransportError):
                semantic._call_validated(
                    self.endpoint, [event("ev-1")], tier="primary",
                    transport=lost, known_evidence_ids={"ev-1"},
                    evidence_time_spans=self.spans, private_text_approved=True,
                    cache=semantic.AnalyzerResponseCache(path),
                )
            self.assertEqual(["transport_error"], [r["failure_code"] for r in _records(path)])


class HTTPRetryAfterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.endpoint = semantic.AnalyzerEndpoint(
            "primary", "http://primary", semantic.DEFAULT_PRIMARY_MODEL,
            revision=semantic.DEFAULT_PRIMARY_REVISION,
        )
        self.body = {"model": semantic.DEFAULT_PRIMARY_MODEL, "messages": []}

    def raised(self, status: int, headers: dict | None):
        error = urllib.error.HTTPError(self.endpoint.url, status, "limited", headers, None)
        self.addCleanup(error.close)
        with mock.patch(
            "scripts.semantic_analyzer.urllib.request.urlopen", side_effect=error
        ):
            with self.assertRaises(semantic.AnalyzerRetryableHTTPError) as raised:
                semantic.http_transport(self.endpoint, self.body)
        return raised.exception

    def test_retry_after_seconds_header(self):
        error = self.raised(429, {"Retry-After": "12"})
        self.assertEqual(429, error.status_code)
        self.assertEqual(12.0, error.retry_after_seconds)
        self.assertTrue(semantic._is_rate_limited(error))

    def test_retry_after_http_date_header(self):
        later = email.utils.formatdate(time.time() + 90, usegmt=True)
        error = self.raised(503, {"Retry-After": later})
        self.assertTrue(80 <= error.retry_after_seconds <= 91)
        self.assertTrue(semantic._is_rate_limited(error))

    def test_plain_503_is_not_a_rate_limit(self):
        error = self.raised(503, None)
        self.assertIsNone(error.retry_after_seconds)
        self.assertFalse(semantic._is_rate_limited(error))


class DeferredExitTests(unittest.TestCase):
    def test_pipeline_exits_tempfail_on_quota_deferral(self):
        deferral = semantic.AnalyzerQuotaDeferred("usage limit", status_code=429)
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            work_accounting_pipeline, "run_accounting", side_effect=deferral
        ), mock.patch("sys.stderr"):
            code = work_accounting_pipeline.main([directory])
        self.assertEqual(semantic.QUOTA_DEFERRED_EXIT_CODE, code)
        self.assertEqual(75, semantic.QUOTA_DEFERRED_EXIT_CODE)

    def test_review_run_defers_without_publishing_a_blocked_result(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory) / "run"
            run_dir.mkdir()
            args = argparse.Namespace(
                routing=run_dir / "routing.json",
                corrections=run_dir / "review-corrections.jsonl",
                state=Path(directory) / "state" / "review-items.json",
                analyzer_cache=Path(directory) / "cache.jsonl",
                analysis_fixture=None,
                analyzer_target_body_bytes=None,
                analyzer_max_events_per_chunk=None,
                analyzer_workers=None,
                review_mode="shadow_all",
            )
            deferred = subprocess.CompletedProcess(
                [], semantic.QUOTA_DEFERRED_EXIT_CODE, "", "work accounting deferred"
            )
            with mock.patch.object(review_run, "_run", return_value=deferred) as run, \
                    redirect_stderr(io.StringIO()):
                code, result_path = review_run._process_run(args, run_dir, {})
            self.assertEqual(1, run.call_count)
            self.assertEqual(75, code)
            self.assertFalse(result_path.exists())
            self.assertFalse((run_dir / "quality_report.json").exists())

    def test_cycle_child_receives_its_termination_deadline(self):
        child = mock.Mock(duration_seconds=1.0)
        budget = [5000.0]
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            cycle, "run_child_bounded", return_value=child
        ) as run:
            before = time.time()
            cycle._run_budgeted_child(
                ["true"], root=Path(directory), budget=budget, cap=2700, grace=30
            )
            after = time.time()
        deadline = float(run.call_args.kwargs["environment"][semantic.CHILD_DEADLINE_ENV])
        self.assertTrue(before + 2700 - 0.001 <= deadline <= after + 2700 + 0.001)
        self.assertEqual(4999.0, budget[0])


class CycleDeferredSliceTests(unittest.TestCase):
    setUp = cycle_fixtures.ReviewCycleSourceDebtTests.setUp
    state = cycle_fixtures.ReviewCycleSourceDebtTests.state

    def config_for_slice(self):
        return {**self.config, "catchup_until": "2026-09-09", "max_slices": 1}

    def test_deferred_child_leaves_slice_incomplete_and_resumable(self):
        deferred = ChildResult(
            semantic.QUOTA_DEFERRED_EXIT_CODE, "", "child stderr suppressed", False, 0.1
        )
        with mock.patch.object(cycle, "run_child_bounded", return_value=deferred) as first:
            result = cycle.run_cycle(
                self.config_for_slice(), enable_sheet_write=True,
                today=dt.date(2026, 9, 12),
            )
        self.assertEqual(1, first.call_count)
        self.assertEqual("incomplete", result["status"])
        record = self.state()["slices"]["2026-09-07"]
        self.assertEqual("incomplete", record["status"])
        self.assertEqual(75, record["source_attempt_outcomes"][0]["returncode"])
        self.assertNotIn("source", record)
        with mock.patch.object(cycle, "run_child_bounded", return_value=deferred) as second:
            cycle.run_cycle(
                self.config_for_slice(), enable_sheet_write=True,
                today=dt.date(2026, 9, 12),
            )
        self.assertEqual(1, second.call_count)


if __name__ == "__main__":
    unittest.main()
