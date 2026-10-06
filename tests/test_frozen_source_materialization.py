"""A frozen captured source must be proved before becoming a review run."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import (
    clockify_review_run as review, clockify_sync_collect as collector,
    collector_receipts, evidence_ledger, reconciliation_manifest, semantic_analyzer,
)
from scripts.collector_checkpoints import PageCheckpointStore


SINCE = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
UNTIL = SINCE + dt.timedelta(days=1)


def frozen_source(root: Path) -> Path:
    """Build a real local collector snapshot, without network or external state."""
    store = PageCheckpointStore(root / "private-test-cache")
    identity = collector._clockify_checkpoint_identity(
        "workspace-one", "user-one", SINCE, UNTIL,
    )
    state = store.open(identity, initial_metadata={"snapshot_at": "2026-09-03T00:00:00Z"})
    state = store.append_page(
        state, payload=[], continuation={"page": 2},
        signature=collector._clockify_page_signature([]),
    )
    store.mark_complete(state)
    source = root / "frozen" / "source"
    source.parent.mkdir()
    with mock.patch.object(collector, "collector_runtime_identity", return_value={"git_sha": "fixture"}), \
         mock.patch.object(collector, "clockify_get", side_effect=AssertionError("network forbidden")), \
         mock.patch.object(collector, "fetch_fathom", return_value={"status": "ok", "complete": True, "meetings": []}), \
         mock.patch.object(collector, "fetch_multica_issues", return_value={"status": "ok", "complete": True, "issues": []}), \
         mock.patch.object(collector, "build_proposals", return_value=([], [], [])):
        collector._collect_slice(
            argparse.Namespace(calendly_optional=True, enrich=False),
            {"clockify_user_id": "user-one"}, {"machines": []},
            {"CLOCKIFY_WORKSPACE_ID": "workspace-one"}, {},
            SINCE, UNTIL, "frozen fixture", store, source,
            coordinator="omarchy-precision",
        )
    report_path = source / "run-report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["source_augmentation"] = {
        "schema_version": "offline-captured-union/v1",
        "original_collection_not_reperformed": True,
        "stage_only": True,
    }
    report_path.write_text(json.dumps(report) + "\n", encoding="utf-8")
    return source


def add_review_inputs(source: Path) -> None:
    period = reconciliation_manifest.PeriodIdentity(
        "user-one", "workspace-one", "Europe/Bucharest", SINCE, UNTIL, 1,
    )
    unsigned = {
        "schema_version": reconciliation_manifest.MANIFEST_COMPATIBILITY_VERSION,
        "compatibility_version": reconciliation_manifest.MANIFEST_COMPATIBILITY_VERSION,
        "period": period.document(), "state": "collecting", "event_count": 1,
        "events_digest": "sha256:" + "0" * 64, "artifacts": [], "blockers": [],
    }
    manifest = {**unsigned, "manifest_digest": reconciliation_manifest._digest(unsigned)}
    (source / "period-manifest.json").write_text(json.dumps(manifest) + "\n")
    (source / "routing.json").write_text(json.dumps({"session_routes": [], "meeting_routes": []}) + "\n")
    (source / "review-corrections.jsonl").write_text("")
    (source / "review-acceptance.jsonl").write_text("")
    (source / "analyzer-cache-used.jsonl").write_text("")
    analysis = {
        "schema_version": 1, "activities": [], "exceptions": [], "omissions": [],
        "ledger_event_count": 0,
        "ledger_evidence_digest": semantic_analyzer.stable_digest("led-", []),
        "analyzer_cache": {
            "records": [], "snapshot": {
                "path": "analyzer-cache-used.jsonl", "record_count": 0,
                "sha256": hashlib.sha256(b"").hexdigest(),
            },
        },
    }
    (source / "semantic-analysis.json").write_text(json.dumps(analysis) + "\n")
    (source / "captured-source-provenance.json").write_text('{"capture":"fixture"}\n')
    (source / "augmentation-acceptance.json").write_text('{"stage_only":true}\n')


class FrozenSourceVerifierTests(unittest.TestCase):
    def test_historical_projection_removes_only_valid_omission_metadata(self) -> None:
        source_ref = {"source_type": "codex_sessions", "source_id": "s:event:1",
                      "machine": "macbook", "session_id": "s", "ordinal": 1}
        attributes = {
            "role": "tool", "kind": "tool_result", "content": "",
            "transport_omitted_tool_content": {
                "sha256": "a" * 64, "byte_count": 10,
                "reason": "native_semantic_projection_omits_tool_content",
            },
        }
        current = evidence_ledger.evidence_event(
            "codex_sessions_event", source_ref, attributes=attributes,
        )
        historical = evidence_ledger.evidence_event(
            "codex_sessions_event", source_ref,
            attributes={key: value for key, value in attributes.items()
                        if key != "transport_omitted_tool_content"},
        )
        projected = collector_receipts._historical_transport_projection(current)
        self.assertEqual(historical.document(), projected.document())
        self.assertNotEqual(current.evidence_id, projected.evidence_id)

    def test_historical_projection_rejects_non_tool_metadata(self) -> None:
        current = evidence_ledger.evidence_event(
            "codex_sessions_event", {"source_id": "s:event:1"},
            attributes={"role": "assistant", "kind": "message", "content": "",
                        "transport_omitted_tool_content": {
                            "sha256": "a" * 64, "byte_count": 10,
                            "reason": "native_semantic_projection_omits_tool_content",
                        }},
        )
        with self.assertRaises(ValueError):
            collector_receipts._historical_transport_projection(current)

    def test_historical_attestation_rejects_unknown_normalizer(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run = root / "stock" / "runs" / "source"
            run.mkdir(parents=True)
            stage = root / "stage" / "runs" / "source"
            stage.mkdir(parents=True)
            receipt = {
                "schema_version": "offline-september-allhost-stage/v1",
                "stage_only": True, "network": False, "inference": False,
                "source_host_rereads": False,
                "runs_root": str(stage.parent),
                "materializer_sha256": "8bbc609fe66b925ee3b223186247c9821b266d90c9b7213de5f2d321c2d1854f",
                "native_module_hashes": {"/attested/evidence_ledger.py": "0" * 64},
                "results": [{"source_run": str(stage), "label": "source",
                             "ledger_identity": {"file_sha256": "0" * 64,
                                                 "manifest_id": "elm-source",
                                                 "events_digest": "0" * 64}}],
            }
            raw = (json.dumps(receipt) + "\n").encode()
            (stage.parent.parent / "allhost-stage-result.json").write_bytes(raw)
            report = {"paths": {"run_dir": str(stage)}, "source_augmentation": {
                "schema_version": "offline-captured-mac-conservative-additive-union/v1",
                "materializer_sha256": receipt["materializer_sha256"],
            }}
            with mock.patch.object(collector_receipts, "_HISTORICAL_STAGE_SHA256", hashlib.sha256(raw).hexdigest()):
                with self.assertRaises(ValueError):
                    collector_receipts._attested_historical_source(
                        run, report, "0" * 64, "elm-source", "0" * 64, {},
                    )

    def test_verifies_raw_ledger_and_existing_native_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = frozen_source(Path(directory))
            before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}

            verified = collector_receipts.verify_frozen_source(source)

            self.assertEqual(before[Path("evidence/evidence-ledger.json")],
                             verified.verified_artifact_bytes["evidence/evidence-ledger.json"])
            self.assertIn("evidence/clockify-native-checkpoint/snapshot.json",
                          verified.verified_artifact_bytes)
            self.assertEqual(before, {p.relative_to(source): p.read_bytes()
                                      for p in source.rglob("*") if p.is_file()})

    def test_rejects_raw_ledger_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = frozen_source(Path(directory))
            (source / "evidence/sessions.json").write_text('[{"machine":"extra"}]\n')
            with self.assertRaises(collector_receipts.CollectorReceiptError):
                collector_receipts.verify_frozen_source(source)

    def test_rejects_symlinked_native_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = frozen_source(Path(directory))
            artifact = source / "evidence/clockify-native-checkpoint/snapshot.json"
            outside = Path(directory) / "outside.json"
            outside.write_bytes(artifact.read_bytes())
            artifact.unlink()
            artifact.symlink_to(outside)
            with self.assertRaises(collector_receipts.CollectorReceiptError):
                collector_receipts.verify_frozen_source(source)

    def test_rejects_period_that_disagrees_with_native_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = frozen_source(Path(directory))
            report_path = source / "run-report.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["date_range"]["until"] = "2026-09-03T00:00:00Z"
            report_path.write_text(json.dumps(report) + "\n")
            with self.assertRaises(collector_receipts.CollectorReceiptError):
                collector_receipts.verify_frozen_source(source)

    def test_preserves_optional_enrichment_that_added_no_ledger_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = frozen_source(Path(directory))
            (source / "evidence/enriched-context.json").write_text(
                '{"claude_contexts":[],"hermes_contexts":[]}\n', encoding="utf-8",
            )
            verified = collector_receipts.verify_frozen_source(source)
            self.assertEqual(
                (source / "evidence/enriched-context.json").read_bytes(),
                verified.verified_artifact_bytes["evidence/enriched-context.json"],
            )


class FrozenMaterializationTests(unittest.TestCase):
    @staticmethod
    def downstream(target: Path, *, quality: str = "pass") -> None:
        (target / "semantic-analysis.json").write_bytes(
            (target / "frozen-fixture/semantic-analysis.json").read_bytes(),
        )
        (target / "work-accounting-result.json").write_text('{}\n')
        (target / "quality_report.json").write_text(json.dumps({"status": quality}) + "\n")
        (target / "review-snapshot.json").write_text('{}\n')

    def test_prepares_distinct_source_bound_run_with_exact_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = frozen_source(root)
            add_review_inputs(source)
            before = {p.relative_to(source): p.read_bytes() for p in source.rglob("*") if p.is_file()}
            runs = root / "runs"
            runs.mkdir()

            with mock.patch.object(review, "RUNS", runs):
                materialized = review._prepare_frozen_source_run(source)

            self.assertNotEqual(source, materialized)
            self.assertEqual(before[Path("evidence/evidence-ledger.json")],
                             (materialized / "evidence/evidence-ledger.json").read_bytes())
            for name in review._RECONCILIATION_INPUTS.values():
                self.assertEqual(before[Path(name)], (materialized / name).read_bytes())
            self.assertEqual(before, {p.relative_to(source): p.read_bytes()
                                      for p in source.rglob("*") if p.is_file()})
            self.assertFalse((materialized / "completion-bundle.json").exists())

    def test_accepts_source_slice_inside_larger_reconciliation_period(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = frozen_source(root)
            add_review_inputs(source)
            manifest_path = source / "period-manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["period"]["since_utc"] = "2026-08-31T00:00:00Z"
            unsigned = {key: value for key, value in manifest.items() if key != "manifest_digest"}
            manifest["manifest_digest"] = reconciliation_manifest._digest(unsigned)
            manifest_path.write_text(json.dumps(manifest) + "\n")
            runs = root / "runs"
            runs.mkdir()
            with mock.patch.object(review, "RUNS", runs):
                materialized = review._prepare_frozen_source_run(source)
            self.assertTrue((materialized / "period-manifest.json").is_file())

    def test_reuses_identical_prepared_run_without_rewriting_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = frozen_source(root)
            add_review_inputs(source)
            runs = root / "runs"
            runs.mkdir()
            with mock.patch.object(review, "RUNS", runs):
                first = review._prepare_frozen_source_run(source)
                before = {p.relative_to(first): (p.stat().st_mtime_ns, p.read_bytes())
                          for p in first.rglob("*") if p.is_file()}
                second = review._prepare_frozen_source_run(source)
                self.assertEqual(first, second)
            self.assertEqual(before, {p.relative_to(second): (p.stat().st_mtime_ns, p.read_bytes())
                                      for p in second.rglob("*") if p.is_file()})

    def test_rejects_symlinked_materialization_directory_on_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = frozen_source(root)
            add_review_inputs(source)
            runs = root / "runs"
            runs.mkdir()
            with mock.patch.object(review, "RUNS", runs):
                target = review._prepare_frozen_source_run(source)
                (target / "evidence").rename(target / "evidence-saved")
                (target / "evidence").symlink_to(source / "evidence", target_is_directory=True)
                with self.assertRaises(ValueError):
                    review._prepare_frozen_source_run(source)
                with self.assertRaises(ValueError):
                    review._verified_frozen_source_run(target)

    def test_seals_only_after_bound_source_and_passing_downstream_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = frozen_source(root)
            add_review_inputs(source)
            runs = root / "runs"
            runs.mkdir()
            with mock.patch.object(review, "RUNS", runs):
                target = review._prepare_frozen_source_run(source)
                self.downstream(target, quality="blocked")
                with self.assertRaises(collector_receipts.CollectorReceiptError):
                    review._finalize_frozen_source_completion(target)
                self.assertFalse((target / "completion-bundle.json").exists())
                self.downstream(target)
                bundle = review._finalize_frozen_source_completion(target)
            self.assertEqual(bundle.bundle_digest,
                             collector_receipts.load_completion_bundle(
                                 target / "completion-bundle.json", run_dir=target,
                             ).bundle_digest)

    def test_refuses_completion_if_original_frozen_input_drifted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = frozen_source(root)
            add_review_inputs(source)
            runs = root / "runs"
            runs.mkdir()
            with mock.patch.object(review, "RUNS", runs):
                target = review._prepare_frozen_source_run(source)
                self.downstream(target)
                (source / "routing.json").write_text('{"session_routes":[{"pattern":"changed"}]}\n')
                with self.assertRaises(ValueError):
                    review._finalize_frozen_source_completion(target)
            self.assertFalse((target / "completion-bundle.json").exists())

    def test_cli_materializes_with_original_four_snapshots_and_sealed_fixture(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = frozen_source(root)
            add_review_inputs(source)
            runs = root / "runs"
            runs.mkdir()
            with mock.patch.object(review, "_process_run", return_value=(0, runs / "result.json")) as processed:
                code = review.main([
                    "--runs-root", str(runs), "--state", str(root / "private-state.json"),
                    "--materialize-frozen-from", str(source),
                ])
            self.assertEqual(0, code)
            run_args, target, _gate = processed.call_args.args
            self.assertEqual(source.name, json.loads((target / "run-report.json").read_text())["frozen_source"]["source_run_id"])
            self.assertEqual(target / "frozen-fixture/semantic-analysis.json", run_args._frozen_analysis_fixture)
            self.assertEqual(target / "routing.json", run_args.routing)
            self.assertEqual(target / "period-manifest.json", run_args.period_manifest)
            self.assertEqual(target / "review-corrections.jsonl", run_args.corrections)
            self.assertEqual(target / "review-acceptance.jsonl", run_args.acceptance_ledger)

    def test_cli_forbids_frozen_source_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = frozen_source(root)
            add_review_inputs(source)
            runs = root / "runs"
            runs.mkdir()
            for override in ("--routing", "--analysis-fixture", "--since"):
                with self.subTest(override=override):
                    self.assertEqual(2, review.main([
                        "--runs-root", str(runs), "--state", str(root / "private-state.json"),
                        "--materialize-frozen-from", str(source),
                        override, "value",
                    ]))
            self.assertFalse(any(runs.iterdir()))


if __name__ == "__main__":
    unittest.main()
