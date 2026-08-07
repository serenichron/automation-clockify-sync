from __future__ import annotations

import copy
import unittest

from scripts import analyzer_evaluation as evaluation


def event_id(number: int) -> str:
    return f"ev-{number:064x}"


def activity(evidence_id: str, *, action: str = "Fixed", outcome: str = "removed raw message fragments") -> dict:
    return {
        "lifecycle": "completed",
        "action": action,
        "object": "Clockify descriptions",
        "outcome": outcome,
        "evidence_ids": [evidence_id],
        "evidence_spans": [{"start": "2026-08-01T10:00:00+03:00", "end": "2026-08-01T10:10:00+03:00"}],
        "project_recommendation": {"name": "Serenichron Level 2", "prefix": "SC", "tag_names": ["Processes"]},
        "effort": {"minimum_minutes": 10, "recommended_minutes": 20, "maximum_minutes": 30},
        "semantic_confidence": "high",
        "timing_confidence": "high",
        "split_rationale": "one proven outcome",
        "merge_rationale": "",
    }


def response(evidence_id: str, **activity_changes: object) -> dict:
    row = activity(evidence_id)
    row.update(activity_changes)
    return {"activities": [row], "exceptions": [], "omissions": []}


def document() -> dict:
    evidence = event_id(1)
    records = [{"evidence_id": evidence, "time_span": {"start": "2026-08-01T10:00:00+03:00", "end": "2026-08-01T10:10:00+03:00"}}]
    return {
        "schema_version": evaluation.INPUT_SCHEMA_VERSION,
        "corpus": {"records": records, "digest": evaluation.sha256_hex(records)},
        "route": {
            "route_id": "ollama-cloud-primary",
            "model": "deepseek-v4-flash:0731-cloud",
            "revision": "d3f1c87447216481a8001f48c517a51e13bfb141853a8df5e52f81bf765dabc3",
            "tier": "primary",
        },
        "prompt_version": "clockify-semantic-v16",
        "semantic_schema_version": 1,
        "evidence_bundle_schema_version": "clockify-semantic-evidence-bundle/v1",
        "cases": [{
            "case_id": "one-atomic-outcome",
            "evidence_ids": [evidence],
            "expected_activity_partitions": [[evidence]],
            "expected_activity_concepts": [{
                "evidence_ids": [evidence],
                "required_terms": ["clockify"],
            }],
            "expected_exceptions": [],
            "expected_omissions": [],
            "replays": [response(evidence), response(evidence)],
        }],
    }


class AnalyzerEvaluationTests(unittest.TestCase):
    def test_moving_cloud_route_requires_release_revision(self) -> None:
        source = document()
        del source["route"]["revision"]
        with self.assertRaisesRegex(evaluation.EvaluationError, "release revision"):
            evaluation.evaluate(source)

    def test_ledger_spans_replace_missing_or_model_invented_spans(self) -> None:
        source = document()
        source["cases"][0]["replays"][0]["activities"][0].pop("evidence_spans")
        source["cases"][0]["replays"][1]["activities"][0]["evidence_spans"] = [
            {"start": "1999-01-01T00:00:00Z", "end": "1999-01-01T01:00:00Z"}
        ]

        scorecard = evaluation.evaluate(source)

        self.assertTrue(scorecard["passed"])

    def test_produces_digest_bound_passing_scorecard(self) -> None:
        scorecard = evaluation.evaluate(document())
        self.assertTrue(scorecard["passed"])
        self.assertEqual("clockify-analyzer-evaluator/v5", scorecard["evaluator_version"])
        self.assertEqual("deepseek-v4-flash:0731-cloud", scorecard["route"]["model"])
        self.assertEqual(64, len(scorecard["input_corpus_digest"]))
        self.assertEqual(scorecard, evaluation.verify_scorecard(scorecard))

    def test_invalid_semantic_schema_or_citations_fails_scorecard(self) -> None:
        source = document()
        source["cases"][0]["replays"][1]["activities"][0]["evidence_ids"] = [event_id(2)]
        scorecard = evaluation.evaluate(source)
        self.assertFalse(scorecard["passed"])
        self.assertFalse(scorecard["results"][0]["checks"]["schema_valid"])
        self.assertFalse(scorecard["results"][0]["checks"]["evidence_citations_valid"])

    def test_atomicity_mismatch_is_rejected(self) -> None:
        source = document()
        source["cases"][0]["expected_activity_partitions"] = []
        source["cases"][0]["expected_activity_concepts"] = []
        source["cases"][0]["expected_exceptions"] = [{
            "evidence_ids": source["cases"][0]["evidence_ids"],
            "kind": "insufficient_evidence",
        }]
        scorecard = evaluation.evaluate(source)
        self.assertFalse(scorecard["passed"])
        self.assertFalse(scorecard["results"][0]["checks"]["atomicity_valid"])

    def test_wrong_nonactivity_disposition_is_rejected(self) -> None:
        source = document()
        case = source["cases"][0]
        evidence = case["evidence_ids"][0]
        case["expected_activity_partitions"] = []
        case["expected_activity_concepts"] = []
        case["expected_exceptions"] = [{
            "evidence_ids": [evidence],
            "kind": "insufficient_evidence",
        }]
        wrong = {
            "activities": [],
            "exceptions": [],
            "omissions": [{
                "lifecycle": "noise",
                "evidence_ids": [evidence],
                "reason": "wrong disposition",
            }],
        }
        case["replays"] = [wrong, copy.deepcopy(wrong)]

        scorecard = evaluation.evaluate(source)

        self.assertFalse(scorecard["passed"])
        self.assertFalse(
            scorecard["results"][0]["checks"]["nonactivity_dispositions_valid"]
        )

    def test_planned_and_noise_are_stable_equivalent_omissions(self) -> None:
        source = document()
        case = source["cases"][0]
        evidence = case["evidence_ids"][0]
        case["expected_activity_partitions"] = []
        case["expected_activity_concepts"] = []
        case["expected_omissions"] = [{
            "evidence_ids": [evidence],
            "lifecycles": ["noise", "planned"],
        }]
        case["replays"] = [
            {
                "activities": [],
                "exceptions": [],
                "omissions": [{
                    "lifecycle": lifecycle,
                    "evidence_ids": [evidence],
                    "reason": "no work performed",
                }],
            }
            for lifecycle in ("planned", "noise")
        ]

        scorecard = evaluation.evaluate(source)

        self.assertTrue(scorecard["passed"])
        self.assertTrue(scorecard["results"][0]["checks"]["stable_replay"])

    def test_forbidden_description_content_is_rejected_by_production_renderer(self) -> None:
        source = document()
        for replay in source["cases"][0]["replays"]:
            replay["activities"][0]["outcome"] = "left [NEEDS REVIEW] pytest -q"
        scorecard = evaluation.evaluate(source)
        self.assertFalse(scorecard["passed"])
        self.assertFalse(scorecard["results"][0]["checks"]["forbidden_descriptions_rejected"])

    def test_different_review_decision_is_rejected(self) -> None:
        source = document()
        source["cases"][0]["replays"][1]["activities"][0]["semantic_confidence"] = "medium"
        scorecard = evaluation.evaluate(source)
        self.assertFalse(scorecard["passed"])
        self.assertFalse(scorecard["results"][0]["checks"]["stable_replay"])

    def test_bounded_effort_and_timing_drift_preserves_review_stability(self) -> None:
        source = document()
        second = source["cases"][0]["replays"][1]["activities"][0]
        second["action"] = "Corrected"
        second["outcome"] = "cleared copied transcript fragments"
        second["effort"] = {
            "minimum_minutes": 5,
            "recommended_minutes": 5,
            "maximum_minutes": 10,
        }
        second["timing_confidence"] = "low"

        scorecard = evaluation.evaluate(source)

        self.assertTrue(scorecard["passed"])
        self.assertTrue(scorecard["results"][0]["checks"]["stable_replay"])

    def test_harmless_wording_drift_preserves_review_stability(self) -> None:
        source = document()
        second = source["cases"][0]["replays"][1]["activities"][0]
        second["action"] = "Corrected"
        second["outcome"] = "removed unsafe report fragments"
        second["split_rationale"] = "one separately supported result"
        scorecard = evaluation.evaluate(source)
        self.assertTrue(scorecard["passed"])
        self.assertTrue(scorecard["results"][0]["checks"]["stable_replay"])

    def test_vague_description_missing_concrete_concepts_is_rejected(self) -> None:
        source = document()
        for replay in source["cases"][0]["replays"]:
            replay["activities"][0]["object"] = "generic process"
            replay["activities"][0]["outcome"] = "completed expected work safely"
        scorecard = evaluation.evaluate(source)
        self.assertFalse(scorecard["passed"])
        self.assertFalse(scorecard["results"][0]["checks"]["semantic_content_valid"])

    def test_incomplete_or_tampered_input_fails_closed(self) -> None:
        source = document()
        source["corpus"]["records"][0]["evidence_id"] = event_id(9)
        with self.assertRaisesRegex(evaluation.EvaluationError, "corpus digest"):
            evaluation.evaluate(source)
        source = document()
        source["cases"][0]["replays"] = source["cases"][0]["replays"][:1]
        with self.assertRaisesRegex(evaluation.EvaluationError, "at least two"):
            evaluation.evaluate(source)
        source = document()
        source["corpus"]["records"].append({
            "evidence_id": event_id(2),
            "time_span": {"start": "2026-08-01T11:00:00+03:00", "end": "2026-08-01T11:10:00+03:00"},
        })
        source["corpus"]["digest"] = evaluation.sha256_hex(source["corpus"]["records"])
        with self.assertRaisesRegex(evaluation.EvaluationError, "complete corpus"):
            evaluation.evaluate(source)

    def test_corpus_timestamp_order_uses_instants_not_string_order(self) -> None:
        source = document()
        source["corpus"]["records"][0]["time_span"] = {
            "start": "2026-08-01T10:00:00+03:00",
            "end": "2026-08-01T08:00:00Z",
        }
        source["corpus"]["digest"] = evaluation.sha256_hex(source["corpus"]["records"])

        scorecard = evaluation.evaluate(source)

        self.assertTrue(scorecard["passed"])

    def test_scorecard_tampering_is_detected(self) -> None:
        scorecard = evaluation.evaluate(document())
        tampered = copy.deepcopy(scorecard)
        tampered["results"][0]["passed"] = False
        with self.assertRaisesRegex(evaluation.EvaluationError, "digest"):
            evaluation.verify_scorecard(tampered)


if __name__ == "__main__":
    unittest.main()
