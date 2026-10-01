import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts import semantic_analyzer as semantic
from scripts import work_accounting_pipeline as pipeline
from test_semantic_analyzer import event, provider_members, provider_response


class ScopedReviewLocalQuarantineTests(unittest.TestCase):
    endpoint = semantic.AnalyzerEndpoint(
        "primary", "http://fixture", semantic.CURRENT_LIVE_FLASH_ROUTE[0],
        revision=semantic.CURRENT_LIVE_FLASH_ROUTE[1],
    )
    taxonomy = [{"project_name": "Serenichron Level 2", "prefix": "SC", "tag_names": ["Processes"]}]
    events = [event(f"ev-{index}") for index in range(1, 5)]
    target = ("ev-1", "ev-2", "ev-3", "ev-4")

    def source(self):
        return {
            "activities": [],
            "exceptions": [{
                "kind": "analyzer_review_failure",
                "evidence_ids": list(self.target),
                "reason": "Flash reviewer exhausted bounded scoped retry: contract_rejected_duplicate_evidence",
            }],
            "omissions": [],
            "ledger_event_count": 4,
            "ledger_evidence_digest": semantic.stable_digest("led-", list(self.target)),
            "analyzer_cache": {
                "records": [],
                "snapshot": {
                    "path": "analyzer-cache-used.jsonl", "record_count": 0,
                    "sha256": hashlib.sha256(b"").hexdigest(),
                },
            },
        }

    @staticmethod
    def response(payload):
        members = provider_members(payload)
        conflicted = provider_response(payload, members[:2])["activities"][0]
        conflicted["object"] = "conflicted work"
        clean = provider_response(payload, [members[2]])["activities"][0]
        clean["object"] = "uncontested work"
        cited_twice = provider_response(payload, [members[1]])["activities"][0]
        last = provider_response(payload, [members[3]])["activities"][0]
        return {
            "activities": [conflicted, clean],
            "exceptions": [{
                "kind": "insufficient_evidence", "reason": "Needs manual review",
                "evidence_partitions": copy.deepcopy(last["evidence_partitions"]),
            }],
            "omissions": [{
                "lifecycle": "noise", "reason": "No supported work",
                "evidence_partitions": copy.deepcopy(cited_twice["evidence_partitions"]),
            }],
        }

    def run_retry(self, cache, transport, *, mode="fresh"):
        return pipeline.run_scoped_failed_review_retry(
            self.source(), copy.deepcopy(self.events), primary=self.endpoint,
            cache=cache, review_taxonomy=self.taxonomy,
            targets={self.target: "contract_rejected_duplicate_evidence"},
            source_semantic_sha256="a" * 64, transport=transport,
            private_text_approved=True, scoped_review_mode=mode,
        )

    def test_fresh_scoped_duplicate_quarantines_whole_conflicted_rows_and_replays(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = semantic.AnalyzerResponseCache(Path(temporary) / "cache.jsonl")
            payloads = []

            def transport(_endpoint, body):
                payload = json.loads(body["messages"][1]["content"])
                payloads.append(payload)
                return self.response(payload)

            result = self.run_retry(cache, transport)
            self.assertEqual(1, len(payloads))
            self.assertEqual("scoped_review_v4_citation_quarantine", payloads[0]["scoped_failed_review"]["mode"])
            self.assertEqual("whole_row_quarantine_v1", payloads[0]["local_coverage_repair"])
            self.assertEqual("scoped_review_v4_citation_quarantine", result["failed_review_retry"]["mode"])
            self.assertEqual(["ev-3"], result["activities"][0]["evidence_ids"])
            self.assertEqual("uncontested work", result["activities"][0]["object"])
            self.assertGreater(result["activities"][0]["effort"]["recommended_minutes"], 0)
            self.assertEqual([], result["omissions"])
            quarantines = [row for row in result["exceptions"] if row["kind"] == "analyzer_review_partial_quarantine"]
            self.assertEqual(1, len(quarantines))
            self.assertEqual(["ev-1", "ev-2"], quarantines[0]["evidence_ids"])
            self.assertNotIn("effort", quarantines[0])
            self.assertEqual(
                ["ev-1", "ev-2", "ev-3", "ev-4"],
                sorted(value for section in ("activities", "exceptions", "omissions")
                       for row in result[section] for value in row["evidence_ids"]),
            )
            sealed = cache.path.read_bytes()
            self.assertEqual("accepted", json.loads(sealed.splitlines()[-1])["status"])
            replayed = self.run_retry(
                semantic.AnalyzerResponseCache(cache.path),
                lambda *_: self.fail("sealed scoped quarantine must not transport"),
                mode="scoped_review_v4_citation_quarantine",
            )
            self.assertEqual(result["activities"], replayed["activities"])
            self.assertEqual(result["exceptions"], replayed["exceptions"])
            self.assertEqual(sealed, cache.path.read_bytes())

    def test_scoped_quarantine_never_salvages_malformed_rows(self):
        defects = (
            ("invalid_effort", lambda response: response["activities"][1]["effort"].update(minimum_minutes=0)),
            ("missing_activity_fields", lambda response: response["activities"][1].update(action="")),
            ("invalid_lifecycle", lambda response: response["activities"][1].update(lifecycle="unrecognized")),
            ("invalid_project", lambda response: response["activities"][1]["project_recommendation"].update(name="Unknown project")),
        )
        for name, mutate in defects:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                cache = semantic.AnalyzerResponseCache(Path(temporary) / "cache.jsonl")

                def transport(_endpoint, body):
                    response = self.response(json.loads(body["messages"][1]["content"]))
                    mutate(response)
                    return response

                result = self.run_retry(cache, transport)
                self.assertEqual([], result["activities"])
                self.assertEqual("analyzer_review_failure", result["exceptions"][0]["kind"])
                record = json.loads(cache.path.read_text().splitlines()[-1])
                self.assertEqual("rejected", record["status"])
                self.assertNotIn("response", record)

    def test_explicit_v4_rejects_nonduplicate_source_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = semantic.AnalyzerResponseCache(Path(temporary) / "cache.jsonl")
            with self.assertRaises(pipeline.WorkAccountingError):
                pipeline.run_scoped_failed_review_retry(
                    self.source(), copy.deepcopy(self.events), primary=self.endpoint,
                    cache=cache, review_taxonomy=self.taxonomy,
                    targets={self.target: "contract_rejected_invalid_effort"},
                    source_semantic_sha256="a" * 64,
                    transport=lambda *_: self.fail("wrong source code must not transport"),
                    private_text_approved=True,
                    scoped_review_mode="scoped_review_v4_citation_quarantine",
                )


if __name__ == "__main__":
    unittest.main()
