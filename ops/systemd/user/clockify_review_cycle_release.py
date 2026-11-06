#!/usr/bin/env python3
"""Materialize and atomically select an immutable user-service release."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import datetime as dt
import fcntl
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from typing import Iterator, Sequence


IDENTITY_NAME = ".clockify-release.json"
IDENTITY_SCHEMA = "clockify-user-release/v1"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}")
ACTOR_CONTRACT = "clockify-semantic-actors/v1"
ACTOR_BINDING_KEYS = {"source_type", "server_origin", "workspace_id", "author_id"}


def _sha(value: str) -> str:
    if not SHA_PATTERN.fullmatch(value):
        raise ValueError("release SHA must be exactly 40 lowercase hexadecimal characters")
    return value


def _canonical(path: Path, *, must_exist: bool = True) -> Path:
    requested = path.expanduser()
    resolved = requested.resolve(strict=must_exist)
    if requested.absolute() != resolved:
        raise ValueError(f"path must be canonical and contain no symlink components: {path}")
    return resolved


def _git(repository: Path, *arguments: str, binary: bool = False):
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=not binary,
    ).stdout


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _secure_directory(
    path: Path, label: str, *, exact_mode: int | None = None
) -> Path:
    requested = path.expanduser().absolute()
    if requested.is_symlink():
        raise ValueError(f"{label} must not be a symlink")
    path = _canonical(requested)
    details = path.stat()
    if not path.is_dir():
        raise ValueError(f"{label} must be a directory")
    if details.st_uid != os.getuid():
        raise ValueError(f"{label} must be owned by the service user")
    mode = stat.S_IMODE(details.st_mode)
    if mode & 0o022:
        raise ValueError(f"{label} must not be group/other writable")
    if exact_mode is not None and mode != exact_mode:
        raise ValueError(f"{label} must have mode {exact_mode:04o}")
    return path


def _atomic_write(path: Path, payload: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = _secure_directory(path.parent, "output parent")
    path = parent / path.name
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("atomic write made no progress")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _safe_members(archive: tarfile.TarFile) -> list[tarfile.TarInfo]:
    members = archive.getmembers()
    for member in members:
        name = PurePosixPath(member.name)
        if name.is_absolute() or ".." in name.parts:
            raise ValueError("Git archive contains an unsafe path")
        if not (member.isdir() or member.isfile()):
            raise ValueError("Git archive contains a non-regular entry")
    return members


def _make_payload_read_only(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_file():
            current = stat.S_IMODE(path.stat().st_mode)
            path.chmod(0o555 if current & 0o111 else 0o444)
        elif path.is_dir():
            path.chmod(0o555)


def _tree_manifest(root: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = [{
        "mode": f"{stat.S_IMODE(root.stat().st_mode):04o}",
        "path": ".",
        "type": "directory",
    }]
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative == IDENTITY_NAME:
            continue
        if path.is_symlink():
            raise ValueError("release tree contains a symlink")
        details = path.stat()
        entry = {
            "mode": f"{stat.S_IMODE(details.st_mode):04o}",
            "path": relative,
            "type": "directory" if path.is_dir() else "file",
        }
        if path.is_file():
            entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        elif not path.is_dir():
            raise ValueError("release tree contains a special file")
        entries.append(entry)
    return entries


def _manifest_digest(manifest: list[dict[str, str]]) -> str:
    encoded = json.dumps(
        manifest, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _release_sha(release: Path) -> str:
    try:
        return _sha(release.name)
    except ValueError as exc:
        raise ValueError(
            "release directory basename must be the exact expected Git SHA"
        ) from exc


def _identity_document(identity_path: Path) -> dict[str, object]:
    if identity_path.is_symlink():
        raise ValueError("release identity must not be a symlink")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(identity_path, flags)
    except OSError as exc:
        raise ValueError("release identity must be a non-symlink regular file") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ValueError("release identity must be a regular file")
        if details.st_uid != os.getuid():
            raise ValueError("release identity must be owned by the service user")
        if stat.S_IMODE(details.st_mode) != 0o444:
            raise ValueError("release identity must have exact mode 0444")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    try:
        value = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("release identity is unreadable") from exc
    if not isinstance(value, dict):
        raise ValueError("release identity must be a JSON object")
    return value


def _identity(release: Path, sha: str) -> dict[str, object]:
    release = _canonical(release)
    _secure_directory(release.parent, "releases root")
    _secure_directory(release, "release root")
    expected_sha = _sha(sha)
    if _release_sha(release) != expected_sha:
        raise ValueError("release directory SHA does not match caller expected SHA")
    identity_path = release / IDENTITY_NAME
    value = _identity_document(identity_path)
    if value.get("git_sha") != expected_sha:
        raise ValueError("release identity SHA does not match caller expected SHA")
    manifest = _tree_manifest(release)
    tree_digest = _manifest_digest(manifest)
    if value.get("tree_manifest") != manifest or value.get("tree_digest") != tree_digest:
        raise ValueError("release tree does not match its immutable manifest")
    routing = release / "routing.json"
    if not routing.is_file() or routing.is_symlink():
        raise ValueError("release tree routing is missing")
    expected = {
        "schema_version": IDENTITY_SCHEMA,
        "git_sha": expected_sha,
        "root": str(release),
        "routing_sha256": hashlib.sha256(routing.read_bytes()).hexdigest(),
        "tree_digest": tree_digest,
        "tree_manifest": manifest,
    }
    if value != expected:
        raise ValueError("release identity does not match its root, SHA, and routing")
    return expected


def _private_file(path: Path, label: str) -> Path:
    requested = path.expanduser().absolute()
    if requested.is_symlink():
        raise ValueError(f"{label} must not be a symlink")
    _secure_directory(requested.parent, f"{label} parent")
    path = _canonical(requested)
    details = path.stat()
    if not path.is_file() or stat.S_IMODE(details.st_mode) != 0o600:
        raise ValueError(f"{label} must be a regular mode-0600 file")
    if details.st_uid != os.getuid():
        raise ValueError(f"{label} must be owned by the service user")
    return path


def _validated_config(release: Path, config: Path) -> tuple[Path, dict[str, object]]:
    config = _private_file(config, "review-cycle config")
    try:
        document = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("review-cycle config must be valid JSON") from exc
    if not isinstance(document, dict) or document.get("root") != str(release):
        raise ValueError("review-cycle config root does not match release")
    base_routing = release / "routing.json"
    private_handle = document.get("private_routing")
    if private_handle is None:
        if document.get("routing") != str(base_routing):
            raise ValueError("review-cycle config routing does not match release")
    else:
        if (
            not isinstance(private_handle, dict)
            or set(private_handle) != {"sha256", "base_sha256"}
            or not all(
                isinstance(private_handle.get(key), str)
                and DIGEST_PATTERN.fullmatch(private_handle[key])
                for key in ("sha256", "base_sha256")
            )
        ):
            raise ValueError("private routing requires exact SHA-256 bindings")
        base_bytes = base_routing.read_bytes()
        if hashlib.sha256(base_bytes).hexdigest() != private_handle["base_sha256"]:
            raise ValueError("private routing base does not match immutable release")
        raw_routing = document.get("routing")
        if not isinstance(raw_routing, str) or not raw_routing:
            raise ValueError("private routing path is missing")
        private_path = _private_file(Path(raw_routing), "private routing")
        private_bytes = private_path.read_bytes()
        if hashlib.sha256(private_bytes).hexdigest() != private_handle["sha256"]:
            raise ValueError("private routing SHA-256 differs")
        try:
            base_document = json.loads(base_bytes)
            private_document = json.loads(private_bytes)
        except json.JSONDecodeError as exc:
            raise ValueError("private routing must be valid JSON") from exc
        if not isinstance(base_document, dict) or not isinstance(private_document, dict):
            raise ValueError("private routing must be a JSON object")
        actor_contract = private_document.pop("semantic_actor_contract", None)
        actor_binding = private_document.pop("semantic_subject_binding", None)
        if json.dumps(
            private_document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ) != json.dumps(
            base_document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ):
            raise ValueError("private routing may change only semantic actor fields")
        if actor_contract != ACTOR_CONTRACT or (
            not isinstance(actor_binding, dict)
            or set(actor_binding) != ACTOR_BINDING_KEYS
            or actor_binding.get("source_type") != "multica"
            or any(
                not isinstance(actor_binding.get(key), str)
                or not actor_binding[key].strip()
                for key in ACTOR_BINDING_KEYS
            )
        ):
            raise ValueError("private routing semantic actor binding is invalid")
    return config, document


def _ledger_file(path: Path, label: str) -> Path:
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symlink")
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError(f"{label} must be an existing regular mode-0600 file") from exc
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise ValueError(f"{label} must be a regular mode-0600 file")
        if details.st_uid != os.getuid():
            raise ValueError(f"{label} must be owned by the service user")
        if stat.S_IMODE(details.st_mode) != 0o600:
            raise ValueError(f"{label} must have exact mode 0600")
    finally:
        os.close(descriptor)
    return path


def _ledger_paths(
    document: dict[str, object], *, require_exists: bool
) -> tuple[Path, Path]:
    raw_state = document.get("state_dir")
    if not isinstance(raw_state, str) or not raw_state:
        raise ValueError("review-cycle state directory is missing")
    state = _secure_directory(Path(raw_state).expanduser().absolute(), "state directory")
    paths: list[Path] = []
    for key, label in (("corrections", "corrections ledger"), ("acceptance", "acceptance ledger")):
        raw = document.get(key)
        if not isinstance(raw, str) or not raw:
            raise ValueError(f"{label} path is missing")
        requested = Path(raw).expanduser().absolute()
        if requested.is_symlink():
            raise ValueError(f"{label} must not be a symlink")
        resolved = requested.resolve(strict=False)
        if requested != resolved:
            raise ValueError(f"{label} path must be canonical")
        try:
            resolved.relative_to(state)
        except ValueError as exc:
            raise ValueError(f"{label} must be within the state directory") from exc
        _secure_directory(resolved.parent, f"{label} parent")
        if resolved.exists() or require_exists:
            _ledger_file(resolved, label)
        paths.append(resolved)
    return paths[0], paths[1]


def _checkpoint_root(document: dict[str, object]) -> Path:
    raw_state = document.get("state_dir")
    if not isinstance(raw_state, str) or not raw_state:
        raise ValueError("collector checkpoint root requires a state directory")
    state = _secure_directory(Path(raw_state).expanduser().absolute(), "state directory")
    raw_override = os.environ.get("CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT", "").strip()
    requested = (
        Path(raw_override).expanduser().absolute()
        if raw_override
        else state / "collector-checkpoints"
    )
    if not Path(raw_override).is_absolute() and raw_override:
        raise ValueError("collector checkpoint root must be absolute")
    if requested.is_symlink():
        raise ValueError("collector checkpoint root must not be a symlink")
    resolved = requested.resolve(strict=False)
    if requested != resolved:
        raise ValueError("collector checkpoint root path must be canonical")
    try:
        resolved.relative_to(state)
    except ValueError as exc:
        raise ValueError("collector checkpoint root must be within state directory") from exc
    if resolved.exists():
        _secure_directory(
            resolved, "collector checkpoint root", exact_mode=0o700
        )
    elif raw_override:
        try:
            _secure_directory(
                resolved.parent, "collector checkpoint root parent", exact_mode=0o700
            )
        except OSError as exc:
            raise ValueError(
                "collector checkpoint root parent must be an existing directory"
            ) from exc
    return resolved


def bootstrap_ledgers(release: Path, config: Path) -> tuple[Path, Path]:
    """Create absent private ledgers without following links or touching existing bytes."""
    release = _canonical(release)
    _identity(release, _release_sha(release))
    _, document = _validated_config(release, config)
    ledgers = _ledger_paths(document, require_exists=False)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    for path, label in zip(ledgers, ("corrections ledger", "acceptance ledger")):
        if path.exists() or path.is_symlink():
            _ledger_file(path, label)
            continue
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            _ledger_file(path, label)
            continue
        try:
            details = os.fstat(descriptor)
            if not stat.S_ISREG(details.st_mode):
                raise ValueError(f"{label} creation did not produce a regular file")
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(path.parent)
        _ledger_file(path, label)
    return ledgers


def _private_tree(path: Path, label: str) -> Path:
    path = _secure_directory(path, label, exact_mode=0o700)
    for item in path.rglob("*"):
        if item.is_symlink():
            raise ValueError(f"{label} must not contain symlinks")
        item_details = item.stat()
        if item_details.st_uid != os.getuid():
            raise ValueError(f"{label} entries must be owned by the service user")
        if not (item.is_dir() or item.is_file()):
            raise ValueError(f"{label} must not contain special files")
        if stat.S_IMODE(item_details.st_mode) & 0o022:
            raise ValueError(f"{label} entries must not be group/other writable")
    return path


def verify_runtime(
    release: Path,
    config: Path,
    environment: Path,
    credential_environment: Path,
    override: Path,
    gws: Path,
) -> dict[str, object]:
    """Fail closed before systemd exposes private inputs to the coordinator."""
    release = _canonical(release)
    expected_sha = _release_sha(release)
    identity = _identity(release, expected_sha)
    config, document = _validated_config(release, config)
    _checkpoint_root(document)
    _ledger_paths(document, require_exists=True)
    _private_file(environment, "review-cycle environment")
    _private_file(credential_environment, "credential environment")
    override = _private_file(override, "activation override")
    expected_override = (
        f"CLOCKIFY_AUTOPILOT_ROOT={release}\n"
        f"CLOCKIFY_REVIEW_CYCLE_CONFIG={config}\n"
    )
    if override.read_text(encoding="utf-8") != expected_override:
        raise ValueError("activation override does not match release and config")
    _private_tree(gws, "GWS directory")
    return identity


def materialize(source_repository: Path, releases_root: Path, sha: str) -> Path:
    """Publish only the exact commit archive under ``releases_root/<sha>``."""
    sha = _sha(sha)
    source = _canonical(source_repository)
    top = Path(str(_git(source, "rev-parse", "--show-toplevel")).strip()).resolve()
    if top != source:
        raise ValueError("source repository must be its exact Git toplevel")
    resolved = str(_git(source, "rev-parse", "--verify", f"{sha}^{{commit}}")).strip()
    if resolved != sha:
        raise ValueError("requested SHA does not resolve to the exact commit")

    releases_root = releases_root.expanduser().absolute()
    releases_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    releases_root = _secure_directory(releases_root, "releases root")
    destination = releases_root / sha
    if destination.exists():
        _identity(destination, sha)
        return destination

    temporary = Path(tempfile.mkdtemp(prefix=".materialize-", dir=releases_root))
    try:
        payload = _git(source, "archive", "--format=tar", sha, binary=True)
        with tarfile.open(fileobj=BytesIO(payload), mode="r:") as archive:
            members = _safe_members(archive)
            archive.extractall(temporary, members=members, filter="data")
        routing = temporary / "routing.json"
        entrypoint = temporary / "scripts" / "clockify_review_cycle.py"
        if not routing.is_file() or routing.is_symlink() or not entrypoint.is_file():
            raise ValueError("release is missing routing or review-cycle entrypoint")
        _make_payload_read_only(temporary)
        identity_path = temporary / IDENTITY_NAME
        identity_path.touch(mode=0o600)
        temporary.chmod(0o555)
        manifest = _tree_manifest(temporary)
        identity = {
            "schema_version": IDENTITY_SCHEMA,
            "git_sha": sha,
            "root": str(destination),
            "routing_sha256": hashlib.sha256(routing.read_bytes()).hexdigest(),
            "tree_digest": _manifest_digest(manifest),
            "tree_manifest": manifest,
        }
        identity_path.write_text(
            json.dumps(identity, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        identity_path.chmod(0o444)
        for file_path in (path for path in temporary.rglob("*") if path.is_file()):
            with file_path.open("rb") as handle:
                os.fsync(handle.fileno())
        _fsync_directory(temporary)
        os.replace(temporary, destination)
        _fsync_directory(releases_root)
    except Exception:
        if temporary.exists():
            for path in [temporary, *temporary.rglob("*")]:
                try:
                    path.chmod(0o700 if path.is_dir() else 0o600)
                except OSError:
                    pass
            shutil.rmtree(temporary, ignore_errors=True)
        raise
    _identity(destination, sha)
    return destination


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    path = path.expanduser().absolute()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _secure_directory(path.parent, "lock parent")
    path = _canonical(path, must_exist=False)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        details = os.fstat(descriptor)
        if (not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid()
                or stat.S_IMODE(details.st_mode) != 0o600):
            raise ValueError("release lock must be a user-owned regular mode-0600 file")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        os.close(descriptor)


def _lock_path(path: Path) -> Path:
    return path.with_name(path.name + ".lock")


def _override_bytes(override: Path) -> bytes | None:
    if not override.exists() and not override.is_symlink():
        return None
    return _private_file(override, "activation override").read_bytes()


def _override_payload(release: Path, config: Path) -> bytes:
    return (
        f"CLOCKIFY_AUTOPILOT_ROOT={release}\n"
        f"CLOCKIFY_REVIEW_CYCLE_CONFIG={config}\n"
    ).encode("utf-8")


class _OverrideChanged(ValueError):
    pass


def _activate_locked(
    release: Path, sha: str, config: Path, override: Path, expected: bytes | None,
) -> None:
    sha = _sha(sha)
    release = _canonical(release)
    _identity(release, sha)
    config, _ = _validated_config(release, config)
    if _override_bytes(override) != expected:
        raise _OverrideChanged("activation override changed")
    _atomic_write(override, _override_payload(release, config), mode=0o600)


def activate(release: Path, sha: str, config: Path, override: Path) -> None:
    """Serialize all selectors and atomically replace a verified activation."""
    with _file_lock(_lock_path(override)):
        expected = _override_bytes(override)
        _activate_locked(release, sha, config, override, expected)


def _selection(override: Path) -> tuple[bytes, Path, Path]:
    payload = _override_bytes(override)
    if payload is None:
        raise ValueError("activation override is missing")
    lines = payload.decode("utf-8").splitlines()
    if (len(lines) != 2 or not lines[0].startswith("CLOCKIFY_AUTOPILOT_ROOT=")
            or not lines[1].startswith("CLOCKIFY_REVIEW_CYCLE_CONFIG=")):
        raise ValueError("activation override must contain exactly the activation pointers")
    release = _canonical(Path(lines[0].split("=", 1)[1]))
    config = _private_file(Path(lines[1].split("=", 1)[1]), "review-cycle config")
    if payload != _override_payload(release, config):
        raise ValueError("activation override does not match release and config")
    _identity(release, _release_sha(release))
    return payload, release, config


def _read_record(path: Path) -> dict[str, object]:
    if not path.exists() and not path.is_symlink():
        return {"rejected": [], "results": [], "proven": []}
    document = json.loads(_private_file(path, "release update record").read_bytes())
    if not isinstance(document, dict):
        raise ValueError("release update record must be a JSON object")
    for key in ("rejected", "results", "proven"):
        document.setdefault(key, [])
        if not isinstance(document[key], list):
            raise ValueError("release update record lists are invalid")
    for item in document["rejected"]:
        if (not isinstance(item, dict) or set(item) != {"sha", "reason"}
                or not isinstance(item["sha"], str) or not isinstance(item["reason"], str)):
            raise ValueError("release update rejection is invalid")
        _sha(item["sha"])
    for item in document["results"]:
        if (not isinstance(item, dict) or not isinstance(item.get("sha"), str)
                or "exit_status" not in item):
            raise ValueError("release update result is invalid")
        _sha(item["sha"])
    for sha in document["proven"]:
        if not isinstance(sha, str):
            raise ValueError("release update proven SHA is invalid")
        _sha(sha)
    for key in ("current", "previous"):
        if document.get(key) is not None:
            if not isinstance(document[key], str):
                raise ValueError("release update SHA is invalid")
            _sha(document[key])
    if document.get("health") not in (None, "proven", "unproven"):
        raise ValueError("release update health is invalid")
    pending = document.get("pending")
    if pending is not None:
        if not isinstance(pending, dict) or set(pending) != {"from", "to"}:
            raise ValueError("release update pending transaction is invalid")
        for key in ("from", "to"):
            if not isinstance(pending[key], str):
                raise ValueError("release update pending SHA is invalid")
            _sha(pending[key])
    return document


def _write_record(path: Path, record: dict[str, object]) -> None:
    _atomic_write(
        path, (json.dumps(record, sort_keys=True, indent=2) + "\n").encode("utf-8"), mode=0o600
    )


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _healthy(result: dict[str, object]) -> bool:
    return result.get("exit_code") == "exited" and str(result.get("exit_status")) in {"0", "75"}


def _failed(result: dict[str, object]) -> bool:
    if str(result.get("exit_status")) == "73" or _healthy(result):
        return False
    return result.get("service_result") in {
        "exit-code", "core-dump", "timeout", "start-limit-hit", "resources",
    }


def _release_health(record: dict[str, object], sha: str) -> str:
    if (sha in record["proven"]
            or (record.get("current") == sha and record.get("health") == "proven")
            or any(item["sha"] == sha and _healthy(item) for item in record["results"])):
        return "proven"
    return "unproven"


def _rejected(record: dict[str, object], sha: str) -> bool:
    return any(item["sha"] == sha for item in record["rejected"])


def _reject(record: dict[str, object], sha: str, reason: str) -> None:
    if not _rejected(record, sha):
        record["rejected"].append({"sha": sha, "reason": reason})


def _finish_switch(record: dict[str, object], previous: str, current: str) -> None:
    rollback = _rejected(record, previous)
    record.update({
        "previous": None if rollback else previous,
        "current": current,
        "activated_at": _now(),
        "health": _release_health(record, current),
        "pending": None,
    })


class _UpdateSkipped(ValueError):
    pass


def _switch(
    record_path: Path, record: dict[str, object], override: Path, expected: bytes,
    old: Path, release: Path, config: Path,
) -> None:
    if _rejected(record, release.name):
        raise _UpdateSkipped("rejected")
    record["pending"] = {"from": old.name, "to": release.name}
    _write_record(record_path, record)
    if _override_bytes(override) != expected:
        raise _UpdateSkipped("override_changed")
    _activate_locked(release, release.name, config, override, expected)
    _finish_switch(record, old.name, release.name)
    _write_record(record_path, record)


def _candidate_file(path: Path, payload: bytes, *, previously_active: bool) -> None:
    if path.exists() or path.is_symlink():
        if _private_file(path, "candidate config").read_bytes() == payload:
            return
        if previously_active:
            raise _UpdateSkipped("config_conflict")
    _atomic_write(path, payload, mode=0o600)


def _derive_config(old: Path, release: Path, config: Path, *, previously_active: bool = True) -> Path:
    original = config.read_bytes()
    derived = original.replace(old.name.encode("ascii"), release.name.encode("ascii"))
    before, after = json.loads(original), json.loads(derived)
    if not isinstance(before, dict) or not isinstance(after, dict):
        raise ValueError("review-cycle config must be a JSON object")
    routing_target = config.parent / f"clockify-review-cycle.{release.name}.routing.json"
    payload = None
    derived_keys = {"root", "routing"}
    if before.get("private_routing") is not None:
        base_bytes = (release / "routing.json").read_bytes()
        if base_bytes != (old / "routing.json").read_bytes():
            _, validated_before = _validated_config(old, config)
            if validated_before != before:
                raise ValueError("review-cycle config changed during derivation")
            private_bytes = _private_file(Path(before["routing"]), "private routing").read_bytes()
            if hashlib.sha256(private_bytes).hexdigest() != before["private_routing"]["sha256"]:
                raise ValueError("private routing changed during derivation")
            private_document = json.loads(private_bytes)
            rebased = json.loads(base_bytes)
            if not isinstance(rebased, dict):
                raise ValueError("private routing base must be a JSON object")
            style = {"indent": 2, "sort_keys": True, "ensure_ascii": False}
            newline = "\n"
            indentation = re.search(rb'\n([ \t]+)"', private_bytes)
            previous_style = {
                "indent": indentation[1].decode("ascii") if indentation else None,
                "sort_keys": list(private_document) == sorted(private_document),
                "ensure_ascii": b"\\u" in private_bytes,
            }
            previous_newline = "\n" if private_bytes.endswith(b"\n") else ""
            # Preserve a detected style only when it reproduces the original bytes.
            previous_serialized = json.dumps(private_document, **previous_style) + previous_newline
            if previous_serialized.encode("utf-8") == private_bytes:
                style, newline = previous_style, previous_newline
            for key in ("semantic_actor_contract", "semantic_subject_binding"):
                rebased[key] = private_document[key]
            payload = (json.dumps(rebased, **style) + newline).encode("utf-8")
            after["routing"] = str(routing_target)
            after["private_routing"] = {
                "sha256": hashlib.sha256(payload).hexdigest(),
                "base_sha256": hashlib.sha256(base_bytes).hexdigest(),
            }
            derived = (
                json.dumps(after, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
            ).encode("utf-8")
            derived_keys.add("private_routing")
    retained_before = {key: item for key, item in before.items() if key not in derived_keys}
    retained_after = {key: item for key, item in after.items() if key not in derived_keys}
    if (json.dumps(retained_before, sort_keys=True, separators=(",", ":"))
            != json.dumps(retained_after, sort_keys=True, separators=(",", ":"))):
        raise _UpdateSkipped("config_drift")
    target = config.parent / f"clockify-review-cycle.{release.name}.json"
    routing = config.parent / f"clockify-review-cycle.{old.name}.routing.json"
    if payload is None and (routing.exists() or routing.is_symlink()):
        payload = _private_file(routing, "private routing").read_bytes().replace(
            old.name.encode("ascii"), release.name.encode("ascii")
        )
    if payload is not None:
        _candidate_file(routing_target, payload,
                        previously_active=previously_active)
    _candidate_file(target, derived, previously_active=previously_active)
    _validated_config(release, target)
    return target


def _update_locked(arguments: argparse.Namespace) -> str:
    override, record_path = arguments.override, arguments.record
    expected, active, config = _selection(override)
    record = _read_record(record_path)
    bound_override = str(_canonical(override))
    if record.get("override", bound_override) != bound_override:
        raise ValueError("release update record override differs")
    record["override"] = bound_override
    pending = record.get("pending")
    if pending is not None:
        if active.name == pending["to"]:
            if _rejected(record, active.name):
                raise _UpdateSkipped("rejected")
            _finish_switch(record, pending["from"], pending["to"])
        elif active.name == pending["from"]:
            record["pending"] = None
        else:
            raise _UpdateSkipped("override_changed")
    if record.get("current") != active.name:
        # The initial selection (or an explicit manual activation) predates this update.
        record.update({"current": active.name, "previous": None, "health": "proven"})
    if record.get("health") == "proven" and active.name not in record["proven"]:
        record["proven"].append(active.name)
    if arguments.clear_rejected is not None:
        sha = _sha(arguments.clear_rejected)
        record["rejected"] = [item for item in record["rejected"] if item["sha"] != sha]
    _write_record(record_path, record)

    decisive = [item for item in record["results"]
                if item["sha"] == active.name and (_healthy(item) or _failed(item))][-2:]
    previous = record.get("previous")
    if (_release_health(record, active.name) == "unproven" and len(decisive) == 2
            and all(_failed(item) for item in decisive)
            and previous is not None and not _rejected(record, previous)):
        restored = active.parent / previous
        restored_config = config.parent / f"clockify-review-cycle.{previous}.json"
        rollback_record = {**record, "rejected": list(record["rejected"])}
        try:
            _identity(restored, previous)
            _validated_config(restored, restored_config)
            _reject(rollback_record, active.name, "blocked_after_activation")
            _switch(record_path, rollback_record, override, expected, active, restored, restored_config)
        except _OverrideChanged:
            raise
        except Exception as exc:
            if isinstance(exc, _UpdateSkipped) and str(exc) == "override_changed":
                raise
            # A failed final record write may follow a successful pointer replacement.
            selected = _override_bytes(override)
            if selected != expected:
                if selected != _override_payload(restored, restored_config):
                    raise _OverrideChanged("activation override changed") from exc
                _finish_switch(rollback_record, active.name, previous)
                record = rollback_record
                expected, active, config = _selection(override)
            record["previous"] = None
            record["pending"] = None
            _write_record(record_path, record)
            print("clockify release rollback unavailable", file=sys.stderr)
        else:
            return f"clockify release rolled back: {active.name} -> {previous}"

    try:
        source = _canonical(arguments.source_repository)
        # Sandboxed user units see root-owned /etc/ssh files as unowned and ssh rejects
        # them, so read only the user ssh configuration.
        ssh_config = Path.home() / ".ssh" / "config"
        environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": (
            f"ssh -F {ssh_config if ssh_config.is_file() else os.devnull} -o BatchMode=yes"
            " -o ConnectTimeout=20 -o StrictHostKeyChecking=yes"
        )}
        subprocess.run(
            ["git", "-C", str(source), "fetch", "--quiet", "--", arguments.remote, arguments.branch],
            check=True, capture_output=True, env=environment, timeout=120,
        )
        sha = subprocess.run(
            ["git", "-C", str(source), "rev-parse", "--verify",
             f"refs/remotes/{arguments.remote}/{arguments.branch}^{{commit}}"],
            check=True, capture_output=True, text=True, env=environment, timeout=120,
        ).stdout.strip()
        _sha(sha)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        raise _UpdateSkipped("fetch_failed") from exc
    if sha == active.name:
        return f"clockify release current: {sha}"
    if _rejected(record, sha):
        raise _UpdateSkipped("rejected")
    try:
        release = materialize(source, arguments.releases_root, sha)
    except (OSError, ValueError, subprocess.SubprocessError, tarfile.TarError) as exc:
        raise _UpdateSkipped("materialize_failed") from exc
    try:
        previously_active = (sha in record["proven"]
                             or sha in {record.get("current"), record.get("previous")}
                             or any(item["sha"] == sha for item in record["results"]))
        candidate_config = _derive_config(active, release, config, previously_active=previously_active)
    except _UpdateSkipped as exc:
        if str(exc) in {"config_drift", "config_conflict"}:
            _reject(record, sha, str(exc))
            _write_record(record_path, record)
        raise
    except (OSError, ValueError) as exc:
        raise _UpdateSkipped("config_invalid") from exc
    try:
        descriptor, name = tempfile.mkstemp(prefix=".clockify-candidate-", dir=override.parent)
        os.close(descriptor)
        candidate_override = Path(name)
        try:
            _atomic_write(candidate_override, _override_payload(release, candidate_config), mode=0o600)
            verify_runtime(release, candidate_config, arguments.environment,
                           arguments.credential_environment, candidate_override, arguments.gws)
        finally:
            candidate_override.unlink()
    except (OSError, ValueError) as exc:
        raise _UpdateSkipped("preflight_failed") from exc
    _switch(record_path, record, override, expected, active, release, candidate_config)
    return f"clockify release activated: {sha}"


def update(arguments: argparse.Namespace) -> None:
    try:
        with _file_lock(_lock_path(arguments.override)), _file_lock(_lock_path(arguments.record)):
            message = _update_locked(arguments)
    except _UpdateSkipped as exc:
        print(f"clockify release update skipped: {exc}", file=sys.stderr)
    except _OverrideChanged:
        print("clockify release update skipped: override_changed", file=sys.stderr)
    except Exception:
        # The active service must still start after an operational updater failure.
        print("clockify release update skipped: update_failed", file=sys.stderr)
    else:
        print(message)


def rollback(release: Path, sha: str, config: Path, override: Path, record_path: Path | None) -> None:
    with _file_lock(_lock_path(override)):
        expected = _override_bytes(override)
        if record_path is None:
            _activate_locked(release, sha, config, override, expected)
            return
        with _file_lock(_lock_path(record_path)):
            expected, active, _ = _selection(override)
            record = _read_record(record_path)
            binding = str(_canonical(override))
            if record.get("override", binding) != binding:
                raise ValueError("release update record override differs")
            if _rejected(record, _sha(sha)):
                raise ValueError("rollback release is rejected")
            # Validate the target before recording a deterministic rejection.
            release = _canonical(release)
            _identity(release, sha)
            config, _ = _validated_config(release, config)
            if active.name == sha:
                return
            record["override"] = binding
            _reject(record, active.name, "manual_rollback")
            _switch(record_path, record, override, expected, active, release, config)


def record_result(arguments: argparse.Namespace) -> None:
    # Units pass --override explicitly; older callers can use the persisted binding.
    initial = _read_record(arguments.record)
    override = arguments.override or Path(initial.get(
        "override", str(Path.home() / ".config/serenichron/clockify-review-cycle-override.env")
    ))
    # Every record writer holds the record lock; the cycle sandbox cannot write the override lock.
    with _file_lock(_lock_path(arguments.record)):
        record = _read_record(arguments.record)
        binding = str(_canonical(override, must_exist=False))
        if record.get("override", binding) != binding:
            raise ValueError("release update record override differs")
        sha = _release_sha(_canonical(arguments.release))
        status = arguments.exit_status
        result = {
            "sha": sha, "service_result": arguments.service_result,
            "exit_code": arguments.exit_code,
            "exit_status": int(status) if status.isdecimal() else status, "at": _now(),
        }
        record["override"] = binding
        record["results"] = (record["results"] + [result])[-50:]
        if _healthy(result):
            if sha not in record["proven"]:
                record["proven"].append(sha)
            if sha == record.get("current"):
                record["health"] = "proven"
        _write_record(arguments.record, record)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("materialize")
    create.add_argument("--source-repository", type=Path, required=True)
    create.add_argument("--releases-root", type=Path, required=True)
    create.add_argument("--sha", required=True)
    for name in ("activate", "rollback"):
        select = commands.add_parser(name)
        select.add_argument("--release", type=Path, required=True)
        select.add_argument("--sha", required=True)
        select.add_argument("--config", type=Path, required=True)
        select.add_argument("--override", type=Path, required=True)
        if name == "rollback":
            select.add_argument("--record", type=Path)
    refresh = commands.add_parser("update")
    refresh.add_argument("--source-repository", type=Path, required=True)
    refresh.add_argument("--remote", default="origin")
    refresh.add_argument("--branch", default="master")
    refresh.add_argument("--releases-root", type=Path, required=True)
    refresh.add_argument("--override", type=Path, required=True)
    refresh.add_argument("--environment", type=Path, required=True)
    refresh.add_argument("--credential-environment", type=Path, required=True)
    refresh.add_argument("--gws", type=Path, required=True)
    refresh.add_argument("--record", type=Path, required=True)
    refresh.add_argument("--clear-rejected", type=_sha)
    result = commands.add_parser("record-result")
    result.add_argument("--record", type=Path, required=True)
    result.add_argument("--release", type=Path, required=True)
    result.add_argument("--override", type=Path)
    result.add_argument("--service-result", default="")
    result.add_argument("--exit-code", default="")
    result.add_argument("--exit-status", default="")
    bootstrap = commands.add_parser("bootstrap-ledgers")
    bootstrap.add_argument("--release", type=Path, required=True)
    bootstrap.add_argument("--config", type=Path, required=True)
    check = commands.add_parser("preflight")
    check.add_argument("--release", type=Path, required=True)
    check.add_argument("--config", type=Path, required=True)
    check.add_argument("--environment", type=Path, required=True)
    check.add_argument("--credential-environment", type=Path, required=True)
    check.add_argument("--override", type=Path, required=True)
    check.add_argument("--gws", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "materialize":
            print(materialize(arguments.source_repository, arguments.releases_root, arguments.sha))
        elif arguments.command == "activate":
            activate(arguments.release, arguments.sha, arguments.config, arguments.override)
        elif arguments.command == "rollback":
            rollback(arguments.release, arguments.sha, arguments.config,
                     arguments.override, arguments.record)
        elif arguments.command == "update":
            update(arguments)
        elif arguments.command == "record-result":
            record_result(arguments)
        elif arguments.command == "bootstrap-ledgers":
            bootstrap_ledgers(arguments.release, arguments.config)
        else:
            verify_runtime(
                arguments.release,
                arguments.config,
                arguments.environment,
                arguments.credential_environment,
                arguments.override,
                arguments.gws,
            )
    except (OSError, ValueError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        parser.exit(2, f"clockify user release blocked: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
