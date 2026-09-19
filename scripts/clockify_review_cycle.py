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
import json
import os
from contextlib import contextmanager
from pathlib import Path
import string
import sys
from typing import Any, Iterator, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from scripts.autopilot_process import ChildTimeoutConfig, run_child_bounded
    from scripts import (
        clockify_review_run,
        collector_receipts,
        collector_slices,
        reconciliation_manifest,
        source_coverage,
    )
    from scripts.clockify_sheet_publish import (
        project_allowlist,
        proposal_row,
        stable_review_id,
    )
except ModuleNotFoundError:  # pragma: no cover
    from autopilot_process import ChildTimeoutConfig, run_child_bounded  # type: ignore[no-redef]
    import clockify_review_run  # type: ignore[no-redef]
    import collector_receipts  # type: ignore[no-redef]
    import collector_slices  # type: ignore[no-redef]
    import reconciliation_manifest  # type: ignore[no-redef]
    import source_coverage  # type: ignore[no-redef]
    from clockify_sheet_publish import (  # type: ignore[no-redef]
        project_allowlist,
        proposal_row,
        stable_review_id,
    )


SCHEMA_VERSION = "clockify-review-cycle/v1"
RECEIPT_SCHEMA_VERSION = "clockify-review-delivery/v1"
GENERIC_COMPATIBILITY_VERSION = "runner-unclassified/v1"
GENERIC_RETRY_LIMIT = 2
RECOVERY_ATTEMPT_SCHEMA_VERSION = "review-cycle-source-recovery-attempt/v1"
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


def _select_work(
    config: Mapping[str, Any], state: Mapping[str, Any],
    store: source_coverage.SourceDebtStore, *, today: dt.date,
) -> list[tuple[str, str, str, source_coverage.DebtItem | None]]:
    """Select bounded exact and ordinary work with deterministic fairness."""
    maximum = int(config.get("max_slices", 1))
    now = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    active = store.active()
    ordinarily_eligible = {item.debt_id for item in store.eligible(now)}
    exact: list[tuple[str, str, str, source_coverage.DebtItem | None]] = []
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
        exact.append((since, until, "exact", item))
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
        ordinary.append((since, until, "generic", item))
        selected_intervals.add(identity)
        if len(ordinary) == maximum:
            break
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
    if not exact:
        return ordinary[:maximum]
    if not ordinary:
        return exact[:maximum]
    if maximum == 1:
        return exact[:1] if state.get("next_work_class", "routine") == "exact" else ordinary[:1]
    selected = [exact[0], ordinary[0]]
    occupied = {(item[0], item[1]) for item in selected}
    for item in [*exact[1:], *ordinary[1:]]:
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
) -> dict[str, Any]:
    result_path = _safe_run_file(
        _runs_dir(config), str(result_path), "result"
    )
    if result_path.name != "autopilot-result.json":
        raise CycleError("review result filename is invalid")
    run_dir = result_path.parent
    result = _json_file(result_path, "review result")
    if not isinstance(result, Mapping) or result.get("quality_status") != "pass":
        raise CycleError("review result did not pass quality")
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
    return {
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


def _stage_from_state(
    config: Mapping[str, Any], record: Mapping[str, Any], key: str,
    since: str, until: str, *, replay: bool,
    expected_snapshot_digests: Mapping[str, str],
    source_run_id: str | None = None, source_run_dir: str | None = None,
) -> dict[str, Any] | None:
    stored = record.get(key)
    if stored is None:
        return None
    if not isinstance(stored, Mapping) or not isinstance(stored.get("result_path"), str):
        raise CycleError(f"stored {key} stage is invalid")
    verified = _validate_stage(
        config, Path(stored["result_path"]), since, until,
        replay=replay, expected_snapshot_digests=expected_snapshot_digests,
        source_run_id=source_run_id, source_run_dir=source_run_dir,
    )
    if dict(stored) != verified:
        raise CycleError(f"stored {key} stage identity has drifted")
    return verified


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
    runs_dir: Path | None = None,
):
    total = min(cap, int(budget[0]))
    if total <= grace:
        raise _BudgetExhausted("total_child_budget_exhausted")
    child_environment = dict(os.environ)
    child_environment["CLOCKIFY_AUTOPILOT_RUNS_ROOT"] = str(
        (runs_dir or root / "runs").resolve()
    )
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
    *, sheet_title: str,
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
        "--enable-write",
    ]


def _delivery_document(
    config: Mapping[str, Any], since: str, until: str,
    source: Mapping[str, Any], replay: Mapping[str, Any], *, sheet_title: str,
) -> dict[str, Any]:
    proposals, _exceptions = _validate_accounting(Path(str(source["run_dir"])))
    try:
        source_dir = Path(str(source["run_dir"])).resolve()
        routing_path = _safe_run_file(
            source_dir, str(source_dir / "routing.json"), "source routing snapshot"
        )
        routing = _json_file(routing_path, "source routing snapshot")
        projects = project_allowlist(routing)
        rows = [
            proposal_row(
                item, str(source["run_id"]), project_allowlist=projects,
            )
            for item in proposals
        ]
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
        "expected_row_contract_digest": _value_digest(rows),
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


def _interval_from_stage(
    config: Mapping[str, Any], source: str, stage: Mapping[str, Any],
) -> source_coverage.SourceInterval:
    run_dir = Path(str(stage["run_dir"]))
    try:
        bundle = collector_receipts.load_completion_bundle(
            run_dir / "completion-bundle.json", run_dir=run_dir
        )
    except collector_receipts.CollectorReceiptError as exc:
        raise CycleError("verified source completion bundle cannot be reloaded") from exc
    if bundle.bundle_digest != stage.get("bundle_digest"):
        raise CycleError("verified source completion bundle identity drifted")
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
    configured_root = os.environ.get("CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT", "").strip()
    checkpoint_root = (
        Path(configured_root).expanduser()
        if configured_root
        else _path(config, "root") / "state" / "collector-checkpoints"
    )
    if not checkpoint_root.is_absolute() or checkpoint_root.is_symlink():
        raise CycleError("collector checkpoint root is unsafe")
    try:
        backlog = collector_slices.BacklogStore(checkpoint_root).read_existing(
            identity, tuple(planned)
        )
    except collector_slices.BacklogError as exc:
        raise CycleError("sealed collector backlog binding is invalid") from exc
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
    for name in incomplete:
        interval = _interval_from_stage(config, name, source)
        resume_digest = _value_digest({
            "bundle_digest": source["bundle_digest"],
            "debt_id": interval.debt_id,
            "run_id": source["run_id"],
        })
        _record_failure_once(
            store, interval, failure_class="coverage_incomplete",
            resume_state_digest=resume_digest,
        )
    return True


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
    if item.retry_count >= GENERIC_RETRY_LIMIT:
        item = store.exhaust(item.debt_id, terminal_reason="retry_limit")
    return item


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


def _finish_attempt(record: dict[str, Any], attempt: Mapping[str, Any]) -> None:
    record["source_attempt"] = {**dict(attempt), "status": "finished"}


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
        bundle = collector_receipts.load_completion_bundle(
            run_dir / "completion-bundle.json", run_dir=run_dir
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
        verified_manifest = _ensure_period(
            config, _path(config, "state_dir"), since, until, bind_inputs=False
        )
        if raw_record.get("period_manifest") != str(verified_manifest):
            raise CycleError("delivered slice period manifest identity has drifted")
        source = _stage_from_state(
            config, raw_record, "source", since, until, replay=False,
            expected_snapshot_digests=expected_snapshots,
        )
        if source is None:
            raise CycleError("delivered slice has no verified source stage")
        replay = _stage_from_state(
            config, raw_record, "replay", since, until, replay=True,
            expected_snapshot_digests=source["snapshot_digests"],
            source_run_id=str(source["run_id"]), source_run_dir=str(source["run_dir"]),
        )
        if replay is None:
            raise CycleError("delivered slice has no verified replay stage")
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
        "requested_source_outcome",
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
    except (OSError, ValueError, clockify_review_run.ReviewRunError) as exc:
        raise CycleError("recovery completion identity is invalid") from exc
    if (
        bundle.bundle_digest != stage["bundle_digest"]
        or bundle.since_utc != debt.interval.since_utc
        or bundle.until_utc != debt.interval.until_utc
        or bundle.slice_id != debt.interval.slice_id
    ):
        raise CycleError("recovery completion does not match exact debt interval")
    return stage, status


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
    for source in incomplete:
        interval = (
            source_coverage.SourceInterval(
                source=source,
                since_utc=interval_template.since_utc,
                until_utc=interval_template.until_utc,
                slice_id=interval_template.slice_id,
                compatibility_version=interval_template.compatibility_version,
            )
            if interval_template is not None
            else _interval_from_stage(config, source, stage)
        )
        current = debt_store.get(interval.debt_id)
        if interval.debt_id not in parents or current is None or current.status != "active":
            candidate = dict(stage)
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
    )
    if parent is None:
        raise CycleError("recovery parent source stage is missing")
    return parent


def _recovery_parent_matches_debt(
    parent: Mapping[str, Any], debt: source_coverage.DebtItem,
) -> bool:
    run_dir = Path(str(parent["run_dir"]))
    try:
        bundle = collector_receipts.load_completion_bundle(
            run_dir / "completion-bundle.json", run_dir=run_dir
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
    return (
        bundle.bundle_digest == parent.get("bundle_digest")
        and bundle.since_utc == debt.interval.since_utc
        and bundle.until_utc == debt.interval.until_utc
        and bundle.slice_id == debt.interval.slice_id
        and isinstance(identity, Mapping)
        and identity.get("compatibility_version") == debt.interval.compatibility_version
        and debt.interval.source in parent["coverage"].get("incomplete_sources", [])
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
            debt_store.record_failure(
                debt.interval, failure_class="recovery_incomplete", retryable=True,
                resume_state_digest=resume_digest, attempted_at=_attempted_at(),
            )
    coverage = stage["coverage"]
    if _bind_incomplete_recovery_parents(
        config, record, debt_store, stage, interval_template=debt.interval,
    ):
        _persist_state(state_path, state, since, record)
    for source in coverage.get("incomplete_sources", []):
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
        if record.get("source_parent") is None:
            record["source_parent"] = dict(parent)
        record.update({
            "status": "source_verified", "source": stage,
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

    source = _stage_from_state(
        config, record, "source", since, until, replay=False,
        expected_snapshot_digests=expected_snapshots,
    )
    attempt: dict[str, Any] | None = None
    if source is None:
        review_command = _review_command(config, since, until)
        interval = generic.interval if generic is not None else _generic_interval(
            config, since, until
        )
        attempt = _source_attempt(
            record, review_command, interval, advance_frontier=advance_frontier
        )
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
            child = _run_budgeted_child(
                review_command, root=root, budget=budget, cap=2700, grace=30,
                runs_dir=_runs_dir(config),
            )
        except _BudgetExhausted:
            record["status"] = "incomplete"
            _persist_state(state_path, state, since, record)
            return {"status": "incomplete", "reason": "total_child_budget_exhausted", "slice": {"since": since, "until": until}, "advance_frontier": False}
        if not child.timed_out:
            try:
                result_path = _result(child.stdout, _runs_dir(config))
            except CycleError:
                result_path = None
            if result_path is not None:
                source = _validate_stage(
                    config, result_path, since, until, replay=False,
                    expected_snapshot_digests=expected_snapshots,
                )
        if source is None:
            failure_class = (
                "child_timeout" if child.timed_out
                else "child_nonzero" if child.returncode != 0
                else "result_unverified"
            )
            _record_generic_failure(
                debt_store, interval, failure_class=failure_class,
                resume_state_digest=str(attempt["resume_state_digest"]),
            )
            source_coverage.write(debt_path, debt_store.document())
            _finish_attempt(record, attempt)
            record["status"] = "incomplete"
            _persist_state(state_path, state, since, record)
            return {
                "status": "incomplete",
                "slice": {"since": since, "until": until},
                "advance_frontier": bool(attempt["advance_frontier"]),
            }
        _finish_attempt(record, attempt)
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
        if not _record_exact_debts(config, debt_store, source):
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
            )
        source_coverage.write(debt_path, debt_store.document())
        record["status"] = "recovery_blocked"
        _persist_state(state_path, state, since, record)
        return {
            "status": "recovery_blocked",
            "slice": {"since": since, "until": until},
            "advance_frontier": advance_frontier,
        }

    _resolve_generic(debt_store, generic, source)
    source_coverage.write(debt_path, debt_store.document())

    replay = _stage_from_state(
        config, record, "replay", since, until, replay=True,
        expected_snapshot_digests=source["snapshot_digests"],
        source_run_id=str(source["run_id"]), source_run_dir=str(source["run_dir"]),
    )
    if replay is None:
        try:
            child = _run_budgeted_child(
                _replay_command(config, Path(str(source["run_dir"]))),
                root=root, runs_dir=_runs_dir(config),
                budget=budget, cap=2700, grace=30,
            )
        except _BudgetExhausted:
            record["status"] = "source_verified"
            _persist_state(state_path, state, since, record)
            return {"status": "incomplete", "reason": "total_child_budget_exhausted", "slice": {"since": since, "until": until}}
        if child.timed_out or child.returncode != 0:
            record["status"] = "failed"
            _persist_state(state_path, state, since, record)
            return {"status": "failed", "slice": {"since": since, "until": until}}
        replay = _validate_stage(
            config, _result(child.stdout, _runs_dir(config)), since, until, replay=True,
            expected_snapshot_digests=source["snapshot_digests"],
            source_run_id=str(source["run_id"]), source_run_dir=str(source["run_dir"]),
        )
        if replay["accounting_digest"] != source["accounting_digest"]:
            raise CycleError("replay accounting does not exactly match the source")
        record.update({"status": "replay_verified", "replay": replay})
        _persist_state(state_path, state, since, record)

    receipt_path = state_dir / "delivery-receipts" / f"{since}.json"
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
                _publisher_command(config, source, replay, sheet_title=sheet_title),
                root=root, runs_dir=_runs_dir(config),
                budget=budget, cap=900, grace=30,
            )
        except _BudgetExhausted:
            record["status"] = "replay_verified"
            _persist_state(state_path, state, since, record)
            return {"status": "incomplete", "reason": "total_child_budget_exhausted", "slice": {"since": since, "until": until}}
        if child.timed_out or child.returncode != 0:
            record["status"] = "failed"
            _persist_state(state_path, state, since, record)
            return {"status": "failed", "slice": {"since": since, "until": until}}
        verified_source = _validate_stage(
            config, Path(str(source["result_path"])), since, until, replay=False,
            expected_snapshot_digests=expected_snapshots,
        )
        verified_replay = _validate_stage(
            config, Path(str(replay["result_path"])), since, until, replay=True,
            expected_snapshot_digests=source["snapshot_digests"],
            source_run_id=str(source["run_id"]), source_run_dir=str(source["run_dir"]),
        )
        if verified_source != source or verified_replay != replay:
            raise CycleError("delivery inputs drifted while the publisher was running")
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


def run_cycle(config: Mapping[str, Any], *, enable_sheet_write: bool, today: dt.date | None = None) -> dict[str, Any]:
    root = _path(config, "root")
    state_dir = _path(config, "state_dir")
    if not root.is_dir():
        raise CycleError("root is unavailable")
    state_path = state_dir / "review-cycle-state.json"
    debt_path = state_dir / "source-coverage.json"
    with single_instance(state_dir / "review-cycle.lock") as acquired:
        if not acquired:
            return {"status": "locked", "slices": []}
        state = _state(state_path, recovery_since=str(config["recovery_since"]))
        debt_store, migration_warnings = _source_debt(debt_path)
        state["source_debt_warnings"] = migration_warnings
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
            exact_pick = _select_work(
                config, {**state, "next_work_class": "exact"}, debt_store,
                today=today or dt.datetime.now(ZoneInfo(str(config["timezone"]))).date(),
            )
            if (
                routine_pick and exact_pick
                and routine_pick[0][2] != "exact"
                and exact_pick[0][2] == "exact"
            ):
                state["next_work_class"] = (
                    "routine" if selected[0][2] == "exact" else "exact"
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
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        _validate_runtime_root(config, os.environ)
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
