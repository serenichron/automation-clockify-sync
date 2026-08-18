import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import clockify_post_approved_portfolio as poster
from scripts import clockify_portfolio_replay as portfolio_replay


class ClockifyPortfolioPostTests(unittest.TestCase):
    def test_posting_plan_preserves_subminute_approved_segments(self) -> None:
        portfolio = {
            "activities": [{
                "review_id": "pvi-0123456789abcdef01234567",
                "client_project": "Example Level 2",
                "tag_names": ["Technical development"],
                "description": "EX — Preserve exact recorded meeting time",
                "duration_minutes": 30,
                "duration_seconds": 1826,
                "allocation_segments": [{
                    "start": "2026-08-14T10:00:17+03:00",
                    "end": "2026-08-14T10:30:43+03:00",
                    "duration_minutes": 30,
                    "duration_seconds": 1826,
                }],
            }],
        }
        routes = {
            ("Example Level 2", ("Technical development",)): {
                "project_id": "project-1", "tag_ids": ["tag-1"], "billable": True,
            },
        }

        plan = poster._plans(portfolio, routes)[0]

        self.assertEqual("2026-08-14T07:00:17Z", plan["start"])
        self.assertEqual("2026-08-14T07:30:43Z", plan["end"])
        self.assertEqual(1826, plan["duration_seconds"])
    def test_post_gate_requires_clean_flash_validated_replay_bound_repair(self) -> None:
        portfolio = {
            "external_writes": False,
            "repair": {"status": "complete", "unresolved_wording": []},
            "activities": [{"validation_status": "flash_validated"}],
        }
        quality = {"status": "pass"}
        replay = {
            "status": "pass",
            "identity": {"artifacts": {
                "repair": portfolio_replay._digest(portfolio),
                "quality": portfolio_replay._digest(quality),
            }},
        }

        poster._verify_approved_artifacts(portfolio, quality, replay)

        portfolio["activities"][0]["validation_status"] = (
            "source_semantic_review_carried_after_flash_contract_failure"
        )
        replay["identity"]["artifacts"]["repair"] = portfolio_replay._digest(portfolio)
        with self.assertRaisesRegex(
            poster.PortfolioPostError, "Flash portfolio validation"
        ):
            poster._verify_approved_artifacts(portfolio, quality, replay)

        portfolio["activities"][0]["validation_status"] = "flash_validated"
        portfolio["repair"]["status"] = "complete_with_warnings"
        replay["identity"]["artifacts"]["repair"] = portfolio_replay._digest(portfolio)
        with self.assertRaisesRegex(poster.PortfolioPostError, "cleanly completed"):
            poster._verify_approved_artifacts(portfolio, quality, replay)

        portfolio["repair"]["status"] = "complete"
        replay["identity"]["artifacts"]["repair"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(poster.PortfolioPostError, "not bound"):
            poster._verify_approved_artifacts(portfolio, quality, replay)

    def test_live_conflict_query_pads_the_approved_portfolio_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            portfolio_path = root / "portfolio.json"
            quality_path = root / "quality.json"
            routing_path = root / "routing.json"
            receipt_path = root / "receipt.json"
            replay_path = root / "replay.json"
            portfolio = {
                "external_writes": False,
                "repair": {"status": "complete", "unresolved_wording": []},
                "activities": [{
                    "review_id": "pvi-0123456789abcdef01234567",
                    "client_project": "Example Level 2",
                    "tag_names": ["Technical development"],
                    "description": "EX — Delivered bounded portfolio posting coverage",
                    "duration_minutes": 30,
                    "validation_status": "flash_validated",
                    "allocation_segments": [{
                        "start": "2026-08-14T10:00:00+03:00",
                        "end": "2026-08-14T10:30:00+03:00",
                        "duration_minutes": 30,
                    }],
                }],
            }
            portfolio_path.write_text(json.dumps(portfolio), encoding="utf-8")
            quality = {"status": "pass"}
            quality_path.write_text(json.dumps(quality), encoding="utf-8")
            replay_path.write_text(json.dumps({
                "status": "pass",
                "identity": {"artifacts": {
                    "repair": portfolio_replay._digest(portfolio),
                    "quality": portfolio_replay._digest(quality),
                }},
            }), encoding="utf-8")
            routing_path.write_text(json.dumps({
                "clockify_user_id": "user-1",
                "session_routes": [{
                    "project_name": "Example Level 2",
                    "tag_names": ["Technical development"],
                    "project_suffix": "123456",
                    "tag_suffixes": ["654321"],
                }],
            }), encoding="utf-8")
            calls: list[str] = []

            def paged(path: str, _api_key: str, *, timeout_seconds: int):
                self.assertEqual(45, timeout_seconds)
                calls.append(path)
                if path.startswith("/workspaces/workspace-1/projects"):
                    return [{"id": "project-123456"}]
                if path.startswith("/workspaces/workspace-1/tags"):
                    return [{"id": "tag-654321"}]
                return []

            args = argparse.Namespace(
                portfolio=portfolio_path,
                quality_report=quality_path,
                replay_integrity=replay_path,
                routing=routing_path,
                receipt=receipt_path,
                prior_receipt=None,
                expected_portfolio_sha256=hashlib.sha256(
                    portfolio_path.read_bytes()
                ).hexdigest(),
                execute=False,
            )
            with (
                mock.patch.object(poster, "load_env_file", return_value={
                    "CLOCKIFY_API_KEY": "secret",
                    "CLOCKIFY_WORKSPACE_ID": "workspace-1",
                }),
                mock.patch.object(poster, "_paged", side_effect=paged),
            ):
                poster.run(args)

            self.assertIn(
                "/workspaces/workspace-1/user/user-1/time-entries?"
                "start=2026-08-13T07%3A00%3A00Z&end=2026-08-15T07%3A30%3A00Z",
                calls,
            )


if __name__ == "__main__":
    unittest.main()
