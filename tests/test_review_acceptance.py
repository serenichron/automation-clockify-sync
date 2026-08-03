from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SPEC = importlib.util.spec_from_file_location(
    "review_acceptance", SCRIPTS / "review_acceptance.py"
)
assert SPEC and SPEC.loader
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)

from scripts import review_corrections  # noqa: E402
from scripts import analyzer_evaluation  # noqa: E402


def _write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _review_item(index: int, disposition: str = "pending") -> dict:
    evidence_ids = [f"ev-{index}"]
    return {
        "id": f"rvi-{index}",
        "activity_id": f"act-{index}",
        "evidence_ids": evidence_ids,
        "evidence_fingerprint": review_corrections.evidence_fingerprint(evidence_ids),
        "disposition": disposition,
    }


def _decision(item: dict, decision: str = "approve") -> dict:
    patch = None
    if decision == "modify":
        patch = {"description": {"op": "replace", "value": "SC — Repaired review wording using cited work outcomes"}}
    return review_corrections.build_decision(
        item,
        decision=decision,
        reviewer="board",
        reviewed_at="2026-08-03T12:00:00+03:00",
        correction_categories=["wording"],
        rationale="Verified against the complete review period.",
        field_patch=patch,
    )


def _make_run(
    root: Path,
    name: str,
    rows: list[dict],
    *,
    replay: bool = False,
    source_status: str = "complete",
    quality_status: str = "pass",
    evidence_digest: str = "sha256:evidence",
) -> Path:
    run = root / name
    categories = {
        "new": [] if replay else rows,
        "changed": [],
        "carried_pending": rows if replay else [],
        "resolved_disappeared": [],
    }
    _write(
        run / "review-snapshot.json",
        {
            "run_id": name,
            "categories": categories,
            "summary": {key: len(value) for key, value in categories.items()},
        },
    )
    _write(
        run / "work-accounting-result.json",
        {
            "ledger_manifest": {
                "manifest_id": "manifest-one",
                "source_completeness": {"status": source_status},
            }
        },
    )
    _write(
        run / "semantic-analysis.json",
        {
            "schema_version": 1,
            "prompt_version": "prompt-one",
            "ledger_evidence_digest": evidence_digest,
            "activities": [
                {
                    "analyzer_model": "fixture-model",
                    "analyzer_tier": "primary",
                    "prompt_version": "prompt-one",
                    "schema_version": 1,
                }
            ],
        },
    )
    _write(run / "quality_report.json", {"status": quality_status})
    if replay:
        source_name = name.removesuffix("-replay")
        replay_integrity = {
            "schema_version": 1,
            "status": "pass",
            "source_run_id": source_name,
            "replay_run_id": name,
            "ledger_identity": {"manifest_id": "manifest-one"},
            "ledger_evidence_digest": evidence_digest,
            "analyzer_versions": [
                {
                    "model": "fixture-model",
                    "tier": "primary",
                    "prompt_version": "prompt-one",
                    "schema_version": 1,
                }
            ],
            "failures": [],
        }
        replay_integrity["integrity_digest"] = acceptance.digest(replay_integrity)
        _write(run / "replay-integrity.json", replay_integrity)
    return run


def _passing_period(stage: str, run_id: str, rate: float) -> dict:
    return {
        "schema_version": 1,
        "period_id": f"period-{run_id}",
        "stage": stage,
        "run_id": run_id,
        "replay_run_id": f"{run_id}-replay",
        "full_denominator_complete": True,
        "source_complete": True,
        "quality_pass": True,
        "coverage_clean": True,
        "analyzer_evaluation_pass": True,
        "replay_stable": True,
        "critical_error_count": 0,
        "unchanged_approval_rate": rate,
    }


def _scorecard(evidence_id: str) -> dict:
    span = {"start": "2026-08-03T10:00:00+03:00", "end": "2026-08-03T10:10:00+03:00"}
    activity = {
        "lifecycle": "completed",
        "workstream": "review process",
        "action": "Improved",
        "object": "review process wording",
        "outcome": "using cited accomplishment evidence",
        "evidence_ids": [evidence_id],
        "evidence_spans": [span],
        "project_recommendation": {"name": "Serenichron", "prefix": "SC", "tag_names": ["Processes"]},
        "effort": {"minimum_minutes": 5, "recommended_minutes": 10, "maximum_minutes": 15},
        "semantic_confidence": "high",
        "timing_confidence": "high",
        "split_rationale": "one bounded outcome",
        "merge_rationale": "",
        "omit_rationale": "",
    }
    response = {"activities": [activity], "exceptions": [], "omissions": []}
    records = [{"evidence_id": evidence_id, "time_span": span}]
    return analyzer_evaluation.evaluate(
        {
            "schema_version": analyzer_evaluation.INPUT_SCHEMA_VERSION,
            "corpus": {"records": records, "digest": analyzer_evaluation.sha256_hex(records)},
            "route": {"route_id": "fixture-route", "model": "fixture-model", "tier": "primary"},
            "prompt_version": "clockify-semantic-v3",
            "semantic_schema_version": 1,
            "cases": [
                {
                    "case_id": "acceptance-route",
                    "evidence_ids": [evidence_id],
                    "expected_activity_partitions": [[evidence_id]],
                    "replays": [response, response],
                }
            ],
        }
    )


class ReviewAcceptanceTests(unittest.TestCase):
    def test_full_denominator_includes_ambiguous_and_rejects_missing_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [_review_item(1), _review_item(2, "ambiguous")]
            run = _make_run(root, "run-one", rows)
            replay = _make_run(root, "run-one-replay", rows, replay=True)

            period = acceptance.build_period_report(
                run, replay, [_decision(rows[0])], stage="shadow_baseline"
            )

        self.assertEqual(2, period["denominator_count"])
        self.assertEqual(1, period["missing_decision_count"])
        self.assertFalse(period["full_denominator_complete"])

    def test_changed_decision_requires_assessment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [_review_item(1)]
            run = _make_run(root, "run-one", rows)
            replay = _make_run(root, "run-one-replay", rows, replay=True)
            period = acceptance.build_period_report(
                run, replay, [_decision(rows[0], "modify")], stage="shadow_baseline"
            )

        self.assertEqual(1, period["unassessed_changed_count"])
        self.assertFalse(period["full_denominator_complete"])

    def test_period_cannot_pass_without_a_verified_analyzer_scorecard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [_review_item(1)]
            run = _make_run(root, "run-one", rows)
            replay = _make_run(root, "run-one-replay", rows, replay=True)
            period = acceptance.build_period_report(
                run, replay, [_decision(rows[0])], stage="shadow_baseline"
            )

        self.assertFalse(period["analyzer_evaluation_pass"])
        self.assertFalse(acceptance.period_passes(period, 0.90))

    def test_matching_verified_analyzer_scorecard_satisfies_evaluation_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [_review_item(1)]
            run = _make_run(root, "run-one", rows)
            replay = _make_run(root, "run-one-replay", rows, replay=True)
            period = acceptance.build_period_report(
                run,
                replay,
                [_decision(rows[0])],
                stage="shadow_baseline",
                analyzer_scorecards=[_scorecard("ev-1")],
            )

        self.assertTrue(period["analyzer_evaluation_pass"])
        self.assertTrue(acceptance.period_passes(period, 0.90))

    def test_unstable_replay_incomplete_source_and_failed_quality_all_fail(self):
        cases = [
            {"evidence_digest": "sha256:different"},
            {"source_status": "partial"},
            {"quality_status": "blocked"},
        ]
        for overrides in cases:
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                rows = [_review_item(1)]
                run = _make_run(root, "run-one", rows)
                replay = _make_run(root, "run-one-replay", rows, replay=True, **overrides)
                period = acceptance.build_period_report(
                    run, replay, [_decision(rows[0])], stage="shadow_baseline"
                )
                self.assertFalse(acceptance.period_passes(period, 0.90))

    def test_second_live_collection_cannot_masquerade_as_immutable_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [_review_item(1)]
            run = _make_run(root, "run-one", rows)
            replay = _make_run(root, "run-one-replay", rows, replay=True)
            (replay / "replay-integrity.json").unlink()

            with self.assertRaisesRegex(acceptance.AcceptanceError, "second live collection"):
                acceptance.build_period_report(
                    run, replay, [_decision(rows[0])], stage="shadow_baseline"
                )

    def test_baseline_and_two_later_distinct_guarded_periods_enable_gate(self):
        records = [
            _passing_period("shadow_baseline", "baseline", 0.90),
            _passing_period("guarded", "guarded-one", 0.95),
            _passing_period("guarded", "guarded-two", 0.97),
        ]

        gate = acceptance.evaluate_gate(records)

        self.assertTrue(gate["baseline_90_passed"])
        self.assertTrue(gate["exceptions_only_eligible"])

    def test_gate_rejects_early_low_rate_critical_and_reused_periods(self):
        low = _passing_period("guarded", "guarded-low", 0.949)
        critical = _passing_period("guarded", "guarded-critical", 0.99)
        critical["critical_error_count"] = 1
        scenarios = [
            [_passing_period("shadow_baseline", "base", 0.899), _passing_period("guarded", "g1", 1), _passing_period("guarded", "g2", 1)],
            [_passing_period("shadow_baseline", "base", 1), low, _passing_period("guarded", "g2", 1)],
            [_passing_period("shadow_baseline", "base", 1), critical, _passing_period("guarded", "g2", 1)],
            [_passing_period("shadow_baseline", "same", 1), _passing_period("guarded", "same", 1), _passing_period("guarded", "other", 1)],
        ]
        for records in scenarios:
            with self.subTest(records=records):
                self.assertFalse(acceptance.evaluate_gate(records)["exceptions_only_eligible"])

    def test_critical_error_assessment_rejects_period(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [_review_item(1)]
            decision = _decision(rows[0], "modify")
            run = _make_run(root, "run-one", rows)
            replay = _make_run(root, "run-one-replay", rows, replay=True)
            period = acceptance.build_period_report(
                run,
                replay,
                [decision],
                stage="guarded",
                critical_assessments={
                    decision["decision_id"]: {
                        "severity": "critical",
                        "domain": "description_truth",
                        "reason": "Description claimed an unsupported outcome.",
                    }
                },
            )

        self.assertEqual(1, period["critical_error_count"])
        self.assertFalse(acceptance.period_passes(period, 0.95))

    def test_integrity_chain_detects_record_and_link_tampering(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "acceptance.jsonl"
            first = _passing_period("shadow_baseline", "base", 0.90)
            second = _passing_period("guarded", "guarded", 0.95)
            acceptance.append_period(path, first)
            acceptance.append_period(path, second)
            self.assertEqual(2, len(acceptance.load_ledger(path)))

            lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            lines[1]["previous_digest"] = "sha256:" + "0" * 64
            path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
            with self.assertRaises(acceptance.AcceptanceError):
                acceptance.load_ledger(path)

    def test_same_run_cannot_be_its_own_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [_review_item(1)]
            run = _make_run(root, "run-one", rows)
            with self.assertRaisesRegex(acceptance.AcceptanceError, "distinct run"):
                acceptance.build_period_report(
                    run, run, [_decision(rows[0])], stage="shadow_baseline"
                )


if __name__ == "__main__":
    unittest.main()
