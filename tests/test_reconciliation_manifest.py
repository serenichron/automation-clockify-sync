from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from zoneinfo import ZoneInfo

from scripts.reconciliation_manifest import (
    ArtifactIdentity,
    CoordinatorEventStore,
    ManifestError,
    PeriodIdentity,
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

        store = CoordinatorEventStore(self.path)
        self.path.unlink()
        store.append(self.identity, "period_opened", {"revision": 1}, occurred_at=LATER)
        store.append(self.identity, "slice_completed", {"slice_id": "s1"}, occurred_at=NOW)
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


if __name__ == "__main__":
    unittest.main()
