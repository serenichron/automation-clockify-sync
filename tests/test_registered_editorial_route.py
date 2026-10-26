"""Fresh editorial authority recognizes exact frozen lifecycle routes only."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from scripts import clockify_review_batch_publication as publication
from scripts import clockify_sheet_publish as publisher
from scripts import evidence_ledger, review_corrections, semantic_analyzer
from test_review_batch_publication import source


class RegisteredEditorialRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run = Path(self.temp.name)
        (self.run / "evidence").mkdir()
        self.route = {"project_name": "Mazilu & Partners — Retainer", "prefix": "MP",
            "project_suffix": "54ecf6", "tag_names": ["Mazilu — Sesiuni de lucru"],
            "tag_suffixes": ["f2634e9f"], "billable": True}
        self.routing = {"client_lifecycle_routes": [{"pattern": r"\bMazilu\b",
            "activation": {"effective_at": "2026-07-01T00:00:00+03:00", "route": self.route}}],
            "prefix_overrides": [{"project_name_prefix": "Mazilu", "patterns": ["portal"], "prefix": "M&P"}]}
        self.observed = "2026-08-01T09:00:00+03:00"
        self.content = "Updated the Mazilu portal registry."
        self.seal_ledger()
        model, revision = semantic_analyzer.CURRENT_LIVE_FLASH_ROUTE
        self.proposal = source(1, "2026-08-01T10:00:07+03:00", "2026-08-01T10:01:18+03:00", 71)
        self.proposal.update(client_project=self.route["project_name"], clockify_project_suffix="54ecf6",
            tag_names=self.route["tag_names"], tag_suffixes=self.route["tag_suffixes"],
            description="M&P — Updated the document registry", human_corrected=True)
        self.proposal["provenance"].update(evidence_ids=[self.event.evidence_id],
            semantic_reviewer_model=model, semantic_reviewer_revision=revision)
        self.activity = {"activity_id": self.proposal["activity_id"], "evidence_ids": [self.event.evidence_id],
            "object": "Mazilu registry update", "rendered_description": self.proposal["description"],
            "semantic_reviewer_model": model, "semantic_reviewer_revision": revision}
        self.correct()

    def correct(self):
        (self.run / "review-corrections.jsonl").write_text("")
        item = {"id": "rvi-corrected-1", "current": {"activity_id": self.proposal["activity_id"],
            "evidence_ids": [self.event.evidence_id], "description": self.proposal["description"]}}
        decision = review_corrections.build_decision(item, decision="modify", reviewer="human",
            reviewed_at="2026-08-01T11:00:00+03:00", correction_categories=["routing", "wording"],
            rationale="Exact client route and wording", field_patch={name: {"op": "replace", "value": self.proposal[name]}
                for name in ("client_project", "tag_names", "description")})
        review_corrections.append_decision(self.run / "review-corrections.jsonl", decision, item=item)

    def seal_ledger(self):
        self.event = evidence_ledger.evidence_event("codex_sessions_event", {"source_id": "session:1"},
            observed_at=self.observed, raw_source_span={"timestamp": self.observed},
            attributes={"role": "user", "content": self.content})
        ledger = evidence_ledger.EvidenceLedger((self.event,))
        self.write("evidence/evidence-ledger.json", {"schema_version": evidence_ledger.SCHEMA_VERSION,
            "manifest": ledger.manifest.document(), "events": [self.event.document()]})

    def write(self, name, value):
        (self.run / name).write_text(json.dumps(value))

    def derive(self, fresh=True, marked=True):
        self.write("routing.json", self.routing)
        self.write("semantic-analysis.json", {"activities": [self.activity]})
        authority = {name: publication.artifact_handle(self.run / filename)
                     for name, filename in publication.AUTHORITY_FILES.items()}
        return publication._derive_authority([self.proposal], authority, self.run, "Europe/Bucharest",
            allow_wording_consolidation=True, allow_materialized_corrections=fresh,
            lifecycle_route_authority=({"policy": "frozen-lifecycle-editorial-route/v1",
                "ledger": publication.artifact_handle(self.run / "evidence/evidence-ledger.json")}
                if marked else None))

    def test_fresh_exact_registered_route_and_prefix_alias_become_editorially_ready(self):
        before = copy.deepcopy(self.proposal)
        fields = self.derive()
        identity = publisher.stable_review_id(self.proposal)
        self.assertEqual({identity: "M&P — Updated the document registry"}, fields["editorial_descriptions"])
        self.assertEqual([], fields["preparation_diagnostics"])
        self.assertEqual([identity], fields["materialized_correction_ids"])
        self.assertEqual(before, self.proposal)

    def test_historical_without_opt_in_keeps_static_only_route_validation(self):
        for fresh in (False, True):
            with self.subTest(materialized=fresh):
                fields = self.derive(fresh=fresh, marked=False)
                self.assertEqual({}, fields["editorial_descriptions"])
                self.assertIn("editorial_route_not_bound_to_snapshot", fields["preparation_diagnostics"][0]["reasons"])
                self.assertNotIn("lifecycle_route_authority", fields)

    def test_exact_materialized_correction_updates_stale_scope_not_substantive_evidence(self):
        self.activity["object"] = "Updated registry elements"
        self.activity["project_recommendation"] = {"name": "Serenichron Level 2", "prefix": "SC"}
        self.assertTrue(self.derive()["editorial_descriptions"])

    def test_pre_cutover_citations_cannot_authorize_exact_routing_override(self):
        for observed, content in [("2026-06-30T23:59:59+03:00", self.content)]:
            with self.subTest(observed=observed, content=content):
                event = evidence_ledger.evidence_event("codex_sessions_event", {"source_id": "session:1"},
                    observed_at=observed, raw_source_span={"timestamp": observed},
                    attributes={"role": "user", "content": content})
                ledger = evidence_ledger.EvidenceLedger((event,))
                self.write("evidence/evidence-ledger.json", {"schema_version": evidence_ledger.SCHEMA_VERSION,
                    "manifest": ledger.manifest.document(), "events": [event.document()]})
                self.event = event
                self.proposal["provenance"]["evidence_ids"] = [event.evidence_id]
                self.activity["evidence_ids"] = [event.evidence_id]
                self.correct()
                fields = self.derive()
                self.assertEqual({}, fields["editorial_descriptions"])

    def test_exact_routing_override_does_not_require_repeating_client_classifier_token(self):
        self.content = "Updated the portal registry."
        self.seal_ledger()
        self.proposal["provenance"]["evidence_ids"] = [self.event.evidence_id]
        self.activity["evidence_ids"] = [self.event.evidence_id]
        self.activity["object"] = "Updated registry elements"
        self.correct()
        fields = self.derive()
        self.assertEqual({publisher.stable_review_id(self.proposal): self.proposal["description"]},
                         fields["editorial_descriptions"])
        self.assertEqual([], fields["preparation_diagnostics"])
        self.assertEqual({}, self.derive(marked=False)["editorial_descriptions"])
        # The same source, model recommendation and selected financial route
        # without the exact human override still need classifier evidence.
        (self.run / "review-corrections.jsonl").write_text("")
        self.proposal.pop("human_corrected")
        self.assertEqual({}, self.derive()["editorial_descriptions"])

    def test_conflicting_active_lifecycle_targets_cannot_authorize_override(self):
        other = copy.deepcopy(self.routing["client_lifecycle_routes"][0])
        other["activation"]["route"]["project_suffix"] = "aaaaaa"
        self.routing["client_lifecycle_routes"].append(other)
        self.assertEqual({}, self.derive()["editorial_descriptions"])

    def test_native_financial_route_fields_and_alias_must_stay_exact(self):
        original = copy.deepcopy(self.proposal)
        for patch in ({"client_project": "Other"}, {"tag_names": ["Other"]},
                      {"clockify_project_suffix": "aaaaaa"}, {"tag_suffixes": ["bbbbbbbb"]},
                      {"billable": False}):
            with self.subTest(patch=patch):
                self.proposal = {**copy.deepcopy(original), **patch}
                fields = self.derive()
                self.assertEqual({}, fields["editorial_descriptions"])
                self.assertIn("editorial_route_not_bound_to_snapshot", fields["preparation_diagnostics"][0]["reasons"])
        self.proposal = original
        self.routing["prefix_overrides"] = []
        self.assertEqual({}, self.derive()["editorial_descriptions"])

    def test_tampered_ledger_is_never_route_authority(self):
        document = json.loads((self.run / "evidence/evidence-ledger.json").read_text())
        document["events"][0]["attributes"]["content"] = "Changed source"
        self.write("evidence/evidence-ledger.json", document)
        with self.assertRaises(ValueError):
            self.derive()

    def prepare(self, path):
        return publication.prepare(source_dir=self.run, output=path, run_id=self.run.name,
            spreadsheet_id="sheet", sheet_title="Review", timezone="Europe/Bucharest",
            consolidate_materialized_corrections=True)

    def test_new_binding_pins_ledger_and_replays_without_changing_source_or_authority(self):
        self.derive()
        self.write("proposals.json", [self.proposal])
        before = {str(path.relative_to(self.run)): path.read_bytes() for path in self.run.rglob("*") if path.is_file()}
        path = self.run / "binding.json"
        result = self.prepare(path)
        self.assertEqual("complete", result["status"])
        document, _ = publication.load(path)
        self.assertEqual(publication.frozen_lifecycle_route_authority(self.run), document["lifecycle_route_authority"])
        binding = path.read_bytes()
        self.prepare(path)
        self.assertEqual(binding, path.read_bytes())
        self.assertEqual(before, {name: (self.run / name).read_bytes() for name in before})
        # Even another internally valid ledger must not replace the frozen one.
        self.content = "Another Mazilu portal update."
        self.seal_ledger()
        with self.assertRaises(ValueError):
            publication.load(path)

    def test_existing_materialized_binding_without_marker_replays_original_bytes(self):
        self.derive(marked=False)
        self.write("proposals.json", [self.proposal])
        authority = {name: publication.artifact_handle(self.run / filename)
                     for name, filename in publication.AUTHORITY_FILES.items()}
        fields = publication._auto_fields([self.proposal], authority, self.run, "Europe/Bucharest",
            consolidation_policy=publication.NATURAL_BATCH_CONSOLIDATION_POLICY,
            allow_materialized_corrections=True)
        document = {"schema_version": publication.AUTO_SCHEMA, "run_id": self.run.name,
            "spreadsheet_id": "sheet", "sheet_title": "Review", "timezone": "Europe/Bucharest",
            "source_proposals": publication.artifact_handle(self.run / "proposals.json"),
            "authority": authority, "consolidation_policy": publication.NATURAL_BATCH_CONSOLIDATION_POLICY, **fields}
        path = self.run / "legacy.json"
        path.write_text(json.dumps(document))
        before = path.read_bytes()
        self.prepare(path)
        self.assertEqual(before, path.read_bytes())
        self.assertNotIn("lifecycle_route_authority", publication.load(path)[0])
        document["lifecycle_route_authority"] = None
        path.write_text(json.dumps(document))
        with self.assertRaises(ValueError):
            publication.load(path)


if __name__ == "__main__":
    unittest.main()
