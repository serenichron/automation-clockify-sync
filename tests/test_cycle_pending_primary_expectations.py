"""Cycle expectations agree with native sole-primary pending publication."""
import json
from pathlib import Path
import tempfile
import unittest

from scripts import clockify_monthly_unresolved as monthly
from scripts import clockify_review_cycle as cycle
from scripts import clockify_sheet_publish as publisher
import test_monthly_unresolved as monthly_fixtures
import test_pending_review_selection as pending_fixtures
import test_pending_selection_publication_partition as partition_fixtures
import test_sheet_publish as sheet_fixtures


def unresolved(proposal):
    proposal.update(
        routing_disposition="unresolved-routing", client_project="", clockify_project_suffix="",
        tag_names=[], tag_suffixes=[], billable=False,
        review_warnings=[{"type": "unresolved_routing", "disposition": "unresolved-routing",
                          "reason_code": "no_deterministic_route"}])
    return proposal


class CyclePendingPrimaryExpectationTests(unittest.TestCase):
    def pending_fixture(self, index):
        builder = partition_fixtures.PendingSelectionPartitionTests()
        self.addCleanup(builder.doCleanups)
        fixture = builder.fixture(index)
        artifacts = fixture.binding["sources"]["current"]["artifacts"]
        accounting = json.loads(Path(artifacts["accounting"]["path"]).read_text())
        accounting.update(schema_version=1, allocation_mode="non_overlapping_v1", ambiguous=[], skipped=[])
        for key in ("accounting", "replay_accounting"):
            artifacts[key] = pending_fixtures.write(Path(artifacts[key]["path"]), accounting)
        receipt = json.loads(Path(artifacts["receipt"]["path"]).read_text())
        receipt["deterministic_accounting_replay"]["work-accounting-result.json"] = {
            "byte_equal": True, "primary_sha256": artifacts["accounting"]["sha256"][7:],
            "replay_sha256": artifacts["replay_accounting"]["sha256"][7:]}
        artifacts["receipt"] = pending_fixtures.write(Path(artifacts["receipt"]["path"]), receipt)
        pending_fixtures.write(fixture.binding_path, fixture.binding)
        return fixture

    def assert_pending_native_delivery(self, index, *, selected):
        fixture = self.pending_fixture(index)
        gateway = pending_fixtures.SelectionGateway([publisher.HEADER, *fixture.baseline])
        actual = {"status": "published", "external_writes": True, **fixture.publish(gateway)}
        self.assertEqual(["August 2026 review"], [item["sheet_title"] for item in actual["publications"]])
        self.assertEqual(13, actual["publications"][0]["appended"])
        review_id = publisher.stable_review_id(fixture.current[index])
        self.assertEqual(int(selected), sum(row[0] == review_id for row in gateway.rows[1:]))
        expected = cycle._expected_publication_receipts(
            {"spreadsheet_id": "sheet", "pending_review_selection": str(fixture.binding_path)},
            {"run_dir": str(fixture.current_dir), "run_id": fixture.current_dir.name},
            sheet_title="August 2026 review", publication_profile=None)
        self.assertEqual(["August 2026 review"], [item["sheet_title"] for item in expected])
        # Native validation must accept the real sole-primary publisher result,
        # even when the complete source includes an unresolved ordinary partition.
        validated = cycle._validated_publication_document(actual, expected, source_dir=fixture.current_dir)
        self.assertEqual(["August 2026 review"], [item["sheet_title"] for item in validated])
        self.assertEqual(34, len(validated[0]["row_ids"]))

    def test_selected_unresolved_native_delivery_has_only_requested_primary(self):
        self.assert_pending_native_delivery(0, selected=True)

    def test_unselected_unresolved_native_delivery_has_no_hidden_destination(self):
        self.assert_pending_native_delivery(20, selected=False)

    def test_ordinary_native_publication_keeps_two_primary_destinations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proposals = [sheet_fixtures.proposal(), unresolved(sheet_fixtures.proposal(2))]
            for name, value in {
                "proposals.json": proposals, "routing.json": {},
                "work-accounting-result.json": {"schema_version": 1, "allocation_mode": "non_overlapping_v1",
                    "proposals": proposals, "ambiguous": [], "skipped": []},
            }.items():
                pending_fixtures.write(root / name, value)
            actual = publisher.publish_proposal_partitions(sheet_fixtures.MultiSheetGateway(),
                spreadsheet_id="sheet", sheet_title="August 2026 review", template_title="Proposals",
                proposals=proposals, run_id=root.name, project_allowlist={}, source_dir=root)
            actual = {"status": "published", "external_writes": True, **actual}
            expected = cycle._expected_publication_receipts({"spreadsheet_id": "sheet"},
                {"run_dir": str(root), "run_id": root.name}, sheet_title="August 2026 review",
                publication_profile=None)
            validated = cycle._validated_publication_document(actual, expected, source_dir=root)
            self.assertEqual(["August 2026 review", "unresolved-evidence"],
                             [item["sheet_title"] for item in validated])
            self.assertEqual([1, 1], [len(item["row_ids"]) for item in validated])

    def test_explicit_monthly_diagnostics_remain_source_native(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            documents = monthly_fixtures.frozen_run(root, ambiguous_count=1)
            proposals = [unresolved({**sheet_fixtures.proposal(index + 1), **item})
                         for index, item in enumerate(documents["proposals.json"])]
            accounting = documents["work-accounting-result.json"]
            accounting["proposals"] = proposals
            for name, value in {"proposals.json": proposals, "work-accounting-result.json": accounting,
                                "routing.json": {}}.items():
                pending_fixtures.write(root / name, value)
            rows = monthly.project_rows(root)
            actual = publisher.publish_proposal_partitions(
                monthly_fixtures.MonthlyGateway([monthly.HEADER, ["uev-exemplar"] + [""] * 11]),
                spreadsheet_id="sheet",
                sheet_title="September 2026 portfolio review", template_title="Proposals",
                proposals=proposals, run_id=root.name, project_allowlist={}, source_dir=root,
                monthly_rows=rows)
            actual = {"status": "published", "external_writes": True, **actual}
            expected = cycle._expected_publication_receipts({"spreadsheet_id": "sheet"},
                {"run_dir": str(root), "run_id": root.name}, sheet_title="September 2026 portfolio review",
                publication_profile=monthly.PROFILE)
            validated = cycle._validated_publication_document(actual, expected, source_dir=root)
            self.assertEqual(["September 2026 unresolved evidence"],
                             [item["sheet_title"] for item in validated])
            self.assertEqual(3, len(validated[0]["row_ids"]))


if __name__ == "__main__":
    unittest.main()
