"""Offline sealed route repairs must preserve genuine canonical attendance facts."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_review_run as fixtures
import test_work_accounting_pipeline as evidence
from test_review_run_chained_repair_replay import validate_repair
from scripts import collector_receipts, review_corrections
from scripts import work_accounting_pipeline as accounting

run = fixtures.review_run


class RecordedAttendanceRepairTests(unittest.TestCase):
    def source(self, root):
        source = fixtures.ReviewRunResultTests._write_real_offline_replay_source(root / "runs", root)
        original_bundle = collector_receipts.load_completion_bundle(source / "completion-bundle.json", run_dir=source)
        events = []
        for identity, title, start, end in (
            ("internal-review", "Agency planning review", "2026-08-01T07:11:28+00:00", "2026-08-01T08:09:55+00:00"),
            ("referral-review", "Discovery Call - Referral contact", "2026-08-01T07:45:00+00:00", "2026-08-01T09:10:15+00:00"),
        ):
            base = evidence.fathom_event(start, end, "available")
            events.append(evidence.evidence_ledger.evidence_event(
                "fathom", {"source_type": "fathom", "source_id": identity},
                observed_at=start, raw_source_span=base.raw_source_span,
                attributes={**base.attributes, "title": title,
                            "transcript": [{"text": "Source-proven meeting attendance."}]},
            ))
        events.append(evidence.clockify_event("2026-08-01T07:50:00+00:00", "2026-08-01T08:00:00+00:00"))
        ledger = evidence.evidence_ledger.EvidenceLedger(tuple(events), {
            "clockify": {"status": "complete"}, "fathom": {"status": "complete"},
            "multica_issues": {"status": "complete"},
        })
        fixtures.write_json(source / "evidence/evidence-ledger.json", {
            "schema_version": ledger.manifest.schema_version,
            "manifest": ledger.manifest.document(), "events": [event.document() for event in events],
        })
        fixtures.write_json(source / "routing.json", fixtures.review_run.clockify_sync_collect.load_json(fixtures.ROOT / "routing.json"))
        analysis = root / "attendance-analysis.json"
        fixtures.write_json(analysis, {"activities": [], "exceptions": [{
            "kind": "semantic_review_failure", "reason": "Synthetic offline unavailable review",
            "evidence_ids": [event.evidence_id for event in events[:2]],
        }], "omissions": [],
            "analysis_chunks": [{"analyzer_model": "offline-fixture", "analyzer_tier": "fixture"}],
            "evidence_bundle_schema_version": "clockify-semantic-evidence-bundle/v1",
            "evidence_bundle_manifest": fixtures.bundle_manifest(),
        })
        accounting.run_accounting(source, root=fixtures.ROOT, routing_path=source / "routing.json",
                                 corrections_path=source / "review-corrections.jsonl", analysis_fixture=analysis)
        validate_repair(source, root / "runs", root / "source-items.json")
        slice_ = fixtures.argparse.Namespace(slice_id=original_bundle.slice_id,
            since=fixtures.dt.datetime(2026, 8, 1, tzinfo=fixtures.dt.UTC),
            until=fixtures.dt.datetime(2026, 8, 2, tzinfo=fixtures.dt.UTC))
        collector_receipts.write_completion_bundle(source / "completion-bundle.json",
            collector_receipts.build_completion_bundle(source, slice_=slice_))
        manifest = json.loads((source / "period-manifest.json").read_text())
        manifest["artifacts"][0]["digest"] = "sha256:" + hashlib.sha256((source / "completion-bundle.json").read_bytes()).hexdigest()
        manifest.pop("manifest_digest")
        manifest["manifest_digest"] = "sha256:" + hashlib.sha256(json.dumps(
            manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        fixtures.write_json(source / "period-manifest.json", manifest)
        proposals = json.loads((source / "proposals.json").read_text())
        self.assertEqual([3507, 5115], [row["duration_seconds"] for row in proposals])
        snapshot_items = [item for items in json.loads((source / "review-snapshot.json").read_text())["categories"].values() for item in items]
        corrections = root / "selected-routes.jsonl"
        for index, (proposal, tags) in enumerate(zip(proposals, (["Project Management"], ["Business development"]))):
            item_id = next(item["id"] for item in snapshot_items if review_corrections.proposal_target(item) == review_corrections.proposal_target(proposal))
            item = {"id": item_id, "current": proposal}
            record = review_corrections.build_decision(item, decision="modify", reviewer="offline-test-only",
                reviewed_at="2026-10-07T12:00:00Z", correction_categories=["routing"],
                rationale="Synthetic local fixture, not a human authorization receipt.", field_patch={
                    "client_project": {"op": "replace", "value": "Serenichron Level 1"},
                    "tag_names": {"op": "replace", "value": tags},
                })
            review_corrections.append_decision(corrections, record, item=item)
        return source, corrections, proposals

    def test_exact_empty_semantic_fallback_repair_accounting_and_sealed_replay(self):
        # Rejecting zero semantic matches must prevent this genuine sealed repair from completing.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, corrections, originals = self.source(root)
            before = fixtures.run_tree_snapshot(source)
            self.assertEqual([], json.loads((source / "semantic-analysis.json").read_text())["activities"])
            with mock.patch.object(run, "RUNS", root / "runs"):
                try:
                    repair = run._prepare_repair_run(source, corrections_override=corrections)
                except run.ReviewRunError as exc:
                    self.fail(f"genuine sealed recorded attendance route rejected: {exc}")
                result = accounting.run_accounting(repair, root=fixtures.ROOT,
                    routing_path=repair / "routing.json", corrections_path=repair / "review-corrections.jsonl",
                    analysis_fixture=run._repair_analysis_fixture(repair))
                self.assertEqual(2, len(result["proposals"]))
                self.assertEqual(0, result["correction_regression"]["summary"]["fail"])
                for original, row, tags in zip(originals, result["proposals"], (["Project Management"], ["Business development"])):
                    self.assertEqual("Serenichron Level 1", row["client_project"])
                    self.assertEqual(tags, row["tag_names"])
                    self.assertEqual("31b39a", row["clockify_project_suffix"])
                    self.assertEqual(["35aa9aef"] if tags == ["Project Management"] else ["35aa9b46"], row["tag_suffixes"])
                    for key in ("activity_id", "candidate_key", "start", "end", "duration_seconds", "duration_minutes", "provenance", "source", "effort"):
                        self.assertEqual(original[key], row[key], key)
                    warnings = [warning for warning in original["review_warnings"] if warning["type"] != "unresolved_routing"]
                    # Counterpart route metadata follows the other repaired proposal;
                    # overlap identities, bounds, seconds and reasons must not change.
                    def overlap_facts(values):
                        return [{key: value for key, value in warning.items() if key != "counterpart_project_suffix"}
                                for warning in values]
                    self.assertEqual(overlap_facts(warnings), overlap_facts(row["review_warnings"]))
                    self.assertTrue({"existing_clockify_overlap", "review_proposal_overlap", "semantic_meeting_fallback"} <= {w["type"] for w in warnings})
                validate_repair(repair, root / "runs", root / "repair-items.json")
                run._finalize_repair_completion(repair)
                output = io.StringIO()
                with mock.patch.dict(os.environ, {"CLOCKIFY_ANALYZER_PRIMARY_URL": "", "CLOCKIFY_ANALYZER_FALLBACK_URL": ""}), \
                     mock.patch.object(run, "_sealed_replay_transport", side_effect=AssertionError("network forbidden")), \
                     contextlib.redirect_stdout(output):
                    code = run.main(["--replay-from", str(repair), "--runs-root", str(root / "runs"), "--state", str(root / "replay-items.json")])
                replay = next((root / "runs").glob(f"*-replay-{repair.name}*"))
                self.assertEqual(0, code, output.getvalue() + (replay / "autopilot-result.json").read_text())
                self.assertEqual("pass", json.loads((replay / "replay-integrity.json").read_text())["status"])
                self.assertEqual((repair / "work-accounting-result.json").read_bytes(), (replay / "work-accounting-result.json").read_bytes())
                self.assertEqual([], json.loads((repair / "semantic-analysis.json").read_text())["activities"])
            self.assertEqual(before, fixtures.run_tree_snapshot(source))

    def test_fallback_rejects_unknown_stale_financial_timing_and_duplicate_decisions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, corrections, proposals = self.source(root)
            records = review_corrections._read_log(corrections)
            for name, variant in (
                ("unknown", {**records[0], "activity_id": "unknown"}),
                ("stale", {**records[0], "evidence_fingerprint": "evfp:sha256:" + "0" * 64}),
                ("unknown review item", {**records[0], "review_item_id": "rvi-unrelated"}),
                ("fallback wording", {**records[0], "correction_categories": ["routing", "wording"],
                    "field_patch": {**records[0]["field_patch"], "description": {"op": "replace", "value": "SC — Invented semantic outcome"}}}),
                *[(field, {**records[0], "field_patch": {**records[0]["field_patch"], field: {"op": "replace", "value": value}}})
                  for field, value in (("billable", False), ("duration_seconds", 1), ("start", "2026-08-01T08:00:00Z"))],
            ):
                with self.subTest(name=name):
                    variant = {key: value for key, value in variant.items() if key not in {"canonical_digest", "previous_digest", "decision_id"}}
                    variant["decision_id"] = "rdec-" + review_corrections.canonical_digest(variant)[7:31]
                    corrections.write_text("")
                    review_corrections.append_decision(corrections, variant)
                    with self.assertRaises(run.ReviewRunError):
                        run._validate_repair_credit_transition(source, corrections, runs_root=root / "runs")
            corrections.write_text("")
            for index in range(2):
                item = {"id": f"rvi-duplicate-{index}", "current": proposals[0]}
                record = review_corrections.build_decision(item, decision="modify", reviewer="offline-test-only",
                    reviewed_at="2026-10-07T12:00:00Z", correction_categories=["routing"],
                    rationale="Synthetic duplicate rejection", field_patch=records[0]["field_patch"])
                review_corrections.append_decision(corrections, record, item=item)
            with self.assertRaises(run.ReviewRunError):
                run._validate_repair_credit_transition(source, corrections, runs_root=root / "runs")

    def test_fallback_requires_unique_genuine_canonical_source_and_full_unchanged_span(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, corrections, originals = self.source(root)
            paths = {name: source / name for name in ("proposals.json", "semantic-analysis.json", "fathom-reconciliation.json", "evidence/evidence-ledger.json", "completion-bundle.json", "work-accounting-result.json")}
            baseline = {name: path.read_bytes() for name, path in paths.items()}
            mutations = [
                ("duplicate proposal", "proposals.json", [*originals, originals[0]]),
                ("not fallback", "proposals.json", [{**originals[0], "provenance": {**originals[0]["provenance"], "semantic_fallback": False}}, originals[1]]),
                ("canonical mismatch", "proposals.json", [{**originals[0], "provenance": {**originals[0]["provenance"], "canonical_meeting_id": "cm-" + "0" * 64}}, originals[1]]),
                ("changed seconds", "proposals.json", [{**originals[0], "duration_seconds": 3506}, originals[1]]),
                ("changed span", "proposals.json", [{**originals[0], "start": "2026-08-01T07:11:29+00:00"}, originals[1]]),
                ("absent reconciliation", "fathom-reconciliation.json", []),
            ]
            reconciliation = json.loads(baseline["fathom-reconciliation.json"])
            mutations.append(("duplicate canonical entry", "fathom-reconciliation.json", reconciliation * 2))
            analysis = json.loads(baseline["semantic-analysis.json"])
            activity = {"activity_id": originals[0]["activity_id"], "evidence_ids": originals[0]["provenance"]["evidence_ids"]}
            mutations.append(("duplicate semantic", "semantic-analysis.json", {**analysis, "activities": [activity, activity]}))
            for name, filename, value in mutations:
                with self.subTest(name=name):
                    fixtures.write_json(paths[filename], value)
                    # Even a newly sealed corrupted proposal must not escape
                    # independent canonical source rederivation.
                    if filename == "proposals.json":
                        result = json.loads(baseline["work-accounting-result.json"])
                        result["proposals"] = value
                        fixtures.write_json(paths["work-accounting-result.json"], result)
                        slice_ = fixtures.argparse.Namespace(
                            slice_id=json.loads(baseline["completion-bundle.json"])["slice_id"],
                            since=fixtures.dt.datetime(2026, 8, 1, tzinfo=fixtures.dt.UTC),
                            until=fixtures.dt.datetime(2026, 8, 2, tzinfo=fixtures.dt.UTC))
                        collector_receipts.write_completion_bundle(paths["completion-bundle.json"],
                            collector_receipts.build_completion_bundle(source, slice_=slice_))
                    with self.assertRaises(run.ReviewRunError):
                        run._validate_repair_credit_transition(source, corrections, runs_root=root / "runs")
                    paths[filename].write_bytes(baseline[filename])
                    paths["work-accounting-result.json"].write_bytes(baseline["work-accounting-result.json"])
                    paths["completion-bundle.json"].write_bytes(baseline["completion-bundle.json"])
            paths["completion-bundle.json"].unlink()
            with self.assertRaises(run.ReviewRunError):
                run._validate_repair_credit_transition(source, corrections, runs_root=root / "runs")


if __name__ == "__main__":
    unittest.main()
