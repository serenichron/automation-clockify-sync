"""Pure review packaging: preserve evidence and never manufacture elapsed time."""
import copy
import datetime as dt
import json
import unittest

from scripts import clockify_post_approved_portfolio as poster
from scripts import clockify_sheet_publish as publisher

try:
    from scripts import clockify_review_batches as batches
except ImportError:
    batches = None


def proposal(number, start="2026-09-29T10:00:07+03:00", end="2026-09-29T10:01:18+03:00", seconds=71):
    return {
        "review_activity_key": f"wka-{number:024x}", "allocation_segment": 1,
        "activity_id": f"activity-{number}", "workstream_id": "same-explicit-work",
        "start": start, "end": end, "duration_seconds": seconds,
        "duration_minutes": seconds // 60, "client_project": "Example",
        "clockify_project_suffix": "abc123", "tag_names": ["Development"],
        "tag_suffixes": ["def456"], "billable": True, "confidence": "high",
        "description": "Raw wording is deliberately not the editorial wording",
        "provenance": {"source_type": "semantic_activity", "evidence_ids": [f"ev-{number}"]},
        "review_warnings": [],
    }


def identity(number):
    return f"wka-{number:024x}-s01"


def natural_batch_sources(count):
    """Disjoint day/context fixtures reproducing the 34/19-child split shapes."""
    sources = []
    first = dt.datetime.fromisoformat("2026-09-23T08:00:07+03:00")
    for number in range(1, count + 1):
        seconds = 300 if number <= (30 if count == 34 else 15) else 30
        start = first + dt.timedelta(minutes=(number - 1) * 6)
        source = proposal(number, start.isoformat(), (start + dt.timedelta(seconds=seconds)).isoformat(), seconds)
        source["review_warnings"] = [{"type": "estimated_session_placement", "reason": "Observed work; estimated placement."}]
        sources.append(source)
    return sources


class ReviewBatchTests(unittest.TestCase):
    def package(self, sources, **kwargs):
        self.assertIsNotNone(batches, "deterministic review packaging helper is missing")
        descriptions = kwargs.pop("editorial_descriptions", {
            identity(n): f"SC — Prepared task {n}" for n in range(1, 30)
        })
        contexts = kwargs.pop("work_contexts", {identity(n): "explicit-task" for n in range(1, 30)})
        return batches.package_review_batches(sources, editorial_descriptions=descriptions,
                                             work_contexts=contexts, **kwargs)

    def test_disjoint_batch_keeps_seconds_and_real_portfolio_posting_windows(self):
        sources = [proposal(1), proposal(2, "2026-09-29T10:10:00+03:00", "2026-09-29T10:11:23+03:00", 83)]
        result = self.package(sources)
        self.assertEqual([], result["retained"])
        self.assertEqual(1, len(result["activities"]))
        row = result["activities"][0]
        self.assertEqual([identity(1), identity(2)], row["child_review_ids"])
        self.assertEqual(154, row["duration_seconds"])
        self.assertEqual(2, row["duration_minutes"])
        self.assertEqual(["activity-1", "activity-2"], row["source_activity_ids"])
        self.assertNotIn("validation_status", row)  # No fabricated semantic validation.
        plans = poster._plans(result, {("Example", ("Development",)): {
            "project_id": "real-project", "tag_ids": ["real-tag"], "billable": True,
        }})
        self.assertEqual([71, 83], [p["duration_seconds"] for p in plans])
        self.assertEqual(["2026-09-29T07:00:07Z", "2026-09-29T07:10:00Z"], [p["start"] for p in plans])
        self.assertEqual(["2026-09-29T07:01:18Z", "2026-09-29T07:11:23Z"], [p["end"] for p in plans])

    def test_explicit_overlap_policy_groups_existing_clockify_warnings_without_losing_sources(self):
        sources = [proposal(1), proposal(2, "2026-09-29T10:10:00+03:00", "2026-09-29T10:11:23+03:00", 83)]
        for source, number, start, end in (
            (sources[0], 1, "2026-09-29T10:00:07+03:00", "2026-09-29T10:00:17+03:00"),
            (sources[1], 2, "2026-09-29T10:10:00+03:00", "2026-09-29T10:10:10+03:00"),
        ):
            source["review_warnings"] = [{
                "type": "existing_clockify_overlap", "counterpart_id": f"ev-{number:064x}",
                "overlap_start": start, "overlap_end": end, "overlap_duration_seconds": 10,
            }]
        original = copy.deepcopy(sources)
        result = self.package(sources, allow_review_overlaps=True, allow_clockify_overlaps=True)
        self.assertEqual([], result["retained"])
        self.assertEqual(1, len(result["activities"]))
        row = result["activities"][0]
        self.assertEqual([identity(1), identity(2)], row["child_review_ids"])
        self.assertEqual(["activity-1", "activity-2"], row["source_activity_ids"])
        self.assertEqual(["ev-1", "ev-2"], row["evidence_ids"])
        self.assertEqual(154, row["duration_seconds"])
        self.assertEqual(2, row["duration_minutes"])
        self.assertEqual([
            {"start": "2026-09-29T10:00:07+03:00", "end": "2026-09-29T10:01:18+03:00",
             "duration_seconds": 71, "duration_minutes": 1},
            {"start": "2026-09-29T10:10:00+03:00", "end": "2026-09-29T10:11:23+03:00",
             "duration_seconds": 83, "duration_minutes": 1},
        ], row["allocation_segments"])
        expected_warnings = [{"review_id": identity(n), "warnings": source["review_warnings"]}
                             for n, source in enumerate(original, 1)]
        self.assertEqual(expected_warnings, row["child_review_warnings"])
        visible = publisher.portfolio_row(row, "source-run")
        warning_text, notice = visible[12].split("\n", 1)
        self.assertEqual(expected_warnings, json.loads(warning_text))
        self.assertEqual(batches.OVERLAP_REVIEW_NOTICE, notice)
        self.assertEqual("pending", visible[9])
        self.assertEqual("unposted", visible[13])
        self.assertNotIn("credited_minutes", row)
        self.assertNotIn("validation_status", row)
        self.assertEqual(original, sources)
        self.assertEqual(result, batches.verify_package(result, proposals=sources,
            work_contexts={identity(1): "explicit-task", identity(2): "explicit-task"},
            allow_review_overlaps=True, allow_clockify_overlaps=True))
        with self.assertRaises(ValueError):
            batches.verify_package(result, proposals=sources,
                work_contexts={identity(1): "explicit-task", identity(2): "explicit-task"})
        default = self.package(sources, allow_review_overlaps=True)
        self.assertEqual([], default["activities"])
        self.assertEqual(original, [item["proposal"] for item in default["retained"]])

    def test_explicit_overlap_policy_does_not_exempt_malformed_or_protected_warnings(self):
        warning = {
            "type": "existing_clockify_overlap", "counterpart_id": "ev-" + "1" * 64,
            "overlap_start": "2026-09-29T10:00:07+03:00",
            "overlap_end": "2026-09-29T10:00:17+03:00", "overlap_duration_seconds": 10,
        }
        variants = [dict(warning, type="meeting_proposal_overlap", counterpart_id="wks-" + "1" * 24),
                    dict(warning, type="unknown_overlap"), dict(warning, counterpart_id="missing"),
                    dict(warning, overlap_duration_seconds=11), dict(warning, overlap_start="invalid"),
                    dict(warning, type=["existing_clockify_overlap"]),
                    dict(warning, type={"type": "existing_clockify_overlap"})]
        variants.extend({key: value for key, value in warning.items() if key != missing}
                        for missing in ("counterpart_id", "overlap_start", "overlap_end", "overlap_duration_seconds"))
        for value in variants:
            with self.subTest(warning=value):
                source = {**proposal(1), "review_warnings": [value]}
                result = self.package([source], allow_review_overlaps=True, allow_clockify_overlaps=True)
                self.assertEqual([], result["activities"])
                self.assertEqual(source, result["retained"][0]["proposal"])
                self.assertIn("overlap_warning", result["retained"][0]["preparation_reasons"])
        for changes, protected, reason in (({}, {identity(1)}, "protected_review_id"),
                                          ({"disposition": "approved"}, set(), "approved_or_posted"),
                                          ({"review_status": "posted"}, set(), "approved_or_posted")):
            with self.subTest(reason=reason):
                source = {**proposal(1), **changes, "review_warnings": [warning]}
                result = self.package([source], allow_review_overlaps=True, allow_clockify_overlaps=True,
                                      protected_ids=protected)
                self.assertEqual([], result["activities"])
                self.assertEqual(source, result["retained"][0]["proposal"])
                self.assertIn(reason, result["retained"][0]["preparation_reasons"])

    def test_editorial_input_is_required_and_distinct_prepared_descriptions_are_explicit(self):
        second = proposal(2, "2026-09-29T10:10:00+03:00", "2026-09-29T10:11:23+03:00", 83)
        result = self.package([proposal(1), second])
        row = result["activities"][0]
        self.assertEqual("SC — Prepared task 1; SC — Prepared task 2", row["description"])
        self.assertEqual([
            {"review_id": identity(1), "description": "SC — Prepared task 1"},
            {"review_id": identity(2), "description": "SC — Prepared task 2"},
        ], row["prepared_descriptions"])
        result = self.package([proposal(1)], editorial_descriptions={})
        self.assertEqual([], result["activities"])
        self.assertIn("missing_editorial_description", result["retained"][0]["preparation_reasons"])

    def test_non_overlap_warnings_survive_per_child_without_mutation_or_duplicate_credit(self):
        first = proposal(1)
        first["review_warnings"] = [
            {"type": "estimated_session_placement", "reason": "Observed work; estimated placement."},
            {"type": "observed_capacity_effort_cap", "requested_minutes": 3,
             "observed_minutes": 1, "proposed_minutes": 1},
            {"type": "allocation_capacity_recovery", "requested_minutes": 3,
             "allocator_allocated_minutes": 1, "recovered_minutes": 1,
             "credited_minutes": 0, "residual_minutes": 1},
        ]
        second = proposal(2, "2026-09-29T10:10:00+03:00", "2026-09-29T10:11:23+03:00", 83)
        originals = copy.deepcopy([first, second])
        result = self.package([first, second])
        self.assertEqual([], result["retained"])
        warnings = result["activities"][0].get("child_review_warnings")
        self.assertEqual([
            {"review_id": identity(1), "warnings": originals[0]["review_warnings"]},
            {"review_id": identity(2), "warnings": []},
        ], warnings)
        warnings[0]["warnings"][0]["reason"] = "Changed output only"
        self.assertEqual(originals, [first, second])
        duplicate = self.package([first, copy.deepcopy(first)])
        self.assertEqual([], duplicate["activities"])
        self.assertEqual(2, len(duplicate["retained"]))
        for retained in duplicate["retained"]:
            self.assertEqual(originals[0], retained["proposal"])
            self.assertIn("duplicate_review_identity", retained["preparation_reasons"])
            self.assertNotIn("credited_overlap_receipt", retained["proposal"])

    def test_source_inputs_and_supplied_mappings_are_unchanged(self):
        sources = [proposal(1)]
        descriptions = {identity(1): "SC — Prepared task"}
        contexts = {identity(1): "task"}
        original = copy.deepcopy((sources, descriptions, contexts))
        result = self.package(sources, editorial_descriptions=descriptions, work_contexts=contexts)
        self.assertEqual(original, (sources, descriptions, contexts))
        result["activities"][0]["allocation_segments"][0]["start"] = "changed result"
        self.assertEqual(original, (sources, descriptions, contexts))

    def test_default_microbatch_conserves_every_child_once_without_rounding(self):
        sources = [proposal(n, f"2026-09-29T10:{(n-1)*6:02d}:00+03:00",
                            f"2026-09-29T10:{(n-1)*6+4:02d}:00+03:00", 240) for n in range(1, 6)]
        result = self.package(sources)
        self.assertEqual([1200], [a["duration_seconds"] for a in result["activities"]])
        children = [child for a in result["activities"] for child in a["child_review_ids"]]
        self.assertEqual([identity(n) for n in range(1, 6)], children)
        self.assertEqual(1200, sum(a["duration_seconds"] for a in result["activities"]))

    def test_two_short_sources_reach_fifteen_minutes_without_extra_rows(self):
        result = self.package([proposal(1, "2026-09-29T10:00:00+03:00", "2026-09-29T10:07:30+03:00", 450),
                               proposal(2, "2026-09-29T11:00:00+03:00", "2026-09-29T11:07:30+03:00", 450)])
        self.assertEqual([900], [a["duration_seconds"] for a in result["activities"]])

    def test_natural_batch_keeps_all_34_or_19_compatible_children_without_threshold_splits(self):
        for count, legacy_parents, expected_seconds in ((34, 10, 9120), (19, 5, 4620)):
            with self.subTest(children=count):
                sources = natural_batch_sources(count)
                descriptions = {identity(n): f"SC — Prepared task {n}" for n in range(1, count + 1)}
                contexts = dict.fromkeys(descriptions, "explicit-task")
                legacy = self.package(sources, editorial_descriptions=descriptions, work_contexts=contexts)
                self.assertEqual(legacy_parents, len(legacy["activities"]))
                natural = self.package(sources, editorial_descriptions=descriptions, work_contexts=contexts,
                                       natural_batches=True)
                self.assertEqual([], natural["retained"])
                self.assertEqual(1, len(natural["activities"]))
                group = natural["activities"][0]
                self.assertEqual(expected_seconds, group["duration_seconds"])
                self.assertEqual([identity(n) for n in range(1, count + 1)], group["child_review_ids"])
                self.assertEqual([(p["start"], p["end"], p["duration_seconds"]) for p in sources],
                                 [(p["start"], p["end"], p["duration_seconds"]) for p in group["allocation_segments"]])
                self.assertEqual([p["review_warnings"] for p in sources],
                                 [p["warnings"] for p in group["child_review_warnings"]])
                self.assertEqual(natural, batches.verify_package(natural, proposals=sources,
                    work_contexts=contexts, natural_batches=True))
                with self.assertRaises(ValueError):
                    batches.verify_package(natural, proposals=sources, work_contexts=contexts)

    def test_natural_strategy_alone_does_not_group_terminal_human_decisions(self):
        for status in ("skip", "rejected", "superseded"):
            with self.subTest(status=status):
                source = {**proposal(1), "disposition": status}
                result = self.package([source], natural_batches=True)
                self.assertEqual([], result["activities"])
                self.assertEqual(source, result["retained"][0]["proposal"])
                self.assertIn("protected_terminal_status", result["retained"][0]["preparation_reasons"])

    def test_protected_human_corrected_meeting_and_unresolved_sources_are_retained(self):
        variants = [
            ({}, {identity(1)}, "protected_review_id"),
            ({"disposition": "approved"}, set(), "approved_or_posted"),
            ({"review_status": "posted"}, set(), "approved_or_posted"),
            ({"human_corrected": True}, set(), "human_corrected"),
            ({"provenance": {"source_type": "recorded_meeting"}}, set(), "meeting"),
            ({"routing_disposition": "unresolved-routing"}, set(), "unresolved_routing"),
            ({"review_warnings": [{"type": "existing_clockify_overlap"}]}, set(), "overlap_warning"),
        ]
        for changes, protected, reason in variants:
            with self.subTest(reason=reason):
                source = {**proposal(1), **changes}
                result = self.package([source], protected_ids=protected)
                self.assertEqual([], result["activities"])
                self.assertEqual(source, result["retained"][0]["proposal"])
                self.assertIn(reason, result["retained"][0]["preparation_reasons"])

    def test_missing_or_inconsistent_duration_and_invalid_intervals_are_retained(self):
        for field, value in (("duration_seconds", None), ("duration_seconds", 70),
                             ("duration_seconds", True), ("start", "bad"),
                             ("end", "2026-09-29T10:00:07+03:00"),
                             ("start", "2026-09-29T10:00:07")):
            with self.subTest(field=field, value=value):
                source = proposal(1)
                if value is None:
                    source.pop(field)
                else:
                    source[field] = value
                result = self.package([source])
                self.assertEqual([], result["activities"])
                self.assertEqual(source, result["retained"][0]["proposal"])
                self.assertTrue(result["retained"][0]["preparation_reasons"])

    def test_fractional_elapsed_seconds_are_not_truncated(self):
        source = proposal(1, end="2026-09-29T10:01:18.5+03:00")
        result = self.package([source])
        self.assertEqual([], result["activities"])
        self.assertIn("invalid_duration_seconds", result["retained"][0]["preparation_reasons"])

    def test_all_source_interval_overlaps_block_grouping_even_with_protected_other_project(self):
        other = proposal(2, "2026-09-29T10:01:00+03:00", "2026-09-29T10:02:00+03:00", 60)
        other["client_project"] = "Other client"
        result = self.package([proposal(1), other], protected_ids={identity(2)})
        self.assertEqual([], result["activities"])
        self.assertEqual(2, len(result["retained"]))
        for row in result["retained"]:
            self.assertIn("source_interval_overlap", row["preparation_reasons"])

    def test_date_project_tag_billable_and_explicit_work_context_boundaries_separate_batches(self):
        second = proposal(2, "2026-09-29T11:00:00+03:00", "2026-09-29T11:01:11+03:00")
        variants = [{"client_project": "Other"}, {"tag_names": ["Design"]},
                    {"billable": False}, {"clockify_project_suffix": "other1"},
                    {"start": "2026-09-30T11:00:00+03:00", "end": "2026-09-30T11:01:11+03:00"}]
        for changes in variants:
            with self.subTest(changes=changes):
                result = self.package([proposal(1), {**second, **changes}])
                self.assertEqual(2, len(result["activities"]))
        result = self.package([proposal(1), second], work_contexts={identity(1): "one", identity(2): "another"})
        self.assertEqual(2, len(result["activities"]))
        result = self.package([proposal(1), second], work_contexts={})
        self.assertEqual([], result["activities"])
        self.assertEqual(2, len(result["retained"]))

    def test_cross_midnight_source_is_retained_and_local_date_not_utc_controls_grouping(self):
        crossing = proposal(1, "2026-09-29T23:59:30+03:00", "2026-09-30T00:00:30+03:00", 60)
        result = self.package([crossing])
        self.assertEqual([], result["activities"])
        self.assertIn("cross_local_day", result["retained"][0]["preparation_reasons"])
        sources = [proposal(1, "2026-09-29T20:59:00Z", "2026-09-29T21:00:00Z", 60),
                   proposal(2, "2026-09-29T21:01:00Z", "2026-09-29T21:02:00Z", 60)]
        self.assertEqual(2, len(self.package(sources)["activities"]))

    def test_repeat_and_permutation_keep_identical_ids_and_exact_output(self):
        sources = [proposal(1), proposal(2, "2026-09-29T11:00:00+03:00", "2026-09-29T11:01:11+03:00")]
        first = self.package(sources)
        self.assertEqual(first, self.package(sources))
        self.assertEqual(first, self.package(list(reversed(sources))))
        self.assertRegex(first["activities"][0]["review_id"], r"^pvi-[a-f0-9]{24}$")

    def test_duplicate_and_missing_stable_id_sources_are_retained_not_dropped(self):
        source = proposal(1)
        result = self.package([source, copy.deepcopy(source)])
        self.assertEqual([], result["activities"])
        self.assertEqual(2, len(result["retained"]))
        self.assertIn("duplicate_review_identity", result["retained"][0]["preparation_reasons"])
        source.pop("review_activity_key")
        result = self.package([source])
        self.assertEqual(source, result["retained"][0]["proposal"])
        self.assertIn("invalid_review_identity", result["retained"][0]["preparation_reasons"])

    def test_invalid_identity_provenance_and_conflicting_legacy_minutes_are_retained(self):
        variants = [
            ({"review_activity_key": "wka-not-a-real-digest"}, "invalid_review_identity"),
            ({"allocation_segment": 1.5}, "invalid_review_identity"),
            ({"provenance": {"evidence_ids": None}}, "invalid_provenance"),
            ({"duration_minutes": 50}, "inconsistent_duration_minutes"),
        ]
        for changes, reason in variants:
            with self.subTest(reason=reason, changes=changes):
                source = {**proposal(1), **changes}
                try:
                    result = self.package([source])
                except TypeError as error:
                    self.fail(f"Malformed source must be retained, not crash packaging: {error}")
                self.assertEqual([], result["activities"])
                self.assertEqual(source, result["retained"][0]["proposal"])
                self.assertIn(reason, result["retained"][0]["preparation_reasons"])

    def test_long_source_is_retained_and_touching_windows_are_valid_not_overlaps(self):
        long_source = proposal(1, "2026-09-29T10:00:00+03:00", "2026-09-29T10:15:00+03:00", 900)
        result = self.package([long_source])
        self.assertEqual([], result["activities"])
        self.assertIn("not_micro_entry", result["retained"][0]["preparation_reasons"])
        result = self.package([proposal(1), proposal(2, "2026-09-29T10:01:18+03:00", "2026-09-29T10:02:18+03:00", 60)])
        self.assertEqual([], result["retained"])
        self.assertEqual(131, result["activities"][0]["duration_seconds"])


if __name__ == "__main__":
    unittest.main()
