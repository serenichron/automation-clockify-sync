"""Synthetic subprocess-boundary checks; never invoke gws or Sheets."""
import errno
import json
import unittest
from unittest import mock

from scripts import clockify_sheet_publish as publisher
from scripts import clockify_monthly_unresolved as monthly


class SheetProcess:
    def __init__(self, fail_batch=None):
        self.rows = [list(monthly.HEADER), ["uev-exemplar"] + [""] * 11]
        self.grid_rows = 3
        self.bodies = []
        self.fail_batch = fail_batch

    def run(self, arguments, **_kwargs):
        if "--json" in arguments:
            encoded = arguments[arguments.index("--json") + 1]
            if len(encoded.encode("utf-8")) >= 128 * 1024:
                raise OSError(errno.E2BIG, "Argument list too long")
            if self.fail_batch == len(self.bodies) + 1:
                self.fail_batch = None
                return mock.Mock(returncode=1, stdout="", stderr="synthetic failure")
            body = json.loads(encoded)
            self.bodies.append(encoded)
            for request in body["requests"]:
                if "appendDimension" in request:
                    self.grid_rows += request["appendDimension"]["length"]
                if "updateCells" in request:
                    update = request["updateCells"]
                    start = update["range"]["startRowIndex"]
                    rows = [[cell["userEnteredValue"]["stringValue"] for cell in row["values"]]
                            for row in update["rows"]]
                    if start != len(self.rows):
                        raise AssertionError("append overwrote or skipped existing rows")
                    self.rows.extend(rows)
            response = {}
        elif "values" in arguments:
            response = {"values": self.rows}
        else:
            response = {"sheets": [{"properties": {
                "sheetId": 7, "title": "September 2026 unresolved evidence",
                "gridProperties": {"rowCount": self.grid_rows, "columnCount": 12},
            }}]}
        return mock.Mock(returncode=0, stderr="", stdout=json.dumps(response))


def synthetic_rows(count=54, text="proof-" * 1000):
    return [[f"uev-{index}", "2026-09-21", "machine", "low_confidence", "source",
             "description", "pending", "unposted", "evidence", "proof", "", text]
            for index in range(count)]


class MonthlyGatewayPayloadTests(unittest.TestCase):
    def publish(self, process, rows):
        with mock.patch.object(publisher.subprocess, "run", side_effect=process.run):
            return publisher.publish_monthly_unresolved(
                publisher.GwsSheetsGateway(), spreadsheet_id="synthetic",
                sheet_title="September 2026 unresolved evidence", rows=rows,
            )

    def assert_bounded_native_rows(self, process, rows):
        self.assertGreater(len(process.bodies), 1)
        flattened = []
        next_index = 2
        for encoded in process.bodies:
            self.assertLessEqual(len(encoded.encode("utf-8")), 96 * 1024)
            requests = json.loads(encoded)["requests"]
            update = requests[-1]["updateCells"]
            destination = update["range"]
            self.assertEqual(destination["startRowIndex"], next_index)
            self.assertEqual(destination["endColumnIndex"], 12)
            self.assertEqual(update["fields"], "userEnteredValue")
            copies = [item["copyPaste"] for item in requests if "copyPaste" in item]
            self.assertEqual([item["pasteType"] for item in copies], ["PASTE_FORMAT", "PASTE_DATA_VALIDATION"])
            for item in copies:
                self.assertEqual(item["destination"], destination)
                self.assertEqual(item["source"]["startRowIndex"], 1)
                self.assertEqual(item["source"]["endRowIndex"], 2)
            self.assertIn({"repeatCell": {"range": destination, "cell": {},
                            "fields": "userEnteredFormat.textFormat.link"}}, requests)
            flattened.extend([[cell["userEnteredValue"]["stringValue"] for cell in row["values"]]
                              for row in update["rows"]])
            next_index = destination["endRowIndex"]
        self.assertEqual(flattened, rows)
        self.assertEqual(process.rows[2:], rows)
        self.assertEqual(process.grid_rows, 2 + len(rows))

    def test_large_proof_payload_preserves_rows_formats_and_grid_with_bounded_arguments(self):
        rows = synthetic_rows()
        process = SheetProcess()
        result = self.publish(process, rows)
        self.assertEqual(result["appended"], 54)
        self.assert_bounded_native_rows(process, rows)

    def test_unicode_byte_size_not_character_count_controls_chunking(self):
        rows = synthetic_rows(12, "🧾" * 4000)
        process = SheetProcess()
        self.publish(process, rows)
        self.assert_bounded_native_rows(process, rows)

    def test_single_oversize_later_row_rejects_before_any_sheet_mutation(self):
        rows = synthetic_rows(20) + synthetic_rows(1, "🧾" * 25000)
        rows[-1][0] = "uev-oversize"
        process = SheetProcess()
        with self.assertRaisesRegex(publisher.PublicationError, "single monthly row.*payload"):
            self.publish(process, rows)
        self.assertEqual(process.bodies, [])
        self.assertEqual(len(process.rows), 2)
        self.assertEqual(process.grid_rows, 3)

    def test_retry_after_partial_batch_failure_preserves_verified_prefix_and_human_notes(self):
        rows = synthetic_rows()
        process = SheetProcess(fail_batch=2)
        with self.assertRaisesRegex(publisher.PublicationError, "synthetic failure"):
            self.publish(process, rows)
        prefix = len(process.rows) - 2
        self.assertGreater(prefix, 0)
        self.assertLess(prefix, len(rows))
        process.rows[2][10] = "human decision"
        result = self.publish(process, rows)
        self.assertEqual(result["unchanged"], prefix)
        self.assertEqual(result["appended"], len(rows) - prefix)
        self.assertEqual([row[0] for row in process.rows[2:]], [row[0] for row in rows])
        self.assertEqual(process.rows[2][10], "human decision")
        before = list(process.bodies)
        self.assertEqual(self.publish(process, rows)["appended"], 0)
        self.assertEqual(process.bodies, before)


if __name__ == "__main__":
    unittest.main()
