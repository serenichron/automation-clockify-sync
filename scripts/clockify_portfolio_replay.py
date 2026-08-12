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
import sys
from typing import Any, Mapping


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


def _identity(
    *, run_dir: Path, review: Path, repair: Path, quality: Path, routing: Path
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
    model_revision = {
        "review_model": review_document.get("model"),
        "review_revision": review_document.get("revision"),
        "repair_model": (repair_document.get("repair") or {}).get("model") if isinstance(repair_document.get("repair"), Mapping) else None,
        "repair_revision": (repair_document.get("repair") or {}).get("revision") if isinstance(repair_document.get("repair"), Mapping) else None,
    }
    artifact_digests = {name: _digest(document) for name, document in documents.items()}
    return {
        "schema_version": 1,
        "artifacts": artifact_digests,
        "cache_decisions": _cache_decisions(analysis),
        "model_revision": _normal(model_revision),
    }


def seal(*, run_dir: Path, review: Path, repair: Path, quality: Path, routing: Path) -> dict[str, Any]:
    identity = _identity(run_dir=run_dir, review=review, repair=repair, quality=quality, routing=routing)
    return {"schema_version": 1, "status": "sealed", "identity": identity, "seal_digest": _digest(identity)}


def verify(sealed: Mapping[str, Any], *, run_dir: Path, review: Path, repair: Path, quality: Path, routing: Path) -> dict[str, Any]:
    if sealed.get("status") != "sealed" or not isinstance(sealed.get("identity"), Mapping):
        raise PortfolioReplayError("invalid portfolio replay seal")
    candidate = _identity(run_dir=run_dir, review=review, repair=repair, quality=quality, routing=routing)
    if _normal(sealed["identity"]) != _normal(candidate):
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
