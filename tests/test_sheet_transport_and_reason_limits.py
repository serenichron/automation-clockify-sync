"""Bound CLI arguments and Sheet cells without dropping proof-bearing facts."""
import copy
import hashlib
import inspect
import json
import subprocess
import unittest
from unittest import mock

from scripts import clockify_sheet_publish as publisher
from scripts import clockify_review_batches as batches
from test_review_batches import proposal, identity


class SheetAppendTransportLimitTests(unittest.TestCase):
    def setUp(self):
        self.requests = []
        self.fail_request = None

        def cli(command, **kwargs):
            self.requests.append(command)
            failed = len(self.requests) == self.fail_request
            return subprocess.CompletedProcess(command, 1 if failed else 0, "{}", "append failed" if failed else "")

        patch = mock.patch.object(publisher.subprocess, "run", side_effect=cli)
        patch.start()
        self.addCleanup(patch.stop)
        self.gateway = publisher.GwsSheetsGateway()

    def bodies(self):
        return [command[command.index("--json") + 1] for command in self.requests]

    def test_append_splits_complete_rows_in_order_below_json_byte_limit(self):
        rows = [[f"row-{number}", "payload" * 3000] for number in range(12)]
        original = copy.deepcopy(rows)
        self.gateway.append_values("sheet", "'Review'!A:O", rows)
        self.assertGreater(len(self.requests), 1)
        self.assertTrue(all(len(body.encode("utf-8")) <= 64 * 1024 for body in self.bodies()))
        self.assertEqual(rows, [row for body in self.bodies() for row in json.loads(body)["values"]])
        self.assertEqual(original, rows)

    def test_append_bounds_utf8_bytes_not_character_count(self):
        rows = [[f"row-{number}", "😀" * 9000] for number in range(4)]
        self.gateway.append_values("sheet", "Review!A:O", rows)
        self.assertTrue(all(len(body.encode("utf-8")) <= 64 * 1024 for body in self.bodies()))
        self.assertEqual(rows, [row for body in self.bodies() for row in json.loads(body)["values"]])

    def test_small_append_preserves_exact_single_request(self):
        rows = [["one", 1, "café"], ["two", 2, "😀"]]
        self.gateway.append_values("sheet", "Review!A:O", rows)
        self.assertEqual([json.dumps({"majorDimension": "ROWS", "values": rows}, ensure_ascii=False)], self.bodies())
        self.assertEqual({"spreadsheetId": "sheet", "range": "Review!A:O",
                          "valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
                         json.loads(self.requests[0][self.requests[0].index("--params") + 1]))

    def test_oversize_single_row_rejects_before_any_append(self):
        with self.assertRaises(publisher.PublicationError):
            self.gateway.append_values("sheet", "Review!A:O", [["small"], ["x" * (64 * 1024)]])
        self.assertEqual([], self.requests)

    def test_failed_chunk_stops_without_retry_or_later_append(self):
        self.fail_request = 2
        with self.assertRaisesRegex(publisher.PublicationError, "append failed"):
            self.gateway.append_values("sheet", "Review!A:O", [["x" * 40000] for _ in range(4)])
        self.assertEqual(2, len(self.requests))

    def test_empty_append_has_no_transport_effect(self):
        self.gateway.append_values("sheet", "Review!A:O", [])
        self.assertEqual([], self.requests)


class SheetUpdateTransportLimitTests(unittest.TestCase):
    setUp = SheetAppendTransportLimitTests.setUp
    bodies = SheetAppendTransportLimitTests.bodies

    @staticmethod
    def row_ranges(number, left="left", right="right"):
        return [{"range": f"'Review'!A{number}:I{number}", "values": [[left]]},
                {"range": f"'Review'!K{number}:M{number}", "values": [[right]]}]

    def test_update_splits_ordered_ranges_without_separating_same_row_pairs(self):
        ranges = [value for n in range(2, 11) for value in self.row_ranges(n, "x" * 20000, "y" * 10000)]
        before = copy.deepcopy(ranges)
        self.gateway.update_values("sheet", ranges)
        self.assertGreater(len(self.requests), 1)
        self.assertTrue(all(len(body.encode("utf-8")) <= 64 * 1024 for body in self.bodies()))
        chunks = [json.loads(body)["data"] for body in self.bodies()]
        self.assertEqual(ranges, [value for chunk in chunks for value in chunk])
        for n in range(2, 11):
            containing = [chunk for chunk in chunks if any(value["range"] == f"'Review'!A{n}:I{n}" for value in chunk)]
            self.assertEqual(1, len(containing))
            self.assertIn(f"'Review'!K{n}:M{n}", [value["range"] for value in containing[0]])
        self.assertEqual(before, ranges)

    def test_update_measures_utf8_bytes_including_unicode(self):
        ranges = [value for n in range(2, 6) for value in self.row_ranges(n, "😀" * 9000, "café" * 2000)]
        self.gateway.update_values("sheet", ranges)
        self.assertTrue(all(len(body.encode("utf-8")) <= 64 * 1024 for body in self.bodies()))
        self.assertEqual(ranges, [value for body in self.bodies() for value in json.loads(body)["data"]])

    def test_late_oversize_combined_row_rejects_before_any_update(self):
        ranges = self.row_ranges(2) + self.row_ranges(3, "x" * 40000, "y" * 40000)
        with self.assertRaises(publisher.PublicationError):
            self.gateway.update_values("sheet", ranges)
        self.assertEqual([], self.requests)

    def test_nonadjacent_same_row_ranges_remain_atomic_without_reordering(self):
        row_two = self.row_ranges(2, "x" * 20000, "y" * 20000)
        row_two[1]["range"] = "Review!K2:M2"  # Same physical sheet despite A1 quoting.
        ranges = [row_two[0], *self.row_ranges(3, "a" * 5000, "b" * 5000), row_two[1],
                  *self.row_ranges(4, "c" * 20000, "d" * 20000)]
        self.gateway.update_values("sheet", ranges)
        chunks = [json.loads(body)["data"] for body in self.bodies()]
        self.assertEqual(ranges, [value for chunk in chunks for value in chunk])
        self.assertEqual(ranges[:4], chunks[0])
        self.assertTrue(all(len(body.encode("utf-8")) <= 64 * 1024 for body in self.bodies()))

    def test_large_unsupported_multirow_shape_rejects_all_before_writing(self):
        ranges = self.row_ranges(2, "x" * 40000)
        ranges += [{"range": "'Review'!A3:I4", "values": [["y" * 40000], ["z"]]}]
        with self.assertRaises(publisher.PublicationError):
            self.gateway.update_values("sheet", ranges)
        self.assertEqual([], self.requests)

    def test_update_failure_stops_without_retry_or_later_chunks(self):
        self.fail_request = 2
        ranges = [value for n in range(2, 6) for value in self.row_ranges(n, "x" * 40000)]
        with self.assertRaisesRegex(publisher.PublicationError, "append failed"):
            self.gateway.update_values("sheet", ranges)
        self.assertEqual(2, len(self.requests))

    def test_small_update_preserves_exact_body_even_for_multirow_ranges(self):
        ranges = [{"range": "'Review'!A1:O2", "majorDimension": "ROWS", "values": [["café"], ["😀"]]}]
        self.gateway.update_values("sheet", ranges)
        self.assertEqual([json.dumps({"valueInputOption": "RAW", "includeValuesInResponse": False,
                                      "data": ranges}, ensure_ascii=False)], self.bodies())

    def test_empty_update_has_no_transport_effect(self):
        self.gateway.update_values("sheet", [])
        self.assertEqual([], self.requests)


class PortfolioReasonLimitTests(unittest.TestCase):
    def fixture(self, *, count=24, internal=True, external_repetitions=1):
        sources = [proposal(number) for number in range(1, count + 1)]
        for number, source in enumerate(sources, 1):
            # Use actual keys, deliberately unrelated to native-ID hash recipes.
            source["candidate_key"] = f"wks-{number:024x}"
        for number, source in enumerate(sources, 1):
            counterparts = [row["candidate_key"] for row in sources[:number - 1] + sources[number:]] if internal else []
            source["review_warnings"] = [self.warning("review_proposal_overlap", key) for key in counterparts]
            source["review_warnings"].extend([
                self.warning("review_proposal_overlap", "wks-" + "f" * 24),
                self.warning("existing_clockify_overlap", "ev-" + "a" * 64),
            ] * external_repetitions)
        result = batches.package_review_batches(sources,
            editorial_descriptions={identity(n): "SC — Verified local implementation" for n in range(1, count + 1)},
            work_contexts={identity(n): "explicit-work" for n in range(1, count + 1)},
            allow_review_overlaps=True, allow_clockify_overlaps=True, natural_batches=True)
        self.assertEqual(1, len(result["activities"]))
        return sources, result["activities"][0]

    @staticmethod
    def warning(kind, counterpart):
        return {"type": kind, "counterpart_id": counterpart,
                "overlap_start": "2026-09-29T10:00:07+03:00",
                "overlap_end": "2026-09-29T10:00:17+03:00", "overlap_duration_seconds": 10}

    def render(self, activity, sources):
        self.assertIn("source_proposals", inspect.signature(publisher.portfolio_row).parameters)
        return publisher.portfolio_row(activity, "run", source_proposals=sources)

    def test_oversize_reason_without_bound_child_proof_rejects(self):
        _sources, activity = self.fixture()
        with self.assertRaises(publisher.PublicationError):
            publisher.portfolio_row(activity, "run")

    def test_oversize_reason_summarizes_only_proven_internal_parallel_effort(self):
        sources, activity = self.fixture()
        before = copy.deepcopy(activity)
        row = self.render(activity, sources)
        self.assertLessEqual(len(row[12].encode("utf-16-le")) // 2, 50000)
        displayed, notice = row[12].split("\n", 1)
        document = json.loads(displayed)
        summary = document["internal_parallel_effort"]
        self.assertEqual(552, summary["warning_count"])
        self.assertEqual([identity(n) for n in range(1, 25)], summary["affected_child_review_ids"])
        self.assertEqual("2026-09-29T10:00:07+03:00", summary["span_start"])
        self.assertEqual("2026-09-29T10:00:17+03:00", summary["span_end"])
        self.assertIn("not financial credit", summary["notice"])
        self.assertEqual(batches.OVERLAP_REVIEW_NOTICE, notice)
        expected_external = [{"review_id": identity(n), "warnings": [
            self.warning("review_proposal_overlap", "wks-" + "f" * 24),
            self.warning("existing_clockify_overlap", "ev-" + "a" * 64)]} for n in range(1, 25)]
        self.assertEqual(expected_external, document["child_review_warnings"])
        expected_hash = "sha256:" + hashlib.sha256(json.dumps(
            before["child_review_warnings"], ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()
        self.assertEqual(expected_hash, summary["full_detail_reference"]["sha256"])
        self.assertEqual("package.activities[review_id='" + activity["review_id"] + "'].child_review_warnings",
                         summary["full_detail_reference"]["immutable_package_field"])
        self.assertEqual(1704 / 60, row[3])
        self.assertEqual(("pending", "unposted", ""), (row[9], row[13], row[14]))
        self.assertEqual(before, activity)

    def test_foreign_source_candidate_key_remains_external(self):
        sources, activity = self.fixture()
        foreign = proposal(99)
        foreign["candidate_key"] = "wks-" + "f" * 24
        row = self.render(activity, [*sources, foreign])
        document = json.loads(row[12].split("\n", 1)[0])
        self.assertEqual(24, sum(w["counterpart_id"] == "wks-" + "f" * 24
                                for child in document["child_review_warnings"] for w in child["warnings"]))

    def test_missing_or_ambiguous_child_candidate_proof_rejects(self):
        sources, activity = self.fixture()
        for proof in (sources[:-1], [*sources, copy.deepcopy(sources[0])],
                      [{**source, "candidate_key": "wks-" + "e" * 24} for source in sources]):
            with self.subTest(proof_count=len(proof)), self.assertRaises(publisher.PublicationError):
                self.render(activity, proof)

    def test_candidate_shared_with_foreign_child_cannot_hide_external_overlap(self):
        sources, activity = self.fixture()
        foreign = proposal(99)
        foreign["candidate_key"] = sources[0]["candidate_key"]
        with self.assertRaises(publisher.PublicationError):
            self.render(activity, [*sources, foreign])

    def test_external_detail_above_cell_cap_rejects_without_truncation(self):
        sources, activity = self.fixture(count=4, external_repetitions=60)
        before = copy.deepcopy(activity)
        with self.assertRaisesRegex(publisher.PublicationError, "external detail"):
            self.render(activity, sources)
        self.assertEqual(before, activity)

    def test_publishable_reason_is_byte_identical_even_with_optional_proof(self):
        sources, activity = self.fixture(count=2)
        before = copy.deepcopy(activity)
        legacy = publisher.portfolio_row(activity, "run")
        expected_reason = json.dumps(activity["child_review_warnings"], ensure_ascii=False, sort_keys=True)
        expected_reason += "\n" + batches.OVERLAP_REVIEW_NOTICE
        self.assertEqual(expected_reason, legacy[12])
        self.assertEqual(legacy, self.render(activity, []))
        self.assertEqual(before, activity)

    def test_reason_limit_counts_utf16_units_and_allows_exact_boundary(self):
        sources, activity = self.fixture(count=1, internal=False)
        activity["child_review_warnings"] = []
        activity.pop("review_notice", None)
        activity["validation_status"] = "x" * 50000
        self.assertEqual("x" * 50000, publisher.portfolio_row(activity, "run")[12])
        activity["validation_status"] = "x" * 39999 + "😀" * 5001
        with self.assertRaises(publisher.PublicationError):
            self.render(activity, sources)


if __name__ == "__main__":
    unittest.main()
