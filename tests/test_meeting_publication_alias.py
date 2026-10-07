"""Offline prospective row-representation proofs, never posting credits."""
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import unittest

from scripts import clockify_sheet_publish as publisher
from scripts import clockify_review_cycle as cycle
from scripts import evidence_ledger, work_accounting_pipeline as pipeline
from test_sheet_publish import StatefulGateway


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def handle(path):
    return {"path": str(path), "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}


class MeetingPublicationAliasTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.route = {"project_name": "Serenichron Level 2", "project_suffix": "775f9f",
                      "tag_suffixes": [], "tag_names": [], "prefix": "SC", "billable": False}
        self.routing = {"workspace_id": "workspace", "member_id": "member",
                        "session_routes": [{"source_id": "fixture", **self.route}]}
        self.old_dir, self.old = self.source("old-run", revision="old")
        self.current_dir, self.current = self.source("current-run", revision="new")
        self.prior_row = publisher.proposal_row(self.old, "old-run")
        # A genuine later capture may have preserved human-readable warning
        # context; this is not an assertion of the historical publisher hash.
        self.prior_row[12] = "Preserved recorded-meeting review context"
        self.prior_row[14] = "Human note survives"
        rows = [publisher.HEADER, self.prior_row]
        capture = {"spreadsheetId": "sheet", "sheets": [{
            "properties": {"title": "August 2026 review", "sheetId": 2},
            "data": [{"rowData": [{"values": [{"effectiveValue": {
                "numberValue" if type(value) in (int, float) else "stringValue": value,
            }} for value in row]} for row in rows]}],
        }]}
        self.capture_path = self.root / "sheet-capture.json"
        write(self.capture_path, capture)
        write(self.old_dir / "routing.json", self.routing)
        self.binding = {
            "schema_version": "meeting-publication-bindings/v1",
            "spreadsheet_id": "sheet", "sheet_title": "August 2026 review",
            "bindings": [{"prior_run_id": "old-run",
                          "review_ids": [publisher.stable_review_id(self.old)],
                          "representation_verified_at": "2026-08-02T12:00:00Z",
                          "artifacts": {"proposals": handle(self.old_dir / "proposals.json"),
                                        "source_ledger": handle(self.old_dir / "evidence/evidence-ledger.json"),
                                        "routing": handle(self.old_dir / "routing.json"),
                                        "sheet_capture": handle(self.capture_path)}}],
        }
        self.binding_path = self.root / "bindings.json"
        write(self.binding_path, self.binding)

    def source(self, name, *, revision, recording="recording-one", end="2026-08-01T10:00:03Z", segment=1):
        source = evidence_ledger.evidence_event(
            "fathom", {"source_id": recording, "machine": "fathom"},
            observed_at="2026-08-01T12:00:00Z",
            raw_source_span={"start": "2026-08-01T09:00:01Z", "end": end},
            attributes={"recording_id": recording, "title": "Same title", "summary": revision,
                        "source_digest": "sha256:" + hashlib.sha256(revision.encode()).hexdigest()},
        )
        ledger = evidence_ledger.EvidenceLedger((source,), timezone="Europe/Bucharest")
        document = {"schema_version": "evidence-ledger/v1", "manifest": ledger.manifest.document(),
                    "events": [source.document()]}
        recordings, errors = pipeline._recording_events(document["events"], document["manifest"])
        self.assertEqual([], errors)
        meeting = recordings[0]["meeting"]
        proposal = pipeline._proposal(
            {"activity_id": "activity-" + revision, "workstream_id": "workstream-" + revision,
             "semantic_confidence": "high"}, self.route, "SC — Attended Same title",
            dt.datetime.fromisoformat("2026-08-01T09:00:01+00:00"),
            dt.datetime.fromisoformat(end.replace("Z", "+00:00")), [source.evidence_id], segment,
        )
        proposal["provenance"]["canonical_meeting_id"] = meeting.canonical_id
        root = self.root / name
        write(root / "proposals.json", [proposal])
        write(root / "evidence/evidence-ledger.json", document)
        return root, proposal

    def publish(self, gateway, *, bindings=True):
        kwargs = dict(spreadsheet_id="sheet", sheet_title="August 2026 review",
                      template_title="Proposals", proposals=[self.current],
                      run_id=self.current_dir.name, project_allowlist={})
        if bindings:
            kwargs.update(meeting_bindings=self.binding_path, source_dir=self.current_dir)
        try:
            return publisher.publish_proposal_partitions(gateway, **kwargs)
        except TypeError as exc:
            if "unexpected keyword argument" in str(exc):
                self.fail("missing prospective meeting alias publication path")
            raise

    def test_changed_digest_and_activity_keep_the_genuinely_captured_old_row(self):
        self.assertNotEqual(self.old["review_activity_key"], self.current["review_activity_key"])
        self.assertNotEqual(self.old["provenance"]["canonical_meeting_id"],
                            self.current["provenance"]["canonical_meeting_id"])
        gateway = StatefulGateway([publisher.HEADER, self.prior_row])
        before = copy.deepcopy(gateway.rows)
        result = self.publish(gateway)
        self.assertEqual(before, gateway.rows)
        self.assertEqual(0, result["publications"][0]["appended"])
        aliases = result["publications"][0]["meeting_aliases"]
        self.assertEqual(1, len(aliases))
        self.assertEqual("current_sheet_capture", aliases[0]["verification_basis"])
        self.assertEqual("2026-08-02T12:00:00Z", aliases[0]["representation_verified_at"])
        self.assertEqual(publisher.stable_review_id(self.current), aliases[0]["current_review_id"])
        self.assertEqual(publisher.stable_review_id(self.old), aliases[0]["retained_review_id"])

    def test_distinct_recording_same_title_and_time_appends(self):
        self.current_dir, self.current = self.source("other-run", revision="new", recording="recording-two")
        gateway = StatefulGateway([publisher.HEADER, self.prior_row])
        result = self.publish(gateway)
        self.assertEqual(3, len(gateway.rows))
        self.assertEqual(1, result["publications"][0]["appended"])

    def test_changed_exact_interval_or_split_does_not_alias(self):
        for options in ({"end": "2026-08-01T10:00:04Z"}, {"segment": 2}):
            with self.subTest(options=options):
                self.current_dir, self.current = self.source("different-run", revision="new", **options)
                gateway = StatefulGateway([publisher.HEADER, self.prior_row])
                self.publish(gateway)
                self.assertEqual(3, len(gateway.rows))

    def test_tampered_artifact_or_capture_binding_fails_without_writes(self):
        for mutation in ("artifact", "run", "destination", "timestamp", "selection"):
            with self.subTest(mutation=mutation):
                binding = copy.deepcopy(self.binding)
                if mutation == "artifact":
                    binding["bindings"][0]["artifacts"]["proposals"]["sha256"] = "sha256:" + "0" * 64
                elif mutation == "run":
                    binding["bindings"][0]["prior_run_id"] = "wrong-run"
                elif mutation == "destination":
                    binding["sheet_title"] = "Other review"
                elif mutation == "timestamp":
                    binding["bindings"][0]["representation_verified_at"] = "2026-08-02T12:00:00"
                else:
                    binding["bindings"][0]["review_ids"] = ["wka-missing-s01"]
                write(self.binding_path, binding)
                gateway = StatefulGateway([publisher.HEADER, self.prior_row])
                before = copy.deepcopy(gateway.rows)
                with self.assertRaises(publisher.PublicationError):
                    self.publish(gateway)
                self.assertEqual(before, gateway.rows)

    def test_live_alias_row_drift_or_absence_fails_before_any_write(self):
        for mode in ("reason", "absent"):
            with self.subTest(mode=mode):
                prior = list(self.prior_row)
                prior[12] = "Changed after capture"
                gateway = StatefulGateway([publisher.HEADER, prior] if mode == "reason" else [publisher.HEADER])
                before = copy.deepcopy(gateway.rows)
                with self.assertRaises(publisher.PublicationError):
                    self.publish(gateway)
                self.assertEqual(before, gateway.rows)

    def test_no_binding_keeps_existing_publication_behavior(self):
        gateway = StatefulGateway([publisher.HEADER, self.prior_row])
        result = self.publish(gateway, bindings=False)
        self.assertEqual(3, len(gateway.rows))
        self.assertNotIn("meeting_aliases", result["publications"][0])

    def test_binding_reuses_a_future_run_and_preserves_new_human_decisions(self):
        self.current_dir, self.current = self.source("future-run", revision="future")
        live = list(self.prior_row)
        live[9], live[13], live[14] = "modify", "reviewing", "New human note"
        gateway = StatefulGateway([publisher.HEADER, live])
        self.publish(gateway)
        self.assertEqual(live, gateway.rows[1])
        self.assertEqual(2, len(gateway.rows))

    def test_ambiguous_selected_binding_and_missing_capture_fail_without_writes(self):
        for mode in ("ambiguous", "missing"):
            with self.subTest(mode=mode):
                binding = copy.deepcopy(self.binding)
                if mode == "ambiguous":
                    binding["bindings"].append(copy.deepcopy(binding["bindings"][0]))
                else:
                    binding["bindings"][0]["artifacts"]["sheet_capture"]["path"] = str(self.root / "missing.json")
                write(self.binding_path, binding)
                gateway = StatefulGateway([publisher.HEADER, self.prior_row])
                with self.assertRaises(publisher.PublicationError):
                    self.publish(gateway)
                self.assertEqual([publisher.HEADER, self.prior_row], gateway.rows)

    def test_dry_run_validates_alias_proof_without_constructing_a_gateway(self):
        write(self.current_dir / "routing.json", self.routing)
        write(self.current_dir / "quality.json", {"status": "pass", "summary": {"total_proposals": 1}})
        write(self.current_dir / "replay.json", {"status": "pass", "failures": [],
                                                 "source_run_id": self.current_dir.name,
                                                 "reconciliation_binding": {"routing_sha256": handle(self.current_dir / "routing.json")["sha256"]}})
        args = ["--spreadsheet-id", "sheet", "--sheet-title", "August 2026 review",
                "--proposals", str(self.current_dir / "proposals.json"),
                "--quality-report", str(self.current_dir / "quality.json"),
                "--replay-integrity", str(self.current_dir / "replay.json"),
                "--routing-snapshot", str(self.current_dir / "routing.json"),
                "--run-id", self.current_dir.name,
                "--meeting-publication-bindings", str(self.binding_path)]
        command = [sys.executable, str(Path(publisher.__file__).resolve()), *args]
        result = subprocess.run(command, capture_output=True, text=True, cwd=self.root)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(1, json.loads(result.stdout).get("meeting_aliases"))
        binding = copy.deepcopy(self.binding)
        binding["bindings"][0]["prior_run_id"] = "wrong-run"
        write(self.binding_path, binding)
        result = subprocess.run(command, capture_output=True, text=True, cwd=self.root)
        self.assertNotEqual(0, result.returncode)
        self.assertNotIn('"status": "dry_run"', result.stdout)

    def test_cycle_reconstructs_alias_and_rejects_forged_receipt(self):
        write(self.current_dir / "routing.json", self.routing)
        write(self.current_dir / "work-accounting-result.json", {
            "schema_version": 1, "allocation_mode": "non_overlapping_v1",
            "proposals": [self.current], "ambiguous": [], "skipped": [],
        })
        config = {"spreadsheet_id": "sheet", "meeting_publication_bindings": str(self.binding_path)}
        expected = cycle._expected_publication_receipts(
            config, {"run_dir": str(self.current_dir), "run_id": self.current_dir.name},
            sheet_title="August 2026 review", publication_profile=None,
        )
        result = self.publish(StatefulGateway([publisher.HEADER, self.prior_row]))
        document = {"status": "published", "external_writes": True, **result}
        verified = cycle._validated_publication_document(document, expected, source_dir=self.current_dir)
        self.assertEqual([publisher.stable_review_id(self.old)], verified[0]["row_ids"])
        forged = copy.deepcopy(document)
        forged["publications"][0]["meeting_aliases"][0]["retained_review_id"] = "wka-forged-s01"
        with self.assertRaises(cycle.CycleError):
            cycle._validated_publication_document(forged, expected, source_dir=self.current_dir)

    def test_cycle_uses_binding_only_for_matching_month_and_preserves_old_receipts(self):
        config = {"root": str(self.root), "spreadsheet_id": "sheet",
                  "meeting_publication_bindings": str(self.binding_path)}
        stage = {"run_dir": str(self.current_dir), "run_id": self.current_dir.name}
        command = cycle._publisher_command(config, stage, stage,
            sheet_title="September 2026 review", result_path=self.root / "result.json")
        self.assertNotIn("--meeting-publication-bindings", command)
        matching = cycle._publisher_command(config, stage, stage,
            sheet_title="August 2026 review", result_path=self.root / "result.json")
        self.assertIn("--meeting-publication-bindings", matching)
        legacy_config = cycle._receipt_publication_config(config, {"schema_version": "sheet-publication-result/v1"})
        self.assertNotIn("meeting_publication_bindings", legacy_config)

    def test_provider_qualified_id_does_not_alias_a_different_provider(self):
        ledger_path = self.current_dir / "evidence/evidence-ledger.json"
        old = json.loads(ledger_path.read_text())["events"][0]
        from test_work_accounting_pipeline import calendly_event
        attributes = calendly_event(old["raw_source_span"]["start"], old["raw_source_span"]["end"]).document()["attributes"]
        attributes["recording_id"] = "recording-one"
        event = evidence_ledger.evidence_event(
            "calendly", {"source_type": "calendly", "source_id": "recording-one", "meeting_id": attributes["meeting_id"]},
            observed_at=old["raw_source_span"]["start"], raw_source_span=old["raw_source_span"],
            attributes=attributes,
        )
        ledger = evidence_ledger.EvidenceLedger((event,), timezone="Europe/Bucharest")
        document = {"schema_version": "evidence-ledger/v1", "manifest": ledger.manifest.document(),
                    "events": [event.document()]}
        recordings, _ = pipeline._recording_events(document["events"], document["manifest"])
        self.current["provenance"]["canonical_meeting_id"] = recordings[0]["meeting"].canonical_id
        self.current["provenance"]["evidence_ids"] = [event.evidence_id]
        write(ledger_path, document)
        write(self.current_dir / "proposals.json", [self.current])
        gateway = StatefulGateway([publisher.HEADER, self.prior_row])
        self.publish(gateway)
        self.assertEqual(3, len(gateway.rows))

    def test_duplicate_current_rows_and_distinct_split_count_do_not_collapse(self):
        gateway = StatefulGateway([publisher.HEADER, self.prior_row])
        with self.assertRaises(publisher.PublicationError):
            publisher.publish_proposal_partitions(
                gateway, spreadsheet_id="sheet", sheet_title="August 2026 review", template_title="Proposals",
                proposals=[self.current, self.current], run_id=self.current_dir.name, project_allowlist={},
                meeting_bindings=self.binding_path, source_dir=self.current_dir,
            )
        self.assertEqual([publisher.HEADER, self.prior_row], gateway.rows)
        sibling = copy.deepcopy(self.current)
        sibling["allocation_segment"] = 2
        write(self.current_dir / "proposals.json", [self.current, sibling])
        self.publish(gateway)
        self.assertEqual(3, len(gateway.rows))

    def test_unresolved_current_meeting_keeps_existing_routed_capture_destination(self):
        self.current.update({"routing_disposition": "unresolved-routing", "client_project": "",
                             "clockify_project_suffix": "", "tag_names": [], "tag_suffixes": [],
                             "billable": False, "review_warnings": [{"type": "unresolved_routing",
                             "disposition": "unresolved-routing", "reason_code": "no_deterministic_route"}]})
        write(self.current_dir / "proposals.json", [self.current])
        # A new unresolved proposal must not become another duration row merely
        # because the old recording's route was resolved in its real capture.
        from test_sheet_publish import MultiSheetGateway
        gateway = MultiSheetGateway()
        gateway.sheets["August 2026 review"] = {"sheet_id": 2, "rows": [publisher.HEADER, self.prior_row]}
        before = copy.deepcopy(gateway.sheets)
        result = self.publish(gateway)
        self.assertEqual(before, gateway.sheets)
        self.assertEqual(["August 2026 review"], [p["sheet_title"] for p in result["publications"]])
        write(self.current_dir / "routing.json", self.routing)
        write(self.current_dir / "work-accounting-result.json", {
            "schema_version": 1, "allocation_mode": "non_overlapping_v1",
            "proposals": [self.current], "ambiguous": [], "skipped": [],
        })
        expected = cycle._expected_publication_receipts(
            {"spreadsheet_id": "sheet", "meeting_publication_bindings": str(self.binding_path)},
            {"run_dir": str(self.current_dir), "run_id": self.current_dir.name},
            sheet_title="August 2026 review", publication_profile=None,
        )
        verified = cycle._validated_publication_document(
            {"status": "published", "external_writes": True, **result}, expected,
            source_dir=self.current_dir,
        )
        self.assertEqual([publisher.stable_review_id(self.old)], verified[0]["row_ids"])


if __name__ == "__main__":
    unittest.main()
