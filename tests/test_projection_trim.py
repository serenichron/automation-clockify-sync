import json
import unittest
from unittest import mock

from scripts import semantic_analyzer as semantic
from scripts import work_accounting_pipeline as pipeline


def message(evidence_id, content, role="user"):
    return {
        "evidence_id": evidence_id,
        "source_type": "claude_bursts_event",
        "role": role,
        "observed_start": "2026-10-20 10:00",
        "observed_end": "2026-10-20 10:05",
        "attributes": {"kind": "message", "role": role, "content": content},
    }


def words(prefix, count):
    return " ".join(f"{prefix}{index}" for index in range(count))


class ProjectionTrimTests(unittest.TestCase):
    # Catches pasted bodies dominating request bytes and diffs/code sent as prose.

    def test_unmarked_projection_is_unchanged(self):
        body = words("w", 20_000)
        projected = semantic.project_event(message("ev-1", body))
        self.assertEqual(body, projected["content"])

    def test_large_user_body_keeps_head_and_tail_excerpt_with_marker(self):
        body = f"Please fix the invoice export {words('pasted', 20_000)} then confirm totals"
        [marked] = semantic.with_projection_contract([message("ev-1", body)])
        content = semantic.project_event(marked)["content"]
        self.assertTrue(content.startswith("Please fix the invoice export"))
        self.assertTrue(content.endswith("then confirm totals"))
        self.assertRegex(content, r"\[elided \d+ characters\]")
        limit = semantic.PROJECTION_EXCERPT_HEAD_CHARS + semantic.PROJECTION_EXCERPT_TAIL_CHARS + 64
        self.assertLessEqual(len(content), limit)

    def test_short_user_and_long_assistant_prose_are_not_capped(self):
        short = "Fix the export totals"
        long_assistant = words("done", 5_000)
        events = semantic.with_projection_contract(
            [message("ev-1", short), message("ev-2", long_assistant, role="assistant")]
        )
        self.assertEqual(short, semantic.project_event(events[0])["content"])
        self.assertEqual(long_assistant, semantic.project_event(events[1])["content"])

    def test_diff_hunks_and_fenced_blocks_are_elided_for_any_role(self):
        text = "\n".join([
            "Updated the parser and added a regression test.",
            "diff --git a/x.py b/x.py",
            "--- a/x.py",
            "+++ b/x.py",
            "@@ -1,2 +1,2 @@",
            "-old = 1",
            "+new = 2",
            " context",
            "Then I ran the checks.",
            "```",
            "OK 12 tests",
            "```",
            "All green.",
        ])
        [marked] = semantic.with_projection_contract([message("ev-1", text, role="assistant")])
        content = semantic.project_event(marked)["content"]
        self.assertIn("Updated the parser and added a regression test.", content)
        self.assertIn("[diff elided: 7 lines]", content)
        self.assertIn("Then I ran the checks.", content)
        self.assertIn("[code block elided: 3 lines]", content)
        self.assertIn("All green.", content)
        for leaked in ("old = 1", "new = 2", "OK 12 tests"):
            self.assertNotIn(leaked, content)

    def test_request_body_declares_contract_only_when_marked(self):
        events = [message("ev-1", "Fix the export totals")]
        plain = json.loads(semantic._body_for(events, model="m", mode="extract",
                                              private_text_approved=True)["messages"][1]["content"])
        self.assertNotIn("projection_contract", plain)
        marked = json.loads(semantic._body_for(semantic.with_projection_contract(events), model="m",
                                               mode="extract", private_text_approved=True)["messages"][1]["content"])
        self.assertEqual(semantic.PROJECTION_CONTRACT, marked["projection_contract"])

    def test_mixed_projection_contracts_fail_closed(self):
        events = [message("ev-1", "a"), *semantic.with_projection_contract([message("ev-2", "b")])]
        with self.assertRaises(semantic.AnalyzerError):
            semantic._body_for(events, model="m", mode="extract", private_text_approved=True)


class PipelineProjectionContractTests(unittest.TestCase):
    taxonomy = [{"project_name": "Serenichron Level 2", "prefix": "SC", "tag_names": ["Processes"]}]

    def run_ledger(self, routing):
        seen = {}

        def analyze(events, **_kwargs):
            seen["events"] = list(events)
            return {"activities": [], "exceptions": [], "omissions": [], "analyzer_cache": {}}

        endpoint = semantic.AnalyzerEndpoint("p", "http://fixture", "m")
        with mock.patch.object(semantic.AnalyzerEndpoint, "from_env",
                               side_effect=lambda prefix, **_: endpoint if prefix.endswith("PRIMARY") else None), \
             mock.patch.object(semantic, "probe_endpoint", return_value={}), \
             mock.patch.object(semantic, "analyze_tiered", side_effect=analyze):
            pipeline.analyze_ledger([message("ev-1", "Fix totals")], review_taxonomy=self.taxonomy,
                                    review_routing=routing)
        return seen["events"]

    def test_routing_opt_in_marks_events(self):
        events = self.run_ledger({"session_routes": [], "meeting_routes": [],
                                  "semantic_projection_contract": semantic.PROJECTION_CONTRACT})
        self.assertEqual({semantic.PROJECTION_CONTRACT},
                         {event.get("semantic_projection_contract") for event in events})

    def test_routing_without_opt_in_leaves_events_unmarked(self):
        events = self.run_ledger({"session_routes": [], "meeting_routes": []})
        self.assertEqual({None}, {event.get("semantic_projection_contract") for event in events})

    def test_unknown_projection_contract_is_rejected(self):
        with self.assertRaises(pipeline.WorkAccountingError):
            self.run_ledger({"session_routes": [], "meeting_routes": [],
                             "semantic_projection_contract": "clockify-semantic-projection/v9"})


if __name__ == "__main__":
    unittest.main()
