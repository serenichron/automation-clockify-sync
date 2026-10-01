"""Historical consumers independently verify machine-credit repair ancestry."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import clockify_review_cycle as cycle, clockify_review_run as run
from scripts import review_corrections, work_accounting_pipeline
from test_review_run_posted_credit_snapshot import PostedCreditSnapshotTests


class PostedCreditAdoptionTests(unittest.TestCase):
    def fixture(self, root: Path):
        runs, source, override = PostedCreditSnapshotTests().fixture(root)
        with mock.patch.object(run, "RUNS", runs):
            child = run._prepare_repair_run(source, corrections_override=override)
            for filename in (
                "semantic-analysis.json", "work-accounting-result.json",
                "quality_report.json", "review-snapshot.json",
            ):
                (child / filename).write_bytes((source / filename).read_bytes())
            proposals = json.loads((source / "proposals.json").read_text())
            _ledger, events = work_accounting_pipeline.load_ledger(
                source / "evidence" / "evidence-ledger.json"
            )
            remaining, skipped = work_accounting_pipeline._apply_verified_posted_credits(
                proposals, work_accounting_pipeline._existing_blocks(events),
                review_corrections.load_verified_posted_credits(child / "review-corrections.jsonl"),
            )
            (child / "proposals.json").write_text(json.dumps(remaining, sort_keys=True) + "\n")
            accounting = json.loads((child / "work-accounting-result.json").read_text())
            accounting.update(proposals=remaining, skipped=skipped)
            (child / "work-accounting-result.json").write_text(json.dumps(accounting, sort_keys=True) + "\n")
            bundle = run._finalize_repair_completion(child)
        config = {"runs_dir": str(runs)}
        return runs, source, child, bundle, config

    def test_credit_repair_ancestry_reaches_unchanged_collector(self):
        """Catches ancestry rejecting a verified appended correction snapshot."""
        with tempfile.TemporaryDirectory() as temporary:
            runs, source, child, bundle, config = self.fixture(Path(temporary))
            with mock.patch.object(run, "RUNS", runs):
                ancestor, _bundle = cycle._collector_ancestor_from_repair(
                    config, child, bundle,
                )
            self.assertEqual(source, ancestor)

    def test_credit_adoption_requires_frozen_collector_corrections(self):
        """Catches accepting a valid repair chain rooted at a different frozen input."""
        with tempfile.TemporaryDirectory() as temporary:
            runs, source, child, _bundle, config = self.fixture(Path(temporary))
            frozen = cycle._digest(source / "review-corrections.jsonl")
            adopted = cycle._digest(child / "review-corrections.jsonl")
            with mock.patch.object(run, "RUNS", runs):
                cycle._verify_credit_adoption_transition(config, child, frozen, adopted)
                with self.assertRaises(cycle.CycleError):
                    cycle._verify_credit_adoption_transition(
                        config, child, "sha256:" + "f" * 64, adopted,
                    )

    def test_ancestry_rejects_tampered_credit_digest(self):
        """Catches lineage claims that no longer bind the child correction bytes."""
        with tempfile.TemporaryDirectory() as temporary:
            runs, _source, child, bundle, config = self.fixture(Path(temporary))
            lineage_path = child / "repair-source.json"
            lineage = json.loads(lineage_path.read_text())
            lineage["repair_corrections_sha256"] = "sha256:" + "a" * 64
            lineage_path.write_text(json.dumps(lineage, sort_keys=True) + "\n")
            with mock.patch.object(run, "RUNS", runs), self.assertRaises(cycle.CycleError):
                cycle._collector_ancestor_from_repair(config, child, bundle)

    def test_persisted_adoption_receipt_revalidates_five_row_credit_source(self):
        """Catches losing the correction transition after its adoption receipt is saved."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runs, source, child, _bundle, config = self.fixture(root)
            self.assertEqual(5, len(json.loads((child / "proposals.json").read_text())))
            frozen = {
                name: cycle._digest(source / name)
                for name in (
                    "period-manifest.json", "routing.json", "review-corrections.jsonl",
                    "review-acceptance.jsonl",
                )
            }
            adopted = {name: cycle._digest(child / name) for name in frozen}
            since, until = "2026-09-11", "2026-09-13"
            state_dir = root / "state"
            receipt_path = state_dir / "historical-adoption-receipts" / f"{since}.json"
            receipt_path.parent.mkdir(parents=True)
            unsigned = {
                "schema_version": cycle.HISTORICAL_ADOPTION_SCHEMA_VERSION,
                "since": since, "until": until,
                "frozen_snapshot_digests": frozen,
                "adopted_snapshot_digests": adopted,
                "source": {"run_dir": str(child)},
            }
            digest = cycle._value_digest(unsigned)
            receipt_path.write_text(json.dumps({**unsigned, "receipt_digest": digest}) + "\n")
            record = {
                "expected_snapshot_digests": frozen,
                "historical_adoption_receipt": str(receipt_path),
                "historical_adoption_receipt_digest": digest,
            }
            config["state_dir"] = str(state_dir)
            with mock.patch.object(run, "RUNS", runs):
                first = cycle._historical_adoption_document(config, record, since, until)
                second = cycle._historical_adoption_document(config, record, since, until)
            self.assertEqual(first, second)
            self.assertEqual(adopted, second["adopted_snapshot_digests"])


if __name__ == "__main__":
    unittest.main()
