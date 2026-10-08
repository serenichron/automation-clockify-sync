"""Rejected historical rendering must derive identical native replay diagnostics."""
from __future__ import annotations

import copy
import datetime as dt
import hashlib
import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from scripts import collector_receipts
from scripts import clockify_scoped_semantic_recovery as scoped
from scripts import semantic_analyzer as semantic
from scripts import work_accounting_pipeline as pipeline
import test_review_run as fixtures


review = fixtures.review_run


def write(path: Path, document: object) -> None:
    path.write_text(json.dumps(document, sort_keys=True) + "\n")


def historical_source(root: Path, *, rejected: bool) -> Path:
    """Build literal old output, then seal it; never waive current hygiene."""
    source = fixtures.ReviewRunResultTests._write_real_offline_replay_source(
        root / "runs", root, failed_review=True,
    )
    if not rejected:
        return source
    analysis = json.loads((source / "semantic-analysis.json").read_bytes())
    raw = analysis["activities"][0]
    old_provenance = {key: value for key, value in raw.items() if key in {
        "analyzer_model", "analyzer_tier", "analyzer_revision", "extractor_model",
        "semantic_reviewer_model", "semantic_reviewer_revision", "review_prompt_version",
    }}
    raw["outcome"] = "for reliable review with 47 tests passed"
    # Canonical semantic identity is part of old output, not the bug under test.
    normalized = semantic.validate_result(
        {"activities": [raw], "exceptions": [], "omissions": []},
        known_evidence_ids=set(raw["evidence_ids"]), semantic_validation=False,
        provider_model=raw["analyzer_model"], analyzer_tier=raw["analyzer_tier"],
    )["activities"][0]
    normalized.update(old_provenance)
    normalized["rendered_description"] = (
        "SC — Rebuilt Clockify review process for reliable review with 47 tests passed"
    )
    analysis["activities"][0] = normalized
    cache_path = source / "analyzer-cache-used.jsonl"
    cache = semantic.AnalyzerResponseCache(cache_path)
    endpoint = next(e for e in cache.sealed_endpoints()
                    if e.name == "clockify_analyzer_primary")
    ledger, events = pipeline.load_ledger(source / "evidence/evidence-ledger.json")
    members = pipeline.meeting_reconciliation.manifest_member_identities(ledger.manifest.document())
    filtered, _ = pipeline._analysis_events(events, members)
    hinted = pipeline._with_semantic_route_hints(
        filtered, json.loads((source / "routing.json").read_bytes()),
    )
    extraction_key = semantic.AnalyzerResponseCache._request_identity(
        endpoint, semantic._body_for(hinted, model=endpoint.model, mode="extract",
                                     corrections=[], private_text_approved=True),
    )["cache_key"]
    records = [json.loads(line) for line in cache_path.read_text().splitlines()]
    for record in records:
        if (record["cache_key"] != extraction_key and record["status"] == "accepted"
                and record["response"].get("activities")):
            record["response"]["activities"][0]["outcome"] = raw["outcome"]
            record["decision_digest"] = hashlib.sha256(semantic.canonical_json({
                "status": "accepted", "response": record["response"],
            }).encode()).hexdigest()
    cache_path.write_text("".join(semantic.canonical_json(row) + "\n" for row in records))
    for chunk in analysis["analysis_chunks"]:
        chunk.pop("response_validation_contract", None)
    analysis["analyzer_cache"]["records"] = sorted([
        {"cache_key": row["cache_key"], "decision_digest": row["decision_digest"]}
        for row in records
    ], key=lambda row: row["cache_key"])
    analysis["analyzer_cache"]["snapshot"]["sha256"] = hashlib.sha256(cache_path.read_bytes()).hexdigest()
    write(source / "semantic-analysis.json", analysis)
    bundle = json.loads((source / "completion-bundle.json").read_bytes())
    slice_ = SimpleNamespace(
        slice_id=bundle["slice_id"],
        since=dt.datetime.fromisoformat(bundle["since_utc"].replace("Z", "+00:00")),
        until=dt.datetime.fromisoformat(bundle["until_utc"].replace("Z", "+00:00")),
    )
    collector_receipts.write_completion_bundle(
        source / "completion-bundle.json",
        collector_receipts.build_completion_bundle(source, slice_=slice_),
    )
    manifest = json.loads((source / "period-manifest.json").read_bytes())
    manifest["artifacts"][0]["digest"] = "sha256:" + hashlib.sha256(
        (source / "completion-bundle.json").read_bytes(),
    ).hexdigest()
    manifest.pop("manifest_digest")
    manifest["manifest_digest"] = "sha256:" + hashlib.sha256(json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    write(source / "period-manifest.json", manifest)
    return source


class RejectedDescriptionReplayTests(unittest.TestCase):
    def cached_repair(self, root: Path, *, rejected: bool):
        source = historical_source(root, rejected=rejected)
        before = fixtures.run_tree_snapshot(source)
        analysis = json.loads((source / "semantic-analysis.json").read_bytes())
        failure = next(row for row in analysis["exceptions"]
                       if row["kind"] == "analyzer_review_failure")
        digest = semantic.stable_digest("frt-", failure["evidence_ids"], length=64)
        scope = root / "scope.json"
        write(scope, {"evidence_ids": failure["evidence_ids"]})
        output = root / "recovery"
        endpoint = semantic.AnalyzerResponseCache(
            source / "analyzer-cache-used.jsonl",
        ).sealed_endpoints()[0]
        # Offline synthetic response seeds the sealed cache; no provider exists.
        with (
            mock.patch.object(semantic.AnalyzerEndpoint, "from_env", return_value=endpoint),
            mock.patch.object(semantic, "http_transport", side_effect=lambda _, body:
                              fixtures.analyzer_provider_response(json.loads(body["messages"][-1]["content"]))),
            mock.patch.dict(os.environ, {"CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED": "approved"}),
        ):
            scoped.run(scoped.parse_args([
                str(source), "--scope-file", str(scope), "--output-dir", str(output),
                "--failed-review-digest", digest,
            ]))
        cache_before = (output / "analyzer-response-cache.jsonl").read_bytes()
        stderr = io.StringIO()
        with (
            mock.patch.object(semantic, "http_transport", side_effect=AssertionError("Must not infer")),
            redirect_stdout(io.StringIO()), redirect_stderr(stderr),
        ):
            code = review.main([
                "--repair-from", str(source), "--retry-failed-reviews",
                "--retry-review-digest", digest, "--scoped-recovery-from", str(output),
                "--runs-root", str(source.parent), "--state", str(root / "repair-items.json"),
            ])
        self.assertEqual(0, code, stderr.getvalue())
        self.assertEqual(before, fixtures.run_tree_snapshot(source))
        self.assertEqual(cache_before, (output / "analyzer-response-cache.jsonl").read_bytes())
        return source, next(source.parent.glob("*-repair-*")), before

    def replay(self, root: Path, child: Path) -> Path:
        before = fixtures.run_tree_snapshot(child)
        stderr = io.StringIO()
        with (
            mock.patch.object(semantic, "http_transport", side_effect=AssertionError("Must not infer")),
            mock.patch.object(review, "_sealed_replay_transport", side_effect=AssertionError("Must not infer")),
            redirect_stdout(io.StringIO()), redirect_stderr(stderr),
        ):
            code = review.main([
                "--replay-from", str(child), "--runs-root", str(child.parent),
                "--state", str(root / "replay-items.json"),
            ])
        replay = next(child.parent.glob(f"*-replay-{child.name}*"))
        integrity = json.loads((replay / "replay-integrity.json").read_bytes())
        self.assertEqual(0, code, stderr.getvalue() or str(integrity["failures"]))
        self.assertEqual(before, fixtures.run_tree_snapshot(child))
        self.assertEqual((child / "work-accounting-result.json").read_bytes(),
                         (replay / "work-accounting-result.json").read_bytes())
        self.assertEqual((child / "semantic-analysis.json").read_bytes(),
                         (replay / "semantic-analysis.json").read_bytes())
        self.assertEqual("pass", integrity["status"])
        return replay

    def test_rejected_historical_render_derives_exact_offline_replay(self):
        # Missing canonical rejection leaves historical str vs replay null.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, child, before = self.cached_repair(root, rejected=True)
            self.replay(root, child)
            result = json.loads((child / "work-accounting-result.json").read_bytes())
            rejected = [row for row in result["ambiguous"]
                        if row.get("wording_repair_required")]
            self.assertEqual(1, len(rejected))
            row = rejected[0]
            self.assertEqual("pending", row["review_status"])
            self.assertEqual("description_contract", row["exception_kind"])
            self.assertIsNone(row["activity"]["rendered_description"])
            derived = json.loads((child / "semantic-analysis.json").read_bytes())
            retained = next(a for a in derived["activities"] if a["activity_id"] == row["activity_id"])
            self.assertEqual(retained, row["activity"])
            original = json.loads((source / "semantic-analysis.json").read_bytes())["activities"][0]
            self.assertIsInstance(original["rendered_description"], str)
            expected = copy.deepcopy(original)
            expected["rendered_description"] = None
            self.assertEqual(expected, retained)
            self.assertNotIn(row["activity_id"], {p["activity_id"] for p in result["proposals"]})
            self.assertEqual(before, fixtures.run_tree_snapshot(source))

    def test_valid_historical_render_remains_proposed_and_replays(self):
        # Rejection canonicalization must not clear valid derived rendering.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, child, before = self.cached_repair(root, rejected=False)
            self.replay(root, child)
            result = json.loads((child / "work-accounting-result.json").read_bytes())
            self.assertEqual(2, len(result["proposals"]))
            self.assertFalse(any(row.get("wording_repair_required") for row in result["ambiguous"]))
            original_id = json.loads((source / "semantic-analysis.json").read_bytes())["activities"][0]["activity_id"]
            proposal = next(p for p in result["proposals"] if p["activity_id"] == original_id)
            self.assertIsInstance(proposal["rendered_description"], str)
            self.assertEqual(proposal["description"], proposal["rendered_description"])
            self.assertEqual(before, fixtures.run_tree_snapshot(source))

    def test_true_financial_difference_still_blocks_native_replay_integrity(self):
        # Exact accounting identity must still reject real credit changes.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _, child, _ = self.cached_repair(root, rejected=False)
            replay = self.replay(root, child)
            changed = json.loads((replay / "work-accounting-result.json").read_bytes())
            changed["proposals"][0]["duration_minutes"] += 1
            write(replay / "work-accounting-result.json", changed)
            with mock.patch.object(review, "RUNS", child.parent):
                integrity = review.derive_replay_integrity(child, replay)
            self.assertEqual("blocked", integrity["status"])
            self.assertEqual(["work accounting result differs"], integrity["failures"])


if __name__ == "__main__":
    unittest.main()
