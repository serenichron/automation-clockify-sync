import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_review_run as fixtures

review_run = fixtures.review_run
write_json = fixtures.write_json


class RepairRunTests(unittest.TestCase):
    def fixture(self, runs):
        source, replay = fixtures.ReviewRunResultTests._complete_replay_fixture(runs)
        fixtures.ReviewRunResultTests._bootstrap_snapshots(source, replay)
        (source / "run-report.md").write_text("# synthetic source\n")
        write_json(source / "autopilot-result.json", {"quality_status": "pass"})
        return source

    def test_repair_copies_only_frozen_inputs_to_distinct_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            source = self.fixture(runs)
            before = {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()}
            with mock.patch.object(review_run, "RUNS", runs):
                target = review_run._prepare_repair_run(source)
            self.assertNotEqual(source, target)
            self.assertFalse((target / "work-accounting-result.json").exists())
            self.assertFalse((target / "replay-source.json").exists())
            for filename in review_run._RECONCILIATION_INPUTS.values():
                self.assertEqual(before[filename], (target / filename).read_bytes())
            self.assertEqual(before, {str(p.relative_to(source)): p.read_bytes() for p in source.rglob("*") if p.is_file()})

    def test_repair_cli_never_collects_or_uses_replay_fixture(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            source = self.fixture(runs)
            def process(args, target, _gate):
                self.assertEqual(target / "routing.json", args.routing)
                self.assertEqual(target / "review-corrections.jsonl", args.corrections)
                self.assertEqual(Path(tmp) / "cache", args.analyzer_cache)
                self.assertFalse(getattr(args, "_replay_analysis_fixture", None))
                return 0, target / "autopilot-result.json"
            with mock.patch.object(review_run, "RUNS", runs), mock.patch.object(
                review_run, "_run", side_effect=AssertionError("collector invoked")
            ), mock.patch.object(review_run, "_process_run", side_effect=process), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, review_run.main([
                    "--repair-from", str(source), "--state", str(Path(tmp) / "state"),
                    "--analyzer-cache", str(Path(tmp) / "cache"),
                ]))

    def test_repair_rejects_drifted_completed_source_before_derivation(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            source = self.fixture(runs)
            write_json(source / "quality_report.json", {"status": "blocked"})
            with mock.patch.object(review_run, "RUNS", runs), self.assertRaises(ValueError):
                review_run._prepare_repair_run(source)

    def test_repair_completion_owns_new_artifacts_without_replacing_source_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            source = self.fixture(runs)
            original = (source / "completion-bundle.json").read_bytes()
            with mock.patch.object(review_run, "RUNS", runs):
                target = review_run._prepare_repair_run(source)
                for name in ("semantic-analysis.json", "work-accounting-result.json", "quality_report.json", "review-snapshot.json"):
                    (target / name).write_bytes((source / name).read_bytes())
                bundle = review_run._finalize_repair_completion(target)
            self.assertEqual(target, bundle.run_dir)
            self.assertEqual(original, (source / "completion-bundle.json").read_bytes())
            self.assertTrue((target / "completion-bundle.json").is_file())


if __name__ == "__main__":
    unittest.main()
