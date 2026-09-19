import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import clockify_review_cycle as cycle


class ReviewCycleTests(unittest.TestCase):
    def test_invalid_timezone_is_blocked_before_any_child_starts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("routing.json", "corrections.jsonl", "acceptance.jsonl"):
                (root / name).write_text("{}\n", encoding="utf-8")
            config_path = root / "cycle.json"
            config_path.write_text(json.dumps({
                "root": str(root),
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
