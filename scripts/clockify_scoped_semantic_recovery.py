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
    events = pipeline._with_semantic_route_hints(events, routing)
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
    options = dict(primary=primary, cache=cache, review_taxonomy=pipeline._semantic_review_taxonomy(routing), targets=targets, source_semantic_sha256=source_sha256, selected_evidence_ids=selected)
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
