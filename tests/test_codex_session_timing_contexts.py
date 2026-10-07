"""Codex outcomes may share bounded human timing, never unattended runtime."""
import json
import unittest

from scripts import evidence_ledger, work_accounting_pipeline as pipeline
import test_work_accounting_pipeline as fixtures


def codex_event(timestamp, role="user", *, session="synthetic-codex", machine="macbook",
                content="Reviewed synthetic routing results", kind="message", tool_name=None):
    return evidence_ledger._session_event(
        "codex_sessions",
        {"session_id": session, "start": "2026-09-24T00:00:00+03:00",
         "end": "2026-09-26T23:59:00+03:00", "path": "/tmp/synthetic-session.jsonl",
         "cwd": "/tmp/automation-clockify-sync"},
        {"timestamp": timestamp, "role": role, "kind": kind, "content": content,
         "tool_name": tool_name}, machine, int(timestamp[11:13]) * 60 + int(timestamp[14:16]),
    )


class CodexSessionTimingContextTests(unittest.TestCase):
    def make_run(self, events, groups, minutes=10, omitted=()):
        analysis = fixtures.analysis_for([e.evidence_id for e in groups[0]], recommended=minutes)
        for index, group in enumerate(groups[1:], 2):
            activity = fixtures.analysis_for([e.evidence_id for e in group], recommended=minutes)["activities"][0]
            activity["object"] = f"Synthetic routing outcome {index}"
            analysis["activities"].append(activity)
        analysis["omissions"] = [{"lifecycle": "noise", "reason": "Context anchor only",
                                 "evidence_ids": [e.evidence_id]} for e in omitted]
        run_dir, result = fixtures.WorkAccountingPipelineTests.make_run(self, events, analysis)
        self.saved_analysis = json.loads((run_dir / "semantic-analysis.json").read_text())
        return result

    def test_paired_atomic_outcomes_share_human_context_with_estimated_placement(self):
        first = codex_event("2026-09-25T09:00:00+03:00")
        first_result = codex_event("2026-09-25T09:00:00+03:00", "assistant")
        last = codex_event("2026-09-25T09:20:00+03:00")
        last_result = codex_event("2026-09-25T09:20:00+03:00", "assistant")
        result = self.make_run([first, first_result, last, last_result],
                               [(first, first_result), (last, last_result)])
        self.assertEqual(2, len(result["proposals"]))
        self.assertEqual(20, sum(row["duration_minutes"] for row in result["proposals"]))
        self.assertEqual(2, result["semantic_analysis"]["activity_count"])
        for row in result["proposals"]:
            self.assertEqual("estimated", row["provenance"]["timing_placement"])
            self.assertEqual({first.evidence_id, last.evidence_id},
                             set(row["provenance"]["timing_context_evidence_ids"]))
            self.assertIn("estimated_session_placement", [w["type"] for w in row["review_warnings"]])
            self.assertGreaterEqual(row["start"], "2026-09-25T09:00:00+03:00")
            self.assertLessEqual(row["end"], "2026-09-25T09:20:00+03:00")

    def test_shared_pool_does_not_multiply_effort_and_residual_work_stays_visible(self):
        first = codex_event("2026-09-25T09:00:00+03:00")
        first_result = codex_event("2026-09-25T09:00:00+03:00", "assistant")
        last = codex_event("2026-09-25T09:20:00+03:00")
        last_result = codex_event("2026-09-25T09:20:00+03:00", "assistant")
        result = self.make_run([first, first_result, last, last_result],
                               [(first, first_result), (last, last_result)], minutes=15)
        self.assertEqual(15, sum(row["duration_minutes"] for row in result["proposals"]))
        self.assertEqual([], result["allocation"]["capacity_recoveries"])
        self.assertEqual([15, 15], [a["effort"]["recommended_minutes"]
                                   for a in self.saved_analysis["activities"]])
        residual = next(row for row in result["ambiguous"] if row["exception_kind"] == "contested_time")
        self.assertEqual(15, residual["unallocated_minutes"])
        self.assertEqual("estimated", residual["timing_placement"])

    def test_user_only_unpaired_and_tool_results_cannot_borrow_human_context(self):
        first = codex_event("2026-09-25T09:00:00+03:00")
        last = codex_event("2026-09-25T09:20:00+03:00")
        for extra in (None,
                      codex_event("2026-09-25T09:00:00+03:00", "assistant", session="other-session"),
                      codex_event("2026-09-25T09:00:00+03:00", "assistant", machine="other-machine"),
                      codex_event("2026-09-25T09:00:00+03:00", "assistant", tool_name="synthetic")):
            with self.subTest(extra=extra.source_ref if extra else None):
                group = (first, extra) if extra else (first,)
                result = self.make_run([first, last, *([extra] if extra else [])], [group], omitted=(last,))
                self.assertEqual([], result["proposals"])
                self.assertTrue(any(a["exception_kind"] == "timing_evidence" for a in result["ambiguous"]))

    def test_assistant_tool_and_automated_wrappers_cannot_extend_pool(self):
        first = codex_event("2026-09-25T09:00:00+03:00")
        last = codex_event("2026-09-25T09:20:00+03:00")
        for extra in (
            codex_event("2026-09-25T09:40:00+03:00", "assistant"),
            codex_event("2026-09-25T09:40:00+03:00", "tool", kind="tool", tool_name="synthetic"),
            codex_event("2026-09-25T09:40:00+03:00", content="<codex_delegation>Internal task</codex_delegation>"),
            codex_event("2026-09-25T09:40:00+03:00", content="<subagent_notification>Internal result</subagent_notification>"),
        ):
            with self.subTest(role=extra.attributes["role"], content=extra.attributes["content"]):
                pools = pipeline._session_timing_contexts([e.document() for e in (first, last, extra)])
                self.assertEqual({first.evidence_id, last.evidence_id}, set(pools))
                self.assertEqual({"start": "2026-09-25T09:00:00+03:00", "end": "2026-09-25T09:20:00+03:00"},
                                 pools[first.evidence_id]["interval"])

    def test_idle_day_machine_and_session_boundaries_do_not_create_capacity(self):
        first = codex_event("2026-09-25T23:40:00+03:00")
        completed = codex_event("2026-09-25T23:40:00+03:00", "assistant")
        for other in (codex_event("2026-09-25T23:09:00+03:00"),
                      codex_event("2026-09-26T00:01:00+03:00"),
                      codex_event("2026-09-25T23:50:00+03:00", machine="other-machine"),
                      codex_event("2026-09-25T23:50:00+03:00", session="other-session")):
            with self.subTest(source=other.source_ref):
                result = self.make_run([first, completed, other], [(first, completed)], omitted=(other,))
                self.assertEqual([], result["proposals"])

    def test_separate_contexts_share_global_union_not_separate_full_budgets(self):
        events, groups = [], []
        for session in ("synthetic-one", "synthetic-two"):
            first = codex_event("2026-09-25T09:00:00+03:00", session=session)
            completed = codex_event("2026-09-25T09:00:00+03:00", "assistant", session=session)
            last = codex_event("2026-09-25T09:20:00+03:00", session=session)
            events.extend((first, completed, last))
            groups.append((first, completed))
        result = self.make_run(events, groups, minutes=15, omitted=(events[2], events[5]))
        self.assertEqual(15, sum(row["duration_minutes"] for row in result["proposals"]))
        self.assertEqual([], result["allocation"]["capacity_recoveries"])
        self.assertEqual(15, sum(row["unallocated_minutes"] for row in result["allocation"]["contested_time"]))

    def test_seconds_separated_codex_outcome_uses_bounded_estimated_human_pool(self):
        first = codex_event("2026-09-25T09:00:00+03:00")
        last = codex_event("2026-09-25T09:20:00+03:00")
        for timestamp in ("2026-09-25T09:00:20+03:00", "2026-09-25T09:02:00+03:00"):
            with self.subTest(timestamp=timestamp):
                completed = codex_event(timestamp, "assistant")
                result = self.make_run([first, completed, last], [(first, completed)], omitted=(last,))
                self.assertEqual(10, sum(row["duration_minutes"] for row in result["proposals"]))
                self.assertEqual("estimated", result["proposals"][0]["provenance"]["timing_placement"])
                self.assertEqual(10, self.saved_analysis["activities"][0]["effort"]["recommended_minutes"])

    def test_direct_and_borrowed_outcomes_cannot_double_spend_shared_pool(self):
        first = codex_event("2026-09-25T09:00:00+03:00")
        direct_result = codex_event("2026-09-25T09:10:00+03:00", "assistant")
        last = codex_event("2026-09-25T09:20:00+03:00")
        atomic_result = codex_event("2026-09-25T09:20:00+03:00", "assistant")
        result = self.make_run([first, direct_result, last, atomic_result],
                               [(first, direct_result), (last, atomic_result)])
        self.assertEqual(20, sum(row["duration_minutes"] for row in result["proposals"]))
        self.assertEqual([], result["allocation"]["capacity_recoveries"])
        self.assertEqual(2, result["semantic_analysis"]["activity_count"])


if __name__ == "__main__":
    unittest.main()
