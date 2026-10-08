"""Complete pending selections never leak unresolved source partitions."""
import copy
import json
from pathlib import Path
import unittest

from scripts import clockify_sheet_publish as publisher
import test_pending_review_selection as fixtures


class PendingSelectionPartitionTests(unittest.TestCase):
    def fixture(self, unresolved_index):
        fixture = fixtures.PendingSelectionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.current[unresolved_index].update(
            routing_disposition="unresolved-routing", client_project="", clockify_project_suffix="",
            tag_names=[], tag_suffixes=[], billable=False,
            review_warnings=[{"type": "unresolved_routing", "disposition": "unresolved-routing",
                              "reason_code": "no_deterministic_route"}])
        artifacts = fixture.binding["sources"]["current"]["artifacts"]
        accounting = json.loads(Path(artifacts["accounting"]["path"]).read_text())
        accounting["proposals"] = copy.deepcopy(fixture.current)
        for key, value in (("proposals", fixture.current), ("accounting", accounting),
                           ("replay_proposals", fixture.current), ("replay_accounting", accounting)):
            artifacts[key] = fixtures.write(Path(artifacts[key]["path"]), value)
        receipt = json.loads(Path(artifacts["receipt"]["path"]).read_text())
        for filename, key in (("proposals.json", "proposals"), ("work-accounting-result.json", "accounting")):
            receipt["deterministic_accounting_replay"][filename] = {
                "byte_equal": True, "primary_sha256": artifacts[key]["sha256"][7:],
                "replay_sha256": artifacts[key]["sha256"][7:]}
        artifacts["receipt"] = fixtures.write(Path(artifacts["receipt"]["path"]), receipt)
        fixtures.write(fixture.binding_path, fixture.binding)
        return fixture

    def test_selected_unresolved_outcome_publishes_once_and_native_readback_verifies(self):
        fixture = self.fixture(0)
        gateway = fixtures.SelectionGateway([publisher.HEADER, *fixture.baseline])
        result = fixture.publish(gateway)
        self.assertEqual(["August 2026 review"], [item["sheet_title"] for item in result["publications"]])
        self.assertEqual(13, result["publications"][0]["appended"])
        selected_id = publisher.stable_review_id(fixture.current[0])
        self.assertEqual(1, sum(row[0] == selected_id for row in gateway.rows[1:]))
        operator = fixture.root / "operator.json"
        fixtures.write(operator, {"readback": {"spreadsheetId": "sheet", "sheets": [{"properties": {
            "title": "August 2026 review", "sheetId": 2, "gridProperties": {"rowCount": 1000}},
            "data": [{"startRow": 0, "startColumn": 0, "rowData": [{"values": [{"effectiveValue": {
                "numberValue" if type(cell) in (int, float) else "stringValue": cell}}
                for cell in row]} for row in gateway.rows]}]}]}})
        verified = fixture.publish(None, existing_publication=operator)
        self.assertEqual("verified-existing", verified["status"])
        self.assertFalse(verified["external_writes"])
        self.assertEqual(0, verified["clockify_writes"])
        self.assertEqual(["August 2026 review"], [item["sheet_title"] for item in verified["publications"]])

    def test_unselected_unresolved_outcome_remains_covered_without_hidden_append(self):
        fixture = self.fixture(20)
        gateway = fixtures.SelectionGateway([publisher.HEADER, *fixture.baseline])
        result = fixture.publish(gateway)
        self.assertEqual(["August 2026 review"], [item["sheet_title"] for item in result["publications"]])
        self.assertEqual(13, result["publications"][0]["appended"])
        unselected_id = publisher.stable_review_id(fixture.current[20])
        self.assertFalse(any(row[0] == unselected_id for row in gateway.rows[1:]))


if __name__ == "__main__":
    unittest.main()
