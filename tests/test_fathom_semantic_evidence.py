from __future__ import annotations

import datetime as dt
import unittest
from unittest import mock

from scripts import clockify_sync_collect as collector


UTC = dt.timezone.utc


class FathomSemanticEvidenceTests(unittest.TestCase):
    def test_bucharest_rendering_respects_winter_and_summer_offsets(self):
        winter = collector.parse_dt("2026-01-15T10:00:00Z")
        summer = collector.parse_dt("2026-07-15T10:00:00Z")
        self.assertEqual("2026-01-15 12:00", collector.local_dt_string(winter))
        self.assertEqual("2026-07-15 13:00", collector.local_dt_string(summer))

    def test_fetch_paginates_with_semantic_fields_requested(self):
        calls = []

        def fake_http(url, headers):
            calls.append(url)
            if "cursor=next" in url:
                return {
                    "items": [
                        {
                            "recording_id": "r2",
                            "title": "Second meeting",
                            "recording_start_time": "2026-07-02T09:00:00Z",
                            "recording_end_time": "2026-07-02T09:30:00Z",
                            "default_summary": "Reviewed delivery",
                        }
                    ]
                }
            return {
                "items": [
                    {
                        "recording_id": "r1",
                        "title": "Discovery call",
                        "recording_start_time": "2026-07-01T09:00:00Z",
                        "recording_end_time": "2026-07-01T10:00:00Z",
                        "default_summary": "Defined onboarding needs",
                        "action_items": [{"text": "Prepare scope"}],
                        "transcript": [{"speaker": "Vlad", "text": "Scope"}],
                    }
                ],
                "next_cursor": "next",
            }

        with mock.patch.object(collector, "http_json", side_effect=fake_http):
            result = collector.fetch_fathom(
                {"FATHOM_API_KEY": "not-logged"},
                dt.datetime(2026, 7, 1, tzinfo=UTC),
                dt.datetime(2026, 7, 3, tzinfo=UTC),
            )

        self.assertEqual("ok", result["status"])
        self.assertTrue(result["complete"])
        self.assertEqual(2, result["pages_fetched"])
        self.assertEqual(2, len(result["meetings"]))
        self.assertEqual("available", result["meetings"][0]["semantic_evidence_status"])
        self.assertEqual("Defined onboarding needs", result["meetings"][0]["summary"])
        self.assertTrue(any("cursor=next" in call for call in calls))
        self.assertTrue(all("include_summary=true" in call for call in calls))
        self.assertTrue(all("include_action_items=true" in call for call in calls))
        self.assertTrue(all("include_transcript=true" in call for call in calls))
        self.assertTrue(all("created_after=1970-01-01" in call for call in calls))
        self.assertFalse(any("/meetings/r1" in call for call in calls))

    def test_fetch_filters_by_occurrence_not_record_creation(self):
        def fake_http(url, headers):
            return {
                "items": [
                    {
                        "recording_id": "held-in-window",
                        "created_at": "2026-01-01T09:00:00Z",
                        "recording_start_time": "2026-07-01T09:00:00Z",
                        "recording_end_time": "2026-07-01T10:00:00Z",
                        "default_summary": "Reviewed delivery",
                    },
                    {
                        "recording_id": "held-before-window",
                        "created_at": "2026-06-30T09:00:00Z",
                        "recording_start_time": "2026-06-30T09:00:00Z",
                        "recording_end_time": "2026-06-30T10:00:00Z",
                        "default_summary": "Old meeting",
                    },
                ]
            }

        with mock.patch.object(collector, "http_json", side_effect=fake_http):
            result = collector.fetch_fathom(
                {"FATHOM_API_KEY": "not-logged"},
                dt.datetime(2026, 7, 1, tzinfo=UTC),
                dt.datetime(2026, 7, 2, tzinfo=UTC),
            )

        self.assertTrue(result["complete"])
        self.assertEqual(
            ["held-in-window"],
            [meeting["recording_id"] for meeting in result["meetings"]],
        )
        self.assertEqual("recording_or_scheduled_overlap", result["occurrence_filter"]["basis"])

    def test_partial_recording_window_uses_only_complete_scheduled_pair(self):
        def fake_http(url, headers):
            return {
                "items": [
                    {
                        "recording_id": "partial-recording",
                        "created_at": "2026-07-01T08:00:00Z",
                        "recording_start_time": "2026-07-01T09:05:00Z",
                        "recording_end_time": None,
                        "scheduled_start_time": "2026-07-01T09:00:00Z",
                        "scheduled_end_time": "2026-07-01T10:00:00Z",
                    }
                ]
            }

        with mock.patch.object(collector, "http_json", side_effect=fake_http):
            result = collector.fetch_fathom(
                {"FATHOM_API_KEY": "not-logged"},
                dt.datetime(2026, 7, 1, tzinfo=UTC),
                dt.datetime(2026, 7, 2, tzinfo=UTC),
            )

        meeting = result["meetings"][0]
        self.assertEqual("scheduled", meeting["timing_basis"])
        self.assertEqual("2026-07-01 12:00", meeting["start"])
        self.assertEqual("2026-07-01 13:00", meeting["end"])

    def test_malformed_success_envelope_marks_fathom_incomplete(self):
        with mock.patch.object(collector, "http_json", return_value={"status": "ok"}):
            result = collector.fetch_fathom(
                {"FATHOM_API_KEY": "not-logged"},
                dt.datetime(2026, 7, 1, tzinfo=UTC),
                dt.datetime(2026, 7, 2, tzinfo=UTC),
            )
        self.assertEqual("error", result["status"])
        self.assertFalse(result["complete"])
        self.assertIn("did not contain a list", result["error"])

    def test_fathom_pagination_safety_limit_fails_closed(self):
        calls = 0

        def fake_http(url, headers):
            nonlocal calls
            calls += 1
            return {"items": [], "next_cursor": f"cursor-{calls}"}

        with (
            mock.patch.object(collector, "http_json", side_effect=fake_http),
            mock.patch.object(collector, "FATHOM_MAX_PAGES", 2),
        ):
            result = collector.fetch_fathom(
                {"FATHOM_API_KEY": "not-logged"},
                dt.datetime(2026, 7, 1, tzinfo=UTC),
                dt.datetime(2026, 7, 2, tzinfo=UTC),
            )
        self.assertEqual(2, calls)
        self.assertEqual("error", result["status"])
        self.assertFalse(result["complete"])
        self.assertIn("exceeded safety limit", result["error"])

    def test_title_only_meeting_becomes_exception_not_high_confidence_proposal(self):
        evidence = {
            "clockify": {"entries": []},
            "fathom": {
                "meetings": [
                    {
                        "recording_id": "r-title",
                        "title": "Discovery call",
                        "start": "2026-07-01 10:00",
                        "end": "2026-07-01 11:00",
                        "calendar_invitees": [
                            {"email": "prospect@example.com", "is_external": True}
                        ],
                        "semantic_evidence_status": "title_only",
                    }
                ]
            },
            "sessions": [],
        }
        routing = {"skip_rules": {}, "meeting_routes": []}
        proposals, ambiguous, skipped = collector.build_proposals(evidence, routing)
        self.assertEqual([], proposals)
        self.assertEqual([], skipped)
        self.assertEqual(1, len(ambiguous))
        self.assertEqual("insufficient_meeting_evidence", ambiguous[0]["exception_kind"])

    def test_title_only_response_is_complete_but_requires_semantic_review(self):
        def fake_http(url, headers):
            return {
                "items": [
                    {
                        "recording_id": "r1",
                        "title": "Call",
                        "recording_start_time": "2026-07-01T09:00:00Z",
                        "recording_end_time": "2026-07-01T09:30:00Z",
                    }
                ]
            }

        with mock.patch.object(collector, "http_json", side_effect=fake_http):
            result = collector.fetch_fathom(
                {"FATHOM_API_KEY": "not-logged"},
                dt.datetime(2026, 7, 1, tzinfo=UTC),
                dt.datetime(2026, 7, 2, tzinfo=UTC),
            )
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["complete"])
        self.assertEqual("title_only", result["meetings"][0]["semantic_evidence_status"])

    def test_list_failure_marks_source_incomplete(self):
        with mock.patch.object(collector, "http_json", side_effect=OSError("unavailable")):
            result = collector.fetch_fathom(
                {"FATHOM_API_KEY": "not-logged"},
                dt.datetime(2026, 7, 1, tzinfo=UTC),
                dt.datetime(2026, 7, 2, tzinfo=UTC),
            )
        self.assertEqual("error", result["status"])
        self.assertFalse(result["complete"])
        self.assertEqual([], result["meetings"])


if __name__ == "__main__":
    unittest.main()
