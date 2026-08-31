"""Bounded execution for the Clockify autopilot's owned review child."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import selectors
import signal
import subprocess
import time


_MAX_CAPTURED_STDOUT = 4096
_MAX_DRAIN_CHUNKS_PER_CYCLE = 8
_POST_KILL_REAP_SECONDS = 1.0
_POLL_SECONDS = 0.05


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


def _append_bounded(target: bytearray, value: bytes) -> None:
    target.extend(value[:max(0, _MAX_CAPTURED_STDOUT - len(target))])


def _drain_ready(
    selector: selectors.BaseSelector,
    stdout: bytearray,
    saw_stderr: list[bool],
    *,
    timeout: float,
) -> None:
    """Drain ready pipes without retaining unbounded child output."""
    for key, _ in selector.select(timeout):
        stream = key.fileobj
        for _ in range(_MAX_DRAIN_CHUNKS_PER_CYCLE):
            try:
                chunk = os.read(stream.fileno(), 65536)
            except BlockingIOError:
                break
            if not chunk:
                selector.unregister(stream)
                stream.close()
                break
            if key.data == "stdout":
                _append_bounded(stdout, chunk)
            else:
                saw_stderr[0] = True


def _wait_with_drain(
    process: subprocess.Popen[bytes],
    selector: selectors.BaseSelector,
    stdout: bytearray,
    saw_stderr: list[bool],
    *,
    seconds: float,
) -> bool:
    """Drain pipes while waiting for the direct child; never wait for EOF."""
    deadline = time.monotonic() + seconds
    while True:
        if process.poll() is not None:
            _drain_ready(selector, stdout, saw_stderr, timeout=0)
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        _drain_ready(
            selector, stdout, saw_stderr, timeout=min(_POLL_SECONDS, remaining)
        )


def _close_pipes(selector: selectors.BaseSelector) -> None:
    """Release the parent ends even if an escaped descendant retained theirs."""
    for key in list(selector.get_map().values()):
        stream = key.fileobj
        selector.unregister(stream)
        stream.close()


def _action_contract_stdout(value: bytearray, cwd: str) -> str:
    """Keep only bounded, in-root action-contract paths from child stdout."""
    runs = (Path(cwd) / "runs").resolve()
    paths: list[str] = []
    for line in bytes(value).decode("utf-8", errors="replace").splitlines():
        candidate = line.strip()
        if not candidate:
            continue
        path = Path(candidate).expanduser().resolve()
        if path.name == "autopilot-result.json" and runs in path.parents:
            paths.append(str(path))
    return "".join(f"{path}\n" for path in paths)


def _terminate_owned_group(process: subprocess.Popen[bytes], signal_to_send: int) -> None:
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
    process: subprocess.Popen[bytes] = subprocess.Popen(
        values,
        cwd=working_directory,
        env=child_environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    if process.stdout is None or process.stderr is None:
        raise RuntimeError("owned child pipes are unavailable")
    os.set_blocking(process.stdout.fileno(), False)
    os.set_blocking(process.stderr.fileno(), False)
    selector = selectors.DefaultSelector()
    selector.register(process.stdout, selectors.EVENT_READ, "stdout")
    selector.register(process.stderr, selectors.EVENT_READ, "stderr")
    stdout = bytearray()
    saw_stderr = [False]
    try:
        finished = _wait_with_drain(
            process, selector, stdout, saw_stderr, seconds=timeout.total_seconds
        )
        if finished:
            return ChildResult(
                process.returncode,
                _action_contract_stdout(stdout, working_directory),
                "child stderr suppressed" if saw_stderr[0] else "",
                False,
                time.monotonic() - started,
            )

        _terminate_owned_group(process, signal.SIGTERM)
        finished = _wait_with_drain(
            process, selector, stdout, saw_stderr, seconds=timeout.grace_seconds
        )
        if not finished:
            _terminate_owned_group(process, signal.SIGKILL)
            _wait_with_drain(
                process,
                selector,
                stdout,
                saw_stderr,
                seconds=_POST_KILL_REAP_SECONDS,
            )
        return ChildResult(
            None,
            _action_contract_stdout(stdout, working_directory),
            "child stderr suppressed" if saw_stderr[0] else "",
            True,
            time.monotonic() - started,
        )
    finally:
        _close_pipes(selector)
        selector.close()
