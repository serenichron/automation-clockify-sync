"""Native recording credits must be produced before replay, from finite proof."""
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import unittest
from unittest import mock

from scripts import clockify_review_cycle as cycle, clockify_review_run as run
from scripts import collector_receipts, evidence_ledger, review_corrections
from scripts import work_accounting_pipeline as pipeline
from scripts import clockify_checkpoint_snapshot as snapshot, collector_checkpoints
from scripts import clockify_sync_collect as collector, source_coverage
from scripts.autopilot_process import ChildResult
import test_native_checkpoint_run_transport as transport_fixtures
import test_recurring_native_credit as credit_fixtures
from test_clockify_checkpoint_snapshot import SINCE, UNTIL, OBSERVED


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n")


def handle(path):
    return {"path": str(path), "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}


class CycleNativeCreditProducerTests(unittest.TestCase):
    def fixture(self, *, attrs=None, warnings=None):
        transport = transport_fixtures.NativeCheckpointRunTransportTests()
        transport.setUp()
        self.addCleanup(transport.temporary.cleanup)
        transport.downstream_fixture(native=False)
        root, source = transport.root, transport.source
        prior = root / "posted-proof"
        prior.mkdir()
        current, declaration, synthetic = credit_fixtures.RecurringNativeCreditTests().fixture(
            prior, prior_minutes=(14,), current_minutes=(14,),
            kind="source_native_meeting_intersection", prior_source_local=True,
            current_meeting_attrs=attrs,
        )
        entries = copy.deepcopy(synthetic.entries)
        store = collector_checkpoints.PageCheckpointStore(root / "native-cache")
        identity = collector._clockify_checkpoint_identity("workspace-1", "user-1", SINCE, UNTIL)
        state = store.open(identity, initial_metadata={"snapshot_at": OBSERVED})
        state = store.append_page(state, payload=entries, continuation={"page": 2},
                                 signature=collector._clockify_page_signature(entries))
        state = store.mark_complete(state)
        raw = collector.fetch_clockify({"CLOCKIFY_WORKSPACE_ID": "workspace-1"},
            {"clockify_user_id": "user-1"}, SINCE, UNTIL,
            snapshot_at=dt.datetime.fromisoformat(OBSERVED.replace("Z", "+00:00")), checkpoint_store=store)
        write(source / "evidence/clockify-existing.json", raw)
        original_evidence = root / "original-run/evidence/clockify-existing.json"
        write(original_evidence, raw)
        captured = snapshot.capture_checkpoint_snapshot(checkpoint_manifest=state.directory / "manifest.json",
            clockify_evidence=original_evidence, destination=transport.snapshot_dir,
            workspace_id="workspace-1", user_id="user-1", since=SINCE, until=UNTIL)
        current_ledger = json.loads(Path(declaration["artifacts"]["current_source_ledger"]["path"]).read_text())
        if warnings is not None:
            current[0]["review_warnings"] = copy.deepcopy(warnings)
        events = [evidence_ledger.EvidenceEvent.from_document(event) for event in current_ledger["events"]]
        events.extend(evidence_ledger.normalize_collector_snapshot({"clockify": raw}))
        ledger = evidence_ledger.EvidenceLedger(tuple(events), evidence_ledger.source_inventory_from_collector(transport.raw))
        write(source / "evidence/evidence-ledger.json", {"schema_version": "evidence-ledger/v1",
            "manifest": ledger.manifest.document(), "events": [event.document() for event in ledger.events]})
        transport.report["evidence_ledger"]["source_completeness"] = ledger.manifest.document()["source_completeness"]
        transport.report["clockify_native_checkpoint"] = {"manifest_sha256": captured.manifest_sha256,
            "request": {"workspace_id": "workspace-1", "user_id": "user-1", "since_utc": "2026-09-01T00:00:00Z", "until_utc": "2026-10-01T00:00:00Z"}}
        write(source / "proposals.json", current)
        write(source / "work-accounting-result.json", {"schema_version": 1, "allocation_mode": "non_overlapping_v1",
            "proposals": current, "ambiguous": [], "skipped": []})
        write(source / "quality_report.json", {"status": "pass"})
        (source / "analyzer-cache-used.jsonl").write_bytes(b"")
        analysis = json.loads((source / "semantic-analysis.json").read_text())
        analyzed, _ = pipeline._analysis_events([event.document() for event in ledger.events],
            pipeline.meeting_reconciliation.manifest_member_identities(ledger.manifest.document()))
        analysis["ledger_evidence_digest"] = run.semantic_analyzer.stable_digest("led-", sorted(event["evidence_id"] for event in analyzed))
        analysis["analyzer_cache"] = {"snapshot": {"path": "analyzer-cache-used.jsonl", "record_count": 0,
            "sha256": hashlib.sha256(b"").hexdigest()}}
        write(source / "semantic-analysis.json", analysis)
        for name in ("review-corrections.jsonl", "review-acceptance.jsonl"):
            (source / name).write_bytes(b"")
        (root / "global-corrections.jsonl").write_bytes(b"")
        (root / "analyzer-cache.jsonl").write_bytes(b"synthetic global cache; never consumed by offline repair\n")
        transport.seal()
        result = run.build_result(source, {"status": "pass"}, {})
        result.update(completion_bundle=json.loads(transport.bundle_path.read_text()),
                      completion_bundle_digest=json.loads(transport.bundle_path.read_text())["bundle_digest"])
        write(source / "autopilot-result.json", result)
        manifest = {"schema_version": "clockify-native-credit-input/v1", "posted": [
            {"receipt": handle(prior / "native-receipt.json"), **entry}
            for entry in declaration["prior_entries"]]}
        write(root / "native-input.json", manifest)
        config = {"root": str(root), "runs_dir": str(transport.runs), "state_dir": str(root / "state"),
            "cache": str(root / "analyzer-cache.jsonl"), "routing": str(source / "routing.json"),
            "corrections": str(root / "global-corrections.jsonl"), "acceptance": str(source / "review-acceptance.jsonl"),
            "workspace_id": "workspace-1", "member_id": "user-1", "timezone": "UTC",
            "native_credit_input": handle(root / "native-input.json")}
        expected = {name: cycle._digest(source / name) for name in run._RECONCILIATION_INPUTS.values()}
        stage = cycle._validate_stage(config, source / "autopilot-result.json", "2026-09-01", "2026-10-01",
            replay=False, expected_snapshot_digests=expected)
        return transport, config, stage, manifest, captured

    def plan(self, config, source):
        self.assertTrue(callable(getattr(cycle, "_native_credit_plan", None)), "cycle native-credit producer missing")
        return cycle._native_credit_plan(config, source)

    def test_precision_drift_produces_sealed_credit_and_four_second_tail(self):
        transport, config, source, _manifest, captured = self.fixture()
        before = {path: path.read_bytes() for path in transport.source.rglob("*") if path.is_file()}
        plan = self.plan(config, source)
        current = json.loads((transport.source / "proposals.json").read_text())
        rows, credited = pipeline._apply_verified_posted_credits(current, [], plan["credits"], collection_snapshot=captured)
        self.assertEqual(837, credited[0]["verified_posted_credit"]["covered_seconds"])
        self.assertEqual([4], [row["duration_seconds"] for row in rows])
        self.assertEqual(before, {path: path.read_bytes() for path in transport.source.rglob("*") if path.is_file()})

    def test_distinct_recording_does_not_turn_overlap_into_credit(self):
        attrs = {"recording_id": "different-recording", "share_url": "https://fathom.video/share/different"}
        _transport, config, source, _manifest, _captured = self.fixture(attrs=attrs)
        self.assertEqual([], self.plan(config, source)["credits"])

    def test_foreign_target_and_bad_receipt_or_artifact_hash_fail_closed(self):
        for change in ("foreign", "receipt-hash", "artifact-hash", "duplicate-id"):
            with self.subTest(change=change):
                transport, config, source, manifest, _captured = self.fixture()
                if change == "foreign":
                    config["member_id"] = "foreign"
                elif change == "receipt-hash":
                    manifest["posted"][0]["receipt"]["sha256"] = "sha256:" + "f" * 64
                elif change == "artifact-hash":
                    manifest["posted"][0]["artifacts"]["native_events"]["sha256"] = "sha256:" + "f" * 64
                else:
                    manifest["posted"].append(copy.deepcopy(manifest["posted"][0]))
                write(transport.root / "native-input.json", manifest)
                config["native_credit_input"] = handle(transport.root / "native-input.json")
                self.assertTrue(callable(getattr(cycle, "_native_credit_plan", None)), "cycle native-credit producer missing")
                with self.assertRaises(cycle.CycleError):
                    cycle._native_credit_plan(config, source)

    def test_no_manifest_is_noop_even_with_network_and_scans_forbidden(self):
        transport, config, source, _manifest, _captured = self.fixture()
        config.pop("native_credit_input")
        self.assertTrue(callable(getattr(cycle, "_native_credit_plan", None)), "cycle native-credit producer missing")
        with mock.patch.object(Path, "glob", side_effect=AssertionError("scan forbidden")), mock.patch.object(
            collector, "clockify_get", side_effect=AssertionError("network forbidden")):
            self.assertIsNone(cycle._native_credit_plan(config, source))

    def finish_repair(self, child):
        parent = child.parent / json.loads((child / "repair-source.json").read_text())["source_run_id"]
        current = json.loads((parent / "proposals.json").read_text())
        report = json.loads((child / "run-report.json").read_text())
        request = report["clockify_native_checkpoint"]["request"]
        proof = snapshot.load_checkpoint_snapshot(child / collector_receipts.NATIVE_CHECKPOINT_PREFIX,
            workspace_id=request["workspace_id"], user_id=request["user_id"], since=SINCE, until=UNTIL,
            expected_manifest_sha256=report["clockify_native_checkpoint"]["manifest_sha256"])
        ledger, events = pipeline.load_ledger(child / "evidence/evidence-ledger.json")
        rows, skipped = pipeline._apply_verified_posted_credits(current, pipeline._existing_blocks(events),
            review_corrections.load_verified_posted_credits(child / "review-corrections.jsonl"), collection_snapshot=proof)
        (child / "semantic-analysis.json").write_bytes(run._repair_analysis_fixture(child).read_bytes())
        write(child / "proposals.json", rows)
        write(child / "work-accounting-result.json", {"schema_version": 1, "allocation_mode": "non_overlapping_v1",
            "proposals": rows, "ambiguous": [], "skipped": skipped})
        write(child / "quality_report.json", {"status": "pass"})
        write(child / "review-snapshot.json", {})
        write(child / "fathom-reconciliation.json", [])
        bundle = run._finalize_repair_completion(child)
        result = run.build_result(child, {"status": "pass"}, {})
        result.update(completion_bundle=bundle.document(), completion_bundle_digest=bundle.bundle_digest)
        write(child / "autopilot-result.json", result)

    def test_completed_child_after_crash_is_adopted_once_before_frozen_stage_validation(self):
        transport, config, original, _manifest, _captured = self.fixture()
        self.assertTrue(callable(getattr(cycle, "_adopt_native_credit", None)), "cycle native-credit adoption missing")
        record = {"source": original, "expected_snapshot_digests": original["snapshot_digests"]}
        state = {"slices": {"2026-09-01": record}}
        state_path = transport.root / "state/review-cycle-state.json"
        before = {path: path.read_bytes() for path in transport.source.rglob("*") if path.is_file()}
        launches = []
        def crash(command, **kwargs):
            self.assertIn("--resume-from", command)
            child = Path(command[command.index("--resume-from") + 1])
            launches.append(child)
            self.finish_repair(child)
            raise KeyboardInterrupt("crash after completed repair, before adoption")
        with mock.patch.object(run, "RUNS", transport.runs), mock.patch.object(cycle, "_run_budgeted_child", side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                cycle._adopt_native_credit(config, state, state_path, record, "2026-09-01", "2026-10-01", original, [120])
            record = json.loads(state_path.read_text())["slices"]["2026-09-01"]
            with mock.patch.object(cycle, "_run_budgeted_child", side_effect=AssertionError("duplicate child")):
                adopted = cycle._adopt_native_credit(config, state, state_path, record, "2026-09-01", "2026-10-01", original, [120])
                revalidated = cycle._stage_from_state(config, record, "source", "2026-09-01", "2026-10-01", replay=False,
                    expected_snapshot_digests=original["snapshot_digests"])
        self.assertEqual(1, len(launches))
        self.assertEqual(adopted, revalidated)
        self.assertEqual([4], [row["duration_seconds"] for row in json.loads((Path(adopted["run_dir"]) / "proposals.json").read_text())])
        self.assertEqual(before, {path: path.read_bytes() for path in transport.source.rglob("*") if path.is_file()})

    def test_real_slice_repairs_then_replays_adopted_source(self):
        transport, config, original, _manifest, _captured = self.fixture()
        config.update(monthly_sheet_title_template="{month_name} {year} portfolio review", calendly_optional=True)
        record = {"source": original, "until": "2026-10-01", "expected_snapshot_digests": original["snapshot_digests"]}
        state = {"slices": {"2026-09-01": record}}
        state_path = transport.root / "state/review-cycle-state.json"
        sequence = []
        class ReplayReached(Exception):
            pass
        def child(command, **kwargs):
            if "--resume-from" in command:
                repair = Path(command[command.index("--resume-from") + 1])
                self.finish_repair(repair)
                sequence.append("repair")
                return ChildResult(0, str(repair / "autopilot-result.json") + "\n", "", False, 0.01)
            self.assertIn("--replay-from", command)
            replay_source = Path(command[command.index("--replay-from") + 1])
            self.assertEqual([4], [row["duration_seconds"] for row in json.loads((replay_source / "proposals.json").read_text())],
                "cycle replayed uncredited original source")
            persisted = json.loads(state_path.read_text())["slices"]["2026-09-01"]
            self.assertEqual(str(replay_source), persisted["source"]["run_dir"])
            sequence.append("replay")
            raise ReplayReached()
        with mock.patch.object(run, "RUNS", transport.runs), mock.patch.object(cycle, "_run_budgeted_child", side_effect=child), mock.patch.object(
                cycle, "_ensure_period", return_value=transport.source / "period-manifest.json"):
            with self.assertRaises(ReplayReached):
                cycle._run_slice(config, state, state_path, transport.root / "state", transport.root,
                    "2026-09-01", "2026-10-01", source_coverage.SourceDebtStore(), transport.root / "state/source-coverage.json", budget=[120])
        self.assertEqual(["repair", "replay"], sequence)

    def test_unknown_warning_survives_native_intersection(self):
        warning = {"reason": "unrecognized overlap warning", "counterpart_id": "not-a-ledger-id"}
        transport, config, source, _manifest, captured = self.fixture(warnings=[warning])
        plan = self.plan(config, source)
        proposals = json.loads((transport.source / "proposals.json").read_text())
        rows, _ = pipeline._apply_verified_posted_credits(proposals, [], plan["credits"], collection_snapshot=captured)
        self.assertEqual([warning], rows[0]["review_warnings"])

    def test_missing_checkpoint_native_id_never_suppresses_or_creates_override(self):
        transport, config, source, manifest, _ = self.fixture()
        manifest["posted"][0]["clockify_entry_id"] = "absent-entry"
        write(transport.root / "native-input.json", manifest)
        config["native_credit_input"] = handle(transport.root / "native-input.json")
        with self.assertRaises(cycle.CycleError):
            cycle._native_credit_plan(config, source)
        self.assertFalse((transport.root / "state/native-credit-corrections").exists())

    def test_config_accepts_only_exact_hash_bound_optional_input(self):
        transport, config, _source, _manifest, _ = self.fixture()
        config.update(recovery_since="2026-09-01", spreadsheet_id="sheet-1",
            monthly_sheet_title_template="{month_name} {year}", calendly_optional=True)
        write(transport.root / "cycle.json", config)
        self.assertEqual(config, cycle.load_config(transport.root / "cycle.json"))
        for invalid in ({"path": str(transport.root / "native-input.json")},
                        {"path": "relative.json", "sha256": "sha256:" + "a" * 64}):
            config["native_credit_input"] = invalid
            write(transport.root / "cycle.json", config)
            with self.assertRaises(cycle.CycleError):
                cycle.load_config(transport.root / "cycle.json")

    def test_incomplete_source_cannot_produce_native_credit(self):
        _transport, config, source, _manifest, _ = self.fixture()
        source["coverage"] = {"status": "incomplete", "incomplete_sources": ["sessions/test"]}
        with self.assertRaises(cycle.CycleError):
            self.plan(config, source)

    def test_append_crash_resumes_sealed_plan_without_reopening_posted_handles(self):
        transport, config, original, _manifest, _ = self.fixture()
        record = {"source": original, "expected_snapshot_digests": original["snapshot_digests"]}
        state = {"slices": {"2026-09-01": record}}
        state_path = transport.root / "state/review-cycle-state.json"
        real_append = review_corrections.append_verified_posted_credit
        def crash(*args, **kwargs):
            real_append(*args, **kwargs)
            raise KeyboardInterrupt("crash after first appended credit")
        with mock.patch.object(run, "RUNS", transport.runs), mock.patch.object(review_corrections,
                "append_verified_posted_credit", side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):
                cycle._adopt_native_credit(config, state, state_path, record, "2026-09-01", "2026-10-01", original, [120])
        record = json.loads(state_path.read_text())["slices"]["2026-09-01"]
        from scripts import clockify_source_adoptions as adoptions
        def finish(command, **kwargs):
            child = Path(command[command.index("--resume-from") + 1])
            self.finish_repair(child)
            return ChildResult(0, str(child / "autopilot-result.json") + "\n", "", False, 0.01)
        with mock.patch.object(run, "RUNS", transport.runs), mock.patch.object(cycle, "_run_budgeted_child", side_effect=finish), mock.patch.object(
                adoptions, "_capture", side_effect=AssertionError("original posted handles reopened")):
            adopted = cycle._adopt_native_credit(config, state, state_path, record, "2026-09-01", "2026-10-01", original, [120])
        corrections = Path(adopted["run_dir"]) / "review-corrections.jsonl"
        self.assertEqual(1, len(review_corrections.load_verified_posted_credits(corrections)))
        self.assertEqual(b"", Path(config["corrections"]).read_bytes())
        self.assertEqual(b"synthetic global cache; never consumed by offline repair\n", Path(config["cache"]).read_bytes())

    def test_ambiguous_current_recording_aliases_remain_visible(self):
        transport, config, source, _manifest, _ = self.fixture()
        proposals = json.loads((transport.source / "proposals.json").read_text())
        alias = copy.deepcopy(proposals[0])
        alias.update(candidate_key="wks-second", review_activity_key="wka-second", activity_id="second")
        proposals.append(alias)
        write(transport.source / "proposals.json", proposals)
        source["proposals_digest"] = cycle._digest(transport.source / "proposals.json")
        self.assertEqual([], self.plan(config, source)["credits"])

    def test_delivery_late_validation_uses_adopted_corrections_not_frozen_parent(self):
        transport, config, original, _manifest, _ = self.fixture()
        config.update(monthly_sheet_title_template="{month_name} {year} portfolio review", calendly_optional=True,
            spreadsheet_id="sheet-1")
        record = {"source": original, "until": "2026-10-01", "expected_snapshot_digests": original["snapshot_digests"]}
        state = {"slices": {"2026-09-01": record}}
        state_path = transport.root / "state/review-cycle-state.json"
        def child(command, **kwargs):
            if "--resume-from" in command:
                directory = Path(command[command.index("--resume-from") + 1])
                self.finish_repair(directory)
                result_path = directory / "autopilot-result.json"
            elif "--replay-from" in command:
                parent = Path(command[command.index("--replay-from") + 1])
                directory = run._prepare_replay_run(parent)
                for filename in ("semantic-analysis.json", "work-accounting-result.json", "proposals.json", "quality_report.json", "review-snapshot.json", "analyzer-cache-used.jsonl"):
                    (directory / filename).write_bytes((parent / filename).read_bytes())
                run._verify_replay_integrity(parent, directory)
                bundle = run._finalize_replay_completion(parent, directory)
                result = run.build_result(directory, {"status": "pass"}, {})
                result.update(completion_bundle=bundle.document(), completion_bundle_digest=bundle.bundle_digest)
                result["paths"]["replay_integrity"] = str(directory / "replay-integrity.json")
                result_path = directory / "autopilot-result.json"
                write(result_path, result)
            else:
                parent = Path(command[command.index("--proposals") + 1]).parent
                result_path = Path(command[command.index("--result-output") + 1])
                publications = cycle._expected_publication_receipts(config, {"run_dir": str(parent), "run_id": parent.name},
                    sheet_title="September 2026 portfolio review")
                write(result_path, {"schema_version": "sheet-publication-result/v1", "status": "published",
                    "external_writes": True, "clockify_writes": 0, "publications": publications})
            return ChildResult(0, str(result_path) + "\n", "", False, 0.01)
        with mock.patch.object(run, "RUNS", transport.runs), mock.patch.object(cycle, "_run_budgeted_child", side_effect=child), mock.patch.object(
                cycle, "_ensure_period", return_value=transport.source / "period-manifest.json"):
            result = cycle._run_slice(config, state, state_path, transport.root / "state", transport.root,
                "2026-09-01", "2026-10-01", source_coverage.SourceDebtStore(), transport.root / "state/source-coverage.json", budget=[120])
        self.assertEqual("delivered", result["status"])
        saved = json.loads(state_path.read_text())["slices"]["2026-09-01"]
        self.assertNotEqual(original["run_id"], saved["source"]["run_id"])
        self.assertEqual(original["snapshot_digests"], saved["expected_snapshot_digests"])


if __name__ == "__main__":
    unittest.main()
