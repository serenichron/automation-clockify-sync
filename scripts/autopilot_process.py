"""Bounded execution for the Clockify autopilot's owned review child."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import signal
import subprocess
import time


_MAX_CAPTURED_STDOUT = 4096


@dataclass(frozen=True)
class ChildTimeoutConfig:
    """Execution budget and termination grace period for one owned child."""

    total_seconds: int
    grace_seconds: int

    def __post_init__(self) -> None:
        for name, value in (
            ("total_seconds", self.total_seconds),
            ("grace_seconds", self.grace_seconds),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.grace_seconds >= self.total_seconds:
            raise ValueError("grace_seconds must be less than total_seconds")


@dataclass(frozen=True)
class ChildResult:
    """Safe, bounded result of an owned child process invocation."""

    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool
    duration_seconds: float


def _validate_command(command: Sequence[str]) -> list[str]:
    if (
        isinstance(command, (str, bytes))
        or not isinstance(command, Sequence)
        or not command
    ):
        raise ValueError("command must be a non-empty sequence of strings")
    values = list(command)
    if not all(isinstance(value, str) and value and "\0" not in value for value in values):
        raise ValueError("command must contain non-empty strings without NUL bytes")
    return values


def _validate_cwd(cwd: Path | str) -> str:
    try:
        raw_path = Path(cwd).expanduser()
    except TypeError as exc:
        raise ValueError("cwd must be an existing absolute directory") from exc
    if not raw_path.is_absolute():
        raise ValueError("cwd must be an existing absolute directory")
    path = raw_path.resolve()
    if not path.is_dir():
        raise ValueError("cwd must be an existing absolute directory")
    return str(path)


def _validate_environment(environment: Mapping[str, str]) -> dict[str, str]:
    if not isinstance(environment, Mapping):
        raise ValueError("environment must be a string mapping")
    values = dict(environment)
    if not all(
        isinstance(name, str)
        and name
        and "=" not in name
        and "\0" not in name
        and isinstance(value, str)
        and "\0" not in value
        for name, value in values.items()
    ):
        raise ValueError("environment must contain valid string names and values")
    return values


def _safe_stdout(value: str) -> str:
    return value[:_MAX_CAPTURED_STDOUT]


def _safe_stderr(value: str) -> str:
    return "child stderr suppressed" if value else ""


def _terminate_owned_group(process: subprocess.Popen[str], signal_to_send: int) -> None:
    """Signal just the process group created by this invocation."""
    try:
        os.killpg(process.pid, signal_to_send)
    except ProcessLookupError:
        # The owned child exited between communicate timing out and signaling.
        pass


def run_child_bounded(
    command: Sequence[str],
    *,
    cwd: Path | str,
    timeout: ChildTimeoutConfig,
    environment: Mapping[str, str],
) -> ChildResult:
    """Run one new-session child, terminating only that child's process group."""
    values = _validate_command(command)
    working_directory = _validate_cwd(cwd)
    child_environment = _validate_environment(environment)
    if not isinstance(timeout, ChildTimeoutConfig):
        raise ValueError("timeout must be a ChildTimeoutConfig")

    started = time.monotonic()
    process = subprocess.Popen(
        values,
        cwd=working_directory,
        env=child_environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout.total_seconds)
        return ChildResult(
            process.returncode,
            _safe_stdout(stdout),
            _safe_stderr(stderr),
            False,
            time.monotonic() - started,
        )
    except subprocess.TimeoutExpired:
        _terminate_owned_group(process, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=timeout.grace_seconds)
        except subprocess.TimeoutExpired:
            _terminate_owned_group(process, signal.SIGKILL)
            stdout, stderr = process.communicate()
        return ChildResult(
            None,
            _safe_stdout(stdout),
            _safe_stderr(stderr),
            True,
            time.monotonic() - started,
        )
