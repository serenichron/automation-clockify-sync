# Clockify reconciliation workflow

Purpose: reconcile Vlad's direct interactive work and meetings against existing
Clockify entries, then maintain a stable, approval-gated review queue.

This repository is the canonical implementation. The collector lives only in
`scripts/clockify_sync_collect.py`; the top-level `clockify_sync_collect.py` is
a compatibility wrapper.

## Safe local workflow

```bash
cd /Users/blackthorne/Work/automation-clockify-sync
python3 scripts/clockify_review_run.py
```

The orchestration command runs the collector, quality gate, and durable-state
reconciliation in order. It prints the absolute path to
`autopilot-result.json`. The lower-level commands remain available for targeted
diagnosis.

Outputs:

- `runs/<run-id>/proposals.json`: raw candidates from this collection;
- `runs/<run-id>/quality_report.json`: read-only quality findings;
- `runs/<run-id>/review-snapshot.json`: actionable delta against durable state;
- `runs/<run-id>/autopilot-result.json`: deterministic action contract;
- `runs/<run-id>/autopilot-summary.md`: compact new/changed or coverage summary;
- `runs/<run-id>/review-current.csv`: stable-ID export of the complete active
  review for local inspection or an explicitly approved Sheet patch;
- `state/review-items.json`: mutable local review state, intentionally ignored
  by Git.

`review-snapshot.json` categorizes items as `new`, `changed`,
`carried_pending`, or `resolved_disappeared`. A zero-candidate run never closes
pending work. Source failures remain visible as coverage warnings.

`autopilot-result.json` uses one of four actions:

- `no_comment`: healthy coverage and no actionable delta;
- `review_delta`: one or more new or changed review items;
- `coverage_warning`: incomplete evidence, whether or not candidates exist;
- `blocked`: quality identity/provenance failed and durable state was not
  updated.

The compact summary never reproduces `carried_pending` row content.

## Safety contract

- Collector, quality, and review-state steps do not write to Clockify.
- The quality command never updates Google Sheets or any other external system.
- Clockify posting requires an explicit board decision for each stable review
  item.
- Sheet synchronization, Multica issue mutation, schedule changes, deployment,
  and fleet rollout are separate guarded actions.
- Do not claim a fix is live until the runtime path and Git SHA are read back on
  every collector host.

## Verification

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile \
  clockify_sync_collect.py \
  scripts/clockify_sync_collect.py \
  scripts/clockify_sync_quality.py \
  scripts/clockify_review_state.py \
  scripts/clockify_review_run.py
git diff --check
```

The approval-gated production sequence and exact live IDs are documented in
[`clockify-review-rollout.md`](clockify-review-rollout.md). Re-read live state
before using it; the snapshot is intentionally not treated as current truth.
