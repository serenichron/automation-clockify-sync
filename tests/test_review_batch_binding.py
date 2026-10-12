"""Consumers must bind batch windows to the original source proposals."""
import copy
import unittest

from scripts import clockify_review_batches as batches


def source(number, start, end, seconds):
    return {
        "review_activity_key": f"wka-{number:024x}", "allocation_segment": 1,
        "activity_id": f"act-{number:024x}", "start": start, "end": end,
        "duration_seconds": seconds, "duration_minutes": seconds // 60,
        "client_project": "Example", "tag_names": ["Development"],
        "tag_suffixes": ["def456"], "clockify_project_suffix": "abc123",
        "billable": True, "confidence": "high", "review_warnings": [],
        "provenance": {"source_type": "semantic_activity", "evidence_ids": [f"ev-{number}"]},
    }


class ReviewBatchBindingTests(unittest.TestCase):
    def setUp(self):
        self.sources = [
            source(1, "2026-09-29T10:00:07+03:00", "2026-09-29T10:01:18+03:00", 71),
            source(2, "2026-09-29T10:10:00+03:00", "2026-09-29T10:11:23+03:00", 83),
        ]
        self.ids = [f"wka-{n:024x}-s01" for n in (1, 2)]
        self.contexts = dict.fromkeys(self.ids, "verified-work-context")
        self.package = batches.package_review_batches(
            self.sources, editorial_descriptions=dict.fromkeys(self.ids, "SC — Prepared client deliverables"),
            work_contexts=self.contexts,
        )

    def verify(self, package=None, sources=None, **kwargs):
        verifier = getattr(batches, "verify_package", None)
        self.assertIsNotNone(verifier, "shared source-bound batch verifier is missing")
        return verifier(self.package if package is None else package,
                        proposals=self.sources if sources is None else sources,
                        work_contexts=kwargs.pop("work_contexts", self.contexts), **kwargs)

    def test_accepts_exact_source_bound_windows_without_mutating_inputs(self):
        original = copy.deepcopy((self.package, self.sources, self.contexts))
        verified = self.verify()
        self.assertEqual(154, verified["activities"][0]["duration_seconds"])
        self.assertEqual(self.ids, verified["activities"][0]["child_review_ids"])
        self.assertEqual(original, (self.package, self.sources, self.contexts))
        verified["activities"][0]["allocation_segments"][0]["end"] = "changed result"
        self.assertEqual(original, (self.package, self.sources, self.contexts))

    def test_rejects_extra_seconds_even_when_envelope_and_segment_totals_agree(self):
        forged = copy.deepcopy(self.package)
        row = forged["activities"][0]
        row["allocation_segments"][1].update(end="2026-09-29T10:12:23+03:00", duration_seconds=143,
                                              duration_minutes=2)
        row.update(end="2026-09-29T10:12:23+03:00", duration_seconds=214, duration_minutes=3)
        with self.assertRaises(ValueError):
            self.verify(forged)

    def test_rejects_duplicate_or_missing_child_and_unbound_wording(self):
        for field, value in (("child_review_ids", [self.ids[0], self.ids[0]]),
                             ("child_review_ids", [self.ids[0]]),
                             ("description", "SC — Claimed a different deliverable")):
            with self.subTest(field=field, value=value):
                forged = copy.deepcopy(self.package)
                forged["activities"][0][field] = value
                with self.assertRaises(ValueError):
                    self.verify(forged)

    def test_rejects_changed_source_work_context_or_protected_child(self):
        altered_sources = copy.deepcopy(self.sources)
        altered_sources[0]["provenance"]["evidence_ids"] = ["different-evidence"]
        with self.assertRaises(ValueError):
            self.verify(sources=altered_sources)
        with self.assertRaises(ValueError):
            self.verify(work_contexts=dict.fromkeys(self.ids, "different-context"))
        with self.assertRaises(ValueError):
            self.verify(protected_ids={self.ids[0]})

    def test_retained_sources_cannot_be_omitted_changed_or_added(self):
        held = source(3, "2026-09-29T11:00:00+03:00", "2026-09-29T11:01:00+03:00", 60)
        held["human_corrected"] = True
        sources = [*self.sources, held]
        package = batches.package_review_batches(
            sources, editorial_descriptions=dict.fromkeys(self.ids, "SC — Prepared client deliverables"),
            work_contexts=self.contexts,
        )
        self.assertEqual(1, len(self.verify(package, sources)["retained"]))
        for change in ("omit", "alter", "add"):
            with self.subTest(change=change):
                forged = copy.deepcopy(package)
                if change == "omit":
                    forged["retained"] = []
                elif change == "alter":
                    forged["retained"][0]["proposal"]["duration_seconds"] = 59
                else:
                    forged["retained"].append(copy.deepcopy(forged["retained"][0]))
                with self.assertRaises(ValueError):
                    self.verify(forged, sources)

    def test_rejects_missing_prepared_description_and_false_validation_stamp(self):
        forged = copy.deepcopy(self.package)
        forged["activities"][0]["prepared_descriptions"].pop()
        with self.assertRaises(ValueError):
            self.verify(forged)
        forged = copy.deepcopy(self.package)
        forged["activities"][0]["validation_status"] = "flash_validated"
        with self.assertRaises(ValueError):
            self.verify(forged)


if __name__ == "__main__":
    unittest.main()
