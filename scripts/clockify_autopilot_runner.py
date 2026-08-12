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
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Mapping

try:
    from scripts import source_coverage
except ModuleNotFoundError:  # direct script execution
    import source_coverage  # type: ignore[no-redef]


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


def _result_path(stdout: str, root: Path) -> Path:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not lines:
        raise ConfigurationError("review workflow did not emit an action contract path")
    path = Path(lines[-1]).expanduser().resolve()
    runs = (root / "runs").resolve()
    if path.name != "autopilot-result.json" or runs not in path.parents:
        raise ConfigurationError("review workflow emitted an unsafe action contract path")
    return path


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

        prior = _read_status(status_path)
        coordinator = str(
            environment.get("CLOCKIFY_AUTOPILOT_COORDINATOR") or "omarchy-precision"
        ).strip()
        coverage = source_coverage.read(coverage_path)
        if not coverage_path.exists() or not coverage.get("sources"):
            coverage = source_coverage.bootstrap_from_runs(root / "runs", coordinator)
            if coverage.get("sources"):
                source_coverage.write(coverage_path, coverage)
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
            result_path = _result_path(completed.stdout, root)
            result = json.loads(result_path.read_text(encoding="utf-8"))
            if not isinstance(result, dict):
                raise ConfigurationError("action contract must be a JSON object")
            action = str(result.get("action") or "blocked")
        except (OSError, json.JSONDecodeError, ConfigurationError) as exc:
            _atomic_write(status_path, {
                "schema_version": SCHEMA_VERSION,
                "state": "blocked",
                "started_at": started_at,
                "updated_at": _iso_now(),
                "reason": str(exc)[:300],
            })
            return 2

        completeness = result.get("source_completeness")
        date_range = result.get("date_range")
        if isinstance(completeness, Mapping) and isinstance(date_range, Mapping):
            interval_since = str(date_range.get("since") or effective_since or "")
            coverage = source_coverage.update(
                coverage,
                completeness=completeness,
                interval_since=interval_since,
                interval_until=str(date_range.get("until") or "") or None,
                coordinator=coordinator,
                run_id=str(result.get("run_id") or result_path.parent.name),
                attempted_at=_iso_now(),
            )
            source_coverage.write(coverage_path, coverage)
        debt = source_coverage.active_debt(coverage)

        prior_attempts = (
            int(prior.get("coverage_retry_attempts") or 0)
            if prior.get("state") == "retry_scheduled"
            else 0
        )
        retryable_coverage = (
            action == "coverage_warning"
            or _retryable_peer_coverage(
                result,
                coordinator,
            )
        )
        if retryable_coverage:
            attempts = prior_attempts + 1
            retrying = attempts <= max_retries
            state = "retry_scheduled" if retrying else "coverage_exhausted"
            exit_code = TEMPORARY_COVERAGE_EXIT if retrying else 0
        elif completed.returncode == 0:
            attempts = 0
            state = "complete"
            exit_code = 0
        else:
            attempts = prior_attempts
            state = "blocked" if completed.returncode == 2 else "failed"
            exit_code = completed.returncode or 1

        _atomic_write(status_path, {
            "schema_version": SCHEMA_VERSION,
            "state": state,
            "started_at": started_at,
            "updated_at": _iso_now(),
            "exit_code": exit_code,
            "action": action,
            "result": str(result_path),
            "coverage_retry_attempts": attempts,
            "max_coverage_retries": max_retries,
            "coverage_debt": debt,
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
    if not status or str(resolved) != str(status.get("result") or ""):
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
