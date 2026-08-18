import copy
import importlib.util
from pathlib import Path
import unittest

from scripts import calendly_collector, evidence_ledger


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "clockify_portfolio_quality.py"
SPEC = importlib.util.spec_from_file_location("clockify_portfolio_quality", SCRIPT)
quality = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(quality)


def fathom_event(day="2026-07-10", source_id="meeting-one", *, naive=False):
    start = f"{day} 09:00" if naive else f"{day}T09:00+03:00"
    end = f"{day} 09:30" if naive else f"{day}T09:30+03:00"
    return evidence_ledger.evidence_event(
        "fathom",
        {"source_type": "fathom", "source_id": source_id},
        raw_source_span={"start": start, "end": end},
        attributes={
            "recorded_by_email": "vlad@serenichron.com",
            "meeting_id": f"events/{source_id}",
            "title": "July review",
        },
    )


def calendly_event(day="2026-07-10", source_id="calendar-one", *, meeting_id="meeting-one", start="06:00:00", end="06:30:00"):
    recording = calendly_collector.normalized_recording({
        "uri": f"recordings/{source_id}",
        "event_uri": f"events/{meeting_id}",
        "name": "July review",
        "recording_start_time": f"{day}T{start}Z",
        "recording_end_time": f"{day}T{end}Z",
        "organizer": {"email": "vlad@serenichron.com"},
        "participants": [{"email": "client@example.test"}],
        "transcript": [{"offset_seconds": 0, "text": "Reviewed July work"}],
    })
    return evidence_ledger.evidence_event(
        "calendly",
        {
            "source_type": "calendly",
            "source_id": recording["recording_id"],
            "meeting_id": recording["meeting_id"],
        },
        observed_at=recording["start"],
        raw_source_span={"start": recording["start"], "end": recording["end"]},
        attributes={key: value for key, value in recording.items() if key not in {"start", "end"}},
    )


def clockify_event(day="2026-07-10", source_id="clockify-one"):
    return evidence_ledger.evidence_event(
        "clockify",
        {"source_type": "clockify", "source_id": source_id},
        raw_source_span={"start": f"{day}T09:00+03:00", "end": f"{day}T09:30+03:00"},
        attributes={"description": "Existing July meeting"},
    )


def ledger(*events, timezone="Europe/Bucharest"):
    fathom_count = sum(event.source_type == "fathom" for event in events)
    calendly_count = sum(event.source_type == "calendly" for event in events)
    immutable = evidence_ledger.EvidenceLedger(tuple(events), {
        "fathom": {
            "status": "complete",
            "expected_count": fathom_count,
            "observed_count": fathom_count,
        },
        **({
            "calendly": {
                "status": "complete",
                "expected_count": calendly_count,
                "observed_count": calendly_count,
            },
        } if calendly_count else {}),
    })
    document = {
        "schema_version": evidence_ledger.SCHEMA_VERSION,
        "manifest": immutable.manifest.document(),
        "events": [event.document() for event in immutable.events],
    }
    if timezone is not None:
        document["manifest"] = evidence_ledger.LedgerManifest(
            events_digest=document["manifest"]["events_digest"],
            event_count=document["manifest"]["event_count"],
            source_inventory=document["manifest"]["source_inventory"],
            timezone=timezone,
        ).document()
    return document


def document(event, description="SC — Rebuilt Clockify review into invoice-ready July entries"):
    return {
        "range": {"since": "2026-07-01", "until": "2026-07-31"},
        "model": quality.REQUIRED_MODEL,
        "revision": quality.REQUIRED_REVISION,
        "source_minutes": 30,
        "review_minutes": 30,
        "excluded_minutes": 0,
        "activities": [{
            "review_id": "pvi-one",
            "source_activity_ids": ["act-one"],
            "evidence_ids": [event.evidence_id],
            "description": description,
            "duration_minutes": 30,
            "start": "2026-07-10T09:00+03:00",
            "end": "2026-07-10T09:30+03:00",
            "allocation_segments": [{
                "start": "2026-07-10T09:00+03:00",
                "end": "2026-07-10T09:30+03:00",
                "duration_minutes": 30,
            }],
            "client_project": "Serenichron Level 2",
            "tag_names": ["System development"],
            "semantic_reviewer_model": quality.REQUIRED_MODEL,
            "semantic_reviewer_revision": quality.REQUIRED_REVISION,
            "validation_status": "flash_validated",
        }],
        "exceptions": [],
        "omissions": [],
        "groups": [{
            "source_minutes": 30,
            "review_minutes": 30,
            "excluded_minutes": 0,
            "exclusion_reasons": [],
            "exceptions": 0,
            "omissions": 0,
        }],
    }


def proposals():
    return [{
        "activity_id": "act-one",
        "start": "2026-07-10T09:00+03:00",
        "end": "2026-07-10T09:30+03:00",
        "duration_minutes": 30,
        "client_project": "Serenichron Level 2",
        "tag_names": ["System development"],
    }]


def routing():
    return {"session_routes": [{
        "project_name": "Serenichron Level 2", "prefix": "SC",
        "tag_names": ["System development"],
    }], "meeting_routes": []}


def routing_with_emblem_alias():
    return {
        **routing(),
        "prefix_overrides": [
            {"project_name_prefix": "Serenichron", "prefix": "ES"}
        ],
    }


class PortfolioQualityTests(unittest.TestCase):
    def audit(self, value, source=None, evidence=None):
        event = fathom_event()
        return quality.audit(value, evidence or ledger(event), source_proposals=proposals() if source is None else source)

    def test_clean_review_passes_with_immutable_fathom_coverage(self):
        event = fathom_event()
        report = quality.audit(document(event), ledger(event), source_proposals=proposals())

        self.assertEqual("pass", report["status"])
        self.assertEqual({"expected": 1, "represented": 1, "excluded": 0, "missing": 0}, {key: report["fathom_coverage"][key] for key in ("expected", "represented", "excluded", "missing")})
        self.assertEqual(30, report["fragmentation"]["median_minutes"])

    def test_every_source_recording_is_accounted_once_by_canonical_meeting(self):
        fathom = fathom_event()
        calendly = calendly_event()
        value = document(fathom)
        value["activities"][0]["evidence_ids"] = [
            fathom.evidence_id,
            calendly.evidence_id,
        ]

        report = quality.audit(
            value,
            ledger_document=ledger(fathom, calendly),
            source_proposals=proposals(),
        )

        self.assertEqual("pass", report["status"])
        self.assertEqual(
            {"source_recordings": 2, "canonical_meetings": 1, "missing": 0},
            {key: report["recording_coverage"][key] for key in (
                "source_recordings", "canonical_meetings", "missing",
            )},
        )
        self.assertEqual(
            {
                fathom.evidence_id: "represented",
                calendly.evidence_id: "represented",
            },
            report["recording_coverage"]["source_dispositions"],
        )

    def test_timing_conflict_is_a_blocking_recording_exception(self):
        fathom = fathom_event()
        calendly = calendly_event(start="06:10:00", end="06:40:00")
        report = quality.audit(
            document(fathom),
            ledger_document=ledger(fathom, calendly),
            source_proposals=proposals(),
        )

        self.assertEqual("blocked", report["status"])
        self.assertEqual(1, report["recording_coverage"]["exceptions"])
        self.assertEqual(
            {fathom.evidence_id, calendly.evidence_id},
            set(report["recording_coverage"]["exception_evidence_ids"]),
        )
        self.assertTrue(any("reconciliation exception" in item["reason"] for item in report["structural_issues"]))

    def test_multiple_candidates_are_a_blocking_recording_exception(self):
        fathom = fathom_event()
        first = calendly_event(source_id="calendar-one")
        second = calendly_event(source_id="calendar-two")
        report = quality.audit(
            document(fathom),
            ledger_document=ledger(fathom, first, second),
            source_proposals=proposals(),
        )

        self.assertEqual("blocked", report["status"])
        self.assertEqual(
            {fathom.evidence_id, first.evidence_id, second.evidence_id},
            set(report["recording_coverage"]["exception_evidence_ids"]),
        )

    def test_canonical_routing_requires_exact_prefix_and_source_route(self):
        event = fathom_event()
        self.assertEqual("pass", quality.audit(document(event), ledger(event), source_proposals=proposals(), routing=routing())["status"])
        wrong_prefix = document(event)
        wrong_prefix["activities"][0]["description"] = "TST — Rebuilt Clockify review into invoice-ready July entries"
        report = quality.audit(wrong_prefix, ledger(event), source_proposals=proposals(), routing=routing())
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("canonical routing taxonomy" in issue["reason"] for issue in report["structural_issues"]))
        mixed = [*proposals(), {**proposals()[0], "client_project": "Other", "tag_names": ["SEO"]}]
        report = quality.audit(document(event), ledger(event), source_proposals=mixed, routing=routing())
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("mixed, missing, or unknown" in issue["reason"] for issue in report["structural_issues"]))

    def test_canonical_routing_accepts_configured_project_prefix_alias(self):
        event = fathom_event()
        value = document(
            event,
            description="ES — Rebuilt EmblemStudio review into invoice-ready entries",
        )

        report = quality.audit(
            value,
            ledger(event),
            source_proposals=proposals(),
            routing=routing_with_emblem_alias(),
        )

        self.assertEqual("pass", report["status"])

    def test_cited_fathom_meeting_requires_its_full_interval_in_row_allocation(self):
        event = fathom_event()
        value = document(event)
        row = value["activities"][0]
        row["end"] = "2026-07-10T09:20+03:00"
        row["duration_minutes"] = 20
        row["allocation_segments"][0].update({"end": row["end"], "duration_minutes": 20})
        value.update({"source_minutes": 20, "review_minutes": 20, "excluded_minutes": 0})
        value["groups"][0].update({"source_minutes": 20, "review_minutes": 20})
        source = [{**proposals()[0], "end": row["end"], "duration_minutes": 20}]
        report = quality.audit(value, ledger(event), source_proposals=source, routing=routing())
        self.assertEqual("blocked", report["status"])
        self.assertEqual(1, report["fathom_coverage"]["uncovered"])
        self.assertTrue(any("meeting interval is not covered" in issue["reason"] for issue in report["structural_issues"]))

    def test_broader_proposal_input_is_scoped_to_review_range(self):
        event = fathom_event()
        broader = [{
            "start": "2026-06-30T09:00+03:00",
            "end": "2026-06-30T10:00+03:00",
            "duration_minutes": 60,
        }, *proposals()]
        report = quality.audit(document(event), ledger(event), source_proposals=broader)

        self.assertEqual("pass", report["status"])
        self.assertEqual({"available": True, "in_range_proposal_count": 1, "in_range_proposal_minutes": 30}, report["source_proposal_coverage"])

    def test_missing_or_tampered_ledger_blocks(self):
        event = fathom_event()
        value = document(event)
        self.assertEqual("blocked", quality.audit(value)["status"])
        bad = ledger(event)
        bad["events"][0]["attributes"]["title"] = "tampered"
        self.assertEqual("blocked", quality.audit(value, bad)["status"])

    def test_unknown_or_duplicate_row_evidence_blocks(self):
        event = fathom_event()
        value = document(event)
        value["activities"][0]["evidence_ids"] = [event.evidence_id, event.evidence_id]
        report = quality.audit(value, ledger(event), source_proposals=proposals())
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("duplicate or empty evidence" in item["reason"] for item in report["structural_issues"]))
        value = document(event)
        value["activities"][0]["evidence_ids"] = ["ev-" + "0" * 64]
        self.assertEqual("blocked", quality.audit(value, ledger(event), source_proposals=proposals())["status"])

    def test_eligible_fathom_must_be_represented_or_explicitly_excluded(self):
        first, second = fathom_event(source_id="one"), fathom_event(source_id="two")
        value = document(first)
        report = quality.audit(value, ledger(first, second), source_proposals=proposals())
        self.assertEqual("blocked", report["status"])
        self.assertEqual(1, report["fathom_coverage"]["missing"])
        value["omissions"] = [{"evidence_ids": [second.evidence_id], "reason": "duplicate meeting was already represented"}]
        report = quality.audit(value, ledger(first, second), source_proposals=proposals())
        self.assertEqual("pass", report["status"])
        self.assertEqual(1, report["fathom_coverage"]["excluded"])

    def test_existing_clockify_reconciliation_excludes_eligible_fathom_meeting(self):
        meeting, existing = fathom_event(), clockify_event()
        value = document(meeting)
        value.update({"activities": [], "source_minutes": 0, "review_minutes": 0, "excluded_minutes": 0, "groups": []})
        report = quality.audit(value, ledger(meeting, existing), source_proposals=[])

        self.assertEqual("pass", report["status"])
        self.assertEqual(1, report["fathom_coverage"]["excluded"])
        self.assertEqual(0, report["fathom_coverage"]["missing"])
        self.assertEqual(1, report["fathom_coverage"]["excluded_by_reason"]["existing_clockify_meeting_match"])

    def test_naive_fathom_timestamp_reconciles_with_aware_clockify_block(self):
        meeting, existing = fathom_event(naive=True), clockify_event()
        value = document(meeting)
        value.update({"activities": [], "source_minutes": 0, "review_minutes": 0, "excluded_minutes": 0, "groups": []})

        report = quality.audit(value, ledger(meeting, existing), source_proposals=[])

        self.assertEqual("pass", report["status"])
        self.assertEqual(1, report["fathom_coverage"]["excluded_by_reason"]["existing_clockify_meeting_match"])

    def test_manifest_timezone_controls_local_minute_fathom_reconciliation(self):
        fathom = fathom_event(naive=True)
        calendly = calendly_event()

        report = quality.audit(
            document(fathom),
            ledger(fathom, calendly, timezone="America/New_York"),
            source_proposals=proposals(),
        )

        self.assertEqual("blocked", report["status"])
        self.assertEqual(1, report["recording_coverage"]["exceptions"])

    def test_fathom_account_without_reason_blocks(self):
        first, second = fathom_event(source_id="one"), fathom_event(source_id="two")
        value = document(first)
        value["exceptions"] = [{"evidence_ids": [second.evidence_id]}]
        self.assertEqual("blocked", quality.audit(value, ledger(first, second), source_proposals=proposals())["status"])

    def test_inconsistent_fathom_source_inventory_blocks(self):
        event = fathom_event()
        source = ledger(event)
        source["manifest"]["source_inventory"]["fathom"]["observed_count"] = 2
        source["manifest"]["source_completeness"]["sources"]["fathom"]["observed_count"] = 2
        source["manifest"].pop("manifest_id")
        # Rebuild only the manifest with an internally consistent digest binding.
        source["manifest"] = evidence_ledger.LedgerManifest(
            events_digest=source["manifest"]["events_digest"],
            event_count=source["manifest"]["event_count"],
            source_inventory={"fathom": {"status": "complete", "expected_count": 1, "observed_count": 2}},
        ).document()
        report = quality.audit(document(event), source, source_proposals=proposals())
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("source expected and observed counts differ" in item["reason"] for item in report["structural_issues"]))

    def test_minutes_and_authoritative_source_pool_must_match(self):
        event = fathom_event()
        value = document(event)
        value["source_minutes"] = 20
        self.assertEqual("blocked", quality.audit(value, ledger(event), source_proposals=proposals())["status"])
        value = document(event)
        outside_pool = [{"start": "2026-07-10T10:00+03:00", "end": "2026-07-10T10:30+03:00", "duration_minutes": 30}]
        self.assertEqual("blocked", quality.audit(value, ledger(event), source_proposals=outside_pool)["status"])

    def test_excluded_minutes_balance_with_evidence_backed_group_reason(self):
        event = fathom_event()
        value = document(event)
        value["source_minutes"] = 30
        value["review_minutes"] = 20
        value["excluded_minutes"] = 10
        row = value["activities"][0]
        row["duration_minutes"] = 20
        row["end"] = "2026-07-10T09:20+03:00"
        row["allocation_segments"][0].update({"end": "2026-07-10T09:20+03:00", "duration_minutes": 20})
        value["omissions"] = [{"evidence_ids": [event.evidence_id], "reason": "remaining source time excluded after review"}]
        value["groups"] = [{
            "source_minutes": 30,
            "review_minutes": 20,
            "excluded_minutes": 10,
            "exclusion_reasons": [{"disposition": "omission", "reason": "remaining source time excluded after review", "evidence_count": 1}],
            "exceptions": 0,
            "omissions": 1,
        }]

        report = quality.audit(value, ledger(event), source_proposals=proposals())
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("meeting interval is not covered" in item["reason"] for item in report["structural_issues"]))

    def test_accounting_schema_rejects_unbalanced_top_or_group_exclusions(self):
        event = fathom_event()
        value = document(event)
        value["review_minutes"] = 29
        self.assertEqual("blocked", quality.audit(value, ledger(event), source_proposals=proposals())["status"])
        value = document(event)
        value["review_minutes"] = 20
        value["excluded_minutes"] = 10
        row = value["activities"][0]
        row["duration_minutes"] = 20
        row["end"] = "2026-07-10T09:20+03:00"
        row["allocation_segments"][0].update({"end": "2026-07-10T09:20+03:00", "duration_minutes": 20})
        value["groups"][0].update({"review_minutes": 20, "excluded_minutes": 10})
        report = quality.audit(value, ledger(event), source_proposals=proposals())
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("excluded group minutes lack" in item["reason"] for item in report["structural_issues"]))

    def test_model_revision_and_project_tag_provenance_are_exact_and_nonempty(self):
        event = fathom_event()
        value = document(event)
        value["model"] = "other-model"
        value["activities"][0]["semantic_reviewer_revision"] = "a" * 64
        value["activities"][0]["tag_names"] = []
        report = quality.audit(value, ledger(event), source_proposals=proposals())
        self.assertEqual("blocked", report["status"])
        self.assertGreaterEqual(len(report["structural_issues"]), 3)

    def test_carried_source_review_cannot_pass_portfolio_quality(self):
        event = fathom_event()
        value = document(event)
        value["activities"][0]["validation_status"] = (
            "source_semantic_review_carried_after_flash_contract_failure"
        )

        report = quality.audit(
            value,
            ledger(event),
            source_proposals=proposals(),
            routing=routing(),
        )

        self.assertEqual("blocked", report["status"])
        self.assertTrue(
            any(
                "Flash portfolio validation" in issue["reason"]
                for issue in report["structural_issues"]
            )
        )

    def test_caveman_wording_is_a_repair_candidate_not_a_rewrite(self):
        event = fathom_event()
        value = document(event, "SC — needs review https://example.com")
        original = value["activities"][0]["description"]
        report = quality.audit(value, ledger(event), source_proposals=proposals())
        self.assertEqual("needs_semantic_repair", report["status"])
        self.assertEqual(original, value["activities"][0]["description"])
        self.assertEqual(1, len(report["semantic_repair_rows"]))

    def test_date_and_overlap_block_integrity(self):
        event = fathom_event()
        value = document(event)
        second = copy.deepcopy(value["activities"][0])
        second["review_id"] = "pvi-two"
        second["allocation_segments"][0].update({"start": "2026-07-10T09:15+03:00", "end": "2026-07-10T09:45+03:00"})
        second.update({"start": "2026-07-10T09:15+03:00", "end": "2026-07-10T09:45+03:00"})
        value["activities"].append(second)
        third = copy.deepcopy(second)
        third["review_id"] = "pvi-three"
        third["allocation_segments"][0].update({"start": "2026-08-01T09:15+03:00", "end": "2026-08-01T09:45+03:00"})
        third.update({"start": "2026-08-01T09:15+03:00", "end": "2026-08-01T09:45+03:00"})
        value["activities"].append(third)
        value["source_minutes"] = value["review_minutes"] = 90
        report = quality.audit(value, ledger(event), source_proposals=None)
        self.assertEqual("blocked", report["status"])
        self.assertTrue(any("outside review range" in item["reason"] for item in report["structural_issues"]))
        self.assertEqual(1, len(report["overlaps"]))


if __name__ == "__main__":
    unittest.main()
