"""Immutable transport of completed native Clockify pages, not semantic events.

The caller supplies trusted request identity and pins ``manifest_sha256`` in its
own receipt. Source locators in snapshot.json are audit information only; replay
never reads them. Existing checkpoints and sanitized collector schema are unchanged.
"""
from __future__ import annotations

from dataclasses import dataclass
import copy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Mapping

try:
    from scripts import collector_checkpoints as checkpoints
    from scripts import collector_receipts
    from scripts import clockify_sync_collect as collector
except ModuleNotFoundError:  # direct script execution
    import collector_checkpoints as checkpoints
    import collector_receipts
    import clockify_sync_collect as collector


SCHEMA_VERSION = "clockify-native-checkpoint-snapshot/v1"
SCHEMA_VERSION_V2 = "clockify-native-checkpoint-snapshot/v2"
STOPPED_ONLY_LEGACY_OBSERVATION_VARIANCE = "stopped-only-legacy-observation-variance/v1"
_NATIVE_FIELDS = {
    "id", "workspaceId", "userId", "description", "projectId", "tagIds",
    "taskId", "billable", "timeInterval",
}


@dataclass(frozen=True)
class ClockifyCheckpointSnapshot:
    entries: list[dict]
    manifest: dict
    manifest_sha256: str
    verified_artifact_bytes: dict[str, bytes]


def _hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _safe_path(path: Path) -> Path:
    path = Path(path)
    if ".." in path.parts:
        raise checkpoints.CheckpointError("snapshot path has an ambiguous locator")
    return collector_receipts._safe_path(path)


def _read(path: Path) -> bytes:
    path = _safe_path(path)
    try:
        return collector_receipts._safe_read_bytes_and_digest(path)[0]
    except OSError as error:
        raise checkpoints.CheckpointError("snapshot input file is missing or unsafe") from error


def _document(raw: bytes) -> dict:
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as error:
        raise checkpoints.CheckpointError("snapshot input is invalid JSON") from error
    return checkpoints._mapping(value, "snapshot document")


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise checkpoints.CheckpointError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise checkpoints.CheckpointError(f"{label} is invalid") from error
    if parsed.tzinfo is None:
        raise checkpoints.CheckpointError(f"{label} must be timezone-aware")
    return parsed


def _request(workspace_id: str, user_id: str, since: datetime, until: datetime):
    if not all(isinstance(value, str) and value.strip() for value in (workspace_id, user_id)):
        raise checkpoints.CheckpointError("trusted workspace and user identity are required")
    if not all(isinstance(value, datetime) and value.tzinfo is not None for value in (since, until)):
        raise checkpoints.CheckpointError("trusted period must be timezone-aware")
    if since >= until:
        raise checkpoints.CheckpointError("trusted period is invalid")
    identity = collector._clockify_checkpoint_identity(workspace_id, user_id, since, until)
    request = dict(workspace_id=workspace_id, user_id=user_id,
                   since_utc=identity.since_utc, until_utc=identity.until_utc)
    return identity, request


class _CapturedStore(checkpoints.PageCheckpointStore):
    """Read-only adapter for the existing collector projection over captured bytes."""

    def __init__(self, directory: Path, files: dict[str, bytes]):
        super().__init__(directory.parent)
        self.directory = directory
        self.files = files

    def _read_page(self, path):
        return _document(self.files[path.relative_to(self.directory).as_posix()])

    def open(self, identity, *, initial_metadata=None):
        if self.directory != self._directory_for(identity):
            raise checkpoints.CheckpointError("checkpoint locator does not match trusted identity")
        state = self._state_from_manifest(identity, self.directory, _document(self.files["manifest.json"]))
        if not state.complete:
            raise checkpoints.CheckpointError("checkpoint must be complete")
        return state

    def append_page(self, *args, **kwargs):
        raise checkpoints.CheckpointError("snapshot replay is read-only")

    def mark_complete(self, *args, **kwargs):
        raise checkpoints.CheckpointError("snapshot replay is read-only")


def _legacy_minute_projection(projection: dict) -> dict:
    """Reproduce the historical writer exactly, without normalizing bound inputs."""
    legacy = copy.deepcopy(projection)
    for entry in legacy["entries"]:
        for field in ("start", "end"):
            entry[field] = collector.local_dt_string(collector.parse_dt(entry[field]))
        if entry["running_snapshot"] is not None:
            for field in ("observed_at", "boundary"):
                entry["running_snapshot"][field] = collector.iso_utc(collector.parse_dt(entry["running_snapshot"][field]))
    for field in ("observed_at", "boundary", "requested_until"):
        legacy["collection_snapshot"][field] = collector.iso_utc(collector.parse_dt(legacy["collection_snapshot"][field]))
    return legacy


def _validate(directory: Path, files: dict[str, bytes], evidence: bytes, *, identity,
              request: dict, since: datetime, until: datetime):
    """Existing strict validation and its exact legacy-rounding status."""
    entries, observed, legacy_match, _ = _validate_with_compatibility(
        directory, files, evidence, identity=identity, request=request, since=since, until=until,
    )
    return entries, observed, legacy_match


def _stopped_only_observation_compatibility(entries, legacy_projection, bound_projection, *,
                                           checkpoint_observed_at, observed, since, until):
    """Admit different observations, never different evidence or native bounds."""
    for entry in entries:
        interval = entry["timeInterval"]
        if interval["end"] is None:
            return None
        start = _timestamp(interval["start"], "native interval start")
        end = _timestamp(interval["end"], "native interval end")
        if not since <= start < end <= until:
            return None
    for document in (legacy_projection, bound_projection):
        if document.get("status") != "ok" or document.get("complete") is not True:
            return None
        for field in ("running_entry_count", "running_entry_snapshot_count"):
            if type(document.get(field)) is not int or document[field] != 0:
                return None
        rows = document.get("entries")
        if not isinstance(rows, list) or len(rows) != len(entries):
            return None
        if any(not isinstance(row, dict) or row.get("running") is not False
               or "running_snapshot" not in row or row["running_snapshot"] is not None for row in rows):
            return None
        collection = document.get("collection_snapshot")
        if not isinstance(collection, dict):
            return None
        for field in ("boundary", "requested_until"):
            if _timestamp(collection.get(field), f"collection {field}") != until:
                return None
    source_observed_at = bound_projection["collection_snapshot"].get("observed_at")
    source_observed = _timestamp(source_observed_at, "source collection observation")
    if not until <= observed < source_observed:
        return None
    comparison = copy.deepcopy(legacy_projection)
    comparison["collection_snapshot"]["observed_at"] = source_observed_at
    # Canonical full documents preserve keys, ordering of rows, and JSON types;
    # no unknown fields are dropped and False is not treated as numeric zero.
    if checkpoints._canonical(comparison) != checkpoints._canonical(bound_projection):
        return None
    return {
        "mode": STOPPED_ONLY_LEGACY_OBSERVATION_VARIANCE,
        "checkpoint_observed_at": checkpoint_observed_at,
        "source_observed_at": source_observed_at,
    }


def _validate_with_compatibility(directory: Path, files: dict[str, bytes], evidence: bytes, *, identity,
                                 request: dict, since: datetime, until: datetime,
                                 allow_stopped_only_legacy_observation_variance: bool = False):
    if not isinstance(allow_stopped_only_legacy_observation_variance, bool):
        raise checkpoints.CheckpointError("stopped-only observation compatibility requires an explicit boolean")
    store = _CapturedStore(directory, files)
    state = store.open(identity)
    observed = _timestamp(state.metadata.get("snapshot_at"), "snapshot_at")
    collector._clockify_checkpoint_page(state)
    entries, _ = collector._clockify_checkpoint_entries(store, state)
    for index, page in enumerate(store.iter_pages(state)):
        rows = page["payload"]
        if len(rows) > collector.CLOCKIFY_PAGE_SIZE or not all(isinstance(row, Mapping) for row in rows):
            raise checkpoints.CheckpointError("checkpoint native page payload is invalid")
        if index < len(state.pages) - 1 and len(rows) != collector.CLOCKIFY_PAGE_SIZE:
            raise checkpoints.CheckpointError("checkpoint has a short nonterminal page")
    if len(state.pages) > 100:
        raise checkpoints.CheckpointError("checkpoint exceeds pagination safety limit")
    seen = set()
    for entry in entries:
        if not _NATIVE_FIELDS <= set(entry):
            raise checkpoints.CheckpointError("checkpoint entry lacks full native fields")
        native_id = entry["id"]
        if not isinstance(native_id, str) or not native_id.strip():
            raise checkpoints.CheckpointError("checkpoint full native ID is invalid")
        if native_id in seen:
            raise checkpoints.CheckpointError("checkpoint has duplicate native IDs")
        seen.add(native_id)
        if entry["workspaceId"] != request["workspace_id"] or entry["userId"] != request["user_id"]:
            raise checkpoints.CheckpointError("checkpoint entry has foreign workspace or user")
        if not isinstance(entry["description"], str) or not isinstance(entry["billable"], bool):
            raise checkpoints.CheckpointError("checkpoint native entry fields are invalid")
        if any(value is not None and not isinstance(value, str) for value in (entry["projectId"], entry["taskId"])):
            raise checkpoints.CheckpointError("checkpoint native project or task is invalid")
        if not isinstance(entry["tagIds"], tuple) or not all(isinstance(tag, str) for tag in entry["tagIds"]):
            raise checkpoints.CheckpointError("checkpoint native tags are invalid")
        interval = entry["timeInterval"]
        if not isinstance(interval, Mapping) or not {"start", "end", "duration"} <= set(interval):
            raise checkpoints.CheckpointError("checkpoint native interval is incomplete")
        _timestamp(interval["start"], "native interval start")
        if interval["end"] is not None:
            _timestamp(interval["end"], "native interval end")
    # This path is guaranteed complete above; the read-only adapter cannot collect
    # a new page or alter a cache. No API key is present even if a regression tried.
    projection = collector.fetch_clockify(
        {"CLOCKIFY_WORKSPACE_ID": request["workspace_id"]},
        {"clockify_user_id": request["user_id"]}, since, until,
        snapshot_at=observed, checkpoint_store=store,
    )
    bound_projection = _document(evidence)
    legacy_projection = _legacy_minute_projection(projection)
    legacy_match = bound_projection == legacy_projection
    compatibility = None
    if projection != bound_projection and not legacy_match:
        if allow_stopped_only_legacy_observation_variance:
            compatibility = _stopped_only_observation_compatibility(
                entries, legacy_projection, bound_projection,
                checkpoint_observed_at=state.metadata["snapshot_at"], observed=observed,
                since=since, until=until,
            )
        if compatibility is None:
            raise checkpoints.CheckpointError("checkpoint projection does not match bound source-run evidence")
    # JSON round-trip converts the existing immutable checkpoint views to the
    # native JSON shape without dropping or shortening any fields.
    full_entries = [json.loads(checkpoints._canonical(_plain(entry))) for entry in entries]
    return full_entries, collector._clockify_timestamp(observed.astimezone(timezone.utc)), legacy_match, compatibility


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def capture_checkpoint_snapshot(*, checkpoint_manifest: Path, clockify_evidence: Path,
                                destination: Path, workspace_id: str, user_id: str,
                                since: datetime, until: datetime,
                                allow_stopped_only_legacy_observation_variance: bool = False) -> ClockifyCheckpointSnapshot:
    """Validate an exact completed locator, then copy bytes into a fresh directory."""
    identity, request = _request(workspace_id, user_id, since, until)
    destination = _safe_path(destination)
    if destination.exists():
        raise checkpoints.CheckpointError("snapshot destination must be fresh")
    checkpoint_manifest = _safe_path(checkpoint_manifest)
    clockify_evidence = _safe_path(clockify_evidence)
    directory = checkpoint_manifest.parent
    for protected in (directory.parent, clockify_evidence.parent.parent):
        if destination.is_relative_to(protected):
            raise checkpoints.CheckpointError("snapshot destination cannot alter original cache or source run")
    if checkpoint_manifest.name != "manifest.json" or directory.name != checkpoints._digest(identity.document())[7:]:
        raise checkpoints.CheckpointError("checkpoint locator does not match trusted identity")
    manifest_raw = _read(checkpoint_manifest)
    manifest = _document(manifest_raw)
    if manifest.get("identity") != identity.document():
        raise checkpoints.CheckpointError("checkpoint identity does not match trusted request")
    files = {"manifest.json": manifest_raw}
    references = manifest.get("pages")
    if not isinstance(references, list):
        raise checkpoints.CheckpointError("checkpoint page references are invalid")
    for index, reference in enumerate(references, 1):
        relative = f"pages/{index:06d}.json"
        if not isinstance(reference, dict) or reference.get("path") != relative:
            raise checkpoints.CheckpointError("checkpoint page locator is invalid")
        files[relative] = _read(directory / relative)
    evidence = _read(clockify_evidence)
    entries, observed, _, compatibility = _validate_with_compatibility(
        directory, files, evidence, identity=identity, request=request, since=since, until=until,
        allow_stopped_only_legacy_observation_variance=allow_stopped_only_legacy_observation_variance,
    )
    checkpoint_relative = f"checkpoint/{directory.name}"
    artifacts = {f"{checkpoint_relative}/{relative}": raw for relative, raw in files.items()}
    artifacts["clockify-existing.json"] = evidence
    proof = dict(
        schema_version=SCHEMA_VERSION, request=request, checkpoint_identity=identity.document(),
        snapshot_at=observed, entry_count=len(entries), page_count=len(references),
        source_checkpoint_manifest=str(checkpoint_manifest), source_clockify_evidence=str(clockify_evidence),
        files={relative: _hash(raw) for relative, raw in artifacts.items()},
    )
    if compatibility is not None:
        proof["schema_version"] = SCHEMA_VERSION_V2
        proof["observation_compatibility"] = compatibility
    proof_raw = checkpoints._canonical(proof) + b"\n"
    destination.mkdir(parents=True, exist_ok=False)
    for relative, raw in artifacts.items():
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw)
    (destination / "snapshot.json").write_bytes(proof_raw)
    return load_checkpoint_snapshot(destination, workspace_id=workspace_id, user_id=user_id,
                                    since=since, until=until, expected_manifest_sha256=_hash(proof_raw))


def load_checkpoint_snapshot(snapshot_dir: Path | None, *, workspace_id: str, user_id: str,
                             since: datetime, until: datetime,
                             expected_manifest_sha256: str | None) -> ClockifyCheckpointSnapshot | None:
    """Replay only sealed derived bytes. None explicitly means no optional artifact."""
    if snapshot_dir is None:
        return None
    identity, request = _request(workspace_id, user_id, since, until)
    snapshot_dir = _safe_path(snapshot_dir)
    proof_raw = _read(snapshot_dir / "snapshot.json")
    proof_hash = _hash(proof_raw)
    if not isinstance(expected_manifest_sha256, str) or proof_hash != expected_manifest_sha256:
        raise checkpoints.CheckpointError("snapshot manifest hash does not match external receipt")
    proof = _document(proof_raw)
    expected_keys = {"schema_version", "request", "checkpoint_identity", "snapshot_at", "entry_count",
                     "page_count", "source_checkpoint_manifest", "source_clockify_evidence", "files"}
    version = proof.get("schema_version")
    if version == SCHEMA_VERSION_V2:
        expected_keys.add("observation_compatibility")
    if set(proof) != expected_keys or version not in (SCHEMA_VERSION, SCHEMA_VERSION_V2):
        raise checkpoints.CheckpointError("snapshot manifest schema is unsupported")
    if proof["request"] != request or proof["checkpoint_identity"] != identity.document():
        raise checkpoints.CheckpointError("snapshot identity does not match trusted request")
    count = proof["page_count"]
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 100:
        raise checkpoints.CheckpointError("snapshot page count is invalid")
    checkpoint_relative = f"checkpoint/{checkpoints._digest(identity.document())[7:]}"
    expected_files = {"clockify-existing.json", f"{checkpoint_relative}/manifest.json"}
    expected_files |= {f"{checkpoint_relative}/pages/{index:06d}.json" for index in range(1, count + 1)}
    hashes = proof["files"]
    if not isinstance(hashes, dict) or set(hashes) != expected_files:
        raise checkpoints.CheckpointError("snapshot artifact inventory is invalid")
    artifacts = {}
    for relative in sorted(expected_files):
        raw = _read(snapshot_dir / relative)
        if hashes[relative] != _hash(raw):
            raise checkpoints.CheckpointError("snapshot artifact hash does not match")
        artifacts[relative] = raw
    files = {relative.removeprefix(checkpoint_relative + "/"): raw for relative, raw in artifacts.items()
             if relative.startswith(checkpoint_relative + "/")}
    entries, observed, legacy_match, compatibility = _validate_with_compatibility(
        snapshot_dir / checkpoint_relative, files, artifacts["clockify-existing.json"],
        identity=identity, request=request, since=since, until=until,
        allow_stopped_only_legacy_observation_variance=version == SCHEMA_VERSION_V2,
    )
    if version == SCHEMA_VERSION_V2 and (compatibility is None or proof["observation_compatibility"] != compatibility):
        raise checkpoints.CheckpointError("snapshot observation compatibility metadata does not match pinned evidence")
    observed_matches = proof["snapshot_at"] == observed or (
        legacy_match and proof["snapshot_at"] == collector.iso_utc(_timestamp(observed, "snapshot_at"))
    )
    if not observed_matches or proof["entry_count"] != len(entries):
        raise checkpoints.CheckpointError("snapshot observation or native entry count does not match")
    return ClockifyCheckpointSnapshot(entries=entries, manifest=proof, manifest_sha256=proof_hash,
                                     verified_artifact_bytes={**artifacts, "snapshot.json": proof_raw})
