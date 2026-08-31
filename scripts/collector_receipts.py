"""Sanitized failure receipts and digest-bound downstream completion bundles."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Mapping


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
_BUNDLE_SCHEMA_VERSION = "collector-completion-bundle/v1"


def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, _canonical(receipt.document()) + b"\n")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return receipt

    def load(self) -> tuple[FailureReceipt, ...]:
        if not self.path.exists():
            return ()
        if not self.path.is_file() or self.path.is_symlink():
            raise CollectorReceiptError("failure receipt journal is unsafe")
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise CollectorReceiptError("failure receipt journal cannot be read") from exc
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
    path = _artifact_path(run_dir, kind)
    if not path.is_file() or path.is_symlink():
        raise CollectorReceiptError(f"required {kind.replace('_', '-')} artifact is missing or unsafe")
    digest = _file_digest(path)
    if expected_digest is not None and digest != _digest_string(expected_digest, "artifact"):
        raise CollectorReceiptError(f"{kind.replace('_', '-')} artifact digest does not match")
    return SliceArtifact(kind, path.resolve(), digest)


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
    run_dir = Path(run_dir)
    if not run_dir.is_dir() or run_dir.is_symlink():
        raise CollectorReceiptError("completion bundle run directory is missing or unsafe")
    slice_id = _safe_identity(getattr(slice_, "slice_id", None), "slice ID")
    since_utc = _slice_utc(getattr(slice_, "since", None), "since")
    until_utc = _slice_utc(getattr(slice_, "until", None), "until")
    if since_utc >= until_utc:
        raise CollectorReceiptError("completion bundle slice interval is invalid")
    try:
        report = json.loads((run_dir / "run-report.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectorReceiptError("run-report artifact is not valid JSON") from exc
    if not isinstance(report, Mapping):
        raise CollectorReceiptError("run-report artifact must be an object")
    ledger = report.get("evidence_ledger")
    runtime = report.get("runtime_identity")
    coverage = ledger.get("source_completeness") if isinstance(ledger, Mapping) else None
    if not isinstance(coverage, Mapping) or not isinstance(runtime, Mapping):
        raise CollectorReceiptError("run-report has no completion identities")
    kinds = [*_ARTIFACT_PATHS]
    if replay:
        kinds.append(_REPLAY_ARTIFACT[0])
    artifacts = tuple(_verified_artifact(run_dir, kind) for kind in kinds)
    unsigned = _bundle_unsigned(
        slice_id=slice_id, since_utc=since_utc, until_utc=until_utc,
        source_coverage_digest=_digest(dict(coverage)), runtime_identity_digest=_digest(dict(runtime)),
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
    unsigned = _bundle_unsigned(
        slice_id=bundle.slice_id, since_utc=bundle.since_utc, until_utc=bundle.until_utc,
        source_coverage_digest=bundle.source_coverage_digest,
        runtime_identity_digest=bundle.runtime_identity_digest,
        artifacts=verified, replay=bundle.replay,
    )
    if _digest(unsigned) != _digest_string(bundle.bundle_digest, "completion bundle"):
        raise CollectorReceiptError("completion bundle digest does not match")
    return bundle


def write_completion_bundle(path: Path, bundle: SliceCompletionBundle) -> None:
    verify_completion_bundle(bundle)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(_canonical(bundle.document()) + b"\n")
    os.replace(temporary, path)


def load_completion_bundle(path: Path, *, run_dir: Path) -> SliceCompletionBundle:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
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
