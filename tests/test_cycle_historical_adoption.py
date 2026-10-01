"""Synthetic, offline proof for explicit adoption of historical review delivery."""
from __future__ import annotations

import json
import datetime as dt
import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import clockify_review_cycle as cycle
from scripts import clockify_review_run, collector_receipts, semantic_analyzer, source_coverage
from test_review_cycle_delivery import make_run, write_json


class HistoricalAdoptionTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.runs = self.root / "runs"
        self.state_dir = self.root / "state"
        (self.root / "cache").mkdir()
        for filename, content in (
            ("routing.json", {"workspace_id": "workspace-1", "member_id": "member-1"}),
            ("corrections.jsonl", {}),
            ("acceptance.jsonl", {}),
        ):
            write_json(self.root / filename, content)
        self.config = {
            "root": str(self.root), "runs_dir": str(self.runs),
            "state_dir": str(self.state_dir), "cache": str(self.root / "cache"),
            "routing": str(self.root / "routing.json"),
            "corrections": str(self.root / "corrections.jsonl"),
            "acceptance": str(self.root / "acceptance.jsonl"),
            "workspace_id": "workspace-1", "member_id": "member-1",
            "recovery_since": "2026-09-07", "timezone": "Europe/Bucharest",
            "spreadsheet_id": "sheet-1",
            "monthly_sheet_title_template": "{month_name} {year} portfolio review",
            "calendly_optional": True,
        }
        runs_patch = mock.patch.object(clockify_review_run, "RUNS", self.runs)
        runs_patch.start()
        self.addCleanup(runs_patch.stop)
        self.since = "2026-09-07"
        self.until = "2026-09-09"
        manifest = cycle._ensure_period(
            self.config, self.state_dir, self.since, self.until, bind_inputs=True
        )
        frozen = cycle._expected_snapshot_digests(self.config, manifest)
        self.state_dir.mkdir(exist_ok=True)
        write_json(self.state_dir / "review-cycle-state.json", {
            "schema_version": cycle.SCHEMA_VERSION,
            "completed_through": None,
            "scheduled_through": self.until,
            "next_work_class": "routine",
            "slices": {self.since: {
                "until": self.until, "status": "incomplete",
                "period_manifest": str(manifest),
                "expected_snapshot_digests": frozen,
            }},
        })
        new_routing = {
            "workspace_id": "workspace-1", "member_id": "member-1",
            "session_routes": [], "meeting_routes": [], "evidence_routes": [],
        }
        self.source_result = make_run(
            self.root, "historical-source", replay=False,
            snapshot_overrides={"routing.json": new_routing},
        )
        self.replay_result = make_run(
            self.root, "historical-replay", replay=True,
            source_name="historical-source", snapshots_from=self.source_result.parent,
        )
        self.external_checkpoint = self.root / "external-checkpoints"
        (self.state_dir / "collector-checkpoints").rename(self.external_checkpoint)
        self.publication_result = self.source_result.parent / "sheet-publish-result.json"
        publications = cycle._expected_publication_receipts(
            self.config,
            {"run_dir": str(self.source_result.parent), "run_id": self.source_result.parent.name},
            sheet_title="September 2026 portfolio review",
        )
        write_json(self.publication_result, {
            "schema_version": "sheet-publication-result/v1", "status": "published",
            "external_writes": True, "clockify_writes": 0,
            "publications": publications,
        })
        bundle = json.loads((self.source_result.parent / "completion-bundle.json").read_text())
        interval = source_coverage.SourceInterval(
            source="runner/unclassified", since_utc=bundle["since_utc"],
            until_utc=bundle["until_utc"], slice_id=bundle["slice_id"],
            compatibility_version="runner-unclassified/v1",
        )
        store = source_coverage.SourceDebtStore()
        debt = store.record_failure(
            interval, failure_class="child_nonzero", retryable=True,
            resume_state_digest="sha256:" + "a" * 64,
            attempted_at="2026-09-10T00:00:00Z",
        )
        store.exhaust(debt.debt_id, terminal_reason="retry_limit")
        source_coverage.write(self.state_dir / "source-coverage.json", store.document())
        adopted = {
            filename: cycle._digest(self.source_result.parent / filename)
            for filename in frozen
        }
        checkpoint_manifest = next(self.external_checkpoint.glob("*/backlog-manifest.json"))
        self.request = {
            "schema_version": "clockify-historical-adoption-request/v1",
            "since": self.since, "until": self.until,
            "source_result": str(self.source_result),
            "replay_result": str(self.replay_result),
            "publication_result": str(self.publication_result),
            "checkpoint_root": str(self.external_checkpoint),
            "checkpoint_manifest_digest": cycle._digest(checkpoint_manifest),
            "frozen_snapshot_digests": frozen,
            "adopted_snapshot_digests": adopted,
            "runtime_identity_digest": bundle["runtime_identity_digest"],
            "source_result_digest": cycle._digest(self.source_result),
            "replay_result_digest": cycle._digest(self.replay_result),
            "publication_result_digest": cycle._digest(self.publication_result),
        }

    def test_adopts_exact_historical_delivery_once_without_children_or_republication(self):
        """Catches accepting external publication without durable cycle lineage."""
        with mock.patch.object(cycle, "run_child_bounded", side_effect=AssertionError("child")):
            first = cycle.adopt_historical_slice(self.config, self.request)
            second = cycle.adopt_historical_slice(self.config, self.request)
        self.assertEqual("delivered", first["status"])
        self.assertEqual(first, second)
        state = json.loads((self.state_dir / "review-cycle-state.json").read_text())
        self.assertEqual(self.until, state["completed_through"])
        self.assertEqual(self.request["frozen_snapshot_digests"],
                         state["slices"][self.since]["expected_snapshot_digests"])
        self.assertTrue(Path(state["slices"][self.since]["delivery_receipt"]).is_file())
        self.assertTrue(Path(state["slices"][self.since]["historical_adoption_receipt"]).is_file())
        debt = source_coverage.SourceDebtStore.from_document(
            source_coverage.read(self.state_dir / "source-coverage.json")
        )
        self.assertEqual([], [item for item in debt.active() if item.interval.source == "runner/unclassified"])
        self.assertEqual(3, len(debt.document()["events"]))

    def test_sealed_adoption_does_not_depend_on_ephemeral_checkpoint_root(self):
        """Catches a completed import breaking when its old checkpoint root disappears."""
        cycle.adopt_historical_slice(self.config, self.request)
        self.external_checkpoint.rename(self.root / "detached-checkpoints")
        with mock.patch.object(cycle, "run_child_bounded", side_effect=AssertionError("child")):
            result = cycle.adopt_historical_slice(self.config, self.request)
        self.assertEqual("delivered", result["status"])

    def test_different_publication_result_cannot_be_adopted(self):
        """Catches selecting the same-span publisher with different published rows."""
        different = self.replay_result.parent / "sheet-publish-result.json"
        document = json.loads(self.publication_result.read_text())
        document["publications"][0]["rows_sha256"] = "sha256:" + "b" * 64
        write_json(different, document)
        request = {
            **self.request,
            "publication_result": str(different),
            "publication_result_digest": cycle._digest(different),
        }
        with self.assertRaisesRegex(cycle.CycleError, "readbacks differ"):
            cycle.adopt_historical_slice(self.config, request)
        self.assertFalse((self.state_dir / "delivery-receipts" / f"{self.since}.json").exists())

    def test_wrong_frozen_input_transition_cannot_be_adopted(self):
        """Catches replacing the cycle's original routing snapshot in the import claim."""
        request = dict(self.request)
        request["frozen_snapshot_digests"] = {
            **self.request["frozen_snapshot_digests"],
            "routing.json": "sha256:" + "c" * 64,
        }
        with self.assertRaisesRegex(cycle.CycleError, "frozen input proof"):
            cycle.adopt_historical_slice(self.config, request)

    def test_unsealed_replay_cannot_be_adopted(self):
        """Catches PASS replay integrity being mistaken for a completion bundle."""
        (self.replay_result.parent / "completion-bundle.json").rename(
            self.replay_result.parent / "unsealed-bundle.json"
        )
        with self.assertRaisesRegex(cycle.CycleError, "completion bundle"):
            cycle.adopt_historical_slice(self.config, self.request)

    def test_wrong_checkpoint_binding_cannot_be_adopted(self):
        """Catches a source bundle paired with another checkpoint manifest."""
        request = {
            **self.request,
            "checkpoint_manifest_digest": "sha256:" + "d" * 64,
        }
        with self.assertRaisesRegex(cycle.CycleError, "checkpoint manifest identity"):
            cycle.adopt_historical_slice(self.config, request)

    def test_wrong_historical_runtime_cannot_be_adopted(self):
        """Catches a run from a different runtime being silently relabeled."""
        request = {
            **self.request,
            "runtime_identity_digest": "sha256:" + "e" * 64,
        }
        with self.assertRaisesRegex(cycle.CycleError, "runtime identity"):
            cycle.adopt_historical_slice(self.config, request)

    def test_wrong_spreadsheet_target_cannot_be_adopted(self):
        """Catches a valid-looking readback bound to another spreadsheet."""
        other_target = self.replay_result.parent / "sheet-publish-result.json"
        document = json.loads(self.publication_result.read_text())
        document["publications"][0]["spreadsheet_id"] = "another-sheet"
        write_json(other_target, document)
        request = {
            **self.request,
            "publication_result": str(other_target),
            "publication_result_digest": cycle._digest(other_target),
        }
        with self.assertRaisesRegex(cycle.CycleError, "readbacks differ"):
            cycle.adopt_historical_slice(self.config, request)

    def test_later_slice_does_not_skip_missing_earlier_frontier(self):
        """Catches a published later slice advancing completion over an unknown day."""
        config = {**self.config, "recovery_since": "2026-09-05"}
        result = cycle.adopt_historical_slice(config, self.request)
        state = json.loads((self.state_dir / "review-cycle-state.json").read_text())
        self.assertEqual("delivered", result["status"])
        self.assertIsNone(state["completed_through"])

    def test_invalid_debt_ledger_leaves_no_delivery_receipt(self):
        """Catches sealing delivery before the existing coverage ledger is validated."""
        write_json(self.state_dir / "source-coverage.json", {"schema_version": 999})
        with self.assertRaisesRegex(cycle.CycleError, "source coverage"):
            cycle.adopt_historical_slice(self.config, self.request)
        self.assertFalse((self.state_dir / "delivery-receipts" / f"{self.since}.json").exists())
        self.assertFalse((self.state_dir / "historical-adoption-receipts" / f"{self.since}.json").exists())

    def _repair_stage(self, source: Path) -> dict[str, object]:
        repair = clockify_review_run._prepare_repair_run(source)
        for filename in (
            "semantic-analysis.json", "work-accounting-result.json", "quality_report.json",
            "review-snapshot.json", "proposals.json", "fathom-reconciliation.json",
        ):
            shutil.copyfile(source / filename, repair / filename)
        bundle = clockify_review_run._finalize_repair_completion(repair)
        return {"run_dir": str(repair), "bundle_digest": bundle.bundle_digest}

    def test_repair_interval_uses_digest_bound_collector_checkpoint(self):
        """Catches treating a repair as a new collector with its own finalization."""
        stage = self._repair_stage(self.source_result.parent)
        self.assertFalse((Path(str(stage["run_dir"])) / "slice-finalization.json").exists())
        interval = cycle._interval_from_stage(
            self.config, "runner/unclassified", stage,
            checkpoint_root=self.external_checkpoint,
            checkpoint_manifest_digest=self.request["checkpoint_manifest_digest"],
        )
        self.assertEqual("runner/unclassified", interval.source)
        self.assertEqual("2026-09-06T21:00:00Z", interval.since_utc)

    def test_repair_interval_rejects_changed_parent_digest(self):
        """Catches redirecting a repair to a parent bundle other than its sealed source."""
        stage = self._repair_stage(self.source_result.parent)
        lineage_path = Path(str(stage["run_dir"])) / "repair-source.json"
        lineage = json.loads(lineage_path.read_text())
        lineage["source_completion_sha256"] = "sha256:" + "f" * 64
        write_json(lineage_path, lineage)
        with self.assertRaisesRegex(cycle.CycleError, "repair source completion"):
            cycle._interval_from_stage(self.config, "runner/unclassified", stage)

    def test_repair_interval_rejects_parent_outside_runs(self):
        """Catches a repair source ID escaping the configured runs directory."""
        stage = self._repair_stage(self.source_result.parent)
        lineage_path = Path(str(stage["run_dir"])) / "repair-source.json"
        lineage = json.loads(lineage_path.read_text())
        lineage["source_run_id"] = "../historical-source"
        write_json(lineage_path, lineage)
        with self.assertRaisesRegex(cycle.CycleError, "repair source run ID"):
            cycle._interval_from_stage(self.config, "runner/unclassified", stage)

    def test_repair_interval_rejects_cycle(self):
        """Catches a self-referential repair lineage before checkpoint validation."""
        stage = self._repair_stage(self.source_result.parent)
        lineage_path = Path(str(stage["run_dir"])) / "repair-source.json"
        lineage = json.loads(lineage_path.read_text())
        lineage["source_run_id"] = Path(str(stage["run_dir"])).name
        write_json(lineage_path, lineage)
        with self.assertRaisesRegex(cycle.CycleError, "repair ancestry cycle"):
            cycle._interval_from_stage(self.config, "runner/unclassified", stage)

    def test_chained_repair_reaches_original_collector_checkpoint(self):
        """Catches stopping at the first repaired parent instead of the collector."""
        first = self._repair_stage(self.source_result.parent)
        second = self._repair_stage(Path(str(first["run_dir"])))
        interval = cycle._interval_from_stage(
            self.config, "runner/unclassified", second,
            checkpoint_root=self.external_checkpoint,
            checkpoint_manifest_digest=self.request["checkpoint_manifest_digest"],
        )
        self.assertEqual("2026-09-06T21:00:00Z", interval.since_utc)

    def test_repair_interval_rejects_missing_parent(self):
        """Catches treating an orphan repair as an original collector run."""
        stage = self._repair_stage(self.source_result.parent)
        lineage_path = Path(str(stage["run_dir"])) / "repair-source.json"
        lineage = json.loads(lineage_path.read_text())
        lineage["source_run_id"] = "missing-run"
        write_json(lineage_path, lineage)
        with self.assertRaisesRegex(cycle.CycleError, "repair source run is missing"):
            cycle._interval_from_stage(self.config, "runner/unclassified", stage)

    def test_repair_interval_rejects_unrelated_parent_runtime(self):
        """Catches a digest-bound but unrelated runtime being accepted as ancestry."""
        stage = self._repair_stage(self.source_result.parent)
        other_result = make_run(
            self.root, "other-runtime", replay=False,
            runtime_identity={"git_sha": "unrelated"},
            snapshot_overrides={"routing.json": json.loads(
                (self.source_result.parent / "routing.json").read_text()
            )},
        )
        lineage_path = Path(str(stage["run_dir"])) / "repair-source.json"
        lineage = json.loads(lineage_path.read_text())
        lineage["source_run_id"] = other_result.parent.name
        lineage["source_completion_sha256"] = cycle._digest(
            other_result.parent / "completion-bundle.json"
        )
        write_json(lineage_path, lineage)
        with self.assertRaisesRegex(cycle.CycleError, "repair ancestry slice or runtime"):
            cycle._interval_from_stage(self.config, "runner/unclassified", stage)

    def test_repair_interval_rejects_changed_routing_provenance(self):
        """Catches a rewritten repair routing digest despite a sealed bundle."""
        stage = self._repair_stage(self.source_result.parent)
        lineage_path = Path(str(stage["run_dir"])) / "repair-source.json"
        lineage = json.loads(lineage_path.read_text())
        lineage["source_routing_sha256"] = "sha256:" + "e" * 64
        write_json(lineage_path, lineage)
        with self.assertRaisesRegex(cycle.CycleError, "repair routing provenance"):
            cycle._interval_from_stage(self.config, "runner/unclassified", stage)

    def test_repair_interval_accepts_digest_bound_analyzer_cache(self):
        """Catches misreading the repair cache's raw SHA-256 as a prefixed digest."""
        stage = self._repair_stage(self.source_result.parent)
        repair = Path(str(stage["run_dir"]))
        content = b"fixture analyzer cache\n"
        for run in (self.source_result.parent, repair):
            (run / "analyzer-cache-used.jsonl").write_bytes(content)
        lineage_path = repair / "repair-source.json"
        lineage = json.loads(lineage_path.read_text())
        lineage.update(
            analyzer_cache_path="analyzer-cache-used.jsonl",
            analyzer_cache_sha256=hashlib.sha256(content).hexdigest(),
        )
        write_json(lineage_path, lineage)
        interval = cycle._interval_from_stage(
            self.config, "runner/unclassified", stage,
            checkpoint_root=self.external_checkpoint,
            checkpoint_manifest_digest=self.request["checkpoint_manifest_digest"],
        )
        self.assertEqual("2026-09-06T21:00:00Z", interval.since_utc)

    @staticmethod
    def _cache_record(body_digest: str) -> dict[str, object]:
        route = {
            "name": "clockify_analyzer_primary", "url": "https://offline.invalid/v1",
            "model": "fixture-model", "revision": "fixture-revision",
        }
        route_digest = hashlib.sha256(
            semantic_analyzer.canonical_json(route).encode()
        ).hexdigest()
        decision = {"status": "rejected", "failure_code": "contract_rejected"}
        return {
            "schema_version": semantic_analyzer.ANALYZER_CACHE_SCHEMA_VERSION,
            "cache_key": semantic_analyzer.stable_digest("arc-", {
                "schema_version": semantic_analyzer.ANALYZER_CACHE_SCHEMA_VERSION,
                "prompt_version": semantic_analyzer.PROMPT_VERSION,
                "semantic_schema_version": semantic_analyzer.SCHEMA_VERSION,
                "route_digest": route_digest,
                "body_digest": body_digest,
            }, length=64),
            "body_digest": body_digest, "route_digest": route_digest,
            "model": route["model"], "prompt_version": semantic_analyzer.PROMPT_VERSION,
            "semantic_schema_version": semantic_analyzer.SCHEMA_VERSION,
            "status": "rejected", "failure_code": "contract_rejected",
            "decision_digest": hashlib.sha256(
                semantic_analyzer.canonical_json(decision).encode()
            ).hexdigest(),
            "route": route,
        }

    def _scoped_retry_stage(self, *, legacy_pruned: bool = False) -> dict[str, object]:
        stage = self._repair_stage(self.source_result.parent)
        repair = Path(str(stage["run_dir"]))
        parent = self.source_result.parent
        original = collector_receipts.load_completion_bundle(
            repair / "completion-bundle.json", run_dir=repair,
        )
        prior = self._cache_record("a" * 64)
        added = self._cache_record("b" * 64)
        pruned = self._cache_record("c" * 64)
        parent_rows = (prior, pruned) if legacy_pruned else (prior,)
        parent_content = b"".join(
            json.dumps(row, sort_keys=True).encode() + b"\n"
            for row in sorted(parent_rows, key=lambda row: row["cache_key"])
        )
        child_content = b"".join(
            json.dumps(row, sort_keys=True).encode() + b"\n"
            for row in sorted((prior, added), key=lambda row: row["cache_key"])
        )
        (parent / "analyzer-cache-used.jsonl").write_bytes(parent_content)
        (repair / "analyzer-cache-used.jsonl").write_bytes(child_content)
        lineage_path = repair / "repair-source.json"
        lineage = json.loads(lineage_path.read_text())
        lineage.update(
            analyzer_cache_path="analyzer-cache-used.jsonl",
            analyzer_cache_sha256=hashlib.sha256(parent_content).hexdigest(),
        )
        write_json(lineage_path, lineage)
        analysis_path = repair / "semantic-analysis.json"
        analysis = json.loads(analysis_path.read_text())
        analysis["analyzer_cache"]["snapshot"] = {
            "path": "analyzer-cache-used.jsonl", "record_count": 2,
            "sha256": hashlib.sha256(child_content).hexdigest(),
        }
        retry = {
            "source_semantic_sha256": hashlib.sha256(
                (parent / "semantic-analysis.json").read_bytes()
            ).hexdigest(),
            "source_cache_sha256": hashlib.sha256(parent_content).hexdigest(),
            "target_digest": "frt-" + "a" * 64,
            "failure_code": "contract_rejected",
        }
        if not legacy_pruned:
            retry["mode"] = "scoped_review_v2"
        analysis["failed_review_retry"] = retry
        write_json(analysis_path, analysis)
        slice_ = type("Slice", (), {
            "slice_id": original.slice_id,
            "since": dt.datetime.fromisoformat(original.since_utc.replace("Z", "+00:00")),
            "until": dt.datetime.fromisoformat(original.until_utc.replace("Z", "+00:00")),
        })()
        rebuilt = collector_receipts.build_completion_bundle(repair, slice_=slice_)
        collector_receipts.write_completion_bundle(repair / "completion-bundle.json", rebuilt)
        stage["bundle_digest"] = rebuilt.bundle_digest
        return stage

    def test_repair_interval_accepts_scoped_retry_cache_extension(self):
        """Catches rejecting a sealed retry that preserves parent cache decisions."""
        stage = self._scoped_retry_stage()
        interval = cycle._interval_from_stage(
            self.config, "runner/unclassified", stage,
            checkpoint_root=self.external_checkpoint,
            checkpoint_manifest_digest=self.request["checkpoint_manifest_digest"],
        )
        self.assertEqual("2026-09-06T21:00:00Z", interval.since_utc)

    def test_repair_interval_accepts_legacy_retry_selected_cache(self):
        """Catches requiring every old used record in a legacy retry's new selection."""
        stage = self._scoped_retry_stage(legacy_pruned=True)
        interval = cycle._interval_from_stage(
            self.config, "runner/unclassified", stage,
            checkpoint_root=self.external_checkpoint,
            checkpoint_manifest_digest=self.request["checkpoint_manifest_digest"],
        )
        self.assertEqual("2026-09-06T21:00:00Z", interval.since_utc)

    def test_repair_interval_rejects_tampered_parent_cache(self):
        """Catches an ancestor cache changing after the repair declared its source SHA."""
        stage = self._scoped_retry_stage()
        (self.source_result.parent / "analyzer-cache-used.jsonl").write_bytes(b"changed\n")
        with self.assertRaisesRegex(cycle.CycleError, "repair analyzer cache provenance"):
            cycle._interval_from_stage(self.config, "runner/unclassified", stage)

    def test_repair_interval_rejects_cache_extension_without_retry_binding(self):
        """Catches treating new child cache decisions as a plain copied snapshot."""
        stage = self._scoped_retry_stage()
        repair = Path(str(stage["run_dir"]))
        bundle = collector_receipts.load_completion_bundle(
            repair / "completion-bundle.json", run_dir=repair,
        )
        analysis_path = repair / "semantic-analysis.json"
        analysis = json.loads(analysis_path.read_text())
        analysis.pop("failed_review_retry")
        write_json(analysis_path, analysis)
        slice_ = type("Slice", (), {
            "slice_id": bundle.slice_id,
            "since": dt.datetime.fromisoformat(bundle.since_utc.replace("Z", "+00:00")),
            "until": dt.datetime.fromisoformat(bundle.until_utc.replace("Z", "+00:00")),
        })()
        rebuilt = collector_receipts.build_completion_bundle(repair, slice_=slice_)
        collector_receipts.write_completion_bundle(repair / "completion-bundle.json", rebuilt)
        stage["bundle_digest"] = rebuilt.bundle_digest
        with self.assertRaisesRegex(cycle.CycleError, "repair retry cache binding"):
            cycle._interval_from_stage(self.config, "runner/unclassified", stage)

    def test_repair_interval_rejects_unrelated_parent_slice(self):
        """Catches a valid bundle from another date range being named as the parent."""
        stage = self._repair_stage(self.source_result.parent)
        cycle._ensure_period(
            self.config, self.state_dir, "2026-09-09", "2026-09-11", bind_inputs=True
        )
        other_result = make_run(
            self.root, "other-slice", replay=False,
            since=dt.date(2026, 9, 9), until=dt.date(2026, 9, 11),
        )
        lineage_path = Path(str(stage["run_dir"])) / "repair-source.json"
        lineage = json.loads(lineage_path.read_text())
        lineage["source_run_id"] = other_result.parent.name
        lineage["source_completion_sha256"] = cycle._digest(
            other_result.parent / "completion-bundle.json"
        )
        write_json(lineage_path, lineage)
        with self.assertRaisesRegex(cycle.CycleError, "repair ancestry slice or runtime"):
            cycle._interval_from_stage(self.config, "runner/unclassified", stage)

    def test_adopts_repaired_source_with_original_collector_checkpoint(self):
        """Catches adoption requiring collector finalization in a repaired source."""
        stage = self._repair_stage(self.source_result.parent)
        repair = Path(str(stage["run_dir"]))
        bundle = collector_receipts.load_completion_bundle(
            repair / "completion-bundle.json", run_dir=repair,
        )
        original = self.source_result.parent
        result = json.loads(self.source_result.read_text())
        result.update(
            run_id=repair.name, run_dir=str(repair),
            completion_bundle_digest=bundle.bundle_digest,
            completion_bundle=bundle.document(),
        )
        result["paths"] = {
            key: (value.replace(str(original), str(repair), 1) if value else value)
            for key, value in result["paths"].items()
        }
        repair_result = repair / "autopilot-result.json"
        write_json(repair_result, result)
        replay_result = make_run(
            self.root, "repaired-replay", replay=True,
            source_name=repair.name, snapshots_from=repair,
        )
        publication_result = repair / "sheet-publish-result.json"
        publication = json.loads(self.publication_result.read_text())
        publication["publications"] = cycle._expected_publication_receipts(
            self.config, {"run_dir": str(repair), "run_id": repair.name},
            sheet_title="September 2026 portfolio review",
        )
        write_json(publication_result, publication)
        request = {
            **self.request,
            "source_result": str(repair_result),
            "source_result_digest": cycle._digest(repair_result),
            "replay_result": str(replay_result),
            "replay_result_digest": cycle._digest(replay_result),
            "publication_result": str(publication_result),
            "publication_result_digest": cycle._digest(publication_result),
        }
        with mock.patch.object(cycle, "run_child_bounded", side_effect=AssertionError("child")):
            result = cycle.adopt_historical_slice(self.config, request)
        self.assertEqual("delivered", result["status"])


if __name__ == "__main__":
    unittest.main()
