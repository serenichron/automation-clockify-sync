"""Contract tests for the sanitized Clockify behavioral regression corpus."""
from __future__ import annotations

import hashlib
import json
import re
import unittest
from pathlib import Path
from typing import Any

from scripts import caveman_renderer
from scripts import evidence_ledger


CORPUS_DIR = Path(__file__).parent / "fixtures" / "clockify-regression" / "v1"
RECORDS_PATH = CORPUS_DIR / "records.jsonl"
MANIFEST_PATH = CORPUS_DIR / "manifest.json"
SUPPLIED_SOURCE_PATH = Path("/Users/blackthorne/.codex/attachments/976a8304-0ac1-4cee-87c9-443f907db87f/pasted-text.txt")
CONCRETE_CATEGORIES = {
    "evidence_backed_render",
    "exception_insufficient_evidence",
    "forbidden_status_dump",
    "neutral_meeting",
    "omit_noise",
    "prompt_not_outcome",
    "secret_removal_outcome",
    "sensitive_identifier_redacted",
    "split_required",
    "title_only_meeting",
    "unlabeled_session",
}


def load_records() -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in RECORDS_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class RegressionCorpusTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.records = load_records()
        cls.manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        cls.by_source_line = {record["source_line"]: record for record in cls.records}

    def test_fixture_is_complete_and_content_addressed(self) -> None:
        self.assertEqual("clockify-regression-corpus/v1", self.manifest["schema_version"])
        self.assertEqual(86, self.manifest["source"]["logical_record_count"])
        self.assertEqual(86, len(self.records))
        self.assertEqual(list(range(1, 87)), sorted(self.by_source_line))
        self.assertEqual(86, len({record["record_id"] for record in self.records}))
        self.assertEqual(
            self.manifest["records_digest"],
            hashlib.sha256(RECORDS_PATH.read_bytes()).hexdigest(),
        )
        self.assertRegex(self.manifest["source"]["sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(self.manifest["source_feature_digest"], r"^[0-9a-f]{64}$")
        self.assertIn("Human review remains required", self.manifest["semantic_mapping_boundary"])

    def test_every_record_has_specific_behavior_and_a_valid_disposition(self) -> None:
        for record in self.records:
            with self.subTest(record=record["record_id"]):
                categories = set(record.get("behavior_categories", []))
                self.assertTrue(categories)
                self.assertTrue(
                    categories & CONCRETE_CATEGORIES,
                    "generic-only categories are not an executable behavioral case",
                )
                disposition = record.get("expected_disposition")
                self.assertIn(disposition, {"omit", "exception", "render", "split"})
                if disposition == "omit":
                    self.assertTrue(categories & {"omit_noise", "prompt_not_outcome"})
                    self.assertNotIn("expected_render_parts", record)
                elif disposition == "exception":
                    self.assertIn("exception_insufficient_evidence", categories)
                    self.assertNotIn("expected_render_parts", record)
                elif disposition == "render":
                    self.assertIn("evidence_backed_render", categories)
                    self.assertIsInstance(record.get("expected_render_parts"), dict)
                    self.assertIsInstance(record.get("expected_descriptions"), list)
                    self.assertEqual(1, len(record["expected_descriptions"]))
                else:
                    self.assertIn("split_required", categories)
                    self.assertIsInstance(record.get("expected_render_parts"), list)
                    self.assertGreaterEqual(len(record.get("expected_descriptions", [])), 2)

    def test_supported_descriptions_pass_the_public_renderer(self) -> None:
        for record in self.records:
            parts = record.get("expected_render_parts")
            if parts is None:
                continue
            expected = record.get("expected_descriptions")
            part_list = parts if isinstance(parts, list) else [parts]
            rendered = []
            for item in part_list:
                description = caveman_renderer.render_caveman_description(item)
                self.assertEqual(description, caveman_renderer.validate_description(description))
                rendered.append(description)
            self.assertIsInstance(expected, list)
            self.assertEqual(expected, rendered)

    def test_feature_contract_matches_private_source_only_when_available(self) -> None:
        # Portable clones can still verify the committed no-prose contract.
        evidence_ledger.verify_regression_corpus(self.records, self.manifest)
        if not SUPPLIED_SOURCE_PATH.exists():
            self.skipTest("private supplied attachment is not available in this portable checkout")
        evidence_ledger.verify_regression_corpus(
            self.records,
            self.manifest,
            source_path=SUPPLIED_SOURCE_PATH,
        )

    def test_feature_contract_does_not_claim_semantic_mapping_is_mechanical(self) -> None:
        self.assertIn("Human review remains required", self.manifest["semantic_mapping_boundary"])
        generated = evidence_ledger.build_regression_corpus(["[NEEDS REVIEW] pytest -q ..."])
        self.assertEqual(["command_like", "markup", "needs_review", "status_like", "truncation_ellipsis"], generated[0]["source_features"])
        self.assertNotIn("expected_disposition", generated[0])

    def test_sanitized_payload_contains_no_raw_description_material(self) -> None:
        payload = RECORDS_PATH.read_text(encoding="utf-8")
        self.assertNotRegex(payload, r"(?i)\b(?:https?|ftp)://|\bwww\.")
        self.assertNotRegex(payload, r"\b[^\s@\"]+@[^\s@\"]+\.[^\s@\"]+")
        self.assertNotRegex(payload, r"(?i)(?:^|[\"'\s])/(?:users|home|private)/")
        self.assertNotRegex(payload, r"\[NEEDS[_ ]REVIEW\]|\.\.\.")
        self.assertNotIn("sanitized_text", payload)
        self.assertNotRegex(payload, r"\b[0-9a-f]{32,}\b")

    def test_critical_cases_have_the_required_fail_closed_behavior(self) -> None:
        for line in (13, 34, 60, 69, 71):
            record = self.by_source_line[line]
            self.assertEqual("exception", record["expected_disposition"])
            self.assertTrue(
                {"title_only_meeting", "neutral_meeting", "exception_insufficient_evidence"}
                <= set(record["behavior_categories"])
            )

        unlabeled = self.by_source_line[42]
        self.assertEqual("exception", unlabeled["expected_disposition"])
        self.assertTrue(
            {"unlabeled_session", "exception_insufficient_evidence"}
            <= set(unlabeled["behavior_categories"])
        )

        for line in (23, 44, 48):
            record = self.by_source_line[line]
            self.assertEqual("omit", record["expected_disposition"])
            self.assertIn("omit_noise", record["behavior_categories"])

        for line in (7, 33, 35, 41, 58, 73, 84, 86):
            record = self.by_source_line[line]
            self.assertEqual("omit", record["expected_disposition"])
            self.assertIn("prompt_not_outcome", record["behavior_categories"])

        split = self.by_source_line[29]
        self.assertEqual("split", split["expected_disposition"])
        self.assertEqual(
            [
                "SC — Rewrote 33 internal links across project documentation",
                "SC — Investigated 12 dangling internal links after link rewriting",
            ],
            split["expected_descriptions"],
        )

        for line in (51, 68):
            record = self.by_source_line[line]
            self.assertEqual("render", record["expected_disposition"])
            self.assertIn("secret_removal_outcome", record["behavior_categories"])
            self.assertIn("Removed exposed secrets", caveman_renderer.render(record["expected_render_parts"]))


if __name__ == "__main__":
    unittest.main()
