"""Wording amendments cannot rewrite prior financial decisions or source facts."""
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from scripts import clockify_review_run as run, review_corrections as corrections
from scripts import work_accounting_pipeline as pipeline
from scripts import collector_receipts, evidence_ledger


class WordingAmendmentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        self.parent = self.source / "review-corrections.jsonl"
        self.proposal = {"activity_id": "act-one", "candidate_key": "candidate-one",
                         "evidence_ids": ["ev-one"], "description": "SC — Reviewed checkout for reliable client payments",
                         "client_project": "Serenichron Level 2", "tag_names": ["Processes"],
                         "start": "2026-10-05T09:00:00Z", "end": "2026-10-05T09:30:00Z",
                         "duration_minutes": 30, "duration_seconds": 1800, "billable": True}
        self.item = {"id": "rvi-one", "current": self.proposal}
        base = corrections.build_decision(
            self.item, decision="modify", reviewer="human", reviewed_at="2026-10-06T00:00:00Z",
            correction_categories=["wording", "routing", "allocation"], rationale="Retain source facts",
            field_patch={field: {"op": "replace", "value": self.proposal[field]}
                         for field in ("description", "client_project", "tag_names", "duration_minutes")})
        corrections.append_decision(self.parent, base, item=self.item)
        self.predecessor = corrections._read_log(self.parent)[0]
        for name, value in (("proposals.json", [self.proposal]),
                            ("work-accounting-result.json", {"proposals": [self.proposal]}),
                            ("semantic-analysis.json", {"activities": [self.proposal]}),
                            ("review-snapshot.json", {"categories": {"new": [{"id": "rvi-one", **self.proposal}]}})):
            (self.source / name).write_text(json.dumps(value) + "\n")
        self.digest = "sha256:" + hashlib.sha256((self.source / "proposals.json").read_bytes()).hexdigest()
        ledger = evidence_ledger.EvidenceLedger()
        (self.source / "evidence").mkdir()
        (self.source / "evidence/evidence-ledger.json").write_text(json.dumps({
            "schema_version": ledger.manifest.schema_version,
            "manifest": ledger.manifest.document(), "events": []}) + "\n")
        (self.source / "run-report.json").write_text(json.dumps({
            "date_range": {"since": "2026-10-05T00:00:00Z", "until": "2026-10-06T00:00:00Z"},
            "evidence_ledger": {"source_completeness": ledger.manifest.document()["source_completeness"]},
            "runtime_identity": {"fixture": "local-only"}}) + "\n")
        (self.source / "quality_report.json").write_text("{}\n")
        self.seal()

    def seal(self):
        collector_receipts.write_completion_bundle(self.source / "completion-bundle.json",
            collector_receipts.build_completion_bundle(self.source, slice_=SimpleNamespace(
                slice_id="fixture", since=dt.datetime(2026, 10, 5, tzinfo=dt.UTC),
                until=dt.datetime(2026, 10, 6, tzinfo=dt.UTC))))

    def amendment(self, description="SC — Improved checkout for reliable client payments", **overrides):
        options = {"predecessor": self.predecessor, "parent_proposals_sha256": self.digest,
                   "description": description, "reviewer": "human", "reviewed_at": "2026-10-08T00:00:00Z",
                   "rationale": "Accepted exact client wording"}
        options.update(overrides)
        return corrections.build_wording_amendment(self.item, **options)

    def resign(self, record):
        record = corrections._without_integrity(record)
        record.pop("amendment_id", None)
        record["amendment_id"] = "wamd-" + corrections.canonical_digest(record)[7:31]
        return record

    def proposed(self):
        path = self.root / "proposed.jsonl"
        path.write_bytes(self.parent.read_bytes())
        return path

    def test_existing_pipeline_reads_explicit_description_only_amendment(self):
        # Rejecting this record (or ignoring its active wording) loses the repair.
        record = {"schema_version": 1, "record_type": "wording_amendment",
                  **corrections.review_target(self.item),
                  "predecessor_canonical_digest": self.predecessor["canonical_digest"],
                  "parent_proposals_sha256": self.digest,
                  "parent_description": self.proposal["description"],
                  "field_patch": {"description": {"op": "replace", "value": "SC — Improved checkout for reliable client payments"}},
                  "reviewer": "human", "reviewed_at": "2026-10-08T00:00:00Z",
                  "rationale": "Accepted exact client wording"}
        record["amendment_id"] = "wamd-" + corrections.canonical_digest(record)[7:31]
        record["previous_digest"] = self.predecessor["canonical_digest"]
        record["canonical_digest"] = corrections.canonical_digest(corrections._without_integrity(record))
        proposed = self.proposed()
        with proposed.open("a") as handle:
            handle.write(corrections.canonical_json(record) + "\n")
        try:
            cases = pipeline._load_regression_cases(proposed)
        except pipeline.WorkAccountingError as error:
            self.fail(f"explicit source-bound wording amendment must load: {error}")
        self.assertEqual(record["field_patch"]["description"]["value"],
                         pipeline._description_from_review_correction(self.proposal, cases))

    def test_latest_wording_keeps_original_decisions_learning_and_financial_expectations(self):
        proposed = self.proposed()
        old = self.parent.read_bytes()
        base = corrections.load_decisions(self.parent)
        self.assertTrue(corrections.append_wording_amendment(proposed, self.amendment(), item=self.item))
        self.assertFalse(corrections.append_wording_amendment(proposed, self.amendment(), item=self.item))
        self.assertEqual(old, self.parent.read_bytes())
        self.assertTrue(proposed.read_bytes().startswith(old))
        self.assertEqual(base, corrections.load_decisions(proposed))
        self.assertEqual(corrections.derive_learning_cases(base),
                         corrections.derive_learning_cases(corrections.load_decisions(proposed)))
        case = corrections.load_regression_cases(proposed)[0]
        self.assertEqual("SC — Improved checkout for reliable client payments", case["expected_field_patch"]["description"]["value"])
        for field in ("client_project", "tag_names", "duration_minutes"):
            self.assertEqual(self.predecessor["field_patch"][field], case["expected_field_patch"][field])
        self.assertEqual("SC — Improved checkout for reliable client payments",
                         pipeline._description_from_review_correction(self.proposal, pipeline._load_regression_cases(proposed)))
        self.assertEqual(corrections.derive_regression_cases(base), corrections.load_regression_cases(self.parent))

    def test_financial_patch_extra_fields_stale_evidence_and_tamper_are_rejected(self):
        record = self.amendment()
        for field in ("client_project", "tag_names", "start", "end", "duration_minutes", "duration_seconds", "billable"):
            with self.subTest(field=field):
                altered = copy.deepcopy(record)
                altered["field_patch"][field] = {"op": "replace", "value": self.proposal[field]}
                with self.assertRaises(corrections.ReviewDecisionError):
                    corrections.validate_wording_amendment(self.resign(altered))
        for field, value in (("unexpected", True), ("predecessor_canonical_digest", "sha256:" + "0" * 64),
                             ("activity_id", "act-other"), ("evidence_fingerprint", "evfp:sha256:" + "0" * 64)):
            with self.subTest(field=field):
                altered = copy.deepcopy(record)
                altered[field] = value
                with self.assertRaises(corrections.ReviewDecisionError):
                    corrections.append_wording_amendment(self.proposed(), self.resign(altered), item=self.item)

    def test_branching_predecessor_and_stale_preimage_do_not_advance_head(self):
        proposed = self.proposed()
        corrections.append_wording_amendment(proposed, self.amendment(), item=self.item)
        before = proposed.read_bytes()
        with self.assertRaises(corrections.ReviewDecisionError):
            corrections.append_wording_amendment(proposed, self.amendment("SC — Revised checkout for reliable client payments"), item=self.item)
        self.assertEqual(before, proposed.read_bytes())
        stale = copy.deepcopy(self.item)
        stale["current"]["description"] = "SC — Different source wording for reliable client payments"
        with self.assertRaises(corrections.ReviewDecisionError):
            corrections.build_wording_amendment(stale, predecessor=self.predecessor,
                parent_proposals_sha256=self.digest, description="SC — Revised checkout for reliable client payments",
                reviewer="human", reviewed_at="2026-10-08T00:00:00Z", rationale="Exact wording")

    def test_wrong_source_artifact_or_proposal_preimage_fails_repair_transition(self):
        for mutation in ("artifact", "preimage", "accounting", "evidence", "snapshot"):
            with self.subTest(mutation=mutation):
                proposed = self.proposed()
                record = self.amendment(parent_proposals_sha256="sha256:" + "0" * 64) if mutation == "artifact" else self.amendment()
                corrections.append_wording_amendment(proposed, record, item=self.item)
                names = {"preimage": "proposals.json", "accounting": "work-accounting-result.json",
                         "evidence": "semantic-analysis.json", "snapshot": "review-snapshot.json"}
                path = self.source / names[mutation] if mutation != "artifact" else None
                original = path.read_bytes() if path else None
                if path:
                    path.write_text("{}\n")
                with self.assertRaises(run.ReviewRunError):
                    run._validate_repair_credit_transition(self.source, proposed, runs_root=self.root)
                if path:
                    path.write_bytes(original)

    def test_exact_sealed_amendment_enters_repair_without_changing_parent(self):
        proposed = self.proposed()
        original = self.parent.read_bytes()
        corrections.append_wording_amendment(proposed, self.amendment(), item=self.item)
        try:
            actual = run._validate_repair_credit_transition(self.source, proposed, runs_root=self.root)
        except run.ReviewRunError as error:
            self.fail(f"sealed description-only amendment must enter repair: {error}")
        self.assertEqual(("sha256:" + hashlib.sha256(original).hexdigest(),
                          "sha256:" + hashlib.sha256(proposed.read_bytes()).hexdigest()), actual)
        self.assertEqual(original, self.parent.read_bytes())

    def test_chained_amendment_rejects_foreign_head_and_retains_original_routing(self):
        proposed = self.proposed()
        corrections.append_wording_amendment(proposed, self.amendment(), item=self.item)
        head = corrections._read_log(proposed)[-1]
        item = copy.deepcopy(self.item)
        item["current"]["description"] = head["field_patch"]["description"]["value"]
        foreign = copy.deepcopy(item)
        foreign["current"]["activity_id"] = "act-other"
        with self.assertRaises(corrections.ReviewDecisionError):
            corrections.build_wording_amendment(foreign, predecessor=head,
                parent_proposals_sha256=self.digest, description="SC — Revised checkout for reliable client payments",
                reviewer="human", reviewed_at="2026-10-09T00:00:00Z", rationale="Exact wording")
        next_record = corrections.build_wording_amendment(item, predecessor=head,
            parent_proposals_sha256=self.digest, description="SC — Revised checkout for reliable client payments",
            reviewer="human", reviewed_at="2026-10-09T00:00:00Z", rationale="Exact wording")
        corrections.append_wording_amendment(proposed, next_record, item=item)
        case = corrections.load_regression_cases(proposed)[0]
        self.assertEqual(next_record["field_patch"]["description"], case["expected_field_patch"]["description"])
        self.assertEqual(self.predecessor["field_patch"]["client_project"], case["expected_field_patch"]["client_project"])

    def test_later_ordinary_decision_cannot_make_amended_target_ambiguous(self):
        proposed = self.proposed()
        corrections.append_wording_amendment(proposed, self.amendment(), item=self.item)
        conflicting = corrections.build_decision({"id": "rvi-another", "current": self.proposal},
            decision="modify", reviewer="human", reviewed_at="2026-10-09T00:00:00Z",
            correction_categories=["routing"], rationale="Conflicting ordinary target",
            field_patch={"client_project": {"op": "replace", "value": "Other client"}})
        conflicting["previous_digest"] = corrections._read_log(proposed)[-1]["canonical_digest"]
        conflicting["canonical_digest"] = corrections.canonical_digest(corrections._without_integrity(conflicting))
        with proposed.open("a") as handle:
            handle.write(corrections.canonical_json(conflicting) + "\n")
        with self.assertRaises(corrections.ReviewDecisionError):
            corrections.load_regression_cases(proposed)

    def test_ordinary_append_rejects_amended_target_before_writing(self):
        proposed = self.proposed()
        corrections.append_wording_amendment(proposed, self.amendment(), item=self.item)
        before = proposed.read_bytes()
        decision = corrections.build_decision({"id": "rvi-new-source", "current": self.proposal},
            decision="modify", reviewer="human", reviewed_at="2026-10-09T00:00:00Z",
            correction_categories=["routing"], rationale="Cannot add a new financial decision to an amended target",
            field_patch={"client_project": {"op": "replace", "value": "Other client"}})
        with self.assertRaises(corrections.ReviewDecisionError):
            corrections.append_decision(proposed, decision)
        self.assertEqual(before, proposed.read_bytes())

    def test_log_tamper_reorder_missing_head_and_ambiguous_original_fail_closed(self):
        proposed = self.proposed()
        corrections.append_wording_amendment(proposed, self.amendment(), item=self.item)
        lines = proposed.read_text().splitlines()
        altered = json.loads(lines[1])
        altered["field_patch"]["description"]["value"] = "SC — Unreviewed client wording"
        for broken in (lines[::-1], [lines[1]], [lines[0], corrections.canonical_json(altered)]):
            proposed.write_text("\n".join(broken) + "\n")
            with self.assertRaises(corrections.ReviewDecisionError):
                corrections.load_decisions(proposed)
        proposed = self.proposed()
        another = corrections.build_decision({"id": "rvi-another", "current": self.proposal},
            decision="modify", reviewer="human", reviewed_at="2026-10-09T00:00:00Z",
            correction_categories=["wording"], rationale="Another ordinary target",
            field_patch={"description": {"op": "replace", "value": self.proposal["description"]}})
        corrections.append_decision(proposed, another)
        with self.assertRaises(corrections.ReviewDecisionError):
            corrections.append_wording_amendment(proposed, self.amendment(), item=self.item)

    def test_nonwording_predecessors_and_telemetry_are_not_amendable(self):
        for decision, categories, patch in (
            ("approve", ["wording"], None), ("skip", ["wording"], None),
            ("modify", ["routing"], {"client_project": {"op": "replace", "value": "Other"}}),
            ("modify", ["split"], {}),
        ):
            with self.subTest(decision=decision, categories=categories):
                path = self.root / "ineligible.jsonl"
                path.write_bytes(b"")
                record = corrections.build_decision(self.item, decision=decision, reviewer="human",
                    reviewed_at="2026-10-06T00:00:00Z", correction_categories=categories,
                    rationale="Ineligible predecessor", field_patch=patch,
                    _allow_legacy_split_without_expectation=True)
                corrections.append_decision(path, record)
                with self.assertRaises(corrections.ReviewDecisionError):
                    self.amendment(predecessor=corrections._read_log(path)[0])
        for description in ("SC — Verified checkout with 37 tests passed", "SC — Deployed commit 799a44e"):
            with self.subTest(description=description), self.assertRaises(corrections.ReviewDecisionError):
                self.amendment(description)
        self.amendment("SC — Authored tests for checkout serving 37 client accounts")

    def test_resealed_foreign_semantic_snapshot_accounting_and_split_preimages_fail(self):
        baseline = {name: (self.source / name).read_bytes() for name in (
            "proposals.json", "semantic-analysis.json", "review-snapshot.json", "work-accounting-result.json")}
        for mutation in ("semantic", "snapshot", "snapshot_id", "accounting", "split_preimage", "missing_seal"):
            with self.subTest(mutation=mutation):
                for name, content in baseline.items():
                    (self.source / name).write_bytes(content)
                if mutation == "semantic":
                    value = {"activities": [{**self.proposal, "evidence_ids": ["foreign"]}]}
                    (self.source / "semantic-analysis.json").write_text(json.dumps(value) + "\n")
                elif mutation.startswith("snapshot"):
                    value = {"categories": {"new": [{"id": "foreign", **self.proposal}]}}
                    if mutation == "snapshot":
                        value["categories"]["new"][0]["evidence_ids"] = ["foreign"]
                    (self.source / "review-snapshot.json").write_text(json.dumps(value) + "\n")
                elif mutation == "accounting":
                    (self.source / "work-accounting-result.json").write_text('{"proposals": []}\n')
                elif mutation == "split_preimage":
                    proposals = [self.proposal, {**self.proposal, "candidate_key": "second", "description": "SC — Foreign preimage"}]
                    (self.source / "proposals.json").write_text(json.dumps(proposals) + "\n")
                    (self.source / "work-accounting-result.json").write_text(json.dumps({"proposals": proposals}) + "\n")
                self.seal()
                if mutation == "missing_seal":
                    (self.source / "completion-bundle.json").unlink()
                digest = "sha256:" + hashlib.sha256((self.source / "proposals.json").read_bytes()).hexdigest()
                proposed = self.proposed()
                corrections.append_wording_amendment(proposed, self.amendment(parent_proposals_sha256=digest), item=self.item)
                with self.assertRaises(run.ReviewRunError):
                    run._validate_repair_credit_transition(self.source, proposed, runs_root=self.root)


class WordingAmendmentReplayTests(unittest.TestCase):
    def test_full_pipeline_amends_every_segment_without_changing_combined_route_or_effort(self):
        import test_work_accounting_pipeline as fixtures
        fixture = fixtures.WorkAccountingPipelineTests("runTest")
        self.addCleanup(fixture.doCleanups)
        first = fixtures.session_event("wording-first", "2026-07-10T09:00:00+03:00", span_end="2026-07-10T12:00:00+03:00")
        last = fixtures.session_event("wording-last", "2026-07-10T12:00:00+03:00")
        existing = fixtures.clockify_event("2026-07-10T10:00:00+03:00", "2026-07-10T10:30:00+03:00")
        source, baseline = fixture.make_run([first, last, existing],
            fixtures.analysis_for([first.evidence_id, last.evidence_id], recommended=120))
        self.assertEqual(2, len(baseline["proposals"]))
        proposal = baseline["proposals"][0]
        item = {"id": "rvi-multisegment", "current": proposal}
        parent = source.parent.parent / "parent-corrections.jsonl"
        decision = corrections.build_decision(item, decision="modify", reviewer="offline-test-only",
            reviewed_at="2026-10-06T00:00:00Z", correction_categories=["wording", "routing", "allocation"],
            rationale="Synthetic retained route and active effort", field_patch={
                "description": {"op": "replace", "value": proposal["description"]},
                "client_project": {"op": "replace", "value": proposal["client_project"]},
                "tag_names": {"op": "replace", "value": proposal["tag_names"]},
                "duration_minutes": {"op": "replace", "value": 120}})
        corrections.append_decision(parent, decision, item=item)
        old = pipeline.run_accounting(source, root=fixtures.ROOT,
            analysis_fixture=source.parent.parent / "analysis.json", corrections_path=parent)
        self.assertEqual(2, len(old["proposals"]))
        original_log = parent.read_bytes()
        child = parent.with_name("child-corrections.jsonl")
        child.write_bytes(original_log)
        replacement = "SC — Improved accounting review for reliable client reporting"
        record = corrections.build_wording_amendment(item, predecessor=corrections._read_log(parent)[0],
            parent_proposals_sha256="sha256:" + hashlib.sha256((source / "proposals.json").read_bytes()).hexdigest(),
            description=replacement, reviewer="offline-test-only", reviewed_at="2026-10-08T00:00:00Z",
            rationale="Synthetic accepted wording")
        corrections.append_wording_amendment(child, record, item=item)
        results = [pipeline.run_accounting(source, root=fixtures.ROOT,
            analysis_fixture=source.parent.parent / "analysis.json", corrections_path=child) for _ in range(2)]
        self.assertEqual(results[0]["proposals"], results[1]["proposals"])
        self.assertEqual(2, len(results[0]["proposals"]))
        self.assertEqual({replacement}, {row["description"] for row in results[0]["proposals"]})
        for before, after in zip(old["proposals"], results[0]["proposals"], strict=True):
            self.assertEqual({key: value for key, value in before.items() if key not in {"description", "rendered_description"}},
                             {key: value for key, value in after.items() if key not in {"description", "rendered_description"}})
        self.assertEqual(original_log, parent.read_bytes())
        self.assertEqual([decision["decision_id"]], [row["decision_id"] for row in corrections.load_decisions(child)])
        self.assertEqual(old["correction_regression"]["summary"], results[0]["correction_regression"]["summary"])

    def test_sealed_child_replays_without_inference_and_preserves_every_other_proposal_field(self):
        import test_review_run as fixtures
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runs = root / "runs"
            source = fixtures.ReviewRunResultTests._write_real_offline_replay_source(runs, root)
            proposals = json.loads((source / "proposals.json").read_text())
            snapshot = json.loads((source / "review-snapshot.json").read_text())
            items = [row for group in snapshot["categories"].values() for row in group]
            proposal = proposals[0]
            item = {"id": next(row["id"] for row in items if corrections.proposal_target(row) == corrections.proposal_target(proposal)),
                    "current": proposal}
            original = corrections.build_decision(item, decision="modify", reviewer="offline-test-only",
                reviewed_at="2026-10-06T00:00:00Z", correction_categories=["wording"],
                rationale="Synthetic local fixture, not a human approval receipt",
                field_patch={"description": {"op": "replace", "value": proposal["description"]}})
            corrections.append_decision(source / "review-corrections.jsonl", original, item=item)
            parent_before = fixtures.run_tree_snapshot(source)
            proposed = root / "amended.jsonl"
            proposed.write_bytes((source / "review-corrections.jsonl").read_bytes())
            replacement = "SC — Improved offline review for reliable accounting"
            amendment = corrections.build_wording_amendment(item,
                predecessor=corrections._read_log(proposed)[0],
                parent_proposals_sha256="sha256:" + hashlib.sha256((source / "proposals.json").read_bytes()).hexdigest(),
                description=replacement, reviewer="offline-test-only", reviewed_at="2026-10-08T00:00:00Z",
                rationale="Synthetic accepted wording fixture")
            corrections.append_wording_amendment(proposed, amendment, item=item)
            with mock.patch.object(run, "RUNS", runs), mock.patch.object(run, "_sealed_replay_transport", side_effect=AssertionError("network forbidden")):
                child = run._prepare_repair_run(source, corrections_override=proposed)
                analysis = run._repair_analysis_fixture(child)
                pipeline.run_accounting(child, root=fixtures.ROOT, routing_path=child / "routing.json",
                    corrections_path=child / "review-corrections.jsonl", analysis_fixture=analysis)
                first = (child / "proposals.json").read_bytes()
                pipeline.run_accounting(child, root=fixtures.ROOT, routing_path=child / "routing.json",
                    corrections_path=child / "review-corrections.jsonl", analysis_fixture=analysis)
                self.assertEqual(first, (child / "proposals.json").read_bytes())
            changed = json.loads(first)
            self.assertEqual(len(proposals), len(changed))
            for old, new in zip(proposals, changed):
                self.assertEqual(replacement, new["description"])
                self.assertEqual({key: value for key, value in old.items() if key not in {"description", "rendered_description"}},
                                 {key: value for key, value in new.items() if key not in {"description", "rendered_description"}})
            self.assertEqual(parent_before, fixtures.run_tree_snapshot(source))
            self.assertEqual(proposal["description"], corrections.load_regression_cases(source / "review-corrections.jsonl")[0]["expected_field_patch"]["description"]["value"])
