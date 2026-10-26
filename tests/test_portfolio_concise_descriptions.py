"""Client-facing concise wording must not trigger filler or semantic repair."""
import unittest

from scripts import clockify_portfolio_quality as quality
from scripts import clockify_portfolio_repair as repair
from test_portfolio_quality import document, fathom_event, ledger


APPROVED_DESCRIPTIONS = (
    "M&P — Uploaded missing Mazilu Drive files",
    'M&P — Created sheet "Inventar documente"',
    "MB — Corrected email-rendering metadata",
    "SC — Verified advertising-page routing",
)


class ConcisePortfolioDescriptionTests(unittest.TestCase):
    def test_execution_detail_regressions_require_cleanup_before_review(self):
        event = fathom_event()
        descriptions = (
            "M&P — Uploaded missing Mazilu Drive files 249 files uploaded with identical md5, 2 already present",
            "M&P — Created Inventar documente sheet 392 MP-DOC rows with 383 Copiat and 9 Exclus",
            "TSTP — Built Speech demonstration module with 47 tests and 32 browser checks",
        )
        for description in descriptions:
            with self.subTest(description=description):
                self.assertTrue(repair.repair_reasons(description))
                report = quality.audit(document(event, description), ledger(event))
                self.assertEqual(len(report["semantic_repair_rows"]), 1)

    def test_client_relevant_quantities_are_not_blanket_rejected(self):
        for description in (
            "M&P — Reviewed 12 invoices for the client",
            "SC — Planned delivery capacity for two client engagements",
            "TSTP — Improved test coverage for the Speech module",
        ):
            with self.subTest(description=description):
                self.assertEqual([], repair.repair_reasons(description))

    def test_approved_descriptions_need_no_wording_repair(self):
        for description in APPROVED_DESCRIPTIONS:
            with self.subTest(description=description):
                self.assertEqual(repair.repair_reasons(description), [])

    def test_approved_descriptions_need_no_audit_semantic_repair(self):
        event = fathom_event()
        for description in APPROVED_DESCRIPTIONS:
            with self.subTest(description=description):
                report = quality.audit(document(event, description), ledger(event))
                self.assertEqual(report["semantic_repair_rows"], [])
                self.assertEqual(report["structural_issues"], [])
                self.assertEqual(report["status"], "pass")

    def test_concise_decisions_render_without_filler(self):
        project = {"name": "Mazilu", "prefix": "M&P", "tag_names": ["Administration"]}
        taxonomy = [{"project_name": "Mazilu", "prefix": "M&P", "tag_names": ["Administration"]}]
        cases = (
            ("Uploaded", "missing Mazilu Drive", "files", APPROVED_DESCRIPTIONS[0]),
            ("Created", "sheet", '"Inventar documente"', APPROVED_DESCRIPTIONS[1]),
        )
        for action, object_, outcome, expected in cases:
            with self.subTest(description=expected):
                decision = repair._validate_wording_decision({
                    "disposition": "activity", "action": action, "object": object_,
                    "outcome": outcome, "project_recommendation": project,
                    "exception_kind": "", "reason": "",
                }, taxonomy=taxonomy)
                self.assertEqual(repair._render_wording_decision(decision, prefix="M&P"), expected)

    def test_unsafe_or_overlong_descriptions_still_require_repair(self):
        event = fathom_event()
        descriptions = (
            "M&P — Uploaded /private/files",
            "M&P — Uploaded files https://example.com",
            "M&P — NEEDS REVIEW missing files",
            "M&P — Uploaded missing missing files",
            "M&P — Uploaded",
            "M&P — Uploaded " + " ".join(f"file{number}" for number in range(23)),
        )
        for description in descriptions:
            with self.subTest(description=description):
                self.assertTrue(repair.repair_reasons(description))
                report = quality.audit(document(event, description), ledger(event))
                self.assertEqual(len(report["semantic_repair_rows"]), 1)


if __name__ == "__main__":
    unittest.main()
