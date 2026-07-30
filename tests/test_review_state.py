import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "clockify_review_state.py"
SPEC = importlib.util.spec_from_file_location("clockify_review_state", SCRIPT)
review_state = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(review_state)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def proposal(description="SC — initial", project="Serenichron Level 2"):
    return {
        "id": "P001",
        "start": "2026-07-28T09:00:00+03:00",
        "end": "2026-07-28T10:00:00+03:00",
        "source": ["codex:omarchy-desktop"],
        "source_label": "automation-clockify-sync",
        "client_project": project,
        "description": description,
    }


def ingest(tmp_path, run_name, proposals, ambiguous=()):
    run_dir = tmp_path / run_name
    write_json(run_dir / "proposals.json", list(proposals))
    write_json(run_dir / "ambiguous.json", list(ambiguous))
    state = review_state.load_state(tmp_path / "state.json")
    snapshot = review_state.ingest_run(run_dir, state)
    return state, snapshot, run_dir


class ReviewStateTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_reordered_records_keep_ids_and_create_no_new_items(self):
        first = proposal()
        second = proposal("SC — another task", "Serenichron Level 1")
        second["start"] = "2026-07-28T11:00:00+03:00"
        second["end"] = "2026-07-28T12:00:00+03:00"
        state, _, _ = ingest(self.tmp_path, "run-one", [first, second])
        initial_ids = sorted(state["items"])
        run_dir = self.tmp_path / "run-two"
        write_json(run_dir / "proposals.json", [second, first])
        write_json(run_dir / "ambiguous.json", [])
        second_snapshot = review_state.ingest_run(run_dir, state)
        self.assertEqual(sorted(state["items"]), initial_ids)
        self.assertEqual(second_snapshot["summary"]["new"], 0)
        self.assertEqual(second_snapshot["summary"]["changed"], 0)
        self.assertEqual(second_snapshot["summary"]["carried_pending"], 2)

    def test_description_and_project_change_revises_existing_item(self):
        state, _, _ = ingest(self.tmp_path, "run-one", [proposal()])
        item_id = next(iter(state["items"]))
        run_dir = self.tmp_path / "run-two"
        write_json(run_dir / "proposals.json", [proposal("SC — durable review work", "Serenichron Level 1")])
        write_json(run_dir / "ambiguous.json", [])
        snapshot = review_state.ingest_run(run_dir, state)
        self.assertEqual(list(state["items"]), [item_id])
        self.assertEqual(snapshot["summary"]["new"], 0)
        self.assertEqual(snapshot["summary"]["changed"], 1)
        self.assertEqual(state["items"][item_id]["revision"], 2)
        self.assertEqual(
            snapshot["categories"]["changed"][0]["last_seen_run"], "run-two"
        )
        self.assertEqual(state["items"][item_id]["current"]["client_project"], "Serenichron Level 1")

    def test_zero_candidate_run_carries_pending_without_closing(self):
        state, _, _ = ingest(self.tmp_path, "run-one", [proposal()])
        item = next(iter(state["items"].values()))
        run_dir = self.tmp_path / "run-empty"
        write_json(run_dir / "proposals.json", [])
        write_json(run_dir / "ambiguous.json", [])
        snapshot = review_state.ingest_run(run_dir, state)
        self.assertEqual(item["disposition"], "pending")
        self.assertEqual(snapshot["summary"]["carried_pending"], 1)
        self.assertEqual(snapshot["summary"]["resolved_disappeared"], 0)
        self.assertTrue(any(w["type"] == "coverage_warning" for w in snapshot["coverage_warnings"]))

    def test_lifecycle_preserves_explicit_terminal_disposition(self):
        state, _, _ = ingest(self.tmp_path, "run-one", [proposal()])
        item_id = next(iter(state["items"]))
        review_state.set_disposition(state, item_id, "approved", "board-review")
        run_dir = self.tmp_path / "run-empty"
        write_json(run_dir / "proposals.json", [])
        write_json(run_dir / "ambiguous.json", [])
        snapshot = review_state.ingest_run(run_dir, state)
        self.assertEqual(state["items"][item_id]["disposition"], "approved")
        self.assertEqual(snapshot["summary"]["resolved_disappeared"], 1)
        self.assertEqual(snapshot["summary"]["carried_pending"], 0)

    def test_ambiguous_source_can_become_pending_when_later_routed(self):
        ambiguous = {
            "id": "A001",
            "source": "codex",
            "machine": "omarchy-desktop",
            "time": "2026-07-28T09:00:00+03:00–2026-07-28T10:00:00+03:00",
            "label": "automation-clockify-sync",
            "reason": "No route matched",
        }
        state, initial, _ = ingest(self.tmp_path, "run-ambiguous", [], [ambiguous])
        item_id = next(iter(state["items"]))
        self.assertEqual("No route matched", initial["categories"]["new"][0]["reason"])
        run_dir = self.tmp_path / "run-routed"
        write_json(run_dir / "proposals.json", [proposal()])
        write_json(run_dir / "ambiguous.json", [])
        review_state.ingest_run(run_dir, state)
        self.assertEqual(list(state["items"]), [item_id])
        self.assertEqual(state["items"][item_id]["disposition"], "pending")

    def test_dry_run_does_not_write_state_or_snapshot(self):
        run_dir = self.tmp_path / "run-one"
        write_json(run_dir / "proposals.json", [proposal()])
        write_json(run_dir / "ambiguous.json", [])
        state_path = self.tmp_path / "nested" / "state.json"
        result = review_state.main([str(run_dir), "--state", str(state_path), "--dry-run"])
        self.assertEqual(result, 0)
        self.assertFalse(state_path.exists())
        self.assertFalse((run_dir / "review-snapshot.json").exists())

    def test_unavailable_machine_becomes_coverage_warning(self):
        run_dir = self.tmp_path / "run-unavailable"
        write_json(run_dir / "proposals.json", [])
        write_json(run_dir / "ambiguous.json", [])
        write_json(
            run_dir / "run-report.json",
            {
                "evidence": {
                    "sessions": [
                        {
                            "machine": "macbook",
                            "status": "unavailable",
                            "errors": ["SSH authentication failed"],
                        }
                    ]
                }
            },
        )

        snapshot = review_state.ingest_run(run_dir, review_state._new_state())

        self.assertTrue(
            any(
                warning["type"] == "source_unavailable"
                and warning["source"] == "sessions/macbook"
                for warning in snapshot["coverage_warnings"]
            )
        )
