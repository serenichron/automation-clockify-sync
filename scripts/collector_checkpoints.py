"""Atomic, local checkpoints for paginated collector responses."""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Iterator, Mapping


SCHEMA_VERSION = "collector-page-checkpoint/v1"


class CheckpointError(ValueError):
    pass


@dataclass(frozen=True)
class CheckpointIdentity:
    source: str
    since_utc: str
    until_utc: str
    request_fingerprint: str
    compatibility_version: str

    def document(self) -> dict[str, str]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class CheckpointState:
    identity: CheckpointIdentity
    directory: Path
    pages: tuple[Mapping[str, object], ...]
    continuation: Mapping[str, object]
    metadata: Mapping[str, object]
    complete: bool


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(_canonical(value) + b"\n")
    os.replace(temporary, path)


class PageCheckpointStore:
    def __init__(self, root: Path):
        self.root = Path(root)

    def open(
        self,
        identity: CheckpointIdentity,
        *,
        initial_metadata: Mapping[str, object] | None = None,
    ) -> CheckpointState:
        if not isinstance(identity, CheckpointIdentity):
            raise CheckpointError("identity must be a CheckpointIdentity")
        directory = self._directory_for(identity)
        manifest_path = directory / "manifest.json"
        if not manifest_path.exists():
            metadata = _optional_mapping(initial_metadata, "initial metadata")
            _atomic_json(
                manifest_path,
                {
                    "schema_version": SCHEMA_VERSION,
                    "identity": identity.document(),
                    "pages": [],
                    "continuation": {},
                    "metadata": metadata,
                    "complete": False,
                },
            )
        return self._state_from_manifest(identity, directory, self._read_manifest(manifest_path))

    def append_page(
        self,
        state: CheckpointState,
        *,
        payload: object,
        continuation: Mapping[str, object],
        signature: str,
        metadata: Mapping[str, object] | None = None,
    ) -> CheckpointState:
        current = self._current_state(state)
        if current.complete:
            raise CheckpointError("completed checkpoints are immutable")
        next_continuation = _mapping(continuation, "continuation")
        page_metadata = _optional_mapping(metadata, "page metadata")
        if not isinstance(signature, str):
            raise CheckpointError("signature must be a string")
        manifest = self._read_manifest(current.directory / "manifest.json")
        index = len(current.pages) + 1
        path = f"pages/{index:06d}.json"
        page = {
            "index": index,
            "payload": payload,
            "payload_digest": _digest(payload),
            "continuation": next_continuation,
            "signature": signature,
            "metadata": page_metadata,
        }
        _atomic_json(current.directory / path, page)
        page_reference = {
            "index": index,
            "path": path,
            "payload_digest": page["payload_digest"],
            "page_digest": _digest(page),
        }
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "identity": current.identity.document(),
            "pages": [*manifest["pages"], page_reference],
            "continuation": next_continuation,
            "metadata": dict(current.metadata),
            "complete": False,
        }
        _atomic_json(current.directory / "manifest.json", manifest)
        return self._state_from_manifest(current.identity, current.directory, manifest)

    def mark_complete(
        self,
        state: CheckpointState,
        *,
        metadata: Mapping[str, object] | None = None,
    ) -> CheckpointState:
        current = self._current_state(state)
        if current.complete:
            raise CheckpointError("completed checkpoints are immutable")
        manifest = self._read_manifest(current.directory / "manifest.json")
        completed_metadata = {
            **current.metadata,
            **_optional_mapping(metadata, "completion metadata"),
        }
        completed_manifest = {
            "schema_version": SCHEMA_VERSION,
            "identity": current.identity.document(),
            "pages": manifest["pages"],
            "continuation": dict(current.continuation),
            "metadata": completed_metadata,
            "complete": True,
            "pages_digest": _digest(manifest["pages"]),
            "completed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        _atomic_json(current.directory / "manifest.json", completed_manifest)
        return self._state_from_manifest(
            current.identity, current.directory, completed_manifest
        )

    def remove_completed_before(self, cutoff: datetime) -> tuple[Path, ...]:
        if cutoff.tzinfo is None:
            raise CheckpointError("cutoff must be timezone-aware")
        if not self.root.exists():
            return ()
        removed: list[Path] = []
        for directory in self.root.iterdir():
            if not directory.is_dir() or directory.is_symlink():
                continue
            manifest_path = directory / "manifest.json"
            try:
                manifest = self._read_manifest(manifest_path)
                identity = _identity_from_document(manifest.get("identity"))
                if directory != self._directory_for(identity):
                    raise CheckpointError("checkpoint directory does not match identity")
                state = self._state_from_manifest(identity, directory, manifest)
                if not state.complete:
                    continue
                completed_at = _parse_utc(manifest["completed_at"])
            except (CheckpointError, KeyError):
                continue
            if completed_at < cutoff:
                shutil.rmtree(directory)
                removed.append(directory)
        return tuple(removed)

    def iter_pages(self, state: CheckpointState) -> Iterator[Mapping[str, object]]:
        if not isinstance(state, CheckpointState):
            raise CheckpointError("state must be a CheckpointState")
        if state.directory != self._directory_for(state.identity):
            raise CheckpointError("state directory does not match identity")
        for expected_index, reference in enumerate(state.pages, start=1):
            _, page = self._validate_page(reference, expected_index, state.directory)
            yield MappingProxyType(
                {key: _immutable_value(value) for key, value in page.items()}
            )

    def _directory_for(self, identity: CheckpointIdentity) -> Path:
        return self.root / _digest(identity.document())[7:]

    def _current_state(self, state: CheckpointState) -> CheckpointState:
        if not isinstance(state, CheckpointState):
            raise CheckpointError("state must be a CheckpointState")
        if state.directory != self._directory_for(state.identity):
            raise CheckpointError("state directory does not match identity")
        return self.open(state.identity)

    def _read_manifest(self, path: Path) -> Mapping[str, object]:
        if not path.is_file() or path.is_symlink():
            raise CheckpointError("manifest is missing or unsafe")
        try:
            value = json.loads(path.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CheckpointError("manifest is not valid JSON") from error
        return _mapping(value, "manifest")

    def _state_from_manifest(
        self,
        identity: CheckpointIdentity,
        directory: Path,
        manifest: Mapping[str, object],
    ) -> CheckpointState:
        required = {
            "schema_version",
            "identity",
            "pages",
            "continuation",
            "metadata",
            "complete",
        }
        complete = manifest.get("complete")
        if complete is True:
            required |= {"pages_digest", "completed_at"}
        if set(manifest) != required:
            raise CheckpointError("manifest schema does not match checkpoint version")
        if manifest["schema_version"] != SCHEMA_VERSION:
            raise CheckpointError("manifest schema version is not supported")
        if manifest["identity"] != identity.document():
            raise CheckpointError("manifest identity does not match requested checkpoint")
        continuation = _mapping(manifest["continuation"], "manifest continuation")
        metadata = _mapping(manifest["metadata"], "manifest metadata")
        if not isinstance(complete, bool):
            raise CheckpointError("manifest complete must be a boolean")
        pages = manifest["pages"]
        if not isinstance(pages, list):
            raise CheckpointError("manifest pages must be a list")
        page_references: list[Mapping[str, object]] = []
        for expected_index, reference_value in enumerate(pages, start=1):
            reference, _ = self._validate_page(
                reference_value, expected_index, directory
            )
            page_references.append(MappingProxyType(reference))
        if complete:
            if manifest["pages_digest"] != _digest(pages):
                raise CheckpointError("completed page digest does not match")
            _parse_utc(manifest["completed_at"])
        return CheckpointState(
            identity=identity,
            directory=directory,
            pages=tuple(page_references),
            continuation=continuation,
            metadata=metadata,
            complete=complete,
        )

    def _read_page(self, path: Path) -> Mapping[str, object]:
        if not path.is_file() or path.is_symlink():
            raise CheckpointError("referenced page is missing or unsafe")
        try:
            value = json.loads(path.read_text())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CheckpointError("referenced page is not valid JSON") from error
        return _mapping(value, "page")

    def _validate_page(
        self,
        reference_value: Mapping[str, object],
        expected_index: int,
        directory: Path,
    ) -> tuple[Mapping[str, object], Mapping[str, object]]:
        reference = _mapping(reference_value, "page reference")
        if set(reference) != {"index", "path", "payload_digest", "page_digest"}:
            raise CheckpointError("page reference schema is invalid")
        path = f"pages/{expected_index:06d}.json"
        if (
            not _is_page_index(reference["index"], expected_index)
            or reference["path"] != path
        ):
            raise CheckpointError("page reference index or path is invalid")
        if not _is_digest(reference["payload_digest"]) or not _is_digest(
            reference["page_digest"]
        ):
            raise CheckpointError("page reference digest is invalid")
        page = self._read_page(directory / path)
        if set(page) != {
            "index",
            "payload",
            "payload_digest",
            "continuation",
            "signature",
            "metadata",
        }:
            raise CheckpointError("page schema is invalid")
        if not _is_page_index(page["index"], expected_index):
            raise CheckpointError("page index is invalid")
        if not isinstance(page["signature"], str):
            raise CheckpointError("page signature is invalid")
        _mapping(page["continuation"], "page continuation")
        _mapping(page["metadata"], "page metadata")
        if page["payload_digest"] != _digest(page["payload"]):
            raise CheckpointError("page payload digest does not match")
        if page["payload_digest"] != reference["payload_digest"]:
            raise CheckpointError("manifest payload digest does not match page")
        if _digest(page) != reference["page_digest"]:
            raise CheckpointError("manifest page digest does not match page")
        return reference, page


def _mapping(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise CheckpointError(f"{name} must be a mapping")
    return dict(value)


def _optional_mapping(value: object, name: str) -> dict[str, object]:
    if value is None:
        return {}
    return _mapping(value, name)


def _immutable_value(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _immutable_value(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_immutable_value(item) for item in value)
    return value


def _identity_from_document(value: object) -> CheckpointIdentity:
    document = _mapping(value, "identity")
    expected = {
        "source",
        "since_utc",
        "until_utc",
        "request_fingerprint",
        "compatibility_version",
    }
    if set(document) != expected or not all(isinstance(item, str) for item in document.values()):
        raise CheckpointError("identity document is invalid")
    return CheckpointIdentity(**document)  # type: ignore[arg-type]


def _is_digest(value: object) -> bool:
    return isinstance(value, str) and value.startswith("sha256:") and len(value) == 71


def _is_page_index(value: object, expected: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == expected


def _parse_utc(value: object) -> datetime:
    if not isinstance(value, str):
        raise CheckpointError("completed_at must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CheckpointError("completed_at must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise CheckpointError("completed_at must be timezone-aware")
    return parsed.astimezone(timezone.utc)
