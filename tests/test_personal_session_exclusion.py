"""Declared personal sessions never reach the analyzer or the review Sheet."""
import json
from pathlib import Path
import unittest

from scripts import work_accounting_pipeline as pipeline

ROOT = Path(__file__).resolve().parents[1]
SCRATCH = ("/Users/blackthorne/Library/Application Support/Claude/scratch-workspaces/"
           "1394710b-85ec-4d22-a6f9-906355b8976b/8db63d3c-54d5-4fbd-94d1-46f17118a1f5/scratch-2026-10-19-e146a7")
RULES = {"personal_session_rules": [
    {"id": "tramvai-41", "reason": "Personal hobby project", "machine": "macbook", "cwd_prefix": SCRATCH},
    {"id": "atelieruldevis", "reason": "Personal/non-billable", "route_pattern": "atelieruldevis"},
    {"id": "known-session", "reason": "Personal", "machine": "omarchy-desktop", "session_ids": ["sess-personal"]},
]}


def event(evidence_id, session_id, cwd, machine="macbook", content="Built the next part"):
    return {
        "evidence_id": evidence_id,
        "source_type": "claude_bursts_event",
        "source_ref": {"source_type": "claude_bursts", "machine": machine, "session_id": session_id},
        "raw_source_span": {"cwd": cwd, "timestamp": "2026-10-19T20:00:00+03:00"},
        "attributes": {"role": "user", "kind": "message", "content": content},
    }


class PersonalSessionRulesTests(unittest.TestCase):
    def rules(self, routing=RULES):
        return pipeline.personal_session_rules(routing)

    def split(self, events, routing=RULES):
        retained, noise = pipeline._analysis_events(events, personal_rules=self.rules(routing))
        reasons = {item["evidence_id"]: item["reason"] for item in noise}
        return [item["evidence_id"] for item in retained], reasons

    def test_session_inside_declared_folder_is_excluded_entirely(self):
        retained, reasons = self.split([
            event("ev-1", "tram", SCRATCH + "/tramvai-41-imperio"),
            event("ev-2", "tram", SCRATCH),
            event("ev-work", "work", "/Users/blackthorne/Work/Clients/tstprep.com"),
        ])
        self.assertEqual(["ev-work"], retained)
        self.assertEqual("personal_session:tramvai-41", reasons["ev-1"])
        self.assertEqual("personal_session:tramvai-41", reasons["ev-2"])

    def test_session_only_partly_inside_the_folder_is_kept(self):
        retained, reasons = self.split([
            event("ev-1", "mixed", SCRATCH + "/tramvai-41-imperio"),
            event("ev-2", "mixed", "/Users/blackthorne/Work/serenichron"),
        ])
        self.assertEqual(["ev-1", "ev-2"], retained)
        self.assertFalse(any(r.startswith("personal_session") for r in reasons.values()))

    def test_prefix_is_anchored_on_path_segments(self):
        retained, _ = self.split([event("ev-1", "other", SCRATCH + "-other/project")])
        self.assertEqual(["ev-1"], retained)

    def test_machine_must_match_when_declared(self):
        retained, _ = self.split([event("ev-1", "tram", SCRATCH, machine="omarchy-precision")])
        self.assertEqual(["ev-1"], retained)

    def test_route_pattern_matches_regardless_of_session_route_order(self):
        retained, reasons = self.split([
            event("ev-1", "site", "/Users/blackthorne/Work/atelieruldevis/site"),
        ])
        self.assertEqual([], retained)
        self.assertEqual("personal_session:atelieruldevis", reasons["ev-1"])

    def test_declared_session_id_excludes_that_session_only(self):
        retained, reasons = self.split([
            event("ev-1", "sess-personal", "/home/blackthorne/Work", machine="omarchy-desktop"),
            event("ev-2", "sess-work", "/home/blackthorne/Work", machine="omarchy-desktop"),
        ])
        self.assertEqual(["ev-2"], retained)
        self.assertEqual("personal_session:known-session", reasons["ev-1"])

    def test_absent_rules_change_nothing(self):
        events = [event("ev-1", "tram", SCRATCH), event("ev-2", "work", "/Users/blackthorne/Work/x")]
        self.assertEqual((), pipeline.personal_session_rules({"session_routes": []}))
        self.assertEqual(pipeline._analysis_events(events), pipeline._analysis_events(events, personal_rules=()))

    def test_invalid_rules_fail_closed(self):
        bad = [
            [{"id": "x", "reason": "r"}],
            [{"id": "x", "reason": "r", "cwd_prefix": "/a", "route_pattern": "b"}],
            [{"id": "x", "reason": "r", "cwd_prefix": "/a", "colour": "blue"}],
            [{"id": "x", "reason": "r", "cwd_prefix": ""}],
            [{"id": "x", "reason": "r", "cwd_prefix": "/a"}, {"id": "x", "reason": "r", "cwd_prefix": "/b"}],
            [{"id": "x", "reason": "r", "session_ids": []}],
        ]
        for rules in bad:
            with self.subTest(rules=rules):
                with self.assertRaises(pipeline.WorkAccountingError):
                    pipeline.personal_session_rules({"personal_session_rules": rules})

    def test_repository_routing_declares_the_confident_personal_rules(self):
        routing = json.loads((ROOT / "routing.json").read_text(encoding="utf-8"))
        rules = {rule["id"]: rule for rule in pipeline.personal_session_rules(routing)}
        self.assertEqual(SCRATCH, rules["tramvai-41"]["cwd_prefix"])
        self.assertEqual("macbook", rules["tramvai-41"]["machine"])
        for pattern in ("atelieruldevis", "family-bank", "hartacoruptiei"):
            self.assertIn(pattern, {rule.get("route_pattern") for rule in rules.values()})
        self.assertNotIn("-private-tmp", {rule.get("route_pattern") for rule in rules.values()})


if __name__ == "__main__":
    unittest.main()
