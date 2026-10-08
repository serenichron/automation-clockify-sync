"""Codex result citations may use existing human pools, never create capacity."""
from __future__ import annotations

import copy
import unittest

from scripts import evidence_ledger
from scripts import work_accounting_pipeline as pipeline
import test_work_accounting_pipeline as fixtures


def codex_event(identity, timestamp, *, role="user", machine="macbook",
                session="pool-session", source_type="codex_sessions_event",
                source_ref_type="codex_sessions", kind="message", tool_name=None,
                content="Completed bounded fixture work."):
    attributes = {"role": role, "kind": kind, "content": content}
    if tool_name is not None:
        attributes["tool_name"] = tool_name
    return evidence_ledger.evidence_event(
        source_type,
        {"source_type": source_ref_type, "source_id": identity,
         "machine": machine, "session_id": session},
        observed_at=timestamp, raw_source_span={"timestamp": timestamp},
        attributes=attributes,
    )


class CodexResultPoolIndexTests(unittest.TestCase):
    def make_run(self, events, analysis):
        return fixtures.WorkAccountingPipelineTests.make_run(self, events, analysis)

    def pool_events(self):
        return [codex_event("human-first", "2026-07-10T09:00:00+03:00"),
                codex_event("human-last", "2026-07-10T09:15:00+03:00")]

    def result_events(self):
        return [codex_event(f"result-{minute}", f"2026-07-10T09:{minute:02d}:00+03:00", role="assistant")
                for minute in (3, 7, 11)]

    def test_three_result_points_index_one_existing_human_pool_without_widening_it(self):
        """Missing assistant-ID lookup must not discard a validated human pool."""
        humans, results = self.pool_events(), self.result_events()
        documents = [event.document() for event in humans + results]
        before = copy.deepcopy(documents)
        contexts = pipeline._session_timing_contexts(iter(documents))
        human_ids = sorted(event.evidence_id for event in humans)
        for result in results:
            self.assertIn(result.evidence_id, contexts)
            context = contexts[result.evidence_id]
            self.assertEqual({"start": "2026-07-10T09:00:00+03:00",
                              "end": "2026-07-10T09:15:00+03:00"}, context["interval"])
            self.assertEqual(human_ids, context["evidence_ids"])
            self.assertIs(contexts[humans[0].evidence_id], context)
            self.assertEqual([], pipeline._activity_observed_intervals([result.document()]))
        self.assertEqual(before, documents)

    def test_outside_or_unrelated_results_cannot_borrow_existing_pool(self):
        """Lookup must preserve exact bounds, source, machine and session fences."""
        humans = self.pool_events()
        variants = [
            ("before", "2026-07-10T08:59:59+03:00", {}),
            ("after", "2026-07-10T09:15:01+03:00", {}),
            ("machine", "2026-07-10T09:07:00+03:00", {"machine": "other-machine"}),
            ("session", "2026-07-10T09:07:00+03:00", {"session": "other-session"}),
            ("source", "2026-07-10T09:07:00+03:00", {"source_type": "claude_bursts_event", "source_ref_type": "claude_bursts"}),
            ("source-ref", "2026-07-10T09:07:00+03:00", {"source_ref_type": "other-source"}),
            ("tool", "2026-07-10T09:07:00+03:00", {"tool_name": "exec"}),
            ("nonmessage", "2026-07-10T09:07:00+03:00", {"kind": "tool_result"}),
            ("empty", "2026-07-10T09:07:00+03:00", {"content": " "}),
            ("injected", "2026-07-10T09:07:00+03:00", {"content": "<subagent_notification>Internal fixture result.</subagent_notification>"}),
        ]
        for name, timestamp, changes in variants:
            with self.subTest(name=name):
                result = codex_event(name, timestamp, role="assistant", **changes)
                contexts = pipeline._session_timing_contexts([e.document() for e in humans + [result]])
                self.assertNotIn(result.evidence_id, contexts)

    def test_assistant_points_never_create_pool_or_fill_human_idle_gap(self):
        """Assistant runtime is not a substitute for bounded human observations."""
        lone = codex_event("lone-human", "2026-07-10T09:00:00+03:00")
        late = codex_event("late-human", "2026-07-10T10:00:00+03:00")
        result = codex_event("between", "2026-07-10T09:15:00+03:00", role="assistant")
        for events in ([result], [lone, result], [lone, result, late]):
            with self.subTest(count=len(events)):
                self.assertEqual({}, pipeline._session_timing_contexts([e.document() for e in events]))

    def test_result_only_outcomes_share_fifteen_minutes_and_keep_residual_warnings(self):
        """One human pool must not supply fifteen minutes independently to each result."""
        humans, results = self.pool_events(), self.result_events()
        analysis = {"activities": [], "exceptions": [], "omissions": []}
        original_efforts = []
        for index, (result, recommended) in enumerate(zip(results, (40, 20, 20))):
            activity = fixtures.analysis_for([result.evidence_id], recommended=recommended)["activities"][0]
            activity["object"] = f"Clockify bounded fixture outcome {index}"
            analysis["activities"].append(activity)
            original_efforts.append(copy.deepcopy(activity["effort"]))
        analysis["omissions"] = [{"lifecycle": "noise", "evidence_ids": [h.evidence_id],
                                  "reason": "Independent human timing anchor, not another outcome."} for h in humans]
        run, result = self.make_run(humans + results, analysis)
        self.assertEqual(15, sum(p["duration_minutes"] for p in result["proposals"]))
        self.assertEqual([], result["allocation"]["capacity_recoveries"])
        self.assertEqual(30, sum(c["unallocated_minutes"] for c in result["allocation"]["contested_time"]))
        self.assertEqual([15, 15, 15], sorted(d["effort"]["recommended_minutes"] for d in result["allocation"]["evidence"]))
        self.assertEqual(original_efforts, [a["effort"] for a in analysis["activities"]])
        for proposal in result["proposals"]:
            self.assertEqual("estimated", proposal["provenance"]["timing_placement"])
            self.assertEqual(sorted(h.evidence_id for h in humans), proposal["provenance"]["timing_context_evidence_ids"])
            self.assertIn("estimated_session_placement", [w["type"] for w in proposal["review_warnings"]])
            self.assertIn("observed_capacity_cap", [w["type"] for w in proposal["review_warnings"]])
        self.assertEqual([], [a for a in result["ambiguous"] if a["exception_kind"] == "timing_evidence"])

    def test_occupied_human_pool_stays_contested_without_recovery_respends(self):
        """Already occupied shared capacity cannot become recovered duplicate time."""
        humans = self.pool_events()
        result_event = self.result_events()[0]
        existing = fixtures.clockify_event("2026-07-10T09:00:00+03:00", "2026-07-10T09:15:00+03:00")
        analysis = fixtures.analysis_for([result_event.evidence_id], recommended=20)
        analysis["omissions"] = [{"lifecycle": "noise", "evidence_ids": [h.evidence_id],
                                  "reason": "Human timing anchor only."} for h in humans]
        _, result = self.make_run(humans + [result_event, existing], analysis)
        self.assertEqual([], result["proposals"])
        self.assertEqual([], result["allocation"]["capacity_recoveries"])
        self.assertEqual(15, sum(c["unallocated_minutes"] for c in result["allocation"]["contested_time"]))
        self.assertEqual("Estimated outcomes share one observed human window; remaining effort is not separately timed.", result["ambiguous"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
