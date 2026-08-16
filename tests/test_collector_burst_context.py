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
import urllib.error
from pathlib import Path
from unittest import mock

from scripts import collector_checkpoints as checkpoints


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

    def test_runtime_identity_ignores_untracked_sync_and_state_artifacts(self) -> None:
        responses = [
            mock.Mock(returncode=0, stdout=str(collector.ROOT) + "\n"),
            mock.Mock(returncode=0, stdout="7aa201af\n"),
            mock.Mock(returncode=0, stdout=""),
        ]
        with mock.patch.object(collector.subprocess, "run", side_effect=responses) as run:
            identity = collector.collector_runtime_identity()

        self.assertEqual("7aa201af", identity["git_sha"])
        self.assertFalse(identity["git_dirty"])
        self.assertEqual(
            [
                "git",
                "-C",
                str(collector.ROOT),
                "status",
                "--porcelain",
                "--untracked-files=no",
            ],
            run.call_args_list[2].args[0],
        )

    def test_runtime_identity_rejects_parent_git_repository_for_archive_bundle(self) -> None:
        parent_repo = collector.ROOT.parent
        with mock.patch.object(
            collector.subprocess,
            "run",
            return_value=mock.Mock(returncode=0, stdout=str(parent_repo) + "\n"),
        ) as run:
            identity = collector.collector_runtime_identity()

        self.assertIsNone(identity["git_sha"])
        self.assertIsNone(identity["git_dirty"])
        self.assertEqual(1, run.call_count)
        self.assertEqual(
            ["git", "-C", str(collector.ROOT), "rev-parse", "--show-toplevel"],
            run.call_args.args[0],
        )

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
            self.assertIn("ZoneInfo('Europe/Bucharest')", generated)
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
            return {"issues": [{
                "id": "issue-id", "key": "SER-1", "title": "Test",
                "description": "Rebuild evidence accounting", "status": "open",
                "updatedAt": "2026-08-01T10:00:00Z", "labels": ["clockify"],
            }]}

        with mock.patch.dict(collector.os.environ, env, clear=True), mock.patch.object(
            collector, "_home_candidates", side_effect=AssertionError("profile lookup must not run")
        ), mock.patch.object(collector, "http_json", side_effect=fake_http_json):
            result = collector.fetch_multica_issues()

        self.assertEqual("ok", result["status"])
        self.assertEqual(
            "https://multica.example.test/api/issues?limit=100&offset=0",
            captured["url"],
        )
        self.assertEqual(
            {"Authorization": "Bearer multica-test-token", "X-Workspace-ID": "workspace-id"},
            captured["headers"],
        )
        self.assertEqual("Rebuild evidence accounting", result["issues"][0]["description"])
        self.assertEqual("2026-08-01T10:00:00Z", result["issues"][0]["updated_at"])
        self.assertEqual(["clockify"], result["issues"][0]["labels"])
        self.assertTrue(result["complete"])

    def test_multica_issue_collection_paginates_past_one_hundred(self) -> None:
        env = {
            "MULTICA_TOKEN": "test-token",
            "MULTICA_SERVER_URL": "https://multica.example.test",
            "MULTICA_WORKSPACE_ID": "workspace-id",
        }
        calls = []

        def fake_http_json(url, headers):
            calls.append(url)
            if "offset=0" in url:
                return {"issues": [{"id": f"issue-{index}"} for index in range(100)]}
            if "offset=100" in url:
                return {"issues": [{"id": "issue-100"}]}
            raise AssertionError(url)

        with mock.patch.dict(collector.os.environ, env, clear=True), mock.patch.object(
            collector, "_home_candidates", side_effect=AssertionError("profile lookup must not run")
        ), mock.patch.object(collector, "http_json", side_effect=fake_http_json):
            result = collector.fetch_multica_issues()

        self.assertEqual("ok", result["status"])
        self.assertEqual(101, len(result["issues"]))
        self.assertEqual(2, result["pages_fetched"])
        self.assertTrue(result["complete"])
        self.assertTrue(any("offset=100" in call for call in calls))

    def test_multica_retry_resumes_bound_endpoint_and_offset(self) -> None:
        env = {
            "MULTICA_TOKEN": "test-token",
            "MULTICA_SERVER_URL": "https://multica.example.test",
            "MULTICA_WORKSPACE_ID": "workspace-id",
        }
        first_page = {"issues": [{"id": f"issue-{index}"} for index in range(100)]}
        final_page = {"issues": [{"id": "issue-100"}]}
        resumed_calls: list[str] = []

        def interrupted_http(url: str, _headers: dict[str, str]) -> dict[str, list[dict[str, str]]]:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            self.assertIn("/api/issues", url)
            if query["offset"] == ["0"]:
                return first_page
            self.assertEqual(["100"], query["offset"])
            raise OSError("offline")

        def resumed_http(url: str, _headers: dict[str, str]) -> dict[str, list[dict[str, str]]]:
            resumed_calls.append(url)
            self.assertEqual("/api/issues", urllib.parse.urlsplit(url).path)
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            self.assertEqual(["100"], query["offset"])
            return final_page

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            collector.os.environ, env, clear=True
        ), mock.patch.object(
            collector, "_home_candidates", side_effect=AssertionError("profile lookup must not run")
        ):
            store = checkpoints.PageCheckpointStore(Path(directory))
            with mock.patch.object(collector, "http_json", side_effect=interrupted_http):
                interrupted = collector.fetch_multica_issues(checkpoint_store=store)
            self.assertFalse(interrupted["complete"])
            self.assertEqual([], interrupted["issues"])

            with mock.patch.object(collector, "http_json", side_effect=resumed_http):
                resumed = collector.fetch_multica_issues(checkpoint_store=store)

        self.assertTrue(resumed["complete"])
        self.assertEqual(2, resumed["pages_fetched"])
        self.assertEqual([f"issue-{index}" for index in range(101)], [
            issue["id"] for issue in resumed["issues"]
        ])
        self.assertEqual(1, len(resumed_calls))
        self.assertIn("offset=100", resumed_calls[0])

    def test_multica_complete_checkpoint_replay_skips_http(self) -> None:
        env = {
            "MULTICA_TOKEN": "test-token",
            "MULTICA_SERVER_URL": "https://multica.example.test",
            "MULTICA_WORKSPACE_ID": "workspace-id",
        }
        page = {"issues": [{"id": "completed-issue"}]}

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            collector.os.environ, env, clear=True
        ), mock.patch.object(
            collector, "_home_candidates", side_effect=AssertionError("profile lookup must not run")
        ):
            store = checkpoints.PageCheckpointStore(Path(directory))
            with mock.patch.object(collector, "http_json", return_value=page):
                collected = collector.fetch_multica_issues(checkpoint_store=store)
            with mock.patch.object(collector, "http_json") as http:
                replayed = collector.fetch_multica_issues(checkpoint_store=store)

        self.assertEqual(collected, replayed)
        http.assert_not_called()

    def test_multica_checkpoint_rejects_endpoint_mismatch_before_http(self) -> None:
        env = {
            "MULTICA_TOKEN": "test-token",
            "MULTICA_SERVER_URL": "https://multica.example.test",
            "MULTICA_WORKSPACE_ID": "workspace-id",
        }

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            collector.os.environ, env, clear=True
        ), mock.patch.object(
            collector, "_home_candidates", side_effect=AssertionError("profile lookup must not run")
        ):
            store = checkpoints.PageCheckpointStore(Path(directory))
            identity = collector._multica_checkpoint_identity(
                "https://multica.example.test", "workspace-id", None, None, "/api/issues"
            )
            store.open(identity, initial_metadata={"endpoint_path": "/issues"})

            with mock.patch.object(collector, "http_json") as http:
                result = collector.fetch_multica_issues(checkpoint_store=store)

        self.assertEqual("error", result["status"])
        self.assertFalse(result["complete"])
        self.assertEqual([], result["issues"])
        http.assert_not_called()

    def test_multica_checkpoint_identity_does_not_store_credentials(self) -> None:
        identity = collector._multica_checkpoint_identity(
            collector._multica_server_origin(
                "https://private-user:private-password@multica.example.test/base"
            ),
            "workspace-id",
            SINCE,
            UNTIL,
            "/api/issues",
        )
        different_origin = collector._multica_checkpoint_identity(
            "https://other.example.test", "workspace-id", SINCE, UNTIL, "/api/issues"
        )

        serialized = json.dumps(identity.document(), sort_keys=True)
        self.assertNotEqual(identity.request_fingerprint, different_origin.request_fingerprint)
        self.assertNotIn("private-user", serialized)
        self.assertNotIn("private-password", serialized)

    def test_multica_corrupt_checkpoint_fails_closed_before_http(self) -> None:
        env = {
            "MULTICA_TOKEN": "test-token",
            "MULTICA_SERVER_URL": "https://multica.example.test",
            "MULTICA_WORKSPACE_ID": "workspace-id",
        }
        page = {"issues": [{"id": "completed-issue"}]}

        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(
            collector.os.environ, env, clear=True
        ), mock.patch.object(
            collector, "_home_candidates", side_effect=AssertionError("profile lookup must not run")
        ):
            root = Path(directory)
            store = checkpoints.PageCheckpointStore(root)
            with mock.patch.object(collector, "http_json", return_value=page):
                collector.fetch_multica_issues(checkpoint_store=store)
            page_path = next(
                path / "pages" / "000001.json"
                for path in root.iterdir()
                if (path / "pages" / "000001.json").exists()
            )
            saved_page = json.loads(page_path.read_text())
            saved_page["payload"] = {"issues": [{"id": "tampered"}]}
            page_path.write_text(json.dumps(saved_page))

            with mock.patch.object(collector, "http_json") as http:
                result = collector.fetch_multica_issues(checkpoint_store=store)

        self.assertEqual("error", result["status"])
        self.assertFalse(result["complete"])
        self.assertEqual([], result["issues"])
        http.assert_not_called()

    def test_multica_issue_collection_filters_to_requested_activity_window(self) -> None:
        env = {
            "MULTICA_TOKEN": "test-token",
            "MULTICA_SERVER_URL": "https://multica.example.test",
            "MULTICA_WORKSPACE_ID": "workspace-id",
        }
        items = [
            {"id": "in", "updated_at": "2026-07-21T10:00:00Z"},
            {"id": "old", "updated_at": "2026-06-20T10:00:00Z"},
            {"id": "undated"},
        ]
        with mock.patch.dict(collector.os.environ, env, clear=True), mock.patch.object(
            collector, "_home_candidates", side_effect=AssertionError("profile lookup must not run")
        ), mock.patch.object(
            collector, "http_json", return_value={"issues": items}
        ):
            result = collector.fetch_multica_issues(SINCE, UNTIL)

        self.assertEqual(["in"], [issue["id"] for issue in result["issues"]])
        self.assertEqual(SINCE.isoformat(), result["activity_window"]["since"])
        self.assertEqual(UNTIL.isoformat(), result["activity_window"]["until"])

    def test_clockify_collection_paginates_all_existing_fixed_blocks(self) -> None:
        calls = []

        def fake_get(path, env):
            calls.append(path)
            if "page=1&" in path:
                return [
                    {
                        "id": f"clockify-{index:03d}",
                        "timeInterval": {
                            "start": "2026-07-21T07:00:00Z",
                            "end": "2026-07-21T07:05:00Z",
                        },
                    }
                    for index in range(200)
                ]
            if "page=2&" in path:
                return [
                    {
                        "id": "clockify-200",
                        "timeInterval": {
                            "start": "2026-07-21T08:00:00Z",
                            "end": "2026-07-21T08:05:00Z",
                        },
                    }
                ]
            raise AssertionError(path)

        with mock.patch.object(collector, "clockify_get", side_effect=fake_get):
            result = collector.fetch_clockify(
                {"CLOCKIFY_WORKSPACE_ID": "workspace", "CLOCKIFY_API_KEY": "not-logged"},
                {"clockify_user_id": "user"},
                SINCE,
                UNTIL,
            )

        self.assertEqual("ok", result["status"])
        self.assertTrue(result["complete"])
        self.assertEqual(2, result["pages_fetched"])
        self.assertEqual(201, len(result["entries"]))
        self.assertTrue(any("page=2&" in call for call in calls))

    def test_clockify_retry_resumes_after_persisted_page(self) -> None:
        first_page = [
            {
                "id": f"clockify-{index:03d}",
                "timeInterval": {
                    "start": "2026-07-21T07:00:00Z",
                    "end": "2026-07-21T07:05:00Z",
                },
            }
            for index in range(200)
        ]
        second_page = [{
            "id": "clockify-200",
            "timeInterval": {
                "start": "2026-07-21T08:00:00Z",
                "end": "2026-07-21T08:05:00Z",
            },
        }]
        env = {"CLOCKIFY_WORKSPACE_ID": "workspace", "CLOCKIFY_API_KEY": "not-logged"}
        routing = {"clockify_user_id": "user"}
        observed_at = dt.datetime(2026, 7, 21, 8, 15, tzinfo=TZ)

        def interrupted_get(path, _env):
            if "page=1&" in path:
                return first_page
            if "page=2&" in path:
                raise OSError("offline")
            raise AssertionError(path)

        def resumed_get(path, _env):
            if "page=1&" in path:
                raise AssertionError("resume requested an already persisted page")
            if "page=2&" in path:
                return second_page
            raise AssertionError(path)

        def uninterrupted_get(path, _env):
            if "page=1&" in path:
                return first_page
            if "page=2&" in path:
                return second_page
            raise AssertionError(path)

        with tempfile.TemporaryDirectory() as directory:
            store = checkpoints.PageCheckpointStore(Path(directory))
            with mock.patch.object(collector, "clockify_get", side_effect=interrupted_get):
                interrupted = collector.fetch_clockify(
                    env,
                    routing,
                    SINCE,
                    UNTIL,
                    snapshot_at=observed_at,
                    checkpoint_store=store,
                )
            self.assertFalse(interrupted["complete"])
            self.assertEqual([], interrupted["entries"])

            with mock.patch.object(collector, "clockify_get", side_effect=resumed_get):
                resumed = collector.fetch_clockify(
                    env,
                    routing,
                    SINCE,
                    UNTIL,
                    checkpoint_store=store,
                )

        with mock.patch.object(collector, "clockify_get", side_effect=uninterrupted_get):
            uninterrupted = collector.fetch_clockify(
                env,
                routing,
                SINCE,
                UNTIL,
                snapshot_at=observed_at,
            )

        self.assertTrue(resumed["complete"])
        self.assertEqual(201, len(resumed["entries"]))
        self.assertEqual(2, resumed["pages_fetched"])
        self.assertEqual(
            json.dumps(resumed, sort_keys=True, separators=(",", ":")).encode(),
            json.dumps(uninterrupted, sort_keys=True, separators=(",", ":")).encode(),
        )

    def test_clockify_complete_checkpoint_replay_skips_http(self) -> None:
        entry = [{
            "id": "completed-entry",
            "timeInterval": {
                "start": "2026-07-21T07:00:00Z",
                "end": "2026-07-21T07:05:00Z",
            },
        }]
        env = {"CLOCKIFY_WORKSPACE_ID": "workspace", "CLOCKIFY_API_KEY": "not-logged"}
        routing = {"clockify_user_id": "user"}
        observed_at = dt.datetime(2026, 7, 21, 8, 15, tzinfo=TZ)

        with tempfile.TemporaryDirectory() as directory:
            store = checkpoints.PageCheckpointStore(Path(directory))
            with mock.patch.object(collector, "clockify_get", return_value=entry):
                collected = collector.fetch_clockify(
                    env,
                    routing,
                    SINCE,
                    UNTIL,
                    snapshot_at=observed_at,
                    checkpoint_store=store,
                )

            with mock.patch.object(collector, "clockify_get") as clockify_get:
                replayed = collector.fetch_clockify(
                    env,
                    routing,
                    SINCE,
                    UNTIL,
                    checkpoint_store=store,
                )

        self.assertTrue(collected["complete"])
        self.assertEqual(collected, replayed)
        clockify_get.assert_not_called()

    def test_clockify_corrupt_checkpoint_fails_closed_before_http(self) -> None:
        entry = [{
            "id": "completed-entry",
            "timeInterval": {
                "start": "2026-07-21T07:00:00Z",
                "end": "2026-07-21T07:05:00Z",
            },
        }]
        env = {"CLOCKIFY_WORKSPACE_ID": "workspace", "CLOCKIFY_API_KEY": "not-logged"}
        routing = {"clockify_user_id": "user"}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = checkpoints.PageCheckpointStore(root)
            with mock.patch.object(collector, "clockify_get", return_value=entry):
                collector.fetch_clockify(
                    env,
                    routing,
                    SINCE,
                    UNTIL,
                    checkpoint_store=store,
                )
            page_path = next(root.iterdir()) / "pages" / "000001.json"
            page = json.loads(page_path.read_text())
            page["payload"] = [{"id": "tampered"}]
            page_path.write_text(json.dumps(page))

            with mock.patch.object(collector, "clockify_get") as clockify_get:
                corrupted = collector.fetch_clockify(
                    env,
                    routing,
                    SINCE,
                    UNTIL,
                    checkpoint_store=store,
                )

        self.assertFalse(corrupted["complete"])
        self.assertEqual([], corrupted["entries"])
        clockify_get.assert_not_called()

    def test_clockify_checkpoint_with_skipped_continuation_fails_closed(self) -> None:
        env = {"CLOCKIFY_WORKSPACE_ID": "workspace", "CLOCKIFY_API_KEY": "not-logged"}
        routing = {"clockify_user_id": "user"}
        page = [{"id": "first-entry"}]

        with tempfile.TemporaryDirectory() as directory:
            store = checkpoints.PageCheckpointStore(Path(directory))
            identity = collector._clockify_checkpoint_identity(
                "workspace", "user", SINCE, UNTIL
            )
            state = store.open(
                identity,
                initial_metadata={"snapshot_at": "2026-07-21T05:15:00Z"},
            )
            store.append_page(
                state,
                payload=page,
                continuation={"page": 3},
                signature=collector._clockify_page_signature(page),
            )

            with mock.patch.object(collector, "clockify_get") as clockify_get:
                corrupted = collector.fetch_clockify(
                    env,
                    routing,
                    SINCE,
                    UNTIL,
                    checkpoint_store=store,
                )

        self.assertEqual("error", corrupted["status"])
        self.assertFalse(corrupted["complete"])
        self.assertEqual([], corrupted["entries"])
        clockify_get.assert_not_called()

    def test_clockify_completed_checkpoint_with_full_final_page_fails_closed(self) -> None:
        env = {"CLOCKIFY_WORKSPACE_ID": "workspace", "CLOCKIFY_API_KEY": "not-logged"}
        routing = {"clockify_user_id": "user"}
        full_page = [{"id": f"entry-{index:03d}"} for index in range(200)]

        with tempfile.TemporaryDirectory() as directory:
            store = checkpoints.PageCheckpointStore(Path(directory))
            identity = collector._clockify_checkpoint_identity(
                "workspace", "user", SINCE, UNTIL
            )
            state = store.open(
                identity,
                initial_metadata={"snapshot_at": "2026-07-21T05:15:00Z"},
            )
            state = store.append_page(
                state,
                payload=full_page,
                continuation={"page": 2},
                signature=collector._clockify_page_signature(full_page),
            )
            store.mark_complete(state)

            with mock.patch.object(collector, "clockify_get") as clockify_get:
                corrupted = collector.fetch_clockify(
                    env,
                    routing,
                    SINCE,
                    UNTIL,
                    checkpoint_store=store,
                )

        self.assertEqual("error", corrupted["status"])
        self.assertFalse(corrupted["complete"])
        self.assertEqual([], corrupted["entries"])
        clockify_get.assert_not_called()

    def test_clockify_pagination_failure_marks_source_incomplete(self) -> None:
        with mock.patch.object(collector, "clockify_get", side_effect=OSError("offline")):
            result = collector.fetch_clockify(
                {"CLOCKIFY_WORKSPACE_ID": "workspace", "CLOCKIFY_API_KEY": "not-logged"},
                {"clockify_user_id": "user"},
                SINCE,
                UNTIL,
            )
        self.assertEqual("error", result["status"])
        self.assertFalse(result["complete"])
        self.assertEqual([], result["entries"])

    def test_running_clockify_entry_becomes_snapshot_bounded_existing_block(self) -> None:
        def fake_get(path, env):
            self.assertIn("page=1&", path)
            return [
                {
                    "id": "completed-entry",
                    "timeInterval": {
                        "start": "2026-07-21T07:00:00Z",
                        "end": "2026-07-21T07:30:00Z",
                    },
                },
                {
                    "id": "running-entry",
                    "timeInterval": {
                        "start": "2026-07-21T04:00:00Z",
                        "end": None,
                    },
                },
            ]

        with mock.patch.object(collector, "clockify_get", side_effect=fake_get):
            result = collector.fetch_clockify(
                {"CLOCKIFY_WORKSPACE_ID": "workspace", "CLOCKIFY_API_KEY": "not-logged"},
                {"clockify_user_id": "user"},
                SINCE,
                UNTIL,
                snapshot_at=dt.datetime(2026, 7, 21, 8, 15, tzinfo=TZ),
            )

        self.assertEqual("ok", result["status"])
        self.assertTrue(result["complete"])
        self.assertEqual(1, result["running_entry_count"])
        self.assertEqual(1, result["running_entry_snapshot_count"])
        running = next(entry for entry in result["entries"] if entry["id_suffix"] == "ng-entry")
        self.assertEqual("2026-07-21 08:15", running["end"])
        self.assertTrue(running["running"])
        self.assertEqual("collection_snapshot_boundary", running["running_snapshot"]["basis"])
        self.assertEqual("2026-07-21T05:15:00Z", running["running_snapshot"]["boundary"])
        self.assertEqual("2026-07-21T05:15:00Z", result["collection_snapshot"]["boundary"])

    def test_running_clockify_snapshot_never_exceeds_requested_until(self) -> None:
        with mock.patch.object(collector, "clockify_get", return_value=[{
            "id": "running-entry",
            "timeInterval": {"start": "2026-07-21T04:00:00Z", "end": None},
        }]):
            result = collector.fetch_clockify(
                {"CLOCKIFY_WORKSPACE_ID": "workspace", "CLOCKIFY_API_KEY": "not-logged"},
                {"clockify_user_id": "user"},
                SINCE,
                UNTIL,
                snapshot_at=dt.datetime(2026, 7, 23, tzinfo=TZ),
            )

        running = result["entries"][0]
        self.assertEqual("2026-07-22 00:00", running["end"])
        self.assertEqual("2026-07-21T21:00:00Z", running["running_snapshot"]["boundary"])
        self.assertEqual("2026-07-21T21:00:00Z", result["collection_snapshot"]["boundary"])

    def test_unbounded_running_clockify_entry_remains_partial(self) -> None:
        with mock.patch.object(collector, "clockify_get", return_value=[{
            "id": "future-running-entry",
            "timeInterval": {"start": "2026-07-21T06:00:00Z", "end": None},
        }]):
            result = collector.fetch_clockify(
                {"CLOCKIFY_WORKSPACE_ID": "workspace", "CLOCKIFY_API_KEY": "not-logged"},
                {"clockify_user_id": "user"},
                SINCE,
                UNTIL,
                snapshot_at=dt.datetime(2026, 7, 21, 8, 15, tzinfo=TZ),
            )

        self.assertEqual("partial", result["status"])
        self.assertFalse(result["complete"])
        self.assertEqual(1, result["running_entry_count"])
        self.assertEqual(0, result["running_entry_snapshot_count"])
        self.assertIsNone(result["entries"][0]["end"])
        self.assertIsNone(result["entries"][0]["running_snapshot"])

    def test_fathom_429_retries_using_retry_after_without_network(self) -> None:
        rate_limit = urllib.error.HTTPError(
            "https://fathom.example.test/meetings", 429, "rate limited",
            {"Retry-After": "7"}, None,
        )
        with (
            mock.patch.object(collector, "http_json", side_effect=[rate_limit, {"items": []}]) as http,
            mock.patch.object(collector.time, "sleep") as sleep,
        ):
            result = collector.fetch_fathom(
                {"FATHOM_API_KEY": "not-logged"}, SINCE, UNTIL
            )
        rate_limit.close()

        self.assertEqual("ok", result["status"])
        self.assertTrue(result["complete"])
        self.assertEqual(2, http.call_count)
        sleep.assert_called_once_with(7)
        self.assertEqual(1, result["retry_count"])
        self.assertEqual([7], result["retry_delays_seconds"])

    def test_fathom_429_exhaustion_fails_closed_without_network(self) -> None:
        rate_limit = urllib.error.HTTPError(
            "https://fathom.example.test/meetings", 429, "rate limited", {}, None
        )
        with (
            mock.patch.object(collector, "http_json", side_effect=[rate_limit, rate_limit, rate_limit]) as http,
            mock.patch.object(collector.time, "sleep") as sleep,
        ):
            result = collector.fetch_fathom(
                {"FATHOM_API_KEY": "not-logged"}, SINCE, UNTIL
            )
        rate_limit.close()

        self.assertEqual("error", result["status"])
        self.assertFalse(result["complete"])
        self.assertEqual(3, http.call_count)
        self.assertEqual([mock.call(1), mock.call(2)], sleep.call_args_list)

    def test_fathom_collection_retry_budget_cannot_multiply_across_pages(self) -> None:
        rate_limit = urllib.error.HTTPError(
            "https://fathom.example.test/meetings", 429, "rate limited", {}, None
        )
        responses = [
            rate_limit,
            {"items": [], "next_cursor": "next-page"},
            rate_limit,
        ]
        with (
            mock.patch.object(collector, "http_json", side_effect=responses) as http,
            mock.patch.object(collector.time, "sleep") as sleep,
            mock.patch.object(collector, "FATHOM_MAX_COLLECTION_RETRIES", 1),
        ):
            result = collector.fetch_fathom(
                {"FATHOM_API_KEY": "not-logged"}, SINCE, UNTIL
            )
        rate_limit.close()

        self.assertEqual("error", result["status"])
        self.assertFalse(result["complete"])
        self.assertEqual("collection_retry_count_exhausted", result["error"])
        self.assertEqual(3, http.call_count)
        sleep.assert_called_once_with(1)
        self.assertEqual(2, result["failure"]["page"])
        self.assertEqual("sha256:" + collector.hashlib.sha256(b"next-page").hexdigest()[:12], result["failure"]["cursor"])
        self.assertEqual(1, result["failure"]["retry_count"])
        self.assertEqual([1], result["failure"]["retry_delays_seconds"])

    def test_fathom_retry_resumes_from_private_cursor(self) -> None:
        first_page = {
            "items": [{
                "id": "first-meeting",
                "recording_start_time": "2026-07-21T05:00:00Z",
                "recording_end_time": "2026-07-21T05:30:00Z",
            }],
            "next_cursor": "private-next",
        }
        second_page = {
            "items": [{
                "id": "second-meeting",
                "recording_start_time": "2026-07-21T06:00:00Z",
                "recording_end_time": "2026-07-21T06:30:00Z",
            }],
        }
        env = {"FATHOM_API_KEY": "not-logged"}

        def interrupted_http(url, _headers):
            cursor = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("cursor")
            if cursor is None:
                return first_page
            self.assertEqual(["private-next"], cursor)
            raise OSError("offline")

        def resumed_http(url, _headers):
            cursor = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("cursor")
            self.assertEqual(["private-next"], cursor)
            return second_page

        def uninterrupted_http(url, _headers):
            cursor = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("cursor")
            return second_page if cursor else first_page

        with tempfile.TemporaryDirectory() as directory:
            store = checkpoints.PageCheckpointStore(Path(directory))
            with mock.patch.object(collector, "http_json", side_effect=interrupted_http):
                interrupted = collector.fetch_fathom(
                    env, SINCE, UNTIL, checkpoint_store=store
                )

            self.assertFalse(interrupted["complete"])
            self.assertEqual([], interrupted["meetings"])
            self.assertEqual(
                collector._fathom_cursor_reference("private-next"),
                interrupted["failure"]["cursor"],
            )
            self.assertNotIn("private-next", json.dumps(interrupted))

            with mock.patch.object(collector, "http_json", side_effect=resumed_http):
                resumed = collector.fetch_fathom(
                    env, SINCE, UNTIL, checkpoint_store=store
                )

        with mock.patch.object(collector, "http_json", side_effect=uninterrupted_http):
            uninterrupted = collector.fetch_fathom(env, SINCE, UNTIL)

        self.assertTrue(resumed["complete"])
        self.assertEqual(2, resumed["pages_fetched"])
        self.assertEqual(["first-meeting", "second-meeting"], [
            meeting["recording_id"] for meeting in resumed["meetings"]
        ])
        self.assertEqual(
            json.dumps(uninterrupted, sort_keys=True, separators=(",", ":")).encode(),
            json.dumps(resumed, sort_keys=True, separators=(",", ":")).encode(),
        )

    def test_fathom_complete_checkpoint_replay_skips_http(self) -> None:
        page = {
            "items": [{
                "id": "completed-meeting",
                "recording_start_time": "2026-07-21T05:00:00Z",
                "recording_end_time": "2026-07-21T05:30:00Z",
            }],
        }
        env = {"FATHOM_API_KEY": "not-logged"}

        with tempfile.TemporaryDirectory() as directory:
            store = checkpoints.PageCheckpointStore(Path(directory))
            with mock.patch.object(collector, "http_json", return_value=page):
                collected = collector.fetch_fathom(
                    env, SINCE, UNTIL, checkpoint_store=store
                )
            with mock.patch.object(collector, "http_json") as http:
                replayed = collector.fetch_fathom(
                    env, SINCE, UNTIL, checkpoint_store=store
                )

        self.assertEqual(collected, replayed)
        http.assert_not_called()

    def test_fathom_resume_rejects_repeated_private_cursor(self) -> None:
        first_page = {"items": [], "next_cursor": "private-next"}
        repeated_page = {"items": [], "next_cursor": "private-next"}
        env = {"FATHOM_API_KEY": "not-logged"}

        with tempfile.TemporaryDirectory() as directory:
            store = checkpoints.PageCheckpointStore(Path(directory))
            with mock.patch.object(
                collector, "http_json", side_effect=[first_page, OSError("offline")]
            ):
                collector.fetch_fathom(env, SINCE, UNTIL, checkpoint_store=store)
            with mock.patch.object(collector, "http_json", return_value=repeated_page):
                resumed = collector.fetch_fathom(
                    env, SINCE, UNTIL, checkpoint_store=store
                )

        self.assertEqual("error", resumed["status"])
        self.assertFalse(resumed["complete"])
        self.assertEqual([], resumed["meetings"])
        self.assertEqual(
            collector._fathom_cursor_reference("private-next"),
            resumed["failure"]["cursor"],
        )
        self.assertNotIn("private-next", json.dumps(resumed))

    def test_fathom_corrupt_checkpoint_fails_closed_before_http(self) -> None:
        page = {"items": []}
        env = {"FATHOM_API_KEY": "not-logged"}

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = checkpoints.PageCheckpointStore(root)
            with mock.patch.object(collector, "http_json", return_value=page):
                collector.fetch_fathom(env, SINCE, UNTIL, checkpoint_store=store)
            page_path = next(root.iterdir()) / "pages" / "000001.json"
            saved_page = json.loads(page_path.read_text())
            saved_page["payload"] = {"items": [{"id": "tampered"}]}
            page_path.write_text(json.dumps(saved_page))

            with mock.patch.object(collector, "http_json") as http:
                corrupted = collector.fetch_fathom(
                    env, SINCE, UNTIL, checkpoint_store=store
                )

        self.assertEqual("error", corrupted["status"])
        self.assertFalse(corrupted["complete"])
        self.assertEqual([], corrupted["meetings"])
        http.assert_not_called()

    def test_fathom_unfinished_terminal_checkpoint_fails_closed_before_http(self) -> None:
        page = {"items": []}
        env = {"FATHOM_API_KEY": "not-logged"}

        with tempfile.TemporaryDirectory() as directory:
            store = checkpoints.PageCheckpointStore(Path(directory))
            state = store.open(collector._fathom_checkpoint_identity(SINCE, UNTIL))
            store.append_page(
                state,
                payload=page,
                continuation={"cursor": None},
                signature=collector._fathom_page_signature([]),
            )

            with mock.patch.object(collector, "http_json") as http:
                corrupted = collector.fetch_fathom(
                    env, SINCE, UNTIL, checkpoint_store=store
                )

        self.assertEqual("error", corrupted["status"])
        self.assertFalse(corrupted["complete"])
        self.assertEqual([], corrupted["meetings"])
        http.assert_not_called()

    def test_fathom_checkpoint_rejects_out_of_sequence_request_cursor_before_http(self) -> None:
        first_page = {"items": [], "next_cursor": "private-first-next"}
        second_page = {"items": [], "next_cursor": "private-second-next"}
        env = {"FATHOM_API_KEY": "not-logged"}

        with tempfile.TemporaryDirectory() as directory:
            store = checkpoints.PageCheckpointStore(Path(directory))
            state = store.open(collector._fathom_checkpoint_identity(SINCE, UNTIL))
            state = store.append_page(
                state,
                payload=first_page,
                continuation={"cursor": "private-first-next"},
                signature=collector._fathom_page_signature([]),
                metadata={"request_cursor": None},
            )
            store.append_page(
                state,
                payload=second_page,
                continuation={"cursor": "private-second-next"},
                signature=collector._fathom_page_signature([]),
                metadata={"request_cursor": "private-out-of-sequence"},
            )

            with mock.patch.object(collector, "http_json") as http:
                corrupted = collector.fetch_fathom(
                    env, SINCE, UNTIL, checkpoint_store=store
                )

        self.assertEqual("error", corrupted["status"])
        self.assertFalse(corrupted["complete"])
        self.assertEqual([], corrupted["meetings"])
        self.assertNotIn("private-out-of-sequence", json.dumps(corrupted))
        http.assert_not_called()

    def test_fathom_retry_deadline_fails_closed_before_sleep(self) -> None:
        rate_limit = urllib.error.HTTPError(
            "https://fathom.example.test/meetings", 429, "rate limited",
            {"Retry-After": "7"}, None,
        )
        with (
            mock.patch.object(collector, "http_json", side_effect=rate_limit) as http,
            mock.patch.object(collector.time, "sleep") as sleep,
            mock.patch.object(collector, "FATHOM_COLLECTION_RETRY_DEADLINE_SECONDS", 5),
        ):
            result = collector.fetch_fathom(
                {"FATHOM_API_KEY": "not-logged"}, SINCE, UNTIL
            )
        rate_limit.close()

        self.assertEqual("error", result["status"])
        self.assertEqual("collection_retry_deadline_exhausted", result["error"])
        self.assertEqual(1, http.call_count)
        sleep.assert_not_called()
        self.assertEqual("initial", result["failure"]["cursor"])
        self.assertEqual(0, result["failure"]["retry_count"])

    def test_fathom_month_scale_deadline_allows_many_ordinary_pages(self) -> None:
        calls = 0

        def page_response(_url, _headers):
            nonlocal calls
            calls += 1
            return {"items": [], "next_cursor": f"page-{calls}"} if calls < 32 else {"items": []}

        with (
            mock.patch.object(collector, "http_json", side_effect=page_response),
            mock.patch.object(collector.time, "monotonic", side_effect=[0] + [181] * 32),
        ):
            result = collector.fetch_fathom(
                {"FATHOM_API_KEY": "not-logged"}, SINCE, UNTIL
            )

        self.assertEqual("ok", result["status"])
        self.assertTrue(result["complete"])
        self.assertEqual(32, result["pages_fetched"])
        self.assertEqual(0, result["retry_count"])
        self.assertEqual(1800, result["retry_policy"]["retry_deadline_seconds"])
        self.assertEqual(600, result["retry_policy"]["max_total_retry_delay_seconds"])

    def test_fathom_month_scale_deadline_still_bounds_collection(self) -> None:
        with (
            mock.patch.object(collector, "http_json") as http,
            mock.patch.object(collector.time, "monotonic", side_effect=[0, 1801]),
        ):
            result = collector.fetch_fathom(
                {"FATHOM_API_KEY": "not-logged"}, SINCE, UNTIL
            )

        self.assertEqual("error", result["status"])
        self.assertEqual("collection_retry_deadline_exhausted", result["error"])
        self.assertEqual(1, result["failure"]["page"])
        http.assert_not_called()

    def test_fathom_month_scale_retry_delay_budget_allows_live_backoff_pattern(self) -> None:
        budget = collector.FathomRetryBudget()
        with mock.patch.object(collector.time, "monotonic", return_value=0):
            budget.started_at = 0
            for delay in (38, 39, 40, 41):
                budget.reserve_retry(delay)

        self.assertEqual([38, 39, 40, 41], budget.retry_delays)
        self.assertEqual(600, budget.policy()["max_total_retry_delay_seconds"])

    def test_fathom_final_http_error_has_sanitized_failure_provenance(self) -> None:
        unavailable = urllib.error.HTTPError(
            "https://fathom.example.test/meetings?cursor=private", 503,
            "unavailable", {}, None,
        )
        with mock.patch.object(collector, "http_json", side_effect=unavailable):
            result = collector.fetch_fathom(
                {"FATHOM_API_KEY": "not-logged"}, SINCE, UNTIL
            )
        unavailable.close()

        self.assertEqual("error", result["status"])
        self.assertEqual("Fathom HTTP 503", result["error"])
        self.assertEqual(1, result["failure"]["page"])
        self.assertEqual("initial", result["failure"]["cursor"])
        self.assertEqual(0, result["failure"]["retry_count"])

    def test_canonical_export_attestation_accepts_matching_git_worktree(self) -> None:
        machine = {"name": "precision", "host": "precision.example.test", "collector_root": "/work/clockify"}
        exported = {
            "machine": "precision", "status": "ok", "claude_bursts": [],
            "hermes_sessions": [], "hermes_db_sessions": [], "codex_sessions": [],
            "repository_events": [], "repository_evidence_status": "complete", "errors": [],
            "canonical_export_attestation": {
                "collector_script_sha256": "expected-digest",
                "runtime_identity": {"git_sha": "coordinator-sha", "git_dirty": False},
            },
        }
        completed = collector.subprocess.CompletedProcess(["ssh"], 0, json.dumps(exported), "")
        with (
            mock.patch.object(collector, "collector_script_sha256", return_value="expected-digest"),
            mock.patch.object(collector.subprocess, "run", return_value=completed) as run,
        ):
            result = collector.collect_remote_sessions(
                machine, SINCE, UNTIL, [],
                coordinator_identity={"git_sha": "coordinator-sha", "git_dirty": False},
            )

        self.assertEqual("canonical_export_v1", result["collector_contract"])
        self.assertEqual("git_worktree", result["canonical_export"]["bundle_provenance"])
        command = run.call_args.args[0][-1]
        self.assertIn("--expected-collector-sha256 expected-digest", command)
        self.assertIn("--coordinator-git-sha coordinator-sha", command)
        self.assertIn("--encoded-output", command)

    def test_canonical_export_attestation_ignores_trailing_stdout_noise(self) -> None:
        machine = {"name": "precision", "host": "precision.example.test", "collector_root": "/work/clockify"}
        exported = {
            "machine": "precision", "status": "ok", "claude_bursts": [],
            "hermes_sessions": [], "hermes_db_sessions": [], "codex_sessions": [],
            "repository_events": [], "repository_evidence_status": "complete", "errors": [],
            "canonical_export_attestation": {
                "collector_script_sha256": "expected-digest",
                "runtime_identity": {"git_sha": "coordinator-sha", "git_dirty": False},
            },
        }
        stdout = "profile notice\n" + json.dumps(exported) + "\ntrailing status\n"
        completed = collector.subprocess.CompletedProcess(["ssh"], 0, stdout, "")
        with (
            mock.patch.object(collector, "collector_script_sha256", return_value="expected-digest"),
            mock.patch.object(collector.subprocess, "run", return_value=completed),
        ):
            result = collector.collect_remote_sessions(
                machine, SINCE, UNTIL, [],
                coordinator_identity={"git_sha": "coordinator-sha", "git_dirty": False},
            )

        self.assertEqual("canonical_export_v1", result["collector_contract"])

    def test_canonical_export_attestation_decodes_compressed_envelope_with_noise(self) -> None:
        machine = {"name": "precision", "host": "precision.example.test", "collector_root": "/work/clockify"}
        exported = {
            "machine": "precision", "status": "ok", "claude_bursts": [],
            "hermes_sessions": [], "hermes_db_sessions": [], "codex_sessions": [],
            "repository_events": [], "repository_evidence_status": "complete", "errors": [],
            "canonical_export_attestation": {
                "collector_script_sha256": "expected-digest",
                "runtime_identity": {"git_sha": "coordinator-sha", "git_dirty": False},
            },
        }
        envelope = collector.canonical_export_envelope(exported)
        self.assertLess(len(envelope), len(json.dumps(exported)))
        completed = collector.subprocess.CompletedProcess(
            ["ssh"], 0, "profile notice\n" + envelope + "\ntrailing status\n", ""
        )
        with (
            mock.patch.object(collector, "collector_script_sha256", return_value="expected-digest"),
            mock.patch.object(collector.subprocess, "run", return_value=completed),
        ):
            result = collector.collect_remote_sessions(
                machine, SINCE, UNTIL, [],
                coordinator_identity={"git_sha": "coordinator-sha", "git_dirty": False},
            )

        self.assertEqual("canonical_export_v1", result["collector_contract"])

    def test_canonical_export_attestation_fails_closed_without_matching_payload(self) -> None:
        machine = {"name": "precision", "host": "precision.example.test", "collector_root": "/work/clockify"}
        stdout = "session detail must not be surfaced\n" + json.dumps({"machine": "other"}) + "\n"
        completed = collector.subprocess.CompletedProcess(["ssh"], 0, stdout, "")
        with (
            mock.patch.object(collector, "collector_script_sha256", return_value="expected-digest"),
            mock.patch.object(collector.subprocess, "run", return_value=completed) as run,
        ):
            result = collector.collect_remote_sessions(
                machine, SINCE, UNTIL, [],
                coordinator_identity={"git_sha": "coordinator-sha", "git_dirty": False},
            )

        self.assertEqual("unavailable", result["status"])
        self.assertEqual(1, run.call_count)
        self.assertEqual("invalid_payload", result["canonical_export"]["status"])
        self.assertEqual(len(stdout.encode("utf-8")), result["canonical_export"]["stdout_bytes"])
        self.assertNotIn("session detail", " ".join(result["errors"]))

    def test_canonical_export_attestation_accepts_matching_non_git_bundle(self) -> None:
        machine = {"name": "desktop", "host": "desktop.example.test", "collector_root": "/work/clockify"}
        exported = {
            "machine": "desktop", "status": "ok", "claude_bursts": [],
            "hermes_sessions": [], "hermes_db_sessions": [], "codex_sessions": [],
            "repository_events": [], "repository_evidence_status": "complete", "errors": [],
            "canonical_export_attestation": {
                "collector_script_sha256": "expected-digest",
                "runtime_identity": {"git_sha": None, "git_dirty": None},
            },
        }
        completed = collector.subprocess.CompletedProcess(["ssh"], 0, json.dumps(exported), "")
        with (
            mock.patch.object(collector, "collector_script_sha256", return_value="expected-digest"),
            mock.patch.object(collector.subprocess, "run", return_value=completed),
        ):
            result = collector.collect_remote_sessions(
                machine, SINCE, UNTIL, [],
                coordinator_identity={"git_sha": "coordinator-sha", "git_dirty": False},
            )

        self.assertEqual("canonical_export_v1", result["collector_contract"])
        self.assertEqual("non_git_bundle", result["canonical_export"]["bundle_provenance"])
        self.assertIsNone(result["canonical_export"]["remote_git_sha"])

    def test_canonical_export_attestation_rejects_digest_mismatch_without_fallback(self) -> None:
        machine = {"name": "precision", "host": "precision.example.test", "collector_root": "/work/clockify"}
        exported = {
            "machine": "precision", "canonical_export_attestation": {
                "collector_script_sha256": "different-digest",
                "runtime_identity": {"git_sha": "coordinator-sha", "git_dirty": False},
            },
        }
        completed = collector.subprocess.CompletedProcess(["ssh"], 0, json.dumps(exported), "")
        with (
            mock.patch.object(collector, "collector_script_sha256", return_value="expected-digest"),
            mock.patch.object(collector.subprocess, "run", return_value=completed) as run,
        ):
            result = collector.collect_remote_sessions(
                machine, SINCE, UNTIL, [],
                coordinator_identity={"git_sha": "coordinator-sha", "git_dirty": False},
            )

        self.assertEqual("unavailable", result["status"])
        self.assertEqual(1, run.call_count)
        self.assertIn("script digest mismatch", result["errors"][0])

    def test_canonical_export_attestation_retries_allowlisted_exporter_digest(self) -> None:
        machine = {
            "name": "precision",
            "host": "precision.example.test",
            "collector_root": "/work/clockify",
        }
        compatible_digest = next(iter(
            collector.COMPATIBLE_CANONICAL_EXPORT_DIGESTS
        ))
        mismatch = {
            "machine": "precision",
            "status": "unavailable",
            "canonical_export_attestation": {
                "collector_script_sha256": compatible_digest,
                "runtime_identity": {
                    "git_sha": "approved-remote-sha",
                    "git_dirty": False,
                },
            },
        }
        exported = {
            "machine": "precision",
            "status": "ok",
            "claude_bursts": [],
            "hermes_sessions": [],
            "hermes_db_sessions": [],
            "codex_sessions": [],
            "repository_events": [],
            "repository_evidence_status": "complete",
            "errors": [],
            "canonical_export_attestation": mismatch[
                "canonical_export_attestation"
            ],
        }
        responses = [
            collector.subprocess.CompletedProcess(
                ["ssh"], 0, json.dumps(mismatch), ""
            ),
            collector.subprocess.CompletedProcess(
                ["ssh"], 0, json.dumps(exported), ""
            ),
        ]
        with (
            mock.patch.object(
                collector,
                "collector_script_sha256",
                return_value="new-coordinator-digest",
            ),
            mock.patch.object(
                collector.subprocess, "run", side_effect=responses
            ) as run,
        ):
            result = collector.collect_remote_sessions(
                machine,
                SINCE,
                UNTIL,
                [],
                coordinator_identity={
                    "git_sha": "coordinator-sha",
                    "git_dirty": True,
                },
            )

        self.assertEqual(2, run.call_count)
        self.assertEqual("ok", result["status"])
        self.assertEqual(
            compatible_digest,
            result["canonical_export"]["collector_script_sha256"],
        )
        self.assertFalse(
            result["canonical_export"]["collector_digest_match"]
        )
        self.assertFalse(result["canonical_export"]["git_sha_match"])

    def test_canonical_export_attestation_accepts_sha_drift_and_records_dirty_worktree(self) -> None:
        machine = {"name": "precision", "host": "precision.example.test", "collector_root": "/work/clockify"}
        clean_export = {
            "machine": "precision", "status": "ok", "claude_bursts": [],
            "hermes_sessions": [], "hermes_db_sessions": [], "codex_sessions": [],
            "repository_events": [], "repository_evidence_status": "complete", "errors": [],
            "canonical_export_attestation": {
                "collector_script_sha256": "expected-digest",
                "runtime_identity": {"git_sha": "other-sha", "git_dirty": False},
            },
        }
        completed = collector.subprocess.CompletedProcess(
            ["ssh"], 0, json.dumps(clean_export), ""
        )
        with (
            mock.patch.object(
                collector, "collector_script_sha256", return_value="expected-digest"
            ),
            mock.patch.object(collector.subprocess, "run", return_value=completed),
        ):
            result = collector.collect_remote_sessions(
                machine, SINCE, UNTIL, [],
                coordinator_identity={"git_sha": "coordinator-sha", "git_dirty": False},
            )
        self.assertEqual("ok", result["status"])
        self.assertEqual(
            "git_worktree_content_attested",
            result["canonical_export"]["bundle_provenance"],
        )
        self.assertFalse(result["canonical_export"]["git_sha_match"])

        dirty_export = {
            "machine": "precision", "status": "ok", "claude_bursts": [],
            "hermes_sessions": [], "hermes_db_sessions": [], "codex_sessions": [],
            "repository_events": [], "repository_evidence_status": "complete", "errors": [],
            "canonical_export_attestation": {
                "collector_script_sha256": "expected-digest",
                "runtime_identity": {"git_sha": "coordinator-sha", "git_dirty": True},
            },
        }
        completed = collector.subprocess.CompletedProcess(
            ["ssh"], 0, json.dumps(dirty_export), ""
        )
        with (
            mock.patch.object(
                collector, "collector_script_sha256", return_value="expected-digest"
            ),
            mock.patch.object(collector.subprocess, "run", return_value=completed) as run,
        ):
            result = collector.collect_remote_sessions(
                machine, SINCE, UNTIL, [],
                coordinator_identity={"git_sha": "coordinator-sha", "git_dirty": False},
            )
        self.assertEqual("ok", result["status"])
        self.assertEqual(1, run.call_count)
        self.assertEqual(
            "git_worktree_content_attested_dirty",
            result["canonical_export"]["bundle_provenance"],
        )
        self.assertTrue(result["canonical_export"]["remote_git_dirty"])

    def test_export_local_reports_its_digest_before_collecting_on_mismatch(self) -> None:
        machine = {"name": "desktop"}
        arguments = [
            "clockify_sync_collect.py", "export-local", "--machine-json", json.dumps(machine),
            "--since", SINCE.isoformat(), "--until", UNTIL.isoformat(),
            "--expected-collector-sha256", "coordinator-digest",
        ]
        output = io.StringIO()
        with (
            mock.patch.object(collector.sys, "argv", arguments),
            mock.patch.object(collector, "collector_script_sha256", return_value="remote-digest"),
            mock.patch.object(
                collector,
                "collector_runtime_identity",
                return_value={"git_sha": None, "git_dirty": None},
            ),
            mock.patch.object(collector, "collect_local_sessions") as collect,
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(0, collector.main())

        payload = json.loads(output.getvalue())
        collect.assert_not_called()
        self.assertEqual("unavailable", payload["status"])
        self.assertEqual("remote-digest", payload["canonical_export_attestation"]["collector_script_sha256"])

    def test_canonical_export_timeout_is_bounded_and_does_not_run_legacy_fallback(self) -> None:
        machine = {
            "name": "precision",
            "host": "precision.example.test",
            "collector_root": "/work/clockify",
        }
        with (
            mock.patch.dict(collector.os.environ, {"CLOCKIFY_CANONICAL_EXPORT_TIMEOUT_SECONDS": "600"}),
            mock.patch.object(
                collector.subprocess,
                "run",
                side_effect=collector.subprocess.TimeoutExpired(["ssh"], 600),
            ) as run,
        ):
            result = collector.collect_remote_sessions(
                machine,
                SINCE,
                UNTIL,
                [],
                coordinator_identity={"git_sha": "coordinator", "git_dirty": False},
            )

        self.assertEqual("unavailable", result["status"])
        self.assertEqual(1, run.call_count)
        self.assertEqual(600, run.call_args.kwargs["timeout"])
        self.assertEqual("timed_out", result["canonical_export"]["status"])
        self.assertIn("no legacy metadata fallback", result["errors"][0])

    def test_canonical_export_timeout_configuration_is_clamped(self) -> None:
        with mock.patch.dict(
            collector.os.environ,
            {"CLOCKIFY_CANONICAL_EXPORT_TIMEOUT_SECONDS": "999999"},
        ):
            self.assertEqual(1800, collector.canonical_export_timeout_seconds())
        with mock.patch.dict(
            collector.os.environ,
            {"CLOCKIFY_CANONICAL_EXPORT_TIMEOUT_SECONDS": "not-a-number"},
        ):
            self.assertEqual(900, collector.canonical_export_timeout_seconds())

    def test_repository_evidence_uses_only_commits_from_observed_session_cwds(self) -> None:
        session_result = {
            "machine": "macbook",
            "claude_bursts": [],
            "hermes_sessions": [],
            "hermes_db_sessions": [],
            "codex_sessions": [{"cwd": "/work/project"}],
        }
        responses = [
            mock.Mock(returncode=0, stdout="/work/project\n", stderr=""),
            mock.Mock(
                returncode=0,
                stdout=(
                    "\x1e"
                    + "a" * 40
                    + "\x1f2026-07-20T10:00:00+03:00"
                    + "\x1f2026-07-20T09:55:00+03:00"
                    + "\x1fFix stable review identity\n\n"
                    + "scripts/review.py\ntests/test_review.py\n"
                ),
                stderr="",
            ),
        ]
        with mock.patch.object(Path, "is_dir", return_value=True), mock.patch.object(
            collector.subprocess, "run", side_effect=responses
        ) as run:
            records, roots, errors = collector.collect_repository_events(
                session_result, SINCE, UNTIL
            )
        self.assertEqual([], errors)
        self.assertEqual(["/work/project"], roots)
        self.assertEqual(1, len(records))
        self.assertEqual("Fix stable review identity", records[0]["subject"])
        self.assertEqual(
            ["scripts/review.py", "tests/test_review.py"], records[0]["artifacts"]
        )
        log_command = run.call_args_list[1].args[0]
        self.assertNotIn("--max-count=500", log_command)
        self.assertTrue(any(part.startswith("--since=") for part in log_command))
        self.assertTrue(any(part.startswith("--until=") for part in log_command))
        self.assertTrue(all("status" not in call.args for call in run.call_args_list))

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
        self.assertIn("legacy candidates", text)
        self.assertNotIn("Proposal table", text)


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
