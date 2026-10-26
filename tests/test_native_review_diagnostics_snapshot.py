import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import clockify_review_run as review
from scripts import semantic_analyzer as semantic
from scripts import work_accounting_pipeline as accounting
import test_review_run as replay_fixtures


NAME = "analyzer-cache-used.jsonl.review-diagnostics.jsonl"


class NativeDiagnosticSnapshotTests(unittest.TestCase):
    def fixture(self, root):
        cache_record = {"cache_key": "arc-" + "a" * 64,
                        "body_digest": "b" * 64,
                        "status": "rejected",
                        "failure_code": "contract_rejected_duplicate_evidence"}
        cache = (semantic.canonical_json(cache_record) + "\n").encode()
        diagnostic = {"cache_key": cache_record["cache_key"],
                      "body_digest": cache_record["body_digest"],
                      "failure_code": cache_record["failure_code"],
                      "review_scope": "extraction",
                      "coverage_contract": [{"bundle_ref": "b-0001", "allowed_member_range": [1, 2]}],
                      "activities": [
                          {"evidence_partitions": [{"bundle_ref": "b-0001", "member_ranges": [[1, 2]]}]},
                          {"evidence_partitions": [{"bundle_ref": "b-0001", "member_ranges": [[2, 2]]}]},
                      ], "exceptions": [], "omissions": []}
        content = (semantic.canonical_json(diagnostic) + "\n").encode()
        (root / NAME).write_bytes(content)
        analysis = {"analyzer_cache": {"review_diagnostics_snapshot": {
            "path": NAME, "record_count": 1, "sha256": hashlib.sha256(content).hexdigest()}}}
        return analysis, cache, content, diagnostic

    def test_valid_canonical_bound_sidecar_is_returned_for_native_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            analysis, cache, content, _ = self.fixture(root)
            self.assertEqual(content, review._review_diagnostics_snapshot(root, analysis, cache))

    def test_historical_absent_descriptor_does_not_consult_sidecar(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / NAME).write_text("not trusted or read")
            self.assertIsNone(review._review_diagnostics_snapshot(root, {}, b""))

    def test_missing_or_changed_sealed_diagnostics_are_rejected(self):
        for change in ("missing", "changed"):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                analysis, cache, _, _ = self.fixture(root)
                if change == "missing":
                    (root / NAME).unlink()
                else:
                    (root / NAME).write_text("{}\n")
                with self.assertRaises(ValueError):
                    review._review_diagnostics_snapshot(root, analysis, cache)

    def test_path_count_and_digest_descriptor_variants_fail_closed(self):
        variants = [("path", "../" + NAME), ("path", "/tmp/foreign.jsonl"),
                    ("record_count", True), ("record_count", 0), ("record_count", 2),
                    ("sha256", "A" * 64)]
        for key, value in variants:
            with self.subTest(key=key, value=value), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                analysis, cache, _, _ = self.fixture(root)
                analysis["analyzer_cache"]["review_diagnostics_snapshot"][key] = value
                with self.assertRaises(ValueError):
                    review._review_diagnostics_snapshot(root, analysis, cache)

    def test_explicit_null_descriptor_is_not_historical_absence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            analysis, cache, _, _ = self.fixture(root)
            analysis["analyzer_cache"]["review_diagnostics_snapshot"] = None
            with self.assertRaises(ValueError):
                review._review_diagnostics_snapshot(root, analysis, cache)

    def test_symlink_source_and_noncanonical_parent_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            source.mkdir()
            analysis, cache, content, _ = self.fixture(source)
            (source / NAME).unlink()
            (root / "external.jsonl").write_bytes(content)
            (source / NAME).symlink_to(root / "external.jsonl")
            with self.assertRaises(ValueError):
                review._review_diagnostics_snapshot(source, analysis, cache)
            (source / NAME).unlink()
            (source / NAME).write_bytes(content)
            alias = root / "alias"
            alias.symlink_to(source, target_is_directory=True)
            with self.assertRaises(ValueError):
                review._review_diagnostics_snapshot(alias, analysis, cache)

    def test_foreign_or_unsanitized_record_is_rejected_even_with_matching_digest(self):
        for mutate in (lambda row: row.update(cache_key="arc-" + "c" * 64),
                       lambda row: row.update(body_digest="d" * 64),
                       lambda row: row.update(private_prose="must never be copied"),
                       lambda row: row["activities"][0].update(description="private prose")):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                analysis, cache, _, diagnostic = self.fixture(root)
                mutate(diagnostic)
                content = (semantic.canonical_json(diagnostic) + "\n").encode()
                (root / NAME).write_bytes(content)
                analysis["analyzer_cache"]["review_diagnostics_snapshot"]["sha256"] = hashlib.sha256(content).hexdigest()
                with self.assertRaises(ValueError):
                    review._review_diagnostics_snapshot(root, analysis, cache)

    def test_noncanonical_or_duplicate_jsonl_records_are_rejected(self):
        for duplicate in (False, True):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                analysis, cache, content, diagnostic = self.fixture(root)
                content = content * 2 if duplicate else (json.dumps(diagnostic) + "\n").encode()
                (root / NAME).write_bytes(content)
                descriptor = analysis["analyzer_cache"]["review_diagnostics_snapshot"]
                descriptor.update(sha256=hashlib.sha256(content).hexdigest(), record_count=2 if duplicate else 1)
                with self.assertRaises(ValueError):
                    review._review_diagnostics_snapshot(root, analysis, cache)

    def offline_source(self, root):
        real_seal = accounting._seal_analyzer_cache_snapshot

        def seal_with_bound_numeric_diagnostic(run_dir, cache_path, analysis):
            snapshot = real_seal(run_dir, cache_path, analysis)
            records = [json.loads(line) for line in (run_dir / "analyzer-cache-used.jsonl").read_bytes().splitlines()]
            rejected = next(row for row in records if row.get("status") == "rejected"
                            and row.get("failure_code") == "contract_rejected_duplicate_evidence")
            descriptor, _, _, diagnostic = self.fixture(run_dir)
            diagnostic.update({key: rejected[key] for key in ("cache_key", "body_digest", "failure_code")})
            content = (semantic.canonical_json(diagnostic) + "\n").encode()
            (run_dir / NAME).write_bytes(content)
            descriptor["analyzer_cache"]["review_diagnostics_snapshot"]["sha256"] = hashlib.sha256(content).hexdigest()
            analysis["analyzer_cache"].update(descriptor["analyzer_cache"])
            return snapshot

        with mock.patch.object(accounting, "_seal_analyzer_cache_snapshot", side_effect=seal_with_bound_numeric_diagnostic):
            return replay_fixtures.ReviewRunResultTests._write_real_offline_replay_source(root / "runs", root, failed_review=True)

    def test_replay_copies_and_authenticates_sidecar_before_cache_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.offline_source(root)
            expected = (source / NAME).read_bytes()
            real_preflight = review._preflight_replay_analyzer_cache
            inspected = []

            def preflight(target, cache_path, analysis, **kwargs):
                copied = cache_path.with_name(NAME)
                self.assertEqual(expected, copied.read_bytes())
                inspected.append(copied)
                return real_preflight(target, cache_path, analysis, **kwargs)

            with mock.patch.object(review, "RUNS", root / "runs"), \
                 mock.patch.object(review, "_preflight_replay_analyzer_cache", side_effect=preflight):
                replay = review._prepare_replay_run(source)
                review._replay_analyzer_cache(replay)
                self.assertEqual(1, len(inspected))
                (replay / "replay-fixture" / NAME).write_text("{}\n")
                with self.assertRaises(ValueError):
                    review._replay_analyzer_cache(replay)

    def test_repair_copies_bound_sidecar_and_rejects_copied_drift(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self.offline_source(root)
            with mock.patch.object(review, "RUNS", root / "runs"):
                repair = review._prepare_repair_run(source)
                self.assertEqual((source / NAME).read_bytes(), (repair / NAME).read_bytes())
                review._repair_analysis_fixture(repair)
                (repair / NAME).write_text("{}\n")
                with self.assertRaises(ValueError):
                    review._repair_analysis_fixture(repair)


if __name__ == "__main__":
    unittest.main()
