"""Sanitized failure receipts and digest-bound downstream completion bundles."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Mapping
from zoneinfo import ZoneInfo

try:
    from scripts import evidence_ledger
except ModuleNotFoundError:  # direct script execution
    import evidence_ledger  # type: ignore[no-redef]


class CollectorReceiptError(ValueError):
    pass


_SAFE_IDENTITY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:-]*$")
_ARTIFACT_PATHS = {
    "run_report": "run-report.json",
    "evidence_ledger": "evidence/evidence-ledger.json",
    "semantic_analysis": "semantic-analysis.json",
    "accounting_result": "work-accounting-result.json",
    "quality_report": "quality_report.json",
    "review_snapshot": "review-snapshot.json",
}
REQUIRED_KINDS = frozenset(_ARTIFACT_PATHS)
_REPLAY_ARTIFACT = ("replay_integrity", "replay-integrity.json")
_COLLECTOR_RAW_ARTIFACTS = {
    "clockify": "evidence/clockify-existing.json",
    "fathom": "evidence/fathom-meetings.json",
    "calendly": "evidence/calendly-recordings.json",
    "multica_issues": "evidence/multica-issues.json",
    "sessions": "evidence/sessions.json",
}
_BUNDLE_SCHEMA_VERSION = "collector-completion-bundle/v1"
_PERIOD_TIMEZONE = ZoneInfo("Europe/Bucharest")
_HISTORICAL_STAGE_SHA256 = "6735619bec99effe73492ac2bde554862908e6e86cae8b0de978ae6d0978e9e1"
_HISTORICAL_NORMALIZER_SHA256 = "2020001370f05624ef5a5f2e05123477bea68f566f0c8883bd1ef550722cbea7"
_HISTORICAL_MATERIALIZER_SHA256 = "8bbc609fe66b925ee3b223186247c9821b266d90c9b7213de5f2d321c2d1854f"
_HISTORICAL_AUDIT_PATH = Path("/tmp/clockify-september-normalization-audit-20261006-nSvDlL/proof.json")
_HISTORICAL_AUDIT_SHA256 = "e147f08fd2e9f5556f50993f6803075eb6aa78d6e5acda19e0c2c60ba1f9789b"
NATIVE_CHECKPOINT_PREFIX = "evidence/clockify-native-checkpoint/"


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _reject_symlink_components(path: Path) -> Path:
    """Reject a path that reaches any target through a symlink component."""
    absolute = _absolute(path)
    current = Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current /= part
        if current.is_symlink():
            raise CollectorReceiptError("receipt path contains a symlink")
    return absolute


def _safe_path(path: Path, *, run_dir: Path | None = None) -> Path:
    absolute = _reject_symlink_components(Path(path))
    if run_dir is not None:
        root = _reject_symlink_components(Path(run_dir))
        try:
            absolute.relative_to(root)
        except ValueError as exc:
            raise CollectorReceiptError("receipt path escapes its run directory") from exc
    return absolute


def _safe_read_text(path: Path) -> str:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            return handle.read()
    except Exception:
        # fdopen owns the descriptor after success; retain the original OSError
        # shape for callers while never following a final symlink.
        raise


def _safe_read_bytes_and_digest(path: Path) -> tuple[bytes, str]:
    """Read and hash one stable regular file through one no-follow descriptor."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise CollectorReceiptError("collector artifact is not a regular file")
        chunks: list[bytes] = []
        digest = hashlib.sha256()
        while block := os.read(descriptor, 65_536):
            chunks.append(block)
            digest.update(block)
        after = os.fstat(descriptor)
        identity = lambda value: (
            value.st_dev, value.st_ino, value.st_size,
            value.st_mtime_ns,
        )
        if identity(before) != identity(after):
            raise CollectorReceiptError("collector artifact changed while being read")
        return b"".join(chunks), "sha256:" + digest.hexdigest()
    finally:
        os.close(descriptor)


def native_checkpoint_inventory(run_dir: Path) -> set[str]:
    """Inventory the optional proof tree without following unbound symlinks."""
    root = _safe_path(run_dir / NATIVE_CHECKPOINT_PREFIX, run_dir=run_dir)
    if not root.is_dir():
        raise CollectorReceiptError("native checkpoint directory is missing")
    inventory: set[str] = set()
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in directories:
            _safe_path(Path(directory) / name, run_dir=run_dir)
        for name in files:
            path = _safe_path(Path(directory) / name, run_dir=run_dir)
            if not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
                raise CollectorReceiptError("native checkpoint artifact is not a regular file")
            inventory.add(path.relative_to(_absolute(run_dir)).as_posix())
    return inventory


def _verified_native_checkpoint(
    report: Mapping, *, run_dir: Path, since_utc: str, until_utc: str,
    clockify_evidence: bytes,
) -> dict[str, bytes]:
    # Only the digest-bound original report opts in. A stray optional directory
    # must never change a historical source identity or become new evidence.
    if "clockify_native_checkpoint" not in report:
        return {}
    try:
        metadata = report["clockify_native_checkpoint"]
        if not isinstance(metadata, Mapping) or set(metadata) != {"manifest_sha256", "request"}:
            raise ValueError("metadata schema")
        manifest_hash = metadata["manifest_sha256"]
        if not isinstance(manifest_hash, str) or re.fullmatch(r"[0-9a-f]{64}", manifest_hash) is None:
            raise ValueError("manifest hash")
        request = metadata["request"]
        if not isinstance(request, Mapping) or set(request) != {
            "workspace_id", "user_id", "since_utc", "until_utc",
        }:
            raise ValueError("request schema")
        if (
            _utc_string(request["since_utc"], "native checkpoint since") != since_utc
            or _utc_string(request["until_utc"], "native checkpoint until") != until_utc
        ):
            raise ValueError("period differs from completion slice")
        # Lazy import: the snapshot validator uses the receipt safe-read helpers.
        try:
            from scripts import clockify_checkpoint_snapshot
        except ModuleNotFoundError:  # direct script execution
            import clockify_checkpoint_snapshot  # type: ignore[no-redef]
        snapshot = clockify_checkpoint_snapshot.load_checkpoint_snapshot(
            run_dir / NATIVE_CHECKPOINT_PREFIX,
            workspace_id=request["workspace_id"], user_id=request["user_id"],
            since=datetime.fromisoformat(since_utc.replace("Z", "+00:00")),
            until=datetime.fromisoformat(until_utc.replace("Z", "+00:00")),
            expected_manifest_sha256=manifest_hash,
        )
        if snapshot is None:
            raise ValueError("snapshot is missing")
        artifacts = {
            NATIVE_CHECKPOINT_PREFIX + relative: content
            for relative, content in snapshot.verified_artifact_bytes.items()
        }
        if artifacts.get(NATIVE_CHECKPOINT_PREFIX + "clockify-existing.json") != clockify_evidence:
            raise ValueError("copied evidence differs byte-for-byte from source evidence")
        if native_checkpoint_inventory(run_dir) != set(artifacts):
            raise ValueError("artifact inventory differs")
        return artifacts
    except (OSError, TypeError, ValueError) as exc:
        raise CollectorReceiptError("collector source native checkpoint is invalid") from exc


def verified_run_native_checkpoint(run_dir: Path) -> tuple[dict, dict[str, bytes]]:
    """Read a sealed run's optional native basis without requiring all raw sources.

    Repair/replay historically carry the ledger, not every collector raw file.
    A metadata-free report keeps that compatibility; opting in requires its
    exact original completion binding before any destination can be created.
    """
    run_dir = _safe_path(Path(run_dir))
    try:
        report_content, report_digest = _safe_read_bytes_and_digest(
            _safe_path(run_dir / "run-report.json", run_dir=run_dir)
        )
        report = json.loads(report_content)
        if not isinstance(report, dict):
            raise ValueError("report shape")
        if "clockify_native_checkpoint" not in report:
            return report, {}
        bundle = load_completion_bundle(run_dir / "completion-bundle.json", run_dir=run_dir)
        expected_report_digest = next(
            artifact.digest for artifact in bundle.artifacts if artifact.kind == "run_report"
        )
        if report_digest != expected_report_digest:
            raise ValueError("report differs from completion binding")
        evidence, _digest = _safe_read_bytes_and_digest(
            _safe_path(run_dir / "evidence/clockify-existing.json", run_dir=run_dir)
        )
        artifacts = _verified_native_checkpoint(
            report, run_dir=run_dir, since_utc=bundle.since_utc, until_utc=bundle.until_utc,
            clockify_evidence=evidence,
        )
        return report, {"evidence/clockify-existing.json": evidence, **artifacts}
    except (OSError, TypeError, ValueError) as exc:
        raise CollectorReceiptError("run native checkpoint completion binding is invalid") from exc


def _digest_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", value):
        raise CollectorReceiptError(f"{label} must be a sha256 digest")
    return value


def _safe_identity(value: object, label: str) -> str:
    if not isinstance(value, str) or _SAFE_IDENTITY.fullmatch(value) is None:
        raise CollectorReceiptError(f"{label} must be a safe identity")
    return value


def _utc_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise CollectorReceiptError(f"{label} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise CollectorReceiptError(f"{label} must be a canonical UTC timestamp") from exc
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if canonical != value:
        raise CollectorReceiptError(f"{label} must be a canonical UTC timestamp")
    return value


@dataclass(frozen=True)
class FailureReceipt:
    source: str
    slice_id: str
    checkpoint_identity_digest: str
    failure_class: str
    retryable: bool
    resume_state_digest: str
    occurred_at: str
    receipt_digest: str

    def document(self) -> dict[str, object]:
        return {
            "source": self.source,
            "slice_id": self.slice_id,
            "checkpoint_identity_digest": self.checkpoint_identity_digest,
            "failure_class": self.failure_class,
            "retryable": self.retryable,
            "resume_state_digest": self.resume_state_digest,
            "occurred_at": self.occurred_at,
            "receipt_digest": self.receipt_digest,
        }


def failure_receipt(
    *, source: str, slice_id: str, checkpoint_identity_digest: str,
    failure_class: str, retryable: bool, resume_state_digest: str, occurred_at: str,
    cursor: object | None = None, credential: object | None = None,
) -> FailureReceipt:
    """Build a receipt while deliberately discarding unsafe caller details."""
    del cursor, credential
    unsigned = {
        "source": _safe_identity(source, "source"),
        "slice_id": _safe_identity(slice_id, "slice ID"),
        "checkpoint_identity_digest": _digest_string(checkpoint_identity_digest, "checkpoint identity"),
        "failure_class": _safe_identity(failure_class, "failure class"),
        "retryable": retryable,
        "resume_state_digest": _digest_string(resume_state_digest, "resume state"),
        "occurred_at": _utc_string(occurred_at, "occurred time"),
    }
    if not isinstance(retryable, bool):
        raise CollectorReceiptError("retryable must be a boolean")
    return FailureReceipt(**unsigned, receipt_digest=_digest(unsigned))


def _receipt_from_document(value: object) -> FailureReceipt:
    if not isinstance(value, dict) or set(value) != {
        "source", "slice_id", "checkpoint_identity_digest", "failure_class",
        "retryable", "resume_state_digest", "occurred_at", "receipt_digest",
    }:
        raise CollectorReceiptError("failure receipt schema is invalid")
    receipt = failure_receipt(
        source=value["source"], slice_id=value["slice_id"],
        checkpoint_identity_digest=value["checkpoint_identity_digest"],
        failure_class=value["failure_class"], retryable=value["retryable"],
        resume_state_digest=value["resume_state_digest"], occurred_at=value["occurred_at"],
    )
    if receipt.receipt_digest != value["receipt_digest"]:
        raise CollectorReceiptError("failure receipt digest does not match")
    return receipt


class FailureReceiptStore:
    """Append-only JSONL receipt journal with verified replay."""

    def __init__(self, path: Path):
        self.path = Path(path)

    def append(self, receipt: FailureReceipt) -> FailureReceipt:
        if not isinstance(receipt, FailureReceipt):
            raise CollectorReceiptError("receipt must be a FailureReceipt")
        _receipt_from_document(receipt.document())
        path = _safe_path(self.path)
        _reject_symlink_components(path.parent)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Never extend a malformed journal: append-only recovery starts only
        # from a fully validated history.
        self.load()
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            payload = _canonical(receipt.document()) + b"\n"
            written = 0
            while written < len(payload):
                progress = os.write(descriptor, payload[written:])
                if not isinstance(progress, int) or progress <= 0:
                    raise CollectorReceiptError("failure receipt journal write made no progress")
                written += progress
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return receipt

    def load(self) -> tuple[FailureReceipt, ...]:
        path = _safe_path(self.path)
        if not path.exists():
            return ()
        if not path.is_file() or path.is_symlink():
            raise CollectorReceiptError("failure receipt journal is unsafe")
        try:
            journal = _safe_read_text(path)
        except OSError as exc:
            raise CollectorReceiptError("failure receipt journal cannot be read") from exc
        if journal and not journal.endswith("\n"):
            raise CollectorReceiptError("failure receipt journal must end with a newline")
        lines = journal.splitlines()
        receipts = []
        for line in lines:
            if not line:
                raise CollectorReceiptError("failure receipt journal has a blank line")
            try:
                receipts.append(_receipt_from_document(json.loads(line)))
            except json.JSONDecodeError as exc:
                raise CollectorReceiptError("failure receipt journal is not valid JSONL") from exc
        return tuple(receipts)


@dataclass(frozen=True)
class SliceArtifact:
    kind: str
    path: Path
    digest: str

    def document(self) -> dict[str, str]:
        return {"kind": self.kind, "digest": self.digest}


@dataclass(frozen=True)
class SliceCompletionBundle:
    run_dir: Path
    slice_id: str
    since_utc: str
    until_utc: str
    source_coverage_digest: str
    runtime_identity_digest: str
    artifacts: tuple[SliceArtifact, ...]
    replay: bool
    bundle_digest: str

    def document(self) -> dict[str, object]:
        return {
            "schema_version": _BUNDLE_SCHEMA_VERSION,
            "slice_id": self.slice_id,
            "since_utc": self.since_utc,
            "until_utc": self.until_utc,
            "source_coverage_digest": self.source_coverage_digest,
            "runtime_identity_digest": self.runtime_identity_digest,
            "artifacts": [item.document() for item in self.artifacts],
            "replay": self.replay,
            "bundle_digest": self.bundle_digest,
        }


@dataclass(frozen=True)
class CollectorSourceBundle:
    """Raw collector identity that remains valid if derived artifacts later drift."""

    run_dir: Path
    slice_id: str
    since_utc: str
    until_utc: str
    source_coverage_digest: str
    collector_runtime_identity: dict[str, object]
    legacy_completion_bundle_digest: str
    source_bundle_digest: str
    verified_artifact_bytes: Mapping[str, bytes]
    verified_artifact_digests: Mapping[str, str]

    @property
    def bundle_digest(self) -> str:
        return self.source_bundle_digest

    @property
    def replay(self) -> bool:
        return False


@dataclass(frozen=True)
class FrozenSourceSnapshot:
    """Verified captured bytes, without claiming collector or backlog completion."""

    run_dir: Path
    report: Mapping[str, object]
    since_utc: str
    until_utc: str
    verified_artifact_bytes: Mapping[str, bytes]
    verified_artifact_digests: Mapping[str, str]


@dataclass(frozen=True)
class PendingCollectorSource:
    """Verified raw admission, explicitly not a historical completion receipt."""

    run_dir: Path
    slice_id: str
    since_utc: str
    until_utc: str
    source_coverage_digest: str
    collector_runtime_identity: dict[str, object]
    source_bundle_digest: str
    verified_artifact_bytes: Mapping[str, bytes]
    verified_artifact_digests: Mapping[str, str]
    pending_binding: Mapping[str, object]
    native_checkpoint_metadata: Mapping[str, object]

    @property
    def legacy_completion_bundle_digest(self) -> None:
        return None


def _historical_transport_projection(event: evidence_ledger.EvidenceEvent) -> evidence_ledger.EvidenceEvent:
    """Reproduce the one attested pre-transport-receipt event shape, never its code."""
    attributes = dict(event.attributes)
    if "transport_omitted_tool_content" not in attributes:
        return event
    if (
        event.source_type != "codex_sessions_event"
        or attributes.get("role") != "tool"
        or attributes.get("kind") != "tool_result"
        or attributes.get("content") not in (None, "")
    ):
        raise CollectorReceiptError("historical transport projection is not a tool result")
    attributes.pop("transport_omitted_tool_content")
    return evidence_ledger.evidence_event(
        event.source_type, event.source_ref, observed_at=event.observed_at,
        raw_source_span=event.raw_source_span, attributes=attributes,
        legacy_aliases=event.legacy_aliases,
    )


def _attested_historical_source(
    run_dir: Path, report: Mapping, ledger_digest: str, manifest_id: str,
    events_digest: str, verified_bytes: Mapping[str, bytes],
) -> Mapping:
    """Bind this exact old policy to the immutable stage receipt and read-only audit."""
    augmentation = report.get("source_augmentation")
    paths = report.get("paths")
    if (
        not isinstance(augmentation, Mapping)
        or augmentation.get("schema_version") != "offline-captured-mac-conservative-additive-union/v1"
        or augmentation.get("materializer_sha256") != _HISTORICAL_MATERIALIZER_SHA256
        or not isinstance(paths, Mapping)
        or not isinstance(paths.get("run_dir"), str)
    ):
        raise CollectorReceiptError("historical normalization profile is not attested")
    stage_run = _safe_path(Path(paths["run_dir"]))
    if stage_run.name != run_dir.name or stage_run.parent.name != "runs":
        raise CollectorReceiptError("historical stage source identity differs")
    stage_root = stage_run.parent.parent
    receipt_path = _safe_path(stage_root / "allhost-stage-result.json")
    receipt_bytes, receipt_digest = _safe_read_bytes_and_digest(receipt_path)
    if receipt_digest != "sha256:" + _HISTORICAL_STAGE_SHA256:
        raise CollectorReceiptError("historical stage receipt digest differs")
    receipt = json.loads(receipt_bytes)
    if (
        not isinstance(receipt, Mapping)
        or receipt.get("schema_version") != "offline-september-allhost-stage/v1"
        or receipt.get("runs_root") != str(stage_run.parent)
        or receipt.get("stage_only") is not True
        or receipt.get("network") is not False
        or receipt.get("inference") is not False
        or receipt.get("source_host_rereads") is not False
        or receipt.get("materializer_sha256") != _HISTORICAL_MATERIALIZER_SHA256
    ):
        raise CollectorReceiptError("historical stage receipt is invalid")
    modules = receipt.get("native_module_hashes")
    normalizers = [value for path, value in modules.items()
                   if Path(path).name == "evidence_ledger.py"] if isinstance(modules, Mapping) else []
    if normalizers != [_HISTORICAL_NORMALIZER_SHA256]:
        raise CollectorReceiptError("historical normalizer version is unknown")
    rows = receipt.get("results")
    matches = [row for row in rows if isinstance(row, Mapping)
               and row.get("source_run") == str(stage_run)] if isinstance(rows, list) else []
    if len(matches) != 1:
        raise CollectorReceiptError("historical stage source receipt is missing")
    row = matches[0]
    identity = row.get("ledger_identity")
    if (
        row.get("label") != run_dir.name.removeprefix("captured-mac-conservative-union-")
        or row.get("stage_only") is not True
        or row.get("completion_bundle_created") is not False
        or row.get("native_raw_projection_exact") is not True
        or row.get("native_checkpoint_consumer_validated") is not True
        or row.get("all_frozen_inputs_byte_identical") is not True
        or row.get("inference") is not False
        or not isinstance(identity, Mapping)
        or identity != {"file_sha256": ledger_digest, "manifest_id": manifest_id,
                        "events_digest": events_digest}
    ):
        raise CollectorReceiptError("historical stage ledger identity differs")
    audit_bytes, audit_digest = _safe_read_bytes_and_digest(_safe_path(_HISTORICAL_AUDIT_PATH))
    if audit_digest != "sha256:" + _HISTORICAL_AUDIT_SHA256:
        raise CollectorReceiptError("historical compatibility audit digest differs")
    audit = json.loads(audit_bytes)
    if (
        not isinstance(audit, Mapping)
        or audit.get("schema_version") != "read-only-normalization-compatibility-audit/v1"
        or audit.get("stage_receipt_sha256") != _HISTORICAL_STAGE_SHA256
        or audit.get("attested_historical_normalizer_sha256") != _HISTORICAL_NORMALIZER_SHA256
        or audit.get("all_periods_pass") is not True
    ):
        raise CollectorReceiptError("historical compatibility audit is invalid")
    audit_rows = audit.get("results")
    audited = [item for item in audit_rows if isinstance(item, Mapping)
               and item.get("period") == row["label"]] if isinstance(audit_rows, list) else []
    if len(audited) != 1 or audited[0].get("pass") is not True:
        raise CollectorReceiptError("historical source was not audited")
    for name, relative in {**_COLLECTOR_RAW_ARTIFACTS,
                           "enriched_context": "evidence/enriched-context.json"}.items():
        if relative not in verified_bytes:
            if name == "enriched_context":
                continue
            raise CollectorReceiptError("historical source raw artifact is missing")
        content = verified_bytes[relative]
        if (
            audited[0].get("raw_artifacts_sha256", {}).get(name) != hashlib.sha256(content).hexdigest()
            or _safe_read_bytes_and_digest(_safe_path(stage_run / relative, run_dir=stage_run))[0] != content
        ):
            raise CollectorReceiptError("historical source raw artifact differs")
    if audited[0].get("bound_ledger_file_sha256") != ledger_digest:
        raise CollectorReceiptError("historical source ledger differs from audit")
    return audited[0]


def _slice_utc(value: object, label: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CollectorReceiptError(f"slice {label} must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _artifact_path(run_dir: Path, kind: str) -> Path:
    relative = _ARTIFACT_PATHS.get(kind)
    if relative is None and kind == _REPLAY_ARTIFACT[0]:
        relative = _REPLAY_ARTIFACT[1]
    if relative is None:
        raise CollectorReceiptError("completion bundle artifact kind is invalid")
    return run_dir / relative


def _verified_artifact(run_dir: Path, kind: str, expected_digest: str | None = None) -> SliceArtifact:
    path = _safe_path(_artifact_path(run_dir, kind), run_dir=run_dir)
    if not path.is_file() or path.is_symlink():
        raise CollectorReceiptError(f"required {kind.replace('_', '-')} artifact is missing or unsafe")
    digest = _file_digest(path)
    if expected_digest is not None and digest != _digest_string(expected_digest, "artifact"):
        raise CollectorReceiptError(f"{kind.replace('_', '-')} artifact digest does not match")
    return SliceArtifact(kind, path.resolve(), digest)


def _report_utc(value: object) -> str:
    if not isinstance(value, str):
        raise CollectorReceiptError("run-report interval is invalid")
    if value.endswith("Z"):
        return _utc_string(value, "run-report interval")
    try:
        local = datetime.strptime(value, "%Y-%m-%d %H:%M").replace(tzinfo=_PERIOD_TIMEZONE)
    except ValueError as exc:
        raise CollectorReceiptError("run-report interval is invalid") from exc
    return _slice_utc(local, "run-report interval")


def _completion_identities(
    run_dir: Path, *, since_utc: str, until_utc: str,
) -> tuple[str, str]:
    report_path = _verified_artifact(run_dir, "run_report").path
    ledger_path = _verified_artifact(run_dir, "evidence_ledger").path
    try:
        report = json.loads(_safe_read_text(report_path))
        ledger_document = json.loads(_safe_read_text(ledger_path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectorReceiptError("completion identity artifact is not valid JSON") from exc
    if not isinstance(report, Mapping) or not isinstance(ledger_document, Mapping):
        raise CollectorReceiptError("completion identity artifact must be an object")
    return _completion_identities_from_documents(
        report, ledger_document, since_utc=since_utc, until_utc=until_utc
    )


def _completion_identities_from_documents(
    report: Mapping[str, object], ledger_document: Mapping[str, object], *,
    since_utc: str, until_utc: str,
) -> tuple[str, str]:
    date_range = report.get("date_range")
    if not isinstance(date_range, Mapping) or (
        _report_utc(date_range.get("since")) != since_utc
        or _report_utc(date_range.get("until")) != until_utc
    ):
        raise CollectorReceiptError("run-report interval does not match completion slice")
    reported_ledger = report.get("evidence_ledger")
    coverage = reported_ledger.get("source_completeness") if isinstance(reported_ledger, Mapping) else None
    manifest = ledger_document.get("manifest")
    canonical_coverage = manifest.get("source_completeness") if isinstance(manifest, Mapping) else None
    runtime = report.get("runtime_identity")
    if not isinstance(coverage, Mapping) or not isinstance(canonical_coverage, Mapping) or not isinstance(runtime, Mapping):
        raise CollectorReceiptError("run-report has no completion identities")
    if dict(coverage) != dict(canonical_coverage):
        raise CollectorReceiptError("run-report coverage does not match evidence ledger")
    return _digest(dict(canonical_coverage)), _digest(dict(runtime))


def _bundle_unsigned(
    *, slice_id: str, since_utc: str, until_utc: str, source_coverage_digest: str,
    runtime_identity_digest: str, artifacts: tuple[SliceArtifact, ...], replay: bool,
) -> dict[str, object]:
    return {
        "schema_version": _BUNDLE_SCHEMA_VERSION,
        "slice_id": _safe_identity(slice_id, "slice ID"),
        "since_utc": _utc_string(since_utc, "slice since"),
        "until_utc": _utc_string(until_utc, "slice until"),
        "source_coverage_digest": _digest_string(source_coverage_digest, "source coverage"),
        "runtime_identity_digest": _digest_string(runtime_identity_digest, "runtime identity"),
        "artifacts": [artifact.document() for artifact in artifacts],
        "replay": replay,
    }


def build_completion_bundle(run_dir: Path, *, slice_: object, replay: bool = False) -> SliceCompletionBundle:
    run_dir = _safe_path(Path(run_dir))
    if not run_dir.is_dir() or run_dir.is_symlink():
        raise CollectorReceiptError("completion bundle run directory is missing or unsafe")
    slice_id = _safe_identity(getattr(slice_, "slice_id", None), "slice ID")
    since_utc = _slice_utc(getattr(slice_, "since", None), "since")
    until_utc = _slice_utc(getattr(slice_, "until", None), "until")
    if since_utc >= until_utc:
        raise CollectorReceiptError("completion bundle slice interval is invalid")
    source_coverage_digest, runtime_identity_digest = _completion_identities(
        run_dir, since_utc=since_utc, until_utc=until_utc,
    )
    kinds = [*_ARTIFACT_PATHS]
    if replay:
        kinds.append(_REPLAY_ARTIFACT[0])
    artifacts = tuple(_verified_artifact(run_dir, kind) for kind in kinds)
    unsigned = _bundle_unsigned(
        slice_id=slice_id, since_utc=since_utc, until_utc=until_utc,
        source_coverage_digest=source_coverage_digest, runtime_identity_digest=runtime_identity_digest,
        artifacts=artifacts, replay=replay,
    )
    return SliceCompletionBundle(
        run_dir.resolve(), slice_id, since_utc, until_utc,
        unsigned["source_coverage_digest"], unsigned["runtime_identity_digest"], artifacts,
        replay, _digest(unsigned),
    )


def verify_completion_bundle(bundle: SliceCompletionBundle) -> SliceCompletionBundle:
    if not isinstance(bundle, SliceCompletionBundle):
        raise CollectorReceiptError("completion bundle is invalid")
    expected_kinds = set(REQUIRED_KINDS)
    if bundle.replay:
        expected_kinds.add(_REPLAY_ARTIFACT[0])
    if {artifact.kind for artifact in bundle.artifacts} != expected_kinds or len(bundle.artifacts) != len(expected_kinds):
        raise CollectorReceiptError("completion bundle required artifact kinds do not match")
    verified = tuple(_verified_artifact(bundle.run_dir, artifact.kind, artifact.digest) for artifact in bundle.artifacts)
    source_coverage_digest, runtime_identity_digest = _completion_identities(
        bundle.run_dir, since_utc=bundle.since_utc, until_utc=bundle.until_utc,
    )
    if (
        source_coverage_digest != bundle.source_coverage_digest
        or runtime_identity_digest != bundle.runtime_identity_digest
    ):
        raise CollectorReceiptError("completion bundle identity digest does not match")
    unsigned = _bundle_unsigned(
        slice_id=bundle.slice_id, since_utc=bundle.since_utc, until_utc=bundle.until_utc,
        source_coverage_digest=bundle.source_coverage_digest,
        runtime_identity_digest=bundle.runtime_identity_digest,
        artifacts=verified, replay=bundle.replay,
    )
    if _digest(unsigned) != _digest_string(bundle.bundle_digest, "completion bundle"):
        raise CollectorReceiptError("completion bundle digest does not match")
    return bundle


def completion_coverage(bundle: SliceCompletionBundle) -> dict[str, object]:
    """Return coverage only after its bound run-report and ledger have verified."""
    verify_completion_bundle(bundle)
    ledger_path = _verified_artifact(bundle.run_dir, "evidence_ledger").path
    try:
        ledger_document = json.loads(_safe_read_text(ledger_path))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectorReceiptError("evidence ledger is not valid JSON") from exc
    manifest = ledger_document.get("manifest") if isinstance(ledger_document, Mapping) else None
    coverage = manifest.get("source_completeness") if isinstance(manifest, Mapping) else None
    if not isinstance(coverage, Mapping) or _digest(dict(coverage)) != bundle.source_coverage_digest:
        raise CollectorReceiptError("evidence ledger coverage digest does not match")
    return dict(coverage)


def write_completion_bundle(path: Path, bundle: SliceCompletionBundle) -> None:
    verify_completion_bundle(bundle)
    path = _safe_path(Path(path), run_dir=bundle.run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        payload = _canonical(bundle.document()) + b"\n"
        written = 0
        while written < len(payload):
            progress = os.write(descriptor, payload[written:])
            if not isinstance(progress, int) or progress <= 0:
                raise CollectorReceiptError("completion bundle write made no progress")
            written += progress
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, path)
        parent_descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def load_completion_bundle(path: Path, *, run_dir: Path) -> SliceCompletionBundle:
    try:
        document = json.loads(_safe_read_text(_safe_path(Path(path), run_dir=run_dir)))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectorReceiptError("completion bundle document is invalid") from exc
    if not isinstance(document, dict) or set(document) != {
        "schema_version", "slice_id", "since_utc", "until_utc", "source_coverage_digest",
        "runtime_identity_digest", "artifacts", "replay", "bundle_digest",
    } or document["schema_version"] != _BUNDLE_SCHEMA_VERSION:
        raise CollectorReceiptError("completion bundle document schema is invalid")
    raw_artifacts = document["artifacts"]
    if not isinstance(raw_artifacts, list):
        raise CollectorReceiptError("completion bundle artifacts are invalid")
    artifacts = []
    for item in raw_artifacts:
        if not isinstance(item, dict) or set(item) != {"kind", "digest"}:
            raise CollectorReceiptError("completion bundle artifact schema is invalid")
        kind = item["kind"]
        artifacts.append(SliceArtifact(kind, _artifact_path(Path(run_dir), kind), _digest_string(item["digest"], "artifact")))
    bundle = SliceCompletionBundle(
        Path(run_dir).resolve(), document["slice_id"], document["since_utc"], document["until_utc"],
        document["source_coverage_digest"], document["runtime_identity_digest"], tuple(artifacts),
        document["replay"], document["bundle_digest"],
    )
    return verify_completion_bundle(bundle)


def load_collector_source_bundle(path: Path, *, run_dir: Path) -> CollectorSourceBundle:
    """Verify only collector-owned raw artifacts from a historical completion bundle."""
    run_dir = _safe_path(Path(run_dir))
    bundle_path = _safe_path(Path(path), run_dir=run_dir)
    try:
        document = json.loads(_safe_read_text(bundle_path))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectorReceiptError("collector source completion document is invalid") from exc
    expected_keys = {
        "schema_version", "slice_id", "since_utc", "until_utc",
        "source_coverage_digest", "runtime_identity_digest", "artifacts", "replay",
        "bundle_digest",
    }
    if (
        not isinstance(document, dict)
        or set(document) != expected_keys
        or document.get("schema_version") != _BUNDLE_SCHEMA_VERSION
        or document.get("replay") is not False
    ):
        raise CollectorReceiptError("collector source completion schema is invalid")
    raw_artifacts = document.get("artifacts")
    if not isinstance(raw_artifacts, list):
        raise CollectorReceiptError("collector source completion artifacts are invalid")
    artifact_digests: dict[str, str] = {}
    artifacts: list[SliceArtifact] = []
    for item in raw_artifacts:
        if not isinstance(item, dict) or set(item) != {"kind", "digest"}:
            raise CollectorReceiptError("collector source completion artifact schema is invalid")
        kind = item.get("kind")
        if not isinstance(kind, str) or kind in artifact_digests:
            raise CollectorReceiptError("collector source completion artifact identity is invalid")
        digest = _digest_string(item.get("digest"), "artifact")
        artifact_digests[kind] = digest
        artifacts.append(SliceArtifact(kind, _artifact_path(run_dir, kind), digest))
    if set(artifact_digests) != REQUIRED_KINDS:
        raise CollectorReceiptError("collector source completion artifact set is invalid")
    unsigned = _bundle_unsigned(
        slice_id=document.get("slice_id"),
        since_utc=document.get("since_utc"),
        until_utc=document.get("until_utc"),
        source_coverage_digest=document.get("source_coverage_digest"),
        runtime_identity_digest=document.get("runtime_identity_digest"),
        artifacts=tuple(artifacts),
        replay=False,
    )
    legacy_digest = _digest_string(document.get("bundle_digest"), "completion bundle")
    if _digest(unsigned) != legacy_digest:
        raise CollectorReceiptError("collector source completion digest does not match")
    verified_bytes: dict[str, bytes] = {}
    verified_digests: dict[str, str] = {}
    for kind in ("run_report", "evidence_ledger"):
        artifact_path = _safe_path(_artifact_path(run_dir, kind), run_dir=run_dir)
        try:
            content, digest = _safe_read_bytes_and_digest(artifact_path)
        except OSError as exc:
            raise CollectorReceiptError(
                f"collector source {kind} artifact is missing or unsafe"
            ) from exc
        if digest != artifact_digests[kind]:
            raise CollectorReceiptError(f"collector source {kind} artifact drifted")
        relative = str(artifact_path.relative_to(run_dir))
        verified_bytes[relative] = content
        verified_digests[relative] = digest
    try:
        report = json.loads(verified_bytes["run-report.json"])
        ledger_document = json.loads(verified_bytes["evidence/evidence-ledger.json"])
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CollectorReceiptError("collector source identity artifact is invalid") from exc
    if not isinstance(report, Mapping) or not isinstance(ledger_document, Mapping):
        raise CollectorReceiptError("collector source identity artifact must be an object")
    coverage_digest, runtime_digest = _completion_identities_from_documents(
        report, ledger_document,
        since_utc=str(document["since_utc"]),
        until_utc=str(document["until_utc"]),
    )
    if (
        coverage_digest != document["source_coverage_digest"]
        or runtime_digest != document["runtime_identity_digest"]
    ):
        raise CollectorReceiptError("collector source identities drifted")
    runtime = report.get("runtime_identity") if isinstance(report, Mapping) else None
    if not isinstance(runtime, Mapping):
        raise CollectorReceiptError("collector source runtime identity is invalid")
    try:
        manifest_document = ledger_document.get("manifest")
        events_document = ledger_document.get("events")
        if not isinstance(manifest_document, Mapping) or not isinstance(events_document, list):
            raise ValueError("ledger shape")
        bound_manifest = evidence_ledger.LedgerManifest.from_document(manifest_document)
        bound = evidence_ledger.EvidenceLedger(
            tuple(evidence_ledger.EvidenceEvent.from_document(item) for item in events_document),
            bound_manifest.source_inventory,
            bound_manifest.timezone,
            bound_manifest.member_identities,
        )
        bound.validate(bound_manifest)
        raw: dict[str, object] = {}
        raw_digests: dict[str, str] = {}
        raw_artifacts = dict(_COLLECTOR_RAW_ARTIFACTS)
        enriched_path = run_dir / "evidence/enriched-context.json"
        if enriched_path.exists() or enriched_path.is_symlink():
            raw_artifacts["enriched_context"] = "evidence/enriched-context.json"
        for key, relative in raw_artifacts.items():
            raw_path = _safe_path(run_dir / relative, run_dir=run_dir)
            content, digest = _safe_read_bytes_and_digest(raw_path)
            raw[key] = json.loads(content)
            raw_digests[relative] = digest
            verified_bytes[relative] = content
            verified_digests[relative] = digest
        reconstructed = evidence_ledger.EvidenceLedger(
            tuple(evidence_ledger.normalize_collector_snapshot(raw)),
            evidence_ledger.source_inventory_from_collector(raw),
            bound.timezone,
            bound.member_identities,
        )
        if reconstructed.manifest.document() != bound.manifest.document():
            raise ValueError("manifest mismatch")
        if "enriched_context" in raw:
            legacy_raw = {key: value for key, value in raw.items() if key != "enriched_context"}
            legacy_reconstructed = evidence_ledger.EvidenceLedger(
                tuple(evidence_ledger.normalize_collector_snapshot(legacy_raw)),
                evidence_ledger.source_inventory_from_collector(legacy_raw),
                bound.timezone,
                bound.member_identities,
            )
            if legacy_reconstructed.manifest.document() == bound.manifest.document():
                # The optional file contributed no ledger events. Preserve the
                # identity of historical derivations sealed before it was read.
                relative = "evidence/enriched-context.json"
                raw_digests.pop(relative)
                verified_bytes.pop(relative)
                verified_digests.pop(relative)
    except (
        OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError,
    ) as exc:
        raise CollectorReceiptError(
            "collector source raw evidence does not match its bound ledger"
        ) from exc
    native_artifacts = _verified_native_checkpoint(
        report, run_dir=run_dir, since_utc=str(document["since_utc"]),
        until_utc=str(document["until_utc"]),
        clockify_evidence=verified_bytes["evidence/clockify-existing.json"],
    )
    for relative, content in native_artifacts.items():
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        verified_bytes[relative] = content
        verified_digests[relative] = digest
        raw_digests[relative] = digest
    source_unsigned = {
        "schema_version": "collector-source-bundle/v1",
        "legacy_completion_bundle_digest": legacy_digest,
        "slice_id": document["slice_id"],
        "since_utc": document["since_utc"],
        "until_utc": document["until_utc"],
        "source_coverage_digest": coverage_digest,
        "collector_runtime_identity": dict(runtime),
        "raw_artifact_digests": {
            **{
                kind: artifact_digests[kind]
                for kind in ("run_report", "evidence_ledger")
            },
            **dict(sorted(raw_digests.items())),
        },
    }
    return CollectorSourceBundle(
        run_dir=run_dir.resolve(),
        slice_id=_safe_identity(document["slice_id"], "slice ID"),
        since_utc=_utc_string(document["since_utc"], "slice since"),
        until_utc=_utc_string(document["until_utc"], "slice until"),
        source_coverage_digest=coverage_digest,
        collector_runtime_identity=dict(runtime),
        legacy_completion_bundle_digest=legacy_digest,
        source_bundle_digest=_digest(source_unsigned),
        verified_artifact_bytes=dict(verified_bytes),
        verified_artifact_digests=dict(verified_digests),
    )


def load_pending_collector_source(run_dir: Path, *, checkpoint_root: Path) -> PendingCollectorSource:
    """Admit exact complete raw bytes and existing native proof without any writes."""
    from scripts import clockify_sync_collect as collector, collector_slices as slices
    from scripts import collector_checkpoints as checkpoints, clockify_checkpoint_snapshot as native
    from scripts import reconciliation_manifest

    run_dir = _safe_path(Path(run_dir))
    checkpoint_root = _safe_path(Path(checkpoint_root))
    if (run_dir / "completion-bundle.json").exists() or (run_dir / "completion-bundle.json").is_symlink():
        raise CollectorReceiptError("pending source must not have a completion bundle")
    contents: dict[str, bytes] = {}
    digests: dict[str, str] = {}

    def read(relative: str) -> bytes:
        content, digest = _safe_read_bytes_and_digest(_safe_path(run_dir / relative, run_dir=run_dir))
        contents[relative], digests[relative] = content, digest
        return content

    try:
        pending = json.loads(read("slice-finalization.json"))
        if not isinstance(pending, dict) or set(pending) != {
            "schema_version", "backlog_identity", "slice_id", "since_utc", "until_utc",
        } or pending["schema_version"] != "collector-slice-finalization/v1":
            raise ValueError("pending schema differs")
        identity = slices.BacklogIdentity(**pending["backlog_identity"])
        planned = slices.plan_slices(
            datetime.fromisoformat(identity.since_utc.replace("Z", "+00:00")),
            datetime.fromisoformat(identity.until_utc.replace("Z", "+00:00")),
            zone=ZoneInfo(identity.timezone), max_days=identity.max_days,
        )
        slice_ = next(item for item in planned if item.slice_id == pending["slice_id"])
        since, until = slice_.since, slice_.until
        if collector.iso_utc(since) != pending["since_utc"] or collector.iso_utc(until) != pending["until_utc"]:
            raise ValueError("pending bounds differ")
        backlog = slices.BacklogStore(checkpoint_root).read_existing(identity, planned)
        if any(item.slice_id == slice_.slice_id for item in backlog.completed):
            raise ValueError("pending slice already has a completion receipt")
        backlog_path = _safe_path(backlog.directory / "backlog-manifest.json", run_dir=checkpoint_root)
        backlog_bytes, backlog_digest = _safe_read_bytes_and_digest(backlog_path)
        # Verify the exact opened bytes as well as the read_existing contract.
        opened_backlog = slices.BacklogStore(checkpoint_root)._state_from_manifest(identity, tuple(planned), backlog.directory, json.loads(backlog_bytes))
        if opened_backlog != backlog:
            raise ValueError("pending backlog changed during validation")
        report = json.loads(read("run-report.json"))
        if not isinstance(report, dict):
            raise ValueError("pending report is not an object")
        read("run-report.md")
        period = reconciliation_manifest.ReconciliationManifest.from_document(json.loads(read("period-manifest.json")))
        original_inputs = {"period-manifest.json": digests["period-manifest.json"]}
        for name in ("routing.json", "review-corrections.jsonl", "review-acceptance.jsonl"):
            read(name)
            original_inputs[name] = digests[name]
        if not period.identity.since <= since < until <= period.identity.until:
            raise ValueError("pending period bounds differ")
        ledger_doc = json.loads(read("evidence/evidence-ledger.json"))
        manifest = evidence_ledger.LedgerManifest.from_document(ledger_doc["manifest"])
        ledger = evidence_ledger.EvidenceLedger(
            tuple(evidence_ledger.EvidenceEvent.from_document(item) for item in ledger_doc["events"]),
            manifest.source_inventory, manifest.timezone, manifest.member_identities,
        )
        ledger.validate(manifest)
        coverage = manifest.document()["source_completeness"]
        if coverage["status"] != "complete" or coverage["incomplete_sources"] != []:
            raise ValueError("pending raw coverage is incomplete")
        raw = {key: json.loads(read(relative)) for key, relative in _COLLECTOR_RAW_ARTIFACTS.items()}
        if (run_dir / "evidence/enriched-context.json").exists() or (run_dir / "evidence/enriched-context.json").is_symlink():
            raw["enriched_context"] = json.loads(read("evidence/enriched-context.json"))
        reconstructed = evidence_ledger.EvidenceLedger(
            tuple(evidence_ledger.normalize_collector_snapshot(raw)), evidence_ledger.source_inventory_from_collector(raw),
            manifest.timezone, manifest.member_identities,
        )
        if reconstructed.manifest.document() != manifest.document():
            raise ValueError("pending raw reconstruction differs")
        mode = report.get("collection_mode", {})
        if not isinstance(mode, dict):
            raise ValueError("pending collection mode is not an object")
        collector._verified_existing_slice_bundle(
            run_dir, since, until, report["date_range"]["reason"],
            calendly_optional=mode.get("calendly_optional") is True,
            coordinator=mode.get("coordinator") or "omarchy-precision",
        )
        coverage_digest, _ = _completion_identities_from_documents(report, ledger_doc,
            since_utc=pending["since_utc"], until_utc=pending["until_utc"])
        ci, request = native._request(period.identity.workspace_id, period.identity.member_id, since, until)
        page_store = checkpoints.PageCheckpointStore(backlog.directory / "source-checkpoints")
        directory = page_store._directory_for(ci)
        checkpoint_path = _safe_path(directory / "manifest.json", run_dir=checkpoint_root)
        page_files = {"manifest.json": native._read(checkpoint_path)}
        page_manifest = native._document(page_files["manifest.json"])
        if page_manifest.get("identity") != ci.document():
            raise ValueError("pending native identity differs")
        for i, reference in enumerate(page_manifest["pages"], 1):
            relative = f"pages/{i:06d}.json"
            if reference.get("path") != relative:
                raise ValueError("pending native locator differs")
            page_files[relative] = native._read(directory / relative)
        evidence = contents["evidence/clockify-existing.json"]
        entries, observed, _ = native._validate(directory, page_files, evidence,
            identity=ci, request=request, since=since, until=until)
        checkpoint_relative = f"checkpoint/{directory.name}"
        native_files = {f"{checkpoint_relative}/{name}": value for name, value in page_files.items()}
        native_files["clockify-existing.json"] = evidence
        proof = dict(schema_version=native.SCHEMA_VERSION, request=request, checkpoint_identity=ci.document(),
            snapshot_at=observed, entry_count=len(entries), page_count=len(page_manifest["pages"]),
            source_checkpoint_manifest=str(checkpoint_path), source_clockify_evidence=str(run_dir / "evidence/clockify-existing.json"),
            files={name: native._hash(value) for name, value in native_files.items()})
        proof_bytes = checkpoints._canonical(proof) + b"\n"
        native_files["snapshot.json"] = proof_bytes
        native_metadata = {"manifest_sha256": native._hash(proof_bytes), "request": request}
        if "clockify_native_checkpoint" in report:
            original_native = _verified_native_checkpoint(report, run_dir=run_dir,
                since_utc=pending["since_utc"], until_utc=pending["until_utc"], clockify_evidence=evidence)
            if report["clockify_native_checkpoint"]["request"] != request:
                raise ValueError("pending copied native request differs")
            for name, content in original_native.items():
                relative = name.removeprefix(NATIVE_CHECKPOINT_PREFIX)
                if relative != "snapshot.json" and native_files.get(relative) != content:
                    raise ValueError("pending copied native pages differ from original checkpoint")
                contents[name], digests[name] = content, "sha256:" + native._hash(content)
            native_files = {name.removeprefix(NATIVE_CHECKPOINT_PREFIX): content for name, content in original_native.items()}
            native_metadata = dict(report["clockify_native_checkpoint"])
        binding = {
            "schema_version": "pending-collector-source/v1", "source_run_dir": str(run_dir),
            "checkpoint_root": str(checkpoint_root), "backlog_manifest_path": str(backlog_path),
            "backlog_manifest_digest": backlog_digest, "original_snapshot_digests": original_inputs,
            "original_artifact_digests": dict(sorted(digests.items())),
            "native_checkpoint_digests": {name: "sha256:" + native._hash(value) for name, value in sorted(page_files.items())},
        }
        # Existing derivation consumes raw artifacts only; original snapshots and
        # pending metadata are bound separately, never installed as child completion.
        for name in (*original_inputs, "slice-finalization.json", "run-report.md"):
            contents.pop(name)
            digests.pop(name)
        for name, value in native_files.items():
            relative = NATIVE_CHECKPOINT_PREFIX + name
            contents[relative], digests[relative] = value, "sha256:" + native._hash(value)
        return PendingCollectorSource(run_dir, slice_.slice_id, pending["since_utc"], pending["until_utc"],
            coverage_digest, dict(report["runtime_identity"]), _digest(binding), contents, digests, binding,
            native_metadata)
    except (OSError, ValueError, TypeError, KeyError, StopIteration) as exc:
        raise CollectorReceiptError("pending collector source proof is invalid") from exc


def verify_frozen_source(run_dir: Path) -> FrozenSourceSnapshot:
    """Prove captured raw bytes and their ledger without claiming live collection."""
    run_dir = _safe_path(Path(run_dir))
    if not run_dir.is_dir() or run_dir.is_symlink():
        raise CollectorReceiptError("frozen source directory is missing or unsafe")
    verified_bytes: dict[str, bytes] = {}
    verified_digests: dict[str, str] = {}

    def read(relative: str) -> bytes:
        path = _safe_path(run_dir / relative, run_dir=run_dir)
        content, digest = _safe_read_bytes_and_digest(path)
        verified_bytes[relative] = content
        verified_digests[relative] = digest
        return content

    try:
        report = json.loads(read("run-report.json"))
        ledger_document = json.loads(read("evidence/evidence-ledger.json"))
        if not isinstance(report, dict) or not isinstance(ledger_document, dict):
            raise ValueError("report or ledger is not an object")
        augmentation = report.get("source_augmentation")
        if (
            report.get("run_id") != run_dir.name
            or not isinstance(augmentation, Mapping)
            or augmentation.get("original_collection_not_reperformed") is not True
            or "clockify_native_checkpoint" not in report
        ):
            raise ValueError("frozen capture provenance is missing")
        date_range = report.get("date_range")
        if not isinstance(date_range, Mapping):
            raise ValueError("frozen period is missing")
        since_utc = _report_utc(date_range.get("since"))
        until_utc = _report_utc(date_range.get("until"))
        if since_utc >= until_utc:
            raise ValueError("frozen period is invalid")
        _completion_identities_from_documents(
            report, ledger_document, since_utc=since_utc, until_utc=until_utc,
        )
        reported_ledger = report.get("evidence_ledger")
        manifest_document = ledger_document.get("manifest")
        events_document = ledger_document.get("events")
        if (
            not isinstance(reported_ledger, Mapping)
            or not isinstance(manifest_document, Mapping)
            or not isinstance(events_document, list)
        ):
            raise ValueError("frozen ledger shape is invalid")
        manifest = evidence_ledger.LedgerManifest.from_document(manifest_document)
        bound = evidence_ledger.EvidenceLedger(
            tuple(evidence_ledger.EvidenceEvent.from_document(item) for item in events_document),
            manifest.source_inventory, manifest.timezone, manifest.member_identities,
        )
        bound.validate(manifest)
        if (
            reported_ledger.get("manifest_id") != manifest.manifest_id
            or reported_ledger.get("events_digest") != manifest.events_digest
            or reported_ledger.get("event_count") != manifest.event_count
            or reported_ledger.get("ledger_digest") != verified_digests["evidence/evidence-ledger.json"]
        ):
            raise ValueError("frozen run report ledger binding differs")
        raw: dict[str, object] = {}
        for name, relative in _COLLECTOR_RAW_ARTIFACTS.items():
            raw[name] = json.loads(read(relative))
        enriched = run_dir / "evidence/enriched-context.json"
        if enriched.exists() or enriched.is_symlink():
            raw["enriched_context"] = json.loads(read("evidence/enriched-context.json"))
        current_events = tuple(evidence_ledger.normalize_collector_snapshot(raw))
        reconstructed = evidence_ledger.EvidenceLedger(
            current_events,
            evidence_ledger.source_inventory_from_collector(raw),
            manifest.timezone, manifest.member_identities,
        )
        if reconstructed.manifest.document() != manifest.document():
            audited = _attested_historical_source(
                run_dir, report,
                verified_digests["evidence/evidence-ledger.json"].removeprefix("sha256:"),
                manifest.manifest_id, manifest.events_digest, verified_bytes,
            )
            projected = evidence_ledger.EvidenceLedger(
                tuple(_historical_transport_projection(event) for event in current_events),
                evidence_ledger.source_inventory_from_collector(raw),
                manifest.timezone, manifest.member_identities,
            )
            if (
                projected.manifest.document() != manifest.document()
                or [event.document() for event in projected.events] != events_document
                or audited.get("bound_manifest_sha256") != evidence_ledger.sha256_hex(manifest.document())
                or audited.get("bound_events_sha256") != evidence_ledger.sha256_hex(events_document)
            ):
                raise ValueError("frozen historical raw evidence does not match bound ledger")
            try:
                from scripts import work_accounting_pipeline
            except ModuleNotFoundError:  # direct script execution
                import work_accounting_pipeline  # type: ignore[no-redef]
            old_retained, _old_noise = work_accounting_pipeline._analysis_events(
                events_document, frozenset(manifest.member_identities),
            )
            new_retained, _new_noise = work_accounting_pipeline._analysis_events(
                [event.document() for event in reconstructed.events],
                frozenset(manifest.member_identities),
            )
            if (
                old_retained != new_retained
                or audited.get("retained_analysis_sha256") != evidence_ledger.sha256_hex(old_retained)
            ):
                raise ValueError("frozen historical normalization changed retained analysis")
        native = _verified_native_checkpoint(
            report, run_dir=run_dir, since_utc=since_utc, until_utc=until_utc,
            clockify_evidence=verified_bytes["evidence/clockify-existing.json"],
        )
        for relative, content in native.items():
            verified_bytes[relative] = content
            verified_digests[relative] = "sha256:" + hashlib.sha256(content).hexdigest()
        read("run-report.md")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise CollectorReceiptError("frozen source raw evidence or provenance is invalid") from exc
    return FrozenSourceSnapshot(
        run_dir, report, since_utc, until_utc, verified_bytes, verified_digests,
    )
