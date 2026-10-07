"""Explicit original-source admission across runs roots; no external services."""
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest

from scripts import clockify_monthly_unresolved as monthly
from scripts import clockify_sheet_publish as publisher
from scripts import clockify_review_cycle as cycle
from test_monthly_unresolved import frozen_run, MonthlyGateway


def write(path, document):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def handle(path):
    return {"path": str(path), "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}


class MonthlyCrossRootAliasTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.historical = self.root / "original-runs" / "first"
        self.current = self.root / "repaired-runs" / "second"
        frozen_run(self.historical, 1, meeting_link=True)
        frozen_run(self.current, 2, meeting_link=True)
        write(self.current / "run-report.json", {"date_range": {
            "since": "2026-09-10T21:00:00Z", "until": "2026-09-12T21:00:00Z"}})
        write(self.current / "routing.json", {})
        self.historical_rows = monthly.project_rows(self.historical)
        self.current_rows = monthly.project_rows(self.current)
        current_ids = {row[0] for row in self.current_rows}
        self.retained = next(row for row in self.historical_rows if row[0] in current_ids and row[3] == "low_confidence")
        self.packet = {"schema_version": "monthly-unresolved-historical-sources/v1", "sources": [{
            "source_run_id": self.historical.name,
            "source_ledger": handle(self.historical / "evidence/evidence-ledger.json"),
        }]}
        self.path = self.root / "historical-sources.json"
        write(self.path, self.packet)

    def publish(self, gateway, **kwargs):
        try:
            return publisher.publish_monthly_unresolved(
                gateway, spreadsheet_id="test", sheet_title="September 2026 unresolved evidence",
                rows=self.current_rows, source_dir=self.current, historical_sources=self.path, **kwargs,
            )
        except TypeError as exc:
            if "unexpected keyword argument" in str(exc):
                self.fail("missing explicit historical source admission")
            raise

    def gateway(self, layout=None):
        row = monthly.rows_for_layout([self.retained], layout)[0]
        row[10] = "Human confirmation remains private"
        return MonthlyGateway([monthly.LEGACY_HEADER if layout else monthly.HEADER, row])

    def test_original_cross_root_source_preserves_old_row_and_appends_only_new_evidence(self):
        gateway = self.gateway()
        before = copy.deepcopy(gateway.rows)
        original = {path: path.read_bytes() for path in self.historical.rglob("*.json")}
        result = self.publish(gateway)
        self.assertEqual(before, gateway.rows[:2])
        self.assertEqual(3, result["appended"])
        self.assertEqual(1, result["unchanged"])
        self.assertEqual(1, len(result["canonical_source_aliases"]))
        self.assertEqual(handle(self.path), result["historical_sources"])
        alias = result["canonical_source_aliases"][0]
        self.assertEqual(self.packet["sources"][0]["source_ledger"], alias["historical_source"])
        self.assertEqual(original, {path: path.read_bytes() for path in original})

    def test_legacy_layout_preserves_first_machine_cells_and_human_k(self):
        gateway = self.gateway(monthly.LEGACY_LAYOUT)
        before = copy.deepcopy(gateway.rows)
        result = self.publish(gateway)
        self.assertEqual(before, gateway.rows[:2])
        self.assertEqual(monthly.LEGACY_LAYOUT, result["monthly_layout"])
        self.assertEqual(3, result["appended"])

    def test_no_handle_keeps_cross_root_conflict_fail_closed(self):
        gateway = self.gateway()
        with self.assertRaises(publisher.PublicationError):
            publisher.publish_monthly_unresolved(
                gateway, spreadsheet_id="test", sheet_title="September 2026 unresolved evidence",
                rows=self.current_rows, source_dir=self.current,
            )
        self.assertEqual([], gateway.writes)

    def test_tampered_missing_ambiguous_or_relabelled_source_fails_before_writes(self):
        for mode in ("digest", "name", "missing", "ambiguous", "symlink"):
            with self.subTest(mode=mode):
                packet = copy.deepcopy(self.packet)
                source = packet["sources"][0]
                if mode == "digest":
                    source["source_ledger"]["sha256"] = "sha256:" + "0" * 64
                elif mode == "name":
                    source["source_run_id"] = "relabelled"
                elif mode == "missing":
                    source["source_ledger"]["path"] = str(self.root / "absent/evidence/evidence-ledger.json")
                elif mode == "ambiguous":
                    packet["sources"].append(copy.deepcopy(source))
                else:
                    link = self.root / "symlinked-original"
                    link.symlink_to(self.historical, target_is_directory=True)
                    source["source_ledger"]["path"] = str(link / "evidence/evidence-ledger.json")
                write(self.path, packet)
                gateway = self.gateway()
                before = copy.deepcopy(gateway.rows)
                with self.assertRaises(publisher.PublicationError):
                    self.publish(gateway)
                self.assertEqual(before, gateway.rows)
                self.assertEqual([], gateway.writes)

    def test_original_artifact_drift_is_rejected_even_when_ledger_handle_still_matches(self):
        path = self.historical / "semantic-analysis.json"
        path.write_text(path.read_text() + " ")
        gateway = self.gateway()
        with self.assertRaises(publisher.PublicationError):
            self.publish(gateway)
        self.assertEqual([], gateway.writes)

    def test_receipt_consumer_reopens_same_explicit_proof_and_rejects_forged_locator(self):
        for layout in (None, monthly.LEGACY_LAYOUT):
            with self.subTest(layout=layout):
                result = self.publish(self.gateway(layout))
                config = {"spreadsheet_id": "test", "monthly_unresolved_historical_sources": str(self.path)}
                stage = {"run_id": self.current.name, "run_dir": str(self.current)}
                expected = cycle._expected_publication_receipts(config, stage,
                    sheet_title="September 2026 portfolio review")
                document = {"schema_version": "sheet-publication-result/v1", "status": "published",
                            "external_writes": True, "clockify_writes": 0, "publications": [result]}
                validated = cycle._validated_publication_document(document, expected, source_dir=self.current)
                self.assertEqual(result["canonical_source_aliases"], validated[0]["canonical_source_aliases"])
                forged = copy.deepcopy(document)
                forged["publications"][0]["canonical_source_aliases"][0]["historical_source"]["path"] = str(self.root / "wrong/evidence/evidence-ledger.json")
                with self.assertRaises(cycle.CycleError):
                    cycle._validated_publication_document(forged, expected, source_dir=self.current)

    def test_receipt_keeps_original_manifest_when_recurring_config_changes(self):
        result = self.publish(self.gateway())
        config = {"spreadsheet_id": "test", "monthly_unresolved_historical_sources": "/missing/new-config.json"}
        document = {"publication_receipts": [result]}
        restored = cycle._receipt_publication_config(config, document)
        self.assertEqual(str(self.path), restored["monthly_unresolved_historical_sources"])
        old = cycle._receipt_publication_config(config, {"publication_receipts": []})
        self.assertNotIn("monthly_unresolved_historical_sources", old)
        packet = copy.deepcopy(self.packet)
        packet["sources"][0]["source_run_id"] = "rewritten"
        write(self.path, packet)
        with self.assertRaises(cycle.CycleError):
            cycle._receipt_publication_config(config, document)

    def test_real_no_write_cli_validates_manifest_before_returning_preview(self):
        from test_sheet_publish import proposal
        source_proposals = json.loads((self.current / "proposals.json").read_text())
        proposals = [{**proposal(i + 1), **row, "client_project": "", "clockify_project_suffix": "",
                      "tag_suffixes": [], "tag_names": [], "billable": False, "duration_minutes": 5,
                      "review_warnings": [{"type": "unresolved_routing", "disposition": "unresolved-routing",
                                            "reason_code": "no_deterministic_route"}]}
                     for i, row in enumerate(source_proposals)]
        accounting = json.loads((self.current / "work-accounting-result.json").read_text())
        accounting["proposals"] = proposals
        write(self.current / "proposals.json", proposals)
        write(self.current / "work-accounting-result.json", accounting)
        write(self.current / "quality_report.json", {"status": "pass", "summary": {"total_proposals": 2}})
        replay = self.root / "replay"
        provenance = {"source_run_id": self.current.name, "source_run_dir": str(self.current)}
        for relative, field in (("evidence/evidence-ledger.json", "ledger_file_sha256"),
                ("semantic-analysis.json", "semantic_analysis_sha256"),
                ("work-accounting-result.json", "work_accounting_result_sha256")):
            content = (self.current / relative).read_bytes()
            target = replay / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
            provenance[field] = hashlib.sha256(content).hexdigest()
        write(replay / "replay-source.json", provenance)
        write(replay / "replay-integrity.json", {"status": "pass", "failures": [], "source_run_id": self.current.name,
            "work_accounting_result": {"file_sha256": provenance["work_accounting_result_sha256"]},
            "reconciliation_binding": {"routing_sha256": handle(self.current / "routing.json")["sha256"]}})
        command = [sys.executable, str(Path(publisher.__file__).resolve()),
            "--spreadsheet-id", "test", "--sheet-title", "September 2026 portfolio review",
            "--proposals", str(self.current / "proposals.json"),
            "--quality-report", str(self.current / "quality_report.json"),
            "--replay-integrity", str(replay / "replay-integrity.json"),
            "--routing-snapshot", str(self.current / "routing.json"), "--run-id", self.current.name,
            "--monthly-unresolved", "--monthly-unresolved-historical-sources", str(self.path)]
        result = subprocess.run(command, cwd=self.root, capture_output=True, text=True)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("dry_run", json.loads(result.stdout)["status"])
        packet = copy.deepcopy(self.packet)
        packet["sources"][0]["source_ledger"]["sha256"] = "sha256:" + "0" * 64
        write(self.path, packet)
        result = subprocess.run(command, cwd=self.root, capture_output=True, text=True)
        self.assertNotEqual(0, result.returncode)
        self.assertNotIn('"status": "dry_run"', result.stdout)


if __name__ == "__main__":
    unittest.main()
