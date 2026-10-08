"""Fixed native recording allocations are not allocator effort demands."""
import copy
import datetime as dt
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from scripts import clockify_pending_review_selection as consumer
from scripts import evidence_ledger, work_accounting_pipeline as pipeline


class NativeMeetingCreditTests(unittest.TestCase):
    def fixture(self, *, fallback=False):
        event = evidence_ledger.evidence_event(
            "fathom", {"source_type": "fathom", "source_id": "recording-1", "machine": "fathom"},
            observed_at="2026-09-29T09:00:00Z",
            raw_source_span={"start": "2026-09-29T09:00:00Z", "end": "2026-09-29T09:30:00Z"},
            attributes={"recording_id": "recording-1", "title": "Client delivery review",
                        "start": "2026-09-29T09:00:00Z", "end": "2026-09-29T09:30:00Z",
                        "recorded_by_email": "vlad@serenichron.com",
                        "calendar_invitees": [{"email": "vlad@serenichron.com"}],
                        "share_url": "https://fathom.video/share/example-recording-1",
                        "summary": "Reviewed delivery", "transcript": [{"text": "Reviewed delivery", "offset_seconds": 0}]})
        ledger = evidence_ledger.EvidenceLedger((event,), timezone="UTC")
        document = {"manifest": ledger.manifest.document(), "events": [event.document()]}
        recordings, exceptions = pipeline._recording_events(document["events"], document["manifest"])
        self.assertEqual([], exceptions)
        self.assertEqual(1, len(recordings))
        canonical = recordings[0]["meeting"].canonical_id
        start = dt.datetime(2026, 9, 29, 9, tzinfo=dt.timezone.utc)
        proposal = pipeline._proposal(
            {"activity_id": "meeting-activity", "workstream_id": "meeting-stream", "effort": {}},
            {"project_name": "Serenichron", "project_suffix": "abc123", "tag_names": [], "tag_suffixes": []},
            "SC — Reviewed client delivery", start, start + dt.timedelta(minutes=30), [event.evidence_id], 1)
        proposal["provenance"]["canonical_meeting_id"] = canonical
        if fallback:
            proposal["provenance"].update(source_type="recorded_meeting", semantic_fallback=True,
                recorded_meeting_start=proposal["start"], recorded_meeting_end=proposal["end"])
        source = {"ledger": document, "accounting": {"proposals": [copy.deepcopy(proposal)],
            "allocation": {"evidence": []}, "fathom_reconciliation": [{"canonical_id": canonical,
            "activity_id": "meeting-activity", "source_evidence_ids": [event.evidence_id], "status": "proposed"}]}}
        item = {"proposal": proposal, "source": source, "atoms": {consumer._atom(event.document())}}
        return item, source

    def test_fixed_semantic_meeting_retains_saved_credit_without_fabricating_demand(self):
        item, source = self.fixture()
        before = copy.deepcopy(source)
        normalized, receipt = consumer._credits([item], {"current": source})
        self.assertEqual(30, receipt["saved_credit_minutes"])
        self.assertEqual(0, receipt["native_demand_count"])
        self.assertEqual(1, len(receipt["native_meeting_credit_checks"]))
        self.assertEqual("2026-09-29T09:00:00+00:00", normalized[0]["start"])
        self.assertEqual("2026-09-29T09:30:00+00:00", normalized[0]["end"])
        self.assertEqual(before, source)

    def test_factual_attendance_retains_native_fixed_recording_not_semantic_effort(self):
        item, source = self.fixture(fallback=True)
        normalized, receipt = consumer._credits([item], {"current": source})
        self.assertEqual(30, receipt["saved_credit_minutes"])
        self.assertEqual([], receipt["native_credit_checks"])
        self.assertEqual({}, normalized[0]["effort"])

    def test_fixed_meeting_requires_exact_saved_accounting_proposal(self):
        item, source = self.fixture()
        item["proposal"]["duration_minutes"] = 29
        with self.assertRaises(ValueError): consumer._credits([item], {"current": source})

    def test_fixed_meeting_requires_proposed_native_reconciliation(self):
        item, source = self.fixture()
        source["accounting"]["fathom_reconciliation"][0]["status"] = "reconciled"
        with self.assertRaises(ValueError): consumer._credits([item], {"current": source})

    def test_fixed_meeting_requires_canonical_ledger_identity(self):
        item, source = self.fixture()
        item["proposal"]["provenance"]["canonical_meeting_id"] = "unrelated-meeting"
        source["accounting"]["proposals"] = [copy.deepcopy(item["proposal"])]
        source["accounting"]["fathom_reconciliation"][0]["canonical_id"] = "unrelated-meeting"
        with self.assertRaises(ValueError): consumer._credits([item], {"current": source})

    def test_fixed_meeting_cannot_exceed_source_recording_bounds(self):
        item, source = self.fixture()
        item["proposal"].update(end="2026-09-29T09:31:00+00:00", duration_minutes=31, duration_seconds=1860)
        source["accounting"]["proposals"] = [copy.deepcopy(item["proposal"])]
        with self.assertRaises(ValueError): consumer._credits([item], {"current": source})

    def test_native_split_segments_are_not_expanded_to_recording_duration(self):
        item, source = self.fixture()
        item["proposal"].update(end="2026-09-29T09:10:00+00:00", duration_minutes=10, duration_seconds=600)
        item["proposal"]["provenance"]["timestamped_split_evidence_ids"] = ["source-transcript:0-600"]
        source["accounting"]["proposals"] = [copy.deepcopy(item["proposal"])]
        normalized, receipt = consumer._credits([item], {"current": source})
        self.assertEqual(10, receipt["saved_credit_minutes"])
        self.assertEqual("2026-09-29T09:10:00+00:00", normalized[0]["end"])

    def test_matching_posted_entry_still_rejects_native_meeting_credit(self):
        item, source = self.fixture()
        proposal = item["proposal"]
        posted = evidence_ledger.evidence_event("clockify", {"source_id": "posted", "machine": "clockify"},
            observed_at=proposal["start"], raw_source_span={"start": proposal["start"], "end": proposal["end"]},
            attributes={"project_id_suffix": "abc123", "description": proposal["description"]})
        source["ledger"]["events"].append(posted.document())
        with self.assertRaisesRegex(ValueError, "normalization"):
            consumer._credits([item], {"current": source})

    def test_authentic_subminute_fixed_recording_retains_exact_seconds_and_floor_minutes(self):
        item, source = self.fixture()
        item["proposal"].update(end="2026-09-29T09:00:24+00:00", duration_minutes=0, duration_seconds=24)
        source["accounting"]["proposals"] = [copy.deepcopy(item["proposal"])]
        original = copy.deepcopy(item["proposal"])
        try: normalized, receipt = consumer._credits([item], {"current": source})
        except ValueError as error: self.fail(f"authentic native subminute meeting credit rejected: {error}")
        self.assertEqual(original, {k:v for k,v in normalized[0].items() if k != "review_warnings"} | {"review_warnings": original["review_warnings"]})
        self.assertEqual(0, normalized[0]["duration_minutes"])
        self.assertEqual(24, normalized[0]["duration_seconds"])
        self.assertEqual(0.4, receipt["saved_credit_minutes"])

    def test_subminute_fixed_recording_rejects_inexact_seconds_or_changed_floor_contract(self):
        for seconds, minutes in ((23, 0), (24, 1), (24.5, 0), (True, 0)):
            with self.subTest(seconds=seconds, minutes=minutes):
                item, source = self.fixture()
                item["proposal"].update(end="2026-09-29T09:00:24+00:00", duration_minutes=minutes, duration_seconds=seconds)
                source["accounting"]["proposals"] = [copy.deepcopy(item["proposal"])]
                with self.assertRaises(ValueError): consumer._credits([item], {"current": source})

    def test_nonmeeting_allocator_credit_keeps_strict_minutes_contract(self):
        item, source = self.fixture()
        item["proposal"].update(end="2026-09-29T09:00:24+00:00", duration_minutes=0, duration_seconds=24)
        item["proposal"]["provenance"].pop("canonical_meeting_id")
        source["accounting"]["proposals"] = [copy.deepcopy(item["proposal"])]
        source["accounting"]["allocation"]["evidence"] = [{"activity_id": item["proposal"]["activity_id"]}]
        with self.assertRaisesRegex(ValueError, "exact duration"):
            consumer._credits([item], {"current": source})


class CoveredPostedMeetingTests(unittest.TestCase):
    def fixture(self, root):
        import test_clockify_native_sheet_post as fixtures
        from scripts import clockify_native_sheet_post as native
        item, source = NativeMeetingCreditTests().fixture()
        proposal = item["proposal"]
        proposal.update(client_project="Example", clockify_project_suffix="123456", tag_names=["Process"], tag_suffixes=["654321"])
        source["accounting"]["proposals"] = [copy.deepcopy(proposal)]
        source["ledger"]["schema_version"] = "evidence-ledger/v1"
        current_id = "wka-current-s01"
        proposal["review_activity_key"] = "wka-current"
        item["review_id"] = current_id
        prior = copy.deepcopy(proposal)
        prior.update(review_activity_key="wka-prior", activity_id="prior-meeting-activity")
        def write(path, value):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(value), encoding="utf-8")
            return consumer.artifact_handle(path)
        prior_path = root/"prior-run"/"proposals.json"
        prior_handle = write(prior_path, [prior])
        ledger_handle = write(root/"prior-ledger.json", source["ledger"])
        row = fixtures.row("wka-prior-s01", "2026-09-29 12:00", "2026-09-29 12:30", 30)
        row[6], row[11] = prior["activity_id"], "prior-run"
        test = fixtures.NativeSheetPostTests()
        plan = test._plan(fixtures.capture(row))
        approval = test._approval(plan)
        gateway = fixtures.FakeGateway()
        event_path = root/"events.jsonl"
        native.execute_plan(plan, approval, event_path, root/"receipt.json", gateway,
            now=dt.datetime(2026,10,4,11,tzinfo=dt.timezone.utc))
        entry = gateway.entries[0]
        entry.update(workspaceId="workspace-1", userId="user-1")
        row[9],row[13] = "Approved","posted"
        capture = {"schema":"clockify-live-readonly/v1", "verified_target":{"workspace_id":"workspace-1","member_id":"user-1"},
            "get_requests":[{"method":"GET"}],"all_pages_returned":True,"external_mutations":False,"cache_mutations":False,
            "finished_utc":dt.datetime.now(dt.timezone.utc).isoformat(),"entries":[entry]}
        declaration = {"current_review_id":current_id,"prior_review_id":"wka-prior-s01","clockify_entry_id":entry["id"],
            "prior_proof_artifacts":{"prior_proposals":prior_handle,"source_ledger":ledger_handle,
                "native_plan":write(root/"plan.json",plan),"native_approval":write(root/"approval.json",approval),
                "native_events":consumer.artifact_handle(event_path)},"fresh_clockify_capture":write(root/"live.json",capture)}
        return item,{row[0]:row},declaration,capture

    def verify(self,item,rows,declaration):
        return consumer._covered_source_outcome(declaration,item,rows,{})

    def test_exact_posted_recording_is_coverage_only_not_pending_or_accounting_credit(self):
        with tempfile.TemporaryDirectory() as temp:
            item, rows, declaration, _ = self.fixture(Path(temp))
            original = copy.deepcopy((item,rows))
            proof = self.verify(item,rows,declaration)
            self.assertEqual("verified_posted_source_representation_only",proof["basis"])
            self.assertEqual(1800,proof["covered_seconds"])
            self.assertEqual(0,proof["new_pending_rows"])
            self.assertEqual(0,proof["accounting_credit_mutations"])
            self.assertEqual(original,(item,rows))

    def test_wrong_recording_interval_target_or_source_preimage_rejects(self):
        for changed in ("recording","interval","target","posted-status","description","source-activity"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temp:
                item,rows,declaration,capture=self.fixture(Path(temp))
                if changed=="recording": item["proposal"]["provenance"]["canonical_meeting_id"]="cm-unrelated"
                elif changed=="interval": item["proposal"].update(end="2026-09-29T09:29:00+00:00",duration_seconds=1740,duration_minutes=29)
                elif changed=="target": capture["verified_target"]["member_id"]="wrong-member"
                elif changed=="posted-status": rows["wka-prior-s01"][13]="unposted"
                elif changed=="description": rows["wka-prior-s01"][8]="Different currently approved work"
                else: rows["wka-prior-s01"][6]="different-source"
                Path(declaration["fresh_clockify_capture"]["path"]).write_text(json.dumps(capture))
                declaration["fresh_clockify_capture"]=consumer.artifact_handle(Path(declaration["fresh_clockify_capture"]["path"]))
                with self.assertRaises(ValueError): self.verify(item,rows,declaration)

    def test_stale_or_changed_provider_proof_rejects(self):
        for changed in ("stale-time","provider-payload","provider-id","unverified-method","native-event-bytes"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temp:
                item,rows,declaration,capture=self.fixture(Path(temp))
                if changed=="stale-time": capture["finished_utc"]=(dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=2)).isoformat()
                elif changed=="provider-payload": capture["entries"][0]["description"]="Changed current provider entry"
                elif changed=="provider-id": capture["entries"][0]["id"]="unrelated-entry"
                elif changed=="unverified-method": capture["get_requests"][0]["method"]="POST"
                else: Path(declaration["prior_proof_artifacts"]["native_events"]["path"]).write_text("{}\n")
                Path(declaration["fresh_clockify_capture"]["path"]).write_text(json.dumps(capture))
                declaration["fresh_clockify_capture"]=consumer.artifact_handle(Path(declaration["fresh_clockify_capture"]["path"]))
                with self.assertRaises(ValueError): self.verify(item,rows,declaration)


class CoveredPostedAccomplishmentTests(unittest.TestCase):
    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return consumer.artifact_handle(path)

    def fixture(self, root):
        item, rows, declaration, capture = CoveredPostedMeetingTests().fixture(root)
        proposal = item["proposal"]
        proposal["provenance"].pop("canonical_meeting_id")
        event = evidence_ledger.evidence_event("codex_sessions_event",
            {"machine": "test-host", "session_id": "delivery-review"},
            observed_at=proposal["start"], attributes={"role": "assistant", "kind": "message",
                "content": "Reviewed the deployment patch and confirmed the delivery fix."})
        ledger = evidence_ledger.EvidenceLedger((event,), timezone="UTC")
        document = {"schema_version": "evidence-ledger/v1", "manifest": ledger.manifest.document(),
                    "events": [event.document()]}
        proposal["provenance"]["evidence_ids"] = [event.evidence_id]
        item["source"]["ledger"] = document
        item["atoms"] = {consumer._atom(event.document())}
        prior = copy.deepcopy(proposal)
        prior.update(review_activity_key="wka-prior", activity_id="prior-delivery-review")
        rows["wka-prior-s01"][6] = prior["activity_id"]
        prior_proof = declaration["prior_proof_artifacts"]
        prior_proof["prior_proposals"] = self.write(Path(prior_proof["prior_proposals"]["path"]), [prior])
        prior_proof["source_ledger"] = self.write(Path(prior_proof["source_ledger"]["path"]), document)
        current_semantic = {"activity_id": proposal["activity_id"], "evidence_ids": [event.evidence_id],
                            "action": "Reviewed", "object": "the deployment patch", "outcome": "Confirmed delivery fix"}
        prior_semantic = {"activity_id": prior["activity_id"], "evidence_ids": [event.evidence_id],
                          "action": "Checked", "object": "deployment changes", "outcome": "Verified the fix"}
        current_artifacts = {
            "proposals": self.write(root/"current-run"/"proposals.json", [proposal]),
            "ledger": self.write(root/"current-run"/"evidence/evidence-ledger.json", document),
            "routing": self.write(root/"current-run"/"routing.json", {"session_routes": [{
                "project_name": "Example Level 2", "project_suffix": "abcdef", "tag_names": ["Process"],
                "tag_suffixes": ["654321"], "billable": True}]})}
        item["source"]["artifacts"] = current_artifacts
        witness = {"schema_version": "pending-covered-accomplishment-adjudication/v1",
            "current_review_id": item["review_id"], "prior_review_id": "wka-prior-s01",
            "current_proposal_sha256": consumer.digest(proposal), "prior_proposal_sha256": consumer.digest(prior),
            "current_semantic": {"artifact": self.write(root/"current-run"/"semantic-analysis.json", {"activities": [current_semantic]}),
                                 "activity": current_semantic},
            "prior_semantic": {"artifact": self.write(root/"prior-run"/"semantic-analysis.json", {"activities": [prior_semantic]}),
                               "activity": prior_semantic},
            "canonical_atoms_sha256": consumer.digest(sorted(consumer.digest(atom) for atom in item["atoms"])),
            "same_bounded_accomplishment": True, "basis": "coordinator_source_event_review",
            "operation_anchor": "deployment-patch-delivery-fix",
            "normalized_accomplishment": {"action": "Review", "object": "deployment patch", "outcome": "Confirmed delivery fix"},
            "adjudication_rationale": "Both exact cited source events describe the same bounded patch review and delivery verification; their entire allocation is identical."}
        declaration["semantic_adjudication"] = self.write(root/"adjudication.json", witness)
        capture["entries"][0]["projectId"] = "project-abcdef"
        declaration["fresh_clockify_capture"] = self.write(Path(declaration["fresh_clockify_capture"]["path"]), capture)
        declaration["observed_current_project"] = {"project_id": "project-abcdef", "project_name": "Example Level 2",
                                                    "routing_snapshot": current_artifacts["routing"]}
        return item, rows, declaration, capture, witness

    def verify(self, item, rows, declaration):
        return consumer._covered_source_outcome(declaration, item, rows, {})

    def test_reviewed_complete_accomplishment_covers_only_and_preserves_observed_project(self):
        with tempfile.TemporaryDirectory() as temp:
            item, rows, declaration, _, _ = self.fixture(Path(temp))
            before = copy.deepcopy((item, rows))
            proof = self.verify(item, rows, declaration)
            self.assertEqual("verified_posted_source_representation_only", proof["basis"])
            self.assertEqual(1800, proof["covered_seconds"])
            self.assertEqual(0, proof["new_pending_rows"])
            self.assertEqual(0, proof["accounting_credit_mutations"])
            self.assertEqual(0, proof["clockify_writes"])
            self.assertEqual("project-abcdef", proof["observed_current_project"]["project_id"])
            self.assertFalse(proof["current_project_authorized_by_historical_approval"])
            self.assertNotEqual(proof["historical_approved_project_id"], proof["observed_current_project"]["project_id"])
            self.assertEqual(before, (item, rows))

    def test_unknown_partial_or_unadjudicated_accomplishment_rejects(self):
        for changed in ("atoms", "witness-hash", "same-operation", "current-semantic", "prior-semantic", "proposal", "basis"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temp:
                item, rows, declaration, _, witness = self.fixture(Path(temp))
                if changed == "atoms":
                    item["source"]["ledger"]["events"][0]["attributes"]["content"] = "A different operation"
                elif changed == "witness-hash":
                    declaration["semantic_adjudication"]["sha256"] = "sha256:" + "0" * 64
                elif changed == "same-operation": witness["same_bounded_accomplishment"] = False
                elif changed == "current-semantic": witness["current_semantic"]["activity"]["outcome"] = "Different outcome"
                elif changed == "prior-semantic": witness["prior_semantic"]["activity"]["object"] = "Different object"
                elif changed == "proposal": item["proposal"]["description"] = "Unreviewed revised wording"
                else: witness["basis"] = "shared_context_only"
                if changed not in {"witness-hash", "atoms", "proposal"}:
                    declaration["semantic_adjudication"] = self.write(Path(declaration["semantic_adjudication"]["path"]), witness)
                with self.assertRaises(ValueError): self.verify(item, rows, declaration)

    def test_current_provider_changes_other_than_explicit_configured_project_reject(self):
        for changed in ("description", "tagIds", "billable", "start", "workspaceId", "userId", "projectId", "route-name", "route-hash", "sheet-route"):
            with self.subTest(changed=changed), tempfile.TemporaryDirectory() as temp:
                item, rows, declaration, capture, _ = self.fixture(Path(temp))
                entry = capture["entries"][0]
                if changed == "tagIds": entry[changed] = []
                elif changed == "billable": entry[changed] = not entry[changed]
                elif changed == "start": entry["timeInterval"]["start"] = "2026-09-29T09:01:00Z"
                elif changed == "route-name": declaration["observed_current_project"]["project_name"] = "Unknown project"
                elif changed == "route-hash": declaration["observed_current_project"]["routing_snapshot"]["sha256"] = "sha256:" + "0" * 64
                elif changed == "sheet-route": rows["wka-prior-s01"][4] = "Example Level 2"
                else: entry[changed] = "unrelated"
                declaration["fresh_clockify_capture"] = self.write(Path(declaration["fresh_clockify_capture"]["path"]), capture)
                with self.assertRaises(ValueError): self.verify(item, rows, declaration)


if __name__ == "__main__": unittest.main()
