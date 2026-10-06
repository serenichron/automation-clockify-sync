"""Only source-bound human wording edits may enter an offline repair snapshot."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts import clockify_review_run as run, evidence_ledger, review_corrections


class WordingRepairTransitionTests(unittest.TestCase):
    def fixture(self, root: Path) -> tuple[Path, Path, dict]:
        runs = root / "runs"
        source = runs / "source-run"
        evidence = source / "evidence"
        evidence.mkdir(parents=True)
        ledger = evidence_ledger.EvidenceLedger()
        (evidence / "evidence-ledger.json").write_text(json.dumps({
            "schema_version": ledger.manifest.schema_version,
            "manifest": ledger.manifest.document(),
            "events": [],
        }) + "\n")
        (source / "review-corrections.jsonl").write_bytes(b"")
        (source / "proposals.json").write_text(json.dumps([{
            "candidate_key": "candidate-one", "activity_id": "act-one",
            "evidence_ids": ["ev-one"], "description": "SC — Original wording",
        }]) + "\n")
        item = {"id": "rvi-one", "current": {"activity_id": "act-one", "evidence_ids": ["ev-one"]}}
        return runs, source, item

    def append(self, path: Path, item: dict, *, decision: str = "modify",
               categories: list[str] | None = None, patch: dict | None = None) -> None:
        record = review_corrections.build_decision(
            item, decision=decision, reviewer="reviewer", reviewed_at="2026-10-06T12:00:00Z",
            correction_categories=categories or ["wording"],
            rationale="Verified cited accomplishment.",
            field_patch=patch if patch is not None else {
                "description": {"op": "replace", "value": "SC — Corrected cited outcome"},
            } if decision == "modify" else None,
        )
        self.assertTrue(review_corrections.append_decision(path, record, item=item))

    def routing_fixture(self, root: Path) -> tuple[Path, Path, list[dict], Path]:
        runs, source, first = self.fixture(root)
        second = {"id": "rvi-two", "current": {"activity_id": "act-two", "evidence_ids": ["ev-two"]}}
        (source / "proposals.json").write_text(json.dumps([
            {"candidate_key": f"candidate-{index}", "activity_id": item["current"]["activity_id"],
             "evidence_ids": item["current"]["evidence_ids"], "description": "TSTP — Original wording"}
            for index, item in enumerate((first, second), 1)
        ]) + "\n")
        (source / "semantic-analysis.json").write_text(json.dumps({"activities": [
            item["current"] for item in (first, second)
        ]}) + "\n")
        routing = root / "selected-routing.json"
        routing.write_text(json.dumps({"session_routes": [{
            "project_name": "TST Prep Level 2", "project_suffix": "bc17f7",
            "tag_names": ["Technical development"], "tag_suffixes": ["35aa9b54"],
            "prefix": "TSTP", "billable": True,
        }], "meeting_routes": []}) + "\n")
        return runs, source, [first, second], routing

    def test_accepts_exact_appended_wording_for_source_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs, source, item = self.fixture(root)
            proposed = root / "wording.jsonl"
            self.append(proposed, item)
            source_before = (source / "review-corrections.jsonl").read_bytes()

            self.assertEqual(
                ("sha256:" + hashlib.sha256(source_before).hexdigest(),
                 "sha256:" + hashlib.sha256(proposed.read_bytes()).hexdigest()),
                run._validate_repair_credit_transition(source, proposed, runs_root=runs),
            )
            self.assertEqual(source_before, (source / "review-corrections.jsonl").read_bytes())

    def test_rejects_other_decisions_patches_and_stale_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs, source, item = self.fixture(root)
            cases = (
                ("approve", item, "approve", ["wording"], None),
                ("routing", item, "modify", ["routing"],
                 {"client_project": {"op": "replace", "value": "Other"}}),
                ("duration", item, "modify", ["wording"],
                 {"duration_minutes": {"op": "replace", "value": 20}}),
                ("blank", item, "modify", ["wording"],
                 {"description": {"op": "replace", "value": " "}}),
                ("stale", {"id": "rvi-stale", "current": {"activity_id": "act-one", "evidence_ids": ["ev-other"]}},
                 "modify", ["wording"], None),
            )
            for name, target, decision, categories, patch in cases:
                with self.subTest(name=name):
                    proposed = root / f"{name}.jsonl"
                    self.append(proposed, target, decision=decision, categories=categories, patch=patch)
                    with self.assertRaises(run.ReviewRunError):
                        run._validate_repair_credit_transition(source, proposed, runs_root=runs)

    def test_rejects_conflicting_wording_for_same_proposal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs, source, item = self.fixture(root)
            proposed = root / "duplicate.jsonl"
            self.append(proposed, item)
            another = {"id": "rvi-another", "current": item["current"]}
            self.append(proposed, another, patch={
                "description": {"op": "replace", "value": "SC — Another outcome"},
            })
            with self.assertRaises(run.ReviewRunError):
                run._validate_repair_credit_transition(source, proposed, runs_root=runs)

    def test_accepts_two_exact_combined_wording_and_configured_routing_edits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs, source, items, routing = self.routing_fixture(root)
            proposed = root / "corrected.jsonl"
            for item in items:
                self.append(proposed, item, categories=["routing", "wording"], patch={
                    "description": {"op": "replace", "value": "TSTP — Corrected cited outcome"},
                    "client_project": {"op": "replace", "value": "TST Prep Level 2"},
                    "tag_names": {"op": "replace", "value": ["Technical development"]},
                })
            expected = ("sha256:" + hashlib.sha256(b"").hexdigest(),
                        "sha256:" + hashlib.sha256(proposed.read_bytes()).hexdigest())
            self.assertEqual(expected, run._validate_repair_credit_transition(
                source, proposed, runs_root=runs, routing_snapshot=routing,
            ))

    def test_rejects_unavailable_or_ambiguous_selected_route(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs, source, items, routing = self.routing_fixture(root)
            proposed = root / "route.jsonl"
            self.append(proposed, items[0], categories=["routing"], patch={
                "client_project": {"op": "replace", "value": "TST Prep Level 2"},
                "tag_names": {"op": "replace", "value": ["Technical development"]},
            })
            original = json.loads(routing.read_text())
            for name, routes in (
                ("unavailable", []),
                ("ambiguous", [original["session_routes"][0],
                               {**original["session_routes"][0], "prefix": "ALT"}]),
            ):
                with self.subTest(name=name):
                    routing.write_text(json.dumps({"session_routes": routes, "meeting_routes": []}) + "\n")
                    with self.assertRaises(run.ReviewRunError):
                        run._validate_repair_credit_transition(
                            source, proposed, runs_root=runs, routing_snapshot=routing,
                        )

    def test_rejects_routing_when_original_activity_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs, source, items, routing = self.routing_fixture(root)
            proposed = root / "stale-activity.jsonl"
            self.append(proposed, items[1], categories=["routing"], patch={
                "client_project": {"op": "replace", "value": "TST Prep Level 2"},
                "tag_names": {"op": "replace", "value": ["Technical development"]},
            })
            (source / "semantic-analysis.json").write_text(json.dumps({
                "activities": [items[0]["current"]],
            }) + "\n")
            with self.assertRaises(run.ReviewRunError):
                run._validate_repair_credit_transition(
                    source, proposed, runs_root=runs, routing_snapshot=routing,
                )


if __name__ == "__main__":
    unittest.main()
