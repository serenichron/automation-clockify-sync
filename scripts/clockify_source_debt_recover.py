#!/usr/bin/env python3
"""Recollect one exact source debt without mutating its immutable parent run."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
from typing import Any, Mapping

try:
    from scripts import clockify_sync_collect as collector
    from scripts import collector_receipts
except ModuleNotFoundError:  # direct script execution
    import clockify_sync_collect as collector  # type: ignore[no-redef]
    import collector_receipts  # type: ignore[no-redef]


ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"
_SCHEMA = "source-debt-recovery/v1"
_ATTEMPT = re.compile(r"sha256:[0-9a-f]{64}")
RECONCILIATION_SNAPSHOTS = (
    "period-manifest.json",
    "routing.json",
    "review-corrections.jsonl",
    "review-acceptance.jsonl",
)


class SourceDebtRecoveryError(ValueError):
    """Recovery lineage or attempt state is unsafe."""


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _digest(value: object) -> str:
    return _digest_bytes(_canonical(value))


def _file_digest(path: Path, *, label: str) -> str:
    if path.is_symlink() or not path.is_file():
        raise SourceDebtRecoveryError(f"{label} is missing or unsafe")
    return _digest_bytes(path.read_bytes())


def _read_object(path: Path, *, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise SourceDebtRecoveryError(f"{label} is missing or unsafe")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceDebtRecoveryError(f"{label} is invalid") from exc
    if not isinstance(value, dict):
        raise SourceDebtRecoveryError(f"{label} must be an object")
    return value


def _direct_run(path: Path, *, label: str) -> Path:
    requested = Path(path)
    if not requested.is_absolute():
        raise SourceDebtRecoveryError(f"{label} must be an absolute canonical path")
    resolved = requested.resolve()
    if requested != resolved:
        raise SourceDebtRecoveryError(
            f"{label} must be canonical and contain no symlink components"
        )
    if resolved.parent != RUNS.resolve() or not resolved.is_dir():
        raise SourceDebtRecoveryError(f"{label} must be a direct child of {RUNS.resolve()}")
    return resolved


def _parse_utc(value: object, *, label: str) -> dt.datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise SourceDebtRecoveryError(f"{label} must be canonical UTC")
    try:
        parsed = dt.datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise SourceDebtRecoveryError(f"{label} must be canonical UTC") from exc
    if parsed.isoformat().replace("+00:00", "Z") != value:
        raise SourceDebtRecoveryError(f"{label} must be canonical UTC")
    return parsed


@dataclass(frozen=True)
class RecoveryResult:
    run_dir: Path
    parent_run_dir: Path
    checkpoint_dir: Path
    transition: dict[str, object]
    collection_reused: bool


@dataclass(frozen=True)
class _Parent:
    run_dir: Path
    report: dict[str, Any]
    bundle: collector_receipts.SliceCompletionBundle
    identity: collector.BacklogIdentity
    slice_: object
    source: str
    snapshots: dict[str, str]
    routing: dict[str, Any]
    fleet: dict[str, Any]
    coordinator: str
    calendly_optional: bool


def _validate_parent(
    parent_path: Path, source: str, *, _visited: frozenset[Path] | None = None,
) -> _Parent:
    parent = _direct_run(parent_path, label="recovery parent")
    current_coordinator = collector._current_coordinator_identity()
    if not isinstance(source, str) or not collector._is_tolerated_peer_gap(
        source, current_coordinator
    ):
        raise SourceDebtRecoveryError("recovery source is not a safe peer identity")
    if any((parent / name).exists() for name in ("replay-source.json", "repair-source.json")):
        raise SourceDebtRecoveryError("replay or repair runs cannot be recovery parents")
    report = _read_object(parent / "run-report.json", label="parent run report")
    if report.get("run_id") != parent.name:
        raise SourceDebtRecoveryError("recovery parent identity is invalid")
    derived = "source_debt_recovery" in report
    if derived:
        transition = report.get("source_debt_recovery")
        if not isinstance(transition, Mapping) or not all(
            isinstance(transition.get(key), str)
            for key in ("parent_run_id", "source", "attempt_id")
        ):
            raise SourceDebtRecoveryError("derived recovery parent transition is invalid")
        try:
            if __package__:
                from scripts import clockify_review_run as review_run
            else:  # direct script execution
                import clockify_review_run as review_run  # type: ignore[no-redef]
            review_run.verify_source_debt_recovery_completion(
                parent,
                parent_run_dir=RUNS / str(transition["parent_run_id"]),
                source=str(transition["source"]),
                attempt_id=str(transition["attempt_id"]),
                _visited=_visited,
            )
        except (OSError, ValueError, RecursionError) as exc:
            raise SourceDebtRecoveryError(
                "derived recovery parent terminal verification failed"
            ) from exc
    try:
        bundle = collector_receipts.load_completion_bundle(
            parent / "completion-bundle.json", run_dir=parent
        )
        coverage = collector_receipts.completion_coverage(bundle)
    except (OSError, ValueError, collector_receipts.CollectorReceiptError) as exc:
        raise SourceDebtRecoveryError("parent completion bundle is invalid") from exc
    if bundle.replay:
        raise SourceDebtRecoveryError("replay runs cannot be recovery parents")
    incomplete = coverage.get("incomplete_sources")
    sources = coverage.get("sources")
    source_record = sources.get(source) if isinstance(sources, Mapping) else None
    if (
        not isinstance(incomplete, list)
        or source not in incomplete
        or not isinstance(source_record, Mapping)
        or source_record.get("status") in {"complete", "excluded"}
    ):
        raise SourceDebtRecoveryError("requested source is not canonically incomplete in the parent")

    finalization = _read_object(parent / "slice-finalization.json", label="parent slice finalization")
    if set(finalization) != {
        "schema_version", "backlog_identity", "slice_id", "since_utc", "until_utc"
    } or finalization.get("schema_version") != "collector-slice-finalization/v1":
        raise SourceDebtRecoveryError("parent slice finalization schema is invalid")
    try:
        identity = collector.BacklogIdentity(**finalization["backlog_identity"])
        slices = collector.plan_slices(
            _parse_utc(identity.since_utc, label="backlog since"),
            _parse_utc(identity.until_utc, label="backlog until"),
            zone=collector.BUCHAREST,
            max_days=identity.max_days,
        )
    except (KeyError, TypeError, ValueError, collector.BacklogError) as exc:
        raise SourceDebtRecoveryError("parent backlog identity is invalid") from exc
    slice_ = next((item for item in slices if item.slice_id == finalization.get("slice_id")), None)
    if slice_ is None or (
        finalization.get("since_utc") != collector.iso_utc(slice_.since)
        or finalization.get("until_utc") != collector.iso_utc(slice_.until)
        or bundle.slice_id != slice_.slice_id
        or bundle.since_utc != collector.iso_utc(slice_.since)
        or bundle.until_utc != collector.iso_utc(slice_.until)
    ):
        raise SourceDebtRecoveryError("parent slice identity does not match its bundle")
    if not derived:
        try:
            state = collector.BacklogStore(collector.collector_checkpoint_root()).read_existing(identity, slices)
        except (OSError, ValueError, collector.BacklogError) as exc:
            raise SourceDebtRecoveryError("parent backlog receipt is invalid") from exc
        receipt = next((item for item in state.completed if item.slice_id == slice_.slice_id), None)
        bundle_path = (parent / "completion-bundle.json").resolve()
        bundle_file_digest = _file_digest(bundle_path, label="parent completion bundle")
        if receipt is None or receipt.result_path != bundle_path or receipt.result_digest != bundle_file_digest:
            raise SourceDebtRecoveryError("parent backlog receipt does not match the completion bundle")

    mode = report.get("collection_mode")
    if not isinstance(mode, Mapping):
        raise SourceDebtRecoveryError("parent collection mode is invalid")
    coordinator = mode.get("coordinator")
    calendly_optional = mode.get("calendly_optional")
    if not isinstance(coordinator, str) or not isinstance(calendly_optional, bool):
        raise SourceDebtRecoveryError("parent collection mode is invalid")
    snapshots = {
        name: _file_digest(parent / name, label=f"parent {name}")
        for name in RECONCILIATION_SNAPSHOTS
    }
    routing_path = ROOT / "routing.json"
    if _file_digest(routing_path, label="current routing") != snapshots["routing.json"]:
        raise SourceDebtRecoveryError("current routing differs from the parent snapshot")
    try:
        routing = json.loads(routing_path.read_text(encoding="utf-8"))
        fleet_path = ROOT / "fleet.json"
        fleet = json.loads(fleet_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceDebtRecoveryError("current collector configuration is invalid") from exc
    if not isinstance(routing, dict) or not isinstance(fleet, dict):
        raise SourceDebtRecoveryError("current collector configuration must be objects")
    current = collector._backlog_compatibility_version(
        routing, fleet,
        calendly_optional=calendly_optional,
        coordinator=current_coordinator,
    )
    if current != identity.compatibility_version:
        raise SourceDebtRecoveryError("current collector compatibility differs from the parent")
    return _Parent(
        parent, report, bundle, identity, slice_, source, snapshots,
        routing, fleet, current_coordinator, calendly_optional,
    )


def _transition(parent: _Parent, attempt_id: str) -> tuple[dict[str, object], str]:
    if not isinstance(attempt_id, str) or _ATTEMPT.fullmatch(attempt_id) is None:
        raise SourceDebtRecoveryError("recovery attempt ID must be sha256:<64 lowercase hex>")
    locator_payload = {
        "contract": "source-debt-recovery-attempt/v1",
        "source": parent.source,
        "since_utc": collector.iso_utc(parent.slice_.since),
        "until_utc": collector.iso_utc(parent.slice_.until),
        "slice_id": parent.slice_.slice_id,
        "parent_bundle_digest": parent.bundle.bundle_digest,
        "parent_backlog_compatibility_digest": _digest_bytes(parent.identity.compatibility_version.encode("utf-8")),
        "attempt_id": attempt_id,
    }
    locator = "source-debt-recovery-attempt/" + hashlib.sha256(_canonical(locator_payload)).hexdigest()
    runtime = collector.collector_runtime_identity()
    fleet_path = ROOT / "fleet.json"
    routing_path = ROOT / "routing.json"
    unsigned: dict[str, object] = {
        "schema_version": _SCHEMA,
        "source": parent.source,
        "since_utc": collector.iso_utc(parent.slice_.since),
        "until_utc": collector.iso_utc(parent.slice_.until),
        "slice_id": parent.slice_.slice_id,
        "parent_run_id": parent.run_dir.name,
        "parent_bundle_digest": parent.bundle.bundle_digest,
        "parent_backlog_compatibility_digest": locator_payload["parent_backlog_compatibility_digest"],
        "attempt_id": attempt_id,
        "attempt_locator": locator,
        "recovery_adapter_sha256": _file_digest(Path(__file__), label="recovery adapter"),
        "current_collector_sha256": "sha256:" + collector.collector_script_sha256(),
        "current_runtime_identity_digest": _digest(runtime),
        "current_routing_sha256": _file_digest(routing_path, label="current routing"),
        "current_fleet_sha256": _file_digest(fleet_path, label="current fleet"),
        "snapshot_digests": dict(parent.snapshots),
    }
    return {**unsigned, "transition_digest": _digest(unsigned)}, locator


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_immutable_json(path: Path, value: object) -> None:
    payload = _canonical(value) + b"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
    except FileExistsError:
        if path.is_symlink() or not path.is_file() or path.read_bytes() != payload:
            raise SourceDebtRecoveryError("existing recovery attempt binding differs")
        _fsync_directory(path.parent)
        return
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if not isinstance(written, int) or written <= 0:
                raise OSError("recovery attempt marker write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        try:
            path.unlink()
        except OSError:
            pass
        try:
            _fsync_directory(path.parent)
        except OSError:
            pass
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    else:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _existing_collection(run_dir: Path, parent: _Parent, transition: dict[str, object]) -> bool:
    report_path = run_dir / "run-report.json"
    if not report_path.exists():
        return False
    report = _read_object(report_path, label="existing recovery report")
    existing_transition = report.get("source_debt_recovery")
    if existing_transition is not None and existing_transition != transition:
        raise SourceDebtRecoveryError("existing recovery transition differs")
    reason = str(parent.report.get("date_range", {}).get("reason") or "")
    try:
        collector._verified_existing_slice_bundle(
            run_dir, parent.slice_.since, parent.slice_.until, reason,
            calendly_optional=parent.calendly_optional, coordinator=parent.coordinator,
        )
    except (OSError, ValueError, collector.BacklogError) as exc:
        raise SourceDebtRecoveryError("existing recovery collection is invalid") from exc
    if existing_transition is None:
        report["source_debt_recovery"] = transition
        collector.write_json(run_dir / "run-report.json", report)
    _ensure_finalization(run_dir, parent)
    return True


def _ensure_finalization(run_dir: Path, parent: _Parent) -> None:
    path = run_dir / "slice-finalization.json"
    expected = collector._safe_slice_finalization_document(parent.identity, parent.slice_)
    if path.exists():
        if _read_object(path, label="recovery slice finalization") != expected:
            raise SourceDebtRecoveryError("recovery slice finalization differs")
        return
    collector._write_pending_slice_finalization(run_dir, parent.identity, parent.slice_)


def _preserve_partial(run_dir: Path) -> None:
    if run_dir.is_symlink() or not run_dir.is_dir():
        raise SourceDebtRecoveryError("existing recovery partial is unsafe")
    inventory = {
        str(path.relative_to(run_dir)): _digest_bytes(path.read_bytes())
        for path in sorted(run_dir.rglob("*"))
        if path.is_file() and not path.is_symlink()
    }
    if any(path.is_symlink() for path in run_dir.rglob("*")):
        raise SourceDebtRecoveryError("existing recovery partial contains a symlink")
    inventory_digest = hashlib.sha256(_canonical(inventory)).hexdigest()[:16]
    target = run_dir.with_name(
        run_dir.name + "-incomplete-" + inventory_digest + "-" + secrets.token_hex(8)
    )
    while target.exists():
        target = run_dir.with_name(
            run_dir.name + "-incomplete-" + inventory_digest + "-" + secrets.token_hex(8)
        )
    os.replace(run_dir, target)
    _fsync_directory(run_dir.parent)


def recover(parent_run: Path, source: str, attempt_id: str) -> RecoveryResult:
    """Validate immutable parent lineage and collect its exact slice once per attempt."""
    if not isinstance(attempt_id, str) or _ATTEMPT.fullmatch(attempt_id) is None:
        raise SourceDebtRecoveryError("recovery attempt ID must be sha256:<64 lowercase hex>")
    parent = _validate_parent(Path(parent_run), source)
    transition, locator = _transition(parent, attempt_id)
    locator_digest = locator.rsplit("/", 1)[1]
    run_dir = RUNS.resolve() / f"source-debt-recovery-{locator_digest}"
    attempt_root = collector.collector_checkpoint_root() / "source-debt-recovery" / locator_digest
    marker = {
        "schema_version": "source-debt-recovery-attempt-marker/v1",
        "run_dir": str(run_dir),
        "checkpoint_dir": str(attempt_root / "source-checkpoints"),
        "transition": transition,
    }
    _write_immutable_json(attempt_root / "attempt-marker.json", marker)
    if run_dir.exists():
        if _existing_collection(run_dir, parent, transition):
            return RecoveryResult(run_dir, parent.run_dir, attempt_root / "source-checkpoints", transition, True)
        _preserve_partial(run_dir)

    cenv = collector.load_env_file(
        collector.clockify_env_candidates(), ["CLOCKIFY_API_KEY", "CLOCKIFY_WORKSPACE_ID"]
    )
    fenv = collector.load_env_file(collector.fathom_env_candidates(), ["FATHOM_API_KEY"])
    calendly_env = collector.load_env_file(
        collector.calendly_env_candidates(),
        ["CALENDLY_RECORDINGS_URL", "CALENDLY_GATEWAY_TOKEN", "CALENDLY_GATEWAY_READ_ONLY"],
    )
    args = argparse.Namespace(enrich=False, calendly_optional=parent.calendly_optional)
    reason = str(parent.report.get("date_range", {}).get("reason") or "")
    try:
        collector._collect_slice(
            args, parent.routing, parent.fleet, cenv, fenv,
            parent.slice_.since, parent.slice_.until, reason,
            collector.PageCheckpointStore(attempt_root / "source-checkpoints"),
            run_dir, calendly_env=calendly_env, coordinator=parent.coordinator,
        )
        report = _read_object(run_dir / "run-report.json", label="recovery run report")
        report["source_debt_recovery"] = transition
        collector.write_json(run_dir / "run-report.json", report)
        _ensure_finalization(run_dir, parent)
    except (OSError, ValueError, collector.BacklogError, collector.CheckpointError) as exc:
        raise SourceDebtRecoveryError("source-debt collection did not complete safely") from exc
    return RecoveryResult(run_dir, parent.run_dir, attempt_root / "source-checkpoints", transition, False)


def verify_recovery_run(
    run_path: Path, *, _visited: frozenset[Path] | None = None,
) -> RecoveryResult:
    """Rebuild and verify a derived attempt identity without collecting."""
    run_dir = _direct_run(Path(run_path), label="recovery run")
    visited = frozenset() if _visited is None else _visited
    if run_dir in visited:
        raise SourceDebtRecoveryError("recovery ancestry cycle detected")
    visited = visited | {run_dir}
    report = _read_object(run_dir / "run-report.json", label="recovery run report")
    transition = report.get("source_debt_recovery")
    if not isinstance(transition, dict) or transition.get("schema_version") != _SCHEMA:
        raise SourceDebtRecoveryError("recovery transition is missing or invalid")
    parent_id = transition.get("parent_run_id")
    source = transition.get("source")
    attempt_id = transition.get("attempt_id")
    if not all(isinstance(value, str) for value in (parent_id, source, attempt_id)):
        raise SourceDebtRecoveryError("recovery transition identity is invalid")
    try:
        parent = _validate_parent(RUNS / parent_id, source, _visited=visited)
    except RecursionError as exc:
        raise SourceDebtRecoveryError(
            "recovery ancestry exceeds safe verification depth"
        ) from exc
    expected, locator = _transition(parent, attempt_id)
    if transition != expected:
        raise SourceDebtRecoveryError("recovery transition binding differs")
    locator_digest = locator.rsplit("/", 1)[1]
    expected_run = RUNS.resolve() / f"source-debt-recovery-{locator_digest}"
    attempt_root = collector.collector_checkpoint_root() / "source-debt-recovery" / locator_digest
    if run_dir != expected_run:
        raise SourceDebtRecoveryError("recovery run path does not match its stable locator")
    marker = _read_object(attempt_root / "attempt-marker.json", label="recovery attempt marker")
    expected_marker = {
        "schema_version": "source-debt-recovery-attempt-marker/v1",
        "run_dir": str(run_dir),
        "checkpoint_dir": str(attempt_root / "source-checkpoints"),
        "transition": expected,
    }
    if marker != expected_marker:
        raise SourceDebtRecoveryError("recovery attempt marker differs")
    finalization = _read_object(run_dir / "slice-finalization.json", label="recovery slice finalization")
    if finalization != collector._safe_slice_finalization_document(parent.identity, parent.slice_):
        raise SourceDebtRecoveryError("recovery slice finalization differs")
    return RecoveryResult(
        run_dir, parent.run_dir, attempt_root / "source-checkpoints", expected, True
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recover-source-debt-from", type=Path, required=True)
    parser.add_argument("--recover-source", required=True)
    parser.add_argument("--recover-attempt-id", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = recover(
            args.recover_source_debt_from, args.recover_source, args.recover_attempt_id
        )
    except SourceDebtRecoveryError as exc:
        print(f"clockify source debt recovery: {exc}", file=sys.stderr)
        return 2
    print(result.run_dir / "run-report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
