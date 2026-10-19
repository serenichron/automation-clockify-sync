"""A sealed Claude opt-in uses its routing snapshot without new inference."""
import contextlib
import datetime as dt
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import semantic_analyzer
import test_claude_session_timing as timing
import test_review_run as replay_fixtures
import test_work_accounting_pipeline as fixtures
from test_review_run_chained_repair_replay import validate_repair


class ClaudeTimingReplayTests(unittest.TestCase):
    def test_opted_in_result_only_source_replays_exactly_with_immutable_snapshot(self):
        humans = [timing.event(f"2026-09-25T09:{minute}:00+03:00") for minute in ("00", "20")]
        results = [timing.event(f"2026-09-25T09:03:{second}+03:00", "assistant") for second in ("00", "20")]
        analysis = fixtures.analysis_for([item.evidence_id for item in results], recommended=10)
        analysis["activities"][0].update({"semantic_reviewer_model": "deepseek-v4-flash:cloud",
            "analyzer_model": "fixture", "analyzer_tier": "fixture"})
        analysis.update({"evidence_bundle_schema_version": "clockify-semantic-evidence-bundle/v1",
            "evidence_bundle_manifest": replay_fixtures.bundle_manifest(), "omissions": [
                {"lifecycle": "noise", "reason": "Human timing context only",
                 "evidence_ids": [item.evidence_id]} for item in humans]})
        with tempfile.TemporaryDirectory() as temporary:
            routing_path = Path(temporary) / "routing.json"
            routing = json.loads((fixtures.ROOT / "routing.json").read_text())
            routing["session_timing_pool_sources"] = timing.ENABLED
            fixtures.write_json(routing_path, routing)
            source, result = fixtures.WorkAccountingPipelineTests.make_run(
                self, [*humans, *results], analysis, routing_path=routing_path,
            )
        self.assertEqual(10, sum(row["duration_minutes"] for row in result["proposals"]))
        self.assertEqual("estimated", result["proposals"][0]["provenance"]["timing_placement"])
        run, runs = replay_fixtures.review_run, source.parent
        fixtures.write_json(source / "routing.json", routing)
        fixtures.write_json(source / "run-report.json", {
            "run_id": source.name, "runtime_identity": {"git_sha": "fixture"},
            "date_range": {"since": "2026-09-25T00:00:00Z", "until": "2026-09-26T00:00:00Z"},
            "evidence_ledger": {"source_completeness": result["ledger_manifest"]["source_completeness"]},
        })
        (source / "run-report.md").write_text("# Synthetic opted-in Claude timing source\n")
        (source / "review-corrections.jsonl").write_bytes(b"")
        (source / "review-acceptance.jsonl").write_bytes(b"")
        validate_repair(source, runs, runs.parent / "source-state.json")
        slice_ = run.clockify_sync_collect.plan_slices(
            dt.datetime(2026, 9, 25, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 9, 26, tzinfo=dt.timezone.utc),
            zone=run.clockify_sync_collect.BUCHAREST,
        )[0]
        bundle_path = source / "completion-bundle.json"
        run.collector_receipts.write_completion_bundle(
            bundle_path, run.collector_receipts.build_completion_bundle(source, slice_=slice_),
        )
        manifest = {
            "schema_version": "reconciliation-manifest/v1", "compatibility_version": "reconciliation-manifest/v1",
            "period": {"compatibility_version": "reconciliation-period/v1", "member_id": "member-fixture",
                "workspace_id": "workspace-fixture", "timezone": "Europe/Bucharest",
                "since_utc": "2026-09-25T00:00:00Z", "until_utc": "2026-09-26T00:00:00Z", "revision": 1},
            "state": "reconciling", "event_count": 4, "events_digest": "sha256:" + "d" * 64,
            "blockers": [], "artifacts": [{"path": str(bundle_path.resolve()),
                "schema_version": "collector-completion-bundle/v1",
                "compatibility_version": "collector-completion-bundle/v1",
                "digest": "sha256:" + hashlib.sha256(bundle_path.read_bytes()).hexdigest()}],
        }
        manifest["manifest_digest"] = "sha256:" + hashlib.sha256(json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode()).hexdigest()
        fixtures.write_json(source / "period-manifest.json", manifest)
        before = replay_fixtures.run_tree_snapshot(source)
        output = io.StringIO()
        with mock.patch.object(run, "RUNS", runs), \
             mock.patch.object(run, "_sealed_replay_transport", side_effect=AssertionError("inference forbidden")), \
             mock.patch.object(semantic_analyzer, "analyze_tiered", side_effect=AssertionError("analysis forbidden")), \
             contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = run.main(["--replay-from", str(source), "--runs-root", str(runs),
                             "--state", str(runs.parent / "replay-state.json")])
        self.assertEqual(0, code, output.getvalue())
        replay, = runs.glob(f"*-replay-{source.name}*")
        self.assertEqual("pass", json.loads((replay / "replay-integrity.json").read_text())["status"])
        self.assertEqual((source / "work-accounting-result.json").read_bytes(),
                         (replay / "work-accounting-result.json").read_bytes())
        replay_result = json.loads((replay / "work-accounting-result.json").read_text())
        self.assertEqual(timing.ENABLED, replay_result["allocation"]["deterministic_inputs"]["session_timing_pool_sources"])
        self.assertEqual(before, replay_fixtures.run_tree_snapshot(source))


if __name__ == "__main__":
    unittest.main()
