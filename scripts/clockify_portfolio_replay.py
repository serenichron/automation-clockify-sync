#!/usr/bin/env python3
"""Seal and verify a complete local Clockify portfolio replay.

This is intentionally a file-only verifier.  It neither calls a provider nor
changes a review.  A seal is permitted only after the portfolio quality gate
passes; a replay integrity receipt is written only for an exact normalized
match.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Mapping

try:
    from scripts import clockify_portfolio_quality, meeting_reconciliation
except ImportError:  # pragma: no cover - direct execution fallback
    import clockify_portfolio_quality  # type: ignore[no-redef]
    import meeting_reconciliation  # type: ignore[no-redef]


VOLATILE_KEYS = frozenset({
    "path", "paths", "run_dir", "source_run_dir", "replay_run_dir", "run_id",
    "replay_of_run_id", "created_at", "updated_at", "generated_at", "timestamp",
    "started_at", "completed_at", "cache_hits", "cache_misses", "hit_count", "hits",
})
RUN_ARTIFACTS = {
    "immutable_ledger": "evidence/evidence-ledger.json",
    "semantic_analysis": "semantic-analysis.json",
    "work_accounting_result": "work-accounting-result.json",
    "proposals": "proposals.json",
    "fathom_reconciliation": "fathom-reconciliation.json",
}
_SHA256 = re.compile(r"^sha256:[a-f0-9]{64}$")
_MEETING_IDENTITY_FIELDS = frozenset({
    "meeting_reconciliation_digest",
    "meeting_dedup_version",
    "meeting_dedup_tolerance_seconds",
    "meeting_split_digest",
})


class PortfolioReplayError(RuntimeError):
    pass


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def _normal(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _normal(item)
            for key, item in sorted(value.items(), key=lambda item: str(item[0]))
            if str(key) not in VOLATILE_KEYS and not str(key).endswith("_path")
        }
    if isinstance(value, list):
        return [_normal(item) for item in value]
    return value


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(_normal(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _cache_decisions(analysis: Mapping[str, Any]) -> list[dict[str, str]]:
    raw_cache = analysis.get("analyzer_cache", {})
    raw_records = raw_cache.get("records", []) if isinstance(raw_cache, Mapping) else []
    if not isinstance(raw_records, list):
        raise PortfolioReplayError("semantic analyzer cache records must be a list")
    records: list[dict[str, str]] = []
    for raw in raw_records:
        if not isinstance(raw, Mapping):
            raise PortfolioReplayError("semantic analyzer cache record must be an object")
        key = str(raw.get("cache_key") or "")
        decision = str(raw.get("decision_digest") or "")
        if not key or not decision:
            raise PortfolioReplayError("semantic analyzer cache record lacks immutable decision identity")
        records.append({"cache_key": key, "decision_digest": decision})
    if len({(row["cache_key"], row["decision_digest"]) for row in records}) != len(records):
        raise PortfolioReplayError("semantic analyzer cache decision identities are duplicated")
    return sorted(records, key=lambda row: (row["cache_key"], row["decision_digest"]))


def _meeting_split_digest(accounting: Mapping[str, Any]) -> str:
    """Digest only timestamp-evidenced canonical-meeting split provenance."""
    proposals = accounting.get("proposals", [])
    meeting_splits = [
        {
            "id": proposal.get("id"),
            "canonical_meeting_id": (proposal.get("provenance") or {}).get("canonical_meeting_id"),
            "timestamped_split_evidence_ids": (proposal.get("provenance") or {}).get("timestamped_split_evidence_ids"),
        }
        for proposal in proposals
        if isinstance(proposal, Mapping)
        and isinstance(proposal.get("provenance"), Mapping)
        and (proposal["provenance"].get("canonical_meeting_id") is not None)
    ] if isinstance(proposals, list) else []
    return _digest(meeting_splits)


def _canonical_meeting_identity(ledger: Any, accounting: Mapping[str, Any]) -> dict[str, Any] | None:
    """Recreate the canonical recording identity from immutable evidence."""
    if not isinstance(ledger, Mapping):
        return None
    events = ledger.get("events")
    if not isinstance(events, list) or not all(isinstance(event, Mapping) for event in events):
        return None
    try:
        recordings = clockify_portfolio_quality.canonical_recording_events(
            events, ledger.get("manifest", {})
        )
    except (meeting_reconciliation.MeetingReconciliationError, ValueError) as exc:
        raise PortfolioReplayError(f"canonical meeting timezone normalization failed: {exc}") from exc
    if not recordings:
        return None
    try:
        reconciliation = meeting_reconciliation.reconcile_meetings(
            [event for event in recordings if event.get("source_type") == "fathom"],
            [event for event in recordings if event.get("source_type") == "calendly"],
            vlad_identities={"vlad@serenichron.com"},
        )
    except meeting_reconciliation.MeetingReconciliationError as exc:
        raise PortfolioReplayError(f"canonical meeting reconciliation failed: {exc}") from exc
    return {
        "meeting_reconciliation_digest": _digest(reconciliation.document()),
        "meeting_dedup_version": reconciliation.algorithm_version,
        "meeting_dedup_tolerance_seconds": reconciliation.tolerance_seconds,
        "meeting_split_digest": _meeting_split_digest(accounting),
    }


def _meeting_identity(
    accounting: Mapping[str, Any], reconciliation: Any, ledger: Any, *, derive: bool
) -> dict[str, Any]:
    """Bind canonical meeting semantics without invalidating old sealed runs.

    New accounting results carry these fields directly.  The fallback retains
    old Fathom-only artifacts as readable, immutable replay inputs; it is not
    used to claim that a historical run used the canonical algorithm.
    """
    supplied = {field: accounting.get(field) for field in _MEETING_IDENTITY_FIELDS}
    present = {field for field, value in supplied.items() if value is not None}
    if present:
        if present != _MEETING_IDENTITY_FIELDS:
            raise PortfolioReplayError("canonical meeting replay identity is incomplete")
        if not isinstance(supplied["meeting_reconciliation_digest"], str) or not _SHA256.fullmatch(supplied["meeting_reconciliation_digest"]):
            raise PortfolioReplayError("canonical meeting reconciliation digest is invalid")
        if not isinstance(supplied["meeting_dedup_version"], str) or not supplied["meeting_dedup_version"]:
            raise PortfolioReplayError("canonical meeting dedup version is invalid")
        tolerance = supplied["meeting_dedup_tolerance_seconds"]
        if isinstance(tolerance, bool) or not isinstance(tolerance, int) or tolerance < 0:
            raise PortfolioReplayError("canonical meeting dedup tolerance is invalid")
        if not isinstance(supplied["meeting_split_digest"], str) or not _SHA256.fullmatch(supplied["meeting_split_digest"]):
            raise PortfolioReplayError("canonical meeting split digest is invalid")
    canonical = _canonical_meeting_identity(ledger, accounting) if derive else None
    if canonical is not None:
        if present and supplied != canonical:
            raise PortfolioReplayError("supplied canonical meeting identity does not match immutable recordings")
        return canonical
    if present:
        return supplied
    return {
        "meeting_reconciliation_digest": _digest(reconciliation),
        "meeting_dedup_version": "fathom-only/legacy",
        "meeting_dedup_tolerance_seconds": None,
        "meeting_split_digest": _meeting_split_digest(accounting),
    }


def _identity(
    *, run_dir: Path, review: Path, repair: Path, quality: Path, routing: Path,
    derive_meetings: bool = True,
) -> dict[str, Any]:
    run_dir = run_dir.resolve()
    documents: dict[str, Any] = {}
    for name, relative in RUN_ARTIFACTS.items():
        path = run_dir / relative
        if not path.is_file():
            raise PortfolioReplayError(f"required run artifact is missing: {path}")
        documents[name] = _read(path)
    for name, path in {
        "routing": routing, "review": review, "repair": repair, "quality": quality,
    }.items():
        if not path.is_file():
            raise PortfolioReplayError(f"required portfolio artifact is missing: {path}")
        documents[name] = _read(path)
    if not isinstance(documents["quality"], Mapping) or documents["quality"].get("status") != "pass":
        raise PortfolioReplayError("only a passing portfolio quality report can be sealed or replayed")
    analysis = documents["semantic_analysis"]
    if not isinstance(analysis, Mapping):
        raise PortfolioReplayError("semantic analysis must be an object")
    review_document = documents["review"]
    repair_document = documents["repair"]
    if not isinstance(review_document, Mapping) or not isinstance(repair_document, Mapping):
        raise PortfolioReplayError("review and repair artifacts must be objects")
    accounting_document = documents["work_accounting_result"]
    if not isinstance(accounting_document, Mapping):
        raise PortfolioReplayError("work accounting result must be an object")
    model_revision = {
        "review_model": review_document.get("model"),
        "review_revision": review_document.get("revision"),
        "repair_model": (repair_document.get("repair") or {}).get("model") if isinstance(repair_document.get("repair"), Mapping) else None,
        "repair_revision": (repair_document.get("repair") or {}).get("revision") if isinstance(repair_document.get("repair"), Mapping) else None,
    }
    artifact_digests = {name: _digest(document) for name, document in documents.items()}
    return {
        "schema_version": 2,
        "artifacts": artifact_digests,
        "cache_decisions": _cache_decisions(analysis),
        "model_revision": _normal(model_revision),
        **_meeting_identity(
            accounting_document,
            documents["fathom_reconciliation"],
            documents["immutable_ledger"],
            derive=derive_meetings,
        ),
    }


def seal(*, run_dir: Path, review: Path, repair: Path, quality: Path, routing: Path) -> dict[str, Any]:
    identity = _identity(run_dir=run_dir, review=review, repair=repair, quality=quality, routing=routing)
    return {"schema_version": 1, "status": "sealed", "identity": identity, "seal_digest": _digest(identity)}


def verify(sealed: Mapping[str, Any], *, run_dir: Path, review: Path, repair: Path, quality: Path, routing: Path) -> dict[str, Any]:
    if sealed.get("status") != "sealed" or not isinstance(sealed.get("identity"), Mapping):
        raise PortfolioReplayError("invalid portfolio replay seal")
    sealed_identity = sealed["identity"]
    if not _MEETING_IDENTITY_FIELDS.issubset(sealed_identity):
        candidate = _identity(
            run_dir=run_dir, review=review, repair=repair, quality=quality,
            routing=routing, derive_meetings=False,
        )
        # A v1 Fathom-only seal was already immutable at the artifact layer.
        # Compare exactly the fields it knew, preserving read-only replay.
        candidate = {
            key: value for key, value in candidate.items()
            if key in sealed_identity
        }
        candidate["schema_version"] = sealed_identity.get("schema_version")
    else:
        candidate = _identity(run_dir=run_dir, review=review, repair=repair, quality=quality, routing=routing)
    if _normal(sealed_identity) != _normal(candidate):
        raise PortfolioReplayError("portfolio replay identity differs from seal")
    return {
        "schema_version": 1, "status": "pass", "seal_digest": sealed.get("seal_digest"),
        "identity": candidate,
    }


def _arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--repair", type=Path, required=True)
    parser.add_argument("--quality", type=Path, required=True)
    parser.add_argument("--routing", type=Path, required=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    seal_parser = commands.add_parser("seal")
    _arguments(seal_parser)
    seal_parser.add_argument("--output", type=Path, required=True)
    verify_parser = commands.add_parser("verify")
    _arguments(verify_parser)
    verify_parser.add_argument("--seal", type=Path, required=True)
    verify_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "seal":
            result = seal(run_dir=args.run_dir, review=args.review, repair=args.repair, quality=args.quality, routing=args.routing)
            _write(args.output, result)
        else:
            result = verify(_read(args.seal), run_dir=args.run_dir, review=args.review, repair=args.repair, quality=args.quality, routing=args.routing)
            _write(args.output, result)
    except (OSError, ValueError, json.JSONDecodeError, PortfolioReplayError) as exc:
        print(f"clockify portfolio replay: {exc}", file=sys.stderr)
        return 2
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
