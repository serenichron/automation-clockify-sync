"""A sealed pending display is presentation authority, never financial consent."""
import copy
import json
import unittest

from scripts import clockify_review_batch_publication as prepared
from scripts import clockify_native_sheet_post as native
from scripts import clockify_sheet_publish as publisher
from test_clockify_native_sheet_post import capture
import test_corrected_pending_consolidation as corrected_tests


class CurrentPresentationRetirementTests(unittest.TestCase):
    setUp = corrected_tests.CorrectedPendingConsolidationTests.setUp
    write = staticmethod(corrected_tests.CorrectedPendingConsolidationTests.write)
    correct_wording = corrected_tests.CorrectedPendingConsolidationTests.correct_wording
    corrected_sources = corrected_tests.CorrectedPendingConsolidationTests.corrected_sources
    summaries = staticmethod(corrected_tests.CorrectedPendingConsolidationTests.summaries)

    def fixture(self, changes=None, snapshot_changes=None):
        self.assertTrue(callable(getattr(prepared, "bind_current_presentation", None)),
                        "Missing source-bound current-presentation producer")
        self.corrected_sources()
        rows = [publisher.proposal_row(p, "source-run") for p in self.sources]
        rows[0][8] = "SC — Prepared publication for review"
        rows[0][10] = 2
        rows[0][14] = "Existing review note, not approval"
        rows[1][8] = "SC — Verified the review receipt"
        if changes:
            changes(rows)
        self.capture_path = self.root / "current.json"
        snapshot = capture(*rows)
        snapshot["structuredContent"]["sheets"][0]["properties"]["sheetId"] = 1
        if snapshot_changes:
            snapshot_changes(snapshot)
        self.write(self.capture_path, snapshot)
        self.ids = [publisher.stable_review_id(p) for p in self.sources]
        self.before_rows = rows
        return prepared.bind_current_presentation(capture=prepared.artifact_handle(self.capture_path),
            proposals=self.sources, review_ids=self.ids, timezone="Europe/Bucharest")

    def produce(self, authority):
        path = self.root / "binding.json"
        prepared.prepare(source_dir=self.run, output=path, run_id="source-run",
            spreadsheet_id="sheet-1", sheet_title="September 2026 portfolio review",
            timezone="Europe/Bucharest", consolidate_materialized_corrections=True,
            summary_provider=self.summaries, current_presentation=authority)
        return prepared.load(path)[0]

    def native_plan(self, document, rows=None, authority=True, cell_changes=None):
        group = document["package"]["activities"][0]
        rows = rows or [prepared._retired(row, group["review_id"]) for row in self.before_rows]
        actual = capture(*rows, publisher.portfolio_row(group, "source-run"))
        original_cells = json.loads(self.capture_path.read_text())["structuredContent"]["sheets"][0]["data"][0]["rowData"]
        for original, target in zip(original_cells, actual["structuredContent"]["sheets"][0]["data"][0]["rowData"]):
            for before, after in zip(original["values"], target["values"]):
                if "note" in before:
                    after["note"] = before["note"]
        if cell_changes:
            cell_changes(actual)
        return native.build_plan(actual,
            capture_sha256="c" * 64, routing=json.loads((self.run / "routing.json").read_text()),
            routing_sha256="r" * 64, timezone="Europe/Bucharest", workspace_id="workspace-1",
            member_id="user-1", projects=[{"id": "project-987abc"}], tags=[{"id": "tag-654def"}],
            live_entries=[], review_batches=document["package"], review_batch_sources=self.sources,
            review_batch_work_contexts=document["work_contexts"],
            review_batch_group_summaries=document["group_summaries"],
            review_batch_authority={key: document[key] for key in (
                "consolidation_policy", "wording_only_ids", "materialized_correction_ids")},
            **({"review_batch_current_presentation": document["current_presentation"]} if authority else {}))

    def test_real_producer_consumer_preserves_corrected_caption_revision_and_original_seconds(self):
        # Restoring the source-only caption/revision gate must break this test.
        authority = self.fixture()
        before = {p.name: p.read_bytes() for p in self.run.iterdir()}
        document = self.produce(authority)
        result = self.native_plan(document)
        self.assertEqual([document["package"]["activities"][0]["description"]] * 2,
                         [entry["payload"]["description"] for entry in result["entries"]])
        self.assertEqual(["SC — Prepared publication for review", "SC — Verified the review receipt"],
                         [d["description"] for d in document["package"]["activities"][0]["prepared_descriptions"]])
        self.assertEqual([71 / 60, 83 / 60], [entry["duration_minutes"] for entry in result["entries"]])
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.run.iterdir()})
        self.assertNotIn("financial_approval", result)
        projection = prepared.project(path=self.root / "binding.json", source_dir=self.run,
            proposals=self.sources, run_id="source-run", spreadsheet_id="sheet-1",
            sheet_title="September 2026 portfolio review", project_allowlist={"987abc": "Corrected"})
        self.assertEqual(self.before_rows[0], projection["children"][self.ids[0]])
        with self.assertRaises(native.NativePostError):
            self.native_plan(document, authority=False)

    def test_snapshot_cannot_authorize_changed_accounting_or_terminal_decisions(self):
        # Removing source/status/revision validation must expose these failures.
        for column, value in [(1, "2026-08-01 10:00:08"), (2, "2026-08-01 10:01:19"),
                (3, 2), (4, "Other"), (5, "Other"), (6, "foreign-source"),
                (9, "approved"), (9, "posted"), (9, "skip"), (9, "skipped"),
                (9, "rejected"), (9, "superseded"), (13, "posted"),
                (10, 0), (10, -1), (10, 1.5), (10, True), (10, "2")]:
            with self.subTest(column=column, value=value), self.assertRaises(ValueError):
                self.fixture(lambda rows: rows[0].__setitem__(column, value))

    def test_consumer_refuses_any_bound_retirement_drift(self):
        document = self.produce(self.fixture())
        parent = document["package"]["activities"][0]["review_id"]
        original = [prepared._retired(row, parent) for row in self.before_rows]
        for column, value in [(8, "SC — Unbound wording"), (10, 3), (9, "approved"),
                (9, "skip"), (13, "posted"), (12, "Wrong retirement marker"),
                (14, "Changed notes"), (1, "2026-08-01 10:00:08"), (4, "Other")]:
            rows = copy.deepcopy(original)
            rows[0][column] = value
            with self.subTest(column=column), self.assertRaises(native.NativePostError):
                self.native_plan(document, rows)
        with self.assertRaises(native.NativePostError):
            self.native_plan(document, [original[1], original[0]])
        with self.assertRaises(native.NativePostError):
            self.native_plan(document, [original[0], original[0], original[1]])
        self.capture_path.write_text(self.capture_path.read_text() + " ")
        with self.assertRaises(native.NativePostError):
            self.native_plan(document)

    def test_snapshot_identity_inventory_and_original_source_binding_are_exact(self):
        authority = self.fixture()
        for identities in [self.ids + self.ids[:1], [*self.ids, "wka-" + "f" * 24 + "-s01"]]:
            with self.assertRaises(ValueError):
                prepared.bind_current_presentation(capture=authority["capture"], proposals=self.sources,
                    review_ids=identities, timezone="Europe/Bucharest")
        document = self.produce(authority)
        document["current_presentation"]["source_proposals_sha256"] = "sha256:" + "0" * 64
        with self.assertRaises(native.NativePostError):
            self.native_plan(document)

    def test_readable_reason_and_native_note_do_not_replace_original_warning_authority(self):
        # Requiring M to equal source JSON must reject this legitimate display.
        warning = {"type": "observed_capacity_cap", "requested_minutes": 2,
                   "observed_capacity_minutes": 1, "proposed_minutes": 1}
        self.sources[0]["review_warnings"] = [copy.deepcopy(warning)]
        readable = "Timp limitat la intervalul observat; verificarea rămâne deschisă."
        original_note = json.dumps({"original_review_warnings": [warning]}, ensure_ascii=False)
        def native_note(snapshot):
            snapshot["structuredContent"]["sheets"][0]["data"][0]["rowData"][1]["values"][12]["note"] = original_note
        authority = self.fixture(lambda rows: rows[0].__setitem__(12, readable), native_note)
        source_bytes = (self.run / "proposals.json").read_bytes()
        document = self.produce(authority)
        result = self.native_plan(document)
        group = result["review_batches_binding"]["package"]["activities"][0]
        self.assertEqual([{"review_id": self.ids[0], "warnings": [warning]},
                          {"review_id": self.ids[1], "warnings": []}], group["child_review_warnings"])
        self.assertEqual([warning], result["review_batches_binding"]["source_proposals"][0]["review_warnings"])
        self.assertEqual(source_bytes, (self.run / "proposals.json").read_bytes())
        captured = result["review_batches_binding"]["captured_rows"][0][1]
        self.assertEqual(readable + "\nPrepared review parent: " + group["review_id"]
            + "; original time preserved, not financial credit.", captured["Reason"])
        self.assertEqual([{"review_id": self.ids[0], "warnings": [warning]}],
                         json.loads(publisher.portfolio_row(group, "source-run")[12]))
        for change in (lambda cell: cell.pop("note"), lambda cell: cell.update(note="Replaced warning note")):
            with self.subTest(change=change), self.assertRaises(native.NativePostError):
                self.native_plan(document, cell_changes=lambda snapshot: change(
                    snapshot["structuredContent"]["sheets"][0]["data"][0]["rowData"][1]["values"][12]))
        changed = copy.deepcopy(document)
        changed["package"]["activities"][0]["child_review_warnings"][0]["warnings"] = []
        with self.assertRaises(native.NativePostError):
            self.native_plan(changed)
        parent = group["review_id"]
        retired = [prepared._retired(row, parent) for row in self.before_rows]
        retired[0][12] = "Prepared review parent: " + parent + "; original time preserved, not financial credit."
        with self.assertRaises(native.NativePostError):
            self.native_plan(document, rows=retired)


if __name__ == "__main__":
    unittest.main()
