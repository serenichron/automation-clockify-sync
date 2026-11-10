#!/usr/bin/env python3
"""Inventory and conservatively retire unreferenced runs after a successful cycle."""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
import fcntl
import fnmatch
import glob
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid


# Keep release I/O and canonical-path policy identical to the existing operator tool.
spec = importlib.util.spec_from_file_location(
    "clockify_retention_release", Path(__file__).with_name("clockify_review_cycle_release.py")
)
release_tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_tool)

SCHEMA = "clockify-runs-retention/v1"
TOKENIZER_VERSION = 1
MIN_TOKEN = 8
MAX_FILE_TOKENS = 50_000
MAX_CACHE_BYTES = 256 * 1024 * 1024
MAX_TOKEN_BYTES = 16 * 1024 * 1024
TOKEN_PATTERN = re.compile(rb"[A-Za-z0-9._-]+")
NAME_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
SLICE_PATTERN = re.compile(r"^\d{8}-\d{8}-[0-9a-f]{64}-")
UNITS = (
    "clockify-review-cycle.service",
    "clockify-review-cycle-canary.service",
    "clockify-review-cycle-update.service",
    "clockify-work-accounting.service",
)
PROPERTIES = (
    "ActiveState", "InvocationID", "ExecMainStartTimestampMonotonic",
    "ExecMainExitTimestampMonotonic",
)
LIVE = Path.home() / "Work/automation-clockify-sync"
OVERRIDE_LOCK = Path.home() / ".config/serenichron/clockify-review-cycle-override.env.lock"


class RetentionAbort(ValueError):
    """An incomplete safety proof must never permit deletion."""


class DeadlineAbort(RetentionAbort):
    """Completed tokenizer work can be checkpointed after this safety abort."""


def defaults() -> dict:
    return {
        "runs_dir": str(LIVE / "runs"),
        "state_dir": str(LIVE / "state"),
        "reference_roots": [
            str(LIVE / "state"), str(LIVE / "state.json"),
            str(LIVE / "fleet.json"), str(LIVE / "fleet.precision.json"),
            str(Path.home() / "Work/_ops/clockify-review-packets"),
            str(Path.home() / "Work/automation-clockify-sync-releases"),
            str(Path.home() / ".config/systemd/user/clockify*"),
            str(Path.home() / ".config/serenichron/clockify*"),
        ],
        "optional_roots": [str(Path.home() / ".cache/clk-diag")],
        "coverage_file": os.environ.get(
            "CLOCKIFY_AUTOPILOT_COVERAGE_STATE", str(LIVE / "state/source-coverage.json")
        ),
        "min_age_days": 14, "latest_keep": 3, "mode": "dry-run",
        "max_records": 60, "deadline_seconds": 1500,
    }


def signature(details: os.stat_result) -> tuple:
    return (details.st_dev, details.st_ino, details.st_mode, details.st_uid,
            details.st_size, details.st_mtime_ns, details.st_ctime_ns)


def open_regular(path: Path, *, private: bool = False, owned: bool = False) -> int:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            raise RetentionAbort(f"not a regular file: {path}")
        if (private or owned) and details.st_uid != os.getuid():
            raise RetentionAbort(f"file is not owned by the service user: {path}")
        if private and stat.S_IMODE(details.st_mode) != 0o600:
            raise RetentionAbort(f"not a user-owned mode-0600 file: {path}")
        if signature(details) != signature(path.lstat()):
            raise RetentionAbort(f"file changed while opening: {path}")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def read_json(path: Path, *, private: bool = True):
    descriptor = open_regular(path, private=private, owned=True)
    with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
        return json.load(handle)


def load_config(path: Path) -> dict:
    config = defaults()
    try:
        supplied = read_json(release_tool._canonical(path, must_exist=False))
    except FileNotFoundError:
        supplied = {}
    if not isinstance(supplied, dict) or supplied.keys() - config.keys():
        raise RetentionAbort("config must be an object with only known keys")
    config.update(supplied)
    for key in ("min_age_days", "latest_keep", "max_records", "deadline_seconds"):
        value = config[key]
        minimum = 7 if key == "min_age_days" else 1
        if type(value) is not int or value < minimum:
            raise RetentionAbort(f"{key} must be an integer >= {minimum}")
    if config["deadline_seconds"] > 1500:
        raise RetentionAbort("deadline_seconds must fit the service budget (<= 1500)")
    if config["mode"] not in ("dry-run", "delete"):
        raise RetentionAbort("mode must be dry-run or delete")
    for key in ("runs_dir", "state_dir", "coverage_file"):
        value = config[key]
        if not isinstance(value, str) or not Path(value).expanduser().is_absolute():
            raise RetentionAbort(f"{key} must be an absolute path")
        config[key] = release_tool._canonical(Path(value), must_exist=False)
    for key in ("reference_roots", "optional_roots"):
        values = config[key]
        if not isinstance(values, list) or not all(
            isinstance(value, str) and Path(value).expanduser().is_absolute()
            for value in values
        ):
            raise RetentionAbort(f"{key} must be a list of absolute paths or globs")
    if not config["reference_roots"]:
        raise RetentionAbort("reference_roots cannot be empty")
    runs, state_dir = config["runs_dir"], config["state_dir"]
    if runs == state_dir or runs in state_dir.parents or state_dir in runs.parents:
        raise RetentionAbort("runs_dir and state_dir must be disjoint")
    return config


@contextmanager
def file_lock(path: Path, *, create: bool = False, blocking: bool = False,
              private: bool = True):
    release_tool._canonical(path, must_exist=False)
    if create:
        descriptor = os.open(
            path, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
        )
        os.close(descriptor)
    descriptor = open_regular(path, private=private, owned=True)
    try:
        flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
        fcntl.flock(descriptor, flags)
        if signature(os.fstat(descriptor)) != signature(path.lstat()):
            raise RetentionAbort(f"lock inode changed: {path}")
        yield descriptor
    finally:
        os.close(descriptor)


class Inventory:
    def __init__(self, directory: Path, max_records: int):
        self.directory = directory
        self.path = directory / "inventory.json"
        self.max_records = max_records

    def records(self) -> list:
        try:
            document = read_json(self.path)
        except FileNotFoundError:
            return []
        if (not isinstance(document, dict) or document.get("schema") != SCHEMA
                or not isinstance(document.get("records"), list)
                or not all(isinstance(item, dict) for item in document["records"])):
            raise RetentionAbort("invalid inventory history")
        return document["records"]

    def write(self, record: dict) -> None:
        with file_lock(self.directory / "inventory.lock", create=True, blocking=True):
            records = [item for item in self.records() if item.get("id") != record["id"]]
            records.append(record)
            payload = {"schema": SCHEMA, "records": records[-self.max_records:]}
            release_tool._atomic_write(
                self.path, (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode(),
                mode=0o600,
            )


def unit_markers() -> dict:
    markers = {}
    for unit in UNITS:
        result = subprocess.run(
            ["systemctl", "--user", "show", unit,
             *[f"--property={key}" for key in PROPERTIES]],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=15,
        )
        if result.returncode or result.stderr.strip():
            raise RetentionAbort(f"systemctl show failed for {unit}")
        values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        if any(key not in values for key in PROPERTIES):
            raise RetentionAbort(f"missing systemctl properties for {unit}")
        if values["ActiveState"] not in ("inactive", "failed"):
            raise RetentionAbort(f"unit is not quiescent: {unit}")
        if any(not values[key].isdigit() for key in PROPERTIES[2:]):
            raise RetentionAbort(f"invalid systemctl timestamps for {unit}")
        markers[unit] = values
    return markers


class Scan:
    def __init__(self, config: dict, own_dir: Path):
        self.config = config
        self.own_dir = own_dir
        self.start_ns = time.time_ns()
        self.start_monotonic_ns = time.monotonic_ns()
        self.deadline = time.monotonic() + config["deadline_seconds"]
        self.snapshot = {}
        self.root_sets = None
        self.cache = {}
        self.hits = 0
        self.misses = 0
        self.cache_path = own_dir / "tokens.json"
        try:
            if self.cache_path.lstat().st_size > MAX_CACHE_BYTES:
                raise ValueError("oversized token cache")
            cached = read_json(self.cache_path)
        except (FileNotFoundError, ValueError):
            cached = {}
        if (isinstance(cached, dict) and cached.get("tokenizer_version") == TOKENIZER_VERSION
                and cached.get("MIN_TOKEN") == MIN_TOKEN
                and isinstance(cached.get("entries"), dict)):
            self.cache = cached["entries"]
        self.used_cache = {}

    def check_deadline(self) -> None:
        if time.monotonic() >= self.deadline:
            raise DeadlineAbort("scan deadline exceeded")

    def skip(self, path: Path) -> bool:
        return ".git" in path.parts or path == self.own_dir or self.own_dir in path.parents

    def walk(self, root: Path, snapshot: dict, *, readable: bool,
             skip_git: bool = True) -> list:
        """Never follow links; include directories so removals and renames are visible."""
        root = release_tool._canonical(root)
        device = root.lstat().st_dev
        pending = [root]
        files = []
        while pending:
            self.check_deadline()
            path = pending.pop()
            if path == self.own_dir or self.own_dir in path.parents:
                continue
            if skip_git and ".git" in path.parts:
                continue
            details = path.lstat()
            if details.st_dev != device:
                raise RetentionAbort(f"cross-device entry: {path}")
            if not (stat.S_ISREG(details.st_mode) or stat.S_ISDIR(details.st_mode)):
                raise RetentionAbort(f"symlink or special entry: {path}")
            value = signature(details)
            if path in snapshot and snapshot[path] != value:
                raise RetentionAbort(f"entry changed during scan: {path}")
            snapshot[path] = value
            if stat.S_ISDIR(details.st_mode):
                with os.scandir(path) as entries:
                    for entry in entries:
                        self.check_deadline()
                        pending.append(path / entry.name)
            else:
                if readable:
                    descriptor = open_regular(path)
                    os.close(descriptor)
                files.append(path)
        return files

    def roots(self) -> tuple:
        resolved = []
        for key in ("reference_roots", "optional_roots"):
            paths = set()
            for expression in self.config[key]:
                self.check_deadline()
                expanded = os.path.expanduser(expression)
                # Expand explicitly so inaccessible glob parents cannot look empty.
                if glob.has_magic(expanded):
                    parent = Path(expanded).parent
                    if any(char in expanded for char in "?[") or glob.has_magic(str(parent)):
                        raise RetentionAbort("root globs may only use * in their final component")
                    release_tool._canonical(parent)
                    matches = []
                    with os.scandir(parent) as entries:
                        for entry in entries:
                            self.check_deadline()
                            if fnmatch.fnmatchcase(str(parent / entry.name), expanded):
                                matches.append(parent / entry.name)
                    if not matches and key == "reference_roots":
                        raise RetentionAbort(f"required root has no matches: {expanded}")
                    paths.update(matches)
                else:
                    path = Path(expanded)
                    try:
                        path.lstat()
                    except FileNotFoundError:
                        if key == "optional_roots":
                            continue
                        raise RetentionAbort(f"required root is absent: {path}")
                    paths.add(path)
            resolved.append(tuple(sorted(paths)))
        return tuple(resolved)

    def list_runs(self, snapshot: dict, *, readable: bool) -> tuple:
        runs = release_tool._secure_directory(self.config["runs_dir"], "runs")
        self.walk(runs, snapshot, readable=readable, skip_git=False)
        directories = {}
        top_files = []
        with os.scandir(runs) as entries:
            for entry in entries:
                self.check_deadline()
                path = runs / entry.name
                details = path.lstat()
                if stat.S_ISDIR(details.st_mode):
                    if (not NAME_PATTERN.fullmatch(entry.name) or entry.name in (".", "..")
                            or len(entry.name) < MIN_TOKEN):
                        raise RetentionAbort(f"unsupported run name: {entry.name}")
                    directories[entry.name] = path
                elif stat.S_ISREG(details.st_mode):
                    top_files.append(path)
                else:
                    raise RetentionAbort(f"unsafe top-level entry: {path}")
        return directories, top_files

    def match(self, token: bytes, index: dict) -> set:
        found = set()
        for offset in range(len(token) - MIN_TOKEN + 1):
            if offset % 65536 == 0:
                self.check_deadline()
            for name in index.get(token[offset:offset + MIN_TOKEN], ()):
                self.check_deadline()
                if token.startswith(name.encode("ascii"), offset):
                    found.add(name)
        return found

    def file_matches(self, path: Path, index: dict) -> set:
        self.check_deadline()
        descriptor = open_regular(path)
        with os.fdopen(descriptor, "rb") as handle:
            details = os.fstat(handle.fileno())
            expected = self.snapshot[path]
            if signature(details) != expected:
                raise RetentionAbort(f"file changed before reference scan: {path}")
            key = json.dumps([str(path), details.st_dev, details.st_ino,
                              details.st_size, details.st_mtime_ns, details.st_ctime_ns])
            cached = self.cache.get(key)
            cache_valid = isinstance(cached, list) and len(cached) <= MAX_FILE_TOKENS
            if cache_valid:
                for token in cached:
                    self.check_deadline()
                    if not (isinstance(token, str) and len(token) >= MIN_TOKEN
                            and NAME_PATTERN.fullmatch(token)):
                        cache_valid = False
                        break
            completed_tokens = None
            if cache_valid:
                self.hits += 1
                completed_tokens = cached
                found = set()
                for token in cached:
                    self.check_deadline()
                    found.update(self.match(token.encode("ascii"), index))
            else:
                self.misses += 1
                found = set()
                tokens = set()
                cacheable = True
                carry = b""
                while True:
                    self.check_deadline()
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        pieces = [carry] if carry else []
                    else:
                        data = carry + chunk
                        pieces = TOKEN_PATTERN.finditer(data)
                        carry = b""
                    for piece in pieces:
                        self.check_deadline()
                        if chunk:
                            token = piece.group()
                            if piece.end() == len(data):
                                carry = token
                                continue
                        else:
                            token = piece
                        if len(token) > MAX_TOKEN_BYTES:
                            raise RetentionAbort(f"token exceeds scan memory budget: {path}")
                        if len(token) < MIN_TOKEN:
                            continue
                        found.update(self.match(token, index))
                        if cacheable:
                            tokens.add(token.decode("ascii"))
                            if len(tokens) > MAX_FILE_TOKENS:
                                tokens.clear()
                                cacheable = False
                    if not chunk:
                        break
                    if len(carry) > MAX_TOKEN_BYTES:
                        raise RetentionAbort(f"token exceeds scan memory budget: {path}")
                if cacheable:
                    completed_tokens = sorted(tokens)
            if signature(os.fstat(handle.fileno())) != expected or signature(path.lstat()) != expected:
                raise RetentionAbort(f"file changed during reference scan: {path}")
            if completed_tokens is not None:
                self.used_cache[key] = completed_tokens
            return found

    def save_cache(self, *, partial: bool = False) -> None:
        # An incomplete pass must also preserve entries it has not reached yet.
        entries = self.cache.copy() if partial else {}
        entries.update(self.used_cache)
        sizes = {key: len(json.dumps(key)) + len(json.dumps(value)) + 2
                 for key, value in entries.items()}
        total = sum(sizes.values()) + 1024
        for key in sorted(sizes, key=sizes.get, reverse=True):
            if total <= MAX_CACHE_BYTES:
                break
            total -= sizes[key]
            del entries[key]
        payload = {"tokenizer_version": TOKENIZER_VERSION, "MIN_TOKEN": MIN_TOKEN,
                   "entries": entries}
        release_tool._atomic_write(
            self.cache_path, (json.dumps(payload, separators=(",", ":")) + "\n").encode(),
            mode=0o600,
        )

    def index_runs(self, snapshot: dict) -> dict:
        """Group run metadata and reference sources in one snapshot pass."""
        runs = self.config["runs_dir"]
        prefix = runs.parts
        indexed = {}
        for entry, value in snapshot.items():
            self.check_deadline()
            parts = entry.parts
            if len(parts) <= len(prefix) or parts[:len(prefix)] != prefix:
                continue
            path = runs / parts[len(prefix)]
            if not stat.S_ISDIR(snapshot[path][2]):
                continue
            entries, files = indexed.setdefault(path, ([], []))
            entries.append(value)
            if entry != path and stat.S_ISREG(value[2]) and not self.skip(entry):
                files.append(entry)
        return indexed

    def run_info(self, path: Path, entries: list) -> dict:
        if not entries:
            raise RetentionAbort(f"run missing from snapshot: {path}")
        size = 0
        newest = entries[0][5]
        for value in entries:
            self.check_deadline()
            if stat.S_ISREG(value[2]):
                size += value[4]
            newest = max(newest, value[5])
        return {"name": path.name, "bytes": size, "newest_mtime_ns": newest}

    def scan(self) -> dict:
        self.markers = unit_markers()
        self.root_sets = self.roots()
        root_files = set()
        for roots in self.root_sets:
            for root in roots:
                self.check_deadline()
                root_files.update(self.walk(root, self.snapshot, readable=True))
        coverage = self.config["coverage_file"]
        try:
            coverage.lstat()
        except FileNotFoundError:
            coverage_exists = False
        else:
            root_files.update(self.walk(coverage, self.snapshot, readable=True))
            if not stat.S_ISREG(coverage.lstat().st_mode):
                raise RetentionAbort("coverage_file must be a regular file")
            coverage_exists = True
        self.coverage_exists = coverage_exists
        directories, top_files = self.list_runs(self.snapshot, readable=True)
        self.directories = directories
        self.run_index = self.index_runs(self.snapshot)
        self.info = {}
        index = {}
        for name, path in directories.items():
            self.check_deadline()
            self.info[name] = self.run_info(path, self.run_index[path][0])
            index.setdefault(name[:MIN_TOKEN].encode("ascii"), []).append(name)
        direct = set()
        for path in sorted(root_files | set(top_files)):
            self.check_deadline()
            direct.update(self.file_matches(path, index))
        cutoff = self.start_ns - self.config["min_age_days"] * 86400 * 1_000_000_000
        young = set()
        held = {f"H{number}": set() for number in range(1, 6)}
        for name, path in directories.items():
            self.check_deadline()
            if self.info[name]["newest_mtime_ns"] > cutoff:
                young.add(name)
            if (path / "autopilot-result.json") in self.snapshot:
                held["H1"].add(name)
            if SLICE_PATTERN.match(name) and (path / "completion-bundle.json") not in self.snapshot:
                held["H2"].add(name)
            if name.startswith("collector-derivation-"):
                held["H3"].add(name)
            if not coverage_exists and (path / "run-report.json") in self.snapshot:
                held["H5"].add(name)
        held["H4"].update(sorted(directories)[-self.config["latest_keep"]:])
        retained = direct | young | set().union(*held.values())
        pending = retained.copy()
        rounds = 0
        while pending:
            self.check_deadline()
            rounds += 1
            discovered = set()
            for name in sorted(pending):
                self.check_deadline()
                path = directories[name]
                for entry in self.run_index[path][1]:
                    self.check_deadline()
                    discovered.update(self.file_matches(entry, index))
            pending = discovered - retained
            retained.update(pending)
        eligible = set(directories) - retained
        # Every directory was recursively validated, including eligible ones.
        def summary(names):
            size = 0
            for name in names:
                self.check_deadline()
                size += self.info[name]["bytes"]
            return {"count": len(names), "bytes": size}
        self.eligible = sorted(eligible)
        self.candidates = []
        for name in self.eligible:
            self.check_deadline()
            self.candidates.append(self.info[name])
        top = []
        top_bytes = 0
        for path in sorted(top_files):
            self.check_deadline()
            size = self.snapshot[path][4]
            top.append({"name": path.name, "bytes": size})
            top_bytes += size
        return {
            "scan_start": {"wall_time_ns": self.start_ns, "monotonic_ns": self.start_monotonic_ns},
            "unit_markers": self.markers, "all": summary(set(directories)),
            "top_level_files": {"count": len(top), "bytes": top_bytes, "files": top},
            "direct": summary(direct), "young": summary(young),
            "held": {rule: summary(names) for rule, names in held.items()},
            "after_closure": {**summary(retained), "rounds": rounds},
            "unreferenced": summary(set(directories) - retained), "eligible": summary(eligible),
        }

    def revalidate(self) -> None:
        self.check_deadline()
        if unit_markers() != self.markers:
            raise RetentionAbort("unit markers changed since scan start")
        if self.roots() != self.root_sets:
            raise RetentionAbort("reference root set changed since scan start")
        snapshot = {}
        for roots in self.root_sets:
            for root in roots:
                self.check_deadline()
                self.walk(root, snapshot, readable=False)
        coverage = self.config["coverage_file"]
        if os.path.lexists(coverage) != self.coverage_exists:
            raise RetentionAbort("coverage file presence changed")
        if self.coverage_exists:
            self.walk(coverage, snapshot, readable=False)
        directories, _ = self.list_runs(snapshot, readable=False)
        if len(snapshot) != len(self.snapshot) or directories != self.directories:
            raise RetentionAbort("filesystem snapshot changed since scan start")
        for path, value in snapshot.items():
            self.check_deadline()
            if value != self.snapshot.get(path):
                raise RetentionAbort("filesystem snapshot changed since scan start")
        for value in snapshot.values():
            self.check_deadline()
            if value[5] >= self.start_ns or value[6] >= self.start_ns:
                raise RetentionAbort("filesystem timestamp is at or after scan start")
        indexed = self.index_runs(snapshot)
        for name in self.eligible:
            self.check_deadline()
            path = directories[name]
            if self.run_info(path, indexed[path][0]) != self.info[name]:
                raise RetentionAbort(f"candidate age or size changed: {name}")
        if unit_markers() != self.markers:
            raise RetentionAbort("unit markers changed during final validation")
        self.check_deadline()


def delete_candidates(scan: Scan, record: dict) -> None:
    if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
        raise RetentionAbort("fd-based shutil.rmtree is unavailable")
    with ExitStack() as stack:
        # Match updater ordering, then exclude cycle/canary/legacy writers.
        locks = (
            (OVERRIDE_LOCK, True),
            (scan.config["state_dir"] / "release-updates.json.lock", True),
            (scan.config["state_dir"] / "autopilot-runner.lock", False),
        )
        descriptors = []
        for path, private in locks:
            descriptor = stack.enter_context(file_lock(path, private=private))
            descriptors.append((path, descriptor))
        scan.revalidate()
        for path, descriptor in descriptors:
            if signature(os.fstat(descriptor)) != signature(path.lstat()):
                raise RetentionAbort(f"lock path was replaced: {path}")
        runs = scan.config["runs_dir"]
        descriptor = os.open(runs, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            if signature(os.fstat(descriptor)) != scan.snapshot[runs]:
                raise RetentionAbort("runs directory changed before deletion")
            for candidate in scan.candidates:
                scan.check_deadline()
                name = candidate["name"]
                details = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if signature(details) != scan.snapshot[runs / name]:
                    raise RetentionAbort(f"candidate changed before deletion: {name}")
                try:
                    record["deletion_started"] = True
                    shutil.rmtree(name, dir_fd=descriptor)
                except (OSError, ValueError) as exc:
                    # The failing candidate can itself be partially removed.
                    record["outcome"] = "partial-delete-failure"
                    record["reason"] = str(exc)
                    record["possibly_partial_candidate"] = name
                    return
                record["deleted"].append(candidate)
                record["remaining_candidates"].pop(0)
        finally:
            os.close(descriptor)
    record["outcome"] = "deleted"


def post_totals(scan: Scan, record: dict) -> None:
    """Count the actual surviving files, including a partially removed candidate."""
    # Decisions and deletions are complete. This pass only walks metadata.
    scan.deadline = float("inf")
    snapshot = {}
    directories, top_files = scan.list_runs(snapshot, readable=False)
    size = 0
    for value in snapshot.values():
        scan.check_deadline()
        if stat.S_ISREG(value[2]):
            size += value[4]
    record["post_pass"] = {
        "directories": len(directories), "top_level_files": len(top_files),
        "bytes": size,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path,
                        default=Path.home() / ".config/serenichron/clockify-runs-retention.json")
    parser.add_argument("--release", type=Path, required=True)
    arguments = parser.parse_args()
    os.umask(0o077)
    started = time.monotonic()
    record = {
        "id": uuid.uuid4().hex, "time": release_tool._now(),
        "schema": SCHEMA, "tokenizer_version": TOKENIZER_VERSION, "MIN_TOKEN": MIN_TOKEN,
        "release_sha": None, "mode": "dry-run", "effective_mode": "dry-run",
        "outcome": "aborted-before-delete", "reason": "", "deleted": [],
        "remaining_candidates": [],
    }
    inventory = None
    scan = None
    deadline_abort = False
    with ExitStack() as stack:
        try:
            config = load_config(arguments.config)
            record["mode"] = config["mode"]
            state_dir = release_tool._secure_directory(config["state_dir"], "state")
            own_dir = state_dir / "runs-retention"
            own_dir.mkdir(mode=0o700, exist_ok=True)
            release_tool._secure_directory(own_dir, "retention state", exact_mode=0o700)
            inventory = Inventory(own_dir, config["max_records"])
            # Separate from producer locks; Phase A never blocks a cycle or updater.
            stack.enter_context(file_lock(own_dir / "retention.lock", create=True))
            release = release_tool._canonical(arguments.release)
            record["release_sha"] = release_tool._release_sha(release)
            expected_script = release / "ops/systemd/user/clockify_runs_retention.py"
            if release_tool._canonical(Path(__file__)) != expected_script:
                raise RetentionAbort("executed script does not belong to the declared release")
            history = inventory.records()
            gate = any(
                item.get("outcome") == "dry-run"
                and item.get("effective_mode") == "dry-run"
                and item.get("release_sha") == record["release_sha"]
                and item.get("schema") == SCHEMA
                and item.get("tokenizer_version") == TOKENIZER_VERSION
                and item.get("MIN_TOKEN") == MIN_TOKEN
                for item in history
            )
            scan = Scan(config, own_dir)
            record.update(scan.scan())
            record["scan_duration_seconds"] = (
                time.monotonic_ns() - scan.start_monotonic_ns
            ) / 1_000_000_000
            record["remaining_candidates"] = scan.candidates.copy()
            scan.check_deadline()
            if config["mode"] == "delete" and gate:
                record["effective_mode"] = "delete"
            elif config["mode"] == "delete":
                record["reason"] = "same-release successful dry-run required"
            # Durable intent precedes shared locks and the first destructive operation.
            record["outcome"] = "started"
            inventory.write(record)
            if not scan.eligible:
                record["outcome"] = "nothing-eligible"
            elif record["effective_mode"] == "dry-run":
                record["outcome"] = "dry-run"
            else:
                delete_candidates(scan, record)
            post_totals(scan, record)
            # Cache failures must not obscure an irreversible deletion outcome.
            try:
                scan.save_cache()
            except (OSError, ValueError) as exc:
                record["cache_error"] = str(exc)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            deadline_abort = isinstance(exc, DeadlineAbort)
            if record.get("deletion_started"):
                record["outcome"] = "partial-delete-failure"
            elif record["outcome"] not in ("deleted", "partial-delete-failure"):
                record["outcome"] = "aborted-before-delete"
            record["reason"] = str(exc)
        record.setdefault("scan_duration_seconds", (
            (time.monotonic_ns() - scan.start_monotonic_ns) / 1_000_000_000 if scan else 0
        ))
        record["duration_seconds"] = time.monotonic() - started
        hits, misses = (scan.hits, scan.misses) if scan else (0, 0)
        record["cache"] = {"hits": hits, "misses": misses,
                           "hit_ratio": hits / (hits + misses) if hits + misses else 0}
        record["deleted_summary"] = {
            "count": len(record["deleted"]),
            "bytes": sum(item["bytes"] for item in record["deleted"]),
        }
        try:
            if inventory is None:
                # Even malformed configuration is recorded at the fixed default location.
                fallback = release_tool._secure_directory(LIVE / "state", "default state")
                own_dir = fallback / "runs-retention"
                own_dir.mkdir(mode=0o700, exist_ok=True)
                release_tool._secure_directory(own_dir, "retention state", exact_mode=0o700)
                inventory = Inventory(own_dir, 60)
            inventory.write(record)
            if deadline_abort and scan.used_cache:
                # Persist the abort before checkpoint I/O consumes any shutdown margin.
                try:
                    scan.save_cache(partial=True)
                except (OSError, ValueError) as exc:
                    record["cache_error"] = str(exc)
                record["duration_seconds"] = time.monotonic() - started
                inventory.write(record)
        except (OSError, ValueError) as exc:
            print(f"retention inventory unavailable: {exc}", file=sys.stderr)
            return 1
    print(f"retention {record['outcome']}: {record['reason']}")
    return 0 if record["outcome"] in ("dry-run", "nothing-eligible", "deleted") else 1


if __name__ == "__main__":
    raise SystemExit(main())
