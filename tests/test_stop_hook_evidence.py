"""Automatic hook feedback is evidence context, never a fresh human anchor."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts import clockify_sync_collect as collector
from scripts import evidence_ledger
from scripts import work_accounting_pipeline as accounting


HOOK = "Stop hook feedback: automated delivery status"
TIMES = ("2026-10-14T10:00:00+03:00", "2026-10-14T10:05:00+03:00")


def ledger_events(messages, source="claude_bursts"):
    return [evidence_ledger._session_event(
        source, {"session_id": "synthetic"}, message, "synthetic-host", index,
    ).document() for index, message in enumerate(messages)]


class StopHookEvidenceTests(unittest.TestCase):
    def test_multipart_hook_envelope_cannot_turn_its_followup_text_into_human_work(self):
        content = [{"type": "text", "text": HOOK},
                   {"type": "text", "text": "Automatically continuing the same delivery check."}]
        events = [{"timestamp": timestamp, **message} for timestamp in TIMES
                  for message in collector._message_evidence(content, "user")]
        self.assertEqual([], collector.hermes_user_observed_intervals(events))
        self.assertEqual([], accounting._analysis_events(ledger_events(events))[0])

    def test_generated_remote_claude_and_codex_keep_hooks_out_of_human_bursts(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            scope = {"Path": Path, "dt": dt, "json": json, "BUCHAREST": collector.BUCHAREST,
                     "SINCE": dt.datetime.fromisoformat(TIMES[0]),
                     "UNTIL": dt.datetime.fromisoformat("2026-10-15T00:00:00+03:00"),
                     "CBASE": str(base), "MACHINE": "synthetic-host", "CXBASE": "",
                     "parse_dt": collector.parse_dt, "local_str": collector.local_dt_string,
                     "label": lambda path, root: "Synthetic project", "res": {"errors": []}}
            exec(collector._remote_claude_contract(), scope)
            path = base / "session.jsonl"
            path.write_text("\n".join(json.dumps({
                "timestamp": timestamp, "type": "user", "message": {"content": HOOK},
            }) for timestamp in TIMES), encoding="utf-8")
            self.assertEqual([], scope["parse_claude"](path))
            exec(collector._remote_codex_contract(), scope)
            hook = scope["_codex_message_event"]({"timestamp": TIMES[0], "type": "event_msg",
                "payload": {"type": "user_message", "message": HOOK}})
            self.assertEqual([], scope["_partition_bursts"]([hook]))

    def test_local_and_generated_remote_hermes_freeze_the_same_hook_marker(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.db"
            since = dt.datetime.fromisoformat(TIMES[0])
            until = dt.datetime.fromisoformat("2026-10-15T00:00:00+03:00")
            with sqlite3.connect(path) as connection:
                connection.execute("CREATE TABLE sessions (id, started_at, ended_at, message_count, model, cwd, estimated_cost_usd, title, input_tokens, output_tokens)")
                connection.execute("CREATE TABLE messages (id, session_id, role, timestamp, content, tool_name)")
                connection.execute("INSERT INTO sessions VALUES (?, ?, ?, 2, '', '', 0, '', 0, 0)",
                                   ("synthetic", since.timestamp(), dt.datetime.fromisoformat(TIMES[1]).timestamp()))
                connection.executemany("INSERT INTO messages VALUES (?, 'synthetic', 'user', ?, ?, '')",
                    [(index, dt.datetime.fromisoformat(timestamp).timestamp(), HOOK)
                     for index, timestamp in enumerate(TIMES)])
            local = collector.collect_hermes_db_sessions(str(path), "synthetic-host", since, until)
            scope = {"Path": Path, "dt": dt, "BUCHAREST": collector.BUCHAREST,
                     "SINCE": since, "UNTIL": until, "MACHINE": "synthetic-host", "HDB": str(path),
                     "parse_dt": collector.parse_dt, "local_str": collector.local_dt_string,
                     "res": {"errors": [], "hermes_db_sessions": []}}
            exec(collector._remote_hermes_contract(), scope)
            self.assertEqual([], scope["res"]["errors"])
            self.assertEqual(local, scope["res"]["hermes_db_sessions"])
            self.assertEqual([], local[0]["observed_user_intervals"])
            self.assertEqual([], accounting._analysis_events(ledger_events(local[0]["events"]))[0])

    def test_fresh_claude_hook_only_session_has_no_human_burst(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.jsonl"
            path.write_text("\n".join(json.dumps({
                "timestamp": timestamp, "type": "user", "message": {"content": HOOK},
            }) for timestamp in TIMES), encoding="utf-8")
            events = collector._extract_claude_events(
                path, dt.datetime.fromisoformat(TIMES[0]), dt.datetime.fromisoformat(TIMES[-1]),
            )
            self.assertEqual([], collector._partition_bursts(events))
            self.assertEqual([], collector._burst_context(events)[2])
            self.assertEqual([HOOK, HOOK], [event["content"] for event in events])

    def test_fresh_hook_context_is_retained_without_extending_human_burst(self):
        events = []
        for timestamp, role, content in (
            (TIMES[0], "user", "Review the synthetic deliverable."),
            (TIMES[1], "user", "Confirm the synthetic correction."),
            ("2026-10-14T10:20:00+03:00", "user", HOOK),
            ("2026-10-14T10:40:00+03:00", "assistant", "Unattended later result."),
        ):
            events.append({"timestamp": dt.datetime.fromisoformat(timestamp),
                           **collector._message_evidence(content, role)[0]})
        bursts = collector._partition_bursts(events)
        self.assertEqual(1, len(bursts))
        self.assertEqual([dt.datetime.fromisoformat(value) for value in TIMES],
                         collector._burst_context(bursts[0])[2])
        serialized = collector._serialized_events(bursts[0])
        self.assertEqual([HOOK], [event["content"] for event in serialized
                                  if event["kind"] == "injected_session_message"])
        self.assertNotIn(events[-1], bursts[0])

    def test_fresh_marker_reaches_ledger_noise_and_all_timing_consumers(self):
        for content in (HOOK, " \n" + HOOK.upper()):
            messages = [{"timestamp": timestamp,
                         **collector._message_evidence(content, "user")[0]}
                        for timestamp in TIMES]
            self.assertEqual([], collector.hermes_user_observed_intervals(messages))
            for source in ("claude_bursts", "codex_sessions", "hermes_db_sessions", "hermes_sessions"):
                with self.subTest(content=content, source=source):
                    events = ledger_events(messages, source)
                    retained, noise = accounting._analysis_events(events)
                    self.assertEqual([], retained)
                    self.assertEqual(2, len(noise))
                    self.assertEqual({"injected_wrapper"}, {row["reason"] for row in noise})
                    self.assertEqual({}, accounting._session_timing_contexts(events))
                    self.assertEqual([], accounting._activity_observed_intervals(events))
                    self.assertEqual([content, content], [event["attributes"]["content"] for event in events])

    def test_codex_fresh_transport_uses_same_marker(self):
        for transport, payload in (
            ("event_msg", {"type": "user_message", "message": HOOK}),
            ("response_item", {"type": "message", "role": "user",
                               "content": [{"type": "input_text", "text": HOOK}]}),
        ):
            with self.subTest(transport=transport):
                event = collector._codex_message_event({
                    "timestamp": TIMES[0], "type": transport, "payload": payload,
                })
                self.assertEqual("injected_session_message", event["kind"])
                self.assertEqual(HOOK, event["content"])

    def test_human_quotes_and_assistant_hook_repair_outcomes_stay_evidence(self):
        for role, content in (
            ("user", "Please investigate Stop hook feedback: the validator is failing."),
            ("user", '```text\n' + HOOK + '\n```'),
            ("assistant", HOOK + "; repaired the user-facing hook configuration."),
        ):
            with self.subTest(role=role, content=content):
                message = collector._message_evidence(content, role)[0]
                self.assertEqual("message", message["kind"])
                event = ledger_events([{"timestamp": TIMES[0], **message}])[0]
                self.assertEqual(([event], []), accounting._analysis_events([event]))

    def test_frozen_unmarked_ledger_retains_its_historical_recipe(self):
        messages = [{"timestamp": timestamp, "role": "user", "kind": "message", "content": HOOK}
                    for timestamp in TIMES]
        events = ledger_events(messages)
        frozen = json.dumps(events, sort_keys=True)
        self.assertEqual((events, []), accounting._analysis_events(events))
        self.assertEqual([{"start": TIMES[0], "end": TIMES[1]}],
                         accounting._activity_observed_intervals(events))
        self.assertEqual(frozen, json.dumps(events, sort_keys=True))


if __name__ == "__main__":
    unittest.main()
