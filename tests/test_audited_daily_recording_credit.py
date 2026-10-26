"""Audited GET-backed Daily intersections are review timing, not posting approval."""
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_work_accounting_pipeline as fixtures
import test_clockify_native_sheet_post as native_fixtures
from scripts import clockify_checkpoint_snapshot as snapshots, collector_checkpoints
from scripts import clockify_native_sheet_post as native, clockify_pending_review_selection as pending
from scripts import clockify_sheet_publish as publisher, evidence_ledger, review_corrections
from scripts import work_accounting_pipeline as pipeline


KEY = "audited_daily_recording_intersections"
RECIPE = "audited-daily-recording-intersections/v1"


class AuditedDailyRecordingCreditTests(unittest.TestCase):
    def fixture(self, root, *, native_patch=None, recording_title="Daily Meet", duplicate_native=False):
        collector = pipeline.collector
        since, until = dt.datetime(2026, 10, 19, tzinfo=dt.UTC), dt.datetime(2026, 10, 21, tzinfo=dt.UTC)
        entries = [{"id": f"native-daily-{day}", "workspaceId": "workspace-1", "userId": "user-1",
                    "projectId": "project-89dab8", "tagIds": ["tag-35aa9aef"], "taskId": None,
                    "description": "Daily Meet", "billable": True, "type": "REGULAR",
                    "timeInterval": {"start": start, "end": end, "duration": duration}}
                   for day, start, end, duration in (
                       (19, "2026-10-19T10:36:05Z", "2026-10-19T11:57:35Z", "PT1H21M30S"),
                       (20, "2026-10-20T10:30:00Z", "2026-10-20T11:15:00Z", "PT45M"))]
        if native_patch:
            entries[0].update(native_patch)
        if duplicate_native:
            entries.append({**copy.deepcopy(entries[0]), "id": "native-daily-duplicate"})
        store = collector_checkpoints.PageCheckpointStore(root / "cache")
        state = store.open(collector._clockify_checkpoint_identity("workspace-1", "user-1", since, until),
                           initial_metadata={"snapshot_at": until.isoformat()})
        state = store.append_page(state, payload=entries, continuation={"page": 2},
                                  signature=collector._clockify_page_signature(entries))
        state = store.mark_complete(state)
        with mock.patch.object(collector, "clockify_get", side_effect=AssertionError("No live GET")):
            projection = collector.fetch_clockify({"CLOCKIFY_WORKSPACE_ID": "workspace-1"},
                {"clockify_user_id": "user-1"}, since, until, checkpoint_store=store,
                checkpoint_identity=state.identity)
        original = root / "original/evidence/clockify-existing.json"
        original.parent.mkdir(parents=True)
        fixtures.write_json(original, projection)
        run = root / "run"
        captured = snapshots.capture_checkpoint_snapshot(checkpoint_manifest=state.directory / "manifest.json",
            clockify_evidence=original, destination=run / "evidence/clockify-native-checkpoint",
            workspace_id="workspace-1", user_id="user-1", since=since, until=until)
        (run / "evidence/clockify-existing.json").write_bytes(original.read_bytes())
        recordings = [evidence_ledger.evidence_event("fathom", {"source_type": "fathom", "source_id": f"daily-{day}"},
            observed_at=start, raw_source_span={"start": start, "end": end},
            attributes={"title": recording_title, "semantic_evidence_status": "available",
                "recorded_by_email": "member@example.test", "calendar_invitees": [{"email": "member@example.test"}],
                "share_url": f"https://fathom.video/share/daily-{day}", "summary": "Recorded team alignment"})
            for day, start, end in ((19, "2026-10-19T10:42:01Z", "2026-10-19T12:02:27Z"),
                                    (20, "2026-10-20T10:30:18Z", "2026-10-20T11:54:26Z"))]
        task = fixtures.claude_event("2026-10-19T11:58:00Z", content="Completed independent task")
        events = [*recordings, task, *evidence_ledger.normalize_collector_snapshot({"clockify": projection})]
        ledger = evidence_ledger.EvidenceLedger(tuple(events), {name: {"status": "complete"}
            for name in ("clockify", "fathom", "multica_issues")}, timezone="Europe/Bucharest",
            member_identities=("member@example.test",))
        ledger_doc = {"schema_version": ledger.manifest.schema_version, "manifest": ledger.manifest.document(),
                      "events": [e.document() for e in ledger.events]}
        fixtures.write_json(run / "evidence/evidence-ledger.json", ledger_doc)
        fixtures.write_json(run / "run-report.json", {"date_range": {"since": since.isoformat(), "until": until.isoformat()},
            "clockify_native_checkpoint": {"manifest_sha256": captured.manifest_sha256, "request": captured.manifest["request"]}})
        routing = {"workspace_id": "workspace-1", "clockify_user_id": "user-1", "member_id": "user-1",
            "timezone": "Europe/Bucharest", "session_routes": [{"pattern": "session", "project_name": "Serenichron Level 2",
                "prefix": "SC", "project_suffix": "777777", "tag_names": ["Processes"], "tag_suffixes": ["11111111"], "billable": True}],
            "meeting_routes": [{"title_regex": "^Daily Meet$", "project_name": "Daily meetings", "prefix": "SC",
                "project_suffix": "89dab8", "tag_names": ["Project Management"], "tag_suffixes": ["35aa9aef"], "billable": True}]}
        analysis = {"activities": [], "exceptions": [], "omissions": []}
        for recording in recordings:
            activity = fixtures.meeting_analysis(recording)["activities"][0]
            activity.update(action="Reviewed", object="daily team alignment", outcome="confirmed delivery priorities",
                project_recommendation={"name": "Daily meetings", "prefix": "SC", "tag_names": ["Project Management"]})
            analysis["activities"].append(activity)
        task_activity = fixtures.analysis_for([task.evidence_id], recommended=1)["activities"][0]
        task_activity["evidence_spans"] = [{"evidence_id": task.evidence_id, "start": "2026-10-19T11:58:00Z", "end": "2026-10-19T11:59:00Z"}]
        analysis["activities"].append(task_activity)
        fixtures.write_json(root / "analysis.json", analysis)
        fixtures.write_json(run / "routing.json", routing)
        meetings, _ = pipeline._recording_events(ledger_doc["events"], ledger_doc["manifest"])
        clockify = [e for e in ledger_doc["events"] if e["source_type"] == "clockify"]
        pairs = []
        for entry in entries:
            if entry["id"] == "native-daily-duplicate":
                continue
            day = entry["timeInterval"]["start"][:10]
            meeting = next(e for e in meetings if e["meeting"].start[:10] == day)
            event = next(e for e in clockify if e["attributes"]["id_suffix"] == entry["id"][-8:])
            pairs.append({"same_meeting_confirmed": True, "canonical_meeting_id": meeting["meeting"].canonical_id,
                "recording_evidence_fingerprint": review_corrections.evidence_fingerprint(meeting["source_evidence_ids"]),
                "clockify_evidence_id": event["evidence_id"], "clockify_entry_id": entry["id"],
                "clockify_entry_sha256": "sha256:" + hashlib.sha256(pipeline.semantic_analyzer.canonical_json(entry).encode()).hexdigest(),
                "local_date": day, "project_suffix": "89dab8"})
        declaration = {"schema_version": RECIPE, "workspace_id": "workspace-1", "member_id": "user-1",
                       "collection_snapshot_sha256": captured.manifest_sha256, "pairs": pairs}
        return run, routing, declaration, ledger_doc, entries

    def account(self, root, run, routing):
        fixtures.write_json(run / "routing.json", routing)
        with mock.patch.object(pipeline.semantic_analyzer, "http_transport", side_effect=AssertionError("No provider")):
            return pipeline.run_accounting(run, root=fixtures.ROOT, routing_path=run / "routing.json",
                                           analysis_fixture=root / "analysis.json")

    def test_exact_audited_intersections_preserve_sources_and_replay_pending_timing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run, routing, declaration, ledger, entries = self.fixture(root)
            baseline = self.account(root, run, routing)
            native_before = copy.deepcopy(entries)
            ledger_before = (run / "evidence/evidence-ledger.json").read_bytes()
            routing[KEY] = declaration
            result = self.account(root, run, routing)
            daily = [p for p in result["proposals"] if p["client_project"] == "Daily meetings"]
            self.assertEqual([292, 2366], [p["duration_seconds"] for p in daily])
            from scripts import clockify_sync_quality as quality
            self.assertEqual([], quality.find_time_overlaps(result["proposals"],
                json.loads((run / "evidence/clockify-existing.json").read_text())["entries"]))
            original = [p for p in baseline["proposals"] if p["client_project"] == "Daily meetings"]
            for before, after, credited in zip(original, daily, (4534, 2682)):
                receipt = after["provenance"]["credited_overlap_receipt"]
                self.assertEqual(credited, receipt["credited_seconds"])
                self.assertEqual(before["duration_seconds"], credited + after["duration_seconds"])
                self.assertEqual(before["start"], receipt["original_start"])
                self.assertEqual(before["end"], receipt["original_end"])
                for key in ("activity_id", "candidate_key", "review_activity_key", "allocation_segment", "source", "effort"):
                    self.assertEqual(before[key], after[key], key)
                self.assertEqual(before["provenance"]["canonical_meeting_id"], after["provenance"]["canonical_meeting_id"])
                self.assertNotIn("verified_posted_credit", after["provenance"])
            nonmeeting = [p for p in result["proposals"] if p["client_project"] != "Daily meetings"]
            self.assertEqual([{k: v for k, v in p.items() if k != "review_warnings"} for p in baseline["proposals"] if p["client_project"] != "Daily meetings"],
                             [{k: v for k, v in p.items() if k != "review_warnings"} for p in nonmeeting])
            source = {"ledger": ledger, "accounting": result, "routing": routing,
                      "artifacts": {"receipt": {"path": str(run / "completion-bundle.json")}}}
            selected = [{"proposal": p, "source": source, "review_id": publisher.stable_review_id(p)} for p in daily]
            normalized, proof = pending._credits(selected, {"source": source})
            self.assertEqual(2658, proof["saved_credit_seconds"])
            self.assertEqual(daily, normalized)
            self.assertTrue(all(c["recipe"] == RECIPE for c in proof["fixed_recording_checks"]))
            rows = [publisher.proposal_row(p, run.name, project_allowlist=publisher.project_allowlist(routing)) for p in daily]
            plan = native.build_plan(native_fixtures.capture(*rows), capture_sha256="c" * 64,
                routing=routing, routing_sha256="r" * 64, timezone="Europe/Bucharest",
                workspace_id="workspace-1", member_id="user-1", projects=[{"id": "project-89dab8"}, {"id": "project-777777"}],
                tags=[{"id": "tag-35aa9aef"}, {"id": "tag-11111111"}], live_entries=entries)
            self.assertAlmostEqual(2658 / 60, plan["total_minutes"])
            self.assertTrue(all(row[9] == "pending" and row[13] == "unposted" for row in rows))
            self.assertEqual(native_before, entries)
            self.assertEqual(ledger_before, (run / "evidence/evidence-ledger.json").read_bytes())
            frozen = (run / "work-accounting-result.json").read_bytes()
            replay = self.account(root, run, copy.deepcopy(routing))
            self.assertEqual(result, replay)
            self.assertEqual(frozen, (run / "work-accounting-result.json").read_bytes())

    def test_missing_section_is_byte_identical_and_unconfirmed_or_tampered_pairs_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run, routing, declaration, _, _ = self.fixture(root)
            self.account(root, run, routing)
            before = (run / "work-accounting-result.json").read_bytes()
            self.account(root, run, copy.deepcopy(routing))
            self.assertEqual(before, (run / "work-accounting-result.json").read_bytes())
            for field, value in (("same_meeting_confirmed", False), ("canonical_meeting_id", "cm-wrong"),
                    ("recording_evidence_fingerprint", "foreign"), ("clockify_evidence_id", "foreign"),
                    ("clockify_entry_id", "foreign"), ("clockify_entry_sha256", "sha256:" + "0" * 64),
                    ("local_date", "2026-10-18"), ("project_suffix", "777777"),
                    ("canonical_meeting_id", []), ("clockify_entry_id", []), ("local_date", {})):
                bad = copy.deepcopy(declaration)
                bad["pairs"][0][field] = value
                with self.subTest(field=field), self.assertRaises(pipeline.WorkAccountingError):
                    self.account(root, run, {**routing, KEY: bad})
            for patch in ({"member_id": "wrong"}, {"workspace_id": "wrong"},
                          {"collection_snapshot_sha256": "0" * 64}, {"pairs": declaration["pairs"] * 2}):
                with self.subTest(patch=list(patch)), self.assertRaises(pipeline.WorkAccountingError):
                    self.account(root, run, {**routing, KEY: {**declaration, **patch}})

    def test_coherent_non_daily_ambiguous_nonoverlapping_and_missing_native_sources_reject(self):
        variants = [
            {"recording_title": "Different meeting"},
            {"native_patch": {"description": "Different meeting"}},
            {"native_patch": {"type": "BREAK"}},
            {"native_patch": {"projectId": "project-777777"}},
            {"native_patch": {"timeInterval": {"start": "2026-10-19T08:00:00Z", "end": "2026-10-19T09:00:00Z", "duration": "PT1H"}}},
            {"duplicate_native": True},
        ]
        for variant in variants:
            with self.subTest(variant=variant), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                run, routing, declaration, _, _ = self.fixture(root, **variant)
                with self.assertRaises(pipeline.WorkAccountingError):
                    self.account(root, run, {**routing, KEY: declaration})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run, routing, declaration, _, _ = self.fixture(root)
            (run / "evidence/clockify-native-checkpoint/snapshot.json").unlink()
            with self.assertRaises(pipeline.WorkAccountingError):
                self.account(root, run, {**routing, KEY: declaration})

    def test_nonmeeting_overlapping_task_is_not_credited_and_receipt_tamper_fails_consumer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run, routing, declaration, ledger, _ = self.fixture(root)
            baseline = self.account(root, run, routing)
            routing[KEY] = declaration
            snapshot = pipeline._accounting_collection_snapshot(run, ledger["events"])
            pairs = pipeline._audited_daily_recording_pairs(ledger["events"], ledger["manifest"], routing, snapshot)
            task = pipeline._proposal({"activity_id": "act-independent", "workstream_id": "ws-independent"},
                routing["session_routes"][0], "SC — Completed independent delivery task", pipeline._parse_dt("2026-10-19T11:58:00Z"),
                pipeline._parse_dt("2026-10-19T11:59:00Z"), [ledger["events"][0]["evidence_id"]], 1)
            inputs = [*baseline["proposals"], task]
            output = pipeline._apply_audited_daily_recording_intersections(inputs, pairs, pipeline._existing_blocks(ledger["events"]))
            self.assertEqual(task, next(p for p in output if p["activity_id"] == "act-independent"))
            result = self.account(root, run, routing)
            source = {"ledger": ledger, "accounting": result, "routing": routing,
                      "artifacts": {"receipt": {"path": str(run / "completion-bundle.json")}}}
            target = next(p for p in result["proposals"] if p["client_project"] == "Daily meetings")
            for mutation in ("recipe", "receipt", "native_duration", "saved_recipe", "routing"):
                altered = copy.deepcopy(source)
                proposal = next(p for p in altered["accounting"]["proposals"] if p["candidate_key"] == target["candidate_key"])
                if mutation == "recipe":
                    proposal["provenance"]["audited_daily_recording_credit"]["credited_seconds"] += 1
                elif mutation == "receipt":
                    proposal["provenance"]["credited_overlap_receipt"]["credited_seconds"] += 1
                elif mutation == "native_duration":
                    proposal["duration_seconds"] += 1
                elif mutation == "saved_recipe":
                    altered["accounting"]["allocation"]["deterministic_inputs"].pop(KEY)
                else:
                    altered["routing"][KEY]["pairs"][0]["clockify_entry_sha256"] = "sha256:" + "0" * 64
                with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                    pending._credits([{"proposal": proposal, "source": altered,
                        "review_id": publisher.stable_review_id(proposal)}], {"source": altered})


if __name__ == "__main__":
    unittest.main()
