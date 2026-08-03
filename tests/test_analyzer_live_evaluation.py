from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import analyzer_evaluation  # noqa: E402
from scripts import analyzer_live_evaluation as live  # noqa: E402
from scripts import semantic_analyzer  # noqa: E402


def _activity(evidence_ids: list[str], spans: dict[str, dict[str, str]], index: int, concepts: list[str]) -> dict:
    return {
        "lifecycle": "completed",
        "workstream": "synthetic route evaluation",
        "action": "Verified",
        "object": f"{' '.join(concepts)} behavior {index}",
        "outcome": "against fixed evidence partitions",
        "evidence_ids": evidence_ids,
        "evidence_spans": [spans[evidence_id] for evidence_id in evidence_ids],
        "project_recommendation": {
            "name": "Serenichron Level 2",
            "prefix": "SC",
            "tag_names": ["Processes"],
        },
        "effort": {
            "minimum_minutes": 5,
            "recommended_minutes": 10,
            "maximum_minutes": 15,
        },
        "semantic_confidence": "high",
        "timing_confidence": "high",
        "split_rationale": "one bounded synthetic outcome",
        "merge_rationale": "duplicate evidence merged when applicable",
        "omit_rationale": "",
    }


class AnalyzerLiveEvaluationTests(unittest.TestCase):
    def test_synthetic_capture_produces_a_passing_digest_bound_scorecard(self) -> None:
        cases = {case["case_id"]: case for case in live.synthetic_cases()}
        ordered_cases = live.synthetic_cases()
        calls: list[dict] = []
        evidence_calls = 0

        def transport(_endpoint: semantic_analyzer.AnalyzerEndpoint, body: dict) -> dict:
            nonlocal evidence_calls
            calls.append(body)
            user_content = str(body["messages"][-1]["content"])
            if '"probe"' in user_content:
                return {"probe": "ok"}
            payload = json.loads(user_content)
            events = payload["events"]
            evidence_ids = sorted(str(event["evidence_id"]) for event in events)
            case = ordered_cases[evidence_calls // 2]
            evidence_calls += 1
            original_ids = sorted(str(event["evidence_id"]) for event in case["events"])
            aliases = dict(zip(original_ids, evidence_ids, strict=True))
            spans = {str(event["evidence_id"]): event["time_span"] for event in events}
            activities = [
                _activity(
                    [aliases[value] for value in partition],
                    spans,
                    index,
                    case["expected_activity_concepts"][index - 1]["required_terms"],
                )
                for index, partition in enumerate(case["expected_activity_partitions"], 1)
            ]
            exceptions = []
            omissions = []
            if not activities:
                if case["case_id"].endswith("title-only-meeting"):
                    exceptions = [{
                        "kind": "insufficient_evidence",
                        "evidence_ids": evidence_ids,
                        "reason": "title alone cannot support a meeting outcome",
                    }]
                else:
                    omissions = [{
                        "lifecycle": "noise",
                        "evidence_ids": evidence_ids,
                        "reason": "waiting status contains no substantive work",
                    }]
            return {"activities": activities, "exceptions": exceptions, "omissions": omissions}

        endpoint = semantic_analyzer.AnalyzerEndpoint(
            name="synthetic-test", url="https://example.invalid/v1/chat", model="fixture-model"
        )
        capture = live.capture_evaluation(endpoint, tier="primary", transport=transport)
        scorecard = analyzer_evaluation.evaluate(capture)

        self.assertTrue(scorecard["passed"])
        self.assertEqual(5, scorecard["case_count"])
        self.assertEqual(11, len(calls))
        outbound = semantic_analyzer.canonical_json(calls)
        self.assertNotIn("/Users/", outbound)
        self.assertNotIn("/home/", outbound)
        self.assertNotIn("@", outbound)

    def test_live_capture_requires_two_replays(self) -> None:
        endpoint = semantic_analyzer.AnalyzerEndpoint(
            name="synthetic-test", url="https://example.invalid", model="fixture-model"
        )
        with self.assertRaisesRegex(analyzer_evaluation.EvaluationError, "at least two"):
            live.capture_evaluation(endpoint, tier="primary", replay_count=1)


if __name__ == "__main__":
    unittest.main()
