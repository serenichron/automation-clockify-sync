from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "clockify_sync_collect.py"
SPEC = importlib.util.spec_from_file_location("clockify_fathom_collector", MODULE_PATH)
assert SPEC and SPEC.loader
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


ROUTING = {
    "meeting_routes": [
        {
            "title_regex": r"\bdaily\s*(stand-?up|meet)\b",
            "prefix": "SC",
            "project_name": "Daily meetings",
            "project_suffix": "123456",
            "tag_suffixes": ["abcdef"],
            "tag_names": ["Project Management"],
            "billable": True,
        }
    ],
    "session_routes": [],
    "skip_rules": {"min_minutes": 10, "min_user_messages": 5},
}


def meeting(title: str = "Daily Meet", recording_id: int = 42):
    return {
        "recording_id": recording_id,
        "title": title,
        "start": "2026-07-29 13:00",
        "end": "2026-07-29 13:30",
        "share_url": f"https://fathom.video/share/{recording_id}",
        "calendar_invitees": [
            {"email": "vlad@serenichron.com", "is_external": False},
            {"email": "george@serenichron.com", "is_external": False},
        ],
    }


class FathomRoutingTests(unittest.TestCase):
    def test_lens_title_routes_without_client_domain_invitee(self):
        routing = collector.load_json(MODULE_PATH.parents[1] / "routing.json")
        lens_meeting = meeting("Serenichron × Lens of Alex — Sync")

        route = collector.route_meeting(lens_meeting, routing)

        self.assertEqual("propose", route["action"])
        self.assertEqual("Lens of Alex Retainer", route["project_name"])
        self.assertEqual("LoA", route["prefix"])

    def test_lens_session_aliases_route_to_retainer(self):
        routing = collector.load_json(MODULE_PATH.parents[1] / "routing.json")

        for label in ("lens-of-alex", "lensofalex.com", "lensofalex-com"):
            with self.subTest(label=label):
                route = collector.route_session({"label": label}, routing)
                self.assertEqual("propose", route["action"])
                self.assertEqual("Lens of Alex Retainer", route["project_name"])
                self.assertEqual("LoA", route["prefix"])

    def test_internal_only_meeting_uses_serenichron_fallback(self):
        routing = collector.load_json(MODULE_PATH.parents[1] / "routing.json")
        internal = meeting("Vlad & George - Internal Call")
        internal["calendar_invitees_domains_type"] = None

        route = collector.route_meeting(internal, routing)

        self.assertEqual("propose", route["action"])
        self.assertEqual("Serenichron Level 1", route["project_name"])
        self.assertEqual(["Project Management"], route["tag_names"])

    def test_matched_meeting_becomes_stable_proposal(self):
        proposals, ambiguous, skipped = collector.build_proposals(
            {
                "clockify": {"entries": []},
                "fathom": {"meetings": [meeting()]},
                "sessions": [],
            },
            ROUTING,
        )

        self.assertEqual([], ambiguous)
        self.assertEqual([], skipped)
        self.assertEqual(1, len(proposals))
        self.assertEqual("SC — Daily Meet", proposals[0]["description"])
        self.assertEqual("fathom", proposals[0]["provenance"]["source_type"])
        self.assertEqual("42", proposals[0]["provenance"]["source_session_id"])
        self.assertTrue(proposals[0]["candidate_key"].startswith("ck-"))

    def test_unmapped_meeting_is_ambiguous_not_dropped(self):
        proposals, ambiguous, skipped = collector.build_proposals(
            {
                "clockify": {"entries": []},
                "fathom": {"meetings": [meeting("Discovery Call - New Lead")]},
                "sessions": [],
            },
            ROUTING,
        )

        self.assertEqual([], proposals)
        self.assertEqual([], skipped)
        self.assertEqual(1, len(ambiguous))
        self.assertIn("candidate_key", ambiguous[0])
        self.assertEqual("fathom", ambiguous[0]["provenance"]["source_type"])

    def test_solo_impromptu_recording_is_explicitly_skipped(self):
        solo = meeting("Impromptu Google Meet")
        solo["calendar_invitees"] = [
            {"email": "vlad@serenichron.com", "is_external": False}
        ]

        proposals, ambiguous, skipped = collector.build_proposals(
            {
                "clockify": {"entries": []},
                "fathom": {"meetings": [solo]},
                "sessions": [],
            },
            ROUTING,
        )

        self.assertEqual([], proposals)
        self.assertEqual([], ambiguous)
        self.assertEqual(1, len(skipped))
        self.assertIn("recorder misfire", skipped[0]["reason"])


if __name__ == "__main__":
    unittest.main()
