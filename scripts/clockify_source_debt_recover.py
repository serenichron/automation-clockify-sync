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
import stat
import subprocess
import sys
from typing import Any, Mapping

try:
    from scripts import clockify_sync_collect as collector
    from scripts import collector_receipts
    from scripts import evidence_ledger
    from scripts import source_coverage
except ModuleNotFoundError:  # direct script execution
    import clockify_sync_collect as collector  # type: ignore[no-redef]
    import collector_receipts  # type: ignore[no-redef]
    import evidence_ledger  # type: ignore[no-redef]
    import source_coverage  # type: ignore[no-redef]


ROOT = Path(__file__).resolve().parents[1]
RUNS = ROOT / "runs"
_SCHEMA = "source-debt-recovery/v1"
_RECEIPT_SCHEMA = "source-debt-recovery-receipt/v1"
_ATTEMPT = re.compile(r"sha256:[0-9a-f]{64}")
RECONCILIATION_SNAPSHOTS = (
    "period-manifest.json",
    "routing.json",
    "review-corrections.jsonl",
    "review-acceptance.jsonl",
)
_EVIDENCE_FILES = {
    "clockify": "evidence/clockify-existing.json",
    "fathom": "evidence/fathom-meetings.json",
    "calendly": "evidence/calendly-recordings.json",
    "multica_issues": "evidence/multica-issues.json",
    "sessions": "evidence/sessions.json",
}
_PEER_EVENT_TYPES = frozenset({
    "claude_bursts", "hermes_sessions", "hermes_db_sessions",
    "codex_sessions", "repository_events",
})


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


def _verified_parent_ledger(parent: Path) -> evidence_ledger.EvidenceLedger:
    document = _read_object(
        parent / "evidence" / "evidence-ledger.json", label="parent evidence ledger"
    )
    manifest = document.get("manifest")
    events = document.get("events")
    if not isinstance(manifest, Mapping) or not isinstance(events, list):
        raise SourceDebtRecoveryError("parent evidence ledger is invalid")
    try:
        parsed_manifest = evidence_ledger.LedgerManifest.from_document(manifest)
        ledger = evidence_ledger.EvidenceLedger(
            tuple(evidence_ledger.EvidenceEvent.from_document(item) for item in events),
            parsed_manifest.source_inventory,
            parsed_manifest.timezone,
            parsed_manifest.member_identities,
        )
        ledger.validate(parsed_manifest)
    except (TypeError, ValueError) as exc:
        raise SourceDebtRecoveryError("parent evidence ledger is invalid") from exc
    return ledger


def _verify_parent_raw_matches_bound_ledger(
    parent: Path, bound: evidence_ledger.EvidenceLedger,
) -> None:
    _verified_parent_evidence(parent, bound)


def _verified_parent_evidence(
    parent: Path, bound: evidence_ledger.EvidenceLedger,
) -> dict[str, Any]:
    raw, _digests = _parent_evidence(
        type("RawParent", (), {"run_dir": parent})()  # read-only path adapter
    )
    snapshot = {
        "clockify": raw["clockify"], "fathom": raw["fathom"],
        "calendly": raw["calendly"], "multica_issues": raw["multica_issues"],
        "sessions": raw["sessions"],
    }
    try:
        reconstructed = evidence_ledger.EvidenceLedger(
            tuple(evidence_ledger.normalize_collector_snapshot(snapshot)),
            evidence_ledger.source_inventory_from_collector(snapshot),
            bound.timezone,
            bound.member_identities,
        )
    except (TypeError, ValueError) as exc:
        raise SourceDebtRecoveryError("parent raw evidence is invalid") from exc
    if reconstructed.manifest.document() != bound.manifest.document():
        raise SourceDebtRecoveryError("parent raw evidence does not match bound ledger")
    return raw


def _rebuild_peer_ledger(
    parent: _Parent, recovered: Mapping[str, Any], machine_name: str,
) -> tuple[evidence_ledger.EvidenceLedger, dict[str, Any]]:
    bound = _verified_parent_ledger(parent.run_dir)
    raw = _verified_parent_evidence(parent.run_dir, bound)
    retained: list[evidence_ledger.EvidenceEvent] = []
    for event in bound.events:
        event_machine = event.source_ref.get("machine")
        peer_event = (
            event.source_type in _PEER_EVENT_TYPES
            or event.source_type.endswith("_event")
        )
        if peer_event and not isinstance(event_machine, str):
            raise SourceDebtRecoveryError("bound peer event attribution is incomplete")
        if event_machine != machine_name:
            retained.append(event)
    snapshot = {"sessions": [dict(recovered)]}
    try:
        replacement = evidence_ledger.normalize_collector_snapshot(snapshot)
        peer_inventory = evidence_ledger.source_inventory_from_collector(snapshot)
        inventory = {
            source: dict(details)
            for source, details in bound.source_inventory.items()
            if source not in {
                f"sessions/{machine_name}", f"repositories/{machine_name}"
            }
        }
        for source in (f"sessions/{machine_name}", f"repositories/{machine_name}"):
            details = peer_inventory.get(source)
            if not isinstance(details, Mapping):
                raise ValueError("recovered peer inventory is incomplete")
            inventory[source] = dict(details)
        return evidence_ledger.EvidenceLedger(
            tuple([*retained, *replacement]), inventory,
            bound.timezone, bound.member_identities,
        ), raw
    except (TypeError, ValueError) as exc:
        raise SourceDebtRecoveryError("recovered peer ledger is invalid") from exc


def _evidence_for_rebuilt_ledger(
    ledger: evidence_ledger.EvidenceLedger, parent_evidence: Mapping[str, Any],
    recovered: Mapping[str, Any], machine_name: str,
) -> dict[str, Any]:
    sessions = parent_evidence.get("sessions")
    if not isinstance(sessions, list):
        raise SourceDebtRecoveryError("parent session evidence is invalid")
    materialized = {
        name: json.loads(json.dumps(parent_evidence[name]))
        for name in ("clockify", "fathom", "calendly", "multica_issues")
    }
    materialized["sessions"] = [
        dict(recovered) if item.get("machine") == machine_name else dict(item)
        for item in sessions
    ]
    try:
        reconstructed = evidence_ledger.EvidenceLedger(
            tuple(evidence_ledger.normalize_collector_snapshot(materialized)),
            evidence_ledger.source_inventory_from_collector(materialized),
            ledger.timezone,
            ledger.member_identities,
        )
    except (TypeError, ValueError) as exc:
        raise SourceDebtRecoveryError("rebuilt raw evidence is invalid") from exc
    if reconstructed.manifest.document() != ledger.manifest.document():
        raise SourceDebtRecoveryError("rebuilt raw evidence does not match bound ledger")
    return materialized


def _ledger_recovery_document(
    parent: _Parent, transition: Mapping[str, object],
    ledger: evidence_ledger.EvidenceLedger,
) -> dict[str, object]:
    parent_path = parent.run_dir / "evidence" / "evidence-ledger.json"
    unsigned: dict[str, object] = {
        "schema_version": "source-recovery-ledger/v1",
        "parent_run_id": parent.run_dir.name,
        "parent_bundle_digest": parent.bundle.bundle_digest,
        "parent_ledger_digest": _file_digest(parent_path, label="parent evidence ledger"),
        "requested_source": parent.source,
        "transition_digest": transition["transition_digest"],
        "derived_manifest_id": ledger.manifest.manifest_id,
    }
    return {**unsigned, "lineage_digest": _digest(unsigned)}


def _parent_evidence(parent: _Parent) -> tuple[dict[str, Any], dict[str, str]]:
    evidence: dict[str, Any] = {}
    digests: dict[str, str] = {}
    for key, relative in _EVIDENCE_FILES.items():
        path = parent.run_dir / relative
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SourceDebtRecoveryError("parent evidence artifact is invalid") from exc
        if key == "sessions":
            if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
                raise SourceDebtRecoveryError("parent session evidence is invalid")
        elif not isinstance(value, dict):
            raise SourceDebtRecoveryError("parent evidence artifact is invalid")
        evidence[key] = value
        digests[relative] = _file_digest(path, label=f"parent {relative}")
    return evidence, digests


def _adoption_document(
    parent: _Parent, transition: Mapping[str, object], adopted: Mapping[str, str],
) -> dict[str, object]:
    unsigned: dict[str, object] = {
        "schema_version": "source-recovery-adoption/v1",
        "parent_run_id": parent.run_dir.name,
        "parent_bundle_digest": parent.bundle.bundle_digest,
        "requested_source": parent.source,
        "transition_digest": transition["transition_digest"],
        "adopted_artifacts": dict(sorted(adopted.items())),
        "snapshot_digests": dict(parent.snapshots),
    }
    return {**unsigned, "attestation_digest": _digest(unsigned)}


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


def _legacy_parent_compatibility_matches(
    compatibility_version: str, *, routing: Mapping[str, Any], fleet: Mapping[str, Any],
    coverage_sources: Mapping[str, Any], coordinator: str,
    current_coordinator: str, calendly_optional: bool,
    runtime_identity: object,
) -> bool:
    """Validate the evidence-facing portion of the release-bound bab6 identity."""
    if (
        re.fullmatch(r"collector-slice-bundles/v1:[0-9a-f]{64}", compatibility_version)
        is None
        or coordinator != current_coordinator
    ):
        return False
    machines = fleet.get("machines")
    options = fleet.get("ssh_options")
    if not isinstance(machines, list) or not isinstance(options, list) or not all(
        isinstance(option, str) for option in options
    ):
        return False
    enabled: set[str] = set()
    for machine in machines:
        if not isinstance(machine, Mapping) or not isinstance(
            machine.get("enabled", True), bool
        ):
            return False
        name = machine.get("name")
        if not isinstance(name, str) or not collector._machine_name_is_valid(name):
            return False
        if machine.get("enabled", True):
            if name in enabled:
                return False
            enabled.add(name)
    observed: dict[str, set[str]] = {"sessions": set(), "repositories": set()}
    for source_name in coverage_sources:
        category, separator, machine = str(source_name).partition("/")
        if separator == "/" and category in observed and machine:
            observed[category].add(machine)
    if enabled != observed["sessions"] or enabled != observed["repositories"]:
        return False
    if (
        not isinstance(runtime_identity, Mapping)
        or runtime_identity.get("git_dirty") is not False
        or not isinstance(runtime_identity.get("git_sha"), str)
        or re.fullmatch(r"[0-9a-f]{40}", str(runtime_identity["git_sha"])) is None
    ):
        return False
    repository = Path(collector.__file__).resolve().parents[1]
    try:
        historical = subprocess.run(
            [
                "git", "-C", str(repository), "show",
                f"{runtime_identity['git_sha']}:scripts/clockify_sync_collect.py",
            ],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    if historical.returncode != 0 or not historical.stdout:
        return False
    payload = {
        "contract": "collector-slice-bundles/v1",
        "collector_sha256": hashlib.sha256(historical.stdout).hexdigest(),
        "routing": routing,
        "fleet": fleet,
        "calendly_optional": calendly_optional,
        "coordinator": coordinator,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    expected = "collector-slice-bundles/v1:" + hashlib.sha256(encoded).hexdigest()
    return compatibility_version == expected


@dataclass(frozen=True)
class RecoveryResult:
    run_dir: Path
    parent_run_dir: Path
    checkpoint_dir: Path
    transition: dict[str, object]
    collection_reused: bool


@dataclass(frozen=True)
class RecoveryReceipt:
    path: Path
    digest: str
    document: dict[str, Any]


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
    category, separator, machine_name = source.partition("/") if isinstance(source, str) else ("", "", "")
    if (
        category != "peer" or separator != "/"
        or not collector._machine_name_is_valid(machine_name)
        or machine_name == current_coordinator
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
                f"derived recovery parent terminal verification failed: {exc}"
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
    bound_ledger = _verified_parent_ledger(parent)
    _verify_parent_raw_matches_bound_ledger(parent, bound_ledger)
    incomplete = coverage.get("incomplete_sources")
    sources = coverage.get("sources")
    peer_facets = (f"sessions/{machine_name}", f"repositories/{machine_name}")
    source_records = (
        [sources.get(name) for name in peer_facets]
        if isinstance(sources, Mapping) else []
    )
    if (
        not isinstance(incomplete, list)
        or not any(name in incomplete for name in peer_facets)
        or not all(isinstance(record, Mapping) for record in source_records)
        or all(record.get("status") in {"complete", "excluded"} for record in source_records)
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
    if current != identity.compatibility_version and not _legacy_parent_compatibility_matches(
        identity.compatibility_version,
        routing=routing,
        fleet=fleet,
        coverage_sources=sources,
        coordinator=coordinator,
        current_coordinator=current_coordinator,
        calendly_optional=calendly_optional,
        runtime_identity=report.get("runtime_identity"),
    ):
        raise SourceDebtRecoveryError(
            "current fleet or evidence compatibility differs from the parent"
        )
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
    ledger = _verified_parent_ledger(run_dir)
    _write_immutable_json(
        run_dir / "ledger-recovery.json",
        _ledger_recovery_document(parent, transition, ledger),
    )
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
            existing = RecoveryResult(
                run_dir, parent.run_dir, attempt_root / "source-checkpoints",
                transition, True,
            )
            if not (run_dir / "autopilot-result.json").exists():
                return existing
            try:
                verify_recovery_receipt(run_dir, verified=existing)
            except SourceDebtRecoveryError as exc:
                if "external recovery receipt is missing" not in str(exc):
                    raise
                _preserve_partial(run_dir)
            else:
                return existing
        else:
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
        run_dir.mkdir(parents=False)
        runtime = collector.collector_runtime_identity()
        machine_name = source.split("/", 1)[1]
        machine = next(
            (
                item for item in parent.fleet.get("machines", [])
                if isinstance(item, Mapping) and item.get("name") == machine_name
                and item.get("enabled", True)
            ),
            None,
        )
        if machine is None:
            raise SourceDebtRecoveryError("requested peer is absent from current fleet")
        if collector.machine_is_local(dict(machine)):
            recovered = collector.collect_local_sessions(
                dict(machine), parent.slice_.since, parent.slice_.until
            )
        elif machine.get("kind") in {"ssh", "auto"}:
            recovered = collector.collect_remote_sessions(
                dict(machine), parent.slice_.since, parent.slice_.until,
                parent.fleet.get("ssh_options", []), coordinator_identity=dict(runtime),
            )
        else:
            raise SourceDebtRecoveryError("requested peer transport is unsupported")
        if not isinstance(recovered, Mapping) or recovered.get("machine") != machine_name:
            raise SourceDebtRecoveryError("requested peer returned an invalid identity")
        ledger, parent_evidence = _rebuild_peer_ledger(
            parent, recovered, machine_name
        )
        collector._collect_slice(
            args, parent.routing, parent.fleet, cenv, fenv,
            parent.slice_.since, parent.slice_.until, reason,
            collector.PageCheckpointStore(attempt_root / "source-checkpoints"),
            run_dir, calendly_env=calendly_env, coordinator=parent.coordinator,
            evidence_override=_evidence_for_rebuilt_ledger(
                ledger, parent_evidence, recovered, machine_name
            ),
            ledger_override=ledger,
            preclaimed_run_dir=True,
        )
        report = _read_object(run_dir / "run-report.json", label="recovery run report")
        report["source_debt_recovery"] = transition
        collector.write_json(run_dir / "run-report.json", report)
        _write_immutable_json(
            run_dir / "ledger-recovery.json",
            _ledger_recovery_document(parent, transition, ledger),
        )
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
    ledger = _verified_parent_ledger(run_dir)
    lineage = _read_object(run_dir / "ledger-recovery.json", label="ledger recovery")
    if lineage != _ledger_recovery_document(parent, expected, ledger):
        raise SourceDebtRecoveryError("ledger recovery lineage differs")
    finalization = _read_object(run_dir / "slice-finalization.json", label="recovery slice finalization")
    if finalization != collector._safe_slice_finalization_document(parent.identity, parent.slice_):
        raise SourceDebtRecoveryError("recovery slice finalization differs")
    return RecoveryResult(
        run_dir, parent.run_dir, attempt_root / "source-checkpoints", expected, True
    )


def _recovery_receipt_root(*, create: bool) -> Path:
    base = collector.collector_checkpoint_root()
    if not base.is_absolute() or base != base.resolve():
        raise SourceDebtRecoveryError("external recovery receipt root is not canonical")
    if base.is_symlink() or not base.is_dir() or base.stat().st_uid != os.getuid():
        raise SourceDebtRecoveryError("external recovery receipt root parent is unsafe")
    root = base / "source-debt-recovery-receipts"
    if create and not root.exists():
        try:
            root.mkdir(mode=0o700)
        except FileExistsError:
            pass
    if not root.exists():
        raise SourceDebtRecoveryError("external recovery receipt is missing")
    if (
        root.is_symlink() or not root.is_dir()
        or root.stat().st_uid != os.getuid()
        or stat.S_IMODE(root.stat().st_mode) != 0o700
    ):
        raise SourceDebtRecoveryError("external recovery receipt root is unsafe")
    return root


def _recovery_receipt_path(
    verified: RecoveryResult, *, create_root: bool,
) -> Path:
    locator = verified.transition.get("attempt_locator")
    if not isinstance(locator, str) or not locator.startswith(
        "source-debt-recovery-attempt/"
    ):
        raise SourceDebtRecoveryError("external recovery receipt locator is invalid")
    digest = locator.rsplit("/", 1)[1]
    if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        raise SourceDebtRecoveryError("external recovery receipt locator is invalid")
    return _recovery_receipt_root(create=create_root) / f"{digest}.json"


def _recovery_source_status(
    bundle: collector_receipts.SliceCompletionBundle, source: str,
) -> str:
    coverage = collector_receipts.completion_coverage(bundle)
    incomplete = coverage.get("incomplete_sources")
    sources = coverage.get("sources")
    if not isinstance(incomplete, list) or not isinstance(sources, dict):
        raise SourceDebtRecoveryError(
            "external recovery receipt has no requested-source identity"
        )
    if source.startswith("peer/"):
        machine = source.split("/", 1)[1]
        names = (f"sessions/{machine}", f"repositories/{machine}")
    else:
        names = (source,)
    records = [sources.get(name) for name in names]
    if not all(isinstance(record, dict) for record in records):
        raise SourceDebtRecoveryError(
            "external recovery receipt has no requested-source identity"
        )
    if any(record.get("status") == "excluded" for record in records):
        raise SourceDebtRecoveryError(
            "external recovery receipt requested source is excluded"
        )
    if all(
        name not in incomplete and record.get("status") == "complete"
        for name, record in zip(names, records)
    ):
        return "complete"
    if any(
        name in incomplete and record.get("status") != "complete"
        for name, record in zip(names, records)
    ):
        return "incomplete"
    raise SourceDebtRecoveryError(
        "external recovery receipt requested-source coverage is contradictory"
    )


def _recovery_receipt_document(
    verified: RecoveryResult, path: Path,
) -> dict[str, Any]:
    run_dir = verified.run_dir
    source = str(verified.transition["source"])
    parent = _validate_parent(verified.parent_run_dir, source)
    try:
        bundle = collector_receipts.load_completion_bundle(
            run_dir / "completion-bundle.json", run_dir=run_dir
        )
    except collector_receipts.CollectorReceiptError as exc:
        raise SourceDebtRecoveryError(
            "external recovery receipt completion bundle is invalid"
        ) from exc
    report = _read_object(run_dir / "run-report.json", label="recovery run report")
    result = _read_object(
        run_dir / "autopilot-result.json", label="recovery terminal result"
    )
    ledger_path = run_dir / "evidence" / "evidence-ledger.json"
    ledger = _verified_parent_ledger(run_dir)
    runtime = report.get("runtime_identity")
    current_runtime = collector.collector_runtime_identity()
    if (
        not isinstance(runtime, Mapping)
        or dict(runtime) != current_runtime
        or _digest(dict(runtime)) != verified.transition.get(
            "current_runtime_identity_digest"
        )
        or bundle.runtime_identity_digest != _digest(dict(runtime))
    ):
        raise SourceDebtRecoveryError(
            "external recovery receipt runtime identity differs"
        )
    expected_result = {
        "source": source,
        "attempt_id": verified.transition["attempt_id"],
        "status": _recovery_source_status(bundle, source),
        "transition_digest": verified.transition["transition_digest"],
    }
    result_identity = result.get("source_debt_recovery")
    if (
        result.get("quality_status") != "pass"
        or result.get("completion_bundle_digest") != bundle.bundle_digest
        or not isinstance(result_identity, Mapping)
        or {key: result_identity.get(key) for key in expected_result} != expected_result
    ):
        raise SourceDebtRecoveryError(
            "external recovery receipt terminal result differs"
        )
    interval = source_coverage.SourceInterval(
        source=source,
        since_utc=bundle.since_utc,
        until_utc=bundle.until_utc,
        slice_id=bundle.slice_id,
        compatibility_version=parent.identity.compatibility_version,
    )
    raw_providers = {
        name: {
            "path": str((run_dir / relative).resolve()),
            "sha256": _file_digest(
                run_dir / relative, label=f"derived raw provider {name}"
            ),
        }
        for name, relative in sorted(_EVIDENCE_FILES.items())
    }
    unsigned: dict[str, Any] = {
        "schema_version": _RECEIPT_SCHEMA,
        "trust_boundary": "run-only rewrite and cross-user mutation",
        "receipt_path": str(path),
        "derived_run_id": run_dir.name,
        "derived_run_path": str(run_dir),
        "transition": {
            "schema_version": verified.transition["schema_version"],
            "transition_digest": verified.transition["transition_digest"],
            "debt_id": interval.debt_id,
            "source": source,
            "attempt_id": verified.transition["attempt_id"],
            "attempt_locator": verified.transition["attempt_locator"],
        },
        "parent": {
            "run_id": parent.run_dir.name,
            "bundle_digest": parent.bundle.bundle_digest,
            "bundle_file_sha256": _file_digest(
                parent.run_dir / "completion-bundle.json",
                label="recovery parent completion bundle",
            ),
            "ledger_sha256": _file_digest(
                parent.run_dir / "evidence" / "evidence-ledger.json",
                label="recovery parent evidence ledger",
            ),
        },
        "runtime_identity": dict(runtime),
        "runtime_identity_digest": bundle.runtime_identity_digest,
        "compatibility_version": parent.identity.compatibility_version,
        "artifacts": {
            "raw_providers": raw_providers,
            "ledger": {
                "path": str(ledger_path.resolve()),
                "sha256": _file_digest(ledger_path, label="derived evidence ledger"),
                "manifest_id": ledger.manifest.manifest_id,
            },
            "ledger_recovery_sha256": _file_digest(
                run_dir / "ledger-recovery.json", label="derived ledger recovery"
            ),
            "run_report_sha256": _file_digest(
                run_dir / "run-report.json", label="derived run report"
            ),
            "result_sha256": _file_digest(
                run_dir / "autopilot-result.json", label="derived terminal result"
            ),
            "completion_bundle_sha256": _file_digest(
                run_dir / "completion-bundle.json", label="derived completion bundle"
            ),
            "completion_bundle_digest": bundle.bundle_digest,
        },
    }
    return {**unsigned, "receipt_digest": _digest(unsigned)}


def _read_secure_receipt(path: Path) -> dict[str, Any]:
    try:
        details = path.lstat()
    except FileNotFoundError as exc:
        raise SourceDebtRecoveryError("external recovery receipt is missing") from exc
    if (
        not stat.S_ISREG(details.st_mode)
        or details.st_uid != os.getuid()
        or stat.S_IMODE(details.st_mode) != 0o444
    ):
        raise SourceDebtRecoveryError("external recovery receipt is unsafe")
    try:
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            payload = b""
            while True:
                block = os.read(descriptor, 65536)
                if not block:
                    break
                payload += block
        finally:
            os.close(descriptor)
        document = json.loads(payload)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SourceDebtRecoveryError("external recovery receipt is invalid") from exc
    if not isinstance(document, dict):
        raise SourceDebtRecoveryError("external recovery receipt is invalid")
    return document


def verify_recovery_receipt(
    run_dir: Path, *, verified: RecoveryResult | None = None,
) -> RecoveryReceipt:
    checked = verified or verify_recovery_run(run_dir)
    path = _recovery_receipt_path(checked, create_root=False)
    document = _read_secure_receipt(path)
    expected = _recovery_receipt_document(checked, path)
    if document != expected:
        raise SourceDebtRecoveryError("external recovery receipt binding differs")
    return RecoveryReceipt(path, str(document["receipt_digest"]), document)


def seal_recovery_receipt(run_dir: Path) -> RecoveryReceipt:
    verified = verify_recovery_run(run_dir)
    path = _recovery_receipt_path(verified, create_root=True)
    document = _recovery_receipt_document(verified, path)
    payload = _canonical(document) + b"\n"
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o444,
        )
    except FileExistsError:
        return verify_recovery_receipt(run_dir, verified=verified)
    try:
        os.fchmod(descriptor, 0o444)
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if not isinstance(written, int) or written <= 0:
                raise OSError("external recovery receipt write made no progress")
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        try:
            os.close(descriptor)
        finally:
            try:
                path.unlink()
            except OSError:
                pass
        raise
    else:
        os.close(descriptor)
    _fsync_directory(path.parent)
    return verify_recovery_receipt(run_dir, verified=verified)


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
