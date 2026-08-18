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


def semantic_proposal(candidate_key, activity_id, evidence_ids, *, start="2026-07-28T09:00:00+03:00", end="2026-07-28T10:00:00+03:00"):
    return {
        "id": "S001",
        "candidate_key": candidate_key,
        "review_activity_key": f"wka-{activity_id}",
        "allocation_segment": 1,
        "activity_id": activity_id,
        "workstream_id": "ws-clockify",
        "start": start,
        "end": end,
        "source": [f"evidence:{value}" for value in evidence_ids],
        "source_label": "Clockify review state",
        "client_project": "Serenichron Level 2",
        "description": "SC — Rebuilt stable review state for evidence-backed work",
        "provenance": {
            "source_type": "semantic_activity",
            "source_session_id": activity_id,
            "source_machine": "cross-machine",
            "evidence_ids": evidence_ids,
        },
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

    def test_evidence_backed_ambiguous_activities_do_not_collapse(self):
        ambiguous = [
            {
                "id": "A001",
                "activity_id": "act-one",
                "exception_kind": "contested_time",
                "reason": "recommended effort exceeds available observed capacity",
            },
            {
                "id": "A002",
                "activity_id": "act-two",
                "exception_kind": "contested_time",
                "reason": "recommended effort exceeds available observed capacity",
            },
        ]

        state, snapshot, _ = ingest(self.tmp_path, "run-ambiguous", [], ambiguous)

        self.assertEqual(2, len(state["items"]))
        self.assertEqual(2, snapshot["summary"]["new"])
        self.assertEqual(
            {"act-one", "act-two"},
            {item["activity_id"] for item in snapshot["categories"]["new"]},
        )
        self.assertEqual(
            2,
            len({item["candidate_key"] for item in snapshot["categories"]["new"]}),
        )

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

    def test_semantic_activity_segment_replay_keeps_rvi_and_evidence_view(self):
        record = semantic_proposal("wk-segment-one", "act-clockify", ["ev-1", "ev-2"])
        state, snapshot, _ = ingest(self.tmp_path, "run-one", [record])
        item_id = next(iter(state["items"]))
        run_dir = self.tmp_path / "run-two"
        replay = dict(record, id="S999")
        write_json(run_dir / "proposals.json", [replay])
        write_json(run_dir / "ambiguous.json", [])
        second = review_state.ingest_run(run_dir, state)
        self.assertEqual([item_id], list(state["items"]))
        self.assertEqual(0, second["summary"]["new"])
        self.assertEqual("act-clockify", snapshot["categories"]["new"][0]["activity_id"])
        self.assertEqual(["ev-1", "ev-2"], snapshot["categories"]["new"][0]["evidence_ids"])

    def test_allocation_move_preserves_review_id_and_terminal_decision(self):
        record = semantic_proposal("wks-first", "act-clockify", ["ev-1", "ev-2"])
        state, _, _ = ingest(self.tmp_path, "run-one", [record])
        item_id = next(iter(state["items"]))
        review_state.set_disposition(state, item_id, "approved", "board-review")

        moved = semantic_proposal(
            "wks-first",
            "act-clockify",
            ["ev-1", "ev-2"],
            start="2026-07-28T10:00:00+03:00",
            end="2026-07-28T11:00:00+03:00",
        )
        run_dir = self.tmp_path / "run-moved"
        write_json(run_dir / "proposals.json", [moved])
        write_json(run_dir / "ambiguous.json", [])
        snapshot = review_state.ingest_run(run_dir, state)

        self.assertEqual([item_id], list(state["items"]))
        self.assertEqual("approved", state["items"][item_id]["disposition"])
        self.assertEqual(0, snapshot["summary"]["new"])
        self.assertEqual(1, snapshot["summary"]["changed"])

    def test_one_segment_becoming_multiple_keeps_one_activity_review_item(self):
        original = semantic_proposal("wks-one", "act-clockify", ["ev-1", "ev-2"])
        state, _, _ = ingest(self.tmp_path, "run-one", [original])
        item_id = next(iter(state["items"]))
        review_state.set_disposition(state, item_id, "rejected", "board-review")

        first = semantic_proposal(
            "wks-one",
            "act-clockify",
            ["ev-1", "ev-2"],
            end="2026-07-28T09:30:00+03:00",
        )
        second = semantic_proposal(
            "wks-two",
            "act-clockify",
            ["ev-1", "ev-2"],
            start="2026-07-28T10:30:00+03:00",
            end="2026-07-28T11:00:00+03:00",
        )
        second["allocation_segment"] = 2
        run_dir = self.tmp_path / "run-split-allocation"
        write_json(run_dir / "proposals.json", [second, first])
        write_json(run_dir / "ambiguous.json", [])
        snapshot = review_state.ingest_run(run_dir, state)

        self.assertEqual([item_id], list(state["items"]))
        self.assertEqual("rejected", state["items"][item_id]["disposition"])
        self.assertEqual(2, len(state["items"][item_id]["current"]["allocation_segments"]))
        self.assertEqual(0, snapshot["summary"]["new"])

    def test_semantic_allocation_aggregation_preserves_exact_seconds(self):
        first = semantic_proposal(
            "wks-one", "act-clockify", ["ev-1", "ev-2"],
            end="2026-07-28T09:30:17+03:00",
        )
        first.update({"duration_minutes": 30, "duration_seconds": 1817})
        second = semantic_proposal(
            "wks-two", "act-clockify", ["ev-1", "ev-2"],
            start="2026-07-28T10:00:00+03:00",
            end="2026-07-28T10:17:29+03:00",
        )
        second.update({"allocation_segment": 2, "duration_minutes": 17, "duration_seconds": 1049})

        state, _snapshot, _ = ingest(
            self.tmp_path, "run-exact-seconds", [first, second]
        )

        current = next(iter(state["items"].values()))["current"]
        self.assertEqual(2866, current["duration_seconds"])
        self.assertEqual(
            [1817, 1049],
            [segment["duration_seconds"] for segment in current["allocation_segments"]],
        )

    def test_verified_split_links_children_and_supersedes_parent_without_id_reuse(self):
        legacy = semantic_proposal("legacy-candidate", "legacy-activity", ["ev-1", "ev-2"])
        legacy.pop("activity_id")
        legacy["provenance"].pop("source_session_id")
        state, _, _ = ingest(self.tmp_path, "run-legacy", [legacy])
        parent_id = next(iter(state["items"]))
        fingerprint = review_state._evidence_fingerprint(state["items"][parent_id]["current"])
        child_one = semantic_proposal("wk-child-one", "act-child-one", ["ev-1"], end="2026-07-28T09:30:00+03:00")
        child_two = semantic_proposal("wk-child-two", "act-child-two", ["ev-2"], start="2026-07-28T09:30:00+03:00")
        for child in (child_one, child_two):
            child["parent_review_item_id"] = parent_id
            child["parent_evidence_fingerprint"] = fingerprint
        run_dir = self.tmp_path / "run-split"
        write_json(run_dir / "proposals.json", [child_one, child_two])
        write_json(run_dir / "ambiguous.json", [])
        snapshot = review_state.ingest_run(run_dir, state)
        self.assertEqual("superseded", state["items"][parent_id]["disposition"])
        child_ids = [item_id for item_id in state["items"] if item_id != parent_id]
        self.assertEqual(2, len(child_ids))
        self.assertTrue(all(state["items"][item_id]["parent_review_item_id"] == parent_id for item_id in child_ids))
        self.assertEqual(2, snapshot["summary"]["new"])

    def test_unproven_split_requires_explicit_migration_without_superseding_parent(self):
        legacy = semantic_proposal("legacy-candidate", "legacy-activity", ["ev-1", "ev-2"])
        state, _, _ = ingest(self.tmp_path, "run-legacy", [legacy])
        parent_id = next(iter(state["items"]))
        child = semantic_proposal("wk-child-one", "act-child-one", ["ev-1"])
        child["parent_review_item_id"] = parent_id
        run_dir = self.tmp_path / "run-unproven-split"
        write_json(run_dir / "proposals.json", [child])
        write_json(run_dir / "ambiguous.json", [])
        snapshot = review_state.ingest_run(run_dir, state)
        self.assertNotEqual("superseded", state["items"][parent_id]["disposition"])
        self.assertTrue(any(warning["type"] == "migration_required" for warning in snapshot["coverage_warnings"]))
