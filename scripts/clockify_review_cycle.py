#!/usr/bin/env python3
"""Durable, bounded coordinator for closed-day Clockify review recovery.

This module deliberately owns only local state and child orchestration.  The
review runner remains the sole collector/analyzer; the sheet publisher remains
the sole Sheets writer.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import importlib.util
import json
import os
from contextlib import contextmanager
from pathlib import Path
import stat
import string
import subprocess
import sys
from typing import Any, Iterator, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from scripts.autopilot_process import ChildTimeoutConfig, run_child_bounded
    from scripts import (
        clockify_review_run,
        clockify_source_debt_recover,
        collector_receipts,
        collector_slices,
        reconciliation_manifest,
        semantic_analyzer,
        source_coverage,
    )
    from scripts.clockify_sheet_publish import (
        _publication_receipt,
        project_allowlist,
        proposal_row,
        stable_review_id,
    )
except ModuleNotFoundError:  # pragma: no cover
    from autopilot_process import ChildTimeoutConfig, run_child_bounded  # type: ignore[no-redef]
    import clockify_review_run  # type: ignore[no-redef]
    import clockify_source_debt_recover  # type: ignore[no-redef]
    import collector_receipts  # type: ignore[no-redef]
    import collector_slices  # type: ignore[no-redef]
    import reconciliation_manifest  # type: ignore[no-redef]
    import semantic_analyzer  # type: ignore[no-redef]
    import source_coverage  # type: ignore[no-redef]
    from clockify_sheet_publish import (  # type: ignore[no-redef]
        _publication_receipt,
        project_allowlist,
        proposal_row,
        stable_review_id,
    )


SCHEMA_VERSION = "clockify-review-cycle/v1"
RECEIPT_SCHEMA_VERSION = "clockify-review-delivery/v1"
HISTORICAL_ADOPTION_SCHEMA_VERSION = "clockify-historical-adoption/v1"
HISTORICAL_ADOPTION_REQUEST_SCHEMA_VERSION = "clockify-historical-adoption-request/v1"
DERIVED_ADOPTION_SCHEMA_VERSION = "clockify-historical-adoption/v2"
DERIVED_ADOPTION_REQUEST_SCHEMA_VERSION = "clockify-historical-adoption-request/v2"
GENERIC_COMPATIBILITY_VERSION = "runner-unclassified/v1"
GENERIC_RETRY_LIMIT = 2
EXACT_RETRY_LIMIT = 2
RECOVERY_ATTEMPT_SCHEMA_VERSION = "review-cycle-source-recovery-attempt/v1"
HEALTH_PROBE_TIMEOUT_SECONDS = 3
HEALTH_PROBE_BUDGET_SECONDS = 9
SOURCE_INTERVAL_COVERAGE_AUDIT_SCHEMA_VERSION = "source-interval-coverage-audit/v1"
SOURCE_INTERVAL_FIELDS = (
    "source", "since_utc", "until_utc", "slice_id", "compatibility_version",
)
_HEALTH_SSH_OPTIONS = frozenset({
    "batchmode", "connectionattempts", "connecttimeout", "controlmaster",
    "controlpath", "controlpersist", "identitiesonly", "identityfile",
    "loglevel", "serveralivecountmax", "serveraliveinterval",
    "stricthostkeychecking", "userknownhostsfile",
})
_MONTH_NAMES = (
    "", "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
_REQUIRED = frozenset({
    "root", "state_dir", "cache", "routing", "corrections", "acceptance",
    "workspace_id", "member_id", "recovery_since", "timezone", "spreadsheet_id",
    "monthly_sheet_title_template", "calendly_optional",
})


class CycleError(RuntimeError):
    pass


class _QualityBlocked(CycleError):
    """A verified collector derivation reached only the quality gate."""


class _BudgetExhausted(RuntimeError):
    pass


def _date(value: object, field: str) -> dt.date:
    if not isinstance(value, str):
        raise CycleError(f"{field} must be YYYY-MM-DD")
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise CycleError(f"{field} must be YYYY-MM-DD") from exc


def _path(config: Mapping[str, Any], key: str, *, file: bool = False) -> Path:
    value = config.get(key)
    if not isinstance(value, str) or not value:
        raise CycleError(f"{key} must be an absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise CycleError(f"{key} must be an absolute path")
    path = path.resolve()
    if file and (not path.is_file() or path.is_symlink()):
        raise CycleError(f"{key} must be a regular file")
    return path


def _runs_dir(config: Mapping[str, Any]) -> Path:
    """Use the explicit operational root, preserving legacy root/runs configs."""
    raw = config.get("runs_dir")
    if raw is None:
        root = config.get("root")
        if not isinstance(root, str) or not root:
            raise CycleError("root must be an absolute path")
        requested_root = Path(root)
        if not requested_root.is_absolute() or requested_root != requested_root.resolve():
            raise CycleError("root must be canonical and contain no symlink components")
        requested = requested_root / "runs"
    else:
        requested = Path(str(raw))
    if not requested.is_absolute():
        raise CycleError("runs_dir must be an absolute path")
    resolved = requested.resolve()
    if requested != resolved:
        raise CycleError("runs_dir must be canonical and contain no symlink components")
    if resolved.exists() and (not resolved.is_dir() or resolved.is_symlink()):
        raise CycleError("runs_dir must be a safe directory")
    return resolved


def _canonical_runtime_path(raw: str | Path, *, label: str) -> Path:
    requested = Path(raw)
    if not requested.is_absolute():
        raise CycleError(f"{label} must be absolute")
    resolved = requested.resolve()
    if requested != resolved:
        raise CycleError(f"{label} must be canonical and contain no symlink components")
    return resolved


def _collector_checkpoint_root(
    config: Mapping[str, Any], environment: Mapping[str, str]
) -> Path:
    """Bind collector provenance to one canonical durable state subtree."""
    raw_state = config.get("state_dir")
    if not isinstance(raw_state, str) or not raw_state:
        raise CycleError("state_dir must be an absolute path")
    state_dir = Path(raw_state)
    if not state_dir.is_absolute() or state_dir != state_dir.resolve():
        raise CycleError("collector checkpoint root requires a canonical state_dir")
    if state_dir.exists() and (state_dir.is_symlink() or not state_dir.is_dir()):
        raise CycleError("collector checkpoint root requires a safe state_dir")
    raw_override = str(
        environment.get("CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT") or ""
    ).strip()
    requested = Path(raw_override) if raw_override else state_dir / "collector-checkpoints"
    if not requested.is_absolute():
        raise CycleError("collector checkpoint root must be absolute")
    resolved = requested.resolve()
    if requested != resolved:
        raise CycleError(
            "collector checkpoint root must be canonical and contain no symlink components"
        )
    try:
        resolved.relative_to(state_dir)
    except ValueError as exc:
        raise CycleError("collector checkpoint root must be within state_dir") from exc
    if resolved.exists():
        if resolved.is_symlink() or not resolved.is_dir():
            raise CycleError("collector checkpoint root must be a safe directory")
        details = resolved.stat()
        if details.st_uid != os.getuid():
            raise CycleError("collector checkpoint root must be owned by the service user")
        if stat.S_IMODE(details.st_mode) != 0o700:
            raise CycleError("collector checkpoint root must have exact mode 0700")
    elif raw_override:
        parent = resolved.parent
        if parent.is_symlink() or not parent.is_dir():
            raise CycleError(
                "collector checkpoint root parent must be an existing nonsymlink directory"
            )
        details = parent.stat()
        if details.st_uid != os.getuid():
            raise CycleError(
                "collector checkpoint root parent must be owned by the service user"
            )
        if stat.S_IMODE(details.st_mode) != 0o700:
            raise CycleError(
                "collector checkpoint root parent must have exact mode 0700"
            )
    return resolved


def _ensure_collector_checkpoint_root(path: Path) -> None:
    created = False
    try:
        path.mkdir(mode=0o700)
        created = True
    except FileExistsError:
        pass
    if path.is_symlink() or not path.is_dir():
        raise CycleError("collector checkpoint root must be a safe directory")
    details = path.stat()
    if details.st_uid != os.getuid():
        raise CycleError("collector checkpoint root must be owned by the service user")
    if stat.S_IMODE(details.st_mode) != 0o700:
        raise CycleError("collector checkpoint root must have exact mode 0700")
    if created:
        for directory in (path, path.parent):
            descriptor = os.open(
                directory,
                os.O_RDONLY
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)


def _validate_runtime_root(
    config: Mapping[str, Any],
    environment: Mapping[str, str],
    script_path: Path = Path(__file__),
) -> Path:
    """Require script, environment, and config to identify one exact checkout."""
    script = _canonical_runtime_path(script_path, label="runtime root script")
    if not script.is_file():
        raise CycleError("runtime root script is unavailable")
    checkout = script.parent.parent
    raw_environment = str(environment.get("CLOCKIFY_AUTOPILOT_ROOT") or "").strip()
    if not raw_environment:
        raise CycleError("runtime root CLOCKIFY_AUTOPILOT_ROOT is required")
    environment_root = _canonical_runtime_path(raw_environment, label="runtime root")
    config_root = _canonical_runtime_path(
        str(config.get("root") or ""), label="runtime root"
    )
    if not environment_root.is_dir() or not config_root.is_dir() or not (
        checkout == environment_root == config_root
    ):
        raise CycleError(
            "runtime root must exactly match the checkout containing the running script"
        )
    return checkout


def load_config(path: Path) -> dict[str, Any]:
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CycleError("config must be valid JSON") from exc
    if not isinstance(config, dict) or not _REQUIRED.issubset(config):
        raise CycleError("config is missing required review-cycle fields")
    if set(config) - (_REQUIRED | {"runs_dir", "catchup_until", "max_slices", "total_child_budget_seconds"}):
        raise CycleError("config contains unsupported review-cycle fields")
    if not isinstance(config["calendly_optional"], bool):
        raise CycleError("calendly_optional must be boolean")
    try:
        ZoneInfo(str(config["timezone"]))
    except ZoneInfoNotFoundError as exc:
        raise CycleError("timezone must name an available IANA timezone") from exc
    _date(config["recovery_since"], "recovery_since")
    if config.get("catchup_until") is not None:
        _date(config["catchup_until"], "catchup_until")
    maximum = config.get("max_slices", 1)
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
        raise CycleError("max_slices must be a positive integer")
    total_budget = config.get("total_child_budget_seconds", 7200)
    if isinstance(total_budget, bool) or not isinstance(total_budget, int) or total_budget < 1:
        raise CycleError("total_child_budget_seconds must be a positive integer")
    for key in ("root", "state_dir", "cache"):
        _path(config, key)
    _collector_checkpoint_root(config, os.environ)
    _runs_dir(config)
    for key in ("routing", "corrections", "acceptance"):
        _path(config, key, file=True)
    for key in ("workspace_id", "member_id", "spreadsheet_id", "monthly_sheet_title_template"):
        if not isinstance(config[key], str) or not config[key].strip():
            raise CycleError(f"{key} must be non-empty")
    _sheet_title(config["monthly_sheet_title_template"], since=config["recovery_since"])
    return config


def _atomic(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + ".tmp")
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        payload = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")
        offset = 0
        while offset < len(payload):
            written = os.write(descriptor, payload[offset:])
            if written <= 0:
                raise OSError("atomic write made no progress")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    parent = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent)
    finally:
        os.close(parent)


def _state(path: Path, *, recovery_since: str | None = None) -> dict[str, Any]:
    if not path.exists():
        return {
            "schema_version": SCHEMA_VERSION,
            "completed_through": None,
            "scheduled_through": recovery_since,
            "next_work_class": "routine",
            "slices": {},
        }
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CycleError("cycle state is unreadable") from exc
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION or not isinstance(value.get("slices"), dict):
        raise CycleError("cycle state is invalid")
    if "scheduled_through" not in value:
        if recovery_since is None:
            raise CycleError("legacy cycle state requires recovery_since migration input")
        value["scheduled_through"] = value.get("completed_through") or recovery_since
    if value["scheduled_through"] is not None:
        _date(value["scheduled_through"], "scheduled_through")
    if "next_work_class" not in value:
        value["next_work_class"] = "routine"
    if value["next_work_class"] not in {"routine", "exact"}:
        raise CycleError("cycle state next_work_class is invalid")
    return value


@contextmanager
def single_instance(path: Path) -> Iterator[bool]:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def select_slices(config: Mapping[str, Any], state: Mapping[str, Any], *, today: dt.date) -> list[tuple[str, str]]:
    """Select closed local-date slices, never crossing a month or two-day bound."""
    slices = state.get("slices", {})
    if not isinstance(slices, Mapping):
        raise CycleError("cycle state slices are invalid")
    start = _date(
        state.get("scheduled_through")
        or state.get("completed_through")
        or config["recovery_since"],
        "scheduled_through",
    )
    limit = _date(config.get("catchup_until") or today.isoformat(), "catchup_until")
    limit = min(limit, today)
    maximum = int(config.get("max_slices", 1))
    planned: list[tuple[str, str]] = []
    while start < limit and len(planned) < maximum:
        month_end = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
        end = min(start + dt.timedelta(days=2), month_end, limit)
        planned.append((start.isoformat(), end.isoformat()))
        start = end
    return planned


def _source_debt(path: Path) -> tuple[source_coverage.SourceDebtStore, list[dict[str, str]]]:
    document = source_coverage.read(path)
    try:
        store = source_coverage.SourceDebtStore.from_document(document)
    except ValueError as exc:  # read() should already have conservatively migrated it.
        raise CycleError("source coverage state is invalid") from exc
    warnings = document.get("migration_warnings", [])
    return store, [dict(item) for item in warnings if isinstance(item, Mapping)]


def _health_ssh_options(raw: object) -> list[str]:
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError("health SSH options must be strings")
    checked: list[str] = []
    config_count = 0
    index = 0
    while index < len(raw):
        option = raw[index]
        if option == "-F":
            if index + 1 >= len(raw) or raw[index + 1] != "/dev/null":
                raise ValueError("health SSH config must be /dev/null")
            config_count += 1
            if config_count != 1:
                raise ValueError("health SSH config must appear exactly once")
            checked.extend((option, raw[index + 1]))
            index += 2
            continue
        if option == "-o":
            if index + 1 >= len(raw):
                raise ValueError("health SSH option value is missing")
            value = raw[index + 1]
            index += 2
        elif option.startswith("-o") and len(option) > 2:
            value = option[2:]
            index += 1
        else:
            raise ValueError("health SSH option is not allowlisted")
        key, separator, setting = value.partition("=")
        if (
            separator != "=" or key.casefold() not in _HEALTH_SSH_OPTIONS
            or not setting or any(character in setting for character in "\r\n\x00")
        ):
            raise ValueError("health SSH option is not allowlisted")
        checked.extend(("-o", value))
    if config_count != 1:
        raise ValueError("health SSH config must appear exactly once")
    return checked


def _probe_source_health(
    config: Mapping[str, Any], source: str, *,
    timeout_seconds: int = HEALTH_PROBE_TIMEOUT_SECONDS,
) -> str:
    """Return a bounded non-private peer health state without collecting evidence."""
    _category, separator, machine_name = source.partition("/")
    if separator != "/" or not machine_name:
        return "offline"
    try:
        fleet = _json_file(_path(config, "root") / "fleet.json", "fleet")
        machines = fleet.get("machines") if isinstance(fleet, Mapping) else None
        machine = next(
            item for item in machines or ()
            if isinstance(item, Mapping) and item.get("name") == machine_name
            and item.get("enabled", True)
        )
        if machine.get("kind") == "local":
            return "online"
        host = str(machine.get("host") or "").strip()
        options = _health_ssh_options(fleet.get("ssh_options", []))
        if (
            not host or host.startswith("-")
            or any(character.isspace() or character in "\r\n\x00@" for character in host)
            or isinstance(timeout_seconds, bool) or timeout_seconds <= 0
        ):
            return "offline"
        checked = subprocess.run(
            ["ssh", *options, host, "true"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=timeout_seconds, check=False,
        )
        return "online" if checked.returncode == 0 else "offline"
    except (CycleError, OSError, StopIteration, subprocess.SubprocessError, ValueError):
        return "offline"


def _reactivate_health_transitions(
    config: Mapping[str, Any], state: dict[str, Any],
    store: source_coverage.SourceDebtStore,
) -> bool:
    """Reactivate exhausted exact debt once for each offline-to-online epoch."""
    raw_epochs = state.get("source_health_epochs", {})
    if not isinstance(raw_epochs, Mapping) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in raw_epochs.items()
    ):
        raise CycleError("source health epochs are invalid")
    epochs = dict(raw_epochs)
    changed = False
    items = sorted(store.active(), key=lambda debt: debt.debt_id)
    for item in items:
        previous = epochs.get(item.debt_id, "offline:0")
        if previous in {"offline", "online"}:
            previous_health, epoch = previous, 0
        else:
            previous_health, separator, raw_epoch = previous.partition(":")
            if (
                separator != ":" or previous_health not in {"offline", "online"}
                or not raw_epoch.isdigit()
            ):
                raise CycleError("source health epoch value is invalid")
            epoch = int(raw_epoch)
        transition_digest = _value_digest({
            "contract": "source-health-epoch/v1",
            "source": item.interval.source,
            "health": "online",
            "epoch": epoch,
        })
        if (
            item.failure_class == "source_health_transition"
            and item.resume_state_digest == transition_digest
            and previous_health == "offline"
        ):
            epochs[item.debt_id] = f"online:{epoch}"
            changed = True

    by_machine: dict[str, list[source_coverage.DebtItem]] = {}
    for item in items:
        if item.status != "exhausted" or item.interval.source == "runner/unclassified":
            continue
        _category, separator, machine = item.interval.source.partition("/")
        if separator == "/" and machine:
            by_machine.setdefault(machine, []).append(item)
    maximum_probes = HEALTH_PROBE_BUDGET_SECONDS // HEALTH_PROBE_TIMEOUT_SECONDS
    for machine in sorted(by_machine)[:maximum_probes]:
        health = _probe_source_health(
            config, f"peer/{machine}",
            timeout_seconds=HEALTH_PROBE_TIMEOUT_SECONDS,
        )
        for item in by_machine[machine]:
            previous = epochs.get(item.debt_id, "offline:0")
            if previous in {"offline", "online"}:
                previous_health, epoch = previous, 0
            else:
                previous_health, _separator, raw_epoch = previous.partition(":")
                epoch = int(raw_epoch)
            if previous_health == "online" and health == "offline":
                epoch += 1
            epochs[item.debt_id] = f"{health}:{epoch}"
            if previous_health == "offline" and health == "online":
                store.record_failure(
                    item.interval,
                    failure_class="source_health_transition",
                    retryable=True,
                    resume_state_digest=_value_digest({
                        "contract": "source-health-epoch/v1",
                        "source": item.interval.source,
                        "health": health,
                        "epoch": epoch,
                    }),
                    attempted_at=_attempted_at(),
                )
                changed = True
    if epochs != raw_epochs:
        state["source_health_epochs"] = epochs
        changed = True
    return changed


def _eligible_interval_dates(
    config: Mapping[str, Any], interval: source_coverage.SourceInterval,
    *, today: dt.date,
) -> tuple[str, str] | None:
    zone = ZoneInfo(str(config["timezone"]))
    since = dt.datetime.fromisoformat(interval.since_utc.replace("Z", "+00:00"))
    until = dt.datetime.fromisoformat(interval.until_utc.replace("Z", "+00:00"))
    local_since = since.astimezone(zone)
    local_until = until.astimezone(zone)
    if local_since.timetz().replace(tzinfo=None) != dt.time() or (
        local_until.timetz().replace(tzinfo=None) != dt.time()
    ):
        raise CycleError("source debt interval endpoints must be exact local midnight")
    since_day = local_since.date()
    until_day = local_until.date()
    if (until_day - since_day).days not in {1, 2}:
        raise CycleError("source debt interval must span one or two whole local days")
    expected_since, expected_until = _expected_interval(
        config, since_day.isoformat(), until_day.isoformat()
    )
    if (interval.since_utc, interval.until_utc) != (expected_since, expected_until):
        raise CycleError("source debt interval does not match configured timezone")
    lower = _date(config["recovery_since"], "recovery_since")
    upper = min(
        _date(config.get("catchup_until") or today.isoformat(), "catchup_until"),
        today,
    )
    if since_day < lower or until_day > upper:
        return None
    return since_day.isoformat(), until_day.isoformat()


def _eligible_stored_dates(
    config: Mapping[str, Any], since: str, until: str, *, today: dt.date,
) -> bool:
    since_day = _date(since, "stored slice since")
    until_day = _date(until, "stored slice until")
    if (until_day - since_day).days not in {1, 2}:
        raise CycleError("stored slice must span one or two whole local days")
    lower = _date(config["recovery_since"], "recovery_since")
    upper = min(
        _date(config.get("catchup_until") or today.isoformat(), "catchup_until"),
        today,
    )
    return since_day >= lower and until_day <= upper


def _generic_is_suppressed(
    item: source_coverage.DebtItem,
    active: tuple[source_coverage.DebtItem, ...],
) -> bool:
    interval = item.interval
    return any(
        other.interval.source != "runner/unclassified"
        and other.interval.since_utc == interval.since_utc
        and other.interval.until_utc == interval.until_utc
        and other.status != "resolved"
        for other in active
    )


def _runtime_transition_unlocks_generic(
    config: Mapping[str, Any], state: Mapping[str, Any],
    item: source_coverage.DebtItem, *, today: dt.date,
) -> bool:
    """Allow one historical generic classification pass under a new runtime."""
    runtime = config.get("_runtime_identity")
    if not isinstance(runtime, Mapping):
        return False
    dates = _eligible_interval_dates(config, item.interval, today=today)
    if dates is None:
        return False
    record = state.get("slices", {}).get(dates[0], {})
    source = record.get("source") if isinstance(record, Mapping) else None
    runner_attempt = _runner_attempt(record) if isinstance(record, Mapping) else None
    if runner_attempt is not None and runner_attempt["status"] == "pending":
        return True
    if source is None and isinstance(record, Mapping):
        return runner_attempt is None or (
            runner_attempt["runtime_identity_digest"] != _value_digest(dict(runtime))
        )
    return (
        isinstance(source, Mapping)
        and isinstance(source.get("runtime_identity_digest"), str)
        and source["runtime_identity_digest"] != _value_digest(dict(runtime))
    )


def _select_work(
    config: Mapping[str, Any], state: Mapping[str, Any],
    store: source_coverage.SourceDebtStore, *, today: dt.date,
) -> list[tuple[str, str, str, source_coverage.DebtItem | None]]:
    """Select bounded recovery and ordinary work with deterministic fairness."""
    maximum = int(config.get("max_slices", 1))
    now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    active = store.active()
    ordinarily_eligible = {item.debt_id for item in store.eligible(now)}
    recovery: list[tuple[str, str, str, source_coverage.DebtItem | None]] = []
    exact_intervals: set[tuple[str, str]] = set()
    for item in sorted(
        (
            candidate for candidate in active
            if candidate.interval.source != "runner/unclassified"
            and candidate.debt_id in ordinarily_eligible
        ),
        key=lambda candidate: (
            candidate.interval.since_utc,
            candidate.interval.until_utc,
            candidate.interval.source,
            candidate.debt_id,
        ),
    ):
        dates = _eligible_interval_dates(config, item.interval, today=today)
        if dates is None or dates in exact_intervals:
            continue
        since, until = dates
        record = state.get("slices", {}).get(since, {})
        if not isinstance(record, Mapping) or not isinstance(record.get("source"), Mapping):
            continue
        recovery.append((since, until, "exact", item))
        exact_intervals.add(dates)
    eligible = sorted(
        (
            item for item in active
            if item.interval.source == "runner/unclassified"
            and (
                item.debt_id in ordinarily_eligible
                or (
                    item.status == "exhausted"
                    and item.retryable
                    and item.terminal_reason == "retry_limit"
                    and _runtime_transition_unlocks_generic(
                        config, state, item, today=today
                    )
                )
            )
            and not _generic_is_suppressed(item, active)
        ),
        key=lambda item: (
            item.interval.since_utc,
            item.interval.until_utc,
            item.debt_id,
        ),
    )
    ordinary: list[tuple[str, str, str, source_coverage.DebtItem | None]] = []
    selected_intervals: set[tuple[str, str]] = set()
    for item in eligible:
        dates = _eligible_interval_dates(config, item.interval, today=today)
        if dates is None:
            continue
        since, until = dates
        identity = (since, until)
        if identity in selected_intervals:
            continue
        record = state.get("slices", {}).get(since, {})
        if isinstance(record, Mapping) and record.get("status") in {
            "delivered", "delivered_with_exceptions",
        }:
            continue
        runner_attempt = _runner_attempt(record) if isinstance(record, Mapping) else None
        selected_intervals.add(identity)
        if item.status == "exhausted" and (
            record.get("source") is None
            or runner_attempt is not None and runner_attempt["status"] == "pending"
        ):
            recovery.append((since, until, "generic_classification", item))
            continue
        if len(ordinary) < maximum:
            ordinary.append((since, until, "generic", item))
    resumable = sorted(
        (
            (since, raw)
            for since, raw in state.get("slices", {}).items()
            if isinstance(since, str)
            and isinstance(raw, Mapping)
            and raw.get("status") in {"source_verified", "replay_verified", "failed"}
            and isinstance(raw.get("source"), Mapping)
            and raw["source"].get("coverage", {}).get("status") == "complete"
            and raw["source"].get("coverage", {}).get("incomplete_sources") == []
        ),
        key=lambda item: item[0],
    )
    for since, record in resumable:
        if len(ordinary) == maximum:
            break
        until = record.get("until")
        if not isinstance(until, str):
            raise CycleError("stored slice until must be YYYY-MM-DD")
        if (since, until) in selected_intervals:
            continue
        if not _eligible_stored_dates(config, since, until, today=today):
            continue
        ordinary.append((since, until, "delivery", None))
        selected_intervals.add((since, until))
    routine_state = dict(state)
    routine_state["slices"] = state.get("slices", {})
    routine_config = {**config, "max_slices": maximum - len(ordinary)}
    for since, until in select_slices(routine_config, routine_state, today=today):
        if (since, until) in selected_intervals:
            continue
        record = state.get("slices", {}).get(since, {})
        if isinstance(record, Mapping) and record.get("status") in {
            "delivered", "delivered_with_exceptions",
        }:
            continue
        ordinary.append((since, until, "routine", None))
        selected_intervals.add((since, until))
    if not recovery:
        return ordinary[:maximum]
    if not ordinary:
        return recovery[:maximum]
    if maximum == 1:
        return recovery[:1] if state.get("next_work_class", "routine") == "exact" else ordinary[:1]
    selected = [recovery[0], ordinary[0]]
    occupied = {(item[0], item[1]) for item in selected}
    for item in [*recovery[1:], *ordinary[1:]]:
        if len(selected) == maximum:
            break
        identity = (item[0], item[1])
        if identity not in occupied:
            selected.append(item)
            occupied.add(identity)
    return selected


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _value_digest(value: object) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _sheet_title(template: object, *, since: str) -> str:
    if not isinstance(template, str) or not template:
        raise CycleError("monthly_sheet_title_template must be non-empty")
    start = _date(since, "slice since")
    allowed = {"year", "month", "month_name"}
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise CycleError("monthly_sheet_title_template is malformed") from exc
    for _literal, field, format_spec, conversion in parsed:
        if field is None:
            continue
        if field not in allowed or format_spec or conversion:
            raise CycleError(
                "monthly_sheet_title_template accepts only {year}, {month} and {month_name}"
            )
    try:
        title = template.format(
            year=start.year, month=start.month, month_name=_MONTH_NAMES[start.month]
        )
    except (KeyError, ValueError, IndexError) as exc:
        raise CycleError("monthly_sheet_title_template is malformed") from exc
    if not title.strip():
        raise CycleError("monthly_sheet_title_template produced an empty title")
    return title


def _period_identity(config: Mapping[str, Any], since: str, until: str) -> Any:
    zone = ZoneInfo(str(config["timezone"]))
    return reconciliation_manifest.PeriodIdentity(
        member_id=str(config["member_id"]),
        workspace_id=str(config["workspace_id"]),
        timezone=str(config["timezone"]),
        since=dt.datetime.combine(_date(since, "slice since"), dt.time(), zone),
        until=dt.datetime.combine(_date(until, "slice until"), dt.time(), zone),
        revision=1,
    )


def _validate_config_identity(config: Mapping[str, Any]) -> None:
    routing = _json_file(_path(config, "routing", file=True), "routing input")
    if not isinstance(routing, Mapping) or (
        routing.get("workspace_id") != config.get("workspace_id")
        or routing.get("member_id") != config.get("member_id")
    ):
        raise CycleError("configured workspace/member identity does not match routing")


def _period_paths(state_dir: Path, since: str) -> tuple[Path, Path]:
    return (
        state_dir / f"{since}.period-events.jsonl",
        state_dir / f"{since}.period-manifest.json",
    )


def _ensure_period(
    config: Mapping[str, Any], state_dir: Path, since: str, until: str, *,
    bind_inputs: bool,
) -> Path:
    if bind_inputs:
        _validate_config_identity(config)
    identity = _period_identity(config, since, until)
    events_path, manifest_path = _period_paths(state_dir, since)
    store = reconciliation_manifest.CoordinatorEventStore(events_path)
    if events_path.exists() != manifest_path.exists():
        raise CycleError("period manifest and event history must exist together")
    if not events_path.exists():
        store.append(
            identity,
            "period_opened",
            {"revision": identity.revision},
            occurred_at=dt.datetime.now(dt.timezone.utc),
        )
        derived = reconciliation_manifest.ReconciliationCoordinator(identity, store).derive()
        _atomic(manifest_path, derived.document())
        return manifest_path
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
        stored = reconciliation_manifest.ReconciliationManifest.from_document(document)
        derived = reconciliation_manifest.ReconciliationCoordinator(identity, store).derive()
    except (OSError, json.JSONDecodeError, reconciliation_manifest.ManifestError) as exc:
        raise CycleError("period manifest or append-only history is invalid") from exc
    if stored.identity.document() != identity.document() or stored.document() != derived.document():
        raise CycleError("period manifest identity or history has drifted")
    return manifest_path


def _expected_snapshot_digests(
    config: Mapping[str, Any], manifest_path: Path,
) -> dict[str, str]:
    return {
        "period-manifest.json": _digest(manifest_path),
        "routing.json": _digest(_path(config, "routing", file=True)),
        "review-corrections.jsonl": _digest(_path(config, "corrections", file=True)),
        "review-acceptance.jsonl": _digest(_path(config, "acceptance", file=True)),
    }


def _result(stdout: str, runs: Path) -> Path:
    paths = [
        _canonical_runtime_path(line.strip(), label="review child result")
        for line in stdout.splitlines() if line.strip()
    ]
    runs = runs.resolve()
    if len(paths) != 1 or paths[0].name != "autopilot-result.json" or runs not in paths[0].parents:
        raise CycleError("review child did not emit exactly one safe result path")
    if not paths[0].is_file():
        raise CycleError("review child result is missing")
    return paths[0]


def _artifact_path(result: Mapping[str, Any], name: str, runs: Path) -> Path:
    paths = result.get("paths")
    raw = paths.get(name) if isinstance(paths, Mapping) else None
    candidate = _canonical_runtime_path(str(raw), label=f"result {name}")
    runs = runs.resolve()
    if runs not in candidate.parents or not candidate.is_file():
        raise CycleError(f"result {name} path is unsafe or missing")
    return candidate


def _safe_run_file(run_dir: Path, raw: object, label: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise CycleError(f"{label} path is missing")
    candidate = _canonical_runtime_path(raw, label=f"{label} path")
    try:
        candidate.relative_to(run_dir)
    except ValueError as exc:
        raise CycleError(f"{label} path escapes its bounded run") from exc
    if not candidate.is_file():
        raise CycleError(f"{label} path is missing")
    return candidate


def _json_file(path: Path, label: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CycleError(f"{label} is not valid JSON") from exc


def _validate_accounting(run_dir: Path) -> tuple[list[dict[str, Any]], list[str]]:
    accounting_path = _safe_run_file(
        run_dir, str(run_dir / "work-accounting-result.json"), "work accounting"
    )
    proposals_path = _safe_run_file(
        run_dir, str(run_dir / "proposals.json"), "proposal artifact"
    )
    accounting = _json_file(accounting_path, "work accounting artifact")
    proposals = _json_file(proposals_path, "proposal artifact")
    if not isinstance(accounting, Mapping):
        raise CycleError("work accounting artifact must be an object")
    if accounting.get("schema_version") != 1:
        raise CycleError("work accounting schema version must be 1")
    if accounting.get("allocation_mode") != "non_overlapping_v1":
        raise CycleError("work accounting allocation mode is incompatible")
    for field in ("proposals", "ambiguous", "skipped"):
        rows = accounting.get(field)
        if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
            raise CycleError(f"work accounting {field} must be a list of objects")
    if not isinstance(proposals, list) or not all(isinstance(row, dict) for row in proposals):
        raise CycleError("proposal artifact must be a list of objects")
    if proposals != accounting["proposals"]:
        raise CycleError("proposal artifact does not exactly match work accounting")
    exception_ids: list[str] = []
    for row in accounting["ambiguous"]:
        identity = str(row.get("id") or row.get("review_id") or "").strip()
        if not identity:
            raise CycleError("ambiguous accounting row lacks a durable identity")
        exception_ids.append(identity)
    return proposals, sorted(set(exception_ids))


def _expected_interval(config: Mapping[str, Any], since: str, until: str) -> tuple[str, str]:
    identity = _period_identity(config, since, until).document()
    return str(identity["since_utc"]), str(identity["until_utc"])


def _validate_stage(
    config: Mapping[str, Any], result_path: Path, since: str, until: str, *, replay: bool,
    expected_snapshot_digests: Mapping[str, str],
    source_run_id: str | None = None, source_run_dir: str | None = None,
    allow_historical_runtime: bool = False,
    expected_runtime_digest: str | None = None,
    historical_state_validation: bool = False,
) -> dict[str, Any]:
    result_path = _safe_run_file(
        _runs_dir(config), str(result_path), "result"
    )
    if result_path.name != "autopilot-result.json":
        raise CycleError("review result filename is invalid")
    run_dir = result_path.parent
    result = _json_file(result_path, "review result")
    if not isinstance(result, Mapping) or result.get("quality_status") != "pass":
        raise _QualityBlocked("review result did not pass quality")
    if result.get("run_id") != run_dir.name or result.get("run_dir") != str(run_dir):
        raise CycleError("review result run identity does not match its path")
    expected_since, expected_until = _expected_interval(config, since, until)
    if result.get("date_range") != {"since": expected_since, "until": expected_until}:
        raise CycleError("review result interval does not match the selected slice")
    try:
        bundle = collector_receipts.load_completion_bundle(
            run_dir / "completion-bundle.json", run_dir=run_dir
        )
        collector_receipts.verify_completion_bundle(bundle)
        coverage = collector_receipts.completion_coverage(bundle)
    except collector_receipts.CollectorReceiptError as exc:
        raise CycleError("review completion bundle is invalid") from exc
    if (
        bundle.replay is not replay
        or bundle.since_utc != expected_since
        or bundle.until_utc != expected_until
        or result.get("completion_bundle_digest") != bundle.bundle_digest
        or result.get("completion_bundle") != bundle.document()
    ):
        raise CycleError("review completion bundle identity does not match the result")
    expected_runtime = config.get("_runtime_identity")
    current_runtime = True
    if expected_runtime is not None:
        if not isinstance(expected_runtime, Mapping):
            raise CycleError("configured runtime identity is invalid")
        current_runtime = bundle.runtime_identity_digest == _value_digest(
            dict(expected_runtime)
        )
        if (
            expected_runtime_digest is not None
            and bundle.runtime_identity_digest != expected_runtime_digest
        ):
            raise CycleError("review runtime identity does not match stored runtime")
        if (
            expected_runtime_digest is None
            and not current_runtime
            and not allow_historical_runtime
        ):
            raise CycleError("review runtime identity does not match current runtime")
        if not current_runtime and not historical_state_validation:
            incomplete = coverage.get("incomplete_sources")
            coordinator = str(config.get("coordinator") or "omarchy-precision")
            if (
                replay
                or not isinstance(incomplete, list)
                or not incomplete
                or not all(
                    isinstance(source, str)
                    and source.split("/", 1)[0] in {"sessions", "repositories"}
                    and source.rsplit("/", 1)[-1] != coordinator
                    for source in incomplete
                )
            ):
                raise CycleError("historical runtime bundle is not an actionable peer gap")
    artifacts = {item.kind: item for item in bundle.artifacts}
    paths = result.get("paths")
    if not isinstance(paths, Mapping):
        raise CycleError("review result artifact paths are invalid")
    result_keys = {
        "quality_report": "quality_report",
        "evidence_ledger": "evidence_ledger",
        "semantic_analysis": "semantic_analysis",
        "work_accounting_result": "accounting_result",
        "review_snapshot": "review_snapshot",
    }
    if replay:
        result_keys["replay_integrity"] = "replay_integrity"
    for result_key, bundle_key in result_keys.items():
        path = _safe_run_file(run_dir, paths.get(result_key), result_key)
        artifact = artifacts.get(bundle_key)
        if artifact is None or path != artifact.path.resolve() or _digest(path) != artifact.digest:
            raise CycleError(f"review result {result_key} does not match its completion bundle")
    snapshot_digests: dict[str, str] = {}
    for filename in (
        "period-manifest.json", "routing.json", "review-corrections.jsonl",
        "review-acceptance.jsonl",
    ):
        snapshot = _safe_run_file(run_dir, str(run_dir / filename), filename)
        snapshot_digests[filename] = _digest(snapshot)
    if snapshot_digests != dict(expected_snapshot_digests):
        raise CycleError("review snapshot digests do not match immutable expected inputs")
    proposals, exception_ids = _validate_accounting(run_dir)
    review_ids: list[str] = []
    for item in proposals:
        try:
            review_ids.append(stable_review_id(item))
        except (TypeError, ValueError, RuntimeError) as exc:
            raise CycleError("proposal lacks a valid stable review identity") from exc
    if len(set(review_ids)) != len(review_ids):
        raise CycleError("proposal stable review identities are duplicated")
    replay_integrity: Mapping[str, Any] | None = None
    if replay:
        replay_integrity = _json_file(run_dir / "replay-integrity.json", "replay integrity")
        if not isinstance(source_run_dir, str):
            raise CycleError("replay integrity source directory is missing")
        try:
            expected_integrity = clockify_review_run.derive_replay_integrity(
                Path(source_run_dir), run_dir
            )
        except (OSError, ValueError, clockify_review_run.ReviewRunError) as exc:
            raise CycleError("replay integrity evidence is invalid") from exc
        if (
            not isinstance(replay_integrity, Mapping)
            or expected_integrity.get("status") != "pass"
            or expected_integrity.get("failures") != []
            or replay_integrity != expected_integrity
            or replay_integrity.get("source_run_id") != source_run_id
        ):
            raise CycleError("replay integrity does not bind the exact source run")
    stage = {
        "result_path": str(result_path),
        "result_digest": _digest(result_path),
        "run_dir": str(run_dir),
        "run_id": run_dir.name,
        "bundle_digest": bundle.bundle_digest,
        "artifact_digests": {key: artifacts[key].digest for key in sorted(artifacts)},
        "snapshot_digests": snapshot_digests,
        "coverage": coverage,
        "proposals_digest": _digest(run_dir / "proposals.json"),
        "accounting_digest": _digest(run_dir / "work-accounting-result.json"),
        "quality_digest": _digest(run_dir / "quality_report.json"),
        "replay_integrity_digest": (
            _digest(run_dir / "replay-integrity.json") if replay else None
        ),
        "review_ids": review_ids,
        "exception_ids": exception_ids,
    }
    if expected_runtime is not None:
        stage["runtime_identity_digest"] = bundle.runtime_identity_digest
    return stage


def _validate_collector_source_stage(
    config: Mapping[str, Any], result_path: Path, since: str, until: str, *,
    expected_snapshot_digests: Mapping[str, str],
) -> dict[str, Any]:
    """Verify collection provenance independently of downstream quality."""
    result_path = _safe_run_file(_runs_dir(config), str(result_path), "result")
    if result_path.name != "autopilot-result.json":
        raise CycleError("collector derivation result filename is invalid")
    derived = result_path.parent
    try:
        source, identity, lineage = clockify_review_run._verified_collector_derivation(
            derived
        )
    except (
        OSError, ValueError, json.JSONDecodeError,
        collector_receipts.CollectorReceiptError,
    ) as exc:
        raise CycleError("collector derivation provenance is invalid") from exc
    expected_since, expected_until = _expected_interval(config, since, until)
    if (
        identity.since_utc != expected_since
        or identity.until_utc != expected_until
    ):
        raise CycleError("collector source interval does not match selected slice")
    snapshot_digests = {
        filename: _digest(derived / filename)
        for filename in (
            "period-manifest.json", "routing.json", "review-corrections.jsonl",
            "review-acceptance.jsonl",
        )
    }
    if snapshot_digests != dict(expected_snapshot_digests):
        raise CycleError("collector derivation snapshots differ")
    ledger = _json_file(
        source / "evidence" / "evidence-ledger.json", "collector source ledger"
    )
    manifest = ledger.get("manifest") if isinstance(ledger, Mapping) else None
    coverage = (
        manifest.get("source_completeness") if isinstance(manifest, Mapping) else None
    )
    if not isinstance(coverage, Mapping):
        raise CycleError("collector source coverage is invalid")
    finalization = _json_file(
        source / "slice-finalization.json", "collector source finalization"
    )
    backlog_identity = (
        finalization.get("backlog_identity")
        if isinstance(finalization, Mapping) else None
    )
    compatibility = (
        backlog_identity.get("compatibility_version")
        if isinstance(backlog_identity, Mapping) else None
    )
    if not isinstance(compatibility, str) or not compatibility:
        raise CycleError("collector source compatibility is invalid")
    collector_runtime = lineage["collector_runtime_identity"]
    executor_runtime = lineage["executor_runtime_identity"]
    return {
        "stage_kind": "collector_source",
        "result_path": str(result_path),
        "result_digest": _digest(result_path),
        "run_dir": str(source),
        "run_id": source.name,
        "bundle_digest": identity.source_bundle_digest,
        "legacy_completion_bundle_digest": identity.legacy_completion_bundle_digest,
        "runtime_identity_digest": _value_digest(collector_runtime),
        "collector_runtime_identity_digest": _value_digest(collector_runtime),
        "executor_runtime_identity_digest": _value_digest(executor_runtime),
        "snapshot_digests": snapshot_digests,
        "coverage": dict(coverage),
        "slice_id": identity.slice_id,
        "since_utc": identity.since_utc,
        "until_utc": identity.until_utc,
        "compatibility_version": compatibility,
    }


def _validate_raw_collector_source_stage(
    config: Mapping[str, Any], result_path: Path, since: str, until: str, *,
    expected_snapshot_digests: Mapping[str, str],
) -> dict[str, Any]:
    """Migrate a legacy collector source even if downstream artifacts drifted."""
    result_path = _safe_run_file(_runs_dir(config), str(result_path), "result")
    run_dir = result_path.parent
    try:
        identity = collector_receipts.load_collector_source_bundle(
            run_dir / "completion-bundle.json", run_dir=run_dir
        )
    except collector_receipts.CollectorReceiptError as exc:
        raise CycleError("legacy collector source bundle is invalid") from exc
    expected_since, expected_until = _expected_interval(config, since, until)
    if identity.since_utc != expected_since or identity.until_utc != expected_until:
        raise CycleError("legacy collector source interval differs")
    snapshot_digests = {
        filename: _digest(run_dir / filename)
        for filename in (
            "period-manifest.json", "routing.json", "review-corrections.jsonl",
            "review-acceptance.jsonl",
        )
    }
    if snapshot_digests != dict(expected_snapshot_digests):
        raise CycleError("legacy collector source snapshots differ")
    ledger = _json_file(
        run_dir / "evidence" / "evidence-ledger.json", "legacy collector ledger"
    )
    manifest = ledger.get("manifest") if isinstance(ledger, Mapping) else None
    coverage = (
        manifest.get("source_completeness") if isinstance(manifest, Mapping) else None
    )
    finalization = _json_file(
        run_dir / "slice-finalization.json", "legacy collector finalization"
    )
    backlog_identity = (
        finalization.get("backlog_identity")
        if isinstance(finalization, Mapping) else None
    )
    compatibility = (
        backlog_identity.get("compatibility_version")
        if isinstance(backlog_identity, Mapping) else None
    )
    if not isinstance(coverage, Mapping) or not isinstance(compatibility, str):
        raise CycleError("legacy collector source identity is invalid")
    try:
        parsed_identity = collector_slices.BacklogIdentity(**dict(backlog_identity))
        planned = collector_slices.plan_slices(
            dt.datetime.fromisoformat(
                parsed_identity.since_utc.replace("Z", "+00:00")
            ),
            dt.datetime.fromisoformat(
                parsed_identity.until_utc.replace("Z", "+00:00")
            ),
            zone=ZoneInfo(parsed_identity.timezone),
            max_days=parsed_identity.max_days,
        )
        backlog = collector_slices.BacklogStore(
            _collector_checkpoint_root(config, os.environ)
        ).read_existing(parsed_identity, tuple(planned))
    except (
        TypeError, ValueError, KeyError, ZoneInfoNotFoundError,
        collector_slices.BacklogError,
    ) as exc:
        raise CycleError("legacy collector backlog binding is invalid") from exc
    receipt = next(
        (item for item in backlog.completed if item.slice_id == identity.slice_id), None
    )
    bundle_path = (run_dir / "completion-bundle.json").resolve()
    if (
        receipt is None
        or receipt.result_path.resolve() != bundle_path
        or receipt.result_digest != _digest(bundle_path)
    ):
        raise CycleError("legacy collector backlog receipt differs")
    runtime_digest = _value_digest(identity.collector_runtime_identity)
    return {
        "stage_kind": "collector_source",
        "result_path": str(result_path),
        "result_digest": _digest(result_path),
        "run_dir": str(run_dir),
        "run_id": run_dir.name,
        "bundle_digest": identity.source_bundle_digest,
        "legacy_completion_bundle_digest": identity.legacy_completion_bundle_digest,
        "runtime_identity_digest": runtime_digest,
        "collector_runtime_identity_digest": runtime_digest,
        "executor_runtime_identity_digest": runtime_digest,
        "snapshot_digests": snapshot_digests,
        "coverage": dict(coverage),
        "slice_id": identity.slice_id,
        "since_utc": identity.since_utc,
        "until_utc": identity.until_utc,
        "compatibility_version": compatibility,
    }


def _stage_from_state(
    config: Mapping[str, Any], record: Mapping[str, Any], key: str,
    since: str, until: str, *, replay: bool,
    expected_snapshot_digests: Mapping[str, str],
    source_run_id: str | None = None, source_run_dir: str | None = None,
    allow_historical_runtime: bool = False,
) -> dict[str, Any] | None:
    stored = record.get(key)
    if stored is None:
        return None
    if not isinstance(stored, Mapping) or not isinstance(stored.get("result_path"), str):
        raise CycleError(f"stored {key} stage is invalid")
    stored_runtime_digest = stored.get("runtime_identity_digest")
    if stored_runtime_digest is not None and not _valid_digest(stored_runtime_digest):
        raise CycleError(f"stored {key} runtime identity is invalid")
    if stored.get("stage_kind") == "collector_source":
        if replay:
            raise CycleError("collector source stage cannot be a replay")
        result_path = Path(stored["result_path"])
        validator = (
            _validate_raw_collector_source_stage
            if result_path.parent == Path(str(stored.get("run_dir")))
            else _validate_collector_source_stage
        )
        verified = validator(
            config, result_path, since, until,
            expected_snapshot_digests=expected_snapshot_digests,
        )
    else:
        verified = _validate_stage(
            config, Path(stored["result_path"]), since, until,
            replay=replay, expected_snapshot_digests=expected_snapshot_digests,
            source_run_id=source_run_id, source_run_dir=source_run_dir,
            allow_historical_runtime=allow_historical_runtime,
            expected_runtime_digest=stored_runtime_digest,
            historical_state_validation=stored_runtime_digest is not None,
        )
    comparable = dict(verified)
    if stored_runtime_digest is None:
        comparable.pop("runtime_identity_digest", None)
    if dict(stored) != comparable:
        raise CycleError(f"stored {key} stage identity has drifted")
    return verified


def _migrate_legacy_stage_runtime(
    config: Mapping[str, Any], state: dict[str, Any], state_path: Path, *,
    persist: bool = True,
) -> None:
    """Bind bab6 stage shapes once to their bundle-bound historical runtime."""
    for since, raw_record in list(state.get("slices", {}).items()):
        if not isinstance(raw_record, Mapping):
            continue
        record = dict(raw_record)
        source = record.get("source")
        replay = record.get("replay")
        if not (
            isinstance(source, Mapping) and "runtime_identity_digest" not in source
            or isinstance(replay, Mapping) and "runtime_identity_digest" not in replay
        ):
            continue
        until = record.get("until")
        if not isinstance(until, str):
            raise CycleError("legacy source stage interval is invalid")
        changed = False
        if isinstance(source, Mapping) and "runtime_identity_digest" not in source:
            try:
                verified_source = _validate_stage(
                    config, Path(str(source.get("result_path"))), str(since), until,
                    replay=False,
                    expected_snapshot_digests=_stored_snapshot_digests(record),
                    allow_historical_runtime=True,
                    historical_state_validation=True,
                )
            except CycleError:
                verified_source = _validate_raw_collector_source_stage(
                    config, Path(str(source.get("result_path"))), str(since), until,
                    expected_snapshot_digests=_stored_snapshot_digests(record),
                )
                if (
                    source.get("run_dir") != verified_source["run_dir"]
                    or source.get("run_id") != verified_source["run_id"]
                    or source.get("bundle_digest")
                    != verified_source["legacy_completion_bundle_digest"]
                    or source.get("snapshot_digests")
                    != verified_source["snapshot_digests"]
                    or source.get("coverage") != verified_source["coverage"]
                ):
                    raise CycleError("legacy collector source identity has drifted")
            else:
                legacy_shape = dict(verified_source)
                legacy_shape.pop("runtime_identity_digest", None)
                if dict(source) != legacy_shape:
                    raise CycleError("legacy source stage identity has drifted")
            record["source"] = verified_source
            source = verified_source
            changed = True
        if isinstance(replay, Mapping) and "runtime_identity_digest" not in replay:
            if not isinstance(source, Mapping):
                raise CycleError("legacy replay source stage is invalid")
            verified_replay = _validate_stage(
                config, Path(str(replay.get("result_path"))), str(since), until,
                replay=True,
                expected_snapshot_digests=source["snapshot_digests"],
                source_run_id=str(source["run_id"]),
                source_run_dir=str(source["run_dir"]),
                allow_historical_runtime=True,
                historical_state_validation=True,
            )
            legacy_shape = dict(verified_replay)
            legacy_shape.pop("runtime_identity_digest", None)
            if dict(replay) != legacy_shape:
                raise CycleError("legacy replay stage identity has drifted")
            record["replay"] = verified_replay
            changed = True
        if changed:
            if persist:
                _persist_state(state_path, state, str(since), record)
            else:
                state["slices"][since] = record


def completion_status(result: Mapping[str, Any]) -> dict[str, Any]:
    """Keep collection and exception completeness separate in durable state."""
    coverage = result.get("source_completeness")
    if not isinstance(coverage, Mapping):
        coverage = {}
    source_complete = (
        coverage.get("status") == "complete"
        and coverage.get("incomplete_sources") == []
    )
    accounting = result.get("accounting")
    if not isinstance(accounting, Mapping):
        accounting = {}
    exception_ids: list[str] = []
    ambiguous = accounting.get("ambiguous", [])
    if isinstance(ambiguous, list):
        exception_ids.extend(
            str(row.get("id") or row.get("review_id") or "").strip()
            for row in ambiguous if isinstance(row, Mapping)
            and str(row.get("id") or row.get("review_id") or "").strip()
        )
    return {
        "source_complete": source_complete,
        "exceptions_complete": not exception_ids,
        "exception_ids": sorted(set(exception_ids)),
        "source_completeness": dict(coverage),
    }


def _review_command(config: Mapping[str, Any], since: str, until: str) -> list[str]:
    root = _path(config, "root")
    command = [sys.executable, str(root / "scripts" / "clockify_review_run.py"), "--runs-root", str(_runs_dir(config)), "--since", since, "--until", (dt.date.fromisoformat(until) - dt.timedelta(days=1)).isoformat(), "--state", str(_path(config, "state_dir") / "review-state.json"), "--period-manifest", str(_path(config, "state_dir") / f"{since}.period-manifest.json"), "--routing", str(_path(config, "routing", file=True)), "--corrections", str(_path(config, "corrections", file=True)), "--acceptance-ledger", str(_path(config, "acceptance", file=True)), "--analyzer-cache", str(_path(config, "cache"))]
    if config["calendly_optional"]:
        command.append("--calendly-optional")
    return command


def _recovery_command(
    config: Mapping[str, Any], parent_run_dir: Path, source: str, attempt_id: str,
) -> list[str]:
    root = _path(config, "root")
    return [
        sys.executable,
        str(root / "scripts" / "clockify_review_run.py"),
        "--runs-root", str(_runs_dir(config)),
        "--recover-source-debt-from", str(parent_run_dir),
        "--recover-source", source,
        "--recover-attempt-id", attempt_id,
        "--state", str(_path(config, "state_dir") / "review-state.json"),
        "--analyzer-cache", str(_path(config, "cache")),
    ]


def _run_budgeted_child(
    command: list[str], *, root: Path, budget: list[float], cap: int, grace: int,
    runs_dir: Path | None = None, checkpoint_root: Path | None = None,
):
    total = min(cap, int(budget[0]))
    if total <= grace:
        raise _BudgetExhausted("total_child_budget_exhausted")
    child_environment = dict(os.environ)
    child_environment["CLOCKIFY_AUTOPILOT_RUNS_ROOT"] = str(
        (runs_dir or root / "runs").resolve()
    )
    if checkpoint_root is not None:
        _ensure_collector_checkpoint_root(checkpoint_root)
        child_environment["CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT"] = str(checkpoint_root)
    child = run_child_bounded(
        command, cwd=root, timeout=ChildTimeoutConfig(total, grace),
        environment=child_environment,
    )
    budget[0] = max(0.0, budget[0] - float(child.duration_seconds))
    return child


def _replay_command(config: Mapping[str, Any], source: Path) -> list[str]:
    root = _path(config, "root")
    return [
        sys.executable,
        str(root / "scripts" / "clockify_review_run.py"),
        "--runs-root", str(_runs_dir(config)),
        "--replay-from", str(source),
        "--state", str(_path(config, "state_dir") / "review-state.json"),
        "--analyzer-cache", str(_path(config, "cache")),
    ]


def _publisher_command(
    config: Mapping[str, Any], source: Mapping[str, Any], replay: Mapping[str, Any],
    *, sheet_title: str, result_path: Path,
) -> list[str]:
    root = _path(config, "root")
    source_dir = Path(str(source["run_dir"]))
    replay_dir = Path(str(replay["run_dir"]))
    return [
        sys.executable,
        str(root / "scripts" / "clockify_sheet_publish.py"),
        "--spreadsheet-id", str(config["spreadsheet_id"]),
        "--sheet-title", sheet_title,
        "--proposals", str(source_dir / "proposals.json"),
        "--quality-report", str(source_dir / "quality_report.json"),
        "--replay-integrity", str(replay_dir / "replay-integrity.json"),
        "--routing-snapshot", str((source_dir / "routing.json").resolve()),
        "--run-id", str(source["run_id"]),
        "--result-output", str(result_path),
        "--enable-write",
    ]


def _expected_publication_receipts(
    config: Mapping[str, Any], source: Mapping[str, Any], *, sheet_title: str,
) -> list[dict[str, Any]]:
    proposals, _exceptions = _validate_accounting(Path(str(source["run_dir"])))
    source_dir = Path(str(source["run_dir"])).resolve()
    routing_path = _safe_run_file(
        source_dir, str(source_dir / "routing.json"), "source routing snapshot"
    )
    projects = project_allowlist(_json_file(routing_path, "source routing snapshot"))
    partitions = (
        (sheet_title, [
            item for item in proposals
            if item.get("routing_disposition") != "unresolved-routing"
        ]),
        ("unresolved-evidence", [
            item for item in proposals
            if item.get("routing_disposition") == "unresolved-routing"
        ]),
    )
    return [
        _publication_receipt(
            spreadsheet_id=str(config["spreadsheet_id"]),
            sheet_title=title,
            rows=[
                proposal_row(
                    item, str(source["run_id"]), project_allowlist=projects,
                )
                for item in members
            ],
        )
        for title, members in partitions if members
    ]


def _validated_publication_document(
    document: object, expected: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(document, Mapping):
        raise CycleError("publisher result contract is invalid")
    publications = document.get("publications")
    if (
        document.get("schema_version") != "sheet-publication-result/v1"
        or document.get("status") != "published"
        or document.get("external_writes") is not True
        or document.get("clockify_writes") != 0
        or not isinstance(publications, list)
    ):
        raise CycleError("publisher result contract is invalid")
    retained_fields = (
        "spreadsheet_id", "sheet_title", "row_ids", "rows_sha256",
        "readback_id", "receipt_id",
    )
    retained = [
        {field: item.get(field) for field in retained_fields}
        for item in publications if isinstance(item, Mapping)
    ]
    if len(retained) != len(publications) or retained != expected:
        raise CycleError("publisher result destinations or readbacks differ")
    return retained


def _publisher_result(
    stdout: str, runs: Path, expected: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    path = _result(stdout, runs)
    return _validated_publication_document(
        _json_file(path, "publisher result"), expected,
    )


def _delivery_document(
    config: Mapping[str, Any], since: str, until: str,
    source: Mapping[str, Any], replay: Mapping[str, Any], *, sheet_title: str,
) -> dict[str, Any]:
    try:
        publication_receipts = _expected_publication_receipts(
            config, source, sheet_title=sheet_title,
        )
    except (TypeError, ValueError, RuntimeError) as exc:
        raise CycleError("proposal cannot produce the expected sheet row contract") from exc
    unsigned: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "since": since,
        "until": until,
        "target": {
            "spreadsheet_id": str(config["spreadsheet_id"]),
            "sheet_title": sheet_title,
        },
        "source": {
            key: source[key]
            for key in (
                "run_id", "result_digest", "bundle_digest", "artifact_digests",
                "snapshot_digests", "proposals_digest", "accounting_digest", "quality_digest",
            )
        },
        "replay": {
            key: replay[key]
            for key in (
                "run_id", "result_digest", "bundle_digest", "artifact_digests",
                "snapshot_digests", "replay_integrity_digest",
            )
        },
        "review_ids": list(source["review_ids"]),
        "publication_receipts": publication_receipts,
    }
    return {**unsigned, "receipt_digest": _value_digest(unsigned)}


def _write_delivery_receipt(path: Path, document: Mapping[str, Any]) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise CycleError("existing delivery receipt is unsafe")
        existing = _json_file(path, "delivery receipt")
        if existing != dict(document):
            raise CycleError("existing delivery receipt does not match immutable inputs")
        return
    _atomic(path, document)


def _verify_delivery_receipt(
    path: Path, config: Mapping[str, Any], since: str, until: str,
    source: Mapping[str, Any], replay: Mapping[str, Any], *, sheet_title: str,
) -> None:
    if path.is_symlink() or not path.is_file():
        raise CycleError("delivery receipt is missing or unsafe")
    document = _json_file(path, "delivery receipt")
    expected = _delivery_document(
        config, since, until, source, replay, sheet_title=sheet_title
    )
    if document != expected:
        raise CycleError("delivery receipt target or immutable inputs have drifted")


def _persist_state(path: Path, state: dict[str, Any], since: str, record: dict[str, Any]) -> None:
    state["slices"][since] = record
    _atomic(path, state)


def _attempted_at() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _collector_ancestor_from_repair(
    config: Mapping[str, Any], run_dir: Path,
    bundle: collector_receipts.SliceCompletionBundle,
) -> tuple[Path, collector_receipts.SliceCompletionBundle]:
    """Follow sealed repair inputs back to the collector, without changing a run."""
    runs = _runs_dir(config)
    selected = bundle
    seen: set[Path] = set()
    pending_retry_sources: set[tuple[str, str]] = set()
    while True:
        current = _canonical_runtime_path(run_dir, label="repair ancestry run")
        if current.parent != runs or not current.is_dir():
            raise CycleError("repair ancestry run is not a direct configured run")
        if current in seen:
            raise CycleError("repair ancestry cycle detected")
        seen.add(current)
        lineage_path = current / "repair-source.json"
        if not lineage_path.exists():
            if pending_retry_sources:
                raise CycleError("repair retry source binding is missing")
            return current, bundle
        lineage = _json_file(
            _safe_run_file(current, str(lineage_path), "repair provenance"),
            "repair provenance",
        )
        base_keys = {
            "schema_version", "source_run_id", "source_completion_sha256",
            "ledger_identity", "source_coverage", "semantic_analysis_fixture",
            "semantic_analysis_sha256", "source_routing_sha256",
            "repair_routing_sha256",
        }
        cache_keys = {"analyzer_cache_path", "analyzer_cache_sha256"}
        correction_keys = {"source_corrections_sha256", "repair_corrections_sha256"}
        if not isinstance(lineage, dict) or (
            set(lineage) not in (
                base_keys, base_keys | cache_keys, base_keys | correction_keys,
                base_keys | cache_keys | correction_keys,
            ) or lineage.get("schema_version") != 1
        ):
            raise CycleError("repair provenance schema is invalid")
        source_id = lineage["source_run_id"]
        allowed = string.ascii_letters + string.digits + "._-"
        if (
            not isinstance(source_id, str) or not source_id
            or source_id in {".", ".."} or source_id[0] not in allowed[:-3]
            or any(char not in allowed for char in source_id)
        ):
            raise CycleError("repair source run ID is invalid")
        parent = runs / source_id
        if parent in seen:
            raise CycleError("repair ancestry cycle detected")
        parent = _canonical_runtime_path(parent, label="repair source run")
        if parent.parent != runs or not parent.is_dir():
            raise CycleError("repair source run is missing or unsafe")
        parent_bundle_path = _safe_run_file(
            parent, str(parent / "completion-bundle.json"), "repair source completion"
        )
        if _digest(parent_bundle_path) != lineage["source_completion_sha256"]:
            raise CycleError("repair source completion digest differs")
        try:
            parent_bundle = collector_receipts.load_completion_bundle(
                parent_bundle_path, run_dir=parent,
            )
            parent_coverage = collector_receipts.completion_coverage(parent_bundle)
            child_coverage = collector_receipts.completion_coverage(bundle)
            child_ledger = clockify_review_run._ledger_identity(current)
            parent_ledger = clockify_review_run._ledger_identity(parent)
        except (OSError, ValueError, collector_receipts.CollectorReceiptError) as exc:
            raise CycleError("repair ancestry completion or ledger is invalid") from exc
        if parent_bundle.replay or (
            parent_bundle.slice_id != selected.slice_id
            or parent_bundle.since_utc != selected.since_utc
            or parent_bundle.until_utc != selected.until_utc
            or parent_bundle.runtime_identity_digest != selected.runtime_identity_digest
        ):
            raise CycleError("repair ancestry slice or runtime differs")
        if (
            parent_coverage != lineage["source_coverage"]
            or child_coverage != parent_coverage
            or child_ledger != parent_ledger
            or child_ledger != lineage["ledger_identity"]
        ):
            raise CycleError("repair ancestry coverage or ledger differs")
        report = _json_file(
            _safe_run_file(current, str(current / "run-report.json"), "repair report"),
            "repair report",
        )
        if not isinstance(report, dict) or (
            report.get("run_id") != current.name
            or report.get("repair_of_run_id") != source_id
        ):
            raise CycleError("repair report parent identity differs")
        for filename in (
            "period-manifest.json", "routing.json", "review-corrections.jsonl",
            "review-acceptance.jsonl",
        ):
            child_path = _safe_run_file(current, str(current / filename), "repair snapshot")
            source_path = _safe_run_file(parent, str(parent / filename), "repair source snapshot")
            if filename == "routing.json":
                if (
                    _digest(source_path) != lineage["source_routing_sha256"]
                    or _digest(child_path) != lineage["repair_routing_sha256"]
                ):
                    raise CycleError("repair routing provenance differs")
            elif filename == "review-corrections.jsonl" and correction_keys <= set(lineage):
                try:
                    transition = clockify_review_run._validate_repair_credit_transition(
                        parent, child_path, runs_root=runs,
                    )
                except (OSError, ValueError, clockify_review_run.ReviewRunError) as exc:
                    raise CycleError("repair posted credit provenance is invalid") from exc
                if transition != (
                    lineage["source_corrections_sha256"],
                    lineage["repair_corrections_sha256"],
                ):
                    raise CycleError("repair posted credit provenance differs")
            elif _digest(child_path) != _digest(source_path):
                raise CycleError("repair reconciliation snapshot differs")
        if lineage["semantic_analysis_fixture"] != "repair-fixture/semantic-analysis.json":
            raise CycleError("repair semantic fixture path is invalid")
        fixture = _safe_run_file(
            current, str(current / "repair-fixture" / "semantic-analysis.json"),
            "repair semantic fixture",
        )
        semantic = _safe_run_file(
            parent, str(parent / "semantic-analysis.json"), "repair source semantic"
        )
        expected_semantic = "sha256:" + str(lineage["semantic_analysis_sha256"])
        if _digest(fixture) != expected_semantic or _digest(semantic) != expected_semantic:
            raise CycleError("repair semantic provenance differs")
        parent_semantic_sha = _digest(semantic).removeprefix("sha256:")
        if "analyzer_cache_path" in lineage:
            if lineage["analyzer_cache_path"] != "analyzer-cache-used.jsonl":
                raise CycleError("repair analyzer cache path is invalid")
            expected_cache = "sha256:" + str(lineage["analyzer_cache_sha256"])
            source_cache = _safe_run_file(
                parent, str(parent / "analyzer-cache-used.jsonl"),
                "repair source analyzer cache",
            )
            child_cache = _safe_run_file(
                current, str(current / "analyzer-cache-used.jsonl"),
                "repair analyzer cache",
            )
            if _digest(source_cache) != expected_cache:
                raise CycleError("repair analyzer cache provenance differs")
            pending_retry_sources.discard((
                parent_semantic_sha, expected_cache.removeprefix("sha256:")
            ))
            if _digest(child_cache) != expected_cache:
                child_analysis = _json_file(
                    _safe_run_file(
                        current, str(current / "semantic-analysis.json"),
                        "repair semantic analysis",
                    ), "repair semantic analysis",
                )
                cache_summary = (
                    child_analysis.get("analyzer_cache")
                    if isinstance(child_analysis, Mapping) else None
                )
                snapshot = (
                    cache_summary.get("snapshot")
                    if isinstance(cache_summary, Mapping) else None
                )
                retry = (
                    child_analysis.get("failed_review_retry")
                    if isinstance(child_analysis, Mapping) else None
                )
                content = child_cache.read_bytes()
                if (
                    not isinstance(snapshot, Mapping)
                    or set(snapshot) != {"path", "record_count", "sha256"}
                    or snapshot.get("path") != "analyzer-cache-used.jsonl"
                    or snapshot.get("sha256") != _digest(child_cache).removeprefix("sha256:")
                    or not isinstance(snapshot.get("record_count"), int)
                    or isinstance(snapshot.get("record_count"), bool)
                    or snapshot["record_count"] != sum(bool(line.strip()) for line in content.splitlines())
                    or not isinstance(retry, Mapping)
                    or retry.get("mode") not in {
                        None, "scoped_review_v1", "scoped_review_v2",
                        "scoped_review_v3_invalid_effort",
                        "scoped_review_v4_citation_quarantine",
                    }
                ):
                    raise CycleError("repair retry cache binding is invalid")
                try:
                    clockify_review_run._retry_provenance_digests(retry)
                    semantic_analyzer.AnalyzerResponseCache(source_cache)
                    semantic_analyzer.AnalyzerResponseCache(child_cache)
                    source_records = {
                        row["cache_key"]: row
                        for row in (json.loads(line) for line in source_cache.read_text().splitlines())
                        if isinstance(row, dict)
                    }
                    child_records = {
                        row["cache_key"]: row
                        for row in (json.loads(line) for line in content.splitlines())
                        if isinstance(row, dict)
                    }
                except (OSError, UnicodeDecodeError, ValueError, KeyError,
                        semantic_analyzer.AnalyzerError) as exc:
                    raise CycleError("repair retry cache records are invalid") from exc
                if (
                    len(source_records) != sum(bool(line.strip()) for line in source_cache.read_bytes().splitlines())
                    or len(child_records) != snapshot["record_count"]
                    or not (set(child_records) - set(source_records))
                    or any(
                        child_records[key] != source_records[key]
                        for key in set(source_records) & set(child_records)
                    )
                    or (
                        retry.get("mode") is not None
                        and not set(source_records) <= set(child_records)
                    )
                ):
                    raise CycleError("repair retry cache does not preserve source decisions")
                pending_retry_sources.add((
                    str(retry.get("source_semantic_sha256")),
                    str(retry.get("source_cache_sha256")),
                ))
                pending_retry_sources.discard((
                    parent_semantic_sha, expected_cache.removeprefix("sha256:")
                ))
        run_dir, bundle = parent, parent_bundle


def _verify_credit_adoption_transition(
    config: Mapping[str, Any], source_run: Path, frozen_digest: str,
    adopted_digest: str,
) -> None:
    """Bind changed corrections to frozen collector input and proved repair hops."""
    runs = _runs_dir(config)
    source = _canonical_runtime_path(source_run, label="credit adoption source")
    if source.parent != runs or not source.is_dir():
        raise CycleError("credit adoption source is outside configured runs")
    try:
        bundle = collector_receipts.load_completion_bundle(
            source / "completion-bundle.json", run_dir=source,
        )
    except (OSError, ValueError, collector_receipts.CollectorReceiptError) as exc:
        raise CycleError("credit adoption completion is invalid") from exc
    if bundle.replay or _digest(source / "review-corrections.jsonl") != adopted_digest:
        raise CycleError("credit adoption source corrections differ")
    ancestor, _bundle = _collector_ancestor_from_repair(config, source, bundle)
    if ancestor == source or _digest(ancestor / "review-corrections.jsonl") != frozen_digest:
        raise CycleError("credit adoption does not descend from frozen corrections")


def _interval_from_stage(
    config: Mapping[str, Any], source: str, stage: Mapping[str, Any],
    *, checkpoint_root: Path | None = None,
    checkpoint_manifest_digest: str | None = None,
    checkpoint_manifest_text: str | None = None,
    checkpoint_capture: dict[str, str] | None = None,
    allow_verified_derivation: bool = False,
) -> source_coverage.SourceInterval:
    if stage.get("stage_kind") == "collector_source":
        try:
            return source_coverage.SourceInterval(
                source=source,
                since_utc=str(stage["since_utc"]),
                until_utc=str(stage["until_utc"]),
                slice_id=str(stage["slice_id"]),
                compatibility_version=str(stage["compatibility_version"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CycleError("collector source interval is invalid") from exc
    run_dir = Path(str(stage["run_dir"]))
    try:
        bundle = collector_receipts.load_completion_bundle(
            run_dir / "completion-bundle.json", run_dir=run_dir
        )
    except collector_receipts.CollectorReceiptError as exc:
        raise CycleError("verified source completion bundle cannot be reloaded") from exc
    if bundle.bundle_digest != stage.get("bundle_digest"):
        raise CycleError("verified source completion bundle identity drifted")
    run_dir, bundle = _collector_ancestor_from_repair(config, run_dir, bundle)
    if allow_verified_derivation and (run_dir / "collector-source.json").exists():
        zone = ZoneInfo(str(config["timezone"]))
        since = dt.datetime.fromisoformat(
            bundle.since_utc.replace("Z", "+00:00")
        ).astimezone(zone).date().isoformat()
        until = dt.datetime.fromisoformat(
            bundle.until_utc.replace("Z", "+00:00")
        ).astimezone(zone).date().isoformat()
        return _interval_from_derived_stage(
            config, since, until, stage, {
                "kind": "collector_derivation",
                "derivation_run_dir": str(run_dir),
                "lineage_digest": _digest(run_dir / "collector-source.json"),
            }, source_name=source,
        )
    finalization_path = _safe_run_file(
        run_dir, str(run_dir / "slice-finalization.json"), "slice finalization"
    )
    finalization = _json_file(finalization_path, "slice finalization")
    if not isinstance(finalization, dict) or set(finalization) != {
        "schema_version", "backlog_identity", "slice_id", "since_utc", "until_utc",
    } or finalization.get("schema_version") != "collector-slice-finalization/v1":
        raise CycleError("slice finalization schema is invalid")
    raw_identity = finalization.get("backlog_identity")
    if not isinstance(raw_identity, dict) or set(raw_identity) != {
        "since_utc", "until_utc", "timezone", "max_days", "compatibility_version",
    }:
        raise CycleError("slice finalization backlog identity is invalid")
    try:
        identity = collector_slices.BacklogIdentity(**raw_identity)
        planned = collector_slices.plan_slices(
            dt.datetime.fromisoformat(identity.since_utc.replace("Z", "+00:00")),
            dt.datetime.fromisoformat(identity.until_utc.replace("Z", "+00:00")),
            zone=ZoneInfo(identity.timezone), max_days=identity.max_days,
        )
    except (TypeError, ValueError, KeyError, collector_slices.BacklogError) as exc:
        raise CycleError("slice finalization backlog identity is invalid") from exc
    matched = next((item for item in planned if item.slice_id == bundle.slice_id), None)
    if matched is None or (
        finalization.get("slice_id") != bundle.slice_id
        or finalization.get("since_utc") != bundle.since_utc
        or finalization.get("until_utc") != bundle.until_utc
        or matched.since.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        != bundle.since_utc
        or matched.until.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        != bundle.until_utc
    ):
        raise CycleError("slice finalization identity does not match completion bundle")
    compatibility = identity.compatibility_version
    if not isinstance(compatibility, str) or not compatibility:
        raise CycleError("slice finalization compatibility lineage is invalid")
    if checkpoint_root is None:
        checkpoint_root = _collector_checkpoint_root(config, os.environ)
    else:
        checkpoint_root = _canonical_runtime_path(
            checkpoint_root, label="historical checkpoint root"
        )
        if checkpoint_manifest_text is None:
            if checkpoint_root.is_symlink() or not checkpoint_root.is_dir():
                raise CycleError("historical checkpoint root is unsafe")
            details = checkpoint_root.stat()
            if details.st_uid != os.getuid() or stat.S_IMODE(details.st_mode) & 0o022:
                raise CycleError("historical checkpoint root is not owner-controlled")
    if checkpoint_manifest_text is not None and checkpoint_manifest_digest is None:
        raise CycleError("historical checkpoint proof has no digest")
    try:
        store = collector_slices.BacklogStore(checkpoint_root)
        if checkpoint_manifest_text is None:
            backlog = store.read_existing(identity, tuple(planned))
            manifest_text = (backlog.directory / "backlog-manifest.json").read_text(
                encoding="utf-8"
            )
        else:
            manifest_text = checkpoint_manifest_text
            document = json.loads(manifest_text)
            plan = collector_slices._plan_document(identity, tuple(planned))
            directory = checkpoint_root / collector_slices._digest(plan)[7:]
            backlog = store._state_from_manifest(
                identity, tuple(planned), directory, document,
            )
    except collector_slices.BacklogError as exc:
        raise CycleError("sealed collector backlog binding is invalid") from exc
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CycleError("historical checkpoint manifest is invalid") from exc
    if checkpoint_manifest_digest is not None:
        manifest_digest = "sha256:" + hashlib.sha256(
            manifest_text.encode("utf-8")
        ).hexdigest()
        if manifest_digest != checkpoint_manifest_digest:
            raise CycleError("historical checkpoint manifest identity differs")
    if checkpoint_capture is not None:
        checkpoint_capture["manifest_text"] = manifest_text
    bundle_path = (run_dir / "completion-bundle.json").resolve()
    file_digest = _digest(bundle_path)
    receipt = next(
        (item for item in backlog.completed if item.slice_id == bundle.slice_id), None
    )
    if receipt is None or (
        receipt.result_path.resolve() != bundle_path
        or receipt.result_digest != file_digest
    ):
        raise CycleError("sealed collector backlog does not bind completion bundle")
    return source_coverage.SourceInterval(
        source=source,
        since_utc=bundle.since_utc,
        until_utc=bundle.until_utc,
        slice_id=bundle.slice_id,
        compatibility_version=compatibility,
    )


def _interval_from_derived_stage(
    config: Mapping[str, Any], since: str, until: str,
    stage: Mapping[str, Any], provenance: Mapping[str, Any],
    *, source_name: str = "runner/unclassified",
) -> source_coverage.SourceInterval:
    """Bind a review stage to a genuine collector derivation, not old backlog state."""
    if not isinstance(provenance, Mapping) or set(provenance) != {
        "kind", "derivation_run_dir", "lineage_digest",
    } or provenance.get("kind") != "collector_derivation" or not _valid_digest(
        provenance.get("lineage_digest")
    ):
        raise CycleError("historical collector derivation provenance is invalid")
    runs = _runs_dir(config)
    derived = _canonical_runtime_path(
        str(provenance["derivation_run_dir"]), label="historical derivation run"
    )
    if derived.parent != runs or not derived.is_dir():
        raise CycleError("historical collector derivation provenance is invalid")
    selected = Path(str(stage["run_dir"]))
    try:
        selected_bundle = collector_receipts.load_completion_bundle(
            selected / "completion-bundle.json", run_dir=selected,
        )
        if selected_bundle.bundle_digest != stage.get("bundle_digest"):
            raise CycleError("historical derived executor completion drifted")
        ancestor, _bundle = _collector_ancestor_from_repair(
            config, selected, selected_bundle,
        )
        if ancestor != derived:
            raise CycleError("historical collector derivation ancestry differs")
        lineage_path = _safe_run_file(
            derived, str(derived / "collector-source.json"), "collector derivation lineage"
        )
        if _digest(lineage_path) != provenance["lineage_digest"]:
            raise CycleError("historical collector derivation lineage digest differs")
        raw, identity, lineage = clockify_review_run._verified_collector_derivation(
            derived
        )
        finalization_path = _safe_run_file(
            raw, str(raw / "slice-finalization.json"), "collector source finalization"
        )
        collector_stage = _validate_collector_source_stage(
            config, derived / "autopilot-result.json", since, until,
            expected_snapshot_digests=lineage["snapshot_digests"],
        )
    except (OSError, ValueError, KeyError, collector_receipts.CollectorReceiptError) as exc:
        raise CycleError("historical collector derivation provenance is invalid") from exc
    if (
        collector_stage["run_dir"] != str(raw)
        or collector_stage["bundle_digest"] != identity.source_bundle_digest
        or collector_stage["slice_id"] != selected_bundle.slice_id
        or collector_stage["since_utc"] != selected_bundle.since_utc
        or collector_stage["until_utc"] != selected_bundle.until_utc
    ):
        raise CycleError("historical collector derivation slice differs")
    finalization = _json_file(finalization_path, "collector source finalization")
    if not isinstance(finalization, Mapping) or set(finalization) != {
        "schema_version", "backlog_identity", "slice_id", "since_utc", "until_utc",
    } or finalization.get("schema_version") != "collector-slice-finalization/v1" or (
        finalization.get("slice_id") != identity.slice_id
        or finalization.get("since_utc") != identity.since_utc
        or finalization.get("until_utc") != identity.until_utc
    ):
        raise CycleError("historical collector derivation finalization differs")
    raw_identity = finalization["backlog_identity"]
    if not isinstance(raw_identity, Mapping) or set(raw_identity) != {
        "since_utc", "until_utc", "timezone", "max_days", "compatibility_version",
    }:
        raise CycleError("historical collector derivation finalization is invalid")
    try:
        backlog_identity = collector_slices.BacklogIdentity(**raw_identity)
        planned = collector_slices.plan_slices(
            dt.datetime.fromisoformat(backlog_identity.since_utc.replace("Z", "+00:00")),
            dt.datetime.fromisoformat(backlog_identity.until_utc.replace("Z", "+00:00")),
            zone=ZoneInfo(backlog_identity.timezone), max_days=backlog_identity.max_days,
        )
    except (TypeError, ValueError, KeyError, ZoneInfoNotFoundError, collector_slices.BacklogError) as exc:
        raise CycleError("historical collector derivation finalization is invalid") from exc
    matched = next((item for item in planned if item.slice_id == identity.slice_id), None)
    if matched is None or (
        matched.since.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        != identity.since_utc
        or matched.until.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
        != identity.until_utc
        or collector_stage["compatibility_version"] != backlog_identity.compatibility_version
    ):
        raise CycleError("historical collector derivation slice differs")
    return _interval_from_stage(config, source_name, collector_stage)


def _same_active_failure(
    store: source_coverage.SourceDebtStore,
    interval: source_coverage.SourceInterval,
    *, failure_class: str, resume_state_digest: str,
) -> bool:
    return any(
        item.debt_id == interval.debt_id
        and item.failure_class == failure_class
        and item.resume_state_digest == resume_state_digest
        for item in store.active()
    )


def _record_failure_once(
    store: source_coverage.SourceDebtStore,
    interval: source_coverage.SourceInterval,
    *, failure_class: str, resume_state_digest: str,
) -> None:
    if _same_active_failure(
        store, interval, failure_class=failure_class,
        resume_state_digest=resume_state_digest,
    ):
        return
    store.record_failure(
        interval,
        failure_class=failure_class,
        retryable=True,
        resume_state_digest=resume_state_digest,
        attempted_at=_attempted_at(),
    )


def _record_exact_debts(
    config: Mapping[str, Any], store: source_coverage.SourceDebtStore,
    source: Mapping[str, Any],
) -> bool:
    coverage = source.get("coverage")
    incomplete = coverage.get("incomplete_sources") if isinstance(coverage, Mapping) else None
    if not isinstance(incomplete, list) or not incomplete or not all(
        isinstance(item, str) and item for item in incomplete
    ):
        return False
    peers: dict[str, list[str]] = {}
    for name in incomplete:
        category, separator, machine = name.partition("/")
        if separator != "/" or category not in {"sessions", "repositories"} or not machine:
            return False
        peers.setdefault(machine, []).append(name)
    for machine, required_sources in sorted(peers.items()):
        interval = _interval_from_stage(
            config, f"peer/{machine}", source,
            allow_verified_derivation=True,
        )
        resume_digest = _value_digest({
            "bundle_digest": source["bundle_digest"],
            "debt_id": interval.debt_id,
            "required_sources": sorted(required_sources),
            "run_id": source["run_id"],
        })
        _record_failure_once(
            store, interval, failure_class="coverage_incomplete",
            resume_state_digest=resume_digest,
        )
    return True


def _audit_configured_sources(config: Mapping[str, Any]) -> set[str]:
    configured = {"clockify", "fathom", "multica_issues"}
    if config["calendly_optional"] is False:
        configured.add("calendly")
    fleet_path = _path(config, "root") / "fleet.json"
    if not fleet_path.exists():
        return configured
    fleet = _json_file(fleet_path, "fleet")
    machines = fleet.get("machines") if isinstance(fleet, Mapping) else None
    if not isinstance(machines, list):
        raise CycleError("fleet machines are invalid")
    for machine in machines:
        if not isinstance(machine, Mapping) or machine.get("enabled", True) is False:
            continue
        name = machine.get("name")
        if not isinstance(name, str) or not name:
            raise CycleError("fleet machine identity is invalid")
        configured.update((f"sessions/{name}", f"repositories/{name}"))
    return configured


def _audit_bundle(stage: Mapping[str, Any]) -> tuple[
    dict[str, str], Mapping[str, Any], str, bool | None,
]:
    run_dir = Path(str(stage.get("run_dir") or ""))
    try:
        bundle = collector_receipts.load_collector_source_bundle(
            run_dir / "completion-bundle.json", run_dir=run_dir
        )
        ledger = json.loads(
            bundle.verified_artifact_bytes["evidence/evidence-ledger.json"]
        )
        report = json.loads(bundle.verified_artifact_bytes["run-report.json"])
    except (
        collector_receipts.CollectorReceiptError, KeyError, json.JSONDecodeError,
    ) as exc:
        raise CycleError("coverage audit collector bundle is invalid") from exc
    manifest = ledger.get("manifest") if isinstance(ledger, Mapping) else None
    inventory = manifest.get("source_inventory") if isinstance(manifest, Mapping) else None
    compatibility = stage.get("compatibility_version")
    bundle_digest = stage.get("bundle_digest")
    if (
        not isinstance(inventory, Mapping)
        or not isinstance(compatibility, str)
        or not isinstance(bundle_digest, str)
    ):
        raise CycleError("coverage audit source identity is invalid")
    if (
        stage.get("slice_id") != bundle.slice_id
        or stage.get("since_utc") != bundle.since_utc
        or stage.get("until_utc") != bundle.until_utc
        or bundle_digest not in {
            bundle.source_bundle_digest,
            bundle.legacy_completion_bundle_digest,
        }
    ):
        raise CycleError("coverage audit collector bundle identity drifted")
    mode = report.get("collection_mode") if isinstance(report, Mapping) else None
    snapshotted_optional = (
        mode.get("calendly_optional") if isinstance(mode, Mapping) else None
    )
    if snapshotted_optional is not None and not isinstance(snapshotted_optional, bool):
        raise CycleError("coverage audit collection mode is invalid")
    return ({
        "since_utc": bundle.since_utc,
        "until_utc": bundle.until_utc,
        "slice_id": bundle.slice_id,
        "compatibility_version": compatibility,
    }, inventory, bundle_digest, snapshotted_optional)


def _audit_inventory_complete(
    source: str, details: object, snapshotted_optional: bool | None,
) -> bool:
    if not isinstance(details, Mapping):
        return False
    if details.get("status") == "complete":
        return True
    if details.get("status") == "excluded":
        if source == "calendly" and snapshotted_optional is True:
            return True
    return False


def _audit_debt_covers_gap(
    item: source_coverage.DebtItem, source: str, identity: Mapping[str, str],
) -> bool:
    machine = source.partition("/")[2]
    allowed_sources = {source}
    if machine:
        allowed_sources.add(f"peer/{machine}")
    if item.interval.source == "runner/unclassified":
        compatible = (
            item.interval.compatibility_version == GENERIC_COMPATIBILITY_VERSION
        )
    else:
        compatible = (
            item.interval.compatibility_version == identity["compatibility_version"]
        )
    return (
        item.interval.source in allowed_sources | {"runner/unclassified"}
        and item.interval.since_utc == identity["since_utc"]
        and item.interval.until_utc == identity["until_utc"]
        and item.interval.slice_id == identity["slice_id"]
        and compatible
    )


def source_interval_coverage_audit(config: Mapping[str, Any]) -> dict[str, Any]:
    """Derive a transient observer report from verified bundles and debt state."""
    state_dir = _path(config, "state_dir")
    state = _state(
        state_dir / "review-cycle-state.json",
        recovery_since=str(config["recovery_since"]),
    )
    store = source_coverage.SourceDebtStore.from_document(
        source_coverage.read(state_dir / "source-coverage.json")
    )
    configured = _audit_configured_sources(config)
    verified: list[tuple[dict[str, str], Mapping[str, Any], str, bool | None]] = []
    for raw in state["slices"].values():
        if not isinstance(raw, Mapping):
            raise CycleError("cycle state slice is invalid")
        stage = raw.get("source") or raw.get("source_parent")
        if isinstance(stage, Mapping):
            verified.append(_audit_bundle(stage))

    intervals: dict[str, dict[str, Any]] = {}
    for identity, inventory, bundle_digest, snapshotted_optional in verified:
        for raw_source, details in inventory.items():
            source = str(raw_source)
            if _audit_inventory_complete(source, details, snapshotted_optional):
                row = {
                    "source": source, **identity, "status": "complete",
                    "completion_bundle_digest": bundle_digest,
                }
                interval = source_coverage.SourceInterval(**{
                    field: row[field] for field in SOURCE_INTERVAL_FIELDS
                })
                intervals[interval.debt_id] = row

    items: dict[str, source_coverage.DebtItem] = {}
    for event in store.document()["events"]:
        debt_id = str(event["debt_id"])
        item = store.get(debt_id)
        if item is not None:
            items[debt_id] = item
    for debt_id, item in items.items():
        row: dict[str, Any] = item.interval.document()
        if item.status == "resolved":
            row.update(
                status="resolved",
                completion_bundle_digest=item.completion_bundle_digest,
            )
        else:
            row.update(status="active", resume_state_digest=item.resume_state_digest)
        intervals[debt_id] = row

    for identity, inventory, _bundle_digest, snapshotted_optional in verified:
        for source in configured:
            if _audit_inventory_complete(
                source, inventory.get(source), snapshotted_optional,
            ):
                continue
            covering = [
                item for item in items.values()
                if _audit_debt_covers_gap(item, source, identity)
            ]
            if not covering:
                continue
            covering.sort(key=lambda item: (
                item.interval.source == "runner/unclassified",
                item.interval.source.startswith("peer/"),
                item.debt_id,
            ))
            item = covering[0]
            projected = {
                "source": source,
                **identity,
                "status": "resolved" if item.status == "resolved" else "active",
                "operational_debt_id": item.debt_id,
            }
            if item.status == "resolved":
                projected["completion_bundle_digest"] = item.completion_bundle_digest
            else:
                projected["resume_state_digest"] = item.resume_state_digest
            projected_interval = source_coverage.SourceInterval(**{
                field: projected[field] for field in SOURCE_INTERVAL_FIELDS
            })
            intervals[projected_interval.debt_id] = projected

    for identity, inventory, _bundle_digest, snapshotted_optional in verified:
        for source in configured:
            if _audit_inventory_complete(
                source, inventory.get(source), snapshotted_optional,
            ):
                continue
            if not any(
                _audit_debt_covers_gap(item, source, identity)
                for item in items.values()
            ):
                raise CycleError("verified cycle evidence has an unbound source interval gap")

    ordered = sorted(
        intervals.values(),
        key=lambda row: tuple(str(row[field]) for field in SOURCE_INTERVAL_FIELDS),
    )
    windows = sorted({
        (str(row["since_utc"]), str(row["until_utc"]), str(row["slice_id"]))
        for row in ordered
    })
    frontiers: dict[str, str | None] = {}
    for source in sorted(configured):
        frontier: str | None = None
        for since_utc, until_utc, slice_id in windows:
            rows = [
                row for row in ordered
                if row["source"] == source
                and row["since_utc"] == since_utc
                and row["until_utc"] == until_utc
                and row["slice_id"] == slice_id
            ]
            if (
                len(rows) != 1
                or rows[0]["status"] not in {"complete", "resolved"}
                or (frontier is not None and since_utc != frontier)
            ):
                break
            frontier = until_utc
        frontiers[source] = frontier
    return {
        "schema_version": SOURCE_INTERVAL_COVERAGE_AUDIT_SCHEMA_VERSION,
        "horizon_until_utc": max(
            (str(row["until_utc"]) for row in ordered), default=None,
        ),
        "configured_sources": sorted(configured),
        "frontiers": frontiers,
        "intervals": ordered,
        "active_debt_ids": sorted(item.debt_id for item in store.active()),
    }


def _coverage_audit_output_path(
    config: Mapping[str, Any], requested: Path,
) -> Path:
    expected = (
        _path(config, "root") / "reports" / "source-interval-coverage-audit.json"
    )
    if (
        not requested.is_absolute()
        or requested != expected
        or requested.resolve() != expected
    ):
        raise CycleError(
            "audit coverage output must be the configured transient reports path"
        )
    return expected


def _generic_interval(
    config: Mapping[str, Any], since: str, until: str,
) -> source_coverage.SourceInterval:
    since_utc, until_utc = _expected_interval(config, since, until)
    zone = ZoneInfo(str(config["timezone"]))
    planned = collector_slices.plan_slices(
        dt.datetime.fromisoformat(since_utc.replace("Z", "+00:00")),
        dt.datetime.fromisoformat(until_utc.replace("Z", "+00:00")),
        zone=zone, max_days=2,
    )
    if len(planned) != 1:
        raise CycleError("generic obligation must be one bounded collector slice")
    return source_coverage.SourceInterval(
        source="runner/unclassified",
        since_utc=since_utc,
        until_utc=until_utc,
        slice_id=planned[0].slice_id,
        compatibility_version=GENERIC_COMPATIBILITY_VERSION,
    )


def _record_generic_failure(
    store: source_coverage.SourceDebtStore,
    interval: source_coverage.SourceInterval,
    *, failure_class: str, resume_state_digest: str,
    classification: bool = False,
) -> source_coverage.DebtItem:
    if _same_active_failure(
        store, interval, failure_class=failure_class,
        resume_state_digest=resume_state_digest,
    ):
        return next(item for item in store.active() if item.debt_id == interval.debt_id)
    item = store.record_failure(
        interval, failure_class=failure_class, retryable=True,
        resume_state_digest=resume_state_digest, attempted_at=_attempted_at(),
    )
    if classification or item.retry_count >= GENERIC_RETRY_LIMIT:
        item = store.exhaust(item.debt_id, terminal_reason="retry_limit")
    return item


def _runner_attempt(record: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Validate the runtime binding without changing the source-attempt contract."""
    if "runner_attempt" not in record:
        return None
    raw = record["runner_attempt"]
    source = record.get("source_attempt")
    if (
        not isinstance(raw, Mapping)
        or set(raw) != {"runtime_identity_digest", "source_attempt_ordinal", "status"}
        or raw.get("status") not in {"pending", "finished"}
        or not _valid_digest(raw.get("runtime_identity_digest"))
        or isinstance(raw.get("source_attempt_ordinal"), bool)
        or not isinstance(raw.get("source_attempt_ordinal"), int)
        or raw["source_attempt_ordinal"] < 1
        or not isinstance(source, Mapping)
        or set(source) != {
            "ordinal", "command_digest", "resume_state_digest", "status",
            "advance_frontier",
        }
        or isinstance(source.get("ordinal"), bool)
        or not isinstance(source.get("ordinal"), int)
        or source.get("ordinal") != raw["source_attempt_ordinal"]
        or source.get("status") not in {"started", "finished"}
        or raw["status"] == "finished" and source.get("status") != "finished"
        or (
            raw["status"] == "pending" and source.get("status") == "finished"
            and not isinstance(record.get("source"), Mapping)
        )
        or not _valid_digest(source.get("command_digest"))
        or not _valid_digest(source.get("resume_state_digest"))
        or not isinstance(source.get("advance_frontier"), bool)
    ):
        raise CycleError("stored runner attempt is invalid")
    return raw


def _source_attempt(
    record: dict[str, Any], command: list[str],
    interval: source_coverage.SourceInterval, *, advance_frontier: bool,
) -> dict[str, Any]:
    existing = record.get("source_attempt")
    if existing is not None:
        if not isinstance(existing, Mapping) or set(existing) != {
            "ordinal", "command_digest", "resume_state_digest", "status",
            "advance_frontier",
        } or existing.get("status") not in {"started", "finished"}:
            raise CycleError("stored source attempt identity is invalid")
        if existing["status"] == "started":
            return dict(existing)
        ordinal = existing.get("ordinal")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 1:
            raise CycleError("stored source attempt ordinal is invalid")
    else:
        ordinal = 0
    command_digest = _value_digest(command)
    attempt = {
        "ordinal": ordinal + 1,
        "command_digest": command_digest,
        "resume_state_digest": _value_digest({
            "attempt_ordinal": ordinal + 1,
            "command_digest": command_digest,
            "interval": interval.document(),
        }),
        "status": "started",
        "advance_frontier": advance_frontier,
    }
    record["source_attempt"] = attempt
    return attempt


def _attempt_failure_exists(
    store: source_coverage.SourceDebtStore,
    interval: source_coverage.SourceInterval,
    attempt: Mapping[str, Any],
) -> bool:
    return any(
        item.debt_id == interval.debt_id
        and item.resume_state_digest == attempt.get("resume_state_digest")
        for item in store.active()
    )


def _finish_attempt(
    record: dict[str, Any], attempt: Mapping[str, Any], *, finish_runner: bool = True,
) -> None:
    record["source_attempt"] = {**dict(attempt), "status": "finished"}
    if finish_runner:
        _finish_runner_attempt(record)


def _finish_runner_attempt(record: dict[str, Any]) -> None:
    if "runner_attempt" in record:
        record["runner_attempt"] = {**record["runner_attempt"], "status": "finished"}


def _record_advances_frontier(
    record: Mapping[str, Any], requested: bool,
) -> bool:
    attempt = record.get("source_attempt")
    return requested or (
        isinstance(attempt, Mapping) and attempt.get("advance_frontier") is True
    )


def _resolve_generic(
    store: source_coverage.SourceDebtStore,
    generic: source_coverage.DebtItem | None,
    source: Mapping[str, Any],
) -> None:
    if generic is None:
        return
    run_dir = Path(str(source["run_dir"]))
    try:
        bundle = (
            collector_receipts.load_collector_source_bundle(
                run_dir / "completion-bundle.json", run_dir=run_dir
            )
            if source.get("stage_kind") == "collector_source"
            else collector_receipts.load_completion_bundle(
                run_dir / "completion-bundle.json", run_dir=run_dir
            )
        )
    except collector_receipts.CollectorReceiptError as exc:
        raise CycleError("generic completion bundle cannot be verified") from exc
    if (
        bundle.since_utc != generic.interval.since_utc
        or bundle.until_utc != generic.interval.until_utc
        or bundle.slice_id != generic.interval.slice_id
    ):
        raise CycleError("generic completion does not match exact debt identity")
    if generic.status != "resolved" and any(
        item.debt_id == generic.debt_id for item in store.active()
    ):
        store.record_complete(
            generic.interval,
            completion_bundle_digest=str(source["bundle_digest"]),
            completed_at=_attempted_at(),
        )


def _recompute_completed_through(
    config: Mapping[str, Any], state: dict[str, Any],
) -> None:
    cursor = _date(config["recovery_since"], "recovery_since")
    advanced = False
    slices = state.get("slices", {})
    while True:
        record = slices.get(cursor.isoformat()) if isinstance(slices, Mapping) else None
        if not isinstance(record, Mapping) or record.get("status") not in {
            "delivered", "delivered_with_exceptions",
        }:
            break
        until = _date(record.get("until"), "slice until")
        if until <= cursor:
            raise CycleError("delivered slice interval does not advance")
        cursor = until
        advanced = True
    state["completed_through"] = cursor.isoformat() if advanced else None


def _stored_snapshot_digests(record: Mapping[str, Any]) -> dict[str, str]:
    value = record.get("expected_snapshot_digests")
    expected_names = {
        "period-manifest.json", "routing.json", "review-corrections.jsonl",
        "review-acceptance.jsonl",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != expected_names
        or not all(isinstance(item, str) and item.startswith("sha256:") for item in value.values())
    ):
        raise CycleError("stored immutable snapshot identity is invalid")
    return dict(value)


def _routing_transition_source(
    config: Mapping[str, Any], record: dict[str, Any], since: str, until: str,
    manifest_path: Path,
) -> dict[str, Any] | None:
    """Adopt one verified legacy orphan; never relax the original input binding."""
    frozen = _stored_snapshot_digests(record)
    receipt = record.get("routing_transition")
    if receipt is not None:
        if not isinstance(receipt, Mapping):
            raise CycleError("routing transition receipt is invalid")
        body = {key: value for key, value in receipt.items() if key != "receipt_digest"}
        if receipt.get("receipt_digest") != _value_digest(body):
            raise CycleError("routing transition receipt has drifted")
        adopted = receipt.get("adopted_snapshot_digests")
        if not isinstance(adopted, Mapping) or set(adopted) != set(frozen):
            raise CycleError("routing transition receipt inputs are invalid")
        adopted = dict(adopted)
    else:
        adopted = _expected_snapshot_digests(config, manifest_path)
    if receipt is None and adopted == frozen:
        return None
    runtime = config.get("_runtime_identity")
    runner = _runner_attempt(record)
    attempt = record.get("source_attempt")
    if (
        not isinstance(runtime, Mapping) or runner is None
        or not isinstance(attempt, Mapping)
        or receipt is None and (
            (runner["status"], attempt.get("status")) not in {
                ("pending", "started"), ("finished", "finished"),
            }
            or any(key in record for key in ("source", "replay", "delivery_receipt"))
        )
        or any(adopted[name] != frozen[name] for name in frozen if name != "routing.json")
        or adopted["routing.json"] == frozen["routing.json"]
        or _digest(manifest_path) != frozen["period-manifest.json"]
    ):
        raise CycleError("routing transition does not match the stored launch and immutable inputs")
    root = _path(config, "root")
    if runtime.get("canonical_root") != str(root) or runtime.get("git_dirty") not in (None, False):
        raise CycleError("routing transition release runtime is invalid")
    try:
        helper_path = Path(__file__).resolve().parents[1] / "ops/systemd/user/clockify_review_cycle_release.py"
        spec = importlib.util.spec_from_file_location("clockify_transition_release", helper_path)
        if spec is None or spec.loader is None:
            raise ValueError("release identity validator is unavailable")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        authority_root = root if receipt is None else Path(str(receipt.get("adopting_release_root") or ""))
        authority_sha = root.name if receipt is None else str(receipt.get("adopting_release_sha") or "")
        identity = helper._identity(authority_root, authority_sha)
        if receipt is None and runtime.get("git_sha") not in (None, identity["git_sha"]):
            raise ValueError("runtime SHA differs from release")
        if adopted["routing.json"] != "sha256:" + identity["routing_sha256"]:
            raise ValueError("routing differs from immutable release")
        if receipt is None:
            if _path(config, "routing", file=True) != root / "routing.json":
                raise ValueError("routing path differs from release")
            _validate_config_identity(config)
    except (OSError, ValueError) as exc:
        raise CycleError("routing transition release identity cannot be verified") from exc

    def verified(path: Path) -> dict[str, Any]:
        report = _json_file(path / "run-report.json", "transition launch report")
        if not isinstance(report, Mapping):
            raise CycleError("routing transition launch report is invalid")
        launch = report.get("runtime_identity")
        if not isinstance(launch, Mapping) or _value_digest(dict(launch)) != runner["runtime_identity_digest"]:
            raise CycleError("routing transition source differs from exact launch runtime")
        launch_root = Path(str(launch.get("canonical_root") or ""))
        launch_identity = helper._identity(launch_root, launch_root.name)
        if (
            launch.get("git_dirty") not in (None, False)
            or launch.get("git_sha") not in (None, launch_identity["git_sha"])
            or launch_identity["routing_sha256"] != identity["routing_sha256"]
        ):
            raise CycleError("routing transition original release differs")
        launch_config = {**config, "root": str(launch_root), "routing": str(launch_root / "routing.json")}
        if attempt.get("command_digest") != _value_digest(_review_command(launch_config, since, until)):
            raise CycleError("routing transition command differs from original launch")
        completed = clockify_review_run._adopt_completed_resume(path)
        if completed is None:
            raise CycleError("routing transition source is not completed")
        stage = _validate_stage(config, completed, since, until, replay=False, expected_snapshot_digests=adopted,
            expected_runtime_digest=runner["runtime_identity_digest"], historical_state_validation=True)
        analysis = _json_file(path / "semantic-analysis.json", "transition semantic analysis")
        if clockify_review_run._analysis_is_inference_backed(analysis):
            cache = _safe_run_file(path, str(path / "analyzer-cache-used.jsonl"), "transition sealed analyzer cache")
            snapshot = analysis.get("analyzer_cache", {}).get("snapshot", {})
            if snapshot.get("path") != cache.name or snapshot.get("sha256") != _digest(cache).removeprefix("sha256:"):
                raise CycleError("routing transition sealed analyzer cache differs")
            clockify_review_run._preflight_replay_analyzer_cache(path, cache, analysis)
        return stage

    attempt_identity = {key: value for key, value in attempt.items() if key != "status"}
    if receipt is not None:
        if not isinstance(receipt, Mapping):
            raise CycleError("routing transition receipt is invalid")
        body = {key: value for key, value in receipt.items() if key != "receipt_digest"}
        if (
            set(body) != {"schema_version", "eligibility_basis", "since", "until", "frozen_snapshot_digests", "adopted_snapshot_digests", "runtime_identity_digest", "release_identity_digest", "adopting_release_root", "adopting_release_sha", "source_attempt", "source"}
            or body.get("schema_version") != "clockify-routing-transition/v1"
            or body.get("eligibility_basis") != "unique verified completed legacy-attempt result"
            or
            receipt.get("receipt_digest") != _value_digest(body)
            or body.get("frozen_snapshot_digests") != frozen
            or body.get("adopted_snapshot_digests") != adopted
            or body.get("runtime_identity_digest") != runner["runtime_identity_digest"]
            or body.get("release_identity_digest") != _value_digest(identity)
            or body.get("source_attempt") != attempt_identity
            or body.get("since") != since or body.get("until") != until
        ):
            raise CycleError("routing transition receipt has drifted")
        path = _safe_run_file(_runs_dir(config), body.get("source", {}).get("result_path"), "transition source result")
        source = verified(path.parent)
        if body.get("source") != source:
            raise CycleError("routing transition source has drifted")
        return source
    candidates = []
    for path in sorted(_runs_dir(config).glob("*/autopilot-result.json")):
        try:
            candidates.append(verified(path.parent))
        except (CycleError, OSError, ValueError):
            continue
    if len(candidates) != 1:
        raise CycleError("routing transition requires a unique verified completed source")
    source = candidates[0]
    body = {
        "schema_version": "clockify-routing-transition/v1",
        "eligibility_basis": "unique verified completed legacy-attempt result",
        "since": since, "until": until,
        "frozen_snapshot_digests": frozen, "adopted_snapshot_digests": adopted,
        "runtime_identity_digest": runner["runtime_identity_digest"],
        "release_identity_digest": _value_digest(identity),
        "adopting_release_root": str(root), "adopting_release_sha": identity["git_sha"],
        "source_attempt": attempt_identity, "source": source,
    }
    record["routing_transition"] = {**body, "receipt_digest": _value_digest(body)}
    return source


def _fresh_input_binding(
    config: Mapping[str, Any], record: dict[str, Any], since: str, until: str,
    manifest: Path,
) -> dict[str, str]:
    """Pin one modern attempt without replacing a terminal legacy contract."""
    frozen = _stored_snapshot_digests(record)
    existing = record.get("fresh_input_binding")
    if existing is not None and not isinstance(existing, Mapping):
        raise CycleError("fresh input binding is invalid")
    runtime = config.get("_runtime_identity")
    if not isinstance(runtime, Mapping):
        raise CycleError("fresh routing binding requires exact runtime identity")
    root = _path(config, "root")
    try:
        helper_path = Path(__file__).resolve().parents[1] / "ops/systemd/user/clockify_review_cycle_release.py"
        spec = importlib.util.spec_from_file_location("clockify_fresh_release", helper_path)
        if spec is None or spec.loader is None:
            raise ValueError("release validator unavailable")
        helper = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        authority = root if existing is None else Path(str(existing.get("release_root") or ""))
        identity = helper._identity(authority, authority.name)
    except (OSError, ValueError) as exc:
        raise CycleError("fresh routing release identity cannot be verified") from exc
    if existing is not None:
        if not isinstance(existing, Mapping):
            raise CycleError("fresh input binding is invalid")
        body = {key: value for key, value in existing.items() if key != "binding_digest"}
        expected = body.get("snapshot_digests")
        attempt = record.get("source_attempt")
        runner = _runner_attempt(record)
        if (
            set(body) != {"schema_version", "since", "until", "frozen_snapshot_digests", "snapshot_digests", "runtime_identity", "release_root", "release_identity_digest", "source_attempt", "history_digest"}
            or body.get("schema_version") != "clockify-fresh-input-binding/v1"
            or existing.get("binding_digest") != _value_digest(body)
            or body.get("since") != since or body.get("until") != until
            or body.get("frozen_snapshot_digests") != frozen
            or not isinstance(expected, Mapping) or set(expected) != set(frozen)
            or any(expected[name] != frozen[name] for name in frozen if name != "routing.json")
            or expected["routing.json"] != "sha256:" + identity["routing_sha256"]
            or body.get("release_identity_digest") != _value_digest(identity)
            or body.get("history_digest") != _value_digest(record.get("source_attempt_history"))
            or not isinstance(attempt, Mapping) or runner is None
            or body.get("source_attempt") != {key: value for key, value in attempt.items() if key != "status"}
            or runner["runtime_identity_digest"] != _value_digest(body.get("runtime_identity"))
            or _digest(manifest) != expected["period-manifest.json"]
        ):
            raise CycleError("fresh input binding has drifted")
        if record.get("source") is None and (
            dict(runtime) != body["runtime_identity"]
            or _expected_snapshot_digests(config, manifest) != dict(expected)
            or _value_digest(_review_command(config, since, until)) != attempt["command_digest"]
        ):
            raise CycleError("active fresh attempt runtime or inputs have drifted")
        return dict(expected)
    attempt = record.get("source_attempt")
    current = _expected_snapshot_digests(config, manifest)
    if (
        "runner_attempt" in record or "routing_transition" in record
        or any(key in record for key in ("source", "replay", "delivery_receipt"))
        or not isinstance(attempt, Mapping) or attempt.get("status") != "finished"
        or set(attempt) != {"ordinal", "command_digest", "resume_state_digest", "status", "advance_frontier"}
        or isinstance(attempt.get("ordinal"), bool) or not isinstance(attempt.get("ordinal"), int) or attempt["ordinal"] < 1
        or not _valid_digest(attempt.get("command_digest")) or not _valid_digest(attempt.get("resume_state_digest"))
        or not isinstance(attempt.get("advance_frontier"), bool)
        or current["routing.json"] == frozen["routing.json"]
        or any(current[name] != frozen[name] for name in frozen if name != "routing.json")
        or runtime.get("canonical_root") != str(root) or runtime.get("git_dirty") not in (None, False)
        or runtime.get("git_sha") not in (None, identity["git_sha"])
        or _path(config, "routing", file=True) != root / "routing.json"
        or current["routing.json"] != "sha256:" + identity["routing_sha256"]
    ):
        raise CycleError("fresh routing binding is not an eligible terminal legacy attempt")
    _validate_config_identity(config)
    for result in _runs_dir(config).glob("*/autopilot-result.json"):
        try:
            _validate_stage(config, result, since, until, replay=False, expected_snapshot_digests=current)
        except (CycleError, OSError, ValueError):
            continue
        raise CycleError("completed current-input orphan requires binding; fresh attempt forbidden")
    history = record.get("source_attempt_history", [])
    if not isinstance(history, list):
        raise CycleError("source attempt history is invalid")
    record["source_attempt_history"] = [*history, {"source_attempt": dict(attempt), "expected_snapshot_digests": frozen}]
    new_attempt = _source_attempt(record, _review_command(config, since, until), _generic_interval(config, since, until), advance_frontier=attempt["advance_frontier"])
    record["runner_attempt"] = {"runtime_identity_digest": _value_digest(dict(runtime)), "source_attempt_ordinal": new_attempt["ordinal"], "status": "pending"}
    body = {
        "schema_version": "clockify-fresh-input-binding/v1", "since": since, "until": until,
        "frozen_snapshot_digests": frozen, "snapshot_digests": current,
        "runtime_identity": dict(runtime), "release_root": str(root),
        "release_identity_digest": _value_digest(identity),
        "source_attempt": {key: value for key, value in new_attempt.items() if key != "status"},
        "history_digest": _value_digest(record["source_attempt_history"]),
    }
    record["fresh_input_binding"] = {**body, "binding_digest": _value_digest(body)}
    return current


def _verify_fresh_native_source(config: Mapping[str, Any], record: Mapping[str, Any], source: Mapping[str, Any]) -> str:
    """Require reconstructed raw coverage and modern per-host exporter proof."""
    path = Path(str(source["run_dir"]))
    try:
        if (path / "collector-source.json").exists():
            _parent, identity, _lineage = clockify_review_run._verified_collector_derivation(path)
        else:
            identity = collector_receipts.load_collector_source_bundle(path / "completion-bundle.json", run_dir=path)
        binding = record["fresh_input_binding"]
        runtime = binding["runtime_identity"]
        if identity.collector_runtime_identity != runtime:
            raise ValueError("collector runtime differs from fresh binding")
        sessions = json.loads(identity.verified_artifact_bytes["evidence/sessions.json"])
        root = Path(binding["release_root"])
        fleet = _json_file(root / "fleet.json", "fresh source fleet")
        required = {item["name"] for item in fleet.get("machines", []) if item.get("enabled", True)}
        if not required or not isinstance(sessions, list) or {item.get("machine") for item in sessions if isinstance(item, Mapping)} != required or len(sessions) != len(required):
            raise ValueError("fresh native host coverage differs from approved fleet")
        collector = clockify_review_run.clockify_sync_collect
        digest = _digest(root / "scripts/clockify_sync_collect.py").removeprefix("sha256:")
        for host in sessions:
            name = host["machine"]
            inventory = source["coverage"].get("sources", {})
            if not any(inventory.get(kind + "/" + name, {}).get("status") == "complete" for kind in ("sessions", "repositories")):
                continue  # Existing exact-source debt owns incomplete peer recovery.
            if name != str(config.get("coordinator") or "omarchy-precision"):
                attestation = host.get("canonical_export_attestation")
                export = host.get("canonical_export")
                if (
                    host.get("collector_contract") != "canonical_export_v1"
                    or not isinstance(attestation, Mapping) or not isinstance(export, Mapping)
                    or export.get("provenance") != "full_context_remote_export"
                    or attestation.get("collector_script_sha256") not in {digest, *collector.COMPATIBLE_CANONICAL_EXPORT_DIGESTS}
                    or export.get("collector_script_sha256") != attestation.get("collector_script_sha256")
                    or not isinstance(attestation.get("runtime_identity"), Mapping)
                ):
                    raise ValueError("fresh native peer lacks modern code attestation")
        if record.get("fresh_native_source_digest") not in (None, identity.source_bundle_digest):
            raise ValueError("fresh native source binding has drifted")
        return identity.source_bundle_digest
    except (OSError, ValueError, KeyError, TypeError, collector_receipts.CollectorReceiptError) as exc:
        raise CycleError("fresh source native coverage cannot be verified") from exc


def _historical_adoption_document(
    config: Mapping[str, Any], record: Mapping[str, Any], since: str, until: str,
) -> dict[str, Any] | None:
    raw_path = record.get("historical_adoption_receipt")
    if raw_path is None:
        return None
    expected_path = _path(config, "state_dir") / "historical-adoption-receipts" / f"{since}.json"
    if raw_path != str(expected_path) or expected_path.is_symlink() or not expected_path.is_file():
        raise CycleError("historical adoption receipt path is invalid")
    document = _json_file(expected_path, "historical adoption receipt")
    if not isinstance(document, dict):
        raise CycleError("historical adoption receipt is invalid")
    digest = document.get("receipt_digest")
    unsigned = {key: value for key, value in document.items() if key != "receipt_digest"}
    if (
        document.get("schema_version") not in {
            HISTORICAL_ADOPTION_SCHEMA_VERSION, DERIVED_ADOPTION_SCHEMA_VERSION,
        }
        or document.get("since") != since or document.get("until") != until
        or digest != _value_digest(unsigned)
        or record.get("historical_adoption_receipt_digest") != digest
        or document.get("frozen_snapshot_digests") != _stored_snapshot_digests(record)
    ):
        raise CycleError("historical adoption receipt identity differs")
    adopted = document.get("adopted_snapshot_digests")
    if not isinstance(adopted, Mapping) or set(adopted) != set(_stored_snapshot_digests(record)):
        raise CycleError("historical adoption input transition is invalid")
    for name in adopted:
        if not _valid_digest(adopted[name]):
            raise CycleError("historical adoption input digest is invalid")
        if name not in {"routing.json", "review-corrections.jsonl"} and adopted[name] != document["frozen_snapshot_digests"][name]:
            raise CycleError("historical adoption changed non-routing frozen inputs")
    if adopted["review-corrections.jsonl"] != document["frozen_snapshot_digests"]["review-corrections.jsonl"]:
        source = document.get("source")
        if not isinstance(source, Mapping):
            raise CycleError("historical credit adoption source is invalid")
        _verify_credit_adoption_transition(
            config, Path(str(source.get("run_dir") or "")),
            document["frozen_snapshot_digests"]["review-corrections.jsonl"],
            adopted["review-corrections.jsonl"],
        )
    return document


def _verify_historical_adoption(
    config: Mapping[str, Any], record: Mapping[str, Any], document: Mapping[str, Any],
    since: str, until: str, source: Mapping[str, Any], replay: Mapping[str, Any],
) -> None:
    if (
        document.get("source") != dict(source)
        or document.get("replay") != dict(replay)
        or source.get("runtime_identity_digest") != document.get("runtime_identity_digest")
        or replay.get("runtime_identity_digest") != document.get("runtime_identity_digest")
        or source.get("coverage", {}).get("status") != "complete"
        or source.get("coverage", {}).get("incomplete_sources") != []
    ):
        raise CycleError("historical adoption stage identity differs")
    if document.get("schema_version") == DERIVED_ADOPTION_SCHEMA_VERSION:
        if any(key in document for key in (
            "checkpoint_root", "checkpoint_manifest_digest", "checkpoint_manifest_text",
        )):
            raise CycleError("derived adoption cannot claim a checkpoint proof")
        _interval_from_derived_stage(
            config, since, until, source, document.get("source_provenance"),
        )
    else:
        checkpoint_root = _canonical_runtime_path(
            str(document.get("checkpoint_root")), label="historical checkpoint root"
        )
        checkpoint_digest = document.get("checkpoint_manifest_digest")
        manifest_text = document.get("checkpoint_manifest_text")
        if not _valid_digest(checkpoint_digest) or not isinstance(manifest_text, str):
            raise CycleError("historical checkpoint digest is invalid")
        _interval_from_stage(
            config, "runner/unclassified", source,
            checkpoint_root=checkpoint_root,
            checkpoint_manifest_digest=checkpoint_digest,
            checkpoint_manifest_text=manifest_text,
        )
    path = _safe_run_file(
        _runs_dir(config), document.get("publication_result"), "historical publication result"
    )
    if path.name not in {"sheet-publish-result.json", "autopilot-result.json"} or (
        _digest(path) != document.get("publication_result_digest")
    ):
        raise CycleError("historical publication result identity differs")
    title = _sheet_title(config["monthly_sheet_title_template"], since=since)
    expected = _expected_publication_receipts(config, source, sheet_title=title)
    if document.get("publication_receipts") != expected:
        raise CycleError("historical publication row identity differs")
    _validated_publication_document(_json_file(path, "publisher result"), expected)


def _validate_delivered_state(config: Mapping[str, Any], state: Mapping[str, Any]) -> None:
    slices = state.get("slices")
    if not isinstance(slices, Mapping):
        raise CycleError("cycle state slices are invalid")
    for since, raw_record in slices.items():
        if not isinstance(raw_record, Mapping) or raw_record.get("status") not in {
            "delivered", "delivered_with_exceptions",
        }:
            continue
        until = raw_record.get("until")
        if not isinstance(since, str) or not isinstance(until, str):
            raise CycleError("delivered slice identity is invalid")
        events_path, manifest_path = _period_paths(_path(config, "state_dir"), since)
        if not events_path.is_file() or not manifest_path.is_file():
            raise CycleError("delivered slice period evidence is missing")
        expected_snapshots = _stored_snapshot_digests(raw_record)
        if "fresh_input_binding" in raw_record:
            expected_snapshots = _fresh_input_binding(config, dict(raw_record), since, until, manifest_path)
        if "routing_transition" in raw_record:
            transitioned = _routing_transition_source(config, dict(raw_record), since, until, manifest_path)
            if transitioned is None:
                raise CycleError("delivered routing transition source is missing")
            expected_snapshots = transitioned["snapshot_digests"]
        adoption = _historical_adoption_document(config, raw_record, since, until)
        stage_config = config
        if adoption is not None:
            expected_snapshots = dict(adoption["adopted_snapshot_digests"])
            stage_config = dict(config)
            stage_config.setdefault(
                "_runtime_identity",
                clockify_review_run.clockify_sync_collect.collector_runtime_identity(),
            )
        verified_manifest = _ensure_period(
            config, _path(config, "state_dir"), since, until, bind_inputs=False
        )
        if raw_record.get("period_manifest") != str(verified_manifest):
            raise CycleError("delivered slice period manifest identity has drifted")
        source = _stage_from_state(
            stage_config, raw_record, "source", since, until, replay=False,
            expected_snapshot_digests=expected_snapshots,
        )
        if source is None:
            raise CycleError("delivered slice has no verified source stage")
        if "fresh_input_binding" in raw_record:
            _verify_fresh_native_source(config, raw_record, source)
        replay = _stage_from_state(
            stage_config, raw_record, "replay", since, until, replay=True,
            expected_snapshot_digests=source["snapshot_digests"],
            source_run_id=str(source["run_id"]), source_run_dir=str(source["run_dir"]),
        )
        if replay is None:
            raise CycleError("delivered slice has no verified replay stage")
        if adoption is not None:
            _verify_historical_adoption(
                stage_config, raw_record, adoption, since, until, source, replay,
            )
        receipt = raw_record.get("delivery_receipt")
        if not isinstance(receipt, str):
            raise CycleError("delivered slice has no delivery receipt")
        title = _sheet_title(config["monthly_sheet_title_template"], since=since)
        _verify_delivery_receipt(
            Path(receipt), config, since, until, source, replay, sheet_title=title
        )


def _attempt_id(debt_id: str, ordinal: int) -> str:
    return _value_digest({
        "contract": "source-debt-recovery-attempt/v1",
        "attempt_ordinal": ordinal,
        "debt_id": debt_id,
    })


def _valid_digest(value: object) -> bool:
    return (
        isinstance(value, str) and len(value) == 71 and value.startswith("sha256:")
        and all(character in string.hexdigits.lower() for character in value[7:])
        and value[7:] == value[7:].lower()
    )


def _validate_recovery_attempt(
    raw: object, *, debt_id: str, parent: Mapping[str, Any], command: list[str] | None = None,
) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise CycleError("stored recovery attempt is invalid")
    base = {
        "schema_version", "debt_id", "attempt_ordinal", "attempt_id",
        "parent_run_dir", "parent_bundle_digest", "command_digest", "phase",
    }
    verified = {
        "result_path", "result_digest", "returned_bundle_digest",
        "requested_source_outcome", "recovery_receipt_path",
        "recovery_receipt_digest",
    }
    phase = raw.get("phase")
    expected_keys = base if phase == "started" else base | verified
    if set(raw) != expected_keys or phase not in {
        "started", "verified_complete", "verified_incomplete",
        "finished_complete", "finished_incomplete",
    }:
        raise CycleError("stored recovery attempt shape is invalid")
    ordinal = raw.get("attempt_ordinal")
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 1:
        raise CycleError("stored recovery attempt ordinal is invalid")
    if (
        raw.get("schema_version") != RECOVERY_ATTEMPT_SCHEMA_VERSION
        or raw.get("debt_id") != debt_id
        or raw.get("attempt_id") != _attempt_id(debt_id, ordinal)
        or raw.get("parent_run_dir") != parent.get("run_dir")
        or raw.get("parent_bundle_digest") != parent.get("bundle_digest")
        or not _valid_digest(raw.get("command_digest"))
    ):
        raise CycleError("stored recovery attempt identity is invalid")
    if command is not None and raw.get("command_digest") != _value_digest(command):
        raise CycleError("stored recovery attempt command has drifted")
    if phase != "started" and (
        not isinstance(raw.get("result_path"), str)
        or not _valid_digest(raw.get("result_digest"))
        or not _valid_digest(raw.get("returned_bundle_digest"))
        or not isinstance(raw.get("recovery_receipt_path"), str)
        or not Path(str(raw.get("recovery_receipt_path"))).is_absolute()
        or not _valid_digest(raw.get("recovery_receipt_digest"))
        or raw.get("requested_source_outcome") not in {"complete", "incomplete"}
        or not phase.endswith(str(raw.get("requested_source_outcome")))
    ):
        raise CycleError("stored verified recovery outcome is invalid")
    return dict(raw)


def _recovery_attempt(
    record: dict[str, Any], debt: source_coverage.DebtItem,
    parent: Mapping[str, Any], config: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    attempts_raw = record.get("recovery_attempts", {})
    if not isinstance(attempts_raw, Mapping):
        raise CycleError("stored recovery attempts are invalid")
    attempts = {str(key): dict(value) for key, value in attempts_raw.items() if isinstance(value, Mapping)}
    if len(attempts) != len(attempts_raw):
        raise CycleError("stored recovery attempts are invalid")
    existing = attempts.get(debt.debt_id)
    ordinal = 1
    if existing is not None:
        attempt_id = existing.get("attempt_id")
        if not isinstance(attempt_id, str):
            raise CycleError("stored recovery attempt identity is invalid")
        validation_parent = parent
        if str(existing.get("phase", "")).startswith("finished_"):
            validation_parent = {
                "run_dir": existing.get("parent_run_dir"),
                "bundle_digest": existing.get("parent_bundle_digest"),
            }
        command = _recovery_command(
            config, Path(str(validation_parent["run_dir"])),
            debt.interval.source, attempt_id
        )
        checked = _validate_recovery_attempt(
            existing, debt_id=debt.debt_id, parent=validation_parent, command=command
        )
        if not str(checked["phase"]).startswith("finished_"):
            return checked, command
        ordinal = int(checked["attempt_ordinal"]) + 1
    attempt_id = _attempt_id(debt.debt_id, ordinal)
    command = _recovery_command(
        config, Path(str(parent["run_dir"])), debt.interval.source, attempt_id
    )
    attempt = {
        "schema_version": RECOVERY_ATTEMPT_SCHEMA_VERSION,
        "debt_id": debt.debt_id,
        "attempt_ordinal": ordinal,
        "attempt_id": attempt_id,
        "parent_run_dir": str(parent["run_dir"]),
        "parent_bundle_digest": str(parent["bundle_digest"]),
        "command_digest": _value_digest(command),
        "phase": "started",
    }
    attempts[debt.debt_id] = attempt
    record["recovery_attempts"] = attempts
    return attempt, command


def _validate_recovery_stage(
    config: Mapping[str, Any], result_path: Path, since: str, until: str,
    *, parent: Mapping[str, Any], debt: source_coverage.DebtItem, attempt_id: str,
) -> tuple[dict[str, Any], str]:
    stage = _validate_stage(
        config, result_path, since, until, replay=False,
        expected_snapshot_digests=parent["snapshot_digests"],
    )
    try:
        bundle, status = clockify_review_run.verify_source_debt_recovery_completion(
            Path(str(stage["run_dir"])), parent_run_dir=Path(str(parent["run_dir"])),
            source=debt.interval.source, attempt_id=attempt_id,
        )
        receipt = clockify_source_debt_recover.verify_recovery_receipt(
            Path(str(stage["run_dir"]))
        )
    except (
        OSError, ValueError, clockify_review_run.ReviewRunError,
        clockify_source_debt_recover.SourceDebtRecoveryError,
    ) as exc:
        raise CycleError("recovery completion identity is invalid") from exc
    if (
        bundle.bundle_digest != stage["bundle_digest"]
        or bundle.since_utc != debt.interval.since_utc
        or bundle.until_utc != debt.interval.until_utc
        or bundle.slice_id != debt.interval.slice_id
    ):
        raise CycleError("recovery completion does not match exact debt interval")
    return {
        **stage,
        "recovery_receipt_path": str(receipt.path),
        "recovery_receipt_digest": receipt.digest,
    }, status


def _active_exact_for_interval(
    store: source_coverage.SourceDebtStore, interval: source_coverage.SourceInterval,
) -> tuple[source_coverage.DebtItem, ...]:
    return tuple(
        item for item in store.active()
        if item.interval.source != "runner/unclassified"
        and item.interval.since_utc == interval.since_utc
        and item.interval.until_utc == interval.until_utc
        and item.interval.slice_id == interval.slice_id
    )


def _stored_recovery_parents(record: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    raw = record.get("recovery_parents", {})
    if not isinstance(raw, Mapping) or not all(
        isinstance(key, str) and key and isinstance(value, Mapping)
        for key, value in raw.items()
    ):
        raise CycleError("stored recovery parents are invalid")
    return {str(key): dict(value) for key, value in raw.items()}


def _bind_incomplete_recovery_parents(
    config: Mapping[str, Any], record: dict[str, Any],
    debt_store: source_coverage.SourceDebtStore, stage: Mapping[str, Any],
    *, interval_template: source_coverage.SourceInterval | None = None,
) -> bool:
    coverage = stage.get("coverage")
    incomplete = coverage.get("incomplete_sources") if isinstance(coverage, Mapping) else None
    if not isinstance(incomplete, list) or not all(
        isinstance(source, str) and source for source in incomplete
    ):
        return False
    parents = _stored_recovery_parents(record)
    changed = False
    peer_sources: list[str] = []
    seen_peers: set[str] = set()
    for source in incomplete:
        category, separator, machine = source.partition("/")
        if separator != "/" or category not in {"sessions", "repositories"}:
            continue
        if not machine:
            raise CycleError("incomplete recovery source identity is invalid")
        peer = f"peer/{machine}"
        if peer not in seen_peers:
            peer_sources.append(peer)
            seen_peers.add(peer)
    for source in peer_sources:
        interval = (
            source_coverage.SourceInterval(
                source=source,
                since_utc=interval_template.since_utc,
                until_utc=interval_template.until_utc,
                slice_id=interval_template.slice_id,
                compatibility_version=interval_template.compatibility_version,
            )
            if interval_template is not None
            else _interval_from_stage(
                config, source, stage, allow_verified_derivation=True,
            )
        )
        current = debt_store.get(interval.debt_id)
        if interval.debt_id not in parents or current is None or current.status != "active":
            candidate = {
                key: value for key, value in stage.items()
                if key not in {"recovery_receipt_path", "recovery_receipt_digest"}
            }
            if parents.get(interval.debt_id) != candidate:
                parents[interval.debt_id] = candidate
                changed = True
    if changed:
        record["recovery_parents"] = parents
    return changed


def _recovery_parent(
    config: Mapping[str, Any], record: Mapping[str, Any], debt_id: str,
    since: str, until: str, *, attempt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    parents = _stored_recovery_parents(record)
    candidates: list[Mapping[str, Any]] = []
    bound = parents.get(debt_id)
    if bound is not None:
        candidates.append(bound)
    for key in ("source_parent", "source"):
        value = record.get(key)
        if isinstance(value, Mapping) and value not in candidates:
            candidates.append(value)
    if attempt is not None:
        candidates = [
            candidate for candidate in candidates
            if candidate.get("run_dir") == attempt.get("parent_run_dir")
            and candidate.get("bundle_digest") == attempt.get("parent_bundle_digest")
        ]
    if not candidates:
        raise CycleError("recovery parent source stage is missing")
    holder = {**record, "_recovery_parent": candidates[0]}
    parent = _stage_from_state(
        config, holder, "_recovery_parent", since, until, replay=False,
        expected_snapshot_digests=_stored_snapshot_digests(record),
        allow_historical_runtime=True,
    )
    if parent is None:
        raise CycleError("recovery parent source stage is missing")
    return parent


def _recovery_parent_matches_debt(
    parent: Mapping[str, Any], debt: source_coverage.DebtItem,
) -> bool:
    run_dir = Path(str(parent["run_dir"]))
    try:
        bundle = (
            collector_receipts.load_collector_source_bundle(
                run_dir / "completion-bundle.json", run_dir=run_dir
            )
            if parent.get("stage_kind") == "collector_source"
            else collector_receipts.load_completion_bundle(
                run_dir / "completion-bundle.json", run_dir=run_dir
            )
        )
    except collector_receipts.CollectorReceiptError as exc:
        raise CycleError("recovery parent completion bundle cannot be reloaded") from exc
    finalization = _json_file(
        _safe_run_file(
            run_dir, str(run_dir / "slice-finalization.json"), "slice finalization"
        ),
        "slice finalization",
    )
    identity = finalization.get("backlog_identity") if isinstance(finalization, Mapping) else None
    incomplete = parent["coverage"].get("incomplete_sources", [])
    required_gap = debt.interval.source in incomplete
    if debt.interval.source.startswith("peer/"):
        machine = debt.interval.source.split("/", 1)[1]
        required_gap = any(
            source in incomplete
            for source in (f"sessions/{machine}", f"repositories/{machine}")
        )
    return (
        (
            getattr(bundle, "source_bundle_digest", None)
            if parent.get("stage_kind") == "collector_source"
            else bundle.bundle_digest
        ) == parent.get("bundle_digest")
        and bundle.since_utc == debt.interval.since_utc
        and bundle.until_utc == debt.interval.until_utc
        and bundle.slice_id == debt.interval.slice_id
        and isinstance(identity, Mapping)
        and identity.get("compatibility_version") == debt.interval.compatibility_version
        and required_gap
    )


def _resolve_interval_generics(
    store: source_coverage.SourceDebtStore,
    interval: source_coverage.SourceInterval,
    source: Mapping[str, Any],
) -> None:
    for item in tuple(store.active()):
        if (
            item.interval.source == "runner/unclassified"
            and item.interval.since_utc == interval.since_utc
            and item.interval.until_utc == interval.until_utc
            and item.interval.slice_id == interval.slice_id
        ):
            _resolve_generic(store, item, source)


def _apply_verified_recovery(
    config: Mapping[str, Any], state: dict[str, Any], state_path: Path,
    record: dict[str, Any], debt_store: source_coverage.SourceDebtStore,
    debt_path: Path, since: str, until: str, debt: source_coverage.DebtItem,
    parent: Mapping[str, Any], attempt: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    checked = _validate_recovery_attempt(
        attempt, debt_id=debt.debt_id, parent=parent,
        command=_recovery_command(
            config, Path(str(parent["run_dir"])), debt.interval.source,
            str(attempt["attempt_id"]),
        ),
    )
    stage, status = _validate_recovery_stage(
        config, Path(str(checked["result_path"])), since, until,
        parent=parent, debt=debt, attempt_id=str(checked["attempt_id"]),
    )
    if (
        checked["result_digest"] != stage["result_digest"]
        or checked["returned_bundle_digest"] != stage["bundle_digest"]
        or checked["requested_source_outcome"] != status
        or checked["recovery_receipt_path"] != stage["recovery_receipt_path"]
        or checked["recovery_receipt_digest"] != stage["recovery_receipt_digest"]
    ):
        raise CycleError("verified recovery journal evidence has drifted")
    current = debt_store.get(debt.debt_id)
    if current is None:
        raise CycleError("verified recovery debt identity is missing")
    if status == "complete":
        if current.status == "resolved":
            if current.completion_bundle_digest != stage["bundle_digest"]:
                raise CycleError("recovery completion conflicts with resolved debt")
        else:
            debt_store.record_complete(
                debt.interval, completion_bundle_digest=str(stage["bundle_digest"]),
                completed_at=_attempted_at(),
            )
    else:
        resume_digest = _value_digest({
            "debt_id": debt.debt_id,
            "attempt_id": checked["attempt_id"],
            "parent_bundle_digest": parent["bundle_digest"],
            "returned_bundle_digest": stage["bundle_digest"],
            "result_digest": stage["result_digest"],
        })
        current = debt_store.get(debt.debt_id)
        if current is None or current.resume_state_digest != resume_digest:
            failed = debt_store.record_failure(
                debt.interval, failure_class="recovery_incomplete", retryable=True,
                resume_state_digest=resume_digest, attempted_at=_attempted_at(),
            )
            retry_limit = (
                EXACT_RETRY_LIMIT + 1
                if debt.interval.source.startswith("peer/")
                else EXACT_RETRY_LIMIT
            )
            if failed.retry_count >= retry_limit:
                debt_store.exhaust(failed.debt_id, terminal_reason="retry_limit")
    coverage = stage["coverage"]
    if _bind_incomplete_recovery_parents(
        config, record, debt_store, stage, interval_template=debt.interval,
    ):
        _persist_state(state_path, state, since, record)
    incomplete_peers = sorted({
        f"peer/{source.split('/', 1)[1]}"
        for source in coverage.get("incomplete_sources", [])
    })
    for source in incomplete_peers:
        if source == debt.interval.source:
            continue
        interval = source_coverage.SourceInterval(
            source=source, since_utc=debt.interval.since_utc,
            until_utc=debt.interval.until_utc, slice_id=debt.interval.slice_id,
            compatibility_version=debt.interval.compatibility_version,
        )
        _record_failure_once(
            debt_store, interval, failure_class="coverage_incomplete",
            resume_state_digest=_value_digest({
                "bundle_digest": stage["bundle_digest"], "debt_id": interval.debt_id,
                "run_id": stage["run_id"],
            }),
        )
    overall_complete = (
        coverage.get("status") == "complete"
        and coverage.get("incomplete_sources") == []
    )
    if status == "complete" and overall_complete:
        _resolve_interval_generics(debt_store, debt.interval, stage)
    source_coverage.write(debt_path, debt_store.document())
    attempts = dict(record["recovery_attempts"])
    attempts[debt.debt_id] = {
        **checked, "phase": f"finished_{status}",
    }
    record["recovery_attempts"] = attempts
    if (
        status == "complete"
        and overall_complete
        and not _active_exact_for_interval(debt_store, debt.interval)
    ):
        promoted_stage = {
            key: value for key, value in stage.items()
            if key not in {"recovery_receipt_path", "recovery_receipt_digest"}
        }
        if record.get("source_parent") is None:
            record["source_parent"] = dict(parent)
        record.update({
            "status": "source_verified", "source": promoted_stage,
            "source_completeness": stage["coverage"],
            "exception_ids": stage["exception_ids"],
            "exceptions_complete": not stage["exception_ids"],
        })
    else:
        record["status"] = "recovery_blocked" if status == "complete" else "incomplete"
    return stage, status


def _reconcile_verified_attempts(
    config: Mapping[str, Any], state: dict[str, Any], state_path: Path,
    debt_store: source_coverage.SourceDebtStore, debt_path: Path,
) -> None:
    for since, raw in list(state.get("slices", {}).items()):
        if not isinstance(raw, Mapping):
            continue
        record = dict(raw)
        attempts = record.get("recovery_attempts", {})
        if not isinstance(attempts, Mapping):
            raise CycleError("stored recovery attempts are invalid")
        for debt_id, attempt in list(attempts.items()):
            debt = debt_store.get(str(debt_id))
            if debt is None:
                raise CycleError("stored recovery debt identity is missing")
            if isinstance(attempt, Mapping) and str(attempt.get("phase", "")).startswith(
                "finished_"
            ):
                continue
            until = record.get("until")
            if not isinstance(until, str):
                raise CycleError("recovery slice until is invalid")
            parent = _recovery_parent(
                config, record, debt.debt_id, str(since), until,
                attempt=attempt if isinstance(attempt, Mapping) else None,
            )
            checked = _validate_recovery_attempt(
                attempt, debt_id=debt.debt_id, parent=parent,
                command=_recovery_command(
                    config, Path(str(parent["run_dir"])), debt.interval.source,
                    str(attempt.get("attempt_id")) if isinstance(attempt, Mapping) else "",
                ),
            )
            if checked["phase"] not in {"verified_complete", "verified_incomplete"}:
                continue
            _apply_verified_recovery(
                config, state, state_path, record, debt_store, debt_path, str(since), until,
                debt, parent, checked,
            )
            _persist_state(state_path, state, str(since), record)


def _run_exact_recovery(
    config: Mapping[str, Any], state: dict[str, Any], state_path: Path,
    root: Path, since: str, until: str, debt_store: source_coverage.SourceDebtStore,
    debt_path: Path, debt: source_coverage.DebtItem, budget: list[float],
) -> dict[str, Any]:
    raw = state["slices"].get(since)
    if not isinstance(raw, Mapping):
        raise CycleError("exact recovery slice state is missing")
    record = dict(raw)
    parent = _recovery_parent(config, record, debt.debt_id, since, until)
    if (
        not _recovery_parent_matches_debt(parent, debt)
    ):
        raise CycleError("exact recovery parent does not match stored debt")
    attempt, command = _recovery_attempt(record, debt, parent, config)
    _persist_state(state_path, state, since, record)
    if str(attempt["phase"]).startswith("verified_"):
        stage, status = _apply_verified_recovery(
            config, state, state_path, record, debt_store, debt_path,
            since, until, debt, parent, attempt
        )
        _persist_state(state_path, state, since, record)
    elif str(attempt["phase"]).startswith("finished_"):
        raise CycleError("finished recovery attempt was selected without a new ordinal")
    else:
        try:
            child = _run_budgeted_child(
                command, root=root, runs_dir=_runs_dir(config),
                budget=budget, cap=2700, grace=30,
                checkpoint_root=_collector_checkpoint_root(config, os.environ),
            )
        except _BudgetExhausted:
            record["status"] = "incomplete"
            _persist_state(state_path, state, since, record)
            return {"status": "incomplete", "reason": "total_child_budget_exhausted", "slice": {"since": since, "until": until}}
        try:
            result_path = _result(child.stdout, _runs_dir(config))
        except CycleError:
            record["status"] = "incomplete"
            _persist_state(state_path, state, since, record)
            return {"status": "incomplete", "slice": {"since": since, "until": until}}
        stage, status = _validate_recovery_stage(
            config, result_path, since, until, parent=parent, debt=debt,
            attempt_id=str(attempt["attempt_id"]),
        )
        attempts = dict(record["recovery_attempts"])
        verified = {
            **attempt,
            "phase": f"verified_{status}",
            "result_path": stage["result_path"],
            "result_digest": stage["result_digest"],
            "returned_bundle_digest": stage["bundle_digest"],
            "requested_source_outcome": status,
            "recovery_receipt_path": stage["recovery_receipt_path"],
            "recovery_receipt_digest": stage["recovery_receipt_digest"],
        }
        attempts[debt.debt_id] = verified
        record["recovery_attempts"] = attempts
        _persist_state(state_path, state, since, record)
        stage, status = _apply_verified_recovery(
            config, state, state_path, record, debt_store, debt_path,
            since, until, debt, parent, verified
        )
        _persist_state(state_path, state, since, record)
    if status != "complete" or _active_exact_for_interval(debt_store, debt.interval):
        return {"status": record["status"], "slice": {"since": since, "until": until}}
    return _run_slice(
        config, state, state_path, _path(config, "state_dir"), root, since, until,
        debt_store, debt_path, budget=budget,
    )


def _run_slice(
    config: Mapping[str, Any], state: dict[str, Any], state_path: Path,
    state_dir: Path, root: Path, since: str, until: str,
    debt_store: source_coverage.SourceDebtStore, debt_path: Path,
    *, generic: source_coverage.DebtItem | None = None,
    advance_frontier: bool = False, budget: list[float],
) -> dict[str, Any]:
    sheet_title = _sheet_title(config["monthly_sheet_title_template"], since=since)
    record_raw = state["slices"].get(since, {})
    if not isinstance(record_raw, Mapping):
        raise CycleError("stored slice state is invalid")
    record: dict[str, Any] = dict(record_raw)
    runner_attempt = _runner_attempt(record)
    bind_inputs = "expected_snapshot_digests" not in record
    manifest_path = _ensure_period(
        config, state_dir, since, until, bind_inputs=bind_inputs
    )
    record.update({"until": until, "period_manifest": str(manifest_path)})
    if bind_inputs:
        record["expected_snapshot_digests"] = _expected_snapshot_digests(
            config, manifest_path
        )
        _persist_state(state_path, state, since, record)
    expected_snapshots = _stored_snapshot_digests(record)
    transition_validation: dict[str, Any] = {}
    fresh_binding = "fresh_input_binding" in record or (
        "runner_attempt" not in record and record.get("source") is None
        and _expected_snapshot_digests(config, manifest_path) != expected_snapshots
    )
    if fresh_binding:
        expected_snapshots = _fresh_input_binding(config, record, since, until, manifest_path)
        _persist_state(state_path, state, since, record)
        transition_validation = {"expected_runtime_digest": _value_digest(record["fresh_input_binding"]["runtime_identity"]), "historical_state_validation": True}
        if record.get("source") is None:
            completed = []
            for result in _runs_dir(config).glob("*/autopilot-result.json"):
                try:
                    if clockify_review_run._adopt_completed_resume(result.parent) is None:
                        continue
                    candidate = _validate_stage(config, result, since, until, replay=False, expected_snapshot_digests=expected_snapshots, **transition_validation)
                    _verify_fresh_native_source(config, record, candidate)
                except (CycleError, OSError, ValueError):
                    continue
                completed.append(candidate)
            if len(completed) > 1:
                raise CycleError("fresh attempt completed source is ambiguous")
            if completed:
                record["source"] = completed[0]
                record["fresh_native_source_digest"] = _verify_fresh_native_source(config, record, completed[0])
                _finish_attempt(record, record["source_attempt"])
                record["status"] = "source_verified"
                _persist_state(state_path, state, since, record)
            elif record.get("fresh_child_started"):
                raise CycleError("fresh attempt already launched without verified completion; no duplicate child")
    elif "routing_transition" in record or (
        record.get("source") is None
        and _expected_snapshot_digests(config, manifest_path) != expected_snapshots
    ):
        transitioned = _routing_transition_source(config, record, since, until, manifest_path)
        if transitioned is None:
            raise CycleError("routing transition source is missing")
        # Seal the candidate before changing source state; a crash here resumes
        # only this exact completed result, never a new collection attempt.
        _persist_state(state_path, state, since, record)
        expected_snapshots = transitioned["snapshot_digests"]
        transition_validation = {
            "expected_runtime_digest": transitioned["runtime_identity_digest"],
            "historical_state_validation": True,
        }
        if record.get("source") is None:
            record["source"] = transitioned
            _finish_attempt(record, record["source_attempt"])
            record["status"] = "source_verified"
            _persist_state(state_path, state, since, record)

    source: dict[str, Any] | None
    stored_source = record.get("source")
    classification = (
        generic is not None and generic.status == "exhausted"
        and (
            stored_source is None
            or runner_attempt is not None and runner_attempt["status"] == "pending"
        )
    )
    historical_generic_gate = (
        generic is not None
        and generic.status == "exhausted"
        and isinstance(stored_source, Mapping)
        and isinstance(stored_source.get("result_path"), str)
        and isinstance(config.get("_runtime_identity"), Mapping)
        and isinstance(stored_source.get("runtime_identity_digest"), str)
        and stored_source["runtime_identity_digest"]
        != _value_digest(dict(config["_runtime_identity"]))
    )
    if historical_generic_gate:
        source = _stage_from_state(
            config, record, "source", since, until, replay=False,
            expected_snapshot_digests=expected_snapshots,
            allow_historical_runtime=True,
        )
        if source is None:
            raise CycleError("historical generic source is missing")
        if source.get("runtime_identity_digest") == _value_digest(
            dict(config["_runtime_identity"])
        ):
            raise CycleError("generic runtime transition did not select a historical source")
        record["source"] = source
        _persist_state(state_path, state, since, record)
    else:
        source = _stage_from_state(
            config, record, "source", since, until, replay=False,
            expected_snapshot_digests=expected_snapshots,
        )
    if source is not None and fresh_binding:
        _verify_fresh_native_source(config, record, source)
    attempt: dict[str, Any] | None = None
    collector_source: dict[str, Any] | None = None
    if source is None:
        review_command = _review_command(config, since, until)
        interval = generic.interval if generic is not None else _generic_interval(
            config, since, until
        )
        previous = record.get("source_attempt")
        runtime = config.get("_runtime_identity")
        if (
            classification
            and (
                runner_attempt is None
                or isinstance(runtime, Mapping)
                and runner_attempt["runtime_identity_digest"] != _value_digest(dict(runtime))
            )
            and isinstance(previous, Mapping) and previous.get("status") == "started"
            and _attempt_failure_exists(debt_store, interval, previous)
        ):
            _finish_attempt(record, previous)
        attempt = _source_attempt(
            record, review_command, interval, advance_frontier=advance_frontier
        )
        if isinstance(runtime, Mapping):
            record["runner_attempt"] = {
                "runtime_identity_digest": _value_digest(dict(runtime)),
                "source_attempt_ordinal": attempt["ordinal"],
                "status": "pending",
            }
        _persist_state(state_path, state, since, record)
        if _attempt_failure_exists(debt_store, interval, attempt):
            _finish_attempt(record, attempt)
            record["status"] = "incomplete"
            _persist_state(state_path, state, since, record)
            return {
                "status": "incomplete",
                "slice": {"since": since, "until": until},
                "advance_frontier": bool(attempt["advance_frontier"]),
            }
        try:
            if fresh_binding:
                record["fresh_child_started"] = True
                _persist_state(state_path, state, since, record)
            child = _run_budgeted_child(
                review_command, root=root, budget=budget, cap=2700, grace=30,
                runs_dir=_runs_dir(config),
                checkpoint_root=_collector_checkpoint_root(config, os.environ),
            )
        except _BudgetExhausted:
            if fresh_binding:
                # _run_budgeted_child raises this only before spawning.
                record.pop("fresh_child_started", None)
            record["status"] = "incomplete"
            _persist_state(state_path, state, since, record)
            return {"status": "incomplete", "reason": "total_child_budget_exhausted", "slice": {"since": since, "until": until}, "advance_frontier": False}
        source_error: CycleError | None = None
        try:
            if not child.timed_out:
                try:
                    result_path = _result(child.stdout, _runs_dir(config))
                except CycleError:
                    result_path = None
                if result_path is not None:
                    try:
                        candidate = _validate_stage(
                            config, result_path, since, until, replay=False,
                            expected_snapshot_digests=expected_snapshots,
                        )
                        if fresh_binding:
                            record["fresh_native_source_digest"] = _verify_fresh_native_source(config, record, candidate)
                        source = candidate
                    except _QualityBlocked:
                        if (result_path.parent / "collector-source.json").exists():
                            collector_source = _validate_collector_source_stage(
                                config, result_path, since, until,
                                expected_snapshot_digests=expected_snapshots,
                            )
                        else:
                            raise
                    except CycleError as exc:
                        if "runtime identity" in str(exc) and not fresh_binding:
                            source = _validate_stage(
                                config, result_path, since, until, replay=False,
                                expected_snapshot_digests=expected_snapshots,
                                allow_historical_runtime=True,
                            )
                        else:
                            raise
        except CycleError as exc:
            if not classification:
                raise
            source_error = exc
        if source is None and collector_source is not None:
            record.update({
                "source_parent": collector_source,
                "source_completeness": collector_source["coverage"],
            })
            if _bind_incomplete_recovery_parents(
                config, record, debt_store, collector_source
            ):
                _persist_state(state_path, state, since, record)
            exact_recorded = _record_exact_debts(
                config, debt_store, collector_source
            )
            if exact_recorded:
                source_coverage.write(debt_path, debt_store.document())
                _finish_attempt(record, attempt)
                record["status"] = "recovery_blocked"
                _persist_state(state_path, state, since, record)
                return {
                    "status": "recovery_blocked",
                    "slice": {"since": since, "until": until},
                    "advance_frontier": bool(attempt["advance_frontier"]),
                }
        if source is None:
            failure_class = (
                "child_timeout" if child.timed_out
                else "child_nonzero" if child.returncode != 0
                else "result_unverified"
            )
            _record_generic_failure(
                debt_store, interval, failure_class=failure_class,
                resume_state_digest=str(attempt["resume_state_digest"]),
                classification=classification,
            )
            source_coverage.write(debt_path, debt_store.document())
            _finish_attempt(record, attempt)
            record["status"] = "incomplete"
            _persist_state(state_path, state, since, record)
            if source_error is not None:
                raise source_error
            return {
                "status": "incomplete",
                "slice": {"since": since, "until": until},
                "advance_frontier": bool(attempt["advance_frontier"]),
            }
        _finish_attempt(record, attempt, finish_runner=not classification)
        record.update({
            "status": "source_verified", "source": source,
            "source_completeness": source["coverage"],
            "exception_ids": source["exception_ids"],
            "exceptions_complete": not source["exception_ids"],
        })
        _persist_state(state_path, state, since, record)
    coverage = source["coverage"]
    if coverage.get("status") != "complete" or coverage.get("incomplete_sources") != []:
        if _bind_incomplete_recovery_parents(config, record, debt_store, source):
            _persist_state(state_path, state, since, record)
        exact_recorded = _record_exact_debts(config, debt_store, source)
        if not exact_recorded:
            interval = generic.interval if generic is not None else _generic_interval(
                config, since, until
            )
            resume_digest = (
                str(attempt["resume_state_digest"])
                if attempt is not None
                else _value_digest({"bundle_digest": source["bundle_digest"], "interval": interval.document()})
            )
            _record_generic_failure(
                debt_store, interval, failure_class="coverage_unclassified",
                resume_state_digest=resume_digest,
                classification=classification,
            )
        elif generic is not None and (historical_generic_gate or classification):
            _resolve_generic(debt_store, generic, source)
        source_coverage.write(debt_path, debt_store.document())
        _finish_runner_attempt(record)
        record["status"] = "recovery_blocked"
        _persist_state(state_path, state, since, record)
        return {
            "status": "recovery_blocked",
            "slice": {"since": since, "until": until},
            "advance_frontier": advance_frontier,
        }

    _resolve_generic(debt_store, generic, source)
    source_coverage.write(debt_path, debt_store.document())
    _finish_runner_attempt(record)
    _persist_state(state_path, state, since, record)

    replay = _stage_from_state(
        config, record, "replay", since, until, replay=True,
        expected_snapshot_digests=source["snapshot_digests"],
        source_run_id=str(source["run_id"]), source_run_dir=str(source["run_dir"]),
    )
    if replay is None:
        command = _replay_command(config, Path(str(source["run_dir"])))
        returned = record.get("replay_return")
        if returned is None:
            try:
                child = _run_budgeted_child(
                    command, root=root, runs_dir=_runs_dir(config),
                    budget=budget, cap=2700, grace=30,
                    checkpoint_root=_collector_checkpoint_root(config, os.environ),
                )
            except _BudgetExhausted:
                record["status"] = "source_verified"
                _persist_state(state_path, state, since, record)
                return {"status": "incomplete", "reason": "total_child_budget_exhausted", "slice": {"since": since, "until": until}}
            if child.timed_out or child.returncode != 0:
                record["status"] = "failed"
                _persist_state(state_path, state, since, record)
                return {"status": "failed", "slice": {"since": since, "until": until}}
            result_path = _result(child.stdout, _runs_dir(config))
            body = {"schema_version":"review-cycle-replay-return/v1", "source_digest":_value_digest(source), "command_digest":_value_digest(command), "result_path":str(result_path), "result_digest":_digest(result_path)}
            returned = {**body, "return_digest":_value_digest(body)}
            record["replay_return"] = returned
            _persist_state(state_path, state, since, record)
        if not isinstance(returned, Mapping):
            raise CycleError("stored replay return is invalid")
        body = {key:value for key,value in returned.items() if key != "return_digest"}
        result_path = _safe_run_file(_runs_dir(config), body.get("result_path"), "returned replay result")
        if (
            set(body) != {"schema_version", "source_digest", "command_digest", "result_path", "result_digest"}
            or body.get("schema_version") != "review-cycle-replay-return/v1"
            or returned.get("return_digest") != _value_digest(body)
            or body.get("source_digest") != _value_digest(source)
            or body.get("command_digest") != _value_digest(command)
            or body.get("result_digest") != _digest(result_path)
        ):
            raise CycleError("stored replay return binding has drifted")
        replay = _validate_stage(
            config, result_path, since, until, replay=True,
            expected_snapshot_digests=source["snapshot_digests"],
            source_run_id=str(source["run_id"]), source_run_dir=str(source["run_dir"]),
            **transition_validation,
        )
        if replay["accounting_digest"] != source["accounting_digest"]:
            raise CycleError("replay accounting does not exactly match the source")
        record.update({"status": "replay_verified", "replay": replay})
        _persist_state(state_path, state, since, record)

    receipt_path = state_dir / "delivery-receipts" / f"{since}.json"
    publisher_result_path = (
        _runs_dir(config) / f"publication-{source['run_id']}" / "autopilot-result.json"
    )
    receipt = _delivery_document(
        config, since, until, source, replay, sheet_title=sheet_title
    )
    if receipt_path.exists():
        _verify_delivery_receipt(
            receipt_path, config, since, until, source, replay,
            sheet_title=sheet_title,
        )
    else:
        try:
            child = _run_budgeted_child(
                _publisher_command(
                    config, source, replay, sheet_title=sheet_title,
                    result_path=publisher_result_path,
                ),
                root=root, runs_dir=_runs_dir(config),
                budget=budget, cap=900, grace=30,
                checkpoint_root=_collector_checkpoint_root(config, os.environ),
            )
        except _BudgetExhausted:
            record["status"] = "replay_verified"
            _persist_state(state_path, state, since, record)
            return {"status": "incomplete", "reason": "total_child_budget_exhausted", "slice": {"since": since, "until": until}}
        if child.timed_out or child.returncode != 0:
            record["status"] = "failed"
            _persist_state(state_path, state, since, record)
            return {"status": "failed", "slice": {"since": since, "until": until}}
        _publisher_result(
            child.stdout, _runs_dir(config), receipt["publication_receipts"]
        )
        verified_source = _validate_stage(
            config, Path(str(source["result_path"])), since, until, replay=False,
            expected_snapshot_digests=expected_snapshots,
            **transition_validation,
        )
        verified_replay = _validate_stage(
            config, Path(str(replay["result_path"])), since, until, replay=True,
            expected_snapshot_digests=source["snapshot_digests"],
            source_run_id=str(source["run_id"]), source_run_dir=str(source["run_dir"]),
            **transition_validation,
        )
        if verified_source != source or verified_replay != replay:
            raise CycleError("delivery inputs drifted while the publisher was running")
        if fresh_binding:
            _verify_fresh_native_source(config, record, verified_source)
        _write_delivery_receipt(receipt_path, receipt)

    record.update({
        "status": (
            "delivered_with_exceptions" if source["exception_ids"] else "delivered"
        ),
        "source_run_id": source["run_id"],
        "replay_run_id": replay["run_id"],
        "delivery_receipt": str(receipt_path.resolve()),
        "review_ids": source["review_ids"],
        "exception_ids": source["exception_ids"],
        "exceptions_complete": not source["exception_ids"],
    })
    _persist_state(state_path, state, since, record)
    return {
        "status": record["status"],
        "slice": {"since": since, "until": until, "exception_ids": source["exception_ids"]},
        "advance_frontier": _record_advances_frontier(record, advance_frontier),
    }


def adopt_historical_slice(
    config: Mapping[str, Any], request: Mapping[str, Any],
) -> dict[str, Any]:
    """Import one already-published, sealed slice without invoking any child."""
    common_required = {
        "schema_version", "since", "until", "source_result", "replay_result",
        "publication_result",
        "frozen_snapshot_digests", "adopted_snapshot_digests",
        "runtime_identity_digest", "source_result_digest", "replay_result_digest",
        "publication_result_digest",
    }
    legacy = (
        isinstance(request, Mapping)
        and request.get("schema_version") == HISTORICAL_ADOPTION_REQUEST_SCHEMA_VERSION
        and set(request) == common_required | {
            "checkpoint_root", "checkpoint_manifest_digest",
        }
    )
    derived = (
        isinstance(request, Mapping)
        and request.get("schema_version") == DERIVED_ADOPTION_REQUEST_SCHEMA_VERSION
        and set(request) == common_required | {"source_provenance"}
    )
    if not (legacy or derived):
        raise CycleError("historical adoption request is invalid")
    since = str(request["since"])
    until = str(request["until"])
    if _date(since, "historical since") >= _date(until, "historical until"):
        raise CycleError("historical adoption interval is invalid")
    for name in (
        *(["checkpoint_manifest_digest"] if legacy else []),
        "runtime_identity_digest", "source_result_digest", "replay_result_digest",
        "publication_result_digest",
    ):
        if not _valid_digest(request[name]):
            raise CycleError(f"historical adoption {name} is invalid")
    state_dir = _path(config, "state_dir")
    state_path = state_dir / "review-cycle-state.json"
    debt_path = state_dir / "source-coverage.json"
    with single_instance(state_dir / "review-cycle.lock") as acquired:
        if not acquired:
            return {"status": "locked", "slice": {"since": since, "until": until}}
        state = _state(state_path, recovery_since=str(config["recovery_since"]))
        raw_record = state["slices"].get(since)
        if not isinstance(raw_record, Mapping) or raw_record.get("until") != until:
            raise CycleError("historical adoption requires an existing exact slice")
        record = dict(raw_record)
        if record.get("status") in {"delivered", "delivered_with_exceptions"}:
            adoption = _historical_adoption_document(config, record, since, until)
            if adoption is None or adoption.get("request_digest") != _value_digest(dict(request)):
                raise CycleError("delivered slice has different adoption identity")
            _validate_delivered_state(config, state)
            return {"status": str(record["status"]), "slice": {"since": since, "until": until}}
        if any(record.get(key) is not None for key in (
            "source", "replay", "delivery_receipt", "historical_adoption_receipt",
        )):
            raise CycleError("historical adoption cannot replace an existing stage")
        manifest_path = _ensure_period(config, state_dir, since, until, bind_inputs=False)
        frozen = _stored_snapshot_digests(record)
        if record.get("period_manifest") != str(manifest_path) or (
            frozen != request["frozen_snapshot_digests"]
            or frozen != _expected_snapshot_digests(config, manifest_path)
        ):
            raise CycleError("historical adoption frozen input proof differs")
        adopted = request["adopted_snapshot_digests"]
        if not isinstance(adopted, Mapping) or set(adopted) != set(frozen):
            raise CycleError("historical adoption input transition is invalid")
        for name in frozen:
            if not _valid_digest(adopted[name]) or (
                name not in {"routing.json", "review-corrections.jsonl"}
                and adopted[name] != frozen[name]
            ):
                raise CycleError("historical adoption changed non-routing frozen inputs")
        source_path = _safe_run_file(
            _runs_dir(config), request["source_result"], "historical source result"
        )
        if adopted["review-corrections.jsonl"] != frozen["review-corrections.jsonl"]:
            _verify_credit_adoption_transition(
                config, source_path.parent,
                frozen["review-corrections.jsonl"],
                adopted["review-corrections.jsonl"],
            )
        replay_path = _safe_run_file(
            _runs_dir(config), request["replay_result"], "historical replay result"
        )
        publication_path = _safe_run_file(
            _runs_dir(config), request["publication_result"], "historical publication result"
        )
        for name, path in (
            ("source_result_digest", source_path),
            ("replay_result_digest", replay_path),
            ("publication_result_digest", publication_path),
        ):
            if _digest(path) != request[name]:
                raise CycleError(f"historical adoption {name} differs")
        validation_config = dict(config)
        validation_config.setdefault(
            "_runtime_identity",
            clockify_review_run.clockify_sync_collect.collector_runtime_identity(),
        )
        source = _validate_stage(
            validation_config, source_path, since, until, replay=False,
            expected_snapshot_digests=adopted,
            expected_runtime_digest=str(request["runtime_identity_digest"]),
            historical_state_validation=True,
        )
        if source["coverage"].get("status") != "complete" or (
            source["coverage"].get("incomplete_sources") != []
        ):
            raise CycleError("historical source coverage is incomplete")
        replay = _validate_stage(
            validation_config, replay_path, since, until, replay=True,
            expected_snapshot_digests=adopted,
            source_run_id=str(source["run_id"]), source_run_dir=str(source["run_dir"]),
            expected_runtime_digest=str(request["runtime_identity_digest"]),
            historical_state_validation=True,
        )
        if replay["accounting_digest"] != source["accounting_digest"]:
            raise CycleError("historical replay accounting differs")
        title = _sheet_title(config["monthly_sheet_title_template"], since=since)
        expected_publications = _expected_publication_receipts(
            config, source, sheet_title=title,
        )
        checkpoint_capture: dict[str, str] = {}
        if derived:
            _interval_from_derived_stage(
                validation_config, since, until, source,
                request["source_provenance"],
            )
        else:
            _interval_from_stage(
                validation_config, "runner/unclassified", source,
                checkpoint_root=Path(str(request["checkpoint_root"])),
                checkpoint_manifest_digest=str(request["checkpoint_manifest_digest"]),
                checkpoint_capture=checkpoint_capture,
            )
        unsigned = {
            "schema_version": (
                DERIVED_ADOPTION_SCHEMA_VERSION if derived
                else HISTORICAL_ADOPTION_SCHEMA_VERSION
            ),
            "since": since, "until": until,
            "request_digest": _value_digest(dict(request)),
            "frozen_snapshot_digests": frozen,
            "adopted_snapshot_digests": dict(adopted),
            "runtime_identity_digest": request["runtime_identity_digest"],
            "source": source, "replay": replay,
            "publication_result": str(publication_path),
            "publication_result_digest": request["publication_result_digest"],
            "publication_receipts": expected_publications,
        }
        if derived:
            unsigned["source_provenance"] = dict(request["source_provenance"])
        else:
            unsigned.update({
                "checkpoint_root": request["checkpoint_root"],
                "checkpoint_manifest_digest": request["checkpoint_manifest_digest"],
                "checkpoint_manifest_text": checkpoint_capture["manifest_text"],
            })
        adoption = {**unsigned, "receipt_digest": _value_digest(unsigned)}
        _verify_historical_adoption(
            validation_config, record, adoption, since, until, source, replay,
        )
        delivery = _delivery_document(
            config, since, until, source, replay, sheet_title=title,
        )
        receipt_path = state_dir / "delivery-receipts" / f"{since}.json"
        adoption_path = state_dir / "historical-adoption-receipts" / f"{since}.json"
        raw_debt = _json_file(debt_path, "source coverage")
        try:
            debt_store = source_coverage.SourceDebtStore.from_document(raw_debt)
        except (TypeError, ValueError) as exc:
            raise CycleError("source coverage state is invalid") from exc
        warnings = raw_debt.get("migration_warnings", [])
        if not isinstance(warnings, list):
            raise CycleError("source coverage warnings are invalid")
        generic = debt_store.get(_generic_interval(config, since, until).debt_id)
        before = debt_store.document()
        _resolve_generic(debt_store, generic, source)
        _write_delivery_receipt(receipt_path, delivery)
        _write_delivery_receipt(adoption_path, adoption)
        if debt_store.document() != before:
            source_coverage.write(
                debt_path, debt_store.document(migration_warnings=warnings)
            )
        record.update({
            "status": "delivered_with_exceptions" if source["exception_ids"] else "delivered",
            "source": source, "replay": replay,
            "source_completeness": source["coverage"],
            "source_run_id": source["run_id"], "replay_run_id": replay["run_id"],
            "delivery_receipt": str(receipt_path),
            "historical_adoption_receipt": str(adoption_path),
            "historical_adoption_receipt_digest": adoption["receipt_digest"],
            "review_ids": source["review_ids"],
            "exception_ids": source["exception_ids"],
            "exceptions_complete": not source["exception_ids"],
        })
        state["slices"][since] = record
        if state.get("scheduled_through") == since:
            state["scheduled_through"] = until
        _recompute_completed_through(config, state)
        _atomic(state_path, state)
        return {"status": str(record["status"]), "slice": {"since": since, "until": until}}


def run_cycle(config: Mapping[str, Any], *, enable_sheet_write: bool, today: dt.date | None = None) -> dict[str, Any]:
    root = _path(config, "root")
    state_dir = _path(config, "state_dir")
    if not root.is_dir():
        raise CycleError("root is unavailable")
    clockify_review_run._configure_runs_root(_runs_dir(config))
    state_path = state_dir / "review-cycle-state.json"
    debt_path = state_dir / "source-coverage.json"
    with single_instance(state_dir / "review-cycle.lock") as acquired:
        if not acquired:
            return {"status": "locked", "slices": []}
        state = _state(state_path, recovery_since=str(config["recovery_since"]))
        _migrate_legacy_stage_runtime(
            config, state, state_path, persist=enable_sheet_write,
        )
        debt_store, migration_warnings = _source_debt(debt_path)
        state["source_debt_warnings"] = migration_warnings
        if _reactivate_health_transitions(config, state, debt_store) and enable_sheet_write:
            source_coverage.write(debt_path, debt_store.document())
            _atomic(state_path, state)
        _validate_delivered_state(config, state)
        selected = _select_work(
            config,
            state,
            debt_store,
            today=today or dt.datetime.now(ZoneInfo(str(config["timezone"]))).date(),
        )
        if not enable_sheet_write:
            return {
                "status": "plan",
                "slices": [
                    {"since": since, "until": until}
                    for since, until, _kind, _generic in selected
                ],
            }
        _reconcile_verified_attempts(config, state, state_path, debt_store, debt_path)
        selected = _select_work(
            config,
            state,
            debt_store,
            today=today or dt.datetime.now(ZoneInfo(str(config["timezone"]))).date(),
        )
        if not selected:
            _atomic(state_path, state)
            return {"status": "idle", "slices": []}
        if int(config.get("max_slices", 1)) == 1:
            routine_pick = _select_work(
                config, {**state, "next_work_class": "routine"}, debt_store,
                today=today or dt.datetime.now(ZoneInfo(str(config["timezone"]))).date(),
            )
            recovery_pick = _select_work(
                config, {**state, "next_work_class": "exact"}, debt_store,
                today=today or dt.datetime.now(ZoneInfo(str(config["timezone"]))).date(),
            )
            if (
                routine_pick and recovery_pick
                and routine_pick[0][2] not in {"exact", "generic_classification"}
                and recovery_pick[0][2] in {"exact", "generic_classification"}
            ):
                state["next_work_class"] = (
                    "routine" if selected[0][2] in {"exact", "generic_classification"} else "exact"
                )
                _atomic(state_path, state)
        attempted: list[dict[str, Any]] = []
        final_status = "delivered"
        final_reason: str | None = None
        any_exceptions = False
        budget = [float(config.get("total_child_budget_seconds", 7200))]
        for since, until, kind, generic in selected:
            if kind == "exact":
                if generic is None:
                    raise CycleError("selected exact recovery has no debt identity")
                outcome = _run_exact_recovery(
                    config, state, state_path, root, since, until, debt_store,
                    debt_path, generic, budget,
                )
            else:
                outcome = _run_slice(
                    config, state, state_path, state_dir, root, since, until,
                    debt_store, debt_path, generic=generic,
                    advance_frontier=kind == "routine", budget=budget,
                )
            attempted.append(outcome["slice"])
            final_status = str(outcome["status"])
            if outcome.get("reason") == "total_child_budget_exhausted":
                final_reason = "total_child_budget_exhausted"
            any_exceptions = any_exceptions or final_status == "delivered_with_exceptions"
            if (
                kind == "routine" and outcome.get("advance_frontier") is not False
            ) or outcome.get("advance_frontier") is True:
                current = _date(state["scheduled_through"], "scheduled_through")
                end = _date(until, "slice until")
                if end > current:
                    state["scheduled_through"] = until
            _recompute_completed_through(config, state)
            _atomic(state_path, state)
        if final_status not in {"incomplete", "failed", "recovery_blocked"} and any_exceptions:
            final_status = "delivered_with_exceptions"
        if final_reason == "total_child_budget_exhausted":
            final_status = "incomplete"
        result: dict[str, Any] = {"status": final_status, "slices": attempted}
        if final_reason is not None:
            result["reason"] = final_reason
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--enable-sheet-write", action="store_true")
    parser.add_argument("--audit-coverage-output", type=Path)
    parser.add_argument(
        "--adopt-historical-request", type=Path,
        help="Adopt one exact, already-proven historical delivery without scheduling children.",
    )
    args = parser.parse_args(argv)
    try:
        if args.adopt_historical_request is not None and (
            args.enable_sheet_write or args.audit_coverage_output is not None
        ):
            raise CycleError("historical adoption and scheduling/audit modes are mutually exclusive")
        config = load_config(args.config)
        if args.audit_coverage_output is not None:
            output = _coverage_audit_output_path(
                config, args.audit_coverage_output,
            )
            report = source_interval_coverage_audit(config)
            _atomic(output, report)
            print(json.dumps(report, sort_keys=True))
            return 0
        config["_runtime_identity"] = (
            clockify_review_run.clockify_sync_collect.collector_runtime_identity()
        )
        _validate_runtime_root(config, os.environ)
        if args.adopt_historical_request is not None:
            request_path = _canonical_runtime_path(
                args.adopt_historical_request, label="historical adoption request"
            )
            if not request_path.is_file() or request_path.is_symlink():
                raise CycleError("historical adoption request is missing or unsafe")
            result = adopt_historical_slice(
                config, _json_file(request_path, "historical adoption request")
            )
        else:
            result = run_cycle(
                config, enable_sheet_write=args.enable_sheet_write
            )
        print(json.dumps(result, sort_keys=True))
    except (CycleError, OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"clockify review cycle blocked: {exc}", file=sys.stderr)
        return 2
    if result.get("status") in {"incomplete", "failed", "recovery_blocked"}:
        return 75
    if result.get("status") in {
        "idle", "locked", "delivered", "delivered_with_exceptions", "plan",
    }:
        return 0
    print("clockify review cycle blocked: unsupported result status", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
