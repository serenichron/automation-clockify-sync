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
    from scripts import collector_receipts, reconciliation_manifest
except ModuleNotFoundError:  # direct script execution
    import clockify_sync_collect  # type: ignore[no-redef]
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
    reports = [
        Path(line.strip()).expanduser().resolve()
        for line in stdout.splitlines()
        if line.strip().endswith("/run-report.md")
    ]
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
    resolved = path.resolve()
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
        _validated_period_manifest(Path(period_manifest))
    )
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


def _verify_replay_integrity(source: Path, replay: Path) -> dict[str, Any]:
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
    _write_json(replay / "replay-integrity.json", report)
    if failures:
        raise ValueError("; ".join(failures))
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--since", help="YYYY-MM-DD")
    parser.add_argument("--until", help="YYYY-MM-DD inclusive")
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
    if (run_dir / "slice-finalization.json").is_file() and snapshot is not None:
        if quality.get("status") == "pass":
            try:
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


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    args = parse_args(raw_argv)
    reconciliation_options = (
        "--period-manifest", "--routing", "--corrections", "--acceptance-ledger",
    )
    if args.replay_from and (
        args.since or args.until or args.no_enrich or args.calendly_optional
        or args.analysis_fixture
        or any(_option_was_supplied(raw_argv, option) for option in reconciliation_options)
    ):
        print(
            "clockify review run: --replay-from cannot be combined with collection "
            "range/enrichment options, --analysis-fixture, or reconciliation input overrides",
            file=sys.stderr,
        )
        return 2
    if not args.replay_from and args.period_manifest is None:
        print(
            "clockify review run: every fresh run requires --period-manifest",
            file=sys.stderr,
        )
        return 2

    reconciliation_contents: dict[str, bytes] | None = None
    if not args.replay_from:
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
    if args.replay_from:
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
        if args.replay_from:
            snapshots = {
                filename: run_dir / filename for filename in _RECONCILIATION_INPUTS.values()
            }
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
