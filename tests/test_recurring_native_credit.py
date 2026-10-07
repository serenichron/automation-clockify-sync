"""Explicit recurring credits must reach accounting without alias inference."""
import copy
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import test_clockify_native_sheet_post as native_fixtures
import test_native_source_adoptions as adoption_fixtures
import test_verified_posted_credit as proposal_fixtures
import test_work_accounting_pipeline as accounting_fixtures
from scripts import clockify_source_adoptions as adoption, evidence_ledger
from scripts import clockify_checkpoint_snapshot as snapshot, collector_checkpoints
from scripts import clockify_sync_collect as collector, review_corrections, work_accounting_pipeline as pipeline


NOW = adoption_fixtures.NOW
START = dt.datetime(2026, 9, 7, 9, tzinfo=dt.timezone.utc)


class RecurringNativeCreditTests(unittest.TestCase):
    def fixture(self, root, *, prior_minutes=(30,), current_minutes=(30,), kind="equal_accomplishment", prior_offsets=None, current_meeting_attrs=None, current_end_seconds=844, prior_source_local=False, prior_source_span_minutes=None):
        self.assertTrue(callable(getattr(adoption, "build_recurring_credit", None)), "recurring credit producer is missing")
        make = native_fixtures.NativeSheetPostTests()
        rows, priors = [], []
        cursor = START
        total = sum(prior_minutes)
        intersection = kind == "source_native_meeting_intersection"
        recording = kind == "whole_recording_aliases" or intersection
        meeting_attrs = {"recording_id": "recording-123", "share_url": "https://fathom.video/share/fixture-123",
                         "title": "Discovery call", "semantic_evidence_status": "available",
                         "recorded_by_email": "vlad@serenichron.com", "calendar_invitees": [{"email": "prospect@example.test"}]}
        old_end = START + dt.timedelta(minutes=prior_minutes[0] if intersection else total)
        if prior_source_span_minutes is not None:
            old_end = START + dt.timedelta(minutes=prior_source_span_minutes)
        if intersection:
            source_start, source_end = START, old_end
            if prior_source_local:
                source_start = START.astimezone(collector.BUCHAREST).replace(tzinfo=None)
                source_end = old_end.astimezone(collector.BUCHAREST).replace(tzinfo=None)
            event = evidence_ledger.normalize_collector_snapshot({"fathom": {"meetings": [{
                **meeting_attrs, "start": source_start.isoformat(), "end": source_end.isoformat(),
            }]}})[0]
            self.assertNotIn("recording_id", event.attributes)
        else:
            event = evidence_ledger.evidence_event(
                "fathom" if recording else "repository_events",
                {"source_type": "fathom" if recording else "repository_events", "source_id": "recording-123"},
                observed_at=START.isoformat(), raw_source_span={"start": START.isoformat(), "end": old_end.isoformat()},
                attributes={"description": "Source-accounted accomplishment"},
            )
        ledger = evidence_ledger.EvidenceLedger((event,))
        ledger_doc = {"schema_version": ledger.manifest.schema_version, "manifest": ledger.manifest.document(), "events": [event.document()]}
        for index, minutes in enumerate(prior_minutes):
            if prior_offsets is not None:
                cursor = START + dt.timedelta(minutes=prior_offsets[index])
            prior = proposal_fixtures._proposal(f"prior-{index}", [event.evidence_id], "Original immutable draft", cursor.isoformat(), minutes)
            prior["clockify_project_suffix"] = "777777"
            priors.append(prior)
            row = native_fixtures.row(f"wka-prior-{index}-s01", cursor.astimezone(dt.timezone(dt.timedelta(hours=3))).strftime("%Y-%m-%d %H:%M:%S"),
                                      (cursor + dt.timedelta(minutes=minutes)).astimezone(dt.timezone(dt.timedelta(hours=3))).strftime("%Y-%m-%d %H:%M:%S"), minutes)
            row[8] = f"Human-approved wording {index}" if intersection else "Human-approved wording"
            rows.append(row)
            cursor += dt.timedelta(minutes=minutes)
        plan = make._plan(native_fixtures.capture(*rows))
        approved = make._approval(plan)
        gateway = adoption_fixtures.UniqueGateway()
        native = adoption_fixtures.native
        native.execute_plan(plan, approved, root / "native-events.jsonl", root / "native-receipt.json", gateway, now=NOW)
        for entry in gateway.entries:
            entry.update(workspaceId="workspace-1", userId="user-1")
            interval = entry["timeInterval"]
            interval["duration"] = f"PT{int((native.legacy._parse(interval['end']) - native.legacy._parse(interval['start'])).total_seconds())}S"
        current = [proposal_fixtures._proposal(f"current-{index}", [event.evidence_id], "Current editorial description", START.isoformat(), minutes)
                   for index, minutes in enumerate(current_minutes)]
        current_ledger_doc = ledger_doc
        if intersection:
            current_start, current_end = START + dt.timedelta(seconds=3), START + dt.timedelta(seconds=current_end_seconds)
            current_event = evidence_ledger.normalize_collector_snapshot({"fathom": {"meetings": [{
                **(meeting_attrs if current_meeting_attrs is None else current_meeting_attrs),
                "start": current_start.isoformat(), "end": current_end.isoformat(),
            }]}})[0]
            current_ledger = evidence_ledger.EvidenceLedger((current_event,))
            current_ledger_doc = {"schema_version": current_ledger.manifest.schema_version,
                                  "manifest": current_ledger.manifest.document(), "events": [current_event.document()]}
            current[0].update(start=current_start.isoformat(), end=current_end.isoformat(), duration_seconds=current_end_seconds - 3, duration_minutes=(current_end_seconds - 3) / 60)
            current[0]["provenance"]["evidence_ids"] = [current_event.evidence_id]
        for row in current:
            row["clockify_project_suffix"] = "other-current-route"
        artifact = adoption_fixtures.artifact
        handles = {
            "prior_proposals": artifact(root / "prior-proposals.json", priors),
            "source_ledger": artifact(root / "source-ledger.json", ledger_doc),
            "native_plan": artifact(root / "native-plan.json", plan),
            "native_approval": artifact(root / "native-approval.json", approved),
            "native_events": {"path": str(root / "native-events.jsonl"), "sha256": "sha256:" + hashlib.sha256((root / "native-events.jsonl").read_bytes()).hexdigest()},
        }
        declaration = {
            "operation_anchor": "audit/recording-123/accomplishment", "coverage_kind": kind,
            "current_review_ids": [f"{row['review_activity_key']}-s01" for row in current],
            "artifacts": {"current_proposals": artifact(root / "current-proposals.json", current),
                          "current_source_ledger": artifact(root / "current-source-ledger.json", current_ledger_doc)},
            "prior_entries": [{"prior_review_id": f"wka-prior-{index}-s01", "clockify_entry_id": entry["id"], "artifacts": handles}
                              for index, entry in enumerate(gateway.entries)],
        }
        proof = snapshot.ClockifyCheckpointSnapshot(gateway.entries, {"request": {"workspace_id": "workspace-1", "user_id": "user-1"}}, "a" * 64, {})
        return current, declaration, proof

    def seal(self, declaration):
        return adoption.build_recurring_credit(declaration, workspace_id="workspace-1", member_id="user-1")

    def apply(self, proposals, credits, proof, existing_blocks=None):
        return pipeline._apply_verified_posted_credits(proposals, existing_blocks or [], credits, collection_snapshot=proof)

    def test_retained_only_offsetless_fathom_span_credits_837_and_preserves_four_seconds(self):
        with tempfile.TemporaryDirectory() as tmp:
            current, declaration, proof = self.fixture(Path(tmp), prior_minutes=(14,), current_minutes=(14,),
                kind="source_native_meeting_intersection", prior_source_local=True)
            try:
                credit = self.seal(declaration)
            except ValueError as error:
                self.fail(f"historical Bucharest Fathom span rejected: {error}")
            rows, credited = self.apply(current, [credit], proof)
            self.assertEqual(1, len(credit["prior_proofs"]))
            self.assertEqual(1, len(credited[0]["clockify_entry_ids"]))
            self.assertEqual(837, credited[0]["verified_posted_credit"]["covered_seconds"])
            self.assertEqual(4, rows[0]["duration_seconds"])
            self.assertEqual("2026-09-07T09:14:00+00:00", rows[0]["start"])
            self.assertEqual("2026-09-07T09:14:04+00:00", rows[0]["end"])
            self.assertNotIn("credited_overlap_receipt", credited[0])

    def test_offsetless_fathom_span_uses_bucharest_winter_offset(self):
        event = evidence_ledger.normalize_collector_snapshot({"fathom": {"meetings": [{
            "recording_id": "winter-fixture", "share_url": "https://fathom.video/share/winter-fixture",
            "start": "2026-12-07T11:00:00", "end": "2026-12-07T11:14:00",
        }]}})[0].document()
        proposal = {"start": "2026-12-07T09:00:00Z", "end": "2026-12-07T09:14:00Z", "duration_seconds": 840}
        try:
            identity = adoption._native_meeting_identity([event], proposal)
        except ValueError as error:
            self.fail(f"winter Bucharest Fathom span rejected: {error}")
        self.assertEqual(("fathom", "winter-fixture", "https://fathom.video/share/winter-fixture"), identity)

    def test_fathom_span_keeps_explicit_offset_and_rejects_malformed_time(self):
        proposal = {"start": "2026-09-07T09:00:00Z", "end": "2026-09-07T09:14:00Z", "duration_seconds": 840}
        def event(start, end):
            return evidence_ledger.normalize_collector_snapshot({"fathom": {"meetings": [{
                "recording_id": "offset-fixture", "share_url": "https://fathom.video/share/offset-fixture",
                "start": start, "end": end,
            }]}})[0].document()
        self.assertEqual("offset-fixture", adoption._native_meeting_identity(
            [event("2026-09-07T10:00:00+01:00", "2026-09-07T10:14:00+01:00")], proposal)[1])
        for start, end in (("2026-09-07T10:00:00+02:00", "2026-09-07T10:14:00+02:00"),
                           ("not-a-timestamp", "2026-09-07T12:14:00"),
                           ("2026-09-07T12:00:00", "not-a-timestamp")):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                adoption._native_meeting_identity([event(start, end)], proposal)

    def test_native_recording_precision_drift_credits_union_and_retains_four_second_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            current, declaration, proof = self.fixture(Path(tmp), prior_minutes=(14, 14), prior_offsets=(0, 0),
                                                       current_minutes=(14,), kind="source_native_meeting_intersection")
            credit = self.seal(declaration)
            rows, credited = self.apply(current, [credit], proof)
            self.assertEqual(1, len(rows))
            self.assertEqual(4, rows[0]["duration_seconds"])
            self.assertEqual("2026-09-07T09:14:00+00:00", rows[0]["start"])
            self.assertEqual("2026-09-07T09:14:04+00:00", rows[0]["end"])
            self.assertEqual(837, rows[0]["provenance"]["credited_overlap_receipt"]["credited_seconds"])
            self.assertEqual(837, credited[0]["verified_posted_credit"]["covered_seconds"])
            self.assertNotIn("credited_overlap_receipt", credited[0])  # Partial credit must not tombstone its live tail.
            self.assertEqual(2, len(credited[0]["clockify_entry_ids"]))
            self.assertNotEqual(credit["current_targets"][0]["source_events"][0]["evidence_id"],
                                credit["prior_proofs"][0]["source_events"][0]["evidence_id"])

    def test_native_recording_credit_rejects_missing_conflicting_identity_or_share_url(self):
        for attrs in ({}, {"share_url": "https://fathom.video/share/fixture-123"},
                      {"recording_id": "different", "share_url": "https://fathom.video/share/fixture-123"},
                      {"recording_id": "recording-123"},
                      {"recording_id": "recording-123", "share_url": "https://fathom.video/share/fixture-123", "provider_recording_id": "different"},
                      {"recording_id": "recording-123", "share_url": "https://fathom.video/share/different"}):
            with self.subTest(attrs=attrs), tempfile.TemporaryDirectory() as tmp:
                _, declaration, _ = self.fixture(Path(tmp), prior_minutes=(14,), current_minutes=(14,),
                                                kind="source_native_meeting_intersection", current_meeting_attrs=attrs)
                with self.assertRaises(ValueError):
                    self.seal(declaration)

    def test_native_recording_credit_never_credits_unverified_or_drifted_native_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            current, declaration, proof = self.fixture(Path(tmp), prior_minutes=(14,), current_minutes=(14,),
                                                       kind="source_native_meeting_intersection")
            credit = self.seal(declaration)
            for mutation in ("missing", "interval", "scope"):
                entries = copy.deepcopy(proof.entries)
                if mutation == "missing": entries = []
                elif mutation == "interval": entries[0]["timeInterval"]["end"] = "2026-09-07T09:13:00Z"
                else: entries[0]["userId"] = "other-member"
                changed = snapshot.ClockifyCheckpointSnapshot(entries, proof.manifest, proof.manifest_sha256, {})
                self.assertEqual((current, []), self.apply(current, [credit], changed))
            self.assertEqual((current, []), self.apply(current, [credit], None))

    def test_native_recording_credit_rejects_ambiguous_source_and_tampered_proof(self):
        with tempfile.TemporaryDirectory() as tmp:
            current, declaration, proof = self.fixture(Path(tmp), prior_minutes=(14,), current_minutes=(14,),
                                                       kind="source_native_meeting_intersection")
            original = self.seal(declaration)
            credit = copy.deepcopy(original)
            target = credit["current_targets"][0]
            old = target["source_events"][0]
            extra = evidence_ledger.evidence_event("fathom", {"source_type": "fathom", "source_id": "other"},
                observed_at=old["observed_at"], raw_source_span=old["raw_source_span"],
                attributes={"recording_id": "other", "share_url": "https://fathom.video/share/fixture-123"})
            target["source_events"].append(extra.document())
            target["proposal"]["provenance"]["evidence_ids"].append(extra.evidence_id)
            target["proposal_digest"] = adoption.recurring_proposal_digest(target["proposal"])
            credit["credit_digest"] = adoption_fixtures.native._document_digest(credit, "credit_digest")
            with self.assertRaises(ValueError): adoption.validate_recurring_credit(credit)
            credit = copy.deepcopy(original)
            credit["prior_proofs"][0]["native_confirmed"]["clockify_entry_id"] = "unverified"
            credit["credit_digest"] = adoption_fixtures.native._document_digest(credit, "credit_digest")
            self.assertEqual((current, []), self.apply(current, [credit], proof))
            credit = copy.deepcopy(original)
            credit["prior_proofs"][0]["source_events"] = []
            credit["credit_digest"] = adoption_fixtures.native._document_digest(credit, "credit_digest")
            self.assertEqual((current, []), self.apply(current, [credit], proof))
            credit = copy.deepcopy(original)
            target = credit["current_targets"][0]
            old = target["source_events"][0]
            conflicting = evidence_ledger.evidence_event("fathom", old["source_ref"],
                observed_at=old["observed_at"], raw_source_span=old["raw_source_span"],
                attributes={**old["attributes"], "recording_id": "conflicting-optional-id"})
            target["source_events"] = [conflicting.document()]
            target["proposal"]["provenance"]["evidence_ids"] = [conflicting.evidence_id]
            target["proposal_digest"] = adoption.recurring_proposal_digest(target["proposal"])
            credit["credit_digest"] = adoption_fixtures.native._document_digest(credit, "credit_digest")
            self.assertEqual((current, []), self.apply(current, [credit], proof))

    def accounting_run_fixture(self, root, proof, *, current_source=False):
        since, until = START.replace(day=1, hour=0), START.replace(month=10, day=1, hour=0)
        store = collector_checkpoints.PageCheckpointStore(root / "cache")
        state = store.open(collector._clockify_checkpoint_identity("workspace-1", "user-1", since, until), initial_metadata={"snapshot_at": "2026-10-04T12:00:00Z"})
        state = store.append_page(state, payload=proof.entries, continuation={"page": 2}, signature=collector._clockify_page_signature(proof.entries))
        state = store.mark_complete(state)
        source = root / "original-run/evidence/clockify-existing.json"
        source.parent.mkdir(parents=True)
        projection = collector.fetch_clockify({"CLOCKIFY_WORKSPACE_ID": "workspace-1"}, {"clockify_user_id": "user-1"}, since, until, checkpoint_store=store)
        source.write_text(json.dumps(projection))
        run_dir = root / "fresh-run"
        destination = run_dir / "evidence/clockify-native-checkpoint"
        captured = snapshot.capture_checkpoint_snapshot(checkpoint_manifest=state.directory / "manifest.json",
            clockify_evidence=source, destination=destination, workspace_id="workspace-1", user_id="user-1", since=since, until=until)
        (run_dir / "evidence/clockify-existing.json").write_bytes(source.read_bytes())
        original = json.loads((root / ("current-source-ledger.json" if current_source else "source-ledger.json")).read_bytes())
        events = [evidence_ledger.EvidenceEvent.from_document(event) for event in original["events"]]
        events.extend(evidence_ledger.normalize_collector_snapshot({"clockify": projection}))
        ledger = evidence_ledger.EvidenceLedger(tuple(events), {"clockify": {"status": "complete"}, "fathom": {"status": "complete"}, "multica_issues": {"status": "complete"}})
        ledger_doc = {"schema_version": ledger.manifest.schema_version, "manifest": ledger.manifest.document(), "events": [event.document() for event in ledger.events]}
        (run_dir / "evidence/evidence-ledger.json").write_text(json.dumps(ledger_doc))
        report = {"date_range": {"since": since.isoformat(), "until": until.isoformat()}, "clockify_native_checkpoint": {"manifest_sha256": captured.manifest_sha256, "request": captured.manifest["request"]}}
        (run_dir / "run-report.json").write_text(json.dumps(report))
        analysis = root / "analysis.json"
        analysis.write_text(json.dumps(accounting_fixtures.analysis_for([events[0].evidence_id], recommended=30)))
        return run_dir, analysis, ledger_doc, report

    def test_accounting_native_intersection_retains_four_seconds_without_tombstoning_tail(self):
        self.accounting_intersection_fixture(full_coverage=False)

    def test_native_intersection_residual_rebinds_real_ledger_overlap_warnings_for_quality(self):
        from scripts import clockify_sync_quality as quality
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, declaration, proof = self.fixture(root, prior_minutes=(14,), current_minutes=(14,),
                kind="source_native_meeting_intersection", prior_source_local=True)
            manual = copy.deepcopy(proof.entries[0])
            manual.update(id="retained-manual-fixture", description="Independent manual meeting")
            manual["timeInterval"].update(end=(START + dt.timedelta(minutes=15, seconds=48)).isoformat(), duration="PT948S")
            proof.entries.append(manual)
            native_before = copy.deepcopy(proof.entries)
            run_dir, analysis, ledger_doc, _ = self.accounting_run_fixture(root, proof, current_source=True)
            baseline = pipeline.run_accounting(run_dir, root=accounting_fixtures.ROOT, analysis_fixture=analysis)
            target = baseline["proposals"][0]
            declaration["artifacts"] = {
                "current_proposals": adoption_fixtures.artifact(root / "accounted-proposals.json", baseline["proposals"]),
                "current_source_ledger": adoption_fixtures.artifact(root / "accounted-ledger.json", ledger_doc),
            }
            declaration["current_review_ids"] = [adoption._review_id(target)]
            credit = self.seal(declaration)
            captured = pipeline._accounting_collection_snapshot(run_dir, ledger_doc["events"])
            rows, credited = self.apply(baseline["proposals"], [credit], captured,
                                       pipeline._existing_blocks(ledger_doc["events"]))
            existing = json.loads((run_dir / "evidence/clockify-existing.json").read_text())["entries"]
            self.assertEqual([], quality.find_time_overlaps(rows, existing))
            self.assertEqual(837, credited[0]["verified_posted_credit"]["covered_seconds"])
            self.assertEqual(4, rows[0]["duration_seconds"])
            overlaps = [w for w in rows[0]["review_warnings"] if w["type"] == "existing_clockify_overlap"]
            self.assertEqual(1, len(overlaps))
            manual_event = evidence_ledger._snapshot_event("clockify", existing[1], 2)
            self.assertEqual(manual_event.evidence_id, overlaps[0]["counterpart_id"])
            self.assertEqual(rows[0]["start"], overlaps[0]["overlap_start"])
            self.assertEqual(rows[0]["end"], overlaps[0]["overlap_end"])
            self.assertEqual(4, overlaps[0]["overlap_duration_seconds"])
            self.assertEqual([w for w in target["review_warnings"] if w["type"] != "existing_clockify_overlap"],
                             [w for w in rows[0]["review_warnings"] if w["type"] != "existing_clockify_overlap"])
            self.assertEqual(native_before, proof.entries)
            self.assertEqual(native_before, captured.entries)
            self.assertEqual([], baseline["review_tombstones"])

    def test_native_intersection_middle_credit_clips_warnings_for_both_disjoint_residuals(self):
        from scripts import clockify_sync_quality as quality
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current, declaration, proof = self.fixture(root, prior_minutes=(3,), prior_offsets=(3,), current_minutes=(14,),
                kind="source_native_meeting_intersection", prior_source_span_minutes=14)
            existing = [{"id": "manual-fixture", "start": START.isoformat(),
                         "end": (START + dt.timedelta(minutes=15)).isoformat(), "project_id_suffix": "manual"},
                        {"id": "posted-fixture", "start": (START + dt.timedelta(minutes=3)).isoformat(),
                         "end": (START + dt.timedelta(minutes=6)).isoformat(), "project_id_suffix": "posted"}]
            warnings, blocks = [], []
            for index, entry in enumerate(existing, 1):
                block = {"block_id": evidence_ledger._snapshot_event("clockify", entry, index).evidence_id,
                         "start": pipeline._parse_dt(entry["start"]), "end": pipeline._parse_dt(entry["end"]),
                         "project_id_suffix": entry["project_id_suffix"]}
                block["kind"] = "existing_clockify"
                blocks.append(block)
                warnings.append(pipeline._overlap_warning(pipeline._parse_dt(current[0]["start"]),
                    pipeline._parse_dt(current[0]["end"]), block, "existing_clockify_overlap"))
            note = {"type": "unresolved_routing", "reason_code": "no_deterministic_route"}
            current[0]["billable"] = True
            current[0]["review_warnings"] = [*warnings, note]
            declaration["artifacts"]["current_proposals"] = adoption_fixtures.artifact(root / "current-with-warnings.json", current)
            rows, credited = self.apply(current, [self.seal(declaration)], proof, blocks)
            self.assertEqual([177, 484], [row["duration_seconds"] for row in rows])
            self.assertEqual(180, credited[0]["verified_posted_credit"]["covered_seconds"])
            self.assertEqual([], quality.find_time_overlaps(rows, existing))
            for row in rows:
                overlaps = [w for w in row["review_warnings"] if w["type"] == "existing_clockify_overlap"]
                self.assertEqual(1, len(overlaps))
                self.assertEqual(warnings[0]["counterpart_id"], overlaps[0]["counterpart_id"])
                self.assertEqual(row["duration_seconds"], overlaps[0]["overlap_duration_seconds"])
                self.assertIn(note, row["review_warnings"])
                self.assertEqual(current[0]["billable"], row["billable"])
                self.assertEqual(current[0]["clockify_project_suffix"], row["clockify_project_suffix"])

    def test_native_intersection_preserves_unknown_and_inexact_ledger_warnings(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current, declaration, proof = self.fixture(root, prior_minutes=(14,), current_minutes=(14,),
                kind="source_native_meeting_intersection")
            block = {"block_id": "ev-known", "kind": "existing_clockify", "start": START,
                     "end": START + dt.timedelta(minutes=14), "project_id_suffix": "known"}
            unknown = pipeline._overlap_warning(pipeline._parse_dt(current[0]["start"]),
                pipeline._parse_dt(current[0]["end"]), {**block, "block_id": "ev-unknown"}, "existing_clockify_overlap")
            inexact = {**unknown, "counterpart_id": "ev-known", "overlap_start": START.isoformat(),
                       "overlap_duration_seconds": 840}
            wrong_project = {**unknown, "counterpart_id": "ev-known", "counterpart_project_suffix": "other"}
            current[0]["review_warnings"] = [unknown, inexact, wrong_project]
            declaration["artifacts"]["current_proposals"] = adoption_fixtures.artifact(root / "current-invalid-warnings.json", current)
            rows, _ = self.apply(current, [self.seal(declaration)], proof, [block])
            self.assertEqual(4, rows[0]["duration_seconds"])
            self.assertEqual([unknown, inexact, wrong_project], rows[0]["review_warnings"])

    def test_accounting_native_intersection_full_coverage_emits_publisher_tombstone(self):
        result, baseline = self.accounting_intersection_fixture(full_coverage=True)
        from scripts import clockify_sheet_publish as publisher
        from test_sheet_publish import StatefulGateway
        row = publisher.proposal_row(baseline["proposals"][0], "fixture-run")
        row[14] = "Preserve human note"
        gateway = StatefulGateway([publisher.HEADER, row])
        self.assertEqual(1, publisher._apply_tombstones(
            gateway, spreadsheet_id="sheet", sheet_title="August 2026 review",
            tombstones=result["review_tombstones"]))
        self.assertEqual(row[:9], gateway.rows[1][:9])
        self.assertEqual("superseded", gateway.rows[1][9])
        self.assertEqual("superseded", gateway.rows[1][13])
        self.assertEqual("Preserve human note", gateway.rows[1][14])

    def accounting_intersection_fixture(self, *, full_coverage):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _, declaration, proof = self.fixture(root, prior_minutes=(14, 14), prior_offsets=(0, 0),
                                                 current_minutes=(14,), kind="source_native_meeting_intersection",
                                                 current_end_seconds=840 if full_coverage else 844)
            run_dir, analysis, ledger_doc, _ = self.accounting_run_fixture(root, proof, current_source=True)
            baseline = pipeline.run_accounting(run_dir, root=accounting_fixtures.ROOT, analysis_fixture=analysis)
            self.assertEqual(837 if full_coverage else 841, baseline["proposals"][0]["duration_seconds"])
            declaration["artifacts"] = {
                "current_proposals": adoption_fixtures.artifact(root / "current-accounting-proposals.json", baseline["proposals"]),
                "current_source_ledger": adoption_fixtures.artifact(root / "current-accounting-ledger.json", ledger_doc),
            }
            declaration["current_review_ids"] = [adoption._review_id(baseline["proposals"][0])]
            credit = self.seal(declaration)
            corrections = root / "review-corrections.jsonl"
            captured = pipeline._accounting_collection_snapshot(run_dir, ledger_doc["events"])
            self.assertTrue(review_corrections.append_verified_posted_credit(
                corrections, credit, runs_root=run_dir.parent, current_proposals=baseline["proposals"],
                existing_blocks=[], collection_snapshot=captured))
            result = pipeline.run_accounting(run_dir, root=accounting_fixtures.ROOT, analysis_fixture=analysis,
                                             corrections_path=corrections)
            if full_coverage:
                self.assertEqual([], result["proposals"])
                self.assertEqual(1, len(result["review_tombstones"]))
                self.assertEqual(837, result["review_tombstones"][0]["credited_overlap_receipt"]["credited_seconds"])
                return result, baseline
            self.assertEqual(1, len(result["proposals"]))
            self.assertEqual(4, result["proposals"][0]["duration_seconds"])
            self.assertEqual([], result["review_tombstones"])
            from scripts import clockify_sheet_publish as publisher
            self.assertEqual(4 / 60, publisher.proposal_row(result["proposals"][0], "fixture-run")[3])

    def test_equal_credit_uses_approved_editorial_payload_and_preserves_independent_work(self):
        with tempfile.TemporaryDirectory() as tmp:
            current, declaration, proof = self.fixture(Path(tmp))
            independent = copy.deepcopy(current[0])
            independent.update(activity_id="independent", review_activity_key="wka-independent", candidate_key="independent")
            credit = self.seal(declaration)
            survivors, skipped = self.apply([*current, independent], [credit], proof)
            self.assertEqual([independent], survivors)
            self.assertEqual(1, len(skipped))
            self.assertEqual("preserved_collection_snapshot", skipped[0]["verification_basis"])
            self.assertEqual("Human-approved wording", credit["prior_proofs"][0]["payload"]["description"])
            self.assertEqual("project-123456", credit["prior_proofs"][0]["payload"]["projectId"])

    def test_disjoint_prior_group_covers_exact_sum(self):
        with tempfile.TemporaryDirectory() as tmp:
            current, declaration, proof = self.fixture(Path(tmp), prior_minutes=(2, 1), current_minutes=(3,), kind="disjoint_aggregate")
            survivors, skipped = self.apply(current, [self.seal(declaration)], proof)
            self.assertEqual([], survivors)
            self.assertEqual(1, len(skipped))

    def test_one_equal_unit_covers_explicit_equal_duration_aliases_only(self):
        """Catches summing duplicate aliases instead of each matching one prior."""
        for minutes in (2, 4):
            with self.subTest(minutes=minutes), tempfile.TemporaryDirectory() as tmp:
                current, declaration, proof = self.fixture(
                    Path(tmp), prior_minutes=(minutes,), current_minutes=(minutes, minutes))
                independent = copy.deepcopy(current[0])
                independent.update(activity_id="independent", review_activity_key="wka-independent",
                                   candidate_key="independent")
                credit = self.seal(declaration)
                survivors, skipped = self.apply([*current, independent], [credit], proof)
                self.assertEqual([independent], survivors)
                self.assertEqual(2, len(skipped))
                self.assertTrue(all(row["clockify_entry_ids"] == ["created-1"] for row in skipped))

    def test_equal_aliases_require_each_duration_to_match_one_prior(self):
        for priors, currents in (((2,), (2, 1)), ((2,), (1, 1)), ((2, 2), (4,))):
            with self.subTest(priors=priors, currents=currents), tempfile.TemporaryDirectory() as tmp:
                _current, declaration, _proof = self.fixture(
                    Path(tmp), prior_minutes=priors, current_minutes=currents)
                with self.assertRaises(ValueError):
                    self.seal(declaration)

    def test_disjoint_aggregate_does_not_accept_multiple_current_aliases(self):
        with tempfile.TemporaryDirectory() as tmp:
            _current, declaration, _proof = self.fixture(
                Path(tmp), prior_minutes=(2, 1), current_minutes=(2, 1), kind="disjoint_aggregate")
            with self.assertRaises(ValueError):
                self.seal(declaration)

    def test_equal_aliases_in_separate_units_cannot_reuse_one_prior(self):
        with tempfile.TemporaryDirectory() as tmp:
            current, declaration, proof = self.fixture(
                Path(tmp), prior_minutes=(2,), current_minutes=(2, 2))
            units = []
            for review_id in declaration["current_review_ids"]:
                unit = copy.deepcopy(declaration)
                unit["current_review_ids"] = [review_id]
                units.append(self.seal(unit))
            self.assertEqual((current, []), self.apply(current, units, proof))

    def test_one_whole_recording_unit_covers_both_declared_aliases_without_offsets(self):
        with tempfile.TemporaryDirectory() as tmp:
            current, declaration, proof = self.fixture(Path(tmp), prior_minutes=(86,), current_minutes=(15, 6), kind="whole_recording_aliases")
            survivors, skipped = self.apply(current, [self.seal(declaration)], proof)
            self.assertEqual([], survivors)
            self.assertEqual(2, len(skipped))
            self.assertTrue(all(row["clockify_entry_ids"] == ["created-1"] for row in skipped))

    def test_missing_duplicate_or_drifted_collection_entry_does_not_suppress(self):
        for mutation in ("missing", "duplicate", "description", "projectId", "taskId", "billable", "tagIds", "workspaceId", "userId"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                current, declaration, proof = self.fixture(Path(tmp))
                credit = self.seal(declaration)
                if mutation == "missing": proof.entries.clear()
                elif mutation == "duplicate": proof.entries.append(copy.deepcopy(proof.entries[0]))
                else: proof.entries[0][mutation] = [] if mutation == "tagIds" else False if mutation == "billable" else "changed"
                survivors, skipped = self.apply(current, [credit], proof)
                self.assertEqual(current, survivors)
                self.assertEqual([], skipped)

    def test_reused_prior_across_units_is_rejected_before_any_suppression(self):
        with tempfile.TemporaryDirectory() as tmp:
            current, declaration, proof = self.fixture(Path(tmp))
            credit = self.seal(declaration)
            survivors, skipped = self.apply(current, [credit, copy.deepcopy(credit)], proof)
            self.assertEqual(current, survivors)
            self.assertEqual([], skipped)

    def test_sealed_replay_does_not_reopen_original_handles(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current, declaration, proof = self.fixture(root)
            credit = self.seal(declaration)
            for path in root.iterdir(): path.unlink()
            with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("original proof reopened")):
                survivors, skipped = self.apply(current, [credit], proof)
            self.assertEqual([], survivors)
            self.assertEqual(1, len(skipped))

    def test_changed_current_payload_or_unproved_recording_remains_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            current, declaration, proof = self.fixture(Path(tmp))
            credit = self.seal(declaration)
            current[0]["description"] += " changed"
            self.assertEqual((current, []), self.apply(current, [credit], proof))
            declaration["coverage_kind"] = "whole_recording_aliases"
            with self.assertRaises(ValueError): self.seal(declaration)

    def test_presentation_id_changes_do_not_lose_credit_but_ambiguous_targets_survive(self):
        """Catches binding to S/P display IDs or arbitrarily selecting duplicates."""
        with tempfile.TemporaryDirectory() as tmp:
            current, declaration, proof = self.fixture(Path(tmp))
            original_path = Path(declaration["artifacts"]["current_proposals"]["path"])
            frozen = copy.deepcopy(current)
            frozen[0]["id"] = "P001"
            declaration["artifacts"]["current_proposals"] = adoption_fixtures.artifact(original_path, frozen)
            credit = self.seal(declaration)
            current[0]["id"] = "S001"
            self.assertEqual([], self.apply(current, [credit], proof)[0])
            duplicate = copy.deepcopy(current[0])
            duplicate["id"] = "S002"
            self.assertEqual(([current[0], duplicate], []), self.apply([current[0], duplicate], [credit], proof))
            for field, value in (("description", "changed"), ("clockify_project_suffix", "changed"),
                                 ("duration_seconds", 60), ("activity_id", "changed")):
                with self.subTest(field=field):
                    changed = copy.deepcopy(current)
                    changed[0][field] = value
                    self.assertEqual((changed, []), self.apply(changed, [credit], proof))
            changed = copy.deepcopy(current)
            changed[0]["provenance"]["evidence_ids"] = ["ev-changed-source"]
            self.assertEqual((changed, []), self.apply(changed, [credit], proof))

    def test_original_source_or_approval_bytes_drift_rejects_sealing(self):
        for name in ("prior_proposals", "source_ledger", "native_approval", "native_events"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                _current, declaration, _proof = self.fixture(Path(tmp))
                Path(declaration["prior_entries"][0]["artifacts"][name]["path"]).write_text("{}\n")
                with self.assertRaises(ValueError): self.seal(declaration)

    def test_original_artifacts_read_once_shared_across_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            _current, declaration, _proof = self.fixture(Path(tmp), prior_minutes=(2, 1), current_minutes=(3,), kind="disjoint_aggregate")
            reads = {}
            original = Path.read_bytes
            def read(path):
                reads[str(path)] = reads.get(str(path), 0) + 1
                return original(path)
            with mock.patch.object(Path, "read_bytes", new=read): self.seal(declaration)
            self.assertTrue(reads)
            self.assertTrue(all(count == 1 for count in reads.values()))

    def test_overlapping_prior_entries_cannot_be_added_as_disjoint_credit(self):
        with tempfile.TemporaryDirectory() as tmp:
            _current, declaration, _proof = self.fixture(Path(tmp), prior_minutes=(2, 1), current_minutes=(3,),
                kind="disjoint_aggregate", prior_offsets=(0, 1))
            with self.assertRaisesRegex(ValueError, "overlaps"):
                self.seal(declaration)

    def test_rehashed_sealed_proof_cannot_bypass_native_approval_or_event_binding(self):
        for mutation in ("incomplete_payload", "intent_plan", "confirmed_payload"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                current, declaration, proof = self.fixture(Path(tmp))
                credit = self.seal(declaration)
                prior = credit["prior_proofs"][0]
                native = adoption_fixtures.native
                if mutation == "incomplete_payload":
                    del prior["payload"]["tagIds"]
                    prior["payload_digest"] = native._digest(prior["payload"])
                else:
                    event = prior["native_intent" if mutation == "intent_plan" else "native_confirmed"]
                    event["plan_digest" if mutation == "intent_plan" else "payload_digest"] = "changed"
                    event["event_digest"] = native._document_digest(event, "event_digest")
                prior["proof_digest"] = native._document_digest(prior, "proof_digest")
                credit["credit_digest"] = native._document_digest(credit, "credit_digest")
                self.assertEqual((current, []), self.apply(current, [credit], proof))

    def test_whole_recording_alias_requires_same_source_id_and_full_raw_span(self):
        for mutation in ("source_id", "raw_source_span"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                current, declaration, proof = self.fixture(Path(tmp), prior_minutes=(86,), current_minutes=(15, 6), kind="whole_recording_aliases")
                credit = self.seal(declaration)
                target = credit["current_targets"][0]
                old = target["source_events"][0]
                ref, span = copy.deepcopy(old["source_ref"]), copy.deepcopy(old["raw_source_span"])
                if mutation == "source_id": ref["source_id"] = "different-recording"
                else: span["end"] = (START + dt.timedelta(minutes=85)).isoformat()
                event = evidence_ledger.evidence_event("fathom", ref, observed_at=old["observed_at"], raw_source_span=span, attributes=old["attributes"])
                target["source_events"] = [event.document()]
                target["proposal"]["provenance"]["evidence_ids"] = [event.evidence_id]
                target["proposal_digest"] = adoption.recurring_proposal_digest(target["proposal"])
                credit["credit_digest"] = adoption_fixtures.native._document_digest(credit, "credit_digest")
                with self.assertRaisesRegex(ValueError, "same whole source"):
                    adoption.validate_recurring_credit(credit)

    def test_source_changed_during_single_read_rejects_even_when_bytes_match(self):
        with tempfile.TemporaryDirectory() as tmp:
            _current, declaration, _proof = self.fixture(Path(tmp))
            original = Path.read_bytes
            path_to_change = declaration["artifacts"]["current_proposals"]["path"]
            def changed_read(path):
                content = original(path)
                if str(path) == path_to_change:
                    stat = path.stat()
                    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
                return content
            with mock.patch.object(Path, "read_bytes", new=changed_read):
                with self.assertRaisesRegex(ValueError, "changed while being read"):
                    self.seal(declaration)

    def test_missing_snapshot_does_not_suppress(self):
        with tempfile.TemporaryDirectory() as tmp:
            current, declaration, _proof = self.fixture(Path(tmp))
            self.assertEqual((current, []), self.apply(current, [self.seal(declaration)], None))

    def test_record2_roundtrips_in_existing_correction_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            current, declaration, proof = self.fixture(Path(tmp))
            credit = self.seal(declaration)
            path = Path(tmp) / "corrections.jsonl"
            self.assertTrue(review_corrections.append_verified_posted_credit(path, credit, runs_root=Path(tmp), current_proposals=current, existing_blocks=[], collection_snapshot=proof))
            sealed = review_corrections.load_verified_posted_credits(path)
            self.assertEqual([], self.apply(current, sealed, proof)[0])

    def test_accounting_matches_finalized_recovery_warning_before_native_credit(self):
        """A sealed final proposal must match before the post-credit warning refresh."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _current, declaration, proof = self.fixture(root)
            run_dir, analysis, ledger_doc, _report = self.accounting_run_fixture(root, proof)
            with mock.patch.object(collector, "clockify_get", side_effect=AssertionError("network forbidden")):
                baseline = pipeline.run_accounting(run_dir, root=accounting_fixtures.ROOT, analysis_fixture=analysis)
            self.assertEqual(1, len(baseline["proposals"]))
            final_proposal = baseline["proposals"][0]
            warning = next(w for w in final_proposal["review_warnings"] if w["type"] == "allocation_capacity_recovery")
            self.assertEqual(0, warning["credited_minutes"])
            self.assertEqual(30, final_proposal["duration_minutes"])
            declaration["current_review_ids"] = [f'{final_proposal["review_activity_key"]}-s{final_proposal["allocation_segment"]:02d}']
            declaration["artifacts"]["current_proposals"] = adoption_fixtures.artifact(root / "final-proposals.json", [final_proposal])
            declaration["artifacts"]["current_source_ledger"] = adoption_fixtures.artifact(root / "final-source-ledger.json", ledger_doc)
            credit = self.seal(declaration)
            corrections = root / "corrections.jsonl"
            self.assertTrue(review_corrections.append_verified_posted_credit(
                corrections, credit, runs_root=root, current_proposals=[final_proposal],
                existing_blocks=[], collection_snapshot=proof,
            ))
            with mock.patch.object(collector, "clockify_get", side_effect=AssertionError("network forbidden")):
                credited = pipeline.run_accounting(
                    run_dir, root=accounting_fixtures.ROOT, analysis_fixture=analysis,
                    corrections_path=corrections,
                )
            self.assertEqual([], credited["proposals"])
            self.assertEqual(1, sum(row.get("verification_basis") == "preserved_collection_snapshot" for row in credited["skipped"]))
            native_skip = next(row for row in credited["skipped"] if row.get("verification_basis") == "preserved_collection_snapshot")
            self.assertEqual({"covered_seconds": 1800, "allocation_capacity_recovery": True}, native_skip["verified_posted_credit"])
            self.assertNotIn("credited_overlap_receipt", native_skip)
            self.assertEqual([{
                "activity_id": final_proposal["activity_id"],
                "requested_minutes": 30,
                "allocator_allocated_minutes": 0,
                "recovered_minutes": 0,
                "credited_minutes": 30,
                "residual_minutes": 0,
            }], credited["allocation"]["capacity_recoveries"])

    def test_partial_native_recovery_credit_rebinds_survivor_warning_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current, declaration, proof = self.fixture(
                root, prior_minutes=(15,), current_minutes=(15, 15),
            )
            first, remaining = current
            remaining["start"] = first["end"]
            remaining["end"] = (dt.datetime.fromisoformat(remaining["start"]) + dt.timedelta(minutes=15)).isoformat()
            first["activity_id"] = remaining["activity_id"] = "act-recovery"
            first["provenance"]["allocation_capacity_recovery"] = True
            remaining["provenance"]["allocation_capacity_recovery"] = True
            unrelated_warning = {"type": "routing_review", "detail": "preserve me"}
            remaining["review_warnings"] = [unrelated_warning]
            independent = copy.deepcopy(remaining)
            independent.update(activity_id="act-independent", candidate_key="wks-independent")
            independent["provenance"].pop("allocation_capacity_recovery")
            records = [{"activity_id": "act-recovery", "requested_minutes": 30,
                        "allocator_allocated_minutes": 0, "recovered_minutes": 30,
                        "residual_minutes": 0}]
            pipeline._refresh_capacity_recovery_warnings(current, [], records)
            declaration["current_review_ids"] = [f'{first["review_activity_key"]}-s{first["allocation_segment"]:02d}']
            declaration["artifacts"]["current_proposals"] = adoption_fixtures.artifact(root / "recovery-proposals.json", current)
            credit = self.seal(declaration)

            survivors, skipped = self.apply([*current, independent], [credit], proof)
            self.assertEqual([remaining, independent], survivors)
            self.assertEqual({"covered_seconds": 900, "allocation_capacity_recovery": True}, skipped[0]["verified_posted_credit"])
            pipeline._refresh_capacity_recovery_warnings(survivors, skipped, records)

            self.assertEqual({"activity_id": "act-recovery", "requested_minutes": 30,
                              "allocator_allocated_minutes": 0, "recovered_minutes": 15,
                              "credited_minutes": 15, "residual_minutes": 0}, records[0])
            self.assertEqual(unrelated_warning, remaining["review_warnings"][0])
            self.assertEqual({"type": "allocation_capacity_recovery", "requested_minutes": 30,
                              "allocator_allocated_minutes": 0, "recovered_minutes": 15,
                              "credited_minutes": 15, "residual_minutes": 0}, remaining["review_warnings"][1])
            self.assertEqual([unrelated_warning], independent["review_warnings"])

    def test_mixed_v1_v2_log_appends_in_both_orders_without_losing_v1_checks(self):
        """Catches v1 indexing v2-only records as legacy proof fields."""
        for schemas in ((2, 1), (1, 2)):
            with self.subTest(schemas=schemas), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                current, declaration, proof = self.fixture(root)
                recurring = self.seal(declaration)
                legacy_current, legacy_blocks, legacy_credits = proposal_fixtures.VerifiedPostedCreditTests().fixture(root)
                path = root / "mixed-corrections.jsonl"
                records = {1: legacy_credits[0], 2: recurring}
                kwargs = {
                    1: dict(runs_root=root, current_proposals=legacy_current, existing_blocks=legacy_blocks),
                    2: dict(runs_root=root, current_proposals=current, existing_blocks=[], collection_snapshot=proof),
                }
                for schema in schemas:
                    self.assertTrue(review_corrections.append_verified_posted_credit(path, records[schema], **kwargs[schema]))
                self.assertEqual(list(schemas), [row["schema_version"] for row in review_corrections.load_verified_posted_credits(path)])
                for schema in schemas:
                    self.assertFalse(review_corrections.append_verified_posted_credit(path, records[schema], **kwargs[schema]))
                with self.assertRaises(review_corrections.ReviewDecisionError):
                    review_corrections.append_verified_posted_credit(path, records[1], runs_root=root, current_proposals=legacy_current, existing_blocks=[])
                sealed = review_corrections.load_verified_posted_credits(path)
                survivors, skipped = pipeline._apply_verified_posted_credits([*current, *legacy_current], legacy_blocks, sealed, collection_snapshot=proof)
                self.assertEqual(["new-runtime", "unrelated"], [row["activity_id"] for row in survivors])
                self.assertEqual(2, len(skipped))

    def test_run_accounting_consumes_preserved_snapshot_before_proposal_output(self):
        """Catches a standalone credit helper never wired into actual accounting."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current, declaration, proof = self.fixture(root)
            credit = self.seal(declaration)
            corrections = root / "corrections.jsonl"
            review_corrections.append_verified_posted_credit(corrections, credit, runs_root=root,
                current_proposals=current, existing_blocks=[], collection_snapshot=proof)
            run_dir, analysis, ledger_doc, report = self.accounting_run_fixture(root, proof)
            # Control only the pure allocator/normalizer boundary. The run,
            # snapshot validation, correction transport and final outputs are real.
            with mock.patch.object(pipeline, "_normalize_postable_proposals", return_value=copy.deepcopy(current)), \
                    mock.patch.object(collector, "clockify_get", side_effect=AssertionError("network forbidden")):
                result = pipeline.run_accounting(run_dir, root=accounting_fixtures.ROOT, analysis_fixture=analysis, corrections_path=corrections)
            self.assertEqual([], result["proposals"])
            self.assertTrue(any(row.get("verification_basis") == "preserved_collection_snapshot" for row in result["skipped"]))
            # A valid full-native proof must also bind this actual run's ledger
            # and exact sanitized collector bytes, not just exist beside them.
            ledger = evidence_ledger.EvidenceLedger(tuple(evidence_ledger.EvidenceEvent.from_document(event) for event in ledger_doc["events"]), {"clockify": {"status": "complete"}, "fathom": {"status": "complete"}, "multica_issues": {"status": "complete"}})
            without_clockify = evidence_ledger.EvidenceLedger(tuple(event for event in ledger.events if event.source_type != "clockify"), ledger.source_inventory)
            mutations = {
                "ledger_projection": (run_dir / "evidence/evidence-ledger.json", json.dumps({"schema_version": without_clockify.manifest.schema_version,
                    "manifest": without_clockify.manifest.document(), "events": [event.document() for event in without_clockify.events]}).encode()),
                "evidence_bytes": (run_dir / "evidence/clockify-existing.json", b"{}\n"),
                "missing_metadata": (run_dir / "run-report.json", json.dumps({"date_range": report["date_range"]}).encode()),
            }
            for mutation, (path, changed) in mutations.items():
                with self.subTest(mutation=mutation):
                    original_bytes = path.read_bytes()
                    path.write_bytes(changed)
                    try:
                        with mock.patch.object(pipeline, "_normalize_postable_proposals", return_value=copy.deepcopy(current)), \
                                mock.patch.object(collector, "clockify_get", side_effect=AssertionError("network forbidden")):
                            result = pipeline.run_accounting(run_dir, root=accounting_fixtures.ROOT, analysis_fixture=analysis, corrections_path=corrections)
                        self.assertEqual(1, len(result["proposals"]))
                        self.assertFalse(any(row.get("verification_basis") == "preserved_collection_snapshot" for row in result["skipped"]))
                    finally:
                        path.write_bytes(original_bytes)


if __name__ == "__main__": unittest.main()
