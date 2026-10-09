"""Recorded proofs tolerate authentic runtime upgrades, never semantic drift."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts import clockify_pending_runtime_proof as runtime_proof
from scripts import clockify_pending_review_selection as pending
import test_pending_review_selection as selection_fixtures


class PendingRuntimeProofTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.fixture = selection_fixtures.PendingSelectionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.native = pending.verify(
            bindings_path=self.fixture.binding_path, source_dir=self.fixture.current_dir,
            proposals=self.fixture.current, spreadsheet_id="sheet", sheet_title="August 2026 review",
            run_id=self.fixture.current_dir.name, project_allowlist={},
        )["receipt"]

    @staticmethod
    def seal(proof):
        """Build fixture hashes independently of the production proof helpers."""
        unsigned = {key: value for key, value in proof.items() if key != "acceptance_sha256"}
        proof["acceptance_sha256"] = "sha256:" + hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return proof

    def proof(self, release, upgraded_roles=("consumer", "pipeline", "allocator")):
        proof = copy.deepcopy(self.native)
        if release == "current":
            return proof
        handles = {}
        for role, current in proof["runtime_artifacts"].items():
            path = self.root / release / f"{role}.py"
            path.parent.mkdir(exist_ok=True)
            content = Path(current["path"]).read_bytes()
            if role in upgraded_roles:
                content += b"\n# authentic historical release\n"
            path.write_bytes(content)
            handles[role] = {"path": str(path), "sha256": "sha256:" + hashlib.sha256(content).hexdigest()}
        proof["runtime_artifacts"] = handles
        return self.seal(proof)

    def test_authentic_runtime_upgrades_preserve_the_entire_recorded_proof(self):
        """Catches using runtime version equality as an input-corruption gate."""
        for upgraded in (("consumer",), ("pipeline",), ("allocator",), ("consumer", "pipeline", "allocator")):
            with self.subTest(upgraded=upgraded):
                actual = self.proof("original", upgraded)
                expected = self.proof("current", upgraded)
                actual_before, expected_before = copy.deepcopy(actual), copy.deepcopy(expected)
                paths = [Path(handle["path"]) for proof in (actual, expected)
                         for handle in proof["runtime_artifacts"].values()]
                files_before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths}
                try:
                    result = runtime_proof.verify_recorded(actual, expected)
                except ValueError as exc:
                    self.fail(f"authentic semantically invariant upgrade was rejected: {exc}")
                self.assertEqual(actual_before, result)
                self.assertEqual(actual_before, actual)
                self.assertEqual(expected_before, expected)
                self.assertEqual(files_before, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths})

    def test_identical_code_relocation_preserves_original_handles(self):
        """Catches replacing original runtime locations with current locations."""
        actual = self.proof("original-ș", ())
        expected = self.proof("current")
        self.assertEqual(actual, runtime_proof.verify_recorded(actual, expected))

    def test_role_substitution_is_not_an_authentic_runtime_upgrade(self):
        """Catches duplicate historical roles or non-native current role maps."""
        for side in ("original", "current"):
            for mode in ("duplicate", "swapped"):
                if side == "original" and mode == "swapped":
                    continue  # Old distinct versions have no producer-semantic registry.
                with self.subTest(side=side, mode=mode):
                    actual, expected = self.proof("original"), self.proof("current")
                    proof = actual if side == "original" else expected
                    handles = proof["runtime_artifacts"]
                    if mode == "duplicate":
                        handles["allocator"] = copy.deepcopy(handles["pipeline"])
                    else:
                        handles["allocator"], handles["pipeline"] = handles["pipeline"], handles["allocator"]
                    self.seal(proof)
                    with self.assertRaises(ValueError):
                        runtime_proof.verify_recorded(actual, expected)

    def test_tampered_original_or_current_runtime_bytes_are_rejected(self):
        """Catches authenticating only one release or trusting its claimed hash."""
        for side in ("original", "current"):
            for role in ("consumer", "pipeline", "allocator"):
                with self.subTest(side=side, role=role):
                    actual = self.proof("original")
                    expected = self.proof("current", ("consumer", "pipeline", "allocator"))
                    proof = actual if side == "original" else expected
                    path = Path(proof["runtime_artifacts"][role]["path"])
                    # Current runtime files belong to the isolated checkout;
                    # test drift through a copied handle, never edit code under test.
                    if side == "current":
                        path = self.root / f"tampered-current-{role}.py"
                        path.write_bytes(Path(proof["runtime_artifacts"][role]["path"]).read_bytes())
                        proof["runtime_artifacts"][role]["path"] = str(path)
                        self.seal(proof)
                    path.write_bytes(b"tampered after proof\n")
                    with self.assertRaisesRegex(ValueError, "artifact has drifted"):
                        runtime_proof.verify_recorded(actual, expected)

    def test_authentic_upgrades_cannot_hide_any_non_runtime_field_change(self):
        """Catches limiting comparison to selected fields or publication rows."""
        original = self.proof("original")
        current = self.proof("current", ("consumer", "pipeline", "allocator"))
        for side in ("original", "current"):
            for field in original.keys() - {"schema_version", "runtime_artifacts", "acceptance_sha256"}:
                with self.subTest(side=side, field=field):
                    actual, expected = copy.deepcopy(original), copy.deepcopy(current)
                    proof = actual if side == "original" else expected
                    proof[field] = {"semantic_change": True}
                    self.seal(proof)
                    with self.assertRaises(ValueError):
                        runtime_proof.verify_recorded(actual, expected)
        for side in ("original", "current"):
            with self.subTest(side=side, field="added-field"):
                actual, expected = copy.deepcopy(original), copy.deepcopy(current)
                proof = actual if side == "original" else expected
                proof["unverified_semantics"] = True
                self.seal(proof)
                with self.assertRaises(ValueError):
                    runtime_proof.verify_recorded(actual, expected)

    def test_bad_original_or_current_acceptance_hash_is_rejected(self):
        """Catches trusting either acceptance hash without independently checking it."""
        for side in ("original", "current"):
            with self.subTest(side=side):
                actual = self.proof("original")
                expected = self.proof("current", ("consumer",))
                proof = actual if side == "original" else expected
                proof["acceptance_sha256"] = "sha256:" + "0" * 64
                with self.assertRaises(ValueError):
                    runtime_proof.verify_recorded(actual, expected)

    def test_schema_and_role_inventory_are_required_on_both_sides(self):
        """Catches accepting unknown proof schemas or partial runtime inventories."""
        for side in ("original", "current"):
            for mode in ("schema", "missing-role", "extra-role", "not-mapping"):
                with self.subTest(side=side, mode=mode):
                    actual = self.proof("original")
                    expected = self.proof("current", ("consumer",))
                    proof = actual if side == "original" else expected
                    if mode == "schema":
                        proof["schema_version"] = "unknown/v1"
                    elif mode == "missing-role":
                        del proof["runtime_artifacts"]["allocator"]
                    elif mode == "extra-role":
                        proof["runtime_artifacts"]["extra"] = copy.deepcopy(proof["runtime_artifacts"]["consumer"])
                    else:
                        proof["runtime_artifacts"] = []
                    self.seal(proof)
                    with self.assertRaisesRegex(ValueError, "inventory differs"):
                        runtime_proof.verify_recorded(actual, expected)
        for actual, expected in ((None, self.proof("current")), (self.proof("original"), None)):
            with self.subTest(non_mapping=(actual is None)):
                with self.assertRaisesRegex(ValueError, "inventory differs"):
                    runtime_proof.verify_recorded(actual, expected)

    def test_missing_files_and_malformed_handles_are_rejected_on_both_sides(self):
        """Catches bypassing original-file existence or strict byte-bound handles."""
        for side in ("original", "current"):
            for mode in ("missing-file", "relative-path", "symlink", "not-mapping", "missing-sha", "extra-key", "non-string-path", "non-string-sha"):
                with self.subTest(side=side, mode=mode):
                    actual = self.proof("original")
                    expected = self.proof("current", ("consumer",))
                    proof = actual if side == "original" else expected
                    handle = proof["runtime_artifacts"]["consumer"]
                    if mode == "missing-file":
                        handle["path"] = str(self.root / "missing.py")
                    elif mode == "relative-path":
                        handle["path"] = "consumer.py"
                    elif mode == "symlink":
                        link = self.root / f"linked-{side}.py"
                        link.symlink_to(handle["path"])
                        handle["path"] = str(link)
                    elif mode == "not-mapping":
                        proof["runtime_artifacts"]["consumer"] = None
                    elif mode == "missing-sha":
                        del handle["sha256"]
                    elif mode == "extra-key":
                        handle["unverified"] = True
                    elif mode == "non-string-path":
                        handle["path"] = 1
                    else:
                        handle["sha256"] = 1
                    self.seal(proof)
                    with self.assertRaisesRegex(ValueError, "artifact has drifted"):
                        runtime_proof.verify_recorded(actual, expected)


class HistoricalWarningProjectionTests(unittest.TestCase):
    """Use real native source/selection fixtures; never mock verifier behavior."""

    def setUp(self):
        self.fixture = selection_fixtures.PendingSelectionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.add_clockify_context()
        self.fixture.binding["reason_projection"] = selection_fixtures.write(
            self.fixture.root / "readable-reasons.json",
            {rid: "Readable existing review reason" for rid in self.fixture.binding["selected_current_ids"]},
        )
        selection_fixtures.write(self.fixture.binding_path, self.fixture.binding)
        verified = pending.verify(
            bindings_path=self.fixture.binding_path, source_dir=self.fixture.current_dir,
            proposals=self.fixture.current, spreadsheet_id="sheet", sheet_title="August 2026 review",
            run_id=self.fixture.current_dir.name, project_allowlist={},
        )
        self.rows = verified["rows"]
        self.expected = copy.deepcopy(verified["receipt"])
        self.actual = copy.deepcopy(self.expected)
        for field in ("saved_credit_seconds", "fixed_recording_rows", "fixed_recording_checks"):
            del self.actual[field]
        for warnings in self.actual["native_review_warnings"].values():
            if warnings:
                warnings.append(copy.deepcopy(warnings[0]))
        for role, handle in self.actual["runtime_artifacts"].items():
            path = self.fixture.root / f"original-{role}.py"
            path.write_bytes(Path(handle["path"]).read_bytes() + b"\n# historical runtime version\n")
            self.actual["runtime_artifacts"][role] = {
                "path": str(path), "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
            }
        self.reseal_projection(self.actual)

    @staticmethod
    def digest(value):
        return "sha256:" + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def reseal_projection(self, proof):
        rows = copy.deepcopy(self.rows)
        for row in rows:
            if row[0] in proof["native_review_warnings"]:
                warnings = proof["native_review_warnings"][row[0]]
                row[12] = json.dumps(warnings, ensure_ascii=False, sort_keys=True) if warnings else ""
        proof["native_projection_rows_sha256"] = self.digest(rows)
        PendingRuntimeProofTests.seal(proof)

    def test_authentic_warning_repetitions_and_derived_legacy_summaries_preserve_proof(self):
        """Catches rejecting only repetition removal and independently derivable summaries."""
        self.assertEqual(34, self.actual["saved_credit_minutes"])
        self.assertEqual(2040, self.expected["saved_credit_seconds"])
        self.assertEqual(0, self.expected["fixed_recording_rows"])
        self.assertEqual([], self.expected["fixed_recording_checks"])
        before = copy.deepcopy((self.actual, self.expected))
        originals = [self.fixture.binding_path, Path(self.expected["reason_projection"]["path"]),
                     *(Path(h["path"]) for h in self.actual["runtime_artifacts"].values())]
        pinned = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in originals}
        try:
            result = runtime_proof.verify_recorded(self.actual, self.expected)
        except ValueError as exc:
            self.fail(f"authentic original warning projection was rejected: {exc}")
        self.assertEqual(before[0], result)
        self.assertEqual(before, (self.actual, self.expected))
        self.assertEqual(pinned, {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in originals})

    def test_changed_warning_facts_or_order_are_rejected_even_with_valid_hashes(self):
        """Catches treating warnings as unordered sets or ignoring warning facts."""
        rid = next(rid for rid, warnings in self.actual["native_review_warnings"].items() if len(warnings) >= 3)
        for side in ("original", "current"):
            for mode in ("fact", "order", "removed-fact", "identity"):
                with self.subTest(side=side, mode=mode):
                    actual, expected = copy.deepcopy(self.actual), copy.deepcopy(self.expected)
                    proof = actual if side == "original" else expected
                    warnings = proof["native_review_warnings"][rid]
                    if mode == "fact":
                        warnings[0]["unverified_fact"] = True
                    elif mode == "order":
                        warnings[0], warnings[1] = warnings[1], warnings[0]
                    elif mode == "removed-fact":
                        proof["native_review_warnings"][rid] = warnings[1:]
                    else:
                        proof["native_review_warnings"]["unrelated-id"] = proof["native_review_warnings"].pop(rid)
                    self.reseal_projection(proof)
                    with self.assertRaises(ValueError):
                        runtime_proof.verify_recorded(actual, expected)

    def test_original_and_current_native_projection_hashes_are_required(self):
        """Catches checking only the final readable reason projection's digest."""
        for side in ("original", "current"):
            with self.subTest(side=side):
                actual, expected = copy.deepcopy(self.actual), copy.deepcopy(self.expected)
                proof = actual if side == "original" else expected
                proof["native_projection_rows_sha256"] = "sha256:" + "0" * 64
                PendingRuntimeProofTests.seal(proof)
                with self.assertRaises(ValueError):
                    runtime_proof.verify_recorded(actual, expected)

    def test_warning_numeric_types_cannot_change_under_equal_python_values(self):
        """Catches 60.0 and 60 comparing equal while exact warning bytes differ."""
        actual = copy.deepcopy(self.actual)
        warning = next(warning for warnings in actual["native_review_warnings"].values()
                       for warning in warnings if "overlap_duration_seconds" in warning)
        self.assertEqual(60, warning["overlap_duration_seconds"])
        warning["overlap_duration_seconds"] = 60.0
        self.reseal_projection(actual)
        with self.assertRaises(ValueError):
            runtime_proof.verify_recorded(actual, self.expected)

    def test_credit_recording_project_time_and_final_projection_changes_are_rejected(self):
        """Catches masking credit, recording, or publication changes as warning cleanup."""
        for field, value in (("saved_credit_seconds", 2041), ("fixed_recording_rows", 1),
                             ("fixed_recording_checks", [{"recording_id": "invented"}]),
                             ("saved_credit_minutes", 35), ("saved_credit_rows", 35),
                             ("projected_rows_sha256", "sha256:" + "0" * 64),
                             ("selected_current_ids", ["unrelated-id"])):
            with self.subTest(field=field):
                expected = copy.deepcopy(self.expected)
                expected[field] = value
                PendingRuntimeProofTests.seal(expected)
                with self.assertRaises(ValueError):
                    runtime_proof.verify_recorded(self.actual, expected)
        rid = next(iter(self.actual["native_review_warnings"]))
        for field, value in (("clockify_project_suffix", "def456"), ("start", "2026-08-01T11:00:00+00:00")):
            with self.subTest(warning_field=field):
                actual = copy.deepcopy(self.actual)
                actual["native_review_warnings"][rid][0][field] = value
                self.reseal_projection(actual)
                with self.assertRaises(ValueError):
                    runtime_proof.verify_recorded(actual, self.expected)

    def test_reason_projection_and_original_source_bytes_remain_authenticated(self):
        """Catches reconstructing rows from metadata while skipping source byte binding."""
        paths = (Path(self.expected["reason_projection"]["path"]),
                 Path(self.expected["current_source_artifacts"]["proposals"]["path"]))
        for path in paths:
            with self.subTest(artifact=path.name):
                before = path.read_bytes()
                try:
                    path.write_bytes(before + b"\n ")
                    with self.assertRaises(ValueError):
                        runtime_proof.verify_recorded(self.actual, self.expected)
                finally:
                    path.write_bytes(before)


if __name__ == "__main__":
    unittest.main()
