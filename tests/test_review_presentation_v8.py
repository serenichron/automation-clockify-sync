"""Presentation v8: one accomplishment per workstream, no metric tails, no frozen transient failures."""
import json
import unittest
from unittest import mock

from scripts import caveman_renderer
from scripts import clockify_review_batch_publication as prepared
from scripts import clockify_review_cycle as cycle
from scripts import clockify_review_group_summary as group_summary
from scripts import clockify_native_sheet_post as native
from scripts import evidence_ledger
import test_review_batch_auto_preparation as auto_preparation

V8 = "approved-daily-project-presentation/v8"


class AccomplishmentDescriptionTests(unittest.TestCase):
    def test_metric_list_outcomes_are_dropped(self):
        cases = [
            (("TSTP", "Inspected", "GTM container and GA4 destination",
              "version 9 live, 11 tags, no GA4 or Ads tag, 48 purchases with $0 revenue"),
             "TSTP — Inspected GTM container and GA4 destination"),
            (("TSTP", "Ran prod export", "ldaq submissions CSV on prod",
              "4,539,668 rows and 41 columns current to 2026-10-15 07:34"),
             "TSTP — Ran prod export ldaq submissions CSV on prod"),
            (("SC", "Documented snapshot sync", "ZIP-based SharePoint snapshot",
              "801 files MD5-compared with 28.09, site renamed MP Procedures and Methodology"),
             "SC — Documented snapshot sync ZIP-based SharePoint snapshot"),
        ]
        for arguments, expected in cases:
            with self.subTest(expected):
                self.assertEqual(expected, caveman_renderer.accomplishment_description(*arguments))

    def test_qualitative_outcome_is_kept(self):
        self.assertEqual("SC — Fixed PHP verifier device comparison so staging deploys pass",
            caveman_renderer.accomplishment_description(
                "SC", "Fixed", "PHP verifier device comparison", "so staging deploys pass"))

    def test_limitations_and_blockers_are_kept(self):
        self.assertEqual("SC — Deployed staging build blocked by missing DNS record, 2 retries",
            caveman_renderer.accomplishment_description(
                "SC", "Deployed", "staging build", "blocked by missing DNS record, 2 retries"))

    def test_status_titles_dates_and_results_are_kept(self):
        for outcome in ("deployed to staging, awaiting client approval", "implemented locally, not deployed",
                        "local-only, not deployed to production", "ready for client review, waiting on credentials",
                        "draft saved, needs client sign-off", "for Tests 16-18", "shipped to production on November 3",
                        "reduced load time to 2 seconds"):
            with self.subTest(outcome):
                self.assertEqual("SC — Fixed checkout " + outcome,
                    caveman_renderer.accomplishment_description("SC", "Fixed", "checkout", outcome))

    def test_policy_constant(self):
        self.assertEqual(V8, prepared.ACCOMPLISHMENT_CONSOLIDATION_POLICY)


class PresentationV8Tests(unittest.TestCase):
    setUp_base = auto_preparation.AutomaticPreparationTests.setUp
    write = staticmethod(auto_preparation.AutomaticPreparationTests.write)

    def setUp(self):
        self.setUp_base()
        for activity in self.analysis["activities"]:
            activity["lifecycle"] = "completed"
        self.set_workstreams(["ws-a", "ws-a"])

    def set_workstreams(self, workstreams):
        for proposal, workstream in zip(self.sources, workstreams):
            proposal["workstream_id"] = workstream
        self.persist()

    def persist(self):
        self.write(self.run / "proposals.json", self.sources)
        self.write(self.run / "semantic-analysis.json", self.analysis)
        self.write(self.run / "work-accounting-result.json", {"schema_version": 1,
            "allocation_mode": "non_overlapping_v1", "proposals": self.sources, "ambiguous": [], "skipped": []})

    def add_source(self, index, start, end, seconds, wording, workstream, lifecycle="completed"):
        from test_review_batch_publication import source
        proposal = source(index, start, end, seconds)
        model, revision = auto_preparation.semantic_analyzer.CURRENT_LIVE_FLASH_ROUTE
        proposal["provenance"].update(semantic_reviewer_model=model, semantic_reviewer_revision=revision)
        proposal["workstream_id"] = workstream
        self.sources.append(proposal)
        self.analysis["activities"].append({"activity_id": proposal["activity_id"],
            "evidence_ids": proposal["provenance"]["evidence_ids"], "rendered_description": wording,
            "semantic_reviewer_model": model, "semantic_reviewer_revision": revision,
            "lifecycle": lifecycle})
        self.persist()

    def prepare_with(self, provider):
        return prepared.prepare(source_dir=self.run, output=self.root / "prepared.json", run_id="source-run",
            spreadsheet_id="sheet", sheet_title="August 2026 review", timezone="Europe/Bucharest",
            consolidate_materialized_corrections=True, summary_provider=provider, consolidation_policy=V8)

    def project(self):
        return prepared.project(path=self.root / "prepared.json", source_dir=self.run,
            proposals=self.sources, run_id="source-run", spreadsheet_id="sheet",
            sheet_title="August 2026 review", project_allowlist={"abc123": "Example"})["rows"]

    @staticmethod
    def summarising(calls, fail_for=frozenset()):
        def provider(groups):
            calls.append(sorted(child for group in groups for child in group["child_review_ids"]))
            for group in groups:
                if set(group["child_review_ids"]) & set(fail_for):
                    raise group_summary.GroupSummaryRejected("failed independent review after one targeted repair")
            return {group["review_id"]: {"child_review_ids": group["child_review_ids"],
                    "description": "SC — Summary of " + group["child_review_ids"][0][-6:]} for group in groups}
        return provider

    def ids(self):
        return [prepared.publisher.stable_review_id(p) for p in self.sources]

    def test_new_binding_records_policy_v8(self):
        self.prepare_with(self.summarising([]))
        document = json.loads((self.root / "prepared.json").read_text())
        self.assertEqual(V8, document["consolidation_policy"])

    def test_failed_summary_merges_the_workstream_instead_of_publishing_each_action(self):
        outcome = self.prepare_with(self.summarising([], fail_for=self.ids()))
        self.assertEqual("complete", outcome["status"])
        rows = self.project()
        self.assertEqual(1, len(rows))
        self.assertAlmostEqual((71 + 83) / 60, rows[0][3])
        self.assertEqual("SC — Verified immutable review receipt binding and related steps", rows[0][8])
        document = json.loads((self.root / "prepared.json").read_text())
        self.assertNotIn("summary_fallback_ids", document)
        self.assertEqual([sorted(self.ids())], [sorted(m["child_review_ids"]) for m in document["deterministic_merges"]])

    def test_deterministic_merge_replays_without_calling_the_provider(self):
        self.prepare_with(self.summarising([], fail_for=self.ids()))
        before = (self.root / "prepared.json").read_bytes()
        calls = []
        self.assertEqual("complete", self.prepare_with(self.summarising(calls))["status"])
        self.assertEqual([], calls)
        self.assertEqual(before, (self.root / "prepared.json").read_bytes())

    def test_different_workstreams_are_not_merged(self):
        self.set_workstreams(["ws-a", "ws-b"])
        self.prepare_with(self.summarising([]))
        self.assertEqual(sorted(self.descriptions), sorted(row[8] for row in self.project()))

    def test_one_failing_group_does_not_disband_the_others(self):
        self.add_source(3, "2026-08-01T12:00:00+03:00", "2026-08-01T12:01:00+03:00", 60,
                        "SC — Drafted onboarding checklist", "ws-b")
        self.add_source(4, "2026-08-01T12:05:00+03:00", "2026-08-01T12:07:00+03:00", 120,
                        "SC — Published onboarding checklist", "ws-b")
        a_ids, b_ids = self.ids()[:2], self.ids()[2:]
        calls = []
        outcome = self.prepare_with(self.summarising(calls, fail_for=a_ids))
        self.assertEqual("complete", outcome["status"])
        self.assertEqual(2, len(calls))
        rows = sorted(self.project(), key=lambda row: row[1])
        self.assertEqual(2, len(rows))
        self.assertEqual("SC — Verified immutable review receipt binding and related steps", rows[0][8])
        self.assertTrue(rows[1][8].startswith("SC — Summary of "))

    def test_blocked_child_is_never_folded_into_a_merge(self):
        self.add_source(3, "2026-08-01T10:20:00+03:00", "2026-08-01T10:21:00+03:00", 60,
                        "SC — Attempted receipt upload blocked by expired token", "ws-a", lifecycle="blocked")
        self.prepare_with(self.summarising([], fail_for=self.ids()))
        rows = sorted(self.project(), key=lambda row: row[1])
        self.assertIn("SC — Attempted receipt upload blocked by expired token", [row[8] for row in rows])
        merged = [row for row in rows if row[8].endswith(" and related steps")]
        self.assertEqual(1, len(merged))
        self.assertAlmostEqual((71 + 83) / 60, merged[0][3])

    def bind_sessions(self, sessions):
        """Give each source one ledger event in the named (machine, session)."""
        events = []
        for proposal, activity, (machine, session) in zip(self.sources, self.analysis["activities"], sessions):
            event = evidence_ledger.evidence_event("claude_bursts_event",
                {"source_type": "claude_bursts", "machine": machine, "session_id": session,
                 "source_id": f"{session}:event:{proposal['activity_id']}"},
                observed_at=proposal["start"], raw_source_span={"timestamp": proposal["start"], "cwd": "/work"},
                attributes={"role": "assistant", "kind": "message", "content": "done"})
            events.append(event)
            proposal["provenance"]["evidence_ids"] = [event.evidence_id]
            activity["evidence_ids"] = [event.evidence_id]
        ledger = evidence_ledger.EvidenceLedger(tuple(events), {"sessions/m": {"status": "complete"}})
        (self.run / "evidence").mkdir(exist_ok=True)
        self.write(self.run / "evidence" / "evidence-ledger.json", {"schema_version": ledger.manifest.schema_version,
            "manifest": ledger.manifest.document(), "events": [e.document() for e in ledger.events]})
        self.persist()

    def test_micro_actions_from_one_session_become_one_accomplishment(self):
        self.set_workstreams(["ws-a", "ws-b"])
        self.bind_sessions([("m", "session-1"), ("m", "session-1")])
        self.prepare_with(self.summarising([], fail_for=self.ids()))
        rows = self.project()
        self.assertEqual(1, len(rows))
        self.assertAlmostEqual((71 + 83) / 60, rows[0][3])

    def test_different_sessions_and_workstreams_stay_apart(self):
        self.set_workstreams(["ws-a", "ws-b"])
        self.bind_sessions([("m", "session-1"), ("m", "session-2")])
        self.prepare_with(self.summarising([], fail_for=self.ids()))
        self.assertEqual(2, len(self.project()))

    def test_diagnosis_merges_with_the_fix_from_the_same_stream(self):
        self.analysis["activities"][0]["lifecycle"] = "investigated"
        self.persist()
        self.prepare_with(self.summarising([], fail_for=self.ids()))
        rows = self.project()
        self.assertEqual(1, len(rows))
        self.assertAlmostEqual((71 + 83) / 60, rows[0][3])

    def test_one_pending_child_does_not_keep_the_rest_of_the_stream_apart(self):
        self.add_source(3, "2026-08-01T10:20:00+03:00", "2026-08-01T10:21:00+03:00", 60,
                        "SC — Planned payment migration", "ws-a", lifecycle="planned")
        self.prepare_with(self.summarising([], fail_for=self.ids()))
        document = json.loads((self.root / "prepared.json").read_text())
        merges = [sorted(m["child_review_ids"]) for m in document.get("deterministic_merges", [])]
        self.assertEqual([sorted(self.ids()[:2])], merges)

    def test_summary_with_internal_status_words_is_treated_as_rejected(self):
        def summary(groups):
            return {g["review_id"]: {"child_review_ids": g["child_review_ids"],
                    "description": "SC — Built the checkout page, undeployed"} for g in groups}
        outcome = self.prepare_with(summary)
        self.assertEqual("complete", outcome["status"])
        rows = self.project()
        self.assertEqual(1, len(rows))
        self.assertEqual("SC — Verified immutable review receipt binding and related steps", rows[0][8])

    def test_commits_in_one_repository_group_together(self):
        self.set_workstreams(["ws-a", "ws-b"])
        events = []
        for proposal, activity in zip(self.sources, self.analysis["activities"]):
            event = evidence_ledger.evidence_event("repository_events",
                {"source_type": "repository_events", "machine": "m",
                 "source_id": "/work/repo:" + proposal["activity_id"]},
                observed_at=proposal["start"], raw_source_span={"start": proposal["start"], "end": proposal["end"]},
                attributes={"repository_root": "/work/repo", "subject": "change"})
            events.append(event)
            proposal["provenance"]["evidence_ids"] = [event.evidence_id]
            activity["evidence_ids"] = [event.evidence_id]
        ledger = evidence_ledger.EvidenceLedger(tuple(events), {"repositories/m": {"status": "complete"}})
        (self.run / "evidence").mkdir(exist_ok=True)
        self.write(self.run / "evidence" / "evidence-ledger.json", {"schema_version": ledger.manifest.schema_version,
            "manifest": ledger.manifest.document(), "events": [e.document() for e in ledger.events]})
        self.persist()
        self.prepare_with(self.summarising([], fail_for=self.ids()))
        self.assertEqual(1, len(self.project()))

    def test_symlinked_ledger_falls_back_instead_of_blocking(self):
        self.set_workstreams(["ws-a", "ws-a"])
        real = self.root / "real-evidence"
        real.mkdir()
        (real / "evidence-ledger.json").write_text("{}")
        (self.run / "evidence").symlink_to(real, target_is_directory=True)
        outcome = self.prepare_with(self.summarising([], fail_for=self.ids()))
        self.assertEqual("complete", outcome["status"])
        self.assertEqual(1, len(self.project()))

    def test_children_without_workstream_stay_standalone(self):
        self.set_workstreams([None, None])
        for proposal in self.sources:
            proposal.pop("workstream_id", None)
        self.persist()
        calls = []
        self.prepare_with(self.summarising(calls, fail_for=self.ids()))
        self.assertEqual(sorted(self.descriptions), sorted(row[8] for row in self.project()))

    def native_binding(self):
        document, _ = prepared.load(self.root / "prepared.json")
        return document, {"package": document["package"], "source_proposals": self.sources,
            "work_contexts": document["work_contexts"],
            **({"group_summaries": document["group_summaries"]} if "group_summaries" in document else {}),
            "deterministic_merges": document["deterministic_merges"],
            "captured_rows": [(2, dict(zip(prepared.publisher.HEADER,
                prepared.publisher.portfolio_row(document["package"]["activities"][0], "source-run"))))],
            "preparation_authority": {"consolidation_policy": document["consolidation_policy"],
                "wording_only_ids": document["wording_only_ids"],
                **({"materialized_correction_ids": document["materialized_correction_ids"]}
                   if "materialized_correction_ids" in document else {})}}

    def test_native_posting_reconstructs_a_deterministic_merge(self):
        self.prepare_with(self.summarising([], fail_for=self.ids()))
        document, binding = self.native_binding()
        groups, sources = native._review_batch_groups(binding, "Europe/Bucharest")
        self.assertEqual(1, len(groups))
        group = next(iter(groups.values()))
        self.assertEqual(71 + 83, group["duration_seconds"])
        self.assertEqual("SC — Verified immutable review receipt binding and related steps", group["description"])

    def test_native_posting_rejects_forged_or_missing_merge_authority(self):
        self.prepare_with(self.summarising([], fail_for=self.ids()))
        _, binding = self.native_binding()
        for forged in ([], [{**binding["deterministic_merges"][0], "child_review_ids": self.ids()[:1]}]):
            changed = dict(binding, deterministic_merges=forged)
            with self.subTest(forged=forged), self.assertRaises(native.NativePostError):
                native._review_batch_groups(changed, "Europe/Bucharest")

    def test_other_summary_errors_never_freeze_a_merge(self):
        def broken(_groups):
            raise group_summary.GroupSummaryError("review summary provider response is not structured JSON")
        with self.assertRaises(Exception):
            self.prepare_with(broken)
        self.assertFalse((self.root / "prepared.json").exists())

    def test_missing_summary_endpoint_is_unavailable_not_rejected(self):
        group = {"review_id": "pvi-x", "child_review_ids": ["a", "b"],
                 "prepared_descriptions": [{"review_id": "a", "description": "SC — One"},
                                           {"review_id": "b", "description": "SC — Two"}]}
        with mock.patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(group_summary.GroupSummaryUnavailable):
                group_summary.summarize_groups([group], contract_version=4)

    def test_advanced_or_pending_child_is_not_hidden_by_a_merge(self):
        self.add_source(3, "2026-08-01T10:20:00+03:00", "2026-08-01T10:23:00+03:00", 180,
                        "SC — Started payment migration pending client access", "ws-a", lifecycle="advanced")
        outcome = self.prepare_with(self.summarising([], fail_for=self.ids()))
        pending_id = self.ids()[2]
        document = json.loads((self.root / "prepared.json").read_text())
        merged = {child for merge in document.get("deterministic_merges", []) for child in merge["child_review_ids"]}
        self.assertNotIn(pending_id, merged)
        rows = {row[0]: row[8] for row in self.project()}
        # The pending child stays visible: its own row, or an unprepared gap shown for review.
        self.assertTrue(pending_id in rows or pending_id in outcome.get("unprepared_review_ids", []))
        self.assertFalse(any("payment migration" in description for review_id, description in rows.items()
                             if review_id != pending_id))

    def test_internal_journal_details_are_still_scrubbed(self):
        self.set_workstreams(["ws-a", "ws-b"])
        self.analysis["activities"][0]["rendered_description"] = "SC — Updated checkout module lint clean"
        self.persist()
        self.prepare_with(self.summarising([]))
        descriptions = [row[8] for row in self.project()]
        self.assertIn("SC — Updated checkout module", descriptions)
        self.assertNotIn("SC — Updated checkout module lint clean", descriptions)

    def test_cycle_summaries_go_through_the_cycle_provider_with_contract_4(self):
        calls = []
        def provider(groups, cache, **kwargs):
            calls.append(kwargs.get("contract_version"))
            return {g["review_id"]: {"child_review_ids": g["child_review_ids"],
                    "description": "SC — Prepared source publication and verified its review receipt"} for g in groups}
        with mock.patch.object(cycle, "_summarize_review_groups", side_effect=provider):
            cycle._prepare_new_publication({"spreadsheet_id": "sheet", "timezone": "Europe/Bucharest",
                "cache": str(self.root / "cache.jsonl")}, {"run_dir": str(self.run), "run_id": "source-run"},
                sheet_title="August 2026 review", state_dir=self.root / "state")
        self.assertTrue(calls)
        self.assertEqual({4}, set(calls))

    def test_transient_summary_failure_defers_without_writing_a_binding(self):
        def unavailable(_groups):
            raise group_summary.GroupSummaryUnavailable("summary provider timed out")
        with self.assertRaises(group_summary.GroupSummaryUnavailable):
            self.prepare_with(unavailable)
        self.assertFalse((self.root / "prepared.json").exists())

    def test_cycle_requests_v8_for_new_bindings(self):
        with mock.patch.object(prepared, "prepare", return_value={"status": "complete",
                "binding": {"path": "/x.json", "sha256": "sha256:" + "0" * 64}}) as prepare:
            cycle._prepare_new_publication({"spreadsheet_id": "sheet", "timezone": "Europe/Bucharest",
                "cache": str(self.root / "cache.jsonl")},
                {"run_dir": str(self.run), "run_id": "source-run", "artifact_digests": {}},
                sheet_title="August 2026 review", state_dir=self.root)
        self.assertEqual(V8, prepare.call_args.kwargs["consolidation_policy"])


if __name__ == "__main__":
    unittest.main()
