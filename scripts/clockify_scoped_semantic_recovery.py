#!/usr/bin/env python3
"""Recover sealed human-work contexts without accounting or proposal emission.

--plan is entirely read-only. A live run requires the exact current Flash route,
private-text approval, and a new private output directory. Original failure-group
identities and residual quarantine are retained; all timing is review-only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

try:
    from scripts import semantic_analyzer as semantic
    from scripts import work_accounting_pipeline as pipeline
except ModuleNotFoundError:
    import semantic_analyzer as semantic
    import work_accounting_pipeline as pipeline


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_run", type=Path)
    parser.add_argument("--failed-review-digest", action="append", required=True)
    parser.add_argument("--scope-file", type=Path, required=True)
    parser.add_argument("--scope-key", default="evidence_ids", help="Dot-separated JSON path to the selected evidence-ID list")
    parser.add_argument("--cache-seed", type=Path, help="Source-bound append-only cache; defaults to the sealed source snapshot")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--plan", action="store_true", help="Validate source bindings and print exact requests; no transport or writes")
    args = parser.parse_args(argv)
    if not args.plan and args.output_dir is None:
        parser.error("--output-dir is required without --plan")
    return args


def _timing_report(activities, events, proposals):
    by_id = {event["evidence_id"]: event for event in events}
    blocks = [
        {"kind": "source_proposal", "id": proposal.get("id"), "start": pipeline._parse_dt(proposal.get("start")), "end": pipeline._parse_dt(proposal.get("end"))}
        for proposal in proposals
    ] + [{"kind": "existing_clockify", "id": row["block_id"], "start": row["start"], "end": row["end"]} for row in pipeline._existing_blocks(events)]
    blocks = [row for row in blocks if row["start"] and row["end"] and row["end"] > row["start"]]
    rows = []
    for activity in activities:
        intervals = pipeline._activity_observed_intervals([by_id[eid] for eid in activity["evidence_ids"]])
        overlaps = []
        for interval in intervals:
            start, end = pipeline._parse_dt(interval["start"]), pipeline._parse_dt(interval["end"])
            for block in blocks:
                left, right = max(start, block["start"]), min(end, block["end"])
                if right > left:
                    overlaps.append({"block_kind": block["kind"], "block_id": block["id"], "start": left.isoformat(), "end": right.isoformat(), "seconds": (right - left).total_seconds()})
        remaining = pipeline._subtract_intervals([(pipeline._parse_dt(i["start"]), pipeline._parse_dt(i["end"])) for i in intervals], [(b["start"], b["end"]) for b in blocks])
        rows.append({"activity_id": activity.get("activity_id"), "evidence_ids": activity["evidence_ids"], "observed_intervals": intervals, "overlaps": overlaps, "observed_whole_minute_capacity": pipeline._interval_capacity_minutes(intervals), "unoccupied_whole_minute_capacity": pipeline._interval_capacity_minutes([{"start": a.isoformat(), "end": b.isoformat()} for a, b in remaining]), "timing_requires_review": True})
    return {"schema_version": "semantic-recovery-timing-review/v1", "temporal_overlap_is_not_financial_credit": True, "capacities_are_not_effort_estimates": True, "new_proposal_count": 0, "activities": rows}


RECOVERY_INPUT_FILES = (
    "recovery-receipt.json", "recovery-plan.json", "source-scope-input.json",
    "source-semantic-analysis.json", "preserved-source-proposals.json",
    "semantic-analysis.json", "analyzer-response-cache.jsonl", "analyzer-cache-used.jsonl",
)


def validate_cached_recovery(source_dir, recovery_dir, failed_review_digests):
    """Reconstruct a source-bound recovery from sealed cache, never provider output."""
    try:
        from scripts import clockify_review_run as review
    except ModuleNotFoundError:
        import clockify_review_run as review
    source_dir, recovery_dir = Path(source_dir), Path(recovery_dir)
    if source_dir.is_symlink() or recovery_dir.is_symlink():
        raise ValueError("cached recovery roots must not be symlinks")
    source_dir, recovery_dir = source_dir.resolve(strict=True), recovery_dir.resolve(strict=True)
    files = {name: review._read_snapshot_source(recovery_dir / name, label="cached recovery input")
             for name in RECOVERY_INPUT_FILES}
    receipt = json.loads(files["recovery-receipt.json"])
    if not isinstance(receipt, dict):
        raise ValueError("cached recovery receipt must be an object")
    source_bytes = review._read_snapshot_source(source_dir / "semantic-analysis.json", label="cached recovery source")
    proposal_bytes = review._read_snapshot_source(source_dir / "proposals.json", label="cached recovery proposals")
    source_sha = hashlib.sha256(source_bytes).hexdigest()
    if (receipt.get("schema_version") != "semantic-only-recovery-receipt/v1"
        or Path(str(receipt.get("source_run", ""))).resolve() != source_dir
        or receipt.get("source_semantic_sha256") != source_sha
        or files["source-semantic-analysis.json"] != source_bytes
        or files["preserved-source-proposals.json"] != proposal_bytes
        or receipt.get("source_proposals_sha256") != hashlib.sha256(proposal_bytes).hexdigest()
        or receipt.get("scope_file_sha256") != hashlib.sha256(files["source-scope-input.json"]).hexdigest()
        or receipt.get("new_proposal_count") != 0 or receipt.get("accounting_performed") is not False
        or receipt.get("external_writes") is not False
        or receipt.get("preserved_source_proposal_count") != len(json.loads(proposal_bytes))):
        raise ValueError("cached recovery source or receipt binding differs")
    selected = json.loads(files["source-scope-input.json"])
    scope_key = receipt.get("scope_key")
    if not isinstance(scope_key, str) or not scope_key:
        raise ValueError("cached recovery selected scope is missing")
    for key in scope_key.split("."):
        if not isinstance(selected, dict) or key not in selected:
            raise ValueError("cached recovery selected scope key is absent")
        selected = selected[key]
    source, analysis = json.loads(source_bytes), json.loads(files["semantic-analysis.json"])
    if not isinstance(source, dict) or not isinstance(analysis, dict):
        raise ValueError("cached recovery semantic inputs must be objects")
    provenance = analysis.get("failed_review_retry", {})
    if not isinstance(provenance, dict):
        raise ValueError("cached recovery retry provenance must be an object")
    digests = pipeline._canonical_retry_digests(failed_review_digests)
    if review._retry_provenance_digests(provenance) != digests:
        raise ValueError("cached recovery original failed groups differ")
    seed = files["analyzer-response-cache.jsonl"]
    original_cache = review._read_snapshot_source(source_dir / "analyzer-cache-used.jsonl", label="cached recovery source cache")
    if not seed.startswith(original_cache):
        raise ValueError("cached recovery seed does not preserve sealed source cache")
    cache = semantic.AnalyzerResponseCache(recovery_dir / "analyzer-response-cache.jsonl")
    endpoints = [endpoint for endpoint in cache.sealed_endpoints()
                 if (endpoint.model, endpoint.revision) == semantic.CURRENT_LIVE_FLASH_ROUTE]
    if len(endpoints) != 1:
        raise ValueError("cached recovery lacks one exact approved Flash route")
    primary = endpoints[0]
    ledger, all_events = pipeline.load_ledger(source_dir / "evidence/evidence-ledger.json")
    members = pipeline.meeting_reconciliation.manifest_member_identities(ledger.manifest.document())
    events, _noise = pipeline._analysis_events(all_events, members)
    routing = pipeline._read_json(source_dir / "routing.json")
    actor_contract = provenance.get("actor_contract")
    if actor_contract not in {None, semantic.ACTOR_CONTRACT}:
        raise pipeline.WorkAccountingError("cached semantic actor contract is invalid")
    events = pipeline._with_semantic_route_hints(
        events, routing, normalize_meeting_domains_type=actor_contract is not None,
    )
    targets = pipeline._failed_review_retry_targets(source, events, cache.path, digests)
    options = dict(primary=primary, cache=cache, review_taxonomy=pipeline._semantic_review_taxonomy(routing),
                   targets=targets, source_semantic_sha256=source_sha, selected_evidence_ids=selected,
                   scoped_review_mode=provenance.get("mode"), private_text_approved=True,
                   actor_contract=actor_contract,
                   actor_subject_binding=routing.get("semantic_subject_binding") if actor_contract else None)
    plan = pipeline.run_scoped_failed_review_retry(source, events, plan_only=True, **options)
    captured_plan = json.loads(files["recovery-plan.json"])
    if not isinstance(captured_plan, dict):
        raise ValueError("cached recovery request plan must be an object")
    if (any(plan[key] != captured_plan.get(key) for key in plan)
        or plan["selected_scope_digest"] != receipt.get("selected_scope_digest")
        or plan["selected_scope_digest"] != provenance.get("selected_scope_digest")
        or sorted(selected) != provenance.get("selected_evidence_ids")
        or plan["selected_event_count"] != receipt.get("selected_event_count")
        or len(plan["requests"]) != receipt.get("request_count")):
        raise ValueError("cached recovery exact request plan or selected scope differs")
    def refuse_transport(*_args):
        raise ValueError("cached recovery cache miss; inference is prohibited")
    reconstructed = pipeline.run_scoped_failed_review_retry(source, events, transport=refuse_transport, **options)
    if any(reconstructed.get(key) != analysis.get(key) for key in ("activities", "exceptions", "omissions", "failed_review_retry")):
        raise ValueError("cached recovery semantic output differs from sealed decisions")
    snapshot = analysis.get("analyzer_cache", {}).get("snapshot", {})
    records = cache.records_for_snapshot(analysis.get("analyzer_cache", {}).get("records", []), configured_endpoints=(primary,))
    used = b"".join((semantic.canonical_json(record) + "\n").encode() for record in records)
    if (used != files["analyzer-cache-used.jsonl"]
        or snapshot != {"path": "analyzer-cache-used.jsonl", "record_count": len(records), "sha256": hashlib.sha256(used).hexdigest()}
        or reconstructed["analyzer_cache"]["records"] != analysis["analyzer_cache"]["records"]):
        raise ValueError("cached recovery used-cache closure differs")
    classified = [eid for section in ("activities", "exceptions", "omissions") for row in reconstructed[section] for eid in row["evidence_ids"]]
    if (len(classified) != len(set(classified)) or set(classified) != {event["evidence_id"] for event in events}
        or reconstructed["activities"][:len(source["activities"])] != source["activities"]):
        raise ValueError("cached recovery changed source assertions or evidence conservation")
    return {"files": files, "analysis": reconstructed, "selected_evidence_ids": sorted(selected),
            "target_digests": digests, "selected_scope_digest": plan["selected_scope_digest"],
            "mode": plan["mode"], "request_count": len(plan["requests"]),
            "residual_event_counts": plan["residual_event_counts"]}


def run(args):
    source_dir = args.source_run.resolve()
    source_path = source_dir / "semantic-analysis.json"
    source_bytes = source_path.read_bytes()
    source = json.loads(source_bytes)
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    ledger, all_events = pipeline.load_ledger(source_dir / "evidence/evidence-ledger.json")
    members = pipeline.meeting_reconciliation.manifest_member_identities(ledger.manifest.document())
    events, _noise = pipeline._analysis_events(all_events, members)
    routing = pipeline._read_json(source_dir / "routing.json")
    actor_contract = routing.get("semantic_actor_contract")
    if actor_contract not in {None, semantic.ACTOR_CONTRACT}:
        raise pipeline.WorkAccountingError("semantic actor contract is invalid")
    events = pipeline._with_semantic_route_hints(
        events, routing, normalize_meeting_domains_type=actor_contract is not None,
    )
    scope_bytes = args.scope_file.read_bytes()
    selected = json.loads(scope_bytes)
    scope_sha256 = hashlib.sha256(scope_bytes).hexdigest()
    for key in args.scope_key.split("."):
        if not isinstance(selected, dict) or key not in selected:
            raise pipeline.WorkAccountingError("selected evidence scope key is absent")
        selected = selected[key]
    primary = semantic.AnalyzerEndpoint.from_env("CLOCKIFY_ANALYZER_PRIMARY", default_model=semantic.DEFAULT_PRIMARY_MODEL)
    if primary is None:
        raise pipeline.WorkAccountingError("CLOCKIFY_ANALYZER_PRIMARY_URL is required")
    semantic.require_current_live_flash_route(primary)
    seed = (args.cache_seed or (source_dir / "analyzer-cache-used.jsonl")).resolve()
    targets = pipeline._failed_review_retry_targets(source, events, seed, args.failed_review_digest)
    cache = semantic.AnalyzerResponseCache(seed, record_review_diagnostics=True)
    options = dict(primary=primary, cache=cache, review_taxonomy=pipeline._semantic_review_taxonomy(routing), targets=targets, source_semantic_sha256=source_sha256, selected_evidence_ids=selected,
                   actor_contract=actor_contract,
                   actor_subject_binding=routing.get("semantic_subject_binding") if actor_contract else None)
    plan = pipeline.run_scoped_failed_review_retry(source, events, plan_only=True, **options)
    plan.update(scope_file_sha256=scope_sha256, scope_key=args.scope_key)
    if args.plan:
        return plan
    semantic._require_private_text_approval([event for event in events if event["evidence_id"] in set(selected)], None)
    output = args.output_dir.resolve()
    if output == source_dir or source_dir in output.parents or output in source_dir.parents:
        raise pipeline.WorkAccountingError("recovery output must be isolated from source")
    if output.exists():
        raise pipeline.WorkAccountingError("recovery output must be a new directory")
    proposal_bytes = (source_dir / "proposals.json").read_bytes()
    proposals = json.loads(proposal_bytes)
    if not isinstance(proposals, list):
        raise pipeline.WorkAccountingError("source proposals must be a list")
    output.mkdir(mode=0o700, parents=True)
    output_cache = output / "analyzer-response-cache.jsonl"
    pipeline._write_bytes(output_cache, seed.read_bytes())
    options["cache"] = semantic.AnalyzerResponseCache(output_cache, record_review_diagnostics=True)
    recovered = pipeline.run_scoped_failed_review_retry(source, events, transport=semantic.http_transport, **options)
    if recovered["activities"][:len(source["activities"])] != source["activities"]:
        raise pipeline.WorkAccountingError("recovery changed accepted source assertions")
    classified = [eid for section in ("activities", "exceptions", "omissions") for row in recovered[section] for eid in row["evidence_ids"]]
    if len(classified) != len(set(classified)) or set(classified) != {event["evidence_id"] for event in events}:
        raise pipeline.WorkAccountingError("recovery failed exact source evidence conservation")
    used = options["cache"].records_for_snapshot(recovered["analyzer_cache"]["records"], configured_endpoints=(primary,))
    used_bytes = b"".join((semantic.canonical_json(record) + "\n").encode() for record in used)
    pipeline._write_bytes(output / "analyzer-cache-used.jsonl", used_bytes)
    recovered["analyzer_cache"]["snapshot"] = {"path": "analyzer-cache-used.jsonl", "record_count": len(used), "sha256": hashlib.sha256(used_bytes).hexdigest()}
    timing = _timing_report(recovered["activities"][len(source["activities"]):], all_events, proposals)
    receipt = {"schema_version": "semantic-only-recovery-receipt/v1", "source_run": str(source_dir), "source_semantic_sha256": source_sha256, "scope_file_sha256": scope_sha256, "scope_key": args.scope_key, "source_proposals_sha256": hashlib.sha256(proposal_bytes).hexdigest(), "selected_scope_digest": plan["selected_scope_digest"], "selected_event_count": plan["selected_event_count"], "request_count": len(plan["requests"]), "preserved_source_proposal_count": len(proposals), "new_proposal_count": 0, "accounting_performed": False, "external_writes": False}
    documents = {"semantic-analysis.json": recovered, "timing-overlaps.json": timing, "recovery-plan.json": plan, "recovery-receipt.json": receipt}
    for name, document in documents.items():
        pipeline._write_bytes(output / name, (json.dumps(document, indent=2, sort_keys=True) + "\n").encode())
    pipeline._write_bytes(output / "preserved-source-proposals.json", proposal_bytes)
    pipeline._write_bytes(output / "source-scope-input.json", scope_bytes)
    pipeline._write_bytes(output / "source-semantic-analysis.json", source_bytes)
    return receipt


def main(argv=None):
    try:
        result = run(parse_args(argv))
    except (OSError, ValueError, KeyError, pipeline.WorkAccountingError, semantic.AnalyzerError) as exc:
        print(f"scoped semantic recovery blocked: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
