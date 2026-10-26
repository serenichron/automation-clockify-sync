"""Saved native timing recipes survive pending consumption without reallocation."""
import copy
import json
import unittest

from scripts import clockify_pending_review_selection as pending
from scripts import clockify_sheet_publish as publisher
import test_claude_session_timing as timing


class PendingNativeTimingRecipeTests(unittest.TestCase):
    def make_source(self, *, sources=timing.ENABLED, source_type="claude_bursts", paired=False,
                    positive=False, two=False):
        anchors = [timing.event(f"2026-09-25T09:{minute}:00+03:00", source=source_type)
                   for minute in ("00", "20")]
        first = timing.event("2026-09-25T09:00:00+03:00" if paired else "2026-09-25T09:03:00+03:00",
                             "assistant", source=source_type)
        second = timing.event("2026-09-25T09:07:00+03:00", "assistant", source=source_type)
        group = (anchors[0], first) if paired else (first,)
        events = [*anchors, first]
        if positive:
            completed = timing.event("2026-09-25T09:01:20+03:00", "assistant", source=source_type)
            events = [*anchors, completed]
            group = (anchors[0], completed)
        groups = [group]
        if two:
            events.append(second)
            groups.append((second,))
        result = timing.ClaudeSessionTimingTests.make_run(self, events, groups, sources=sources)
        source = {"ledger": json.loads((self.run_dir / "evidence/evidence-ledger.json").read_text()),
                  "accounting": result, "routing": json.loads(self.routing_path.read_text())}
        records = [{"proposal": p, "source": source, "review_id": publisher.stable_review_id(p)}
                   for p in result["proposals"]]
        return records, source

    def test_opted_in_result_only_and_paired_claude_use_exact_saved_recipe(self):
        for paired in (False, True):
            with self.subTest(paired=paired):
                records, source = self.make_source(paired=paired, two=True)
                before = copy.deepcopy((records, source))
                try:
                    normalized, proof = pending._credits(records, {"native": source})
                except ValueError as exc:
                    self.fail(f"saved opted-in Claude recipe was discarded: {exc}")
                self.assertEqual(20, proof["saved_credit_minutes"])
                self.assertEqual(1, len(proof["shared_pool_debits"]))
                self.assertEqual(20, proof["shared_pool_debits"][0]["debited_minutes"])
                self.assertEqual(20, proof["shared_pool_debits"][0]["capacity_minutes"])
                self.assertEqual(before, (records, source))
                self.assertEqual([r["proposal"] for r in records], normalized)

    def test_positive_observed_claude_span_stays_literal(self):
        records, source = self.make_source(positive=True)
        before = copy.deepcopy((records, source))
        normalized, proof = pending._credits(records, {"native": source})
        self.assertEqual(1, proof["saved_credit_minutes"])
        self.assertEqual([], proof["shared_pool_debits"])
        self.assertNotIn("timing_placement", normalized[0]["provenance"])
        self.assertEqual(before, (records, source))

    def test_legacy_absent_codex_recipe_keeps_existing_saved_proof_shape(self):
        records, source = self.make_source(sources=None, source_type="codex_sessions", two=True)
        normalized, proof = pending._credits(records, {"native": source})
        self.assertEqual(1200, proof["saved_credit_seconds"])
        self.assertEqual(20, proof["shared_pool_debits"][0]["debited_minutes"])
        self.assertEqual([r["proposal"] for r in records], normalized)
        self.assertNotIn("session_timing_pool_sources", source["accounting"]["allocation"]["deterministic_inputs"])
        self.assertNotIn("session_timing_pool_sources", proof)

    def test_unknown_or_malformed_saved_recipe_is_rejected(self):
        for setting in (["hermes_db_sessions"], "claude_bursts", None, ["claude_bursts", "claude_bursts"]):
            with self.subTest(setting=setting):
                records, source = self.make_source(paired=True)
                source["accounting"]["allocation"]["deterministic_inputs"]["session_timing_pool_sources"] = setting
                source["routing"]["session_timing_pool_sources"] = setting
                with self.assertRaisesRegex(ValueError, "timing.*recipe|session_timing_pool_sources"):
                    pending._credits(records, {"native": source})

    def test_saved_recipe_must_match_immutable_source_routing(self):
        for drift in ("routing-value", "routing-absent", "record-absent"):
            with self.subTest(drift=drift):
                records, source = self.make_source(paired=True)
                if drift == "routing-value":
                    source["routing"]["session_timing_pool_sources"] = ["codex_sessions"]
                elif drift == "routing-absent":
                    source["routing"].pop("session_timing_pool_sources")
                else:
                    source["accounting"]["allocation"]["deterministic_inputs"].pop("session_timing_pool_sources")
                with self.assertRaisesRegex(ValueError, "timing.*recipe"):
                    pending._credits(records, {"native": source})

    def test_mixed_reproducible_recipes_debit_the_same_canonical_pool(self):
        legacy_records, legacy = self.make_source(sources=None, paired=True)
        enabled_records, enabled = self.make_source(paired=True, two=True)
        # Both sources come from real pipeline runs: the newer source allocated
        # its second singleton only after the same higher-attention paired work.
        selected = [legacy_records[0], enabled_records[1]]
        normalized, proof = pending._credits(selected, {"legacy": legacy, "enabled": enabled})
        self.assertEqual(20, proof["saved_credit_minutes"])
        self.assertEqual(1, len(proof["shared_pool_debits"]))
        self.assertEqual(20, proof["shared_pool_debits"][0]["debited_minutes"])
        self.assertEqual([r["proposal"] for r in selected], normalized)

    def test_mixed_recipe_cannot_use_opted_in_alias_for_legacy_result_only_claim(self):
        records, source = self.make_source(paired=True, two=True)
        legacy = copy.deepcopy(source)
        legacy["routing"].pop("session_timing_pool_sources")
        legacy["accounting"]["allocation"]["deterministic_inputs"].pop("session_timing_pool_sources")
        forged = {**records[1], "source": legacy}
        with self.assertRaisesRegex(ValueError, "saved native placement"):
            pending._credits([records[0], forged], {"enabled": source, "legacy": legacy})

    def test_opted_in_shared_pool_cannot_spend_same_minutes_twice(self):
        records, source = self.make_source(two=True)
        proposal = records[1]["proposal"]
        proposal.update(start="2026-09-25T09:05:00+03:00", end="2026-09-25T09:15:00+03:00")
        with self.assertRaisesRegex(ValueError, "shared human-pool debit"):
            pending._credits(records, {"native": source})


if __name__ == "__main__":
    unittest.main()
