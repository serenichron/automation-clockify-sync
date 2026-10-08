"""A reviewed historical serialization witness is coverage, never new credit."""
import copy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

from scripts import clockify_pending_review_selection as consumer
from scripts import clockify_review_run as review, clockify_review_cycle as cycle, collector_receipts, evidence_ledger
from scripts.collector_slices import CollectionSlice
import test_collector_receipts as collector_fixtures
import test_pending_review_meeting_credits as coverage_fixtures


class LegacySerializationCoverageTests(unittest.TestCase):
    def fixture(self, root):
        builder = coverage_fixtures.CoveredPostedAccomplishmentTests()
        item, rows, declaration, capture, witness = builder.fixture(root)
        prior_handle = declaration["prior_proof_artifacts"]["prior_proposals"]
        prior = json.loads(Path(prior_handle["path"]).read_text())[0]
        graphs, cited = {}, {}
        for label, proposal, run in (("current", item["proposal"], root/"current-run"),
                                     ("prior", prior, root/"prior-run")):
            events = [{"role": "assistant", "kind": "message", "content": f"Verified bounded delivery result {n}.",
                       "timestamp": f"2026-09-29T12:0{n}:01+03:00" if label == "current" else f"2026-09-29 12:0{n}"}
                      for n in range(6)]
            raw = {"clockify": {"status": "complete", "entries": []},
                   "fathom": {"status": "complete", "meetings": []},
                   "calendly": {"status": "complete", "recordings": []},
                   "multica_issues": {"status": "complete", "issues": []},
                   "sessions": [{"machine": "test-host", "codex_sessions": [{"session_id": "review-session", "events": events}]}]}
            ledger = evidence_ledger.EvidenceLedger(tuple(evidence_ledger.normalize_collector_snapshot(raw)),
                       evidence_ledger.source_inventory_from_collector(raw))
            document = {"schema_version": "evidence-ledger/v1", "manifest": ledger.manifest.document(),
                        "events": [e.document() for e in ledger.events]}
            cited[label] = [e for e in document["events"] if e["source_type"] == "codex_sessions_event"]
            proposal["provenance"]["evidence_ids"] = [e["evidence_id"] for e in cited[label]]
            if label == "current":
                proposal.update(start="2026-09-29T09:00:01+00:00", end="2026-09-29T09:30:01+00:00")
                item["source"]["ledger"] = document
                item["atoms"] = {consumer._atom(e) for e in cited[label]}
            # Genuine collector inventory, sealed completion and native semantic
            # artifacts: no observer or posting-verification mocks.
            helper = collector_fixtures.CompletionBundleTests(); helper.setUp(); helper._write_required_artifacts(run)
            for name, filename in (("clockify", "clockify-existing.json"), ("fathom", "fathom-meetings.json"),
                                   ("calendly", "calendly-recordings.json"), ("multica_issues", "multica-issues.json"),
                                   ("sessions", "sessions.json")):
                builder.write(run/"evidence"/filename, raw[name])
            ledger_handle = builder.write(run/"evidence/evidence-ledger.json", document)
            report = {"runtime_identity": {"git_sha": "fixture"},
                      "date_range": {"since": "2026-09-28T21:00:00Z", "until": "2026-09-29T21:00:00Z"},
                      "evidence_ledger": {"source_completeness": document["manifest"]["source_completeness"]}}
            builder.write(run/"run-report.json", report)
            proposal_handle = builder.write(run/"proposals.json", [proposal])
            builder.write(run/"work-accounting-result.json", {"proposals": [proposal]})
            activity = {"activity_id": proposal["activity_id"], "evidence_ids": proposal["provenance"]["evidence_ids"],
                        "action": "Reviewed", "object": "deployment patch", "outcome": "Verified delivery fix"}
            witness[label+"_semantic"] = {"artifact": builder.write(run/"semantic-analysis.json", {"activities": [activity]}),
                                           "activity": activity}
            for filename in review._RECONCILIATION_INPUTS.values():
                if not (run/filename).exists(): builder.write(run/filename, {})
            slice_ = CollectionSlice(dt.datetime(2026, 9, 29, tzinfo=dt.timezone(dt.timedelta(hours=3))),
                                     dt.datetime(2026, 9, 30, tzinfo=dt.timezone(dt.timedelta(hours=3))), label+"-slice")
            collector_receipts.write_completion_bundle(run/"completion-bundle.json",
                        collector_receipts.build_completion_bundle(run, slice_=slice_))
            bundle = collector_receipts.load_collector_source_bundle(run/"completion-bundle.json", run_dir=run)
            graphs[label] = {"runs_root": str(root), "semantic_completion": consumer.artifact_handle(run/"completion-bundle.json"),
                             "raw_completion": consumer.artifact_handle(run/"completion-bundle.json"),
                             "raw_ancestor": str(run), "observer_stage": {"run_dir": str(run), "slice_id": bundle.slice_id,
                             "since_utc": bundle.since_utc, "until_utc": bundle.until_utc,
                             "compatibility_version": "collector-completion-bundle/v1",
                             "bundle_digest": bundle.legacy_completion_bundle_digest}}
            if label == "current":
                item["source"]["artifacts"].update(proposals=proposal_handle, ledger=ledger_handle)
            else:
                declaration["prior_proof_artifacts"].update(prior_proposals=proposal_handle, source_ledger=ledger_handle)
        witness.update(schema_version="pending-covered-legacy-serialization-adjudication/v1",
                       current_proposal_sha256=consumer.digest(item["proposal"]), prior_proposal_sha256=consumer.digest(prior),
                       canonical_atoms_sha256=consumer.digest(sorted(consumer.digest(a) for a in item["atoms"])))
        witness["legacy_serialization"] = {"current_graph": graphs["current"], "prior_graph": graphs["prior"],
            "interval_offsets_seconds": {"start": 1, "end": 1},
            "event_pairs": [{"current_evidence_id": c["evidence_id"], "prior_evidence_id": p["evidence_id"],
                             "current_event_sha256": consumer.digest(c), "prior_event_sha256": consumer.digest(p),
                             "current_observed_at": c["observed_at"], "prior_observed_at": p["observed_at"]}
                            for c, p in zip(sorted(cited["current"], key=lambda e:e["source_ref"]["ordinal"]),
                                            sorted(cited["prior"], key=lambda e:e["source_ref"]["ordinal"]))]}
        declaration["semantic_adjudication"] = builder.write(root/"adjudication.json", witness)
        return item, rows, declaration, capture, witness

    def verify(self, item, rows, declaration):
        return consumer._covered_source_outcome(declaration, item, rows, {})

    def test_complete_pinned_legacy_serialization_is_only_covered_representation(self):
        with tempfile.TemporaryDirectory() as temp:
            item, rows, declaration, _, _ = self.fixture(Path(temp)); before = copy.deepcopy((item, rows))
            try: receipt = self.verify(item, rows, declaration)
            except ValueError as error: self.fail(f"reviewed complete historical serialization not admitted: {error}")
            self.assertEqual(1800, receipt["covered_seconds"])
            self.assertEqual({"start": 1, "end": 1}, receipt["legacy_serialization_interval_offsets_seconds"])
            self.assertEqual(6, receipt["legacy_serialization_event_pair_count"])
            self.assertEqual((0, 0, 0), tuple(receipt[k] for k in ("new_pending_rows", "accounting_credit_mutations", "clockify_writes")))
            self.assertEqual(before, (item, rows))

    def test_generic_adjudication_stays_strict_on_native_atom_and_interval_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            item, rows, declaration, _, witness = self.fixture(Path(temp))
            witness.pop("legacy_serialization"); witness["schema_version"] = "pending-covered-accomplishment-adjudication/v1"
            declaration["semantic_adjudication"] = coverage_fixtures.CoveredPostedAccomplishmentTests().write(Path(declaration["semantic_adjudication"]["path"]), witness)
            with self.assertRaisesRegex(ValueError, "whole interval differs"): self.verify(item, rows, declaration)

    def test_changed_evidence_graph_mapping_or_adjudication_rejects(self):
        for mode in ("content", "source_ref", "source_type", "kind", "role", "snapshot", "hop", "graph-pin", "offset",
                     "offset-bool", "missing-pair", "duplicate-pair", "pair-hash", "legacy-minute", "semantic", "same-operation", "stale-GET", "sheet"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                item, rows, declaration, capture, witness = self.fixture(Path(temp)); legacy = witness["legacy_serialization"]
                event = next(e for e in item["source"]["ledger"]["events"] if e["source_type"] == "codex_sessions_event")
                if mode == "content": event["attributes"]["content"] = "Different operation"
                elif mode == "source_ref": event["source_ref"]["ordinal"] += 100
                elif mode == "source_type": event["source_type"] = "claude_bursts_event"
                elif mode in {"kind", "role"}: event["attributes"][mode] = "changed"
                elif mode == "snapshot": Path(legacy["current_graph"]["raw_ancestor"], "evidence/sessions.json").write_text("[]")
                elif mode == "hop": Path(legacy["current_graph"]["raw_ancestor"], "repair-source.json").write_text("{}")
                elif mode == "graph-pin": legacy["prior_graph"]["semantic_completion"]["sha256"] = "sha256:"+"0"*64
                elif mode == "offset": legacy["interval_offsets_seconds"]["start"] = 2
                elif mode == "offset-bool": legacy["interval_offsets_seconds"] = {"start": True, "end": True}
                elif mode == "missing-pair": legacy["event_pairs"].pop()
                elif mode == "duplicate-pair": legacy["event_pairs"][1] = legacy["event_pairs"][0]
                elif mode == "pair-hash": legacy["event_pairs"][0]["current_event_sha256"] = "sha256:"+"0"*64
                elif mode == "legacy-minute": legacy["event_pairs"][0]["prior_observed_at"] += ":00"
                elif mode == "semantic": witness["current_semantic"]["activity"]["outcome"] = "Different result"
                elif mode == "same-operation": witness["same_bounded_accomplishment"] = False
                elif mode == "stale-GET":
                    capture["finished_utc"] = (dt.datetime.now(dt.timezone.utc)-dt.timedelta(hours=2)).isoformat()
                    declaration["fresh_clockify_capture"] = coverage_fixtures.CoveredPostedAccomplishmentTests().write(Path(declaration["fresh_clockify_capture"]["path"]), capture)
                else: rows["wka-prior-s01"][13] = "unposted"
                declaration["semantic_adjudication"] = coverage_fixtures.CoveredPostedAccomplishmentTests().write(Path(declaration["semantic_adjudication"]["path"]), witness)
                with self.assertRaises((ValueError, review.ReviewRunError, cycle.CycleError)): self.verify(item, rows, declaration)
