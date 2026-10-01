import copy
import json
from pathlib import Path
import tempfile
import unittest

from scripts import semantic_analyzer as semantic
from test_semantic_analyzer import event, provider_members, provider_response


class PartialCitationQuarantineTests(unittest.TestCase):
    endpoint = semantic.AnalyzerEndpoint("primary", "http://fixture", "flash-fixture")
    taxonomy = [{"project_name": "Serenichron Level 2", "prefix": "SC", "tag_names": ["Processes"]}]
    candidate = {"activities": [], "exceptions": [], "omissions": []}

    def review(self, events, transport, cache, *, retry=True):
        ids = {row["evidence_id"] for row in events}
        return semantic._call_semantic_review(
            self.endpoint, events, candidate=self.candidate, taxonomy=self.taxonomy,
            tier="primary", transport=transport, known_evidence_ids=ids,
            evidence_time_spans={value: {"start": "2026-07-10 10:00", "end": "2026-07-10 10:10"} for value in ids},
            cache=cache, before_transport=None, cancelled=None,
            failed_review_retry_targets={tuple(sorted(ids)): "contract_rejected_omitted_evidence"} if retry else None,
        )

    @staticmethod
    def member_row(payload, positions, object_name):
        members = provider_members(payload)
        row = provider_response(payload, [members[index - 1] for index in positions])["activities"][0]
        row["object"] = object_name
        return row

    def test_retry_quarantines_complete_conflicted_rows_and_missing_members_with_replay(self):
        events = [event(f"ev-{number}") for number in range(1, 5)]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cache.jsonl"
            cache = semantic.AnalyzerResponseCache(path)
            bodies = []

            def transport(_endpoint, body):
                bodies.append(body)
                payload = json.loads(body["messages"][1]["content"])
                a = self.member_row(payload, [1, 2], "conflicted first row")
                b = self.member_row(payload, [2], "conflicted second row")
                clean = self.member_row(payload, [3], "uncontested row")
                return {"activities": [a, b, clean], "exceptions": [], "omissions": []}

            fresh = self.review(events, transport, cache)
            self.assertEqual(1, len(bodies))
            payload = json.loads(bodies[0]["messages"][1]["content"])
            self.assertEqual("whole_row_quarantine_v1", payload["local_coverage_repair"])
            self.assertEqual(["ev-3"], fresh["activities"][0]["evidence_ids"])
            self.assertEqual(20, fresh["activities"][0]["effort"]["recommended_minutes"])
            self.assertEqual(["ev-1", "ev-2", "ev-4"], fresh["exceptions"][0]["evidence_ids"])
            self.assertEqual("analyzer_review_partial_quarantine", fresh["exceptions"][0]["kind"])
            self.assertIn("Locally derived", fresh["exceptions"][0]["reason"])
            self.assertEqual([], fresh["omissions"])

            sealed = path.read_bytes()
            records = [json.loads(line) for line in sealed.splitlines()]
            self.assertEqual(["accepted"], [record["status"] for record in records])
            self.assertEqual(1, len(records[0]["response"]["activities"]))
            self.assertEqual("analyzer_review_partial_quarantine", records[0]["response"]["exceptions"][0]["kind"])

            replay = self.review(events, lambda *_: self.fail("cache replay called transport"), semantic.AnalyzerResponseCache(path))
            self.assertEqual(fresh, replay)
            self.assertEqual(sealed, path.read_bytes())

    def test_activity_omission_overlap_quarantines_both_rows(self):
        events = [event(f"ev-{number}") for number in range(1, 4)]

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            conflicted = self.member_row(payload, [1, 2], "conflicted activity")
            clean = self.member_row(payload, [3], "clean activity")
            omission = {
                "lifecycle": "noise", "reason": "No work", "evidence_partitions":
                    copy.deepcopy(self.member_row(payload, [2], "unused")["evidence_partitions"]),
            }
            return {"activities": [conflicted, clean], "exceptions": [], "omissions": [omission]}

        result = self.review(events, transport, None)
        self.assertEqual(["ev-3"], result["activities"][0]["evidence_ids"])
        self.assertEqual([], result["omissions"])
        self.assertEqual(["ev-1", "ev-2"], result["exceptions"][0]["evidence_ids"])

    def test_all_conflicted_rows_and_missing_member_have_no_effort(self):
        events = [event(f"ev-{number}") for number in range(1, 3)]

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            first = self.member_row(payload, [1], "first activity")
            second = self.member_row(payload, [1], "second activity")
            return {"activities": [first, second], "exceptions": [], "omissions": []}

        result = self.review(events, transport, None)
        self.assertEqual([], result["activities"])
        self.assertEqual(["ev-1", "ev-2"], result["exceptions"][0]["evidence_ids"])
        self.assertNotIn("effort", result["exceptions"][0])

    def test_missing_only_preserves_complete_cited_activity(self):
        events = [event(f"ev-{number}") for number in range(1, 3)]

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            return {"activities": [self.member_row(payload, [1], "cited activity")], "exceptions": [], "omissions": []}

        result = self.review(events, transport, None)
        self.assertEqual(["ev-1"], result["activities"][0]["evidence_ids"])
        self.assertEqual(["ev-2"], result["exceptions"][0]["evidence_ids"])

    def test_opt_in_request_does_not_reuse_legacy_retry_cache_entry(self):
        events = [event(f"ev-{number}") for number in range(1, 3)]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cache.jsonl"
            cache = semantic.AnalyzerResponseCache(path)
            legacy_body = semantic._review_body(
                events, candidate=self.candidate, taxonomy=self.taxonomy,
                model=self.endpoint.model,
                repair_failure_code="contract_rejected_omitted_evidence",
                repair_attempt=1,
                failed_review_retry_code="contract_rejected_omitted_evidence",
            )
            legacy_payload = json.loads(legacy_body["messages"][1]["content"])
            cache.store_accepted(self.endpoint, legacy_body, provider_response(legacy_payload))
            calls = []

            def transport(_endpoint, body):
                calls.append(body)
                payload = json.loads(body["messages"][1]["content"])
                return {"activities": [self.member_row(payload, [1], "fresh activity")], "exceptions": [], "omissions": []}

            result = self.review(events, transport, cache)
            self.assertEqual(1, len(calls))
            self.assertEqual(["ev-1"], result["activities"][0]["evidence_ids"])
            self.assertEqual(["ev-2"], result["exceptions"][0]["evidence_ids"])
            self.assertEqual(2, len(path.read_text().splitlines()))

    def test_invalid_retained_taxonomy_still_rejects_partial_result(self):
        events = [event(f"ev-{number}") for number in range(1, 4)]

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            conflicted = self.member_row(payload, [1, 2], "conflicted activity")
            repeated = self.member_row(payload, [2], "repeated activity")
            clean = self.member_row(payload, [3], "clean activity")
            clean["project_recommendation"]["name"] = "Unknown project"
            return {"activities": [conflicted, repeated, clean], "exceptions": [], "omissions": []}

        result = self.review(events, transport, None)
        self.assertEqual([], result["activities"])
        self.assertEqual("analyzer_review_failure", result["exceptions"][0]["kind"])

    def test_invalid_conflicted_taxonomy_cannot_disappear_into_quarantine(self):
        events = [event(f"ev-{number}") for number in range(1, 4)]

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            conflicted = self.member_row(payload, [1, 2], "conflicted activity")
            conflicted["project_recommendation"]["name"] = "Unknown project"
            repeated = self.member_row(payload, [2], "repeated activity")
            clean = self.member_row(payload, [3], "clean activity")
            return {"activities": [conflicted, repeated, clean], "exceptions": [], "omissions": []}

        result = self.review(events, transport, None)
        self.assertEqual([], result["activities"])
        self.assertEqual("analyzer_review_failure", result["exceptions"][0]["kind"])

    def test_invalid_retained_effort_still_rejects_partial_result(self):
        events = [event(f"ev-{number}") for number in range(1, 4)]

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            conflicted = self.member_row(payload, [1, 2], "conflicted activity")
            repeated = self.member_row(payload, [2], "repeated activity")
            clean = self.member_row(payload, [3], "clean activity")
            clean["effort"]["minimum_minutes"] = 0
            return {"activities": [conflicted, repeated, clean], "exceptions": [], "omissions": []}

        result = self.review(events, transport, None)
        self.assertEqual([], result["activities"])
        self.assertEqual("analyzer_review_failure", result["exceptions"][0]["kind"])

    def test_malformed_partitions_and_non_retry_reviews_do_not_quarantine(self):
        events = [event(f"ev-{number}") for number in range(1, 3)]

        def transport(_endpoint, body):
            payload = json.loads(body["messages"][1]["content"])
            row = self.member_row(payload, [1], "first")
            row["evidence_partitions"][0]["member_ranges"] = [[1, 3]]
            return {"activities": [row], "exceptions": [], "omissions": []}

        retry_result = self.review(events, transport, None)
        self.assertEqual([], retry_result["activities"])
        self.assertEqual("analyzer_review_failure", retry_result["exceptions"][0]["kind"])

        calls = []
        def duplicate_transport(endpoint, body):
            calls.append(body)
            payload = json.loads(body["messages"][1]["content"])
            row = self.member_row(payload, [1], "first")
            return {"activities": [row, copy.deepcopy(row)], "exceptions": [], "omissions": []}

        ordinary = self.review(events, duplicate_transport, None, retry=False)
        self.assertEqual(3, len(calls))
        self.assertEqual([], ordinary["activities"])
        self.assertEqual("analyzer_review_failure", ordinary["exceptions"][0]["kind"])


if __name__ == "__main__":
    unittest.main()
