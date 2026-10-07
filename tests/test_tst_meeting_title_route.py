from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "clockify_sync_collect.py"
SPEC = importlib.util.spec_from_file_location("clockify_tst_route_collector", MODULE_PATH)
assert SPEC and SPEC.loader
collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(collector)


def load_routing():
    return collector.load_json(MODULE_PATH.parents[1] / "routing.json")


class TstMeetingTitleRouteTests(unittest.TestCase):
    def test_explicit_tst_weekly_title_routes_without_external_invitees(self):
        routing = load_routing()
        for meeting in (
            {
                "title": "TST - Weekly check-in with Josh",
                "calendar_invitees": [
                    {"email": "teammate@serenichron.com", "is_external": False}
                ],
            },
            {"title": "TST - Weekly check-in with Josh"},
        ):
            with self.subTest(invitees=meeting.get("calendar_invitees", "absent")):
                route = collector.route_meeting(meeting, routing)

                self.assertEqual("propose", route["action"])
                self.assertEqual("TST Prep Level 1", route["project_name"])
                self.assertEqual("TSTP", route["prefix"])

    def test_daily_meet_still_uses_daily_meetings_route(self):
        route = collector.route_meeting({"title": "Daily Meet"}, load_routing())

        self.assertEqual("propose", route["action"])
        self.assertEqual("Daily meetings", route["project_name"])

    def test_embedded_tst_substring_does_not_match_tst_route(self):
        route = collector.route_meeting(
            {"title": "Contest TST platform planning"}, load_routing()
        )

        self.assertNotEqual("TST Prep Level 1", route.get("project_name"))


if __name__ == "__main__":
    unittest.main()
