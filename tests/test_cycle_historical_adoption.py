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
from scripts import clockify_review_run, collector_receipts, evidence_ledger, semantic_analyzer, source_coverage
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
        self.since = getattr(self, "fixture_since", "2026-09-07")
        self.until = getattr(self, "fixture_until", "2026-09-09")
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
            since=dt.date.fromisoformat(self.since), until=dt.date.fromisoformat(self.until),
            snapshot_overrides={"routing.json": new_routing},
        )
        self.replay_result = make_run(
            self.root, "historical-replay", replay=True,
            since=dt.date.fromisoformat(self.since), until=dt.date.fromisoformat(self.until),
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

    def _derived_adoption_request(
        self, *, incomplete_peer: bool = False,
    ) -> tuple[dict[str, object], Path, Path]:
        """Seal a real collector derivation; leave its old backlog receipt stale."""
        collector = self.source_result.parent
        raw = {
            "clockify": {"status": "complete", "entries": []},
            "fathom": {"status": "complete", "meetings": []},
            "calendly": {"status": "complete", "recordings": []},
            "multica_issues": {"status": "complete", "issues": []},
            "sessions": ([{
                "machine": "macbook", "status": "unavailable", "reason": "offline",
                "repository_evidence_status": "complete", "repository_events": [],
            }] if incomplete_peer else []),
        }
        names = {
            "clockify": "clockify-existing.json",
            "fathom": "fathom-meetings.json",
            "calendly": "calendly-recordings.json",
            "multica_issues": "multica-issues.json",
            "sessions": "sessions.json",
        }
        for key, filename in names.items():
            write_json(collector / "evidence" / filename, raw[key])
        ledger = evidence_ledger.EvidenceLedger(
            tuple(evidence_ledger.normalize_collector_snapshot(raw)),
            evidence_ledger.source_inventory_from_collector(raw),
        )
        write_json(collector / "evidence" / "evidence-ledger.json", {
            "schema_version": evidence_ledger.SCHEMA_VERSION,
            "manifest": ledger.manifest.document(),
            "events": [event.document() for event in ledger.events],
        })
        report = json.loads((collector / "run-report.json").read_text())
        report["evidence_ledger"] = {
            "source_completeness": ledger.manifest.document()["source_completeness"]
        }
        write_json(collector / "run-report.json", report)
        old_bundle = json.loads((collector / "completion-bundle.json").read_text())
        slice_ = type("Slice", (), {
            "slice_id": old_bundle["slice_id"],
            "since": dt.datetime.fromisoformat(old_bundle["since_utc"].replace("Z", "+00:00")),
            "until": dt.datetime.fromisoformat(old_bundle["until_utc"].replace("Z", "+00:00")),
        })()
        raw_bundle = collector_receipts.build_completion_bundle(collector, slice_=slice_)
        write_json(collector / "completion-bundle.json", raw_bundle.document())
        collector_receipts.load_collector_source_bundle(
            collector / "completion-bundle.json", run_dir=collector,
        )
        # The old manifest still names the pre-rebuild bundle and cannot serve
        # as this child's proof.
        derived = clockify_review_run._prepare_collector_derivation_run(
            collector,
            {name: collector / name for name in clockify_review_run._RECONCILIATION_INPUTS.values()},
            executor_runtime_identity={"git_sha": "fixture-sha"},
            environment={},
        )
        for filename in (
            "semantic-analysis.json", "work-accounting-result.json", "quality_report.json",
            "review-snapshot.json", "proposals.json", "fathom-reconciliation.json",
        ):
            shutil.copyfile(collector / filename, derived / filename)
        analysis = json.loads((derived / "semantic-analysis.json").read_text())
        analysis["activities"][0]["analyzer_tier"] = "fixture"
        write_json(derived / "semantic-analysis.json", analysis)
        bundle = clockify_review_run._finalize_collector_derivation_completion(derived)
        result = json.loads(self.source_result.read_text())
        result.update(
            run_id=derived.name, run_dir=str(derived),
            completion_bundle_digest=bundle.bundle_digest,
            completion_bundle=bundle.document(),
        )
        result["paths"] = {
            key: (value.replace(str(collector), str(derived), 1) if value else value)
            for key, value in result["paths"].items()
        }
        derived_result = derived / "autopilot-result.json"
        write_json(derived_result, result)
        replay = clockify_review_run._prepare_replay_run(derived)
        for filename in (
            "semantic-analysis.json", "work-accounting-result.json", "quality_report.json",
            "review-snapshot.json", "proposals.json", "fathom-reconciliation.json",
        ):
            shutil.copyfile(derived / filename, replay / filename)
        clockify_review_run._verify_replay_integrity(derived, replay)
        replay_bundle = clockify_review_run._finalize_replay_completion(derived, replay)
        replay_document = dict(result)
        replay_document.update(
            run_id=replay.name, run_dir=str(replay),
            completion_bundle_digest=replay_bundle.bundle_digest,
            completion_bundle=replay_bundle.document(),
        )
        replay_document["paths"] = {
            key: (value.replace(str(derived), str(replay), 1) if value else value)
            for key, value in result["paths"].items()
        }
        replay_document["paths"]["replay_integrity"] = str(replay / "replay-integrity.json")
        replay_result = replay / "autopilot-result.json"
        write_json(replay_result, replay_document)
        publication_result = derived / "sheet-publish-result.json"
        write_json(publication_result, {
            "schema_version": "sheet-publication-result/v1", "status": "published",
            "external_writes": True, "clockify_writes": 0,
            "publications": cycle._expected_publication_receipts(
                self.config, {"run_dir": str(derived), "run_id": derived.name},
                sheet_title="September 2026 portfolio review",
            ),
        })
        request = {
            **{key: value for key, value in self.request.items()
               if key not in {"checkpoint_root", "checkpoint_manifest_digest"}},
            "schema_version": "clockify-historical-adoption-request/v2",
            "source_provenance": {
                "kind": "collector_derivation",
                "derivation_run_dir": str(derived),
                "lineage_digest": cycle._digest(derived / "collector-source.json"),
            },
            "source_result": str(derived_result),
            "source_result_digest": cycle._digest(derived_result),
            "replay_result": str(replay_result),
            "replay_result_digest": cycle._digest(replay_result),
            "publication_result": str(publication_result),
            "publication_result_digest": cycle._digest(publication_result),
            "runtime_identity_digest": bundle.runtime_identity_digest,
        }
        return request, derived, collector

    def test_adopts_verified_derived_collector_without_old_backlog_receipt(self):
        """Catches rejecting authentic derived source solely on stale parent backlog."""
        request, derived, _collector = self._derived_adoption_request()
        self.assertFalse((derived / "slice-finalization.json").exists())
        with mock.patch.object(cycle, "run_child_bounded", side_effect=AssertionError("child")):
            result = cycle.adopt_historical_slice(self.config, request)
            repeated = cycle.adopt_historical_slice(self.config, request)
        self.assertEqual("delivered", result["status"])
        self.assertEqual(result, repeated)

    def _external_graph_request(self):
        request, derived, collector = self._derived_adoption_request()
        request["runs_root"] = str(self.runs)
        operational_runs = self.root / "operational-runs"
        operational_runs.mkdir()
        config = {**self.config, "runs_dir": str(operational_runs)}
        return config, request, derived, collector

    def test_native_v2_external_graph_is_durable_without_rebinding_operational_config(self):
        """Catches graph validation reverting to operational runs after sealing adoption."""
        config, request, derived, _collector = self._external_graph_request()
        original_config = dict(config)
        immutable = {path: (path.read_bytes(), path.stat().st_mtime_ns)
                     for path in self.runs.rglob("*") if path.is_file()}
        operational_runs = Path(config["runs_dir"])
        with mock.patch.object(clockify_review_run, "RUNS", operational_runs):
            first = cycle.adopt_historical_slice(config, request)
            self.assertEqual(operational_runs, clockify_review_run.RUNS)
            state_path = self.state_dir / "review-cycle-state.json"
            state = json.loads(state_path.read_bytes())
            record = state["slices"][self.since]
            adoption = json.loads(Path(record["historical_adoption_receipt"]).read_bytes())
            self.assertEqual(str(self.runs), adoption["runs_root"])
            self.assertEqual(str(derived), record["source"]["run_dir"])
            self.assertEqual(self.until, state["completed_through"])
            durable = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (
                state_path, Path(record["delivery_receipt"]),
                Path(record["historical_adoption_receipt"]),
            )}
            cycle._validate_delivered_state(config, state)
            self.assertEqual(first, cycle.adopt_historical_slice(config, request))
            self.assertEqual(operational_runs, clockify_review_run.RUNS)
            self.assertEqual(durable, {path: (path.read_bytes(), path.stat().st_mtime_ns)
                                      for path in durable})
        self.assertEqual(original_config, config)
        self.assertEqual("delivered", first["status"])
        self.assertEqual([], list(operational_runs.iterdir()))
        self.assertEqual(immutable, {path: (path.read_bytes(), path.stat().st_mtime_ns)
                                     for path in immutable})

    def test_native_v2_external_graph_rejects_missing_altered_and_uncontrolled_roots(self):
        """Catches root selection becoming discovery or escaping owner-controlled containment."""
        config, request, _derived, _collector = self._external_graph_request()
        wrong = self.root / "wrong-runs"
        wrong.mkdir()
        uncontrolled = self.root / "shared-runs"
        uncontrolled.mkdir(mode=0o777)
        uncontrolled.chmod(0o777)
        linked = self.root / "linked-runs"
        linked.symlink_to(self.runs, target_is_directory=True)
        state_path = self.state_dir / "review-cycle-state.json"
        before = state_path.read_bytes()
        for root in (None, "relative-runs", str(self.root / "missing-runs"),
                     str(wrong), str(uncontrolled), str(linked)):
            with self.subTest(root=root), mock.patch.object(
                clockify_review_run, "RUNS", Path(config["runs_dir"]),
            ):
                changed = {**request, "runs_root": root}
                with self.assertRaises(cycle.CycleError):
                    cycle.adopt_historical_slice(config, changed)
                self.assertEqual(Path(config["runs_dir"]), clockify_review_run.RUNS)
                self.assertEqual(before, state_path.read_bytes())
                self.assertFalse((self.state_dir / "delivery-receipts").exists())
        unscoped = {key: value for key, value in request.items() if key != "runs_root"}
        with self.assertRaisesRegex(cycle.CycleError, "escapes its bounded run"):
            cycle.adopt_historical_slice(config, unscoped)

    def test_native_v2_external_graph_rejects_out_of_root_artifacts_and_provenance(self):
        """Catches scoping only the source while admitting an unbounded replay, publisher or ancestor."""
        config, request, _derived, _collector = self._external_graph_request()
        before = (self.state_dir / "review-cycle-state.json").read_bytes()
        for key in ("source_result", "replay_result", "publication_result"):
            with self.subTest(key=key):
                outside = self.root / key / Path(request[key]).name
                outside.parent.mkdir()
                shutil.copyfile(request[key], outside)
                changed = {**request, key: str(outside)}
                with self.assertRaisesRegex(cycle.CycleError, "escapes its bounded run"):
                    cycle.adopt_historical_slice(config, changed)
                self.assertEqual(before, (self.state_dir / "review-cycle-state.json").read_bytes())
        changed = {**request, "source_provenance": {
            **request["source_provenance"], "derivation_run_dir": str(self.root / "ancestor"),
        }}
        with self.assertRaisesRegex(cycle.CycleError, "derivation provenance"):
            cycle.adopt_historical_slice(config, changed)

    def test_native_v2_external_graph_revalidates_source_and_publication_drift(self):
        """Catches receipt-scoped adoption trusting sealed metadata after graph bytes change."""
        config, request, _derived, collector = self._external_graph_request()
        cycle.adopt_historical_slice(config, request)
        state = json.loads((self.state_dir / "review-cycle-state.json").read_bytes())
        publication = Path(request["publication_result"])
        original_publication = publication.read_bytes()
        write_json(publication, {"status": "tampered"})
        with self.assertRaisesRegex(cycle.CycleError, "publication result identity"):
            cycle._validate_delivered_state(config, state)
        publication.write_bytes(original_publication)
        write_json(collector / "evidence" / "clockify-existing.json", {
            "status": "complete", "entries": [{"tampered": True}],
        })
        with self.assertRaisesRegex(cycle.CycleError, "collector derivation provenance"):
            cycle._validate_delivered_state(config, state)

    def test_native_v2_delivered_receipt_rejects_missing_graph_and_changed_root_identity(self):
        """Catches a durable delivery silently falling back when its pinned graph disappears."""
        config, request, _derived, _collector = self._external_graph_request()
        cycle.adopt_historical_slice(config, request)
        state_path = self.state_dir / "review-cycle-state.json"
        before = state_path.read_bytes()
        state = json.loads(before)
        record = state["slices"][self.since]
        path = Path(record["historical_adoption_receipt"])
        document = json.loads(path.read_bytes())
        detached = self.root / "detached-runs"
        self.runs.rename(detached)
        try:
            with self.assertRaisesRegex(cycle.CycleError, "runs root"):
                cycle._validate_delivered_state(config, state)
        finally:
            detached.rename(self.runs)
        document["runs_root"] = str(Path(config["runs_dir"]))
        write_json(path, document)
        with self.assertRaisesRegex(cycle.CycleError, "receipt identity differs"):
            cycle._validate_delivered_state(config, state)
        # Even rehashing both stored claims cannot admit paths outside the
        # replacement graph's containment boundary.
        unsigned = {key: value for key, value in document.items() if key != "receipt_digest"}
        document["receipt_digest"] = cycle._value_digest(unsigned)
        record["historical_adoption_receipt_digest"] = document["receipt_digest"]
        write_json(path, document)
        with self.assertRaisesRegex(cycle.CycleError, "escapes its bounded run"):
            cycle._validate_delivered_state(config, state)
        self.assertEqual(before, state_path.read_bytes())

    def test_native_v2_external_graph_preserves_publication_target_validation(self):
        """Catches a scoped graph overriding the operational spreadsheet destination."""
        config, request, _derived, _collector = self._external_graph_request()
        cycle.adopt_historical_slice(config, request)
        state = json.loads((self.state_dir / "review-cycle-state.json").read_bytes())
        with self.assertRaisesRegex(cycle.CycleError, "readbacks differ"):
            cycle._validate_delivered_state({**config, "spreadsheet_id": "wrong-sheet"}, state)

    def test_native_v2_repeat_does_not_leak_external_graph_to_legacy_native_slice(self):
        """Catches an outer adoption scope contaminating another receipt's native ancestry."""
        request, _derived, _collector = self._derived_adoption_request()
        request["runs_root"] = str(self.runs)
        cycle.adopt_historical_slice(self.config, request)
        second = HistoricalAdoptionTests()
        second.fixture_since = self.until
        second.fixture_until = "2026-09-11"
        second.setUp()
        self.addCleanup(second.doCleanups)
        second_request, _second_derived, _second_collector = second._derived_adoption_request()
        state_path = self.state_dir / "review-cycle-state.json"
        state = json.loads(state_path.read_bytes())
        second_state = json.loads((second.state_dir / "review-cycle-state.json").read_bytes())
        second_record = second_state["slices"][second.since]
        for source, destination in zip(
            cycle._period_paths(second.state_dir, second.since),
            cycle._period_paths(self.state_dir, second.since),
        ):
            shutil.copyfile(source, destination)
        second_record["period_manifest"] = str(cycle._period_paths(self.state_dir, second.since)[1])
        state["slices"][second.since] = second_record
        write_json(state_path, state)
        # The current config's operational graph owns the legacy v2 receipt;
        # the earlier receipt explicitly owns its different historical graph.
        config = {**second.config, "state_dir": str(self.state_dir)}
        cycle.adopt_historical_slice(config, second_request)
        state = json.loads(state_path.read_bytes())
        self.assertEqual(second.until, state["completed_through"])
        with mock.patch.object(clockify_review_run, "RUNS", second.runs):
            cycle._validate_delivered_state(config, state)
            repeated = cycle.adopt_historical_slice(config, request)
            self.assertEqual(second.runs, clockify_review_run.RUNS)
        self.assertEqual("delivered", repeated["status"])

    def test_derived_adoption_rejects_rewritten_lineage(self):
        """Catches trusting a matching request digest instead of verifying ancestry."""
        request, derived, _collector = self._derived_adoption_request()
        path = derived / "collector-source.json"
        lineage = json.loads(path.read_text())
        lineage["source_bundle_digest"] = "sha256:" + "f" * 64
        unsigned = {key: value for key, value in lineage.items() if key != "lineage_digest"}
        lineage["lineage_digest"] = "sha256:" + hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        write_json(path, lineage)
        request["source_provenance"]["lineage_digest"] = cycle._digest(path)
        with self.assertRaisesRegex(cycle.CycleError, "collector derivation provenance"):
            cycle.adopt_historical_slice(self.config, request)

    def test_derived_adoption_rejects_changed_raw_collector_evidence(self):
        """Catches accepting lineage whose verified raw source bytes have drifted."""
        request, _derived, collector = self._derived_adoption_request()
        write_json(collector / "evidence" / "clockify-existing.json", {
            "status": "complete", "entries": [{"tampered": True}],
        })
        with self.assertRaisesRegex(cycle.CycleError, "collector derivation provenance"):
            cycle.adopt_historical_slice(self.config, request)

    def test_derived_adoption_receipt_rechecks_raw_collector_evidence(self):
        """Catches trusting a stored delivery after its collector ancestry drifts."""
        request, _derived, collector = self._derived_adoption_request()
        cycle.adopt_historical_slice(self.config, request)
        write_json(collector / "evidence" / "clockify-existing.json", {
            "status": "complete", "entries": [{"tampered": True}],
        })
        with self.assertRaisesRegex(cycle.CycleError, "collector derivation provenance"):
            cycle.adopt_historical_slice(self.config, request)

    def test_derived_adoption_rejects_rebound_slice(self):
        """Catches assigning a sealed derivation to another collector slice."""
        request, derived, _collector = self._derived_adoption_request()
        path = derived / "collector-source.json"
        lineage = json.loads(path.read_text())
        lineage["source_slice_id"] = "sha256:" + "a" * 64
        unsigned = {key: value for key, value in lineage.items() if key != "lineage_digest"}
        lineage["lineage_digest"] = "sha256:" + hashlib.sha256(
            json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        write_json(path, lineage)
        request["source_provenance"]["lineage_digest"] = cycle._digest(path)
        with self.assertRaisesRegex(cycle.CycleError, "collector derivation provenance"):
            cycle.adopt_historical_slice(self.config, request)

    def test_derived_adoption_rejects_external_finalization_symlink(self):
        """Catches sourcing unsealed compatibility metadata outside the raw run."""
        request, _derived, collector = self._derived_adoption_request()
        finalization = collector / "slice-finalization.json"
        outside = self.root / "external-finalization.json"
        outside.write_bytes(finalization.read_bytes())
        finalization.unlink()
        finalization.symlink_to(outside)
        with self.assertRaisesRegex(cycle.CycleError, "collector source finalization path"):
            cycle.adopt_historical_slice(self.config, request)

    def test_adopts_repair_descendant_of_verified_derivation(self):
        """Catches stopping a sealed repair chain before its derived collector node."""
        request, derived, _collector = self._derived_adoption_request()
        repair = clockify_review_run._prepare_repair_run(derived)
        for filename in (
            "semantic-analysis.json", "work-accounting-result.json", "quality_report.json",
            "review-snapshot.json", "proposals.json", "fathom-reconciliation.json",
        ):
            shutil.copyfile(derived / filename, repair / filename)
        repair_bundle = clockify_review_run._finalize_repair_completion(repair)
        derived_result = json.loads((derived / "autopilot-result.json").read_text())
        repair_document = dict(derived_result)
        repair_document.update(
            run_id=repair.name, run_dir=str(repair),
            completion_bundle_digest=repair_bundle.bundle_digest,
            completion_bundle=repair_bundle.document(),
        )
        repair_document["paths"] = {
            key: (value.replace(str(derived), str(repair), 1) if value else value)
            for key, value in derived_result["paths"].items()
        }
        repair_result = repair / "autopilot-result.json"
        write_json(repair_result, repair_document)
        replay = clockify_review_run._prepare_replay_run(repair)
        for filename in (
            "semantic-analysis.json", "work-accounting-result.json", "quality_report.json",
            "review-snapshot.json", "proposals.json", "fathom-reconciliation.json",
        ):
            shutil.copyfile(repair / filename, replay / filename)
        clockify_review_run._verify_replay_integrity(repair, replay)
        replay_bundle = clockify_review_run._finalize_replay_completion(repair, replay)
        replay_document = dict(repair_document)
        replay_document.update(
            run_id=replay.name, run_dir=str(replay),
            completion_bundle_digest=replay_bundle.bundle_digest,
            completion_bundle=replay_bundle.document(),
        )
        replay_document["paths"] = {
            key: (value.replace(str(repair), str(replay), 1) if value else value)
            for key, value in repair_document["paths"].items()
        }
        replay_document["paths"]["replay_integrity"] = str(replay / "replay-integrity.json")
        replay_result = replay / "autopilot-result.json"
        write_json(replay_result, replay_document)
        publication_result = repair / "sheet-publish-result.json"
        write_json(publication_result, {
            "schema_version": "sheet-publication-result/v1", "status": "published",
            "external_writes": True, "clockify_writes": 0,
            "publications": cycle._expected_publication_receipts(
                self.config, {"run_dir": str(repair), "run_id": repair.name},
                sheet_title="September 2026 portfolio review",
            ),
        })
        request.update({
            "source_result": str(repair_result),
            "source_result_digest": cycle._digest(repair_result),
            "replay_result": str(replay_result),
            "replay_result_digest": cycle._digest(replay_result),
            "publication_result": str(publication_result),
            "publication_result_digest": cycle._digest(publication_result),
        })
        result = cycle.adopt_historical_slice(self.config, request)
        self.assertEqual("delivered", result["status"])

    def test_incomplete_pass_derivation_records_exact_peer_debt(self):
        """Catches demanding a derived child's absent finalization for peer recovery."""
        request, _derived, _collector = self._derived_adoption_request(
            incomplete_peer=True,
        )
        stage = cycle._validate_stage(
            self.config, Path(request["source_result"]), self.since, self.until,
            replay=False, expected_snapshot_digests=request["adopted_snapshot_digests"],
            expected_runtime_digest=request["runtime_identity_digest"],
            historical_state_validation=True,
        )
        self.assertEqual(["sessions/macbook"], stage["coverage"]["incomplete_sources"])
        store = source_coverage.SourceDebtStore()
        self.assertTrue(cycle._record_exact_debts(self.config, store, stage))
        active = store.active()
        self.assertEqual(1, len(active))
        self.assertEqual("peer/macbook", active[0].interval.source)
        self.assertEqual("2026-09-06T21:00:00Z", active[0].interval.since_utc)
        self.assertEqual("2026-09-08T21:00:00Z", active[0].interval.until_utc)

    def test_legacy_checkpoint_request_cannot_borrow_derived_proof(self):
        """Catches silently treating a v1 checkpoint claim as v2 derivation proof."""
        request, _derived, _collector = self._derived_adoption_request()
        request.pop("source_provenance")
        request.update({
            "schema_version": "clockify-historical-adoption-request/v1",
            "checkpoint_root": self.request["checkpoint_root"],
            "checkpoint_manifest_digest": self.request["checkpoint_manifest_digest"],
        })
        with self.assertRaisesRegex(cycle.CycleError, "slice finalization"):
            cycle.adopt_historical_slice(self.config, request)

    def test_derived_interval_rejects_executor_bundle_drift_after_stage_validation(self):
        """Catches consuming a new completion bundle under an old verified stage."""
        request, derived, _collector = self._derived_adoption_request()
        stage = cycle._validate_stage(
            self.config, Path(request["source_result"]), self.since, self.until,
            replay=False, expected_snapshot_digests=request["adopted_snapshot_digests"],
            expected_runtime_digest=request["runtime_identity_digest"],
            historical_state_validation=True,
        )
        quality_path = derived / "quality_report.json"
        quality = json.loads(quality_path.read_text())
        quality["summary"]["audit_marker"] = "changed after stage verification"
        write_json(quality_path, quality)
        old_bundle = json.loads((derived / "completion-bundle.json").read_text())
        slice_ = type("Slice", (), {
            "slice_id": old_bundle["slice_id"],
            "since": dt.datetime.fromisoformat(old_bundle["since_utc"].replace("Z", "+00:00")),
            "until": dt.datetime.fromisoformat(old_bundle["until_utc"].replace("Z", "+00:00")),
        })()
        changed = collector_receipts.build_completion_bundle(derived, slice_=slice_)
        write_json(derived / "completion-bundle.json", changed.document())
        with self.assertRaisesRegex(cycle.CycleError, "derived executor completion drifted"):
            cycle._interval_from_derived_stage(
                self.config, self.since, self.until, stage,
                request["source_provenance"],
            )

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
