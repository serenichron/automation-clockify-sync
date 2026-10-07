from __future__ import annotations

import hashlib
import importlib.util
import json
import unittest
from pathlib import Path

from scripts.clockify_sheet_publish import project_allowlist
from scripts import work_accounting_pipeline as pipeline


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
    def test_mab_session_aliases_are_billable_client_work(self):
        """Catches the legacy personal skip and SC fallback for MAB implementation."""
        routing = collector.load_json(MODULE_PATH.parents[1] / "routing.json")
        for label, tags, suffixes in (
            ("mihaelabrailescu", ["Technical development"], ["35aa9b54"]),
            ("MBrailescu/site", ["Technical development"], ["35aa9b54"]),
            ("Mihaela Brăilescu", ["Technical development"], ["35aa9b54"]),
            ("Reset Feminin", ["System development"], ["35aa9afb"]),
            ("ResetFeminin", ["System development"], ["35aa9afb"]),
            ("MAB Food Fairy", ["Technical development"], ["35aa9b54"]),
            ("rf-access-ops", ["System development"], ["35aa9afb"]),
        ):
            with self.subTest(label=label):
                route = collector.route_session({"label": label}, routing)
                self.assertEqual("propose", route["action"])
                self.assertEqual("MAB Food Fairy Level 2", route["project_name"])
                self.assertEqual("7cb152", route["project_suffix"])
                self.assertEqual(tags, route["tag_names"])
                self.assertEqual(suffixes, route["tag_suffixes"])
                self.assertTrue(route["billable"])
                event = {
                    "source_type": "codex_sessions", "observed_at": "2026-09-25T10:00:00+03:00",
                    "raw_source_span": {"cwd": "/work/" + label},
                    "attributes": {"label": label, "content": "Implement the approved client changes."},
                }
                resolved, error = pipeline.resolve_route({}, [event], routing)
                self.assertIsNone(error)
                self.assertEqual("7cb152", resolved["project_suffix"])
                self.assertEqual(tags, resolved["tag_names"])

    def test_mab_evidence_routes_require_client_identity(self):
        """Catches absent authored-evidence routing and cross-client keyword capture."""
        routing = collector.load_json(MODULE_PATH.parents[1] / "routing.json")
        for content, project, suffix, tags in (
            ("Implement Mihaela Brăilescu website fixes.", "MAB Food Fairy Level 2", "7cb152", ["Technical development"]),
            ("Implement rf-access-ops workflow for Reset Feminin.", "MAB Food Fairy Level 2", "7cb152", ["System development"]),
            ("MAB Food Fairy project management and delivery planning.", "MAB Food Fairy PM", "d07be7", ["Project Management"]),
            ("Fix TST Prep scorecard and access workflow.", "Serenichron Level 2", "775f9f", ["System development"]),
        ):
            with self.subTest(content=content):
                event = {
                    "source_type": "codex_sessions", "observed_at": "2026-09-25T10:00:00+03:00",
                    "raw_source_span": {"cwd": "/work/general"},
                    "attributes": {"content": content},
                }
                route, error = pipeline.resolve_route({}, [event], routing)
                self.assertIsNone(error)
                self.assertEqual(project, route["project_name"])
                self.assertEqual(suffix, route["project_suffix"])
                self.assertEqual(tags, route["tag_names"])

    def test_mab_client_meetings_route_to_pm_before_internal_fallback(self):
        """Catches a client sync silently becoming a Serenichron internal meeting."""
        routing = collector.load_json(MODULE_PATH.parents[1] / "routing.json")
        for title in (
            "MAB Food Fairy — Sync", "Mihaela Brăilescu — Planning", "Reset Feminin — Review",
            "MB - Vlad & Alex - Flow Consiliere",
            "MB - Client Call - Vlad & Mihaela - Consiliere & Scorecard Hormonal",
            "MB — Delivery planning",
        ):
            with self.subTest(title=title):
                item = meeting(title)
                route = collector.route_meeting(item, routing)
                self.assertEqual("MAB Food Fairy PM", route["project_name"])
                self.assertEqual("d07be7", route["project_suffix"])
                self.assertEqual(["Project Management"], route["tag_names"])
                self.assertEqual(["35aa9aef"], route["tag_suffixes"])
                self.assertTrue(route["billable"])
                event = {"source_type": "fathom", "attributes": item}
                resolved, error = pipeline.resolve_route({}, [event], routing)
                self.assertIsNone(error)
                self.assertEqual("d07be7", resolved["project_suffix"])

    def test_mb_meeting_alias_requires_a_delimited_client_prefix(self):
        """Catches a short alias stealing unrelated internal meetings."""
        routing = collector.load_json(MODULE_PATH.parents[1] / "routing.json")
        for title in ("Discuss MB storage", "MB storage planning", "Mihaela — General sync"):
            with self.subTest(title=title):
                route = collector.route_meeting(meeting(title), routing)
                self.assertEqual("Serenichron Level 1", route["project_name"])

    def test_mab_incidental_content_does_not_take_over_other_client_route(self):
        """Catches evidence rules overriding an unrelated deterministic client route."""
        routing = collector.load_json(MODULE_PATH.parents[1] / "routing.json")
        event = {
            "source_type": "codex_sessions", "observed_at": "2026-09-25T10:00:00+03:00",
            "raw_source_span": {"cwd": "/work/tstprep-com-site-codebase"},
            "attributes": {"content": "Fix scorecard access; compare Reset Feminin workflow."},
        }
        route, error = pipeline.resolve_route({}, [event], routing)
        self.assertIsNone(error)
        self.assertEqual("TST Prep Level 2", route["project_name"])
        self.assertEqual("bc17f7", route["project_suffix"])

    def test_mab_label_does_not_override_unattended_session_exclusion(self):
        """Catches a billable client alias bypassing the unattended-agent skip guard."""
        routing = collector.load_json(MODULE_PATH.parents[1] / "routing.json")
        route = collector.route_session(
            {"label": "MAB Food Fairy", "path": "/work/multica/session.jsonl"}, routing
        )
        self.assertEqual("skip", route["action"])

    def test_release_routing_pins_clockify_identity_and_has_stable_digest(self):
        """Catches a release routing artifact that cannot pass the cycle identity gate."""
        path = MODULE_PATH.parents[1] / "routing.json"
        raw = path.read_bytes()
        routing = json.loads(raw)

        self.assertEqual("5f5b532dcec6824135aa9a85", routing["workspace_id"])
        self.assertEqual("5f5b5121a551633f6dfa31e6", routing["member_id"])
        self.assertEqual(routing["clockify_user_id"], routing["member_id"])
        self.assertEqual(
            "e28ab252f630dec9350bc51f9d7037b77a75299638510608ab5dda5606ee0abe",
            hashlib.sha256(raw).hexdigest(),
        )
        self.assertEqual(
            "Serenichron Level 2", project_allowlist(routing)["775f9f"]
        )

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
