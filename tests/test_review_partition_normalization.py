import io
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest import mock

from scripts import semantic_analyzer as semantic
from scripts import work_accounting_pipeline as pipeline
from test_semantic_analyzer import event, provider_members, provider_response


def covered(response):
    members = []
    for section in ("activities", "exceptions", "omissions"):
        for row in response[section]:
            for partition in row["evidence_partitions"]:
                for start, end in partition["member_ranges"]:
                    members.extend((partition["bundle_ref"], n) for n in range(start, end + 1))
    return members


def rows(*partition_lists, section="activities"):
    response = {"activities": [], "exceptions": [], "omissions": []}
    for partitions in partition_lists:
        response[section].append({"object": f"row {len(response[section])}", "evidence_partitions": partitions})
    return response


class NormalizeReviewPartitionsTests(unittest.TestCase):
    # Catches bookkeeping-only rejections that cost a model repair call.
    counts = {"b-0001": 4, "b-0002": 1}

    def normalize(self, response):
        return semantic.normalize_review_partitions(response, member_counts=self.counts)

    def test_exact_cover_is_left_untouched(self):
        response = rows([{"bundle_ref": "b-0001", "member_ranges": [[1, 4]]}],
                        [{"bundle_ref": "b-0002", "member_ranges": [[1, 1]]}])
        self.assertIsNone(self.normalize(response))

    def test_overlaps_repeats_and_out_of_range_members_are_lossless(self):
        response = rows(
            [{"bundle_ref": "b-0001", "member_ranges": [[1, 2], [2, 3]]},
             {"bundle_ref": "b-0001", "member_ranges": [[4, 9]]}],
            [{"bundle_ref": "b-0002", "member_ranges": [[0, 1], [5, 3]]},
             {"bundle_ref": "b-0999", "member_ranges": [[1, 1]]}],
        )
        replacement, report = self.normalize(response)
        self.assertEqual([[1, 4]], replacement["activities"][0]["evidence_partitions"][0]["member_ranges"])
        self.assertEqual([{"bundle_ref": "b-0002", "member_ranges": [[1, 1]]}],
                         replacement["activities"][1]["evidence_partitions"])
        self.assertEqual([], replacement["exceptions"])
        self.assertEqual(0, report["members_routed"])
        self.assertEqual((1, 1, 2, 1), (report["partitions_dropped"], report["ranges_dropped"],
                                        report["ranges_clipped"], report["members_merged"]))
        self.assertEqual("row 0", replacement["activities"][0]["object"])

    def test_contested_rows_and_unassigned_members_go_to_one_versioned_quarantine(self):
        response = rows(
            [{"bundle_ref": "b-0001", "member_ranges": [[1, 2]]}],
            [{"bundle_ref": "b-0001", "member_ranges": [[2, 2]]}],
            [{"bundle_ref": "b-0001", "member_ranges": [[3, 3]]}],
        )
        replacement, report = self.normalize(response)
        self.assertEqual(["row 2"], [row["object"] for row in replacement["activities"]])
        [quarantine] = replacement["exceptions"]
        self.assertEqual("analyzer_review_partial_quarantine", quarantine["kind"])
        self.assertIn(semantic.PARTITION_NORMALIZATION_VERSION, quarantine["reason"])
        self.assertEqual([{"bundle_ref": "b-0001", "member_ranges": [[1, 2], [4, 4]]},
                          {"bundle_ref": "b-0002", "member_ranges": [[1, 1]]}],
                         quarantine["evidence_partitions"])
        self.assertEqual((2, 4, 5), (report["rows_quarantined"], report["members_routed"], report["members_total"]))

    def test_ungrounded_rows_are_dropped_and_their_members_stay_accounted(self):
        response = rows([], [{"bundle_ref": "b-0001", "member_ranges": [[1, 4]]}])
        replacement, report = self.normalize(response)
        self.assertEqual(1, report["rows_dropped_ungrounded"])
        self.assertEqual(["row 1"], [row["object"] for row in replacement["activities"]])
        self.assertEqual([("b-0002", 1)], covered({**replacement, "activities": []}))

    def test_randomized_outputs_are_always_an_exact_cover(self):
        expected = {(ref, n) for ref, total in self.counts.items() for n in range(1, total + 1)}
        generator = random.Random(20261026)
        for _ in range(500):
            response = {"activities": [], "exceptions": [], "omissions": []}
            for section in response:
                for _row in range(generator.randint(0, 3)):
                    response[section].append({"reason": "r", "evidence_partitions": [
                        {"bundle_ref": generator.choice(["b-0001", "b-0002", "b-0003", 7]),
                         "member_ranges": [[generator.randint(-1, 6), generator.randint(-1, 6)]
                                           for _ in range(generator.randint(0, 2))]}
                        for _ in range(generator.randint(0, 2))
                    ]})
            normalized = self.normalize(response)
            result = response if normalized is None else normalized[0]
            if normalized is None:
                continue
            members = covered(result)
            self.assertEqual(len(members), len(set(members)))
            self.assertEqual(expected, set(members))


class ReviewPartitionNormalizationCallTests(unittest.TestCase):
    endpoint = semantic.AnalyzerEndpoint("primary", "http://fixture", "flash-fixture")
    taxonomy = [{"project_name": "Serenichron Level 2", "prefix": "SC", "tag_names": ["Processes"]}]
    candidate = {"activities": [], "exceptions": [], "omissions": []}

    def review(self, events, transport, cache):
        ids = {row["evidence_id"] for row in events}
        return semantic._call_semantic_review(
            self.endpoint, events, candidate=self.candidate, taxonomy=self.taxonomy,
            tier="primary", transport=transport, known_evidence_ids=ids,
            evidence_time_spans={value: {"start": "2026-07-10 10:00", "end": "2026-07-10 10:10"} for value in ids},
            cache=cache, before_transport=None, cancelled=None,
        )

    @staticmethod
    def member_row(payload, positions, object_name):
        members = provider_members(payload)
        row = provider_response(payload, [members[index - 1] for index in positions])["activities"][0]
        row["object"] = object_name
        return row

    def duplicate_transport(self, bodies, *, rows_for):
        def transport(_endpoint, body):
            bodies.append(body)
            payload = json.loads(body["messages"][1]["content"])
            return {"activities": rows_for(payload), "exceptions": [], "omissions": []}
        return transport

    def test_fresh_duplicate_is_normalized_without_a_repair_call_and_replays_byte_stable(self):
        events = [event(f"ev-{number}") for number in range(1, 9)]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cache.jsonl"
            cache = semantic.AnalyzerResponseCache(path, record_review_diagnostics=True,
                                                   partition_normalization=True)
            bodies = []
            transport = self.duplicate_transport(bodies, rows_for=lambda payload: [
                self.member_row(payload, [1], "first conflicted"),
                self.member_row(payload, [1], "second conflicted"),
                self.member_row(payload, [2, 3, 4, 5, 6, 7, 8], "clean work"),
            ])
            fresh = self.review(events, transport, cache)
            self.assertEqual(1, len(bodies))
            self.assertEqual(["clean work"], [row["object"] for row in fresh["activities"]])
            self.assertEqual(["ev-1"], fresh["exceptions"][0]["evidence_ids"])
            self.assertEqual("analyzer_review_partial_quarantine", fresh["exceptions"][0]["kind"])

            sealed = path.read_bytes()
            self.assertEqual(["accepted"], [json.loads(line)["status"] for line in sealed.splitlines()])
            diagnostics = path.with_name(path.name + ".review-diagnostics.jsonl").read_text().splitlines()
            self.assertEqual("contract_rejected_duplicate_evidence", json.loads(diagnostics[0])["failure_code"])
            self.assertEqual(3, len(json.loads(diagnostics[0])["activities"]))

            replay = self.review(events, lambda *_: self.fail("replay called transport"),
                                 semantic.AnalyzerResponseCache(path, partition_normalization=True))
            self.assertEqual(fresh, replay)
            self.assertEqual(sealed, path.read_bytes())

    def test_lossless_out_of_range_member_needs_no_quarantine(self):
        events = [event(f"ev-{number}") for number in range(1, 3)]
        bodies = []

        def rows_for(payload):
            row = self.member_row(payload, [1, 2], "whole bundle")
            row["evidence_partitions"][0]["member_ranges"] = [[1, 5]]
            return [row]

        cache_dir = tempfile.TemporaryDirectory()
        self.addCleanup(cache_dir.cleanup)
        cache = semantic.AnalyzerResponseCache(Path(cache_dir.name) / "c.jsonl", partition_normalization=True)
        result = self.review(events, self.duplicate_transport(bodies, rows_for=rows_for), cache)
        self.assertEqual(1, len(bodies))
        self.assertEqual([], result["exceptions"])
        self.assertEqual(["ev-1", "ev-2"], result["activities"][0]["evidence_ids"])

    def test_quarantine_above_threshold_keeps_the_model_repair_path(self):
        events = [event(f"ev-{number}") for number in range(1, 3)]
        bodies = []
        transport = self.duplicate_transport(bodies, rows_for=lambda payload: [
            self.member_row(payload, [1, 2], "first"), self.member_row(payload, [2], "second"),
        ])
        cache_dir = tempfile.TemporaryDirectory()
        self.addCleanup(cache_dir.cleanup)
        cache = semantic.AnalyzerResponseCache(Path(cache_dir.name) / "c.jsonl", partition_normalization=True)
        result = self.review(events, transport, cache)
        self.assertEqual(1 + semantic.MAX_CONTRACT_REPAIR_ATTEMPTS, len(bodies))
        self.assertEqual("analyzer_review_failure", result["exceptions"][0]["kind"])

    def test_disabled_by_default_and_sealed_rejections_replay_unchanged(self):
        events = [event(f"ev-{number}") for number in range(1, 9)]
        rows_for = lambda payload: [  # noqa: E731
            self.member_row(payload, [1], "a"), self.member_row(payload, [1], "b"),
            self.member_row(payload, [2, 3, 4, 5, 6, 7, 8], "c"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "cache.jsonl"
            bodies = []
            legacy = self.review(events, self.duplicate_transport(bodies, rows_for=rows_for),
                                 semantic.AnalyzerResponseCache(path))
            self.assertEqual(1 + semantic.MAX_CONTRACT_REPAIR_ATTEMPTS, len(bodies))
            sealed = path.read_bytes()
            replay = self.review(events, lambda *_: self.fail("replay called transport"),
                                 semantic.AnalyzerResponseCache(path, partition_normalization=True))
            self.assertEqual(legacy, replay)
            self.assertEqual(sealed, path.read_bytes())

    def test_conflicted_row_with_invalid_taxonomy_still_rejects(self):
        events = [event(f"ev-{number}") for number in range(1, 9)]
        bodies = []

        def rows_for(payload):
            bad = self.member_row(payload, [1], "conflicted")
            bad["project_recommendation"]["name"] = "Unknown project"
            return [bad, self.member_row(payload, [1], "other"),
                    self.member_row(payload, [2, 3, 4, 5, 6, 7, 8], "clean")]

        cache_dir = tempfile.TemporaryDirectory()
        self.addCleanup(cache_dir.cleanup)
        cache = semantic.AnalyzerResponseCache(Path(cache_dir.name) / "c.jsonl", partition_normalization=True)
        result = self.review(events, self.duplicate_transport(bodies, rows_for=rows_for), cache)
        self.assertEqual(1 + semantic.MAX_CONTRACT_REPAIR_ATTEMPTS, len(bodies))
        self.assertEqual("analyzer_review_failure", result["exceptions"][0]["kind"])


class PipelinePartitionNormalizationTests(unittest.TestCase):
    # Catches the production ledger path still paying repair calls for a
    # small, deterministic coverage gap.
    taxonomy = [{"project_name": "Serenichron Level 2", "prefix": "SC", "tag_names": ["Processes"]}]
    endpoint = semantic.AnalyzerEndpoint(
        "clockify_analyzer_primary", "http://fixture/v1/chat/completions",
        semantic.CURRENT_LIVE_FLASH_ROUTE[0], revision=semantic.CURRENT_LIVE_FLASH_ROUTE[1],
    )

    def test_analyze_ledger_quarantines_one_unassigned_member_without_repair(self):
        events = [event(f"ev-{number}") for number in range(1, 9)]
        reviews = []

        def urlopen(request, **_kwargs):
            payload = json.loads(json.loads(request.data)["messages"][1]["content"])
            if payload.get("probe"):
                return io.BytesIO(b'{"probe":"ok"}')
            members = provider_members(payload)
            if payload["mode"] == "review":
                reviews.append(payload)
                members = members[:-1]
            return io.BytesIO(json.dumps(provider_response(payload, members)).encode())

        with tempfile.TemporaryDirectory() as temporary, \
             mock.patch.object(semantic.AnalyzerEndpoint, "from_env",
                               side_effect=lambda prefix, **_: self.endpoint
                               if prefix == "CLOCKIFY_ANALYZER_PRIMARY" else None), \
             mock.patch.dict("os.environ", {"CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED": "approved"}), \
             mock.patch("urllib.request.urlopen", side_effect=urlopen):
            result = pipeline.analyze_ledger(events, analyzer_cache_path=Path(temporary) / "cache.jsonl",
                                             review_taxonomy=self.taxonomy, analyzer_workers=1)
        self.assertEqual(1, len(reviews))
        self.assertEqual(1, len(result["activities"]))
        self.assertEqual("analyzer_review_partial_quarantine", result["exceptions"][0]["kind"])
        self.assertEqual(["ev-8"], result["exceptions"][0]["evidence_ids"])


if __name__ == "__main__":
    unittest.main()
