from __future__ import annotations

import copy
import datetime as dt
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import collector_checkpoints as checkpoints
from scripts import clockify_sync_collect as collector

try:
    snapshot = importlib.import_module("scripts.clockify_checkpoint_snapshot")
except ModuleNotFoundError:
    snapshot = None


SINCE = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
UNTIL = dt.datetime(2026, 10, 1, tzinfo=dt.timezone.utc)
OBSERVED = "2026-10-02T12:00:00Z"
ENTRY = {
    "id": "0123456789abcdef01234567",
    "workspaceId": "workspace-one",
    "userId": "user-one",
    "description": "Existing work",
    "projectId": "project-123456",
    "tagIds": ["tag-01234567"],
    "taskId": None,
    "billable": False,
    "timeInterval": {
        "start": "2026-09-10T09:00:00Z",
        "end": "2026-09-10T10:00:00Z",
        "duration": "PT1H",
    },
}
EVIDENCE = {
    "status": "ok",
    "entries": [{
        "id_suffix": "01234567", "description": "Existing work",
        "project_id_suffix": "123456", "tag_id_suffixes": ["01234567"],
        "start": "2026-09-10 12:00", "end": "2026-09-10 13:00",
        "running": False, "running_snapshot": None, "duration": "PT1H",
        "billable": False,
    }],
    "pages_fetched": 1, "running_entry_count": 0,
    "running_entry_snapshot_count": 0,
    "collection_snapshot": {
        "observed_at": OBSERVED, "boundary": "2026-10-01T00:00:00Z",
        "requested_until": "2026-10-01T00:00:00Z",
    },
    "complete": True,
}


class ClockifyCheckpointSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(snapshot, "immutable checkpoint transport is missing")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = checkpoints.PageCheckpointStore(self.root / "source-cache")
        self.identity = collector._clockify_checkpoint_identity(
            "workspace-one", "user-one", SINCE, UNTIL
        )
        self.source_evidence = self.root / "source-run/evidence/clockify-existing.json"
        self.source_evidence.parent.mkdir(parents=True)
        self.source_evidence.write_text(json.dumps(EVIDENCE, indent=2) + "\n")
        self.destination = self.root / "fresh-run/evidence/clockify-native-checkpoint"

    def checkpoint(self, pages=None, *, complete=True, continuation=None, metadata=None):
        state = self.store.open(self.identity, initial_metadata=metadata or {"snapshot_at": OBSERVED})
        for number, entries in enumerate(pages or [[copy.deepcopy(ENTRY)]], 1):
            state = self.store.append_page(
                state, payload=entries,
                continuation=continuation if continuation is not None else {"page": number + 1},
                signature=collector._clockify_page_signature(entries),
            )
        if complete:
            state = self.store.mark_complete(state)
        return state.directory / "manifest.json"

    def capture(self, manifest=None, **overrides):
        arguments = dict(
            checkpoint_manifest=manifest or self.checkpoint(),
            clockify_evidence=self.source_evidence,
            destination=self.destination, workspace_id="workspace-one", user_id="user-one",
            since=SINCE, until=UNTIL,
        )
        arguments.update(overrides)
        return snapshot.capture_checkpoint_snapshot(**arguments)

    def load(self, captured, **overrides):
        arguments = dict(workspace_id="workspace-one", user_id="user-one", since=SINCE, until=UNTIL,
                         expected_manifest_sha256=captured.manifest_sha256)
        arguments.update(overrides)
        return snapshot.load_checkpoint_snapshot(self.destination, **arguments)

    def test_capture_preserves_full_native_bytes_and_uses_existing_projection_without_network(self):
        manifest = self.checkpoint()
        original = {"manifest.json": manifest.read_bytes(),
                    "pages/000001.json": (manifest.parent / "pages/000001.json").read_bytes()}
        with patch.object(collector, "clockify_get", side_effect=AssertionError("network forbidden")):
            captured = self.capture(manifest)
            loaded = self.load(captured)
        self.assertEqual([ENTRY], loaded.entries)
        self.assertEqual(OBSERVED, loaded.manifest["snapshot_at"])
        self.assertEqual(1, loaded.manifest["entry_count"])
        self.assertEqual(hashlib.sha256((self.destination / "snapshot.json").read_bytes()).hexdigest(),
                         captured.manifest_sha256)
        copied = self.destination / "checkpoint" / self.identity_directory_name()
        for relative, raw in original.items():
            self.assertEqual(raw, (copied / relative).read_bytes())
            self.assertEqual(raw, (manifest.parent / relative).read_bytes())
        self.assertEqual(self.source_evidence.read_bytes(), (self.destination / "clockify-existing.json").read_bytes())

    def identity_directory_name(self):
        return self.store._directory_for(self.identity).name

    def stopped_observation_variance(self, *, entry=None, observed="2026-10-01T12:00:00.625Z"):
        entry = copy.deepcopy(entry or ENTRY)
        entry["timeInterval"] = {
            "start": "2026-09-10T09:00:00Z", "end": "2026-09-10T09:50:38Z",
            "duration": "PT50M38S",
        }
        evidence = copy.deepcopy(EVIDENCE)
        evidence["entries"][0].update(end="2026-09-10 12:50", duration="PT50M38S")
        self.source_evidence.write_text(json.dumps(evidence, indent=2) + "\n")
        return self.checkpoint([[entry]], metadata={"snapshot_at": observed}), entry

    def test_stopped_observation_variance_requires_opt_in_and_replays_original_bytes(self):
        manifest, entry = self.stopped_observation_variance()
        original = {"manifest.json": manifest.read_bytes(),
                    "pages/000001.json": (manifest.parent / "pages/000001.json").read_bytes(),
                    "evidence": self.source_evidence.read_bytes()}
        with self.assertRaisesRegex(ValueError, "projection"):
            self.capture(manifest)
        self.assertFalse(self.destination.exists())
        with patch.object(collector, "clockify_get", side_effect=AssertionError("network forbidden")):
            try:
                captured = self.capture(manifest, allow_stopped_only_legacy_observation_variance=True)
            except (TypeError, ValueError) as error:
                self.fail(f"explicit stopped-only observation compatibility must capture: {error}")
            manifest.write_text("source no longer available")
            self.source_evidence.write_text("source no longer available")
            loaded = self.load(captured)
        self.assertEqual([entry], loaded.entries)
        self.assertEqual("2026-10-01T12:00:00.625000Z", loaded.manifest["snapshot_at"])
        self.assertEqual("clockify-native-checkpoint-snapshot/v2", loaded.manifest["schema_version"])
        self.assertEqual({
            "mode": "stopped-only-legacy-observation-variance/v1",
            "checkpoint_observed_at": "2026-10-01T12:00:00.625Z",
            "source_observed_at": OBSERVED,
        }, loaded.manifest["observation_compatibility"])
        prefix = f"checkpoint/{self.identity_directory_name()}"
        self.assertEqual(original["manifest.json"], loaded.verified_artifact_bytes[f"{prefix}/manifest.json"])
        self.assertEqual(original["pages/000001.json"], loaded.verified_artifact_bytes[f"{prefix}/pages/000001.json"])
        self.assertEqual(original["evidence"], loaded.verified_artifact_bytes["clockify-existing.json"])

    def test_opt_in_does_not_version_existing_exact_legacy_proofs(self):
        captured = self.capture(allow_stopped_only_legacy_observation_variance=True)
        self.assertEqual("clockify-native-checkpoint-snapshot/v1", captured.manifest["schema_version"])
        self.assertNotIn("observation_compatibility", captured.manifest)
        self.assertEqual([ENTRY], self.load(captured).entries)

    def test_stopped_variance_internal_strict_validator_remains_strict(self):
        manifest, entry = self.stopped_observation_variance()
        files = {"manifest.json": manifest.read_bytes(),
                 "pages/000001.json": (manifest.parent / "pages/000001.json").read_bytes()}
        _, request = snapshot._request("workspace-one", "user-one", SINCE, UNTIL)
        arguments = dict(identity=self.identity, request=request, since=SINCE, until=UNTIL)
        with self.assertRaisesRegex(ValueError, "projection"):
            snapshot._validate(manifest.parent, files, self.source_evidence.read_bytes(), **arguments)
        entries, observed, legacy_match, compatibility = snapshot._validate_with_compatibility(
            manifest.parent, files, self.source_evidence.read_bytes(), **arguments,
            allow_stopped_only_legacy_observation_variance=True,
        )
        self.assertEqual([entry], entries)
        self.assertEqual("2026-10-01T12:00:00.625000Z", observed)
        self.assertFalse(legacy_match, "observation variance must not unlock legacy proof rounding")
        self.assertEqual(OBSERVED, compatibility["source_observed_at"])

    def test_stopped_variance_rejects_any_other_frozen_value_key_count_or_order_difference(self):
        manifest, _ = self.stopped_observation_variance()
        original = json.loads(self.source_evidence.read_bytes())
        changes = [
            ((), "status", "partial"), ((), "complete", False),
            ((), "running_entry_count", 1), ((), "running_entry_snapshot_count", 1),
            ((), "running_entry_count", False), ((), "pages_fetched", True),
            ((), "unknown_field", "extra"), ((), "entries", []),
            ((), "entries", original["entries"] * 2),
            (("entries", 0), "running", True), (("entries", 0), "running", 0),
            (("entries", 0), "running_snapshot", {}),
            (("entries", 0), "description", "Different work"),
            (("entries", 0), "billable", 0), (("entries", 0), "unknown_field", "extra"),
            (("entries", 0), "start", "2026-09-10T09:00:00Z"),
            (("entries", 0), "end", "2026-09-10T09:50:38Z"),
            (("collection_snapshot",), "unknown_field", "extra"),
            (("collection_snapshot",), "boundary", "2026-10-01T00:00:01Z"),
            (("collection_snapshot",), "requested_until", "2026-10-01T00:00:01Z"),
        ]
        for path, field, value in changes:
            with self.subTest(path=path, field=field, value=value):
                evidence = copy.deepcopy(original)
                target = evidence
                for key in path:
                    target = target[key]
                target[field] = value
                self.source_evidence.write_text(json.dumps(evidence))
                with self.assertRaises(ValueError):
                    self.capture(manifest, allow_stopped_only_legacy_observation_variance=True)
                self.assertFalse(self.destination.exists())
        for path, field in [(("entries", 0), "running_snapshot"),
                            (("entries", 0), "duration"), ((), "complete")]:
            with self.subTest(missing=field):
                evidence = copy.deepcopy(original)
                target = evidence
                for key in path:
                    target = target[key]
                del target[field]
                self.source_evidence.write_text(json.dumps(evidence))
                with self.assertRaises(ValueError):
                    self.capture(manifest, allow_stopped_only_legacy_observation_variance=True)
                self.assertFalse(self.destination.exists())

    def test_stopped_variance_rejects_reordered_full_entry_projection(self):
        second = copy.deepcopy(ENTRY)
        second.update(id="fedcba987654321001234568", description="Second work")
        manifest = self.checkpoint([[ENTRY, second]], metadata={"snapshot_at": "2026-10-01T12:00:00Z"})
        evidence = copy.deepcopy(EVIDENCE)
        second_projection = copy.deepcopy(evidence["entries"][0])
        second_projection.update(id_suffix="01234568", description="Second work")
        evidence["entries"] = [second_projection, evidence["entries"][0]]
        self.source_evidence.write_text(json.dumps(evidence))
        with self.assertRaisesRegex(ValueError, "projection"):
            self.capture(manifest, allow_stopped_only_legacy_observation_variance=True)
        self.assertFalse(self.destination.exists())

    def test_stopped_variance_rejects_source_observation_before_end_equal_earlier_naive_or_malformed(self):
        manifest, _ = self.stopped_observation_variance()
        evidence = json.loads(self.source_evidence.read_bytes())
        for observed in ("2026-09-30T23:59:59Z", "2026-10-01T12:00:00.625Z",
                         "2026-10-01T12:00:00.624Z", "2026-10-02T12:00:00", "bad", None):
            with self.subTest(observed=observed):
                evidence["collection_snapshot"]["observed_at"] = observed
                self.source_evidence.write_text(json.dumps(evidence))
                with self.assertRaises(ValueError):
                    self.capture(manifest, allow_stopped_only_legacy_observation_variance=True)
                self.assertFalse(self.destination.exists())

    def test_stopped_variance_rejects_checkpoint_observation_before_end_later_naive_or_malformed(self):
        for index, observed in enumerate(("2026-09-30T23:59:59Z", "2026-10-02T12:00:01Z",
                                          "2026-10-01T12:00:00", "bad")):
            with self.subTest(observed=observed):
                self.store = checkpoints.PageCheckpointStore(self.root / f"observation-{index}")
                manifest, _ = self.stopped_observation_variance(observed=observed)
                with self.assertRaises(ValueError):
                    self.capture(manifest, allow_stopped_only_legacy_observation_variance=True)
                self.assertFalse(self.destination.exists())

    def test_stopped_variance_rejects_invalid_native_bounds_even_with_matching_legacy_entries(self):
        cases = [
            ("2026-09-10T09:00:00Z", "2026-10-03T10:00:00Z", "2026-09-10 12:00", "2026-10-03 13:00"),
            ("2026-09-10T09:00:00Z", "2026-10-01T00:00:01Z", "2026-09-10 12:00", "2026-10-01 03:00"),
            ("2026-09-10T09:00:00Z", "2026-09-10T08:59:59Z", "2026-09-10 12:00", "2026-09-10 11:59"),
            ("2026-09-10T09:00:00Z", "2026-09-10T09:00:00Z", "2026-09-10 12:00", "2026-09-10 12:00"),
            ("2026-08-31T23:59:59Z", "2026-09-10T10:00:00Z", "2026-09-01 02:59", "2026-09-10 13:00"),
            ("2026-09-10T09:00:00", "2026-09-10T10:00:00Z", "2026-09-10 12:00", "2026-09-10 13:00"),
            ("2026-09-10T09:00:00Z", "2026-09-10T10:00:00", "2026-09-10 12:00", "2026-09-10 13:00"),
            ("2026-09-10T09:00:00Z", "bad", "2026-09-10 12:00", None),
            ("2026-09-10T09:00:00Z", "", "2026-09-10 12:00", None),
        ]
        for index, (start, end, frozen_start, frozen_end) in enumerate(cases):
            with self.subTest(start=start, end=end):
                self.store = checkpoints.PageCheckpointStore(self.root / f"interval-{index}")
                entry = copy.deepcopy(ENTRY)
                entry["timeInterval"].update(start=start, end=end)
                manifest = self.checkpoint([[entry]], metadata={"snapshot_at": "2026-10-01T12:00:00Z"})
                evidence = copy.deepcopy(EVIDENCE)
                evidence["entries"][0].update(start=frozen_start, end=frozen_end)
                self.source_evidence.write_text(json.dumps(evidence))
                with self.assertRaises(ValueError):
                    self.capture(manifest, allow_stopped_only_legacy_observation_variance=True)
                self.assertFalse(self.destination.exists())

    def test_stopped_variance_rejects_native_running_end_even_with_matching_running_snapshot(self):
        entry = copy.deepcopy(ENTRY)
        entry["timeInterval"].update(end=None, duration=None)
        manifest = self.checkpoint([[entry]], metadata={"snapshot_at": "2026-10-01T12:00:00Z"})
        evidence = copy.deepcopy(EVIDENCE)
        evidence.update(running_entry_count=1, running_entry_snapshot_count=1)
        evidence["entries"][0].update(end="2026-10-01 03:00", running=True, duration=None,
            running_snapshot={"observed_at": "2026-10-01T12:00:00Z", "boundary": "2026-10-01T00:00:00Z",
                              "basis": "collection_snapshot_boundary"})
        self.source_evidence.write_text(json.dumps(evidence))
        with self.assertRaisesRegex(ValueError, "projection"):
            self.capture(manifest, allow_stopped_only_legacy_observation_variance=True)
        self.assertFalse(self.destination.exists())

    def test_stopped_variance_retains_identity_and_checkpoint_integrity_requirements(self):
        for index, field in enumerate(("workspaceId", "userId", "id", "taskId", "duplicate", "incomplete", "page", "manifest")):
            with self.subTest(field=field):
                self.store = checkpoints.PageCheckpointStore(self.root / f"integrity-{index}")
                entry = copy.deepcopy(ENTRY)
                if field in ("workspaceId", "userId"):
                    entry[field] = "foreign"
                elif field == "id":
                    entry[field] = ""
                elif field == "taskId":
                    del entry[field]
                manifest = self.checkpoint([[entry, entry] if field == "duplicate" else [entry]],
                    complete=field != "incomplete", metadata={"snapshot_at": "2026-10-01T12:00:00Z"})
                if field == "page":
                    page = manifest.parent / "pages/000001.json"
                    corrupted = json.loads(page.read_bytes())
                    corrupted["payload"][0]["description"] = "corrupted page"
                    page.write_text(json.dumps(corrupted))
                elif field == "manifest":
                    manifest.write_text("{}")
                with self.assertRaises(ValueError):
                    self.capture(manifest, allow_stopped_only_legacy_observation_variance=True)
                self.assertFalse(self.destination.exists())

    def test_stopped_variance_proof_metadata_schema_and_observation_are_rederived_not_trusted(self):
        manifest, _ = self.stopped_observation_variance()
        captured = self.capture(manifest, allow_stopped_only_legacy_observation_variance=True)
        original = copy.deepcopy(captured.manifest)
        mutations = [
            ("mode", "unsupported"), ("checkpoint_observed_at", "2026-10-01T12:00:00.625000Z"),
            ("source_observed_at", "2026-10-02T12:00:01Z"), ("unknown", "extra"),
            ("snapshot_at", "2026-10-01T12:00:00Z"), ("snapshot_at", OBSERVED),
            ("schema_version", "clockify-native-checkpoint-snapshot/v1"),
            ("missing", None), ("downgrade", None),
        ]
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                proof = copy.deepcopy(original)
                if field in ("schema_version", "snapshot_at"):
                    proof[field] = value
                elif field == "missing":
                    del proof["observation_compatibility"]
                elif field == "downgrade":
                    proof["schema_version"] = "clockify-native-checkpoint-snapshot/v1"
                    del proof["observation_compatibility"]
                else:
                    proof["observation_compatibility"][field] = value
                raw = (json.dumps(proof) + "\n").encode()
                (self.destination / "snapshot.json").write_bytes(raw)
                with self.assertRaises(ValueError):
                    self.load(captured, expected_manifest_sha256=hashlib.sha256(raw).hexdigest())

    def test_stopped_variance_replay_rechecks_pinned_source_predicate_and_artifact_hashes(self):
        manifest, _ = self.stopped_observation_variance()
        captured = self.capture(manifest, allow_stopped_only_legacy_observation_variance=True)
        evidence_path = self.destination / "clockify-existing.json"
        evidence_raw = evidence_path.read_bytes()
        evidence_path.write_bytes(evidence_raw + b" ")
        with self.assertRaisesRegex(ValueError, "hash"):
            self.load(captured)
        for field, value in (("unknown", "extra"), ("running_entry_count", 1)):
            with self.subTest(field=field):
                evidence = json.loads(evidence_raw)
                evidence[field] = value
                changed = json.dumps(evidence).encode()
                evidence_path.write_bytes(changed)
                proof = copy.deepcopy(captured.manifest)
                proof["files"]["clockify-existing.json"] = hashlib.sha256(changed).hexdigest()
                proof_raw = json.dumps(proof).encode()
                (self.destination / "snapshot.json").write_bytes(proof_raw)
                with self.assertRaisesRegex(ValueError, "projection"):
                    self.load(captured, expected_manifest_sha256=hashlib.sha256(proof_raw).hexdigest())

    def test_observation_opt_in_requires_boolean_not_truthy_configuration(self):
        manifest, _ = self.stopped_observation_variance()
        for value in ("true", 1, None):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "boolean"):
                    self.capture(manifest, allow_stopped_only_legacy_observation_variance=value)
                self.assertFalse(self.destination.exists())

    def test_exact_precision_snapshot_retains_fractional_observation_and_native_bytes(self):
        entry = copy.deepcopy(ENTRY)
        entry["timeInterval"] = {"start": "2026-09-10T09:00:00.125Z", "end": "2026-09-10T09:50:38.875Z", "duration": "PT50M38.75S"}
        observed = "2026-10-02T12:00:00.625Z"
        evidence = copy.deepcopy(EVIDENCE)
        evidence["entries"][0].update(start="2026-09-10T09:00:00.125000Z", end="2026-09-10T09:50:38.875000Z", duration="PT50M38.75S")
        evidence["collection_snapshot"]["observed_at"] = "2026-10-02T12:00:00.625000Z"
        self.source_evidence.write_text(json.dumps(evidence, indent=2) + "\n")
        manifest = self.checkpoint([[entry]], metadata={"snapshot_at": observed})
        native_bytes = (manifest.parent / "pages/000001.json").read_bytes()
        evidence_bytes = self.source_evidence.read_bytes()
        with patch.object(collector, "clockify_get", side_effect=AssertionError("network forbidden")):
            captured = self.capture(manifest)
            loaded = self.load(captured)
        self.assertEqual("2026-10-02T12:00:00.625000Z", loaded.manifest["snapshot_at"])
        self.assertEqual([entry], loaded.entries)
        self.assertEqual(native_bytes, loaded.verified_artifact_bytes[f"checkpoint/{self.identity_directory_name()}/pages/000001.json"])
        self.assertEqual(evidence_bytes, loaded.verified_artifact_bytes["clockify-existing.json"])
        self.assertEqual(native_bytes, (manifest.parent / "pages/000001.json").read_bytes())
        self.assertEqual(evidence_bytes, self.source_evidence.read_bytes())
        proof = json.loads(loaded.verified_artifact_bytes["snapshot.json"])
        proof["snapshot_at"] = OBSERVED
        rounded_proof_bytes = (json.dumps(proof) + "\n").encode()
        (self.destination / "snapshot.json").write_bytes(rounded_proof_bytes)
        with self.assertRaisesRegex(ValueError, "observation"):
            self.load(captured, expected_manifest_sha256=hashlib.sha256(rounded_proof_bytes).hexdigest())

    def test_exact_legacy_minute_snapshot_replays_precise_native_entries_without_rewrite(self):
        entry = copy.deepcopy(ENTRY)
        entry["timeInterval"] = {"start": "2026-09-10T09:00:00.125Z", "end": "2026-09-10T09:50:38.875Z", "duration": "PT50M38.75S"}
        evidence = copy.deepcopy(EVIDENCE)
        evidence["entries"][0].update(end="2026-09-10 12:50", duration="PT50M38.75S")
        self.source_evidence.write_text(json.dumps(evidence, indent=2) + "\n")
        evidence_bytes = self.source_evidence.read_bytes()
        manifest = self.checkpoint([[entry]], metadata={"snapshot_at": "2026-10-02T12:00:00.625Z"})
        native_bytes = (manifest.parent / "pages/000001.json").read_bytes()
        with patch.object(collector, "clockify_get", side_effect=AssertionError("network forbidden")):
            captured = self.capture(manifest)
            loaded = self.load(captured)
        self.assertEqual([entry], loaded.entries)
        self.assertEqual(3038.75, (dt.datetime.fromisoformat(loaded.entries[0]["timeInterval"]["end"])
                                 - dt.datetime.fromisoformat(loaded.entries[0]["timeInterval"]["start"])).total_seconds())
        self.assertEqual(evidence_bytes, loaded.verified_artifact_bytes["clockify-existing.json"])
        self.assertEqual(native_bytes, loaded.verified_artifact_bytes[f"checkpoint/{self.identity_directory_name()}/pages/000001.json"])
        self.assertEqual(evidence_bytes, self.source_evidence.read_bytes())
        self.assertEqual(native_bytes, (manifest.parent / "pages/000001.json").read_bytes())
        # A frozen historical writer also serialized proof observation to whole seconds.
        proof = json.loads(loaded.verified_artifact_bytes["snapshot.json"])
        proof["snapshot_at"] = OBSERVED
        historical_proof_bytes = (json.dumps(proof) + "\n").encode()
        (self.destination / "snapshot.json").write_bytes(historical_proof_bytes)
        loaded = self.load(captured, expected_manifest_sha256=hashlib.sha256(historical_proof_bytes).hexdigest())
        self.assertEqual(OBSERVED, loaded.manifest["snapshot_at"])
        self.assertEqual(historical_proof_bytes, loaded.verified_artifact_bytes["snapshot.json"])
        self.assertEqual(native_bytes, loaded.verified_artifact_bytes[f"checkpoint/{self.identity_directory_name()}/pages/000001.json"])
        self.assertEqual(evidence_bytes, loaded.verified_artifact_bytes["clockify-existing.json"])

    def test_hybrid_or_partially_truncated_projection_is_not_a_supported_frozen_version(self):
        entry = copy.deepcopy(ENTRY)
        entry["timeInterval"] = {"start": "2026-09-10T09:00:00.125Z", "end": "2026-09-10T09:50:38.875Z", "duration": "PT50M38.75S"}
        manifest = self.checkpoint([[entry]])
        for start, end in [
            ("2026-09-10 12:00", "2026-09-10T09:50:38.875000Z"),
            ("2026-09-10T09:00:00.125000Z", "2026-09-10 12:50"),
            ("2026-09-10T09:00:00.125000Z", "2026-09-10T09:50:38Z"),
            ("2026-09-10T09:00:00.125000Z", "2026-09-10T09:50:00Z"),
            ("2026-09-10 12:00", "2026-09-10 12:51"),
        ]:
            with self.subTest(start=start, end=end):
                evidence = copy.deepcopy(EVIDENCE)
                evidence["entries"][0].update(start=start, end=end, duration="PT50M38.75S")
                self.source_evidence.write_text(json.dumps(evidence))
                with self.assertRaisesRegex(ValueError, "projection"):
                    self.capture(manifest)
                self.assertFalse(self.destination.exists())

    def test_replay_never_reopens_changed_source_cache_or_original_evidence(self):
        manifest = self.checkpoint()
        captured = self.capture(manifest)
        manifest.write_text("changed source cache")
        (manifest.parent / "pages/000001.json").write_text("changed source page")
        self.source_evidence.write_text("changed source evidence")
        loaded = self.load(captured)
        self.assertEqual([ENTRY], loaded.entries)

    def test_verified_artifact_bytes_are_the_exact_bytes_consumed_by_replay(self):
        captured = self.capture()
        loaded = self.load(captured)
        raw = (self.destination / "clockify-existing.json").read_bytes()
        (self.destination / "clockify-existing.json").write_text("later changed bytes")
        self.assertTrue(hasattr(loaded, "verified_artifact_bytes"), "verified replay bytes are missing")
        self.assertEqual(raw, loaded.verified_artifact_bytes["clockify-existing.json"])
        self.assertEqual(loaded.manifest_sha256,
                         hashlib.sha256(loaded.verified_artifact_bytes["snapshot.json"]).hexdigest())
        self.assertEqual(set(loaded.manifest["files"]) | {"snapshot.json"}, set(loaded.verified_artifact_bytes))

    def test_derived_destination_cannot_write_into_original_cache_or_old_source_run(self):
        manifest = self.checkpoint()
        for destination in (manifest.parent / "derived", self.store.root / "derived",
                            self.source_evidence.parent / "clockify-native-checkpoint"):
            with self.subTest(destination=destination):
                with self.assertRaisesRegex(ValueError, "original"):
                    self.capture(manifest, destination=destination)
                self.assertFalse(destination.exists())

    def test_changed_while_read_source_is_rejected_before_copy(self):
        manifest = self.checkpoint()
        from scripts import collector_receipts
        original_read = collector_receipts.os.read
        with patch.object(collector_receipts.os, "read", wraps=collector_receipts.os.read) as read:
            changed = False

            def change_during_read(descriptor, size):
                nonlocal changed
                data = original_read(descriptor, size)
                if data and not changed:
                    manifest.write_bytes(manifest.read_bytes() + b" ")
                    changed = True
                return data

            read.side_effect = change_during_read
            with self.assertRaisesRegex(ValueError, "changed while being read"):
                self.capture(manifest)
        self.assertFalse(self.destination.exists())

    def test_projection_mismatch_fails_before_creating_destination(self):
        evidence = copy.deepcopy(EVIDENCE)
        evidence["entries"][0]["description"] = "Different source run"
        self.source_evidence.write_text(json.dumps(evidence))
        with self.assertRaisesRegex(ValueError, "projection"):
            self.capture()
        self.assertFalse(self.destination.exists())

    def test_duplicate_full_native_ids_fail_even_when_suffix_projection_matches(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.capture(self.checkpoint([[copy.deepcopy(ENTRY), copy.deepcopy(ENTRY)]]))
        self.assertFalse(self.destination.exists())

    def test_missing_full_native_fields_fail(self):
        entry = copy.deepcopy(ENTRY)
        del entry["taskId"]
        with self.assertRaisesRegex(ValueError, "native"):
            self.capture(self.checkpoint([[entry]]))

    def test_foreign_workspace_or_user_rows_fail(self):
        for field in ("workspaceId", "userId"):
            with self.subTest(field=field):
                with tempfile.TemporaryDirectory() as directory:
                    self.store = checkpoints.PageCheckpointStore(Path(directory))
                    entry = copy.deepcopy(ENTRY)
                    entry[field] = "foreign"
                    with self.assertRaisesRegex(ValueError, "workspace|user"):
                        self.capture(self.checkpoint([[entry]]))

    def test_foreign_trusted_request_identity_fails(self):
        manifest = self.checkpoint()
        for changes in ({"workspace_id": "foreign"}, {"user_id": "foreign"},
                        {"since": SINCE + dt.timedelta(days=1)}, {"until": UNTIL + dt.timedelta(days=1)}):
            with self.subTest(changes=changes):
                with self.assertRaisesRegex(ValueError, "identity|locator"):
                    self.capture(manifest, **changes)

    def test_incomplete_checkpoint_fails(self):
        with self.assertRaisesRegex(ValueError, "complete"):
            self.capture(self.checkpoint(complete=False))

    def test_bad_page_continuation_fails(self):
        with self.assertRaisesRegex(ValueError, "continuation"):
            self.capture(self.checkpoint(continuation={"page": 99}))

    def test_short_nonterminal_page_is_not_complete_pagination(self):
        second = copy.deepcopy(ENTRY)
        second["id"] = "fedcba987654321001234568"
        with self.assertRaisesRegex(ValueError, "nonterminal"):
            self.capture(self.checkpoint([[ENTRY], [second]]))

    def test_complete_checkpoint_requires_short_terminal_page(self):
        entries = []
        for index in range(collector.CLOCKIFY_PAGE_SIZE):
            entry = copy.deepcopy(ENTRY)
            entry["id"] = f"{index:024x}"
            entries.append(entry)
        with self.assertRaisesRegex(ValueError, "final page"):
            self.capture(self.checkpoint([entries]))

    def test_invalid_snapshot_at_fails(self):
        with self.assertRaisesRegex(ValueError, "snapshot_at"):
            self.capture(self.checkpoint(metadata={"snapshot_at": "2026-10-02T12:00:00"}))

    def test_snapshot_tampering_is_rejected(self):
        captured = self.capture()
        targets = [self.destination / "snapshot.json", self.destination / "clockify-existing.json",
                   self.destination / "checkpoint" / self.identity_directory_name() / "manifest.json",
                   self.destination / "checkpoint" / self.identity_directory_name() / "pages/000001.json"]
        for target in targets:
            with self.subTest(target=target.name):
                original = target.read_bytes()
                target.write_bytes(original + b" ")
                with self.assertRaisesRegex(ValueError, "hash|digest"):
                    self.load(captured)
                target.write_bytes(original)

    def test_replay_requires_external_proof_hash_and_matching_identity(self):
        captured = self.capture()
        for changes in ({"expected_manifest_sha256": None}, {"expected_manifest_sha256": "0" * 64},
                        {"workspace_id": "foreign"}, {"user_id": "foreign"},
                        {"until": UNTIL + dt.timedelta(days=1)}):
            with self.subTest(changes=changes):
                with self.assertRaises(ValueError):
                    self.load(captured, **changes)

    def test_symlinked_source_component_fails(self):
        manifest = self.checkpoint()
        link = self.root / "cache-link"
        link.symlink_to(self.store.root, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.capture(link / manifest.parent.name / "manifest.json")

    def test_symlinked_snapshot_component_fails(self):
        captured = self.capture()
        page = self.destination / "checkpoint" / self.identity_directory_name() / "pages/000001.json"
        outside = self.root / "outside.json"
        outside.write_bytes(page.read_bytes())
        page.unlink()
        page.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.load(captured)

    def test_destination_must_be_fresh_and_not_symlinked(self):
        manifest = self.checkpoint()
        self.destination.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError, "fresh"):
            self.capture(manifest)
        link = self.root / "destination-link"
        link.symlink_to(self.root / "fresh-run", target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            self.capture(manifest, destination=link / "other")

    def test_renamed_or_ambiguous_checkpoint_locator_fails(self):
        manifest = self.checkpoint()
        renamed = manifest.parent.with_name("unbound-checkpoint")
        manifest.parent.rename(renamed)
        with self.assertRaisesRegex(ValueError, "locator"):
            self.capture(renamed / "manifest.json")

    def test_absent_optional_snapshot_preserves_compatibility(self):
        self.assertIsNone(snapshot.load_checkpoint_snapshot(
            None, workspace_id="workspace-one", user_id="user-one", since=SINCE, until=UNTIL,
            expected_manifest_sha256=None))

    def test_transport_imports_for_direct_script_consumer(self):
        scripts = Path(__file__).resolve().parents[1] / "scripts"
        result = subprocess.run(
            [sys.executable, "-c", "import clockify_checkpoint_snapshot"],
            cwd=self.root, env={**os.environ, "PYTHONPATH": str(scripts), "PYTHONDONTWRITEBYTECODE": "1"},
            text=True, capture_output=True,
        )
        self.assertEqual(0, result.returncode, result.stderr)


if __name__ == "__main__":
    unittest.main()
