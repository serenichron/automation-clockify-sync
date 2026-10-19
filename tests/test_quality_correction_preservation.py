"""A clean proposal list cannot hide failed previously reviewed corrections."""
import copy
import unittest

from scripts import clockify_sync_quality as quality


class CorrectionPreservationQualityTests(unittest.TestCase):
    def report(self, regression=None, *, skipped=None):
        accounting = {"allocation_mode": "non_overlapping_v1", "external_writes": False,
                      "ledger_manifest": {"manifest_id": "synthetic-manifest"}}
        if regression is not None:
            accounting["correction_regression"] = copy.deepcopy(regression)
        if skipped is not None:
            accounting["skipped"] = copy.deepcopy(skipped)
        return quality.build_report("synthetic", [], {}, [], accounting=accounting)

    def test_failed_corrections_block_even_without_proposal_quality_defects(self):
        report = self.report({"schema_version": 1, "summary": {"pass": 88, "fail": 5},
                              "results": [{"status": "fail"}]})
        self.assertEqual("blocked", report["status"])
        self.assertEqual(1, report["summary"]["accounting_contract_issues"])
        self.assertIn("reviewed corrections", report["accounting_contract_issues"][0].lower())

    def test_failed_result_cannot_be_hidden_by_zero_failure_summary(self):
        report = self.report({"schema_version": 1, "summary": {"pass": 1, "fail": 0},
                              "results": [{"status": "fail"}]})
        self.assertEqual("blocked", report["status"])

    def test_passing_or_historical_absent_regressions_keep_prior_report_shape(self):
        absent = self.report()
        passing = self.report({"schema_version": 1, "summary": {"pass": 93, "fail": 0},
                               "results": [{"status": "pass"}, {"status": "not_applicable"}]})
        self.assertEqual("pass", absent["status"])
        self.assertEqual(absent, passing)

    def test_exact_successful_skip_satisfies_older_presence_expectation(self):
        missing = {"activity_id": "activity-a", "evidence_fingerprint": "evidence-a",
                   "decision": "modify", "status": "fail",
                   "failures": ["reviewed activity is missing"]}
        skip = {"activity_id": "activity-a", "evidence_fingerprint": "evidence-a",
                "decision": "skip", "status": "pass", "failures": []}
        report = self.report({"summary": {"fail": 1}, "results": [missing, skip]})
        self.assertEqual("pass", report["status"])
        self.assertEqual([], report["accounting_contract_issues"])

    def test_skip_cannot_hide_different_evidence_or_different_failed_expectation(self):
        for fingerprint, failures in (("evidence-b", ["reviewed activity is missing"]),
                                      ("evidence-a", ["duration_seconds differs from reviewed value"])):
            with self.subTest(fingerprint=fingerprint, failures=failures):
                report = self.report({"summary": {"fail": 1}, "results": [
                    {"activity_id": "activity-a", "evidence_fingerprint": fingerprint,
                     "decision": "modify", "status": "fail", "failures": failures},
                    {"activity_id": "activity-a", "evidence_fingerprint": "evidence-a",
                     "decision": "skip", "status": "pass", "failures": []},
                ]})
                self.assertEqual("blocked", report["status"])

    def test_final_evidence_bound_skip_is_not_a_surviving_regression(self):
        regression = {"summary": {"fail": 1}, "results": [{
            "activity_id": "activity-a", "evidence_fingerprint": "evidence-a",
            "regression_case_id": "case-a", "decision": "skip", "status": "fail",
            "failures": ["previously skipped activity reappeared"],
        }]}
        skipped = [{"id": "activity-a", "evidence_fingerprint": "evidence-a",
                    "reason": "preserved evidence-bound skip decision", "regression_case_ids": ["case-a"]}]
        self.assertEqual("pass", self.report(regression, skipped=skipped)["status"])
        skipped[0]["regression_case_ids"] = ["different-case"]
        self.assertEqual("blocked", self.report(regression, skipped=skipped)["status"])


if __name__ == "__main__":
    unittest.main()
