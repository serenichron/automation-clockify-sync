import contextlib
import hashlib
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

    def test_repair_cli_uses_private_semantic_fixture_without_source_cache_or_inference(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            source = self.fixture(runs)
            source_before = {
                str(path.relative_to(source)): path.read_bytes()
                for path in source.rglob("*") if path.is_file()
            }
            def process(args, target, _gate):
                self.assertEqual(target / "routing.json", args.routing)
                self.assertEqual(target / "review-corrections.jsonl", args.corrections)
                fixture = getattr(args, "_repair_analysis_fixture", None)
                self.assertEqual(
                    target / "repair-fixture" / "semantic-analysis.json", fixture
                )
                self.assertEqual(
                    (source / "semantic-analysis.json").read_bytes(),
                    fixture.read_bytes(),
                )
                self.assertIsNone(getattr(args, "_repair_analyzer_cache", None))
                self.assertEqual(
                    source_before,
                    {
                        str(path.relative_to(source)): path.read_bytes()
                        for path in source.rglob("*") if path.is_file()
                    },
                )
                return 0, target / "autopilot-result.json"
            with mock.patch.object(review_run, "RUNS", runs), mock.patch.object(
                review_run, "_run", side_effect=AssertionError("collector invoked")
            ), mock.patch.object(review_run, "_process_run", side_effect=process), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, review_run.main([
                    "--repair-from", str(source), "--state", str(Path(tmp) / "state"),
                    "--analyzer-cache", str(source / "missing-cache.jsonl"),
                ]))

    def test_repair_copies_bound_private_cache_for_later_replay_without_consuming_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            source = self.fixture(runs)
            cache_content = b'{"private":"sealed"}\n'
            (source / "analyzer-cache-used.jsonl").write_bytes(cache_content)
            analysis_path = source / "semantic-analysis.json"
            analysis = json.loads(analysis_path.read_text())
            analysis["activities"] = [{
                "analyzer_model": "deepseek-v4.1-flash:cloud",
                "analyzer_tier": "primary",
            }]
            analysis["analyzer_cache"] = {
                "records": [],
                "snapshot": {
                    "path": "analyzer-cache-used.jsonl",
                    "record_count": 1,
                    "sha256": hashlib.sha256(cache_content).hexdigest(),
                },
            }
            write_json(analysis_path, analysis)
            slice_ = review_run.clockify_sync_collect.plan_slices(
                fixtures.dt.datetime(2026, 8, 1, tzinfo=fixtures.dt.timezone.utc),
                fixtures.dt.datetime(2026, 8, 2, tzinfo=fixtures.dt.timezone.utc),
                zone=review_run.clockify_sync_collect.BUCHAREST,
            )[0]
            bundle = review_run.collector_receipts.build_completion_bundle(
                source, slice_=slice_,
            )
            review_run.collector_receipts.write_completion_bundle(
                source / "completion-bundle.json", bundle,
            )
            source_before = {
                str(path.relative_to(source)): path.read_bytes()
                for path in source.rglob("*") if path.is_file()
            }

            def process(args, target, _gate):
                self.assertEqual(
                    target / "repair-fixture" / "semantic-analysis.json",
                    args._repair_analysis_fixture,
                )
                self.assertIsNone(args._repair_analyzer_cache)
                target_cache = target / "analyzer-cache-used.jsonl"
                self.assertEqual(cache_content, target_cache.read_bytes())
                target_analysis = json.loads(
                    args._repair_analysis_fixture.read_text()
                )
                snapshot = target_analysis["analyzer_cache"]["snapshot"]
                self.assertEqual(target_cache.name, snapshot["path"])
                self.assertEqual(
                    hashlib.sha256(target_cache.read_bytes()).hexdigest(),
                    snapshot["sha256"],
                )
                return 0, target / "autopilot-result.json"

            with mock.patch.object(review_run, "RUNS", runs), mock.patch.object(
                review_run, "_run", side_effect=AssertionError("collector invoked")
            ), mock.patch.object(
                review_run, "_process_run", side_effect=process
            ), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, review_run.main([
                    "--repair-from", str(source),
                    "--state", str(Path(tmp) / "state"),
                    "--analyzer-cache", str(source / "mutable-cache.jsonl"),
                ]))

            self.assertEqual(source_before, {
                str(path.relative_to(source)): path.read_bytes()
                for path in source.rglob("*") if path.is_file()
            })

    def test_repair_rejects_drifted_completed_source_before_derivation(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            source = self.fixture(runs)
            write_json(source / "quality_report.json", {"status": "blocked"})
            with mock.patch.object(review_run, "RUNS", runs), self.assertRaises(ValueError):
                review_run._prepare_repair_run(source)

    def test_repair_accepts_verified_incomplete_coverage_and_preserves_debt_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            source = self.fixture(runs)
            coverage = {
                "status": "partial",
                "incomplete_sources": ["sessions/omarchy-desktop"],
                "sources": {
                    "sessions/omarchy-desktop": {
                        "status": "partial", "reason": "host unavailable",
                    },
                },
            }
            before = {
                str(path.relative_to(source)): path.read_bytes()
                for path in source.rglob("*") if path.is_file()
            }

            with mock.patch.object(review_run, "RUNS", runs), mock.patch.object(
                review_run.collector_receipts,
                "completion_coverage",
                return_value=coverage,
            ):
                target = review_run._prepare_repair_run(source)

            lineage = json.loads((target / "repair-source.json").read_text())
            self.assertEqual(coverage, lineage["source_coverage"])
            self.assertEqual(
                before,
                {
                    str(path.relative_to(source)): path.read_bytes()
                    for path in source.rglob("*") if path.is_file()
                },
            )

    def test_repair_routing_override_is_isolated_and_digest_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            runs = Path(tmp) / "runs"
            source = self.fixture(runs)
            source_routing = (source / "routing.json").read_bytes()
            override = Path(tmp) / "routing-override.json"
            override.write_text(json.dumps({
                "workspace_id": "workspace-1",
                "member_id": "member-1",
                "client_lifecycle_routes": [{
                    "client": "Mazilu & Partners",
                    "activation": {"effective_at": "2026-09-24T00:00:00+03:00"},
                }],
            }, sort_keys=True) + "\n")

            with mock.patch.object(review_run, "RUNS", runs):
                target = review_run._prepare_repair_run(
                    source, routing_override=override,
                )

            lineage = json.loads((target / "repair-source.json").read_text())
            self.assertEqual(source_routing, (source / "routing.json").read_bytes())
            self.assertEqual(override.read_bytes(), (target / "routing.json").read_bytes())
            self.assertEqual(
                review_run._file_sha256(source / "routing.json", label="source routing"),
                lineage["source_routing_sha256"],
            )
            self.assertEqual(
                review_run._file_sha256(target / "routing.json", label="target routing"),
                lineage["repair_routing_sha256"],
            )
            for filename in (
                "period-manifest.json", "review-corrections.jsonl",
                "review-acceptance.jsonl",
            ):
                self.assertEqual(
                    (source / filename).read_bytes(), (target / filename).read_bytes()
                )

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
