"""Portable native recovery graph; only provider acquisition is substituted."""
import argparse
import datetime as dt
import json
import shutil
from pathlib import Path
from unittest import mock


def build(release, root, delivery_file):
    from scripts import clockify_review_cycle as cycle, clockify_review_run as review
    from scripts import clockify_source_debt_recover as recovery, clockify_sync_collect as collector
    from scripts import collector_receipts, source_coverage
    import runpy
    import sys
    sys.path.extend((str(Path(delivery_file).parent.parent), str(Path(delivery_file).parent)))
    make_run = runpy.run_path(str(delivery_file))["make_run"]
    root, release = Path(root), Path(release)
    runs, state = root / "runs", root / "state"
    review._configure_runs_root(runs)
    config = {"root": str(release), "runs_dir": str(runs), "state_dir": str(state),
              "recovery_since": "2026-09-07", "timezone": "Europe/Bucharest", "cache": str(root / "cache"),
              "routing": str(release / "routing.json"), "calendly_optional": True,
              "workspace_id": "workspace-1", "member_id": "member-1",
              "corrections": str(root / "corrections.jsonl"),
              "acceptance": str(root / "acceptance.jsonl")}
    for name in ("corrections.jsonl", "acceptance.jsonl"):
        (root / name).write_bytes(b"")
    config["_runtime_identity"] = collector.collector_runtime_identity()
    period = cycle._ensure_period(config, state, "2026-09-07", "2026-09-09", bind_inputs=True)
    zone = collector.BUCHAREST
    since, until = (dt.datetime(2026, 9, day, tzinfo=zone) for day in (7, 9))
    planned = collector.plan_slices(since, until, zone=zone)
    routing, fleet = (json.loads((release / name).read_bytes()) for name in ("routing.json", "fleet.json"))
    compatibility = collector._backlog_compatibility_version(routing, fleet, calendly_optional=True,
                                                            coordinator="omarchy-precision")
    identity = collector.BacklogIdentity(collector.iso_utc(since), collector.iso_utc(until),
                                        zone.key, 2, compatibility)
    checkpoints = collector.collector_checkpoint_root()
    checkpoints.mkdir(parents=True, exist_ok=True, mode=0o700)
    checkpoints.chmod(0o700)
    store = collector.BacklogStore(checkpoints)
    backlog = store.open(identity, planned)
    parent = collector._slice_run_dir(planned[0], compatibility)
    peer = {"machine": "macbook", "status": "error", "complete": False,
            "collector_contract": "canonical_export_v1", "claude_bursts": [],
            "hermes_sessions": [], "hermes_db_sessions": [], "codex_sessions": [],
            "repository_events": [], "repository_evidence_status": "unavailable", "errors": ["timeout"]}

    def finish(run):
        scratch = root / "scratch"
        result_path = make_run(root, "pipeline", runs_dir=scratch, replay=False, proposals=[],
                               ledger_from=run, snapshots_from=run, record_checkpoint=False,
                               runtime_identity=config["_runtime_identity"])
        for name in ("semantic-analysis.json", "work-accounting-result.json", "quality_report.json",
                     "review-snapshot.json", "proposals.json", "ambiguous.json", "fathom-reconciliation.json"):
            shutil.copyfile(result_path.parent / name, run / name)
        bundle = collector_receipts.build_completion_bundle(run, slice_=planned[0])
        collector_receipts.write_completion_bundle(run / "completion-bundle.json", bundle)
        result = json.loads(result_path.read_bytes())
        result.update(run_id=run.name, run_dir=str(run), completion_bundle_digest=bundle.bundle_digest,
                      completion_bundle=bundle.document())
        result["paths"] = {key: str(run / Path(value).relative_to(result_path.parent)) if value else None
                           for key, value in result["paths"].items()}
        report = json.loads((run / "run-report.json").read_bytes())
        if "source_debt_recovery" in report:
            transition = report["source_debt_recovery"]
            result["source_debt_recovery"] = {"source": "peer/macbook", "attempt_id": transition["attempt_id"],
                                              "status": "complete", "transition_digest": transition["transition_digest"]}
        (run / "autopilot-result.json").write_text(json.dumps(result) + "\n")
        return cycle._validate_stage(config, run / "autopilot-result.json", "2026-09-07", "2026-09-09",
                                     replay=False, expected_snapshot_digests={name: cycle._digest(run / name)
                                         for name in recovery.RECONCILIATION_SNAPSHOTS})

    with mock.patch.object(collector, "fetch_clockify", return_value={"status": "ok", "complete": True, "entries": []}), \
         mock.patch.object(collector, "fetch_fathom", return_value={"status": "ok", "complete": True, "meetings": []}), \
         mock.patch.object(collector, "fetch_multica_issues", return_value={"status": "ok", "complete": True, "issues": []}), \
         mock.patch.object(collector, "load_env_file", return_value={"_missing": True}), \
         mock.patch.object(collector, "machine_is_local", return_value=False), \
         mock.patch.object(collector, "collect_remote_sessions", side_effect=lambda *a, **k: dict(peer)):
        collector._collect_slice(argparse.Namespace(enrich=False, calendly_optional=True), routing, fleet,
            {"_missing": True}, {"_missing": True}, since, until, "fixture",
            collector.PageCheckpointStore(backlog.directory / "source-checkpoints"), parent,
            calendly_env={"_missing": True}, coordinator="omarchy-precision")
        collector._write_pending_slice_finalization(parent, identity, planned[0])
        for name, path in (("period-manifest.json", period), ("routing.json", release / "routing.json"),
                           ("review-corrections.jsonl", root / "corrections.jsonl"),
                           ("review-acceptance.jsonl", root / "acceptance.jsonl")):
            shutil.copyfile(path, parent / name)
        parent_stage = finish(parent)
        store.record_complete(backlog, planned[0].slice_id, parent / "completion-bundle.json",
                              cycle._digest(parent / "completion-bundle.json"))
        interval = source_coverage.SourceInterval("peer/macbook", identity.since_utc, identity.until_utc,
                                                  planned[0].slice_id, compatibility)
        debts = source_coverage.SourceDebtStore()
        debt = debts.record_failure(interval, failure_class="peer_unavailable", retryable=True,
                                    resume_state_digest="sha256:" + "a" * 64, attempted_at="2026-09-10T00:00:00Z")
        record = {"until": "2026-09-09", "source_parent": parent_stage}
        attempt, command = cycle._recovery_attempt(record, debt, parent_stage, config)
        peer.update(status="ok", complete=True, repository_evidence_status="complete", errors=[])
        child = recovery.recover(parent, "peer/macbook", attempt["attempt_id"],
                                 expected_parent_routing_digest=parent_stage["snapshot_digests"]["routing.json"]).run_dir
        review._snapshot_recovery_inputs(child, parent)
        stage = finish(child)
        receipt = recovery.seal_recovery_receipt(child)
        review.verify_source_debt_recovery_completion(child, parent_run_dir=parent,
                                                      source="peer/macbook", attempt_id=attempt["attempt_id"])
        record.update(source=stage, recovery_parents={debt.debt_id: parent_stage})
        record["recovery_attempts"][debt.debt_id].update(phase="finished_complete", result_path=stage["result_path"],
            result_digest=stage["result_digest"], returned_bundle_digest=stage["bundle_digest"],
            requested_source_outcome="complete", recovery_receipt_path=str(receipt.path), recovery_receipt_digest=receipt.digest)
        debts.record_complete(interval, completion_bundle_digest=stage["bundle_digest"], completed_at="2026-09-10T01:00:00Z")
    (state / "review-cycle-state.json").write_text(json.dumps({"schema_version": cycle.SCHEMA_VERSION,
        "completed_through": None, "scheduled_through": "2026-09-09", "next_work_class": "routine",
        "slices": {"2026-09-07": record}}) + "\n")
    source_coverage.write(state / "source-coverage.json", debts.document())
    (root / "fixture.json").write_text(json.dumps({"config": config, "parent": str(parent), "child": str(child),
        "receipt": str(receipt.path), "backlog": str(backlog.directory / "backlog-manifest.json"),
        "debt_id": debt.debt_id, "command": command}) + "\n")
