import argparse
import datetime as dt
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts import clockify_review_run as review
from scripts import clockify_sync_collect as collector
from scripts import reconciliation_manifest as periods


CUTOFF = "2026-10-22T03:00:00Z"


class ExactCutoffTests(unittest.TestCase):
    def collection_args(self, **overrides):
        return argparse.Namespace(since="2026-10-21", until=None,
                                  until_exclusive=CUTOFF, **overrides)

    def inputs(self, root, until=CUTOFF):
        identity = periods.PeriodIdentity(
            member_id="member", workspace_id="workspace", timezone="Europe/Bucharest",
            since=dt.datetime.fromisoformat("2026-10-21T00:00:00+03:00"),
            until=dt.datetime.fromisoformat(until.replace("Z", "+00:00")), revision=1,
        )
        store = periods.CoordinatorEventStore(root / "period-events.jsonl")
        store.append(identity, "period_opened", {"revision": 1},
                     occurred_at=dt.datetime(2026, 10, 22, 4, tzinfo=dt.timezone.utc))
        manifest = periods.ReconciliationCoordinator(identity, store).derive().document()
        (root / "period-manifest.json").write_text(json.dumps(manifest))
        (root / "routing.json").write_text("{}")
        (root / "review-corrections.jsonl").write_text("")
        (root / "review-acceptance.jsonl").write_text("")
        return ["--since", "2026-10-21", "--until-exclusive", CUTOFF,
                "--period-manifest", str(root / "period-manifest.json"),
                "--routing", str(root / "routing.json"),
                "--corrections", str(root / "review-corrections.jsonl"),
                "--acceptance-ledger", str(root / "review-acceptance.jsonl")]

    def test_collector_uses_exact_exclusive_utc_endpoint(self):
        since, until, _ = collector.compute_range(self.collection_args(), {}, {"_missing": True})
        self.assertEqual("2026-10-20T21:00:00+00:00", since.astimezone(dt.timezone.utc).isoformat())
        self.assertEqual("2026-10-22T03:00:00+00:00", until.astimezone(dt.timezone.utc).isoformat())

    def test_collector_rejects_naive_malformed_and_nonpositive_bounds(self):
        for raw in ("2026-10-22T03:00:00", "not-a-time", "2026-10-20T21:00:00Z"):
            args = self.collection_args()
            args.until_exclusive = raw
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                collector.compute_range(args, {}, {"_missing": True})

    def test_collector_rejects_date_end_with_timestamp_end(self):
        args = self.collection_args()
        args.until = "2026-10-21"
        with self.assertRaises(ValueError):
            collector.compute_range(args, {}, {"_missing": True})

    def test_legacy_inclusive_date_end_is_unchanged(self):
        args = argparse.Namespace(since="2026-10-21", until="2026-10-21")
        _, until, _ = collector.compute_range(args, {}, {"_missing": True})
        self.assertEqual("2026-10-21T21:00:00+00:00", until.astimezone(dt.timezone.utc).isoformat())

    def test_native_bound_overrides_are_rejected_before_external_work(self):
        for mode in ("--resume-from", "--repair-from", "--replay-from",
                     "--materialize-frozen-from", "--derive-pending-from"):
            with self.subTest(mode=mode), mock.patch.object(review, "_run", side_effect=AssertionError("external work")):
                self.assertEqual(2, review.main([mode, "/tmp/source", "--until-exclusive", CUTOFF]))

    def test_native_rejects_manifest_bound_mismatch_before_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            argv = self.inputs(Path(directory), until="2026-10-22T04:00:00Z")
            with mock.patch.object(review, "_run", side_effect=AssertionError("external work")):
                self.assertEqual(2, review.main(argv))

    def test_native_forwards_exact_bound_after_matching_manifest_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            argv = self.inputs(Path(directory))
            commands = []

            def external(command):
                commands.append(command)
                return subprocess.CompletedProcess(command, 2, "", "fixture stop")

            with mock.patch.object(review, "_run", side_effect=external):
                self.assertEqual(2, review.main(argv))
            self.assertEqual(1, len(commands))
            self.assertIn("clockify_sync_collect.py", commands[0][1])
            self.assertEqual(CUTOFF, commands[0][commands[0].index("--until-exclusive") + 1])
            self.assertNotIn("--until", commands[0])

    def test_native_blocks_crossing_proposal_without_clipping_or_reconciling(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            argv = self.inputs(root)
            args = review.parse_args(argv + ["--state", str(root / "private-state.json")])
            proposal = {"id": "p", "start": "2026-10-22T05:55:00+03:00",
                        "end": "2026-10-22T06:01:00+03:00"}
            (root / "run-report.json").write_text(json.dumps({"run_id": root.name,
                "requested_until_exclusive": CUTOFF}))
            (root / "proposals.json").write_text(json.dumps([proposal]))
            commands = []

            def accounting_only(command):
                commands.append(command)
                if "work_accounting_pipeline.py" not in command[1]:
                    raise AssertionError("crossing output must not reconcile or seal")
                return subprocess.CompletedProcess(command, 0, "", "")

            with mock.patch.object(review, "_run", side_effect=accounting_only):
                code, result_path = review._process_run(args, root, {})
            self.assertEqual(2, code)
            self.assertEqual("blocked", json.loads(result_path.read_text())["quality_status"])
            self.assertEqual([proposal], json.loads((root / "proposals.json").read_text()))
            self.assertFalse((root / "completion-bundle.json").exists())
            self.assertEqual(1, len(commands))

    def test_guard_accepts_equal_instants_in_different_offsets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.inputs(root)
            (root / "run-report.json").write_text(json.dumps({"requested_until_exclusive": CUTOFF}))
            (root / "proposals.json").write_text(json.dumps([{
                "start": "2026-10-22T05:55:00+03:00", "end": "2026-10-22T06:00:00+03:00"}]))
            review._validate_exclusive_proposal_bound(root, root / "period-manifest.json")

    def test_guard_does_not_change_historical_unmarked_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "run-report.json").write_text("{}")
            review._validate_exclusive_proposal_bound(root, root / "nonexistent-old-manifest.json")


if __name__ == "__main__":
    unittest.main()
