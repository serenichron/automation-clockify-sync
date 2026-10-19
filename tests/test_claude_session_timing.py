"""Opted-in Claude outcomes share human placement capacity, never runtime."""
import json
from pathlib import Path
import tempfile
import unittest

from scripts import evidence_ledger, work_accounting_pipeline as pipeline
import test_work_accounting_pipeline as fixtures


ENABLED = ["codex_sessions", "claude_bursts"]


def event(timestamp, role="user", *, source="claude_bursts", session="synthetic",
          machine="macbook", content="Completed synthetic routing work",
          kind="message", tool_name=None):
    return evidence_ledger.evidence_event(
        source + "_event",
        {"source_type": source, "source_id": f"{machine}:{session}:{role}:{timestamp}:{content}",
         "machine": machine, "session_id": session},
        observed_at=timestamp,
        raw_source_span={"timestamp": timestamp, "session_start": "2026-09-25T00:00:00+03:00",
                         "session_end": "2026-09-27T23:59:00+03:00"},
        attributes={"role": role, "kind": kind, "content": content, "tool_name": tool_name},
    )


class ClaudeSessionTimingTests(unittest.TestCase):
    def make_run(self, events, groups, *, sources=ENABLED, minutes=10):
        activities = []
        for index, group in enumerate(groups):
            activity = fixtures.analysis_for(
                [item.evidence_id for item in group], recommended=minutes,
            )["activities"][0]
            activity["object"] = f"Synthetic routing outcome {index}"
            activities.append(activity)
        cited = {item.evidence_id for group in groups for item in group}
        analysis = {"activities": activities, "exceptions": [], "omissions": [
            {"lifecycle": "noise", "reason": "Human placement context only",
             "evidence_ids": [item.evidence_id]}
            for item in events if item.evidence_id not in cited and item.source_type != "clockify"
        ]}
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        routing_path = Path(temporary.name) / "routing.json"
        routing = json.loads((fixtures.ROOT / "routing.json").read_text())
        if sources is not None:
            routing["session_timing_pool_sources"] = sources
        fixtures.write_json(routing_path, routing)
        run_dir, result = fixtures.WorkAccountingPipelineTests.make_run(
            self, events, analysis, routing_path=routing_path,
        )
        self.run_dir, self.routing_path = run_dir, routing_path
        return result

    def anchors(self, **kwargs):
        return [event(f"2026-09-25T09:{minute:02d}:00+03:00", **kwargs)
                for minute in (0, 20)]

    def assert_borrowed(self, result, anchors, minutes=10):
        self.assertEqual(minutes, sum(row["duration_minutes"] for row in result["proposals"]))
        for row in result["proposals"]:
            self.assertEqual("estimated", row["provenance"]["timing_placement"])
            self.assertEqual({item.evidence_id for item in anchors},
                             set(row["provenance"]["timing_context_evidence_ids"]))
            self.assertIn("estimated_session_placement", {w["type"] for w in row["review_warnings"]})
            self.assertGreaterEqual(row["start"], "2026-09-25T09:00:00+03:00")
            self.assertLessEqual(row["end"], "2026-09-25T09:20:00+03:00")
        self.assertEqual([], result["allocation"]["capacity_recoveries"])

    def test_opted_in_seconds_separated_pair_uses_human_pool(self):
        anchors = self.anchors()
        result_point = event("2026-09-25T09:00:20+03:00", "assistant")
        result = self.make_run([*anchors, result_point], [(anchors[0], result_point)])
        self.assert_borrowed(result, anchors)
        self.assertEqual(ENABLED, result["allocation"]["deterministic_inputs"]["session_timing_pool_sources"])

    def test_opted_in_single_and_multi_point_results_use_containing_pool(self):
        anchors = self.anchors()
        first = event("2026-09-25T09:03:00+03:00", "assistant")
        second = event("2026-09-25T09:03:20+03:00", "assistant")
        for group in [(first,), (first, second)]:
            with self.subTest(points=len(group)):
                self.assert_borrowed(self.make_run([*anchors, *group], [group]), anchors)

    def test_positive_claude_cited_minutes_stay_literal_despite_larger_recommended_effort(self):
        anchors = self.anchors()
        paired_result = event("2026-09-25T09:01:20+03:00", "assistant")
        first_result = event("2026-09-25T09:03:00+03:00", "assistant")
        last_result = event("2026-09-25T09:04:20+03:00", "assistant")
        for group, start, end in (
            ((anchors[0], paired_result), "2026-09-25T09:00:00+03:00", "2026-09-25T09:01:00+03:00"),
            ((first_result, last_result), "2026-09-25T09:03:00+03:00", "2026-09-25T09:04:00+03:00"),
        ):
            with self.subTest(group=[item.evidence_id for item in group]):
                events = {item.evidence_id: item for item in [*anchors, *group]}
                result = self.make_run(list(events.values()), [group], minutes=10)
                self.assertEqual(1, len(result["proposals"]))
                proposal = result["proposals"][0]
                self.assertEqual((start, end, 1),
                                 (proposal["start"], proposal["end"], proposal["duration_minutes"]))
                self.assertNotIn("timing_placement", proposal["provenance"])
                self.assertNotIn("estimated_session_placement", {w["type"] for w in proposal["review_warnings"]})
                saved = json.loads((self.run_dir / "semantic-analysis.json").read_text())
                self.assertEqual(10, saved["activities"][0]["effort"]["recommended_minutes"])

    def test_absent_option_keeps_legacy_claude_under_capacity_and_output_shape(self):
        anchors = self.anchors()
        completed = event("2026-09-25T09:00:20+03:00", "assistant")
        result = self.make_run([*anchors, completed], [(anchors[0], completed)], sources=None)
        self.assertEqual([], result["proposals"])
        self.assertEqual(["timing_evidence"], [row["exception_kind"] for row in result["ambiguous"]])
        self.assertEqual({"point_observation_clustering"}, set(result["allocation"]["deterministic_inputs"]))
        before = (self.run_dir / "work-accounting-result.json").read_bytes()
        pipeline.run_accounting(self.run_dir, root=fixtures.ROOT, routing_path=self.routing_path,
                                analysis_fixture=self.run_dir / "semantic-analysis.json")
        self.assertEqual(before, (self.run_dir / "work-accounting-result.json").read_bytes())

    def test_explicit_codex_only_does_not_enable_claude(self):
        anchors = self.anchors()
        completed = event("2026-09-25T09:00:20+03:00", "assistant")
        result = self.make_run([*anchors, completed], [(anchors[0], completed)], sources=["codex_sessions"])
        self.assertEqual([], result["proposals"])

    def test_invalid_setting_fails_before_accounting(self):
        anchors = self.anchors()
        for setting in (None, "claude_bursts", ["hermes_db_sessions"], ["claude_bursts_event"],
                        ["claude_bursts", "claude_bursts"], [17], [{}], {"claude_bursts": True}):
            # Explicit JSON null differs from an absent legacy option.
            with self.subTest(setting=setting), tempfile.TemporaryDirectory() as temporary:
                routing = json.loads((fixtures.ROOT / "routing.json").read_text())
                routing["session_timing_pool_sources"] = setting
                path = Path(temporary) / "routing.json"
                fixtures.write_json(path, routing)
                with self.assertRaisesRegex(pipeline.WorkAccountingError, "session_timing_pool_sources"):
                    fixtures.WorkAccountingPipelineTests.make_run(
                        self, anchors, fixtures.analysis_for([item.evidence_id for item in anchors]), routing_path=path,
                    )

    def test_tool_wrapper_and_user_only_citations_cannot_borrow(self):
        anchors = self.anchors()
        for extra in (None,
                      event("2026-09-25T09:00:20+03:00", "assistant", tool_name="synthetic"),
                      event("2026-09-25T09:00:20+03:00", "assistant", content="<subagent_notification>Internal result</subagent_notification>"),
                      event("2026-09-25T09:00:20+03:00", "assistant", content="<codex_delegation>Internal task</codex_delegation>")):
            with self.subTest(extra=extra.document() if extra else None):
                group = (anchors[0], extra) if extra else (anchors[0],)
                result = self.make_run([*anchors, *([extra] if extra else [])], [group])
                self.assertEqual([], result["proposals"])

    def test_aliases_never_cross_machine_session_day_idle_or_pool_bounds(self):
        anchors = self.anchors()
        for completed in (
            event("2026-09-25T09:03:00+03:00", "assistant", machine="other"),
            event("2026-09-25T09:03:00+03:00", "assistant", session="other"),
            event("2026-09-26T09:03:00+03:00", "assistant"),
            event("2026-09-25T09:20:01+03:00", "assistant"),
            event("2026-09-25T08:59:59+03:00", "assistant"),
        ):
            with self.subTest(completed=completed.document()):
                result = self.make_run([*anchors, completed], [(completed,)])
                self.assertEqual([], result["proposals"])
        for start, end, result_at in (("09:00:00", "09:31:00", "09:10:00"),
                                      ("23:50:00", "00:10:00", "23:55:00")):
            day = "26" if end == "00:10:00" else "25"
            humans = [event(f"2026-09-25T{start}+03:00"), event(f"2026-09-{day}T{end}+03:00")]
            completed = event(f"2026-09-25T{result_at}+03:00", "assistant")
            self.assertEqual([], self.make_run([*humans, completed], [(completed,)])["proposals"])

    def test_two_parallel_sessions_share_global_capacity_not_full_budgets(self):
        all_events, groups = [], []
        for session in ("one", "two"):
            anchors = self.anchors(session=session)
            completed = event("2026-09-25T09:00:20+03:00", "assistant", session=session)
            all_events.extend([*anchors, completed])
            groups.append((anchors[0], completed))
        result = self.make_run(all_events, groups, minutes=15)
        self.assertEqual(15, sum(row["duration_minutes"] for row in result["proposals"]))
        self.assertEqual(15, sum(row["unallocated_minutes"] for row in result["allocation"]["contested_time"]))
        self.assertEqual([], result["allocation"]["capacity_recoveries"])

    def test_result_only_outcomes_share_one_pool_and_preserve_requested_effort(self):
        anchors = self.anchors()
        results = [event(f"2026-09-25T09:{minute}:00+03:00", "assistant") for minute in ("03", "07")]
        result = self.make_run([*anchors, *results], [(item,) for item in results], minutes=15)
        self.assert_borrowed(result, anchors, minutes=15)
        self.assertEqual(15, sum(row["unallocated_minutes"] for row in result["allocation"]["contested_time"]))
        saved = json.loads((self.run_dir / "semantic-analysis.json").read_text())
        self.assertEqual([15, 15], [row["effort"]["recommended_minutes"] for row in saved["activities"]])
        self.assertEqual(2, len(saved["activities"]))

    def test_occupied_pool_cannot_be_recovered_as_additional_effort(self):
        anchors = self.anchors()
        completed = event("2026-09-25T09:03:00+03:00", "assistant")
        occupied = fixtures.clockify_event("2026-09-25T09:00:00+03:00", "2026-09-25T09:20:00+03:00")
        result = self.make_run([*anchors, completed, occupied], [(completed,)])
        self.assertEqual([], result["proposals"])
        self.assertEqual([], result["allocation"]["capacity_recoveries"])
        self.assertEqual(10, sum(row["unallocated_minutes"] for row in result["allocation"]["contested_time"]))

    def test_result_group_cannot_borrow_with_only_one_point_inside_pool(self):
        anchors = self.anchors()
        inside = event("2026-09-25T09:19:50+03:00", "assistant")
        outside = event("2026-09-25T09:20:10+03:00", "assistant")
        result = self.make_run([*anchors, inside, outside], [(inside, outside)])
        self.assertEqual([], result["proposals"])

    def test_parallel_result_group_cannot_combine_distinct_session_pools(self):
        humans, results = [], []
        for session, seconds in (("one", "00"), ("two", "20")):
            humans.extend(self.anchors(session=session))
            results.append(event(f"2026-09-25T09:03:{seconds}+03:00", "assistant", session=session))
        result = self.make_run([*humans, *results], [results])
        self.assertEqual([], result["proposals"])

    def test_codex_positive_result_only_span_preserves_legacy_restriction(self):
        anchors = self.anchors(source="codex_sessions")
        results = [event(f"2026-09-25T09:03:{second}+03:00", "assistant", source="codex_sessions")
                   for second in ("00", "20")]
        for sources in (None, ENABLED):
            with self.subTest(sources=sources):
                result = self.make_run([*anchors, *results], [results], sources=sources)
                self.assertEqual([], result["proposals"])


if __name__ == "__main__":
    unittest.main()
