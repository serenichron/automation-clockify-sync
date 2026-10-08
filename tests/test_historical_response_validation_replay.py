"""A sealed historical response contract is not an ordinary cache-hit waiver."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import clockify_review_run as review
from scripts import collector_receipts
from scripts import semantic_analyzer as semantic
from scripts import work_accounting_pipeline as pipeline
import test_review_run as fixtures
from test_semantic_analyzer import event, provider_response


CURRENT_CONTRACT = "clockify-semantic-response-validation/v2:client-description-hygiene"


def write(path, value):
    path.write_text(json.dumps(value, sort_keys=True) + "\n")


def legacy_source(root, outcome):
    """Literal old accepted review fixture; never generates old data via exemption."""
    source = fixtures.ReviewRunResultTests._write_real_offline_replay_source(root / "runs", root)
    analysis = json.loads((source / "semantic-analysis.json").read_bytes())
    cache_path = source / "analyzer-cache-used.jsonl"
    records = [json.loads(line) for line in cache_path.read_text().splitlines()]
    for record in records:
        # The review and extraction share the synthetic activity. Only the
        # accepted reviewed response changes; request identities stay sealed.
        if record["status"] == "accepted" and record["response"].get("activities"):
            record["response"]["activities"][0]["outcome"] = outcome
            record["decision_digest"] = hashlib.sha256(semantic.canonical_json({
                "status": "accepted", "response": record["response"],
            }).encode()).hexdigest()
    # Extraction candidate is part of the review request. Keep its response
    # unchanged, so the historical review body has its original identity.
    original = [json.loads(line) for line in cache_path.read_text().splitlines()]
    ledger, all_events = pipeline.load_ledger(source / "evidence/evidence-ledger.json")
    members = pipeline.meeting_reconciliation.manifest_member_identities(ledger.manifest.document())
    events, _noise = pipeline._analysis_events(all_events, members)
    hinted = pipeline._with_semantic_route_hints(events, json.loads((source / "routing.json").read_bytes()))
    endpoint = next(value for value in semantic.AnalyzerResponseCache(cache_path).sealed_endpoints()
                    if value.name == "clockify_analyzer_primary")
    extraction = semantic._body_for(hinted, model=endpoint.model, mode="extract", corrections=[], private_text_approved=True)
    extraction_key = semantic.AnalyzerResponseCache._request_identity(endpoint, extraction)["cache_key"]
    for index, record in enumerate(records):
        if record["cache_key"] == extraction_key:
            records[index] = original[index]
    cache_path.write_text("".join(semantic.canonical_json(row) + "\n" for row in records))
    for activity in analysis["activities"]:
        activity["outcome"] = outcome
    for chunk in analysis["analysis_chunks"]:
        chunk.pop("response_validation_contract", None)
    analysis["analyzer_cache"]["records"] = sorted([
        {"cache_key": r["cache_key"], "decision_digest": r["decision_digest"]}
        for r in records
    ], key=lambda r: r["cache_key"])
    analysis["analyzer_cache"]["snapshot"]["sha256"] = hashlib.sha256(cache_path.read_bytes()).hexdigest()
    write(source / "semantic-analysis.json", analysis)
    # Read the old interval before replacing the now-stale artifact digests.
    document = json.loads((source / "completion-bundle.json").read_bytes())
    from types import SimpleNamespace
    import datetime as dt
    slice_ = SimpleNamespace(slice_id=document["slice_id"],
        since=dt.datetime.fromisoformat(document["since_utc"].replace("Z", "+00:00")),
        until=dt.datetime.fromisoformat(document["until_utc"].replace("Z", "+00:00")))
    collector_receipts.write_completion_bundle(source / "completion-bundle.json",
        collector_receipts.build_completion_bundle(source, slice_=slice_))
    return source, analysis


class HistoricalResponseReplayTests(unittest.TestCase):
    def preflight(self, source, analysis, cache=None):
        with mock.patch.object(review, "RUNS", source.parent), mock.patch.object(
            review, "_sealed_replay_transport", side_effect=AssertionError("No transport, including a probe, is needed"),
        ):
            return review._preflight_replay_analyzer_cache(source, cache or source / "analyzer-cache-used.jsonl", analysis)

    def test_authenticated_historical_telemetry_and_hash_preserve_exact_semantics(self):
        # Removing source-bound historical validation must lose old activities.
        for outcome in ("for reliable review with 47 tests passed", "for release commit 799a44e"):
            with self.subTest(outcome=outcome), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, analysis = legacy_source(root, outcome)
                before = fixtures.run_tree_snapshot(source)
                try:
                    records = self.preflight(source, analysis)
                except (ValueError, semantic.AnalyzerError, AssertionError) as exc:
                    self.fail(f"sealed legacy decisions must replay without new hygiene repairs: {exc}")
                self.assertEqual(analysis["analyzer_cache"]["records"], records)
                self.assertEqual(before, fixtures.run_tree_snapshot(source))
                self.assertEqual(outcome, json.loads((source / "semantic-analysis.json").read_bytes())["activities"][0]["outcome"])

    def test_fresh_chunk_contract_survives_native_fixture_and_replay(self):
        # Missing provenance would let a fresh source masquerade as legacy.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = fixtures.ReviewRunResultTests._write_real_offline_replay_source(root / "runs", root)
            analysis = json.loads((source / "semantic-analysis.json").read_bytes())
            self.assertTrue(analysis["analysis_chunks"])
            self.assertEqual([CURRENT_CONTRACT], list({row.get("response_validation_contract") for row in analysis["analysis_chunks"]}))
            with mock.patch.object(review, "RUNS", source.parent):
                child = review._prepare_replay_run(source)
                copied = json.loads(review._replay_analysis_fixture(source, child).read_bytes())
            self.assertEqual(analysis["analysis_chunks"], copied["analysis_chunks"])

    def test_forged_contract_does_not_select_historical_validation(self):
        for mutate in ("unknown", "mixed", "unsealed-semantic", "unbound-argument"):
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as temporary:
                source, analysis = legacy_source(Path(temporary), "for reliable review with 47 tests passed")
                if mutate == "unknown":
                    analysis["analysis_chunks"][0]["response_validation_contract"] = "pretend-legacy-contract"
                elif mutate == "mixed":
                    analysis["analysis_chunks"].append(copy.deepcopy(analysis["analysis_chunks"][0]))
                    analysis["analysis_chunks"][-1]["response_validation_contract"] = CURRENT_CONTRACT
                elif mutate == "unsealed-semantic":
                    analysis["activities"][0]["outcome"] = "for a forged result"
                    write(source / "semantic-analysis.json", analysis)
                else:
                    analysis["activities"][0]["outcome"] = "for a forged result"
                with self.assertRaises((ValueError, semantic.AnalyzerError)):
                    self.preflight(source, analysis)

    def test_unsealed_source_and_changed_cache_never_admit_legacy_context(self):
        for mutate in ("missing-completion", "missing-cache-record", "corrupt-cache-record"):
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, analysis = legacy_source(root, "for release commit 799a44e")
                cache = root / "private-cache.jsonl"
                content = (source / "analyzer-cache-used.jsonl").read_bytes()
                cache.write_bytes(content)
                if mutate == "missing-completion":
                    (source / "completion-bundle.json").unlink()
                elif mutate == "missing-cache-record":
                    cache.write_bytes(content.splitlines(keepends=True)[0])
                else:
                    row = json.loads(content.splitlines()[0])
                    row["decision_digest"] = "0" * 64
                    cache.write_text(semantic.canonical_json(row) + "\n")
                with self.assertRaises((ValueError, semantic.AnalyzerError)):
                    self.preflight(source, analysis, cache)

    def test_ordinary_bad_cache_hit_and_fresh_provider_response_keep_hygiene(self):
        # A cache-hit-only waiver would wrongly admit the same historical prose.
        events = [event("ev-1")]
        candidate = {"activities": [], "exceptions": [], "omissions": []}
        taxonomy = [{"project_name": "Serenichron Level 2", "prefix": "SC", "tag_names": ["Processes"]}]
        endpoint = semantic.AnalyzerEndpoint("primary", "http://fixture", "flash-fixture")
        body = semantic._review_body(events, candidate=candidate, taxonomy=taxonomy, model=endpoint.model)
        response = provider_response(json.loads(body["messages"][-1]["content"]))
        response["activities"][0]["outcome"] = "for reliable review with 47 tests passed"
        for cached in (False, True):
            with self.subTest(cached=cached), tempfile.TemporaryDirectory() as temporary:
                cache = semantic.AnalyzerResponseCache(Path(temporary) / "cache.jsonl")
                if cached:
                    cache.store_accepted(endpoint, body, response)
                with self.assertRaisesRegex(semantic.AnalyzerContractError, "client description hygiene"):
                    semantic._call_semantic_review_once(endpoint, events, candidate=candidate, taxonomy=taxonomy,
                        tier="primary", transport=lambda *_: response, known_evidence_ids={"ev-1"},
                        evidence_time_spans={"ev-1": {"start": "2026-07-10 10:00", "end": "2026-07-10 10:10"}},
                        cache=cache, before_transport=None, cancelled=None)

    def test_bound_context_rejects_all_cache_stores_and_new_requests(self):
        # A source-bound context must never become a writable/inferencing cache.
        with tempfile.TemporaryDirectory() as temporary:
            source, analysis = legacy_source(Path(temporary), "for release commit 799a44e")
            cache = semantic.AnalyzerResponseCache(source / "analyzer-cache-used.jsonl", record_review_diagnostics=True)
            before = cache.path.read_bytes()
            review._bind_native_replay_response_contract(source, source, cache, analysis,
                "clockify-semantic-response-validation/v1")
            record = json.loads(before.splitlines()[0])
            with self.assertRaisesRegex(semantic.AnalyzerError, "read-only"):
                cache._store_record(record)
            endpoint = next(value for value in cache.sealed_endpoints() if value.name == "clockify_analyzer_primary")
            body = {"model": endpoint.model, "messages": [{"role": "user", "content": "{}"}]}
            with self.assertRaisesRegex(semantic.AnalyzerError, "reconstructed request"):
                cache.lookup(endpoint, body)
            with self.assertRaisesRegex(semantic.AnalyzerError, "read-only"):
                cache.record_rejected_review(endpoint, body, failure_code="contract_rejected",
                    review_scope="extraction", response={})
            self.assertEqual(before, cache.path.read_bytes())

    def test_current_contract_never_admits_bad_historical_response(self):
        # A sealed current contract must use current hygiene, even with old prose.
        with tempfile.TemporaryDirectory() as temporary:
            source, analysis = legacy_source(Path(temporary), "for release commit 799a44e")
            for chunk in analysis["analysis_chunks"]:
                chunk["response_validation_contract"] = CURRENT_CONTRACT
            write(source / "semantic-analysis.json", analysis)
            document = json.loads((source / "completion-bundle.json").read_bytes())
            from types import SimpleNamespace
            import datetime as dt
            slice_ = SimpleNamespace(slice_id=document["slice_id"],
                since=dt.datetime.fromisoformat(document["since_utc"].replace("Z", "+00:00")),
                until=dt.datetime.fromisoformat(document["until_utc"].replace("Z", "+00:00")))
            collector_receipts.write_completion_bundle(source / "completion-bundle.json",
                collector_receipts.build_completion_bundle(source, slice_=slice_))
            with self.assertRaisesRegex((ValueError, semantic.AnalyzerError), "activities|reconstructed request"):
                self.preflight(source, analysis)


if __name__ == "__main__":
    unittest.main()
