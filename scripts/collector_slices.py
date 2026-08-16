"""Deterministic local-calendar slices for recovery-only collection backlogs."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from zoneinfo import ZoneInfo


SCHEMA_VERSION = "collector-backlog/v1"


class BacklogError(ValueError):
    pass


@dataclass(frozen=True)
class CollectionSlice:
    since: datetime
    until: datetime
    slice_id: str


@dataclass(frozen=True)
class BacklogIdentity:
    since_utc: str
    until_utc: str
    timezone: str
    max_days: int
    compatibility_version: str

    def document(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SliceReceipt:
    slice_id: str
    result_path: Path
    result_digest: str


@dataclass(frozen=True)
class BacklogState:
    identity: BacklogIdentity
    directory: Path
    slices: tuple[CollectionSlice, ...]
    completed: tuple[SliceReceipt, ...]
    complete: bool


def plan_slices(
    since: datetime,
    until: datetime,
    *,
    zone: ZoneInfo,
    max_days: int = 2,
) -> tuple[CollectionSlice, ...]:
    """Partition an aware interval at local midnight boundaries."""
    _validate_interval(since, until, zone, max_days)
    local_since = since.astimezone(zone)
    local_until = until.astimezone(zone)
    slices: list[CollectionSlice] = []
    cursor = local_since
    while cursor < local_until:
        boundary = datetime.combine(
            cursor.date() + timedelta(days=max_days), time.min, tzinfo=zone
        )
        end = min(boundary, local_until)
        slices.append(CollectionSlice(cursor, end, _slice_id(cursor, end, zone)))
        cursor = end
    return tuple(slices)


def _validate_interval(
    since: datetime, until: datetime, zone: ZoneInfo, max_days: int
) -> None:
    if not isinstance(zone, ZoneInfo):
        raise BacklogError("zone must be a ZoneInfo")
    if not isinstance(max_days, int) or isinstance(max_days, bool) or not 1 <= max_days <= 2:
        raise BacklogError("max_days must be one or two local calendar days")
    for value, name in ((since, "since"), (until, "until")):
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise BacklogError(f"{name} must be timezone-aware")
    if since >= until:
        raise BacklogError("since must be before until")


def _slice_id(since: datetime, until: datetime, zone: ZoneInfo) -> str:
    identity = {
        "since_utc": _utc_string(since),
        "timezone": zone.key,
        "until_utc": _utc_string(until),
    }
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _utc_string(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class BacklogStore:
    """Atomically maintains verified, immutable completion receipts."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def open(
        self, identity: BacklogIdentity, slices: tuple[CollectionSlice, ...]
    ) -> BacklogState:
        plan = _plan_document(identity, slices)
        directory = self.root / _digest(plan)[7:]
        manifest_path = directory / "backlog-manifest.json"
        if not manifest_path.exists():
            if directory.exists():
                raise BacklogError("backlog manifest is missing")
            _atomic_json(
                manifest_path,
                {**plan, "completed": [], "complete": False},
            )
        return self._state_from_manifest(
            identity, tuple(slices), directory, self._read_manifest(manifest_path)
        )

    def record_complete(
        self,
        state: BacklogState,
        slice_id: str,
        result_path: Path,
        result_digest: str,
    ) -> BacklogState:
        current = self._current_state(state)
        if current.complete:
            raise BacklogError("completed backlogs are immutable")
        if not isinstance(slice_id, str) or slice_id not in {
            slice_.slice_id for slice_ in current.slices
        }:
            raise BacklogError("receipt references an unknown slice")
        if slice_id in {receipt.slice_id for receipt in current.completed}:
            raise BacklogError("slice receipt is immutable")

        artifact = _verified_artifact(result_path, result_digest)
        receipt = {
            "slice_id": slice_id,
            "result_path": str(artifact),
            "result_digest": result_digest,
        }
        manifest = self._read_manifest(current.directory / "backlog-manifest.json")
        completed = [*manifest["completed"], receipt]
        _atomic_json(
            current.directory / "backlog-manifest.json",
            {
                **_plan_document(current.identity, current.slices),
                "completed": completed,
                "complete": len(completed) == len(current.slices),
            },
        )
        return self.open(current.identity, current.slices)

    def next_incomplete(self, state: BacklogState) -> CollectionSlice | None:
        current = self._current_state(state)
        completed = {receipt.slice_id for receipt in current.completed}
        return next(
            (slice_ for slice_ in current.slices if slice_.slice_id not in completed), None
        )

    def _current_state(self, state: BacklogState) -> BacklogState:
        if not isinstance(state, BacklogState):
            raise BacklogError("state must be a BacklogState")
        expected = self.root / _digest(_plan_document(state.identity, state.slices))[7:]
        if state.directory != expected:
            raise BacklogError("state directory does not match its backlog identity")
        return self.open(state.identity, state.slices)

    def _read_manifest(self, path: Path) -> Mapping[str, object]:
        if not path.is_file() or path.is_symlink():
            raise BacklogError("backlog manifest is missing or unsafe")
        try:
            document = json.loads(path.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise BacklogError("backlog manifest is not valid JSON") from error
        if not isinstance(document, dict):
            raise BacklogError("backlog manifest must be an object")
        return MappingProxyType(document)

    def _state_from_manifest(
        self,
        identity: BacklogIdentity,
        slices: tuple[CollectionSlice, ...],
        directory: Path,
        manifest: Mapping[str, object],
    ) -> BacklogState:
        plan = _plan_document(identity, slices)
        required = {*plan, "completed", "complete"}
        if set(manifest) != required or any(manifest[key] != value for key, value in plan.items()):
            raise BacklogError("backlog manifest does not match its identity")
        completed_document = manifest["completed"]
        if not isinstance(completed_document, list):
            raise BacklogError("backlog completed receipts must be a list")
        receipts = tuple(_receipt_from_document(value) for value in completed_document)
        valid_ids = {slice_.slice_id for slice_ in slices}
        if len({receipt.slice_id for receipt in receipts}) != len(receipts):
            raise BacklogError("backlog contains duplicate receipts")
        if any(receipt.slice_id not in valid_ids for receipt in receipts):
            raise BacklogError("backlog receipt references an unknown slice")
        for receipt in receipts:
            _verified_artifact(receipt.result_path, receipt.result_digest)
        complete = len(receipts) == len(slices)
        if manifest["complete"] is not complete:
            raise BacklogError("backlog completion state is inconsistent")
        return BacklogState(identity, directory, slices, receipts, complete)


def _plan_document(
    identity: BacklogIdentity, slices: tuple[CollectionSlice, ...]
) -> dict[str, object]:
    if not isinstance(identity, BacklogIdentity):
        raise BacklogError("identity must be a BacklogIdentity")
    _validate_identity(identity)
    validated_slices = tuple(slices)
    expected = plan_slices(
        _parse_utc(identity.since_utc),
        _parse_utc(identity.until_utc),
        zone=ZoneInfo(identity.timezone),
        max_days=identity.max_days,
    )
    if validated_slices != expected:
        raise BacklogError("slices do not match the requested backlog identity")
    return {
        "schema_version": SCHEMA_VERSION,
        "identity": identity.document(),
        "slices": [
            {
                "slice_id": slice_.slice_id,
                "since_utc": _utc_string(slice_.since),
                "until_utc": _utc_string(slice_.until),
            }
            for slice_ in validated_slices
        ],
    }


def _validate_identity(identity: BacklogIdentity) -> None:
    if not isinstance(identity.timezone, str):
        raise BacklogError("identity timezone must be a string")
    try:
        ZoneInfo(identity.timezone)
    except (ValueError, KeyError) as error:
        raise BacklogError("identity timezone is invalid") from error
    _validate_interval(
        _parse_utc(identity.since_utc),
        _parse_utc(identity.until_utc),
        ZoneInfo(identity.timezone),
        identity.max_days,
    )
    if not isinstance(identity.compatibility_version, str) or not identity.compatibility_version:
        raise BacklogError("identity compatibility version must be a nonempty string")


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise BacklogError("UTC timestamps must use a canonical Z suffix")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise BacklogError("UTC timestamp is invalid") from error
    if _utc_string(parsed) != value:
        raise BacklogError("UTC timestamp is not canonical")
    return parsed


def _receipt_from_document(value: object) -> SliceReceipt:
    if not isinstance(value, dict) or set(value) != {
        "slice_id", "result_path", "result_digest"
    }:
        raise BacklogError("backlog receipt schema is invalid")
    if not isinstance(value["slice_id"], str) or not isinstance(value["result_path"], str):
        raise BacklogError("backlog receipt fields are invalid")
    path = Path(value["result_path"])
    if not path.is_absolute():
        raise BacklogError("backlog receipt path must be absolute")
    return SliceReceipt(value["slice_id"], path, _digest_string(value["result_digest"]))


def _verified_artifact(path: Path, expected_digest: str) -> Path:
    if not isinstance(path, Path):
        path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise BacklogError("receipt result artifact is missing or unsafe")
    resolved = path.resolve()
    digest = "sha256:" + _file_sha256(resolved)
    if digest != _digest_string(expected_digest):
        raise BacklogError("receipt result artifact digest does not match")
    return resolved


def _file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            hasher.update(block)
    return hasher.hexdigest()


def _digest_string(value: object) -> str:
    if not isinstance(value, str) or len(value) != 71 or not value.startswith("sha256:"):
        raise BacklogError("digest must be a sha256 digest")
    try:
        int(value[7:], 16)
    except ValueError as error:
        raise BacklogError("digest must be hexadecimal") from error
    return value


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(_canonical(value) + b"\n")
    os.replace(temporary, path)
