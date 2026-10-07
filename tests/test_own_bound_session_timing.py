"""Pool membership must not erase independently cited observed boundaries."""
import json
import unittest
import datetime as dt
import hashlib
import contextlib
import io
from unittest import mock

from scripts import work_accounting_pipeline as pipeline
from test_codex_session_timing_contexts import codex_event
import test_work_accounting_pipeline as fixtures
import test_review_run as run_fixtures
from test_review_run_chained_repair_replay import validate_repair


class OwnBoundSessionTimingTests(unittest.TestCase):
    def scenario(self):
        first = codex_event("2026-09-25T09:00:00+03:00")
        atomic = codex_event("2026-09-25T09:00:00+03:00", "assistant")
        direct = codex_event("2026-09-25T09:00:10+03:00")
        short = codex_event("2026-09-25T09:01:00+03:00")
        short_result = codex_event("2026-09-25T09:02:00+03:00", "assistant")
        borrowed_short = codex_event("2026-09-25T09:03:00+03:00")
        borrowed_short_result = codex_event("2026-09-25T09:05:00+03:00", "assistant")
        unpaired = codex_event("2026-09-25T09:06:00+03:00")
        last = codex_event("2026-09-25T09:25:10+03:00")
        direct_result = codex_event("2026-09-25T09:25:10+03:00", "assistant")
        native = fixtures.clockify_event("2026-09-25T09:00:00+03:00", "2026-09-25T09:25:10+03:00",
                                         project_name="Other client", description="Distinct pre-existing work")
        groups = [([direct, direct_result], 12, "Independent cited clarification"),
                  ([short, short_result], 1, "Independent cited blocker correction"),
                  ([first, atomic], 15, "Borrowed atomic outcome"),
                  ([borrowed_short, borrowed_short_result], 15, "Borrowed under-capacity outcome"),
                  ([unpaired], 15, "Unpaired zero-capacity outcome")]
        activities = []
        for group, minutes, name in groups:
            activity = fixtures.analysis_for([event.evidence_id for event in group], recommended=minutes)["activities"][0]
            activity["object"] = name
            activity["semantic_reviewer_model"] = "deepseek-v4-flash:cloud"
            activity["analyzer_model"] = "fixture"
            activity["analyzer_tier"] = "fixture"
            activities.append(activity)
        analysis = {"activities": activities, "exceptions": [],
                    "evidence_bundle_schema_version": "clockify-semantic-evidence-bundle/v1",
                    "evidence_bundle_manifest": run_fixtures.bundle_manifest(), "omissions": [{
            "lifecycle": "noise", "reason": "Human context anchor only", "evidence_ids": [last.evidence_id],
        }]}
        return fixtures.WorkAccountingPipelineTests.make_run(
            self, [first, atomic, direct, short, short_result, borrowed_short,
                   borrowed_short_result, unpaired, last, direct_result, native], analysis,
        )

    def test_independent_citation_bounds_recover_only_requested_overlap_with_warnings(self):
        # Blanket shared-pool membership must fail: it wrongly suppresses these two recoveries.
        run_dir, result = self.scenario()
        proposals = result["proposals"]
        self.assertEqual([1, 12], sorted(row["duration_minutes"] for row in proposals))
        self.assertEqual(2, len(result["allocation"]["capacity_recoveries"]))
        for row in proposals:
            self.assertNotIn("timing_placement", row["provenance"])
            warnings = {warning["type"] for warning in row["review_warnings"]}
            self.assertIn("allocation_capacity_recovery", warnings)
            self.assertIn("existing_clockify_overlap", warnings)
            self.assertNotIn("estimated_session_placement", warnings)
            if row["duration_minutes"] == 1:
                self.assertEqual("2026-09-25T09:01:00+03:00", row["start"])
                self.assertEqual("2026-09-25T09:02:00+03:00", row["end"])
            else:
                self.assertEqual("2026-09-25T09:00:10+03:00", row["start"])
                self.assertEqual("2026-09-25T09:12:10+03:00", row["end"])
        residual = result["ambiguous"]
        self.assertEqual(3, len(residual))
        contested = [row for row in residual if row["exception_kind"] == "contested_time"]
        self.assertEqual(2, len(contested))
        for row in contested:
            self.assertEqual(15, row["unallocated_minutes"])
            self.assertEqual("estimated", row["timing_placement"])
        self.assertEqual(1, len([row for row in residual if row["exception_kind"] == "timing_evidence"]))
        self.assertEqual([], result["allocation"]["allocations"])
        # Real deterministic re-accounting of the exact derived analysis is byte-identical.
        before = (run_dir / "work-accounting-result.json").read_bytes()
        pipeline.run_accounting(run_dir, root=fixtures.ROOT,
                                analysis_fixture=run_dir / "semantic-analysis.json")
        self.assertEqual(before, (run_dir / "work-accounting-result.json").read_bytes())

    def test_completed_own_bound_accounting_replays_exactly_without_inference(self):
        source, result = self.scenario()
        run = run_fixtures.review_run
        runs = source.parent
        fixtures.write_json(source / "run-report.json", {
            "run_id": source.name, "runtime_identity": {"git_sha": "fixture"},
            "date_range": {"since": "2026-09-25T00:00:00Z", "until": "2026-09-26T00:00:00Z"},
            "evidence_ledger": {"source_completeness": result["ledger_manifest"]["source_completeness"]},
        })
        (source / "run-report.md").write_text("# Synthetic own-bound timing source\n")
        (source / "routing.json").write_bytes((fixtures.ROOT / "routing.json").read_bytes())
        (source / "review-corrections.jsonl").write_bytes(b"")
        (source / "review-acceptance.jsonl").write_bytes(b"")
        validate_repair(source, runs, source.parent.parent / "source-state.json")
        slice_ = run.clockify_sync_collect.plan_slices(
            dt.datetime(2026, 9, 25, tzinfo=dt.timezone.utc),
            dt.datetime(2026, 9, 26, tzinfo=dt.timezone.utc),
            zone=run.clockify_sync_collect.BUCHAREST,
        )[0]
        bundle = run.collector_receipts.build_completion_bundle(source, slice_=slice_)
        run.collector_receipts.write_completion_bundle(source / "completion-bundle.json", bundle)
        manifest = {
            "schema_version": "reconciliation-manifest/v1",
            "compatibility_version": "reconciliation-manifest/v1",
            "period": {"compatibility_version": "reconciliation-period/v1",
                "member_id": "member-fixture", "workspace_id": "workspace-fixture",
                "timezone": "Europe/Bucharest", "since_utc": "2026-09-25T00:00:00Z",
                "until_utc": "2026-09-26T00:00:00Z", "revision": 1},
            "state": "reconciling", "event_count": 11,
            "events_digest": "sha256:" + "d" * 64, "blockers": [],
            "artifacts": [{"path": str((source / "completion-bundle.json").resolve()),
                "schema_version": "collector-completion-bundle/v1",
                "compatibility_version": "collector-completion-bundle/v1",
                "digest": "sha256:" + hashlib.sha256((source / "completion-bundle.json").read_bytes()).hexdigest()}],
        }
        manifest["manifest_digest"] = "sha256:" + hashlib.sha256(json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()
        fixtures.write_json(source / "period-manifest.json", manifest)
        before = run_fixtures.run_tree_snapshot(source)
        output = io.StringIO()
        with mock.patch.object(run, "RUNS", runs), \
             mock.patch.object(run, "_sealed_replay_transport", side_effect=AssertionError("inference forbidden")), \
             contextlib.redirect_stdout(output):
            code = run.main(["--replay-from", str(source), "--runs-root", str(runs),
                             "--state", str(runs.parent / "replay-state.json")])
        replay = next(runs.glob(f"*-replay-{source.name}*"))
        self.assertEqual(0, code, (replay / "autopilot-result.json").read_text())
        self.assertEqual("pass", json.loads((replay / "replay-integrity.json").read_text())["status"])
        self.assertEqual((source / "work-accounting-result.json").read_bytes(),
                         (replay / "work-accounting-result.json").read_bytes())
        self.assertEqual(before, run_fixtures.run_tree_snapshot(source))


if __name__ == "__main__":
    unittest.main()
