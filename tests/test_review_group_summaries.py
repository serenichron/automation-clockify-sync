"""Reviewed parent summaries never rewrite original child accounting or wording."""
import copy
import json
import tempfile
import unittest
from unittest import mock

from scripts import clockify_review_batches as batches
from scripts import clockify_review_batch_publication as prepared
from scripts import clockify_native_sheet_post as native
from scripts import clockify_sheet_publish as publisher
import test_native_review_batches as native_fixtures
import test_native_review_batches_cli as cli_fixtures
import test_review_batch_publication as publication_fixtures
import test_review_batch_auto_preparation as auto_fixtures


SUMMARY = "SC — Improved review publication and verified receipt binding across the complete grouped work."


def summary_map(group, description=SUMMARY):
    return {group["review_id"]: {"child_review_ids": list(group["child_review_ids"]),
                                 "description": description}}


class ReviewGroupSummaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = native_fixtures.NativeReviewBatchTests()
        self.fixture.setUp()
        self.sources = self.fixture.sources
        self.contexts = self.fixture.contexts
        self.original = self.fixture.package
        self.group = self.original["activities"][0]
        self.summaries = summary_map(self.group)
        self.descriptions = {item["review_id"]: item["description"]
                             for item in self.group["prepared_descriptions"]}

    def package(self, summaries=None):
        try:
            return batches.package_review_batches(self.sources, editorial_descriptions=self.descriptions,
                work_contexts=self.contexts, group_summaries=self.summaries if summaries is None else summaries)
        except TypeError as error:
            self.fail(f"reviewed group summary input is missing: {error}")

    def test_summary_changes_only_parent_description_after_identity_derivation(self):
        package = self.package()
        expected = copy.deepcopy(self.original)
        expected["activities"][0]["description"] = SUMMARY
        self.assertEqual(expected, package)
        self.assertEqual("pending", package["activities"][0]["disposition"])
        self.assertEqual(154, package["activities"][0]["duration_seconds"])
        self.assertEqual(["SC — Prepared one", "SC — Prepared two"],
                         [item["description"] for item in package["activities"][0]["prepared_descriptions"]])
        verified = batches.verify_package(package, proposals=self.sources, work_contexts=self.contexts,
                                          group_summaries=self.summaries)
        self.assertEqual(package, verified)

    def test_rejects_foreign_stale_empty_or_concatenated_summary(self):
        foreign = {"pvi-" + "f" * 24: next(iter(self.summaries.values()))}
        stale = copy.deepcopy(self.summaries)
        stale[self.group["review_id"]]["child_review_ids"] = self.group["child_review_ids"][:1]
        reversed_children = copy.deepcopy(self.summaries)
        reversed_children[self.group["review_id"]]["child_review_ids"].reverse()
        for value in [foreign, stale, reversed_children, summary_map(self.group, " "),
                      summary_map(self.group, "SC — Prepared one; SC — Prepared two"),
                      summary_map(self.group, "SC — Prepared one\nSC — Prepared two")]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.package(value)

    def test_summary_does_not_certify_semantic_coverage_or_require_concatenation(self):
        # Coverage remains the caller's editorial responsibility, not a token
        # overlap heuristic which could reject a genuine synthesized summary.
        package = self.package(summary_map(self.group, "SC — Resolved publication inconsistencies."))
        self.assertEqual("SC — Resolved publication inconsistencies.", package["activities"][0]["description"])

    def test_unbound_summary_tampering_cannot_pass_reconstruction(self):
        package = self.package()
        with self.assertRaises(ValueError):
            batches.verify_package(package, proposals=self.sources, work_contexts=self.contexts)
        package["activities"][0]["description"] = "SC — Unreviewed replacement."
        with self.assertRaises(ValueError):
            batches.verify_package(package, proposals=self.sources, work_contexts=self.contexts,
                                  group_summaries=self.summaries)

    def test_native_accepts_exact_summary_but_keeps_each_original_child_interval(self):
        package = self.package()
        sheet_row = publisher.portfolio_row(package["activities"][0], "source-run")
        baseline = self.fixture.plan(authority={"consolidation_policy": prepared.NATURAL_BATCH_CONSOLIDATION_POLICY,
                                               "wording_only_ids": []})
        plan = native.build_plan(native_fixtures.capture(sheet_row), capture_sha256="c" * 64,
            routing=native_fixtures.route(), routing_sha256="r" * 64, timezone="Europe/Bucharest",
            workspace_id="workspace-1", member_id="user-1", projects=[{"id": "project-123456"}],
            tags=[{"id": "tag-654321"}], live_entries=[], review_batches=package,
            review_batch_sources=self.sources, review_batch_work_contexts=self.contexts,
            review_batch_authority={"consolidation_policy": prepared.NATURAL_BATCH_CONSOLIDATION_POLICY,
                                    "wording_only_ids": []}, review_batch_group_summaries=self.summaries)
        self.assertEqual([SUMMARY, SUMMARY], [entry["payload"]["description"] for entry in plan["entries"]])
        for before, after in zip(baseline["entries"], plan["entries"], strict=True):
            before, after = copy.deepcopy(before), copy.deepcopy(after)
            before.pop("payload_digest")
            after.pop("payload_digest")
            before["payload"].pop("description")
            after["payload"].pop("description")
            self.assertEqual(before, after)
        self.assertEqual(self.summaries, plan["review_batches_binding"]["group_summaries"])
        native._validate_review_batch_plan(plan)
        tampered = copy.deepcopy(plan)
        tampered["review_batches_binding"]["captured_rows"][0][1]["Description"] = "SC — Changed live wording."
        with self.assertRaises(native.NativePostError):
            native._validate_review_batch_plan(tampered)

    def test_explicit_null_native_summary_authority_is_not_silently_ignored(self):
        binding = {"package": self.original, "source_proposals": self.sources,
                   "work_contexts": self.contexts, "group_summaries": None,
                   "captured_rows": native._sheet_rows(native_fixtures.capture(self.fixture.sheet_row))[2]}
        with self.assertRaises(native.NativePostError):
            native._review_batch_groups(binding, "Europe/Bucharest")

    def test_native_cli_consumes_the_same_source_bound_summary_extension(self):
        fixture = cli_fixtures.NativeReviewBatchCliTests()
        fixture.setUp()
        group = fixture.preparation["package"]["activities"][0]
        fixture.preparation["group_summaries"] = summary_map(group)
        group["description"] = SUMMARY
        fixture.document = cli_fixtures.capture(publisher.portfolio_row(group, "source-run"))
        with tempfile.TemporaryDirectory() as directory, \
             mock.patch.object(native, "_gateway", return_value=(
                 cli_fixtures.ReadOnlyGateway(), "workspace-1", "user-1", "synthetic-test-key")), \
             mock.patch.object(native.legacy, "_paged", side_effect=fixture.request):
            result, output = fixture.invoke(directory, fixture.preparation)
            self.assertEqual(0, result)
            plan = json.loads(output.read_text())
        self.assertEqual(fixture.preparation["group_summaries"], plan["review_batches_binding"]["group_summaries"])
        self.assertEqual(154 / 60, plan["total_minutes"])


class ReviewGroupSummaryPublicationTests(unittest.TestCase):
    def fixture(self, cls):
        instance = cls()
        instance.setUp()
        self.addCleanup(instance.doCleanups)
        return instance

    def test_fresh_automatic_preparation_consumes_and_replays_explicit_reviewed_summaries(self):
        fixture = self.fixture(auto_fixtures.AutomaticPreparationTests)
        legacy_path = fixture.root / "legacy.json"
        arguments = dict(source_dir=fixture.run, run_id="source-run", spreadsheet_id="sheet",
                         sheet_title="August 2026 review", timezone="Europe/Bucharest")
        prepared.prepare(output=legacy_path, **arguments)
        legacy_bytes = legacy_path.read_bytes()
        legacy, _ = prepared.load(legacy_path)
        summaries = summary_map(legacy["package"]["activities"][0])
        path = fixture.root / "summarized.json"
        try:
            prepared.prepare(output=path, group_summaries=summaries, **arguments)
        except TypeError as error:
            self.fail(f"automatic producer cannot consume explicit summaries: {error}")
        document, _ = prepared.load(path)
        self.assertEqual(summaries, document["group_summaries"])
        self.assertEqual(SUMMARY, document["package"]["activities"][0]["description"])
        expected = copy.deepcopy(legacy)
        expected["group_summaries"] = summaries
        expected["package"]["activities"][0]["description"] = SUMMARY
        self.assertEqual(expected, document)
        before = path.read_bytes()
        prepared.prepare(output=path, **arguments)
        self.assertEqual(before, path.read_bytes())
        changed = summary_map(document["package"]["activities"][0], "SC — Different reviewed summary.")
        with self.assertRaises(ValueError):
            prepared.prepare(output=path, group_summaries=changed, **arguments)
        with self.assertRaises(ValueError):
            prepared.prepare(output=legacy_path, group_summaries=summaries, **arguments)
        prepared.prepare(output=legacy_path, **arguments)
        self.assertEqual(legacy_bytes, legacy_path.read_bytes())

    def test_explicit_v1_binding_projects_summary_and_rejects_stale_members(self):
        fixture = self.fixture(publication_fixtures.BatchPublicationTests)
        summaries = summary_map(fixture.package["activities"][0])
        fixture.document["group_summaries"] = summaries
        fixture.document["package"]["activities"][0]["description"] = SUMMARY
        fixture.write(fixture.binding, fixture.document)
        projection = prepared.project(path=fixture.binding, source_dir=fixture.run, proposals=fixture.sources,
            run_id="source-run", spreadsheet_id="sheet", sheet_title="August 2026 review", project_allowlist={})
        self.assertEqual(SUMMARY, projection["rows"][0][8])
        children = [publisher.proposal_row(p, "source-run") for p in fixture.sources[:2]]
        stage, _ = fixture.staging(children)
        staged = prepared.stage_projection(stage)
        self.assertEqual(SUMMARY, staged["rows"][0][8])
        self.assertEqual(summaries, staged["group_summaries"])
        fixture.document["group_summaries"][fixture.package["activities"][0]["review_id"]]["child_review_ids"].pop()
        fixture.write(fixture.binding, fixture.document)
        with self.assertRaises(ValueError):
            prepared.load(fixture.binding)

