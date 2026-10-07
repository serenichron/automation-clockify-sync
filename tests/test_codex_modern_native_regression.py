"""Synthetic regression for the modern native formats observed on October 6.

No native messages or private evidence are included in these fixtures.
"""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

from scripts import clockify_sync_collect as collector


SINCE = dt.datetime(2026, 10, 6, tzinfo=dt.timezone.utc)
UNTIL = dt.datetime(2026, 10, 7, tzinfo=dt.timezone.utc)


def message(timestamp, role, content, nested=False):
    item = {"type": "message", "role": role, "content": content}
    return {"timestamp": timestamp, "type": "response_item",
            "payload": {"item": item} if nested else item}


def event(timestamp, kind, content):
    return {"timestamp": timestamp, "type": "event_msg",
            "payload": {"type": kind, "message": content}}


class ModernNativeCodexRegressionTests(unittest.TestCase):
    def parse(self, rows):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-synthetic.jsonl"
            path.write_text("\n".join(json.dumps(row) for row in [
                {"type": "session_meta", "payload": {"id": "synthetic-modern", "cwd": "/work/synthetic"}},
                *rows,
            ]) + "\n")
            return collector.parse_codex_rollout_file(path, "synthetic", SINCE, UNTIL)

    def test_response_item_preserves_text_blocks_roles_and_milliseconds(self):
        # Dropping modern messages or flattening only the first text block loses evidence.
        bursts = self.parse([
            message("2026-10-06T08:00:13.123Z", "user", [
                {"type": "input_text", "text": "Check synthetic totals."},
                {"type": "text", "text": "Keep line two intact."},
                {"type": "input_image", "image_url": "not-text"},
            ]),
            message("2026-10-06T08:00:14.456Z", "assistant", [
                {"type": "output_text", "text": "Synthetic totals checked."},
                {"type": "Text", "text": "Second result line."},
            ], nested=True),
        ])
        self.assertEqual(1, len(bursts))
        self.assertEqual([
            ("user", "2026-10-06T11:00:13.123000+03:00", "Check synthetic totals.\nKeep line two intact."),
            ("assistant", "2026-10-06T11:00:14.456000+03:00", "Synthetic totals checked.\nSecond result line."),
        ], [(row["role"], row["timestamp"], row["content"]) for row in bursts[0]["events"]])
        self.assertEqual(1, bursts[0]["user_messages"])

    def test_uppercase_events_recover_native_user_and_assistant_messages(self):
        # Lowercase-only dispatch incorrectly reports an empty native session.
        bursts = self.parse([
            event("2026-10-06T08:00:13.123Z", "UserMessage", "Review synthetic export.\nPreserve detail."),
            event("2026-10-06T08:00:14.456Z", "AgentMessage", "Synthetic export reviewed."),
        ])
        self.assertEqual(1, len(bursts))
        self.assertEqual([
            ("user", "2026-10-06T11:00:13.123000+03:00", "Review synthetic export.\nPreserve detail."),
            ("assistant", "2026-10-06T11:00:14.456000+03:00", "Synthetic export reviewed."),
        ], [(row["role"], row["timestamp"], row["content"]) for row in bursts[0]["events"]])

    def test_paired_transports_deduplicate_one_to_one_without_erasing_repeats(self):
        # Global content dedup erases genuine repeats; no pairing double-counts one turn.
        text = "Verify synthetic totals again."
        bursts = self.parse([
            event("2026-10-06T08:00:00Z", "UserMessage", text),
            message("2026-10-06T08:00:00.123Z", "user", [{"type": "input_text", "text": text}]),
            message("2026-10-06T08:00:00.456Z", "user", [{"type": "input_text", "text": text}]),
            event("2026-10-06T08:05:00Z", "UserMessage", text),
            message("2026-10-06T08:05:00.123Z", "user", [{"type": "input_text", "text": text}]),
        ])
        self.assertEqual(1, len(bursts))
        self.assertEqual(3, bursts[0]["user_messages"])
        self.assertEqual([
            "2026-10-06T11:00:00.123000+03:00",
            "2026-10-06T11:00:00.456000+03:00",
            "2026-10-06T11:05:00.123000+03:00",
        ], [row["timestamp"] for row in bursts[0]["events"]])

    def test_nonhuman_context_and_tool_messages_do_not_create_user_bursts(self):
        # Treating every response message or role=user envelope as an anchor fabricates work.
        rows = [message("2026-10-06T08:00:00Z", role, [{"type": "input_text", "text": "Synthetic policy."}])
                for role in ("system", "developer", "tool", "assistant")]
        for content in (
            "# AGENTS.md instructions\nSynthetic policy.",
            "<environment_context>Synthetic runtime.</environment_context>",
            "<subagent_notification>Synthetic completion.</subagent_notification>",
            "<codex_internal_context>## My request:\nHistorical instruction.</codex_internal_context>",
            "<in-app-browser-context>Synthetic ambient state.</in-app-browser-context>",
        ):
            rows.append(message("2026-10-06T08:00:01Z", "user", [{"type": "input_text", "text": content}]))
            rows.append(event("2026-10-06T08:00:01Z", "UserMessage", content))
        rows.append({"timestamp": "2026-10-06T08:00:02Z", "type": "response_item", "payload": {
            "type": "function_call_output", "call_id": "synthetic-call", "output": "Synthetic tool result."}})
        self.assertEqual([], self.parse(rows))


if __name__ == "__main__":
    unittest.main(verbosity=2)
