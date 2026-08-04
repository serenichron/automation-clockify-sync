#!/usr/bin/env python3
"""Evidence-bound acceptance gates for Clockify shadow review periods.

The ledger is local-only. It never changes Clockify, Sheets, Multica, review
state, schedules, or analyzer configuration. Exceptions-only presentation is
eligible only after one complete >=90% baseline and two later consecutive
complete >=95% guarded periods with zero assessed critical errors.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

try:
    from scripts import analyzer_evaluation
    from scripts import review_corrections
except ModuleNotFoundError:  # direct script execution
    import analyzer_evaluation  # type: ignore[no-redef]
    import review_corrections  # type: ignore[no-redef]


SCHEMA_VERSION = 1
STAGES = {"shadow_baseline", "guarded"}
SEVERITIES = {"critical", "noncritical"}
CRITICAL_DOMAINS = {"routing", "description_truth", "meeting", "allocation", "other"}


class AcceptanceError(ValueError):
    """Raised when period evidence cannot support an acceptance claim."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _require_mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AcceptanceError(f"{label} must be an object")
    return value


def _target(item: Mapping[str, Any]) -> tuple[str, str, str] | None:
    review_id = str(item.get("id") or "").strip()
    activity_id = str(item.get("activity_id") or "").strip()
    fingerprint = str(item.get("evidence_fingerprint") or "").strip()
    if not review_id or not activity_id or not fingerprint:
        return None
    return review_id, activity_id, fingerprint


def _denominator(snapshot: Mapping[str, Any]) -> list[dict[str, str]]:
    categories = _require_mapping(snapshot.get("categories"), "review categories")
    rows: dict[tuple[str, str, str], dict[str, str]] = {}
    for name in ("new", "changed", "carried_pending"):
        values = categories.get(name, [])
        if not isinstance(values, list):
            raise AcceptanceError(f"review category {name} must be a list")
        for raw in values:
            item = _require_mapping(raw, f"review category {name} item")
            # Shadow evaluation measures the complete active review surface.
            # Excluding ambiguous rows would let the process promote itself by
            # hiding exactly the cases that still require human judgment.
            if str(item.get("disposition") or "") not in {"pending", "ambiguous"}:
                continue
            target = _target(item)
            if target is None:
                raise AcceptanceError("pending review item lacks stable activity evidence identity")
            rows[target] = {
                "review_item_id": target[0],
                "activity_id": target[1],
                "evidence_fingerprint": target[2],
            }
    return [rows[key] for key in sorted(rows)]


def _versions(analysis: Mapping[str, Any]) -> list[dict[str, Any]]:
    values: set[str] = set()
    output: list[dict[str, Any]] = []
    activities = analysis.get("activities", [])
    if not isinstance(activities, list):
        raise AcceptanceError("semantic activities must be a list")
    for raw in activities:
        activity = _require_mapping(raw, "semantic activity")
        version = {
            "model": str(activity.get("analyzer_model") or ""),
            "revision": str(activity.get("analyzer_revision") or ""),
            "tier": str(activity.get("analyzer_tier") or ""),
            "prompt_version": str(activity.get("prompt_version") or analysis.get("prompt_version") or ""),
            "schema_version": activity.get("schema_version", analysis.get("schema_version")),
        }
        key = canonical_json(version)
        if key not in values:
            values.add(key)
            output.append(version)
    chunks = analysis.get("analysis_chunks", [])
    if not isinstance(chunks, list):
        raise AcceptanceError("analysis_chunks must be a list")
    pending = [(chunk, None, None) for chunk in chunks]
    recovery_nodes = 0
    while pending:
        raw_chunk, expected_path, expected_depth = pending.pop(0)
        chunk = _require_mapping(raw_chunk, "analysis chunk")
        if expected_path is not None and (
            chunk.get("partition_path") != expected_path
            or chunk.get("partition_depth") != expected_depth
        ):
            raise AcceptanceError("analysis recovery path or depth is invalid")
        recovery = chunk.get("recovery")
        if recovery is not None:
            recovery_nodes += 1
            if recovery_nodes > 511:
                raise AcceptanceError("analysis recovery tree exceeds its bound")
            recovery_record = _require_mapping(recovery, "analysis recovery")
            children = recovery_record.get("children")
            if not isinstance(children, list):
                raise AcceptanceError("analysis recovery children must be a list")
            path = str(chunk.get("partition_path") or "")
            depth = chunk.get("partition_depth")
            child_counts = [
                child.get("event_count") if isinstance(child, Mapping) else None
                for child in children
            ]
            expected_recovery_status = {
                "recovered": "recovered_by_partition",
                "exhausted": "partition_exception",
            }.get(str(recovery_record.get("status") or ""))
            if (
                not path
                or not isinstance(depth, int)
                or isinstance(depth, bool)
                or recovery_record.get("path") != path
                or recovery_record.get("depth") != depth
                or len(children) != 2
                or depth >= 8
                or any(
                    not isinstance(count, int) or isinstance(count, bool) or count <= 0
                    for count in child_counts
                )
                or sum(child_counts) != chunk.get("event_count")
                or chunk.get("recovery_status") != expected_recovery_status
            ):
                raise AcceptanceError("analysis recovery metadata is invalid")
            pending[0:0] = [
                (child, f"{path}.{label}", depth + 1)
                for label, child in zip(("a", "b"), children, strict=True)
            ]
        model = str(chunk.get("model") or "")
        tier = str(chunk.get("tier") or "")
        if not model or not tier:
            continue
        version = {
            "model": model,
            "revision": str(chunk.get("revision") or ""),
            "tier": tier,
            "prompt_version": str(analysis.get("prompt_version") or ""),
            "schema_version": analysis.get("schema_version"),
        }
        key = canonical_json(version)
        if key not in values:
            values.add(key)
            output.append(version)
    return sorted(output, key=canonical_json)


def _critical_assessments(path: Path | None) -> dict[str, dict[str, str]]:
    if path is None:
        return {}
    document = _require_mapping(_read_json(path), "critical assessment")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise AcceptanceError("unsupported critical assessment schema")
    values = document.get("assessments")
    if not isinstance(values, list):
        raise AcceptanceError("critical assessments must be a list")
    output: dict[str, dict[str, str]] = {}
    for raw in values:
        item = _require_mapping(raw, "critical assessment item")
        decision_id = str(item.get("decision_id") or "").strip()
        severity = str(item.get("severity") or "").strip()
        domain = str(item.get("domain") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if not decision_id or severity not in SEVERITIES or domain not in CRITICAL_DOMAINS or not reason:
            raise AcceptanceError("critical assessment requires decision_id, severity, domain, and reason")
        if decision_id in output:
            raise AcceptanceError("critical assessment repeats a decision_id")
        output[decision_id] = {"severity": severity, "domain": domain, "reason": reason}
    return output


def build_period_report(
    run_dir: Path,
    replay_run_dir: Path,
    decisions: Iterable[Mapping[str, Any]],
    *,
    stage: str,
    critical_assessments: Mapping[str, Mapping[str, str]] | None = None,
    analyzer_scorecards: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    if stage not in STAGES:
        raise AcceptanceError("stage must be shadow_baseline or guarded")
    run_dir = run_dir.resolve()
    replay_run_dir = replay_run_dir.resolve()
    if run_dir == replay_run_dir:
        raise AcceptanceError("acceptance replay must be a distinct run")
    snapshot = _require_mapping(_read_json(run_dir / "review-snapshot.json"), "review snapshot")
    replay_snapshot = _require_mapping(_read_json(replay_run_dir / "review-snapshot.json"), "replay snapshot")
    accounting = _require_mapping(_read_json(run_dir / "work-accounting-result.json"), "accounting result")
    replay_accounting = _require_mapping(_read_json(replay_run_dir / "work-accounting-result.json"), "replay accounting result")
    analysis = _require_mapping(_read_json(run_dir / "semantic-analysis.json"), "semantic analysis")
    replay_analysis = _require_mapping(_read_json(replay_run_dir / "semantic-analysis.json"), "replay semantic analysis")
    quality = _require_mapping(_read_json(run_dir / "quality_report.json"), "quality report")
    replay_quality = _require_mapping(_read_json(replay_run_dir / "quality_report.json"), "replay quality report")
    replay_integrity_path = replay_run_dir / "replay-integrity.json"
    if not replay_integrity_path.is_file():
        raise AcceptanceError(
            "acceptance replay lacks replay-integrity.json; a second live collection is not replay evidence"
        )
    replay_integrity = _require_mapping(
        _read_json(replay_integrity_path), "replay integrity"
    )

    denominator = _denominator(snapshot)
    replay_denominator = _denominator(replay_snapshot)
    if not denominator:
        raise AcceptanceError("acceptance period requires a non-empty full review denominator")
    decisions_by_target: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for raw in decisions:
        decision = review_corrections.validate_decision(raw)
        target = (
            str(decision["review_item_id"]),
            str(decision["activity_id"]),
            str(decision["evidence_fingerprint"]),
        )
        if target in decisions_by_target:
            raise AcceptanceError("acceptance decisions repeat an evidence-bound target")
        decisions_by_target[target] = decision

    matched: list[Mapping[str, Any]] = []
    missing_targets: list[dict[str, str]] = []
    for target in denominator:
        key = (target["review_item_id"], target["activity_id"], target["evidence_fingerprint"])
        decision = decisions_by_target.get(key)
        if decision is None:
            missing_targets.append(target)
        else:
            matched.append(decision)

    assessments = dict(critical_assessments or {})
    changed = [decision for decision in matched if decision["decision"] != "approve"]
    changed_ids = {str(decision["decision_id"]) for decision in changed}
    if set(assessments) - changed_ids:
        raise AcceptanceError("critical assessment references a decision outside the changed denominator")
    unassessed = sorted(changed_ids - set(assessments))
    critical = [
        {"decision_id": decision_id, **dict(assessments[decision_id])}
        for decision_id in sorted(assessments)
        if assessments[decision_id].get("severity") == "critical"
    ]
    unchanged = sum(decision["decision"] == "approve" for decision in matched)
    denominator_count = len(denominator)
    rate = unchanged / denominator_count

    ledger_manifest = _require_mapping(accounting.get("ledger_manifest"), "ledger manifest")
    replay_manifest = _require_mapping(replay_accounting.get("ledger_manifest"), "replay ledger manifest")
    manifest_id = str(ledger_manifest.get("manifest_id") or "")
    replay_manifest_id = str(replay_manifest.get("manifest_id") or "")
    integrity_identity = _require_mapping(
        replay_integrity.get("ledger_identity"), "replay integrity ledger identity"
    )
    integrity_payload = {
        str(key): value
        for key, value in replay_integrity.items()
        if key != "integrity_digest"
    }
    immutable_replay_pass = bool(
        replay_integrity.get("integrity_digest") == digest(integrity_payload)
        and replay_integrity.get("schema_version") == SCHEMA_VERSION
        and replay_integrity.get("status") == "pass"
        and str(replay_integrity.get("source_run_id") or "")
        == str(snapshot.get("run_id") or run_dir.name)
        and str(replay_integrity.get("replay_run_id") or "")
        == str(replay_snapshot.get("run_id") or replay_run_dir.name)
        and str(integrity_identity.get("manifest_id") or "") == manifest_id
        and str(replay_integrity.get("ledger_evidence_digest") or "")
        == str(analysis.get("ledger_evidence_digest") or "")
        and not replay_integrity.get("failures")
    )
    source_complete = (
        _require_mapping(ledger_manifest.get("source_completeness"), "source completeness").get("status") == "complete"
        and _require_mapping(replay_manifest.get("source_completeness"), "replay source completeness").get("status") == "complete"
    )
    replay_summary = _require_mapping(replay_snapshot.get("summary"), "replay summary")
    versions = _versions(analysis)
    replay_versions = _versions(replay_analysis)
    versions_complete = bool(versions) and all(
        version.get("model")
        and version.get("tier")
        and version.get("prompt_version")
        and version.get("schema_version") is not None
        for version in versions
    )
    verified_scorecards = [
        analyzer_evaluation.verify_scorecard(scorecard)
        for scorecard in analyzer_scorecards
    ]
    evaluated_routes = {
        (
            str(scorecard.get("route", {}).get("model") or ""),
            str(scorecard.get("route", {}).get("revision") or ""),
            str(scorecard.get("route", {}).get("tier") or ""),
        )
        for scorecard in verified_scorecards
        if scorecard.get("passed") is True and isinstance(scorecard.get("route"), Mapping)
    }
    analyzer_evaluation_pass = bool(verified_scorecards) and {
        (
            str(version.get("model") or ""),
            str(version.get("revision") or ""),
            str(version.get("tier") or ""),
        )
        for version in versions
    } <= evaluated_routes
    coverage_clean = not snapshot.get("coverage_warnings") and not replay_snapshot.get("coverage_warnings")
    replay_stable = bool(
        analysis.get("ledger_evidence_digest")
        and analysis.get("ledger_evidence_digest") == replay_analysis.get("ledger_evidence_digest")
        and manifest_id
        and manifest_id == replay_manifest_id
        and denominator == replay_denominator
        and versions_complete
        and versions == replay_versions
        and immutable_replay_pass
        and int(replay_summary.get("new", -1)) == 0
        and int(replay_summary.get("changed", -1)) == 0
    )
    quality_pass = quality.get("status") == "pass" and replay_quality.get("status") == "pass"
    full_denominator_complete = not missing_targets and not unassessed
    core = {
        "schema_version": SCHEMA_VERSION,
        "stage": stage,
        "run_id": str(snapshot.get("run_id") or run_dir.name),
        "replay_run_id": str(replay_snapshot.get("run_id") or replay_run_dir.name),
        "ledger_manifest_id": manifest_id,
        "ledger_evidence_digest": str(analysis.get("ledger_evidence_digest") or ""),
        "analyzer_versions": versions,
        "analyzer_scorecard_digests": sorted(
            str(scorecard.get("scorecard_digest") or "") for scorecard in verified_scorecards
        ),
        "analyzer_evaluation_pass": analyzer_evaluation_pass,
        "denominator_digest": digest(denominator),
        "decision_ids": sorted(str(decision["decision_id"]) for decision in matched),
        "denominator_count": denominator_count,
        "approve_unchanged_count": unchanged,
        "modify_count": sum(decision["decision"] == "modify" for decision in matched),
        "skip_count": sum(decision["decision"] == "skip" for decision in matched),
        "missing_decision_count": len(missing_targets),
        "unassessed_changed_count": len(unassessed),
        "unchanged_approval_rate": round(rate, 6),
        "critical_errors": critical,
        "critical_error_count": len(critical),
        "source_complete": source_complete,
        "quality_pass": quality_pass,
        "coverage_clean": coverage_clean,
        "immutable_replay_pass": immutable_replay_pass,
        "replay_stable": replay_stable,
        "full_denominator_complete": full_denominator_complete,
    }
    core["period_id"] = "rperiod-" + digest(core)[7:31]
    return core


def _digest_payload(record: Mapping[str, Any]) -> dict[str, Any]:
    """Bind record content and its predecessor; exclude only self-digest."""
    return {str(key): value for key, value in record.items() if key != "record_digest"}


def load_ledger(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    previous: str | None = None
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            raise AcceptanceError(f"blank acceptance ledger line {number}")
        record = _require_mapping(json.loads(line), f"acceptance ledger line {number}")
        if record.get("schema_version") != SCHEMA_VERSION or record.get("stage") not in STAGES:
            raise AcceptanceError(f"invalid acceptance record at line {number}")
        expected = digest(_digest_payload(record))
        if record.get("previous_digest") != previous or record.get("record_digest") != expected:
            raise AcceptanceError(f"acceptance ledger integrity failure at line {number}")
        records.append(dict(record))
        previous = expected
    return records


def append_period(path: Path, period: Mapping[str, Any]) -> bool:
    records = load_ledger(path)
    if any(record.get("period_id") == period.get("period_id") for record in records):
        return False
    line = dict(period)
    line["previous_digest"] = records[-1]["record_digest"] if records else None
    line["record_digest"] = digest(_digest_payload(line))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(canonical_json(line) + "\n")
    return True


def period_passes(record: Mapping[str, Any], threshold: float) -> bool:
    return bool(
        record.get("full_denominator_complete")
        and record.get("source_complete")
        and record.get("quality_pass")
        and record.get("coverage_clean")
        and record.get("analyzer_evaluation_pass")
        and record.get("replay_stable")
        and int(record.get("critical_error_count", -1)) == 0
        and float(record.get("unchanged_approval_rate", 0.0)) >= threshold
    )


def evaluate_gate(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    values = list(records)
    baseline_indexes = [
        index for index, record in enumerate(values)
        if record.get("stage") == "shadow_baseline" and period_passes(record, 0.90)
    ]
    guarded_pair: list[Mapping[str, Any]] = []
    if baseline_indexes:
        baseline_index = baseline_indexes[0]
        later = values[baseline_index + 1 :]
        for left, right in zip(later, later[1:]):
            period_run_ids = [
                str(record.get(field) or "")
                for record in (values[baseline_index], left, right)
                for field in ("run_id", "replay_run_id")
            ]
            if (
                left.get("stage") == right.get("stage") == "guarded"
                and period_passes(left, 0.95)
                and period_passes(right, 0.95)
                and "" not in period_run_ids
                and len(set(period_run_ids)) == 6
            ):
                guarded_pair = [left, right]
                break
    return {
        "schema_version": SCHEMA_VERSION,
        "period_count": len(values),
        "baseline_90_passed": bool(baseline_indexes),
        "guarded_95_period_ids": [str(record.get("period_id") or "") for record in guarded_pair],
        "exceptions_only_eligible": bool(baseline_indexes and len(guarded_pair) == 2),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    record = sub.add_parser("record")
    record.add_argument("--run-dir", type=Path, required=True)
    record.add_argument("--replay-run-dir", type=Path, required=True)
    record.add_argument("--decisions", type=Path, required=True)
    record.add_argument("--critical-assessments", type=Path)
    record.add_argument("--analyzer-scorecard", type=Path, action="append", default=[])
    record.add_argument("--stage", choices=sorted(STAGES), required=True)
    record.add_argument("--ledger", type=Path, required=True)
    status = sub.add_parser("status")
    status.add_argument("--ledger", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "record":
            decisions = review_corrections.load_decisions(args.decisions)
            assessments = _critical_assessments(args.critical_assessments)
            scorecards = [
                _require_mapping(_read_json(path), "analyzer scorecard")
                for path in args.analyzer_scorecard
            ]
            period = build_period_report(
                args.run_dir,
                args.replay_run_dir,
                decisions,
                stage=args.stage,
                critical_assessments=assessments,
                analyzer_scorecards=scorecards,
            )
            appended = append_period(args.ledger, period)
            print(json.dumps({"appended": appended, "period": period, "gate": evaluate_gate(load_ledger(args.ledger))}, indent=2, sort_keys=True))
        else:
            print(json.dumps(evaluate_gate(load_ledger(args.ledger)), indent=2, sort_keys=True))
        return 0
    except (
        OSError,
        json.JSONDecodeError,
        AcceptanceError,
        analyzer_evaluation.EvaluationError,
        review_corrections.ReviewDecisionError,
    ) as exc:
        print(f"clockify review acceptance: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
