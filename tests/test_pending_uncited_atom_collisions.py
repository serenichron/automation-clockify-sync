"""Uncited tool placeholders cannot veto immutable native selected credits."""
import copy
import datetime as dt
import unittest

from scripts import clockify_pending_review_selection as consumer
from scripts import clockify_source_adoptions as adoptions
from scripts import evidence_ledger, work_accounting_pipeline as pipeline


class NativeSharedResidualTests(unittest.TestCase):
    def fixture(self):
        events = []
        for ordinal, role, timestamp, content in (
                (1, "user", "2026-09-26T12:16:20.439000+03:00", "Reconcile the native worktree."),
                (2, "assistant", "2026-09-26T12:20:00+03:00", "Verified the first bounded result."),
                (3, "assistant", "2026-09-26T12:30:00+03:00", "Verified the second bounded result."),
                (4, "user", "2026-09-26T12:37:31.902000+03:00", "Continue with the next task.")):
            events.append(evidence_ledger.evidence_event(
                "codex_sessions_event",
                {"source_type": "codex_sessions", "machine": "precision", "session_id": "shared-session",
                 "source_id": f"shared-session:event:{ordinal}", "ordinal": ordinal},
                observed_at=timestamp, raw_source_span={"timestamp": timestamp},
                attributes={"role": role, "kind": "message", "content": content}))
        native_context = [{"start": "2026-09-26T12:16:20.439000+03:00",
                           "end": "2026-09-26T12:37:31.902000+03:00"}]
        human_ids = sorted([events[0].evidence_id, events[3].evidence_id])
        proposals, demands = [], []
        for number, start, end, effort in ((1, "2026-09-26T12:17:00+03:00", "2026-09-26T12:27:00+03:00", 10),
                                            (2, "2026-09-26T12:27:00+03:00", "2026-09-26T12:37:00+03:00", 12)):
            proposal = pipeline._proposal(
                {"activity_id": f"shared-{number}", "workstream_id": "shared-stream", "effort": {
                    "min": 6, "recommended": effort, "max": 20}},
                {"project_name": "Serenichron", "project_suffix": "abc123", "billable": False},
                f"SC — Verified bounded result {number}.", dt.datetime.fromisoformat(start),
                dt.datetime.fromisoformat(end), [events[number].evidence_id], 1)
            proposal["provenance"].update(timing_placement="estimated", timing_context_intervals=copy.deepcopy(native_context),
                                          timing_context_evidence_ids=human_ids)
            proposals.append(proposal)
            demands.append({"activity_id": proposal["activity_id"], "workstream_id": "shared-stream",
                            "evidence_spans": [], "effort": {"min": 6, "recommended": effort, "max": 20},
                            "allowed_intervals": [["2026-09-26T12:16:20+03:00", "2026-09-26T12:37:31+03:00"]]})
        ledger = evidence_ledger.EvidenceLedger(tuple(events), timezone="Europe/Bucharest")
        source = {"ledger": {"manifest": ledger.manifest.document(), "events": [e.document() for e in events]},
                  "accounting": {"proposals": copy.deepcopy(proposals), "allocation": {"evidence": demands,
                      "capacity_recoveries": [], "contested_time": [{"activity_id": "shared-2",
                      "requested_minutes": 12, "allocated_minutes": 10, "unallocated_minutes": 2}]}}}
        selected = [{"proposal": prop, "source": source,
                     "atoms": {consumer._atom(e) for e in adoptions._source_events(prop, source["ledger"])}}
                    for prop in proposals]
        return selected, source

    def test_authentic_shared_pool_retains_contested_effort_without_recovering_or_crediting_it(self):
        selected, source = self.fixture()
        before = copy.deepcopy(source)
        try:
            normalized, receipt = consumer._credits(selected, {"current": source})
        except ValueError as error:
            self.fail(f"authentic native shared-pool contested residual must remain unallocated: {error}")
        self.assertEqual([600, 600], [p["duration_seconds"] for p in normalized])
        self.assertEqual([False, False], [p["billable"] for p in normalized])
        self.assertEqual(20, receipt["saved_credit_minutes"])
        self.assertEqual(2, receipt["native_residual_minutes"])
        self.assertEqual(0, receipt["remaining_recoverable_minutes"])
        self.assertEqual(2, receipt["native_credit_checks"][1]["native_residual_minutes"])
        self.assertEqual(0, receipt["native_credit_checks"][1]["recoverable_minutes"])
        self.assertEqual(21, receipt["shared_pool_debits"][0]["capacity_minutes"])
        self.assertEqual(20, receipt["shared_pool_debits"][0]["debited_minutes"])
        self.assertEqual(before, source)

    def test_forged_shared_context_cannot_bypass_residual_gate(self):
        for mutation in ("interval", "human_ids", "demand", "missing-proof"):
            with self.subTest(mutation=mutation):
                selected, source = self.fixture()
                proposal = selected[1]["proposal"]
                if mutation == "interval":
                    proposal["provenance"]["timing_context_intervals"][0]["start"] = "2026-09-26T12:15:00+03:00"
                elif mutation == "human_ids":
                    proposal["provenance"]["timing_context_evidence_ids"] = proposal["provenance"]["evidence_ids"]
                elif mutation == "demand":
                    source["accounting"]["allocation"]["evidence"][1]["allowed_intervals"][0][0] = "2026-09-26T12:15:00+03:00"
                else:
                    proposal["provenance"].pop("timing_context_intervals")
                with self.assertRaisesRegex(ValueError, "saved native placement context differs"):
                    consumer._credits(selected, {"current": source})

    def test_authentic_shared_pool_still_rejects_overspend(self):
        selected, source = self.fixture()
        first = selected[0]["proposal"]
        first.update(start="2026-09-26T12:16:20+03:00", end="2026-09-26T12:28:20+03:00",
                     duration_minutes=12, duration_seconds=720)
        source["accounting"]["allocation"]["evidence"][0]["effort"]["recommended"] = 12
        with self.assertRaisesRegex(ValueError, "shared human-pool debit exceeds native capacity"):
            consumer._credits(selected, {"current": source})

    def test_observed_placement_with_recoverable_effort_still_fails(self):
        item, source = UncitedAtomCollisionTests().fixture()
        source["accounting"]["allocation"]["evidence"][0]["effort"]["recommended"] = 6
        source["accounting"]["allocation"]["evidence"][0]["effort"]["max"] = 6
        source["accounting"]["allocation"]["evidence"][0]["allowed_intervals"][0]["end"] = "2026-09-25T10:06:00+00:00"
        with self.assertRaisesRegex(ValueError, "leaves recoverable whole-minute capacity"):
            consumer._credits([item], {"current": source})


class UncitedAtomCollisionTests(unittest.TestCase):
    def fixture(self):
        start = dt.datetime(2026, 9, 25, 10, tzinfo=dt.timezone.utc)
        result = evidence_ledger.evidence_event(
            "codex_sessions_event",
            {"source_type": "codex_sessions", "machine": "precision", "session_id": "selected-session",
             "source_id": "selected-session:event:10", "ordinal": 10},
            observed_at=start.isoformat(), raw_source_span={"timestamp": start.isoformat()},
            attributes={"role": "assistant", "kind": "message", "content": "Verified the worktree state."})
        proposal = pipeline._proposal(
            {"activity_id": "selected-native-activity", "workstream_id": "native-stream",
             "semantic_confidence": "high", "effort": {"min": 4, "recommended": 4, "max": 4}},
            {"project_name": "Serenichron", "project_suffix": "abc123", "billable": False,
             "tag_names": ["System development"], "tag_suffixes": ["def456"]},
            "SC — Verified the worktree state.", start, start + dt.timedelta(minutes=4), [result.evidence_id], 1)
        ledger = evidence_ledger.EvidenceLedger((result,), timezone="UTC")
        source = {"ledger": {"manifest": ledger.manifest.document(), "events": [result.document()]},
                  "accounting": {"proposals": [copy.deepcopy(proposal)], "allocation": {"evidence": [{
                      "activity_id": proposal["activity_id"], "workstream_id": proposal["workstream_id"],
                      "evidence_ids": [result.evidence_id], "effort": {"min": 4, "recommended": 4, "max": 4},
                      "allowed_intervals": [{"start": proposal["start"], "end": proposal["end"]}]}]}}}
        item = {"proposal": proposal, "source": source,
                "atoms": {consumer._atom(event) for event in adoptions._source_events(proposal, source["ledger"])}}
        return item, source

    def placeholders(self):
        # Mirrors the native Sep25 minute-floor placeholder collision: same
        # session/content/time atom, distinct source ordinals and tool-use IDs.
        return [evidence_ledger.evidence_event(
            "claude_bursts_event",
            {"source_type": "claude_bursts", "machine": "macbook", "session_id": "uncited-session",
             "source_id": f"uncited-session:event:{ordinal}", "ordinal": ordinal},
            observed_at="2026-09-25 17:39", raw_source_span={"timestamp": "2026-09-25 17:39"},
            attributes={"role": "tool", "kind": "tool_result", "content": "Tool output unavailable in this capture.", "tool_name": tool})
            .document() for ordinal, tool in ((141, "toolu_first"), (142, "toolu_second"))]

    def test_uncited_tool_collision_preserves_saved_credit_and_immutable_ledgers(self):
        item, source = self.fixture()
        expected, expected_receipt = consumer._credits([item], {"current": source})
        placeholders = self.placeholders()
        self.assertEqual(consumer._atom(placeholders[0]), consumer._atom(placeholders[1]))
        self.assertNotIn(consumer._atom(placeholders[0]), item["atoms"])
        source["ledger"]["events"].extend(placeholders)
        before = copy.deepcopy(source)
        try:
            normalized, receipt = consumer._credits([item], {"current": source})
        except ValueError as error:
            self.fail(f"uncited tool metadata must not veto selected native allocation: {error}")
        self.assertEqual(expected, normalized)
        self.assertEqual(expected_receipt, receipt)
        self.assertEqual(240, normalized[0]["duration_seconds"])
        self.assertIs(False, normalized[0]["billable"])
        self.assertEqual(["def456"], normalized[0]["tag_suffixes"])
        self.assertEqual(4, receipt["saved_credit_minutes"])
        self.assertEqual(0, receipt["remaining_recoverable_minutes"])
        self.assertEqual(before, source)
        source["ledger"]["events"].reverse()
        self.assertEqual((normalized, receipt), consumer._credits([item], {"current": source}))

    def test_selected_atom_collision_still_rejects_tool_source_and_native_timestamp_drift(self):
        for mutation in ("tool_name", "source_type", "timestamp"):
            with self.subTest(mutation=mutation):
                item, source = self.fixture()
                alias = copy.deepcopy(source["ledger"]["events"][0])
                alias["evidence_id"] = "uncited-alias-of-selected-atom"
                if mutation == "tool_name":
                    alias["attributes"]["tool_name"] = "different-tool"
                elif mutation == "source_type":
                    alias["source_type"] = "claude_bursts_event"
                else:
                    alias["raw_source_span"]["timestamp"] = "2026-09-25T10:00:01Z"
                self.assertIn(consumer._atom(alias), item["atoms"])
                # Strictness follows selected atoms, not just cited event IDs,
                # and must be independent of the source iteration order.
                other = {"ledger": {"events": [alias]}}
                for sources in ({"current": source, "other": other}, {"other": other, "current": source}):
                    with self.assertRaisesRegex(ValueError, "native human timestamp/source"):
                        consumer._credits([item], sources)

    def test_uncited_collision_does_not_relax_native_capacity_or_duration(self):
        item, source = self.fixture()
        source["ledger"]["events"].extend(self.placeholders())
        item["proposal"]["end"] = "2026-09-25T10:05:00+00:00"
        item["proposal"].update(duration_minutes=5, duration_seconds=300)
        with self.assertRaisesRegex(ValueError, "original native envelope"):
            consumer._credits([item], {"current": source})

    def test_uncited_tool_collision_still_rejects_source_or_native_timestamp_drift(self):
        for mutation in ("source_type", "timestamp"):
            with self.subTest(mutation=mutation):
                item, source = self.fixture()
                placeholders = self.placeholders()
                if mutation == "source_type":
                    placeholders[1]["source_type"] = "codex_sessions_event"
                else:
                    placeholders[1]["raw_source_span"]["timestamp"] = "2026-09-25 17:40"
                source["ledger"]["events"].extend(placeholders)
                with self.assertRaisesRegex(ValueError, "native human timestamp/source"):
                    consumer._credits([item], {"current": source})


if __name__ == "__main__":
    unittest.main()
