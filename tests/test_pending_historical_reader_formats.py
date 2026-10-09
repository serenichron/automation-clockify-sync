"""Real sealed historical record compatibility; no invented provider output."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest

from scripts import review_corrections as corrections, semantic_analyzer as analyzer


class HistoricalReaderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixture = os.environ.get("CLOCKIFY_APPEND_FIXTURE")
        if not fixture:
            raise unittest.SkipTest("genuine historical formats not supplied")
        document = json.loads(Path(fixture).read_bytes())["binding"]
        cls.B = Path(document["sources"]["20261008T180235Z-repair-n5we89e1"]["artifacts"]["proposals"]["path"]).parent
        cls.C = Path(document["sources"]["20261008T183839Z-repair-pm0kbo1e"]["artifacts"]["proposals"]["path"]).parent

    def test_genuine_timing_metadata_preserves_chain_without_becoming_decision_or_credit(self):
        path = self.B / "review-corrections.jsonl"
        before = path.read_bytes()
        try:
            records = corrections._read_log(path)
        except ValueError as exc:
            self.fail("genuine historical timing metadata is unreadable: " + str(exc))
        self.assertEqual(5, len(records))
        self.assertEqual(["source_bound_timing_correction"] * 2, [record.get("record_type") for record in records[:2]])
        self.assertEqual(3, len(corrections.load_decisions(path)))
        self.assertEqual([], corrections.load_verified_posted_credits(path))
        self.assertEqual(before, path.read_bytes())

    def test_timing_metadata_can_precede_exact_new_wording_decision(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "corrections.jsonl"
            before = (self.B / "review-corrections.jsonl").read_bytes()
            path.write_bytes(before)
            proposals = json.loads((self.B / "proposals.json").read_bytes())
            proposal = next(p for p in proposals if p["activity_id"] == "act-b3a685aaf1effaa1cb39d310")
            item = {"id": "wka-29b3347a5e5497a3cbf5959a-s01", "current": proposal}
            decision = corrections.build_decision(item, decision="modify", reviewer="Test source wording",
                reviewed_at="2026-10-09T14:00:00Z", correction_categories=["wording"], rationale="Exact accepted source wording.",
                field_patch={"description": {"op": "replace", "value": "ES — Reviewed the auth-mail handoff"}})
            try:
                appended = corrections.append_decision(path, decision, item=item)
            except ValueError as exc:
                self.fail("historical metadata blocks independent source wording: " + str(exc))
            self.assertTrue(appended)
            self.assertTrue(path.read_bytes().startswith(before))
            self.assertEqual(4, len(corrections.load_decisions(path)))
            self.assertEqual([], corrections.load_verified_posted_credits(path))

    def test_timing_metadata_still_rejects_integrity_and_duration_shape_drift(self):
        original = json.loads((self.B / "review-corrections.jsonl").read_text().splitlines()[0])
        for mutation, reseal in (({"duration_seconds": 241}, False), ({"duration_seconds": True}, True),
                                 ({"decision": "approve"}, True), ({"correction_id": "tcor-" + "0" * 24}, True)):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                record = {**copy.deepcopy(original), **mutation}
                if reseal:
                    record["canonical_digest"] = corrections.canonical_digest(corrections._without_integrity(record))
                path = Path(directory) / "bad.jsonl"
                path.write_text(json.dumps(record) + "\n")
                with self.assertRaises(ValueError):
                    corrections._read_log(path)

    def test_historical_client_hygiene_rejection_loads_as_rejected_not_response(self):
        path = self.C / "analyzer-cache-used.jsonl"
        before = path.read_bytes()
        try:
            cache = analyzer.AnalyzerResponseCache(path)
        except analyzer.AnalyzerError as exc:
            self.fail("genuine historical rejected record is unreadable: " + str(exc))
        record = cache._records["arc-bb417fded74b57e7a3227ed8167471339e4d59c67b6d544fd3d11fd683368cb2"]
        self.assertEqual("rejected", record["status"])
        self.assertEqual("contract_rejected_client_description_hygiene", record["failure_code"])
        self.assertNotIn("response", record)
        self.assertEqual(before, path.read_bytes())

    def test_rejection_does_not_bypass_status_decision_or_unknown_failure_guards(self):
        original = json.loads((self.C / "analyzer-cache-used.jsonl").read_text().splitlines()[43])
        for mutation in ({"status": "accepted"}, {"decision_digest": "0" * 64},
                         {"failure_code": "contract_rejected_unknown_invented"}):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "bad.jsonl"
                path.write_text(json.dumps({**copy.deepcopy(original), **mutation}) + "\n")
                with self.assertRaises(analyzer.AnalyzerError):
                    analyzer.AnalyzerResponseCache(path)


if __name__ == "__main__":
    unittest.main()
