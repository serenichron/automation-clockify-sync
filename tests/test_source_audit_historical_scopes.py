"""Source observers must authenticate and independently scope sealed imports."""
import copy
import itertools
import json
import shutil
import unittest
from pathlib import Path
from unittest import mock

from scripts import clockify_review_cycle as cycle, clockify_review_run as review
import test_cycle_historical_adoption as historical
import test_cycle_selected_delivery_adoption as selected_fixtures
import test_cycle_pending_frozen_routing_adoption as pending_fixtures
from test_review_cycle_delivery import write_json


class SourceAuditHistoricalScopeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = selected_fixtures.SelectedHistoricalDeliveryTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.adopt()
        self.root, self.state_dir = self.fixture.root, self.fixture.state_dir
        self.state_path = self.state_dir / "review-cycle-state.json"
        self.state = json.loads(self.state_path.read_bytes())
        operational = self.root / "operational-runs"
        operational.mkdir()
        self.config = {**self.fixture.config, "runs_dir": str(operational)}
        self.since = self.fixture.since
        self.record = self.state["slices"][self.since]
        self.receipt_path = Path(self.record["historical_adoption_receipt"])
        self.receipt = json.loads(self.receipt_path.read_bytes())

    def immutable(self):
        return {path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.root.rglob("*") if path.is_file()}

    def audit(self):
        return cycle.source_interval_coverage_audit(self.config)

    def test_authentic_selected_graph_projects_original_inventory_without_writes(self):
        """Using operational runs for any selected ancestry step rejects this graph."""
        before, config = self.immutable(), copy.deepcopy(self.config)
        sentinel = self.root / "sentinel-runs"
        with mock.patch.object(review, "RUNS", sentinel):
            report = self.audit()
            self.assertEqual(sentinel, review.RUNS)
        complete = {row["source"]: row for row in report["intervals"]
                    if row["status"] == "complete"}
        self.assertEqual({"clockify", "fathom", "calendly", "multica_issues",
                          "sessions/macbook", "repositories/macbook"}, set(complete))
        for row in complete.values():
            self.assertEqual("2026-09-06T21:00:00Z", row["since_utc"])
            self.assertEqual("2026-09-08T21:00:00Z", row["until_utc"])
            self.assertEqual("fixture-collector-lineage/v1", row["compatibility_version"])
        self.assertEqual(before, self.immutable())
        self.assertEqual(config, self.config)

    def test_selected_raw_source_and_replay_drift_cannot_be_replaced_by_receipt(self):
        """Checking only source reload, or overwriting raw state, admits replay drift."""
        for name in ("source", "replay"):
            for field in ("run_dir", "result_path", "accounting_digest"):
                with self.subTest(stage=name, field=field):
                    changed = copy.deepcopy(self.state)
                    changed["slices"][self.since][name][field] = "tampered"
                    write_json(self.state_path, changed)
                    before = self.immutable()
                    sentinel = self.root / "sentinel-runs"
                    with mock.patch.object(review, "RUNS", sentinel):
                        with self.assertRaisesRegex(cycle.CycleError, "stage identity differs"):
                            self.audit()
                        self.assertEqual(sentinel, review.RUNS)
                    self.assertEqual(before, self.immutable())

    def test_selected_receipt_authentication_precedes_graph_projection(self):
        """Ignoring sealed request/state digests admits tampered receipt claims."""
        for field in ("runs_root", "request_digest", "receipt_digest", "state_digest"):
            with self.subTest(field=field):
                document, state = copy.deepcopy(self.receipt), copy.deepcopy(self.state)
                if field == "runs_root":
                    document["request"][field] = self.config["runs_dir"]
                elif field == "state_digest":
                    state["slices"][self.since]["historical_adoption_receipt_digest"] = "sha256:" + "0" * 64
                else:
                    document[field] = "sha256:" + "0" * 64
                write_json(self.receipt_path, document)
                write_json(self.state_path, state)
                before = self.immutable()
                with self.assertRaisesRegex(cycle.CycleError, "receipt identity differs"):
                    self.audit()
                self.assertEqual(before, self.immutable())

    def test_resealed_selected_root_still_enforces_canonical_owner_control_and_containment(self):
        """Digest claims cannot authorize a missing, linked, writable or wrong graph."""
        linked = self.root / "linked-runs"
        linked.symlink_to(self.fixture.source.parent, target_is_directory=True)
        writable = self.root / "writable-runs"
        writable.mkdir()
        writable.chmod(0o777)
        cases = ((self.root / "missing-runs", "runs root"),
                 (linked, "canonical|symlink"), (writable, "owner controlled"),
                 (Path(self.config["runs_dir"]), "escapes its bounded run"))
        for root, error in cases:
            with self.subTest(root=root):
                document, state = copy.deepcopy(self.receipt), copy.deepcopy(self.state)
                document["request"]["runs_root"] = str(root)
                document["request_digest"] = cycle._value_digest(document["request"])
                document["receipt_digest"] = cycle._value_digest({
                    key: value for key, value in document.items() if key != "receipt_digest"})
                state["slices"][self.since]["historical_adoption_receipt_digest"] = document["receipt_digest"]
                write_json(self.receipt_path, document)
                write_json(self.state_path, state)
                sentinel = self.root / "sentinel-runs"
                with mock.patch.object(review, "RUNS", sentinel):
                    with self.assertRaisesRegex(cycle.CycleError, error):
                        self.audit()
                    self.assertEqual(sentinel, review.RUNS)

    def test_projection_error_restores_outer_scope_without_writes(self):
        """Losing finally restoration leaves later records under the selected root."""
        sentinel = self.root / "sentinel-runs"
        sentinel.mkdir()
        before = self.immutable()
        real = cycle._audit_bundle

        def fail_selected_projection(stage):
            # Only inject a fault after authentic selected provenance validation.
            self.assertEqual(self.fixture.source.parent, review.RUNS)
            real(stage)
            raise cycle.CycleError("projection fault")

        with cycle._selected_runs_config(self.config, str(sentinel)), \
                mock.patch.object(cycle, "_audit_bundle", side_effect=fail_selected_projection):
            with self.assertRaisesRegex(cycle.CycleError, "projection fault"):
                self.audit()
            self.assertEqual(sentinel, review.RUNS)
        self.assertEqual(before, self.immutable())

    def test_mixed_legacy_selected_native_v2_are_independent_in_every_order(self):
        """One import's root must never contaminate a later legacy or native graph."""
        for kind, since, until in (("legacy", "2026-09-09", "2026-09-11"),
                                   ("native", "2026-09-11", "2026-09-13")):
            fixture = historical.HistoricalAdoptionTests()
            fixture.fixture_since, fixture.fixture_until = since, until
            fixture.setUp()
            self.addCleanup(fixture.doCleanups)
            request, _derived, _collector = fixture._derived_adoption_request()
            if kind == "native":
                request["runs_root"] = str(fixture.runs)
            else:
                self.config["runs_dir"] = str(fixture.runs)
            cycle.adopt_historical_slice(fixture.config, request)
            record = json.loads((fixture.state_dir / "review-cycle-state.json").read_bytes())["slices"][since]
            receipt = self.state_dir / "historical-adoption-receipts" / f"{since}.json"
            shutil.copyfile(record["historical_adoption_receipt"], receipt)
            record["historical_adoption_receipt"] = str(receipt)
            self.state["slices"][since] = record
        expected = None
        outer = self.root / "outer-native-runs"
        outer.mkdir()
        outer_document = {"schema_version": cycle.DERIVED_ADOPTION_SCHEMA_VERSION,
                          "runs_root": str(outer)}
        for order in itertools.permutations(self.state["slices"]):
            with self.subTest(order=order):
                # The shared write_json helper sorts keys: preserve insertion
                # order here so all six consumer iteration orders are genuine.
                self.state_path.write_text(json.dumps({**self.state, "slices": {
                    since: self.state["slices"][since] for since in order}}) + "\n")
                before = self.immutable()
                with cycle._native_adoption_runs_config(self.config, outer_document):
                    report = self.audit()
                    self.assertEqual(outer, review.RUNS)
                complete = [row for row in report["intervals"] if row["status"] == "complete"]
                self.assertEqual(14, len(complete))
                self.assertEqual({"2026-09-06T21:00:00Z", "2026-09-08T21:00:00Z",
                                  "2026-09-10T21:00:00Z"}, {row["since_utc"] for row in complete})
                if expected is None:
                    expected = report
                self.assertEqual(expected, report)
                self.assertEqual(before, self.immutable())


class SourceAuditPendingProjectionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = pending_fixtures.PendingFrozenRoutingAdoptionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.config = self.fixture.config
        cycle.adopt_historical_slice(self.config, self.fixture.request)

    def immutable(self):
        return {path: (path.read_bytes(), path.stat().st_mtime_ns)
                for path in self.fixture.root.rglob("*") if path.is_file()}

    def test_pending_native_import_projects_verified_raw_inventory_without_completion(self):
        """Demanding a raw completion loses the authentic pending native graph."""
        self.assertFalse((self.fixture.raw / "completion-bundle.json").exists())
        before = self.immutable()
        sentinel = self.fixture.root / "outer-scope"
        sentinel.mkdir()
        with cycle._native_adoption_runs_config(self.config, {
                "schema_version": cycle.DERIVED_ADOPTION_SCHEMA_VERSION,
                "runs_root": str(sentinel)}):
            report = cycle.source_interval_coverage_audit(self.config)
            self.assertEqual(sentinel, review.RUNS)
        self.assertEqual({"clockify", "fathom", "calendly", "multica_issues"},
                         {row["source"] for row in report["intervals"]})
        for row in report["intervals"]:
            self.assertEqual("complete", row["status"])
            self.assertEqual("2026-08-31T21:00:00Z", row["since_utc"])
            self.assertEqual("2026-09-01T21:00:00Z", row["until_utc"])
            self.assertEqual("collector-evidence-compatibility/v2:fixture", row["compatibility_version"])
        self.assertEqual(before, self.immutable())

    def test_pending_projection_never_trusts_changed_raw_ancestor_or_lineage_claims(self):
        """Pending inventory still requires original raw bytes and authentic lineage."""
        lineage_path = self.fixture.derived / "collector-source.json"
        original = lineage_path.read_bytes()
        raw_path = self.fixture.raw / "evidence/clockify-existing.json"
        raw_original = raw_path.read_bytes()
        for field in ("raw_bytes", "source_run_dir", "lineage_digest", "schema_version"):
            with self.subTest(field=field):
                if field == "raw_bytes":
                    raw_path.write_bytes(raw_original + b" ")
                else:
                    document = json.loads(original)
                    document[field] = {"source_run_dir": str(self.fixture.derived),
                                       "lineage_digest": "sha256:" + "0" * 64,
                                       "schema_version": "collector-derivation/v1"}[field]
                    if field != "lineage_digest":
                        document["lineage_digest"] = cycle._value_digest({
                            key: value for key, value in document.items() if key != "lineage_digest"})
                    write_json(lineage_path, document)
                before = self.immutable()
                sentinel = self.fixture.root / "sentinel-runs"
                with mock.patch.object(review, "RUNS", sentinel):
                    with self.assertRaises(cycle.CycleError):
                        cycle.source_interval_coverage_audit(self.config)
                    self.assertEqual(sentinel, review.RUNS)
                self.assertEqual(before, self.immutable())
                raw_path.write_bytes(raw_original)
                lineage_path.write_bytes(original)
