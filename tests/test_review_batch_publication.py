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


if __name__ == "__main__":
    unittest.main()
