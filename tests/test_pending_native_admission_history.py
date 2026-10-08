"""Elapsed GET freshness never invalidates independently pinned native history."""
import copy
import datetime as dt
import json
from pathlib import Path
import unittest
from unittest import mock

from scripts import clockify_pending_review_selection as pending
from scripts import clockify_review_cycle as cycle
from scripts import clockify_sheet_publish as publisher
from scripts import evidence_ledger
import test_pending_review_selection as fixtures
import test_pending_review_meeting_credits as covered_fixtures
import test_review_cycle_delivery as delivery_fixtures


DATETIME = dt.datetime


class NativeAdmissionHistoryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixture = fixtures.PendingSelectionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        item, posted_rows, declaration, self.capture = covered_fixtures.CoveredPostedMeetingTests().fixture(
            fixture.root / "posted")
        fixture.current.append(item["proposal"])
        source = fixture.binding["sources"]["current"]["artifacts"]
        ledger_document = json.loads(Path(source["ledger"]["path"]).read_text())
        events = [*fixture.events, *[evidence_ledger.EvidenceEvent.from_document(row)
                                   for row in item["source"]["ledger"]["events"]]]
        ledger = evidence_ledger.EvidenceLedger(tuple(events), timezone="UTC")
        ledger_document.update(manifest=ledger.manifest.document(), events=[event.document() for event in events])
        source["ledger"] = fixtures.write(Path(source["ledger"]["path"]), ledger_document)
        accounting = json.loads(Path(source["accounting"]["path"]).read_text())
        accounting.update(proposals=fixture.current, schema_version=1, allocation_mode="non_overlapping_v1",
                          ambiguous=[], skipped=[], fathom_reconciliation=item["source"]["accounting"]["fathom_reconciliation"])
        for key, value in (("proposals", fixture.current), ("replay_proposals", fixture.current),
                           ("accounting", accounting), ("replay_accounting", accounting)):
            source[key] = fixtures.write(Path(source[key]["path"]), value)
        packet = json.loads(Path(source["receipt"]["path"]).read_text())
        packet["input_hashes"]["evidence-ledger.private.json"] = source["ledger"]["sha256"][7:]
        for name, key in (("proposals.json", "proposals"), ("work-accounting-result.json", "accounting")):
            packet["deterministic_accounting_replay"][name] = {"byte_equal": True,
                "primary_sha256": source[key]["sha256"][7:], "replay_sha256": source[key]["sha256"][7:]}
        source["receipt"] = fixtures.write(Path(source["receipt"]["path"]), packet)
        source["quality"] = fixtures.write(Path(source["quality"]["path"]),
                                             {"status": "pass", "summary": {"total_proposals": 22}})
        fixture.baseline.extend(posted_rows.values())
        fixture.binding["sheet_capture"] = fixtures.write(fixture.root / "capture.json",
            {"spreadsheet_id": "sheet", "sheet_title": "August 2026 review", "rows": fixture.baseline})
        fixture.binding["covered_source_outcomes"] = [declaration]
        fixtures.write(fixture.binding_path, fixture.binding)
        state_dir = fixture.root / "state"
        events, manifest = cycle._period_paths(state_dir, "2026-08-01")
        fixtures.write(events, [])
        fixtures.write(manifest, {})
        snapshots = {name: "sha256:" + "a" * 64 for name in (
            "period-manifest.json", "routing.json", "review-corrections.jsonl", "review-acceptance.jsonl")}
        self.config = {"spreadsheet_id": "sheet", "pending_review_selection": str(fixture.binding_path),
            "root": str(fixture.root), "state_dir": str(state_dir), "monthly_sheet_title_template": "August 2026 review"}
        self.source = {"run_dir": str(fixture.current_dir), "run_id": fixture.current_dir.name,
            "review_ids": fixture.binding["selected_current_ids"], **{key: "native-fixture" for key in (
                "result_digest", "bundle_digest", "artifact_digests", "snapshot_digests",
                "proposals_digest", "accounting_digest", "quality_digest")},
            "snapshot_digests": snapshots, "coverage": {"status": "complete", "incomplete_sources": []}}
        self.replay = {**self.source, "replay_integrity_digest": "native-fixture-replay"}
        self.observed = DATETIME.now(dt.timezone.utc)
        self.result = {"status": "published", "external_writes": True,
                       **fixture.publish(fixtures.SelectionGateway([publisher.HEADER, *fixture.baseline]))}
        self.delivery = cycle._delivery_document(self.config, "2026-08-01", "2026-08-02",
            self.source, self.replay, sheet_title="August 2026 review", publication_profile=None,
            publication_readbacks=self.result["publications"])
        self.delivery_path = fixture.root / "delivery.json"
        fixtures.write(self.delivery_path, self.delivery)
        self.delivery_pin = cycle._digest(self.delivery_path)
        self.record = {"status": "delivered", "until": "2026-08-02", "source": self.source,
            "replay": self.replay, "source_run_id": self.source["run_id"], "replay_run_id": self.replay["run_id"],
            "review_ids": self.source["review_ids"], "delivery_receipt": str(self.delivery_path),
            "delivery_result_digest": self.delivery_pin, "period_manifest": str(manifest),
            "expected_snapshot_digests": snapshots}

    def advanced_clock(self, delta):
        instant = self.observed + delta
        class Clock(DATETIME):
            @classmethod
            def now(cls, tz=None):
                return instant.astimezone(tz) if tz is not None else instant.replace(tzinfo=None)
        return mock.patch.object(pending.dt, "datetime", Clock)

    def verify_delivery(self, *, record=True):
        cycle._verify_delivery_receipt(self.delivery_path, self.config, "2026-08-01", "2026-08-02",
            self.source, self.replay, sheet_title="August 2026 review", record=self.record if record else None)

    def test_previously_native_admitted_delivery_survives_two_days_of_elapsed_get_age(self):
        self.verify_delivery()
        self.assertEqual(self.delivery_pin, cycle._digest(self.delivery_path))
        with self.advanced_clock(dt.timedelta(days=2)):
            try:
                self.verify_delivery()
            except cycle.CycleError as exc:
                self.fail("Independently pinned native admission must remain historical proof: " + str(exc))

    def test_fresh_admission_still_rejects_stale_and_future_get(self):
        for delta in (dt.timedelta(days=2), -dt.timedelta(minutes=1)):
            with self.subTest(delta=delta), self.advanced_clock(delta):
                with self.assertRaises(publisher.PublicationError):
                    self.fixture.publish(fixtures.SelectionGateway([publisher.HEADER, *self.fixture.baseline]))

    def test_no_independent_state_authority_still_requires_fresh_admission(self):
        with self.advanced_clock(dt.timedelta(days=2)), self.assertRaises(cycle.CycleError):
            self.verify_delivery(record=False)

    @staticmethod
    def reseal(receipt):
        if "native_admission" in receipt:
            admission = receipt["native_admission"]
            admission["sha256"] = pending.digest({key: value for key, value in admission.items() if key != "sha256"})
        receipt["acceptance_sha256"] = pending.digest({key: value for key, value in receipt.items()
                                                      if key != "acceptance_sha256"})

    def write_delivery(self, document):
        document["receipt_digest"] = cycle._value_digest({key: value for key, value in document.items()
                                                         if key != "receipt_digest"})
        fixtures.write(self.delivery_path, document)

    def state_entrypoint(self):
        # The real state consumer obtains the pin from its independent record.
        # Only collector stage lookup/period maintenance are fixture boundaries;
        # native POST/GET, pending sources/credits, admission and delivery are real.
        state_dir = Path(self.config["state_dir"])
        manifest = Path(self.record["period_manifest"])
        state_path = state_dir / "native-state.json"
        fixtures.write(state_path, {"slices": {"2026-08-01": self.record}})
        state = json.loads(state_path.read_text())
        with mock.patch.object(cycle, "_ensure_period", return_value=manifest), mock.patch.object(
                cycle, "_stage_from_state", side_effect=[self.source, self.replay]):
            cycle._validate_delivered_state(self.config, state)

    def test_production_state_entrypoint_uses_its_independent_native_pin(self):
        with self.advanced_clock(dt.timedelta(days=2)):
            self.state_entrypoint()
            with self.assertRaises(publisher.PublicationError):
                self.fixture.publish(fixtures.SelectionGateway([publisher.HEADER, *self.fixture.baseline]))

    def test_self_resealed_witness_or_foreign_graph_cannot_replace_native_state_pin(self):
        original = copy.deepcopy(self.delivery)
        for mode in ("time", "witness", "acceptance", "GET-handle", "financial", "foreign-target", "foreign-source", "missing-witness"):
            with self.subTest(mode=mode):
                changed = copy.deepcopy(original)
                receipt = changed["publication_receipts"][0]["pending_selection"]
                if mode == "time":
                    receipt["native_admission"]["observed_utc"] = (self.observed + dt.timedelta(days=2)).isoformat()
                elif mode == "witness":
                    receipt["native_admission"]["acceptance_bindings_sha256"] = "sha256:" + "0" * 64
                elif mode == "acceptance":
                    receipt["saved_credit_minutes"] += 1
                elif mode == "GET-handle":
                    receipt["native_admission"]["fresh_clockify_gets"][0]["artifact"]["sha256"] = "sha256:" + "0" * 64
                elif mode == "financial":
                    changed["publication_receipts"][0]["rows_sha256"] = "sha256:" + "0" * 64
                elif mode == "foreign-target":
                    changed["target"]["sheet_title"] = "September 2026 review"
                elif mode == "foreign-source":
                    changed["source"]["run_id"] = "foreign-run"
                else:
                    receipt.pop("native_admission")
                self.reseal(receipt)
                self.write_delivery(changed)
                with self.advanced_clock(dt.timedelta(days=2)), self.assertRaises(cycle.CycleError):
                    self.state_entrypoint()
        self.write_delivery(original)

    def test_pinned_legacy_without_genuine_admission_witness_is_not_retroactively_stamped(self):
        legacy = copy.deepcopy(self.delivery)
        receipt = legacy["publication_receipts"][0]["pending_selection"]
        receipt.pop("native_admission")
        self.reseal(receipt)
        self.write_delivery(legacy)
        self.record["delivery_result_digest"] = cycle._digest(self.delivery_path)
        with self.advanced_clock(dt.timedelta(days=2)), self.assertRaises(cycle.CycleError):
            self.verify_delivery()
        self.assertNotIn("native_admission", json.loads(self.delivery_path.read_text())["publication_receipts"][0]["pending_selection"])

    def test_changed_source_get_and_post_evidence_reject_inside_authentic_history(self):
        handles = self.fixture.binding["sources"]["current"]["artifacts"]
        covered = self.fixture.binding["covered_source_outcomes"][0]
        for handle in (handles["proposals"], handles["accounting"], handles["ledger"], handles["routing"],
                       covered["fresh_clockify_capture"], covered["prior_proof_artifacts"]["native_events"]):
            with self.subTest(path=handle["path"]):
                path = Path(handle["path"])
                original = path.read_bytes()
                path.write_bytes(original + b" ")
                try:
                    with self.advanced_clock(dt.timedelta(days=2)), self.assertRaises(cycle.CycleError):
                        self.verify_delivery()
                finally:
                    path.write_bytes(original)

    def test_exact_graph_scope_does_not_admit_foreign_selection_or_leak_clock(self):
        foreign = self.fixture.root / "foreign-selection.json"
        fixtures.write(foreign, self.fixture.binding)
        with self.advanced_clock(dt.timedelta(days=2)):
            with cycle._native_pending_graph(self.delivery_path, self.delivery_pin,
                    self.config, self.source, "August 2026 review"):
                with self.assertRaises(ValueError):
                    pending.verify(bindings_path=foreign, source_dir=self.fixture.current_dir,
                        proposals=self.fixture.current, spreadsheet_id="sheet", sheet_title="August 2026 review",
                        run_id=self.fixture.current_dir.name, project_allowlist={})
            with self.assertRaises(publisher.PublicationError):
                self.fixture.publish(fixtures.SelectionGateway([publisher.HEADER, *self.fixture.baseline]))

    def test_admission_witness_rejects_future_clock_and_nonexact_get_before_use(self):
        original = self.delivery["publication_receipts"][0]["pending_selection"]
        admission = original["native_admission"]
        self.assertGreaterEqual(pending._time(admission["observed_utc"]), pending._time(self.capture["finished_utc"]))
        self.assertLessEqual(pending._time(admission["observed_utc"]), DATETIME.now(dt.timezone.utc))
        for mode in ("future", "stale-at-admission", "wrong-exact-GET", "wrong-bindings"):
            with self.subTest(mode=mode):
                receipt = copy.deepcopy(original)
                stamp = receipt["native_admission"]
                if mode == "future":
                    stamp["observed_utc"] = (DATETIME.now(dt.timezone.utc) + dt.timedelta(hours=1)).isoformat()
                elif mode == "stale-at-admission":
                    stamp["observed_utc"] = (pending._time(self.capture["finished_utc"]) - dt.timedelta(seconds=1)).isoformat()
                elif mode == "wrong-exact-GET":
                    stamp["fresh_clockify_gets"][0]["finished_utc"] = self.observed.isoformat()
                else:
                    stamp["acceptance_bindings_sha256"] = "sha256:" + "0" * 64
                self.reseal(receipt)
                with self.assertRaises(ValueError):
                    pending._validate_native_admission(receipt)

    def test_covered_history_keeps_identical_role_paths_but_rejects_changed_role_bytes(self):
        original = Path(pending.__file__).resolve()
        copied = self.fixture.root / "historical-release" / original.name
        copied.parent.mkdir()
        copied.write_bytes(original.read_bytes())
        with mock.patch.object(pending, "__file__", str(copied)):
            result = {"status": "published", "external_writes": True,
                      **self.fixture.publish(fixtures.SelectionGateway([publisher.HEADER, *self.fixture.baseline]))}
        self.delivery = cycle._delivery_document(self.config, "2026-08-01", "2026-08-02",
            self.source, self.replay, sheet_title="August 2026 review", publication_profile=None,
            publication_readbacks=result["publications"])
        self.write_delivery(self.delivery)
        self.record["delivery_result_digest"] = cycle._digest(self.delivery_path)
        with self.advanced_clock(dt.timedelta(days=2)):
            self.verify_delivery()
            copied.write_bytes(copied.read_bytes() + b"\n# changed role bytes\n")
            with self.assertRaises(cycle.CycleError):
                self.verify_delivery()

    def test_native_publication_adoption_reuses_only_the_exact_pinned_graph(self):
        path = self.fixture.root / "sheet-publish-result.json"
        fixtures.write(path, self.result)
        self.source["runtime_identity_digest"] = self.replay["runtime_identity_digest"] = "sha256:" + "b" * 64
        document = {"schema_version": cycle.DERIVED_ADOPTION_SCHEMA_VERSION,
            "source": self.source, "replay": self.replay, "runtime_identity_digest": self.source["runtime_identity_digest"],
            "publication_result": str(path), "publication_result_digest": cycle._digest(path),
            "publication_profile": None, "publication_receipts": self.delivery["publication_receipts"],
            "source_provenance": {}}
        config = {**self.config, "runs_dir": str(self.fixture.root)}
        # Ancestry resolution is a collector-fixture boundary, not an admission
        # override. Native publication bytes, source/credit checks and time are real.
        with mock.patch.object(cycle, "_interval_from_derived_stage"), self.advanced_clock(dt.timedelta(days=2)):
            cycle._verify_historical_adoption(config, self.record, document, "2026-08-01", "2026-08-02",
                                             self.source, self.replay)
            changed = copy.deepcopy(self.result)
            changed["publications"][0]["rows_sha256"] = "sha256:" + "0" * 64
            fixtures.write(path, changed)
            with self.assertRaises(cycle.CycleError):
                cycle._verify_historical_adoption(config, self.record, document, "2026-08-01", "2026-08-02",
                                                 self.source, self.replay)


class NativeDeliveryPinProducerTests(unittest.TestCase):
    def test_producer_pins_only_successfully_verified_native_delivery(self):
        for publish_code in (0, 1):
            with self.subTest(publish_code=publish_code):
                fixture = delivery_fixtures.ReviewCycleDeliveryTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                with mock.patch.object(cycle, "run_child_bounded", side_effect=fixture.child_for_runs(
                        [], publish_codes=[publish_code])):
                    result = cycle.run_cycle(fixture.config, enable_sheet_write=True, today=dt.date(2026, 9, 10))
                record = json.loads((fixture.state_dir / "review-cycle-state.json").read_text())["slices"]["2026-09-07"]
                if publish_code:
                    self.assertEqual("failed", result["status"])
                    self.assertNotIn("delivery_result_digest", record)
                else:
                    self.assertEqual("delivered", result["status"])
                    self.assertIn("delivery_result_digest", record)
                    self.assertEqual(cycle._digest(Path(record["delivery_receipt"])), record["delivery_result_digest"])


if __name__ == "__main__":
    unittest.main()
