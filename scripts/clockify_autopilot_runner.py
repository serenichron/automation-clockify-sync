#!/usr/bin/env python3
"""Persistent scheduler boundary for the Clockify review workflow.

The Multica specialist starts this user service and returns quickly. The service
owns collection, semantic analysis, durable review ingestion, and bounded delayed
retries when fleet evidence is incomplete. It never writes Clockify or Sheets.
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Mapping

try:
    from scripts import source_coverage
    from scripts import collector_receipts
except ModuleNotFoundError:  # direct script execution
    import source_coverage  # type: ignore[no-redef]
    import collector_receipts  # type: ignore[no-redef]


SCHEMA_VERSION = "clockify-autopilot-runner/v1"
TEMPORARY_COVERAGE_EXIT = 75


class ConfigurationError(ValueError):
    """The persistent runner cannot safely start."""


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _path(environment: Mapping[str, str], name: str, default: Path) -> Path:
    value = str(environment.get(name) or default).strip()
    path = Path(value).expanduser().resolve()
    if not path.is_absolute():
        raise ConfigurationError(f"{name} must be absolute")
    return path


def _positive_int(environment: Mapping[str, str], name: str, default: int) -> int:
    try:
        value = int(str(environment.get(name) or default))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an integer") from exc
    if value < 0:
        raise ConfigurationError(f"{name} must be non-negative")
    return value


def _atomic_write(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _read_status(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _result_paths(stdout: str, root: Path) -> tuple[Path, ...]:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise ConfigurationError("review workflow did not emit an action contract path")
    runs = (root / "runs").resolve()
    paths: list[Path] = []
    for line in lines:
        path = Path(line).expanduser().resolve()
        if path.name != "autopilot-result.json" or runs not in path.parents:
            raise ConfigurationError("review workflow emitted an unsafe action contract path")
        if path in paths:
            raise ConfigurationError("review workflow emitted a duplicate action contract path")
        paths.append(path)
    return tuple(paths)


def _command(
    environment: Mapping[str, str], root: Path, *, effective_since: str | None = None
) -> list[str]:
    command = [sys.executable, str(root / "scripts" / "clockify_review_run.py")]
    for variable, option in (
        ("CLOCKIFY_AUTOPILOT_SINCE", "--since"),
        ("CLOCKIFY_AUTOPILOT_UNTIL", "--until"),
        ("CLOCKIFY_AUTOPILOT_STATE", "--state"),
        ("CLOCKIFY_AUTOPILOT_CORRECTIONS", "--corrections"),
        ("CLOCKIFY_AUTOPILOT_CACHE", "--analyzer-cache"),
        ("CLOCKIFY_AUTOPILOT_ANALYZER_TARGET_BODY_BYTES", "--analyzer-target-body-bytes"),
        ("CLOCKIFY_AUTOPILOT_ANALYZER_MAX_EVENTS", "--analyzer-max-events-per-chunk"),
        ("CLOCKIFY_AUTOPILOT_ANALYZER_WORKERS", "--analyzer-workers"),
    ):
        value = str(environment.get(variable) or "").strip()
        if variable == "CLOCKIFY_AUTOPILOT_SINCE" and effective_since:
            value = effective_since
        if value:
            command.extend([option, value])
    return command


def _retryable_peer_coverage(
    result: Mapping[str, object], coordinator: str
) -> bool:
    completeness = result.get("source_completeness")
    if not isinstance(completeness, Mapping) or completeness.get("status") != "incomplete":
        return False
    incomplete = completeness.get("incomplete_sources")
    if not isinstance(incomplete, list) or not incomplete:
        return False
    peer_prefixes = ("sessions/", "repositories/")
    return all(
        isinstance(source, str)
        and source.startswith(peer_prefixes)
        and source.rsplit("/", 1)[-1] != coordinator
        for source in incomplete
    )


def _result_interval(
    source: str, result: Mapping[str, object]
) -> source_coverage.SourceInterval:
    """Derive an exact identity only when the action contract supplies UTC bounds."""
    date_range = result.get("date_range")
    if not isinstance(date_range, Mapping):
        raise ValueError("source coverage interval requires explicit UTC bounds")
    since = date_range.get("since")
    until = date_range.get("until")
    if not (
        isinstance(since, str) and since.endswith("Z")
        and isinstance(until, str) and until.endswith("Z")
    ):
        raise ValueError("source coverage interval requires explicit UTC bounds")
    raw_slice_id = result.get("slice_id")
    if raw_slice_id is None:
        identity = f"{source}\0{since}\0{until}".encode("utf-8")
        slice_id = "source-completeness-" + hashlib.sha256(identity).hexdigest()[:16]
    elif isinstance(raw_slice_id, str) and raw_slice_id.strip():
        slice_id = raw_slice_id.strip()
    else:
        raise ValueError("source coverage interval slice ID must be a non-empty string")
    return source_coverage.SourceInterval(
        source=source,
        since_utc=since,
        until_utc=until,
        slice_id=slice_id,
        compatibility_version="source-debt/v1",
    )


def _resume_state_digest(source: str, interval: source_coverage.SourceInterval) -> str:
    value = f"{source}\0{interval.debt_id}".encode("utf-8")
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _record_coverage_failures(
    store: source_coverage.SourceDebtStore,
    results: list[Mapping[str, object]],
    coordinator: str,
    attempted_at: str,
) -> tuple[source_coverage.DebtItem, ...]:
    recorded: list[source_coverage.DebtItem] = []
    for result in results:
        action = str(result.get("action") or "blocked")
        completeness = result.get("source_completeness")
        incomplete = (
            completeness.get("incomplete_sources")
            if isinstance(completeness, Mapping)
            and completeness.get("status") == "incomplete"
            else ()
        )
        peers = [
            source for source in incomplete
            if isinstance(source, str) and source_coverage.is_peer(source, coordinator)
        ]
        if peers:
            for source in peers:
                interval = _result_interval(source, result)
                recorded.append(store.record_failure(
                    interval,
                    failure_class="coverage_incomplete",
                    retryable=True,
                    resume_state_digest=_resume_state_digest(source, interval),
                    attempted_at=attempted_at,
                ))
        elif action == "coverage_warning":
            interval = _result_interval("runner/coverage-warning", result)
            recorded.append(store.record_failure(
                interval,
                failure_class="coverage_warning",
                retryable=True,
                resume_state_digest=_resume_state_digest("runner/coverage-warning", interval),
                attempted_at=attempted_at,
            ))
    return tuple(recorded)


def _record_verified_completions(
    store: source_coverage.SourceDebtStore,
    result_path: Path,
    result: Mapping[str, object],
    *,
    completed_at: str,
) -> tuple[source_coverage.DebtItem, ...]:
    """Resolve only currently active exact debts bound by a verified bundle."""
    bundle_digest = result.get("completion_bundle_digest")
    if bundle_digest is None:
        return ()
    if not isinstance(bundle_digest, str):
        raise ValueError("completion bundle digest is invalid")
    try:
        bundle = collector_receipts.load_completion_bundle(
            result_path.parent / "completion-bundle.json", run_dir=result_path.parent,
        )
    except collector_receipts.CollectorReceiptError as exc:
        raise ValueError("completion bundle cannot be verified") from exc
    if bundle.bundle_digest != bundle_digest:
        raise ValueError("completion bundle digest does not match action contract")
    date_range = result.get("date_range")
    if not isinstance(date_range, Mapping):
        raise ValueError("completion bundle action contract has no date range")
    since, until, slice_id = date_range.get("since"), date_range.get("until"), result.get("slice_id")
    if not all(isinstance(value, str) for value in (since, until, slice_id)):
        raise ValueError("completion bundle action contract identity is invalid")
    if (bundle.since_utc, bundle.until_utc, bundle.slice_id) != (since, until, slice_id):
        raise ValueError("completion bundle action contract identity does not match")
    completeness = result.get("source_completeness")
    if not isinstance(completeness, Mapping):
        raise ValueError("completion bundle action contract has no source completeness")
    incomplete = completeness.get("incomplete_sources")
    if not isinstance(incomplete, list) or not all(isinstance(value, str) for value in incomplete):
        raise ValueError("completion bundle action contract source completeness is invalid")
    sources = completeness.get("sources")

    def source_is_complete(source: str) -> bool:
        if completeness.get("status") == "complete" and not incomplete:
            return True
        item = sources.get(source) if isinstance(sources, Mapping) else None
        return isinstance(item, Mapping) and item.get("status") in {"complete", "excluded"}

    resolved = []
    for item in store.active():
        interval = item.interval
        if (
            interval.compatibility_version != "source-debt/v1"
            or interval.slice_id != bundle.slice_id
            or interval.since_utc != bundle.since_utc
            or interval.until_utc != bundle.until_utc
            or not source_is_complete(interval.source)
        ):
            continue
        resolved.append(store.record_complete(
            interval, completion_bundle_digest=bundle.bundle_digest, completed_at=completed_at,
        ))
    return tuple(resolved)


def _debt_status(items: tuple[source_coverage.DebtItem, ...]) -> list[dict[str, object]]:
    return [
        {
            "debt_id": item.debt_id,
            "source": item.interval.source,
            "since_utc": item.interval.since_utc,
            "until_utc": item.interval.until_utc,
            "slice_id": item.interval.slice_id,
            "compatibility_version": item.interval.compatibility_version,
            "retry_count": item.retry_count,
            "retryable": item.retryable,
            "status": item.status,
            "next_eligible_at": item.next_eligible_at,
            "terminal_reason": item.terminal_reason,
        }
        for item in items
    ]


def run(environment: Mapping[str, str] | None = None) -> int:
    environment = os.environ if environment is None else environment
    try:
        root = _path(
            environment,
            "CLOCKIFY_AUTOPILOT_ROOT",
            Path.home() / "Work" / "automation-clockify-sync",
        )
        status_path = _path(
            environment,
            "CLOCKIFY_AUTOPILOT_STATUS",
            root / "state" / "autopilot-runner-status.json",
        )
        lock_path = _path(
            environment,
            "CLOCKIFY_AUTOPILOT_LOCK",
            root / "state" / "autopilot-runner.lock",
        )
        coverage_path = _path(
            environment,
            "CLOCKIFY_AUTOPILOT_COVERAGE_STATE",
            root / "state" / "source-coverage.json",
        )
        max_retries = _positive_int(
            environment, "CLOCKIFY_AUTOPILOT_MAX_COVERAGE_RETRIES", 2
        )
        if not (root / "scripts" / "clockify_review_run.py").is_file():
            raise ConfigurationError("Clockify review entrypoint is unavailable")
    except ConfigurationError as exc:
        print(f"clockify autopilot runner blocked: {exc}", file=sys.stderr)
        return 2

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0

        coordinator = str(
            environment.get("CLOCKIFY_AUTOPILOT_COORDINATOR") or "omarchy-precision"
        ).strip()
        coverage = source_coverage.read(coverage_path)
        if not coverage_path.exists() and not coverage.get("events"):
            legacy = source_coverage.bootstrap_from_runs(root / "runs", coordinator)
            if legacy.get("sources"):
                source_coverage.write(coverage_path, legacy)
                coverage = source_coverage.read(coverage_path)
        store = source_coverage.SourceDebtStore.from_document(coverage)
        migration_warnings = coverage.get("migration_warnings")
        if not isinstance(migration_warnings, list):
            migration_warnings = []
        effective_since = source_coverage.effective_since(
            str(environment.get("CLOCKIFY_AUTOPILOT_SINCE") or "").strip() or None,
            coverage,
        )
        started_at = _iso_now()
        _atomic_write(status_path, {
            "schema_version": SCHEMA_VERSION,
            "state": "running",
            "started_at": started_at,
            "updated_at": started_at,
            "pid": os.getpid(),
        })
        completed = subprocess.run(
            _command(environment, root, effective_since=effective_since),
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        try:
            result_paths = _result_paths(completed.stdout, root)
            results = []
            for result_path in result_paths:
                result = json.loads(result_path.read_text(encoding="utf-8"))
                if not isinstance(result, dict):
                    raise ConfigurationError("action contract must be a JSON object")
                results.append(result)
        except (OSError, json.JSONDecodeError, ConfigurationError) as exc:
            _atomic_write(status_path, {
                "schema_version": SCHEMA_VERSION,
                "state": "blocked",
                "started_at": started_at,
                "updated_at": _iso_now(),
                "reason": str(exc)[:300],
            })
            return 2

        attempted_at = _iso_now()
        try:
            recorded_debt = _record_coverage_failures(
                store, results, coordinator, attempted_at
            )
            completed_debt = tuple(
                item
                for result_path, result in zip(result_paths, results, strict=True)
                for item in _record_verified_completions(
                    store, result_path, result, completed_at=attempted_at
                )
            )
        except ValueError as exc:
            _atomic_write(status_path, {
                "schema_version": SCHEMA_VERSION,
                "state": "blocked",
                "started_at": started_at,
                "updated_at": _iso_now(),
                "reason": str(exc)[:300],
            })
            return 2
        retryable_coverage = False
        for result in results:
            action = str(result.get("action") or "blocked")
            completeness = result.get("source_completeness")
            incomplete = (
                isinstance(completeness, Mapping)
                and completeness.get("status") == "incomplete"
            )
            if action not in {"blocked", "coverage_warning"} and not incomplete:
                continue
            retryable_coverage = (
                action == "coverage_warning"
                or _retryable_peer_coverage(result, coordinator)
            )
            break
        action = str(results[-1].get("action") or "blocked")
        if retryable_coverage:
            for item in recorded_debt:
                if item.retry_count > max_retries:
                    store.exhaust(item.debt_id, terminal_reason="retry_limit")
            eligible_ids = {item.debt_id for item in store.eligible(attempted_at)}
            retrying = any(item.debt_id in eligible_ids for item in recorded_debt)
            state = "retry_scheduled" if retrying else "coverage_exhausted"
            exit_code = TEMPORARY_COVERAGE_EXIT if retrying else 0
        elif completed.returncode == 0:
            state = "complete"
            exit_code = 0
        else:
            state = "blocked" if completed.returncode == 2 else "failed"
            exit_code = completed.returncode or 1
        coverage = store.document(migration_warnings=migration_warnings)
        source_coverage.write(coverage_path, coverage)
        debt = source_coverage.active_debt(coverage)
        debt_items = store.active()
        attempts = max((item.retry_count for item in debt_items), default=0)

        _atomic_write(status_path, {
            "schema_version": SCHEMA_VERSION,
            "state": state,
            "started_at": started_at,
            "updated_at": _iso_now(),
            "exit_code": exit_code,
            "action": action,
            "result": str(result_paths[-1]),
            "results": [str(result_path) for result_path in result_paths],
            "coverage_retry_attempts": attempts,
            "max_coverage_retries": max_retries,
            "coverage_debt": debt,
            "coverage_debts": _debt_status(debt_items),
            "completed_coverage_debts": [item.debt_id for item in completed_debt],
            "coverage_state": str(coverage_path),
            "effective_since": effective_since,
        })
        if completed.stderr and exit_code not in {0, TEMPORARY_COVERAGE_EXIT}:
            print(completed.stderr.strip()[:500], file=sys.stderr)
        return int(exit_code)


def mark_reported(environment: Mapping[str, str] | None, result: Path) -> int:
    environment = os.environ if environment is None else environment
    root = _path(
        environment,
        "CLOCKIFY_AUTOPILOT_ROOT",
        Path.home() / "Work" / "automation-clockify-sync",
    )
    status_path = _path(
        environment,
        "CLOCKIFY_AUTOPILOT_STATUS",
        root / "state" / "autopilot-runner-status.json",
    )
    status = _read_status(status_path)
    resolved = result.expanduser().resolve()
    results = status.get("results")
    allowed = (
        {str(path) for path in results if isinstance(path, str)}
        if isinstance(results, list)
        else {str(status.get("result") or "")}
    )
    if not status or str(resolved) not in allowed:
        print("clockify autopilot runner blocked: result does not match status", file=sys.stderr)
        return 2
    status["reported_at"] = _iso_now()
    _atomic_write(status_path, status)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mark-reported", type=Path)
    args = parser.parse_args(argv)
    if args.mark_reported:
        return mark_reported(None, args.mark_reported)
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
