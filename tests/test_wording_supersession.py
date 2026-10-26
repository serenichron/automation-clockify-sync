"""Description-only edits replace wording, never frozen evidence or routing."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts import clockify_review_run as run, evidence_ledger
from scripts import review_corrections as corrections, work_accounting_pipeline as pipeline


ITEM = {"id": "rvi-one", "current": {
    "activity_id": "act-one", "workstream_id": "ws-one", "evidence_ids": ["ev-one"],
    "description": "SC — Original cited outcome",
}}
ROUTING = {"session_routes": [{
    "project_name": "Serenichron Level 2", "project_suffix": "project-one",
    "tag_names": ["Technical development"], "tag_suffixes": ["tag-one"],
    "prefix": "SC", "billable": True,
}], "meeting_routes": []}
PROPOSAL = {
    "candidate_key": "candidate-one", "activity_id": "act-one", "workstream_id": "ws-one",
    "evidence_ids": ["ev-one"], "provenance": {"evidence_ids": ["ev-one"]},
    "description": "SC — Original cited outcome", "client_project": "Serenichron Level 2",
    "tag_names": ["Technical development"], "project_suffix": "project-one",
    "tag_suffixes": ["tag-one"], "billable": True, "status": "pending",
    "start": "2026-10-06T09:00:00Z", "end": "2026-10-06T09:20:00Z",
    "duration_minutes": 20, "duration_seconds": 1200,
}


def decision(text="SC — First reviewed outcome", *, item=ITEM, kind="modify", categories=None, patch=None):
    return corrections.build_decision(
        item, decision=kind, reviewer="reviewer", reviewed_at="2026-10-06T12:00:00Z",
        correction_categories=categories or ["wording"], rationale="Verified cited outcome.",
        field_patch=patch if patch is not None else (
            {"description": {"op": "replace", "value": text}} if kind == "modify" else None),
    )


def superseding(parent, text="SC — Latest reviewed outcome", **kwargs):
    # Construct the wished-for signed v1 record without depending on the new API.
    record = decision(text, **kwargs)
    record["supersedes_digest"] = corrections.canonical_digest(corrections._without_integrity(parent))
    record["decision_id"] = "rdec-" + corrections.canonical_digest({
        key: value for key, value in record.items() if key != "decision_id"
    })[7:31]
    return record


def with_predecessor_digest(record, digest):
    record = copy.deepcopy(record)
    record["supersedes_digest"] = digest
    record["decision_id"] = "rdec-" + corrections.canonical_digest({
        key: value for key, value in record.items() if key != "decision_id"
    })[7:31]
    return record


def write_log(path, records):
    """Seal adversarial records without calling admission, to test real replay."""
    previous = None
    lines = []
    for record in records:
        line = copy.deepcopy(record)
        line["previous_digest"] = previous
        line["canonical_digest"] = corrections.canonical_digest(corrections._without_integrity(line))
        previous = line["canonical_digest"]
        lines.append(corrections.canonical_json(line) + "\n")
    path.write_text("".join(lines), encoding="utf-8")


class WordingSupersessionTests(unittest.TestCase):
    def accepted(self, operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except (corrections.ReviewDecisionError, run.ReviewRunError) as error:
            self.fail(f"Valid description-only supersession was rejected: {error}")

    def fixture(self, root, records=()):
        runs = root / "runs"
        source = runs / "source-run"
        evidence = source / "evidence"
        evidence.mkdir(parents=True)
        ledger = evidence_ledger.EvidenceLedger()
        (evidence / "evidence-ledger.json").write_text(json.dumps({
            "schema_version": ledger.manifest.schema_version,
            "manifest": ledger.manifest.document(), "events": [],
        }) + "\n")
        write_log(source / "review-corrections.jsonl", records)
        (source / "proposals.json").write_text(json.dumps([PROPOSAL]) + "\n")
        (source / "semantic-analysis.json").write_text(json.dumps({"activities": [ITEM["current"]]}) + "\n")
        (source / "routing.json").write_text(json.dumps(ROUTING) + "\n")
        return runs, source

    def test_builder_binds_optional_predecessor_in_signed_decision_content(self):
        first = decision()
        digest = corrections.canonical_digest(first)
        try:
            record = corrections.build_decision(
                ITEM, decision="modify", reviewer="reviewer", reviewed_at="2026-10-06T12:00:00Z",
                correction_categories=["wording"], rationale="Verified cited outcome.",
                field_patch={"description": {"op": "replace", "value": "SC — Latest reviewed outcome"}},
                supersedes_digest=digest,
            )
        except TypeError as error:
            self.fail(f"Missing optional predecessor API: {error}")
        self.assertEqual(digest, corrections.validate_decision(record)["supersedes_digest"])
        altered = {**record, "supersedes_digest": "sha256:" + "0" * 64}
        with self.assertRaises(corrections.ReviewDecisionError):
            corrections.validate_decision(altered)

    def test_two_successive_edits_append_without_rewriting_old_records(self):
        # Catches immutable conflict rejection and obsolete wording replay.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corrections.jsonl"
            first = decision()
            corrections.append_decision(path, first)
            original = path.read_bytes()
            second = superseding(first)
            self.assertTrue(self.accepted(corrections.append_decision, path, second))
            after_second = path.read_bytes()
            third = superseding(second, "SC — Final reviewed outcome")
            self.assertTrue(self.accepted(corrections.append_decision, path, third))
            self.assertTrue(path.read_bytes().startswith(after_second))
            self.assertTrue(after_second.startswith(original))
            self.assertFalse(corrections.append_decision(path, third))
            records = corrections.load_decisions(path)
            self.assertEqual("SC — First reviewed outcome", records[0]["field_patch"]["description"]["value"])
            cases = corrections.derive_regression_cases(records)
            self.assertEqual("SC — Final reviewed outcome", pipeline._description_from_review_correction(PROPOSAL, cases))
            latest = {**PROPOSAL, "description": "SC — Final reviewed outcome"}
            self.assertEqual({"pass": 1, "fail": 0, "not_applicable": 0},
                             corrections.evaluate_regression_cases(cases, [latest])["summary"])
            self.assertEqual(1, corrections.evaluate_regression_cases(cases, [PROPOSAL])["summary"]["fail"])

    def test_repair_admits_chain_on_exact_parent_snapshot_and_preserves_source(self):
        # Catches repair's blanket duplicate activity/evidence rejection.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = decision()
            runs, source = self.fixture(root, [first])
            source_before = {path.name: path.read_bytes() for path in source.iterdir() if path.is_file()}
            second = superseding(first)
            third = superseding(second, "SC — Final reviewed outcome")
            proposed = root / "proposed.jsonl"
            write_log(proposed, [first, second, third])
            expected = tuple("sha256:" + hashlib.sha256(content).hexdigest()
                             for content in (source_before["review-corrections.jsonl"], proposed.read_bytes()))
            self.assertEqual(expected, self.accepted(run._validate_repair_credit_transition, source, proposed, runs_root=runs))
            self.assertEqual(source_before, {path.name: path.read_bytes() for path in source.iterdir() if path.is_file()})

    def test_latest_wording_retains_prior_routing_effect_and_regression(self):
        # Catches dropping routing with the old wording or shadowing it during routing selection.
        first = decision(categories=["wording", "routing"], patch={
            "description": {"op": "replace", "value": "SC — First reviewed outcome"},
            "client_project": {"op": "replace", "value": "Serenichron Level 2"},
            "tag_names": {"op": "replace", "value": ["Technical development"]},
        })
        second = superseding(first)
        cases = self.accepted(corrections.derive_regression_cases, [first, second])
        self.assertEqual("SC — Latest reviewed outcome", pipeline._description_from_review_correction(PROPOSAL, cases))
        route = pipeline._route_from_review_correction(PROPOSAL, cases, ROUTING)
        self.assertEqual("Serenichron Level 2", route["project_name"])
        latest = {**PROPOSAL, "description": "SC — Latest reviewed outcome"}
        self.assertEqual({"pass": 2, "fail": 0, "not_applicable": 0},
                         corrections.evaluate_regression_cases(cases, [latest])["summary"])
        self.assertEqual(1, corrections.evaluate_regression_cases(cases, [{**latest, "client_project": "Wrong"}])["summary"]["fail"])

    def test_implicit_duplicates_and_non_wording_predecessors_remain_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name, first in (
                ("wording", decision()),
                ("skip", decision(kind="skip", categories=["omission"])),
                ("approve", decision(kind="approve")),
                ("routing", decision(categories=["routing"], patch={"client_project": {"op": "replace", "value": "Other"}})),
                ("time", decision(categories=["allocation"], patch={"duration_minutes": {"op": "replace", "value": 25}})),
            ):
                with self.subTest(name=name):
                    path = root / (name + ".jsonl")
                    corrections.append_decision(path, first)
                    with self.assertRaises(corrections.ReviewDecisionError):
                        corrections.append_decision(path, decision("SC — Conflicting implicit edit"))
                    if name != "wording":
                        with self.assertRaises(corrections.ReviewDecisionError):
                            corrections.append_decision(path, superseding(first))
            skipped = decision(kind="skip", categories=["omission"])
            cases = corrections.derive_regression_cases([skipped])
            self.assertEqual(1, corrections.evaluate_regression_cases(cases, [])["summary"]["pass"])
            self.assertEqual(1, corrections.evaluate_regression_cases(cases, [PROPOSAL])["summary"]["fail"])

    def test_repair_rejects_forged_missing_wrong_target_stale_fork_and_cycles(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = decision()
            runs, source = self.fixture(root, [first])
            second = superseding(first)
            other_item = {"id": "rvi-other", "current": {
                "activity_id": "act-other", "evidence_ids": ["ev-other"],
            }}
            other = decision(item=other_item)
            bad_digest = superseding(first)
            bad_digest["supersedes_digest"] = "sha256:" + "0" * 64
            bad_digest["decision_id"] = "rdec-" + corrections.canonical_digest({
                key: value for key, value in bad_digest.items() if key != "decision_id"
            })[7:31]
            forged = superseding(first)
            forged["supersedes_digest"] = "sha256:" + "1" * 64
            cases = {
                "missing": [first, bad_digest], "forged": [first, forged],
                "wrong-target": [first, other, superseding(other)],
                "wrong-parent-target": [first, other, superseding(first, item=other_item)],
                "stale-fork": [first, second, superseding(first, "SC — Forked outcome")],
                "implicit": [first, decision("SC — Implicit outcome")],
                "cycle": [first, superseding(second), second],
            }
            for name, records in cases.items():
                with self.subTest(name=name):
                    proposed = root / (name + ".jsonl")
                    write_log(proposed, records)
                    with self.assertRaises(run.ReviewRunError):
                        run._validate_repair_credit_transition(source, proposed, runs_root=runs)

    def test_append_rejects_stale_and_absent_parent_without_changing_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corrections.jsonl"
            first = decision()
            with self.assertRaises(corrections.ReviewDecisionError):
                corrections.append_decision(path, superseding(first))
            self.assertFalse(path.exists())
            corrections.append_decision(path, first)
            second = superseding(first)
            corrections.append_decision(path, second)
            before = path.read_bytes()
            with self.assertRaises(corrections.ReviewDecisionError):
                corrections.append_decision(path, superseding(first, "SC — Forked outcome"))
            self.assertEqual(before, path.read_bytes())

    def test_legacy_metadata_record_can_be_superseded_using_loaded_content_digest(self):
        # Catches rehashing normalized content after legacy metadata was discarded.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corrections.jsonl"
            first = {**decision(), "operator_metadata": {"note": "historical local review"}}
            write_log(path, [first])
            original = path.read_bytes()
            loaded = corrections._read_log(path)[0]
            self.assertNotEqual(corrections.canonical_digest(corrections._without_integrity(loaded)),
                                loaded["canonical_digest"])
            second = with_predecessor_digest(decision("SC — Latest reviewed outcome"), loaded["canonical_digest"])
            self.assertTrue(self.accepted(corrections.append_decision, path, second))
            self.assertTrue(path.read_bytes().startswith(original))
            records = self.accepted(corrections.load_decisions, path)
            cases = self.accepted(corrections.derive_regression_cases, records)
            self.assertEqual("SC — Latest reviewed outcome", pipeline._description_from_review_correction(PROPOSAL, cases))

    def test_regression_retains_verified_legacy_digest_through_normalization(self):
        # Catches derivation losing the original hash even when append admission is fixed.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corrections.jsonl"
            first = {**decision(), "operator_metadata": "historical local review"}
            write_log(path, [first])
            loaded = corrections._read_log(path)[0]
            second = with_predecessor_digest(decision("SC — Latest reviewed outcome"), loaded["canonical_digest"])
            cases = self.accepted(corrections.derive_regression_cases, [loaded, second])
            latest = {**PROPOSAL, "description": "SC — Latest reviewed outcome"}
            self.assertEqual({"pass": 1, "fail": 0, "not_applicable": 0},
                             corrections.evaluate_regression_cases(cases, [latest])["summary"])

    def test_forged_serialized_legacy_digest_is_not_predecessor_authority(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corrections.jsonl"
            first = {**decision(), "operator_metadata": "historical local review"}
            write_log(path, [first])
            line = json.loads(path.read_text())
            line["canonical_digest"] = "sha256:" + "0" * 64
            path.write_text(corrections.canonical_json(line) + "\n")
            second = with_predecessor_digest(decision("SC — Latest reviewed outcome"), line["canonical_digest"])
            before = path.read_bytes()
            with self.assertRaises(corrections.ReviewDecisionError):
                corrections.append_decision(path, second)
            with self.assertRaises(corrections.ReviewDecisionError):
                corrections.load_decisions(path)
            self.assertEqual(before, path.read_bytes())

    def test_supersession_cannot_change_time_project_status_or_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = decision()
            runs, source = self.fixture(root, [first])
            records = []
            for field, value in (("duration_minutes", 25), ("client_project", "Other"), ("status", "approved")):
                record = superseding(first)
                record["field_patch"][field] = {"op": "replace", "value": value}
                if field == "client_project":
                    record["correction_categories"].append("routing")
                record["decision_id"] = "rdec-" + corrections.canonical_digest({
                    key: value for key, value in record.items() if key != "decision_id"
                })[7:31]
                records.append(record)
            records.extend([superseding(first, kind="skip", categories=["omission"]),
                            superseding(first, kind="approve")])
            for index, record in enumerate(records):
                with self.subTest(index=index):
                    proposed = root / f"effect-{index}.jsonl"
                    write_log(proposed, [first, record])
                    with self.assertRaises(run.ReviewRunError):
                        run._validate_repair_credit_transition(source, proposed, runs_root=runs)


if __name__ == "__main__":
    unittest.main()
