"""Routine new deliveries use frozen editorial authority, never raw fallback."""
import copy
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import clockify_review_batch_publication as prepared
from scripts import clockify_review_cycle as cycle
from scripts import clockify_sheet_publish as publisher
from scripts import review_corrections, semantic_analyzer, evidence_ledger
from scripts.autopilot_process import ChildResult
from test_review_batch_publication import source
from test_sheet_publish import StatefulGateway


class AutomaticPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run = self.root / "source-run"
        self.run.mkdir()
        self.sources = [source(1, "2026-08-01T10:00:07+03:00", "2026-08-01T10:01:18+03:00", 71),
                        source(2, "2026-08-01T10:10:00+03:00", "2026-08-01T10:11:23+03:00", 83)]
        self.descriptions = ["SC — Prepared guarded source publication safely",
                             "SC — Verified immutable review receipt binding"]
        model, revision = semantic_analyzer.CURRENT_LIVE_FLASH_ROUTE
        self.analysis = {"activities": []}
        for proposal, wording in zip(self.sources, self.descriptions):
            proposal["provenance"].update(semantic_reviewer_model=model, semantic_reviewer_revision=revision)
            self.analysis["activities"].append({"activity_id": proposal["activity_id"],
                "evidence_ids": proposal["provenance"]["evidence_ids"], "rendered_description": wording,
                "semantic_reviewer_model": model, "semantic_reviewer_revision": revision})
        self.write(self.run / "proposals.json", self.sources)
        self.write(self.run / "semantic-analysis.json", self.analysis)
        self.write(self.run / "routing.json", {"session_routes": [{"project_name": "Example",
            "project_suffix": "abc123", "prefix": "SC", "tag_names": ["Development"],
            "tag_suffixes": ["def456"], "billable": True}]})
        (self.run / "review-corrections.jsonl").write_text("")
        self.write(self.run / "work-accounting-result.json", {"schema_version": 1,
            "allocation_mode": "non_overlapping_v1", "proposals": self.sources,
            "ambiguous": [], "skipped": []})

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value))

    def prepare(self):
        function = getattr(prepared, "prepare", None)
        self.assertIsNotNone(function, "automatic editorial preparation is missing")
        return function(source_dir=self.run, output=self.root / "prepared.json", run_id="source-run",
            spreadsheet_id="sheet", sheet_title="August 2026 review", timezone="Europe/Bucharest")

    def test_untagged_reviewed_time_publishes_natively_without_invalid_portfolio_parent(self):
        # Native proposals permit no tags; portfolio parents require at least
        # one. Packaging must not turn valid time into an unpublishable parent.
        for proposal in self.sources:
            proposal.update(tag_names=[], tag_suffixes=[])
        self.write(self.run / "proposals.json", self.sources)
        self.write(self.run / "routing.json", {"session_routes": [{
            "project_name": "Example", "project_suffix": "abc123", "prefix": "SC",
            "tag_names": [], "tag_suffixes": [], "billable": True}]})
        outcome = self.prepare()
        projection = prepared.project(path=self.root / "prepared.json", source_dir=self.run,
            proposals=self.sources, run_id="source-run", spreadsheet_id="sheet",
            sheet_title="August 2026 review", project_allowlist={"abc123": "Example"})
        self.assertEqual("complete", outcome["status"])
        self.assertEqual([publisher.stable_review_id(p) for p in self.sources],
                         [row[0] for row in projection["rows"]])
        self.assertEqual([71 / 60, 83 / 60], [row[3] for row in projection["rows"]])
        self.assertEqual({}, projection["parents"])

    def test_two_distinct_reviewed_accomplishments_consolidate_without_new_inference(self):
        outcome = self.prepare()
        self.assertEqual("complete", outcome["status"])
        document = json.loads((self.root / "prepared.json").read_text())
        self.assertEqual("review-batch-publication/v2", document["schema_version"])
        self.assertEqual(1, len(document["package"]["activities"]))
        activity = document["package"]["activities"][0]
        self.assertEqual(154, activity["duration_seconds"])
        self.assertEqual(self.descriptions, [item["description"] for item in activity["prepared_descriptions"]])
        projection = prepared.project(path=self.root / "prepared.json", source_dir=self.run,
            proposals=self.sources, run_id="source-run", spreadsheet_id="sheet",
            sheet_title="August 2026 review", project_allowlist={"abc123": "Example"})
        self.assertEqual(154 / 60, projection["rows"][0][3])
        self.assertNotIn("raw source wording", projection["rows"][0])
        gateway = StatefulGateway(rows=[publisher.HEADER])
        result = publisher.publish_proposal_partitions(gateway, spreadsheet_id="sheet",
            sheet_title="August 2026 review", template_title="Proposals", proposals=self.sources,
            run_id="source-run", project_allowlist={"abc123": "Example"}, source_dir=self.run,
            review_batch_publication=self.root / "prepared.json")
        expected = cycle._expected_publication_receipts({"spreadsheet_id": "sheet",
            "review_batch_publication": str(self.root / "prepared.json")},
            {"run_dir": str(self.run), "run_id": "source-run"}, sheet_title="August 2026 review",
            publication_profile=None)
        self.assertEqual(expected[0], {key: result["publications"][0][key] for key in expected[0]})

    def test_missing_or_unsafe_reviewed_wording_is_private_and_blocks_full_preparation(self):
        for wording in (None, "SC — Bad", "SC — See https://private.example.com/secret for execution details"):
            with self.subTest(wording=wording):
                self.analysis["activities"][0]["rendered_description"] = wording
                self.write(self.run / "semantic-analysis.json", self.analysis)
                output = self.root / "prepared.json"
                # Each case gets its own immutable preparation destination.
                function = getattr(prepared, "prepare", None)
                self.assertIsNotNone(function, "automatic editorial preparation is missing")
                outcome = function(source_dir=self.run, output=self.root / f"case-{len(str(wording))}.json",
                    run_id="source-run", spreadsheet_id="sheet", sheet_title="August 2026 review",
                    timezone="Europe/Bucharest")
                self.assertEqual("incomplete", outcome["status"])
                self.assertGreater(outcome["unprepared_seconds"], 0)
                document = json.loads(Path(outcome["binding"]["path"]).read_text())
                identity = publisher.stable_review_id(self.sources[0])
                self.assertNotIn(identity, document["editorial_descriptions"])
                self.assertTrue(document["preparation_diagnostics"])

    def test_exact_local_wording_correction_is_preserved_as_standalone_not_grouped(self):
        proposal = self.sources[0]
        corrected = "SC — Preserved exact reviewer wording correction"
        proposal["description"] = corrected
        self.write(self.run / "proposals.json", self.sources)
        self.write(self.run / "work-accounting-result.json", {"schema_version": 1,
            "allocation_mode": "non_overlapping_v1", "proposals": self.sources, "ambiguous": [], "skipped": []})
        item = {"id": "rvi-corrected", "current": {"activity_id": proposal["activity_id"],
            "evidence_ids": proposal["provenance"]["evidence_ids"], "description": corrected}}
        record = review_corrections.build_decision(item, decision="modify", reviewer="human",
            reviewed_at="2026-08-01T11:00:00+03:00", correction_categories=["wording"], rationale="Exact wording",
            field_patch={"description": {"op": "replace", "value": corrected}})
        review_corrections.append_decision(self.run / "review-corrections.jsonl", record, item=item)
        outcome = self.prepare()
        self.assertEqual("complete", outcome["status"])
        document = json.loads((self.root / "prepared.json").read_text())
        identity = publisher.stable_review_id(proposal)
        self.assertIn(identity, document["protected_ids"])
        self.assertIn(identity, document["standalone_review_ids"])
        projection = prepared.project(path=self.root / "prepared.json", source_dir=self.run,
            proposals=self.sources, run_id="source-run", spreadsheet_id="sheet",
            sheet_title="August 2026 review", project_allowlist={"abc123": "Example"})
        row = next(row for row in projection["rows"] if row[0] == identity)
        self.assertEqual(corrected, row[8])

    def test_immutable_retry_reuses_binding_and_authority_drift_is_rejected(self):
        first = self.prepare()
        path = self.root / "prepared.json"
        before = (path.read_bytes(), path.stat().st_mtime_ns)
        self.assertEqual(first, self.prepare())
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))
        self.analysis["activities"][0]["rendered_description"] = self.descriptions[1]
        self.write(self.run / "semantic-analysis.json", self.analysis)
        with self.assertRaises(ValueError):
            self.prepare()

    def test_unapproved_reviewer_or_mismatched_event_fingerprint_cannot_authorize_raw_words(self):
        self.analysis["activities"][0]["semantic_reviewer_revision"] = "unapproved"
        self.analysis["activities"][1]["evidence_ids"] = ["different-event"]
        self.write(self.run / "semantic-analysis.json", self.analysis)
        outcome = self.prepare()
        self.assertEqual("incomplete", outcome["status"])
        self.assertEqual(154, outcome["unprepared_seconds"])
        document = json.loads((self.root / "prepared.json").read_text())
        self.assertEqual({}, document["editorial_descriptions"])
        self.assertEqual([], document["package"]["activities"])
        self.assertEqual(2, len(document["package"]["retained"]))

    def test_existing_approved_source_stays_protected_not_withheld_new_time(self):
        self.sources[0]["disposition"] = "approved"
        self.write(self.run / "proposals.json", self.sources)
        outcome = self.prepare()
        self.assertEqual("complete", outcome["status"])
        self.assertEqual(0, outcome["unprepared_seconds"])
        self.assertEqual(71, outcome["preserved_seconds"])
        self.assertEqual(83, outcome["prepared_seconds"])
        projection = prepared.project(path=self.root / "prepared.json", source_dir=self.run,
            proposals=self.sources, run_id="source-run", spreadsheet_id="sheet",
            sheet_title="August 2026 review", project_allowlist={"abc123": "Example"})
        self.assertEqual(1, len(projection["rows"]))
        self.assertNotIn(publisher.stable_review_id(self.sources[0]), projection["parents"])

    def test_v2_asserted_editorial_map_cannot_override_its_frozen_authority(self):
        self.prepare()
        path = self.root / "prepared.json"
        document = json.loads(path.read_text())
        identity = publisher.stable_review_id(self.sources[0])
        document["editorial_descriptions"][identity] = "SC — Invented different source accomplishment entirely"
        self.write(path, document)
        with self.assertRaises(ValueError):
            prepared.load(path)

    def test_editorial_authority_cannot_authorize_different_tag_or_billable_route(self):
        self.sources[0]["tag_suffixes"] = ["different-tag"]
        self.sources[1]["billable"] = False
        self.write(self.run / "proposals.json", self.sources)
        outcome = self.prepare()
        self.assertEqual("incomplete", outcome["status"])
        self.assertEqual(154, outcome["unprepared_seconds"])
        document = json.loads((self.root / "prepared.json").read_text())
        self.assertEqual({}, document["editorial_descriptions"])

    def test_new_delivery_automatically_uses_one_effective_prepared_config_and_blocks_unsafe(self):
        helper = getattr(cycle, "_prepare_new_publication", None)
        self.assertIsNotNone(helper, "routine new-delivery preparation seam is missing")
        config = {"spreadsheet_id": "sheet", "timezone": "Europe/Bucharest"}
        source_record = {"run_dir": str(self.run), "run_id": "source-run"}
        effective, outcome = helper(config, source_record, sheet_title="August 2026 review", state_dir=self.root / "state")
        self.assertEqual("complete", outcome["status"])
        self.assertIn("review_batch_publication", effective)
        self.assertNotIn("review_batch_publication", config)
        receipts = cycle._expected_publication_receipts(effective, source_record,
            sheet_title="August 2026 review", publication_profile=None)
        self.assertEqual(1, len(receipts[0]["row_ids"]))
        self.assertEqual(outcome["binding"], receipts[0]["review_batches"]["binding"])
        self.analysis["activities"][0]["rendered_description"] = None
        self.write(self.run / "semantic-analysis.json", self.analysis)
        # A different state destination permits an independent source test;
        # attempting to mutate the existing bound retry would correctly fail.
        effective, outcome = helper(config, source_record, sheet_title="August 2026 review", state_dir=self.root / "other-state")
        self.assertEqual("incomplete", outcome["status"])
        self.assertEqual(71, outcome["unprepared_seconds"])

    def test_conflicting_projection_is_an_explicit_gap_not_a_raw_publication_fallback(self):
        helper = getattr(cycle, "_prepare_new_publication", None)
        self.assertIsNotNone(helper, "routine new-delivery preparation seam is missing")
        from scripts import clockify_publication_presentation as display
        presentation = self.root / "presentation.json"
        self.write(presentation, {"schema_version": display.SCHEMA, "spreadsheet_id": "sheet",
            "source_run_id": "source-run", "source_artifacts": {name: {"path": str(self.run / name), "sha256": "sha256:" + "a" * 64}
                for name in display.SOURCE_FILES}, "destinations": {"August 2026 review": {}}})
        effective, outcome = helper({"spreadsheet_id": "sheet", "timezone": "Europe/Bucharest",
                "publication_presentation": str(presentation)},
            {"run_dir": str(self.run), "run_id": "source-run"},
            sheet_title="August 2026 review", state_dir=self.root / "state")
        self.assertEqual("incomplete", outcome["status"])
        self.assertEqual("prepared_projection_integration_gap", outcome["reason"])
        self.assertEqual(["publication_presentation"], outcome["exclusions"])
        self.assertNotIn("review_batch_publication", effective)

    def test_irrelevant_old_month_projection_does_not_block_or_leak_into_new_preparation(self):
        from scripts import clockify_meeting_publication_alias as meeting, clockify_publication_presentation as display
        old_meeting = self.root / "old-meeting.json"
        self.write(old_meeting, {"schema_version": meeting.SCHEMA, "spreadsheet_id": "sheet",
            "sheet_title": "July 2026 review", "bindings": [{}]})
        old_presentation = self.root / "old-presentation.json"
        self.write(old_presentation, {"schema_version": display.SCHEMA, "spreadsheet_id": "sheet",
            "source_run_id": "old-source-run", "source_artifacts": {name: {"path": str(self.root / "old-source-run" / name),
                "sha256": "sha256:" + "a" * 64} for name in display.SOURCE_FILES}, "destinations": {"July 2026 review": {}}})
        old_pending = self.root / "old-pending.json"
        self.write(old_pending, {"schema_version": "pending-review-selection/v1", "spreadsheet_id": "sheet",
            "sheet_title": "July 2026 review", "current_source": "old", "sources": {"old": {"run_id": "old-source-run",
                "artifacts": {"proposals": {"path": str(self.root / "old-source-run" / "proposals.json")}}}}})
        effective, outcome = cycle._prepare_new_publication({"spreadsheet_id": "sheet", "timezone": "Europe/Bucharest",
            "meeting_publication_bindings": str(old_meeting), "publication_presentation": str(old_presentation),
            "pending_review_selection": str(old_pending)}, {"run_dir": str(self.run), "run_id": "source-run"},
            sheet_title="August 2026 review", state_dir=self.root / "state")
        self.assertEqual("complete", outcome["status"])
        for field in ("meeting_publication_bindings", "publication_presentation", "pending_review_selection"):
            self.assertNotIn(field, effective)

    def run_slice(self, *, child, real_monthly=False):
        """Exercise orchestration after its existing verified source/replay gates."""
        state_dir = self.root / "cycle-state"
        state_dir.mkdir(exist_ok=True, mode=0o700)
        snapshots = dict.fromkeys(("period-manifest.json", "routing.json", "review-corrections.jsonl", "review-acceptance.jsonl"),
                                 "sha256:" + "a" * 64)
        source_record = {"run_dir": str(self.run), "run_id": "source-run", "result_path": str(self.run / "result.json"),
            "snapshot_digests": snapshots, "coverage": {"status": "complete", "incomplete_sources": []},
            "exception_ids": [], "review_ids": [publisher.stable_review_id(p) for p in self.sources],
            "artifact_digests": {}, **dict.fromkeys(("result_digest", "bundle_digest", "proposals_digest",
                "accounting_digest", "quality_digest"), "sha256:" + "b" * 64)}
        replay_record = {**source_record, "run_id": "replay-run", "run_dir": str(self.root / "replay-run"),
            "replay_integrity_digest": "sha256:" + "c" * 64}
        state_path = state_dir / "state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {"slices": {"2026-08-01": {
            "source": source_record, "replay": replay_record, "expected_snapshot_digests": snapshots}}}
        config = {"root": str(self.root), "state_dir": str(state_dir), "runs_dir": str(self.root / "runs"),
            "spreadsheet_id": "sheet", "timezone": "Europe/Bucharest",
            "monthly_sheet_title_template": "{month_name} {year} portfolio review" if real_monthly else "{month_name} {year} review"}
        with ExitStack() as patches:
            patches.enter_context(mock.patch.object(cycle, "_ensure_period", return_value=state_dir / "period.json"))
            patches.enter_context(mock.patch.object(cycle, "_expected_snapshot_digests", return_value=snapshots))
            patches.enter_context(mock.patch.object(cycle, "_stage_from_state",
                side_effect=lambda _config, _record, kind, *_args, **_kwargs: source_record if kind == "source" else replay_record))
            patches.enter_context(mock.patch.object(cycle, "_validate_stage",
                side_effect=lambda *_args, **kwargs: replay_record if kwargs["replay"] else source_record))
            if not real_monthly:
                patches.enter_context(mock.patch.object(cycle.clockify_monthly_unresolved, "project_rows", return_value=[]))
            patches.enter_context(mock.patch.object(cycle, "_run_budgeted_child", side_effect=child))
            result = cycle._run_slice(config, state, state_path, state_dir, self.root, "2026-08-01", "2026-08-02",
                cycle.source_coverage.SourceDebtStore(), state_dir / "debt.json", advance_frontier=True, budget=[1000])
        return result, state, state_dir

    def publication_child(self, gateway, *, real_monthly=False):
        def child(command, **_kwargs):
            binding = Path(command[command.index("--review-batch-publication") + 1])
            result = publisher.publish_proposal_partitions(gateway, spreadsheet_id="sheet",
                sheet_title="August 2026 portfolio review" if real_monthly else "August 2026 review", template_title="Proposals", proposals=self.sources,
                run_id="source-run", project_allowlist={"abc123": "Example"}, source_dir=self.run,
                review_batch_publication=binding,
                monthly_rows=cycle.clockify_monthly_unresolved.project_rows(self.run) if real_monthly else None,
                monthly_source_dir=self.run if real_monthly else None)
            output = Path(command[command.index("--result-output") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            self.write(output, {"status": "published", "external_writes": True, **result})
            return ChildResult(0, str(output) + "\n", "", False, 0.1)
        return child

    def test_real_new_slice_prepares_before_publishing_and_historical_retry_never_prepares(self):
        gateway = StatefulGateway(rows=[publisher.HEADER])
        outcome, state, state_dir = self.run_slice(child=self.publication_child(gateway))
        self.assertEqual("delivered", outcome["status"])
        self.assertEqual(2, len(gateway.rows))
        receipt = state_dir / "delivery-receipts" / "2026-08-01.json"
        self.assertTrue(receipt.exists())
        binding = state["slices"]["2026-08-01"]["review_preparation"]["binding"]
        self.assertEqual(binding, json.loads(receipt.read_text())["publication_receipts"][0]["review_batches"]["binding"])
        with mock.patch.object(prepared, "prepare", side_effect=AssertionError("historical reprojection")):
            repeated, _state, _directory = self.run_slice(child=lambda *_args, **_kwargs: self.fail("historical child launch"))
        self.assertEqual("delivered", repeated["status"])
        self.assertEqual(2, len(gateway.rows))

    def test_real_partial_slice_publishes_clean_subset_keeps_unsafe_private_and_retries_read_only(self):
        self.analysis["activities"][0]["rendered_description"] = "SC — Bad"
        self.write(self.run / "semantic-analysis.json", self.analysis)
        gateway = StatefulGateway(rows=[publisher.HEADER])
        outcome, state, state_dir = self.run_slice(child=self.publication_child(gateway))
        self.assertEqual("published_with_preparation_gaps", outcome["status"])
        self.assertFalse(outcome["advance_frontier"])
        self.assertEqual(2, len(gateway.rows))
        self.assertEqual(83 / 60, gateway.rows[1][3])
        self.assertEqual(self.descriptions[1], gateway.rows[1][8])
        cycle._recompute_completed_through({"recovery_since": "2026-08-01"}, state)
        self.assertIsNone(state["completed_through"])
        self.assertFalse((state_dir / "delivery-receipts" / "2026-08-01.json").exists())
        receipt = state_dir / "partial-publication-receipts" / "2026-08-01-source-run.json"
        document = json.loads(receipt.read_text())
        self.assertEqual(cycle.PARTIAL_RECEIPT_SCHEMA_VERSION, document["schema_version"])
        self.assertNotIn("source_completeness", document)  # Collector completeness is still genuinely complete.
        self.assertEqual(71, document["review_preparation"]["outcome"]["unprepared_seconds"])
        self.assertEqual(71, state["slices"]["2026-08-01"]["review_preparation"]["unprepared_seconds"])
        with mock.patch.object(prepared, "prepare", side_effect=AssertionError("partial historical reprojection")):
            repeated, _state, _directory = self.run_slice(child=lambda *_args, **_kwargs: self.fail("partial duplicate child"))
        self.assertEqual("published_with_preparation_gaps", repeated["status"])
        self.assertEqual(2, len(gateway.rows))

    def test_malformed_native_identity_stays_private_without_suppressing_ready_peer(self):
        self.sources[0]["review_activity_key"] = "wka-unresolved"
        self.analysis["activities"][0]["rendered_description"] = None
        original = copy.deepcopy(self.sources[0])
        self.write(self.run / "semantic-analysis.json", self.analysis)
        self.write(self.run / "proposals.json", self.sources)
        self.write(self.run / "work-accounting-result.json", {"schema_version": 1, "allocation_mode": "non_overlapping_v1",
            "proposals": self.sources, "ambiguous": [], "skipped": []})
        gateway = StatefulGateway(rows=[publisher.HEADER])
        outcome, state, _state_dir = self.run_slice(child=self.publication_child(gateway))
        self.assertEqual("published_with_preparation_gaps", outcome["status"])
        self.assertFalse(outcome["advance_frontier"])
        self.assertEqual(2, len(gateway.rows))
        self.assertEqual(83 / 60, gateway.rows[1][3])
        self.assertNotIn("wka-unresolved-s01", [row[0] for row in gateway.rows])
        preparation = state["slices"]["2026-08-01"]["review_preparation"]
        document = json.loads(Path(preparation["binding"]["path"]).read_text())
        self.assertEqual(original, document["package"]["retained"][0]["proposal"])
        self.assertEqual(71, preparation["unprepared_seconds"])
        self.assertEqual(["wka-unresolved-s01"], [item["review_id"] for item in document["preparation_diagnostics"]])

    def test_duplicate_source_identity_copies_stay_retained_while_distinct_peer_is_ready(self):
        duplicate = copy.deepcopy(self.sources[0])
        duplicate.update(start="2026-08-01T10:03:00+03:00", end="2026-08-01T10:04:00+03:00",
                         duration_seconds=60, duration_minutes=1)
        self.sources.insert(1, duplicate)
        self.write(self.run / "proposals.json", self.sources)
        outcome = self.prepare()
        self.assertEqual("incomplete", outcome["status"])
        self.assertEqual(83, outcome["prepared_seconds"])
        self.assertEqual(131, outcome["unprepared_seconds"])
        document = json.loads((self.root / "prepared.json").read_text())
        self.assertCountEqual(self.sources[:2], [row["proposal"] for row in document["package"]["retained"]])
        projection = prepared.project(path=self.root / "prepared.json", source_dir=self.run, proposals=self.sources,
            run_id="source-run", spreadsheet_id="sheet", sheet_title="August 2026 review",
            project_allowlist={"abc123": "Example"})
        self.assertEqual(1, len(projection["rows"]))
        self.assertEqual(83 / 60, projection["rows"][0][3])
        self.assertNotIn(publisher.stable_review_id(duplicate), projection["parents"])

    def test_all_unsafe_no_ready_primary_or_monthly_rows_do_not_publish(self):
        for activity in self.analysis["activities"]:
            activity["rendered_description"] = "SC — Bad"
        self.write(self.run / "semantic-analysis.json", self.analysis)
        outcome, state, state_dir = self.run_slice(child=lambda *_args, **_kwargs: self.fail("all-unsafe child"))
        self.assertEqual("incomplete", outcome["status"])
        self.assertFalse(outcome["advance_frontier"])
        self.assertFalse((state_dir / "partial-publication-receipts" / "2026-08-01-source-run.json").exists())
        self.assertEqual(154, state["slices"]["2026-08-01"]["review_preparation"]["unprepared_seconds"])

    def test_unresolved_routing_remains_native_monthly_visible_without_a_primary_row(self):
        from test_monthly_unresolved import MonthlyGateway
        from scripts import clockify_monthly_unresolved as monthly
        events = []
        for index, proposal in enumerate(self.sources):
            event = evidence_ledger.evidence_event("codex_session", {"source_id": f"session-{index}", "machine": "test"},
                observed_at=proposal["start"], raw_source_span={"start": proposal["start"], "end": proposal["end"]},
                attributes={"title": "Private title should not be exported"})
            events.append(event)
            proposal["provenance"]["evidence_ids"] = [event.evidence_id]
            self.analysis["activities"][index]["evidence_ids"] = [event.evidence_id]
        self.sources[0].update(client_project="", clockify_project_suffix="", tag_names=[], tag_suffixes=[], billable=False,
            routing_disposition="unresolved-routing", review_warnings=[{"type": "unresolved_routing",
                "disposition": "unresolved-routing", "reason_code": "no_deterministic_route"}])
        self.analysis["activities"][1]["rendered_description"] = "SC — Bad"
        ledger = evidence_ledger.EvidenceLedger(tuple(events), timezone="Europe/Bucharest")
        (self.run / "evidence").mkdir()
        self.write(self.run / "evidence/evidence-ledger.json", {"schema_version": "evidence-ledger/v1",
            "manifest": ledger.manifest.document(), "events": [event.document() for event in events]})
        self.write(self.run / "run-report.json", {"date_range": {"since": "2026-07-31T21:00:00Z", "until": "2026-08-01T21:00:00Z"}})
        self.write(self.run / "ambiguous.json", [])
        self.write(self.run / "semantic-analysis.json", self.analysis)
        self.write(self.run / "proposals.json", self.sources)
        self.write(self.run / "work-accounting-result.json", {"schema_version": 1, "allocation_mode": "non_overlapping_v1",
            "proposals": self.sources, "ambiguous": [], "skipped": []})
        class AugustMonthlyGateway(MonthlyGateway):
            def spreadsheet(self, _id):
                return {"sheets": [{"properties": {"sheetId": 7, "title": "August 2026 unresolved evidence",
                    "gridProperties": {"rowCount": 1000, "columnCount": 12}}}]}
        gateway = AugustMonthlyGateway([monthly.HEADER, ["uev-exemplar"] + [""] * 11])
        result, state, state_dir = self.run_slice(child=self.publication_child(gateway, real_monthly=True), real_monthly=True)
        self.assertEqual("published_with_preparation_gaps", result["status"])
        self.assertFalse(result["advance_frontier"])
        self.assertEqual(3, len(gateway.rows))
        self.assertEqual("routing_gap", gateway.rows[-1][3])
        self.assertNotIn("raw source wording", str(gateway.rows))
        self.assertNotIn("Private title", str(gateway.rows))
        receipt = state_dir / "partial-publication-receipts" / "2026-08-01-source-run.json"
        document = json.loads(receipt.read_text())
        self.assertEqual(0, document["review_preparation"]["outcome"]["prepared_seconds"])
        self.assertEqual(["August 2026 unresolved evidence"], [item["sheet_title"] for item in document["publication_receipts"]])
        with mock.patch.object(prepared, "prepare", side_effect=AssertionError("monthly partial reprojection")):
            repeated, _state, _directory = self.run_slice(child=lambda *_args, **_kwargs: self.fail("monthly duplicate child"), real_monthly=True)
        self.assertEqual("published_with_preparation_gaps", repeated["status"])
        self.assertEqual(3, len(gateway.rows))

    def test_selected_preparation_partial_cannot_bypass_predecessor_history_guard(self):
        prior = {"status": "published_with_preparation_gaps", "until": "2026-08-02",
                 "delivery_receipt": "/unrelated/full-delivery.json"}
        with self.assertRaises(cycle.CycleError):
            cycle._verify_selected_partial_predecessor({}, prior, "2026-08-01", "2026-08-02")

    def test_partial_state_validation_binds_debt_status_to_exact_receipt_proof(self):
        self.analysis["activities"][0]["rendered_description"] = "SC — Bad"
        self.write(self.run / "semantic-analysis.json", self.analysis)
        gateway = StatefulGateway(rows=[publisher.HEADER])
        _outcome, state, state_dir = self.run_slice(child=self.publication_child(gateway))
        for path in cycle._period_paths(state_dir, "2026-08-01"):
            path.write_text("{}")
        config = {"root": str(self.root), "state_dir": str(state_dir), "spreadsheet_id": "sheet",
            "monthly_sheet_title_template": "{month_name} {year} review"}
        record = state["slices"]["2026-08-01"]
        with mock.patch.object(cycle, "_ensure_period", return_value=Path(record["period_manifest"])), \
                mock.patch.object(cycle, "_stage_from_state", side_effect=lambda _config, stored, key, *_args, **_kwargs: stored[key]), \
                mock.patch.object(cycle.clockify_monthly_unresolved, "project_rows", return_value=[]):
            cycle._validate_delivered_state(config, state)
            corrupted = copy.deepcopy(state)
            corrupted["slices"]["2026-08-01"]["review_preparation"]["unprepared_seconds"] = 0
            with self.assertRaises(cycle.CycleError):
                cycle._validate_delivered_state(config, corrupted)
            fake_complete = copy.deepcopy(state)
            fake_complete["slices"]["2026-08-01"].update(status="delivered", delivery_receipt=record["publication_receipt"])
            with self.assertRaises(cycle.CycleError):
                cycle._validate_delivered_state(config, fake_complete)

    def test_later_complete_slice_does_not_hide_earlier_preparation_debt_in_cycle_result(self):
        state_dir = self.root / "summary-state"
        config = {"root": str(self.root), "runs_dir": str(self.root / "summary-runs"), "state_dir": str(state_dir),
            "timezone": "Europe/Bucharest", "recovery_since": "2026-08-01", "max_slices": 2}
        work = [("2026-08-01", "2026-08-02", "routine", None), ("2026-08-02", "2026-08-03", "routine", None)]
        def verified_slice(_config, state, _path, _state_dir, _root, since, until, *_args, **_kwargs):
            status = "published_with_preparation_gaps" if since == "2026-08-01" else "delivered"
            state["slices"][since] = {"status": status, "until": until}
            return {"status": status, "slice": {"since": since, "until": until}, "advance_frontier": status == "delivered"}
        with mock.patch.object(cycle, "_select_work", return_value=work), mock.patch.object(cycle, "_run_slice", side_effect=verified_slice):
            result = cycle.run_cycle(config, enable_sheet_write=True)
        self.assertEqual("published_with_preparation_gaps", result["status"])
        state = json.loads((state_dir / "review-cycle-state.json").read_text())
        self.assertIsNone(state["completed_through"])


if __name__ == "__main__":
    unittest.main()
