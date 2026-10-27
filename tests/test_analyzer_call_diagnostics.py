import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import semantic_analyzer as semantic
from scripts import work_accounting_pipeline as pipeline
from test_semantic_analyzer import event, provider_response


ENDPOINT = semantic.AnalyzerEndpoint(
    "clockify_analyzer_primary", "http://fixture/v1/chat/completions",
    semantic.CURRENT_LIVE_FLASH_ROUTE[0], revision=semantic.CURRENT_LIVE_FLASH_ROUTE[1],
)
USAGE = {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500,
         "completion_tokens_details": {"reasoning_tokens": 120}}


def body_for(content="PRIVATE EVIDENCE TEXT"):
    return semantic._body_for([event("ev-1", content=content)], model=ENDPOINT.model,
                              mode="extract", private_text_approved=True)


class CallDiagnosticsTests(unittest.TestCase):
    # Catches per-call cost and latency being unknowable without new inference.

    def records(self, path):
        return [json.loads(line) for line in path.read_text().splitlines()]

    def test_successful_call_appends_numeric_usage_and_latency(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cache.jsonl.call-diagnostics.jsonl"
            body = body_for()
            response = {"choices": [], "usage": USAGE}
            with semantic.call_diagnostics(path), \
                 mock.patch("urllib.request.urlopen", return_value=io.BytesIO(json.dumps(response).encode())):
                self.assertEqual(response, semantic.http_transport(ENDPOINT, body))
            [record] = self.records(path)
            self.assertEqual(semantic.CALL_DIAGNOSTICS_SCHEMA_VERSION, record["schema_version"])
            self.assertEqual(("ok", "extract", ENDPOINT.model), (record["outcome"], record["mode"], record["model"]))
            self.assertEqual(hashlib.sha256(semantic.canonical_json(body).encode()).hexdigest(), record["body_digest"])
            self.assertEqual(len(semantic.canonical_json(body).encode()), record["body_bytes"])
            self.assertEqual({"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500,
                              "reasoning_tokens": 120}, record["usage"])
            self.assertIsInstance(record["latency_ms"], int)
            self.assertEqual(0o600, path.stat().st_mode & 0o777)
            self.assertNotIn("PRIVATE", path.read_text())

    def test_timeout_is_recorded_and_still_raised(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "calls.jsonl"
            with semantic.call_diagnostics(path), \
                 mock.patch("urllib.request.urlopen", side_effect=TimeoutError()):
                with self.assertRaises(semantic.AnalyzerTimeoutError):
                    semantic.http_transport(ENDPOINT, body_for())
            [record] = self.records(path)
            self.assertEqual("timeout", record["outcome"])
            self.assertEqual({}, record["usage"])

    def test_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch("urllib.request.urlopen", return_value=io.BytesIO(b'{"usage": {}}')):
            semantic.http_transport(ENDPOINT, body_for())
            self.assertEqual([], list(Path(temporary).iterdir()))

    def test_ledger_writes_sibling_diagnostics_and_leaves_cache_unchanged(self):
        events = [event("ev-1")]
        taxonomy = [{"project_name": "Serenichron Level 2", "prefix": "SC", "tag_names": ["Processes"]}]

        def urlopen(request, **_kwargs):
            payload = json.loads(json.loads(request.data)["messages"][1]["content"])
            response = {"probe": "ok"} if payload.get("probe") else {**provider_response(payload), "usage": USAGE}
            return io.BytesIO(json.dumps(response).encode())

        caches = {}
        real = semantic.call_diagnostics
        for name in ("plain", "diagnosed"):
            with tempfile.TemporaryDirectory() as temporary, \
                 mock.patch.object(semantic.AnalyzerEndpoint, "from_env",
                                   side_effect=lambda prefix, **_: ENDPOINT if prefix == "CLOCKIFY_ANALYZER_PRIMARY" else None), \
                 mock.patch.dict("os.environ", {"CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED": "approved"}), \
                 mock.patch("urllib.request.urlopen", side_effect=urlopen):
                cache = Path(temporary) / "cache.jsonl"
                if name == "plain":
                    with mock.patch.object(semantic, "call_diagnostics", side_effect=lambda _path: real(None)):
                        pipeline.analyze_ledger(events, analyzer_cache_path=cache, review_taxonomy=taxonomy, analyzer_workers=1)
                    self.assertFalse(cache.with_name("cache.jsonl.call-diagnostics.jsonl").exists())
                else:
                    pipeline.analyze_ledger(events, analyzer_cache_path=cache, review_taxonomy=taxonomy, analyzer_workers=1)
                    records = self.records(cache.with_name("cache.jsonl.call-diagnostics.jsonl"))
                    self.assertEqual({"extract", "review"}, {record["mode"] for record in records if record["mode"] != "probe"})
                    self.assertTrue(all(record["usage"].get("total_tokens") == 1500
                                        for record in records if record["mode"] != "probe"))
                caches[name] = cache.read_bytes()
        self.assertEqual(caches["plain"], caches["diagnosed"])
        self.assertIsNone(semantic._CALL_DIAGNOSTICS_PATH)


if __name__ == "__main__":
    unittest.main()
