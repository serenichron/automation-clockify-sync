"""Native posts publish replayable original meeting proof, never alias credit."""
import contextlib
import copy
import datetime as dt
import hashlib
import io
import inspect
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import clockify_native_sheet_post as native
from scripts import clockify_review_batch_publication as prepared
from scripts import clockify_sheet_publish as publisher, evidence_ledger
from test_clockify_native_sheet_post import FakeGateway, capture, route
import test_review_batch_auto_preparation as automatic_fixtures


NOW = dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc)


class NativeCreditPostProducerTests(unittest.TestCase):
    def setUp(self):
        self.fixture = automatic_fixtures.AutomaticPreparationTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root, self.run = self.fixture.root, self.fixture.run
        self.sources = self.fixture.sources
        events = []
        for index, source in enumerate(self.sources):
            event = evidence_ledger.evidence_event("fathom",
                {"source_type": "fathom", "source_id": f"recording-{index}"},
                observed_at=source["start"], raw_source_span={"start": source["start"], "end": source["end"]},
                attributes={"share_url": f"https://fathom.video/share/recording-{index}",
                            "description": "PRIVATE RAW EVIDENCE"})
            events.append(event)
            source["provenance"]["evidence_ids"] = [event.evidence_id]
            self.fixture.analysis["activities"][index]["evidence_ids"] = [event.evidence_id]
        ledger = evidence_ledger.EvidenceLedger(tuple(events))
        self.ledger = {"schema_version": ledger.manifest.schema_version,
            "manifest": ledger.manifest.document(), "events": [event.document() for event in events]}
        (self.run / "evidence").mkdir()
        self.fixture.write(self.run / "evidence/evidence-ledger.json", self.ledger)
        self.fixture.write(self.run / "proposals.json", self.sources)
        self.fixture.write(self.run / "semantic-analysis.json", self.fixture.analysis)
        self.fixture.write(self.run / "work-accounting-result.json", {"schema_version": 1,
            "allocation_mode": "non_overlapping_v1", "proposals": self.sources, "ambiguous": [], "skipped": []})
        self.output = self.root / "native-credit-inputs"

    def preparation(self, *, credit=True, output=None):
        self.assertIn("native_credit_directory", inspect.signature(prepared.prepare).parameters,
            "new preparations must bind their original native-credit source")
        kwargs = {"native_credit_directory": self.output} if credit else {}
        prepared.prepare(source_dir=self.run, output=output or self.root / "prepared.json", run_id="source-run",
            spreadsheet_id="sheet-1", sheet_title="September 2026 portfolio review", timezone="Europe/Bucharest", **kwargs)
        return prepared.load(output or self.root / "prepared.json")[0]

    def plan(self, preparation=None):
        preparation = preparation or self.preparation()
        sheet = capture(*(publisher.portfolio_row(g, "source-run") for g in preparation["package"]["activities"]))
        self.fixture.write(self.root / "capture.json", sheet)
        self.fixture.write(self.root / "routing.json", route(suffix="abc123"))
        gateway = FakeGateway()
        gateway.timeout_seconds = 45
        with mock.patch.object(native, "_gateway", return_value=(gateway, "workspace-1", "user-1", "test")), \
                mock.patch.object(native.legacy, "_paged", side_effect=lambda path, *args, **kwargs:
                    [{"id": "project-abc123"}] if "projects" in path else [{"id": "tag-def456"}]), \
                contextlib.redirect_stdout(io.StringIO()) as stdout:
            routing = route(suffix="abc123")
            routing["session_routes"][0].update(tag_names=["Development"], tag_suffixes=["def456"])
            self.fixture.write(self.root / "routing.json", routing)
            status = native.main(["plan", "--sheet-capture", str(self.root / "capture.json"),
                "--expected-capture-sha256", hashlib.sha256((self.root / "capture.json").read_bytes()).hexdigest(),
                "--routing", str(self.root / "routing.json"), "--review-batches", str(self.root / "prepared.json"),
                "--output", str(self.root / "plan.json")])
        self.assertEqual(0, status)
        self.assertNotIn("PRIVATE RAW EVIDENCE", stdout.getvalue())
        return json.loads((self.root / "plan.json").read_text())

    def execute(self, plan, gateway=None):
        approval = native.approval_template(plan, approval_id="board", approver="human board user",
            approved_at="2026-10-04T10:00:00Z", expires_at="2026-10-05T10:00:00Z")
        gateway = gateway or FakeGateway()
        receipt = native.execute_plan(plan, approval, self.root / "events.jsonl", self.root / "receipt.json", gateway, now=NOW)
        return receipt, gateway

    def test_original_binding_cli_post_emits_verified_v1_packet_and_replays_without_post(self):
        from scripts import clockify_source_adoptions as adoptions
        plan = self.plan()
        self.assertEqual(str(self.output), plan["native_credit_source"]["output_directory"])
        receipt, gateway = self.execute(plan)
        path = self.output / plan["plan_digest"] / "packet.json"
        packet_bytes = path.read_bytes()
        packet = json.loads(packet_bytes)
        self.assertEqual("clockify-native-credit-input/v1", packet["schema_version"])
        self.assertEqual(2, len(packet["posted"]))
        self.assertNotIn("PRIVATE RAW EVIDENCE", packet_bytes.decode())
        for item in packet["posted"]:
            proof = adoptions.verify_prior_native_proof(item["artifacts"], item["prior_review_id"], item["clockify_entry_id"],
                workspace_id="workspace-1", member_id="user-1")
            self.assertEqual("fathom", adoptions._native_meeting_identity(proof["source_events"], proof["prior_proposal"])[0])
            self.assertEqual(str(self.run / "proposals.json"), item["artifacts"]["prior_proposals"]["path"])
            self.assertEqual(receipt, json.loads(adoptions._capture(item["receipt"], {})))
        self.assertEqual(receipt, self.execute(plan, gateway)[0])
        self.assertEqual(2, len(gateway.posts))
        self.assertEqual(packet_bytes, path.read_bytes())

    def test_existing_preparation_retains_bytes_when_new_credit_destination_is_supplied(self):
        self.preparation(credit=False)
        before = (self.root / "prepared.json").read_bytes()
        document = self.preparation()
        self.assertNotIn("native_credit_source", document)
        self.assertEqual(before, (self.root / "prepared.json").read_bytes())

    def test_original_legacy_publication_binding_plans_without_credit_side_effect(self):
        document = self.preparation(credit=False)
        legacy = {key: document[key] for key in prepared.FIELDS}
        legacy["schema_version"] = prepared.SCHEMA
        self.fixture.write(self.root / "prepared.json", legacy)
        before = (self.root / "prepared.json").read_bytes()
        plan = self.plan(legacy)
        self.assertNotIn("native_credit_source", plan)
        self.execute(plan)
        self.assertEqual(before, (self.root / "prepared.json").read_bytes())
        self.assertFalse(self.output.exists())

    def test_sidecar_failure_retry_uses_confirmed_posts(self):
        plan, gateway = self.plan(), FakeGateway()
        from scripts import clockify_native_credit_history as history
        with mock.patch.object(history, "emit_posted_packet", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.execute(plan, gateway)
        self.assertEqual(2, len(gateway.posts))
        self.execute(plan, gateway)
        self.assertEqual(2, len(gateway.posts))
        self.assertTrue((self.output / plan["plan_digest"] / "packet.json").is_file())

    def test_tampered_original_rejects_before_gateway(self):
        plan = self.plan()
        self.fixture.write(self.run / "proposals.json", [])
        gateway = mock.Mock()
        with self.assertRaisesRegex(ValueError, "drifted"):
            self.execute(plan, gateway)
        gateway.verify_target.assert_not_called()

    def test_symlink_destination_rejected_before_gateway(self):
        plan = self.plan()
        real = self.root / "foreign"
        real.mkdir()
        self.output.symlink_to(real, target_is_directory=True)
        gateway = mock.Mock()
        with self.assertRaises(ValueError):
            self.execute(plan, gateway)
        gateway.verify_target.assert_not_called()

    def test_changed_native_interval_cannot_claim_original_recording(self):
        plan = self.plan()
        plan["entries"][0]["payload"].update(start="2026-08-01T12:00:07Z", end="2026-08-01T12:01:18Z")
        plan["entries"][0]["payload_digest"] = native._digest(plan["entries"][0]["payload"])
        # Retained rows also need this invariant, not just grouped children.
        plan.pop("review_batches_binding")
        for item in plan["entries"]:
            item.pop("sheet_review_id", None)
        plan["plan_digest"] = native._document_digest(plan, "plan_digest")
        gateway = FakeGateway()
        with self.assertRaisesRegex(ValueError, "original.*interval|source.*payload"):
            self.execute(plan, gateway)
        self.assertEqual([], gateway.posts)

    def test_nonmeeting_sources_do_not_emit_packet(self):
        self.ledger["events"][0]["source_type"] = "repository_events"
        # Replace rather than mutate sealed events so their IDs stay valid.
        events = []
        for index, source in enumerate(self.sources):
            event = evidence_ledger.evidence_event("repository_events", {"source_id": str(index)},
                observed_at=source["start"], raw_source_span={"start": source["start"], "end": source["end"]},
                attributes={"description": "PRIVATE RAW EVIDENCE"})
            events.append(event)
            source["provenance"]["evidence_ids"] = [event.evidence_id]
            self.fixture.analysis["activities"][index]["evidence_ids"] = [event.evidence_id]
        ledger = evidence_ledger.EvidenceLedger(tuple(events))
        self.fixture.write(self.run / "evidence/evidence-ledger.json", {"schema_version": ledger.manifest.schema_version,
            "manifest": ledger.manifest.document(), "events": [event.document() for event in events]})
        self.fixture.write(self.run / "proposals.json", self.sources)
        self.fixture.write(self.run / "semantic-analysis.json", self.fixture.analysis)
        self.fixture.write(self.run / "work-accounting-result.json", {"schema_version": 1,
            "allocation_mode": "non_overlapping_v1", "proposals": self.sources, "ambiguous": [], "skipped": []})
        plan = self.plan()
        _, gateway = self.execute(plan)
        self.assertEqual(2, len(gateway.posts))
        self.assertFalse(self.output.exists())

    def test_existing_packet_drift_is_not_overwritten_on_retry(self):
        plan = self.plan()
        _, gateway = self.execute(plan)
        path = self.output / plan["plan_digest"] / "packet.json"
        path.write_text("tampered")
        with self.assertRaisesRegex(ValueError, "immutable snapshot differs"):
            self.execute(plan, gateway)
        self.assertEqual("tampered", path.read_text())
        self.assertEqual(2, len(gateway.posts))

    def test_no_work_source_still_prepares_complete_empty_binding(self):
        self.fixture.write(self.run / "proposals.json", [])
        self.fixture.write(self.run / "semantic-analysis.json", {"activities": []})
        self.fixture.write(self.run / "work-accounting-result.json", {"schema_version": 1,
            "allocation_mode": "non_overlapping_v1", "proposals": [], "ambiguous": [], "skipped": []})
        document = self.preparation()
        self.assertEqual("complete", document["preparation_outcome"]["status"])
        self.assertEqual([], document["package"]["activities"])

    def test_interrupted_snapshot_write_does_not_poison_retry(self):
        from scripts import clockify_native_credit_history as history
        plan, gateway = self.plan(), FakeGateway()
        real_fdopen = history.os.fdopen

        class InterruptedWriter:
            def __init__(self, descriptor, mode):
                self.handle = real_fdopen(descriptor, mode)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                self.handle.close()

            def write(self, content):
                self.handle.write(content[:10])
                raise OSError("interrupted snapshot write")

        with mock.patch.object(history.os, "fdopen", InterruptedWriter):
            with self.assertRaisesRegex(OSError, "interrupted snapshot write"):
                self.execute(plan, gateway)
        self.assertFalse((self.output / plan["plan_digest"] / "packet.json").exists())
        self.execute(plan, gateway)
        self.assertEqual(2, len(gateway.posts))
        self.assertTrue((self.output / plan["plan_digest"] / "packet.json").is_file())

    def standalone_plan(self, binding, rows, routing=None):
        return native.build_plan(capture(*rows), capture_sha256="c" * 64,
            routing=routing or route(), routing_sha256="r" * 64,
            timezone="Europe/Bucharest", workspace_id="workspace-1", member_id="user-1",
            projects=[{"id": "project-123456"}], tags=[{"id": "tag-654321"}], live_entries=[],
            native_credit_source=binding)

    def test_approved_route_and_tag_correction_remains_original_meeting_proof(self):
        preparation = self.preparation()
        rows = [publisher.proposal_row(source, "source-run", project_allowlist={}) for source in self.sources]
        for item in rows:
            item[4], item[5] = "Corrected project", "Process"
        plan = self.standalone_plan(preparation["native_credit_source"], rows,
            routing=route(project="Corrected project"))
        self.execute(plan)
        from scripts import clockify_source_adoptions as adoptions
        packet = json.loads((self.output / plan["plan_digest"] / "packet.json").read_bytes())
        proof = adoptions.verify_prior_native_proof(packet["posted"][0]["artifacts"],
            packet["posted"][0]["prior_review_id"], packet["posted"][0]["clockify_entry_id"],
            workspace_id="workspace-1", member_id="user-1")
        self.assertEqual("abc123", proof["prior_proposal"]["clockify_project_suffix"])
        self.assertEqual("project-123456", proof["payload"]["projectId"])
        self.assertEqual(["tag-654321"], proof["payload"]["tagIds"])

    def test_same_batch_source_alias_does_not_become_original_post_packet_row(self):
        first, second = self.sources
        second.update(start=first["start"], end=first["end"], duration_seconds=first["duration_seconds"],
            duration_minutes=first["duration_minutes"])
        second["provenance"]["evidence_ids"] = first["provenance"]["evidence_ids"][:]
        self.fixture.write(self.run / "proposals.json", self.sources)
        binding = {"source_proposals": prepared.artifact_handle(self.run / "proposals.json"),
            "source_ledger": prepared.artifact_handle(self.run / "evidence/evidence-ledger.json"),
            "output_directory": str(self.output)}
        rows = [publisher.proposal_row(source, "source-run", project_allowlist={}) for source in self.sources]
        for item in rows:
            item[4], item[5] = "Example", "Process"
        plan = self.standalone_plan(binding, rows)
        receipt, gateway = self.execute(plan)
        self.assertEqual(["created", "same_batch_payload_credit"], [row["disposition"] for row in receipt["entries"]])
        self.assertEqual(1, len(gateway.posts))
        packet = json.loads((self.output / plan["plan_digest"] / "packet.json").read_bytes())
        self.assertEqual([publisher.stable_review_id(first)], [item["prior_review_id"] for item in packet["posted"]])

    @staticmethod
    def add_unrelated_entry(gateway):
        entry = copy.deepcopy(gateway.entries[0])
        entry.update(id=f"unrelated-{len(gateway.entries)}", description="Unrelated work posted later")
        entry["timeInterval"] = {"start": "2026-08-01T07:05:00Z", "end": "2026-08-01T07:06:00Z"}
        gateway.entries.append(entry)

    def test_late_packet_failure_retry_keeps_frozen_proof_and_fresh_live_receipt(self):
        from scripts import clockify_native_credit_history as history
        from scripts import clockify_source_adoptions as adoptions
        plan, gateway = self.plan(), FakeGateway()
        write = history._write

        def fail_packet(path, content):
            if path.name == "packet.json":
                raise OSError("late packet write failure")
            return write(path, content)

        with mock.patch.object(history, "_write", side_effect=fail_packet):
            with self.assertRaisesRegex(OSError, "late packet write failure"):
                self.execute(plan, gateway)
        directory = self.output / plan["plan_digest"]
        frozen_bytes = (directory / "native-receipt.json").read_bytes()
        frozen = json.loads(frozen_bytes)
        self.add_unrelated_entry(gateway)
        fresh, _ = self.execute(plan, gateway)
        self.assertEqual(2, len(gateway.posts))
        self.assertNotEqual(frozen["final_live_snapshot_sha256"], fresh["final_live_snapshot_sha256"])
        self.assertEqual(fresh, json.loads((self.root / "receipt.json").read_bytes()))
        self.assertEqual(frozen_bytes, (directory / "native-receipt.json").read_bytes())
        packet = json.loads((directory / "packet.json").read_bytes())
        self.assertEqual(frozen, json.loads(adoptions._capture(packet["posted"][0]["receipt"], {})))

    def test_successful_packet_bytes_stay_stable_after_unrelated_later_posts(self):
        plan = self.plan()
        first, gateway = self.execute(plan)
        directory = self.output / plan["plan_digest"]
        packet_bytes = (directory / "packet.json").read_bytes()
        self.add_unrelated_entry(gateway)
        fresh, _ = self.execute(plan, gateway)
        self.assertEqual(2, len(gateway.posts))
        self.assertNotEqual(first["final_live_snapshot_sha256"], fresh["final_live_snapshot_sha256"])
        self.assertEqual(packet_bytes, (directory / "packet.json").read_bytes())

    def test_frozen_receipt_critical_drift_remains_fatal_on_retry(self):
        plan = self.plan()
        _, gateway = self.execute(plan)
        snapshot = self.output / plan["plan_digest"] / "native-receipt.json"
        frozen = json.loads(snapshot.read_bytes())
        frozen["entries"][0]["clockify_entry_id"] = "foreign-entry"
        self.fixture.write(snapshot, frozen)
        self.add_unrelated_entry(gateway)
        with self.assertRaises(ValueError):
            self.execute(plan, gateway)
        self.assertEqual(2, len(gateway.posts))
        self.assertEqual("foreign-entry", json.loads(snapshot.read_bytes())["entries"][0]["clockify_entry_id"])

    def test_frozen_receipt_invalid_observation_digest_remains_fatal(self):
        plan = self.plan()
        _, gateway = self.execute(plan)
        snapshot = self.output / plan["plan_digest"] / "native-receipt.json"
        frozen = json.loads(snapshot.read_bytes())
        frozen["final_live_snapshot_sha256"] = "not-a-snapshot-digest"
        self.fixture.write(snapshot, frozen)
        self.add_unrelated_entry(gateway)
        with self.assertRaises(ValueError):
            self.execute(plan, gateway)
        self.assertEqual(2, len(gateway.posts))


if __name__ == "__main__":
    unittest.main()
