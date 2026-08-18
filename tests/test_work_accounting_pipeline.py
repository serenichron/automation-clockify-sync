from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import evidence_ledger
from scripts import review_corrections
from scripts import work_accounting_pipeline as pipeline
from scripts.meeting_reconciliation import MeetingReconciliationError, reconcile_meetings


ROOT = Path(__file__).resolve().parents[1]


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def session_event(source_id: str, timestamp: str, content: str = "Fix Clockify"):
    return evidence_ledger.evidence_event(
        "codex_sessions_event",
        {
            "source_type": "codex_sessions",
            "source_id": source_id,
            "machine": "macbook",
            "session_id": "session-1",
        },
        observed_at=timestamp,
        raw_source_span={
            "timestamp": timestamp,
            "path": "/Users/blackthorne/Work/automation-clockify-sync/session.jsonl",
        },
        attributes={"role": "user", "kind": "message", "content": content},
    )


def clockify_event(start: str, end: str):
    return evidence_ledger.evidence_event(
        "clockify",
        {"source_type": "clockify", "source_id": "existing-1"},
        observed_at=start,
        raw_source_span={"start": start, "end": end},
        attributes={"description": "Existing work"},
    )


def fathom_event(start: str, end: str, status: str = "title_only"):
    return evidence_ledger.evidence_event(
        "fathom",
        {"source_type": "fathom", "source_id": "meeting-1"},
        observed_at=start,
        raw_source_span={"start": start, "end": end},
        attributes={
            "title": "Discovery call",
            "semantic_evidence_status": status,
            "recorded_by_email": "vlad@serenichron.com",
            "calendar_invitees": [{"email": "prospect@example.test", "is_external": True}],
        },
    )


def calendly_event(start: str, end: str):
    return evidence_ledger.evidence_event(
        "calendly",
        {
            "source_type": "calendly", "source_id": "calendly-meeting-1",
            "meeting_id": "meeting-1",
        },
        observed_at=start,
        raw_source_span={"start": start, "end": end},
        attributes={
            "recording_id": "calendly-meeting-1",
            "meeting_id": "meeting-1",
            "duration_seconds": int((
                dt.datetime.fromisoformat(end.replace("Z", "+00:00"))
                - dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
            ).total_seconds()),
            "title": "Discovery call",
            "organizer": {"email": "vlad@serenichron.com"},
            "participants": [{"email": "prospect@example.test"}],
            "join_url": "https://meet.example.test/meeting-1",
            "summary": "Recorded discovery discussion",
            "transcript": [],
            "source_digest": "sha256:" + "c" * 64,
        },
    )


def analysis_for(evidence_ids, recommended=120):
    return {
        "activities": [
            {
                "lifecycle": "completed",
                "action": "Rebuilt",
                "object": "Clockify review process",
                "outcome": "for accurate automatic timesheets",
                "evidence_ids": list(evidence_ids),
                "evidence_spans": [
                    {
                        "evidence_id": evidence_id,
                        "start": "2026-07-10T09:00:00+03:00",
                        "end": "2026-07-10T09:01:00+03:00",
                    }
                    for evidence_id in evidence_ids
                ],
                "project_recommendation": {
                    "name": "Serenichron Level 2",
                    "prefix": "SC",
                    "tag_names": ["Processes"],
                },
                "effort": {
                    "minimum_minutes": min(10, recommended),
                    "recommended_minutes": recommended,
                    "maximum_minutes": max(recommended, 180),
                },
                "semantic_confidence": "high",
                "timing_confidence": "medium",
                "split_rationale": "one accomplishment",
                "merge_rationale": "cross-session evidence",
            }
        ],
        "exceptions": [],
        "omissions": [],
    }


def meeting_analysis(meeting):
    span = meeting.raw_source_span
    return {
        "activities": [
            {
                "lifecycle": "meeting",
                "action": "Confirmed",
                "object": "Discovery call scope",
                "outcome": "for the client implementation plan",
                "evidence_ids": [meeting.evidence_id],
                "evidence_spans": [
                    {
                        "evidence_id": meeting.evidence_id,
                        "start": span["start"],
                        "end": span["end"],
                    }
                ],
                "project_recommendation": {
                    "name": "Serenichron Level 2",
                    "prefix": "SC",
                    "tag_names": ["Processes"],
                },
                "effort": {
                    "minimum_minutes": 30,
                    "recommended_minutes": 60,
                    "maximum_minutes": 60,
                },
                "semantic_confidence": "high",
                "timing_confidence": "high",
                "split_rationale": "one fixed meeting",
                "merge_rationale": "one meeting record",
            }
        ],
        "exceptions": [],
        "omissions": [],
    }


class WorkAccountingPipelineTests(unittest.TestCase):
    def test_analyzer_tuning_cli_options_are_explicit(self):
        args = pipeline.parse_args([
            "/tmp/run",
            "--analyzer-target-body-bytes", "250000",
            "--analyzer-max-events-per-chunk", "250",
            "--analyzer-workers", "4",
        ])
        self.assertEqual(250_000, args.analyzer_target_body_bytes)
        self.assertEqual(250, args.analyzer_max_events_per_chunk)
        self.assertEqual(4, args.analyzer_workers)

    def test_analyzer_tuning_reaches_semantic_analyzer(self):
        primary = pipeline.semantic_analyzer.AnalyzerEndpoint(
            "primary", "http://primary", "flash"
        )
        expected = {
            "schema_version": 1,
            "prompt_version": "clockify-semantic-v8",
            "activities": [],
            "exceptions": [],
            "omissions": [],
            "analysis_chunks": [],
        }
        with (
            mock.patch.object(
                pipeline.semantic_analyzer.AnalyzerEndpoint,
                "from_env",
                side_effect=[primary, None],
            ),
            mock.patch.object(
                pipeline.semantic_analyzer,
                "analyze_tiered",
                autospec=True,
                return_value=expected,
            ) as analyze,
        ):
            result = pipeline.analyze_ledger(
                [{"evidence_id": "ev-1"}],
                analyzer_target_body_bytes=250_000,
                analyzer_max_events_per_chunk=50,
                analyzer_workers=4,
            )

        self.assertIs(expected, result)
        self.assertEqual(250_000, analyze.call_args.kwargs["target_body_bytes"])
        self.assertEqual(50, analyze.call_args.kwargs["max_events_per_chunk"])
        self.assertEqual(4, analyze.call_args.kwargs["max_workers"])

    def test_noise_classifier_excludes_transport_and_status_only_events(self):
        cases = (
            ({"attributes": {"role": "tool", "kind": "tool_result", "content": "private output"}}, "tool_transport"),
            ({"attributes": {"role": "tool", "kind": "message", "content": "private output"}}, "tool_transport"),
            ({"attributes": {"role": "assistant", "kind": "tool", "content": "private output"}}, "tool_transport"),
            ({"attributes": {"role": "assistant", "kind": "message", "content": "Heartbeat: ok"}}, "heartbeat"),
            ({"attributes": {"role": "assistant", "kind": "message", "content": "Standing by."}}, "standing_by"),
            ({"attributes": {"role": "assistant", "kind": "message", "content": "Awaiting board approval: deploy"}}, "approval_wait"),
            ({"attributes": {"role": "assistant", "kind": "message", "content": "Still running: download"}}, "polling"),
        )
        for event, expected in cases:
            with self.subTest(content=event["attributes"]["content"]):
                self.assertEqual(expected, pipeline.classify_noise(event))

    def test_analysis_events_drop_only_summaries_backed_by_canonical_events(self):
        duplicate_summary = {
            "evidence_id": "ev-summary-1",
            "source_type": "hermes_db_sessions",
            "source_ref": {
                "source_type": "hermes_db_sessions",
                "machine": "precision",
                "session_id": "session-1",
            },
            "attributes": {"first_user_message": "duplicate"},
        }
        canonical_event = {
            "evidence_id": "ev-event-1",
            "source_type": "hermes_db_sessions_event",
            "source_ref": {
                "source_type": "hermes_db_sessions",
                "machine": "precision",
                "session_id": "session-1",
            },
            "attributes": {"role": "user", "kind": "message", "content": "real work"},
        }
        unmatched_summary = {
            "evidence_id": "ev-summary-2",
            "source_type": "hermes_db_sessions",
            "source_ref": {
                "source_type": "hermes_db_sessions",
                "machine": "precision",
                "session_id": "session-2",
            },
            "attributes": {"first_user_message": "only available context"},
        }

        retained, noise = pipeline._analysis_events(
            [duplicate_summary, canonical_event, unmatched_summary]
        )

        self.assertEqual(
            ["ev-event-1", "ev-summary-2"],
            [event["evidence_id"] for event in retained],
        )
        self.assertIn(
            {"evidence_id": "ev-summary-1", "reason": "duplicate_session_summary"},
            noise,
        )

    def test_analysis_events_do_not_deduplicate_without_exact_machine_source_pair(self):
        summary = {
            "evidence_id": "ev-summary",
            "source_type": "hermes_db_sessions",
            "source_ref": {
                "source_type": "hermes_db_sessions",
                "session_id": "session-1",
            },
            "attributes": {"first_user_message": "only proven summary"},
        }
        missing_machine = {
            "evidence_id": "ev-missing-machine",
            "source_type": "hermes_db_sessions_event",
            "source_ref": {
                "source_type": "hermes_db_sessions",
                "session_id": "session-1",
            },
            "attributes": {"role": "user", "kind": "message", "content": "work"},
        }
        spoofed_source = {
            "evidence_id": "ev-spoofed-source",
            "source_type": "unrelated_event",
            "source_ref": {
                "source_type": "hermes_db_sessions",
                "machine": "precision",
                "session_id": "session-1",
            },
            "attributes": {"role": "user", "kind": "message", "content": "work"},
        }

        retained, noise = pipeline._analysis_events(
            [summary, missing_machine, spoofed_source]
        )

        self.assertEqual(
            ["ev-summary", "ev-missing-machine", "ev-spoofed-source"],
            [event["evidence_id"] for event in retained],
        )
        self.assertFalse(any(item["evidence_id"] == "ev-summary" for item in noise))

    def test_repository_history_reaches_semantic_review_as_corroboration(self):
        repository_event = {
            "evidence_id": "ev-commit",
            "source_type": "repository_events",
            "source_ref": {"source_type": "repository_events", "source_id": "commit-1"},
            "observed_at": "2026-07-10T09:05:00+03:00",
            "attributes": {
                "repository_root": "/work/upstream",
                "subject": "Bump dependency version",
                "artifacts": ["package.json"],
            },
        }
        session_event = {
            "evidence_id": "ev-session",
            "source_type": "codex_sessions_event",
            "source_ref": {
                "source_type": "codex_sessions",
                "machine": "precision",
                "session_id": "session-1",
            },
            "observed_at": "2026-07-10T09:00:00+03:00",
            "attributes": {
                "role": "user",
                "kind": "message",
                "content": "Fix the review workflow",
            },
        }

        retained, noise = pipeline._analysis_events([repository_event, session_event])

        self.assertEqual(
            ["ev-commit", "ev-session"],
            [event["evidence_id"] for event in retained],
        )
        self.assertFalse(any(item["evidence_id"] == "ev-commit" for item in noise))

    def test_multica_coding_agent_sessions_are_autonomous_background(self):
        summary = {
            "evidence_id": "ev-summary",
            "source_type": "hermes_db_sessions",
            "source_ref": {
                "source_type": "hermes_db_sessions",
                "machine": "macbook",
                "session_id": "agent-session",
            },
            "attributes": {
                "first_user_message": (
                    "You are running as a local coding agent for a Multica workspace. "
                    "Your assigned issue ID is: issue-1"
                ),
            },
        }
        autonomous_event = {
            "evidence_id": "ev-agent",
            "source_type": "hermes_db_sessions_event",
            "source_ref": {
                "source_type": "hermes_db_sessions",
                "machine": "macbook",
                "session_id": "agent-session",
            },
            "attributes": {
                "role": "assistant",
                "kind": "message",
                "content": "Completed the assigned issue",
            },
        }
        direct_chat = {
            "evidence_id": "ev-direct",
            "source_type": "hermes_db_sessions_event",
            "source_ref": {
                "source_type": "hermes_db_sessions",
                "machine": "macbook",
                "session_id": "direct-chat",
            },
            "attributes": {
                "role": "user",
                "kind": "message",
                "content": "Review the sales workflow",
            },
        }

        retained, noise = pipeline._analysis_events(
            [summary, autonomous_event, direct_chat]
        )

        self.assertEqual(["ev-direct"], [event["evidence_id"] for event in retained])
        self.assertEqual(
            {"ev-summary", "ev-agent"},
            {
                item["evidence_id"]
                for item in noise
                if item["reason"] == "autonomous_background_session"
            },
        )

    def test_noise_words_inside_substantive_accomplishments_are_preserved(self):
        for content in (
            "Fixed heartbeat failures in the session monitor",
            "Corrected approval request validation for guarded actions",
            "Diagnosed polling load and reduced API traffic",
        ):
            with self.subTest(content=content):
                self.assertIsNone(
                    pipeline.classify_noise(
                        {"attributes": {"role": "assistant", "kind": "message", "content": content}}
                    )
                )

    def test_cross_project_evidence_requires_semantic_split(self):
        activity = {
            "project_recommendation": {"name": "Serenichron Level 2"},
        }
        cited = [
            evidence_ledger.evidence_event(
                "codex_sessions_event",
                {"source_type": "codex_sessions", "source_id": "one"},
                observed_at="2026-07-10T09:00:00+03:00",
                raw_source_span={"path": "/Users/blackthorne/Work/serenichron/session.jsonl"},
                attributes={"role": "user", "kind": "message", "label": "serenichron"},
            ).document(),
            evidence_ledger.evidence_event(
                "codex_sessions_event",
                {"source_type": "codex_sessions", "source_id": "two"},
                observed_at="2026-07-10T09:10:00+03:00",
                raw_source_span={"path": "/Users/blackthorne/Work/tstprep-com-site-codebase/session.jsonl"},
                attributes={"role": "user", "kind": "message", "label": "tstprep-com-site-codebase"},
            ).document(),
        ]
        route, error = pipeline.resolve_route(activity, cited, json.loads((ROOT / "routing.json").read_text()))
        self.assertIsNone(route)
        self.assertIn("multiple deterministic routes", error)
        self.assertIn("semantic split required", error)

    def test_flash_review_project_and_task_selection_owns_routing(self):
        routing = json.loads((ROOT / "routing.json").read_text())
        activity = {
            "semantic_reviewer_model": "deepseek-v4-flash:0731-cloud",
            "project_recommendation": {
                "name": "Serenichron Level 1",
                "prefix": "SC",
                "tag_names": ["Project Management"],
            },
        }
        cited = [
            evidence_ledger.evidence_event(
                "codex_sessions_event",
                {"source_type": "codex_sessions", "source_id": "reviewed"},
                observed_at="2026-07-10T09:00:00+03:00",
                raw_source_span={
                    "path": "/Users/blackthorne/Work/serenichron/session.jsonl"
                },
                attributes={"role": "user", "kind": "message"},
            ).document()
        ]

        route, error = pipeline.resolve_route(activity, cited, routing)

        self.assertIsNone(error)
        self.assertEqual("Serenichron Level 1", route["project_name"])
        self.assertEqual(["Project Management"], route["tag_names"])

    def test_emblemstudio_uses_serenichron_project_with_es_prefix(self):
        routing = json.loads((ROOT / "routing.json").read_text())
        cited = [
            evidence_ledger.evidence_event(
                "codex_sessions_event",
                {"source_type": "codex_sessions", "source_id": "emblem"},
                observed_at="2026-07-10T09:00:00+03:00",
                raw_source_span={
                    "path": "/Users/blackthorne/Work/master-emblem-studio/session.jsonl",
                    "cwd": "/Users/blackthorne/Work/master-emblem-studio",
                },
                attributes={"role": "user", "kind": "message", "content": "Refine EmblemStudio launch plan"},
            ).document()
        ]
        activity = {
            "semantic_reviewer_model": "deepseek-v4-flash:cloud",
            "project_recommendation": {
                "name": "Serenichron Level 2",
                "prefix": "ES",
                "tag_names": ["Business development"],
            },
        }

        route, error = pipeline.resolve_route(activity, cited, routing)

        self.assertIsNone(error)
        self.assertEqual("Serenichron Level 2", route["project_name"])
        self.assertEqual("ES", route["prefix"])
        self.assertEqual(["Business development"], route["tag_names"])

    def test_ordinary_serenichron_keeps_sc_when_es_alias_exists(self):
        routing = json.loads((ROOT / "routing.json").read_text())
        taxonomy = pipeline._semantic_review_taxonomy(routing)
        prefixes = {
            row["prefix"]
            for row in taxonomy
            if row["project_name"] == "Serenichron Level 2"
            and row["tag_names"] == ["System development"]
        }
        self.assertEqual({"ES", "SC"}, prefixes)

        cited = [session_event("ordinary", "2026-07-10T09:00:00+03:00").document()]
        activity = {
            "semantic_reviewer_model": "deepseek-v4-flash:cloud",
            "project_recommendation": {
                "name": "Serenichron Level 2",
                "prefix": "ES",
                "tag_names": ["Processes"],
            },
        }
        route, error = pipeline.resolve_route(activity, cited, routing)
        self.assertIsNone(error)
        self.assertEqual("SC", route["prefix"])

    def test_semantic_review_taxonomy_contains_distinct_project_task_pairs(self):
        routing = json.loads((ROOT / "routing.json").read_text())

        taxonomy = pipeline._semantic_review_taxonomy(routing)

        serenichron_l2 = {
            tuple(row["tag_names"])
            for row in taxonomy
            if row["project_name"] == "Serenichron Level 2"
        }
        self.assertTrue({
            ("Business development",),
            ("Processes",),
            ("System development",),
        } <= serenichron_l2)
        system_development = next(
            row for row in taxonomy
            if row["project_name"] == "Serenichron Level 2"
            and row["prefix"] == "SC"
            and row["tag_names"] == ["System development"]
        )
        self.assertIn("serenichron", system_development["selection_guidance"])

    def test_semantic_route_hint_prefers_normalized_cwd_over_session_file_path(self):
        routing = json.loads((ROOT / "routing.json").read_text())
        event = evidence_ledger.evidence_event(
            "codex_sessions_event",
            {
                "source_type": "codex_sessions",
                "source_id": "session:event:1",
                "machine": "macbook",
                "session_id": "session",
            },
            observed_at="2026-07-10T09:00:00+03:00",
            raw_source_span={
                "timestamp": "2026-07-10T09:00:00+03:00",
                "path": "/Users/blackthorne/.codex/sessions/rollout.jsonl",
                "cwd": "/Users/blackthorne/Work/tstprep.com/site-codebase/site",
            },
            attributes={"role": "user", "kind": "message", "content": "Fix site"},
        ).document()

        hint = pipeline._semantic_route_hint(event, routing)

        self.assertEqual("TST Prep Level 2", hint["project_name"])
        self.assertEqual(["Technical development"], hint["tag_names"])

    def test_proposal_identity_ignores_placement_but_distinguishes_segments(self):
        activity = analysis_for(["ev-1"])["activities"][0]
        activity["activity_id"] = "act-stable"
        activity["workstream_id"] = "ws-stable"
        route = {
            "project_name": "Serenichron Level 2",
            "project_suffix": "775f9f",
            "tag_suffixes": ["35aa9afb"],
            "tag_names": ["System development"],
            "prefix": "SC",
        }
        first = pipeline._proposal(
            activity,
            route,
            "SC — Rebuilt Clockify review for accurate automatic timesheets",
            dt.datetime.fromisoformat("2026-07-10T09:00:00+03:00"),
            dt.datetime.fromisoformat("2026-07-10T10:00:00+03:00"),
            ["ev-1"],
            1,
        )
        moved = pipeline._proposal(
            activity,
            route,
            first["description"],
            dt.datetime.fromisoformat("2026-07-10T11:00:00+03:00"),
            dt.datetime.fromisoformat("2026-07-10T12:00:00+03:00"),
            ["ev-1"],
            1,
        )
        second_segment = pipeline._proposal(
            activity,
            route,
            first["description"],
            dt.datetime.fromisoformat("2026-07-10T13:00:00+03:00"),
            dt.datetime.fromisoformat("2026-07-10T13:30:00+03:00"),
            ["ev-1"],
            2,
        )
        self.assertEqual(first["review_activity_key"], moved["review_activity_key"])
        self.assertEqual(first["candidate_key"], moved["candidate_key"])
        self.assertNotEqual(first["candidate_key"], second_segment["candidate_key"])

    def make_run(self, events, analysis, *, corrections_path=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        run_dir = Path(temp.name) / "runs" / "fixture"
        ledger = evidence_ledger.EvidenceLedger(
            tuple(events),
            {
                "clockify": {"status": "complete"},
                "fathom": {"status": "complete"},
                "sessions/macbook": {"status": "complete"},
            },
        )
        write_json(
            run_dir / "evidence" / "evidence-ledger.json",
            {
                "schema_version": ledger.manifest.schema_version,
                "manifest": ledger.manifest.document(),
                "events": [event.document() for event in ledger.events],
            },
        )
        fixture = Path(temp.name) / "analysis.json"
        write_json(fixture, analysis)
        result = pipeline.run_accounting(
            run_dir,
            root=ROOT,
            analysis_fixture=fixture,
            corrections_path=corrections_path,
        )
        return run_dir, result

    def test_collector_snapshot_local_minute_fathom_reaches_accounting_in_ledger_timezone(self):
        """The collector's local Fathom minute evidence is normalized before deduplication."""
        snapshot = {
            "clockify": {"status": "ok", "complete": True, "entries": []},
            "fathom": {
                "status": "ok", "complete": True,
                "meetings": [{
                    "recording_id": "local-minute", "meeting_id": "local-event",
                    "title": "Local-minute client review",
                    "start": "2026-07-10 13:00", "end": "2026-07-10 13:37",
                    "recorded_by_email": "vlad@serenichron.com",
                    "calendar_invitees": [{"email": "client@example.test", "is_external": True}],
                    "semantic_evidence_status": "available",
                }],
            },
            "calendly": {"status": "disabled", "complete": True, "recordings": []},
            "multica_issues": {"status": "ok", "complete": True, "issues": []},
        }
        events = evidence_ledger.normalize_collector_snapshot(snapshot)
        meeting = next(event for event in events if event.source_type == "fathom")
        inventory = evidence_ledger.source_inventory_from_collector(snapshot)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = root / "runs" / "fixture"
            immutable = evidence_ledger.EvidenceLedger(
                tuple(events), inventory, "Europe/Bucharest"
            )
            write_json(run_dir / "evidence" / "evidence-ledger.json", {
                "schema_version": immutable.manifest.schema_version,
                "manifest": immutable.manifest.document(),
                "events": [event.document() for event in immutable.events],
            })
            fixture = root / "analysis.json"
            write_json(fixture, meeting_analysis(meeting))
            result = pipeline.run_accounting(run_dir, root=ROOT, analysis_fixture=fixture)

        self.assertEqual("2026-07-10T13:00:00+03:00", result["proposals"][0]["start"])
        self.assertEqual("2026-07-10T13:37:00+03:00", result["proposals"][0]["end"])

    def test_meeting_seconds_survive_accounting_proposal_contract(self):
        meeting = fathom_event(
            "2026-07-10T13:00:17+03:00",
            "2026-07-10T13:37:43+03:00",
            status="available",
        )
        _, result = self.make_run([meeting], meeting_analysis(meeting))

        proposal = result["proposals"][0]
        self.assertEqual("2026-07-10T13:00:17+03:00", proposal["start"])
        self.assertEqual("2026-07-10T13:37:43+03:00", proposal["end"])
        self.assertEqual(2246, proposal["duration_seconds"])

    def test_subminute_final_meeting_segment_preserves_positive_seconds(self):
        meeting = fathom_event(
            "2026-07-10T13:00:00+03:00",
            "2026-07-10T13:35:43+03:00",
            status="available",
        )
        calendly = calendly_event(
            "2026-07-10T10:00:00Z", "2026-07-10T10:35:43Z"
        )
        analysis = meeting_analysis(meeting)
        first = analysis["activities"][0]
        first["evidence_spans"][0]["end"] = "2026-07-10T13:35:00+03:00"
        final = json.loads(json.dumps(first))
        final["object"] = "Discovery call follow-up"
        final["project_recommendation"] = {
            "name": "Serenichron Level 1",
            "prefix": "SC",
            "tag_names": ["Project Management"],
        }
        final["evidence_ids"] = [calendly.evidence_id]
        final["evidence_spans"][0].update({
            "evidence_id": calendly.evidence_id,
            "start": "2026-07-10T13:35:00+03:00",
            "end": "2026-07-10T13:35:43+03:00",
        })
        analysis["activities"].append(final)
        _, result = self.make_run([meeting, calendly], analysis)

        proposal = result["proposals"][-1]
        self.assertEqual("2026-07-10T13:35:00+03:00", proposal["start"])
        self.assertEqual("2026-07-10T13:35:43+03:00", proposal["end"])
        self.assertEqual(43, proposal["duration_seconds"])
        self.assertEqual(0, proposal["duration_minutes"])

        schema = json.loads((ROOT / "schemas" / "work-accounting-result-v1.json").read_text())
        duration_minutes = schema["$defs"]["proposal"]["properties"]["duration_minutes"]
        duration_seconds = schema["$defs"]["proposal"]["properties"]["duration_seconds"]
        self.assertGreaterEqual(proposal["duration_minutes"], duration_minutes["minimum"])
        self.assertGreaterEqual(proposal["duration_seconds"], duration_seconds["minimum"])

    def test_proposal_rejects_nonpositive_bounds(self):
        start = dt.datetime(2026, 7, 10, 13, 35, tzinfo=dt.timezone.utc)

        with self.assertRaisesRegex(pipeline.WorkAccountingError, "positive"):
            pipeline._proposal(
                {"activity_id": "act-one", "workstream_id": "ws-one"},
                {},
                "SC — Preserve exact seconds",
                start,
                start,
                ["ev-one"],
                1,
            )

    def test_manifest_member_identity_is_used_for_meeting_eligibility(self):
        meeting = evidence_ledger.evidence_event(
            "fathom",
            {"source_type": "fathom", "source_id": "member-alias"},
            raw_source_span={
                "start": "2026-07-10T13:00:00+03:00",
                "end": "2026-07-10T13:37:00+03:00",
            },
            attributes={
                "title": "Member alias review",
                "semantic_evidence_status": "available",
                "recorded_by_email": "alternate@example.test",
                "calendar_invitees": [{"email": "client@example.test", "is_external": True}],
            },
        )
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        run_dir = Path(temp.name) / "runs" / "fixture"
        immutable = evidence_ledger.EvidenceLedger(
            (meeting,),
            {
                "clockify": {"status": "complete"},
                "fathom": {"status": "complete"},
                "sessions/macbook": {"status": "complete"},
            },
            member_identities=("alternate@example.test",),
        )
        write_json(run_dir / "evidence" / "evidence-ledger.json", {
            "schema_version": immutable.manifest.schema_version,
            "manifest": immutable.manifest.document(),
            "events": [event.document() for event in immutable.events],
        })
        fixture = Path(temp.name) / "analysis.json"
        write_json(fixture, meeting_analysis(meeting))

        result = pipeline.run_accounting(run_dir, root=ROOT, analysis_fixture=fixture)

        self.assertEqual("proposed", result["fathom_reconciliation"][0]["status"])

    def append_correction(self, path, proposal, decision, *, field_patch=None, categories=None):
        item = {"id": "rvi-regression", "current": proposal}
        record = review_corrections.build_decision(
            item,
            decision=decision,
            reviewer="reviewer",
            reviewed_at="2026-08-01T10:00:00+03:00",
            correction_categories=categories or (["omission"] if decision == "skip" else ["wording"]),
            rationale="Keep the evidence-bound review decision stable.",
            field_patch=field_patch,
        )
        review_corrections.append_decision(path, record, item=item)

    def test_reviewed_description_bypasses_legacy_regex_in_full_pipeline(self):
        first = session_event(
            "session-1:event:review-1",
            "2026-07-10T09:00:00+03:00",
            "Hold brand strategy alignment meeting",
        )
        last = session_event(
            "session-1:event:review-2",
            "2026-07-10T10:00:00+03:00",
            "Schedule Friday follow-up one-to-one",
        )
        analysis = analysis_for([first.evidence_id, last.evidence_id], recommended=30)
        activity = analysis["activities"][0]
        activity.update({
            "action": "Held",
            "object": "brand strategy alignment meeting",
            "outcome": "scheduled Friday follow-up one-to-one",
            "semantic_reviewer_model": "deepseek-v4-flash:0731-cloud",
            "semantic_reviewer_revision": "a" * 64,
            "review_prompt_version": "clockify-semantic-review-v5",
        })

        _, result = self.make_run([first, last], analysis)

        self.assertEqual(1, len(result["proposals"]))
        self.assertEqual(
            "SC — Held brand strategy alignment meeting scheduled Friday follow-up one-to-one",
            result["proposals"][0]["description"],
        )
        self.assertFalse(
            any(
                row.get("exception_kind") == "description_contract"
                for row in result["ambiguous"]
            )
        )

    def test_completed_analysis_fixture_preserves_replay_identity_metadata(self):
        first = session_event("session-1:event:1", "2026-07-10T09:00:00+03:00")
        analysis = analysis_for([first.evidence_id], recommended=30)
        analysis["activities"][0].update({
            "analyzer_model": "deepseek-v4-flash:0731-cloud",
            "analyzer_tier": "primary_flash_review",
            "analyzer_revision": "a" * 64,
            "semantic_reviewer_model": "deepseek-v4-flash:0731-cloud",
            "semantic_reviewer_revision": "a" * 64,
            "review_prompt_version": "clockify-semantic-review-v6",
        })
        metadata = {
            "review_prompt_version": "clockify-semantic-review-v6",
            "evidence_bundle_schema_version": "clockify-semantic-evidence-bundle/v1",
            "evidence_bundle_manifest": {
                "schema_version": "clockify-semantic-evidence-bundle/v1",
                "digest": "sebm-" + "b" * 64,
                "bundles": [],
            },
            "ledger_event_count": 1,
            "ledger_evidence_digest": "led-" + "c" * 64,
            "analysis_chunks": [{"model": "deepseek-v4-flash:0731-cloud"}],
            "analyzer_cache": {
                "schema_version": "clockify-analyzer-cache/v2",
                "records": [],
            },
        }
        analysis.update(metadata)

        run_dir, _ = self.make_run([first], analysis)
        restored = json.loads(
            (run_dir / "semantic-analysis.json").read_text(encoding="utf-8")
        )

        for key, value in metadata.items():
            self.assertEqual(value, restored[key])
        self.assertEqual(
            {
                "analyzer_model": "deepseek-v4-flash:0731-cloud",
                "analyzer_tier": "primary_flash_review",
                "analyzer_revision": "a" * 64,
            },
            {
                key: restored["activities"][0][key]
                for key in ("analyzer_model", "analyzer_tier", "analyzer_revision")
            },
        )

    def test_splits_effort_around_existing_block_without_overlap(self):
        first = session_event("session-1:event:1", "2026-07-10T09:00:00+03:00")
        last = session_event("session-1:event:2", "2026-07-10T12:00:00+03:00")
        existing = clockify_event("2026-07-10T10:00:00+03:00", "2026-07-10T10:30:00+03:00")
        _, result = self.make_run(
            [first, last, existing],
            analysis_for([first.evidence_id, last.evidence_id], recommended=120),
        )
        self.assertEqual(2, len(result["proposals"]))
        self.assertEqual(120, sum(row["duration_minutes"] for row in result["proposals"]))
        self.assertLessEqual(result["proposals"][0]["end"], "2026-07-10T10:00:00+03:00")
        self.assertGreaterEqual(result["proposals"][1]["start"], "2026-07-10T10:30+03:00")
        self.assertTrue(all(row["allocation_mode"] == "non_overlapping_v1" for row in result["proposals"]))

    def test_prior_skip_removes_every_segment_and_records_one_skip(self):
        first = session_event("session-1:event:1", "2026-07-10T09:00:00+03:00")
        last = session_event("session-1:event:2", "2026-07-10T12:00:00+03:00")
        existing = clockify_event("2026-07-10T10:00:00+03:00", "2026-07-10T10:30:00+03:00")
        run_dir, initial = self.make_run(
            [first, last, existing],
            analysis_for([first.evidence_id, last.evidence_id], recommended=120),
        )
        self.assertEqual(2, len(initial["proposals"]))
        activity_id = initial["proposals"][0]["activity_id"]
        corrections_path = run_dir.parent.parent / "review-corrections.jsonl"
        self.append_correction(corrections_path, initial["proposals"][0], "skip")

        rerun = pipeline.run_accounting(
            run_dir,
            root=ROOT,
            analysis_fixture=run_dir.parent.parent / "analysis.json",
            corrections_path=corrections_path,
        )

        self.assertFalse(any(row["activity_id"] == activity_id for row in rerun["proposals"]))
        self.assertFalse(any(row["activity_id"] == activity_id for row in rerun["allocation"]["allocations"]))
        preserved = [row for row in rerun["skipped"] if row.get("reason") == "preserved evidence-bound skip decision"]
        self.assertEqual(1, len(preserved))
        self.assertEqual(1, rerun["correction_regression"]["summary"]["fail"])

    def test_modify_mismatch_removes_segments_and_emits_one_exception(self):
        first = session_event("session-1:event:1", "2026-07-10T09:00:00+03:00")
        last = session_event("session-1:event:2", "2026-07-10T12:00:00+03:00")
        existing = clockify_event("2026-07-10T10:00:00+03:00", "2026-07-10T10:30:00+03:00")
        run_dir, initial = self.make_run(
            [first, last, existing],
            analysis_for([first.evidence_id, last.evidence_id], recommended=120),
        )
        activity_id = initial["proposals"][0]["activity_id"]
        corrections_path = run_dir.parent.parent / "review-corrections.jsonl"
        self.append_correction(
            corrections_path,
            initial["proposals"][0],
            "modify",
            field_patch={
                "description": {
                    "op": "replace",
                    "value": "SC — Repaired Clockify review process for accurate automatic timesheets",
                }
            },
        )

        rerun = pipeline.run_accounting(
            run_dir,
            root=ROOT,
            analysis_fixture=run_dir.parent.parent / "analysis.json",
            corrections_path=corrections_path,
        )

        self.assertFalse(any(row["activity_id"] == activity_id for row in rerun["proposals"]))
        self.assertFalse(any(row["activity_id"] == activity_id for row in rerun["allocation"]["allocations"]))
        failures = [row for row in rerun["ambiguous"] if row.get("exception_kind") == "correction_regression"]
        self.assertEqual(1, len(failures))
        self.assertEqual(activity_id, failures[0]["activity_id"])

    def test_matching_modify_correction_passes_and_keeps_all_segments(self):
        first = session_event("session-1:event:1", "2026-07-10T09:00:00+03:00")
        last = session_event("session-1:event:2", "2026-07-10T12:00:00+03:00")
        existing = clockify_event("2026-07-10T10:00:00+03:00", "2026-07-10T10:30:00+03:00")
        run_dir, initial = self.make_run(
            [first, last, existing],
            analysis_for([first.evidence_id, last.evidence_id], recommended=120),
        )
        corrections_path = run_dir.parent.parent / "review-corrections.jsonl"
        self.append_correction(
            corrections_path,
            initial["proposals"][0],
            "modify",
            field_patch={
                "description": {
                    "op": "replace",
                    "value": initial["proposals"][0]["description"],
                }
            },
        )

        rerun = pipeline.run_accounting(
            run_dir,
            root=ROOT,
            analysis_fixture=run_dir.parent.parent / "analysis.json",
            corrections_path=corrections_path,
        )

        self.assertEqual(2, len(rerun["proposals"]))
        self.assertEqual(1, rerun["correction_regression"]["summary"]["pass"])
        self.assertFalse(any(row.get("exception_kind") == "correction_regression" for row in rerun["ambiguous"]))

    def test_missing_modified_activity_is_a_visible_correction_exception(self):
        first = session_event("session-1:event:1", "2026-07-10T09:00:00+03:00")
        last = session_event("session-1:event:2", "2026-07-10T10:00:00+03:00")
        run_dir, initial = self.make_run(
            [first, last],
            analysis_for([first.evidence_id, last.evidence_id], recommended=30),
        )
        corrections_path = run_dir.parent.parent / "review-corrections.jsonl"
        self.append_correction(
            corrections_path,
            initial["proposals"][0],
            "modify",
            field_patch={
                "description": {
                    "op": "replace",
                    "value": initial["proposals"][0]["description"],
                }
            },
        )
        write_json(
            run_dir.parent.parent / "analysis.json",
            {
                "activities": [],
                "exceptions": [],
                "omissions": [{
                    "lifecycle": "noise",
                    "reason": "fixture removed the previously reviewed activity",
                    "evidence_ids": [first.evidence_id, last.evidence_id],
                }],
            },
        )

        rerun = pipeline.run_accounting(
            run_dir,
            root=ROOT,
            analysis_fixture=run_dir.parent.parent / "analysis.json",
            corrections_path=corrections_path,
        )

        failures = [row for row in rerun["ambiguous"] if row.get("exception_kind") == "correction_regression"]
        self.assertEqual(1, len(failures))
        self.assertIn("reviewed activity is missing", failures[0]["reason"])
        self.assertEqual(1, rerun["correction_regression"]["summary"]["fail"])

    def test_only_generalized_corrections_reach_pipeline_analyzer(self):
        first = session_event("session-1:event:1", "2026-07-10T09:00:00+03:00")
        last = session_event("session-1:event:2", "2026-07-10T10:00:00+03:00")
        run_dir, initial = self.make_run(
            [first, last],
            analysis_for([first.evidence_id, last.evidence_id], recommended=30),
        )
        corrections_path = run_dir.parent.parent / "review-corrections.jsonl"
        self.append_correction(corrections_path, initial["proposals"][0], "skip")

        with mock.patch.object(pipeline, "analyze_ledger", wraps=pipeline.analyze_ledger) as analyze:
            pipeline.run_accounting(
                run_dir,
                root=ROOT,
                analysis_fixture=run_dir.parent.parent / "analysis.json",
                corrections_path=corrections_path,
            )
        projected = analyze.call_args.kwargs["corrections"]
        rendered = json.dumps(projected)
        self.assertTrue(projected)
        self.assertNotIn("local_only", rendered)
        self.assertNotIn("expected_field_patch", rendered)
        self.assertNotIn("evidence_fingerprint", rendered)

    def test_honcho_workstream_splits_memory_reduction_from_rollout_plan(self):
        memory = session_event(
            "session-1:event:1",
            "2026-07-10T09:00:00+03:00",
            "Reduced Honcho memory use",
        )
        rollout = session_event(
            "session-1:event:2",
            "2026-07-10T10:00:00+03:00",
            "Wrote Honcho rollout plan",
        )
        analysis = analysis_for([memory.evidence_id], recommended=20)
        memory_activity = analysis["activities"][0]
        memory_activity.update({
            "workstream": "Honcho adoption",
            "action": "Reduced",
            "object": "Honcho memory use",
            "outcome": "for leaner local operations",
            "split_rationale": "memory reduction is an independent accomplishment",
        })
        rollout_activity = json.loads(json.dumps(memory_activity))
        rollout_activity.update({
            "workstream": "Honcho adoption",
            "action": "Wrote",
            "object": "Honcho rollout plan",
            "outcome": "for safer staged adoption",
            "evidence_ids": [rollout.evidence_id],
            "evidence_spans": [{
                "evidence_id": rollout.evidence_id,
                "start": "2026-07-10T10:00:00+03:00",
                "end": "2026-07-10T10:01:00+03:00",
            }],
            "split_rationale": "rollout planning is an independent accomplishment",
        })
        analysis["activities"].append(rollout_activity)

        _, result = self.make_run([memory, rollout], analysis)

        self.assertEqual(
            {
                "SC — Reduced Honcho memory use for leaner local operations",
                "SC — Wrote Honcho rollout plan for safer staged adoption",
            },
            {row["description"] for row in result["proposals"]},
        )

    def test_does_not_expand_short_effort_to_fill_empty_day(self):
        first = session_event("session-1:event:1", "2026-07-10T09:00:00+03:00")
        last = session_event("session-1:event:2", "2026-07-10T12:00:00+03:00")
        _, result = self.make_run(
            [first, last],
            analysis_for([first.evidence_id, last.evidence_id], recommended=20),
        )
        self.assertEqual(20, sum(row["duration_minutes"] for row in result["proposals"]))
        self.assertGreater(result["allocation"]["unallocated_capacity"]["total_minutes"], 100)

    def test_infeasible_effort_emits_contested_time_without_hiding_demand(self):
        first = session_event("session-1:event:1", "2026-07-10T09:00:00+03:00")
        last = session_event("session-1:event:2", "2026-07-10T10:00:00+03:00")
        _, result = self.make_run(
            [first, last],
            analysis_for([first.evidence_id, last.evidence_id], recommended=90),
        )
        contested = [row for row in result["ambiguous"] if row.get("exception_kind") == "contested_time"]
        self.assertEqual(1, len(contested))
        self.assertEqual(90, contested[0]["requested_minutes"])
        self.assertGreater(contested[0]["unallocated_minutes"], 0)

    def test_unrelated_workstream_does_not_widen_allocation_envelope(self):
        early_one = session_event("early:event:1", "2026-07-10T09:00:00+03:00")
        early_two = session_event("early:event:2", "2026-07-10T09:20:00+03:00")
        late_one = session_event("late:event:1", "2026-07-10T17:00:00+03:00")
        late_two = session_event("late:event:2", "2026-07-10T17:20:00+03:00")
        early_analysis = analysis_for(
            [early_one.evidence_id, early_two.evidence_id], recommended=60
        )
        late_activity = analysis_for(
            [late_one.evidence_id, late_two.evidence_id], recommended=10
        )["activities"][0]
        late_activity.update({
            "action": "Documented",
            "object": "Separate deployment checklist",
            "outcome": "for guarded release verification",
        })
        early_analysis["activities"].append(late_activity)

        _, result = self.make_run(
            [early_one, early_two, late_one, late_two], early_analysis
        )

        self.assertFalse(
            any(row["source_label"] == "Clockify review process" for row in result["proposals"])
        )
        contested = next(
            row for row in result["ambiguous"]
            if row.get("exception_kind") == "contested_time"
        )
        self.assertEqual(60, contested["unallocated_minutes"])
        self.assertIn(
            ["2026-07-10T09:00:00+03:00", "2026-07-10T09:21:00+03:00"],
            result["allocation"]["unallocated_capacity"]["intervals"],
        )

    def test_title_only_fathom_meeting_is_a_fixed_exception(self):
        meeting = fathom_event("2026-07-10T13:00:00+03:00", "2026-07-10T14:00:00+03:00")
        _, result = self.make_run([meeting], {"activities": [], "exceptions": [], "omissions": []})
        self.assertEqual([], result["proposals"])
        self.assertEqual("exception", result["fathom_reconciliation"][0]["status"])
        self.assertEqual("title_only", result["fathom_reconciliation"][0]["reason"])
        self.assertEqual(meeting.evidence_id, result["fathom_reconciliation"][0]["evidence_id"])
        self.assertEqual([meeting.evidence_id], result["fathom_reconciliation"][0]["source_evidence_ids"])
        self.assertEqual(1, len(result["fathom_reconciliation"][0]["fixed_block_ids"]))
        self.assertTrue(any(row.get("exception_kind") == "insufficient_meeting_evidence" for row in result["ambiguous"]))

    def test_eligible_fathom_meeting_is_an_exact_fixed_proposal(self):
        meeting = fathom_event(
            "2026-07-10T13:07:00+03:00",
            "2026-07-10T14:11:00+03:00",
            status="available",
        )
        _, result = self.make_run([meeting], meeting_analysis(meeting))

        self.assertEqual(1, len(result["proposals"]))
        proposal = result["proposals"][0]
        self.assertEqual("2026-07-10T13:07:00+03:00", proposal["start"])
        self.assertEqual("2026-07-10T14:11:00+03:00", proposal["end"])
        self.assertEqual(proposal["description"], proposal["rendered_description"])
        self.assertEqual("proposed", result["fathom_reconciliation"][0]["status"])
        self.assertEqual(meeting.evidence_id, result["fathom_reconciliation"][0]["evidence_id"])
        self.assertEqual([meeting.evidence_id], result["fathom_reconciliation"][0]["source_evidence_ids"])

    def test_accounting_uses_the_canonical_meeting_identity_for_fixed_time(self):
        meeting = fathom_event(
            "2026-07-10T13:07:00+03:00",
            "2026-07-10T14:11:00+03:00",
            status="available",
        )
        _, result = self.make_run([meeting], meeting_analysis(meeting))

        canonical = reconcile_meetings(
            [meeting.document()], [], vlad_identities={"vlad@serenichron.com"}
        ).meetings[0]
        reconciliation = result["fathom_reconciliation"][0]
        self.assertEqual(canonical.canonical_id, reconciliation["canonical_id"])
        self.assertEqual([meeting.evidence_id], reconciliation["source_evidence_ids"])

    def test_accounting_proposes_one_canonical_meeting_for_same_email_with_extra_name(self):
        """Accounting must retain both source receipts for one compatible person."""
        base_meeting = fathom_event(
            "2026-07-10T13:07:00+03:00",
            "2026-07-10T14:11:00+03:00",
            status="available",
        )
        meeting = evidence_ledger.evidence_event(
            "fathom",
            dict(base_meeting.source_ref),
            observed_at=base_meeting.observed_at,
            raw_source_span=dict(base_meeting.raw_source_span),
            attributes={
                **dict(base_meeting.attributes),
                "calendar_invitees": [
                    {"email": "vlad@serenichron.com"},
                    {"email": "prospect@example.test", "name": "Prospect"},
                ],
            },
        )
        calendly = calendly_event(
            "2026-07-10T10:07:00Z", "2026-07-10T11:11:00Z"
        )
        analysis = meeting_analysis(meeting)
        analysis["activities"][0]["project_recommendation"] = {
            "name": "Serenichron Level 1",
            "prefix": "SC",
            "tag_names": ["Project Management"],
        }
        analysis["activities"][0]["evidence_ids"].append(calendly.evidence_id)
        analysis["activities"][0]["evidence_spans"].append({
            "evidence_id": calendly.evidence_id,
            "start": "2026-07-10T10:07:00Z",
            "end": "2026-07-10T11:11:00Z",
        })

        _, result = self.make_run([meeting, calendly], analysis)

        self.assertEqual(1, len(result["proposals"]))
        self.assertEqual(
            {meeting.evidence_id, calendly.evidence_id},
            set(result["proposals"][0]["provenance"]["evidence_ids"]),
        )

    def test_timestamped_meeting_activities_become_conserved_fixed_splits(self):
        meeting = fathom_event(
            "2026-07-10T13:00:00+03:00",
            "2026-07-10T13:37:00+03:00",
            status="available",
        )
        calendly = calendly_event(
            "2026-07-10T10:00:00Z", "2026-07-10T10:37:00Z"
        )
        analysis = meeting_analysis(meeting)
        first = analysis["activities"][0]
        first["evidence_spans"] = [{
            "evidence_id": meeting.evidence_id,
            "start": "2026-07-10T13:00:00+03:00",
            "end": "2026-07-10T13:20:00+03:00",
        }]
        second = json.loads(json.dumps(first))
        second["object"] = "Internal project handoff"
        second["project_recommendation"] = {
            "name": "Serenichron Level 1",
            "prefix": "SC",
            "tag_names": ["Project Management"],
        }
        second["evidence_spans"] = [{
            "evidence_id": calendly.evidence_id,
            "start": "2026-07-10T13:20:00+03:00",
            "end": "2026-07-10T13:37:00+03:00",
        }]
        second["evidence_ids"] = [calendly.evidence_id]
        analysis["activities"].append(second)

        _, result = self.make_run([meeting, calendly], analysis)

        self.assertEqual(2, len(result["proposals"]))
        self.assertEqual(37, sum(item["duration_minutes"] for item in result["proposals"]))
        self.assertEqual(
            ["2026-07-10T13:00:00+03:00", "2026-07-10T13:20:00+03:00"],
            [item["start"] for item in result["proposals"]],
        )

    def test_ambiguous_canonical_sources_are_quarantined_without_booking(self):
        meeting = fathom_event(
            "2026-07-10T13:00:00+03:00",
            "2026-07-10T13:37:00+03:00",
            status="available",
        )
        conflict = calendly_event(
            "2026-07-10T10:08:00Z", "2026-07-10T10:45:00Z"
        )
        analysis = meeting_analysis(meeting)
        analysis["activities"][0]["project_recommendation"] = {
            "name": "Serenichron Level 1", "prefix": "SC",
            "tag_names": ["Project Management"],
        }
        calendar_activity = json.loads(json.dumps(analysis["activities"][0]))
        calendar_activity["evidence_ids"] = [conflict.evidence_id]
        calendar_activity["evidence_spans"] = [{
            "evidence_id": conflict.evidence_id,
            "start": "2026-07-10T10:08:00Z",
            "end": "2026-07-10T10:45:00Z",
        }]
        analysis["activities"].append(calendar_activity)

        _, result = self.make_run([meeting, conflict], analysis)

        self.assertEqual([], result["proposals"])
        exception = next(
            row for row in result["ambiguous"]
            if row.get("exception_kind") == "canonical_meeting_reconciliation"
        )
        self.assertEqual(
            {meeting.evidence_id, conflict.evidence_id}, set(exception["evidence_ids"])
        )
        self.assertFalse(any(
            row["status"] == "proposed" for row in result["fathom_reconciliation"]
        ))

    def test_rejected_split_member_cannot_fall_back_to_a_full_meeting(self):
        meeting = fathom_event(
            "2026-07-10T13:00:00+03:00",
            "2026-07-10T13:37:00+03:00",
            status="available",
        )
        calendly = calendly_event(
            "2026-07-10T10:00:00Z", "2026-07-10T10:37:00Z"
        )
        analysis = meeting_analysis(meeting)
        first = analysis["activities"][0]
        first["evidence_spans"] = [{
            "evidence_id": meeting.evidence_id,
            "start": "2026-07-10T13:00:00+03:00",
            "end": "2026-07-10T13:20:00+03:00",
        }]
        rejected = json.loads(json.dumps(first))
        rejected["evidence_ids"] = [calendly.evidence_id]
        rejected["evidence_spans"] = [{
            "evidence_id": calendly.evidence_id,
            "start": "2026-07-10T13:20:00+03:00",
            "end": "2026-07-10T13:37:00+03:00",
        }]
        rejected["project_recommendation"] = {
            "name": "TST Prep Level 1", "prefix": "TSTP",
            "tag_names": ["Project Management"],
        }
        analysis["activities"].append(rejected)

        _, result = self.make_run([meeting, calendly], analysis)

        self.assertEqual([], result["proposals"])
        self.assertEqual(1, sum(
            row.get("exception_kind") == "invalid_meeting_split"
            for row in result["ambiguous"]
        ))

    def test_split_boundary_rejects_unrelated_or_ambiguous_parent_evidence(self):
        meeting = fathom_event(
            "2026-07-10T13:00:00+03:00",
            "2026-07-10T13:37:00+03:00",
            status="available",
        )
        canonical = reconcile_meetings(
            [meeting.document()], [], vlad_identities={"vlad@serenichron.com"}
        ).meetings[0]
        route = {"project_name": "Serenichron Level 2", "tag_names": ["Processes"]}
        unrelated = session_event("other:event", "2026-07-10T13:00:00+03:00")
        valid_activity = meeting_analysis(meeting)["activities"][0]
        valid_activity["evidence_spans"] = [{
            "start": "2026-07-10T13:00:00+03:00",
            "end": "2026-07-10T13:20:00+03:00",
        }]
        split, _, _ = pipeline._meeting_split_candidate(
            canonical, valid_activity, route, [meeting.evidence_id], [meeting.evidence_id], 0
        )
        self.assertTrue(split.evidence_ids[0].startswith(meeting.evidence_id + ":"))

        for activity, evidence_ids in (
            (
                {**valid_activity, "evidence_spans": [{
                    "evidence_id": unrelated.evidence_id,
                    "start": "2026-07-10T13:00:00+03:00",
                    "end": "2026-07-10T13:20:00+03:00",
                }]},
                [meeting.evidence_id, unrelated.evidence_id],
            ),
            (
                {**valid_activity, "evidence_spans": [{
                    "start": "2026-07-10T13:00:00+03:00",
                    "end": "2026-07-10T13:20:00+03:00",
                }]},
                [meeting.evidence_id, unrelated.evidence_id],
            ),
        ):
            with self.subTest(evidence_ids=evidence_ids):
                with self.assertRaisesRegex(MeetingReconciliationError, "canonical source"):
                    pipeline._meeting_split_candidate(
                        canonical, activity, route, evidence_ids,
                        [meeting.evidence_id], 0,
                    )

    def test_fathom_correction_never_leaves_removed_meeting_marked_proposed(self):
        for decision, expected_status, expected_reason in (
            ("skip", "excluded", "review_correction_skip"),
            ("modify", "exception", "correction_regression"),
        ):
            with self.subTest(decision=decision):
                meeting = fathom_event(
                    "2026-07-10T13:07:00+03:00",
                    "2026-07-10T14:11:00+03:00",
                    status="available",
                )
                run_dir, initial = self.make_run([meeting], meeting_analysis(meeting))
                corrections_path = run_dir.parent.parent / "review-corrections.jsonl"
                patch = None
                if decision == "modify":
                    patch = {
                        "description": {
                            "op": "replace",
                            "value": "SC — Defined prospect meeting outcome for corrected client planning",
                        }
                    }
                self.append_correction(
                    corrections_path,
                    initial["proposals"][0],
                    decision,
                    field_patch=patch,
                )

                rerun = pipeline.run_accounting(
                    run_dir,
                    root=ROOT,
                    analysis_fixture=run_dir.parent.parent / "analysis.json",
                    corrections_path=corrections_path,
                )

                self.assertEqual([], rerun["proposals"])
                reconciliation = rerun["fathom_reconciliation"][0]
                self.assertEqual(expected_status, reconciliation["status"])
                self.assertEqual(expected_reason, reconciliation["reason"])

    def test_meeting_not_recorded_by_or_attended_by_vlad_is_excluded(self):
        meeting = fathom_event(
            "2026-07-10T13:07:00+03:00",
            "2026-07-10T14:11:00+03:00",
            status="available",
        )
        document = meeting.document()
        attributes = dict(document["attributes"])
        attributes["recorded_by_email"] = "other@example.test"
        attributes["calendar_invitees"] = [
            {"email": "prospect@example.test", "is_external": True}
        ]
        other_meeting = evidence_ledger.evidence_event(
            "fathom",
            dict(document["source_ref"]),
            observed_at=document.get("observed_at"),
            raw_source_span=dict(document["raw_source_span"]),
            attributes=attributes,
        )
        _, result = self.make_run([other_meeting], {"activities": [], "exceptions": [], "omissions": []})
        self.assertEqual([], result["proposals"])
        self.assertEqual("excluded", result["fathom_reconciliation"][0]["status"])
        self.assertEqual("not_vlads_meeting", result["fathom_reconciliation"][0]["reason"])

    def test_meeting_with_unknown_ownership_and_no_vlad_attendee_is_excluded(self):
        meeting = fathom_event(
            "2026-07-10T13:07:00+03:00",
            "2026-07-10T14:11:00+03:00",
            status="available",
        )
        document = meeting.document()
        attributes = dict(document["attributes"])
        attributes.pop("recorded_by_email")
        attributes["calendar_invitees"] = [
            {"email": "prospect@example.test", "is_external": True}
        ]
        unknown_meeting = evidence_ledger.evidence_event(
            "fathom",
            dict(document["source_ref"]),
            observed_at=document.get("observed_at"),
            raw_source_span=dict(document["raw_source_span"]),
            attributes=attributes,
        )

        _, result = self.make_run(
            [unknown_meeting],
            {"activities": [], "exceptions": [], "omissions": []},
        )

        self.assertEqual([], result["proposals"])
        self.assertEqual("excluded", result["fathom_reconciliation"][0]["status"])
        self.assertEqual(
            "unknown_meeting_ownership",
            result["fathom_reconciliation"][0]["reason"],
        )

    def test_matching_clockify_block_reconciles_fathom_meeting_without_proposal(self):
        meeting = fathom_event(
            "2026-07-10T13:00:00+03:00",
            "2026-07-10T14:00:00+03:00",
            status="available",
        )
        existing = clockify_event(
            "2026-07-10T13:00:00+03:00", "2026-07-10T14:00:00+03:00"
        )
        _, result = self.make_run([meeting, existing], meeting_analysis(meeting))

        self.assertEqual([], result["proposals"])
        reconciliation = result["fathom_reconciliation"][0]
        self.assertEqual("reconciled", reconciliation["status"])
        self.assertEqual("existing_clockify_meeting_match", reconciliation["reason"])
        self.assertEqual(meeting.evidence_id, reconciliation["evidence_id"])
        self.assertEqual([meeting.evidence_id], reconciliation["source_evidence_ids"])

    def test_partial_clockify_overlap_is_fixed_block_conflict_not_proposal(self):
        meeting = fathom_event(
            "2026-07-10T13:00:00+03:00",
            "2026-07-10T14:00:00+03:00",
            status="available",
        )
        existing = clockify_event(
            "2026-07-10T13:30:00+03:00", "2026-07-10T14:30:00+03:00"
        )
        _, result = self.make_run([meeting, existing], meeting_analysis(meeting))

        self.assertEqual([], result["proposals"])
        reconciliation = result["fathom_reconciliation"][0]
        self.assertEqual("exception", reconciliation["status"])
        self.assertEqual("meeting_overlap", reconciliation["reason"])
        self.assertEqual(meeting.evidence_id, reconciliation["evidence_id"])
        self.assertEqual([meeting.evidence_id], reconciliation["source_evidence_ids"])
        conflict = next(
            row for row in result["ambiguous"]
            if row.get("exception_kind") == "fixed_block_conflict"
        )
        self.assertEqual("meeting_overlap", conflict["conflict_reason"])
        self.assertFalse(any(
            row.get("exception_kind") == "insufficient_meeting_evidence"
            for row in result["ambiguous"]
        ))

    def test_incomplete_coordinator_source_blocks_semantic_accounting(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        run_dir = Path(temp.name) / "run-partial"
        event = session_event("session-1:event:1", "2026-07-10T09:00:00+03:00")
        ledger = evidence_ledger.EvidenceLedger(
            (event,),
            {
                "clockify": {"status": "complete"},
                "fathom": {"status": "complete"},
                "sessions/macbook": {"status": "complete"},
                "sessions/omarchy-precision": {
                    "status": "partial",
                    "reason": "legacy metadata fallback",
                },
            },
        )
        write_json(
            run_dir / "evidence" / "evidence-ledger.json",
            {
                "schema_version": ledger.manifest.schema_version,
                "manifest": ledger.manifest.document(),
                "events": [event.document()],
            },
        )
        fixture = Path(temp.name) / "analysis.json"
        write_json(fixture, analysis_for([event.evidence_id]))
        with self.assertRaisesRegex(pipeline.WorkAccountingError, "incomplete"):
            pipeline.run_accounting(run_dir, root=ROOT, analysis_fixture=fixture)

    def test_incomplete_peer_source_allows_available_evidence_accounting(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        run_dir = Path(temp.name) / "run-peer-partial"
        event = session_event("session-1:event:1", "2026-07-10T09:00:00+03:00")
        ledger = evidence_ledger.EvidenceLedger(
            (event,),
            {
                "clockify": {"status": "complete"},
                "fathom": {"status": "complete"},
                "multica_issues": {"status": "complete"},
                "sessions/omarchy-precision": {"status": "complete"},
                "repositories/omarchy-precision": {"status": "complete"},
                "sessions/macbook": {"status": "unavailable"},
            },
        )
        write_json(
            run_dir / "evidence" / "evidence-ledger.json",
            {
                "schema_version": ledger.manifest.schema_version,
                "manifest": ledger.manifest.document(),
                "events": [event.document()],
            },
        )
        fixture = Path(temp.name) / "analysis.json"
        write_json(fixture, analysis_for([event.evidence_id], recommended=10))

        result = pipeline.run_accounting(run_dir, root=ROOT, analysis_fixture=fixture)

        self.assertIn("semantic_analysis", result)
        self.assertEqual("sessions/macbook", result["coverage_warnings"][0]["source"])

    def test_replay_is_byte_stable_for_unchanged_inputs_and_versions(self):
        first = session_event("session-1:event:1", "2026-07-10T09:00:00+03:00")
        last = session_event("session-1:event:2", "2026-07-10T10:00:00+03:00")
        run_dir, first_result = self.make_run(
            [first, last],
            analysis_for([first.evidence_id, last.evidence_id], recommended=30),
        )
        fixture = run_dir.parents[1] / "analysis.json"
        second_result = pipeline.run_accounting(run_dir, root=ROOT, analysis_fixture=fixture)
        self.assertEqual(first_result, second_result)
        expected = json.dumps(first_result, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        self.assertEqual(expected, (run_dir / "work-accounting-result.json").read_text(encoding="utf-8"))
        semantic_result = json.loads((run_dir / "semantic-analysis.json").read_text())
        self.assertEqual(
            first_result["proposals"][0]["description"],
            semantic_result["activities"][0]["rendered_description"],
        )

    def test_completion_marker_is_published_after_required_artifacts(self):
        first = session_event("session-1:event:1", "2026-07-10T09:00:00+03:00")
        last = session_event("session-1:event:2", "2026-07-10T10:00:00+03:00")
        with mock.patch.object(
            pipeline, "_write_json", wraps=pipeline._write_json
        ) as writer:
            self.make_run(
                [first, last],
                analysis_for([first.evidence_id, last.evidence_id], recommended=30),
            )

        self.assertEqual(
            "work-accounting-result.json",
            writer.call_args_list[-1].args[0].name,
        )

    def test_correction_log_is_integrity_checked_and_generalized_before_analysis(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "review-corrections.jsonl"
        item = {
            "id": "rvi-one",
            "current": {"activity_id": "act-one", "evidence_ids": ["ev-one"]},
        }
        decision = review_corrections.build_decision(
            item,
            decision="modify",
            reviewer="reviewer",
            reviewed_at="2026-08-01T10:00:00+03:00",
            correction_categories=["wording"],
            rationale="Remove private@example.test from the description.",
            field_patch={
                "description": {
                    "op": "replace",
                    "value": "SC — Rebuilt concise work description",
                }
            },
        )
        review_corrections.append_decision(path, decision, item=item)
        cases = pipeline._load_corrections(path)
        regression_cases = pipeline._load_regression_cases(path)
        self.assertEqual(1, len(cases))
        self.assertEqual(1, len(regression_cases))
        self.assertTrue(regression_cases[0]["local_only"])
        rendered = json.dumps(cases)
        self.assertNotIn("private@example.test", rendered)
        self.assertNotIn("Rebuilt concise", rendered)
        self.assertEqual("wording", cases[0]["category"])

        line = json.loads(path.read_text(encoding="utf-8"))
        line["rationale"] = "tampered"
        path.write_text(json.dumps(line) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(pipeline.WorkAccountingError, "invalid"):
            pipeline._load_corrections(path)

    def test_load_ledger_preserves_digest_bound_timezone(self):
        with tempfile.TemporaryDirectory() as temp:
            event = evidence_ledger.evidence_event(
                "fathom", {"source_type": "fathom", "source_id": "meeting-one"},
                raw_source_span={"start": "2026-07-10 09:00", "end": "2026-07-10 09:30"},
                attributes={"recorded_by_email": "vlad@serenichron.com", "meeting_id": "one", "title": "Review"},
            )
            ledger = evidence_ledger.EvidenceLedger((event,), {"fathom": {"status": "complete"}}, "Europe/Bucharest")
            path = Path(temp) / "evidence-ledger.json"
            write_json(path, {"schema_version": ledger.manifest.schema_version, "manifest": ledger.manifest.document(), "events": [event.document()]})

            loaded, _ = pipeline.load_ledger(path)

        self.assertEqual("Europe/Bucharest", loaded.timezone)
        self.assertEqual(ledger.manifest.manifest_id, loaded.manifest.manifest_id)


if __name__ == "__main__":
    unittest.main()
