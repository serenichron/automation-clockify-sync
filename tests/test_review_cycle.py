import datetime as dt
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import clockify_review_cycle as cycle


class ReviewCycleTests(unittest.TestCase):
    def test_private_actor_routing_is_the_effective_fresh_snapshot_input(self):
        """Catches fresh requests hashing or launching with the immutable base routing."""
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory)
            root = Path(__file__).resolve().parents[1]
            state = private / "state"
            state.mkdir()
            corrections = private / "corrections.jsonl"
            acceptance = private / "acceptance.jsonl"
            manifest = private / "period-manifest.json"
            for path in (corrections, acceptance):
                path.write_bytes(b"")
            manifest.write_text("{}\n", encoding="utf-8")
            base_path = root / "routing.json"
            base = json.loads(base_path.read_text(encoding="utf-8"))
            effective = {
                **base,
                "semantic_actor_contract": "clockify-semantic-actors/v1",
                "semantic_subject_binding": {
                    "source_type": "multica",
                    "server_origin": "https://multica.example.invalid",
                    "workspace_id": "workspace-fixture",
                    "author_id": "comment-author-fixture",
                },
            }
            routing = private / "routing.private.json"
            routing_bytes = (
                json.dumps(effective, sort_keys=True, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            routing.write_bytes(routing_bytes)
            routing.chmod(0o600)
            config_path = private / "cycle.json"
            config_path.write_text(json.dumps({
                "root": str(root),
                "runs_dir": str(private / "runs"),
                "state_dir": str(state),
                "cache": str(private / "cache"),
                "routing": str(routing),
                "private_routing": {
                    "sha256": hashlib.sha256(routing_bytes).hexdigest(),
                    "base_sha256": hashlib.sha256(base_path.read_bytes()).hexdigest(),
                },
                "corrections": str(corrections),
                "acceptance": str(acceptance),
                "workspace_id": base["workspace_id"],
                "member_id": base["member_id"],
                "recovery_since": "2026-09-07",
                "timezone": "Europe/Bucharest",
                "spreadsheet_id": "sheet-1",
                "monthly_sheet_title_template": "{month_name} {year}",
                "calendly_optional": True,
            }), encoding="utf-8")

            config = cycle.load_config(config_path)

            self.assertEqual(
                "sha256:" + hashlib.sha256(routing_bytes).hexdigest(),
                cycle._expected_snapshot_digests(config, manifest)["routing.json"],
            )
            command = cycle._review_command(config, "2026-09-07", "2026-09-09")
            self.assertEqual(str(routing), command[command.index("--routing") + 1])

            missing_binding = dict(effective)
            missing_binding.pop("semantic_subject_binding")
            missing_bytes = (
                json.dumps(missing_binding, sort_keys=True, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
            routing.write_bytes(missing_bytes)
            document = json.loads(config_path.read_text(encoding="utf-8"))
            document["private_routing"]["sha256"] = hashlib.sha256(
                missing_bytes
            ).hexdigest()
            config_path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(cycle.CycleError, "subject binding"):
                cycle.load_config(config_path)

            routing.write_bytes(routing_bytes)
            document["private_routing"]["sha256"] = hashlib.sha256(
                routing_bytes
            ).hexdigest()
            alias = private / "routing-alias.json"
            alias.symlink_to(routing)
            document["routing"] = str(alias)
            config_path.write_text(json.dumps(document), encoding="utf-8")
            with self.assertRaisesRegex(cycle.CycleError, "canonical"):
                cycle.load_config(config_path)

    def test_invalid_timezone_is_blocked_before_any_child_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("routing.json", "corrections.jsonl", "acceptance.jsonl"):
                (root / name).write_text("{}\n", encoding="utf-8")
            config_path = root / "cycle.json"
            config_path.write_text(json.dumps({
                "root": str(root),
                "runs_dir": str(root / "runs"),
                "state_dir": str(root / "state"),
                "cache": str(root / "cache"),
                "routing": str(root / "routing.json"),
                "corrections": str(root / "corrections.jsonl"),
                "acceptance": str(root / "acceptance.jsonl"),
                "workspace_id": "workspace-1",
                "member_id": "member-1",
                "recovery_since": "2026-09-07",
                "timezone": "Not/AZone",
                "spreadsheet_id": "sheet-1",
                "monthly_sheet_title_template": "{month_name} {year}",
                "calendly_optional": True,
            }), encoding="utf-8")

            with mock.patch.object(cycle, "run_child_bounded") as child:
                with self.assertRaisesRegex(cycle.CycleError, "timezone"):
                    cycle.load_config(config_path)
                self.assertEqual(2, cycle.main(["--config", str(config_path)]))

            child.assert_not_called()

    def test_selects_one_closed_two_day_slice_without_crossing_month(self):
        config = {
            "recovery_since": "2026-08-28",
            "timezone": "Europe/Bucharest",
            "max_slices": 1,
        }
        selected = cycle.select_slices(config, {}, today=dt.date(2026, 9, 2))
        self.assertEqual([("2026-08-28", "2026-08-30")], selected)

    def test_selects_weekend_slice_and_keeps_incomplete_slice(self):
        config = {"recovery_since": "2026-08-28", "timezone": "Europe/Bucharest"}
        state = {"slices": {"2026-08-28": {"status": "incomplete"}}}
        selected = cycle.select_slices(config, state, today=dt.date(2026, 9, 2))
        self.assertEqual([("2026-08-28", "2026-08-30")], selected)

    def test_nonblocking_lock_returns_locked_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lock = root / "cycle.lock"
            lock.parent.mkdir(exist_ok=True)
            with cycle.single_instance(lock) as first:
                self.assertTrue(first)
                with cycle.single_instance(lock) as second:
                    self.assertFalse(second)

    def test_quality_pass_keeps_accounting_ambiguous_items_separate(self):
        result = {
            "quality": {"status": "pass"},
            "source_completeness": {"status": "complete", "incomplete_sources": []},
            "accounting": {"ambiguous": [{"id": "wka-x-s01"}]},
        }
        completion = cycle.completion_status(result)
        self.assertTrue(completion["source_complete"])
        self.assertFalse(completion["exceptions_complete"])
        self.assertEqual(["wka-x-s01"], completion["exception_ids"])


if __name__ == "__main__":
    unittest.main()
