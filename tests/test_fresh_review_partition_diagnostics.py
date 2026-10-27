"""Fresh pipeline diagnostics must not change historical repair/replay identity."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import semantic_analyzer as semantic
from scripts import work_accounting_pipeline as pipeline
from test_semantic_analyzer import event, provider_members, provider_response


class FreshReviewPartitionDiagnosticsTests(unittest.TestCase):
    taxonomy = [{"project_name": "Serenichron Level 2", "prefix": "SC", "tag_names": ["Processes"]}]
    endpoint = semantic.AnalyzerEndpoint(
        "clockify_analyzer_primary", "http://fixture/v1/chat/completions",
        semantic.CURRENT_LIVE_FLASH_ROUTE[0], revision=semantic.CURRENT_LIVE_FLASH_ROUTE[1],
    )

    def analyze(self, events, cache):
        return pipeline.analyze_ledger(events, analyzer_cache_path=cache,
                                       review_taxonomy=self.taxonomy, analyzer_workers=1)

    def endpoints(self, prefix, **_kwargs):
        return self.endpoint if prefix == "CLOCKIFY_ANALYZER_PRIMARY" else None

    def test_fresh_failed_partition_capture_preserves_generic_repairs_and_sealed_replay(self):
        # Missing capture loses original ordinal coverage; enabling feedback on
        # fresh capture changes repair identities and breaks sidecar-free replay.
        events = [event(f"ev-private-{number}") for number in range(1, 5)]
        for defect in ("duplicate", "missing", "duplicate_and_missing"):
            with self.subTest(defect=defect), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                cache = root / "cache.jsonl"
                sidecar = root / "cache.jsonl.review-diagnostics.jsonl"
                bodies = []

                def urlopen(request, **_kwargs):
                    body = json.loads(request.data)
                    payload = json.loads(body["messages"][1]["content"])
                    if payload.get("probe"):
                        return io.BytesIO(b'{"probe":"ok"}')
                    bodies.append(body)
                    if payload["mode"] != "review":
                        response = provider_response(payload)
                    else:
                        members = provider_members(payload)
                        selections = {
                            "duplicate": ([0, 1], [1], [2, 3]),
                            # Two of four unassigned stays above the local
                            # partition-normalization cap, so generic repair runs.
                            "missing": ([0, 1],),
                            "duplicate_and_missing": ([0, 1], [1], [2]),
                        }[defect]
                        rows = [provider_response(payload, [members[i] for i in selection])["activities"][0]
                                for selection in selections]
                        for row in rows:
                            row["object"] = "PRIVATE PROVIDER OBJECT PROSE"
                        response = {"activities": rows, "exceptions": [], "omissions": [],
                                    "private_note": "PRIVATE-RAW-PROSE /private/path secret=hidden"}
                    return io.BytesIO(json.dumps(response).encode())

                with mock.patch.object(semantic.AnalyzerEndpoint, "from_env", side_effect=self.endpoints), \
                     mock.patch.dict("os.environ", {"CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED": "approved"}), \
                     mock.patch("urllib.request.urlopen", side_effect=urlopen):
                    result = self.analyze(events, cache)
                    self.assertEqual("analyzer_review_failure", result["exceptions"][0]["kind"])
                    self.assertTrue(sidecar.exists(), "fresh failed reviews must retain sanitized ordinal diagnostics")
                    records = [json.loads(line) for line in sidecar.read_text().splitlines()]
                    self.assertEqual(3, len(records))
                    self.assertEqual(0o600, sidecar.stat().st_mode & 0o777)
                    record = records[0]
                    self.assertEqual("contract_rejected_duplicate_evidence" if defect == "duplicate"
                                     else "contract_rejected_omitted_evidence", record["failure_code"])
                    self.assertEqual([{"bundle_ref": "b-0001", "allowed_member_range": [1, 4]}], record["coverage_contract"])
                    expected = {"duplicate": ([[1, 2]], [[2, 2]], [[3, 4]]),
                                "missing": ([[1, 2]],),
                                "duplicate_and_missing": ([[1, 2]], [[2, 2]], [[3, 3]])}[defect]
                    self.assertEqual([{"evidence_partitions": [{"bundle_ref": "b-0001", "member_ranges": ranges}]}
                                      for ranges in expected], record["activities"])
                    for forbidden in ("PRIVATE", "secret=", "/private/", "ev-private-", "private_note", '"object"'):
                        self.assertNotIn(forbidden, sidecar.read_text())
                    review_payloads = [json.loads(body["messages"][1]["content"]) for body in bodies
                                       if json.loads(body["messages"][1]["content"])["mode"] == "review"]
                    self.assertEqual(3, len(review_payloads))
                    for payload in review_payloads[1:]:
                        self.assertNotIn("citation_conflicts", payload["repair_feedback"])
                        self.assertNotIn("coverage_counts", payload["repair_feedback"])
                    fresh_bodies = list(bodies)
                    baseline_cache = root / "historical.jsonl"
                    semantic.analyze_tiered(events, primary=self.endpoint,
                        cache=semantic.AnalyzerResponseCache(baseline_cache),
                        review_taxonomy=self.taxonomy, private_text_approved=True, max_workers=1)
                    self.assertEqual(fresh_bodies, bodies[len(fresh_bodies):],
                                     "diagnostic capture must not change historical request bodies")
                    self.assertEqual(baseline_cache.read_bytes(), cache.read_bytes(),
                                     "diagnostic capture must not change cache records or identities")
                    before = {path.name: path.read_bytes() for path in root.iterdir()}
                    with mock.patch("urllib.request.urlopen", side_effect=AssertionError("replay must not infer")):
                        replay = self.analyze(events, cache)
                    self.assertEqual(result["exceptions"], replay["exceptions"])
                    self.assertEqual(before, {path.name: path.read_bytes() for path in root.iterdir()})
                    sealed_dir = root / "sealed"
                    sealed_dir.mkdir()
                    snapshot = pipeline._seal_analyzer_cache_snapshot(sealed_dir, cache, result)
                    sealed = sealed_dir / snapshot["path"]
                    self.assertEqual([sealed], list(sealed_dir.iterdir()))
                    sealed_bytes = sealed.read_bytes()
                    with mock.patch("urllib.request.urlopen", side_effect=AssertionError("sealed replay must not infer")):
                        replay = semantic.analyze_tiered(events, primary=self.endpoint,
                            cache=semantic.AnalyzerResponseCache(sealed), review_taxonomy=self.taxonomy,
                            private_text_approved=True, max_workers=1)
                    self.assertEqual(result["exceptions"], replay["exceptions"])
                    self.assertEqual(sealed_bytes, sealed.read_bytes())
                    self.assertEqual([sealed], list(sealed_dir.iterdir()))

    def test_success_and_supplied_fixture_never_create_diagnostics(self):
        # A success is not a rejected partition; supplied fixture validation is
        # strictly offline even when a cache path is supplied.
        events = [event("ev-private-1")]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "cache.jsonl"

            def urlopen(request, **_kwargs):
                payload = json.loads(json.loads(request.data)["messages"][1]["content"])
                response = {"probe": "ok"} if payload.get("probe") else provider_response(payload)
                return io.BytesIO(json.dumps(response).encode())

            with mock.patch.object(semantic.AnalyzerEndpoint, "from_env", side_effect=self.endpoints), \
                 mock.patch.dict("os.environ", {"CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED": "approved"}), \
                 mock.patch("urllib.request.urlopen", side_effect=urlopen):
                result = self.analyze(events, cache)
            self.assertEqual(1, len(result["activities"]))
            self.assertFalse((root / "cache.jsonl.review-diagnostics.jsonl").exists())
            fixture = root / "semantic.json"
            fixture.write_text(json.dumps(result))
            before = {path.name: path.read_bytes() for path in root.iterdir()}
            with mock.patch.object(semantic.AnalyzerEndpoint, "from_env", side_effect=AssertionError("fixture must not select endpoints")), \
                 mock.patch("urllib.request.urlopen", side_effect=AssertionError("fixture must not infer")):
                replay = pipeline.analyze_ledger(events, analysis_fixture=fixture,
                    analyzer_cache_path=cache, review_taxonomy=self.taxonomy)
            self.assertEqual(result["activities"], replay["activities"])
            self.assertEqual(before, {path.name: path.read_bytes() for path in root.iterdir()})
