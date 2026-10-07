"""Synthetic monthly unresolved publication contracts; no external services."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import contextlib
import io
import copy
from unittest import mock

from scripts import evidence_ledger
from scripts import clockify_sheet_publish as publisher
from scripts import clockify_monthly_unresolved as monthly
from scripts import clockify_review_cycle as cycle
from test_sheet_publish import MultiSheetGateway, proposal

LEGACY_HEADER = [
    "Evidence ID", "Local day", "Source / machine", "Exception kind",
    "Precise reason", "Route recommendation", "Sanitized accomplishment",
    "Duration", "Timing status", "Next step", "Disposition", "Evidence digest",
]


def frozen_run(root, ambiguous_count=25, meeting_link=False):
    events = [evidence_ledger.evidence_event(
        "fathom" if meeting_link and i == 0 else "codex_session", {"source_id": f"session-{i}", "machine": "test"},
        observed_at="2026-09-08T12:00:00Z" if i == 0 else "2026-09-09T12:00:00Z",
        raw_source_span={"start": "2026-09-09T12:00:00Z", "end": "2026-09-09T12:05:00Z"},
        attributes={"title": f"Synthetic work {i}",
                    **({"share_url": "https://fathom.video/share/synthetic-link"} if meeting_link and i == 0 else {})},
    ) for i in range(ambiguous_count + 2)]
    ledger = evidence_ledger.EvidenceLedger(tuple(events), timezone="Europe/Bucharest")
    ambiguities = [{"id": f"a-{i}", "exception_kind": "low_confidence",
                    "evidence_ids": [events[i].evidence_id]} for i in range(ambiguous_count)]
    proposals = [{"id": f"p-{i}", "routing_disposition": "unresolved-routing",
                  "evidence_ids": [events[ambiguous_count + i].evidence_id],
                  "start": "2026-09-09T12:00:00Z", "end": "2026-09-09T12:05:00Z",
                  "description": "Synthetic valid unrouted interval", "confidence": "high"} for i in range(2)]
    documents = {
        "evidence/evidence-ledger.json": {"schema_version": "evidence-ledger/v1",
            "manifest": ledger.manifest.document(), "events": [e.document() for e in ledger.events]},
        "ambiguous.json": ambiguities, "proposals.json": proposals,
        "work-accounting-result.json": {"schema_version": 1, "allocation_mode": "non_overlapping_v1", "skipped": [], "proposals": proposals, "ambiguous": ambiguities},
        "semantic-analysis.json": {"activities": []},
        "run-report.json": {"date_range": {"since": "2026-09-08T21:00:00Z", "until": "2026-09-10T21:00:00Z"}},
    }
    for relative, document in documents.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document))
    return documents


class MonthlyGateway:
    def __init__(self, rows):
        self.rows = rows
        self.writes = []

    def spreadsheet(self, _id):
        return {"sheets": [{"properties": {"sheetId": 7, "title": "September 2026 unresolved evidence",
            "gridProperties": {"rowCount": 1000, "columnCount": 12}}}]}

    def values(self, _id, _range):
        return [list(row) for row in self.rows]

    def append_monthly_rows(self, _id, sheet_id, start_row, grid_rows, rows):
        self.writes.append((sheet_id, start_row, grid_rows, rows))
        self.rows.extend([list(row) for row in rows])


class MonthlyTests(unittest.TestCase):
    def test_legacy_receipt_consumer_reconstructs_source_and_rejects_target_tampering(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frozen_run(root, 1)
            (root / "routing.json").write_text("{}")
            source = {"run_dir": str(root), "run_id": root.name, "review_ids": [],
                **{key: "fixture" for key in ("result_digest", "bundle_digest", "artifact_digests", "snapshot_digests", "proposals_digest", "accounting_digest", "quality_digest")}}
            replay = {**source, "replay_integrity_digest": "fixture"}
            config = {"spreadsheet_id": "test"}
            rows = monthly.project_rows(root)
            gateway = MonthlyGateway([list(LEGACY_HEADER), ["uev-exemplar"] + [""] * 11])
            result = publisher.publish_monthly_unresolved(gateway, spreadsheet_id="test",
                sheet_title="September 2026 unresolved evidence", rows=rows, source_dir=root)
            document = {"schema_version": "sheet-publication-result/v1", "status": "published",
                "external_writes": True, "clockify_writes": 0, "publications": [result]}
            expected = cycle._expected_publication_receipts(config, source, sheet_title="September 2026 portfolio review")
            validated = cycle._validated_publication_document(document, expected, source_dir=root)
            self.assertEqual(result["rows_sha256"], validated[0]["rows_sha256"])
            delivery = cycle._delivery_document(config, "2026-09-09", "2026-09-11", source, replay,
                sheet_title="September 2026 portfolio review", publication_readbacks=validated)
            receipt = root / "delivery.json"
            receipt.write_text(json.dumps(delivery))
            cycle._verify_delivery_receipt(receipt, config, "2026-09-09", "2026-09-11", source, replay,
                sheet_title="September 2026 portfolio review")
            for field, replacement in (("monthly_layout", "unknown"), ("monthly_target_readback", None)):
                bad = copy.deepcopy(document)
                bad["publications"][0][field] = replacement
                with self.assertRaises(cycle.CycleError):
                    cycle._validated_publication_document(bad, expected, source_dir=root)
            evidence = result["monthly_target_readback"]
            self.assertEqual(LEGACY_HEADER, evidence["header"])
            self.assertEqual(7, evidence["sheet_id"])
            self.assertEqual(3, len(evidence["machine_rows"]))
            for field, replacement in (("header", monthly.HEADER), ("spreadsheet_id", "wrong"),
                ("sheet_title", "October 2026 unresolved evidence"), ("sheet_id", "7"),
                ("schema_version", "unknown"), ("machine_rows", [])):
                bad = copy.deepcopy(document)
                bad_evidence = bad["publications"][0]["monthly_target_readback"]
                bad_evidence[field] = replacement
                # Even a repinned snapshot must match the source/target contract.
                unsigned = {key: value for key, value in bad_evidence.items() if key != "sha256"}
                bad_evidence["sha256"] = "sha256:" + hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                with self.assertRaises(cycle.CycleError):
                    cycle._validated_publication_document(bad, expected, source_dir=root)
            bad = copy.deepcopy(document)
            bad["publications"][0]["monthly_target_readback"]["machine_rows"][0][7] = "5"
            repinned = bad["publications"][0]["monthly_target_readback"]
            unsigned = {key: value for key, value in repinned.items() if key != "sha256"}
            repinned["sha256"] = "sha256:" + hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            with self.assertRaises(cycle.CycleError):
                cycle._validated_publication_document(bad, expected, source_dir=root)
            bad = copy.deepcopy(document)
            bad["publications"][0]["monthly_target_readback"]["sheet_id"] = 8
            with self.assertRaises(cycle.CycleError):
                cycle._validated_publication_document(bad, expected, source_dir=root)

    def test_legacy_collision_without_json_provenance_fails_before_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frozen_run(root, 1)
            rows = monthly.project_rows(root)
            old = list(rows[0])
            old[6], old[7], old[10], old[11] = "existing summary", "human timing note", "human decision", "sha256:legacy"
            gateway = MonthlyGateway([list(LEGACY_HEADER), old])
            before = copy.deepcopy(gateway.rows)
            with self.assertRaises(publisher.PublicationError):
                publisher.publish_monthly_unresolved(gateway, spreadsheet_id="test",
                    sheet_title="September 2026 unresolved evidence", rows=rows, source_dir=root)
            self.assertEqual(before, gateway.rows)
            self.assertEqual([], gateway.writes)

    def test_legacy_duration_layout_publishes_without_header_or_existing_row_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frozen_run(root, 1)
            (root / "routing.json").write_text("{}")
            rows = monthly.project_rows(root)
            # A canonical meeting link must never become legacy column H time.
            rows[0][7] = "https://fathom.video/share/synthetic-link"
            old = ["uev-exemplar", "2026-09-01", "test", "timing_evidence", "unconfirmed",
                   "", "old summary", "human duration note", "pending", "review", "human decision", "sha256:old"]
            gateway = MonthlyGateway([list(LEGACY_HEADER), old])
            before = copy.deepcopy(gateway.rows)
            kwargs = dict(spreadsheet_id="test", sheet_title="September 2026 unresolved evidence",
                          rows=rows, source_dir=root)
            result = publisher.publish_monthly_unresolved(gateway, **kwargs)
            self.assertEqual(before, gateway.rows[:2])
            self.assertEqual(3, result["appended"])
            for row in gateway.rows[2:]:
                self.assertEqual("", row[7])
                self.assertIn("no", row[9].lower())
            self.assertIn("https://fathom.video/share/synthetic-link", gateway.rows[2][9])
            self.assertEqual(rows[0][11], gateway.rows[2][11])
            physical = copy.deepcopy(gateway.rows[2:])
            digest = "sha256:" + hashlib.sha256(json.dumps(physical, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
            self.assertEqual(digest, result["rows_sha256"])
            gateway.rows[2][10] = "reviewed by human"
            retry_before = copy.deepcopy(gateway.rows)
            retry = publisher.publish_monthly_unresolved(gateway, **kwargs)
            self.assertEqual(0, retry["appended"])
            self.assertEqual(retry_before, gateway.rows)
            self.assertEqual(result["receipt_id"], retry["receipt_id"])

    def test_contested_time_uses_only_unique_exact_sealed_activity_citations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docs = frozen_run(root, 1)
            item = docs["ambiguous.json"][0]
            ids = item.pop("evidence_ids")
            item.update(exception_kind="contested_time", activity_id="exact-activity",
                        requested_minutes=25, allocated_minutes=23, unallocated_minutes=2)
            docs["semantic-analysis.json"]["activities"] = [{"activity_id": "exact-activity", "evidence_ids": ids}]
            for relative in ("ambiguous.json", "work-accounting-result.json", "semantic-analysis.json"):
                (root / relative).write_text(json.dumps(docs[relative]))
            frozen = {relative: (root / relative).read_bytes() for relative in docs}
            rows = monthly.project_rows(root)
            row = next(row for row in rows if row[3] == "contested_time")
            identity = json.dumps({"kind": "contested_time", "evidence_ids": sorted(set(ids))}, sort_keys=True, separators=(",", ":"))
            self.assertEqual("uev-" + hashlib.sha256(identity.encode()).hexdigest()[:24], row[0])
            self.assertEqual(sorted(ids), json.loads(row[11])["evidence_ids"])
            self.assertIn("23 minutes already allocated", row[4])
            self.assertIn("2 requested minutes remain unplaced", row[4])
            self.assertIn("not zero work", row[4])
            self.assertIn("not additional time", row[6])
            self.assertEqual(frozen, {relative: (root / relative).read_bytes() for relative in docs})
            for activities in ([], [{"activity_id": "wrong-activity", "evidence_ids": ids}],
                [{"activity_id": "exact-activity", "evidence_ids": ids}] * 2,
                [{"activity_id": "exact-activity", "evidence_ids": ["unknown-ledger-reference"]}]):
                (root / "semantic-analysis.json").write_text(json.dumps({"activities": activities}))
                with self.assertRaises(ValueError):
                    monthly.project_rows(root)

    def test_same_activity_unrouted_segments_share_evidence_row_without_losing_context(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docs = frozen_run(root, 1)
            first, second = docs["proposals.json"]
            first["activity_id"] = second["activity_id"] = "same-activity"
            second["evidence_ids"] = first["evidence_ids"]
            second.update(start="2026-09-09T13:00:00Z", end="2026-09-09T13:05:00Z")
            for relative in ("proposals.json", "work-accounting-result.json"):
                (root / relative).write_text(json.dumps(docs[relative]))
            rows = monthly.project_rows(root)
            self.assertEqual(2, len(rows))
            routing = next(row for row in rows if row[3] == "routing_gap")
            for value in (first["id"], second["id"], first["start"], first["end"], second["start"], second["end"]):
                self.assertIn(value, routing[4])
            self.assertIn("2 distinct", routing[6])
            second["activity_id"] = "different-activity"
            for relative in ("proposals.json", "work-accounting-result.json"):
                (root / relative).write_text(json.dumps(docs[relative]))
            with self.assertRaises(ValueError):
                monthly.project_rows(root)
            second["activity_id"] = first["activity_id"]
            docs["ambiguous.json"].append(dict(docs["ambiguous.json"][0]))
            for relative in ("ambiguous.json", "proposals.json", "work-accounting-result.json"):
                (root / relative).write_text(json.dumps(docs[relative]))
            with self.assertRaises(ValueError):
                monthly.project_rows(root)

    def test_legacy_layout_canonical_alias_preserves_first_source_and_human_k(self):
        self.test_automatic_canonical_alias_across_slices_preserves_first_row_and_k(header=LEGACY_HEADER)

    def test_automatic_canonical_alias_across_slices_preserves_first_row_and_k(self, header=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, second = root / "first", root / "second"
            frozen_run(first, 1)
            frozen_run(second, 1)
            (second / "run-report.json").write_text(json.dumps({"date_range": {
                "since": "2026-09-10T21:00:00Z", "until": "2026-09-12T21:00:00Z"}}))
            first_rows, second_rows = monthly.project_rows(first), monthly.project_rows(second)
            gateway = MonthlyGateway([header or monthly.HEADER, ["uev-exemplar"] + [""] * 11])
            kwargs = dict(spreadsheet_id="test", sheet_title="September 2026 unresolved evidence")
            publisher.publish_monthly_unresolved(gateway, **kwargs, rows=first_rows, source_dir=first)
            for row in gateway.rows[2:]:
                row[10] = "human confirmed"
            before = copy.deepcopy(gateway.rows)
            result = publisher.publish_monthly_unresolved(gateway, **kwargs, rows=second_rows, source_dir=second)
            self.assertEqual(0, result["appended"])
            self.assertEqual(before, gateway.rows)
            self.assertEqual(3, len(result["canonical_source_aliases"]))
            layout = monthly.LEGACY_LAYOUT if header is not None else None
            validated = monthly.validate_canonical_aliases(second, monthly.rows_for_layout(second_rows, layout),
                result["canonical_source_aliases"], layout=layout)
            self.assertEqual(result["canonical_source_aliases"], validated)
            for alias in validated:
                self.assertEqual("first", alias["source_run_id"])
                self.assertTrue(alias["preserved_machine_digest"].startswith("sha256:"))
                self.assertTrue(alias["historical_ledger_digest"].startswith("sha256:"))
            (second / "routing.json").write_text("{}")
            config = {"spreadsheet_id": "test"}
            source = {"run_dir": str(second), "run_id": second.name, "review_ids": [],
                **{key: "fixture" for key in ("result_digest", "bundle_digest", "artifact_digests", "snapshot_digests", "proposals_digest", "accounting_digest", "quality_digest")}}
            replay = {**source, "replay_integrity_digest": "fixture"}
            expected = cycle._expected_publication_receipts(config, source, sheet_title="September 2026 portfolio review")
            publisher_document = {"schema_version": "sheet-publication-result/v1", "status": "published",
                "external_writes": True, "clockify_writes": 0, "publications": [result]}
            readbacks = cycle._validated_publication_document(publisher_document, expected, source_dir=second)
            self.assertEqual(result["rows_sha256"], readbacks[0]["rows_sha256"])
            delivered = cycle._delivery_document(config, "2026-09-11", "2026-09-13", source, replay,
                sheet_title="September 2026 portfolio review", publication_readbacks=readbacks)
            self.assertEqual(validated, delivered["publication_receipts"][0]["canonical_source_aliases"])
            receipt = root / "canonical-delivery.json"
            receipt.write_text(json.dumps(delivered))
            original = receipt.read_bytes()
            cycle._verify_delivery_receipt(receipt, config, "2026-09-11", "2026-09-13", source, replay,
                sheet_title="September 2026 portfolio review")
            self.assertEqual(original, receipt.read_bytes())
            tampered = json.loads(json.dumps(publisher_document))
            provenance = json.loads(tampered["publications"][0]["canonical_source_aliases"][0]["historical_provenance"])
            provenance["evidence_ids"] = ["wrong citation"]
            tampered["publications"][0]["canonical_source_aliases"][0]["historical_provenance"] = json.dumps(provenance)
            with self.assertRaises(cycle.CycleError):
                cycle._validated_publication_document(tampered, expected, source_dir=second)
            writes = len(gateway.writes)
            ledger_path = first / "evidence/evidence-ledger.json"
            ledger_path.write_text(ledger_path.read_text() + " ")
            with self.assertRaises(publisher.PublicationError):
                publisher.publish_monthly_unresolved(gateway, **kwargs, rows=second_rows, source_dir=second)
            self.assertEqual(writes, len(gateway.writes))
            with self.assertRaises(cycle.CycleError):
                cycle._verify_delivery_receipt(receipt, config, "2026-09-11", "2026-09-13", source, replay,
                    sheet_title="September 2026 portfolio review")

    def test_serialized_legacy_adoption_keeps_hidden_receipts_and_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current = root / "runs" / "legacy-source"
            docs = frozen_run(current, 1)
            proposals = [{**proposal(index + 1), **row, "client_project": "", "clockify_project_suffix": "",
                "tag_suffixes": [], "tag_names": [], "billable": False, "duration_minutes": 5,
                "review_warnings": [{"type": "unresolved_routing", "disposition": "unresolved-routing", "reason_code": "no_deterministic_route"}]}
                for index, row in enumerate(docs["proposals.json"])]
            docs["proposals.json"] = proposals
            docs["work-accounting-result.json"]["proposals"] = proposals
            for relative in ("proposals.json", "work-accounting-result.json"):
                (current / relative).write_text(json.dumps(docs[relative]))
            (current / "routing.json").write_text("{}")
            config = {"runs_dir": str(root / "runs"), "spreadsheet_id": "test",
                "monthly_sheet_title_template": "{month_name} {year} portfolio review",
                "monthly_unresolved_alias_proof": "/must-not-load-for-legacy.json"}
            source = {"run_dir": str(current), "run_id": current.name, "runtime_identity_digest": "fixture",
                "coverage": {"status": "complete", "incomplete_sources": []}}
            replay = dict(source)
            receipts = cycle._expected_publication_receipts(config, source,
                sheet_title="September 2026 portfolio review", publication_profile=None)
            publication = current / "sheet-publish-result.json"
            publication.write_text(json.dumps({"schema_version": "sheet-publication-result/v1", "status": "published",
                "external_writes": True, "clockify_writes": 0, "publications": receipts}))
            document = {"schema_version": cycle.DERIVED_ADOPTION_SCHEMA_VERSION, "source": source, "replay": replay,
                "runtime_identity_digest": "fixture", "source_provenance": {}, "publication_result": str(publication),
                "publication_result_digest": cycle._digest(publication), "publication_receipts": receipts}
            fixture = root / "historical-adoption.json"
            fixture.write_text(json.dumps(document))
            original = fixture.read_bytes()
            with mock.patch.object(cycle, "_interval_from_derived_stage"), mock.patch.object(monthly, "project_rows", side_effect=AssertionError("legacy renderer invoked")):
                cycle._verify_historical_adoption(config, {}, json.loads(original), "2026-09-09", "2026-09-11", source, replay)
            self.assertEqual(original, fixture.read_bytes())
            self.assertEqual("unresolved-evidence", receipts[0]["sheet_title"])

    def test_legacy_exact_source_alias_with_meeting_link_keeps_receipt_and_human_cells(self):
        self.test_exact_source_alias_preserves_legacy_machine_cells_and_human_k(header=LEGACY_HEADER, meeting_link=True)

    def test_exact_source_alias_preserves_legacy_machine_cells_and_human_k(self, header=None, meeting_link=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            current, historical = root / "current", root / "historical"
            docs = frozen_run(current, 1, meeting_link=meeting_link)
            frozen_run(historical, 1, meeting_link=meeting_link)
            rows = monthly.project_rows(current)
            row = next(row for row in rows if row[3] == "low_confidence")
            layout = monthly.LEGACY_LAYOUT if header is not None else None
            old = monthly.rows_for_layout([row], layout)[0]
            if meeting_link:
                self.assertEqual("", old[7])
                self.assertIn("https://fathom.video/share/synthetic-link", old[9])
            old[6] = "Historical privacy-safe summary"
            old[10] = "legacy review"
            old[11] = "sha256:" + hashlib.sha256((historical / "ambiguous.json").read_bytes()).hexdigest()
            native = {"rows": {"spreadsheetId": "test", "sheets": [{"properties": {"sheetId": 7}, "data": [{"startRow": 1, "rowData": [{"values": [
                {"effectiveValue": {"stringValue": value}} for value in old]}]}]}]}}
            packet = {"source": str(current), "spreadsheet_id": "test", "sheet_title": "September 2026 unresolved evidence", "sheet_id": 7, "rows": rows, "records": [{"stable_evidence_id": row[0],
                "kind": row[3], "evidence_ids": docs["ambiguous.json"][0]["evidence_ids"], "source_item_id": "a-0"}]}
            (root / "native.json").write_text(json.dumps(native))
            (root / "packet.json").write_text(json.dumps(packet))
            binding = lambda path: {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            ids = docs["ambiguous.json"][0]["evidence_ids"]
            proof = {"schema_version": "clockify-unresolved-exact-source-alias-proof/v1", "source_alias_count": 1,
                "historical_source": binding(historical / "ambiguous.json"), "native_saved_read": binding(root / "native.json"),
                "current_canonical_packet": binding(root / "packet.json"), "proofs": [{"stable_evidence_id": row[0],
                "kind": row[3], "evidence_ids": ids, "native_row_1based": 2,
                "historical_exception_id": "a-0", "current_exception_id": "a-0", "native_provenance_digest": old[11]}]}
            path = root / "proof.json"
            path.write_text(json.dumps(proof))
            aliases = monthly.load_alias_proofs(path, current)
            (current / "routing.json").write_text("{}")
            other_rows = monthly.rows_for_layout([candidate for candidate in rows if candidate[0] != row[0]], layout)
            gateway = MonthlyGateway([header or monthly.HEADER, old, *other_rows])
            gateway.rows[1][10] = "changed by human"
            result = publisher.publish_monthly_unresolved(gateway, spreadsheet_id="test",
                sheet_title="September 2026 unresolved evidence", rows=rows, aliases=aliases)
            self.assertEqual(0, result["appended"])
            self.assertEqual([], gateway.writes)
            self.assertEqual("changed by human", gateway.rows[1][10])
            self.assertEqual(1, len(result["source_aliases"]))
            config = {"spreadsheet_id": "test", "monthly_unresolved_alias_proof": str(path)}
            source = {"run_dir": str(current), "run_id": current.name, "review_ids": [],
                **{key: "fixture" for key in ("result_digest", "bundle_digest", "artifact_digests", "snapshot_digests", "proposals_digest", "accounting_digest", "quality_digest")}}
            replay = {**source, "replay_integrity_digest": "fixture"}
            expected = cycle._expected_publication_receipts(config, source, sheet_title="September 2026 portfolio review")
            fields = ("spreadsheet_id", "sheet_title", "row_ids", "rows_sha256", "readback_id", "receipt_id", "source_aliases")
            publisher_document = {"schema_version": "sheet-publication-result/v1", "status": "published", "external_writes": True,
                "clockify_writes": 0, "publications": [result]}
            readbacks = cycle._validated_publication_document(publisher_document, expected, source_dir=current)
            if header is None:
                self.assertEqual(expected, [{field: result[field] for field in fields}])
            self.assertEqual(result["source_aliases"], readbacks[0]["source_aliases"])
            delivered = cycle._delivery_document(config, "2026-09-09", "2026-09-11", source, replay,
                sheet_title="September 2026 portfolio review", publication_readbacks=readbacks)
            self.assertEqual(monthly.ALIAS_PROFILE, delivered["publication_profile"])
            receipt = root / "delivery.json"
            receipt.write_text(json.dumps(delivered))
            original_receipt = receipt.read_bytes()
            cycle._verify_delivery_receipt(receipt, {**config, "monthly_unresolved_alias_proof": "/missing/new-config.json"},
                "2026-09-09", "2026-09-11", source, replay, sheet_title="September 2026 portfolio review")
            self.assertEqual(original_receipt, receipt.read_bytes())
            cycle._validated_publication_document(publisher_document, expected, source_dir=current)
            tampered = json.loads(json.dumps(publisher_document))
            tampered["publications"][0]["source_aliases"][0]["preserved_machine_digest"] = "wrong"
            with self.assertRaises(cycle.CycleError):
                cycle._validated_publication_document(tampered, expected, source_dir=current)
            gateway.rows[1][6] = "conflicting machine cells"
            with self.assertRaises(publisher.PublicationError):
                publisher.publish_monthly_unresolved(gateway, spreadsheet_id="test",
                    sheet_title="September 2026 unresolved evidence", rows=[row], aliases=aliases)
            self.assertEqual([], gateway.writes)
            proof["proofs"][0]["evidence_ids"] = ["wrong"]
            path.write_text(json.dumps(proof))
            with self.assertRaises(ValueError):
                monthly.load_alias_proofs(path, current)
            with self.assertRaises(cycle.CycleError):
                cycle._verify_delivery_receipt(receipt, config, "2026-09-09", "2026-09-11", source, replay,
                    sheet_title="September 2026 portfolio review")

    def test_real_cli_legacy_retry_preserves_target_readback_and_human_k(self):
        self.test_real_cli_and_cycle_share_receipts_and_frozen_retry_adds_zero(header=LEGACY_HEADER)

    def test_real_cli_and_cycle_share_receipts_and_frozen_retry_adds_zero(self, header=None):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            replay = Path(directory) / "replay"
            documents = frozen_run(source, 25)
            proposals = []
            for index, row in enumerate(documents["proposals.json"]):
                proposals.append({**proposal(index + 1), **row,
                    "client_project": "", "clockify_project_suffix": "", "tag_suffixes": [], "tag_names": [], "billable": False,
                    "duration_minutes": 5, "review_warnings": [{"type": "unresolved_routing", "disposition": "unresolved-routing", "reason_code": "no_deterministic_route"}]})
            documents["proposals.json"] = proposals
            documents["work-accounting-result.json"].update({"schema_version": 1,
                "allocation_mode": "non_overlapping_v1", "skipped": [], "proposals": proposals})
            for relative, doc in documents.items():
                (source / relative).write_text(json.dumps(doc))
            (source / "routing.json").write_text("{}")
            (source / "quality_report.json").write_text(json.dumps({"status": "pass", "summary": {"total_proposals": 2}}))
            provenance = {"source_run_id": source.name, "source_run_dir": str(source)}
            for relative, field in (("evidence/evidence-ledger.json", "ledger_file_sha256"),
                ("semantic-analysis.json", "semantic_analysis_sha256"), ("work-accounting-result.json", "work_accounting_result_sha256")):
                content = (source / relative).read_bytes()
                target = replay / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
                provenance[field] = hashlib.sha256(content).hexdigest()
            (replay / "replay-source.json").write_text(json.dumps(provenance))
            report = {"status": "pass", "failures": [], "source_run_id": source.name,
                "work_accounting_result": {"file_sha256": provenance["work_accounting_result_sha256"]},
                "reconciliation_binding": {"routing_sha256": "sha256:" + hashlib.sha256(b"{}").hexdigest()}}
            (replay / "replay-integrity.json").write_text(json.dumps(report))
            gateway = MonthlyGateway([header or monthly.HEADER, ["uev-exemplar"] + [""] * 11])
            result = Path(directory) / "publication.json"
            command = ["--spreadsheet-id", "test", "--sheet-title", "September 2026 portfolio review",
                "--proposals", str(source / "proposals.json"), "--quality-report", str(source / "quality_report.json"),
                "--replay-integrity", str(replay / "replay-integrity.json"), "--routing-snapshot", str(source / "routing.json"),
                "--run-id", source.name, "--result-output", str(result), "--monthly-unresolved", "--enable-write"]
            with mock.patch.object(publisher, "GwsSheetsGateway", return_value=gateway), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(0, publisher.main(command))
                first = json.loads(result.read_text())
                gateway.rows[-1][10] = "human reviewed"
                self.assertEqual(0, publisher.main(command))
            self.assertEqual(1, len(gateway.writes))
            self.assertEqual(27, len(gateway.writes[0][-1]))
            expected = cycle._expected_publication_receipts({"spreadsheet_id": "test"},
                {"run_dir": str(source), "run_id": source.name}, sheet_title="September 2026 portfolio review")
            verified = cycle._validated_publication_document(first, expected, source_dir=source)
            self.assertEqual(verified, first["publications"])
            if header is not None:
                self.assertEqual(LEGACY_HEADER, gateway.rows[0])
                self.assertTrue(all(row[7] == "" for row in gateway.rows[2:]))
                self.assertEqual(LEGACY_HEADER, verified[0]["monthly_target_readback"]["header"])
            self.assertEqual(first, json.loads(result.read_text()))
            self.assertEqual("human reviewed", gateway.rows[-1][10])
            # Accounting tamper is rejected before the gateway is even created.
            (source / "work-accounting-result.json").write_text("{}")
            with mock.patch.object(publisher, "GwsSheetsGateway") as factory, self.assertRaises(publisher.PublicationError):
                publisher.main(command)
            factory.assert_not_called()

    def test_private_descriptions_reasons_and_titles_are_not_exported(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docs = frozen_run(root, 1)
            docs["ambiguous.json"][0]["reason"] = "Private Person must review raw session text"
            docs["proposals.json"][0]["rendered_description"] = "Private Person attended"
            for relative in ("ambiguous.json", "proposals.json", "work-accounting-result.json"):
                (root / relative).write_text(json.dumps(docs[relative]))
            for row in monthly.project_rows(root):
                self.assertNotIn("Private Person", json.dumps(row))

    def test_unrouted_session_is_not_described_as_recorded_attendance(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            docs = frozen_run(root, 1)
            docs["proposals.json"][0]["source"] = "semantic_activity"
            for relative in ("proposals.json", "work-accounting-result.json"):
                (root / relative).write_text(json.dumps(docs[relative]))
            rows = monthly.project_rows(root)
            row = next(row for row in rows if row[3] == "routing_gap")
            self.assertNotIn("Recorded meeting attendance", row[6])
            self.assertNotIn("recording credit", row[9])

    def test_partition_publisher_plans_monthly_before_any_primary_writes(self):
        class Gateway(MultiSheetGateway):
            def __init__(self):
                super().__init__()
                self.sheets["September 2026 unresolved evidence"] = {
                    "sheet_id": 7, "rows": [monthly.HEADER, ["uev-exemplar"] + [""] * 11]}
                self.monthly_writes = []

            def spreadsheet(self, spreadsheet_id):
                metadata = super().spreadsheet(spreadsheet_id)
                for sheet in metadata["sheets"]:
                    sheet["properties"]["gridProperties"]["columnCount"] = 15
                return metadata

            def values(self, spreadsheet_id, range_name):
                if ":L" in range_name:
                    return [list(row) for row in self.sheets[self._title(range_name)]["rows"]]
                return super().values(spreadsheet_id, range_name)

            def append_monthly_rows(self, _id, sheet_id, start_row, grid_rows, rows):
                self.monthly_writes.append(rows)
                self.sheets["September 2026 unresolved evidence"]["rows"].extend([list(row) for row in rows])

        gateway = Gateway()
        row = ["uev-test"] + ["safe"] * 9 + ["needs_review", "sha256:test"]
        kwargs = dict(spreadsheet_id="test", sheet_title="September 2026 portfolio review",
                      template_title="Proposals", proposals=[proposal()], run_id="test", project_allowlist={}, monthly_rows=[row])
        gateway.sheets["September 2026 unresolved evidence"]["rows"][0] = ["wrong header"]
        with self.assertRaises(publisher.PublicationError):
            publisher.publish_proposal_partitions(gateway, **kwargs)
        self.assertEqual([], gateway.created)
        self.assertEqual([], gateway.appended)
        gateway.sheets["September 2026 unresolved evidence"]["rows"][0] = monthly.HEADER
        result = publisher.publish_proposal_partitions(gateway, **kwargs)
        self.assertEqual(["September 2026 portfolio review", "September 2026 unresolved evidence"],
                         [item["sheet_title"] for item in result["publications"]])
        self.assertNotIn("unresolved-evidence", gateway.sheets)
        gateway.sheets["September 2026 unresolved evidence"]["rows"][-1][10] = "human decision"
        repeated = publisher.publish_proposal_partitions(gateway, **kwargs)
        self.assertEqual(0, repeated["publications"][1]["appended"])
        self.assertEqual(result["publications"][1]["receipt_id"], repeated["publications"][1]["receipt_id"])

    def test_collector_local_slice_labels_use_ledger_timezone(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frozen_run(root, 1)
            (root / "run-report.json").write_text(json.dumps({"date_range": {
                "since": "2026-09-09 00:00", "until": "2026-09-11 00:00"}}))
            self.assertEqual(3, len(monthly.project_rows(root)))

    def test_frozen_projection_27_rows_stable_ids_and_context_dates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            documents = frozen_run(root)
            rows = monthly.project_rows(root)
            self.assertEqual(27, len(rows))
            self.assertTrue(all(len(row) == 12 for row in rows))
            self.assertEqual(2, sum(row[3] == "routing_gap" for row in rows))
            identity = {"kind": "low_confidence", "evidence_ids": documents["ambiguous.json"][0]["evidence_ids"]}
            stable = "uev-" + hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:24]
            row = next(row for row in rows if row[0] == stable)
            self.assertIn("outside slice", row[1])
            self.assertIn("2026-09-08", row[1])
            self.assertTrue(all(row[10] == "needs_review" for row in rows))
            self.assertEqual(rows, monthly.project_rows(root))

    def test_missing_ledger_reference_and_conflicting_identity_fail(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            documents = frozen_run(root, 1)
            documents["work-accounting-result.json"]["ambiguous"][0]["evidence_ids"] = ["missing"]
            for name in ("work-accounting-result.json", "ambiguous.json"):
                (root / name).write_text(json.dumps(documents[name]))
            with self.assertRaisesRegex(ValueError, "evidence"):
                monthly.project_rows(root)

    def test_append_retry_preserves_human_k_and_conflict_is_prewrite(self):
        row = ["uev-test", "2026-09-09", "test", "low_confidence", "unknown interval", "", "summary", "", "quality pass", "review", "needs_review", "sha256:test"]
        gateway = MonthlyGateway([monthly.HEADER, ["uev-exemplar"] + [""] * 11])
        kwargs = dict(spreadsheet_id="test", sheet_title="September 2026 unresolved evidence", rows=[row])
        self.assertEqual(1, publisher.publish_monthly_unresolved(gateway, **kwargs)["appended"])
        gateway.rows[2][10] = "confirmed by human"
        self.assertEqual(0, publisher.publish_monthly_unresolved(gateway, **kwargs)["appended"])
        self.assertEqual("confirmed by human", gateway.rows[2][10])
        gateway.rows[2][6] = "machine drift"
        writes = len(gateway.writes)
        with self.assertRaisesRegex(publisher.PublicationError, "conflict"):
            publisher.publish_monthly_unresolved(gateway, **kwargs)
        self.assertEqual(writes, len(gateway.writes))

    def test_wrong_header_and_duplicate_existing_ids_fail_before_writes(self):
        for existing in ([["wrong header"]], [monthly.HEADER, ["uev-dupe"], ["uev-dupe"]]):
            gateway = MonthlyGateway(existing)
            with self.assertRaises(publisher.PublicationError):
                publisher.publish_monthly_unresolved(gateway, spreadsheet_id="test",
                    sheet_title="September 2026 unresolved evidence", rows=[["uev-new"] + [""] * 11])
            self.assertEqual([], gateway.writes)

    def test_native_adapter_formats_only_new_rows(self):
        with mock.patch.object(publisher.GwsSheetsGateway, "_call", return_value={}) as call:
            publisher.GwsSheetsGateway().append_monthly_rows("test", 7, 390, 389, [["uev-new"] + [""] * 11])
        requests = json.loads(call.call_args.args[0][-1])["requests"]
        self.assertEqual("ROWS", requests[0]["appendDimension"]["dimension"])
        self.assertEqual(["PASTE_FORMAT", "PASTE_DATA_VALIDATION"], [r["copyPaste"]["pasteType"] for r in requests[1:3]])
        self.assertEqual(389, requests[1]["copyPaste"]["destination"]["startRowIndex"])
        self.assertEqual({"repeatCell": {"range": requests[1]["copyPaste"]["destination"],
            "cell": {}, "fields": "userEnteredFormat.textFormat.link"}}, requests[-2])
        self.assertEqual("userEnteredValue", requests[-1]["updateCells"]["fields"])

    def test_native_gateway_simulation_does_not_inherit_exemplar_hyperlinks(self):
        exemplar = {"format": {"wrapStrategy": "WRAP", "backgroundColor": {"red": 1},
            "textFormat": {"bold": True, "link": {"uri": "https://fathom.video/share/old"}}},
            "validation": {"condition": {"type": "ONE_OF_LIST", "values": [{"userEnteredValue": "needs_review"}]}}}
        cells = {}

        def native_call(arguments):
            requests = json.loads(arguments[-1])["requests"]
            for request in requests:
                if "copyPaste" in request:
                    operation = request["copyPaste"]
                    destination = operation["destination"]
                    field = "format" if operation["pasteType"] == "PASTE_FORMAT" else "validation"
                    for row in range(destination["startRowIndex"], destination["endRowIndex"]):
                        for column in range(12):
                            cells.setdefault((row, column), {})[field] = copy.deepcopy(exemplar[field])
                elif "repeatCell" in request:
                    operation = request["repeatCell"]
                    self.assertEqual("userEnteredFormat.textFormat.link", operation["fields"])
                    destination = operation["range"]
                    for row in range(destination["startRowIndex"], destination["endRowIndex"]):
                        for column in range(12):
                            cells[row, column]["format"]["textFormat"].pop("link", None)
                elif "updateCells" in request:
                    operation = request["updateCells"]
                    for offset, row in enumerate(operation["rows"]):
                        for column, value in enumerate(row["values"]):
                            cell = cells[operation["range"]["startRowIndex"] + offset, column]
                            cell["value"] = value["userEnteredValue"]["stringValue"]
                            if cell["value"].startswith("https://"):
                                cell["format"]["textFormat"]["link"] = {"uri": cell["value"]}
            return {}

        rows = [["uev-new-1"] + [""] * 11, ["uev-new-2"] + [""] * 11]
        rows[1][7] = "https://fathom.video/share/new"
        with mock.patch.object(publisher.GwsSheetsGateway, "_call", side_effect=native_call):
            publisher.GwsSheetsGateway().append_monthly_rows("test", 7, 390, 389, rows)
        self.assertNotIn("link", cells[389, 7]["format"]["textFormat"])
        self.assertEqual({"uri": rows[1][7]}, cells[390, 7]["format"]["textFormat"]["link"])
        self.assertEqual("WRAP", cells[389, 7]["format"]["wrapStrategy"])
        self.assertTrue(cells[389, 7]["format"]["textFormat"]["bold"])
        self.assertEqual(exemplar["validation"], cells[389, 10]["validation"])
        self.assertEqual("https://fathom.video/share/old", exemplar["format"]["textFormat"]["link"]["uri"])
