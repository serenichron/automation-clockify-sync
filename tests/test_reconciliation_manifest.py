from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from zoneinfo import ZoneInfo

from scripts.reconciliation_manifest import (
    ArtifactIdentity,
    CoordinatorEventStore,
    ManifestError,
    PeriodIdentity,
    ReconciliationCoordinator,
    ReconciliationManifest,
)


BUCHAREST = ZoneInfo("Europe/Bucharest")
NOW = datetime(2026, 8, 16, 9, 0, tzinfo=timezone.utc)
LATER = NOW + timedelta(minutes=1)


def august_identity() -> PeriodIdentity:
    return PeriodIdentity(
        member_id="member-vlad",
        workspace_id="workspace-serenichron",
        timezone="Europe/Bucharest",
        since=datetime(2026, 8, 1, 0, 0, tzinfo=BUCHAREST),
        until=datetime(2026, 8, 16, 0, 0, tzinfo=BUCHAREST),
        revision=1,
    )


def read_jsonl(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def write_jsonl(path: Path, documents: list[dict[str, object]]) -> None:
    path.write_text(
        "".join(json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n" for document in documents),
        encoding="utf-8",
    )


def event_schema_validation(document: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["jsonschema", str(Path(__file__).parents[1] / "schemas/reconciliation-event-v1.json")],
        input=json.dumps(document), text=True, capture_output=True, check=False,
    )


def manifest_schema_validation(document: dict[str, object]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["jsonschema", str(Path(__file__).parents[1] / "schemas/reconciliation-manifest-v1.json")],
        input=json.dumps(document), text=True, capture_output=True, check=False,
    )


def schema_event(payload: dict[str, object]) -> dict[str, object]:
    return {
        "sequence": 1,
        "period_id": "rperiod-" + "a" * 64,
        "event_type": "slice_completed",
        "payload": payload,
        "previous_digest": "sha256:" + "0" * 64,
        "occurred_at": "2026-08-16T00:00:00Z",
        "event_digest": "sha256:" + "a" * 64,
    }


def event_digest(document: dict[str, object]) -> str:
    unsigned = {
        key: document[key]
        for key in ("sequence", "period_id", "event_type", "payload", "previous_digest", "occurred_at")
    }
    canonical = json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def artifact_document(path: Path) -> dict[str, object]:
    return ArtifactIdentity(
        path=path.resolve(),
        schema_version="synthetic-artifact/v1",
        compatibility_version="synthetic/v1",
        digest="sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(),
    ).document()


def append_events(
    store: CoordinatorEventStore,
    identity: PeriodIdentity,
    event_types: list[str],
    artifact: Path | None = None,
) -> None:
    for sequence, event_type in enumerate(event_types):
        payload: dict[str, object] = {}
        if artifact is not None:
            payload["artifacts"] = [artifact_document(artifact)]
        if event_type == "publication_authorized":
            payload.update({"contract_digest": "sha256:" + "a" * 64, "idempotency_identity": "publication-1"})
        elif event_type == "shared_report_verified":
            payload.update({
                "contract_digest": "sha256:" + "a" * 64, "idempotency_identity": "publication-1",
                "report_receipt": {"contract_digest": "sha256:" + "a" * 64, "idempotency_identity": "publication-1", "status": "verified"},
            })
        elif event_type == "publication_complete":
            payload.update({
                "contract_digest": "sha256:" + "a" * 64,
                "idempotency_identity": "publication-1",
                "slack_receipt": {"contract_digest": "sha256:" + "a" * 64, "idempotency_identity": "publication-1", "status": "verified"},
            })
        store.append(identity, event_type, payload, occurred_at=NOW + timedelta(minutes=sequence))


def complete_event_types() -> list[str]:
    return [
        "period_opened", "collection_complete", "reconciliation_complete",
    ]


class PeriodIdentityTests(unittest.TestCase):
    def test_artifact_identity_exposes_only_safe_immutable_metadata(self) -> None:
        artifact = ArtifactIdentity(
            path=Path("/private/recovery/evidence-ledger.json"),
            schema_version="evidence-ledger/v1",
            compatibility_version="collector/v2",
            digest="sha256:" + "a" * 64,
        )

        self.assertEqual(
            {
                "path": "/private/recovery/evidence-ledger.json",
                "schema_version": "evidence-ledger/v1",
                "compatibility_version": "collector/v2",
                "digest": "sha256:" + "a" * 64,
            },
            artifact.document(),
        )

    def test_bucharest_august_period_is_half_open_and_deterministic(self) -> None:
        identity = august_identity()

        self.assertEqual("2026-07-31T21:00:00Z", identity.document()["since_utc"])
        self.assertEqual("2026-08-15T21:00:00Z", identity.document()["until_utc"])
        self.assertEqual(identity.period_id, august_identity().period_id)

    def test_rejects_naive_or_reversed_period_intervals(self) -> None:
        for since, until in (
            (datetime(2026, 8, 1), datetime(2026, 8, 16, tzinfo=BUCHAREST)),
            (datetime(2026, 8, 16, tzinfo=BUCHAREST), datetime(2026, 8, 16, tzinfo=BUCHAREST)),
            (datetime(2026, 8, 17, tzinfo=BUCHAREST), datetime(2026, 8, 16, tzinfo=BUCHAREST)),
        ):
            with self.subTest(since=since, until=until), self.assertRaises(ManifestError):
                PeriodIdentity(
                    member_id="member-vlad",
                    workspace_id="workspace-serenichron",
                    timezone="Europe/Bucharest",
                    since=since,
                    until=until,
                    revision=1,
                )

    def test_rejects_invalid_revision_and_timezone(self) -> None:
        for revision, timezone_name in ((0, "Europe/Bucharest"), (-1, "Europe/Bucharest"), (1, "Mars/Olympus")):
            with self.subTest(revision=revision, timezone=timezone_name), self.assertRaises(ManifestError):
                PeriodIdentity(
                    member_id="member-vlad",
                    workspace_id="workspace-serenichron",
                    timezone=timezone_name,
                    since=datetime(2026, 8, 1, 0, 0, tzinfo=BUCHAREST),
                    until=datetime(2026, 8, 16, 0, 0, tzinfo=BUCHAREST),
                    revision=revision,
                )


class CoordinatorEventStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.path = Path(self.temporary_directory.name) / "coordinator-events.jsonl"
        self.identity = august_identity()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_append_chains_events_and_loads_verified_history(self) -> None:
        store = CoordinatorEventStore(self.path)
        first = store.append(self.identity, "period_opened", {"revision": 1}, occurred_at=NOW)
        second = store.append(self.identity, "slice_completed", {"slice_id": "s1"}, occurred_at=LATER)

        self.assertEqual("sha256:" + "0" * 64, first.previous_digest)
        self.assertEqual(first.event_digest, second.previous_digest)
        self.assertEqual((first, second), store.load(self.identity))
        self.assertEqual((first, second), store.verify(self.identity))

    def test_verify_rejects_reordered_events(self) -> None:
        store = CoordinatorEventStore(self.path)
        store.append(self.identity, "period_opened", {"revision": 1}, occurred_at=NOW)
        store.append(self.identity, "slice_completed", {"slice_id": "s1"}, occurred_at=LATER)
        documents = read_jsonl(self.path)
        write_jsonl(self.path, list(reversed(documents)))

        with self.assertRaises(ManifestError):
            store.verify(self.identity)

    def test_verify_rejects_wrong_period_and_duplicate_sequence(self) -> None:
        store = CoordinatorEventStore(self.path)
        store.append(self.identity, "period_opened", {"revision": 1}, occurred_at=NOW)
        document = read_jsonl(self.path)[0]
        wrong_period = {**document, "period_id": "rperiod-other"}
        write_jsonl(self.path, [wrong_period])
        with self.assertRaises(ManifestError):
            store.verify(self.identity)

        self.path.unlink()
        store.append(self.identity, "period_opened", {"revision": 1}, occurred_at=NOW)
        documents = read_jsonl(self.path)
        write_jsonl(self.path, [documents[0], documents[0]])
        with self.assertRaises(ManifestError):
            store.verify(self.identity)

    def test_verify_rejects_blank_or_truncated_line(self) -> None:
        store = CoordinatorEventStore(self.path)
        store.append(self.identity, "period_opened", {"revision": 1}, occurred_at=NOW)
        original = self.path.read_text(encoding="utf-8")
        for suffix in ("\n", '{"sequence":'):
            with self.subTest(suffix=suffix):
                self.path.write_text(original + suffix, encoding="utf-8")
                with self.assertRaises(ManifestError):
                    store.verify(self.identity)
                self.path.write_text(original, encoding="utf-8")

    def test_verify_rejects_mutated_payload_and_non_monotonic_timestamp(self) -> None:
        store = CoordinatorEventStore(self.path)
        store.append(self.identity, "period_opened", {"revision": 1}, occurred_at=NOW)
        store.append(self.identity, "slice_completed", {"slice_id": "s1"}, occurred_at=LATER)
        documents = read_jsonl(self.path)
        documents[1]["payload"] = {"slice_id": "changed"}
        write_jsonl(self.path, documents)
        with self.assertRaises(ManifestError):
            store.verify(self.identity)

        self.path.unlink()
        store.append(self.identity, "period_opened", {"revision": 1}, occurred_at=NOW)
        store.append(self.identity, "slice_completed", {"slice_id": "s1"}, occurred_at=LATER)
        documents = read_jsonl(self.path)
        documents[1]["occurred_at"] = "2026-08-16T08:59:00Z"
        documents[1]["event_digest"] = event_digest(documents[1])
        write_jsonl(self.path, documents)
        with self.assertRaises(ManifestError):
            store.verify(self.identity)

    def test_append_rejects_private_evidence_payloads(self) -> None:
        store = CoordinatorEventStore(self.path)

        with self.assertRaises(ManifestError):
            store.append(
                self.identity,
                "slice_completed",
                {"transcript": "private client evidence"},
                occurred_at=NOW,
            )

    def test_append_rejects_non_json_payload_values(self) -> None:
        store = CoordinatorEventStore(self.path)

        with self.assertRaises(ManifestError):
            store.append(
                self.identity,
                "slice_completed",
                {"confidence": float("nan")},
                occurred_at=NOW,
            )

    def test_append_rejects_timestamp_before_head_without_writing(self) -> None:
        store = CoordinatorEventStore(self.path)
        store.append(self.identity, "period_opened", {"revision": 1}, occurred_at=LATER)
        before = self.path.read_bytes()

        with self.assertRaises(ManifestError):
            store.append(self.identity, "slice_completed", {"slice_id": "s1"}, occurred_at=NOW)

        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(1, len(store.verify(self.identity)))


class ReconciliationEventSchemaTests(unittest.TestCase):
    def test_schema_rejects_uppercase_or_mixed_case_private_payload_keys_recursively(self) -> None:
        for key in ("TRANSCRIPT", "Api_Key"):
            with self.subTest(key=key):
                result = event_schema_validation(schema_event({"nested": {key: "private"}}))
                self.assertNotEqual(0, result.returncode, result.stderr)

    def test_schema_accepts_safe_nested_payload(self) -> None:
        document = schema_event({"artifact": {"digest": "sha256:" + "a" * 64}})
        document["event_type"] = "period_opened"
        result = event_schema_validation(document)

        self.assertEqual(0, result.returncode, result.stderr)

    def test_schema_covers_all_legal_events_and_rejects_unknown_type(self) -> None:
        legal_events = (
            "period_opened", "collection_complete", "reconciliation_complete", "review_approved",
            "posting_started", "posting_complete", "clockify_readback_verified",
            "publication_prepared", "publication_authorized", "shared_report_verified",
            "publication_complete", "coverage_incomplete", "semantic_exceptions", "awaiting_approval",
            "post_interrupted", "readback_mismatch", "report_mismatch", "currency_quote_unavailable",
            "publication_deferred", "report_residual_resolved", "fathom_repair_complete",
            "coverage_limitation_approved",
        )
        for event_type in legal_events:
            with self.subTest(event_type=event_type):
                document = schema_event({})
                document["event_type"] = event_type
                result = event_schema_validation(document)
                self.assertEqual(0, result.returncode, result.stderr)

        document = schema_event({})
        document["event_type"] = "slice_completed"
        result = event_schema_validation(document)
        self.assertNotEqual(0, result.returncode, result.stderr)



class ReconciliationCoordinatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.identity = august_identity()
        self.store = CoordinatorEventStore(self.root / "period-events.jsonl")
        self.artifact = self.root / "review.json"
        self.artifact.write_text('{"status":"complete"}\n', encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def coordinator(self) -> ReconciliationCoordinator:
        return ReconciliationCoordinator(self.identity, self.store)

    def test_complete_verified_events_derive_awaiting_review(self) -> None:
        append_events(self.store, self.identity, complete_event_types(), self.artifact)

        manifest = self.coordinator().derive()

        self.assertEqual("awaiting_review", manifest.state)
        self.assertTrue(manifest.manifest_digest.startswith("sha256:"))
        self.assertEqual((artifact_document(self.artifact),), manifest.artifacts)

    def test_referenced_artifact_drift_blocks_state_advance(self) -> None:
        append_events(self.store, self.identity, complete_event_types(), self.artifact)
        self.artifact.write_text("changed\n", encoding="utf-8")

        with self.assertRaisesRegex(ManifestError, "artifact digest mismatch"):
            self.coordinator().derive()

    def test_rejects_illegal_and_duplicate_advancing_transitions(self) -> None:
        for event_types in (
            ["period_opened", "posting_started"],
            ["period_opened", "collection_complete", "reconciliation_complete", "review_approved", "posting_started", "posting_complete", "publication_prepared"],
            ["period_opened", "collection_complete", "reconciliation_complete", "review_approved", "posting_started", "posting_complete", "clockify_readback_verified", "publication_prepared", "publication_authorized", "shared_report_verified", "publication_complete", "publication_complete"],
            ["period_opened", "period_opened"],
        ):
            with self.subTest(event_types=event_types):
                path = self.root / f"{len(event_types)}-{event_types[-1]}.jsonl"
                store = CoordinatorEventStore(path)
                append_events(store, self.identity, event_types, self.artifact)
                with self.assertRaises(ManifestError):
                    ReconciliationCoordinator(self.identity, store).derive()

    def test_authorization_and_publication_require_bound_receipts(self) -> None:
        cases = (
            ["period_opened", "collection_complete", "reconciliation_complete", "review_approved", "posting_started", "posting_complete", "clockify_readback_verified", "publication_prepared", "publication_authorized", "shared_report_verified", "publication_complete"],
            ["period_opened", "collection_complete", "reconciliation_complete", "review_approved", "posting_started", "posting_complete", "clockify_readback_verified", "publication_prepared", "publication_authorized", "publication_complete"],
        )
        for index, event_types in enumerate(cases):
            with self.subTest(event_types=event_types):
                store = CoordinatorEventStore(self.root / f"publication-{index}.jsonl")
                append_events(store, self.identity, event_types, self.artifact)
                if index == 0:
                    self.assertEqual("published", ReconciliationCoordinator(self.identity, store).derive().state)
                else:
                    with self.assertRaises(ManifestError):
                        ReconciliationCoordinator(self.identity, store).derive()

    def test_rejects_receipt_with_a_different_publication_binding(self) -> None:
        event_types = [
            "period_opened", "collection_complete", "reconciliation_complete", "review_approved",
            "posting_started", "posting_complete", "clockify_readback_verified", "publication_prepared",
            "publication_authorized", "shared_report_verified",
        ]
        append_events(self.store, self.identity, event_types, self.artifact)
        documents = read_jsonl(self.store.path)
        documents[-1]["payload"]["report_receipt"]["idempotency_identity"] = "other-publication"
        documents[-1]["event_digest"] = event_digest(documents[-1])
        write_jsonl(self.store.path, documents)

        with self.assertRaisesRegex(ManifestError, "shared report receipt"):
            self.coordinator().derive()

    def test_blockers_and_audits_are_recorded_without_advancing(self) -> None:
        event_types = [
            "period_opened", "coverage_incomplete", "semantic_exceptions", "awaiting_approval",
            "post_interrupted", "readback_mismatch", "report_mismatch", "currency_quote_unavailable",
            "publication_deferred", "report_residual_resolved", "fathom_repair_complete",
            "coverage_limitation_approved",
        ]
        append_events(self.store, self.identity, event_types, self.artifact)

        manifest = self.coordinator().derive()

        self.assertEqual("collecting", manifest.state)
        self.assertEqual((
            "awaiting_approval", "coverage_incomplete", "currency_quote_unavailable", "post_interrupted",
            "publication_deferred", "readback_mismatch", "report_mismatch", "semantic_exceptions",
        ), manifest.blockers)

    def test_rejects_unknown_event_type_and_private_payload_at_derivation(self) -> None:
        self.store.append(self.identity, "period_opened", {}, occurred_at=NOW)
        self.store.append(self.identity, "unknown", {}, occurred_at=LATER)
        with self.assertRaisesRegex(ManifestError, "unknown reconciliation event"):
            self.coordinator().derive()

        for private_field in ("transcript", "raw_payload", "cursor", "api_key", "credential"):
            with self.subTest(private_field=private_field):
                private_store = CoordinatorEventStore(self.root / f"{private_field}.jsonl")
                with self.assertRaisesRegex(ManifestError, "prohibited private field"):
                    private_store.append(self.identity, "period_opened", {private_field: "secret"}, occurred_at=NOW)

    def test_manifest_serialization_is_schema_valid_and_digest_bound(self) -> None:
        append_events(self.store, self.identity, complete_event_types(), self.artifact)
        manifest = self.coordinator().derive()
        document = manifest.document()

        self.assertEqual(0, manifest_schema_validation(document).returncode)
        tampered = {**document, "state": "approved"}
        with self.assertRaisesRegex(ManifestError, "manifest digest"):
            ReconciliationManifest.from_document(tampered)


class ReconciliationManifestCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "recovery"
        self.events = self.root / "period-events.jsonl"
        self.manifest = self.root / "period-manifest.json"
        self.routing = self.root.parent / "routing.json"
        self.routing.write_text(json.dumps({"workspace_id": "workspace-serenichron", "member_id": "member-vlad"}), encoding="utf-8")
        self.script = Path(__file__).parents[1] / "scripts" / "reconciliation_manifest.py"

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["python3", str(self.script), *arguments], text=True, capture_output=True, check=False,
        )

    def init_arguments(self, *, dry_run: bool = False) -> list[str]:
        arguments = [
            "init", "--workspace-id-from-routing", str(self.routing), "--member-id-from-routing", str(self.routing),
            "--timezone", "Europe/Bucharest", "--since", "2026-08-01T00:00:00+03:00",
            "--until", "2026-08-16T00:00:00+03:00", "--revision", "1", "--events", str(self.events),
            "--manifest", str(self.manifest),
        ]
        return [*arguments, "--dry-run"] if dry_run else arguments

    def test_init_dry_run_leaves_no_files_and_init_writes_collecting_manifest(self) -> None:
        result = self.run_cli(*self.init_arguments(dry_run=True))
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertFalse(self.root.exists())

        result = self.run_cli(*self.init_arguments())
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(self.events.exists())
        self.assertEqual("collecting", json.loads(self.manifest.read_text(encoding="utf-8"))["state"])

    def test_import_artifacts_stores_safe_identities_and_verify_detects_drift(self) -> None:
        self.assertEqual(0, self.run_cli(*self.init_arguments()).returncode)
        artifact = self.root / "review.json"
        artifact.write_text('{"safe":true}\n', encoding="utf-8")
        diagnostic = self.root.parent / "diagnostic.json"
        diagnostic.write_text(json.dumps({"artifacts": [{
            "kind": "quality_report", "path": str(artifact), "schema_version": "quality/v1",
            "compatibility_version": "quality/v1",
        }]}), encoding="utf-8")
        inventory = self.root / "imported-artifacts.json"

        result = self.run_cli("import-artifacts", "--events", str(self.events), "--manifest", str(self.manifest),
                              "--diagnostic", str(diagnostic), "--discover-preserved-august", "--output", str(inventory))
        self.assertEqual(0, result.returncode, result.stderr)
        imported = json.loads(inventory.read_text(encoding="utf-8"))
        self.assertEqual({"artifacts"}, set(imported))
        self.assertEqual({"kind", "path", "schema_version", "compatibility_version", "digest"}, set(imported["artifacts"][0]))

        result = self.run_cli("verify", "--events", str(self.events), "--manifest", str(self.manifest))
        self.assertEqual(0, result.returncode, result.stderr)
        artifact.write_text('{"safe":false}\n', encoding="utf-8")
        result = self.run_cli("verify", "--events", str(self.events), "--manifest", str(self.manifest))
        self.assertNotEqual(0, result.returncode)
        self.assertIn("artifact digest mismatch", result.stderr)

    def test_import_rejects_a_manifest_stale_against_the_event_history(self) -> None:
        self.assertEqual(0, self.run_cli(*self.init_arguments()).returncode)
        identity = ReconciliationManifest.from_document(json.loads(self.manifest.read_text(encoding="utf-8"))).identity
        CoordinatorEventStore(self.events).append(
            identity, "coverage_incomplete", {}, occurred_at=datetime.now(timezone.utc),
        )
        diagnostic = self.root.parent / "diagnostic.json"
        diagnostic.write_text(json.dumps({"artifacts": []}), encoding="utf-8")

        result = self.run_cli("import-artifacts", "--events", str(self.events), "--manifest", str(self.manifest),
                              "--diagnostic", str(diagnostic), "--output", str(self.root / "imported-artifacts.json"))

        self.assertNotEqual(0, result.returncode)
        self.assertIn("manifest does not match verified event history", result.stderr)

    def test_rejects_output_paths_outside_the_private_recovery_root(self) -> None:
        outside_events = self.root.parent / "period-events.jsonl"
        result = self.run_cli(
            "init", "--workspace-id-from-routing", str(self.routing), "--member-id-from-routing", str(self.routing),
            "--timezone", "Europe/Bucharest", "--since", "2026-08-01T00:00:00+03:00",
            "--until", "2026-08-16T00:00:00+03:00", "--revision", "1", "--events", str(outside_events),
            "--manifest", str(self.manifest),
        )
        self.assertNotEqual(0, result.returncode)
        self.assertIn("private recovery root", result.stderr)


if __name__ == "__main__":
    unittest.main()
