#!/usr/bin/env python3
"""Durable single-instance runner for one resumable Clockify accounting pass.

The semantic cache remains the source of resumability. This wrapper adds a
machine-readable lifecycle status, duplicate-run prevention, and exit semantics
that a systemd user service can supervise without logging environment secrets.
"""
from __future__ import annotations

import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Mapping


SCHEMA_VERSION = "clockify-accounting-runner/v1"
RESULT_SCHEMA_VERSION = 1
APPROVED_FLASH_MODELS = {
    "deepseek-v4-flash:cloud",
    "deepseek-v4-flash:0731-cloud",
}
APPROVED_FLASH_REVISION = (
    "6ca9e29c41ded618e527ee40e305ed5e4d8319b571d5b6695a30e1df65f103cc"
)
REQUIRED_RESULT_ARTIFACTS = (
    "semantic-analysis.json",
    "allocation-report.json",
    "fathom-reconciliation.json",
    "review-regression-results.json",
    "proposals.json",
    "ambiguous.json",
    "skipped.json",
)


class RunnerConfigurationError(ValueError):
    """The runner environment is absent or unsafe."""


def _validated_analyzer_route(
    environment: Mapping[str, str], cache: Path
) -> dict[str, str]:
    """Return the approved route and reject model drift in a sealed cache."""
    model = str(environment.get("CLOCKIFY_ANALYZER_PRIMARY_MODEL") or "").strip()
    revision = str(
        environment.get("CLOCKIFY_ANALYZER_PRIMARY_REVISION") or ""
    ).strip()
    if model not in APPROVED_FLASH_MODELS:
        raise RunnerConfigurationError(
            "CLOCKIFY_ANALYZER_PRIMARY_MODEL must be an approved Flash route"
        )
    if revision != APPROVED_FLASH_REVISION:
        raise RunnerConfigurationError(
            "CLOCKIFY_ANALYZER_PRIMARY_REVISION must match the approved Flash release"
        )
    if cache.is_file():
        cached_models: set[str] = set()
        try:
            with cache.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    cached_model = str(record.get("model") or "").strip()
                    if cached_model:
                        cached_models.add(cached_model)
        except (OSError, json.JSONDecodeError, AttributeError) as exc:
            raise RunnerConfigurationError(
                "CLOCKIFY_ACCOUNTING_CACHE metadata is invalid"
            ) from exc
        if cached_models and cached_models != {model}:
            raise RunnerConfigurationError(
                "CLOCKIFY_ACCOUNTING_CACHE is sealed to a different or mixed model tag"
            )
    return {"model": model, "revision": revision}


def _iso_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _required_path(environment: Mapping[str, str], name: str) -> Path:
    value = str(environment.get(name) or "").strip()
    if not value:
        raise RunnerConfigurationError(f"{name} is required")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RunnerConfigurationError(f"{name} must be absolute")
    return path.resolve()


def _optional_positive_int(
    environment: Mapping[str, str], name: str
) -> int | None:
    value = str(environment.get(name) or "").strip()
    if not value:
        return None
    try:
        parsed = int(value)
    except ValueError as exc:
        raise RunnerConfigurationError(f"{name} must be an integer") from exc
    if parsed <= 0:
        raise RunnerConfigurationError(f"{name} must be positive")
    return parsed


def _optional_absolute_path(
    environment: Mapping[str, str], name: str, default: Path
) -> Path:
    value = str(environment.get(name) or "").strip()
    if not value:
        return default.resolve()
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RunnerConfigurationError(f"{name} must be absolute")
    return path.resolve()


def build_command(
    environment: Mapping[str, str], *, python_executable: str = sys.executable
) -> tuple[list[str], Path, Path, Path, Path]:
    """Return the private-data-free command and durable control paths."""
    root = _required_path(environment, "CLOCKIFY_ACCOUNTING_ROOT")
    run_dir = _required_path(environment, "CLOCKIFY_ACCOUNTING_RUN_DIR")
    cache = _required_path(environment, "CLOCKIFY_ACCOUNTING_CACHE")
    _validated_analyzer_route(environment, cache)
    status = _optional_absolute_path(
        environment,
        "CLOCKIFY_ACCOUNTING_STATUS",
        cache.parent / "runner-status.json",
    )
    lock = _optional_absolute_path(
        environment,
        "CLOCKIFY_ACCOUNTING_LOCK",
        cache.parent / "runner.lock",
    )
    script = root / "scripts" / "work_accounting_pipeline.py"
    if not script.is_file():
        raise RunnerConfigurationError("work accounting pipeline is missing")
    if not run_dir.is_dir():
        raise RunnerConfigurationError("CLOCKIFY_ACCOUNTING_RUN_DIR does not exist")
    command = [
        python_executable,
        str(script),
        str(run_dir),
        "--root",
        str(root),
        "--analyzer-cache",
        str(cache),
    ]
    for environment_name, flag in (
        ("CLOCKIFY_ACCOUNTING_TARGET_BODY_BYTES", "--analyzer-target-body-bytes"),
        ("CLOCKIFY_ACCOUNTING_MAX_EVENTS", "--analyzer-max-events-per-chunk"),
        ("CLOCKIFY_ACCOUNTING_WORKERS", "--analyzer-workers"),
    ):
        if value := _optional_positive_int(environment, environment_name):
            command.extend([flag, str(value)])
    return command, run_dir, cache, status, lock


def _atomic_write(path: Path, document: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _command_digest(command: list[str]) -> str:
    return hashlib.sha256(
        json.dumps(command, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _complete_result(run_dir: Path) -> Path | None:
    """Return the completion marker only when its full artifact set is valid."""
    result = run_dir / "work-accounting-result.json"
    try:
        document = json.loads(result.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (
        not isinstance(document, dict)
        or document.get("schema_version") != RESULT_SCHEMA_VERSION
        or document.get("external_writes") is not False
        or any(not (run_dir / name).is_file() for name in REQUIRED_RESULT_ARTIFACTS)
    ):
        return None
    return result


def run(environment: Mapping[str, str] | None = None) -> int:
    environment = os.environ if environment is None else environment
    try:
        command, run_dir, cache, status, lock = build_command(environment)
    except RunnerConfigurationError as exc:
        print(f"clockify accounting runner blocked: {exc}", file=sys.stderr)
        return 2

    base_status: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "run_dir": str(run_dir),
        "cache": str(cache),
        "command_digest": _command_digest(command),
        "analyzer_route": {
            "model": str(environment["CLOCKIFY_ANALYZER_PRIMARY_MODEL"]),
            "revision": str(environment["CLOCKIFY_ANALYZER_PRIMARY_REVISION"]),
        },
    }
    if final_result := _complete_result(run_dir):
        _atomic_write(status, {
            **base_status,
            "state": "complete",
            "updated_at": _iso_now(),
            "result": str(final_result),
        })
        return 0

    lock.parent.mkdir(parents=True, exist_ok=True)
    with lock.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("clockify accounting runner blocked: another run holds the lock", file=sys.stderr)
            return 2
        started_at = _iso_now()
        _atomic_write(status, {
            **base_status,
            "state": "running",
            "started_at": started_at,
            "updated_at": started_at,
            "pid": os.getpid(),
        })
        completed = subprocess.run(command, cwd=root_from_command(command), check=False)
        finished_at = _iso_now()
        final_result = _complete_result(run_dir)
        if completed.returncode == 0 and final_result is not None:
            state = "complete"
            runner_exit_code = 0
        elif completed.returncode == 2:
            state = "blocked"
            runner_exit_code = 2
        else:
            state = "failed"
            runner_exit_code = completed.returncode or 1
        _atomic_write(status, {
            **base_status,
            "state": state,
            "started_at": started_at,
            "updated_at": finished_at,
            "exit_code": runner_exit_code,
            **({"result": str(final_result)} if final_result is not None else {}),
        })
        return int(runner_exit_code)


def root_from_command(command: list[str]) -> Path:
    """Return the configured root without consulting process environment."""
    try:
        return Path(command[command.index("--root") + 1])
    except (ValueError, IndexError) as exc:
        raise RunnerConfigurationError("runner command lacks --root") from exc


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
