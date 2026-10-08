from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "caveman_renderer.py"
SPEC = importlib.util.spec_from_file_location("caveman_renderer", MODULE_PATH)
assert SPEC and SPEC.loader
renderer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = renderer
SPEC.loader.exec_module(renderer)


class CavemanRendererTests(unittest.TestCase):
    def test_verification_telemetry_is_not_client_description_content(self):
        for description in (
            "SC — Repaired payment checkout with 47 tests passing",
            "SC — Repaired payment checkout after passing build",
            "SC — Repaired payment checkout with build status successful",
            "SC — Repaired payment checkout with 32 browser checks passed",
            "SC — Repaired payment checkout after 47 passing tests",
            "SC — Repaired payment checkout after tests passed: 47",
            "SC — Repaired payment checkout with all checks passed",
        ):
            with self.subTest(description=description):
                with self.assertRaisesRegex(renderer.CavemanValidationError, "verification telemetry"):
                    renderer.validate_description(description)

    def test_real_testing_work_and_business_quantities_remain_client_content(self):
        for description in (
            "SC — Built a test harness for reliable payment validation",
            "SC — Verified payment rules for reliable client checkout",
            "SC — Created 47 regression tests for payment validation",
            "SC — Built 32 assessment tests for student practice",
            "SC — Built assessment module with 32 tests for student practice",
            "SC — Rewrote 33 internal links across priority service pages",
        ):
            with self.subTest(description=description):
                self.assertEqual(description, renderer.validate_description(description))

    def test_hard_hygiene_preserves_business_numbers_and_ordinary_hex_words(self):
        for description in (
            "SC — Processed 1000000 orders for accurate account reconciliation",
            "SC — Documented defaced image for client remediation planning",
        ):
            with self.subTest(description=description):
                self.assertEqual(description, renderer.validate_client_description_hygiene(description))

    def test_hard_hygiene_rejects_mixed_and_explicit_numeric_or_alphabetic_commit_ids(self):
        for token in ("799a44e", "commit 1234567", "SHA deadbee", "commit deadbee"):
            with self.subTest(token=token):
                with self.assertRaisesRegex(renderer.CavemanValidationError, "hash"):
                    renderer.validate_client_description_hygiene(
                        f"SC — Repaired payment checkout with {token}"
                    )

    def test_portfolio_mode_allows_named_technical_slashes_but_not_paths(self):
        self.assertEqual(
            "SC — Configured Sol/Terra routing for reliable delegated execution",
            renderer.validate_description(
                "SC — Configured Sol/Terra routing for reliable delegated execution",
                allow_compact_technical_slashes=True,
            ),
        )
        with self.assertRaisesRegex(renderer.CavemanValidationError, "path"):
            renderer.validate_description(
                "SC — Reviewed /Users/private/session output for invoice preparation",
                allow_compact_technical_slashes=True,
            )
        self.assertEqual(
            "SC — Fixed exec_command routing and prepared handoff prompt",
            renderer.validate_description(
                "SC — Fixed exec_command routing and prepared handoff prompt",
                allow_compact_technical_underscores=True,
            ),
        )
    def test_representative_examples_follow_the_contract(self):
        cases = [
            (
                {"prefix": "SC", "action": "Rewrote", "object": "thirty-three internal links", "outcome": "across priority service pages"},
                "SC — Rewrote thirty-three internal links across priority service pages",
            ),
            (
                {"prefix": "SC", "action": "Investigated", "object": "twelve broken links", "outcome": "for affected marketing pages"},
                "SC — Investigated twelve broken links for affected marketing pages",
            ),
            (
                {"prefix": "LoA", "action": "Defined", "object": "onboarding needs", "outcome": "during prospect discovery call"},
                "LoA — Defined onboarding needs during prospect discovery call",
            ),
        ]
        for parts, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(expected, renderer.render_caveman_description(parts))

    def test_short_user_examples_are_valid_caveman_descriptions(self):
        for description in (
            "SC — Reduced Honcho memory use",
            "SC — Wrote Honcho rollout plan",
            "SC — Rewrote 33 internal links",
        ):
            with self.subTest(description=description):
                self.assertEqual(description, renderer.validate_description(description))

    def test_fewer_than_five_words_is_rejected(self):
        with self.assertRaisesRegex(renderer.CavemanValidationError, "at least 5 words"):
            renderer.validate_description("SC — Fixed clock sync")

    def test_adversarial_forbidden_content_fails_closed(self):
        base = {"prefix": "SC", "action": "Reviewed", "object": "service page", "outcome": "for client conversion planning"}
        forbidden = {
            "needs review marker": "[NEEDS REVIEW]",
            "Unicode needs review marker": "[needs\u00a0review]",
            "spaced needs review marker": "[needs _ review]",
            "Markdown": "*Markdown*",
            "two-dot truncation": "unfinished..",
            "spaced-dot truncation": "unfinished. . .",
            "Unicode ellipsis": "unfinished…",
            "Unicode midline ellipsis": "unfinished⋯",
            "URL": "https://example.com",
            "bare domain": "portal.example.com",
            "path": "/Users/blackthorne/Work",
            "hash": "deadbeef",
            "email": "hello@example.com",
            "git command": "git status",
            "pytest command": "pytest -q",
            "python pytest command": "python3 -m pytest -q",
            "npm command": "npm run test",
            "prompt": "system prompt",
            "rule framing": "follow these rules",
            "instruction framing": "must always obey",
            "in-progress status": "still in progress",
            "running status": "status: running",
            "agent status": "agent completed",
            "evidence dump": "evidence dump",
        }
        for label, token in forbidden.items():
            with self.subTest(label=label, token=token):
                row = dict(base)
                row["outcome"] = f"for client conversion planning {token}"
                result = renderer.try_render_caveman_description(row)
                self.assertFalse(result.ok)
                self.assertIsNone(result.description)
                self.assertIsInstance(result.error, renderer.CavemanValidationError)

    def test_substantive_approval_polling_and_heartbeat_work_is_not_blanket_banned(self):
        allowed = [
            "SC — Documented agent routing rules for reliable task delegation",
            "SC — Verified approval requirements for guarded rollout readiness",
            "SC — Tested health polling after service recovery work",
            "SC — Repaired heartbeat monitoring after service failure for stable operations",
            "SC — Built immutable evidence ledger for reliable session reconstruction",
            "SC — Filtered error logs to remove repeated transport noise",
        ]
        for description in allowed:
            with self.subTest(description=description):
                self.assertEqual(description, renderer.validate_description(description))

    def test_long_atomic_result_is_preserved_without_truncation(self):
        parts = {
            "prefix": "SC",
            "action": "Implemented stable review identity",
            "object": "review identity from activity evidence fingerprints",
            "outcome": "same identity survives allocation movement",
        }
        self.assertEqual(
            (
                "SC — Implemented stable review identity review identity from activity "
                "evidence fingerprints same identity survives allocation movement"
            ),
            renderer.render_caveman_description(parts),
        )

    def test_adjacent_repeated_words_are_rejected(self):
        with self.assertRaisesRegex(renderer.CavemanValidationError, "repeated words"):
            renderer.validate_description(
                "SC — Wrote guarded rollout plan plan for safe deployment"
            )

    def test_no_field_is_silently_rewritten(self):
        parts = {"prefix": "SC", "action": "Reviewed", "object": "service page", "outcome": "for client conversion planning"}
        result = renderer.render_caveman_description(parts)
        self.assertEqual("SC — Reviewed service page for client conversion planning", result)
        self.assertEqual(parts["action"], "Reviewed")


if __name__ == "__main__":
    unittest.main()
