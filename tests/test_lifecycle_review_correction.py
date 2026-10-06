"""Exact editorial selections include lifecycle routes without changing autorouting."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts import clockify_review_run as run, evidence_ledger, review_corrections
from scripts import work_accounting_pipeline as pipeline


ROUTE = {
    "project_name": "Mazilu & Partners — Retainer",
    "project_suffix": "54ecf6",
    "tag_names": ["Mazilu — Sesiuni de lucru"],
    "tag_suffixes": ["f2634e9f"],
    "prefix": "M&P",
    "billable": True,
    "confidence": "high",
}


class LifecycleReviewCorrectionTests(unittest.TestCase):
    def routing(self):
        return {
            "session_routes": [], "meeting_routes": [], "evidence_routes": [],
            "client_lifecycle_routes": [{
                "pattern": r"\bMazilu\b",
                "activation": {
                    "effective_at": "2026-09-24T00:00:00+03:00",
                    "route": copy.deepcopy(ROUTE),
                },
            }],
        }

    def activity(self):
        return {"activity_id": "act-exact-portfolio", "evidence_ids": ["ev-exact-source"]}

    def case(self, activity=None):
        activity = activity or self.activity()
        return {
            "activity_id": activity["activity_id"],
            "evidence_fingerprint": review_corrections.evidence_fingerprint(activity["evidence_ids"]),
            "decision": "modify",
            "expected_field_patch": {
                "client_project": {"op": "replace", "value": "Mazilu & Partners — Retainer"},
                "tag_names": {"op": "replace", "value": ["Mazilu — Sesiuni de lucru"]},
            },
        }

    def test_exact_selection_resolves_lifecycle_only_canonical_native_route(self):
        """Catches omitting configured activation.route from correction lookup."""
        routing = self.routing()
        before = copy.deepcopy(routing)
        route = pipeline._route_from_review_correction(self.activity(), [self.case()], routing)
        self.assertEqual(ROUTE, route)
        self.assertEqual(before, routing)

    def test_duplicate_lifecycle_selection_with_same_native_identity_is_not_ambiguous(self):
        """Catches treating duplicate declarations as different native selections."""
        routing = self.routing()
        routing["client_lifecycle_routes"].append(copy.deepcopy(routing["client_lifecycle_routes"][0]))
        self.assertEqual(ROUTE, pipeline._route_from_review_correction(
            self.activity(), [self.case()], routing,
        ))

    def test_conflicting_ordinary_and_lifecycle_native_targets_are_rejected(self):
        """Catches choosing the ordinary declaration over a conflicting lifecycle target."""
        for field, value in (("project_suffix", "other-project"),
                             ("tag_suffixes", ["other-task"]),
                             ("prefix", "ALT"), ("billable", False)):
            with self.subTest(field=field):
                routing = self.routing()
                routing["session_routes"] = [{**ROUTE, field: value}]
                self.assertIsNone(pipeline._route_from_review_correction(
                    self.activity(), [self.case()], routing,
                ))

    def test_conflicting_lifecycle_native_targets_are_rejected(self):
        routing = self.routing()
        other = copy.deepcopy(routing["client_lifecycle_routes"][0])
        other["activation"]["route"]["project_suffix"] = "other-project"
        routing["client_lifecycle_routes"].append(other)
        self.assertIsNone(pipeline._route_from_review_correction(
            self.activity(), [self.case()], routing,
        ))

    def test_same_named_ordinary_declarations_cannot_hide_conflicting_native_identity(self):
        """Catches selection-map deduplication hiding a conflicting configured ID."""
        for field, value in (("project_suffix", "other-project"),
                             ("tag_suffixes", ["other-task"]), ("billable", False)):
            with self.subTest(field=field):
                routing = self.routing()
                routing["session_routes"] = [copy.deepcopy(ROUTE)]
                routing["meeting_routes"] = [{**ROUTE, field: value}]
                self.assertIsNone(pipeline._route_from_review_correction(
                    self.activity(), [self.case()], routing,
                ))

    def test_identical_native_identity_across_ordinary_and_lifecycle_declarations_is_unique(self):
        routing = self.routing()
        for section in ("session_routes", "meeting_routes", "evidence_routes"):
            routing[section] = [copy.deepcopy(ROUTE)]
        self.assertEqual(ROUTE, pipeline._route_from_review_correction(
            self.activity(), [self.case()], routing,
        ))

    def test_unknown_or_stale_selection_never_selects_lifecycle_route(self):
        for change in ("activity", "evidence", "project", "tags", "decision"):
            with self.subTest(change=change):
                case = self.case()
                if change == "activity": case["activity_id"] = "act-other"
                elif change == "evidence":
                    case["evidence_fingerprint"] = review_corrections.evidence_fingerprint(["ev-other"])
                elif change == "project": case["expected_field_patch"]["client_project"]["value"] = "Unknown"
                elif change == "tags": case["expected_field_patch"]["tag_names"]["value"] = ["Other task"]
                else: case["decision"] = "approve"
                self.assertIsNone(pipeline._route_from_review_correction(
                    self.activity(), [case], self.routing(),
                ))

    def test_malformed_lifecycle_entries_are_ignored_without_hiding_ordinary_route(self):
        routing = self.routing()
        routing["session_routes"] = [copy.deepcopy(ROUTE)]
        routing["client_lifecycle_routes"] = [None, {}, {"activation": None},
            {"activation": {"route": None}}, {"activation": {"route": {}}}]
        self.assertEqual(ROUTE, pipeline._route_from_review_correction(
            self.activity(), [self.case()], routing,
        ))

    def test_native_editorial_transition_accepts_source_bound_lifecycle_route_without_financial_edits(self):
        """Catches repair validation rejecting an exact configured lifecycle selection."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / "runs"
            source = runs / "source-run"
            (source / "evidence").mkdir(parents=True)
            event = evidence_ledger.evidence_event(
                "repository_events", {"source_type": "repository_events", "source_id": "source-exact"},
                observed_at="2026-10-02T09:00:00Z", attributes={"description": "Reviewed working-session agenda"},
            )
            ledger = evidence_ledger.EvidenceLedger((event,))
            activity = {"activity_id": "act-exact-portfolio", "evidence_ids": [event.evidence_id]}
            proposal = {
                **activity, "candidate_key": "candidate-exact", "description": "SC — Reviewed agenda",
                "start": "2026-10-02T09:00:00Z", "end": "2026-10-02T09:15:23Z",
                "duration_minutes": 16, "duration_seconds": 923, "billable": True,
            }
            documents = {
                "evidence/evidence-ledger.json": {
                    "schema_version": ledger.manifest.schema_version,
                    "manifest": ledger.manifest.document(), "events": [event.document()],
                },
                "proposals.json": [proposal],
                "semantic-analysis.json": {"activities": [activity]},
            }
            for name, value in documents.items():
                (source / name).write_text(json.dumps(value) + "\n")
            (source / "review-corrections.jsonl").write_bytes(b"")
            routing = root / "selected-routing.json"
            routing.write_text(json.dumps(self.routing()) + "\n")
            proposed = root / "editorial.jsonl"
            item = {"id": "rvi-exact", "current": proposal}
            decision = review_corrections.build_decision(
                item, decision="modify", reviewer="reviewer", reviewed_at="2026-10-06T12:00:00Z",
                correction_categories=["routing"], rationale="Verified source-bound client selection.",
                field_patch=self.case(activity)["expected_field_patch"],
            )
            review_corrections.append_decision(proposed, decision, item=item)
            originals = {p: p.read_bytes() for p in source.rglob("*") if p.is_file()}
            expected = ("sha256:" + hashlib.sha256(b"").hexdigest(),
                        "sha256:" + hashlib.sha256(proposed.read_bytes()).hexdigest())
            self.assertEqual(expected, run._validate_repair_credit_transition(
                source, proposed, runs_root=runs, routing_snapshot=routing,
            ))
            self.assertEqual(originals, {p: p.read_bytes() for p in originals})
            self.assertEqual(923, json.loads((source / "proposals.json").read_text())[0]["duration_seconds"])
            self.assertEqual({"client_project", "tag_names"}, set(decision["field_patch"]))


if __name__ == "__main__":
    unittest.main()
