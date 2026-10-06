"""Offline native-accounting regressions for factual recorded attendance."""
import unittest
import copy
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import os
import base64

from scripts import clockify_sheet_publish as publisher
from scripts import clockify_sync_quality as quality
from scripts import work_accounting_pipeline as pipeline

import test_work_accounting_pipeline as fixtures

fathom_event = fixtures.fathom_event
meeting_analysis = fixtures.meeting_analysis


class RecordedMeetingFallbackTests(unittest.TestCase):
    make_run = fixtures.WorkAccountingPipelineTests.make_run

    def empty_analysis(self, events):
        return {"activities": [], "exceptions": [{
            "kind": "semantic_review_failure", "reason": "review unavailable",
            "evidence_ids": [event.evidence_id for event in events],
        }], "omissions": []}

    def recording(self, identity, start, end, title="Discovery call"):
        base = fathom_event(start, end, status="available")
        return fixtures.evidence_ledger.evidence_event(
            "fathom", {"source_type": "fathom", "source_id": identity},
            observed_at=start, raw_source_span=dict(base.raw_source_span),
            attributes={**base.attributes, "meeting_id": identity, "title": title,
                        "transcript": [{"text": "Recorded meeting attendance."}]},
        )

    def test_same_recording_from_two_providers_has_one_fallback(self):
        meeting = fathom_event("2026-07-10T13:00:00+03:00",
                               "2026-07-10T14:00:00+03:00", status="available")
        calendly = fixtures.calendly_event("2026-07-10T10:00:00Z", "2026-07-10T11:00:00Z")
        _, result = self.make_run([meeting, calendly], self.empty_analysis([meeting, calendly]))
        self.assertEqual(1, len(result["proposals"]))
        self.assertEqual({meeting.evidence_id, calendly.evidence_id},
                         set(result["proposals"][0]["provenance"]["evidence_ids"]))

    def test_distinct_recordings_with_overlap_remain_distinct_and_warn(self):
        first = self.recording("first", "2026-07-10T13:00:00+03:00", "2026-07-10T14:00:00+03:00")
        second = self.recording("second", "2026-07-10T13:30:00+03:00", "2026-07-10T14:30:00+03:00", "Project review")
        _, result = self.make_run([first, second], self.empty_analysis([first, second]))
        self.assertEqual(2, len(result["proposals"]))
        self.assertEqual(7200, sum(row["duration_seconds"] for row in result["proposals"]))
        self.assertTrue(any(warning["type"] == "review_proposal_overlap"
                            for row in result["proposals"] for warning in row["review_warnings"]))

    def test_different_clockify_work_is_warning_not_credit(self):
        meeting = self.recording("first", "2026-07-10T13:00:00+03:00", "2026-07-10T14:00:00+03:00")
        existing = fixtures.clockify_event("2026-07-10T13:10:00+03:00", "2026-07-10T13:20:00+03:00")
        _, result = self.make_run([meeting, existing], self.empty_analysis([meeting]))
        self.assertEqual(3600, result["proposals"][0]["duration_seconds"])
        self.assertIn("existing_clockify_overlap", {w["type"] for w in result["proposals"][0]["review_warnings"]})

    def test_exact_canonical_posted_meeting_is_not_reproposed(self):
        meeting = self.recording("first", "2026-07-10T13:00:00+03:00", "2026-07-10T14:00:00+03:00")
        canonical = fixtures.reconcile_meetings([meeting.document()], [], vlad_identities={"vlad@serenichron.com"}).meetings[0]
        existing = fixtures.clockify_event("2026-07-10T13:00:00+03:00", "2026-07-10T14:00:00+03:00", canonical_meeting_id=canonical.canonical_id)
        _, result = self.make_run([meeting, existing], self.empty_analysis([meeting]))
        self.assertEqual([], result["proposals"])
        self.assertEqual("reconciled", result["fathom_reconciliation"][0]["status"])

    def test_partial_exact_recorded_meeting_credit_preserves_uncovered_seconds(self):
        meeting = self.recording("first", "2026-07-10T13:00:11+03:00", "2026-07-10T14:00:29+03:00", "Serenichron client meeting")
        canonical = fixtures.reconcile_meetings([meeting.document()], [], vlad_identities={"vlad@serenichron.com"}).meetings[0]
        route, _ = pipeline.resolve_route({}, [meeting.document()], quality.get_routing(fixtures.ROOT))
        existing = fixtures.clockify_event("2026-07-10T13:10:11+03:00", "2026-07-10T13:20:29+03:00", canonical_meeting_id=canonical.canonical_id,
                                           project_id_suffix=route["project_suffix"], tag_suffixes=route["tag_suffixes"], billable=route["billable"])
        _, result = self.make_run([meeting, existing], self.empty_analysis([meeting]))
        self.assertEqual(2, len(result["proposals"]))
        self.assertEqual(3000, sum(row["duration_seconds"] for row in result["proposals"]))
        self.assertEqual([("2026-07-10T13:00:11+03:00", "2026-07-10T13:10:11+03:00"),
                          ("2026-07-10T13:20:29+03:00", "2026-07-10T14:00:29+03:00")],
                         [(row["start"], row["end"]) for row in result["proposals"]])
        self.assertTrue(all(row["provenance"]["credited_overlap_receipt"]["credited_seconds"] == 618
                            for row in result["proposals"]))
        self.assertTrue(all(not quality.review_proposal(row, {}, quality._quality_routes(quality.get_routing(fixtures.ROOT)))["issues"]
                            for row in result["proposals"]))

    def test_partial_credit_requires_canonical_identity_and_financial_compatibility(self):
        meeting = self.recording("first", "2026-07-10T13:00:00+03:00", "2026-07-10T14:00:00+03:00", "Serenichron client meeting")
        canonical = fixtures.reconcile_meetings([meeting.document()], [], vlad_identities={"vlad@serenichron.com"}).meetings[0]
        route, _ = pipeline.resolve_route({}, [meeting.document()], quality.get_routing(fixtures.ROOT))
        valid = {"canonical_meeting_id": canonical.canonical_id, "project_id_suffix": route["project_suffix"],
                 "tag_suffixes": route["tag_suffixes"], "billable": route["billable"]}
        for mismatch in ({"canonical_meeting_id": "cm-" + "f" * 64}, {"project_id_suffix": "ffffff"},
                         {"tag_suffixes": ["ffffff"]}, {"billable": not route["billable"]}):
            with self.subTest(mismatch=mismatch):
                existing = fixtures.clockify_event("2026-07-10T13:10:00+03:00", "2026-07-10T13:20:00+03:00", **{**valid, **mismatch})
                _, result = self.make_run([meeting, existing], self.empty_analysis([meeting]))
                self.assertEqual(1, len(result["proposals"]))
                self.assertEqual(3600, result["proposals"][0]["duration_seconds"])
                self.assertNotIn("credited_overlap_receipt", result["proposals"][0]["provenance"])
        existing = fixtures.clockify_event("2026-07-10T13:10:00+03:00", "2026-07-10T13:20:00+03:00", canonical_meeting_id=canonical.canonical_id)
        _, result = self.make_run([meeting, existing], self.empty_analysis([meeting]))
        self.assertEqual(3600, result["proposals"][0]["duration_seconds"])

    def test_verified_previously_posted_fallback_is_not_duplicated(self):
        import test_verified_posted_credit as credit_fixtures
        meeting = self.recording("first", "2026-07-10T13:00:00+03:00", "2026-07-10T14:00:00+03:00", "Serenichron client meeting")
        _, result = self.make_run([meeting], self.empty_analysis([meeting]))
        row = result["proposals"][0]
        prior_bytes = json.dumps([row]).encode()
        credit = {
            "schema_version": 1, "record_type": "verified_posted_credit",
            "evidence_fingerprint": fixtures.review_corrections.evidence_fingerprint(row["provenance"]["evidence_ids"]),
            "project_suffix": row["clockify_project_suffix"],
            "current_description_sha256": credit_fixtures._description_digest(row["description"]),
            "prior_run_id": "prior-run", "sheet_publication_run_id": "prior-run",
            "prior_proposals_sha256": "sha256:" + fixtures.hashlib.sha256(prior_bytes).hexdigest(),
            "prior_proposals_base64": base64.b64encode(prior_bytes).decode(),
            "posted_rows": [{"sheet_row": credit_fixtures._sheet_row(row), "clockify_block_id": "posted-entry"}],
        }
        survivors, skipped = pipeline._apply_verified_posted_credits([row], [credit_fixtures._block(row, "posted-entry")], [credit])
        self.assertEqual([], survivors)
        self.assertEqual("verified previously posted accomplishment", skipped[0]["reason"])

    def test_unavailable_route_remains_visible_nonbillable_debt(self):
        meeting = self.recording("first", "2026-07-10T13:00:00+03:00", "2026-07-10T14:00:00+03:00")
        with tempfile.TemporaryDirectory() as temporary:
            route_path = Path(temporary) / "routing.json"
            fixtures.write_json(route_path, {"session_routes": [], "meeting_routes": [], "evidence_routes": []})
            _, result = self.make_run([meeting], self.empty_analysis([meeting]), routing_path=route_path)
        row = result["proposals"][0]
        self.assertEqual("unresolved-routing", row["routing_disposition"])
        self.assertIs(row["billable"], False)
        self.assertEqual("", row["client_project"])
        self.assertIn("unresolved_routing", {w["type"] for w in row["review_warnings"]})

    def test_source_quarantines_remain_explicit(self):
        for status, seconds, expected in (("title_only", 3600, "title_only"), ("available", 18, "short_recording_without_transcript")):
            with self.subTest(reason=expected):
                meeting = fathom_event("2026-07-10T13:00:00+03:00", "2026-07-10T14:00:00+03:00" if seconds == 3600 else "2026-07-10T13:00:18+03:00", status=status)
                _, result = self.make_run([meeting], {"activities": [], "exceptions": [], "omissions": []})
                self.assertEqual([], result["proposals"])
                self.assertEqual(expected, result["fathom_reconciliation"][0]["reason"])

    def test_quality_rejects_malformed_fallback_and_publisher_rejects_arbitrary_warning(self):
        meeting = self.recording("first", "2026-07-10T13:00:00+03:00", "2026-07-10T14:00:00+03:00")
        _, result = self.make_run([meeting], self.empty_analysis([meeting]))
        row = result["proposals"][0]
        routes = quality._quality_routes(quality.get_routing(fixtures.ROOT))
        for field, value in (("recorded_meeting_title", "Different title"), ("canonical_meeting_id", "fake"), ("analyzer_model", "fake-model"), ("semantic_fallback", False), ("evidence_ids", [])):
            malformed = copy.deepcopy(row)
            malformed["provenance"][field] = value
            self.assertTrue(quality.review_proposal(malformed, {}, routes)["issues"])
        malformed = copy.deepcopy(row)
        malformed["duration_seconds"] = 1
        self.assertTrue(quality.review_proposal(malformed, {}, routes)["issues"])
        projects = publisher.project_allowlist(quality.get_routing(fixtures.ROOT))
        native_row = publisher.proposal_row(row, "offline", project_allowlist=projects)
        self.assertEqual("pending", native_row[9])
        self.assertEqual("unposted", native_row[13])
        for warning in ({"type": "semantic_meeting_fallback", "reason": "arbitrary"}, {"type": "unknown", "reason": "unknown"}):
            malformed = copy.deepcopy(row)
            malformed["review_warnings"] = [warning]
            with self.assertRaises(publisher.PublicationError):
                publisher.proposal_row(malformed, "offline", project_allowlist=projects)

    def test_real_offline_native_replay_and_publication_dry_run(self):
        import test_review_run as replay_fixtures
        with tempfile.TemporaryDirectory() as temporary:
            task_root = Path(temporary)
            runs = task_root / "runs"
            source = runs / "source-run"
            replay_fixtures.ReviewRunResultTests._write_reconciliation_snapshots(source)
            meeting = self.recording("offline-fallback", "2026-08-01T13:00:11+03:00", "2026-08-01T13:10:29+03:00", "Serenichron recorded client review")
            session = fixtures.session_event("offline-independent-session", "2026-08-01T15:00:00+03:00", span_end="2026-08-01T15:10:00+03:00")
            ledger = fixtures.evidence_ledger.EvidenceLedger((meeting, session), {
                "clockify": {"status": "complete"}, "fathom": {"status": "complete"},
                "multica_issues": {"status": "complete"},
            })
            fixtures.write_json(source / "evidence" / "evidence-ledger.json", {
                "schema_version": ledger.manifest.schema_version, "manifest": ledger.manifest.document(),
                "events": [event.document() for event in ledger.events],
            })
            fixtures.write_json(source / "run-report.json", {"run_id": source.name,
                "runtime_identity": {"git_sha": "offline-fixture"},
                "date_range": {"since": "2026-08-01T00:00:00Z", "until": "2026-08-02T00:00:00Z"},
                "evidence_ledger": {"source_completeness": ledger.manifest.document()["source_completeness"]},
            })
            (source / "run-report.md").write_text("# Synthetic offline attendance replay\n")
            fixtures.write_json(source / "routing.json", quality.get_routing(fixtures.ROOT))
            analysis_path = task_root / "analysis.json"
            analysis = self.empty_analysis([meeting])
            analysis["activities"] = fixtures.analysis_for([session.evidence_id], recommended=10)["activities"]
            analysis["evidence_bundle_schema_version"] = "clockify-semantic-evidence-bundle/v1"
            analysis["evidence_bundle_manifest"] = replay_fixtures.bundle_manifest()
            fixtures.write_json(analysis_path, analysis)
            pipeline.run_accounting(source, root=fixtures.ROOT, routing_path=source / "routing.json", analysis_fixture=analysis_path, corrections_path=source / "review-corrections.jsonl")
            source_hashes = {str(path.relative_to(source)): fixtures.hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in source.rglob("*") if path.is_file()}
            command = [sys.executable, str(fixtures.ROOT / "scripts" / "clockify_review_run.py"),
                       "--replay-from", str(source), "--runs-root", str(runs), "--state", str(task_root / "state.json")]
            environment = {key: value for key, value in os.environ.items() if not key.startswith("CLOCKIFY_ANALYZER_")}
            environment["CLOCKIFY_AUTOPILOT_PRIVATE_TEXT_APPROVED"] = "false"
            completed = subprocess.run(command, cwd=fixtures.ROOT, env=environment, capture_output=True, text=True, timeout=30)
            (task_root / "native-replay.stdout.log").write_text(completed.stdout)
            (task_root / "native-replay.stderr.log").write_text(completed.stderr)
            replay = next(path for path in runs.iterdir() if path.name.endswith("-replay-source-run"))
            self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr + (replay / "autopilot-result.json").read_text())
            integrity = json.loads((replay / "replay-integrity.json").read_text())
            self.assertEqual("pass", integrity["status"])
            self.assertEqual([], integrity["failures"])
            self.assertEqual("pass", json.loads((replay / "quality_report.json").read_text())["status"])
            native = subprocess.run([sys.executable, str(fixtures.ROOT / "scripts" / "clockify_sheet_publish.py"),
                "--spreadsheet-id", "offline-only", "--sheet-title", "offline-only", "--run-id", source.name,
                "--proposals", str(replay / "proposals.json"), "--quality-report", str(replay / "quality_report.json"),
                "--replay-integrity", str(replay / "replay-integrity.json"), "--routing-snapshot", str(replay / "routing.json")],
                cwd=fixtures.ROOT, env=environment, capture_output=True, text=True, timeout=30)
            (task_root / "native-publisher.stdout.log").write_text(native.stdout)
            (task_root / "native-publisher.stderr.log").write_text(native.stderr)
            self.assertEqual(0, native.returncode, native.stdout + native.stderr)
            self.assertEqual({"external_writes": False, "rows": 2, "sheet_title": "offline-only", "status": "dry_run"}, json.loads(native.stdout))
            self.assertEqual(source_hashes, {str(path.relative_to(source)): fixtures.hashlib.sha256(path.read_bytes()).hexdigest()
                                             for path in source.rglob("*") if path.is_file()})

    def test_semantic_failure_retains_factual_recorded_attendance(self):
        meeting = fathom_event(
            "2026-07-10T13:07:11+03:00", "2026-07-10T14:11:29+03:00",
            status="available",
        )
        analysis = {"activities": [], "exceptions": [{
            "kind": "semantic_review_failure", "reason": "review unavailable",
            "evidence_ids": [meeting.evidence_id],
        }], "omissions": []}
        run_dir, result = self.make_run([meeting], analysis)

        self.assertEqual(1, len(result["proposals"]))
        row = result["proposals"][0]
        self.assertEqual("SC — Attended Discovery call", row["description"])
        self.assertEqual("2026-07-10T13:07:11+03:00", row["start"])
        self.assertEqual("2026-07-10T14:11:29+03:00", row["end"])
        self.assertEqual(3858, row["duration_seconds"])
        self.assertEqual("recorded_meeting", row["provenance"]["source_type"])
        self.assertTrue(row["provenance"]["semantic_fallback"])
        self.assertFalse(row["provenance"].get("analyzer_model"))
        self.assertFalse(row["provenance"].get("semantic_reviewer_model"))
        self.assertIn("semantic_meeting_fallback", {
            warning["type"] for warning in row["review_warnings"]
        })
        self.assertTrue(any(item["exception_kind"] == "semantic_review_failure"
                            for item in result["ambiguous"]))
        self.assertFalse(any(item["exception_kind"] == "missing_meeting_activity"
                             for item in result["ambiguous"]))
        import json
        self.assertEqual([], json.loads((run_dir / "semantic-analysis.json").read_text())["activities"])
        fixtures.assert_schema_valid(json.loads((fixtures.ROOT / "schemas" / "work-accounting-result-v1.json").read_text()), result)

    def test_native_quality_accepts_factual_attendance_without_a_fabricated_outcome(self):
        from scripts import clockify_sync_quality as quality
        base = fathom_event("2026-07-10T13:00:11+03:00",
                            "2026-07-10T13:00:29+03:00", status="available")
        meeting = fixtures.evidence_ledger.evidence_event(
            "fathom", dict(base.source_ref), observed_at=base.observed_at,
            raw_source_span=dict(base.raw_source_span),
            attributes={**base.attributes, "transcript": [{"text": "Meeting attended."}]},
        )
        _, result = self.make_run([meeting], {"activities": [], "exceptions": [{
            "kind": "semantic_review_failure", "reason": "review unavailable",
            "evidence_ids": [meeting.evidence_id],
        }], "omissions": []})
        row = result["proposals"][0]
        review = quality.review_proposal(row, {}, quality._quality_routes(quality.get_routing(fixtures.ROOT)))
        self.assertEqual([], review["issues"])

    def test_valid_semantic_meeting_still_uses_reviewed_outcome(self):
        meeting = fathom_event("2026-07-10T13:00:00+03:00",
                               "2026-07-10T14:00:00+03:00", status="available")
        _, result = self.make_run([meeting], meeting_analysis(meeting))
        self.assertEqual(1, len(result["proposals"]))
        self.assertIn("Confirmed", result["proposals"][0]["description"])
        self.assertFalse(result["proposals"][0]["provenance"].get("semantic_fallback"))
