import copy
import json
import unittest
from unittest import mock

from scripts import clockify_sheet_publish as publisher


def metadata():
    criteria = {
        "9": {"condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": "pending"}]}},
        "13": {"condition": {"type": "TEXT_EQ", "values": [{"userEnteredValue": "unposted"}]}},
    }
    return {"sheets": [{
        "properties": {"sheetId": 41, "gridProperties": {"rowCount": 652}},
        "basicFilter": {
            "range": {"sheetId": 41, "endRowIndex": 507, "endColumnIndex": 15},
            "sortSpecs": [{"dimensionIndex": 1, "sortOrder": "DESCENDING"}],
        },
        "filterViews": [{
            "filterViewId": 73, "title": "Pending / unposted — review",
            "range": {"sheetId": 41, "startRowIndex": 0, "endRowIndex": 652,
                      "startColumnIndex": 0, "endColumnIndex": 15},
            "criteria": criteria,
            "filterSpecs": [{"columnIndex": int(column), "filterCriteria": copy.deepcopy(value)}
                            for column, value in criteria.items()],
            "sortSpecs": [{"dimensionIndex": 1, "sortOrder": "ASCENDING"}],
        }],
    }, {"properties": {"sheetId": 99, "gridProperties": {"rowCount": 1000}}}]}


def appended_requests(start=653, end=654):
    return [{"repeatCell": {"range": {
        "sheetId": 41, "startRowIndex": start - 1, "endRowIndex": end,
        "startColumnIndex": 0, "endColumnIndex": 15,
    }, "cell": {"userEnteredFormat": {"backgroundColor": {"red": 1, "green": 1, "blue": 1}}},
        "fields": "userEnteredFormat.backgroundColor"}}, {"repeatCell": {"range": {
            "sheetId": 41, "startRowIndex": start - 1, "endRowIndex": end,
            "startColumnIndex": 3, "endColumnIndex": 4,
        }, "cell": {"userEnteredFormat": {"numberFormat": {"type": "NUMBER", "pattern": "0.######"}}},
        "fields": "userEnteredFormat.numberFormat"}}, {"setDataValidation": {"range": {
            "sheetId": 41, "startRowIndex": start - 1, "endRowIndex": end,
            "startColumnIndex": 13, "endColumnIndex": 14,
        }, "rule": {"condition": {"type": "ONE_OF_LIST", "values": [
            {"userEnteredValue": value} for value in (
                "pending", "ambiguous", "approved", "posted", "rejected", "superseded", "unposted")
        ]}, "showCustomUi": True, "strict": True}}}]


class PendingReviewFilterViewTests(unittest.TestCase):
    def prepare(self, document, *, sheet=False):
        gateway = publisher.GwsSheetsGateway()
        calls = []

        def request(arguments):
            calls.append(arguments)
            if arguments[:2] == ["spreadsheets", "get"]:
                return copy.deepcopy(document)
            self.assertEqual(["spreadsheets", "batchUpdate"], arguments[:2])
            return {}

        with mock.patch.object(gateway, "_call", side_effect=request):
            if sheet:
                gateway.prepare_sheet("spreadsheet", 41)
            else:
                gateway.prepare_new_rows("spreadsheet", 41, 653, 654)
        return calls, json.loads(calls[-1][calls[-1].index("--json") + 1])["requests"]

    def test_extends_only_end_row_of_exact_view_and_preserves_existing_requests(self):
        document = metadata()
        original = copy.deepcopy(document)
        calls, requests = self.prepare(document)
        self.assertEqual(["spreadsheets", "get"], calls[0][:2])
        self.assertEqual({"spreadsheetId": "spreadsheet", "includeGridData": False},
                         json.loads(calls[0][calls[0].index("--params") + 1]))
        self.assertEqual(appended_requests(), requests[:3])
        self.assertEqual([{"updateFilterView": {
            "filter": {"filterViewId": 73, "range": {"sheetId": 41, "endRowIndex": 654}},
            "fields": "range.endRowIndex",
        }}], requests[3:])
        self.assertEqual(original, document)

    def test_google_like_grid_routing_requires_sheet_id_even_with_narrow_field_mask(self):
        document = metadata()
        original = copy.deepcopy(document)
        gateway = publisher.GwsSheetsGateway()

        def apply_filter(update):
            target_id = update["filter"]["range"].get("sheetId", 0)
            sheets = [sheet for sheet in document["sheets"]
                      if sheet["properties"]["sheetId"] == target_id]
            if not sheets:
                raise publisher.PublicationError(f"No grid with id: {target_id}")
            self.assertEqual("range.endRowIndex", update["fields"])
            view = next(view for view in sheets[0]["filterViews"]
                        if view["filterViewId"] == update["filter"]["filterViewId"])
            view["range"]["endRowIndex"] = update["filter"]["range"]["endRowIndex"]

        with self.assertRaisesRegex(publisher.PublicationError, "No grid with id: 0"):
            apply_filter({"filter": {"filterViewId": 73, "range": {"endRowIndex": 654}},
                          "fields": "range.endRowIndex"})

        def request(arguments):
            if arguments[:2] == ["spreadsheets", "get"]:
                return copy.deepcopy(document)
            requests = json.loads(arguments[arguments.index("--json") + 1])["requests"]
            for item in requests:
                if "updateFilterView" in item:
                    apply_filter(item["updateFilterView"])
            return {}

        with mock.patch.object(gateway, "_call", side_effect=request):
            gateway.prepare_new_rows("spreadsheet", 41, 653, 654)
        expected = copy.deepcopy(original)
        expected["sheets"][0]["filterViews"][0]["range"]["endRowIndex"] = 654
        self.assertEqual(expected, document)

    def test_does_not_touch_nonmatching_or_already_large_views(self):
        for label, field, value in (
            ("title", "title", "My pending review"),
            ("id missing", "filterViewId", None),
            ("id bool", "filterViewId", True),
            ("id text", "filterViewId", "73"),
            ("id negative", "filterViewId", -1),
            ("criteria", "criteria", {}),
            ("specs", "filterSpecs", []),
            ("specs wrong", "filterSpecs", [{"columnIndex": 9, "filterCriteria": {}}]),
            ("wrong sheet", "range.sheetId", 99),
            ("wrong start row", "range.startRowIndex", 1),
            ("wrong start column", "range.startColumnIndex", 1),
            ("wrong width", "range.endColumnIndex", 14),
            ("already enough", "range.endRowIndex", 654),
            ("never shrink", "range.endRowIndex", 1000),
            ("unbounded", "range.endRowIndex", None),
        ):
            with self.subTest(label=label):
                document = metadata()
                view = document["sheets"][0]["filterViews"][0]
                target, key = (view["range"], field.split(".")[1]) if "." in field else (view, field)
                if value is None:
                    target.pop(key)
                else:
                    target[key] = value
                _, requests = self.prepare(document)
                self.assertEqual(appended_requests(), requests)
        document = metadata()
        document["sheets"][0]["properties"]["sheetId"] = 99
        _, requests = self.prepare(document)
        self.assertEqual(appended_requests(), requests)

    def test_omitted_zero_starts_and_missing_mirror_specs_are_equivalent(self):
        document = metadata()
        view = document["sheets"][0]["filterViews"][0]
        del view["range"]["startRowIndex"]
        del view["range"]["startColumnIndex"]
        del view["filterSpecs"]
        _, requests = self.prepare(document)
        self.assertEqual(73, requests[-1]["updateFilterView"]["filter"]["filterViewId"])

    def test_fresh_metadata_makes_repeated_preparation_idempotent(self):
        document = metadata()
        gateway = publisher.GwsSheetsGateway()
        batches = []
        reads = []

        def request(arguments):
            if arguments[:2] == ["spreadsheets", "get"]:
                reads.append(True)
                return copy.deepcopy(document)
            batch = json.loads(arguments[arguments.index("--json") + 1])["requests"]
            batches.append(batch)
            for item in batch:
                if "updateFilterView" in item:
                    document["sheets"][0]["filterViews"][0]["range"].update(
                        item["updateFilterView"]["filter"]["range"])
            return {}

        with mock.patch.object(gateway, "_call", side_effect=request):
            gateway.prepare_new_rows("spreadsheet", 41, 653, 654)
            gateway.prepare_new_rows("spreadsheet", 41, 653, 654)
        self.assertEqual(2, len(reads))
        self.assertEqual(4, len(batches[0]))
        self.assertEqual(appended_requests(), batches[1])

    def test_failed_preparation_retries_existing_appended_rows_without_append(self):
        document = metadata()
        gateway = publisher.GwsSheetsGateway()
        batches = []

        def request(arguments):
            if arguments[:3] == ["spreadsheets", "values", "get"]:
                return {"values": [publisher.HEADER] + [[] for _ in range(651)] + [["new-a"], ["new-b"]]}
            if arguments[:2] == ["spreadsheets", "get"]:
                return copy.deepcopy(document)
            self.assertEqual(["spreadsheets", "batchUpdate"], arguments[:2])
            batches.append(json.loads(arguments[arguments.index("--json") + 1])["requests"])
            if len(batches) == 1:
                raise publisher.PublicationError("preparation failed")
            return {}

        with mock.patch.object(gateway, "_call", side_effect=request):
            for attempt in range(2):
                if attempt == 0:
                    with self.assertRaisesRegex(publisher.PublicationError, "preparation failed"):
                        publisher._prepare_appended_rows(gateway, "spreadsheet", 41, "'Review'", 654,
                                                         frozenset({"new-a", "new-b"}))
                else:
                    publisher._prepare_appended_rows(gateway, "spreadsheet", 41, "'Review'", 654,
                                                     frozenset({"new-a", "new-b"}))
        self.assertEqual(batches[0], batches[1])
        self.assertEqual(654, batches[1][-1]["updateFilterView"]["filter"]["range"]["endRowIndex"])

    def test_prepare_sheet_preserves_basic_filter_and_formats_only_actual_grid(self):
        document = metadata()
        original = copy.deepcopy(document)
        _, requests = self.prepare(document, sheet=True)
        self.assertEqual(1, len(requests))
        self.assertEqual({"sheetId": 41, "startRowIndex": 1, "endRowIndex": 652,
                          "startColumnIndex": 3, "endColumnIndex": 4}, requests[0]["repeatCell"]["range"])
        self.assertEqual(original, document)

    def test_prepare_sheet_initializes_only_missing_basic_filter_with_grid_bound(self):
        document = metadata()
        del document["sheets"][0]["basicFilter"]
        _, requests = self.prepare(document, sheet=True)
        self.assertEqual(652, requests[0]["repeatCell"]["range"]["endRowIndex"])
        self.assertEqual({"setBasicFilter": {"filter": {"range": {
            "sheetId": 41, "startRowIndex": 0, "endRowIndex": 652,
            "startColumnIndex": 0, "endColumnIndex": 15,
        }}}}, requests[1])

    def test_prepare_sheet_retains_legacy_bound_only_when_metadata_omitted(self):
        for document in ({}, {"sheets": [{"properties": {"sheetId": 41}}]}):
            with self.subTest(document=document):
                _, requests = self.prepare(document, sheet=True)
                self.assertEqual(1000, requests[0]["repeatCell"]["range"]["endRowIndex"])

    def test_prepare_sheet_fails_before_batch_for_missing_target_or_invalid_explicit_grid(self):
        documents = [{"sheets": []}, {"sheets": [{"properties": {"sheetId": 99}}]}]
        for value in (0, -1, True, "652", None):
            document = metadata()
            document["sheets"][0]["properties"]["gridProperties"]["rowCount"] = value
            documents.append(document)
        for document in documents:
            with self.subTest(document=document):
                gateway = publisher.GwsSheetsGateway()
                with mock.patch.object(gateway, "_call", return_value=document) as request:
                    with self.assertRaises(publisher.PublicationError):
                        gateway.prepare_sheet("spreadsheet", 41)
                self.assertEqual(1, request.call_count)


if __name__ == "__main__":
    unittest.main()
