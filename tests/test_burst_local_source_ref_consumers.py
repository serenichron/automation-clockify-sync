"""Distinct captured bursts are not duplicates merely because ordinals restart."""
import unittest

from scripts import evidence_ledger as ledger
from scripts import semantic_analyzer as analyzer
from scripts import work_accounting_pipeline as pipeline


def snapshot(second_instruction="Implement the requested change"):
    records = []
    for start, end, instruction, reply in (
        ("2026-09-09T10:10:23.565000+03:00", "2026-09-09T10:11:00+03:00",
         "Implement the requested change", "First change completed"),
        ("2026-09-09T10:40:58.664000+03:00", "2026-09-09T10:41:30+03:00",
         second_instruction, "Second change completed"),
    ):
        records.append({
            "session_id": "fixture-session", "start": start, "end": end,
            "events": [
                {"timestamp": start, "role": "user", "kind": "message", "content": instruction},
                {"timestamp": end, "role": "assistant", "kind": "message", "content": reply},
            ],
        })
    return {"sessions": [{"machine": "fixture", "codex_sessions": records}]}


class BurstLocalSourceRefConsumerTests(unittest.TestCase):
    def test_real_ledger_deduplicates_replay_not_distinct_burst_observations(self):
        # Catches a regression replacing full evidence-ID dedup with source-ref dedup.
        for second in ("Implement the requested change", "Implement a different change"):
            with self.subTest(second=second):
                events = ledger.normalize_collector_snapshot(snapshot(second))
                replay = ledger.EvidenceLedger(tuple(events)).append(
                    ledger.normalize_collector_snapshot(snapshot(second)))
                messages = [event for event in replay.events if event.source_type == "codex_sessions_event"]
                self.assertEqual(4, len(messages))
                users = [event for event in messages if event.attributes["role"] == "user"]
                self.assertEqual(2, len(users))
                self.assertEqual(users[0].source_ref, users[1].source_ref)
                self.assertNotEqual(users[0].evidence_id, users[1].evidence_id)
                self.assertEqual({"2026-09-09T10:10:23.565000+03:00", "2026-09-09T10:40:58.664000+03:00"},
                                 {event.observed_at for event in users})
                retained, _noise = pipeline._analysis_events([event.document() for event in replay.events])
                self.assertEqual(4, len(retained))
                bundles, manifests = analyzer._semantic_evidence_bundles(retained)
                self.assertEqual(4, sum(bundle["member_count"] for bundle in bundles))
                self.assertEqual(4, len({identifier for row in manifests for identifier in row["evidence_ids"]}))

    def test_scoped_retry_keeps_each_burst_instruction_with_its_own_reply(self):
        # Catches sorting reused burst-local ordinals across the full session.
        events = ledger.normalize_collector_snapshot(snapshot())
        messages = [event.document() for event in events if event.source_type == "codex_sessions_event"]
        partitions = pipeline._scoped_review_partitions(messages, maximum_members=2)
        self.assertEqual([
            ["2026-09-09T10:10:23.565000+03:00", "2026-09-09T10:11:00+03:00"],
            ["2026-09-09T10:40:58.664000+03:00", "2026-09-09T10:41:30+03:00"],
        ], [[event["observed_at"] for event in partition] for partition in partitions])


if __name__ == "__main__":
    unittest.main()
