# Clockify review recovery rollout

Status: superseded by the evidence-grounded accounting candidate; no guarded
action in this document has been executed. The 2026-07-30 identifiers below are
historical evidence and must be re-read before any rollout.

## Candidate status — 2026-08-03

The current guarded candidate is developed on
`codex/clockify-analyzer-determinism`. Resolve its exact SHA with `git rev-parse
HEAD` immediately before every publication, deployment, and run, and bind each
manifest and readback to that SHA; any later edit creates a new candidate. Do
not infer merge, private-text egress, Sheet refresh, Clockify write, Multica
mutation, or schedule approval from a validation deployment. Calendly remains
excluded. Clockify remains the system of record; the process only creates local
review artifacts until the applicable guarded approval is granted.

The standard local command is `python3 scripts/clockify_review_run.py`, which
defaults to `--review-mode shadow_all`. In that mode the full active review
denominator is visible, including ambiguous rows. `--review-mode
exceptions_only --acceptance-ledger state/review-acceptance.jsonl` is locked
until `scripts/review_acceptance.py status --ledger
state/review-acceptance.jsonl` reports `exceptions_only_eligible: true`.
Clean rows then appear only as a content-addressed batch count/ID; genuine
ambiguous rows remain detailed. Neither mode writes Clockify or Google Sheets.

## Verified live baseline — 2026-07-30

- Canonical issue: `SER-651`
  (`c4383e65-3967-4ae2-8a44-8d398f0303ec`), still carrying the July 29
  33-row/1,021-minute description.
- Authoritative schedule candidate:
  - Autopilot: `6e1bd5f6-06ee-4824-9dd0-d06147574a7a`
  - Agent: `11af6181-aa36-4c10-9c55-71aee5b38e6b`
  - Trigger: `5ca60571-6007-465a-bb8e-3c49cd3641e3`
  - Schedule: weekdays 07:00 Europe/Bucharest
  - Latest run `eed5fa36-e873-4bce-9e49-2d5eeaac46ac` failed because
    `/home/blackthorne/.local/bin/hermes` does not exist.
- Redundant schedule:
  - Autopilot: `f10f0de3-7288-4f41-ab28-495baae5371b`
  - Agent: `65e964bf-2425-431f-a919-b7a77c81e0c4`
  - Trigger: `79d429fe-2ab3-45d3-a8db-f4c122a17f6d`
  - Schedule: weekdays 18:30 Europe/Bucharest
  - Its three latest runs failed; the agent still selects unsupported
    `custom:headroom:deepseek-v4-flash:cloud`.
- Both live agents still mention SER-106, host-specific stale paths,
  auto-closing the issue, and run-local proposal IDs. Neither mentions stable
  review item IDs.
- Both autopilots still require replacing the SER-651 description and adding a
  comment on every run.
- Proven supported execution target:
  - Runtime: Codex (omarchy-precision)
    `22c4ede4-8141-4db1-b20d-2909cec902bb`
  - Model: `gpt-5.6-luna`
  - Existing Luna agents on this runtime have completed tasks successfully.

## Preconditions

The candidate behavior and fail-closed contracts are defined in `README.md` and
the versioned files under `schemas/`. This rollout document does not replace the
noise, Fathom eligibility, analyzer fallback, deterministic description,
allocation, or correction-regression contracts recorded there.

1. Obtain direct board approval for each applicable guarded phase. Validation
   approval may cover feature-branch publication, exact-SHA fleet deployment,
   non-private synthetic analyzer evaluation, and the read-only shadow run. It
   does not imply merge, Multica configuration, Sheet refresh, Clockify posting,
   or scheduling approval.
2. Re-read all IDs and statuses above. Abort on drift.
3. Run:

   ```bash
   python3 -m unittest discover -s tests -v
   python3 -m py_compile \
     clockify_sync_collect.py \
     scripts/clockify_sync_collect.py \
     scripts/evidence_ledger.py \
     scripts/semantic_analyzer.py \
     scripts/work_allocator.py \
     scripts/caveman_renderer.py \
     scripts/work_accounting_pipeline.py \
     scripts/review_corrections.py \
     scripts/review_acceptance.py \
     scripts/analyzer_evaluation.py \
     scripts/analyzer_live_evaluation.py \
     scripts/clockify_sync_quality.py \
     scripts/clockify_review_state.py \
     scripts/clockify_review_run.py
   git diff --check
   ```

4. Confirm `state/review-items.json` and `runs/` are ignored and absent from the
   commit.
5. Resolve and record the clean candidate SHA. Every deployment, manifest, run,
   and readback must use that exact SHA.
6. After validation deployment, run
   `scripts/analyzer_live_evaluation.py` on Precision for the intended primary
   route and any configured fallback. Require the evidence-free probe plus a
   passing digest-bound scorecard across the fixed synthetic atomic, split,
   merge, title-only-meeting, and noise cases. Configuration alone is not proof.
7. Obtain a separate explicit privacy decision before setting
   `CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED=approved`. The minimal probe contains
   no evidence and does not authorize private agent/session, meeting, or issue
   prose to leave the machine.
8. Run one complete July 1–August 3 read-only shadow reconciliation, then replay
   the same immutable inputs and analyzer versions through the same durable
   state. Require complete canonical fleet evidence, Fathom reconciliation,
   strict non-overlap, and replay `0 new / 0 changed` before any issue or Sheet
   refresh.
9. Do not claim acceptance until all active rows, including ambiguous ones,
   have approve/skip/modify dispositions and every skip/modify has a
   criticality assessment. `exceptions_only` requires one complete >=90%
   baseline followed by two distinct later consecutive >=95% guarded periods,
   with complete sources, passing quality and analyzer scorecards, stable replay, and no critical
   routing, description-truth, meeting, or allocation error.

## Guarded validation order

1. Publish only the feature branch. Do not merge it. Fetch the remote and verify
   that the remote feature-branch SHA equals the approved candidate SHA.
2. Deploy that exact feature-branch SHA to Mac, Precision, and Desktop using the
   paths below. Preserve `runs/`, durable state, environment files, credentials,
   and unrelated changes. Read back Git HEAD on Git worktrees and compare a
   tracked-file hash manifest for the Desktop non-Git copy.
3. Run the complete verification suite on every collector host.
4. Run the selected analyzer route through
   `scripts/analyzer_live_evaluation.py`. Stop on any probe, schema, partition,
   concrete-concept, rendering, or review-decision-stability failure; retain its synthetic capture
   and sealed scorecard. Harmless rationale wording may vary, but evidence
   disposition, partitions, lifecycle, effort, and confidence may not.
   An extraction rejection exhausted across every configured qualified route may
   continue only through bounded, turn-aware deterministic partition recovery,
   including when only one qualified primary is configured. User intent must stay
   with its following assistant result. Any indivisible or exhausted recovery
   leaf remains a blocking `analyzer_failure`; do not treat bisection itself as a
   model route. An indivisible transport timeout gets at most three distinct,
   content-addressed recovery requests on the same qualified route, each gated
   by a fresh probe before live transport. Exhaustion becomes a complete
   evidence-bound `analyzer_failure` exception. A connection loss or retryable
   HTTP status (`408`, `425`, `429`, or `5xx`) gets the same bound. Probe,
   authentication, and other HTTP failures still block.
   Repeated-workstream synthesis exhausted across the qualified route set becomes
   a visible `analyzer_synthesis_failure`; exhausted retryable synthesis transport
   does the same so unresolved duplicates cannot enter proposals.
5. Enable `CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED=approved` only when the exact
   validation approval includes redacted private semantic-text egress, and only
   for the approved run scope.
6. On Precision, create fresh validation state from the complete July 1–August
   3 read-only shadow backfill using explicit paths under
   `state/validation/july-baseline/`. Do not copy Mac state and do not mutate the
   existing production review state.
7. Replay the exact immutable evidence and model versions through that same
   validation state and its append-only `analyzer-cache-v2.jsonl` with
   `scripts/clockify_review_run.py --replay-from
   /absolute/path/to/runs/<first-run-id>`. Require a passing
   `replay-integrity.json`, identical sealed cache-decision digests, `new=0`,
   `changed=0`, all unresolved active rows as
   `carried_pending`, complete source manifests, passing quality, and explicit
   Fathom reconciliation. A second live collection is not replay evidence.
8. Attach every passing route scorecard to the complete evidence-bound review
   denominator. Do not refresh Google Sheets or record a passing acceptance
   period until every active row has a human disposition.

## Precision runner requirement

Guarded month-scale runs must use the reviewed user unit at
`ops/systemd/clockify-work-accounting.service` with a private environment file,
user lingering enabled, and `scripts/clockify_accounting_runner.py` as its only
entry point. Verify the exact deployed SHA, unit file hash, `Linger=yes`, active
service state, cache growth, and `runner-status.json`. Do not use an SSH-owned
foreground process. The service may restart unexpected crashes; exit status 2
is a durable known blocker and must not restart-loop.

## Separately approved production rollout order

Only after guarded validation evidence is accepted and a separate production
approval is granted:

1. Create/review the PR, merge the exact accepted candidate to `master`, fetch
   the remote, and verify the remote master SHA.
2. Pause redundant autopilot
   `f10f0de3-7288-4f41-ab28-495baae5371b`. Do not delete it. Verify its status
   is `paused`; retain its trigger for rollback evidence.
3. Update authoritative agent
   `11af6181-aa36-4c10-9c55-71aee5b38e6b`:
   - runtime `22c4ede4-8141-4db1-b20d-2909cec902bb`;
   - model `gpt-5.6-luna`;
   - instructions exactly from
     `multica-clockify-analyst-agent-instructions.md`.
4. Update authoritative autopilot
   `6e1bd5f6-06ee-4824-9dd0-d06147574a7a` with the exact content of
   `multica-clockify-autopilot-prompt.md`. Keep it `run_only`, active, and on the
   existing 07:00 trigger.
5. Deploy the exact remote master SHA:
   - Mac:
     `/Users/blackthorne/Work/automation-clockify-sync`
   - Precision:
     `/home/blackthorne/Work/serenichron/automation/clockify-sync`
   - Desktop:
     `/home/blackthorne/Work/serenichron/automation/clockify-sync`

   Preserve `runs/`, `state/review-items.json`, environment files, and all
   credentials. The Desktop non-Git copy must be populated from an archive of
   the exact SHA and verified by a tracked-file hash manifest.
6. Promote the accepted validation state on Precision only through an explicit,
   verified state-migration step; never copy Mac state or silently overwrite the
   authoritative review ledger.
7. Run the accepted bundle through the authoritative durable state and require:
   - `new=0`;
   - `changed=0`;
   - active items appear only as `carried_pending`;
   - action `no_comment`;
   - `should_comment=false`.
8. Append and inspect each acceptance period with
   `scripts/review_acceptance.py record` and
   `scripts/review_acceptance.py status`; do not request exceptions-only mode
   unless the integrity-checked ledger reports eligible.
9. Refresh any approved review surface by stable ID only after the shadow run
   passes. Preserve reviewer Status and Notes. Do not use a full-sheet overwrite
   and do not publish transcript-derived legacy descriptions.
10. Trigger one manual authoritative canary. It must perform no Clockify or
   Google Sheet write. With unchanged evidence it must make no SER-651
   mutation.

## Required readback

- GitHub remote master equals the candidate SHA.
- Mac and Precision Git HEAD equal the candidate SHA and are clean.
- Desktop tracked-file hashes equal `git archive <candidate-sha>`.
- Authoritative agent readback shows:
  - Codex Precision runtime;
  - model `gpt-5.6-luna`;
  - SER-651;
  - portable root resolution;
  - stable review IDs;
  - no automatic issue close.
- Authoritative autopilot readback shows:
  - active;
  - 07:00 weekday trigger unchanged;
  - `no_comment` means no Multica mutation;
  - no issue-description overwrite loop.
- Redundant autopilot readback shows `paused`.
- Canary run status is `completed`.
- Canary action contract and SER-651 `updated_at` prove that a healthy no-delta
  run did not comment, replace the description, change status, assignment,
  title, or due date.

## Rollback

If agent execution or the canary fails:

1. Keep the redundant autopilot paused.
2. Pause the authoritative autopilot.
3. Restore the previous agent/autopilot JSON captured immediately before
   mutation.
4. Restore deployed files from the previous exact SHA
   `2774570c9c4d8825997093d42d2264fade5ea16a`.
5. Preserve the failed run bundle and durable state for diagnosis; never replace
   it with the contaminated Mac validation state.
6. Do not restore the old always-comment/description-overwrite behavior merely
   to make the schedule appear active.

Google Sheet and Clockify writes remain separate guarded actions and are
excluded from the implementation and shadow-evaluation stages.
