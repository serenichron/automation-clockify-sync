#!/usr/bin/env python3
"""Run the local Clockify review pipeline and emit a compact action contract.

This command performs external reads through the collector and writes only
local run artifacts and durable review state. It never mutates Clockify,
Google Sheets, Multica, schedules, or agent configuration.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import Any

try:
    from scripts import clockify_sync_collect, review_acceptance, semantic_analyzer
    from scripts import clockify_source_debt_recover
    from scripts import collector_receipts, reconciliation_manifest
except ModuleNotFoundError:  # direct script execution
    import clockify_sync_collect  # type: ignore[no-redef]
    import clockify_source_debt_recover  # type: ignore[no-redef]
    import review_acceptance  # type: ignore[no-redef]
    import semantic_analyzer  # type: ignore[no-redef]
    import collector_receipts  # type: ignore[no-redef]
    import reconciliation_manifest  # type: ignore[no-redef]


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
RUNS = ROOT / "runs"
DEFAULT_STATE = ROOT / "state" / "review-items.json"
DEFAULT_ROUTING = ROOT / "routing.json"
DEFAULT_CORRECTIONS = ROOT / "state" / "review-corrections.jsonl"
DEFAULT_ACCEPTANCE_LEDGER = ROOT / "state" / "review-acceptance.jsonl"
REVIEW_MODES = {"shadow_all", "exceptions_only"}
_RECONCILIATION_INPUTS = {
    "period_manifest": "period-manifest.json",
    "routing": "routing.json",
    "corrections": "review-corrections.jsonl",
    "acceptance": "review-acceptance.jsonl",
}
_CANONICAL_MEETING_RECONCILIATION = "fathom-reconciliation.json"
_COMPLETION_BUNDLE_SCHEMA = "collector-completion-bundle/v1"


class ReviewRunError(ValueError):
    """A replay cannot prove that its reconciliation inputs are identical."""


def _configure_runs_root(path: Path) -> Path:
    """Bind every local collector/recovery component to one safe run root."""
    global RUNS
    requested = Path(path).expanduser()
    if not requested.is_absolute():
        raise ValueError("runs root must be absolute")
    resolved = requested.resolve()
    if requested != resolved:
        raise ValueError("runs root must be canonical and contain no symlink components")
    if resolved.exists() and (not resolved.is_dir() or resolved.is_symlink()):
        raise ValueError("runs root must be a safe directory")
    RUNS = resolved
    clockify_sync_collect.RUNS = resolved
    clockify_source_debt_recover.RUNS = resolved
    return resolved


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_result(
    run_dir: Path,
    quality: dict[str, Any],
    snapshot: dict[str, Any] | None,
    *,
    review_mode: str = "shadow_all",
    acceptance_gate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if review_mode not in REVIEW_MODES:
        raise ValueError(f"unsupported review mode: {review_mode}")
    run_report: dict[str, Any] = {}
    accounting: dict[str, Any] = {}
    try:
        if (run_dir / "run-report.json").is_file():
            value = _read_json(run_dir / "run-report.json")
            run_report = value if isinstance(value, dict) else {}
        if (run_dir / "work-accounting-result.json").is_file():
            value = _read_json(run_dir / "work-accounting-result.json")
            accounting = value if isinstance(value, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        # Quality and durable state remain authoritative for action selection;
        # unreadable optional summary metadata is simply omitted here.
        run_report = {}
        accounting = {}
    categories = (snapshot or {}).get("categories", {})
    category_names = ("new", "changed", "carried_pending")
    categorized = [
        (name, item)
        for name in category_names
        for item in categories.get(name, [])
        if isinstance(item, dict)
    ]
    new_all = list(categories.get("new", []))
    changed_all = list(categories.get("changed", []))
    exception_rows = [
        {"category": name, **item}
        for name, item in categorized
        if str(item.get("disposition") or "") == "ambiguous"
    ]
    clean_rows = [
        {"category": name, **item}
        for name, item in categorized
        if str(item.get("disposition") or "") == "pending"
    ]
    clean_ids = sorted({str(item.get("id") or "") for item in clean_rows if item.get("id")})
    clean_members = sorted(
        (
            {
                "review_item_id": str(item.get("id") or ""),
                "revision": int(item.get("revision") or 0),
                "evidence_fingerprint": str(item.get("evidence_fingerprint") or ""),
            }
            for item in clean_rows
            if item.get("id")
        ),
        key=lambda item: item["review_item_id"],
    )
    exception_delta = [
        item for item in exception_rows if item["category"] in {"new", "changed"}
    ]
    clean_delta_count = sum(
        item["category"] in {"new", "changed"} for item in clean_rows
    )
    clean_batch_id = (
        "rbatch-" + hashlib.sha256(
            json.dumps(clean_members, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24]
        if clean_ids else None
    )
    if review_mode == "exceptions_only":
        new_items = [item for item in new_all if str(item.get("disposition") or "") == "ambiguous"]
        changed_items = [item for item in changed_all if str(item.get("disposition") or "") == "ambiguous"]
    else:
        new_items = new_all
        changed_items = changed_all
    warnings = list((snapshot or {}).get("coverage_warnings", []))
    quality_status = str(quality.get("status") or "blocked")

    if quality_status == "blocked":
        action = "blocked"
    elif warnings:
        action = "coverage_warning"
    elif review_mode == "exceptions_only" and exception_delta:
        action = "review_exceptions"
    elif review_mode == "exceptions_only" and clean_delta_count:
        action = "review_batch"
    elif new_items or changed_items:
        action = "review_delta"
    else:
        action = "no_comment"

    summary = (snapshot or {}).get("summary", {})
    return {
        "schema_version": 1,
        "run_id": run_dir.name,
        "run_dir": str(run_dir.resolve()),
        "action": action,
        "should_comment": action != "no_comment",
        "should_update_issue_description": False,
        "external_writes": False,
        "review_mode": review_mode,
        "acceptance_gate": acceptance_gate or {
            "exceptions_only_eligible": False,
            "status": "not_recorded",
        },
        "quality_status": quality_status,
        "quality_summary": quality.get("summary", {}),
        "date_range": run_report.get("date_range"),
        "source_completeness": (
            (run_report.get("evidence_ledger") or {}).get("source_completeness")
            if isinstance(run_report.get("evidence_ledger"), dict)
            else None
        ),
        "accounting_summary": {
            "proposals": len(accounting.get("proposals", [])),
            "exceptions": len(accounting.get("ambiguous", [])),
            "omissions": len(accounting.get("skipped", [])),
            "contested_time": len(
                [
                    value
                    for value in accounting.get("ambiguous", [])
                    if isinstance(value, dict)
                    and value.get("exception_kind") == "contested_time"
                ]
            ),
            "fathom_records": len(accounting.get("fathom_reconciliation", [])),
        },
        "review_summary": {
            "new": int(summary.get("new", len(new_items))),
            "changed": int(summary.get("changed", len(changed_items))),
            "carried_pending": int(summary.get("carried_pending", 0)),
            "resolved_disappeared": int(summary.get("resolved_disappeared", 0)),
        },
        "new": new_items,
        "changed": changed_items,
        "exceptions": exception_delta if review_mode == "exceptions_only" else [],
        "active_exception_count": len(exception_rows),
        "clean_batch": {
            "batch_id": clean_batch_id,
            "count": len(clean_ids),
            "review_item_ids": clean_ids,
            "members": clean_members,
            "new": sum(item["category"] == "new" for item in clean_rows),
            "changed": sum(item["category"] == "changed" for item in clean_rows),
            "carried_pending": sum(item["category"] == "carried_pending" for item in clean_rows),
        },
        "coverage_warnings": warnings,
        "paths": {
            "run_report": str((run_dir / "run-report.md").resolve()),
            "quality_report": str((run_dir / "quality_report.json").resolve()),
            "evidence_ledger": str((run_dir / "evidence" / "evidence-ledger.json").resolve()),
            "semantic_analysis": str((run_dir / "semantic-analysis.json").resolve()),
            "work_accounting_result": str((run_dir / "work-accounting-result.json").resolve()),
            "review_snapshot": (
                str((run_dir / "review-snapshot.json").resolve())
                if snapshot is not None
                else None
            ),
        },
    }


def write_summary(path: Path, result: dict[str, Any]) -> None:
    summary = result["review_summary"]
    lines = [
        f"# Clockify review action — {result['run_id']}",
        "",
        f"- Action: `{result['action']}`",
        f"- Review mode: `{result['review_mode']}`",
        f"- Exceptions-only eligible: `{str(bool(result['acceptance_gate'].get('exceptions_only_eligible'))).lower()}`",
        f"- Quality: `{result['quality_status']}`",
        (
            "- Delta: "
            f"{summary['new']} new, {summary['changed']} changed; "
            f"{summary['carried_pending']} carried pending"
        ),
        f"- Coverage warnings: {len(result['coverage_warnings'])}",
        (
            "- Clean batch: "
            f"{result['clean_batch']['count']} rows; "
            f"ID `{result['clean_batch']['batch_id'] or 'none'}`"
        ),
        (
            "- Accounting: "
            f"{result['accounting_summary']['proposals']} proposals, "
            f"{result['accounting_summary']['exceptions']} exceptions, "
            f"{result['accounting_summary']['contested_time']} contested"
        ),
        f"- Run report: `{result['paths']['run_report']}`",
        "",
    ]
    if result["review_mode"] == "exceptions_only":
        delta = [(str(item.get("category") or "exception"), item) for item in result["exceptions"]]
    else:
        delta = [("new", item) for item in result["new"]]
        delta.extend(("changed", item) for item in result["changed"])
    if delta:
        lines.extend(
            [
                "## Genuine exceptions" if result["review_mode"] == "exceptions_only" else "## Actionable delta",
                "",
                "| Kind | Review ID | Project | Description |",
                "|---|---|---|---|",
            ]
        )
        for kind, item in delta:
            description = str(item.get("description") or item.get("reason") or "")
            description = " ".join(description.split()).replace("|", "\\|")
            project = str(item.get("client_project") or "ambiguous").replace("|", "\\|")
            lines.append(
                f"| {kind} | {item.get('id', '')} | {project} | {description} |"
            )
        lines.append("")
    if result["coverage_warnings"]:
        lines.extend(["## Coverage warnings", ""])
        for warning in result["coverage_warnings"]:
            lines.append(
                f"- {warning.get('source', 'unknown')}: {warning.get('reason', '')}"
            )
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def write_current_review_csv(path: Path, snapshot: dict[str, Any]) -> None:
    """Write a stable-ID review export without changing any external Sheet."""
    categories = snapshot.get("categories", {})
    rows_by_id: dict[str, dict[str, Any]] = {}
    for name in ("new", "changed", "carried_pending"):
        for item in categories.get(name, []):
            rows_by_id[str(item.get("id") or "")] = item
    fields = [
        "Review ID",
        "Segments",
        "Start",
        "End",
        "Duration (min)",
        "Project",
        "Tags",
        "Source",
        "Confidence",
        "Description",
        "Disposition",
        "Revision",
        "Last Seen Run",
        "Reason",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for item_id, item in sorted(rows_by_id.items()):
            segments = item.get("allocation_segments")
            if not isinstance(segments, list):
                segments = []
            start = item.get("start")
            end = item.get("end")
            raw_time = str(item.get("time") or "")
            if not start or not end:
                if "–" in raw_time:
                    start, end = raw_time.split("–", 1)
                elif " - " in raw_time:
                    start, end = raw_time.split(" - ", 1)
            duration = item.get("duration_minutes")
            if not duration and start and end:
                try:
                    start_dt = dt.datetime.fromisoformat(str(start).replace("Z", "+00:00"))
                    end_dt = dt.datetime.fromisoformat(str(end).replace("Z", "+00:00"))
                    duration = max(1, int((end_dt - start_dt).total_seconds() / 60))
                except ValueError:
                    duration = None
            source = item.get("source")
            if isinstance(source, list):
                source = ", ".join(str(value) for value in source)
            tags = item.get("tag_names")
            if isinstance(tags, list):
                tags = ", ".join(str(value) for value in tags)
            writer.writerow(
                {
                    "Review ID": item_id,
                    "Segments": "; ".join(
                        f"{segment.get('start', '')} - {segment.get('end', '')}"
                        for segment in segments
                        if isinstance(segment, dict)
                    ),
                    "Start": start or raw_time,
                    "End": end,
                    "Duration (min)": duration,
                    "Project": item.get("client_project"),
                    "Tags": tags,
                    "Source": source,
                    "Confidence": item.get("confidence"),
                    "Description": item.get("description") or item.get("label"),
                    "Disposition": item.get("disposition"),
                    "Revision": item.get("revision"),
                    "Last Seen Run": item.get("last_seen_run"),
                    "Reason": item.get("reason"),
                }
            )


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def _collector_run_dirs(stdout: str) -> tuple[Path, ...]:
    reports: list[Path] = []
    for line in stdout.splitlines():
        raw = line.strip()
        if not raw.endswith("/run-report.md"):
            continue
        requested = Path(raw)
        if not requested.is_absolute():
            raise ValueError("Collector emitted a non-canonical run-report path")
        resolved = requested.resolve()
        if requested != resolved:
            raise ValueError("Collector emitted a non-canonical run-report path")
        reports.append(resolved)
    if not reports:
        raise ValueError("Collector did not emit a completed run-report.md path.")
    run_dirs: list[Path] = []
    seen: set[Path] = set()
    for report in reports:
        run_dir = report.parent
        report_json = run_dir / "run-report.json"
        ledger_json = run_dir / "evidence" / "evidence-ledger.json"
        if (
            report.name != "run-report.md"
            or run_dir.parent != RUNS.resolve()
            or not report.is_file()
            or not report_json.is_file()
            or not ledger_json.is_file()
        ):
            raise ValueError(f"Collector emitted an invalid run directory: {run_dir}")
        try:
            receipt = _read_json(report_json)
            ledger = _read_json(ledger_json)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Collector emitted an invalid run receipt: {run_dir}") from exc
        reported_ledger = receipt.get("evidence_ledger") if isinstance(receipt, dict) else None
        manifest = ledger.get("manifest") if isinstance(ledger, dict) else None
        reported_completeness = (
            reported_ledger.get("source_completeness")
            if isinstance(reported_ledger, dict)
            else None
        )
        ledger_completeness = (
            manifest.get("source_completeness") if isinstance(manifest, dict) else None
        )
        if (
            not isinstance(reported_completeness, dict)
            or not isinstance(ledger_completeness, dict)
            or reported_completeness != ledger_completeness
            or not clockify_sync_collect._slice_is_complete(receipt)
        ):
            raise ValueError(f"Collector emitted a run receipt that is not complete: {run_dir}")
        if run_dir in seen:
            raise ValueError(f"Collector emitted duplicate run directory: {run_dir}")
        seen.add(run_dir)
        run_dirs.append(run_dir)
    return tuple(run_dirs)


def _run_child(path: Path, *, label: str) -> Path:
    requested = Path(path)
    if not requested.is_absolute():
        raise ValueError(f"{label} must be an absolute canonical path")
    resolved = requested.resolve()
    if requested != resolved:
        raise ValueError(f"{label} must be canonical and contain no symlink components")
    if resolved.parent != RUNS.resolve() or not resolved.is_dir():
        raise ValueError(f"{label} must be a direct child of {RUNS.resolve()}: {resolved}")
    return resolved


def _finalize_backlog_completion(
    run_dir: Path, *, replay: bool = False
) -> collector_receipts.SliceCompletionBundle:
    """Finalize only the exact pending slice whose downstream artifacts verify."""
    run_dir = Path(run_dir).resolve()
    try:
        pending = _read_json(run_dir / "slice-finalization.json")
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("slice finalization metadata is missing or invalid") from exc
    if not isinstance(pending, dict) or set(pending) != {
        "schema_version", "backlog_identity", "slice_id", "since_utc", "until_utc",
    } or pending["schema_version"] != "collector-slice-finalization/v1":
        raise ValueError("slice finalization metadata schema is invalid")
    raw_identity = pending["backlog_identity"]
    if not isinstance(raw_identity, dict):
        raise ValueError("slice finalization backlog identity is invalid")
    try:
        identity = clockify_sync_collect.BacklogIdentity(**raw_identity)
        slices = clockify_sync_collect.plan_slices(
            dt.datetime.fromisoformat(identity.since_utc[:-1] + "+00:00"),
            dt.datetime.fromisoformat(identity.until_utc[:-1] + "+00:00"),
            zone=clockify_sync_collect.BUCHAREST,
            max_days=identity.max_days,
        )
    except (TypeError, ValueError, clockify_sync_collect.BacklogError) as exc:
        raise ValueError("slice finalization backlog identity is invalid") from exc
    slice_ = next((item for item in slices if item.slice_id == pending["slice_id"]), None)
    if slice_ is None or (
        clockify_sync_collect.iso_utc(slice_.since) != pending["since_utc"]
        or clockify_sync_collect.iso_utc(slice_.until) != pending["until_utc"]
    ):
        raise ValueError("slice finalization identity does not match backlog")
    bundle_path = run_dir / "completion-bundle.json"
    bundle = collector_receipts.build_completion_bundle(run_dir, slice_=slice_, replay=replay)
    if bundle_path.exists():
        existing = collector_receipts.load_completion_bundle(bundle_path, run_dir=run_dir)
        if existing.bundle_digest != bundle.bundle_digest:
            raise ValueError("existing completion bundle does not match verified artifacts")
    else:
        collector_receipts.write_completion_bundle(bundle_path, bundle)
    verified = collector_receipts.load_completion_bundle(bundle_path, run_dir=run_dir)
    store = clockify_sync_collect.BacklogStore(clockify_sync_collect.collector_checkpoint_root())
    state = store.open(identity, slices)
    bundle_digest = "sha256:" + hashlib.sha256(bundle_path.read_bytes()).hexdigest()
    store.record_complete(state, slice_.slice_id, bundle_path.resolve(), bundle_digest)
    return verified


def _ledger_identity(run_dir: Path) -> dict[str, str]:
    path = run_dir / "evidence" / "evidence-ledger.json"
    document = _read_json(path)
    if not isinstance(document, dict) or document.get("schema_version") != "evidence-ledger/v1":
        raise ValueError(f"invalid evidence ledger document: {path}")
    manifest = document.get("manifest")
    if not isinstance(manifest, dict):
        raise ValueError(f"evidence ledger manifest is missing: {path}")
    manifest_id = str(manifest.get("manifest_id") or "")
    events_digest = str(manifest.get("events_digest") or "")
    if not manifest_id.startswith("elm-") or len(events_digest) != 64:
        raise ValueError(f"evidence ledger identity is incomplete: {path}")
    return {
        "manifest_id": manifest_id,
        "events_digest": events_digest,
        "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _accounting_identity(run_dir: Path) -> dict[str, str]:
    """Return the exact completion-marker identity used by immutable replay.

    The accounting result is the deterministic allocation/reconciliation
    outcome, not merely a convenient summary.  A replay which changes it must
    fail even if the upstream ledger and cached model decisions agree.
    """
    path = run_dir / "work-accounting-result.json"
    document = _read_json(path)
    if not isinstance(document, dict) or document.get("schema_version") != 1:
        raise ValueError(f"invalid work accounting result: {path}")
    if document.get("allocation_mode") != "non_overlapping_v1":
        raise ValueError(f"work accounting result has invalid allocation mode: {path}")
    return {"file_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _file_sha256(path: Path, *, label: str) -> str:
    return "sha256:" + hashlib.sha256(
        _read_snapshot_source(Path(path), label=label)
    ).hexdigest()


def _reconciliation_binding(
    run_dir: Path,
    *,
    period_manifest: Path,
    routing: Path,
    corrections: Path,
    acceptance: Path,
    completion_source: Path | None = None,
) -> dict[str, str]:
    """Return only stable identities required to replay a reconciled period.

    The period manifest names every completed slice.  We validate its artifact
    references and each completion bundle before emitting digests, so neither
    raw artifact paths nor source evidence reach the replay receipt.
    """
    run_dir = Path(run_dir).resolve()
    if not run_dir.is_dir() or run_dir.is_symlink():
        raise ReviewRunError("reconciliation run is missing or unsafe")
    manifest_document, manifest, bundle_records, manifest_content = (
        _validated_period_manifest(Path(period_manifest), allow_collecting_bootstrap=True)
    )
    if not bundle_records:
        # The immutable input precedes accounting, so it cannot name its own
        # future completion bundle. Bind that verified output separately. A
        # distinct replay uses the completed SOURCE, never a fabricated bundle
        # or its own not-yet-completed output, and input snapshots stay intact.
        completed = _run_child(completion_source or run_dir, label="bootstrap completion source")
        try:
            bundle_path = completed / "completion-bundle.json"
            bundle = collector_receipts.load_completion_bundle(bundle_path, run_dir=completed)
        except (OSError, ValueError, collector_receipts.CollectorReceiptError) as exc:
            raise ReviewRunError("bootstrap source completion bundle is invalid") from exc
        period = manifest.identity.document()
        if bundle.replay or (bundle.since_utc, bundle.until_utc) != (
            period["since_utc"], period["until_utc"],
        ):
            raise ReviewRunError("bootstrap source completion does not match exact period")
        if completed != run_dir and (
            _ledger_identity(completed) != _ledger_identity(run_dir)
            or _accounting_identity(completed) != _accounting_identity(run_dir)
        ):
            raise ReviewRunError("bootstrap source completion does not match replay outputs")
        bundle_records = [{
            "slice_id": bundle.slice_id,
            "since_utc": bundle.since_utc,
            "until_utc": bundle.until_utc,
            "bundle_digest": bundle.bundle_digest,
            "artifact_sha256": _file_sha256(bundle_path, label="bootstrap completion bundle"),
        }]
    digests = {
        "period_manifest": "sha256:" + hashlib.sha256(manifest_content).hexdigest(),
        "routing": _file_sha256(Path(routing), label="reconciliation routing"),
        "corrections": _file_sha256(
            Path(corrections), label="reconciliation corrections"
        ),
        "acceptance": _file_sha256(
            Path(acceptance), label="reconciliation acceptance"
        ),
    }

    meeting_path = run_dir / _CANONICAL_MEETING_RECONCILIATION
    meeting_digest = _file_sha256(
        meeting_path, label="canonical meeting reconciliation"
    )
    try:
        meeting = _read_json(meeting_path)
    except (OSError, json.JSONDecodeError) as exc:
        raise ReviewRunError("canonical meeting reconciliation is invalid") from exc
    if not isinstance(meeting, list):
        raise ReviewRunError("canonical meeting reconciliation must be a list")

    bundle_digest = "sha256:" + hashlib.sha256(
        json.dumps(bundle_records, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "period_manifest_sha256": digests["period_manifest"],
        "period_id": manifest.identity.period_id,
        "period_revision": str(manifest.identity.revision),
        "period_events_digest": manifest.events_digest,
        "routing_sha256": digests["routing"],
        "corrections_sha256": digests["corrections"],
        "acceptance_sha256": digests["acceptance"],
        "canonical_meeting_reconciliation_sha256": meeting_digest,
        "slice_completion_bundle_count": str(len(bundle_records)),
        "slice_completion_bundles_sha256": bundle_digest,
    }


def _read_snapshot_source(path: Path, *, label: str) -> bytes:
    """Read one regular file through an owned descriptor without following links."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReviewRunError(f"{label} is missing or unsafe") from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ReviewRunError(f"{label} is missing or unsafe")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 65_536):
            chunks.append(chunk)
        after = os.fstat(descriptor)
        identity = lambda value: (
            value.st_dev, value.st_ino, value.st_size,
            value.st_mtime_ns, value.st_ctime_ns,
        )
        if identity(before) != identity(after):
            raise ReviewRunError(f"{label} changed while being snapshotted")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _validated_period_manifest(
    path: Path, *, allow_collecting_bootstrap: bool = False,
) -> tuple[
    dict[str, Any], reconciliation_manifest.ReconciliationManifest,
    list[dict[str, str]], bytes,
]:
    """Validate one exact manifest snapshot and every referenced artifact."""
    content = _read_snapshot_source(path, label="reconciliation period manifest")
    try:
        document = json.loads(content)
        manifest = reconciliation_manifest.ReconciliationManifest.from_document(document)
    except (
        UnicodeDecodeError, json.JSONDecodeError, reconciliation_manifest.ManifestError,
    ) as exc:
        raise ReviewRunError("reconciliation period manifest is invalid") from exc
    if not isinstance(document, dict):
        raise ReviewRunError("reconciliation period manifest is invalid")

    bundles: list[dict[str, str]] = []
    for reference in manifest.artifacts:
        artifact_path = Path(str(reference["path"]))
        if (
            _file_sha256(artifact_path, label="reconciliation manifest artifact")
            != reference["digest"]
        ):
            raise ReviewRunError(
                "reconciliation manifest artifact differs from its manifest"
            )
        if reference["schema_version"] != _COMPLETION_BUNDLE_SCHEMA:
            continue
        try:
            bundle = collector_receipts.load_completion_bundle(
                artifact_path, run_dir=artifact_path.parent,
            )
        except (OSError, ValueError, collector_receipts.CollectorReceiptError) as exc:
            raise ReviewRunError("reconciliation completion bundle is invalid") from exc
        bundles.append({
            "slice_id": bundle.slice_id,
            "since_utc": bundle.since_utc,
            "until_utc": bundle.until_utc,
            "bundle_digest": bundle.bundle_digest,
            "artifact_sha256": str(reference["digest"]),
        })
    collecting_bootstrap = (
        allow_collecting_bootstrap
        and manifest.state == "collecting"
        and manifest.event_count == 1
        and not manifest.artifacts
        and not manifest.blockers
    )
    if not bundles and not collecting_bootstrap:
        raise ReviewRunError("reconciliation period manifest has no completion bundles")
    if len({record["slice_id"] for record in bundles}) != len(bundles):
        raise ReviewRunError("reconciliation period manifest repeats a completion bundle slice")
    return document, manifest, bundles, content


def _write_snapshot(target: Path, content: bytes, *, label: str) -> None:
    """Create one durable snapshot, accepting only an identical retry."""
    if target.exists() or target.is_symlink():
        if _read_snapshot_source(target, label=label) != content:
            raise ReviewRunError(f"{label} snapshot differs")
        return
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        temporary = Path(temporary_name)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise ReviewRunError(f"{label} snapshot write was incomplete")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        try:
            os.link(temporary, target, follow_symlinks=False)
        except FileExistsError:
            if _read_snapshot_source(target, label=label) != content:
                raise ReviewRunError(f"{label} snapshot differs")
        directory = os.open(target.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except ReviewRunError:
        raise
    except OSError as exc:
        raise ReviewRunError(f"{label} snapshot write failed") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _reconciliation_input_sources(args: argparse.Namespace) -> dict[str, Path]:
    sources = {
        "period-manifest.json": getattr(args, "period_manifest", None),
        "routing.json": getattr(args, "routing", None),
        "review-corrections.jsonl": getattr(args, "corrections", None),
        "review-acceptance.jsonl": getattr(args, "acceptance_ledger", None),
    }
    missing = [filename for filename, source in sources.items() if source is None]
    if missing:
        raise ReviewRunError(f"normal reconciliation requires {missing[0]}")
    return {filename: Path(source) for filename, source in sources.items()}


def _snapshot_reconciliation_inputs(
    run_dir: Path,
    args: argparse.Namespace,
    *,
    contents: dict[str, bytes] | None = None,
) -> dict[str, Path]:
    """Persist the exact normal-run inputs that a later replay must compare."""
    run_dir = _run_child(Path(run_dir), label="reconciliation run")
    sources = _reconciliation_input_sources(args)
    exact = contents or {
        filename: _read_snapshot_source(
            source, label=f"normal reconciliation {filename}"
        )
        for filename, source in sources.items()
    }
    if set(exact) != set(sources):
        raise ReviewRunError("normal reconciliation snapshot set is incomplete")
    targets: dict[str, Path] = {}
    for filename, content in exact.items():
        target = run_dir / filename
        _write_snapshot(target, content, label=f"normal reconciliation {filename}")
        targets[filename] = target
    return targets


def _prepare_replay_run(source: Path) -> Path:
    """Create a distinct run with immutable ledger and semantic fixture copies."""
    source = _run_child(source, label="replay source")
    for required in (
        "run-report.json", "run-report.md", "semantic-analysis.json",
        "work-accounting-result.json",
    ):
        if not (source / required).is_file():
            raise ValueError(f"replay source is incomplete; missing {source / required}")
    source_identity = _ledger_identity(source)
    source_accounting_identity = _accounting_identity(source)
    source_analysis_path = source / "semantic-analysis.json"
    source_analysis_sha256 = hashlib.sha256(source_analysis_path.read_bytes()).hexdigest()
    reconciliation_snapshots: dict[str, bytes] = {}
    for filename in _RECONCILIATION_INPUTS.values():
        source_path = source / filename
        try:
            reconciliation_snapshots[filename] = _read_snapshot_source(
                source_path, label=f"replay source {filename}"
            )
        except ReviewRunError as exc:
            raise ValueError(f"replay source missing reconciliation snapshot: {filename}") from exc
    stem = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ") + f"-replay-{source.name}"
    target = RUNS.resolve() / stem
    suffix = 1
    while target.exists():
        target = RUNS.resolve() / f"{stem}-{suffix}"
        suffix += 1
    try:
        (target / "evidence").mkdir(parents=True)
        shutil.copyfile(
            source / "evidence" / "evidence-ledger.json",
            target / "evidence" / "evidence-ledger.json",
        )
        fixture_path = target / "replay-fixture" / "semantic-analysis.json"
        fixture_path.parent.mkdir(parents=True)
        shutil.copyfile(source_analysis_path, fixture_path)
        for filename, content in reconciliation_snapshots.items():
            _write_snapshot(target / filename, content, label=f"replay {filename}")
        meeting_source = source / _CANONICAL_MEETING_RECONCILIATION
        if meeting_source.is_file() and not meeting_source.is_symlink():
            shutil.copyfile(meeting_source, target / _CANONICAL_MEETING_RECONCILIATION)
        report = _read_json(source / "run-report.json")
        if not isinstance(report, dict):
            raise ValueError("replay source run report must be an object")
        report = dict(report)
        report["run_id"] = target.name
        report["replay_of_run_id"] = source.name
        _write_json(target / "run-report.json", report)
        shutil.copyfile(source / "run-report.md", target / "run-report.md")
        _write_json(
            target / "replay-source.json",
            {
                "schema_version": 1,
                "source_run_id": source.name,
                "source_run_dir": str(source),
                "source_manifest_id": source_identity["manifest_id"],
                "source_events_digest": source_identity["events_digest"],
                "ledger_file_sha256": source_identity["file_sha256"],
                "semantic_analysis_sha256": source_analysis_sha256,
                "semantic_analysis_fixture": str(fixture_path.relative_to(target)),
                "work_accounting_result_sha256": source_accounting_identity["file_sha256"],
            },
        )
        if _ledger_identity(target) != source_identity:
            raise ValueError("replay ledger copy does not match its immutable source")
        if hashlib.sha256(fixture_path.read_bytes()).hexdigest() != source_analysis_sha256:
            raise ValueError("replay semantic analysis fixture copy does not match its immutable source")
        return target
    except Exception:
        shutil.rmtree(target, ignore_errors=True)
        raise


def _replay_analysis_fixture(source: Path, replay: Path) -> Path:
    """Resolve and verify the sealed offline analyzer fixture for a replay."""
    source = _run_child(source, label="replay source")
    replay = _run_child(replay, label="replay run")
    provenance = _read_json(replay / "replay-source.json")
    if not isinstance(provenance, dict):
        raise ValueError("replay source provenance must be an object")
    relative = str(provenance.get("semantic_analysis_fixture") or "")
    expected = str(provenance.get("semantic_analysis_sha256") or "")
    if not relative or len(expected) != 64:
        raise ValueError("replay semantic analysis fixture identity is incomplete")
    fixture = (replay / relative).resolve()
    try:
        fixture.relative_to(replay)
    except ValueError as exc:
        raise ValueError("replay semantic analysis fixture escapes the replay run") from exc
    if not fixture.is_file():
        raise ValueError("replay semantic analysis fixture is missing")
    source_digest = hashlib.sha256((source / "semantic-analysis.json").read_bytes()).hexdigest()
    fixture_digest = hashlib.sha256(fixture.read_bytes()).hexdigest()
    if source_digest != expected or fixture_digest != expected:
        raise ValueError("replay semantic analysis fixture differs from its immutable source")
    return fixture


def _prepare_repair_run(source: Path) -> Path:
    """Derive a new accounting run without recollection or changing its source."""
    source, snapshots = _resume_source(source)
    if _adopt_completed_resume(source) is None:
        raise ReviewRunError("repair requires a verified completed source")
    _validated_period_manifest(snapshots["period-manifest.json"], allow_collecting_bootstrap=True)
    target = Path(tempfile.mkdtemp(
        prefix=dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ") + "-repair-",
        dir=RUNS.resolve(),
    ))
    # All source reads are verified before a destination is eligible for resume.
    for filename in ("run-report.md", "evidence/evidence-ledger.json", *_RECONCILIATION_INPUTS.values()):
        content = _read_snapshot_source(source / filename, label=f"repair source {filename}")
        (target / filename).parent.mkdir(parents=True, exist_ok=True)
        _write_snapshot(target / filename, content, label=f"repair {filename}")
    report = dict(_read_json(source / "run-report.json"))
    report.update(run_id=target.name, repair_of_run_id=source.name)
    _write_json(target / "run-report.json", report)
    _write_snapshot(target / "repair-source.json", json.dumps({
        "schema_version": 1,
        "source_run_id": source.name,
        "source_completion_sha256": _file_sha256(source / "completion-bundle.json", label="repair source completion"),
        "ledger_identity": _ledger_identity(source),
    }, sort_keys=True).encode("utf-8") + b"\n", label="repair provenance")
    return target


def _finalize_repair_completion(run_dir: Path) -> collector_receipts.SliceCompletionBundle:
    """Seal derived output without replacing any collector/backlog receipt."""
    lineage = _read_json(run_dir / "repair-source.json")
    source = _run_child(RUNS / str(lineage.get("source_run_id", "")), label="repair source")
    source_bundle_path = source / "completion-bundle.json"
    if _file_sha256(source_bundle_path, label="repair source completion") != lineage.get("source_completion_sha256"):
        raise ReviewRunError("repair source completion changed")
    original = collector_receipts.load_completion_bundle(source_bundle_path, run_dir=source)
    if _ledger_identity(run_dir) != lineage.get("ledger_identity"):
        raise ReviewRunError("repair immutable evidence changed")
    for filename in _RECONCILIATION_INPUTS.values():
        if _read_snapshot_source(source / filename, label="repair original input") != _read_snapshot_source(run_dir / filename, label="repair snapshot input"):
            raise ReviewRunError("repair reconciliation snapshot changed")
    slice_ = argparse.Namespace(
        slice_id=original.slice_id,
        since=dt.datetime.fromisoformat(original.since_utc.replace("Z", "+00:00")),
        until=dt.datetime.fromisoformat(original.until_utc.replace("Z", "+00:00")),
    )
    bundle = collector_receipts.build_completion_bundle(run_dir, slice_=slice_)
    path = run_dir / "completion-bundle.json"
    if path.exists():
        if collector_receipts.load_completion_bundle(path, run_dir=run_dir).bundle_digest != bundle.bundle_digest:
            raise ReviewRunError("repair completion bundle differs")
    else:
        collector_receipts.write_completion_bundle(path, bundle)
    return collector_receipts.load_completion_bundle(path, run_dir=run_dir)


def _snapshot_recovery_inputs(run_dir: Path, parent: Path) -> dict[str, Path]:
    """Copy only byte-identical immutable reconciliation inputs from the parent."""
    verified = clockify_source_debt_recover.verify_recovery_run(run_dir)
    parent = _run_child(parent, label="recovery parent")
    if verified.parent_run_dir != parent:
        raise ReviewRunError("recovery parent differs from its transition")
    snapshots: dict[str, Path] = {}
    for filename in clockify_source_debt_recover.RECONCILIATION_SNAPSHOTS:
        content = _read_snapshot_source(parent / filename, label=f"recovery parent {filename}")
        target = verified.run_dir / filename
        _write_snapshot(target, content, label=f"recovery {filename}")
        snapshots[filename] = target
    return snapshots


def _recovery_source_status(
    bundle: collector_receipts.SliceCompletionBundle, source: str
) -> str:
    coverage = collector_receipts.completion_coverage(bundle)
    incomplete = coverage.get("incomplete_sources")
    sources = coverage.get("sources")
    record = sources.get(source) if isinstance(sources, dict) else None
    if not isinstance(incomplete, list) or not isinstance(record, dict):
        raise ReviewRunError("recovery bundle has no canonical requested-source identity")
    if record.get("status") == "excluded":
        raise ReviewRunError("recovery requested source is excluded rather than collected")
    if source not in incomplete and record.get("status") == "complete":
        return "complete"
    if source in incomplete and record.get("status") != "complete":
        return "incomplete"
    raise ReviewRunError("recovery requested-source coverage is contradictory")


def _finalize_recovery_completion(
    run_dir: Path,
) -> collector_receipts.SliceCompletionBundle:
    """Seal one derived attempt without modifying either backlog receipt."""
    verified = clockify_source_debt_recover.verify_recovery_run(run_dir)
    parent_bundle = collector_receipts.load_completion_bundle(
        verified.parent_run_dir / "completion-bundle.json",
        run_dir=verified.parent_run_dir,
    )
    for filename in clockify_source_debt_recover.RECONCILIATION_SNAPSHOTS:
        if _read_snapshot_source(
            verified.parent_run_dir / filename, label=f"recovery parent {filename}"
        ) != _read_snapshot_source(verified.run_dir / filename, label=f"recovery snapshot {filename}"):
            raise ReviewRunError("recovery reconciliation snapshot changed")
    slice_ = argparse.Namespace(
        slice_id=parent_bundle.slice_id,
        since=dt.datetime.fromisoformat(parent_bundle.since_utc.replace("Z", "+00:00")),
        until=dt.datetime.fromisoformat(parent_bundle.until_utc.replace("Z", "+00:00")),
    )
    bundle = collector_receipts.build_completion_bundle(verified.run_dir, slice_=slice_)
    path = verified.run_dir / "completion-bundle.json"
    if path.exists():
        existing = collector_receipts.load_completion_bundle(path, run_dir=verified.run_dir)
        if existing.bundle_digest != bundle.bundle_digest:
            raise ReviewRunError("recovery completion bundle differs")
    else:
        collector_receipts.write_completion_bundle(path, bundle)
    sealed = collector_receipts.load_completion_bundle(path, run_dir=verified.run_dir)
    _recovery_source_status(sealed, str(verified.transition["source"]))
    return sealed


def verify_source_debt_recovery_completion(
    run_dir: Path,
    *,
    parent_run_dir: Path,
    source: str,
    attempt_id: str,
    _visited: frozenset[Path] | None = None,
) -> tuple[collector_receipts.SliceCompletionBundle, str]:
    """Read-only verification boundary for Task 3's explicit durable debt."""
    try:
        verified = clockify_source_debt_recover.verify_recovery_run(
            run_dir, _visited=_visited
        )
    except clockify_source_debt_recover.SourceDebtRecoveryError as exc:
        raise ReviewRunError("source-debt recovery transition is invalid") from exc
    parent = _run_child(parent_run_dir, label="recovery completion parent")
    if verified.parent_run_dir != parent:
        raise ReviewRunError("recovery completion parent differs")
    if verified.transition.get("source") != source:
        raise ReviewRunError("recovery completion source differs")
    if verified.transition.get("attempt_id") != attempt_id:
        raise ReviewRunError("recovery completion attempt differs")
    for filename in clockify_source_debt_recover.RECONCILIATION_SNAPSHOTS:
        if _read_snapshot_source(
            parent / filename, label=f"recovery completion parent {filename}"
        ) != _read_snapshot_source(
            verified.run_dir / filename, label=f"recovery completion snapshot {filename}"
        ):
            raise ReviewRunError("recovery completion snapshot differs")
    try:
        parent_bundle = collector_receipts.load_completion_bundle(
            parent / "completion-bundle.json", run_dir=parent
        )
        bundle = collector_receipts.load_completion_bundle(
            verified.run_dir / "completion-bundle.json", run_dir=verified.run_dir
        )
        slice_ = argparse.Namespace(
            slice_id=parent_bundle.slice_id,
            since=dt.datetime.fromisoformat(parent_bundle.since_utc.replace("Z", "+00:00")),
            until=dt.datetime.fromisoformat(parent_bundle.until_utc.replace("Z", "+00:00")),
        )
        rebuilt = collector_receipts.build_completion_bundle(verified.run_dir, slice_=slice_)
    except (OSError, ValueError, collector_receipts.CollectorReceiptError) as exc:
        raise ReviewRunError("recovery completion bundle is invalid") from exc
    if bundle.bundle_digest != rebuilt.bundle_digest:
        raise ReviewRunError("recovery completion bundle differs from verified artifacts")
    status = _recovery_source_status(bundle, source)
    result_path = verified.run_dir / "autopilot-result.json"
    try:
        result = _read_json(result_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ReviewRunError("recovery terminal result is invalid") from exc
    expected_result_identity = {
        "source": source,
        "attempt_id": attempt_id,
        "status": status,
        "transition_digest": verified.transition["transition_digest"],
    }
    if (
        not isinstance(result, dict)
        or result.get("quality_status") != "pass"
        or result.get("completion_bundle_digest") != bundle.bundle_digest
        or result.get("source_debt_recovery") != expected_result_identity
    ):
        raise ReviewRunError("recovery terminal result identity differs")
    return bundle, status


def _analysis_versions(document: dict[str, Any]) -> list[str]:
    versions: set[str] = set()
    prompt_version = str(document.get("prompt_version") or "")
    schema_version = document.get("schema_version")
    evidence_bundle_schema_version = str(
        document.get("evidence_bundle_schema_version") or ""
    )
    for key in ("activities", "analysis_chunks"):
        values = document.get(key, [])
        if not isinstance(values, list):
            raise ValueError(f"semantic analysis {key} must be a list")
        pending = [(value, None, None) for value in values]
        recovery_nodes = 0
        while pending:
            raw, expected_path, expected_depth = pending.pop(0)
            if not isinstance(raw, dict):
                raise ValueError(f"semantic analysis {key} item must be an object")
            if expected_path is not None and (
                raw.get("partition_path") != expected_path
                or raw.get("partition_depth") != expected_depth
            ):
                raise ValueError("semantic analysis recovery path or depth is invalid")
            recovery = raw.get("recovery")
            if recovery is not None:
                recovery_nodes += 1
                if recovery_nodes > 511:
                    raise ValueError("semantic analysis recovery tree exceeds its bound")
                if not isinstance(recovery, dict) or not isinstance(
                    recovery.get("children"), list
                ):
                    raise ValueError("semantic analysis recovery metadata is invalid")
                path = str(raw.get("partition_path") or "")
                depth = raw.get("partition_depth")
                children = recovery["children"]
                child_counts = [
                    child.get("event_count") if isinstance(child, dict) else None
                    for child in children
                ]
                expected_recovery_status = {
                    "recovered": "recovered_by_partition",
                    "exhausted": "partition_exception",
                }.get(str(recovery.get("status") or ""))
                if (
                    not path
                    or not isinstance(depth, int)
                    or isinstance(depth, bool)
                    or recovery.get("path") != path
                    or recovery.get("depth") != depth
                    or len(children) != 2
                    or depth >= semantic_analyzer.MAX_PARTITION_RECOVERY_DEPTH
                    or any(
                        not isinstance(count, int) or isinstance(count, bool) or count <= 0
                        for count in child_counts
                    )
                    or sum(child_counts) != raw.get("event_count")
                    or raw.get("recovery_status") != expected_recovery_status
                ):
                    raise ValueError("semantic analysis recovery metadata is invalid")
                pending[0:0] = [
                    (child, f"{path}.{label}", depth + 1)
                    for label, child in zip(("a", "b"), children, strict=True)
                ]
            model = str(raw.get("analyzer_model") or raw.get("model") or "")
            tier = str(raw.get("analyzer_tier") or raw.get("tier") or "")
            if model and tier:
                version = {
                    "model": model,
                    "tier": tier,
                    "prompt_version": str(raw.get("prompt_version") or prompt_version),
                    "schema_version": raw.get("schema_version", schema_version),
                    "evidence_bundle_schema_version": str(
                        raw.get("evidence_bundle_schema_version")
                        or evidence_bundle_schema_version
                    ),
                }
                versions.add(json.dumps(version, sort_keys=True, separators=(",", ":")))
    if not versions:
        raise ValueError("semantic analysis does not identify an analyzer route/version")
    return sorted(versions)


def _analysis_bundle_identity(document: dict[str, Any]) -> dict[str, str]:
    manifest = document.get("evidence_bundle_manifest")
    if not isinstance(manifest, dict):
        raise ValueError("semantic analysis lacks an evidence bundle manifest")
    schema_version = str(manifest.get("schema_version") or "")
    digest = str(manifest.get("digest") or "")
    bundles = manifest.get("bundles")
    if (
        not schema_version
        or not digest.startswith("sebm-")
        or len(digest) != 69
        or not isinstance(bundles, list)
    ):
        raise ValueError("semantic analysis evidence bundle manifest is invalid")
    expected = semantic_analyzer.stable_digest(
        "sebm-", bundles, length=64
    )
    if digest != expected:
        raise ValueError("semantic analysis evidence bundle manifest digest differs")
    if str(document.get("evidence_bundle_schema_version") or "") != schema_version:
        raise ValueError("semantic analysis evidence bundle schema differs")
    return {"schema_version": schema_version, "digest": digest}


def _analysis_cache_records(document: dict[str, Any]) -> list[dict[str, str]]:
    cache = document.get("analyzer_cache")
    if not isinstance(cache, dict):
        return []
    records = cache.get("records", [])
    if not isinstance(records, list):
        raise ValueError("semantic analysis cache records must be a list")
    normalized: list[dict[str, str]] = []
    for raw in records:
        if not isinstance(raw, dict) or set(raw) != {"cache_key", "decision_digest"}:
            raise ValueError("semantic analysis cache record is invalid")
        cache_key = str(raw.get("cache_key") or "")
        decision_digest = str(raw.get("decision_digest") or "")
        if not cache_key.startswith("arc-") or len(decision_digest) != 64:
            raise ValueError("semantic analysis cache identity is invalid")
        normalized.append({"cache_key": cache_key, "decision_digest": decision_digest})
    return sorted(normalized, key=lambda value: value["cache_key"])


def derive_replay_integrity(source: Path, replay: Path) -> dict[str, Any]:
    """Derive replay integrity without writing into either sealed run."""
    source = _run_child(source, label="replay source")
    replay = _run_child(replay, label="replay run")
    source_identity = _ledger_identity(source)
    replay_identity = _ledger_identity(replay)
    source_accounting_identity = _accounting_identity(source)
    replay_accounting_identity = _accounting_identity(replay)
    source_analysis = _read_json(source / "semantic-analysis.json")
    replay_analysis = _read_json(replay / "semantic-analysis.json")
    if not isinstance(source_analysis, dict) or not isinstance(replay_analysis, dict):
        raise ValueError("semantic analysis artifacts must be objects")
    source_versions = _analysis_versions(source_analysis)
    replay_versions = _analysis_versions(replay_analysis)
    source_cache_records = _analysis_cache_records(source_analysis)
    replay_cache_records = _analysis_cache_records(replay_analysis)
    source_bundle_identity = _analysis_bundle_identity(source_analysis)
    replay_bundle_identity = _analysis_bundle_identity(replay_analysis)
    source_evidence_digest = str(source_analysis.get("ledger_evidence_digest") or "")
    replay_evidence_digest = str(replay_analysis.get("ledger_evidence_digest") or "")
    failures: list[str] = []
    if source_identity != replay_identity:
        failures.append("immutable ledger identity differs")
    if not source_evidence_digest or source_evidence_digest != replay_evidence_digest:
        failures.append("semantic ledger evidence digest differs")
    if source_versions != replay_versions:
        failures.append("analyzer route or version differs")
    if source_cache_records != replay_cache_records:
        failures.append("validated analyzer cache decisions differ")
    if source_bundle_identity != replay_bundle_identity:
        failures.append("semantic evidence bundle manifest differs")
    if source_accounting_identity != replay_accounting_identity:
        failures.append("work accounting result differs")
    source_reconciliation_binding: dict[str, str] | None = None
    replay_reconciliation_binding: dict[str, str] | None = None
    if not failures:
        source_reconciliation_binding = _reconciliation_binding(
            source,
            **{name: source / filename for name, filename in _RECONCILIATION_INPUTS.items()},
        )
        replay_reconciliation_binding = _reconciliation_binding(
            replay,
            completion_source=source,
            **{name: replay / filename for name, filename in _RECONCILIATION_INPUTS.items()},
        )
        if source_reconciliation_binding != replay_reconciliation_binding:
            failures.append("reconciliation period binding differs")
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "pass" if not failures else "blocked",
        "source_run_id": source.name,
        "replay_run_id": replay.name,
        "ledger_identity": replay_identity,
        "ledger_evidence_digest": replay_evidence_digest,
        "analyzer_versions": [json.loads(value) for value in replay_versions],
        "analyzer_cache_records": replay_cache_records,
        "evidence_bundle_manifest": replay_bundle_identity,
        "work_accounting_result": replay_accounting_identity,
        "reconciliation_binding": replay_reconciliation_binding,
        "failures": failures,
    }
    report["integrity_digest"] = "sha256:" + hashlib.sha256(
        json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return report


def _verify_replay_integrity(source: Path, replay: Path) -> dict[str, Any]:
    report = derive_replay_integrity(source, replay)
    replay = _run_child(replay, label="replay run")
    _write_json(replay / "replay-integrity.json", report)
    if report["failures"]:
        raise ValueError("; ".join(report["failures"]))
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="YYYY-MM-DD")
    parser.add_argument("--until", help="YYYY-MM-DD inclusive")
    parser.add_argument(
        "--runs-root",
        type=Path,
        default=Path(os.environ.get("CLOCKIFY_AUTOPILOT_RUNS_ROOT", str(RUNS))),
        help="Canonical operational run directory; defaults to <release-root>/runs.",
    )
    parser.add_argument("--no-enrich", action="store_true")
    parser.add_argument(
        "--calendly-optional", action="store_true",
        help="Explicitly exclude Calendly from this bounded collection without contacting its gateway",
    )
    parser.add_argument(
        "--replay-from",
        type=Path,
        help="Reuse a completed run's immutable evidence ledger in a distinct replay run.",
    )
    parser.add_argument(
        "--resume-from", type=Path,
        help="Resume accounting for one existing, locally snapshotted source run.",
    )
    parser.add_argument("--repair-from", type=Path, help="Re-derive accounting in a distinct run from a completed source's exact snapshots and validated cache.")
    parser.add_argument(
        "--recover-source-debt-from", type=Path,
        help="Recollect one exact incomplete source from a verified immutable parent run.",
    )
    parser.add_argument("--recover-source")
    parser.add_argument("--recover-attempt-id")
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE)
    parser.add_argument("--period-manifest", type=Path)
    parser.add_argument("--routing", type=Path, default=DEFAULT_ROUTING)
    parser.add_argument("--corrections", type=Path, default=DEFAULT_CORRECTIONS)
    parser.add_argument(
        "--review-mode",
        choices=sorted(REVIEW_MODES),
        default="shadow_all",
        help="shadow_all reviews the full denominator; exceptions_only requires a passing acceptance ledger.",
    )
    parser.add_argument(
        "--acceptance-ledger",
        type=Path,
        default=DEFAULT_ACCEPTANCE_LEDGER,
    )
    parser.add_argument(
        "--analysis-fixture",
        type=Path,
        help="Offline validated semantic response fixture; never used by scheduled production runs.",
    )
    parser.add_argument(
        "--analyzer-cache",
        type=Path,
        help="Validated append-only semantic response cache; defaults beside --state.",
    )
    parser.add_argument("--analyzer-target-body-bytes", type=int)
    parser.add_argument("--analyzer-max-events-per-chunk", type=int)
    parser.add_argument("--analyzer-workers", type=int)
    return parser.parse_args(argv)


def _process_run(
    args: argparse.Namespace,
    run_dir: Path,
    acceptance_gate: dict[str, Any],
) -> tuple[int, Path]:
    replay_source = getattr(args, "_replay_source", None)
    replay_analysis_fixture = getattr(args, "_replay_analysis_fixture", None)
    accounting_command = [
        sys.executable,
        str(SCRIPTS / "work_accounting_pipeline.py"),
        str(run_dir),
        "--root",
        str(ROOT),
        "--routing",
        str(args.routing),
        "--corrections",
        str(args.corrections),
        "--analyzer-cache",
        str(args.analyzer_cache or (args.state.parent / "analyzer-cache-v2.jsonl")),
    ]
    analysis_fixture = replay_analysis_fixture or args.analysis_fixture
    if analysis_fixture:
        accounting_command.extend(["--analysis-fixture", str(analysis_fixture)])
    for option, value in (
        ("--analyzer-target-body-bytes", args.analyzer_target_body_bytes),
        ("--analyzer-max-events-per-chunk", args.analyzer_max_events_per_chunk),
        ("--analyzer-workers", args.analyzer_workers),
    ):
        if value is not None:
            accounting_command.extend([option, str(value)])
    accounted = _run(accounting_command)
    if accounted.returncode != 0:
        quality = {
            "status": "blocked",
            "summary": {
                "semantic_analysis": "unavailable_or_invalid",
                "reason": (accounted.stderr or accounted.stdout).strip()[:500],
            },
        }
        _write_json(run_dir / "quality_report.json", quality)
        result = build_result(
            run_dir,
            quality,
            None,
            review_mode=args.review_mode,
            acceptance_gate=acceptance_gate,
        )
        result["paths"]["work_accounting_result"] = None
        result_path = run_dir / "autopilot-result.json"
        summary_path = run_dir / "autopilot-summary.md"
        _write_json(result_path, result)
        write_summary(summary_path, result)
        return accounted.returncode or 2, result_path

    if replay_source is not None:
        try:
            _verify_replay_integrity(replay_source, run_dir)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            quality = {
                "status": "blocked",
                "summary": {"immutable_replay": "failed", "reason": str(exc)},
            }
            _write_json(run_dir / "quality_report.json", quality)
            result = build_result(
                run_dir,
                quality,
                None,
                review_mode=args.review_mode,
                acceptance_gate=acceptance_gate,
            )
            result_path = run_dir / "autopilot-result.json"
            _write_json(result_path, result)
            write_summary(run_dir / "autopilot-summary.md", result)
            return 2, result_path

    checked = _run(
        [
            sys.executable,
            str(SCRIPTS / "clockify_sync_quality.py"),
            run_dir.name,
            "--runs-root",
            str(RUNS),
            "--root",
            str(ROOT),
            "--routing",
            str(args.routing),
        ]
    )
    if checked.returncode != 0:
        print(checked.stderr or checked.stdout, file=sys.stderr)
        return checked.returncode or 2, run_dir / "autopilot-result.json"
    quality = _read_json(run_dir / "quality_report.json")

    snapshot = None
    if quality.get("status") != "blocked":
        reconciled = _run(
            [
                sys.executable,
                str(SCRIPTS / "clockify_review_state.py"),
                str(run_dir),
                "--state",
                str(args.state),
            ]
        )
        if reconciled.returncode != 0:
            print(reconciled.stderr or reconciled.stdout, file=sys.stderr)
            return reconciled.returncode or 2, run_dir / "autopilot-result.json"
        snapshot = _read_json(run_dir / "review-snapshot.json")

    result = build_result(
        run_dir,
        quality,
        snapshot,
        review_mode=args.review_mode,
        acceptance_gate=acceptance_gate,
    )
    completion_error = None
    has_repair_source = (run_dir / "repair-source.json").is_file()
    report_document = _read_json(run_dir / "run-report.json")
    has_recovery_source = (
        isinstance(report_document, dict)
        and isinstance(report_document.get("source_debt_recovery"), dict)
    )
    if ((run_dir / "slice-finalization.json").is_file() or has_repair_source) and snapshot is not None:
        if quality.get("status") == "pass":
            try:
                if has_repair_source:
                    bundle = _finalize_repair_completion(run_dir)
                elif has_recovery_source:
                    bundle = _finalize_recovery_completion(run_dir)
                else:
                    bundle = _finalize_backlog_completion(
                        run_dir, replay=replay_source is not None
                    )
            except (OSError, ValueError, collector_receipts.CollectorReceiptError) as exc:
                completion_error = str(exc)
            else:
                # The runner consumes only this safe exact identity, never report paths
                # or downstream evidence names.
                result["slice_id"] = bundle.slice_id
                result["date_range"] = {
                    "since": bundle.since_utc,
                    "until": bundle.until_utc,
                }
                result["completion_bundle_digest"] = bundle.bundle_digest
                result["completion_bundle"] = bundle.document()
                if has_recovery_source:
                    transition = report_document["source_debt_recovery"]
                    result["source_debt_recovery"] = {
                        "source": transition["source"],
                        "attempt_id": transition["attempt_id"],
                        "status": _recovery_source_status(bundle, transition["source"]),
                        "transition_digest": transition["transition_digest"],
                    }
        else:
            result["completion_bundle_digest"] = None
    if completion_error is not None:
        quality = {
            "status": "blocked",
            "summary": {"completion_bundle": "invalid_or_incomplete", "reason": completion_error},
        }
        result = build_result(
            run_dir, quality, None, review_mode=args.review_mode,
            acceptance_gate=acceptance_gate,
        )
        result["completion_bundle_digest"] = None
    result["paths"]["work_accounting_result"] = str(
        (run_dir / "work-accounting-result.json").resolve()
    )
    result["paths"]["replay_integrity"] = (
        str((run_dir / "replay-integrity.json").resolve())
        if replay_source is not None
        else None
    )
    result_path = run_dir / "autopilot-result.json"
    summary_path = run_dir / "autopilot-summary.md"
    if snapshot is not None:
        review_csv = run_dir / "review-current.csv"
        write_current_review_csv(review_csv, snapshot)
        result["paths"]["review_current_csv"] = str(review_csv.resolve())
    else:
        result["paths"]["review_current_csv"] = None
    _write_json(result_path, result)
    write_summary(summary_path, result)
    return (2 if completion_error is not None else 0), result_path


def _acceptance_gate(path: Path) -> dict[str, Any]:
    gate: dict[str, Any] = {
        "exceptions_only_eligible": False,
        "status": "not_recorded",
    }
    try:
        gate = review_acceptance.evaluate_gate(review_acceptance.load_ledger(path))
        gate["status"] = "evaluated"
    except (OSError, json.JSONDecodeError, review_acceptance.AcceptanceError) as exc:
        gate = {
            "exceptions_only_eligible": False,
            "status": "invalid",
            "reason": str(exc),
        }
    return gate


def _acceptance_gate_content(content: bytes) -> dict[str, Any]:
    """Evaluate the exact bytes retained for a future run snapshot."""
    with tempfile.NamedTemporaryFile(prefix="clockify-acceptance-") as handle:
        handle.write(content)
        handle.flush()
        return _acceptance_gate(Path(handle.name))


def _option_was_supplied(argv: list[str], option: str) -> bool:
    return option in argv or any(value.startswith(option + "=") for value in argv)


def _resume_source(path: Path) -> tuple[Path, dict[str, Path]]:
    """Validate an interrupted normal run before accounting can resume it."""
    if path.is_symlink():
        raise ValueError("resume source must not be a symlink")
    run_dir = _run_child(path, label="resume source")
    for required in ("run-report.json", "evidence/evidence-ledger.json"):
        if not (run_dir / required).is_file():
            raise ValueError(f"resume source is incomplete; missing {required}")
    snapshots: dict[str, Path] = {}
    for filename in _RECONCILIATION_INPUTS.values():
        candidate = run_dir / filename
        try:
            _read_snapshot_source(candidate, label=f"resume source {filename}")
        except ReviewRunError as exc:
            raise ValueError(f"resume source missing reconciliation snapshot: {filename}") from exc
        snapshots[filename] = candidate
    if (run_dir / "replay-source.json").is_file():
        raise ValueError("resume source must not be an immutable replay run")
    return run_dir, snapshots


def _adopt_completed_resume(source: Path) -> Path | None:
    """Return only a digest-bound completed normal source result."""
    result_path = source / "autopilot-result.json"
    if not result_path.is_file() or not (source / "review-snapshot.json").is_file():
        return None
    try:
        result = _read_json(result_path)
        bundle = collector_receipts.load_completion_bundle(
            source / "completion-bundle.json", run_dir=source
        )
        coverage = collector_receipts.completion_coverage(bundle)
    except (OSError, ValueError, json.JSONDecodeError, collector_receipts.CollectorReceiptError) as exc:
        raise ValueError("completed resume source cannot be verified") from exc
    if (
        bundle.replay
        or not isinstance(result, dict)
        or result.get("quality_status") != "pass"
        or coverage.get("status") != "complete"
        or coverage.get("incomplete_sources") != []
    ):
        raise ValueError("completed resume source does not prove normal completion")
    return result_path


def _adopt_completed_recovery(source: Path) -> Path | None:
    """Reuse either trustworthy terminal outcome for the exact same attempt."""
    result_path = source / "autopilot-result.json"
    if not result_path.is_file() or not (source / "review-snapshot.json").is_file():
        return None
    try:
        verified = clockify_source_debt_recover.verify_recovery_run(source)
        bundle, status = verify_source_debt_recovery_completion(
            source,
            parent_run_dir=verified.parent_run_dir,
            source=str(verified.transition["source"]),
            attempt_id=str(verified.transition["attempt_id"]),
        )
    except (
        OSError, ValueError, json.JSONDecodeError,
        collector_receipts.CollectorReceiptError,
        clockify_source_debt_recover.SourceDebtRecoveryError,
    ) as exc:
        raise ValueError("completed recovery terminal result cannot be verified") from exc
    if (
        bundle.replay
        or status not in {"complete", "incomplete"}
    ):
        raise ValueError("completed recovery attempt does not prove a terminal outcome")
    return result_path


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(raw_argv)
    try:
        args.runs_root = _configure_runs_root(args.runs_root)
    except ValueError as exc:
        print(f"clockify review run: {exc}", file=sys.stderr)
        return 2
    reconciliation_options = (
        "--period-manifest", "--routing", "--corrections", "--acceptance-ledger",
    )
    recovery_values = (
        args.recover_source_debt_from,
        args.recover_source,
        args.recover_attempt_id,
    )
    recovery_mode = all(value is not None for value in recovery_values)
    if any(value is not None for value in recovery_values) and not recovery_mode:
        print(
            "clockify review run: source-debt recovery requires --recover-source-debt-from, "
            "--recover-source and --recover-attempt-id together",
            file=sys.stderr,
        )
        return 2
    if sum((args.replay_from is not None, args.resume_from is not None, args.repair_from is not None, recovery_mode)) > 1:
        print("clockify review run: replay, resume, repair and recovery modes are mutually exclusive", file=sys.stderr)
        return 2
    if (args.replay_from or args.resume_from or args.repair_from or recovery_mode) and (
        args.since or args.until or args.no_enrich or args.calendly_optional
        or args.analysis_fixture
        or any(_option_was_supplied(raw_argv, option) for option in reconciliation_options)
    ):
        print(
            "clockify review run: --replay-from/--resume-from cannot be combined with collection "
            "range/enrichment options, --analysis-fixture, or reconciliation input overrides",
            file=sys.stderr,
        )
        return 2
    if not args.replay_from and not args.resume_from and not args.repair_from and not recovery_mode and args.period_manifest is None:
        print(
            "clockify review run: every fresh run requires --period-manifest",
            file=sys.stderr,
        )
        return 2

    reconciliation_contents: dict[str, bytes] | None = None
    if not args.replay_from and not args.resume_from and not args.repair_from and not recovery_mode:
        try:
            reconciliation_contents = {
                filename: _read_snapshot_source(
                    source, label=f"normal reconciliation {filename}"
                )
                for filename, source in _reconciliation_input_sources(args).items()
            }
        except ReviewRunError as exc:
            print(f"clockify review run: {exc}", file=sys.stderr)
            return 2
        if args.review_mode == "exceptions_only":
            acceptance_gate = _acceptance_gate_content(
                reconciliation_contents["review-acceptance.jsonl"]
            )
            if not acceptance_gate.get("exceptions_only_eligible"):
                print(
                    "clockify review run: exceptions_only is locked until one passing 90% "
                    "baseline and two later consecutive passing 95% guarded periods",
                    file=sys.stderr,
                )
                return 2

    collector_code = 0
    collector_error = ""
    if recovery_mode:
        try:
            recovered = clockify_source_debt_recover.recover(
                args.recover_source_debt_from,
                args.recover_source,
                args.recover_attempt_id,
            )
            snapshots = _snapshot_recovery_inputs(
                recovered.run_dir, recovered.parent_run_dir
            )
            existing = _adopt_completed_recovery(recovered.run_dir)
            if existing is not None:
                print(existing)
                return 0
            run_dirs = (recovered.run_dir,)
            args._recovery_snapshots = snapshots
        except (
            OSError, ValueError, json.JSONDecodeError,
            clockify_source_debt_recover.SourceDebtRecoveryError,
        ) as exc:
            print(f"clockify review run: cannot recover source debt: {exc}", file=sys.stderr)
            return 2
    elif args.repair_from:
        try:
            run_dirs = (_prepare_repair_run(args.repair_from),)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"clockify review run: cannot prepare repair: {exc}", file=sys.stderr)
            return 2
    elif args.resume_from:
        try:
            source, snapshots = _resume_source(args.resume_from)
            existing = _adopt_completed_resume(source)
            if existing is not None:
                print(existing)
                return 0
            run_dirs = (source,)
            args._resume_snapshots = snapshots
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"clockify review run: cannot resume source: {exc}", file=sys.stderr)
            return 2
    elif args.replay_from:
        try:
            replay_source = _run_child(args.replay_from, label="replay source")
            run_dirs = (_prepare_replay_run(replay_source),)
            args._replay_source = replay_source
            args._replay_analysis_fixture = _replay_analysis_fixture(
                replay_source, run_dirs[0]
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"clockify review run: cannot prepare immutable replay: {exc}", file=sys.stderr)
            return 2
    else:
        collector = [
            sys.executable,
            str(SCRIPTS / "clockify_sync_collect.py"),
            "run",
            "--runs-root",
            str(RUNS),
        ]
        if args.since:
            collector.extend(["--since", args.since])
        if args.until:
            collector.extend(["--until", args.until])
        if args.calendly_optional:
            collector.append("--calendly-optional")
        collector.append("--no-enrich" if args.no_enrich else "--enrich")
        collected = _run(collector)
        collector_code = collected.returncode
        collector_error = collected.stderr or ""
        try:
            run_dirs = _collector_run_dirs(collected.stdout)
        except ValueError as exc:
            print(f"clockify review run: {exc}", file=sys.stderr)
            if collector_code != 0 and collector_error:
                print(collector_error, file=sys.stderr)
            return collector_code or 2

    for run_dir in run_dirs:
        run_args = argparse.Namespace(**vars(args))
        if args.replay_from or args.repair_from:
            snapshots = {
                filename: run_dir / filename for filename in _RECONCILIATION_INPUTS.values()
            }
        elif args.resume_from:
            snapshots = args._resume_snapshots
        elif recovery_mode:
            snapshots = args._recovery_snapshots
        else:
            try:
                snapshots = _snapshot_reconciliation_inputs(
                    run_dir, args, contents=reconciliation_contents
                )
            except ReviewRunError as exc:
                print(f"clockify review run: cannot snapshot reconciliation inputs: {exc}", file=sys.stderr)
                return 2
        run_args.period_manifest = snapshots["period-manifest.json"]
        run_args.routing = snapshots["routing.json"]
        run_args.corrections = snapshots["review-corrections.jsonl"]
        run_args.acceptance_ledger = snapshots["review-acceptance.jsonl"]
        try:
            _validated_period_manifest(
                run_args.period_manifest, allow_collecting_bootstrap=True,
            )
        except ReviewRunError as exc:
            print(
                f"clockify review run: period manifest preflight failed: {exc}",
                file=sys.stderr,
            )
            return 2
        acceptance_gate = _acceptance_gate(run_args.acceptance_ledger)
        if run_args.review_mode == "exceptions_only" and not acceptance_gate.get(
            "exceptions_only_eligible"
        ):
            print(
                "clockify review run: exceptions_only is locked until one passing 90% baseline "
                "and two later consecutive passing 95% guarded periods",
                file=sys.stderr,
            )
            return 2
        code, result_path = _process_run(run_args, run_dir, acceptance_gate)
        if code == 0 or result_path.is_file():
            print(result_path)
        if code != 0:
            return code
    if collector_code != 0:
        if collector_error:
            print(collector_error, file=sys.stderr)
        return collector_code or 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
