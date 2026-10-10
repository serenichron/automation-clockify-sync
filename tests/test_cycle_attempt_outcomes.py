"""Behavioral coverage for diagnostic-only failed source attempts."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import unittest
from unittest import mock

from scripts import clockify_review_cycle as cycle, source_coverage
from scripts.autopilot_process import ChildResult
import test_review_cycle_source_debt as fixtures


class CycleAttemptOutcomeTests(unittest.TestCase):
    setUp = fixtures.ReviewCycleSourceDebtTests.setUp
    state = fixtures.ReviewCycleSourceDebtTests.state
    debts = fixtures.ReviewCycleSourceDebtTests.debts

    def config_for_slice(self):
        return {**self.config, "catchup_until": "2026-09-09", "max_slices": 1}

    def blocked_child(self, command, **_kwargs):
        path = fixtures.make_run(self.root, "blocked", replay=False, record_checkpoint=False)
        result = json.loads(path.read_text())
        result["quality_status"] = "blocked"
        fixtures.write_json(path, result)
        self.result_path = path
        return ChildResult(7, str(path) + "\n", "PRIVATE SECRET stderr", False, 0.1)

    def run_failure(self, child):
        with mock.patch.object(cycle, "run_child_bounded", side_effect=child):
            try:
                cycle.run_cycle(self.config_for_slice(), enable_sheet_write=True,
                                today=dt.date(2026, 9, 12))
            except cycle.CycleError:
                pass
        return self.state()["slices"]["2026-09-07"]

    def test_nonzero_blocked_candidate_retains_bound_identity_without_credit(self):
        """Catches dropping the only safe candidate identity on quality rejection."""
        record = self.run_failure(self.blocked_child)
        outcome = record.get("source_attempt_outcomes", [{}])[0]
        self.assertEqual(7, outcome.get("returncode"))
        self.assertFalse(outcome["timed_out"])
        self.assertEqual("child_nonzero", outcome["failure_class"])
        self.assertEqual("quality_blocked", outcome["failure_code"])
        self.assertEqual({
            "result_path": str(self.result_path),
            "result_digest": "sha256:" + hashlib.sha256(self.result_path.read_bytes()).hexdigest(),
            "run_id": "blocked", "run_dir": str(self.result_path.parent),
        }, outcome["candidate"])
        self.assertEqual(record["source_attempt"]["command_digest"], outcome["command_digest"])
        self.assertEqual(record["source_attempt"]["resume_state_digest"], outcome["resume_state_digest"])
        self.assertEqual(1, outcome["ordinal"])
        self.assertEqual("2026-09-06T21:00:00Z", outcome["interval"]["since_utc"])
        self.assertEqual("2026-09-08T21:00:00Z", outcome["interval"]["until_utc"])
        self.assertEqual({"ordinal", "command_digest", "resume_state_digest", "status", "advance_frontier"},
                         set(record["source_attempt"]))
        self.assertNotIn("source", record)
        self.assertNotIn("delivery_receipt", record)
        self.assertEqual(["runner/unclassified"], [item.interval.source for item in self.debts()])
        self.assertNotIn("PRIVATE SECRET", json.dumps(self.state()))

    def test_diagnostic_before_debt_crash_resumes_without_another_child(self):
        """Catches relaunching a completed failure when debt persistence was interrupted."""
        with mock.patch.object(cycle, "run_child_bounded", side_effect=self.blocked_child), \
             mock.patch.object(cycle.source_coverage, "write", side_effect=RuntimeError("before debt")):
            with self.assertRaisesRegex(RuntimeError, "before debt"):
                cycle.run_cycle(self.config_for_slice(), enable_sheet_write=True,
                                today=dt.date(2026, 9, 12))
        before = self.state()["slices"]["2026-09-07"].get("source_attempt_outcomes")
        self.assertIsNotNone(before)
        with mock.patch.object(cycle, "run_child_bounded",
                               side_effect=AssertionError("duplicate child")):
            cycle.run_cycle(self.config_for_slice(), enable_sheet_write=True,
                            today=dt.date(2026, 9, 12))
        record = self.state()["slices"]["2026-09-07"]
        self.assertEqual(before, record["source_attempt_outcomes"])
        self.assertEqual("finished", record["source_attempt"]["status"])
        self.assertEqual(1, self.debts()[0].retry_count)

    def test_fresh_binding_failure_resumes_before_completed_run_scan(self):
        """Catches the fresh orphan guard trapping an already durable failed child."""
        fixture = fixtures._DELIVERY.ReviewCycleDeliveryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        config, _, _ = fixture.fresh_routing_fixture()
        config = {**config, "catchup_until": "2026-09-09", "max_slices": 1}

        def child(command, **_kwargs):
            path = fixtures.make_run(fixture.root, "fresh-blocked", replay=False,
                                     runtime_identity=config["_runtime_identity"], record_checkpoint=False)
            result = json.loads(path.read_text())
            result["quality_status"] = "blocked"
            fixtures.write_json(path, result)
            return ChildResult(7, str(path) + "\n", "PRIVATE SECRET", False, 0.1)

        with mock.patch.object(cycle, "run_child_bounded", side_effect=child), \
             mock.patch.object(cycle.source_coverage, "write", side_effect=RuntimeError("before fresh debt")):
            with self.assertRaisesRegex(RuntimeError, "before fresh debt"):
                cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 10))
        state_path = fixture.state_dir / "review-cycle-state.json"
        record = json.loads(state_path.read_text())["slices"]["2026-09-07"]
        self.assertIn("fresh_input_binding", record)
        self.assertTrue(record["fresh_child_started"])
        self.assertEqual(2, record["source_attempt"]["ordinal"])
        before = record["source_attempt_outcomes"]
        with mock.patch.object(cycle, "run_child_bounded", side_effect=AssertionError("duplicate fresh child")), \
             mock.patch.object(cycle.clockify_review_run, "_adopt_completed_resume",
                               side_effect=AssertionError("scanned completed runs after known failure")):
            cycle.run_cycle(config, enable_sheet_write=True, today=dt.date(2026, 9, 10))
        record = json.loads(state_path.read_text())["slices"]["2026-09-07"]
        self.assertEqual(before, record["source_attempt_outcomes"])
        self.assertEqual("finished", record["source_attempt"]["status"])
        self.assertEqual("finished", record["runner_attempt"]["status"])
        self.assertNotIn("source", record)
        store, _ = cycle._source_debt(fixture.state_dir / "source-coverage.json")
        self.assertEqual(1, store.active()[0].retry_count)

    def test_unsafe_or_unbound_candidates_never_retain_result_identity(self):
        """Catches retaining paths that are unsafe, ambiguous, or from another slice."""
        def child(command, **_kwargs):
            path = fixtures.make_run(self.root, "unsafe", replay=False, record_checkpoint=False)
            if self.variant == "missing":
                stdout = str(path.parent / "missing" / "autopilot-result.json")
            elif self.variant == "multiple":
                stdout = str(path) + "\n" + str(path)
            elif self.variant == "outside":
                outside = self.root / "autopilot-result.json"
                outside.write_bytes(path.read_bytes())
                stdout = str(outside)
            elif self.variant == "symlink":
                alias = self.root / "runs" / "alias"
                alias.symlink_to(path.parent, target_is_directory=True)
                stdout = str(alias / path.name)
            else:
                result = json.loads(path.read_text())
                result["quality_status"] = "blocked"
                result["date_range"]["since"] = "2026-09-05T21:00:00Z"
                fixtures.write_json(path, result)
                stdout = str(path)
            return ChildResult(7, stdout + "\n", "PRIVATE SECRET", False, 0.1)

        for self.variant in ("missing", "multiple", "outside", "symlink", "wrong_interval"):
            with self.subTest(variant=self.variant):
                # Each case needs its own durable interval/attempt state.
                self.setUp()
                record = self.run_failure(child)
                self.assertIsNone(record["source_attempt_outcomes"][0]["candidate"])
                self.assertNotIn("source", record)
                self.assertEqual(1, self.debts()[0].retry_count)

    def test_timeout_candidate_never_adopted_and_two_attempts_preserved(self):
        """Catches timeout adoption or replacement of the previous failure diagnostic."""
        def child(command, **_kwargs):
            ordinal = len(self.state()["slices"]["2026-09-07"].get("source_attempt_outcomes", [])) + 1
            path = fixtures.make_run(self.root, f"timeout-{ordinal}", replay=False, record_checkpoint=False)
            return ChildResult(None, str(path) + "\n", "PRIVATE SECRET", True, 0.1)

        first = self.run_failure(child)["source_attempt_outcomes"][0]
        record = self.run_failure(child)
        self.assertEqual([1, 2], [row["ordinal"] for row in record["source_attempt_outcomes"]])
        self.assertEqual(first, record["source_attempt_outcomes"][0])
        self.assertNotEqual(first["resume_state_digest"], record["source_attempt_outcomes"][1]["resume_state_digest"])
        self.assertTrue(all(row["timed_out"] for row in record["source_attempt_outcomes"]))
        self.assertTrue(all(row["failure_code"] == "child_timeout" for row in record["source_attempt_outcomes"]))
        self.assertNotIn("source", record)
        self.assertEqual(2, self.debts()[0].retry_count)

    def test_only_bound_semantic_quality_reason_gets_allowlisted_code(self):
        """Catches deriving semantic causes from an unbound or private quality reason."""
        for binding, reason, wanted in (
            (True, "PRIVATE SECRET: contract_rejected_duplicate_evidence", "contract_rejected_duplicate_evidence"),
            (True, "PRIVATE SECRET unknown failure", "quality_blocked"),
            (False, "contract_rejected_duplicate_evidence", "quality_blocked"),
            ("escaped_quality", "contract_rejected_duplicate_evidence", "quality_blocked"),
            ("symlink_quality", "contract_rejected_duplicate_evidence", "quality_blocked"),
            ("different_bundle", "contract_rejected_duplicate_evidence", "quality_blocked"),
            ("malformed_quality", "contract_rejected_duplicate_evidence", "quality_blocked"),
        ):
            with self.subTest(binding=binding, wanted=wanted):
                self.setUp()

                def child(command, **_kwargs):
                    returned = self.blocked_child(command, **_kwargs)
                    path = self.result_path
                    quality = {"status": "blocked", "summary": {
                        "semantic_analysis": "unavailable_or_invalid", "reason": reason,
                    }}
                    fixtures.write_json(path.parent / "quality_report.json", quality)
                    result = json.loads(path.read_text())
                    result["quality_summary"] = quality["summary"]
                    if binding == "malformed_quality":
                        fixtures.write_json(path.parent / "quality_report.json", [])
                    if binding:
                        local = cycle.ZoneInfo("Europe/Bucharest")
                        slice_ = fixtures.collector_slices.plan_slices(
                            dt.datetime(2026, 9, 7, tzinfo=local),
                            dt.datetime(2026, 9, 9, tzinfo=local), zone=local, max_days=2,
                        )[0]
                        bundle = fixtures.collector_receipts.build_completion_bundle(path.parent, slice_=slice_)
                        fixtures.collector_receipts.write_completion_bundle(path.parent / "completion-bundle.json", bundle)
                        result.update(completion_bundle=bundle.document(), completion_bundle_digest=bundle.bundle_digest)
                    if binding == "escaped_quality":
                        outside = self.root / "quality_report.json"
                        fixtures.write_json(outside, quality)
                        result["paths"]["quality_report"] = str(outside)
                    elif binding == "symlink_quality":
                        alias = path.parent / "quality-alias.json"
                        alias.symlink_to(path.parent / "quality_report.json")
                        result["paths"]["quality_report"] = str(alias)
                    elif binding == "different_bundle":
                        result["completion_bundle_digest"] = "sha256:" + "0" * 64
                    fixtures.write_json(path, result)
                    return returned

                record = self.run_failure(child)
                self.assertEqual(wanted, record["source_attempt_outcomes"][0]["failure_code"])
                self.assertNotIn("PRIVATE SECRET", json.dumps(self.state()))
