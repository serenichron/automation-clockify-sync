"""Source-only wording cannot promote untimed work or rewrite accounted facts."""
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

from scripts import clockify_review_run as run
from scripts import collector_receipts, evidence_ledger
from scripts import review_corrections as corrections
from scripts import work_accounting_pipeline as pipeline
from test_work_accounting_pipeline import session_event


NEW = "SC — Reconciled the policy update while preserving cleanup rules"


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n")


def digest(path):
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def sign(record):
    record = corrections._without_integrity(record)
    record.pop("correction_id", None)
    record["correction_id"] = "zwrd-" + corrections.canonical_digest(record)[7:31]
    return record


class ZeroAllocationWordingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        event = session_event("zero-allocation", "2026-10-05T09:00:00Z",
                              span_end="2026-10-05T09:10:00Z")
        ledger = evidence_ledger.EvidenceLedger((event,), {}, "Europe/Bucharest")
        (self.source / "evidence").mkdir()
        write(self.source / "evidence/evidence-ledger.json", {
            "schema_version": ledger.manifest.schema_version,
            "manifest": ledger.manifest.document(), "events": [event.document()]})
        self.activity = {
            "activity_id": "act-zero", "evidence_ids": [event.evidence_id],
            "action": "Reconciled", "object": "policy update",
            "outcome": "preserving cleanup rules over commit d63a5ed",
            "rendered_description": "SC — Reconciled policy update over commit d63a5ed preserving cleanup rules",
            "effort": {"minimum_minutes": 8, "recommended_minutes": 15, "maximum_minutes": 25},
            "workstream_id": "ws-policy", "semantic_reviewer_model": "fixture-reviewer",
        }
        self.fingerprint = corrections.evidence_fingerprint(self.activity["evidence_ids"])
        self.proposals = [{"activity_id": "act-postable", "evidence_ids": [event.evidence_id],
            "candidate_key": "candidate-postable", "description": "SC — Reviewed client reporting",
            "id": "P001", "duration_minutes": 10, "duration_seconds": 600,
            "client_project": "Internal", "tag_names": ["Systems"]}]
        self.semantic = {"activities": [self.activity], "prompt_version": "fixture", "exceptions": [],
                         "omissions": [], "noise_classifications": []}
        self.accounting = {
            "schema_version": 1, "allocation_mode": "non_overlapping_v1", "proposals": self.proposals,
            "semantic_analysis": {"prompt_version": "fixture", "activity_count": 1, "exception_count": 0,
                                  "omission_count": 0, "noise_count": 0, "learning_case_count": 0},
            "allocation": {
                "allocations": [{"activity_id": "act-postable", "duration_minutes": 10}],
                "evidence": [{"activity_id": "act-zero", "workstream_id": "ws-policy",
                              "effort": self.activity["effort"], "allowed_intervals": [["09:00", "09:10"]]}],
                "contested_time": [{"activity_id": "act-zero", "workstream_id": "ws-policy",
                    "requested_minutes": 15, "allocated_minutes": 0, "unallocated_minutes": 15,
                    "minimum_minutes": 8, "reason": "shared observed capacity"}]},
            "ambiguous": [{"activity_id": "act-zero", "exception_kind": "contested_time",
                "evidence_ids": self.activity["evidence_ids"], "requested_minutes": 15,
                "allocated_minutes": 0, "unallocated_minutes": 15, "reason": "shared observed capacity"}],
            "skipped": [], "correction_regression": {"schema_version": 1,
                "summary": {"pass": 0, "fail": 0, "not_applicable": 0}, "results": []},
        }
        write(self.source / "semantic-analysis.json", self.semantic)
        write(self.source / "work-accounting-result.json", self.accounting)
        write(self.source / "proposals.json", self.proposals)
        write(self.source / "review-snapshot.json", {"categories": {"new": [{
            "id": "rvi-zero", "activity_id": "act-zero", "evidence_ids": self.activity["evidence_ids"],
            "evidence_fingerprint": self.fingerprint, "description": None, "disposition": "ambiguous",
            "allocation_segments": [], "reason": "shared observed capacity"}]}})
        self.parent = self.source / "review-corrections.jsonl"
        self.parent.write_bytes(b"")
        write(self.source / "run-report.json", {
            "date_range": {"since": "2026-10-05T00:00:00Z", "until": "2026-10-06T00:00:00Z"},
            "evidence_ledger": {"source_completeness": ledger.manifest.document()["source_completeness"]},
            "runtime_identity": {"fixture": "synthetic-local-only"}})
        write(self.source / "quality_report.json", {"status": "pass"})
        self.seal()

    def seal(self):
        collector_receipts.write_completion_bundle(self.source / "completion-bundle.json",
            collector_receipts.build_completion_bundle(self.source, slice_=SimpleNamespace(
                slice_id="zero-fixture", since=dt.datetime(2026, 10, 5, tzinfo=dt.UTC),
                until=dt.datetime(2026, 10, 6, tzinfo=dt.UTC))))

    def record(self):
        return sign({"schema_version": 1, "record_type": "zero_allocation_wording",
            "review_item_id": "rvi-zero", "activity_id": "act-zero", "evidence_fingerprint": self.fingerprint,
            "prior_run_id": "source", "parent_semantic_sha256": digest(self.source / "semantic-analysis.json"),
            "parent_accounting_sha256": digest(self.source / "work-accounting-result.json"),
            "parent_proposals_sha256": digest(self.source / "proposals.json"),
            "parent_description": self.activity["rendered_description"],
            "requested_minutes": 15, "allocated_minutes": 0, "unallocated_minutes": 15,
            "field_patch": {"description": {"op": "replace", "value": NEW}},
            "reviewer": "Codex synthetic authorized editorial cleanup",
            "reviewed_at": "2026-10-08T00:00:00Z", "rationale": "Local-only wording, not an approval or posting vote"})

    def proposed(self, record=None):
        path = self.root / "proposed.jsonl"
        line = dict(record or self.record(), previous_digest=None)
        line["canonical_digest"] = corrections.canonical_digest(corrections._without_integrity(line))
        path.write_text(corrections.canonical_json(line) + "\n")
        return path

    def test_source_only_wording_loads_without_a_human_vote_or_proposal(self):
        # Rejecting or misclassifying the new record loses this local correction.
        proposed = self.proposed()
        try:
            cases = corrections.load_regression_cases(proposed)
        except corrections.ReviewDecisionError as error:
            self.fail(f"source-only zero-allocation wording must load: {error}")
        self.assertEqual([], corrections.load_decisions(proposed))
        self.assertEqual([], corrections.derive_learning_cases(corrections.load_decisions(proposed)))
        self.assertFalse(cases[0]["expected_presence"])
        self.assertEqual(NEW, pipeline._description_from_review_correction(self.activity, cases))
        self.assertEqual("not_applicable", corrections.evaluate_regression_cases(cases, [])["results"][0]["status"])
        self.assertEqual("fail", corrections.evaluate_regression_cases(cases,
            [{**self.activity, "description": NEW}])["results"][0]["status"])

    def test_sealed_zero_allocation_source_enters_append_only_repair(self):
        # Missing native admission is the original no-proposal capability gap.
        old = self.parent.read_bytes()
        proposed = self.proposed()
        try:
            run._validate_repair_credit_transition(self.source, proposed, runs_root=self.root)
        except run.ReviewRunError as error:
            self.fail(f"exact zero-allocation source correction must enter repair: {error}")
        self.assertEqual(old, self.parent.read_bytes())

    def test_nonzero_mixed_stale_and_missing_accounting_preimages_are_rejected(self):
        # Each mutation would otherwise permit financial/source drift.
        baseline = {p.name: p.read_bytes() for p in self.source.iterdir() if p.is_file()}
        for mutation in ("nonzero", "proposal", "missing_evidence", "missing_contested", "missing_ambiguity",
                         "foreign_evidence", "stale_description", "stale_semantic", "unsealed"):
            with self.subTest(mutation=mutation):
                for name, content in baseline.items():
                    (self.source / name).write_bytes(content)
                record = self.record()
                accounting = copy.deepcopy(self.accounting)
                semantic = copy.deepcopy(self.semantic)
                if mutation == "nonzero":
                    accounting["allocation"]["contested_time"][0]["allocated_minutes"] = 1
                elif mutation == "proposal":
                    accounting["proposals"].append({**self.activity, "description": NEW})
                    write(self.source / "proposals.json", accounting["proposals"])
                elif mutation == "missing_evidence":
                    accounting["allocation"]["evidence"] = []
                elif mutation == "missing_contested":
                    accounting["allocation"]["contested_time"] = []
                elif mutation == "missing_ambiguity":
                    accounting["ambiguous"] = []
                elif mutation == "foreign_evidence":
                    semantic["activities"][0]["evidence_ids"] = ["foreign"]
                elif mutation == "stale_description":
                    record["parent_description"] = "SC — Foreign rendered preimage"
                elif mutation == "stale_semantic":
                    record["parent_semantic_sha256"] = "sha256:" + "0" * 64
                if mutation in {"nonzero", "proposal", "missing_evidence", "missing_contested", "missing_ambiguity"}:
                    write(self.source / "work-accounting-result.json", accounting)
                    record["parent_accounting_sha256"] = digest(self.source / "work-accounting-result.json")
                    record["parent_proposals_sha256"] = digest(self.source / "proposals.json")
                if mutation == "foreign_evidence":
                    write(self.source / "semantic-analysis.json", semantic)
                    record["parent_semantic_sha256"] = digest(self.source / "semantic-analysis.json")
                self.seal()
                if mutation == "unsealed":
                    (self.source / "completion-bundle.json").unlink()
                with self.assertRaises(run.ReviewRunError):
                    run._validate_repair_credit_transition(self.source, self.proposed(sign(record)), runs_root=self.root)

    def test_only_clean_description_patch_is_allowed(self):
        # A broadened source-only record must not become financial authority.
        for field, value in (("duration_minutes", 15), ("client_project", "Other"), ("tag_names", []),
                             ("description", "SC — Deployed commit d63a5ed")):
            with self.subTest(field=field):
                record = self.record()
                record["field_patch"][field] = {"op": "replace", "value": value}
                with self.assertRaises(corrections.ReviewDecisionError):
                    corrections.validate_zero_allocation_wording(sign(record))

    def test_source_only_log_keeps_history_and_rejects_conflicting_votes(self):
        # New source-only records cannot rewrite or mint decision history.
        record = self.record()
        path = self.root / "child.jsonl"
        unrelated = corrections.build_decision({"id": "rvi-other", "current": self.proposals[0]},
            decision="approve", reviewer="synthetic-human", reviewed_at="2026-10-07T00:00:00Z",
            correction_categories=["wording"], rationale="Synthetic prior vote")
        corrections.append_decision(path, unrelated)
        before = path.read_bytes()
        corrections.append_zero_allocation_wording(path, record)
        self.assertTrue(path.read_bytes().startswith(before))
        self.assertEqual([unrelated["decision_id"]], [r["decision_id"] for r in corrections.load_decisions(path)])
        after = path.read_bytes()
        self.assertFalse(corrections.append_zero_allocation_wording(path, record))
        altered = copy.deepcopy(record)
        altered["field_patch"]["description"]["value"] = "SC — Reconciled a foreign policy change"
        with self.assertRaises(corrections.ReviewDecisionError):
            corrections.append_zero_allocation_wording(path, sign(altered))
        vote = corrections.build_decision({"id": "rvi-zero", "current": self.activity}, decision="approve",
            reviewer="synthetic-human", reviewed_at="2026-10-08T00:00:00Z",
            correction_categories=["wording"], rationale="Conflicting promotion")
        with self.assertRaises(corrections.ReviewDecisionError):
            corrections.append_decision(path, vote)
        self.assertEqual(after, path.read_bytes())
        prior_vote = self.root / "prior-vote.jsonl"
        corrections.append_decision(prior_vote, vote)
        prior_bytes = prior_vote.read_bytes()
        with self.assertRaises(corrections.ReviewDecisionError):
            corrections.append_zero_allocation_wording(prior_vote, record)
        self.assertEqual(prior_bytes, prior_vote.read_bytes())

    def test_output_preserves_proposals_contested_evidence_and_non_description_semantics(self):
        # Output validation must reject drift even if record admission passed.
        output = self.root / "output"
        output.mkdir()
        for name in ("semantic-analysis.json", "work-accounting-result.json", "proposals.json"):
            (output / name).write_bytes((self.source / name).read_bytes())
        semantic = copy.deepcopy(self.semantic)
        semantic["activities"][0]["rendered_description"] = NEW
        accounting = copy.deepcopy(self.accounting)
        accounting["correction_regression"] = corrections.evaluate_regression_cases(
            corrections.load_regression_cases(self.proposed()), self.proposals)
        write(output / "semantic-analysis.json", semantic)
        write(output / "work-accounting-result.json", accounting)
        run._validate_zero_allocation_wording_output(self.source, output, self.record())
        for mutation in ("allocation", "contested", "evidence", "ambiguous", "semantic_route", "semantic_summary", "description", "proposal", "regression"):
            with self.subTest(mutation=mutation):
                changed = copy.deepcopy(accounting)
                if mutation == "allocation":
                    changed["allocation"]["allocations"][0]["duration_minutes"] = 11
                elif mutation == "contested":
                    changed["allocation"]["contested_time"][0]["requested_minutes"] = 16
                elif mutation == "evidence":
                    changed["allocation"]["evidence"] = []
                elif mutation == "ambiguous":
                    changed["ambiguous"] = []
                elif mutation == "semantic_route":
                    altered_semantic = copy.deepcopy(semantic)
                    altered_semantic["activities"][0]["workstream_id"] = "foreign"
                    write(output / "semantic-analysis.json", altered_semantic)
                elif mutation == "semantic_summary":
                    changed["semantic_analysis"]["activity_count"] = 2
                elif mutation == "description":
                    altered_semantic = copy.deepcopy(semantic)
                    altered_semantic["activities"][0]["rendered_description"] = "SC — Different wording"
                    write(output / "semantic-analysis.json", altered_semantic)
                elif mutation == "proposal":
                    changed["proposals"][0]["duration_minutes"] = 11
                elif mutation == "regression":
                    changed["correction_regression"]["results"][0]["status"] = "pass"
                if mutation not in {"description", "semantic_route"}:
                    write(output / "semantic-analysis.json", semantic)
                write(output / "work-accounting-result.json", changed)
                with self.assertRaises(run.ReviewRunError):
                    run._validate_zero_allocation_wording_output(self.source, output, self.record())


if __name__ == "__main__":
    unittest.main()
