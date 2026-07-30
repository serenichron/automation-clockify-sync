from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest
import csv


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "clockify_review_run.py"
SPEC = importlib.util.spec_from_file_location("clockify_review_run", SCRIPT)
review_run = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(review_run)


def item(item_id: str, description: str) -> dict:
    return {
        "id": item_id,
        "client_project": "Serenichron Level 2",
        "description": description,
    }


class ReviewRunResultTests(unittest.TestCase):
    def test_healthy_carried_queue_requires_no_comment(self):
        snapshot = {
            "summary": {
                "new": 0,
                "changed": 0,
                "carried_pending": 5,
                "resolved_disappeared": 0,
            },
            "categories": {
                "new": [],
                "changed": [],
                "carried_pending": [
                    item("rvi-old", "SC — unchanged private backlog text")
                ],
            },
            "coverage_warnings": [],
        }

        result = review_run.build_result(
            Path("/tmp/run-1"), {"status": "review_required", "summary": {}}, snapshot
        )

        self.assertEqual("no_comment", result["action"])
        self.assertFalse(result["should_comment"])
        self.assertEqual([], result["new"])
        self.assertEqual([], result["changed"])

    def test_changed_item_produces_delta_without_carried_backlog(self):
        snapshot = {
            "summary": {
                "new": 0,
                "changed": 1,
                "carried_pending": 4,
                "resolved_disappeared": 0,
            },
            "categories": {
                "new": [],
                "changed": [item("rvi-change", "SC — useful changed description")],
                "carried_pending": [
                    item("rvi-old", "SC — unchanged private backlog text")
                ],
            },
            "coverage_warnings": [],
        }

        result = review_run.build_result(
            Path("/tmp/run-2"), {"status": "review_required", "summary": {}}, snapshot
        )

        self.assertEqual("review_delta", result["action"])
        self.assertTrue(result["should_comment"])
        self.assertEqual(["rvi-change"], [row["id"] for row in result["changed"]])
        self.assertNotIn("carried_pending", result)

    def test_coverage_warning_outranks_delta(self):
        snapshot = {
            "summary": {
                "new": 1,
                "changed": 0,
                "carried_pending": 0,
                "resolved_disappeared": 0,
            },
            "categories": {
                "new": [item("rvi-new", "SC — new")],
                "changed": [],
            },
            "coverage_warnings": [
                {
                    "type": "source_unavailable",
                    "source": "clockify",
                    "reason": "Collector evidence status: error.",
                }
            ],
        }

        result = review_run.build_result(
            Path("/tmp/run-3"), {"status": "pass", "summary": {}}, snapshot
        )

        self.assertEqual("coverage_warning", result["action"])
        self.assertTrue(result["should_comment"])

    def test_blocked_quality_never_claims_external_writes(self):
        result = review_run.build_result(
            Path("/tmp/run-4"),
            {"status": "blocked", "summary": {"missing_candidate_keys": 1}},
            None,
        )

        self.assertEqual("blocked", result["action"])
        self.assertFalse(result["external_writes"])
        self.assertFalse(result["should_update_issue_description"])

    def test_summary_contains_only_actionable_delta(self):
        result = review_run.build_result(
            Path("/tmp/run-5"),
            {"status": "pass", "summary": {}},
            {
                "summary": {
                    "new": 1,
                    "changed": 0,
                    "carried_pending": 1,
                    "resolved_disappeared": 0,
                },
                "categories": {
                    "new": [item("rvi-new", "SC — actionable")],
                    "changed": [],
                    "carried_pending": [
                        item("rvi-old", "SC — must not be reprinted")
                    ],
                },
                "coverage_warnings": [],
            },
        )

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "summary.md"
            review_run.write_summary(path, result)
            text = path.read_text()

        self.assertIn("rvi-new", text)
        self.assertNotIn("rvi-old", text)
        self.assertNotIn("must not be reprinted", text)

    def test_current_review_csv_uses_stable_ids_and_includes_carried_items(self):
        snapshot = {
            "categories": {
                "new": [
                    {
                        **item("rvi-new", "SC — actionable"),
                        "duration_minutes": 20,
                        "disposition": "pending",
                        "tag_names": ["System development"],
                    }
                ],
                "changed": [],
                "carried_pending": [
                    {
                        **item("rvi-old", "SC — carried"),
                        "duration_minutes": 10,
                        "disposition": "pending",
                        "tag_names": ["Processes"],
                    }
                ],
            }
        }

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "review.csv"
            review_run.write_current_review_csv(path, snapshot)
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(["rvi-new", "rvi-old"], [row["Review ID"] for row in rows])
        self.assertEqual("System development", rows[0]["Tags"])
        self.assertEqual("10", rows[1]["Duration (min)"])


if __name__ == "__main__":
    unittest.main()
