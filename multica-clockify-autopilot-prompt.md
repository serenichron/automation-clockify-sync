# Clockify evidence-grounded reconciliation — dry run

Run the Clockify work-accounting process in dry-run mode for aggregate issue
SER-651. Calendly is optional only for a bounded historical recovery explicitly
recorded in coverage; future autopilot coverage treats it as a required source.

1. On the Precision runtime, resolve `$CLOCKIFY_SYNC_ROOT`, or the first host-local directory containing
   `scripts/clockify_review_run.py` from:
   - `$HOME/Work/automation-clockify-sync`
   - `$HOME/Work/serenichron/automation/clockify-sync`
2. Read `state/autopilot-runner-status.json` before starting anything. The
   systemd user service owns long-running collection and inference; the Multica
   task must never keep a 30–120 minute analyzer job in its foreground session.
3. If today's job is absent, stale, blocked pending a newly corrected route, or
   not running, start `clockify-work-accounting.service` with `systemctl --user
   start`. Do not start a second process when the service or its lock is active.
   The service recollects and retries incomplete fleet coverage after one hour,
   at most twice per scheduled job. Missing MacBook or desktop evidence is a
   temporary coverage condition, never proof of zero work. Analyze complete
   Precision and central evidence immediately. Persist missed peer intervals in
   `state/source-coverage.json`; every later primary run must expand collection
   to the earliest debt date until that peer successfully backfills it.
4. Fail closed if the root, canonical exporter, or analyzer configuration is
   unavailable.
   A minimal analyzer route probe contains no evidence. Do not enable
   `CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED=approved` unless that private-text
   egress was separately authorized for this rollout.
5. If status is `running` or `retry_scheduled`, end this Multica wake without a
   comment; a later trigger checks the same durable status. If status is
   `complete` and has no `reported_at`, read only its emitted
   `autopilot-result.json` and
   `autopilot-summary.md` during normal operation. Use cited ledger events for
   targeted exception diagnosis; never load entire session histories into the
   agent context.
6. Obey `action`:
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
7. After any required comment succeeds, run `python3
   scripts/clockify_autopilot_runner.py --mark-reported <absolute-result-path>`.
   If no comment is required, mark the result reported immediately. Never mark
   it reported before the required Multica mutation succeeds.
8. A permitted comment includes date range, runtime path and Git SHA,
   new/changed stable IDs, genuine exceptions, carried-pending count, source
   coverage, and the absolute local run path. Do not reproduce unchanged rows.

The pipeline must retain complete cited evidence, semantically split atomic
accomplishments, hydrate Fathom meetings, render 8–14 word Caveman descriptions,
use a separate pinned-Flash review for client/project, task type, effort,
consolidation boundary, and wording, and allocate human effort within cited
semantic-workstream spans without
unrelated daily gap borrowing, overlaps, gap filling, overnight bridging, or
silent trimming. Crowded demand becomes `contested_time`. A title-only meeting
becomes an exception. First/last-message extraction and one-session-one-entry
grouping are forbidden shortcuts.
EmblemStudio work is logged under the applicable Serenichron project/task type
with `ES —`; ordinary Serenichron work continues to use `SC —`.

Do not post, edit, or delete Clockify entries without a human board decision
naming the stable review item and exact action. Do not update Google Sheets,
create downstream issues, alter routing, change schedules, or change SER-651's
description, title, status, assignment, or due date during a scheduled run.
An explicitly approved Sheet refresh happens only as a separate post-run call
to `scripts/clockify_sheet_publish.py --enable-write`, after both quality and
immutable replay pass for the same source run. It must use the named monthly
tab, deduplicate stable activity-segment IDs, and preserve human decision and
notes columns. It never authorizes a Clockify write.

Finance-report publication is a separate fail-closed path. It requires a
verified period manifest, exact consumed Clockify posting receipt, fresh API and
shared-report readbacks, eligible ECB conversion evidence, a prepared contract,
and a separate approval binding the report and Slack targets. The adapter must
update then read back the report before it may upsert Slack; persist the report
receipt before Slack so a delivery failure retries only Slack. `publication_deferred`
means readiness or approval is missing, `publication_incomplete` means the
verified report awaits Slack retry, and `published` is idempotent by the bound
contract identity. No scheduler, service, timer, launchd artifact, production
transport, or external publication is authorized by this prompt.
