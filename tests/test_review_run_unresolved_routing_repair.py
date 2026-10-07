"""Source-bound zero-capacity repairs remain evidence, never invented time."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_review_run as fixtures
from scripts import clockify_monthly_unresolved as monthly, review_corrections
from scripts import work_accounting_pipeline as accounting
from test_review_run_chained_repair_replay import validate_repair

run = fixtures.review_run


class UnresolvedRoutingRepairTests(unittest.TestCase):
    def source(self, root):
        original = fixtures.evidence_ledger.evidence_event

        def short_event(*args, **kwargs):
            kwargs["raw_source_span"] = {
                **kwargs["raw_source_span"], "end": "2026-08-01T10:00:59.792000Z",
            }
            return original(*args, **kwargs)

        with mock.patch.object(fixtures.evidence_ledger, "evidence_event", side_effect=short_event):
            source = fixtures.ReviewRunResultTests._write_real_offline_replay_source(root / "runs", root)
        self.assertEqual([], json.loads((source / "proposals.json").read_text()))
        analysis = json.loads((source / "semantic-analysis.json").read_text())
        activity = analysis["activities"][0]
        self.assertEqual("timing_evidence", json.loads((source / "ambiguous.json").read_text())[0]["exception_kind"])
        item = {"id": "rvi-unresolved", "current": activity}
        routing = root / "routing.json"
        fixtures.write_json(routing, {"session_routes": [*json.loads((source / "routing.json").read_text())["session_routes"], {
            "project_name": "Serenichron Internal", "project_suffix": "internal1",
            "tag_names": ["Technical development"], "tag_suffixes": ["technical1"],
            "prefix": "SC", "billable": False,
        }], "meeting_routes": []})
        proposed = root / "corrections.jsonl"
        record = review_corrections.build_decision(
            item, decision="modify", reviewer="human", reviewed_at="2026-10-07T12:00:00Z",
            correction_categories=["routing", "wording"], rationale="Internal authentication work, not client image work.",
            field_patch={
                "description": {"op": "replace", "value": "SC — Fixed internal authentication"},
                "client_project": {"op": "replace", "value": "Serenichron Internal"},
                "tag_names": {"op": "replace", "value": ["Technical development"]},
            },
        )
        review_corrections.append_decision(proposed, record, item=item)
        return source, proposed, routing

    def test_unresolved_repair_rejects_stale_targets_and_non_metadata_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, proposed, routing = self.source(root)
            record = json.loads(proposed.read_text())
            variants = {
                "unknown activity": {**record, "activity_id": "unknown"},
                "stale evidence": {**record, "evidence_fingerprint": "sha256:" + "0" * 64},
                "skip": {**record, "decision": "skip", "field_patch": {}, "correction_categories": ["allocation"]},
                "duration": {**record, "field_patch": {**record["field_patch"], "duration_minutes": {"op": "replace", "value": 1}}},
                "unknown project": {**record, "field_patch": {**record["field_patch"], "client_project": {"op": "replace", "value": "Unknown"}}},
                "unknown tag": {**record, "field_patch": {**record["field_patch"], "tag_names": {"op": "replace", "value": ["Unknown"]}}},
                "wording only": {**record, "correction_categories": ["wording"], "field_patch": {"description": record["field_patch"]["description"]}},
            }
            for name, variant in variants.items():
                with self.subTest(name=name):
                    proposed.write_text(json.dumps(variant) + "\n")
                    with self.assertRaises(run.ReviewRunError):
                        run._validate_repair_credit_transition(source, proposed, runs_root=root / "runs", routing_snapshot=routing)

    def test_unresolved_target_requires_unique_semantic_and_exact_accounting_citations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, proposed, routing = self.source(root)
            original = fixtures.run_tree_snapshot(source)
            semantic_path = source / "semantic-analysis.json"
            semantic_bytes = semantic_path.read_bytes()
            analysis = json.loads(semantic_bytes)
            for activities in ([], analysis["activities"] * 2):
                with self.subTest(activities=len(activities)):
                    fixtures.write_json(semantic_path, {**analysis, "activities": activities})
                    with self.assertRaises(run.ReviewRunError):
                        run._validate_repair_credit_transition(source, proposed, runs_root=root / "runs", routing_snapshot=routing)
            semantic_path.write_bytes(semantic_bytes)
            ambiguity_path = source / "ambiguous.json"
            original_ambiguity = ambiguity_path.read_bytes()
            ambiguity_path.write_text("[]\n")
            with self.assertRaises(run.ReviewRunError):
                run._validate_repair_credit_transition(source, proposed, runs_root=root / "runs", routing_snapshot=routing)
            ambiguity_path.write_bytes(original_ambiguity)
            proposed.write_bytes(proposed.read_bytes() + b"not-json\n")
            with self.assertRaises(run.ReviewRunError):
                run._validate_repair_credit_transition(source, proposed, runs_root=root / "runs", routing_snapshot=routing)
            self.assertEqual(original, fixtures.run_tree_snapshot(source))

    def test_zero_capacity_metadata_repair_is_visible_and_replays_without_time(self):
        # Removing unresolved admission or derived metadata must fail this real pipeline contract.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, proposed, routing = self.source(root)
            before = fixtures.run_tree_snapshot(source)
            with mock.patch.object(run, "RUNS", root / "runs"):
                try:
                    repair = run._prepare_repair_run(source, corrections_override=proposed, routing_override=routing)
                except run.ReviewRunError as exc:
                    self.fail(f"exact source-backed zero-capacity metadata repair rejected: {exc}")
                result = accounting.run_accounting(
                    repair, root=fixtures.ROOT, routing_path=repair / "routing.json",
                    corrections_path=repair / "review-corrections.jsonl",
                    analysis_fixture=run._repair_analysis_fixture(repair),
                )
                self.assertEqual([], result["proposals"])
                self.assertEqual(1, len(result["ambiguous"]))
                self.assertEqual("timing_evidence", result["ambiguous"][0]["exception_kind"])
                self.assertEqual("pass", result["correction_regression"]["results"][0]["status"])
                rows = monthly.project_rows(repair)
                self.assertEqual(1, len(rows))
                self.assertEqual("Serenichron Internal", rows[0][5])
                self.assertEqual("SC — Fixed internal authentication", rows[0][6])
                validate_repair(repair, root / "runs", root / "repair-state.json")
                run._finalize_repair_completion(repair)
                with mock.patch.dict(os.environ, {"CLOCKIFY_ANALYZER_PRIMARY_URL": "", "CLOCKIFY_ANALYZER_FALLBACK_URL": ""}), \
                     mock.patch.object(run, "_sealed_replay_transport", side_effect=AssertionError("cache miss")), \
                     contextlib.redirect_stdout(io.StringIO()):
                    code = run.main(["--replay-from", str(repair), "--runs-root", str(root / "runs"), "--state", str(root / "replay-state.json")])
                self.assertEqual(0, code)
                replay = next((root / "runs").glob(f"*-replay-{repair.name}*"))
                self.assertEqual("pass", json.loads((replay / "replay-integrity.json").read_text())["status"])
                self.assertEqual((repair / "work-accounting-result.json").read_bytes(), (replay / "work-accounting-result.json").read_bytes())
                self.assertEqual(rows[0][:11], monthly.project_rows(replay)[0][:11])
            self.assertEqual(before, fixtures.run_tree_snapshot(source))


if __name__ == "__main__":
    unittest.main()
