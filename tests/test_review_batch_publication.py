"""Source-bound batches must replace raw rows without changing credited time."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts import clockify_review_batches as batches
from scripts import clockify_sheet_publish as publisher
from scripts import clockify_review_cycle as cycle
from scripts import clockify_review_batch_publication as prepared
from scripts.clockify_meeting_publication_alias import artifact_handle
from test_sheet_publish import StatefulGateway, AppendCorruptingGateway


def source(number, start, end, seconds):
    return {"review_activity_key": f"wka-{number:024x}", "allocation_segment": 1,
            "activity_id": f"act-{number}", "start": start, "end": end,
            "duration_seconds": seconds, "duration_minutes": seconds // 60,
            "client_project": "Example", "tag_names": ["Development"],
            "tag_suffixes": ["def456"], "clockify_project_suffix": "abc123",
            "billable": True, "confidence": "high", "review_warnings": [],
            "description": "raw source wording",
            "provenance": {"source_type": "semantic_activity", "evidence_ids": [f"ev-{number}"]}}


class BatchPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.run = self.root / "source-run"
        self.run.mkdir()
        self.sources = [source(1, "2026-08-01T10:00:07+03:00", "2026-08-01T10:01:18+03:00", 71),
                        source(2, "2026-08-01T10:10:00+03:00", "2026-08-01T10:11:23+03:00", 83)]
        held = source(3, "2026-08-01T11:00:00+03:00", "2026-08-01T11:01:00+03:00", 60)
        held["human_corrected"] = True
        self.sources.append(held)
        ids = [publisher.stable_review_id(p) for p in self.sources[:2]]
        self.descriptions = dict.fromkeys(ids, "SC — Prepared client deliverables")
        self.contexts = dict.fromkeys(ids, "verified-work-context")
        self.package = batches.package_review_batches(self.sources,
            editorial_descriptions=self.descriptions, work_contexts=self.contexts)
        self.write(self.run / "proposals.json", self.sources)
        self.write(self.run / "routing.json", {})
        self.write(self.run / "work-accounting-result.json", {"schema_version": 1,
            "allocation_mode": "non_overlapping_v1", "proposals": self.sources,
            "ambiguous": [], "skipped": []})
        self.binding = self.root / "prepared.json"
        self.document = {"schema_version": "review-batch-publication/v1", "run_id": "source-run",
            "spreadsheet_id": "sheet", "sheet_title": "August 2026 review",
            "source_proposals": {"path": str(self.run / "proposals.json"),
                "sha256": "sha256:" + hashlib.sha256((self.run / "proposals.json").read_bytes()).hexdigest()},
            "editorial_descriptions": self.descriptions, "work_contexts": self.contexts,
            "protected_ids": [], "timezone": "Europe/Bucharest", "package": self.package}
        self.write(self.binding, self.document)

    @staticmethod
    def write(path, value):
        path.write_text(json.dumps(value))

    def publish(self, gateway):
        return publisher.publish_proposal_partitions(gateway, spreadsheet_id="sheet",
            sheet_title="August 2026 review", template_title="Proposals", proposals=self.sources,
            run_id="source-run", project_allowlist={}, source_dir=self.run,
            review_batch_publication=self.binding)

    def staging(self, children, *, historical=None, notes=None):
        cells = [[{"userEnteredValue": {"numberValue" if type(value) in (int, float) else "stringValue": value}}
                  for value in row] for row in [publisher.HEADER, *children]]
        for row, column in notes or []:
            cells[row][column]["note"] = "Keep this native annotation"
        capture = {"spreadsheetId": "sheet", "sheets": [{"properties": {"sheetId": 1,
            "title": "August 2026 review", "gridProperties": {"rowCount": 100, "columnCount": 15}},
            "data": [{"rowData": [{"values": row} for row in cells]}]}]}
        capture_path = self.root / "capture.json"
        self.write(capture_path, capture)
        clockify = self.root / "clockify.json"
        self.write(clockify, {"read_only": True, "entries": []})
        materialization = self.root / "materialization.json"
        self.write(materialization, {"schema_version": "isolated-unsealed-editorial-projection-qualification/v1",
            "sources": [{"index": 0, "source_handles": {"proposals.json": artifact_handle(self.run / "proposals.json"),
                "routing.json": artifact_handle(self.run / "routing.json")},
                "projection_proposals": artifact_handle(self.run / "proposals.json")} ]})
        declaration = {"schema_version": "review-batch-staging/v1", "source_index": 0,
            "projection_binding": artifact_handle(self.binding), "qualified_materialization": artifact_handle(materialization),
            "sheet_capture": artifact_handle(capture_path), "clockify_capture": artifact_handle(clockify),
            "historical_sources": historical or []}
        path = self.root / "stage.json"
        self.write(path, declaration)
        return path, capture

    def test_staged_source_bound_display_preimage_retires_exact_cells_without_rewriting_run_marker(self):
        children = [publisher.proposal_row(p, "source-run") for p in self.sources[:2]]
        for child in children:
            child[8] = self.descriptions[child[0]]
            child[12] = "Historical display warning remains visible"
        path, capture = self.staging(children)
        function = getattr(prepared, "stage_projection", None)
        self.assertIsNotNone(function, "source-bound captured display preimage adapter is missing")
        projection = function(path)
        self.assertEqual(children, list(projection["children"].values()))
        gateway = StatefulGateway(rows=[publisher.HEADER, *copy.deepcopy(children)])
        gateway.cell_data = lambda *_args: copy.deepcopy(capture)
        plans = prepared.plan_children(gateway, spreadsheet_id="sheet", sheet_title="August 2026 review", projection=projection)
        gateway.rows.extend(copy.deepcopy(projection["rows"]))
        self.assertEqual(2, prepared.retire_children(gateway, spreadsheet_id="sheet", sheet_title="August 2026 review",
            plans=plans, parent_rows=projection["rows"]))
        self.assertEqual(["source-run", "source-run"], [row[11] for row in gateway.rows[1:3]])
        self.assertEqual([71 / 60, 83 / 60], [row[3] for row in gateway.rows[1:3]])
        self.assertTrue(all("Historical display warning remains visible" in row[12] for row in gateway.rows[1:3]))

    def test_staging_rejects_unbound_wording_run_financial_identity_and_revision(self):
        baseline = [publisher.proposal_row(p, "source-run") for p in self.sources[:2]]
        for column, value in ((8, "Unbound human wording"), (11, "invented-run"), (4, "Other client"),
                              (6, "other-activity"), (3, 99), (10, 2)):
            with self.subTest(column=column):
                children = copy.deepcopy(baseline)
                children[0][column] = value
                path, _capture = self.staging(children)
                projection = prepared.stage_projection(path)
                self.assertEqual([publisher.stable_review_id(self.sources[1])], [row[0] for row in projection["rows"]])
                self.assertEqual({}, projection["children"])
                self.assertTrue(projection["staging_receipt"]["diagnostics"])

    def test_staging_native_notes_protect_group_and_new_note_or_values_block_retirement(self):
        children = [publisher.proposal_row(p, "source-run") for p in self.sources[:2]]
        path, _capture = self.staging(children, notes=[(1, 12)])
        self.assertEqual([publisher.stable_review_id(self.sources[1])],
                         [row[0] for row in prepared.stage_projection(path)["rows"]])
        for change in ("note", "values"):
            with self.subTest(change=change):
                path, capture = self.staging(children)
                projection = prepared.stage_projection(path)
                gateway = StatefulGateway(rows=[publisher.HEADER, *copy.deepcopy(children)])
                gateway.cell_data = lambda *_args: copy.deepcopy(capture)
                plans = prepared.plan_children(gateway, spreadsheet_id="sheet", sheet_title="August 2026 review", projection=projection)
                gateway.rows.extend(copy.deepcopy(projection["rows"]))
                if change == "note":
                    capture["sheets"][0]["data"][0]["rowData"][1]["values"][12]["note"] = "Added after planning"
                else:
                    capture["sheets"][0]["data"][0]["rowData"][1]["values"][8]["userEnteredValue"] = {"stringValue": "Changed after planning"}
                with self.assertRaises(publisher.PublicationError):
                    prepared.retire_children(gateway, spreadsheet_id="sheet", sheet_title="August 2026 review",
                        plans=plans, parent_rows=projection["rows"])
                self.assertEqual(children, gateway.rows[1:3])

    def test_staging_historical_run_needs_real_same_native_source_witness(self):
        witness = self.root / "historical-run"
        witness.mkdir()
        self.write(witness / "proposals.json", self.sources)
        self.write(witness / "routing.json", {})
        children = [publisher.proposal_row(p, "historical-run") for p in self.sources[:2]]
        declaration = [{"proposals": artifact_handle(witness / "proposals.json"), "routing": artifact_handle(witness / "routing.json")}]
        path, _capture = self.staging(children, historical=declaration)
        projection = prepared.stage_projection(path)
        self.assertEqual(1, len(projection["rows"]))
        self.assertEqual(["historical-run"] * 2, [row[11] for row in projection["children"].values()])
        self.write(witness / "proposals.json", [])
        with self.assertRaises(ValueError):
            prepared.stage_projection(path)

    def test_staging_missing_or_mutated_materialization_and_source_pin_fails(self):
        children = [publisher.proposal_row(p, "source-run") for p in self.sources[:2]]
        for mutation in ("missing", "materialization", "source"):
            with self.subTest(mutation=mutation):
                path, _capture = self.staging(children)
                if mutation == "missing":
                    document = json.loads(path.read_bytes())
                    document.pop("qualified_materialization")
                    self.write(path, document)
                elif mutation == "materialization":
                    self.write(self.root / "materialization.json", {})
                else:
                    self.write(self.run / "proposals.json", [])
                with self.assertRaises(ValueError):
                    prepared.stage_projection(path)
                self.write(self.run / "proposals.json", self.sources)

    def test_fresh_clockify_warning_binds_native_counterpart_identity_not_normalized_sort_position(self):
        from scripts import evidence_ledger
        entries = [{"id": "later", "timeInterval": {"start": self.sources[1]["start"], "end": self.sources[1]["end"]}},
                   {"id": "earlier", "timeInterval": {"start": self.sources[0]["start"], "end": self.sources[0]["end"]}}]
        capture = {"read_only": True, "entries": entries}
        events = evidence_ledger.normalize_collector_snapshot({"clockify": capture})
        by_source = {event.source_ref["source_id"]: event.evidence_id for event in events}
        view = prepared.fresh_warning_view(self.sources, capture)
        self.assertEqual(by_source["earlier"], view[0]["review_warnings"][0]["counterpart_id"])
        self.assertEqual(by_source["later"], view[1]["review_warnings"][0]["counterpart_id"])
        self.assertEqual(71, view[0]["review_warnings"][0]["overlap_duration_seconds"])
        self.assertEqual(83, view[1]["review_warnings"][0]["overlap_duration_seconds"])
        self.assertEqual([], self.sources[0]["review_warnings"])

    def test_staged_value_only_publication_and_exact_retirement_retry_preserve_every_original_cell(self):
        children = [publisher.proposal_row(p, "source-run") for p in self.sources[:2]]
        for child in children:
            child[8] = self.descriptions[child[0]]
            child[12] = "Historical warning text"
        path, capture = self.staging(children)
        projection = prepared.stage_projection(path)
        gateway = StatefulGateway(rows=[publisher.HEADER, *copy.deepcopy(children)])
        def current_cells(*_args):
            result = copy.deepcopy(capture)
            result["sheets"][0]["data"][0]["rowData"] = [{"values":[{"userEnteredValue":{
                "numberValue" if type(v) in (int,float) else "stringValue":v}} for v in row]} for row in gateway.rows]
            return result
        gateway.cell_data = current_cells
        gateway.prepare_sheet = lambda *_args: self.fail("staged publication must not reformat existing rows")
        function = getattr(prepared, "publish_staged_projection", None)
        self.assertIsNotNone(function, "staged value-only mutation boundary is missing")
        receipt = function(gateway, projection=projection, spreadsheet_id="sheet", sheet_title="August 2026 review")
        self.assertEqual(2, receipt["terminal_updates"])
        self.assertEqual([2,3], receipt["retired_child_rows"])
        self.assertEqual(children[0][:9], gateway.rows[1][:9])
        self.assertEqual("source-run", gateway.rows[1][11])
        repeated = function(gateway, projection=projection, spreadsheet_id="sheet", sheet_title="August 2026 review")
        self.assertEqual(0, repeated["terminal_updates"])
        self.assertEqual(4, len(gateway.rows))

    def test_fresh_warning_standalone_preserves_wording_run_and_reason_and_rechecks_native_notes(self):
        children = [publisher.proposal_row(p, "source-run") for p in self.sources[:2]]
        for child in children:
            child[8] = self.descriptions[child[0]]
            child[12] = "Keep historical warning"
        path, capture = self.staging(children)
        clockify = {"read_only": True, "entries": [{"id":"live-counterpart",
            "timeInterval":{"start":self.sources[0]["start"],"end":self.sources[0]["end"]}}]}
        self.write(self.root / "clockify.json", clockify)
        view = prepared.fresh_warning_view(self.sources,clockify)
        folder = self.root / "warning-view"
        folder.mkdir()
        self.write(folder / "proposals.json",view)
        document = {**self.document,"run_id":"warning-view","source_proposals":artifact_handle(folder / "proposals.json"),
            "package":batches.package_review_batches(view,editorial_descriptions=self.descriptions,work_contexts=self.contexts)}
        self.write(self.binding,document)
        declaration=json.loads(path.read_bytes())
        declaration["projection_binding"]=artifact_handle(self.binding)
        declaration["clockify_capture"]=artifact_handle(self.root / "clockify.json")
        self.write(path,declaration)
        projection=prepared.stage_projection(path)
        target=next(row for row in projection["rows"] if row[0]==children[0][0])
        self.assertEqual(children[0][8],target[8])
        self.assertEqual("source-run",target[11])
        self.assertIn("Keep historical warning",target[12])
        self.assertIn("existing_clockify_overlap",target[12])
        self.assertEqual(71/60,target[3])
        gateway=StatefulGateway(rows=[publisher.HEADER,*copy.deepcopy(children)])
        gateway.cell_data=lambda *_args:copy.deepcopy(capture)
        self.assertEqual([],prepared.plan_children(gateway,spreadsheet_id="sheet",sheet_title="August 2026 review",projection=projection))
        capture["sheets"][0]["data"][0]["rowData"][1]["values"][12]["note"]="Added after plan"
        with self.assertRaises(publisher.PublicationError):
            prepared.publish_staged_projection(gateway,projection=projection,spreadsheet_id="sheet",sheet_title="August 2026 review")
        self.assertEqual(children,gateway.rows[1:])

    def test_real_publisher_cycle_receipts_agree_on_disjoint_exact_seconds_and_retry(self):
        gateway = StatefulGateway(rows=[publisher.HEADER])
        result = self.publish(gateway)
        self.assertEqual(2, len(gateway.rows))

        row = gateway.rows[1]
        self.assertTrue(row[0].startswith("pvi-"))
        self.assertEqual(154 / 60, row[3])
        self.assertEqual("SC — Prepared client deliverables", row[8])
        self.assertEqual("", row[12])  # No manufactured Flash validation.
        config = {"spreadsheet_id": "sheet", "review_batch_publication": str(self.binding)}
        expected = cycle._expected_publication_receipts(config,
            {"run_dir": str(self.run), "run_id": "source-run"},
            sheet_title="August 2026 review", publication_profile=None)
        for key, value in expected[0].items():
            self.assertEqual(value, result["publications"][0][key])
        cycle._validated_publication_document({"schema_version": "sheet-publication-result/v1",
            "status": "published", "external_writes": True, "clockify_writes": 0,
            "publications": result["publications"]}, expected, source_dir=self.run)
        recovered = cycle._receipt_publication_config(config, {"publications": result["publications"]})
        self.assertEqual(str(self.binding), recovered["review_batch_publication"])
        self.publish(gateway)
        self.assertEqual(2, len(gateway.rows))

    def test_cycle_loads_prepared_binding_and_passes_it_to_publisher_but_rejects_unknown_key(self):
        for name in ("corrections.jsonl", "acceptance.jsonl"):
            (self.root / name).write_text("")
        config = {"root": str(self.root), "runs_dir": str(self.root / "runs"),
            "state_dir": str(self.root / "state"), "cache": str(self.root / "cache"),
            "routing": str(self.run / "routing.json"), "corrections": str(self.root / "corrections.jsonl"),
            "acceptance": str(self.root / "acceptance.jsonl"), "workspace_id": "workspace",
            "member_id": "member", "recovery_since": "2026-08-01", "timezone": "Europe/Bucharest",
            "spreadsheet_id": "sheet", "monthly_sheet_title_template": "{month_name} {year} review",
            "calendly_optional": True, "review_batch_publication": str(self.binding)}
        path = self.root / "config.json"
        self.write(path, config)
        loaded = cycle.load_config(path)
        command = cycle._publisher_command(loaded, {"run_dir": str(self.run), "run_id": "source-run"},
            {"run_dir": str(self.root / "replay")}, sheet_title="August 2026 review",
            result_path=self.root / "result.json")
        self.assertEqual(str(self.binding), command[command.index("--review-batch-publication") + 1])
        self.write(path, {**config, "typo_batch_input": str(self.binding)})
        with self.assertRaises(cycle.CycleError):
            cycle.load_config(path)

    def test_tampered_source_or_package_rejected_before_gateway_effects(self):
        for kind in ("source", "package", "editorial", "protected"):
            with self.subTest(kind=kind):
                altered = copy.deepcopy(self.document)
                if kind == "source":
                    altered["source_proposals"]["sha256"] = "sha256:" + "0" * 64
                elif kind == "package":
                    altered["package"]["activities"][0]["duration_seconds"] += 1
                elif kind == "editorial":
                    altered["editorial_descriptions"][next(iter(self.descriptions))] = "SC — unrelated wording"
                else:
                    altered["protected_ids"] = [publisher.stable_review_id(self.sources[0])]
                self.write(self.binding, altered)
                gateway = StatefulGateway(rows=[publisher.HEADER])
                with self.assertRaises(publisher.PublicationError):
                    self.publish(gateway)
                self.assertEqual([publisher.HEADER], gateway.rows)
                self.assertEqual([], gateway.read_ranges)

    def test_parent_readback_precedes_pending_child_retirement_and_retry_is_stable(self):
        children = [publisher.proposal_row(p, "source-run") for p in self.sources[:2]]
        gateway = StatefulGateway(rows=[publisher.HEADER, *children])
        result = self.publish(gateway)
        self.assertEqual(2, result["terminal_updates"])
        self.assertEqual(4, len(gateway.rows))
        for before, after in zip(children, gateway.rows[1:3]):
            self.assertEqual(before[:9], after[:9])
            self.assertEqual("superseded", after[9])
            self.assertEqual("unposted", after[13])
            self.assertIn(gateway.rows[-1][0], after[12])
        self.assertEqual(0, self.publish(gateway)["terminal_updates"])
        self.assertEqual(4, len(gateway.rows))

    def test_explicit_already_cleaned_child_preimage_is_preserved(self):
        child = publisher.proposal_row(self.sources[0], "source-run")
        child[8] = self.descriptions[child[0]]
        gateway = StatefulGateway(rows=[publisher.HEADER, child])
        self.publish(gateway)
        self.assertEqual(child[:9], gateway.rows[1][:9])
        self.assertEqual("superseded", gateway.rows[1][9])
        self.assertEqual(0, self.publish(gateway)["terminal_updates"])

    def test_protected_or_human_changed_live_children_reject_before_parent_append(self):
        for column, value in ((9, "approved"), (13, "posted"), (14, "human correction"), (8, "edited wording")):
            child = publisher.proposal_row(self.sources[0], "source-run")
            child[column] = value
            gateway = StatefulGateway(rows=[publisher.HEADER, child])
            with self.subTest(column=column), self.assertRaises(publisher.PublicationError):
                self.publish(gateway)
            self.assertEqual(2, len(gateway.rows))

    def test_failed_parent_exact_readback_does_not_retire_any_child(self):
        children = [publisher.proposal_row(p, "source-run") for p in self.sources[:2]]
        gateway = AppendCorruptingGateway(rows=[publisher.HEADER, *children])
        with self.assertRaises(publisher.PublicationError):
            self.publish(gateway)
        self.assertEqual(children, gateway.rows[1:3])

    def test_child_human_decision_between_parent_append_and_retirement_is_preserved(self):
        class HumanRaceGateway(StatefulGateway):
            def append_values(self, spreadsheet_id, range_name, rows):
                super().append_values(spreadsheet_id, range_name, rows)
                self.rows[1][9] = "approved"
        children = [publisher.proposal_row(p, "source-run") for p in self.sources[:2]]
        gateway = HumanRaceGateway(rows=[publisher.HEADER, *children])
        with self.assertRaises(publisher.PublicationError):
            self.publish(gateway)
        self.assertEqual("approved", gateway.rows[1][9])
        self.assertEqual(children[1], gateway.rows[2])

    def test_clean_retained_overlap_and_long_sources_publish_only_explicit_wording(self):
        overlap = source(4, "2026-08-01T12:00:00+03:00", "2026-08-01T12:01:00+03:00", 60)
        overlap["review_warnings"] = [{"type": "existing_clockify_overlap",
            "counterpart_id": "ev-" + "a" * 64, "overlap_start": overlap["start"],
            "overlap_end": overlap["end"], "overlap_duration_seconds": 60}]
        long = source(5, "2026-08-01T13:00:00+03:00", "2026-08-01T13:30:00+03:00", 1800)
        self.sources.extend([overlap, long])
        for p in (overlap, long):
            identity = publisher.stable_review_id(p)
            self.descriptions[identity] = "SC — Clean standalone deliverable"
            self.contexts[identity] = "separate verified work"
        self.write(self.run / "proposals.json", self.sources)
        self.document["source_proposals"]["sha256"] = "sha256:" + hashlib.sha256((self.run / "proposals.json").read_bytes()).hexdigest()
        self.document["package"] = batches.package_review_batches(self.sources,
            editorial_descriptions=self.descriptions, work_contexts=self.contexts)
        self.write(self.binding, self.document)
        gateway = StatefulGateway(rows=[publisher.HEADER])
        self.publish(gateway)
        self.assertEqual(4, len(gateway.rows))
        self.assertEqual(2014, round(sum(row[3] for row in gateway.rows[1:]) * 60))
        self.assertEqual(["SC — Clean standalone deliverable"] * 2, [row[8] for row in gateway.rows[2:]])
        self.assertIn("existing_clockify_overlap", gateway.rows[2][12])

    def test_grouped_child_warnings_remain_visible_on_parent_review_and_receipt(self):
        warning = {"type": "estimated_session_placement", "reason": "Observed work; estimated placement."}
        self.sources[0]["review_warnings"] = [warning]
        self.write(self.run / "proposals.json", self.sources)
        self.write(self.run / "work-accounting-result.json", {"schema_version": 1,
            "allocation_mode": "non_overlapping_v1", "proposals": self.sources,
            "ambiguous": [], "skipped": []})
        self.document["source_proposals"]["sha256"] = "sha256:" + hashlib.sha256((self.run / "proposals.json").read_bytes()).hexdigest()
        self.document["package"] = batches.package_review_batches(self.sources,
            editorial_descriptions=self.descriptions, work_contexts=self.contexts)
        self.write(self.binding, self.document)
        gateway = StatefulGateway(rows=[publisher.HEADER])
        published = self.publish(gateway)
        self.assertNotEqual("", gateway.rows[1][12], "Grouped review must expose original child warnings")
        visible = json.loads(gateway.rows[1][12])
        self.assertEqual([{"review_id": "wka-000000000000000000000001-s01", "warnings": [warning]}], visible)
        expected = cycle._expected_publication_receipts(
            {"spreadsheet_id": "sheet", "review_batch_publication": str(self.binding)},
            {"run_dir": str(self.run), "run_id": "source-run"},
            sheet_title="August 2026 review", publication_profile=None)
        self.assertEqual(expected[0]["rows_sha256"], published["publications"][0]["rows_sha256"])

    def test_rejected_existing_parent_never_supersedes_still_pending_children(self):
        children = [publisher.proposal_row(p, "source-run") for p in self.sources[:2]]
        for column, value in ((9, "skip"), (13, "posted"), (14, "Do not use this proposed grouping")):
            with self.subTest(column=column):
                parent = publisher.portfolio_row(self.package["activities"][0], "source-run")
                parent[column] = value
                gateway = StatefulGateway(rows=[publisher.HEADER, *copy.deepcopy(children), parent])
                with self.assertRaises(publisher.PublicationError):
                    self.publish(gateway)
                self.assertEqual(children, gateway.rows[1:3])
                self.assertEqual(parent, gateway.rows[-1])


if __name__ == "__main__":
    unittest.main()
