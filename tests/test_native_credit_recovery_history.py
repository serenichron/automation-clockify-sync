"""Source promotion must archive and retire its frozen native-credit selection."""
import copy
import datetime as dt
import json
from pathlib import Path
import unittest
from unittest import mock

import test_additive_recovery_promotion as recovery_fixtures
from scripts import clockify_review_cycle as cycle, source_coverage


fixtures = recovery_fixtures.fixtures


class NativeCreditRecoveryHistoryTests(unittest.TestCase):
    def setUp(self):
        recovery_fixtures.RecoveryPromotionHistoryTests.setUp(self)

    def publisher_child_result(self, command, *, code=0):
        return fixtures.publisher_result_for_command(self.config, command, code=code)

    def recovery_with_frozen_selection(self):
        child = fixtures.ReviewCycleDeliveryTests.child_for_runs(self, [])
        with mock.patch.object(cycle, "run_child_bounded", side_effect=child):
            cycle.run_cycle(self.config, enable_sheet_write=True, today=dt.date(2026, 9, 10))
        state_path = self.state_dir / "review-cycle-state.json"
        state = json.loads(state_path.read_bytes())
        record = state["slices"]["2026-09-07"]
        parent = record["source"]
        packet_path = self.state_dir / "native-credit-snapshots" / "original-selection.json"
        fixtures.write_json(packet_path, {"schema_version": "clockify-native-credit-input/v1", "posted": []})
        record["native_credit_snapshot"] = {
            "source_digest": cycle._value_digest(parent),
            "input_handle": {"path": str(packet_path), "sha256": cycle._digest(packet_path)},
            "routing_debt": [],
        }
        interval = cycle._interval_from_stage(self.config, "peer/desktop", parent)
        store = source_coverage.SourceDebtStore()
        debt = store.record_failure(interval, failure_class="peer_unavailable", retryable=True,
            resume_state_digest="sha256:" + "a" * 64, attempted_at="2026-09-09T00:00:00Z")
        attempt, _command = cycle._recovery_attempt(record, debt, parent, self.config)
        with mock.patch.object(fixtures.collector_slices.BacklogStore, "record_complete", return_value=None):
            recovered = fixtures.make_run(self.root, "recovered-source", replay=False,
                snapshots_from=Path(parent["run_dir"]))
        stage = cycle._validate_stage(self.config, recovered, "2026-09-07", "2026-09-09",
            replay=False, expected_snapshot_digests=parent["snapshot_digests"])
        stage.update(recovery_receipt_path=str(self.root / "recovery-receipt.json"),
            recovery_receipt_digest="sha256:" + "b" * 64)
        attempt = {**attempt, "phase": "verified_complete", "result_path": stage["result_path"],
            "result_digest": stage["result_digest"], "returned_bundle_digest": stage["bundle_digest"],
            "requested_source_outcome": "complete", "recovery_receipt_path": stage["recovery_receipt_path"],
            "recovery_receipt_digest": stage["recovery_receipt_digest"]}
        record["recovery_attempts"][debt.debt_id] = attempt
        return state_path, state, record, parent, store, debt, attempt, stage

    def promote(self, state_path, state, record, parent, store, debt, attempt, stage):
        # Only the external recovery receipt/transport boundary is substituted.
        # Source, replay, publication and history authentication remain real.
        with mock.patch.object(cycle, "_validate_recovery_stage", return_value=(stage, "complete")):
            return cycle._apply_verified_recovery(self.config, state, state_path, record, store,
                self.state_dir / "source-coverage.json", "2026-09-07", "2026-09-09",
                debt, parent, attempt)

    def test_promotion_archives_frozen_selection_and_clears_it_from_new_source(self):
        arguments = self.recovery_with_frozen_selection()
        state_path, _state, record, parent, *_rest = arguments
        original = copy.deepcopy(record["native_credit_snapshot"])
        packet = Path(original["input_handle"]["path"])
        packet_bytes = packet.read_bytes()
        self.promote(*arguments)
        self.assertNotEqual(parent["run_id"], record["source"]["run_id"])
        history = record["source_recovery_history"][-1]
        self.assertEqual(original, history.get("native_credit_snapshot"),
            "promotion lost the original source's frozen native-credit selection")
        self.assertNotIn("native_credit_snapshot", record,
            "replacement source inherited a selection bound to the superseded source")
        persisted = json.loads(state_path.read_bytes())["slices"]["2026-09-07"]
        self.assertEqual(original, persisted["source_recovery_history"][-1]["native_credit_snapshot"])
        self.assertNotIn("native_credit_snapshot", persisted)
        self.assertEqual(packet_bytes, packet.read_bytes())

    def test_promotion_rejects_snapshot_bound_to_another_original_source(self):
        arguments = self.recovery_with_frozen_selection()
        _state_path, _state, record, *_rest = arguments
        record["native_credit_snapshot"]["source_digest"] = "sha256:" + "f" * 64
        before = copy.deepcopy(record)
        with self.assertRaises(cycle.CycleError):
            self.promote(*arguments)
        self.assertEqual(before, record)


if __name__ == "__main__":
    unittest.main()
