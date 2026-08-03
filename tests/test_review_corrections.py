from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "review_corrections.py"
SPEC = importlib.util.spec_from_file_location("review_corrections", SCRIPT)
corrections = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(corrections)


def item(*, evidence_ids=None):
    return {
        "id": "rvi-stable",
        "current": {
            "activity_id": "act-stable",
            "workstream_id": "ws-stable",
            "evidence_ids": evidence_ids or ["ev-001", "ev-002"],
            "description": "SC — Rebuilt stable review state for evidence-backed work",
        },
    }


def decision(subject, kind="approve", **kwargs):
    return corrections.build_decision(
        subject,
        decision=kind,
        reviewer="reviewer-1",
        reviewed_at="2026-08-01T10:00:00+03:00",
        correction_categories=kwargs.pop("categories", ["wording"]),
        rationale=kwargs.pop("rationale", "Reviewed against the cited work evidence."),
        field_patch=kwargs.pop("field_patch", None),
        split_expectation=kwargs.pop("split_expectation", None),
        **kwargs,
    )


class ReviewCorrectionsTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tempdir.name) / "review-corrections.jsonl"

    def tearDown(self):
        self.tempdir.cleanup()

    def test_approve_skip_and_modify_decisions_are_evidence_bound(self):
        approved = decision(item())
        skipped = decision(item(), "skip", categories=["omission"])
        modified = decision(
            item(),
            "modify",
            categories=["wording", "routing"],
            field_patch={
                "description": {"op": "replace", "value": "SC — Corrected bounded work description"},
                "client_project": {"op": "replace", "value": "Serenichron Level 2"},
            },
        )
        for value in (approved, skipped, modified):
            self.assertTrue(value["evidence_fingerprint"].startswith("evfp:sha256:"))
            corrections.validate_decision(value, item=item())
        self.assertEqual({}, approved["field_patch"])
        self.assertEqual(["client_project", "description"], list(modified["field_patch"]))

    def test_append_is_idempotent_and_conflicts_fail_closed(self):
        first = decision(item())
        self.assertTrue(corrections.append_decision(self.path, first, item=item()))
        self.assertFalse(corrections.append_decision(self.path, first, item=item()))
        conflict = decision(item(), "skip", categories=["omission"])
        with self.assertRaisesRegex(corrections.ReviewDecisionError, "conflicting"):
            corrections.append_decision(self.path, conflict, item=item())
        self.assertEqual(1, len(corrections.load_decisions(self.path)))

    def test_stale_and_tampered_decisions_fail_closed(self):
        first = decision(item())
        with self.assertRaisesRegex(corrections.ReviewDecisionError, "stale"):
            corrections.append_decision(self.path, first, item=item(evidence_ids=["ev-new"]))
        self.assertTrue(corrections.append_decision(self.path, first, item=item()))
        line = json.loads(self.path.read_text(encoding="utf-8"))
        line["rationale"] = "tampered after review"
        self.path.write_text(json.dumps(line) + "\n", encoding="utf-8")
        with self.assertRaisesRegex(corrections.ReviewDecisionError, "decision_id|integrity"):
            corrections.load_decisions(self.path)

    def test_opaque_or_unbound_replacements_are_rejected(self):
        with self.assertRaisesRegex(corrections.ReviewDecisionError, "opaque"):
            decision(item(), "modify", field_patch={"replacement": {"op": "replace", "value": "copied"}})
        without_evidence = item(evidence_ids=[])
        without_evidence["current"].pop("evidence_ids")
        with self.assertRaisesRegex(corrections.ReviewDecisionError, "evidence_ids"):
            decision(without_evidence)

    def test_learning_cases_are_generalized_and_sanitized(self):
        record = decision(
            item(),
            "modify",
            categories=["wording", "allocation"],
            rationale="Remove alex@example.test from the wording and correct /private/path.",
            field_patch={"description": {"op": "replace", "value": "alex@example.test /private/path"}},
        )
        cases = corrections.derive_learning_cases([record])
        rendered = json.dumps(cases)
        self.assertEqual(2, len(cases))
        self.assertNotIn("alex@example.test", rendered)
        self.assertNotIn("/private/path", rendered)
        self.assertNotIn("reviewer-1", rendered)
        self.assertTrue(all("instruction" in value for value in cases))

    def test_exact_local_regression_case_is_executable_without_reviewer_metadata(self):
        record = decision(
            item(),
            "modify",
            categories=["wording", "routing"],
            rationale="Private reviewer rationale.",
            field_patch={
                "description": {"op": "replace", "value": "SC — Corrected bounded work description for stable review output"},
                "client_project": {"op": "replace", "value": "Serenichron Level 2"},
            },
        )
        case = corrections.derive_regression_cases([record])[0]
        self.assertTrue(case["local_only"])
        self.assertNotIn("reviewer", json.dumps(case))
        self.assertNotIn("Private reviewer rationale", json.dumps(case))
        proposal = {
            "activity_id": "act-stable",
            "description": "SC — Corrected bounded work description for stable review output",
            "client_project": "Serenichron Level 2",
            "provenance": {"evidence_ids": ["ev-001", "ev-002"]},
        }
        passed = corrections.evaluate_regression_cases([case], [proposal])
        self.assertEqual({"pass": 1, "fail": 0, "not_applicable": 0}, passed["summary"])
        proposal["description"] = "SC — Repeated the original bad description instead of learning"
        failed = corrections.evaluate_regression_cases([case], [proposal])
        self.assertEqual(1, failed["summary"]["fail"])

    def test_evidence_bound_skip_case_blocks_reappearing_activity(self):
        record = decision(item(), "skip", categories=["omission"])
        case = corrections.derive_regression_cases([record])[0]
        proposal = {
            "activity_id": "act-stable",
            "provenance": {"evidence_ids": ["ev-001", "ev-002"]},
        }
        self.assertEqual(
            1,
            corrections.evaluate_regression_cases([case], [proposal])["summary"]["fail"],
        )
        self.assertEqual(
            1,
            corrections.evaluate_regression_cases([case], [])["summary"]["pass"],
        )

    def test_missing_expected_activity_fails_visible_regression(self):
        record = decision(
            item(),
            "modify",
            field_patch={
                "description": {
                    "op": "replace",
                    "value": "SC — Corrected bounded work description for stable review output",
                }
            },
        )
        case = corrections.derive_regression_cases([record])[0]
        result = corrections.evaluate_regression_cases([case], [])
        self.assertEqual(1, result["summary"]["fail"])
        self.assertEqual(["reviewed activity is missing"], result["results"][0]["failures"])

    def test_split_correction_replay_requires_exact_child_evidence_partition(self):
        record = decision(
            item(evidence_ids=["ev-001", "ev-002", "ev-003"]),
            "modify",
            categories=["split"],
            split_expectation={
                "expected_child_count": 2,
                "expected_child_evidence_ids": [["ev-001", "ev-002"], ["ev-003"]],
            },
        )
        case = corrections.derive_regression_cases([record])[0]
        self.assertTrue(case["local_only"])
        self.assertEqual(2, case["expected_split"]["expected_child_count"])
        self.assertNotIn("Reviewed against the cited work evidence.", json.dumps(case))

        valid = [
            {"activity_id": "act-child-a", "provenance": {"evidence_ids": ["ev-001", "ev-002"]}},
            {"activity_id": "act-child-b", "provenance": {"evidence_ids": ["ev-003"]}},
        ]
        self.assertEqual(1, corrections.evaluate_regression_cases([case], valid)["summary"]["pass"])

        missing_child = valid[:1]
        missing_failures = corrections.evaluate_regression_cases([case], missing_child)["results"][0]["failures"]
        self.assertIn("split child count differs from reviewed value", missing_failures)
        self.assertIn("split child evidence union differs from reviewed value", missing_failures)

        wrong_count = [
            *valid,
            {"activity_id": "act-child-c", "provenance": {"evidence_ids": ["ev-003"]}},
        ]
        wrong_count_failures = corrections.evaluate_regression_cases([case], wrong_count)["results"][0]["failures"]
        self.assertIn("split child count differs from reviewed value", wrong_count_failures)
        self.assertIn("split child evidence is duplicated across activities", wrong_count_failures)

        duplicated = [
            {"activity_id": "act-child-a", "provenance": {"evidence_ids": ["ev-001", "ev-002"]}},
            {"activity_id": "act-child-b", "provenance": {"evidence_ids": ["ev-002", "ev-003"]}},
        ]
        duplicated_failures = corrections.evaluate_regression_cases([case], duplicated)["results"][0]["failures"]
        self.assertIn("split child evidence is duplicated across activities", duplicated_failures)
        self.assertIn("split child evidence partition differs from reviewed value", duplicated_failures)

        lost = [
            {"activity_id": "act-child-a", "provenance": {"evidence_ids": ["ev-001"]}},
            {"activity_id": "act-child-b", "provenance": {"evidence_ids": ["ev-003"]}},
        ]
        lost_failures = corrections.evaluate_regression_cases([case], lost)["results"][0]["failures"]
        self.assertIn("split child evidence union differs from reviewed value", lost_failures)
        self.assertIn("split child evidence partition differs from reviewed value", lost_failures)

    def test_split_contract_must_partition_the_reviewed_evidence(self):
        with self.assertRaisesRegex(corrections.ReviewDecisionError, "exactly partition"):
            decision(
                item(),
                "modify",
                categories=["split"],
                split_expectation={
                    "expected_child_count": 2,
                    "expected_child_evidence_ids": [["ev-001"], ["ev-other"]],
                },
            )
        with self.assertRaisesRegex(corrections.ReviewDecisionError, "require an executable"):
            decision(item(), "modify", categories=["split"])


if __name__ == "__main__":
    unittest.main()
