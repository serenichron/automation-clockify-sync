"""Regression tests for half-open Hermes DB collection windows."""
from __future__ import annotations

import datetime as dt
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import clockify_sync_collect as collector


TZ = collector.BUCHAREST
SINCE = dt.datetime(2026, 9, 10, tzinfo=TZ)
UNTIL = dt.datetime(2026, 9, 11, tzinfo=TZ)


class HermesBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "state.db"
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                started_at REAL NOT NULL,
                ended_at REAL,
                message_count INTEGER,
                model TEXT,
                cwd TEXT,
                estimated_cost_usd REAL,
                title TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER
            )"""
        )
        conn.execute(
            """CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_name TEXT,
                timestamp REAL NOT NULL
            )"""
        )
        conn.close()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def add_session(
        self,
        session_id: str,
        start: dt.datetime,
        end: dt.datetime | None,
        messages: list[tuple[str, dt.datetime, str]],
        *,
        title: str = "session title",
    ) -> None:
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                start.timestamp(),
                end.timestamp() if end else None,
                len(messages),
                "test-model",
                "/work/project",
                0.25,
                title,
                10,
                20,
            ),
        )
        conn.executemany(
            "INSERT INTO messages (session_id, role, content, tool_name, timestamp) "
            "VALUES (?, ?, ?, '', ?)",
            [
                (session_id, role, content, timestamp.timestamp())
                for role, timestamp, content in messages
            ],
        )
        conn.commit()
        conn.close()

    def collect_legacy_remote(self) -> dict:
        original_run = subprocess.run

        def run_embedded(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            marker = "python3 - <<'PY'\n"
            embedded = command[-1]
            self.assertTrue(embedded.startswith(marker))
            code = embedded[len(marker):].removesuffix("\nPY")
            return original_run(
                ["python3", "-c", code], capture_output=True, text=True, check=False
            )

        machine = {
            "name": "remote-test",
            "host": "example.invalid",
            "claude_projects": "",
            "hermes_sessions": "",
            "hermes_db": str(self.db_path),
            "codex_home": "",
        }
        with mock.patch.object(collector.subprocess, "run", side_effect=run_embedded):
            return collector.collect_remote_sessions(machine, SINCE, UNTIL, [])

    def test_cross_boundary_session_is_clipped_without_content_leakage(self) -> None:
        self.add_session(
            "cross-midnight",
            SINCE - dt.timedelta(minutes=10),
            UNTIL + dt.timedelta(minutes=10),
            [
                ("user", SINCE - dt.timedelta(minutes=1), "before-window secret"),
                ("user", SINCE, "at-window-start"),
                ("assistant", SINCE + dt.timedelta(hours=12), "inside-window"),
                ("assistant", UNTIL, "at-next-window-start"),
                ("user", UNTIL + dt.timedelta(minutes=1), "after-window secret"),
            ],
        )

        records = collector.collect_hermes_db_sessions(
            str(self.db_path), "precision", SINCE, UNTIL
        )

        self.assertEqual(1, len(records))
        record = records[0]
        self.assertEqual("2026-09-10 00:00", record["start"])
        self.assertEqual("2026-09-11 00:00", record["end"])
        self.assertEqual(1440, record["duration_minutes"])
        self.assertTrue(record["boundary_clipped"])
        self.assertTrue(record["start_clipped"])
        self.assertTrue(record["end_clipped"])
        self.assertEqual(5, record["source_message_count"])
        self.assertEqual(2, record["message_count"])
        self.assertEqual("at-window-start", record["first_user_message"])
        self.assertEqual(
            ["at-window-start", "inside-window"],
            [event["content"] for event in record["events"]],
        )

    def test_exact_boundary_event_occurs_in_only_one_adjacent_window(self) -> None:
        self.add_session(
            "adjacent-windows",
            SINCE + dt.timedelta(hours=23),
            UNTIL + dt.timedelta(hours=1),
            [
                ("user", UNTIL - dt.timedelta(seconds=1), "first-window"),
                ("assistant", UNTIL, "second-window-boundary"),
            ],
        )

        first = collector.collect_hermes_db_sessions(
            str(self.db_path), "precision", SINCE, UNTIL
        )
        second = collector.collect_hermes_db_sessions(
            str(self.db_path), "precision", UNTIL, UNTIL + dt.timedelta(days=1)
        )

        self.assertEqual(["first-window"], [event["content"] for event in first[0]["events"]])
        self.assertEqual(
            ["second-window-boundary"],
            [event["content"] for event in second[0]["events"]],
        )
        first_timestamps = {event["timestamp"] for event in first[0]["events"]}
        second_timestamps = {event["timestamp"] for event in second[0]["events"]}
        self.assertFalse(first_timestamps & second_timestamps)

    def test_open_session_uses_last_message_end_without_inventing_duration(self) -> None:
        self.add_session(
            "open-session",
            SINCE - dt.timedelta(hours=1),
            None,
            [("user", SINCE, "boundary point")],
        )

        records = collector.collect_hermes_db_sessions(
            str(self.db_path), "precision", SINCE, UNTIL
        )

        self.assertEqual(1, len(records))
        self.assertEqual("2026-09-10 00:00", records[0]["start"])
        self.assertEqual("2026-09-10 00:00", records[0]["end"])
        self.assertEqual(0, records[0]["duration_minutes"])
        self.assertTrue(records[0]["start_clipped"])
        self.assertFalse(records[0]["end_clipped"])

    def test_non_overlapping_exact_edges_are_excluded(self) -> None:
        self.add_session(
            "ends-at-start",
            SINCE - dt.timedelta(hours=1),
            SINCE,
            [("assistant", SINCE - dt.timedelta(minutes=1), "before")],
        )
        self.add_session(
            "starts-at-end",
            UNTIL,
            UNTIL + dt.timedelta(hours=1),
            [("user", UNTIL, "next")],
        )

        records = collector.collect_hermes_db_sessions(
            str(self.db_path), "precision", SINCE, UNTIL
        )

        self.assertEqual([], records)

    def test_enriched_context_clips_spanning_session_and_all_message_context(self) -> None:
        self.add_session(
            "enriched-spanning",
            SINCE - dt.timedelta(hours=1),
            UNTIL + dt.timedelta(hours=1),
            [
                ("assistant", SINCE - dt.timedelta(minutes=1), "before context secret"),
                ("user", SINCE, "window request"),
                ("assistant", UNTIL - dt.timedelta(seconds=1), "window response"),
                ("user", UNTIL, "next window secret"),
            ],
        )

        records = collector.extract_hermes_db_context(str(self.db_path), SINCE, UNTIL)

        self.assertEqual(1, len(records))
        record = records[0]
        self.assertEqual("2026-09-10 00:00", record["start"])
        self.assertEqual("2026-09-11 00:00", record["end"])
        self.assertTrue(record["start_clipped"])
        self.assertTrue(record["end_clipped"])
        self.assertEqual("window response", record["last_message"])
        self.assertEqual("", record["user_messages"][0]["prev_assistant"])
        self.assertEqual("window response", record["user_messages"][0]["next_assistant"])
        self.assertNotIn("secret", repr(record))

    def test_legacy_remote_fallback_matches_local_half_open_contract(self) -> None:
        self.add_session(
            "remote-spanning",
            SINCE - dt.timedelta(minutes=5),
            UNTIL + dt.timedelta(minutes=5),
            [
                ("user", SINCE - dt.timedelta(seconds=1), "remote before secret"),
                ("user", SINCE, "remote window start"),
                ("assistant", UNTIL, "remote next window"),
            ],
        )
        result = self.collect_legacy_remote()

        self.assertEqual("partial", result["status"])
        self.assertEqual(1, len(result["hermes_db_sessions"]))
        record = result["hermes_db_sessions"][0]
        self.assertEqual("2026-09-10 00:00", record["start"])
        self.assertEqual("2026-09-11 00:00", record["end"])
        self.assertEqual(["remote window start"], [event["content"] for event in record["events"]])
        self.assertTrue(record["start_clipped"])
        self.assertTrue(record["end_clipped"])

    def test_clipped_local_title_uses_only_in_window_user_evidence(self) -> None:
        self.add_session(
            "local-title",
            SINCE - dt.timedelta(hours=1),
            SINCE + dt.timedelta(hours=1),
            [("assistant", SINCE + dt.timedelta(minutes=30), "in-window assistant")],
            title="outside-window secret title",
        )

        records = collector.collect_hermes_db_sessions(
            str(self.db_path), "precision", SINCE, UNTIL
        )

        self.assertEqual(1, len(records))
        self.assertEqual("", records[0]["title"])
        self.assertNotIn("outside-window secret title", repr(records[0]))

    def test_clipped_legacy_remote_title_uses_only_in_window_user_evidence(self) -> None:
        self.add_session(
            "remote-title",
            SINCE - dt.timedelta(hours=1),
            SINCE + dt.timedelta(hours=1),
            [("assistant", SINCE + dt.timedelta(minutes=30), "in-window assistant")],
            title="outside-window secret title",
        )

        result = self.collect_legacy_remote()

        self.assertEqual(1, len(result["hermes_db_sessions"]))
        self.assertEqual("", result["hermes_db_sessions"][0]["title"])
        self.assertNotIn(
            "outside-window secret title", repr(result["hermes_db_sessions"][0])
        )

    def test_malformed_closed_and_open_spans_are_excluded_everywhere(self) -> None:
        self.add_session(
            "closed-backwards",
            SINCE + dt.timedelta(hours=2),
            SINCE + dt.timedelta(hours=1),
            [("user", SINCE + dt.timedelta(hours=1, minutes=30), "closed backwards")],
        )
        self.add_session(
            "open-last-message-before-start",
            SINCE + dt.timedelta(hours=4),
            None,
            [("user", SINCE + dt.timedelta(hours=3), "open backwards")],
        )

        local = collector.collect_hermes_db_sessions(
            str(self.db_path), "precision", SINCE, UNTIL
        )
        enriched = collector.extract_hermes_db_context(str(self.db_path), SINCE, UNTIL)
        remote = self.collect_legacy_remote()

        self.assertEqual([], local)
        self.assertEqual([], enriched)
        self.assertEqual([], remote["hermes_db_sessions"])


if __name__ == "__main__":
    unittest.main()
