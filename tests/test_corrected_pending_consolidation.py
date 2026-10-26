"""Materialized corrections grant presentation eligibility, never new edits."""
import copy
import json
from pathlib import Path
import unittest

from scripts import clockify_review_batch_publication as prepared
from scripts import clockify_review_batches as batches
from scripts import clockify_sheet_publish as publisher
from scripts import review_corrections
from scripts import clockify_review_cycle as cycle
from scripts import clockify_native_sheet_post as native
import test_review_batch_auto_preparation as auto_tests
from test_clockify_native_sheet_post import capture


class CorrectedPendingConsolidationTests(unittest.TestCase):
    setUp = auto_tests.AutomaticPreparationTests.setUp
    write = staticmethod(auto_tests.AutomaticPreparationTests.write)
    correct_wording = auto_tests.AutomaticPreparationTests.correct_wording

    def corrected_sources(self):
        self.write(self.run / "routing.json", {"session_routes": [{
            "project_name": "Corrected", "project_suffix": "987abc", "prefix": "SC",
            "tag_names": ["Management"], "tag_suffixes": ["654def"], "billable": True}]})
        for index, proposal in enumerate(self.sources):
            proposal.update(client_project="Corrected", clockify_project_suffix="987abc",
                            tag_names=["Management"], tag_suffixes=["654def"], disposition="pending")
            self.correct_wording(index, self.descriptions[index], categories=["routing", "wording"],
                extra_patch={"client_project": {"op": "replace", "value": "Corrected"},
                             "tag_names": {"op": "replace", "value": ["Management"]}})

    def fields(self, version=6, *, allow_materialized=True):
        authority = {name: prepared.artifact_handle(self.run / filename)
                     for name, filename in prepared.AUTHORITY_FILES.items()}
        return prepared._auto_fields(self.sources, authority, self.run, "Europe/Bucharest",
            consolidation_policy=f"approved-daily-project-presentation/v{version}",
            allow_materialized_corrections=allow_materialized)

    def test_materialized_pending_routing_and_wording_consolidate_exact_source_fields(self):
        self.corrected_sources()
        fields = self.fields()
        self.assertEqual(1, len(fields["package"]["activities"]))
        self.assertEqual([], fields["package"]["retained"])
        activity = fields["package"]["activities"][0]
        self.assertEqual(154, activity["duration_seconds"])
        self.assertEqual("Corrected", activity["client_project"])
        self.assertEqual(["Management"], activity["tag_names"])
        self.assertEqual(self.descriptions, [item["description"] for item in activity["prepared_descriptions"]])

    def test_fresh_recurring_preparation_consolidates_materialized_corrections(self):
        self.corrected_sources()
        config, outcome = cycle._prepare_new_publication(
            {"spreadsheet_id": "sheet", "timezone": "Europe/Bucharest"},
            {"run_id": "source-run", "run_dir": str(self.run)}, state_dir=self.root / "state",
            sheet_title="August 2026 review")
        self.assertEqual("complete", outcome["status"])
        document, _ = prepared.load(Path(config["review_batch_publication"]))
        self.assertEqual(1, len(document["package"]["activities"]))
        self.assertEqual([], document["package"]["retained"])

    @staticmethod
    def summaries(activities):
        return {item["review_id"]: {"child_review_ids": item["child_review_ids"],
                "description": "SC — Prepared source publication and verified its review receipt"}
                for item in activities if len({child["description"] for child in item["prepared_descriptions"]}) > 1}

    def produce(self, output=None, **kwargs):
        return prepared.prepare(source_dir=self.run, output=output or self.root / "binding.json",
            run_id="source-run", spreadsheet_id="sheet", sheet_title="August 2026 review",
            timezone="Europe/Bucharest", consolidate_materialized_corrections=True,
            summary_provider=self.summaries, **kwargs)

    def test_producer_consumer_roundtrip_preserves_route_time_evidence_and_source_bytes(self):
        self.corrected_sources()
        before = {path.name: path.read_bytes() for path in self.run.iterdir()}
        self.produce()
        document, _ = prepared.load(self.root / "binding.json")
        ids = [publisher.stable_review_id(item) for item in self.sources]
        self.assertEqual(ids, document["materialized_correction_ids"])
        self.assertEqual([], document["wording_only_ids"])
        package = batches.verify_package(document["package"], proposals=self.sources,
            work_contexts=document["work_contexts"], materialized_correction_ids=ids,
            natural_batches=True, allow_review_overlaps=True, allow_clockify_overlaps=True,
            group_summaries=document["group_summaries"])
        activity = package["activities"][0]
        self.assertEqual(["ev-1", "ev-2"], activity["evidence_ids"])
        self.assertEqual([{"start": "2026-08-01T10:00:07+03:00", "end": "2026-08-01T10:01:18+03:00",
                           "duration_seconds": 71, "duration_minutes": 1},
                          {"start": "2026-08-01T10:10:00+03:00", "end": "2026-08-01T10:11:23+03:00",
                           "duration_seconds": 83, "duration_minutes": 1}], activity["allocation_segments"])
        projection = prepared.project(path=self.root / "binding.json", source_dir=self.run,
            proposals=self.sources, run_id="source-run", spreadsheet_id="sheet",
            sheet_title="August 2026 review", project_allowlist={"987abc": "Corrected"})
        self.assertEqual(1, len(projection["rows"]))
        self.assertEqual(154 / 60, projection["rows"][0][3])
        self.assertEqual("SC — Prepared source publication and verified its review receipt", projection["rows"][0][8])
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.run.iterdir()})

    def test_stale_correction_and_route_snapshot_mismatch_never_group(self):
        self.corrected_sources()
        original = copy.deepcopy(self.sources)
        for update in ({"description": "SC — Different pending wording"}, {"client_project": "Other"},
                       {"tag_names": ["Development"]}, {"clockify_project_suffix": "aaaaaa"},
                       {"tag_suffixes": ["bbbbbb"]}, {"billable": False}):
            with self.subTest(update=update):
                self.sources = copy.deepcopy(original)
                self.sources[0].update(update)
                fields = self.fields()
                self.assertEqual(71, fields["preparation_outcome"]["unprepared_seconds"])
                self.assertNotIn(publisher.stable_review_id(self.sources[0]), fields["materialized_correction_ids"])

    def test_terminal_status_and_explicit_protection_always_win(self):
        self.corrected_sources()
        for status in ("approved", "approve", "posted", "skip", "skipped", "rejected", "superseded"):
            with self.subTest(status=status):
                self.sources[0]["disposition"] = status
                fields = self.fields()
                identity = publisher.stable_review_id(self.sources[0])
                self.assertIn(identity, fields["protected_ids"])
                self.assertNotIn(identity, fields["materialized_correction_ids"])
                self.assertEqual(71, fields["preparation_outcome"]["preserved_seconds"])
        self.sources[0]["disposition"] = "pending"
        ids = [publisher.stable_review_id(item) for item in self.sources]
        package = batches.package_review_batches(self.sources,
            editorial_descriptions=dict(zip(ids, self.descriptions)), work_contexts=dict.fromkeys(ids, "context"),
            materialized_correction_ids=ids, protected_ids=ids, natural_batches=True)
        self.assertEqual([], package["activities"])
        self.assertEqual(2, len(package["retained"]))

    def test_legacy_v1_through_v7_replay_bytes_unchanged_even_with_new_opt_in(self):
        self.corrected_sources()
        authority = {name: prepared.artifact_handle(self.run / filename)
                     for name, filename in prepared.AUTHORITY_FILES.items()}
        for version in range(1, 8):
            with self.subTest(version=version):
                fields = self.fields(version, allow_materialized=False)
                self.assertEqual([], fields["package"]["activities"])
                document = {"schema_version": prepared.AUTO_SCHEMA, "run_id": "source-run",
                    "spreadsheet_id": "sheet", "sheet_title": "August 2026 review",
                    "source_proposals": prepared.artifact_handle(self.run / "proposals.json"),
                    "timezone": "Europe/Bucharest", "authority": authority,
                    "consolidation_policy": f"approved-daily-project-presentation/v{version}", **fields}
                path = self.root / f"legacy-{version}.json"
                self.write(path, document)
                before = path.read_bytes()
                self.produce(output=path)
                self.assertEqual(before, path.read_bytes())

    def test_no_missing_or_forged_materialized_authority_in_binding(self):
        self.corrected_sources()
        self.produce()
        path = self.root / "binding.json"
        document = json.loads(path.read_text())
        for ids in ([], [publisher.stable_review_id(self.sources[0])], ["wka-" + "a" * 24 + "-s01"]):
            with self.subTest(ids=ids):
                self.write(path, {**document, "materialized_correction_ids": ids})
                with self.assertRaisesRegex(ValueError, "derived maps differ"):
                    prepared.load(path)

    def test_new_opt_in_requires_reviewed_summary_in_producer_and_consumer(self):
        self.corrected_sources()
        with self.assertRaisesRegex(ValueError, "group summaries are incomplete"):
            prepared.prepare(source_dir=self.run, output=self.root / "binding.json", run_id="source-run",
                spreadsheet_id="sheet", sheet_title="August 2026 review", timezone="Europe/Bucharest",
                consolidate_materialized_corrections=True)
        self.assertFalse((self.root / "binding.json").exists())
        self.produce()
        document = json.loads((self.root / "binding.json").read_text())
        document.pop("group_summaries")
        self.write(self.root / "binding.json", document)
        with self.assertRaisesRegex(ValueError, "group summaries are incomplete"):
            prepared.load(self.root / "binding.json")

    def test_native_consumer_reconstructs_corrected_children_and_rejects_tampering(self):
        self.corrected_sources()
        self.produce()
        document, _ = prepared.load(self.root / "binding.json")
        binding = {"package": document["package"], "source_proposals": self.sources,
            "work_contexts": document["work_contexts"], "group_summaries": document["group_summaries"],
            "captured_rows": [(2, dict(zip(publisher.HEADER,
                publisher.portfolio_row(document["package"]["activities"][0], "source-run"))))],
            "preparation_authority": {
                "consolidation_policy": document["consolidation_policy"],
                "wording_only_ids": document["wording_only_ids"],
                "materialized_correction_ids": document["materialized_correction_ids"]}}
        groups, sources = native._review_batch_groups(binding, "Europe/Bucharest")
        self.assertEqual(self.sources, list(sources.values()))
        self.assertEqual(154, next(iter(groups.values()))["duration_seconds"])
        routing = json.loads((self.run / "routing.json").read_text())
        plan = native.build_plan(capture(publisher.portfolio_row(document["package"]["activities"][0], "source-run")),
            capture_sha256="c" * 64, routing=routing, routing_sha256="r" * 64,
            timezone="Europe/Bucharest", workspace_id="workspace-1", member_id="user-1",
            projects=[{"id": "project-987abc"}], tags=[{"id": "tag-654def"}], live_entries=[],
            review_batches=document["package"], review_batch_sources=self.sources,
            review_batch_work_contexts=document["work_contexts"],
            review_batch_authority=binding["preparation_authority"],
            review_batch_group_summaries=document["group_summaries"])
        self.assertEqual([document["package"]["activities"][0]["description"]] * 2,
                         [entry["payload"]["description"] for entry in plan["entries"]])
        self.assertEqual(["project-987abc"] * 2, [entry["payload"]["projectId"] for entry in plan["entries"]])
        self.assertEqual([["tag-654def"]] * 2, [entry["payload"]["tagIds"] for entry in plan["entries"]])
        self.assertEqual(154, sum(entry["duration_minutes"] * 60 for entry in plan["entries"]))
        for field, value in (("client_project", "Other"), ("description", "SC — Changed corrected words"),
                             ("start", "2026-08-01T10:00:08+03:00")):
            changed = copy.deepcopy(binding)
            changed["source_proposals"][0][field] = value
            with self.subTest(field=field), self.assertRaises(native.NativePostError):
                native._review_batch_groups(changed, "Europe/Bucharest")

    def test_approval_skip_and_timing_split_or_other_patch_cannot_grant_exemption(self):
        self.corrected_sources()
        baseline = (self.run / "review-corrections.jsonl").read_text()
        for decision in ("approve", "skip"):
            with self.subTest(decision=decision):
                (self.run / "review-corrections.jsonl").write_text("")
                self.correct_wording(0, self.descriptions[0], decision=decision)
                fields = self.fields()
                identity = publisher.stable_review_id(self.sources[0])
                self.assertIn(identity, fields["protected_ids"])
                self.assertNotIn(identity, fields["materialized_correction_ids"])
        for categories, patch in ((["routing", "wording"], {"billable": {"op": "replace", "value": True}}),
                                  (["allocation", "wording"], {"duration_seconds": {"op": "replace", "value": 71}})):
            with self.subTest(categories=categories):
                (self.run / "review-corrections.jsonl").write_text("")
                self.correct_wording(0, self.descriptions[0], categories=categories, extra_patch=patch)
                self.assertNotIn(publisher.stable_review_id(self.sources[0]), self.fields()["materialized_correction_ids"])
        log = self.run / "review-corrections.jsonl"
        log.write_text("")
        item = {"id": "rvi-split", "current": self.sources[0]}
        split = review_corrections.build_decision(item, decision="modify", reviewer="human",
            reviewed_at="2026-08-01T11:00:00+03:00", correction_categories=["split", "wording"],
            rationale="Historical split", field_patch={"description": {"op": "replace", "value": self.descriptions[0]}},
            _allow_legacy_split_without_expectation=True)
        review_corrections.append_decision(log, split, item=item)
        self.assertNotIn(publisher.stable_review_id(self.sources[0]), self.fields()["materialized_correction_ids"])
        log.write_text(baseline)
        prior = review_corrections._read_log(log)[-1]
        proposal = self.sources[0]
        record = {"schema_version": 1, "record_type": "source_bound_timing_correction",
            "activity_id": proposal["activity_id"], "review_item_id": "rvi-timing",
            "evidence_fingerprint": review_corrections.evidence_fingerprint(proposal["provenance"]["evidence_ids"]),
            "parent_run_id": "prior-run", "parent_artifacts": dict.fromkeys(("evidence/evidence-ledger.json",
                "proposals.json", "semantic-analysis.json", "work-accounting-result.json"), "sha256:" + "a" * 64),
            "request_evidence_id": "request-id", "result_evidence_id": "result-id",
            "start": proposal["start"], "end": proposal["end"], "duration_seconds": 71,
            "reviewer": "human", "reviewed_at": "2026-08-01T11:00:00+03:00", "rationale": "Timing correction"}
        record["correction_id"] = "tcor-" + review_corrections.canonical_digest(record)[7:31]
        record["canonical_digest"] = review_corrections.canonical_digest(record)
        record["previous_digest"] = prior["canonical_digest"]
        log.write_text(baseline + json.dumps(record) + "\n")
        fields = self.fields()
        identity = publisher.stable_review_id(proposal)
        self.assertIn(identity, fields["protected_ids"])
        self.assertNotIn(identity, fields["materialized_correction_ids"])
        record["record_type"] = "unknown_metadata"
        log.write_text(baseline + json.dumps(record) + "\n")
        with self.assertRaises(ValueError):
            self.fields()

    def test_native_full_capture_accepts_only_exact_source_bound_parent_retirements(self):
        self.corrected_sources()
        self.produce()
        document, _ = prepared.load(self.root / "binding.json")
        group = document["package"]["activities"][0]
        parent = publisher.portfolio_row(group, "source-run")
        retired = [prepared._retired(publisher.proposal_row(child, "source-run"), group["review_id"])
                   for child in self.sources]
        retired[0][14] = "Preserved pre-existing review note"
        routing = json.loads((self.run / "routing.json").read_text())
        def plan(rows):
            return native.build_plan(capture(*rows), capture_sha256="c" * 64, routing=routing,
                routing_sha256="r" * 64, timezone="Europe/Bucharest", workspace_id="workspace-1",
                member_id="user-1", projects=[{"id": "project-987abc"}], tags=[{"id": "tag-654def"}],
                live_entries=[], review_batches=document["package"], review_batch_sources=self.sources,
                review_batch_work_contexts=document["work_contexts"],
                review_batch_group_summaries=document["group_summaries"],
                review_batch_authority={key: document[key] for key in (
                    "consolidation_policy", "wording_only_ids", "materialized_correction_ids")})
        try:
            result = plan([parent, *retired])
        except native.NativePostError as error:
            self.fail(f"mechanically retired exact children must remain postable: {error}")
        self.assertEqual(1, result["row_count"])
        self.assertEqual([document["package"]["activities"][0]["description"]] * 2,
                         [entry["payload"]["description"] for entry in result["entries"]])
        self.assertEqual(154, sum(entry["duration_minutes"] * 60 for entry in result["entries"]))
        invalid = [(9, "approved"), (9, "skip"), (9, "rejected"), (13, "posted"),
                   (12, "User superseded this work"),
                   (12, "Prepared review parent: pvi-" + "a" * 24 + "; original time preserved, not financial credit."),
                   (1, "2026-08-01 10:00:08"), (4, "Other"),
                   (8, "SC — Unrelated corrected wording")]
        for column, value in invalid:
            with self.subTest(column=column, value=value):
                changed = copy.deepcopy(retired)
                changed[0][column] = value
                with self.assertRaises(native.NativePostError):
                    plan([parent, *changed])


if __name__ == "__main__":
    unittest.main()
