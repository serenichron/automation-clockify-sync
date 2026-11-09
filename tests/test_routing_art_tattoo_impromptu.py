"""Vlad's 2026-11-06 routing decisions: Stefania Zaharia work and unrouted impromptu Google Meet calls."""
import json
from pathlib import Path
import unittest

from scripts import clockify_sync_collect as collect

ROOT = Path(__file__).resolve().parents[1]
ROUTING = json.loads((ROOT / "routing.json").read_text(encoding="utf-8"))


class ArtTattooAndImpromptuMeetRoutingTests(unittest.TestCase):
    def test_stefania_zaharia_sessions_route_to_art_tattoo_level_2(self):
        route = next(item for item in ROUTING["session_routes"] if item.get("pattern") == "stefaniazaharia")
        self.assertEqual(("Art Tattoo Level 2", "59c81b"), (route["project_name"], route["project_suffix"]))
        self.assertEqual((["Technical development"], ["35aa9b54"]), (route["tag_names"], route["tag_suffixes"]))
        self.assertEqual("SZ", route["prefix"])

    def test_external_impromptu_google_meet_defaults_to_serenichron_level_1(self):
        meeting = {"title": "Impromptu Google Meet Meeting", "calendar_invitees_domains_type": "one_or_more_external",
                   "calendar_invitees": [{"email": "guest@example.com", "is_external": True}]}
        routed = collect.route_meeting(meeting, ROUTING)
        self.assertEqual("propose", routed["action"])
        self.assertEqual(("Serenichron Level 1", ["Project Management"], "SC"),
                         (routed["project_name"], routed["tag_names"], routed["prefix"]))

    def test_named_client_meetings_still_win_over_the_impromptu_default(self):
        meeting = {"title": "TST Prep - Impromptu Google Meet", "calendar_invitees": []}
        routed = collect.route_meeting(meeting, ROUTING)
        self.assertEqual("TST Prep Level 1", routed["project_name"])


if __name__ == "__main__":
    unittest.main()
