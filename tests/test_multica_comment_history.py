"""Historical comment observations must not become current-state dates or time."""
from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from scripts import clockify_sync_collect as collector
from scripts import collector_checkpoints as checkpoints
from scripts import evidence_ledger as ledger
from scripts import semantic_analyzer
from scripts import work_accounting_pipeline as accounting


SINCE = dt.datetime.fromisoformat("2026-09-01T00:00:00+03:00")
UNTIL = dt.datetime.fromisoformat("2026-10-01T00:00:00+03:00")
CONFIG = {"token": "fixture-token", "server_url": "https://multica.example.test",
          "workspace_id": "workspace-fixture"}
ISSUE = {"id": "issue-158", "key": "SER-158", "title": "Historical work",
         "status": "in_progress", "created_at": "2026-06-03T09:00:00Z",
         "updated_at": "2026-10-05T10:00:00Z", "completed_at": None}
COMMENT = {"id": "comment-september", "issue_id": "issue-158", "parent_id": None,
           "content": "September research result", "created_at": "2026-09-16T12:00:00Z",
           "updated_at": "2026-09-16T12:00:00Z", "author_id": "author-fixture",
           "author_type": "user", "type": "comment", "source_task_id": None,
           "revision": 1}


class MulticaCommentHistoryTests(unittest.TestCase):
    def collect(self, home, *, comments=None, rows=None, stderr="", returncode=0, store=None,
                profile_config=None, transport_error=None, environment=None, raw_stdout=None,
                strict_since=False):
        config_path = Path(home) / ".multica" / "profiles" / collector.MULTICA_PROFILE / "config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(CONFIG if profile_config is None else profile_config))
        payload = [copy.deepcopy(COMMENT)] if comments is None else comments
        commands = []

        def cli(command, **kwargs):
            commands.append(command)
            if transport_error:
                raise transport_error
            self.assertEqual(
                ["multica", "--profile", collector.MULTICA_PROFILE, "--workspace-id",
                 "workspace-fixture", "issue", "comment", "list", command[8],
                 "--since", "2026-08-31T20:59:59Z", "--output", "json"], command)
            response = payload
            if strict_since:
                lower = dt.datetime.fromisoformat(command[10].replace("Z", "+00:00"))
                response = [row for row in payload if dt.datetime.fromisoformat(
                    row["created_at"].replace("Z", "+00:00")) > lower]
            return subprocess.CompletedProcess(command, returncode,
                json.dumps(response) if raw_stdout is None else raw_stdout, stderr)

        with mock.patch.dict(collector.os.environ, {
            "MULTICA_TOKEN": CONFIG["token"], "MULTICA_SERVER_URL": CONFIG["server_url"],
            "MULTICA_WORKSPACE_ID": CONFIG["workspace_id"]} if environment is None else environment,
                clear=True), mock.patch.object(
                collector.Path, "home", return_value=Path(home)), mock.patch.object(
                collector, "http_json", return_value={"issues": [copy.deepcopy(ISSUE)] if rows is None else rows}), mock.patch.object(
                collector.subprocess, "run", side_effect=cli):
            result = collector.fetch_multica_issues(SINCE, UNTIL, checkpoint_store=store)
        return result, commands

    def test_september_comment_survives_june_creation_and_october_update(self):
        """Catches temporal filtering of parent snapshots before acquiring history."""
        with tempfile.TemporaryDirectory() as home:
            result, commands = self.collect(home)
        self.assertTrue(result["complete"])
        self.assertEqual([], result["issues"])
        self.assertEqual(["comment-september"], [row["id"] for row in result.get("comments", [])])
        self.assertEqual("issue-158", commands[0][8])
        events = ledger.normalize_collector_snapshot({"multica_issues": result})
        self.assertEqual(1, len(events))
        self.assertEqual("multica", events[0].source_type)
        self.assertEqual("comment", events[0].attributes["activity_kind"])
        self.assertEqual("2026-09-16T12:00:00Z", events[0].observed_at)
        self.assertEqual("comment:comment-september", events[0].source_ref["source_id"])
        chunks = semantic_analyzer.chunk_events([event.document() for event in events], max_body_bytes=50_000,
                                               private_text_approved=True)
        self.assertEqual("2026-09-16", chunks[0][0]["time_span"]["start"][:10])
        self.assertEqual([], accounting._activity_observed_intervals([event.document() for event in events]))

    def test_comment_identity_is_stable_when_current_issue_status_changes(self):
        """Catches hashing snapshot completion/status into a historical comment."""
        with tempfile.TemporaryDirectory() as home:
            before, _ = self.collect(home)
            changed = {**ISSUE, "status": "done", "completed_at": "2026-10-06T11:00:00Z"}
            after, _ = self.collect(home, rows=[changed])
        first = ledger.normalize_collector_snapshot({"multica_issues": before})[0]
        second = ledger.normalize_collector_snapshot({"multica_issues": after})[0]
        self.assertEqual(first.evidence_id, second.evidence_id)
        self.assertNotIn("completed_at", first.attributes)
        self.assertNotIn("status", first.attributes)
        self.assertNotIn("id:issue-158", ledger.EvidenceLedger((first,)).aliases())
        self.assertEqual("done", after["comments"][0]["issue_snapshot"]["status"])

    def test_half_open_timezone_window_filters_by_creation_not_edit(self):
        """Catches timezone boundary mistakes and edit timestamps replacing creation."""
        comments = [
            {**COMMENT, "id": "start", "created_at": "2026-08-31T21:00:00Z"},
            {**COMMENT, "id": "before", "created_at": "2026-09-01T00:59:59+04:00"},
            {**COMMENT, "id": "end", "created_at": "2026-10-01T00:00:00+03:00"},
            {**COMMENT, "id": "edited", "updated_at": "2026-10-07T00:00:00Z"},
        ]
        with tempfile.TemporaryDirectory() as home:
            result, _ = self.collect(home, comments=comments)
        self.assertTrue(result["complete"])
        self.assertEqual(["edited", "start"], sorted(row["id"] for row in result["comments"]))

    def test_malformed_or_truncated_history_preserves_valid_points_and_debt(self):
        """Catches silently blessing dropped malformed rows or CLI cursor output."""
        cases = [([COMMENT, {**COMMENT, "id": "bad-date", "created_at": "not-a-date"}], "", 0),
                 ([COMMENT, {**COMMENT, "id": "naive", "created_at": "2026-09-16T12:00:00"}], "", 0),
                 ([COMMENT, {**COMMENT, "id": "wrong-parent", "issue_id": "other"}], "", 0),
                 ([COMMENT, 7], "", 0), ([COMMENT], "next_cursor: more\n", 0),
                 ([COMMENT], "failed request\n", 1), ({"unknown": []}, "", 0)]
        for comments, stderr, returncode in cases:
            with self.subTest(comments=comments, stderr=stderr), tempfile.TemporaryDirectory() as home:
                result, _ = self.collect(home, comments=comments, stderr=stderr, returncode=returncode)
                self.assertFalse(result["complete"])
                self.assertEqual("partial", result["status"])
                self.assertTrue(result["comment_history"]["errors"])
                if isinstance(comments, list):
                    self.assertEqual(["comment-september"], [row["id"] for row in result["comments"]])
                inventory = ledger.source_inventory_from_collector({"multica_issues": result})
                self.assertEqual("partial", inventory["multica_issues"]["status"])

    def test_duplicate_comment_ids_are_idempotent_but_conflicts_are_debt(self):
        """Catches duplicate aliases and conflicting revisions hidden by deduplication."""
        with tempfile.TemporaryDirectory() as home:
            result, _ = self.collect(home, comments=[COMMENT, COMMENT])
            conflict, _ = self.collect(home, comments=[COMMENT, {**COMMENT, "content": "changed"}])
        self.assertTrue(result["complete"])
        self.assertEqual(1, len(result["comments"]))
        self.assertFalse(conflict["complete"])
        self.assertEqual(1, len(conflict["comments"]))

    def test_profile_identity_mismatch_never_invokes_cli(self):
        """Catches sending a read under a different credential/origin/workspace."""
        for field, value in [("token", "other-token"), ("workspace_id", "other-workspace"),
                             ("server_url", "https://wrong.example.test")]:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as home:
                result, commands = self.collect(home, profile_config={**CONFIG, field: value})
                self.assertEqual([], commands)
                self.assertFalse(result["complete"])
                self.assertIn("identity", result["comment_history"]["errors"][0]["reason"])

    def test_successful_history_checkpoint_replay_needs_no_transport(self):
        """Catches old issue-index completion incorrectly standing in for comment history."""
        with tempfile.TemporaryDirectory() as home:
            store = checkpoints.PageCheckpointStore(Path(home) / "checkpoints")
            result, commands = self.collect(home, store=store)
            replay, replay_commands = self.collect(home, store=store,
                transport_error=AssertionError("completed history must replay locally"))
        self.assertTrue(result["complete"])
        self.assertEqual(1, len(commands))
        self.assertEqual([], replay_commands)
        self.assertEqual(result, replay)

    def test_failed_history_retries_despite_completed_issue_index(self):
        """Catches reusing complete issue pagination to skip missing comment collection."""
        with tempfile.TemporaryDirectory() as home:
            store = checkpoints.PageCheckpointStore(Path(home) / "checkpoints")
            failed, _ = self.collect(home, store=store, transport_error=OSError("offline"))
            resumed, commands = self.collect(home, store=store)
        self.assertFalse(failed["complete"])
        self.assertTrue(resumed["complete"])
        self.assertEqual(1, len(commands))

    def test_partial_environment_override_cannot_switch_cli_identity(self):
        """Catches ignoring a present override when API configuration falls back to profile."""
        for name, value in [("MULTICA_TOKEN", "other-token"),
                            ("MULTICA_SERVER_URL", "https://wrong.example.test"),
                            ("MULTICA_WORKSPACE_ID", "other-workspace")]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as home:
                result, commands = self.collect(home, environment={name: value})
                self.assertEqual([], commands)
                self.assertFalse(result["complete"])

    def test_history_checkpoint_stores_only_requested_window_comments(self):
        """Catches retaining unneeded later private content from the since-only CLI response."""
        future = {**COMMENT, "id": "future", "created_at": "2026-10-05T12:00:00Z",
                  "content": "outside-window-private-content"}
        with tempfile.TemporaryDirectory() as home:
            store = checkpoints.PageCheckpointStore(Path(home) / "checkpoints")
            result, _ = self.collect(home, store=store, comments=[COMMENT, future])
            identity = collector._multica_comment_checkpoint_identity(
                CONFIG["server_url"], CONFIG["workspace_id"], ISSUE["id"], SINCE, UNTIL)
            page = list(store.iter_pages(store.open(identity)))[0]
            self.assertEqual(["comment-september"], [row["id"] for row in page["payload"]])
        self.assertTrue(result["complete"])

    def test_comment_checkpoint_preserves_fractional_window_identity(self):
        """Catches replay of a differently bounded window sharing second-truncated identity."""
        first = collector._multica_comment_checkpoint_identity(
            CONFIG["server_url"], CONFIG["workspace_id"], ISSUE["id"],
            SINCE + dt.timedelta(microseconds=1), UNTIL)
        second = collector._multica_comment_checkpoint_identity(
            CONFIG["server_url"], CONFIG["workspace_id"], ISSUE["id"],
            SINCE + dt.timedelta(microseconds=2), UNTIL)
        with tempfile.TemporaryDirectory() as home:
            store = checkpoints.PageCheckpointStore(Path(home))
            completed = store.open(first)
            completed = store.append_page(completed, payload=[], continuation={}, signature="empty")
            store.mark_complete(completed)
            self.assertFalse(store.open(second).complete)

    def test_empty_index_and_invalid_json_do_not_invent_history(self):
        with tempfile.TemporaryDirectory() as home:
            empty, commands = self.collect(home, rows=[])
            broken, _ = self.collect(home, raw_stdout="[{invalid")
        self.assertTrue(empty["complete"])
        self.assertEqual([], commands)
        self.assertEqual([], empty["comments"])
        self.assertFalse(broken["complete"])
        self.assertEqual([], broken["comments"])

    def test_old_snapshot_comments_require_explicit_new_source_version(self):
        """Catches reinterpreting already sealed unversioned source snapshots."""
        snapshot = {"multica_issues": {"issues": [ISSUE], "comments": [COMMENT]}}
        events = ledger.normalize_collector_snapshot(snapshot)
        self.assertEqual(1, len(events))
        self.assertEqual("issue-158", events[0].source_ref["source_id"])
        self.assertEqual("2026-10-05T10:00:00Z", events[0].observed_at)

    def test_multiple_comments_are_not_human_capacity_or_issue_aliases(self):
        """Catches creating duration from a comment cluster or sharing issue aliases."""
        issue = {**ISSUE, "updated_at": "2026-09-17T12:00:00Z"}
        comments = [{**COMMENT, "id": "issue-158"},
                    {**COMMENT, "id": "second", "created_at": "2026-09-16T12:15:00Z"}]
        with tempfile.TemporaryDirectory() as home:
            result, _ = self.collect(home, comments=comments, rows=[issue])
        events = ledger.normalize_collector_snapshot({"multica_issues": result})
        self.assertEqual(3, len(events))
        evidence = ledger.EvidenceLedger(tuple(events))
        self.assertEqual("issue-158", evidence.resolve("id:issue-158").source_ref["source_id"])
        self.assertEqual("comment:issue-158", evidence.resolve("multica_comment_id:issue-158").source_ref["source_id"])
        self.assertEqual([], accounting._activity_observed_intervals([event.document() for event in events]))

    def test_comment_author_roles_survive_private_projection_without_human_time(self):
        """Catches projecting agent work as untyped source evidence (or human effort)."""
        for author_type, expected_role in [("agent", "assistant"), ("member", "user"), ("user", "source"),
                                           ("unknown", "source"), ([], "source"), (None, "source")]:
            with self.subTest(author_type=author_type), tempfile.TemporaryDirectory() as home:
                comments = [{**COMMENT, "author_type": author_type},
                            {**COMMENT, "id": "second", "author_type": author_type,
                             "created_at": "2026-09-16T12:15:00Z"}]
                result, _ = self.collect(home, comments=comments)
                events = ledger.normalize_collector_snapshot({"multica_issues": result})
                projected = semantic_analyzer.project_events([event.document() for event in events])
                self.assertEqual([expected_role, expected_role], [event["role"] for event in projected])
                self.assertEqual(["September research result"] * 2, [event["content"] for event in projected])
                self.assertNotIn("author-fixture", json.dumps(projected))
                self.assertNotIn("issue-158", json.dumps(projected))
                self.assertEqual([], accounting._activity_observed_intervals([event.document() for event in events]))

    def test_strict_cli_since_preserves_comment_exactly_at_local_lower_bound(self):
        """Catches exclusive CLI --since dropping the inclusive local window boundary."""
        boundary = {**COMMENT, "created_at": "2026-08-31T21:00:00Z"}
        with tempfile.TemporaryDirectory() as home:
            result, _ = self.collect(home, comments=[boundary], strict_since=True)
        self.assertTrue(result["complete"])
        self.assertEqual(["comment-september"], [comment["id"] for comment in result["comments"]])

    def test_same_issue_comments_share_semantic_context_without_human_duration(self):
        """Catches separating member instruction from its agent reply by comment ID."""
        comments = [COMMENT, {**COMMENT, "id": "reply", "parent_id": COMMENT["id"],
                              "author_type": "agent", "created_at": "2026-09-16T12:05:00Z"}]
        with tempfile.TemporaryDirectory() as home:
            result, _ = self.collect(home, comments=comments)
        events = ledger.normalize_collector_snapshot({"multica_issues": result})
        documents = [event.document() for event in events]
        contexts = [semantic_analyzer._semantic_context_key(event) for event in documents]
        self.assertEqual(contexts[0], contexts[1])
        other_issue = copy.deepcopy(documents[0])
        other_issue["source_ref"]["issue_id"] = "issue-other"
        self.assertNotEqual(contexts[0], semantic_analyzer._semantic_context_key(other_issue))
        self.assertEqual([], accounting._activity_observed_intervals(documents))


if __name__ == "__main__":
    unittest.main()
