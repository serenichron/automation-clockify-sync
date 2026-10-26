"""Execution telemetry must trigger editorial repair, not leak into client prose."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from scripts import caveman_renderer as renderer
from scripts import clockify_portfolio_repair as repair
from scripts import clockify_review_group_summary as summary
from scripts import semantic_analyzer as semantic
from test_semantic_analyzer import event, provider_response


TELEMETRY = (
    "SC — Verified receipt comparison with 38 subagent tests.",
    "SC — Verified receipt comparison with 373 relevant tests passing.",
    "SC — Verified receipt comparison with 1000 assertions passed.",
    "SC — Updated routing.py for reliable delegation.",
    "SC — Updated models.json for reliable delegation.",
    "SC — Updated wp-config.php for reliable client onboarding.",
    "SC — Updated styles.css for clearer client navigation.",
    "SC — Updated client.js for reliable client onboarding.",
    "SC — Updated client-node.js for reliable client onboarding.",
    "SC — Updated app.Node.js for reliable client onboarding.",
    "SC — Updated client.Next.js for reliable client onboarding.",
    "SC — Updated routing.js for reliable client onboarding.",
    "SC — Verified SHA-256 parity for receipt comparison.",
    "SC — Verified receipt comparison across 12 files.",
    "SC — Verified receipt comparison across 12 registry entries.",
)

CLIENT_OUTCOMES = (
    "TST — Revised Tests 16–18 for clearer exam preparation.",
    "TST — Revised practice tests for clearer exam preparation.",
    "SC — Expanded onboarding to 20 markets and improved conversion by 32 percent.",
    "SC — Set package price to 1000 euros for October 2026.",
    "SC — Prepared invoice dated 2026-10-22 for 12 service hours.",
    "SC — Updated 12 service pages for clearer client navigation.",
    "SC — Restored receipt comparison locally; deployment remains blocked.",
    "SC — Deployed receipt comparison after approval.",
    "SC — Improved checksum verification to prevent corrupted downloads.",
    "SC — Built Node.js integration for client onboarding.",
    "SC — Improved Next.js application accessibility.",
    "SC — Prepared Node.js integration locally; deployment remains blocked.",
    "SC — Deployed Next.js accessibility improvements after approval.",
    "SC — Improved Vue.js application accessibility.",
    "SC — Built Nuxt.js integration for client onboarding.",
    "SC — Improved Chart.js reporting for client planning.",
    "SC — Prepared Node.js-based integration locally; deployment remains blocked.",
    "SC — Improved React.js application accessibility.",
    "SC — Improved Angular.js application accessibility.",
    "SC — Improved D3.js reporting for client planning.",
    "SC — Improved Three.js product preview accessibility.",
)


class ExecutionMetadataTests(unittest.TestCase):
    def test_execution_telemetry_is_rejected_by_shared_description_contract(self):
        # Catches contextual telemetry escaping the shared renderer boundary.
        for description in TELEMETRY:
            with self.subTest(description=description):
                with self.assertRaisesRegex(renderer.CavemanValidationError, "execution detail"):
                    renderer.validate_client_description(description)

    def test_execution_telemetry_selects_existing_wording_repair_routes(self):
        # Catches repair consumers disagreeing with the shared renderer.
        for description in TELEMETRY:
            with self.subTest(description=description):
                self.assertIsNotNone(summary._wording_problem(description, {
                    "prefix": "SC", "children": [{"description": description}]}))
                # Historical source/evidence repair contracts do not change.
                self.assertEqual([], repair.repair_reasons(description))

    def test_business_numbers_product_tests_and_lifecycle_truth_are_preserved(self):
        # Catches blind number removal or a blanket ban on test/checksum concepts.
        for description in CLIENT_OUTCOMES:
            with self.subTest(description=description):
                try:
                    actual = renderer.validate_client_description(description)
                except renderer.CavemanValidationError as error:
                    self.fail(f"material client wording was rejected: {error}")
                self.assertEqual(description, actual)
                self.assertEqual([], repair.repair_reasons(description))
                self.assertIsNone(summary._wording_problem(description, {
                    "prefix": description.split(" — ")[0],
                    "children": [{"description": description}]}))


class StandaloneAdmissionTests(unittest.TestCase):
    def setUp(self):
        self.endpoint = semantic.AnalyzerEndpoint("fixture", "https://fixture.invalid", "fixture")
        self.events = [event("synthetic-evidence")]
        self.options = {"tier": "primary", "private_text_approved": True,
            "evidence_time_spans": {"synthetic-evidence": {
                "start": "2026-07-10 10:00", "end": "2026-07-10 10:10"}}}

    def response(self, body):
        response = provider_response(json.loads(body["messages"][1]["content"]))
        response["activities"][0].update(action="Verified", object="receipt comparison",
                                        outcome="with 38 subagent tests")
        return response

    def test_preexisting_accepted_semantic_decision_replays_without_rewriting_history(self):
        # Catches applying a fresh publication hygiene contract to sealed evidence decisions.
        body = semantic._body_for(self.events, model=self.endpoint.model, mode="extract",
                                  private_text_approved=True)
        response = self.response(body)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.jsonl"
            semantic.AnalyzerResponseCache(path).store_accepted(self.endpoint, body, response)
            before = path.read_bytes()
            replay = semantic._call_validated(self.endpoint, self.events,
                transport=lambda *_: self.fail("accepted replay must not infer"),
                cache=semantic.AnalyzerResponseCache(path), **self.options)
            self.assertEqual("with 38 subagent tests", replay["activities"][0]["outcome"])
            self.assertEqual(["synthetic-evidence"], replay["activities"][0]["evidence_ids"])
            self.assertEqual(before, path.read_bytes())


if __name__ == "__main__":
    unittest.main()
