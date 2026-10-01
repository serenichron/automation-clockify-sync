"""Verified posted credit is exact, local, and independent of activity IDs."""
from __future__ import annotations

import copy
import base64
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts import review_corrections, work_accounting_pipeline as pipeline


def _description_digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _proposal(activity: str, evidence: list[str], description: str, start: str, minutes: int, segment: int = 1) -> dict:
    begins = dt.datetime.fromisoformat(start)
    ends = begins + dt.timedelta(minutes=minutes)
    return {
        "activity_id": activity, "review_activity_key": "wka-" + activity,
        "allocation_segment": segment, "candidate_key": "wks-" + activity + str(segment),
        "start": begins.isoformat(), "end": ends.isoformat(),
        "duration_minutes": minutes, "duration_seconds": minutes * 60,
        "client_project": "Example Project", "clockify_project_suffix": "project-1",
        "description": description, "provenance": {"evidence_ids": evidence},
    }


def _sheet_row(proposal: dict) -> list[object]:
    start = dt.datetime.fromisoformat(proposal["start"])
    end = dt.datetime.fromisoformat(proposal["end"])
    return [
        f'{proposal["review_activity_key"]}-s{proposal["allocation_segment"]:02d}',
        start.strftime("%Y-%m-%d %H:%M"), end.strftime("%Y-%m-%d %H:%M"),
        proposal["duration_minutes"], proposal["client_project"], "", "", "",
        proposal["description"], "Approved", 1, "prior-run", "", "posted", "",
    ]


def _block(proposal: dict, block_id: str) -> dict:
    return {
        "block_id": block_id, "kind": "existing_clockify",
        "start": dt.datetime.fromisoformat(proposal["start"]),
        "end": dt.datetime.fromisoformat(proposal["end"]),
        "project_id_suffix": proposal["clockify_project_suffix"],
        "description": proposal["description"],
    }


class VerifiedPostedCreditTests(unittest.TestCase):
    def fixture(self, root: Path):
        prior_dir = root / "prior-run"
        prior_dir.mkdir()
        prior = [
            _proposal("old-contract", ["ev-a", "ev-b", "ev-c"], "Old contract outcome", "2026-09-12T08:19:00+03:00", 2),
            _proposal("old-contract", ["ev-a", "ev-b", "ev-c"], "Old contract outcome", "2026-09-12T08:23:00+03:00", 1, 2),
            _proposal("old-runtime", ["ev-d", "ev-e"], "Old runtime outcome", "2026-09-12T08:24:00+03:00", 1),
        ]
        prior_path = prior_dir / "proposals.json"
        prior_path.write_text(json.dumps(prior), encoding="utf-8")
        current = [
            _proposal("new-contract", ["ev-a", "ev-b", "ev-c"], "Revised contract outcome", "2026-09-12T08:34:00+03:00", 3),
            _proposal("new-runtime", ["ev-d", "ev-e"], "Assessed runtime outcome", "2026-09-12T08:51:00+03:00", 1),
            _proposal("unrelated", ["ev-z"], "Unrelated outcome", "2026-09-12T09:00:00+03:00", 2),
        ]
        blocks = [_block(row, f"clockify-{index}") for index, row in enumerate(prior)]
        credits = []
        for current_row, posted in ((current[0], prior[:2]), (current[1], prior[2:])):
            credits.append({
                "schema_version": 1,
                "record_type": "verified_posted_credit",
                "evidence_fingerprint": review_corrections.evidence_fingerprint(current_row["provenance"]["evidence_ids"]),
                "project_suffix": current_row["clockify_project_suffix"],
                "current_description_sha256": _description_digest(current_row["description"]),
                "prior_run_id": "prior-run",
                "sheet_publication_run_id": "prior-run",
                "prior_proposals_sha256": "sha256:" + hashlib.sha256(prior_path.read_bytes()).hexdigest(),
                "prior_proposals_base64": base64.b64encode(prior_path.read_bytes()).decode("ascii"),
                "posted_rows": [
                    {"sheet_row": _sheet_row(row), "clockify_block_id": f"clockify-{prior.index(row)}"}
                    for row in posted
                ],
            })
        return current, blocks, credits

    def test_explicit_posted_equivalence_credits_changed_ids_without_touching_unrelated_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current, blocks, credits = self.fixture(root)
            original = copy.deepcopy(current)
            survivors, skipped = pipeline._apply_verified_posted_credits(current, blocks, credits)
            self.assertEqual(["unrelated"], [row["activity_id"] for row in survivors])
            self.assertEqual({"new-contract", "new-runtime"}, {row["activity_id"] for row in skipped})
            self.assertTrue(all(row["reason"] == "verified previously posted accomplishment" for row in skipped))
            self.assertTrue(all("credited_overlap_receipt" not in row for row in skipped))
            self.assertEqual(original, current)
            self.assertEqual((survivors, skipped), pipeline._apply_verified_posted_credits(current, blocks, credits))

    def test_missing_or_ambiguous_baseline_and_changed_current_key_remain_visible(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current, blocks, credits = self.fixture(root)
            for candidate_blocks in (blocks[1:], [*blocks, copy.deepcopy(blocks[0])]):
                with self.subTest(blocks=len(candidate_blocks)):
                    survivors, skipped = pipeline._apply_verified_posted_credits(current, candidate_blocks, credits)
                    self.assertIn("new-contract", [row["activity_id"] for row in survivors])
                    self.assertNotIn("new-contract", [row["activity_id"] for row in skipped])
            for field, value in (
                ("description", "Different deliverable"),
                ("clockify_project_suffix", "different-project"),
                ("provenance", {"evidence_ids": ["ev-a", "ev-b"]}),
            ):
                changed = copy.deepcopy(current)
                changed[0][field] = value
                survivors, _skipped = pipeline._apply_verified_posted_credits(changed, blocks, credits)
                self.assertIn("new-contract", [row["activity_id"] for row in survivors])

    def test_all_current_segments_of_one_accomplishment_are_credited_together(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current, blocks, credits = self.fixture(root)
            prior_path = root / "prior-run" / "proposals.json"
            prior = json.loads(prior_path.read_text(encoding="utf-8"))
            old_contract = prior[0]
            old_contract["end"] = (dt.datetime.fromisoformat(old_contract["start"]) + dt.timedelta(minutes=3)).isoformat()
            old_contract["duration_minutes"] = 3
            old_contract["duration_seconds"] = 180
            prior_path.write_text(json.dumps([old_contract, prior[2]]), encoding="utf-8")
            blocks = [_block(old_contract, "clockify-0"), blocks[2]]
            credits[0]["posted_rows"] = [{"sheet_row": _sheet_row(old_contract), "clockify_block_id": "clockify-0"}]
            credits[0]["prior_proposals_sha256"] = "sha256:" + hashlib.sha256(prior_path.read_bytes()).hexdigest()
            credits[0]["prior_proposals_base64"] = base64.b64encode(prior_path.read_bytes()).decode("ascii")
            first = current[0]
            first["duration_minutes"] = 2
            first["duration_seconds"] = 120
            first["end"] = (dt.datetime.fromisoformat(first["start"]) + dt.timedelta(minutes=2)).isoformat()
            second = copy.deepcopy(first)
            second["allocation_segment"] = 2
            second["candidate_key"] = "wks-new-contract-2"
            second["start"] = first["end"]
            second["end"] = (dt.datetime.fromisoformat(second["start"]) + dt.timedelta(minutes=1)).isoformat()
            second["duration_minutes"] = 1
            second["duration_seconds"] = 60
            survivors, skipped = pipeline._apply_verified_posted_credits(
                [first, second, *current[1:]], blocks, credits[:1]
            )
            self.assertEqual(["new-runtime", "unrelated"], [row["activity_id"] for row in survivors])
            self.assertEqual(2, len(skipped))
            self.assertEqual(2, sum(row["activity_id"] == "new-contract" for row in skipped))
            distinct = copy.deepcopy(second)
            distinct["activity_id"] = "different-accomplishment"
            distinct["review_activity_key"] = "wka-different-accomplishment"
            survivors, skipped = pipeline._apply_verified_posted_credits(
                [first, distinct, *current[1:]], blocks, credits[:1]
            )
            self.assertEqual([], skipped)
            self.assertEqual(2, len([row for row in survivors if row["provenance"]["evidence_ids"] == ["ev-a", "ev-b", "ev-c"]]))

    def test_replay_uses_only_embedded_prior_proposals_after_prior_run_disappears(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current, blocks, credits = self.fixture(root)
            (root / "prior-run" / "proposals.json").unlink()
            (root / "prior-run").rmdir()
            survivors, skipped = pipeline._apply_verified_posted_credits(current, blocks, credits)
            self.assertEqual(["unrelated"], [row["activity_id"] for row in survivors])
            self.assertEqual(2, len(skipped))
            altered = copy.deepcopy(credits)
            altered[0]["prior_proposals_base64"] = base64.b64encode(b"[]").decode("ascii")
            survivors, skipped = pipeline._apply_verified_posted_credits(current, blocks, altered)
            self.assertIn("new-contract", [row["activity_id"] for row in survivors])
            self.assertNotIn("new-contract", [row["activity_id"] for row in skipped])

    def test_later_legacy_snapshot_can_prove_posted_rows_without_original_publication_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current, blocks, credits = self.fixture(root)
            later = root / "later-capture"
            later.mkdir()
            legacy = json.loads((root / "prior-run" / "proposals.json").read_text(encoding="utf-8"))
            for row in legacy:
                del row["duration_seconds"]
            later_path = later / "proposals.json"
            later_path.write_text(json.dumps(legacy), encoding="utf-8")
            (root / "prior-run" / "proposals.json").unlink()
            (root / "prior-run").rmdir()
            for credit in credits:
                credit["prior_run_id"] = "later-capture"
                credit["sheet_publication_run_id"] = "missing-original-run"
                credit["prior_proposals_sha256"] = "sha256:" + hashlib.sha256(later_path.read_bytes()).hexdigest()
                credit["prior_proposals_base64"] = base64.b64encode(later_path.read_bytes()).decode("ascii")
                for posted in credit["posted_rows"]:
                    posted["sheet_row"][11] = "missing-original-run"
            path = root / "review-corrections.jsonl"
            kwargs = {"runs_root": root, "current_proposals": current, "existing_blocks": blocks}
            self.assertTrue(review_corrections.append_verified_posted_credit(path, credits[0], **kwargs))
            self.assertTrue(review_corrections.append_verified_posted_credit(path, credits[1], **kwargs))
            later_path.unlink()
            later.rmdir()
            sealed = review_corrections.load_verified_posted_credits(path)
            survivors, skipped = pipeline._apply_verified_posted_credits(current, blocks, sealed)
            self.assertEqual(["unrelated"], [row["activity_id"] for row in survivors])
            self.assertEqual(2, len(skipped))
            self.assertTrue(all(row["sheet_publication_run_id"] == "missing-original-run" for row in skipped))
            self.assertTrue(all(row["prior_run_id"] == "later-capture" for row in skipped))

    def test_sheet_duration_numeric_strings_are_exact_but_invalid_numbers_are_not(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current, blocks, credits = self.fixture(root)
            for entry in credits[0]["posted_rows"]:
                entry["sheet_row"][3] = f'{entry["sheet_row"][3]}.0'
            survivors, _skipped = pipeline._apply_verified_posted_credits(current, blocks, credits)
            self.assertNotIn("new-contract", [row["activity_id"] for row in survivors])
            for invalid in (True, "nan", "inf", "2.5", "two"):
                changed = copy.deepcopy(credits)
                changed[0]["posted_rows"][0]["sheet_row"][3] = invalid
                survivors, _skipped = pipeline._apply_verified_posted_credits(current, blocks, changed)
                self.assertIn("new-contract", [row["activity_id"] for row in survivors])

    def test_malformed_or_evidence_only_credit_is_rejected_from_correction_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _current, _blocks, credits = self.fixture(root)
            malformed = copy.deepcopy(credits[0])
            del malformed["current_description_sha256"]
            with self.assertRaises(review_corrections.ReviewDecisionError):
                review_corrections.validate_verified_posted_credit(malformed)
            with self.assertRaises(review_corrections.ReviewDecisionError):
                review_corrections.validate_verified_posted_credit({"evidence_fingerprint": credits[0]["evidence_fingerprint"]})

    def test_machine_credit_round_trips_in_hash_chain_without_becoming_a_human_decision(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current, blocks, credits = self.fixture(root)
            path = root / "review-corrections.jsonl"
            kwargs = {"runs_root": root, "current_proposals": current, "existing_blocks": blocks}
            self.assertTrue(review_corrections.append_verified_posted_credit(path, credits[0], **kwargs))
            self.assertFalse(review_corrections.append_verified_posted_credit(path, credits[0], **kwargs))
            self.assertTrue(review_corrections.append_verified_posted_credit(path, credits[1], **kwargs))
            self.assertEqual([], review_corrections.load_decisions(path))
            loaded = review_corrections.load_verified_posted_credits(path)
            self.assertEqual(2, len(loaded))
            self.assertEqual([], review_corrections.derive_learning_cases(review_corrections.load_decisions(path)))
            self.assertEqual([], pipeline._load_corrections(path))
            self.assertEqual([], pipeline._load_regression_cases(path))
            self.assertEqual(2, len(pipeline._load_verified_posted_credits(path)))
            lines = path.read_text(encoding="utf-8").splitlines()
            altered = json.loads(lines[0])
            altered["project_suffix"] = "another-project"
            lines[0] = json.dumps(altered)
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(review_corrections.ReviewDecisionError):
                review_corrections.load_verified_posted_credits(path)

    def test_append_rejects_source_or_baseline_that_differs_from_captured_proof(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            current, blocks, credits = self.fixture(root)
            path = root / "review-corrections.jsonl"
            prior_path = root / "prior-run" / "proposals.json"
            prior_path.write_text("[]", encoding="utf-8")
            with self.assertRaises(review_corrections.ReviewDecisionError):
                review_corrections.append_verified_posted_credit(
                    path, credits[0], runs_root=root, current_proposals=current, existing_blocks=blocks
                )
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
