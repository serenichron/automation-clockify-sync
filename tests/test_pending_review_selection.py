"""Offline saved-pending publication selection, with genuine native helpers."""
import copy
import contextlib
import datetime as dt
import hashlib
import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import clockify_sheet_publish as publisher
from scripts import evidence_ledger, work_accounting_pipeline as pipeline
from test_sheet_publish import StatefulGateway


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")
    return {"path": str(path), "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}


class SelectionGateway(StatefulGateway):
    def update_values(self, spreadsheet_id, ranges):
        single = [item for item in ranges if "!J" in item["range"] and ":" not in item["range"].split("!")[1]]
        super().update_values(spreadsheet_id, [item for item in ranges if item not in single])
        self.updated.extend(single)
        for item in single:
            self.rows[int(item["range"].split("!J")[1]) - 1][9] = item["values"][0][0]


class PendingSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.current_dir = self.root / "current-run"
        self.route = {"project_name": "Serenichron", "project_suffix": "abc123", "tag_suffixes": [], "tag_names": []}
        self.events = []
        self.current = [self.proposal("new-" + str(i), i, "atom-" + str(i)) for i in range(21)]
        self.old = [self.proposal("old-" + str(i), i, "atom-" + str(i % 13)) for i in range(16)]
        self.retained = [self.proposal("retained-" + str(i), i + 30, "retained-atom-" + str(i)) for i in range(21)]
        # Held proposals are covered by saved older outcomes, not silently dropped.
        for i in range(13, 21):
            self.current[i]["provenance"]["evidence_ids"] = self.retained[i - 13]["provenance"]["evidence_ids"]
        self.inactive = [self.proposal("inactive-" + str(i), i + 60, "inactive-atom-" + str(i)) for i in range(7)]
        prior = self.old + self.retained + self.inactive
        self.baseline = [publisher.proposal_row(p, "prior-run", project_allowlist={}) for p in prior]
        for row in self.baseline[-7:]:
            row[9] = row[13] = "superseded"
        ledger = evidence_ledger.EvidenceLedger(tuple(self.events), timezone="UTC")
        document = {"schema_version": "evidence-ledger/v1", "manifest": ledger.manifest.document(),
                    "events": [event.document() for event in self.events]}
        self.binding = {"schema_version": "pending-review-selection/v1", "spreadsheet_id": "sheet",
                        "sheet_title": "August 2026 review", "current_source": "current",
                        "selected_current_ids": [publisher.stable_review_id(p) for p in self.current[:13]],
                        "prior_rows": [{"review_id": row[0], "source": "prior", "disposition": disposition}
                                       for row, disposition in zip(self.baseline, ["supersede"] * 16 + ["retain"] * 21 + ["inactive"] * 7)],
                        "sheet_capture": write(self.root / "capture.json", {"spreadsheet_id": "sheet", "sheet_title": "August 2026 review", "rows": self.baseline}),
                        "sources": {}}
        for name, proposals in (("current", self.current), ("prior", prior)):
            source_dir = self.current_dir if name == "current" else self.root / "prior-run"
            accounting = {"proposals": proposals, "allocation": {"evidence": [self.demand(p) for p in proposals]}}
            artifacts = {"proposals": write(source_dir / "proposals.json", proposals),
                         "ledger": write(source_dir / "evidence/evidence-ledger.json", document),
                         "accounting": write(source_dir / "work-accounting-result.json", accounting),
                         "routing": write(source_dir / "routing.json", {})}
            artifacts["replay_proposals"] = write(self.root / (name + "-replay") / "proposals.json", proposals)
            artifacts["replay_accounting"] = write(self.root / (name + "-replay") / "work-accounting-result.json", accounting)
            receipt = {"schema_version": "macbook-only-supplemental-native-accounting/v1", "strict_full_source_quality_run": False,
                       "input_hashes": {"evidence-ledger.private.json": artifacts["ledger"]["sha256"][7:], "routing.private.json": artifacts["routing"]["sha256"][7:]},
                       "deterministic_accounting_replay": {filename: {"byte_equal": True, "primary_sha256": artifacts[key]["sha256"][7:], "replay_sha256": artifacts[key]["sha256"][7:]}
                                                           for filename, key in (("proposals.json", "proposals"), ("work-accounting-result.json", "accounting"))}}
            artifacts["receipt"] = write(source_dir / "packet.json", receipt)
            if name == "current":
                artifacts["quality"] = write(source_dir / "quality_report.json", {"status": "pass", "summary": {"total_proposals": 21}})
                artifacts["replay"] = write(self.root / "current-replay" / "replay-integrity.json", {"status": "pass", "failures": [], "source_run_id": source_dir.name,
                                                                                                     "reconciliation_binding": {"routing_sha256": artifacts["routing"]["sha256"]}})
            self.binding["sources"][name] = {"run_id": source_dir.name, "basis": "supplemental-native-packet", "artifacts": artifacts}
        self.binding_path = self.root / "selection.json"
        write(self.binding_path, self.binding)

    def proposal(self, name, offset, atom):
        start = dt.datetime(2026, 8, 1, 9, tzinfo=dt.timezone.utc) + dt.timedelta(minutes=offset * 2)
        event = evidence_ledger.evidence_event("test_event", {"machine": "test", "session_id": atom}, observed_at=start.isoformat(),
                                              attributes={"role": "assistant", "kind": "message", "content": atom})
        # Same canonical atom is independent of interval/description identity.
        existing = next((e for e in self.events if e.attributes["content"] == atom), None)
        event = existing or event
        if existing is None:
            self.events.append(event)
        return pipeline._proposal({"activity_id": name, "workstream_id": name, "semantic_confidence": "high"},
                                  self.route, "SC — " + name, start, start + dt.timedelta(minutes=1), [event.evidence_id], 1)

    def demand(self, proposal):
        return {"activity_id": proposal["activity_id"], "workstream_id": proposal["workstream_id"],
                "evidence_ids": proposal["provenance"]["evidence_ids"], "effort": {"min": 1, "recommended": 1, "max": 1},
                "allowed_intervals": [{"start": proposal["start"], "end": proposal["end"]}]}

    def publish(self, gateway, **overrides):
        kwargs = dict(spreadsheet_id="sheet", sheet_title="August 2026 review", template_title="Proposals",
                      proposals=self.current, run_id=self.current_dir.name, project_allowlist={},
                      source_dir=self.current_dir, pending_selection=self.binding_path)
        kwargs.update(overrides)
        try:
            return publisher.publish_proposal_partitions(gateway, **kwargs)
        except TypeError as exc:
            if "unexpected keyword argument" in str(exc):
                self.fail("missing native pending selection consumer: blind full-source append is not accepted")
            raise

    def test_full_source_projects_13_new_16_superseded_21_retained_7_inactive(self):
        gateway = SelectionGateway([publisher.HEADER, *self.baseline])
        blind = SelectionGateway([publisher.HEADER, *self.baseline])
        result = publisher.publish_proposal_partitions(blind, spreadsheet_id="sheet", sheet_title="August 2026 review",
                                                      template_title="Proposals", proposals=self.current,
                                                      run_id=self.current_dir.name, project_allowlist={})
        self.assertEqual(21, result["publications"][0]["appended"])
        result = self.publish(gateway)
        self.assertEqual(13, result["publications"][0]["appended"])
        self.assertEqual(16, result["terminal_updates"])
        for index, before in enumerate(self.baseline):
            expected = list(before)
            if index < 16:
                expected[9] = "superseded"
            self.assertEqual(expected, gateway.rows[index + 1])
        self.assertEqual(34, result["pending_selection"]["saved_credit_rows"])
        self.assertEqual(0, result["pending_selection"]["remaining_recoverable_minutes"])
        before = copy.deepcopy(gateway.rows)
        again = self.publish(gateway)
        self.assertEqual(before, gateway.rows)
        self.assertEqual(0, again["publications"][0]["appended"])
        self.assertEqual(0, again["publications"][0]["updated"])
        self.assertEqual(0, again["terminal_updates"])

    def test_human_decision_or_any_bound_cell_drift_fails_before_mutation(self):
        for row_number, column, value in ((0, 9, "approved"), (0, 13, "posted"), (0, 14, "human note"),
                                         (0, 3, 2), (16, 4, "new human routing"), (43, 12, "changed inactive")):
            with self.subTest(row=row_number, column=column):
                rows = copy.deepcopy(self.baseline)
                rows[row_number][column] = value
                gateway = SelectionGateway([publisher.HEADER, *rows])
                before = copy.deepcopy(gateway.rows)
                with self.assertRaises(publisher.PublicationError):
                    self.publish(gateway)
                self.assertEqual(before, gateway.rows)
                self.assertEqual([], gateway.prepared)
                self.assertEqual([], gateway.updated)
                self.assertEqual([], gateway.appended)

    def test_source_drift_full21_gate_and_partial_outcome_selection_rejected(self):
        for mode in ("source-bytes", "quality-count", "held-outcome", "fake-lineage", "wrong-destination"):
            with self.subTest(mode=mode):
                binding = copy.deepcopy(self.binding)
                if mode == "source-bytes":
                    binding["sources"]["prior"]["artifacts"]["proposals"]["sha256"] = "sha256:" + "0" * 64
                elif mode == "quality-count":
                    binding["sources"]["current"]["artifacts"]["quality"] = write(self.root / "bad-quality.json", {"status": "pass", "summary": {"total_proposals": 13}})
                elif mode == "held-outcome":
                    binding["selected_current_ids"].pop()
                elif mode == "fake-lineage":
                    binding["sources"]["prior"]["basis"] = "whole177-quality-approved"
                else:
                    binding["sheet_title"] = "another Sheet"
                write(self.binding_path, binding)
                gateway = SelectionGateway([publisher.HEADER, *self.baseline])
                with self.assertRaises(publisher.PublicationError):
                    self.publish(gateway)
                self.assertEqual([], gateway.prepared)
                self.assertEqual([], gateway.updated)
        write(self.binding_path, self.binding)

    def test_native_original_demand_is_not_capped_to_make_selection_pass(self):
        source = self.binding["sources"]["prior"]
        accounting_path = Path(source["artifacts"]["accounting"]["path"])
        accounting = json.loads(accounting_path.read_text())
        demand = accounting["allocation"]["evidence"][16]
        demand["effort"] = {"min": 1, "recommended": 2, "max": 2}
        proposal = self.retained[0]
        demand["allowed_intervals"][0]["end"] = (dt.datetime.fromisoformat(proposal["end"]) + dt.timedelta(minutes=1)).isoformat()
        source["artifacts"]["accounting"] = write(accounting_path, accounting)
        source["artifacts"]["replay_accounting"] = write(Path(source["artifacts"]["replay_accounting"]["path"]), accounting)
        receipt_path = Path(source["artifacts"]["receipt"]["path"])
        receipt = json.loads(receipt_path.read_text())
        receipt["deterministic_accounting_replay"]["work-accounting-result.json"].update(primary_sha256=source["artifacts"]["accounting"]["sha256"][7:], replay_sha256=source["artifacts"]["accounting"]["sha256"][7:])
        source["artifacts"]["receipt"] = write(receipt_path, receipt)
        write(self.binding_path, self.binding)
        gateway = SelectionGateway([publisher.HEADER, *self.baseline])
        with self.assertRaises(publisher.PublicationError) as error:
            self.publish(gateway)
        self.assertIn("recoverable whole-minute", str(error.exception.__cause__))
        self.assertEqual([], gateway.prepared)

    def test_repeat_rejects_new_row_human_decision_change(self):
        gateway = SelectionGateway([publisher.HEADER, *self.baseline])
        self.publish(gateway)
        gateway.rows[-1][9] = "approved"
        gateway.prepared.clear()
        before = copy.deepcopy(gateway.rows)
        with self.assertRaises(publisher.PublicationError):
            self.publish(gateway)
        self.assertEqual(before, gateway.rows)
        self.assertEqual([], gateway.prepared)

    def test_post_plan_collaborator_changes_are_preserved_without_J_transitions(self):
        # The native primary append happens after planning but before pending
        # supersession. A collaborator can approve, edit or move a predecessor
        # during that gap; stale cached J ranges must never overwrite them.
        for mode in ("approval", "note", "duration", "movement"):
            with self.subTest(mode=mode):
                class ConcurrentGateway(SelectionGateway):
                    def append_values(gateway, spreadsheet_id, range_name, rows):
                        super().append_values(spreadsheet_id, range_name, rows)
                        if mode == "approval":
                            gateway.rows[1][9] = "approved"
                        elif mode == "note":
                            gateway.rows[1][14] = "collaborator decision"
                        elif mode == "duration":
                            gateway.rows[1][3] = 2
                        else:
                            gateway.rows[1], gateway.rows[2] = gateway.rows[2], gateway.rows[1]
                        gateway.collaborator_rows = copy.deepcopy(gateway.rows)

                gateway = ConcurrentGateway([publisher.HEADER, *self.baseline])
                with self.assertRaises(publisher.PublicationError):
                    self.publish(gateway)
                self.assertEqual(gateway.collaborator_rows, gateway.rows)
                self.assertFalse(any(row[9] == "superseded" for row in gateway.rows[1:17]))

    def test_native_overlap_warnings_surface_only_on_new_rows(self):
        # The selected new row and distinct retained row collide in time but
        # have different canonical atoms; native normalization must warn.
        retained = self.retained[0]
        retained["start"], retained["end"] = self.current[0]["start"], self.current[0]["end"]
        self.baseline[16] = publisher.proposal_row(retained, "prior-run", project_allowlist={})
        prior_source = self.binding["sources"]["prior"]
        for name, value in (("proposals", self.old + self.retained + self.inactive),
                            ("replay_proposals", self.old + self.retained + self.inactive),
                            ("accounting", {"proposals": self.old + self.retained + self.inactive, "allocation": {"evidence": [self.demand(p) for p in self.old + self.retained + self.inactive]}}),
                            ("replay_accounting", {"proposals": self.old + self.retained + self.inactive, "allocation": {"evidence": [self.demand(p) for p in self.old + self.retained + self.inactive]}})):
            prior_source["artifacts"][name] = write(Path(prior_source["artifacts"][name]["path"]), value)
        receipt_path = Path(prior_source["artifacts"]["receipt"]["path"])
        receipt = json.loads(receipt_path.read_text())
        for name in ("proposals", "accounting"):
            item = receipt["deterministic_accounting_replay"]["proposals.json" if name == "proposals" else "work-accounting-result.json"]
            item["primary_sha256"] = item["replay_sha256"] = prior_source["artifacts"][name]["sha256"][7:]
        prior_source["artifacts"]["receipt"] = write(receipt_path, receipt)
        self.binding["sheet_capture"] = write(self.root / "capture.json", {"spreadsheet_id": "sheet", "sheet_title": "August 2026 review", "rows": self.baseline})
        write(self.binding_path, self.binding)
        gateway = SelectionGateway([publisher.HEADER, *self.baseline])
        self.publish(gateway)
        self.assertEqual(self.baseline[16], gateway.rows[17])
        new = {row[0]: row for row in gateway.rows[-13:]}
        warnings = json.loads(new[publisher.stable_review_id(self.current[0])][12])
        self.assertIn("review_proposal_overlap", {warning["type"] for warning in warnings})
        self.assertIn("pending_review_replacement", {warning["type"] for warning in warnings})

    def test_unrelated_rows_and_grid_growth_are_not_frozen_by_the_capture(self):
        unrelated = publisher.proposal_row(self.proposal("other-date", 70, "other-date"), "other-run", project_allowlist={})
        unrelated[9], unrelated[13], unrelated[14] = "approved", "posted", "unrelated human decision"
        gateway = SelectionGateway([publisher.HEADER, *self.baseline, unrelated], row_count=2000)
        self.publish(gateway)
        self.assertEqual(unrelated, gateway.rows[45])

    def test_readable_reason_projection_preserves_other14_cells_and_repeat_is_zero(self):
        from scripts import clockify_pending_review_selection as consumer
        verified = consumer.verify(bindings_path=self.binding_path, source_dir=self.current_dir, proposals=self.current,
                                   spreadsheet_id="sheet", sheet_title="August 2026 review", run_id=self.current_dir.name,
                                   project_allowlist={})
        native_rows = {row[0]: row for row in verified["rows"]}
        reasons = {review_id: "Revizuire necesară: alocare nativă, neînregistrată."
                   for review_id in self.binding["selected_current_ids"]}
        self.binding["reason_projection"] = write(self.root / "readable-reasons.json", reasons)
        write(self.binding_path, self.binding)
        gateway = SelectionGateway([publisher.HEADER, *self.baseline])
        try:
            first = self.publish(gateway)
        except publisher.PublicationError as exc:
            self.fail(f"native consumer cannot yet accept the immutable readable reason projection: {exc.__cause__}")
        for row in gateway.rows[-13:]:
            self.assertEqual(reasons[row[0]], row[12])
            self.assertEqual([native_rows[row[0]][i] for i in range(15) if i != 12],
                             [row[i] for i in range(15) if i != 12])
        receipt = first["pending_selection"]
        self.assertEqual(self.binding["reason_projection"], receipt["reason_projection"])
        self.assertEqual(set(reasons), set(receipt["native_review_warnings"]))
        self.assertTrue(all(receipt["native_review_warnings"].values()))
        before = copy.deepcopy(gateway.rows)
        again = self.publish(gateway)
        self.assertEqual(before, gateway.rows)
        self.assertEqual(first["pending_selection"], again["pending_selection"])
        self.assertEqual(0, again["publications"][0]["appended"])
        self.assertEqual(0, again["publications"][0]["updated"])
        self.assertEqual(0, again["terminal_updates"])

    def test_reason_projection_requires_exact_ids_nonempty_text_and_immutable_bytes(self):
        ids = self.binding["selected_current_ids"]
        for mode in ("missing", "extra", "empty", "nonstring", "changed-bytes"):
            with self.subTest(mode=mode):
                reasons = {review_id: "Revizuire necesară." for review_id in ids}
                if mode == "missing":
                    reasons.pop(ids[0])
                elif mode == "extra":
                    reasons["unselected"] = "Nu este selectată."
                elif mode == "empty":
                    reasons[ids[0]] = " "
                elif mode == "nonstring":
                    reasons[ids[0]] = {"text": "Nu este șir."}
                binding = copy.deepcopy(self.binding)
                reasons_path = self.root / "readable-reasons.json"
                binding["reason_projection"] = write(reasons_path, reasons)
                write(self.binding_path, binding)
                if mode == "changed-bytes":
                    reasons[ids[0]] = "Schimbată după legare."
                    write(reasons_path, reasons)
                gateway = SelectionGateway([publisher.HEADER, *self.baseline])
                before = copy.deepcopy(gateway.rows)
                with self.assertRaises(publisher.PublicationError):
                    self.publish(gateway)
                self.assertEqual(before, gateway.rows)
                self.assertEqual([], gateway.prepared)
                self.assertEqual([], gateway.updated)
        write(self.binding_path, self.binding)

    def test_cli_verifies_full_source_then_seals_optional_consumer_receipt(self):
        args = ["--spreadsheet-id", "sheet", "--sheet-title", "August 2026 review",
                "--proposals", str(self.current_dir / "proposals.json"),
                "--quality-report", str(self.current_dir / "quality_report.json"),
                "--replay-integrity", str(self.root / "current-replay" / "replay-integrity.json"),
                "--routing-snapshot", str(self.current_dir / "routing.json"), "--run-id", self.current_dir.name,
                "--pending-review-selection", str(self.binding_path)]
        with mock.patch.object(publisher, "GwsSheetsGateway") as construct, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(0, publisher.main(args))
            construct.assert_not_called()
        preview = json.loads(output.getvalue())
        self.assertEqual(21, preview["rows"])
        self.assertEqual(34, preview["selected_rows"])
        gateway = SelectionGateway([publisher.HEADER, *self.baseline])
        result_path = self.root / "publication.json"
        with mock.patch.object(publisher, "GwsSheetsGateway", return_value=gateway), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(0, publisher.main([*args, "--enable-write", "--result-output", str(result_path)]))
        receipt = json.loads(result_path.read_text())["pending_selection"]
        self.assertEqual(self.binding_path.as_posix(), receipt["selection"]["path"])
        from scripts import clockify_pending_review_selection as consumer
        expected = receipt.pop("acceptance_sha256")
        self.assertEqual(expected, consumer.digest(receipt))
        self.assertEqual(34, receipt["saved_credit_rows"])
        self.assertEqual("saved_native_pending_credits", receipt["verification_basis"])


if __name__ == "__main__":
    unittest.main()
