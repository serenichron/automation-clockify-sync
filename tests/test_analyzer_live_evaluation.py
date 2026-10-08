from __future__ import annotations

import json
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import analyzer_evaluation  # noqa: E402
from scripts import analyzer_live_evaluation as live  # noqa: E402
from scripts import semantic_analyzer  # noqa: E402


def _partitions(members: list[dict]) -> list[dict]:
    grouped: dict[str, list[int]] = {}
    for member in members:
        grouped.setdefault(member["bundle_ref"], []).append(member["member"])
    return [
        {
            "bundle_ref": bundle_ref,
            "member_ranges": [[position, position] for position in sorted(positions)],
        }
        for bundle_ref, positions in grouped.items()
    ]


def _activity(members: list[dict], index: int, concepts: list[str]) -> dict:
    return {
        "lifecycle": "completed",
        "workstream": "synthetic route evaluation",
        "action": "Verified",
        "object": f"{' '.join(concepts)} behavior {index}",
        "outcome": "against fixed evidence partitions",
        "evidence_partitions": _partitions(members),
        "evidence_spans": [member["time_span"] for member in members],
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
    def test_invalid_probe_json_records_synthetic_response_and_stage(self) -> None:
        raw = {"choices": [{"message": {"content": "not-json"}}]}
        endpoint = semantic_analyzer.AnalyzerEndpoint(
            name="synthetic-test",
            url="https://example.invalid/v1/chat",
            model="fixture-model",
            revision="a" * 64,
            reasoning_effort="none",
        )

        try:
            live.capture_evaluation(
                endpoint,
                tier="primary",
                transport=lambda _endpoint, _body: raw,
            )
        except Exception as exc:  # The diagnostic contract is asserted below.
            failure = exc
        else:
            self.fail("invalid probe JSON must fail qualification")

        capture = getattr(failure, "failure_capture", None)
        self.assertIsInstance(capture, dict)
        self.assertEqual("failed", capture["status"])
        self.assertEqual("probe", capture["failure"]["stage"])
        self.assertIsNone(capture["failure"]["case_id"])
        self.assertIsNone(capture["failure"]["replay"])
        self.assertEqual(raw, capture["failure"]["response"])
        self.assertEqual("analyzer returned invalid JSON", capture["failure"]["error"])
        self.assertEqual("fixture-model", capture["route"]["model"])
        self.assertEqual("a" * 64, capture["route"]["revision"])

    def test_transport_failure_does_not_reuse_prior_probe_response(self) -> None:
        endpoint = semantic_analyzer.AnalyzerEndpoint(
            name="synthetic-test",
            url="https://example.invalid/v1/chat",
            model="fixture-model",
            revision="a" * 64,
        )
        calls = 0

        def transport(_endpoint, _body):
            nonlocal calls
            calls += 1
            if calls == 1:
                return {"probe": "ok"}
            raise semantic_analyzer.AnalyzerError("synthetic route failure")

        try:
            live.capture_evaluation(endpoint, tier="primary", transport=transport)
        except Exception as exc:  # The diagnostic contract is asserted below.
            failure = exc
        else:
            self.fail("analysis transport failure must stop qualification")

        capture = getattr(failure, "failure_capture", None)
        self.assertIsInstance(capture, dict)
        self.assertEqual("analysis", capture["failure"]["stage"])
        self.assertEqual("synthetic.atomic", capture["failure"]["case_id"])
        self.assertEqual(1, capture["failure"]["replay"])
        self.assertIsNone(capture["failure"]["response"])

    def test_main_writes_attached_failure_capture_before_blocked_exit(self) -> None:
        endpoint = semantic_analyzer.AnalyzerEndpoint(
            name="synthetic-test",
            url="https://example.invalid/v1/chat",
            model="fixture-model",
            revision="a" * 64,
        )
        expected = {
            "schema_version": "clockify-analyzer-live-evaluation-failure/v1",
            "status": "failed",
            "failure": {
                "stage": "probe",
                "case_id": None,
                "replay": None,
                "response": {"choices": [{"message": {"content": "not-json"}}]},
                "error": "analyzer returned invalid JSON",
            },
        }
        failure = analyzer_evaluation.EvaluationError("analyzer returned invalid JSON")
        failure.failure_capture = expected

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            semantic_analyzer.AnalyzerEndpoint,
            "from_env",
            return_value=endpoint,
        ), mock.patch.object(
            live,
            "capture_evaluation",
            side_effect=failure,
        ):
            capture_path = Path(directory) / "capture.json"
            scorecard_path = Path(directory) / "scorecard.json"
            status = live.main([
                "--tier", "primary",
                "--capture-output", str(capture_path),
                "--scorecard-output", str(scorecard_path),
            ])

            self.assertEqual(2, status)
            self.assertTrue(capture_path.exists())
            self.assertEqual(0o600, stat.S_IMODE(capture_path.stat().st_mode))
            self.assertEqual(expected, json.loads(capture_path.read_text()))
            self.assertFalse(scorecard_path.exists())

    def test_synthetic_merge_accepts_captured_atomic_provider_response(self) -> None:
        case = next(
            item for item in live.synthetic_cases()
            if item["case_id"] == "synthetic.merge"
        )
        evidence_calls = 0

        def transport(
            _endpoint: semantic_analyzer.AnalyzerEndpoint,
            body: dict,
        ) -> dict:
            nonlocal evidence_calls
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            evidence_calls += 1
            members = [
                {"bundle_ref": bundle["bundle_ref"], **member}
                for bundle in payload["bundles"]
                for member in bundle["members"]
            ]
            activity = _activity(
                members,
                1,
                ["review", "identity", "allocation"],
            )
            activity.update({
                "action": "Implemented stable review identity",
                "object": "review identity from activity evidence fingerprints",
                "outcome": "same identity survives allocation movement",
            })
            return {"activities": [activity], "exceptions": [], "omissions": []}

        result = semantic_analyzer.analyze_tiered(
            case["events"],
            primary=semantic_analyzer.AnalyzerEndpoint(
                "primary",
                "http://primary",
                "fixture",
            ),
            transport=transport,
            max_workers=1,
            max_events_per_chunk=len(case["events"]),
            private_text_approved=True,
        )

        self.assertEqual(1, evidence_calls)
        self.assertEqual(1, len(result["activities"]))
        self.assertEqual(
            sorted(case["expected_activity_partitions"][0]),
            sorted(result["activities"][0]["evidence_ids"]),
        )
        rendered = " ".join(
            result["activities"][0][field]
            for field in ("action", "object", "outcome")
        ).casefold()
        for term in ("review", "identity", "allocation"):
            self.assertIn(term, rendered)

    def test_reviewable_synthetic_partitions_pair_intent_with_result(self) -> None:
        for case in live.synthetic_cases():
            events_by_id = {event["evidence_id"]: event for event in case["events"]}
            for partition in case["expected_activity_partitions"]:
                roles = {events_by_id[evidence_id]["role"] for evidence_id in partition}
                self.assertEqual({"user", "assistant"}, roles, case["case_id"])

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
            self.assertIn("bundles", payload)
            self.assertNotIn("events", payload)
            members = [
                {"bundle_ref": bundle["bundle_ref"], **member}
                for bundle in payload["bundles"]
                for member in bundle["members"]
            ]
            case = ordered_cases[evidence_calls // 2]
            evidence_calls += 1
            _provider_bundles, manifest = semantic_analyzer._semantic_evidence_bundles(
                case["events"]
            )
            aliases = {
                evidence_id: {"bundle_ref": bundle["bundle_ref"], **member}
                for bundle, manifest_item in zip(payload["bundles"], manifest, strict=True)
                for evidence_id, member in zip(
                    manifest_item["evidence_ids"], bundle["members"], strict=True
                )
            }
            activities = [
                _activity(
                    [aliases[value] for value in partition],
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
                        "evidence_partitions": _partitions(members),
                        "reason": "title alone cannot support a meeting outcome",
                    }]
                else:
                    omissions = [{
                        "lifecycle": "noise",
                        "evidence_partitions": _partitions(members),
                        "reason": "waiting status contains no substantive work",
                    }]
            return {"activities": activities, "exceptions": exceptions, "omissions": omissions}

        endpoint = semantic_analyzer.AnalyzerEndpoint(
            name="synthetic-test", url="https://example.invalid/v1/chat", model="fixture-model",
            revision="a" * 64,
            reasoning_effort="none",
        )
        capture = live.capture_evaluation(endpoint, tier="primary", transport=transport)
        scorecard = analyzer_evaluation.evaluate(capture)

        self.assertTrue(scorecard["passed"])
        self.assertEqual("a" * 64, scorecard["route"]["revision"])
        self.assertEqual("none", scorecard["route"]["reasoning_effort"])
        self.assertEqual(5, scorecard["case_count"])
        self.assertEqual(11, len(calls))
        outbound = semantic_analyzer.canonical_json(calls)
        self.assertNotIn("/Users/", outbound)
        self.assertNotIn("/home/", outbound)
        self.assertNotIn("@", outbound)

    def test_live_capture_scores_the_bounded_production_repair_path(self) -> None:
        case = live.synthetic_cases()[0]
        calls: list[dict] = []

        def transport(_endpoint: semantic_analyzer.AnalyzerEndpoint, body: dict) -> dict:
            calls.append(body)
            payload = json.loads(body["messages"][-1]["content"])
            if payload.get("probe"):
                return {"probe": "ok"}
            members = [
                {"bundle_ref": bundle["bundle_ref"], **member}
                for bundle in payload["bundles"]
                for member in bundle["members"]
            ]
            activity = _activity(
                members,
                1,
                case["expected_activity_concepts"][0]["required_terms"],
            )
            if "repair_feedback" not in payload:
                activity["action"] = "Verified and published"
            return {"activities": [activity], "exceptions": [], "omissions": []}

        endpoint = semantic_analyzer.AnalyzerEndpoint(
            name="synthetic-test",
            url="https://example.invalid/v1/chat",
            model="fixture-model",
            revision="a" * 64,
        )
        with mock.patch.object(live, "synthetic_cases", return_value=[case]):
            capture = live.capture_evaluation(
                endpoint,
                tier="primary",
                transport=transport,
            )

        scorecard = analyzer_evaluation.evaluate(capture)
        evidence_payloads = [
            json.loads(body["messages"][-1]["content"])
            for body in calls
            if '"probe"' not in body["messages"][-1]["content"]
        ]
        self.assertTrue(scorecard["passed"])
        self.assertEqual(4, len(evidence_payloads))
        self.assertEqual(2, sum("repair_feedback" in item for item in evidence_payloads))

    def test_live_capture_requires_two_replays(self) -> None:
        endpoint = semantic_analyzer.AnalyzerEndpoint(
            name="synthetic-test", url="https://example.invalid", model="fixture-model"
        )
        with self.assertRaisesRegex(analyzer_evaluation.EvaluationError, "at least two"):
            live.capture_evaluation(endpoint, tier="primary", replay_count=1)


if __name__ == "__main__":
    unittest.main()
