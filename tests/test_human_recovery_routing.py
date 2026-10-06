"""Regression proof for human work lost to stale recovery routing."""
from __future__ import annotations

import json
from pathlib import Path
import unittest

from scripts import review_corrections
from scripts import work_accounting_pipeline as pipeline


ROOT = Path(__file__).resolve().parents[1]


def event(identifier, cwd, content, session="human"):
    return {
        "evidence_id": identifier,
        "source_type": "codex_sessions_event",
        "source_ref": {"source_type": "codex_sessions", "machine": "desktop", "session_id": session},
        "observed_at": "2026-10-05T00:10:00+03:00",
        "raw_source_span": {"cwd": cwd},
        "attributes": {"role": "user", "kind": "message", "content": content},
    }


class HumanRecoveryRoutingTests(unittest.TestCase):
    def setUp(self):
        self.routing = json.loads((ROOT / "routing.json").read_text())

    def test_client_source_uses_mb_prefix_and_verified_technical_route(self):
        """Catches a personal skip, SC fallback, or stale MAB display prefix."""
        item = event("ev-client", "/work/Clients/mihaelabrailescu.ro/.scorecard-v2-review", "Build native email render safety harness")
        route, error = pipeline.resolve_route({}, [item], self.routing)
        self.assertIsNone(error)
        self.assertEqual("MAB Food Fairy Level 2", route["project_name"])
        self.assertEqual("7cb152", route["project_suffix"])
        self.assertEqual(["35aa9b54"], route["tag_suffixes"])
        self.assertEqual("MB", route["prefix"])

    def test_human_clockify_conversation_is_not_autonomous(self):
        """Catches cwd alone discarding real human reconciliation work."""
        item = event("ev-human", "/work/multica-command", "Audit September unresolved evidence")
        retained, noise = pipeline._analysis_events([item])
        self.assertEqual(["ev-human"], [value["evidence_id"] for value in retained])
        self.assertEqual([], noise)
        route, error = pipeline.resolve_route({}, retained, self.routing)
        self.assertIsNone(error)
        self.assertEqual("Serenichron Level 2", route["project_name"])
        self.assertEqual("SC", route["prefix"])

    def test_autonomous_thread_remains_excluded_before_routing(self):
        """Catches source promotion accidentally admitting an unattended thread."""
        summary = {
            "evidence_id": "ev-summary", "source_type": "codex_sessions",
            "source_ref": {"source_type": "codex_sessions", "machine": "desktop", "session_id": "agent"},
            "attributes": {"first_user_message": "You are running as a local coding agent for a Multica workspace. Your assigned issue ID is: issue-1"},
        }
        automated = event("ev-agent", "/work/multica-command", "Completed assigned client implementation", session="agent")
        human = event("ev-human", "/work/multica-command", "Audit September unresolved evidence")
        retained, noise = pipeline._analysis_events([summary, automated, human])
        self.assertEqual(["ev-human"], [value["evidence_id"] for value in retained])
        self.assertEqual({"ev-summary", "ev-agent"}, {value["evidence_id"] for value in noise})

    def test_exact_correction_resolves_canonical_non_sc_prefix(self):
        """Catches exact client corrections assuming every project uses SC."""
        activity = {"activity_id": "act-client", "evidence_ids": ["ev-client"]}
        case = {
            "activity_id": "act-client", "evidence_fingerprint": review_corrections.evidence_fingerprint(["ev-client"]),
            "decision": "modify", "expected_field_patch": {
                "client_project": {"op": "replace", "value": "MAB Food Fairy Level 2"},
                "tag_names": {"op": "replace", "value": ["Technical development"]},
            },
        }
        route = pipeline._route_from_review_correction(activity, [case], self.routing)
        self.assertIsNotNone(route)
        self.assertEqual("7cb152", route["project_suffix"])
        self.assertEqual("MB", route["prefix"])

    def test_exact_correction_does_not_guess_ambiguous_native_targets(self):
        """Catches duplicate display labels routing to an arbitrary native target."""
        self.routing["session_routes"].extend([
            {"pattern": "alias-a", "project_name": "Ambiguous", "project_suffix": "aaaaaa", "prefix": "A", "tag_names": ["Technical development"]},
            {"pattern": "alias-b", "project_name": "Ambiguous", "project_suffix": "bbbbbb", "prefix": "B", "tag_names": ["Technical development"]},
        ])
        activity = {"activity_id": "act-client", "evidence_ids": ["ev-client"]}
        case = {
            "activity_id": "act-client", "evidence_fingerprint": review_corrections.evidence_fingerprint(["ev-client"]),
            "decision": "modify", "expected_field_patch": {
                "client_project": {"op": "replace", "value": "Ambiguous"},
                "tag_names": {"op": "replace", "value": ["Technical development"]},
            },
        }
        self.assertIsNone(pipeline._route_from_review_correction(activity, [case], self.routing))

    def test_reviewed_sc_outcome_ignores_historical_client_transcript(self):
        """Catches incidental client lifecycle text preempting reviewed system work."""
        item = event("ev-history", "/work/multica-command", "Earlier we prepared Mazilu & Partners onboarding; now diagnose missing MAB Food Fairy routes in Clockify")
        activity = {
            "action": "Diagnosed", "object": "missing Clockify review projects", "outcome": "identified stale routing rules",
            "semantic_reviewer_model": "deepseek-v4.1-flash:cloud",
            "project_recommendation": {"name": "Serenichron Level 2", "prefix": "SC", "tag_names": ["Processes"]},
        }
        route, error = pipeline.resolve_route(activity, [item], self.routing)
        self.assertIsNone(error)
        self.assertEqual("Serenichron Level 2", route["project_name"])
        self.assertEqual(["Processes"], route["tag_names"])

    def test_unrouted_system_outcome_ignores_historical_client_transcript(self):
        """Catches a shared conversation's client mentions stealing an SC outcome."""
        item = event("ev-history", "/work/multica-command", "Earlier we prepared Mazilu & Partners onboarding and MAB Food Fairy implementation")
        activity = {"action": "Audited", "object": "September unresolved evidence tab", "outcome": "identified review gaps"}
        route, error = pipeline.resolve_route(activity, [item], self.routing)
        self.assertIsNone(error)
        self.assertEqual("Serenichron Level 2", route["project_name"])
        self.assertEqual("SC", route["prefix"])

    def test_high_confidence_system_source_does_not_turn_overlap_audit_into_meeting(self):
        """Catches a BNI overlap diagnostic being billed as the BNI meeting itself."""
        item = event("ev-overlap", "/work/multica-command", "Diagnose BNI meeting overlap with SMTP troubleshooting")
        activity = {"action": "Diagnosed", "object": "BNI meeting overlap with SMTP troubleshooting", "outcome": "recovered missing review time"}
        route, error = pipeline.resolve_route(activity, [item], self.routing)
        self.assertIsNone(error)
        self.assertEqual("Serenichron Level 2", route["project_name"])
        self.assertEqual(["System development"], route["tag_names"])


if __name__ == "__main__":
    unittest.main()
