"""Completed historical timing metadata is not fresh repair authority."""
import copy
import os
from pathlib import Path
import unittest

from scripts import clockify_review_run as native, review_corrections


class HistoricalTimingTransitionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = os.environ.get("CLOCKIFY_HISTORICAL_WORDING_ROOT")
        if not root:
            raise unittest.SkipTest("genuine immutable historical ancestry not supplied")
        cls.root = Path(root) / "runs"
        cls.parent = cls.root / "20261008T171050Z-repair-9krd5s05"
        cls.timing_child = cls.root / "20261008T174825Z-repair-y5dwetiq"
        cls.wording_child = cls.root / "20261008T180235Z-repair-n5we89e1"

    def test_timing_metadata_does_not_consume_prior_human_wording_target(self):
        try:
            result = native._validate_repair_credit_transition(
                self.timing_child, self.wording_child / "review-corrections.jsonl", runs_root=self.root)
        except native.ReviewRunError as exc:
            self.fail("historical timing metadata misclassified as a prior human decision: " + str(exc))
        self.assertEqual(2, len(result))

    def test_completed_historical_timing_ancestry_retains_verified_inference_context(self):
        previous = native.RUNS
        try:
            native._configure_runs_root(self.root)
            try:
                context = native._verified_replay_inference_context(self.wording_child)
            except native.ReviewRunError as exc:
                self.fail("completed source-bound historical timing ancestry cannot replay: " + str(exc))
            self.assertTrue((context / "analyzer-cache-used.jsonl").is_file())
        finally:
            native._configure_runs_root(previous)

    def test_new_timing_tail_without_completed_historical_child_binding_stays_rejected(self):
        with self.assertRaises(native.ReviewRunError):
            native._validate_repair_credit_transition(
                self.parent, self.timing_child / "review-corrections.jsonl", runs_root=self.root)

    def test_historical_witness_rejects_resealed_source_and_request_result_drift(self):
        original = review_corrections._read_log(self.timing_child / "review-corrections.jsonl")[0]
        mutations = (
            {"parent_artifacts": {**original["parent_artifacts"], "proposals.json": "sha256:" + "0" * 64}},
            {"request_evidence_id": original["result_evidence_id"], "result_evidence_id": original["request_evidence_id"]},
            {"parent_run_id": self.wording_child.name},
        )
        for mutation in mutations:
            with self.subTest(fields=sorted(mutation)):
                record = {**copy.deepcopy(review_corrections._without_integrity(original)), **mutation}
                unsigned = {key: value for key, value in record.items() if key != "correction_id"}
                record["correction_id"] = "tcor-" + review_corrections.canonical_digest(unsigned)[7:31]
                with self.assertRaises(native.ReviewRunError):
                    native._validate_historical_timing_witness(
                        self.parent, self.timing_child, record, runs_root=self.root)


if __name__ == "__main__":
    unittest.main()
