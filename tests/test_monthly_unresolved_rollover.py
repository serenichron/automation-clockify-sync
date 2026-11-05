"""A new month's unresolved tab is opened from the previous month without copying its rows."""
import copy
import json
import unittest
from unittest import mock

from scripts import clockify_sheet_publish as publisher
from scripts import clockify_monthly_unresolved as monthly

OCTOBER = "October 2026 unresolved evidence"
NOVEMBER = "November 2026 unresolved evidence"


def row(identity: str) -> list[str]:
    return [identity, "2026-11-01", "test", "low_confidence", "unknown interval", "", "summary",
            "", "quality pass", "review", "needs_review", "sha256:test"]


class TabsGateway:
    """Multi-tab fake: values are lists of rows; row 1 is the header."""

    def __init__(self, tabs: dict[str, dict]):
        self.tabs = tabs
        self.calls: list[tuple] = []

    def spreadsheet(self, _id):
        return {"sheets": [{"properties": {
            "sheetId": tab["id"], "title": title, "hidden": tab.get("hidden", False),
            "gridProperties": {"rowCount": tab.get("grid", 1000), "columnCount": 12}}}
            for title, tab in self.tabs.items()]}

    def _title(self, range_name: str) -> str:
        return range_name.split("!")[0].strip("'").replace("''", "'")

    def values(self, _id, range_name):
        return [list(r) for r in self.tabs[self._title(range_name)]["rows"]]

    def _by_id(self, sheet_id):
        return next(tab for tab in self.tabs.values() if tab["id"] == sheet_id)

    def open_monthly_tab(self, _id, template_sheet_id, title, grid_rows):
        """Atomic: the new tab never exists with the template's data rows."""
        self.calls.append(("open", template_sheet_id, title, grid_rows))
        source = self._by_id(template_sheet_id)
        new_id = max(tab["id"] for tab in self.tabs.values()) + 1
        self.tabs[title] = {**copy.deepcopy(source), "id": new_id, "hidden": False}
        self.tabs[title]["rows"] = self.tabs[title]["rows"][:1]
        return new_id

    def append_monthly_rows(self, _id, sheet_id, start_row, grid_rows, rows, *, exemplar_row=None):
        self.calls.append(("append", sheet_id, start_row, exemplar_row, len(rows)))
        tab = self._by_id(sheet_id)
        assert len(tab["rows"]) + 1 == start_row
        tab["rows"].extend([list(r) for r in rows])


class MonthlyRolloverTests(unittest.TestCase):
    def october(self, **extra):
        return {"id": 11, "rows": [list(monthly.HEADER), row("uev-oct-1"), row("uev-oct-2")], **extra}

    def publish(self, gateway, rows):
        return publisher.publish_monthly_unresolved(
            gateway, spreadsheet_id="sheet", sheet_title=NOVEMBER, rows=rows)

    def test_new_month_tab_is_opened_from_previous_month_with_only_new_rows(self):
        gateway = TabsGateway({OCTOBER: self.october()})
        october_before = copy.deepcopy(gateway.tabs[OCTOBER])
        rows = [row("uev-nov-1"), row("uev-nov-2"), row("uev-nov-3")]

        result = self.publish(gateway, rows)

        self.assertEqual(3, result["appended"])
        new_id = gateway.tabs[NOVEMBER]["id"]
        self.assertEqual([
            ("open", 11, NOVEMBER, 1000),
            ("append", new_id, 2, 2, 3),
        ], gateway.calls)
        self.assertEqual([list(monthly.HEADER), *rows], gateway.tabs[NOVEMBER]["rows"])
        self.assertEqual(october_before, gateway.tabs[OCTOBER])

    def test_new_month_keeps_the_previous_month_legacy_layout(self):
        gateway = TabsGateway({OCTOBER: {"id": 11, "rows": [list(monthly.LEGACY_HEADER), row("uev-oct-1")]}})
        linked = row("uev-nov-1")
        linked[7] = "https://fathom.video/share/example"

        result = self.publish(gateway, [linked])

        self.assertEqual(1, result["appended"])
        written = gateway.tabs[NOVEMBER]["rows"]
        self.assertEqual(list(monthly.LEGACY_HEADER), written[0])
        self.assertEqual("", written[1][7])
        self.assertTrue(written[1][9].endswith("; Meeting linkage: https://fathom.video/share/example"))

    def test_header_only_tab_from_an_interrupted_bootstrap_is_filled_from_row_two(self):
        gateway = TabsGateway({OCTOBER: self.october(),
                               NOVEMBER: {"id": 12, "rows": [list(monthly.HEADER)]}})

        result = self.publish(gateway, [row("uev-nov-1")])

        self.assertEqual(1, result["appended"])
        self.assertEqual([("append", 12, 2, 2, 1)], gateway.calls)

    def test_retry_after_rollover_appends_nothing(self):
        gateway = TabsGateway({OCTOBER: self.october()})
        rows = [row("uev-nov-1")]
        self.publish(gateway, rows)
        calls = len(gateway.calls)

        self.assertEqual(0, self.publish(gateway, rows)["appended"])
        self.assertEqual(calls, len(gateway.calls))

    def test_missing_hidden_or_foreign_template_fails_before_any_write(self):
        cases = {
            "missing": {},
            "hidden": {OCTOBER: self.october(hidden=True)},
            "legacy header": {OCTOBER: {"id": 11, "rows": [["Evidence ID", "Local day"], row("uev-oct-1")]}},
            "no data row": {OCTOBER: {"id": 11, "rows": [list(monthly.HEADER)]}},
        }
        for name, tabs in cases.items():
            with self.subTest(name):
                gateway = TabsGateway(tabs)
                with self.assertRaisesRegex(publisher.PublicationError, "template"):
                    self.publish(gateway, [row("uev-nov-1")])
                self.assertEqual([], gateway.calls)
                self.assertNotIn(NOVEMBER, gateway.tabs)

    def test_month_without_unresolved_rows_creates_no_tab(self):
        gateway = TabsGateway({OCTOBER: self.october()})

        result = self.publish(gateway, [])

        self.assertEqual(0, result["appended"])
        self.assertEqual([], gateway.calls)
        self.assertNotIn(NOVEMBER, gateway.tabs)

    def test_january_rolls_over_from_previous_december(self):
        december = "December 2026 unresolved evidence"
        gateway = TabsGateway({december: {"id": 21, "rows": [list(monthly.HEADER), row("uev-dec")]}})

        publisher.publish_monthly_unresolved(
            gateway, spreadsheet_id="sheet", sheet_title="January 2027 unresolved evidence",
            rows=[row("uev-jan")])

        self.assertEqual("open", gateway.calls[0][0])
        self.assertEqual(21, gateway.calls[0][1])
        self.assertEqual("January 2027 unresolved evidence", gateway.calls[0][2])


class NativeGatewayRolloverTests(unittest.TestCase):
    def requests(self, call):
        return json.loads(call.call_args.args[0][-1])["requests"]

    def test_open_is_one_atomic_batch_that_duplicates_then_empties_the_new_tab(self):
        metadata = {"sheets": [{"properties": {"sheetId": 11, "title": OCTOBER}}]}

        def native(arguments):
            if "batchUpdate" in arguments:
                requests = json.loads(arguments[-1])["requests"]
                chosen = requests[0]["duplicateSheet"]["newSheetId"]
                return {"replies": [{"duplicateSheet": {"properties": {"sheetId": chosen}}}]}
            return metadata

        with mock.patch.object(publisher.GwsSheetsGateway, "_call", side_effect=native) as call:
            sheet_id = publisher.GwsSheetsGateway().open_monthly_tab("sheet", 11, NOVEMBER, 400)
        batches = [c.args[0] for c in call.call_args_list if "batchUpdate" in c.args[0]]
        self.assertEqual(1, len(batches))
        requests = json.loads(batches[0][-1])["requests"]
        duplicate = requests[0]["duplicateSheet"]
        self.assertEqual((11, NOVEMBER, sheet_id),
                         (duplicate["sourceSheetId"], duplicate["newSheetName"], duplicate["newSheetId"]))
        self.assertNotEqual(11, sheet_id)
        self.assertTrue(0 <= sheet_id < 2 ** 31)
        fields = []
        for request in requests[1:]:
            operation = next(iter(request.values()))
            target = operation["range"]
            self.assertEqual((sheet_id, 1, 400, 0, 12), (
                target["sheetId"], target["startRowIndex"], target["endRowIndex"],
                target["startColumnIndex"], target["endColumnIndex"]))
            fields.append(operation["fields"])
        self.assertEqual(["userEnteredFormat.textFormat.link", "userEnteredValue"], sorted(fields))

    def test_open_fails_closed_when_google_reports_another_sheet_id(self):
        def native(arguments):
            if "batchUpdate" in arguments:
                return {"replies": [{"duplicateSheet": {"properties": {"sheetId": 999999}}}]}
            return {"sheets": [{"properties": {"sheetId": 11, "title": OCTOBER}}]}

        with mock.patch.object(publisher.GwsSheetsGateway, "_call", side_effect=native):
            with self.assertRaises(publisher.PublicationError):
                publisher.GwsSheetsGateway().open_monthly_tab("sheet", 11, NOVEMBER, 400)

    def test_append_can_take_its_format_from_row_two(self):
        with mock.patch.object(publisher.GwsSheetsGateway, "_call", return_value={}) as call:
            publisher.GwsSheetsGateway().append_monthly_rows(
                "sheet", 12, 2, 400, [row("uev-nov-1")], exemplar_row=2)
        requests = self.requests(call)
        copies = [r["copyPaste"] for r in requests if "copyPaste" in r]
        self.assertTrue(copies)
        for operation in copies:
            self.assertEqual(1, operation["source"]["startRowIndex"])
            self.assertEqual(2, operation["source"]["endRowIndex"])
            self.assertEqual(1, operation["destination"]["startRowIndex"])

    def test_header_is_never_a_format_exemplar(self):
        with mock.patch.object(publisher.GwsSheetsGateway, "_call", return_value={}):
            with self.assertRaisesRegex(publisher.PublicationError, "exemplar"):
                publisher.GwsSheetsGateway().append_monthly_rows(
                    "sheet", 12, 2, 400, [row("uev-nov-1")])


if __name__ == "__main__":
    unittest.main()
