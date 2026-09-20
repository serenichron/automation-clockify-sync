#!/usr/bin/env python3
"""Materialize and atomically select an immutable user-service release."""
from __future__ import annotations

import argparse
from io import BytesIO
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
from typing import Sequence


IDENTITY_NAME = ".clockify-release.json"
IDENTITY_SCHEMA = "clockify-user-release/v1"
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")


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
    if document.get("routing") != str(release / "routing.json"):
        raise ValueError("review-cycle config routing does not match release")
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


def activate(release: Path, sha: str, config: Path, override: Path) -> None:
    """Atomically point both runtime root and config at one verified release."""
    sha = _sha(sha)
    release = _canonical(release)
    _identity(release, sha)
    config, _ = _validated_config(release, config)
    payload = (
        f"CLOCKIFY_AUTOPILOT_ROOT={release}\n"
        f"CLOCKIFY_REVIEW_CYCLE_CONFIG={config}\n"
    ).encode("utf-8")
    _atomic_write(override.expanduser().absolute(), payload, mode=0o600)


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
        elif arguments.command in {"activate", "rollback"}:
            activate(arguments.release, arguments.sha, arguments.config, arguments.override)
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
