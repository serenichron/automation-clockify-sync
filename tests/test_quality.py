from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).parents[1] / "scripts" / "clockify_sync_quality.py"
SPEC = importlib.util.spec_from_file_location("clockify_sync_quality", MODULE_PATH)
assert SPEC and SPEC.loader
quality = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(quality)


def proposal(**overrides):
    result = {
        "id": "P001",
        "candidate_key": "claude:machine:session-b:2026-07-29T10:00",
        "start": "2026-07-29 10:00",
        "end": "2026-07-29 10:20",
        "duration_minutes": 20,
        "source_label": "shared-project",
        "client_project": "Serenichron Level 2",
        "description": "[NEEDS REVIEW] — SC — Unlabeled session",
        "provenance": {
            "source_type": "claude",
            "source_machine": "macbook",
            "source_session_id": "session-b",
            "burst_start": "2026-07-29 10:00",
            "burst_end": "2026-07-29 10:20",
        },
    }
    result.update(overrides)
    return result


class QualityMatchingTests(unittest.TestCase):
    def test_stable_project_identity_wins_over_misleading_label_text(self):
        routes = [
            {
                "pattern": "serenichron",
                "project_name": "Serenichron Level 2",
                "project_suffix": "775f9f",
                "prefix": "SC",
            },
            {
                "pattern": "lensofalex-com",
                "project_name": "Lens of Alex Retainer",
                "project_suffix": "1207d5",
                "prefix": "LoA",
            },
        ]
        row = proposal(
            source_label="Serenichron × Lens of Alex — Sync",
            client_project="Lens of Alex Retainer",
            clockify_project_suffix="1207d5",
            description="LoA — Serenichron × Lens of Alex — Sync",
        )

        self.assertIsNone(quality.check_prefix_match(row, routes))

    def test_exact_session_identity_wins_over_same_label(self):
        enriched = {
            "claude_contexts": [
                {
                    "source": "claude_jsonl",
                    "session_id": "session-a",
                    "label": "shared-project",
                    "start": "2026-07-29 10:00",
                    "end": "2026-07-29 10:20",
                    "user_messages": [
                        {
                            "timestamp": "2026-07-29 10:01",
                            "user_message": "Wrong session task",
                        }
                    ],
                },
                {
                    "source": "claude_jsonl",
                    "session_id": "session-b",
                    "label": "shared-project",
                    "start": "2026-07-29 10:00",
                    "end": "2026-07-29 10:20",
                    "user_messages": [
                        {
                            "timestamp": "2026-07-29 10:02",
                            "user_message": "Review Clockify descriptions",
                            "next_assistant": "Reconciled row-specific Clockify descriptions",
                        }
                    ],
                },
            ],
            "hermes_contexts": [],
            "codex_contexts": [],
        }

        context = quality.find_context_for_proposal(proposal(), enriched)

        self.assertEqual("session-b", context["session_id"])
        self.assertEqual(
            "Reconciled row-specific Clockify descriptions",
            quality.infer_work_from_context(proposal(), context),
        )

    def test_label_only_fallback_is_forbidden(self):
        row = proposal(provenance={})
        enriched = {
            "claude_contexts": [
                {
                    "source": "claude_jsonl",
                    "session_id": "session-a",
                    "label": "shared-project",
                }
            ]
        }
        self.assertIsNone(quality.find_context_for_proposal(row, enriched))

    def test_report_blocks_missing_stable_identity(self):
        row = proposal(candidate_key=None, provenance={})
        report = quality.build_report("run-1", [row], {}, [])
        self.assertEqual("blocked", report["status"])
        self.assertEqual(["P001"], report["missing_candidate_key_rows"])
        self.assertEqual(["P001"], report["missing_provenance_rows"])
        self.assertFalse(report["external_writes"])

    def test_report_blocks_runtime_noise_description(self):
        row = proposal(
            description=(
                "TSTP — You've hit your session limit · resets 4:40am "
                "(Europe/Bucharest)"
            )
        )

        report = quality.build_report("run-1", [row], {}, [])

        self.assertEqual("blocked", report["status"])
        self.assertEqual(1, report["summary"]["rows_with_issues"])
        self.assertIn(
            "Description contains runtime or injected system noise",
            report["reviews"][0]["issues"],
        )

    def test_report_blocks_invalid_window_and_duplicate_candidate_key(self):
        first = proposal(
            id="P001",
            candidate_key="duplicate-key",
            start="2026-07-29 10:20",
            end="2026-07-29 10:00",
        )
        second = proposal(id="P002", candidate_key="duplicate-key")

        report = quality.build_report("run-1", [first, second], {}, [])

        self.assertEqual("blocked", report["status"])
        self.assertEqual(1, report["summary"]["duplicate_candidate_key_groups"])
        self.assertEqual(
            "duplicate-key",
            report["duplicate_candidate_keys"][0]["candidate_key"],
        )
        self.assertIn(
            "Proposal has an invalid or empty time window",
            report["reviews"][0]["issues"],
        )

    def test_semantic_segments_may_share_one_activity_description(self):
        proposals = [
            {
                "id": "P001",
                "activity_id": "act-one",
                "description": "SC — Rebuilt Clockify review process for accurate automatic timesheets",
            },
            {
                "id": "P002",
                "activity_id": "act-one",
                "description": "SC — Rebuilt Clockify review process for accurate automatic timesheets",
            },
        ]
        self.assertEqual([], quality.find_duplicate_descriptions(proposals))

    def test_quality_detects_proposal_overlap_with_existing_clockify(self):
        overlaps = quality.find_time_overlaps(
            [
                {
                    "id": "P001",
                    "start": "2026-07-10T10:00:00+03:00",
                    "end": "2026-07-10T11:00:00+03:00",
                }
            ],
            [
                {
                    "start": "2026-07-10T10:30:00+03:00",
                    "end": "2026-07-10T11:30:00+03:00",
                }
            ],
        )
        self.assertEqual([{"left": "P001", "right": "existing-1"}], overlaps)

    def test_semantic_proposal_must_pass_caveman_contract(self):
        row = proposal(
            id="P001",
            candidate_key="wk-one",
            description="SC — [NEEDS REVIEW] raw prompt...",
            start="2026-07-10T10:00:00+03:00",
            end="2026-07-10T10:30:00+03:00",
            duration_minutes=30,
        )
        row.update(
            {
                "activity_id": "act-one",
                "workstream_id": "ws-one",
                "review_activity_key": "wka-one",
                "allocation_segment": 1,
                "rendered_description": "SC — [NEEDS REVIEW] raw prompt...",
                "allocation_mode": "non_overlapping_v1",
                "provenance": {
                    "source_type": "semantic_activity",
                    "source_session_id": "act-one",
                    "evidence_ids": ["ev-one"],
                },
            }
        )
        review = quality.review_proposal(row, {}, [])
        self.assertTrue(review["has_issues"])
        self.assertTrue(
            any("Caveman description contract" in issue for issue in review["issues"])
        )

    def test_semantic_proposal_requires_matching_structured_render(self):
        row = proposal(
            id="P001",
            candidate_key="wks-one",
            description="SC — Rebuilt Clockify review process for accurate automatic timesheets",
            start="2026-07-10T10:00:00+03:00",
            end="2026-07-10T10:30:00+03:00",
            duration_minutes=30,
        )
        row.update(
            {
                "activity_id": "act-one",
                "workstream_id": "ws-one",
                "review_activity_key": "wka-one",
                "allocation_segment": 1,
                "rendered_description": "SC — Different structured description that cannot be trusted",
                "allocation_mode": "non_overlapping_v1",
                "provenance": {
                    "source_type": "semantic_activity",
                    "source_session_id": "act-one",
                    "evidence_ids": ["ev-one"],
                },
            }
        )
        review = quality.review_proposal(row, {}, [])
        self.assertTrue(
            any("rendered_description" in issue for issue in review["issues"])
        )


class QualityCliTests(unittest.TestCase):
    def test_dry_run_performs_no_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "runs" / "run-1"
            evidence = run_dir / "evidence"
            evidence.mkdir(parents=True)
            (run_dir / "proposals.json").write_text("[]\n")
            (evidence / "enriched-context.json").write_text("{}\n")
            (root / "routing.json").write_text('{"session_routes": []}\n')

            exit_code = quality.main(
                [
                    "run-1",
                    "--runs-root",
                    str(root / "runs"),
                    "--root",
                    str(root),
                    "--dry-run",
                ]
            )

            self.assertEqual(0, exit_code)
            self.assertFalse((run_dir / "quality_report.json").exists())

    def test_default_writes_only_local_quality_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            run_dir = root / "runs" / "run-1"
            evidence = run_dir / "evidence"
            evidence.mkdir(parents=True)
            (run_dir / "proposals.json").write_text("[]\n")
            (evidence / "enriched-context.json").write_text("{}\n")
            (root / "routing.json").write_text('{"session_routes": []}\n')

            exit_code = quality.main(
                ["run-1", "--runs-root", str(root / "runs"), "--root", str(root)]
            )

            self.assertEqual(0, exit_code)
            report = json.loads((run_dir / "quality_report.json").read_text())
            self.assertEqual("pass", report["status"])
            self.assertFalse(report["external_writes"])


    def test_review_suggestion_prefers_specific_existing_topic(self):
        row = proposal(
            description=(
                "SC — [NEEDS REVIEW] Collapse per-session MCP process fan-out in Codex"
            )
        )
        enriched = {
            "claude_contexts": [
                {
                    "source": "claude_jsonl",
                    "session_id": "session-b",
                    "start": "2026-07-29 10:00",
                    "end": "2026-07-29 10:20",
                    "user_messages": [
                        {
                            "timestamp": "2026-07-29 10:01",
                            "user_message": "what is currently using system resources?",
                            "next_assistant": "[thinking]",
                        }
                    ],
                }
            ],
            "hermes_contexts": [],
            "codex_contexts": [],
        }
        routes = [
            {
                "pattern": "shared-project",
                "project_name": "Serenichron Level 2",
                "prefix": "SC",
            }
        ]

        review = quality.review_proposal(row, enriched, routes)

        self.assertEqual(
            "SC — Collapse per-session MCP process fan-out in Codex",
            review["improved_description"],
        )

    def test_review_suggestion_strips_completion_boilerplate_and_markdown(self):
        row = proposal(
            description=(
                "SC — [NEEDS REVIEW] Done. **GWS CLI configured** and "
                "`authorized` on this machine."
            )
        )

        review = quality.review_proposal(row, {}, [])

        self.assertEqual(
            "SC — GWS CLI configured and authorized on this machine.",
            review["improved_description"],
        )

    def test_duplicate_descriptions_block_durable_reconciliation(self):
        first = proposal(id="P001", candidate_key="candidate-one")
        second = proposal(id="P002", candidate_key="candidate-two")

        report = quality.build_report(
            "run-duplicate",
            [first, second],
            {"claude_contexts": [], "hermes_contexts": [], "codex_contexts": []},
            [],
        )

        self.assertEqual("blocked", report["status"])
        self.assertEqual(1, report["summary"]["duplicate_description_groups"])


if __name__ == "__main__":
    unittest.main()
