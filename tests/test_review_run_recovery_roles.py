"""Recovery provenance must not turn a derived run into a new recovery."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import test_review_run as fixtures


review = fixtures.review_run


class RecoveryRoleTests(unittest.TestCase):
    def fixture(self, runs: Path) -> Path:
        source, replay = fixtures.ReviewRunResultTests._complete_replay_fixture(runs)
        fixtures.ReviewRunResultTests._bootstrap_snapshots(source, replay)
        (source / "run-report.md").write_text("# synthetic recovery\n", encoding="utf-8")
        coverage = {
            "status": "complete", "incomplete_sources": [],
            "sources": {
                "sessions/macbook": {"status": "complete"},
                "repositories/macbook": {"status": "complete"},
            },
        }
        ledger_path = source / "evidence" / "evidence-ledger.json"
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        ledger["manifest"]["source_completeness"] = coverage
        fixtures.write_json(ledger_path, ledger)
        report_path = source / "run-report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report["evidence_ledger"]["source_completeness"] = coverage
        report["source_debt_recovery"] = {
            "source": "peer/macbook",
            "attempt_id": "sha256:" + "a" * 64,
            "transition_digest": "sha256:" + "b" * 64,
        }
        fixtures.write_json(report_path, report)
        slice_ = review.clockify_sync_collect.plan_slices(
            fixtures.dt.datetime(2026, 8, 1, tzinfo=fixtures.dt.timezone.utc),
            fixtures.dt.datetime(2026, 8, 2, tzinfo=fixtures.dt.timezone.utc),
            zone=review.clockify_sync_collect.BUCHAREST,
        )[0]
        bundle = review.collector_receipts.build_completion_bundle(source, slice_=slice_)
        review.collector_receipts.write_completion_bundle(
            source / "completion-bundle.json", bundle,
        )
        return source

    def process(self, args, run: Path):
        completed = subprocess.CompletedProcess([], 0, "", "")
        with mock.patch.object(review, "_run", return_value=completed):
            return review._process_run(args, run, {})

    def test_repair_of_recovery_keeps_completion_without_claiming_new_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            runs = Path(temp) / "runs"
            source = self.fixture(runs)
            original_bundle = (source / "completion-bundle.json").read_bytes()
            with mock.patch.object(review, "RUNS", runs):
                repair = review._prepare_repair_run(source)
                for name in (
                    "semantic-analysis.json", "work-accounting-result.json",
                    "quality_report.json", "review-snapshot.json",
                ):
                    (repair / name).write_bytes((source / name).read_bytes())
                args = review.parse_args([
                    "--repair-from", str(source), "--state", str(Path(temp) / "state.json"),
                ])
                code, result_path = self.process(args, repair)

            result = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual(0, code)
            self.assertEqual("pass", result["quality_status"])
            self.assertNotIn("source_debt_recovery", result)
            self.assertEqual(
                result["completion_bundle_digest"],
                review.collector_receipts.load_completion_bundle(
                    repair / "completion-bundle.json", run_dir=repair,
                ).bundle_digest,
            )
            self.assertEqual(original_bundle, (source / "completion-bundle.json").read_bytes())
            self.assertTrue((repair / "repair-source.json").is_file())
            self.assertEqual(
                json.loads((source / "run-report.json").read_text())["source_debt_recovery"],
                json.loads((repair / "run-report.json").read_text())["source_debt_recovery"],
            )

    def test_genuine_recovery_fresh_or_resumed_still_seals_receipt(self):
        for mode in ("fresh", "resume"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                runs = Path(temp) / "runs"
                source = self.fixture(runs)
                fixtures.write_json(source / "ledger-recovery.json", {"schema_version": 1})
                fixtures.write_json(source / "slice-finalization.json", {"schema_version": 1})
                bundle = review.collector_receipts.load_completion_bundle(
                    source / "completion-bundle.json", run_dir=source,
                )
                seal = source / "receipt-sealed.txt"

                def seal_receipt(run_dir: Path) -> None:
                    seal.write_text(run_dir.name, encoding="utf-8")

                mode_args = (
                    ["--resume-from", str(source)] if mode == "resume" else [
                        "--recover-source-debt-from", str(runs / "parent"),
                        "--recover-source", "peer/macbook",
                        "--recover-attempt-id", "sha256:" + "a" * 64,
                    ]
                )
                args = review.parse_args([
                    *mode_args, "--state", str(Path(temp) / "state.json"),
                ])
                with mock.patch.object(review, "RUNS", runs), mock.patch.object(
                    review, "_finalize_recovery_completion", return_value=bundle,
                ), mock.patch.object(
                    review, "_recovery_source_status", return_value="complete",
                ), mock.patch.object(
                    review.clockify_source_debt_recover, "seal_recovery_receipt",
                    side_effect=seal_receipt,
                ):
                    code, result_path = self.process(args, source)

                result = json.loads(result_path.read_text(encoding="utf-8"))
                self.assertEqual(0, code)
                self.assertEqual("pass", result["quality_status"])
                self.assertEqual("complete", result["source_debt_recovery"]["status"])
                self.assertEqual(bundle.bundle_digest, result["completion_bundle_digest"])
                self.assertEqual(source.name, seal.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
