# Clockify evidence-grounded reconciliation — dry run

Run the Clockify work-accounting process in dry-run mode for aggregate issue
SER-651. Calendly is excluded.

1. Resolve `$CLOCKIFY_SYNC_ROOT`, or the first host-local directory containing
   `scripts/clockify_review_run.py` from:
   - `$HOME/Work/automation-clockify-sync`
   - `$HOME/Work/serenichron/automation/clockify-sync`
2. Fail closed if the root, canonical exporter, required source coverage, or
   analyzer configuration is unavailable.
   A minimal analyzer route probe contains no evidence. Do not enable
   `CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED=approved` unless that private-text
   egress was separately authorized for this rollout.
3. Run `python3 <resolved-root>/scripts/clockify_review_run.py`.
4. Read only the emitted `autopilot-result.json` and
   `autopilot-summary.md` during normal operation. Use cited ledger events for
   targeted exception diagnosis; never load entire session histories into the
   agent context.
5. Obey `action`:
   - `no_comment`: make no Multica mutation.
   - `review_delta`: post one concise comment containing only new/changed stable
     `rvi-...` items and genuine exceptions.
   - `review_exceptions`: post the clean-batch ID/count and only the detailed
     active exceptions.
   - `review_batch`: post only the clean-batch ID/count; do not print its clean
     descriptions.
   - `coverage_warning`: report incomplete sources and delta counts. Never infer
     that a missing source contains no work.
   - `blocked`: report the contract failure and stop.
6. A permitted comment includes date range, runtime path and Git SHA,
   new/changed stable IDs, genuine exceptions, carried-pending count, source
   coverage, and the absolute local run path. Do not reproduce unchanged rows.

The pipeline must retain complete cited evidence, semantically split atomic
accomplishments, hydrate Fathom meetings, render 8–14 word Caveman descriptions,
and allocate human effort within cited semantic-workstream spans without
unrelated daily gap borrowing, overlaps, gap filling, overnight bridging, or
silent trimming. Crowded demand becomes `contested_time`. A title-only meeting
becomes an exception. First/last-message extraction and one-session-one-entry
grouping are forbidden shortcuts.

Do not post, edit, or delete Clockify entries without a human board decision
naming the stable review item and exact action. Do not update Google Sheets,
create downstream issues, alter routing, change schedules, or change SER-651's
description, title, status, assignment, or due date during a scheduled run.
