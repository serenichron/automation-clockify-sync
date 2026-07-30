"""Regression tests for per-burst Clockify collector attribution."""
from __future__ import annotations

import datetime as dt
import contextlib
import importlib.util
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "clockify_sync_collect.py"
SPEC = importlib.util.spec_from_file_location("clockify_sync_collect", SCRIPT)
assert SPEC and SPEC.loader
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)

TZ = dt.timezone(dt.timedelta(hours=3))
SINCE = dt.datetime(2026, 7, 21, tzinfo=TZ)
UNTIL = dt.datetime(2026, 7, 22, tzinfo=TZ)


def event(timestamp: str, event_type: str, message: str) -> dict:
    return {"timestamp": timestamp, "type": "event_msg", "payload": {"type": event_type, "message": message}}


class CollectorBurstContextTests(unittest.TestCase):
    def write_jsonl(self, path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    def test_codex_two_bursts_use_distinct_user_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout.jsonl"
            self.write_jsonl(path, [
                {"type": "session_meta", "payload": {"id": "codex-session", "cwd": "/work/project-a"}},
                event("2026-07-21T05:00:00Z", "user_message", "Repair the billing export totals."),
                event("2026-07-21T05:01:00Z", "agent_message", "Billing export repaired."),
                event("2026-07-21T05:02:00Z", "user_message", "Add regression coverage for the exporter."),
                # This assistant message must not bridge the 38-minute user gap.
                event("2026-07-21T05:20:00Z", "agent_message", "First burst result retained."),
                event("2026-07-21T05:40:00Z", "user_message", "Investigate the login redirect loop."),
                event("2026-07-21T05:41:00Z", "agent_message", "Redirect loop root cause documented."),
                event("2026-07-21T05:42:00Z", "user_message", "Patch the authentication callback."),
                event("2026-07-21T06:30:00Z", "agent_message", "Late result must not contaminate a burst."),
            ])
            bursts = collector.parse_codex_rollout_file(path, "precision", SINCE, UNTIL)

        self.assertEqual(2, len(bursts))
        self.assertEqual("Repair the billing export totals.", bursts[0]["first_user_message"])
        self.assertEqual("Investigate the login redirect loop.", bursts[1]["first_user_message"])
        self.assertNotEqual(bursts[0]["title"], bursts[1]["title"])
        self.assertEqual(2, bursts[0]["duration_minutes"])
        self.assertEqual(2, bursts[1]["duration_minutes"])
        self.assertEqual("Redirect loop root cause documented.", bursts[1]["last_assistant_message"])

    def test_claude_two_bursts_use_distinct_user_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "projects"
            path = base / "project-a" / "session.jsonl"
            self.write_jsonl(path, [
                {"timestamp": "2026-07-21T05:00:00Z", "type": "user", "message": {"content": "Implement the invoice parser fix."}},
                {"timestamp": "2026-07-21T05:01:00Z", "type": "assistant", "message": {"content": [{"type": "text", "text": "Invoice parser fixed."}]}},
                {"timestamp": "2026-07-21T05:02:00Z", "type": "user", "message": {"content": "Add parser tests."}},
                {"timestamp": "2026-07-21T05:40:00Z", "type": "user", "message": {"content": "Audit the OAuth callback security."}},
                {"timestamp": "2026-07-21T05:41:00Z", "type": "assistant", "message": {"content": [{"type": "tool_use", "name": "rg"}, {"type": "text", "text": "OAuth callback audit complete."}]}},
                {"timestamp": "2026-07-21T05:42:00Z", "type": "user", "message": {"content": "Add the state validation."}},
            ])
            bursts = collector.parse_claude_jsonl_file(path, str(base), SINCE, UNTIL, "precision")

        self.assertEqual(2, len(bursts))
        self.assertEqual("Implement the invoice parser fix.", bursts[0]["first_user_message"])
        self.assertEqual("Audit the OAuth callback security.", bursts[1]["first_user_message"])
        self.assertEqual("OAuth callback audit complete.", bursts[1]["last_assistant_message"])
        self.assertEqual(2, bursts[0]["duration_minutes"])
        self.assertEqual(2, bursts[1]["duration_minutes"])

    def test_local_cross_midnight_bursts_keep_identity_in_clipped_windows(self) -> None:
        broad_since = dt.datetime(2026, 7, 20, tzinfo=TZ)
        clipped_since = dt.datetime(2026, 7, 21, tzinfo=TZ)
        until = dt.datetime(2026, 7, 22, tzinfo=TZ)
        expected_window = ("2026-07-20 23:58", "2026-07-21 00:02")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_base = root / "projects"
            claude_path = claude_base / "project-a" / "session.jsonl"
            self.write_jsonl(claude_path, [
                {"timestamp": "2026-07-20T20:58:00Z", "type": "user", "message": {"content": "Finish the midnight Claude task."}},
                {"timestamp": "2026-07-20T21:02:00Z", "type": "user", "message": {"content": "Complete the same Claude task."}},
            ])
            codex_path = root / "rollout.jsonl"
            self.write_jsonl(codex_path, [
                {"type": "session_meta", "payload": {"id": "midnight-codex"}},
                event("2026-07-20T20:58:00Z", "user_message", "Finish the midnight Codex task."),
                event("2026-07-20T21:02:00Z", "user_message", "Complete the same Codex task."),
            ])

            sources = {
                "claude": lambda since: collector.parse_claude_jsonl_file(
                    claude_path, str(claude_base), since, until, "precision"
                ),
                "codex": lambda since: collector.parse_codex_rollout_file(
                    codex_path, "precision", since, until
                ),
            }
            for source, parse in sources.items():
                with self.subTest(source=source):
                    broad = parse(broad_since)
                    clipped = parse(clipped_since)
                    self.assertEqual(1, len(broad))
                    self.assertEqual(1, len(clipped))
                    self.assertEqual(expected_window, (broad[0]["start"], broad[0]["end"]))
                    self.assertEqual(expected_window, (clipped[0]["start"], clipped[0]["end"]))
                    self.assertEqual(
                        collector._record_provenance(broad[0]),
                        collector._record_provenance(clipped[0]),
                    )
                    self.assertEqual(
                        collector._candidate_key(broad[0]),
                        collector._candidate_key(clipped[0]),
                    )

    def test_candidate_identity_uses_session_and_provenance(self) -> None:
        first = {"source": "codex", "machine": "precision", "session_id": "one", "start": "2026-07-21 08:00", "end": "2026-07-21 08:10", "label": "same-label", "path": "/tmp/one", "cwd": "/work/same-label"}
        second = {**first, "session_id": "two", "path": "/tmp/two"}
        self.assertNotEqual(collector._candidate_key(first), collector._candidate_key(second))

        routing = {"skip_rules": {"min_minutes": 0, "min_user_messages": 0}, "session_routes": [{"pattern": "same-label", "project_name": "Project", "project_suffix": "123456", "tag_suffixes": [], "tag_names": [], "prefix": "SC"}]}
        proposals, _, _ = collector.build_proposals({"clockify": {"entries": []}, "sessions": [{"codex_sessions": [{**first, "duration_minutes": 10, "user_messages": 2, "first_user_message": "Do the focused work."}]}]}, routing)
        self.assertEqual(1, len(proposals))
        self.assertEqual("codex", proposals[0]["provenance"]["source_type"])
        self.assertEqual("one", proposals[0]["provenance"]["source_session_id"])
        self.assertEqual("2026-07-21 08:00", proposals[0]["provenance"]["burst_start"])
        self.assertEqual(collector._candidate_key(first), proposals[0]["candidate_key"])
        self.assertEqual("hermes", collector._record_provenance({"source": "hermes_db"})["source_type"])

    def test_remote_claude_contract_has_per_burst_context_without_assistant_bridging(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp) / "projects"
            path = base / "project-a" / "remote-session.jsonl"
            self.write_jsonl(path, [
                {"timestamp": "2026-07-21T05:00:00Z", "type": "user", "message": {"content": "First remote task prompt."}},
                {"timestamp": "2026-07-21T05:02:00Z", "type": "user", "message": {"content": "Continue first remote task."}},
                {"timestamp": "2026-07-21T05:20:00Z", "type": "assistant", "message": {"content": "First remote result."}},
                {"timestamp": "2026-07-21T05:40:00Z", "type": "user", "message": {"content": "Second remote task prompt."}},
                {"timestamp": "2026-07-21T05:41:00Z", "type": "assistant", "message": {"content": "Second remote result."}},
                {"timestamp": "2026-07-21T05:42:00Z", "type": "user", "message": {"content": "Continue second remote task."}},
                {"timestamp": "2026-07-21T06:30:00Z", "type": "assistant", "message": {"content": "Late remote result must not attach."}},
            ])
            machine = {"name": "remote-machine", "host": "example.invalid", "claude_projects": str(base), "hermes_sessions": "", "hermes_db": "", "codex_home": ""}
            captured: dict[str, list[str]] = {}

            def fake_run(command: list[str], **_: object) -> object:
                captured["command"] = command
                return collector.subprocess.CompletedProcess(command, 0, '{"status":"ok"}', "")

            with mock.patch.object(collector.subprocess, "run", side_effect=fake_run):
                collector.collect_remote_sessions(machine, SINCE, UNTIL, [])
            generated = captured["command"][-1].split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
            scope: dict[str, object] = {}
            with contextlib.redirect_stdout(io.StringIO()):
                exec(generated, scope)
            bursts = scope["res"]["claude_bursts"]

        self.assertEqual(2, len(bursts))
        self.assertEqual("First remote task prompt.", bursts[0]["first_user_message"])
        self.assertEqual("Second remote task prompt.", bursts[1]["first_user_message"])
        self.assertEqual("First remote result.", bursts[0]["last_assistant_message"])
        self.assertEqual("Second remote result.", bursts[1]["last_assistant_message"])
        self.assertEqual("remote-session", bursts[0]["session_id"])
        self.assertEqual(str(path), bursts[0]["path"])

    def test_remote_contracts_keep_full_cross_midnight_burst_boundaries(self) -> None:
        clipped_since = dt.datetime(2026, 7, 21, tzinfo=TZ)
        until = dt.datetime(2026, 7, 22, tzinfo=TZ)
        expected_window = ("2026-07-20 23:58", "2026-07-21 00:02")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            claude_base = root / "projects"
            claude_path = claude_base / "project-a" / "remote-session.jsonl"
            self.write_jsonl(claude_path, [
                {"timestamp": "2026-07-20T20:58:00Z", "type": "user", "message": {"content": "Finish the midnight remote Claude task."}},
                {"timestamp": "2026-07-20T21:02:00Z", "type": "user", "message": {"content": "Complete the same remote Claude task."}},
            ])
            codex_home = root / "codex"
            codex_home.mkdir()
            rollout = root / "rollout.jsonl"
            self.write_jsonl(rollout, [
                {"type": "session_meta", "payload": {"id": "midnight-remote-codex"}},
                event("2026-07-20T20:58:00Z", "user_message", "Finish the midnight remote Codex task."),
                event("2026-07-20T21:02:00Z", "user_message", "Complete the same remote Codex task."),
            ])
            conn = sqlite3.connect(codex_home / "state_5.sqlite")
            conn.execute("CREATE TABLE threads (id, rollout_path, cwd, title, first_user_message, thread_source, archived, updated_at)")
            conn.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("midnight-remote-codex", str(rollout), "/work/project-a", "", "", "user", 0, int(clipped_since.timestamp())),
            )
            conn.commit()
            conn.close()
            machine = {
                "name": "remote-machine", "host": "example.invalid",
                "claude_projects": str(claude_base), "hermes_sessions": "",
                "hermes_db": "", "codex_home": str(codex_home),
            }
            captured: dict[str, list[str]] = {}

            def fake_run(command: list[str], **_: object) -> object:
                captured["command"] = command
                return collector.subprocess.CompletedProcess(command, 0, '{"status":"ok"}', "")

            with mock.patch.object(collector.subprocess, "run", side_effect=fake_run):
                collector.collect_remote_sessions(machine, clipped_since, until, [])
            generated = captured["command"][-1].split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
            scope: dict[str, object] = {}
            with contextlib.redirect_stdout(io.StringIO()):
                exec(generated, scope)
            result = scope["res"]

        self.assertEqual(1, len(result["claude_bursts"]))
        self.assertEqual(1, len(result["codex_sessions"]))
        for source, burst in (
            ("claude", result["claude_bursts"][0]),
            ("codex", result["codex_sessions"][0]),
        ):
            with self.subTest(source=source):
                self.assertEqual(expected_window, (burst["start"], burst["end"]))

    def test_remote_codex_contract_has_row_local_bursts_and_context(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / "codex"
            rollout = Path(tmp) / "rollout.jsonl"
            self.write_jsonl(rollout, [
                {"type": "session_meta", "payload": {"id": "remote-codex-session"}},
                event("2026-07-21T05:00:00Z", "user_message", "First remote Codex task."),
                event("2026-07-21T05:01:00Z", "agent_message", "First Codex result."),
                event("2026-07-21T05:02:00Z", "user_message", "Continue first Codex task."),
                event("2026-07-21T05:40:00Z", "user_message", "Second remote Codex task."),
                event("2026-07-21T05:41:00Z", "agent_message", "Second Codex result."),
                event("2026-07-21T05:42:00Z", "user_message", "Continue second Codex task."),
                event("2026-07-21T06:30:00Z", "agent_message", "Late Codex result must not attach."),
            ])
            codex_home.mkdir()
            conn = sqlite3.connect(codex_home / "state_5.sqlite")
            conn.execute("CREATE TABLE threads (id, rollout_path, cwd, title, first_user_message, thread_source, archived, updated_at)")
            conn.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?)", ("remote-codex-session", str(rollout), "/work/project-a", "session-wide title must not leak", "first message must not leak", "user", 0, int(SINCE.timestamp())))
            conn.commit()
            conn.close()
            machine = {"name": "remote-machine", "host": "example.invalid", "claude_projects": "", "hermes_sessions": "", "hermes_db": "", "codex_home": str(codex_home)}
            captured: dict[str, list[str]] = {}

            def fake_run(command: list[str], **_: object) -> object:
                captured["command"] = command
                return collector.subprocess.CompletedProcess(command, 0, '{"status":"ok"}', "")

            with mock.patch.object(collector.subprocess, "run", side_effect=fake_run):
                collector.collect_remote_sessions(machine, SINCE, UNTIL, [])
            generated = captured["command"][-1].split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
            scope: dict[str, object] = {}
            with contextlib.redirect_stdout(io.StringIO()):
                exec(generated, scope)
            bursts = scope["res"]["codex_sessions"]

        self.assertEqual(2, len(bursts))
        self.assertEqual("First remote Codex task.", bursts[0]["first_user_message"])
        self.assertEqual("Second remote Codex task.", bursts[1]["first_user_message"])
        self.assertEqual("First Codex result.", bursts[0]["last_assistant_message"])
        self.assertEqual("Second Codex result.", bursts[1]["last_assistant_message"])
        self.assertEqual("Second remote Codex task.", bursts[1]["title"])
        self.assertNotIn("session-wide", bursts[1]["title"])
        self.assertEqual("remote-codex-session", bursts[0]["session_id"])
        self.assertEqual(str(rollout), bursts[0]["path"])
        self.assertEqual("/work/project-a", bursts[0]["cwd"])

    def test_os_account_home_credentials_are_found_with_an_isolated_task_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            task_home = Path(tmp) / "task-home"
            account_home = Path(tmp) / "account-home"
            clockify_env = account_home / ".config" / "serenichron" / "clockify.env"
            fathom_env = account_home / ".config" / "serenichron" / "fathom.env"
            clockify_env.parent.mkdir(parents=True)
            clockify_env.write_text("CLOCKIFY_API_KEY=clockify-test-key\nCLOCKIFY_WORKSPACE_ID=workspace-id\n")
            fathom_env.write_text("FATHOM_API_KEY=fathom-test-key\n")
            account = mock.Mock(pw_dir=str(account_home))

            with mock.patch.dict(collector.os.environ, {"HOME": str(task_home)}, clear=True), mock.patch.object(
                collector.pwd, "getpwuid", return_value=account
            ):
                cenv = collector.load_env_file(
                    collector.clockify_env_candidates(),
                    ["CLOCKIFY_API_KEY", "CLOCKIFY_WORKSPACE_ID"],
                )
                fenv = collector.load_env_file(
                    collector.fathom_env_candidates(), ["FATHOM_API_KEY"]
                )

        self.assertEqual(str(clockify_env), cenv["_env_file"])
        self.assertEqual("clockify-test-key", cenv["CLOCKIFY_API_KEY"])
        self.assertEqual(str(fathom_env), fenv["_env_file"])
        self.assertEqual("fathom-test-key", fenv["FATHOM_API_KEY"])

    def test_multica_env_only_configuration_needs_no_profile(self) -> None:
        env = {
            "MULTICA_TOKEN": "multica-test-token",
            "MULTICA_SERVER_URL": "https://multica.example.test",
            "MULTICA_WORKSPACE_ID": "workspace-id",
        }
        captured: dict[str, object] = {}

        def fake_http_json(url: str, headers: dict[str, str]) -> dict[str, list[dict[str, str]]]:
            captured["url"] = url
            captured["headers"] = headers
            return {"issues": [{"id": "issue-id", "key": "SER-1", "title": "Test", "status": "open"}]}

        with mock.patch.dict(collector.os.environ, env, clear=True), mock.patch.object(
            collector, "_home_candidates", side_effect=AssertionError("profile lookup must not run")
        ), mock.patch.object(collector, "http_json", side_effect=fake_http_json):
            result = collector.fetch_multica_issues()

        self.assertEqual("ok", result["status"])
        self.assertEqual("https://multica.example.test/api/issues?limit=100", captured["url"])
        self.assertEqual(
            {"Authorization": "Bearer multica-test-token", "X-Workspace-ID": "workspace-id"},
            captured["headers"],
        )

    def test_description_is_single_line_capped_and_ignores_sentinels(self) -> None:
        burst = {
            "first_user_message": "<app-context>\npermissions instructions\n</app-context>",
            "last_assistant_message": "Completed the narrowly scoped reconciliation fix.\n" + "x" * 300,
            "label": "project-a",
            "machine": "precision",
            "source": "codex",
            "user_messages": 2,
            "duration_minutes": 12,
        }
        description = collector._make_description("Project", burst, prefix="SC")
        self.assertNotIn("\n", description)
        self.assertLessEqual(len(description), collector.DESCRIPTION_LIMIT)
        self.assertIn("Completed the narrowly scoped reconciliation fix.", description)
        self.assertNotIn("permissions instructions", description.lower())

    def test_markdown_records_runtime_identity_and_stable_review_approval_rule(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = {
                "run_id": "run-1",
                "date_range": {"since": "2026-07-21 00:00", "until": "2026-07-22 00:00", "reason": "test"},
                "safety": {"dry_run": True},
                "runtime_identity": {"collector_path": "/repo/scripts/clockify_sync_collect.py", "canonical_root": "/repo", "git_sha": "abc123"},
                "evidence": {"clockify": {"status": "ok", "entries": []}, "fathom": {"status": "ok", "meetings": []}, "multica_issues": {"status": "ok", "issues": []}, "sessions": []},
                "proposals": [], "ambiguous": [], "skipped": [],
            }
            output = Path(tmp) / "run-report.md"
            collector.write_markdown(Path(tmp), report)
            text = output.read_text()
        self.assertIn("/repo/scripts/clockify_sync_collect.py", text)
        self.assertIn("git: abc123", text)
        self.assertIn("review-snapshot.json", text)
        self.assertIn("stable review item IDs", text)


    def test_auto_fleet_entry_is_local_only_on_matching_host(self) -> None:
        macbook = {
            "name": "macbook",
            "kind": "auto",
            "host": "vlads-macbook-pro.tail5a1162.ts.net",
            "local_hostnames": ["Vlads-MacBook-Pro.local"],
        }
        precision = {
            "name": "omarchy-precision",
            "kind": "auto",
            "host": "omarchy-precision.tail5a1162.ts.net",
        }
        self.assertTrue(collector.machine_is_local(macbook, "Vlads-MacBook-Pro.local"))
        self.assertFalse(collector.machine_is_local(macbook, "omarchy-precision"))
        self.assertTrue(collector.machine_is_local(precision, "omarchy-precision"))

    def test_codex_file_scaffold_is_removed_from_description(self) -> None:
        burst = {
            "first_user_message": (
                "# Files mentioned by the user:\n"
                "## repo: /work/repo\n"
                "# My request for Codex:\n"
                "Audit the existing billing workflow."
            ),
            "last_assistant_message": "",
            "label": "repo",
            "machine": "macbook",
            "source": "codex",
            "user_messages": 2,
            "duration_minutes": 12,
        }
        description = collector._make_description("Project", burst, prefix="SC")
        self.assertEqual("SC — Audit the existing billing workflow.", description)
        self.assertNotIn("Files mentioned", description)


    def test_description_prefers_embedded_goal_heading_over_generic_preamble(self) -> None:
        burst = {
            "first_user_message": "can you please just give me the whole goal to run?",
            "last_assistant_message": (
                "Here it is — ready to paste.\n\n"
                "# Goal: Collapse per-session MCP process fan-out in Codex "
                "**Run this at end-of-day.**\n"
                "Changes take effect for new sessions only."
            ),
            "label": "-Users-blackthorne",
            "machine": "macbook",
            "source": "claude",
            "user_messages": 4,
            "duration_minutes": 21,
        }

        description = collector._make_description(
            "Serenichron Level 2", burst, confidence="low", prefix="SC"
        )

        self.assertEqual(
            "SC — [NEEDS REVIEW] Collapse per-session MCP process fan-out in Codex",
            description,
        )
        self.assertNotIn("Here it is", description)

    def test_goal_command_is_extracted_as_plain_description(self) -> None:
        burst = {
            "first_user_message": "write the whole goal",
            "last_assistant_message": (
                "Here’s the updated goal I’d use: ```text /goal Investigate, "
                "troubleshoot, and fix the local Codex CLI routing mismatch. "
                "Current symptoms follow."
            ),
            "label": "project-a",
            "machine": "desktop",
            "source": "codex",
            "user_messages": 2,
            "duration_minutes": 16,
        }

        description = collector._make_description("Project", burst, prefix="SC")

        self.assertEqual(
            "SC — Investigate, troubleshoot, and fix the local Codex CLI routing mismatch",
            description,
        )
        self.assertNotIn("/goal", description)
        self.assertNotIn("```", description)

    def test_session_hook_uses_embedded_human_directive(self) -> None:
        burst = {
            "first_user_message": (
                'A session-scoped Stop hook is now active with condition: '
                '"investigate chromium audio not playing back over the Kave headset'
            ),
            "last_assistant_message": (
                "Saved to memory. Bottom line: the USB DAC was wedged after an update."
            ),
            "label": "-home-blackthorne",
            "machine": "desktop",
            "source": "claude",
            "user_messages": 4,
            "duration_minutes": 22,
        }

        description = collector._make_description(
            "Serenichron Level 2", burst, confidence="low", prefix="SC"
        )

        self.assertEqual(
            "SC — [NEEDS REVIEW] investigate chromium audio not playing back over the Kave headset",
            description,
        )
        self.assertNotIn("session-scoped", description)
        self.assertNotIn("Saved to memory", description)

    def test_low_confidence_direct_request_beats_status_heavy_response(self) -> None:
        burst = {
            "first_user_message": (
                "Troubleshoot the worker-service error loading zod/v3."
            ),
            "last_assistant_message": (
                "Fixed. Worker service loads clean under bun. Summary: the plugin "
                "cache contained a partial node_modules installation."
            ),
            "label": "-home-blackthorne",
            "machine": "desktop",
            "source": "claude",
            "user_messages": 5,
            "duration_minutes": 14,
        }

        description = collector._make_description(
            "Serenichron Level 2", burst, confidence="low", prefix="SC"
        )

        self.assertEqual(
            "SC — [NEEDS REVIEW] Troubleshoot the worker-service error loading zod/v3.",
            description,
        )
        self.assertNotIn("Fixed.", description)

    def test_stop_hook_error_becomes_work_summary_without_goal_command(self) -> None:
        burst = {
            "first_user_message": (
                "i am encountering this error: Ran 1 stop hook. "
                "Stop hook error: Hook evaluator API error: "
                "API Error: 400 Tool reference 'Bash' not found in available tools"
            ),
            "last_assistant_message": (
                "TLDR: The failing Stop hook is Claude Code's built-in /goal "
                "feature. /goal registers a session-scoped Stop hook."
            ),
            "label": "-claude",
            "machine": "macbook",
            "source": "claude",
            "user_messages": 4,
            "duration_minutes": 8,
        }

        description = collector._make_description(
            "Serenichron Level 2", burst, confidence="low", prefix="SC"
        )

        self.assertEqual(
            "SC — [NEEDS REVIEW] Troubleshoot Claude Code Stop hook evaluator error",
            description,
        )
        self.assertNotIn("/goal", description)

    def test_contextless_routed_session_moves_to_ambiguous_with_identity(self) -> None:
        routing = {
            "skip_rules": {
                "min_minutes": 10,
                "min_user_messages": 5,
                "weekend_short_max_minutes": 60,
            },
            "session_routes": [
                {
                    "pattern": "cpu",
                    "project_name": "Serenichron Level 2",
                    "project_suffix": "775f9f",
                    "tag_suffixes": ["35aa9afb"],
                    "tag_names": ["System development"],
                    "prefix": "SC",
                    "confidence": "low",
                }
            ],
        }
        burst = {
            "source": "claude",
            "machine": "omarchy-precision",
            "session_id": "contextless-session",
            "path": "/home/blackthorne/.claude/projects/cpu/session.jsonl",
            "label": "cpu",
            "start": "2026-07-10 17:22",
            "end": "2026-07-10 17:23",
            "duration_minutes": 1,
            "user_messages": 13,
            "first_user_message": "",
            "last_assistant_message": "",
        }

        proposals, ambiguous, skipped = collector.build_proposals(
            {
                "clockify": {"entries": []},
                "sessions": [{"claude_bursts": [burst]}],
            },
            routing,
        )

        self.assertEqual([], proposals)
        self.assertEqual([], skipped)
        self.assertEqual(1, len(ambiguous))
        self.assertTrue(ambiguous[0]["candidate_key"].startswith("ck-"))
        self.assertEqual(
            "contextless-session",
            ambiguous[0]["provenance"]["source_session_id"],
        )
        self.assertEqual("Serenichron Level 2", ambiguous[0]["client_project"])
        self.assertEqual("cpu", ambiguous[0]["label"])
        self.assertEqual(
            "2026-07-10 17:22–2026-07-10 17:23",
            ambiguous[0]["time"],
        )
        self.assertIn("review, revise, or reject", ambiguous[0]["reason"])
        self.assertTrue(
            collector._is_contextless_description(
                ambiguous[0]["description"] + " (trimmed around parallel work)"
            )
        )

    def test_usage_limit_response_and_setup_only_prompt_require_review(self) -> None:
        burst = {
            "first_user_message": "read multica skills first",
            "last_assistant_message": (
                "You've hit your session limit · resets 4:40am (Europe/Bucharest)"
            ),
            "label": "site",
            "machine": "macbook",
            "source": "claude",
            "user_messages": 2,
            "duration_minutes": 12,
        }

        description = collector._make_description(
            "TST Prep Level 1", burst, confidence="medium", prefix="TSTP"
        )

        self.assertEqual(
            "TSTP — [NEEDS REVIEW] site (2 msgs, 12m)",
            description,
        )
        self.assertNotIn("session limit", description)
        self.assertNotIn("read multica", description)
        self.assertTrue(collector._is_system_message("HEADROOM_CLAUDE_OK"))
        self.assertTrue(
            collector._is_system_message(
                "Reply with exactly HEADROOM_CLAUDE_OK"
            )
        )

    def test_ornamental_insight_rule_is_removed_from_description(self) -> None:
        result = collector._plain_description_text(
            "## Investigation complete\n"
            "★ Insight ─────────────────────────────────────\n"
            "Daily updates lag the release channel."
        )

        self.assertEqual(
            "Investigation complete Daily updates lag the release channel.",
            result,
        )

    def test_weekend_session_at_or_below_sixty_minutes_is_skipped(self) -> None:
        routing = {
            "skip_rules": {
                "min_minutes": 0,
                "min_user_messages": 0,
                "weekend_short_max_minutes": 60,
            },
            "session_routes": [
                {
                    "pattern": "project-a",
                    "project_name": "Project",
                    "project_suffix": "123456",
                    "tag_suffixes": [],
                    "tag_names": [],
                    "prefix": "SC",
                }
            ],
        }
        session = {
            "source": "codex",
            "machine": "macbook",
            "session_id": "weekend",
            "cwd": "/work/project-a",
            "start": "2026-07-18 10:00",
            "end": "2026-07-18 10:30",
            "duration_minutes": 30,
            "user_messages": 8,
            "first_user_message": "Do weekend work.",
        }

        proposals, _, skipped = collector.build_proposals(
            {
                "clockify": {"entries": []},
                "sessions": [{"codex_sessions": [session]}],
            },
            routing,
        )

        self.assertEqual([], proposals)
        self.assertIn("weekend session", skipped[0]["reason"])

    def test_synced_session_replica_is_proposed_only_once(self) -> None:
        routing = {
            "skip_rules": {
                "min_minutes": 0,
                "min_user_messages": 0,
                "weekend_short_max_minutes": 60,
            },
            "session_routes": [
                {
                    "pattern": "project-a",
                    "project_name": "Project",
                    "project_suffix": "123456",
                    "tag_suffixes": [],
                    "tag_names": [],
                    "prefix": "SC",
                }
            ],
        }
        base = {
            "source": "codex",
            "session_id": "same-session",
            "cwd": "/work/project-a",
            "start": "2026-07-20 10:00",
            "end": "2026-07-20 10:20",
            "duration_minutes": 20,
            "user_messages": 8,
            "first_user_message": "Do one logical task.",
        }
        macbook = {**base, "machine": "macbook", "path": "/mac/session.jsonl"}
        desktop = {
            **base,
            "machine": "omarchy-desktop",
            "path": "/linux/session.jsonl",
        }

        proposals, _, skipped = collector.build_proposals(
            {
                "clockify": {"entries": []},
                "sessions": [
                    {"codex_sessions": [macbook]},
                    {"codex_sessions": [desktop]},
                ],
            },
            routing,
        )

        self.assertEqual(1, len(proposals))
        self.assertEqual(1, len(skipped))
        self.assertIn("replicated session evidence", skipped[0]["reason"])

    def test_adjacent_same_work_sessions_are_consolidated(self) -> None:
        routing = {
            "skip_rules": {
                "min_minutes": 0,
                "min_user_messages": 0,
                "weekend_short_max_minutes": 60,
            },
            "session_routes": [
                {
                    "pattern": "project-a",
                    "project_name": "Project",
                    "project_suffix": "123456",
                    "tag_suffixes": [],
                    "tag_names": [],
                    "prefix": "SC",
                }
            ],
        }
        first = {
            "source": "codex",
            "machine": "macbook",
            "session_id": "first",
            "cwd": "/work/project-a",
            "start": "2026-07-20 10:00",
            "end": "2026-07-20 10:05",
            "duration_minutes": 5,
            "user_messages": 8,
            "last_assistant_message": "Same completed work.",
        }
        second = {
            **first,
            "session_id": "second",
            "start": "2026-07-20 10:06",
            "end": "2026-07-20 10:07",
            "duration_minutes": 1,
        }

        proposals, _, skipped = collector.build_proposals(
            {
                "clockify": {"entries": []},
                "sessions": [{"codex_sessions": [first, second]}],
            },
            routing,
        )

        self.assertEqual(1, len(proposals))
        self.assertEqual(6, proposals[0]["duration_minutes"])
        self.assertEqual("2026-07-20 10:07", proposals[0]["end"])
        self.assertTrue(proposals[0]["candidate_key"].startswith("ckm-"))
        self.assertIn("adjacent same-work continuation", skipped[0]["reason"])


if __name__ == "__main__":
    unittest.main()
