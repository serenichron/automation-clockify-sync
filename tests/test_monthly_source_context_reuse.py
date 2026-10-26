"""Synthetic monthly aliases retain source checks without per-row ledger scans."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import clockify_monthly_unresolved as monthly
from scripts import clockify_sheet_publish as publisher
from scripts import clockify_review_cycle as cycle
from scripts import evidence_ledger
from test_monthly_unresolved import frozen_run, MonthlyGateway


class MonthlySourceContextReuseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.old = self.root / "old"
        self.current = self.root / "current"
        frozen_run(self.old, 5)
        frozen_run(self.current, 6)
        self.old_rows = monthly.project_rows(self.old)
        self.rows = monthly.project_rows(self.current)
        identities = {row[0] for row in self.rows}
        self.retained = [row for row in self.old_rows if row[0] in identities]
        self.gateway = MonthlyGateway([monthly.HEADER, *copy.deepcopy(self.retained)])

    def plan(self):
        return publisher._plan_monthly_unresolved(self.gateway, spreadsheet_id="test",
            sheet_title="September 2026 unresolved evidence", rows=self.rows, source_dir=self.current)

    def spy(self):
        return mock.patch.object(evidence_ledger.EvidenceLedger, "validate", autospec=True,
            side_effect=evidence_ledger.EvidenceLedger.validate)

    def expected_aliases(self):
        # Independent literal contract: original row provenance, machine cells, ledger bytes.
        ledger_digest = "sha256:" + hashlib.sha256((self.old / "evidence/evidence-ledger.json").read_bytes()).hexdigest()
        return [{"stable_evidence_id": row[0], "source_run_id": "old",
                 "historical_provenance": row[11],
                 "preserved_machine_digest": "sha256:" + hashlib.sha256(json.dumps(
                     [*row[:10], row[11]], sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                 "historical_ledger_digest": ledger_digest}
                for row in self.retained]

    def test_planner_validates_each_source_once_not_once_per_alias(self):
        # Catches rebuilding/validating whole ledgers for every retained row.
        with self.spy() as validation:
            plan = self.plan()
        self.assertEqual(self.expected_aliases(), plan["canonical_aliases"])
        self.assertEqual(2, validation.call_count)

    def test_independent_consumer_validates_each_source_once(self):
        aliases = self.expected_aliases()
        with self.spy() as validation:
            actual = monthly.validate_canonical_aliases(self.current, self.rows, aliases)
        self.assertEqual(aliases, actual)
        self.assertEqual(2, validation.call_count)

    def test_prewrite_rechecks_source_bytes_after_planning(self):
        # Catches invocation reuse hiding source changes between planning and append.
        for root, relative in ((self.old, "semantic-analysis.json"),
                               (self.current, "evidence/evidence-ledger.json")):
            with self.subTest(relative=relative):
                plan = self.plan()
                path = root / relative
                before = path.read_bytes()
                try:
                    path.write_bytes(before + b" ")
                    with self.assertRaises(publisher.PublicationError):
                        publisher._apply_monthly_unresolved(self.gateway, spreadsheet_id="test", plan=plan)
                    self.assertEqual([], self.gateway.writes)
                finally:
                    path.write_bytes(before)

    def test_receipt_validation_reuses_its_own_context_across_projection_and_aliases(self):
        result = publisher.publish_monthly_unresolved(self.gateway, spreadsheet_id="test",
            sheet_title="September 2026 unresolved evidence", rows=self.rows, source_dir=self.current)
        expected = [cycle._publication_receipt(spreadsheet_id="test",
            sheet_title="September 2026 unresolved evidence", rows=self.rows)]
        document = {"schema_version": "sheet-publication-result/v1", "status": "published",
                    "external_writes": True, "clockify_writes": 0, "publications": [result]}
        with self.spy() as validation:
            actual = cycle._validated_publication_document(document, expected, source_dir=self.current)
        self.assertEqual(self.expected_aliases(), actual[0]["canonical_source_aliases"])
        self.assertEqual(2, validation.call_count)

    def test_explicit_handle_manifest_changes_fail_prewrite(self):
        ledger = self.old / "evidence/evidence-ledger.json"
        packet = self.root / "historical-sources.json"
        packet.write_text(json.dumps({"schema_version": monthly.HISTORICAL_SOURCES_SCHEMA,
            "sources": [{"source_run_id": "old", "source_ledger": {"path": str(ledger),
                "sha256": "sha256:" + hashlib.sha256(ledger.read_bytes()).hexdigest()}}]}))
        with self.spy() as validation:
            plan = publisher._plan_monthly_unresolved(self.gateway, spreadsheet_id="test",
                sheet_title="September 2026 unresolved evidence", rows=self.rows, source_dir=self.current,
                historical_sources=packet)
        self.assertEqual(2, validation.call_count)
        packet.write_text(packet.read_text() + " ")
        with self.assertRaises(publisher.PublicationError):
            publisher._apply_monthly_unresolved(self.gateway, spreadsheet_id="test", plan=plan)
        self.assertEqual([], self.gateway.writes)

    def test_each_invocation_reopens_source_and_rejects_changed_unrelated_event(self):
        for source in (self.old, self.current):
            with self.subTest(source=source.name):
                self.plan()
                path = source / "evidence/evidence-ledger.json"
                before = path.read_bytes()
                document = json.loads(before)
                document["events"][-1]["attributes"]["title"] = "tampered unrelated evidence"
                try:
                    path.write_text(json.dumps(document))
                    with self.assertRaises(publisher.PublicationError):
                        self.plan()
                    with self.assertRaises(ValueError):
                        monthly.validate_canonical_aliases(self.current, self.rows, self.expected_aliases())
                    self.assertEqual([], self.gateway.writes)
                finally:
                    path.write_bytes(before)

    def test_later_alias_identity_citations_and_metadata_still_fail_closed(self):
        for defect in ("machine", "citations", "metadata", "duplicate"):
            with self.subTest(defect=defect):
                aliases = self.expected_aliases()
                requested = copy.deepcopy(self.rows)
                if defect == "machine":
                    self.gateway.rows[-1][5] = "changed project"
                    with self.assertRaises(publisher.PublicationError):
                        self.plan()
                    self.gateway.rows[-1] = copy.deepcopy(self.retained[-1])
                    continue
                if defect == "citations":
                    identity = aliases[-1]["stable_evidence_id"]
                    row = next(row for row in requested if row[0] == identity)
                    provenance = json.loads(row[11])
                    provenance["evidence_ids"] = ["unbound-evidence"]
                    row[11] = json.dumps(provenance)
                elif defect == "metadata":
                    aliases[-1]["historical_ledger_digest"] = "sha256:" + "0" * 64
                else:
                    aliases[-1] = copy.deepcopy(aliases[0])
                with self.assertRaises(ValueError):
                    monthly.validate_canonical_aliases(self.current, requested, aliases)

    def test_context_derived_views_are_immutable_and_do_not_leak_source_objects(self):
        context = monthly.MonthlySourceContext()
        index = context.row_index(self.old)
        events, _, _ = context.ledger(self.old)
        with self.assertRaises(TypeError):
            index[self.retained[0][0]][5] = "mutable row"
        with self.assertRaises(TypeError):
            next(iter(events.values()))["attributes"]["title"] = "mutable event"
        self.assertEqual(tuple(self.retained[0]), index[self.retained[0][0]])

    def test_prewrite_rejects_relocated_source_symlink_even_with_identical_bytes(self):
        plan = self.plan()
        moved = self.root / "relocated-old"
        self.old.rename(moved)
        self.old.symlink_to(moved, target_is_directory=True)
        with self.assertRaises(publisher.PublicationError):
            publisher._apply_monthly_unresolved(self.gateway, spreadsheet_id="test", plan=plan)
        self.assertEqual([], self.gateway.writes)

    def test_unrelated_valid_replacement_event_still_requires_full_manifest_validation(self):
        path = self.current / "evidence/evidence-ledger.json"
        document = json.loads(path.read_bytes())
        document["events"][-1] = evidence_ledger.evidence_event(
            "multica", {"id": "replacement-unrelated"}).document()
        path.write_text(json.dumps(document))
        with self.assertRaises(publisher.PublicationError):
            self.plan()
        self.assertEqual([], self.gateway.writes)

    def test_legacy_only_consumer_rechecks_source_before_returning(self):
        # Catches cached legacy projections returning success after readback-time drift.
        gateway = MonthlyGateway([monthly.LEGACY_HEADER,
            monthly.rows_for_layout(self.rows[:1], monthly.LEGACY_LAYOUT)[0]])
        result = publisher.publish_monthly_unresolved(gateway, spreadsheet_id="test",
            sheet_title="September 2026 unresolved evidence", rows=self.rows, source_dir=self.current)
        self.assertNotIn("canonical_source_aliases", result)
        expected = [cycle._publication_receipt(spreadsheet_id="test",
            sheet_title="September 2026 unresolved evidence", rows=self.rows)]
        document = {"schema_version": "sheet-publication-result/v1", "status": "published",
                    "external_writes": True, "clockify_writes": 0, "publications": [result]}
        validate_readback = monthly.validate_target_readback

        def readback_then_drift(*args, **kwargs):
            validate_readback(*args, **kwargs)
            path = self.current / "run-report.json"
            path.write_text(path.read_text() + " ")

        with mock.patch.object(monthly, "validate_target_readback", side_effect=readback_then_drift):
            with self.assertRaises(cycle.CycleError):
                cycle._validated_publication_document(document, expected, source_dir=self.current)


if __name__ == "__main__":
    unittest.main()
