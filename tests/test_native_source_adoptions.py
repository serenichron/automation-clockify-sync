"""Explicit audited source adoption, never temporal/wording-based deduplication."""
from __future__ import annotations

import contextlib
import copy
import datetime as dt
import hashlib
import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_clockify_native_sheet_post as fixtures
from scripts import clockify_native_sheet_post as native, evidence_ledger


NOW = dt.datetime(2026, 10, 4, 11, tzinfo=dt.timezone.utc)


def artifact(path: Path, value: object) -> dict:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
    return {"path": str(path), "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()}


class UniqueGateway(fixtures.FakeGateway):
    def create(self, payload):
        entry = super().create(payload)
        entry["id"] = f"created-{len(self.entries)}"
        return entry


class ManualMeetingCreditTests(unittest.TestCase):
    _plan = fixtures.NativeSheetPostTests._plan
    _approval = fixtures.NativeSheetPostTests._approval
    adopted_plan = lambda self, document, gateway, snapshot: SourceAdoptionTests.adopted_plan(self, document, gateway, snapshot)
    execute = lambda self, root, plan, gateway: SourceAdoptionTests.execute(self, root, plan, gateway)

    def proof(self, root, *, independent=False):
        from scripts import clockify_source_adoptions as adoptions, work_accounting_pipeline as pipeline, review_corrections
        event = evidence_ledger.evidence_event(
            "fathom", {"source_type": "fathom", "source_id": "example-recording-1"},
            observed_at="2026-09-29T10:32:00Z",
            raw_source_span={"start": "2026-09-29T10:32:00Z", "end": "2026-09-29T11:30:00Z"},
            attributes={"title": "Example meeting", "recorded_by_email": "member@example.invalid",
                        "calendar_invitees": [{"email": "member@example.invalid"}]},
        )
        ledger = evidence_ledger.EvidenceLedger((event,), member_identities=("member@example.invalid",))
        ledger_document = {"schema_version": ledger.manifest.schema_version,
                           "manifest": ledger.manifest.document(), "events": [event.document()]}
        meetings, errors = pipeline._recording_events(ledger_document["events"], ledger_document["manifest"])
        self.assertEqual([], errors)
        meeting_id = meetings[0]["meeting"].canonical_id
        proposal = {"activity_id": "act-0123456789abcdef01234567", "candidate_key": "candidate-meeting",
                    "review_activity_key": "wka-manual", "allocation_segment": 1,
                    "duration_seconds": 3480, "start": "2026-09-29T10:32:00Z", "end": "2026-09-29T11:30:00Z",
                    "clockify_project_suffix": "123456", "tag_suffixes": ["654321"], "billable": True,
                    "provenance": {"evidence_ids": [event.evidence_id], "canonical_meeting_id": meeting_id}}
        current = fixtures.row("wka-manual-s01", "2026-09-29 13:32:00", "2026-09-29 14:30:00", 58)
        current[6], current[8] = proposal["activity_id"], "Example recorded outcome"
        rows = [current]
        if independent:
            rows.insert(0, fixtures.row("independent", "2026-09-29 15:00", "2026-09-29 15:05", 5))
        document = fixtures.capture(*rows)
        manual = fixtures.live("manual-1", "2026-09-29T10:31:07Z", "2026-09-29T11:30:20Z", description="Example manual meeting")
        manual.update(workspaceId="workspace-1", userId="user-1")
        manual["timeInterval"]["duration"] = "PT59M13S"
        gateway = UniqueGateway([manual])
        raw_plan = self._plan(document, existing=gateway.entries)
        item = next(value for value in raw_plan["entries"] if value["review_id"] == "wka-manual-s01")
        declaration = {"adoption_kind": "manual_meeting", "operation_anchor": "human-confirmed/example-meeting",
                       "same_meeting_confirmed": True, "canonical_meeting_id": meeting_id,
                       "current_review_id": item["review_id"], "current_payload_digest": item["payload_digest"],
                       "current_proposal_digest": adoptions.recurring_proposal_digest(proposal),
                       "source_fingerprint": review_corrections.evidence_fingerprint([event.evidence_id]),
                       "clockify_entry_id": "manual-1", "retained_entry_digest": native._digest(manual),
                       "artifacts": {"current_proposals": artifact(root/"current-proposals.json", [proposal]),
                                     "current_source_ledger": artifact(root/"current-ledger.json", ledger_document),
                                     "retained_entry_snapshot": artifact(root/"manual-entry.json", manual)}}
        return document, gateway, {"schema_version": "clockify-source-accounted-adoptions/v3", "declarations": [declaration]}

    def supported_plan(self, document, gateway, snapshot):
        try:
            return self.adopted_plan(document, gateway, snapshot)
        except native.NativePostError as error:
            self.fail(f"explicit audited manual meeting must be credited: {error}")

    def test_manual_58_minutes_credit_retains_59_minutes_13_seconds_without_post(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc, gateway, snapshot = self.proof(root)
            original = copy.deepcopy(gateway.entries)
            plan = self.supported_plan(doc, gateway, snapshot)
            receipt = self.execute(root, plan, gateway)
            self.assertEqual([], gateway.posts)
            self.assertEqual(original, gateway.entries)
            entry = receipt["entries"][0]
            self.assertEqual("credited_manual_meeting", entry["disposition"])
            self.assertEqual(3480, entry["approved_current_seconds"])
            self.assertEqual(3553, entry["retained_native_seconds"])
            self.assertEqual(0, entry["posted_seconds"])
            self.assertEqual(0, receipt["posted_seconds"])
            self.assertEqual(3553, receipt["retained_native_seconds"])
            self.assertEqual(receipt, self.execute(root, plan, gateway))

    def test_manual_credit_and_batch_alias_report_only_actual_new_native_time(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc, gateway, snapshot = self.proof(root, independent=True)
            rows = doc["structuredContent"]["sheets"][0]["data"][0]["rowData"]
            alias = copy.deepcopy(rows[1])
            alias["values"][0] = fixtures.cell("independent-alias")
            rows.append(alias)
            plan = self.supported_plan(doc, gateway, snapshot)
            receipt = self.execute(root, plan, gateway)
            self.assertEqual(1, len(gateway.posts))
            self.assertEqual(5, receipt["posted_minutes"])
            self.assertEqual(5, receipt["same_batch_alias_minutes"])
            self.assertEqual(300, receipt["posted_seconds"])
            self.assertEqual(3553, receipt["retained_native_seconds"])
            self.assertEqual(receipt, self.execute(root, plan, gateway))

    def test_manual_missing_authority_or_wrong_canonical_source_is_rejected(self):
        for field, wrong in (("same_meeting_confirmed", False), ("same_meeting_confirmed", "true"),
                             ("operation_anchor", ""), ("canonical_meeting_id", "cm-other"),
                             ("source_fingerprint", "different"), ("current_proposal_digest", "0"*64),
                             ("current_payload_digest", "0"*64), ("clockify_entry_id", "other")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                doc, gateway, snapshot = self.proof(Path(directory))
                snapshot["declarations"][0][field] = wrong
                with self.assertRaisesRegex(native.NativePostError, "manual meeting"):
                    self.adopted_plan(doc, gateway, snapshot)
                self.assertEqual([], gateway.posts)

    def test_manual_route_target_and_partial_interval_mismatch_are_rejected(self):
        mutations = {"projectId": "other", "tagIds": [], "taskId": "other-task", "billable": False,
                     "userId": "other", "workspaceId": "other", "start": "2026-09-29T10:33:00Z"}
        for field, wrong in mutations.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                doc, gateway, snapshot = self.proof(root)
                target = gateway.entries[0]["timeInterval"] if field == "start" else gateway.entries[0]
                target[field] = wrong
                if field == "start":
                    gateway.entries[0]["timeInterval"]["duration"] = "PT57M20S"
                declaration = snapshot["declarations"][0]
                declaration["retained_entry_digest"] = native._digest(gateway.entries[0])
                declaration["artifacts"]["retained_entry_snapshot"] = artifact(root/"manual-entry.json", gateway.entries[0])
                expected = "route or full interval coverage" if field == "start" else "manual meeting"
                with self.assertRaisesRegex(native.NativePostError, expected):
                    self.adopted_plan(doc, gateway, snapshot)

    def test_manual_snapshot_drift_duplicate_id_and_source_bytes_drift_reject(self):
        for mutation in ("description", "duplicate", "retained_entry_snapshot", "current_proposals", "current_source_ledger"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                doc, gateway, snapshot = self.proof(Path(directory))
                if mutation == "description": gateway.entries[0]["description"] += " changed"
                elif mutation == "duplicate": gateway.entries.append(copy.deepcopy(gateway.entries[0]))
                else: Path(snapshot["declarations"][0]["artifacts"][mutation]["path"]).write_text("{}\n")
                with self.assertRaises(native.NativePostError): self.adopted_plan(doc, gateway, snapshot)

    def test_manual_fresh_direct_get_drift_blocks_all_creates_even_unrelated_first_row(self):
        for field in ("description", "userId", "workspaceId", "id", "taskId", "billable"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                doc, gateway, snapshot = self.proof(root, independent=True)
                plan = self.supported_plan(doc, gateway, snapshot)
                direct_get = gateway.entry_by_id
                def drift(entry_id):
                    entry = direct_get(entry_id)
                    entry[field] = False if field == "billable" else "changed"
                    return entry
                gateway.entry_by_id = drift
                with self.assertRaisesRegex(native.NativePostError, "exact direct GET"):
                    self.execute(root, plan, gateway)
                self.assertEqual([], gateway.posts)

    def test_manual_sealed_credit_preserves_source_binding_without_artifact_io(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc, gateway, snapshot = self.proof(root, independent=True)
            plan = self.supported_plan(doc, gateway, snapshot)
            for handle in snapshot["declarations"][0]["artifacts"].values(): Path(handle["path"]).unlink()
            receipt = self.execute(root, plan, gateway)
            self.assertEqual(["created", "credited_manual_meeting"], [value["disposition"] for value in receipt["entries"]])
            self.assertEqual(1, len(gateway.posts))
            self.assertEqual(300, receipt["posted_seconds"])
            self.assertEqual(receipt, self.execute(root, plan, gateway))

    def test_manual_tampered_sealed_source_rejects_even_resealed_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc, gateway, snapshot = self.proof(root, independent=True)
            plan = self.supported_plan(doc, gateway, snapshot)
            item = next(value for value in plan["entries"] if value["review_id"] == "wka-manual-s01")
            credit = item["prior_entry_credit"]
            credit["source_events"][0]["attributes"]["title"] = "Different meeting"
            credit["credit_digest"] = native._document_digest(credit, "credit_digest")
            plan["plan_digest"] = native._document_digest(plan, "plan_digest")
            with self.assertRaises(native.NativePostError): self.execute(root, plan, gateway)
            self.assertEqual([], gateway.posts)

    def test_manual_one_entry_cannot_credit_another_review_even_same_generic_title(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc, gateway, snapshot = self.proof(root)
            from scripts import clockify_source_adoptions as adoptions
            declaration = snapshot["declarations"][0]
            original = json.loads((root/"current-proposals.json").read_text())[0]
            alias = {**original, "review_activity_key": "wka-alias"}
            declaration["artifacts"]["current_proposals"] = artifact(root/"current-proposals.json", [original, alias])
            alias_row = fixtures.row("wka-alias-s01", "2026-09-29 13:32:00", "2026-09-29 14:30:00", 58)
            alias_row[6], alias_row[8] = original["activity_id"], "Example recorded outcome"
            current_row = fixtures.row("wka-manual-s01", "2026-09-29 13:32:00", "2026-09-29 14:30:00", 58)
            current_row[6], current_row[8] = original["activity_id"], "Example recorded outcome"
            doc = fixtures.capture(current_row, alias_row)
            second = copy.deepcopy(snapshot["declarations"][0])
            raw = self._plan(doc, existing=gateway.entries)
            second.update(current_review_id="wka-alias-s01", current_payload_digest=raw["entries"][1]["payload_digest"],
                          current_proposal_digest=adoptions.recurring_proposal_digest(alias))
            snapshot["declarations"].append(second)
            with self.assertRaisesRegex(native.NativePostError, "reuses one prior entry"):
                self.adopted_plan(doc, gateway, snapshot)

    def test_manual_different_recording_with_same_title_time_route_is_not_equivalent(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc, gateway, snapshot = self.proof(root)
            from scripts import clockify_source_adoptions as adoptions, review_corrections
            ledger_document = json.loads((root/"current-ledger.json").read_text())
            prior = ledger_document["events"][0]
            other = evidence_ledger.evidence_event(
                "fathom", {"source_type": "fathom", "source_id": "different-recording"},
                observed_at=prior["observed_at"], raw_source_span=prior["raw_source_span"], attributes=prior["attributes"],
            )
            ledger = evidence_ledger.EvidenceLedger((other,), member_identities=("member@example.invalid",))
            ledger_document = {"schema_version": ledger.manifest.schema_version, "manifest": ledger.manifest.document(),
                               "events": [other.document()]}
            proposal = json.loads((root/"current-proposals.json").read_text())[0]
            proposal["provenance"]["evidence_ids"] = [other.evidence_id]
            declaration = snapshot["declarations"][0]
            declaration["artifacts"]["current_source_ledger"] = artifact(root/"current-ledger.json", ledger_document)
            declaration["artifacts"]["current_proposals"] = artifact(root/"current-proposals.json", [proposal])
            declaration["current_proposal_digest"] = adoptions.recurring_proposal_digest(proposal)
            declaration["source_fingerprint"] = review_corrections.evidence_fingerprint([other.evidence_id])
            with self.assertRaisesRegex(native.NativePostError, "canonical recording source"):
                self.adopted_plan(doc, gateway, snapshot)

    def test_manual_without_declaration_keeps_overlap_warning_and_normal_posting(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc, gateway, _ = self.proof(root)
            plan = self._plan(doc, existing=gateway.entries)
            self.assertEqual(3480, plan["entries"][0]["live_overlaps"][0]["overlap_seconds"])
            receipt = self.execute(root, plan, gateway)
            self.assertEqual("created", receipt["entries"][0]["disposition"])
            self.assertEqual(1, len(gateway.posts))


class SourceAdoptionTests(unittest.TestCase):
    _plan = fixtures.NativeSheetPostTests._plan
    _approval = fixtures.NativeSheetPostTests._approval
    # Removing explicit adoption consumption must cause a second POST; each
    # rejection test guards a different required link in the proof chain.
    def proof(self, root: Path, *, seconds: bool = False, independent: bool = False):
        start = "2026-09-07 12:00:00"
        end, minutes = ("2026-09-07 12:02:02", 122 / 60) if seconds else ("2026-09-07 12:30:00", 30)
        prior_row = fixtures.row("wka-original-s01", start, end, minutes)
        prior_row[8] = "Human-approved corrected wording"
        old_plan = self._plan(fixtures.capture(prior_row))
        old_approval = self._approval(old_plan)
        gateway = UniqueGateway()
        events_path = root / "old-events.jsonl"
        native.execute_plan(old_plan, old_approval, events_path, root / "old-receipt.json", gateway, now=NOW)
        source_event = evidence_ledger.evidence_event(
            "repository_events", {"source_type": "repository_events", "source_id": "synthetic-operation-1"},
            observed_at="2026-09-07T09:00:00Z", raw_source_span={"start": "2026-09-07T09:00:00Z", "end": "2026-09-07T09:30:00Z"},
            attributes={"operation": "source-backed delivery"},
        )
        ledger = evidence_ledger.EvidenceLedger((source_event,))
        original = [{
            "activity_id": "original", "review_activity_key": "wka-original", "allocation_segment": 1,
            "duration_seconds": 122 if seconds else 1800,
            "description": "Original immutable wording", "clockify_project_suffix": "777777",
            "provenance": {"evidence_ids": [source_event.evidence_id]},
        }]
        current_original = [{**original[0], "activity_id": "rerun", "review_activity_key": "wka-rerun"}]
        if independent:
            current_original.append({**original[0], "activity_id": "independent", "review_activity_key": "wka-independent"})
        artifacts = {
            "prior_proposals": artifact(root / "original-proposals.json", original),
            "source_ledger": artifact(root / "source-ledger.json", {
                "schema_version": ledger.manifest.schema_version, "manifest": ledger.manifest.document(),
                "events": [event.document() for event in ledger.events],
            }),
            "current_proposals": artifact(root / "current-original-proposals.json", current_original),
            "native_plan": artifact(root / "old-plan.json", old_plan),
            "native_approval": artifact(root / "old-approval.json", old_approval),
            "native_events": {"path": str(events_path), "sha256": "sha256:" + hashlib.sha256(events_path.read_bytes()).hexdigest()},
        }
        artifacts["current_source_ledger"] = dict(artifacts["source_ledger"])
        current_row = fixtures.row("wka-rerun-s01", start, end, minutes)
        current_row[8] = "Renamed rerun wording"
        rows = [current_row]
        if independent:
            extra = fixtures.row("wka-independent-s01", start, end, minutes)
            extra[8] = "Independent accomplishment from the same source operation"
            rows.append(extra)
        document = fixtures.capture(*rows)
        raw_plan = self._plan(document, existing=gateway.entries)
        snapshot = {"schema_version": "clockify-source-accounted-adoptions/v1", "declarations": [{
            "operation_anchor": "audit/operation-1/accomplishment-1",
            "current_review_id": "wka-rerun-s01", "current_payload_digest": raw_plan["entries"][0]["payload_digest"],
            "prior_review_id": "wka-original-s01", "clockify_entry_id": "created-1", "artifacts": artifacts,
        }]}
        return document, gateway, snapshot

    def adopted_plan(self, document, gateway, snapshot):
        return native.build_plan(
            document, capture_sha256="a" * 64, routing=fixtures.route(), routing_sha256="b" * 64,
            timezone="Europe/Bucharest", workspace_id="workspace-1", member_id="user-1",
            projects=[{"id": "project-123456"}], tags=[{"id": "tag-654321"}],
            live_entries=gateway.entries, source_adoptions=snapshot,
        )

    def execute(self, root, plan, gateway):
        return native.execute_plan(plan, self._approval(plan), root / "new-events.jsonl",
                                   root / "new-receipt.json", gateway, now=NOW)

    def current_live_proof(self, root, *, independent=False):
        doc, gateway, snapshot = self.proof(root, independent=independent)
        # A different historical recipe cannot be silently interpreted as the
        # current recipe. Preserve the original receipt and seal a new basis.
        records = native._events(root / "old-events.jsonl")
        path = root / "historical-events.jsonl"
        for record in records:
            event = {key: value for key, value in record.items() if key not in {
                "schema_version", "sequence", "previous_digest", "event_digest",
            }}
            if event["event_type"] == "confirmed":
                event["readback_digest"] = "1" * 64
            native._append_event(path, event)
        snapshot["declarations"][0]["artifacts"]["native_events"] = {
            "path": str(path), "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        snapshot["schema_version"] = "clockify-source-accounted-adoptions/v2"
        gateway.entries[0].update(userId="user-1", workspaceId="workspace-1")
        gateway.posts.clear()
        return doc, gateway, snapshot

    def test_v2_current_verification_credits_without_claiming_historical_digest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc, gateway, snapshot = self.current_live_proof(root)
            originals = {path: path.read_bytes() for path in root.iterdir() if path.is_file()}
            plan = self.adopted_plan(doc, gateway, snapshot)
            receipt = self.execute(root, plan, gateway)
            credit = plan["entries"][0]["prior_entry_credit"]
            self.assertEqual("current_live_snapshot", credit["verification_basis"])
            self.assertEqual("1" * 64, credit["historical_readback_digest"])
            self.assertEqual("1" * 64, credit["confirmed_binding"]["readback_digest"])
            self.assertNotEqual(credit["historical_readback_digest"], credit["readback_digest"])
            self.assertEqual([], gateway.posts)
            self.assertEqual("credited_prior_source", receipt["entries"][0]["disposition"])
            self.assertEqual("current_live_snapshot", receipt["entries"][0].get("adoption_verification_basis"))
            self.assertEqual(originals, {path: path.read_bytes() for path in originals})

    def test_v1_does_not_fallback_when_historical_digest_differs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc, gateway, snapshot = self.current_live_proof(root)
            snapshot["schema_version"] = "clockify-source-accounted-adoptions/v1"
            plan = self.adopted_plan(doc, gateway, snapshot)
            with self.assertRaisesRegex(native.NativePostError, "exact direct GET"):
                self.execute(root, plan, gateway)
            self.assertEqual([], gateway.posts)

    def test_v2_missing_duplicate_or_wrong_target_live_entry_is_rejected(self):
        for mutation in ("missing", "duplicate", "userId", "workspaceId", "missing-user", "missing-workspace"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                doc, gateway, snapshot = self.current_live_proof(root)
                if mutation == "missing":
                    gateway.entries.clear()
                elif mutation == "duplicate":
                    gateway.entries.append(copy.deepcopy(gateway.entries[0]))
                elif mutation.startswith("missing-"):
                    gateway.entries[0].pop("userId" if mutation == "missing-user" else "workspaceId")
                else:
                    gateway.entries[0][mutation] = "wrong-target"
                with self.assertRaises(native.NativePostError):
                    self.adopted_plan(doc, gateway, snapshot)
                self.assertEqual([], gateway.posts)

    def test_v2_live_payload_drift_rejects_every_approved_field(self):
        mutations = {"description": "Human-approved corrected wording ", "projectId": "other",
                     "tagIds": [], "taskId": "task-other", "billable": False,
                     "start": "2026-09-07T09:00:01Z", "end": "2026-09-07T09:30:01Z",
                     "duration": "PT29M"}
        for field, wrong in mutations.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                doc, gateway, snapshot = self.current_live_proof(root)
                target = gateway.entries[0]["timeInterval"] if field in {"start", "end", "duration"} else gateway.entries[0]
                target[field] = wrong
                with self.assertRaises(native.NativePostError):
                    self.adopted_plan(doc, gateway, snapshot)
                self.assertEqual([], gateway.posts)

    def test_v2_fresh_direct_get_drift_blocks_independent_creates(self):
        for field, wrong in (("description", "Human-approved corrected wording "),
                             ("userId", "other"), ("workspaceId", "other"), ("id", "other")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                doc, gateway, snapshot = self.current_live_proof(root, independent=True)
                plan = self.adopted_plan(doc, gateway, snapshot)
                direct_get = gateway.entry_by_id
                def drift(entry_id):
                    value = direct_get(entry_id)
                    value[field] = wrong
                    return value
                gateway.entry_by_id = drift
                with self.assertRaisesRegex(native.NativePostError, "exact direct GET"):
                    self.execute(root, plan, gateway)
                self.assertEqual([], gateway.posts)

    def test_v2_replay_uses_sealed_facts_and_preserves_independent_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc, gateway, snapshot = self.current_live_proof(root, independent=True)
            plan = self.adopted_plan(doc, gateway, snapshot)
            for path in {handle["path"] for handle in snapshot["declarations"][0]["artifacts"].values()}:
                Path(path).unlink()
            receipt = self.execute(root, plan, gateway)
            replay = self.execute(root, plan, gateway)
            self.assertEqual(receipt, replay)
            self.assertEqual(["credited_prior_source", "created"], [row["disposition"] for row in receipt["entries"]])
            self.assertEqual(1, len(gateway.posts))
            self.assertEqual("Independent accomplishment from the same source operation", gateway.posts[0]["description"])

    def test_v2_source_and_approval_artifact_drift_remains_rejected(self):
        for name in ("prior_proposals", "source_ledger", "current_proposals", "current_source_ledger", "native_approval"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                doc, gateway, snapshot = self.current_live_proof(root)
                Path(snapshot["declarations"][0]["artifacts"][name]["path"]).write_text("{}\n")
                with self.assertRaisesRegex(native.NativePostError, "bytes drifted"):
                    self.adopted_plan(doc, gateway, snapshot)

    def test_v2_one_prior_entry_cannot_credit_two_current_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc, gateway, snapshot = self.current_live_proof(root, independent=True)
            raw = self._plan(doc, existing=gateway.entries)
            second = copy.deepcopy(snapshot["declarations"][0])
            second.update(current_review_id="wka-independent-s01", current_payload_digest=raw["entries"][1]["payload_digest"])
            snapshot["declarations"].append(second)
            with self.assertRaisesRegex(native.NativePostError, "reuses one prior"):
                self.adopted_plan(doc, gateway, snapshot)

    def test_corrected_prior_payload_and_renamed_current_review_credit_without_create(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc, gateway, snapshot = self.proof(root)
            original_path = root / "original-proposals.json"
            original_bytes = original_path.read_bytes()
            plan = self.adopted_plan(doc, gateway, snapshot)
            gateway.posts.clear()
            receipt = self.execute(root, plan, gateway)
            receipt_bytes = (root / "new-receipt.json").read_bytes()
            rerun = self.execute(root, plan, gateway)
            self.assertEqual([], gateway.posts)
            self.assertEqual("created-1", receipt["entries"][0]["clockify_entry_id"])
            self.assertEqual("credited_prior_source", receipt["entries"][0]["disposition"])
            self.assertEqual("Human-approved corrected wording", receipt["entries"][0]["prior_approved_payload"]["description"])
            self.assertEqual("historical_native_readback", receipt["entries"][0].get("adoption_verification_basis"))
            self.assertEqual("project-123456", receipt["entries"][0]["prior_approved_payload"]["projectId"])
            self.assertEqual("prior_accomplishment_retained_current_payload_not_posted", receipt["entries"][0]["posting_semantics"])
            self.assertEqual("Renamed rerun wording", plan["entries"][0]["payload"]["description"])
            self.assertEqual(receipt, rerun)
            self.assertEqual(receipt_bytes, (root / "new-receipt.json").read_bytes())
            self.assertEqual(original_bytes, original_path.read_bytes())

    def test_wrong_confirmed_entry_or_current_payload_is_rejected(self):
        for field, wrong in (("clockify_entry_id", "not-confirmed"), ("current_payload_digest", "0" * 64)):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                doc, gateway, snapshot = self.proof(root)
                snapshot["declarations"][0][field] = wrong
                gateway.posts.clear()
                with self.assertRaises(native.NativePostError):
                    self.adopted_plan(doc, gateway, snapshot)
                self.assertEqual([], gateway.posts)

    def test_source_bytes_drift_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc, gateway, snapshot = self.proof(root)
            (root / "original-proposals.json").write_text("[]\n")
            gateway.posts.clear()
            with self.assertRaises(native.NativePostError):
                self.adopted_plan(doc, gateway, snapshot)
            self.assertEqual([], gateway.posts)

    def test_live_direct_get_drift_blocks_all_creates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc, gateway, snapshot = self.proof(root, independent=True)
            plan = self.adopted_plan(doc, gateway, snapshot)
            gateway.posts.clear()
            direct_get = gateway.entry_by_id
            def drift(entry_id):
                value = direct_get(entry_id)
                value["description"] = "Changed after approval"
                return value
            gateway.entry_by_id = drift
            with self.assertRaises(native.NativePostError):
                self.execute(root, plan, gateway)
            self.assertEqual([], gateway.posts)
            self.assertFalse((root / "new-receipt.json").exists())

    def test_independent_accomplishment_from_same_source_remains_creatable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc, gateway, snapshot = self.proof(root, independent=True)
            plan = self.adopted_plan(doc, gateway, snapshot)
            gateway.posts.clear()
            receipt = self.execute(root, plan, gateway)
            self.assertEqual(1, len(gateway.posts))
            self.assertEqual("Independent accomplishment from the same source operation", gateway.posts[0]["description"])
            self.assertEqual(["credited_prior_source", "created"], [entry["disposition"] for entry in receipt["entries"]])
            self.assertEqual(1800, plan["entries"][1]["live_overlaps"][0]["overlap_seconds"])

    def test_second_precision_credit_preserves_exact_seconds_and_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc, gateway, snapshot = self.proof(root, seconds=True)
            plan = self.adopted_plan(doc, gateway, snapshot)
            gateway.posts.clear()
            gateway.entries[0]["timeInterval"]["duration"] = "PT2M2S"
            receipt = self.execute(root, plan, gateway)
            self.assertEqual([], gateway.posts)
            self.assertAlmostEqual(122 / 60, receipt["total_minutes"])
            self.assertEqual("2026-09-07T09:02:02Z", gateway.entries[0]["timeInterval"]["end"])

    def test_current_source_identity_must_exist_in_captured_proposals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc, gateway, snapshot = self.proof(root)
            snapshot["declarations"][0]["artifacts"]["current_proposals"] = artifact(
                root / "wrong-current.json", [{"review_activity_key": "wka-other", "allocation_segment": 1}]
            )
            gateway.posts.clear()
            with self.assertRaises(native.NativePostError):
                self.adopted_plan(doc, gateway, snapshot)
            self.assertEqual([], gateway.posts)

    def test_current_source_evidence_must_exist_in_verified_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc, gateway, snapshot = self.proof(root)
            path = root / "current-original-proposals.json"
            proposals = json.loads(path.read_text())
            proposals[0]["provenance"]["evidence_ids"] = ["ev-" + "0" * 64]
            snapshot["declarations"][0]["artifacts"]["current_proposals"] = artifact(path, proposals)
            with self.assertRaisesRegex(native.NativePostError, "evidence is absent"):
                self.adopted_plan(doc, gateway, snapshot)

    def test_prior_plan_account_and_workspace_must_match_current_target(self):
        for field in ("workspace_id", "member_id"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                doc, gateway, snapshot = self.proof(root)
                path = root / "old-plan.json"
                plan = json.loads(path.read_text())
                plan[field] = "wrong-target"
                plan["plan_digest"] = native._document_digest(plan, "plan_digest")
                snapshot["declarations"][0]["artifacts"]["native_plan"] = artifact(path, plan)
                with self.assertRaisesRegex(native.NativePostError, "target differs"):
                    self.adopted_plan(doc, gateway, snapshot)

    def test_confirmed_event_payload_must_match_actual_prior_approved_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc, gateway, snapshot = self.proof(root)
            old = native._events(root / "old-events.jsonl")
            path = root / "wrong-events.jsonl"
            for record in old:
                event = {key: value for key, value in record.items() if key not in {
                    "schema_version", "sequence", "previous_digest", "event_digest",
                }}
                if event["event_type"] == "confirmed":
                    event["payload_digest"] = "0" * 64
                native._append_event(path, event)
            snapshot["declarations"][0]["artifacts"]["native_events"] = {
                "path": str(path), "sha256": "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            with self.assertRaisesRegex(native.NativePostError, "confirmed-entry binding"):
                self.adopted_plan(doc, gateway, snapshot)

    def test_execution_uses_sealed_plan_when_original_artifacts_no_longer_exist(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc, gateway, snapshot = self.proof(root, independent=True)
            plan = self.adopted_plan(doc, gateway, snapshot)
            gateway.posts.clear()
            for path in {handle["path"] for handle in snapshot["declarations"][0]["artifacts"].values()}:
                Path(path).unlink()
            receipt = self.execute(root, plan, gateway)
            self.assertEqual(1, len(gateway.posts))
            self.assertEqual("Independent accomplishment from the same source operation", gateway.posts[0]["description"])
            self.assertEqual("credited_prior_source", receipt["entries"][0]["disposition"])

    def test_tampered_embedded_credit_cannot_bypass_exact_plan_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc, gateway, snapshot = self.proof(root)
            plan = self.adopted_plan(doc, gateway, snapshot)
            approval = self._approval(plan)
            plan["entries"][0]["prior_entry_credit"]["clockify_entry_id"] = "forged-id"
            plan["plan_digest"] = native._document_digest(plan, "plan_digest")
            gateway.posts.clear()
            with self.assertRaisesRegex(native.NativePostError, "approval does not match"):
                native.execute_plan(plan, approval, root / "new-events.jsonl", root / "new-receipt.json", gateway, now=NOW)
            self.assertEqual([], gateway.posts)

    def test_plan_consumes_each_original_artifact_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc, gateway, snapshot = self.proof(root)
            original_read = Path.read_bytes
            reads = {}
            def read(path):
                reads[str(path)] = reads.get(str(path), 0) + 1
                return original_read(path)
            with mock.patch.object(Path, "read_bytes", new=read):
                self.adopted_plan(doc, gateway, snapshot)
            paths = {handle["path"] for handle in snapshot["declarations"][0]["artifacts"].values()}
            self.assertEqual({path: 1 for path in paths}, reads)

    def test_edited_credit_facts_fail_the_sealed_credit_integrity_check(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc, gateway, snapshot = self.proof(root)
            plan = self.adopted_plan(doc, gateway, snapshot)
            plan["entries"][0]["prior_entry_credit"]["confirmed_binding"]["member_id"] = "wrong-member"
            plan["plan_digest"] = native._document_digest(plan, "plan_digest")
            gateway.posts.clear()
            with self.assertRaisesRegex(native.NativePostError, "credit integrity differs"):
                self.execute(root, plan, gateway)
            self.assertEqual([], gateway.posts)

    def test_cli_optional_snapshot_is_bound_into_native_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            doc, gateway, snapshot = self.proof(root)
            gateway.timeout_seconds = 45
            capture_handle = artifact(root / "sheet.json", doc)
            artifact(root / "routing.json", fixtures.route())
            artifact(root / "adoptions.json", snapshot)
            def catalog(path, *_args, **_kwargs):
                if path.endswith("projects?archived=false"):
                    return [{"id": "project-123456"}]
                self.assertTrue(path.endswith("/tags"))
                return [{"id": "tag-654321"}]
            with mock.patch.object(native, "_gateway", return_value=(gateway, "workspace-1", "user-1", "fixture-key")), \
                    mock.patch.object(native.legacy, "_paged", side_effect=catalog), contextlib.redirect_stdout(io.StringIO()):
                status = native.main([
                    "plan", "--sheet-capture", str(root / "sheet.json"),
                    "--expected-capture-sha256", capture_handle["sha256"][7:],
                    "--routing", str(root / "routing.json"), "--source-adoptions", str(root / "adoptions.json"),
                    "--output", str(root / "planned.json"),
                ])
            self.assertEqual(0, status)
            plan = json.loads((root / "planned.json").read_text())
            gateway.posts.clear()
            receipt = self.execute(root, plan, gateway)
            self.assertEqual([], gateway.posts)
            self.assertEqual("credited_prior_source", receipt["entries"][0]["disposition"])


if __name__ == "__main__":
    unittest.main()
