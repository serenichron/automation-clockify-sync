"""Confirmed native recording history feeds fresh slices, never old replay."""
import copy
import json
from pathlib import Path
import unittest
from unittest import mock

from scripts import clockify_review_cycle as cycle, work_accounting_pipeline as pipeline
import test_cycle_native_credit_producer as fixtures


class AutomaticNativeCreditHistoryTests(unittest.TestCase):
    def fixture(self, *, attrs=None):
        helper = fixtures.CycleNativeCreditProducerTests()
        self.addCleanup(helper.doCleanups)
        transport, config, source, packet, captured = helper.fixture(attrs=attrs)
        config.pop("native_credit_input")
        proposals = json.loads((transport.source / "proposals.json").read_text())
        for row in proposals:
            row.update(clockify_project_suffix="123456", tag_suffixes=["654321"], billable=True)
        source = self.refresh_source(transport, config, source, proposals)
        history = transport.root / "state/native-credit-inputs"
        original_plan = json.loads(Path(packet["posted"][0]["artifacts"]["native_plan"]["path"]).read_text())
        packet_path = history / original_plan["plan_digest"] / "packet.json"
        fixtures.write(packet_path, packet)
        record = {"source": source, "expected_snapshot_digests": source["snapshot_digests"]}
        state = {"slices": {"2026-09-01": record}}
        state_path = transport.root / "state/review-cycle-state.json"
        return helper, transport, config, source, packet, captured, packet_path, record, state, state_path

    def refresh_source(self, transport, config, source, proposals):
        fixtures.write(transport.source / "proposals.json", proposals)
        accounting = json.loads((transport.source / "work-accounting-result.json").read_text())
        accounting["proposals"] = proposals
        fixtures.write(transport.source / "work-accounting-result.json", accounting)
        transport.seal()
        result = fixtures.run.build_result(transport.source, {"status": "pass"}, {})
        bundle = json.loads(transport.bundle_path.read_text())
        result.update(completion_bundle=bundle, completion_bundle_digest=bundle["bundle_digest"])
        fixtures.write(transport.source / "autopilot-result.json", result)
        return cycle._validate_stage(config, transport.source / "autopilot-result.json", "2026-09-01", "2026-10-01",
            replay=False, expected_snapshot_digests=source["snapshot_digests"])

    def freeze(self, config, source, record, state, state_path):
        operation = getattr(cycle, "_freeze_native_credit_input", None)
        self.assertTrue(callable(operation), "fresh cycle does not snapshot confirmed native posting history")
        return operation(config, state, state_path, record, "2026-09-01", source)

    def test_new_slice_credits_posted_recording_without_manual_config(self):
        _, transport, config, source, packet, captured, _, record, state, state_path = self.fixture()
        before = {p: p.read_bytes() for p in transport.source.rglob("*") if p.is_file()}
        effective = self.freeze(config, source, record, state, state_path)
        self.assertNotIn("native_credit_input", config)
        persisted = json.loads(state_path.read_text())["slices"]["2026-09-01"]
        self.assertEqual(record["native_credit_snapshot"], persisted["native_credit_snapshot"])
        frozen = json.loads(Path(effective["native_credit_input"]["path"]).read_text())
        self.assertEqual(packet, frozen)
        plan = cycle._native_credit_plan(effective, source)
        rows, skipped = pipeline._apply_verified_posted_credits(json.loads((transport.source / "proposals.json").read_text()),
            [], plan["credits"], collection_snapshot=captured)
        self.assertEqual([4], [r["duration_seconds"] for r in rows])
        self.assertEqual(837, skipped[0]["verified_posted_credit"]["covered_seconds"])
        self.assertEqual(before, {p: p.read_bytes() for p in transport.source.rglob("*") if p.is_file()})

    def test_resume_uses_frozen_selection_without_rediscovering_live_history(self):
        _, _, config, source, _, _, packet_path, record, state, state_path = self.fixture()
        effective = self.freeze(config, source, record, state, state_path)
        frozen = Path(effective["native_credit_input"]["path"]).read_bytes()
        packet_path.write_text("changed inventory after original selection")
        with mock.patch.object(Path, "iterdir", side_effect=AssertionError("history rediscovery forbidden")):
            resumed = self.freeze(config, source, record, state, state_path)
        self.assertEqual(effective, resumed)
        self.assertEqual(frozen, Path(resumed["native_credit_input"]["path"]).read_bytes())
        self.assertEqual(1, len(cycle._native_credit_plan(resumed, source)["credits"]))

    def test_config_changes_cannot_replace_frozen_auto_selection(self):
        _, transport, config, source, _, _, _, record, state, state_path = self.fixture()
        effective = self.freeze(config, source, record, state, state_path)
        replacement = transport.root / "replacement-credit.json"
        fixtures.write(replacement, {"schema_version": "clockify-native-credit-input/v1", "posted": []})
        changed = {**config, "native_credit_input": fixtures.handle(replacement)}
        resumed = self.freeze(changed, source, record, state, state_path)
        self.assertEqual(effective["native_credit_input"], resumed["native_credit_input"])

    def test_fresh_publication_automatically_carries_post_credit_destination(self):
        helper = fixtures.CycleNativeCreditProducerTests()
        self.addCleanup(helper.doCleanups)
        transport, config, source, _, _ = helper.fixture(prepared_editorial=True)
        config["spreadsheet_id"] = "sheet"
        _, outcome = cycle._prepare_new_publication(config, source,
            sheet_title="September 2026 portfolio review", state_dir=transport.root / "state")
        self.assertIn("binding", outcome)  # Even preserved unready sources retain exact original handles.
        document = json.loads(Path(outcome["binding"]["path"]).read_text())
        self.assertIn("native_credit_source", document,
            "new recurring publications do not carry automatic original-post proof production")
        self.assertEqual(str(transport.root / "state/native-credit-inputs"),
            document["native_credit_source"]["output_directory"])
        self.assertEqual(fixtures.handle(transport.source / "proposals.json"),
            document["native_credit_source"]["source_proposals"])

    def test_route_difference_preserves_whole_meeting_and_explicit_correction_debt(self):
        _, transport, config, source, _, _, _, record, state, state_path = self.fixture()
        proposals = json.loads((transport.source / "proposals.json").read_text())
        proposals[0].update(clockify_project_suffix="775f9f", tag_suffixes=["35aa9b46"],
            end="2026-09-07T09:13:03+00:00", duration_seconds=780, duration_minutes=13)
        source = self.refresh_source(transport, config, source, proposals)
        record["source"] = source
        effective = self.freeze(config, source, record, state, state_path)
        plan = cycle._native_credit_plan(effective, source)
        self.assertEqual([], plan["credits"], "physical meeting identity must not erase wrong-client routing debt")
        self.assertEqual([780], [p["duration_seconds"] for p in json.loads((transport.source / "proposals.json").read_text())])
        debt = record["native_credit_snapshot"]["routing_debt"]
        self.assertEqual(1, len(debt))
        self.assertEqual("123456", debt[0]["posted_project_suffix"])
        self.assertEqual("775f9f", debt[0]["proposed_project_suffix"])

    def test_eight_character_tag_suffix_is_not_truncated_to_six(self):
        _, transport, config, source, _, _, _, record, state, state_path = self.fixture()
        proposals = json.loads((transport.source / "proposals.json").read_text())
        proposals[0]["tag_suffixes"] = ["g-654321"]  # Actual fake native tag ID is tag-654321.
        source = self.refresh_source(transport, config, source, proposals)
        record["source"] = source
        effective = self.freeze(config, source, record, state, state_path)
        self.assertEqual([], record["native_credit_snapshot"]["routing_debt"])
        self.assertEqual(1, len(cycle._native_credit_plan(effective, source)["credits"]))

    def test_absent_history_freezes_empty_selection_before_first_post_arrives(self):
        _, _, config, source, packet, _, packet_path, record, state, state_path = self.fixture()
        packet_path.unlink()
        packet_path.parent.rmdir()
        packet_path.parent.parent.rmdir()
        effective = self.freeze(config, source, record, state, state_path)
        fixtures.write(packet_path, packet)
        with mock.patch.object(Path, "iterdir", side_effect=AssertionError("history rediscovery forbidden")):
            self.assertEqual(effective, self.freeze(config, source, record, state, state_path))
        self.assertEqual([], json.loads(Path(effective["native_credit_input"]["path"]).read_text())["posted"])

    def test_legacy_completed_source_without_native_basis_keeps_proposals_visible(self):
        _, transport, config, source, _, _, _, record, state, state_path = self.fixture()
        transport.report.pop("clockify_native_checkpoint")
        fixtures.write(transport.source / "run-report.json", transport.report)
        effective = self.freeze(config, source, record, state, state_path)
        self.assertEqual([], cycle._native_credit_plan(effective, source)["credits"])
        self.assertEqual(841, json.loads((transport.source / "proposals.json").read_text())[0]["duration_seconds"])

    def test_empty_selection_is_frozen_before_later_packets_arrive(self):
        _, _, config, source, packet, _, packet_path, record, state, state_path = self.fixture()
        fixtures.write(packet_path, {"schema_version": "clockify-native-credit-input/v1", "posted": []})
        effective = self.freeze(config, source, record, state, state_path)
        fixtures.write(packet_path, packet)
        self.assertEqual([], json.loads(Path(effective["native_credit_input"]["path"]).read_text())["posted"])
        with mock.patch.object(Path, "iterdir", side_effect=AssertionError("history rediscovery forbidden")):
            self.assertEqual(effective, self.freeze(config, source, record, state, state_path))

    def test_unrelated_prior_outside_checkpoint_does_not_fail_or_credit_slice(self):
        _, _, config, source, packet, _, packet_path, record, state, state_path = self.fixture()
        packet["posted"][0]["clockify_entry_id"] = "outside-this-slice"
        fixtures.write(packet_path, packet)
        effective = self.freeze(config, source, record, state, state_path)
        self.assertEqual([], json.loads(Path(effective["native_credit_input"]["path"]).read_text())["posted"])

    def test_distinct_recording_overlap_never_becomes_coverage(self):
        attrs = {"recording_id": "different-recording", "share_url": "https://fathom.video/share/other"}
        _, _, config, source, _, _, _, record, state, state_path = self.fixture(attrs=attrs)
        effective = self.freeze(config, source, record, state, state_path)
        self.assertEqual([], cycle._native_credit_plan(effective, source)["credits"])

    def test_tampered_relevant_source_never_persists_or_credits_snapshot(self):
        _, _, config, source, packet, _, packet_path, record, state, state_path = self.fixture()
        packet["posted"][0]["artifacts"]["source_ledger"]["sha256"] = "sha256:" + "f" * 64
        fixtures.write(packet_path, packet)
        with self.assertRaises(cycle.CycleError):
            self.freeze(config, source, record, state, state_path)
        self.assertNotIn("native_credit_snapshot", record)
        self.assertFalse(state_path.exists())

    def test_old_replay_never_selects_newly_available_history(self):
        _, _, config, source, _, _, packet_path, record, state, state_path = self.fixture()
        record["replay_return"] = {"historical": True}
        packet_path.write_text("not consulted for historical replay")
        with mock.patch.object(Path, "iterdir", side_effect=AssertionError("history rediscovery forbidden")):
            self.assertEqual(config, self.freeze(config, source, record, state, state_path))
        self.assertNotIn("native_credit_snapshot", record)

    def test_automatic_history_reaches_real_offline_adoption_before_replay(self):
        helper, transport, config, source, _, _, _, record, state, state_path = self.fixture()
        launched = []
        def child(command, **kwargs):
            repair = Path(command[command.index("--resume-from") + 1])
            launched.append(repair)
            helper.finish_repair(repair)
            return fixtures.ChildResult(0, str(repair / "autopilot-result.json") + "\n", "", False, 0.01)
        with mock.patch.object(fixtures.run, "RUNS", transport.runs), mock.patch.object(cycle, "_run_budgeted_child", side_effect=child):
            adopted = cycle._adopt_native_credit(config, state, state_path, record, "2026-09-01", "2026-10-01", source, [120])
        self.assertEqual(1, len(launched), "automatic packet was not used for native credit adoption")
        self.assertEqual([4], [r["duration_seconds"] for r in json.loads((Path(adopted["run_dir"]) / "proposals.json").read_text())])
        self.assertIn("native_credit_snapshot", json.loads(state_path.read_text())["slices"]["2026-09-01"])

    def test_real_post_success_packet_is_consumed_by_next_completed_collection(self):
        from scripts import clockify_native_sheet_post as native
        _, transport, config, source, packet, captured, packet_path, record, state, state_path = self.fixture()
        prior = packet["posted"][0]
        plan = json.loads(Path(prior["artifacts"]["native_plan"]["path"]).read_text())
        plan["native_credit_source"] = {"source_proposals": prior["artifacts"]["prior_proposals"],
            "source_ledger": prior["artifacts"]["source_ledger"],
            "output_directory": str(transport.root / "state/native-credit-inputs")}
        plan["plan_digest"] = native._document_digest(plan, "plan_digest")
        approval = native.approval_template(plan, approval_id="new-approved-original", approver="human board user",
            approved_at="2026-10-04T10:00:00Z", expires_at="2026-10-05T10:00:00Z")
        packet_path.unlink()  # Remove only the test fixture's preseeded manual packet.
        gateway = fixtures.credit_fixtures.adoption_fixtures.UniqueGateway()
        native.execute_plan(plan, approval, transport.root / "new-events.jsonl", transport.root / "new-receipt.json",
            gateway, now=fixtures.credit_fixtures.NOW)
        emitted = transport.root / "state/native-credit-inputs" / plan["plan_digest"] / "packet.json"
        self.assertTrue(emitted.exists())
        self.assertEqual(1, len(gateway.posts))
        effective = self.freeze(config, source, record, state, state_path)
        credit_plan = cycle._native_credit_plan(effective, source)
        rows, credited = pipeline._apply_verified_posted_credits(json.loads((transport.source / "proposals.json").read_text()),
            [], credit_plan["credits"], collection_snapshot=captured)
        self.assertEqual([4], [row["duration_seconds"] for row in rows])
        self.assertEqual(837, credited[0]["verified_posted_credit"]["covered_seconds"])


if __name__ == "__main__":
    unittest.main()
