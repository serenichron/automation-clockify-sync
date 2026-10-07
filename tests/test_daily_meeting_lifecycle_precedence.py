"""Canonical Daily recordings retain their agreed route despite client mentions."""
import copy
import json
from pathlib import Path
import unittest

from scripts import evidence_ledger, work_accounting_pipeline as pipeline
import test_work_accounting_pipeline as fixtures


ROOT = Path(__file__).resolve().parents[1]


class DailyMeetingLifecyclePrecedenceTests(unittest.TestCase):
    make_run = fixtures.WorkAccountingPipelineTests.make_run
    append_correction = fixtures.WorkAccountingPipelineTests.append_correction

    def setUp(self):
        self.routing = json.loads((ROOT / "routing.json").read_text())
        self.recording = evidence_ledger.evidence_event(
            "fathom", {"source_type": "fathom", "source_id": "daily-recording"},
            observed_at="2026-10-06T10:35:27Z",
            raw_source_span={"start": "2026-10-06T10:35:27Z",
                             "end": "2026-10-06T12:00:37Z"},
            attributes={
                "title": "Daily Meet", "semantic_evidence_status": "available",
                "summary": "Daily priorities include Mazilu & Partners and other clients.",
                "recorded_by_email": "vlad@serenichron.com",
                "calendar_invitees": [{"email": "colleague@example.test", "is_external": True}],
            },
        )
        self.analysis = fixtures.meeting_analysis(self.recording)
        self.activity = self.analysis["activities"][0]
        self.activity.update({
            "action": "Attended", "object": "Daily Meet",
            "outcome": "aligned priorities for Mazilu & Partners and other clients",
            "project_recommendation": {"name": "Daily meetings", "prefix": "SC",
                                       "tag_names": ["Project Management"]},
            "semantic_reviewer_model": "deepseek-v4.1-flash:cloud",
            "semantic_reviewer_revision": "a" * 64,
            "review_prompt_version": "clockify-semantic-review-v7",
        })

    def test_agreed_reviewed_daily_route_survives_incidental_client_cutover(self):
        """Catches the broad lifecycle override winning before agreed Daily routing."""
        route, error = pipeline.resolve_route(
            self.activity, [self.recording.document()], self.routing,
        )
        self.assertIsNone(error)
        self.assertEqual("Daily meetings", route["project_name"])
        self.assertEqual("SC", route["prefix"])
        self.assertEqual(["Project Management"], route["tag_names"])

    def test_canonical_recording_proposal_preserves_daily_route_and_exact_interval(self):
        """Catches the real canonical accounting path misbilling a Daily recording."""
        _, result = self.make_run([self.recording], self.analysis)
        self.assertEqual(1, len(result["proposals"]))
        proposal = result["proposals"][0]
        self.assertEqual("Daily meetings", proposal["client_project"])
        self.assertEqual("89dab8", proposal["clockify_project_suffix"])
        self.assertEqual("SC — Attended Daily Meet aligned priorities for Mazilu & Partners and other clients",
                         proposal["description"])
        self.assertEqual(5110, proposal["duration_seconds"])
        self.assertEqual("2026-10-06T10:35:27+00:00", proposal["start"])
        self.assertEqual("2026-10-06T12:00:37+00:00", proposal["end"])
        self.assertTrue(proposal["provenance"]["canonical_meeting_id"].startswith("cm-"))

    def test_explicit_human_routing_correction_still_wins(self):
        """Catches automatic Daily protection overriding an exact reviewed selection."""
        run, initial = self.make_run([self.recording], self.analysis)
        correction = run.parent.parent / "review-corrections.jsonl"
        self.append_correction(correction, initial["proposals"][0], "modify",
            categories=["routing"], field_patch={
                "client_project": {"op": "replace", "value": "Mazilu & Partners — Retainer"},
                "tag_names": {"op": "replace", "value": ["Mazilu — Sesiuni de lucru"]},
            })
        result = pipeline.run_accounting(run, root=ROOT,
            analysis_fixture=run.parent.parent / "analysis.json", corrections_path=correction)
        self.assertEqual("Mazilu & Partners — Retainer", result["proposals"][0]["client_project"])
        self.assertEqual(5110, result["proposals"][0]["duration_seconds"])
        self.assertEqual(0, result["correction_regression"]["summary"]["fail"])

    def test_dedicated_mazilu_recording_keeps_lifecycle_route(self):
        """Catches suppressing genuine post-cutover Mazilu meeting routing."""
        event = self.recording.document()
        event["attributes"]["title"] = "Mazilu & Partners working session"
        activity = copy.deepcopy(self.activity)
        activity["object"] = "Mazilu & Partners working session"
        activity["project_recommendation"] = {"name": "Serenichron Level 1", "prefix": "SC",
                                               "tag_names": ["Project Management"]}
        route, error = pipeline.resolve_route(activity, [event], self.routing)
        self.assertIsNone(error)
        self.assertEqual("Mazilu & Partners — Retainer", route["project_name"])

    def test_protection_requires_reviewed_daily_selection_to_agree(self):
        """Catches broadening the exception to unreviewed or differently routed work."""
        for change in ("unreviewed", "project", "prefix", "tags", "lifecycle"):
            with self.subTest(change=change):
                activity = copy.deepcopy(self.activity)
                if change == "unreviewed": activity.pop("semantic_reviewer_model")
                elif change == "project": activity["project_recommendation"]["name"] = "Serenichron Level 1"
                elif change == "prefix": activity["project_recommendation"]["prefix"] = "M&P"
                elif change == "tags": activity["project_recommendation"]["tag_names"] = ["Other task"]
                else: activity["lifecycle"] = "completed"
                route, error = pipeline.resolve_route(activity, [self.recording.document()], self.routing)
                self.assertIsNone(error)
                self.assertEqual("Mazilu & Partners — Retainer", route["project_name"])

    def test_nonrecording_daily_text_keeps_existing_lifecycle_behavior(self):
        """Catches letting a session title or recommendation masquerade as a recording."""
        event = fixtures.session_event("session:daily", "2026-10-06T10:35:27Z",
                                       "Reviewed Daily Meet Mazilu & Partners priorities").document()
        route, error = pipeline.resolve_route(self.activity, [event], self.routing)
        self.assertIsNone(error)
        self.assertEqual("Mazilu & Partners — Retainer", route["project_name"])

    def test_disagreeing_deterministic_route_keeps_lifecycle_precedence(self):
        """Catches protecting Daily merely because the reviewer selected it."""
        routing = copy.deepcopy(self.routing)
        route = copy.deepcopy(routing["client_lifecycle_routes"][0]["activation"]["route"])
        routing["meeting_routes"].insert(0, {**route, "title_regex": r"Daily Meet"})
        selected, error = pipeline.resolve_route(self.activity, [self.recording.document()], routing)
        self.assertIsNone(error)
        self.assertEqual("Mazilu & Partners — Retainer", selected["project_name"])

    def test_mixed_recording_and_session_scope_does_not_gain_daily_exception(self):
        """Catches extending recording-only protection to mixed-source work."""
        session = fixtures.session_event("session:mixed", "2026-10-06T10:35:27Z",
                                         "Reviewed Mazilu & Partners priorities").document()
        route, error = pipeline.resolve_route(
            self.activity, [self.recording.document(), session], self.routing,
        )
        self.assertIsNone(error)
        self.assertEqual("Mazilu & Partners — Retainer", route["project_name"])


if __name__ == "__main__":
    unittest.main()
