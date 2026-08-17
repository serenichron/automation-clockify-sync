# Clockify Reconciliation and Publication Manifest Design

**Date:** 2026-08-17

**Status:** Approved design

## Purpose

Recover Vlad's complete evidence-supported August 1-15, 2026 work, post the
approved missing time without duplicates, correct the finance report, and make
future Clockify reconciliation and bimonthly reporting fail closed when a
period is incomplete or stale.

The same architecture must serve the normal one- or two-day review cadence and
exceptional backlogs. Backlog recovery may take longer and may finish in
independent slices, but it must not use a different correctness model.

## Approved Decisions

- The publication readiness gate is owned by this repository. The existing
  external bimonthly reporter remains a thin adapter that may act only on an
  approved `publication_authorized` contract.
- A single end-to-end reconciliation manifest binds collection, coverage,
  semantic review, approval, Clockify posting, live readback, shared-report
  verification, currency conversion, and publication.
- Calendly recordings are first-class meeting evidence. A Calendly recording
  that has no matching Fathom recording is handled like a Fathom meeting: its
  full recorded duration is fixed meeting time and is eligible for Clockify.
- A meeting present in both Calendly and Fathom is one meeting, not two.
  Cross-source deduplication is required only for such duplicate recordings.
- When a recording contains evidence of work for multiple clients or projects,
  semantic analysis may split the fixed duration across projects. Every split
  must sum exactly to the recording duration and retain the same source
  recording identity.
- Reports preserve every native currency bucket. Each bucket is also converted
  to USD using a receipt-bound exchange-rate quote, and the converted values
  are summed into one explicit USD-equivalent grand total.
- Unavailable Desktop evidence remains an explicit coverage limitation and is
  never interpreted as zero work.

## Scope Decomposition

The goal is implemented as four testable subprojects that share one manifest
contract:

1. **August recovery:** repair only missing Fathom partitions, collect and
   reconcile Calendly recordings, recover available device evidence, resolve
   the ten-minute report residual, and prepare a clean review packet.
2. **Reconciliation coordinator:** bind source coverage, slice artifacts,
   semantic exceptions, human decisions, replay, approval, posting, and
   readback into an append-only period history.
3. **Runtime resilience:** add exact source-interval debt, per-debt retry state,
   child-process timeouts, failure receipts, and digest-bound downstream slice
   artifacts while preserving existing page checkpoints and caches.
4. **Publication gate:** produce an immutable publication contract that the
   external shared-report and Slack adapter can consume idempotently.

Each subproject produces usable, independently verifiable software, but the
August correction is not complete until all four reach the relevant gate for
the August 1-15 period.

## Architecture

### Existing Layers Retained

The design keeps the proven pipeline:

`page checkpoint -> collection slice -> evidence ledger -> semantic analysis -> allocation -> review -> quality -> replay -> guarded post`

No database, generalized workflow engine, or replacement collector framework
is introduced. Existing content-addressed analyzer and portfolio caches remain
authoritative and reusable.

### New Period Coordinator

A focused period coordinator sits above the existing layers. It owns no raw
private evidence. It references immutable artifacts by absolute path, schema,
and SHA-256 digest.

The coordinator derives the current state from append-only events:

`collecting -> reconciling -> awaiting_review -> approved -> posting -> verifying -> publication_prepared -> publication_authorized -> published`

Failure or uncertainty does not advance the state. Instead, the coordinator
records one of:

- `coverage_incomplete`
- `semantic_exceptions`
- `awaiting_approval`
- `post_interrupted`
- `readback_mismatch`
- `report_mismatch`
- `currency_quote_unavailable`
- `publication_deferred`

The period identity contains the Clockify workspace and user identities, local
accounting timezone, half-open interval, and correction revision. For the
current recovery it is Vlad, Europe/Bucharest, `[2026-08-01T00:00,
2026-08-16T00:00)`, revision 1.

### Period Manifest

The derived manifest contains:

- schema and compatibility versions;
- member, workspace, timezone, period start/end, and revision;
- requested and completed collection slices;
- exact per-source interval coverage and active coverage debt;
- evidence-ledger, semantic-analysis, accounting, quality, review, replay, and
  acceptance artifact identities;
- Fathom and Calendly recording inventories and their reconciliation statuses;
- unresolved semantic, routing, overlap, billability, and device exceptions;
- human review decision and approval receipt identities;
- routing snapshot and correction-log digests;
- Clockify post attempt and final live-readback receipts;
- shared-report filter, refresh, totals, and readback receipts;
- native currency totals, FX quote receipt, converted USD values, and USD grand
  total;
- publication contract and publication receipt identities.

Every transition checks the referenced artifacts again. A mutable path whose
content digest changes invalidates the derived state.

## Source Collection and Coverage

### Fathom

Fathom retains its existing page checkpoints, semantic hydration, fixed meeting
intervals, project routing, overlap protection, and completeness manifest.
August recovery reuses sealed analyzer and portfolio caches and resubmits only
failed content-addressed partitions to the approved DeepSeek V4 Flash cloud
revision. DeepSeek Pro remains prohibited.

### Calendly Recordings

The Calendly collector is read-only and requires API configuration that can
enumerate Vlad's meeting recordings and return, at minimum:

- a stable recording or meeting identity;
- title and occurrence timestamps;
- exact recording start and end or exact duration;
- organizer and participant identities sufficient for Vlad eligibility;
- available recording semantics such as transcript, summary, or metadata;
- pagination and completeness information.

A scheduled Calendly event without recording evidence is not automatically
billable time under this design. It is recorded as an explicit
`scheduled_without_recording` exclusion so it can be reviewed separately
without inventing duration.

If the configured Calendly API cannot expose recordings or exact duration, the
collector reports a capability gap and the period remains incomplete. It must
not substitute scheduled event length silently.

Calendly-only recordings pass through the same semantic, routing, fixed-time,
quality, review, and posting contracts as Fathom recordings.

### Fathom/Calendly Deduplication

Deduplication compares only Fathom and Calendly recordings. It uses the
strongest available evidence in this order:

1. shared provider or recording identity;
2. explicit cross-provider meeting identity or join URL;
3. same Vlad occurrence satisfying the `meeting-dedup/v1` participant and
   start/end-window rule below;
4. otherwise an ambiguity requiring human review.

The fallback in step 3 is `meeting-dedup/v1`: after excluding Vlad's own
normalized identities, both sources must expose the same non-empty normalized
participant set, and both the start and end timestamps must differ by no more
than five minutes. Missing participant data, a larger boundary difference, or
multiple candidate matches is ambiguous rather than duplicate. The algorithm
version and tolerance are recorded in the reconciliation artifact and period
manifest so replay uses the identical rule.

One canonical meeting record retains both source identities and both provenance
digests. Fathom semantic content is preferred when it is richer; Calendly may
fill missing semantics. Timing is accepted only when the two sources agree
under `meeting-dedup/v1`'s five-minute start/end rule. A timing conflict is not
averaged or trimmed; it becomes an exception.

### Multi-Project Meeting Splits

A meeting is unsplit by default. A split is allowed only when timestamped
recording semantics cite distinct client/project workstreams, provide reliable
topic-change boundaries, and the Flash reviewer validates each route and
allocation. Human approval may resolve an ambiguity but cannot replace missing
evidence for a duration share. Split segments must:

- be positive five-minute-granularity durations except for a final exact
  remainder required to preserve the source duration;
- sum exactly to the authoritative full meeting duration;
- share the canonical meeting identity;
- carry distinct project/task routes and evidence citations;
- remain non-overlapping and collectively cover the full fixed interval.

If timestamped evidence cannot support a split, the whole meeting is routed to
one evidenced project or remains an explicit routing exception.

### Devices and Coverage Debt

Coverage is tracked by exact source and half-open interval, not only by source
and first missing day. A failed source slice remains debt until that same
interval is recollected under compatible collector code and its complete
receipt verifies.

For August:

- existing complete MacBook and Precision evidence through August 14 is
  retained;
- August 15 MacBook evidence is recollected if available;
- existing Precision evidence is reused and agent/runtime-only activity remains
  excluded;
- Desktop remains explicitly unavailable unless it becomes reachable later.

Desktop unavailability does not prevent posting evidence-supported time if the
human approves the residual coverage limitation, but the exception must remain
visible in the approval and publication contracts.

## August Recovery Flow

1. Create the August period identity and import the preserved source, review,
   post, API-ledger, and diagnostic artifact digests.
2. Reconcile the current live Clockify ledger with the preserved 172-entry
   ledger and the shared report to identify the exact ten-minute difference.
   Check filters, rounding, running/deleted entries, user/workspace scope,
   refresh time, and inclusion settings. No guessed adjustment is allowed.
3. Repair only the nine failed Fathom partitions, resolve the two project-route
   exceptions, and review the sixteen uncovered overlap minutes.
4. Configure read-only Calendly recording access, collect the full August
   interval, reconcile duplicates with Fathom, and surface Calendly-only
   recordings for the same review path.
5. Recollect recoverable August 15 MacBook evidence and retain the explicit
   Desktop coverage limitation.
6. Produce one clean human review packet containing every new or changed row,
   every ambiguity, source coverage, native durations, routing, billability,
   and expected currency impact. Do not estimate costs for unrouted rows.
7. After human decisions, run quality and immutable replay against byte-identical
   source evidence and sealed model decisions.
8. Create an approval receipt bound to the exact portfolio, quality, replay,
   routing, Clockify target, period, coverage exceptions, and operation.
9. Preflight the existing poster, then execute only the approved digest. Resume
   from append-only post events and live exact-match readback; never blindly
   retry an ambiguous POST.
10. Re-read the full Clockify period and require exact agreement with the final
    posting receipt.
11. Derive the report duration and native currency buckets from the verified
    Clockify report data, attach an FX quote receipt, compute the USD-equivalent
    grand total, and emit `publication_prepared`.
12. Record a separate approval receipt that explicitly authorizes the exact
    shared-report correction and Slack correction bound to that contract, then
    emit `publication_authorized`.
13. The external adapter idempotently refreshes or updates the shared report,
    re-reads it, and requires its filters, duration, and native currency buckets
    to match the contract. Only after that readback passes may the adapter post
    or update Slack. The coordinator verifies both receipts before `published`.

## Approval and Posting Receipts

Approval is a first-class immutable artifact. It contains:

- approver identity and timestamp;
- member, workspace, period, and operation;
- portfolio, quality, replay, routing, correction-log, and coverage digests;
- approved residual exceptions, including unavailable Desktop evidence;
- expiry and consumption semantics;
- exact intended external mutation.

A Clockify posting approval may be single-use. The separate publication
approval authorizes one exact idempotent operation bundle—shared-report
correction followed by Slack correction—through its expiry. It remains usable
for the same bundle and idempotency identity after partial success, including a
verified report update followed by a Slack failure. It is consumed only when
both report and Slack receipts verify; a retry may not change targets, content,
period, revision, or contract digest.

Posting uses append-only events rather than repeatedly replacing one mutable
JSON receipt. Each planned entry ends in exactly one derived disposition:

- `created`
- `already_existing`
- `recovered_after_ambiguous_response`
- `interrupted`

The final post receipt includes created/existing entry identities and a fresh
live-readback digest. Tampered, reordered, truncated, or target-mismatched event
history fails closed.

## Runtime Resilience

### Child-Process Timeout

The persistent autopilot runner receives a configurable total child-process
timeout. On expiry it terminates the child, waits a bounded grace period, kills
only that child if necessary, releases the lock, and writes a sanitized timeout
receipt. Completed slice receipts remain reusable.

Timeout configuration must support normal one- or two-day operation and larger
backlogs without pretending one fixed duration fits both. The parent period
retains a bounded per-slice budget and an independently configurable total
runner budget.

### Per-Debt Retry State

Each source-interval debt item owns its retry count, last failure class, next
eligible retry time, and terminal reason. One failing source cannot consume or
reset another source's retry budget. Retry exhaustion preserves debt for the
next scheduled run.

### Slice Completion Bundle

A completed slice receipt binds every downstream artifact consumed by review:
run report, evidence ledger, semantic analysis, accounting result, quality,
review snapshot, replay identity when available, schema versions, and runtime
compatibility. A later artifact drift invalidates the slice for downstream
approval without discarding the immutable collection checkpoint.

### Structured Failure Receipts

Incomplete collection emits a credential-free receipt containing source,
slice identity, checkpoint identity digest, failure class, retryability, and
safe resume state digest. Raw cursors, private filenames, payloads, credentials,
and client evidence never appear.

## Publication Gate

The repository-owned gate emits `publication_prepared` only when all required
internal conditions are true:

- exact member, workspace, timezone, period, and revision;
- every required slice has a verified completion bundle;
- source coverage is complete or every residual limitation is explicitly
  approved;
- all Fathom and Calendly recordings are represented, deduplicated, excluded
  with reason, or explicitly reviewed as exceptions;
- no unresolved routing, overlap, billability, or semantic exception remains;
- quality passes;
- immutable replay passes and binds routing, corrections, and acceptance input;
- an unexpired approval receipt matches the exact intended Clockify operation;
- posting completes and final Clockify live readback matches;
- the FX quote and every USD conversion validate.

It emits `publication_authorized` only when a separate unexpired approval
receipt explicitly names the exact shared-report mutation, Slack correction,
period, revision, publication-contract digest, and idempotency identity.

After authorization, the external adapter must refresh or update and re-read
the shared report. A report filter, duration, or native-currency mismatch stops
the adapter before Slack. The final `published` state requires verified report
and Slack receipts bound to the same contract and idempotency identity.

At the scheduled 09:00 Europe/Bucharest publication time, an unready period is
deferred. The adapter posts nothing partial and retains a machine-readable
reason. It may retry after a later ready event; the accounting pipeline is not
required to finish before 09:00 merely to satisfy the old schedule.

### Currency Contract

The publication contract contains:

- native cost buckets keyed by ISO 4217 currency code;
- the exact source value for each bucket;
- an FX quote receipt with provider identity, quote timestamp/date, base and
  quote currencies, rates, and payload digest;
- the rounding policy and converted USD value for each bucket;
- `usd_equivalent_total`, equal to the sum of the converted bucket values.

USD uses rate 1. The default non-USD source is the European Central Bank daily
reference-rate feed, using its EUR-base cross rates. The quote date is the
publication date when a quote exists; on weekends or holidays it is the latest
prior published business-day quote, no more than four calendar days old. A
different provider or freshness window requires explicit configuration and is
recorded in the contract. The receipt always identifies the effective quote
date.

Conversion is presentation and reporting metadata; it does not alter native
Clockify costs. Native and converted values are both shown. Missing currency,
missing rate, stale quote, arithmetic mismatch, or inconsistent rounding blocks
publication.

The USD grand total uses decimal arithmetic. Each native bucket is converted at
full rate precision, rounded to USD cents using half-up rounding, then the
rounded bucket values are summed. Slack and the shared finance summary render
the same contract values.

### External Reporter Adapter

With an exact `publication_authorized` contract, the external adapter may:

- refresh or update the named Clockify shared report;
- render native currency buckets and the USD-equivalent grand total;
- post or update one Slack finance-report message;
- write a publication receipt back to the coordinator.

It may not infer readiness from a runner exit code or the current Clockify
total. It verifies the digest-bound publication contract and external-mutation
approval immediately before acting. It verifies the refreshed shared report
before making any Slack call.

The idempotency identity is
`finance-report:<workspace>:<member>:<period-start>:<period-end>:<revision>`.
Retrying the same identity updates or confirms the same publication; it does
not create another Slack message. A correction revision retains the prior
publication receipt and creates an auditable new revision.

## Error Handling

- Missing or corrupt artifacts fail closed without deleting recovery state.
- Source incompleteness never becomes zero activity.
- Ambiguous semantic or routing results enter human review; they are not
  silently omitted or locally guessed.
- A duplicate recording with conflicting timing or participants becomes an
  exception.
- A failed Calendly capability or incomplete pagination leaves Calendly
  coverage incomplete.
- Ambiguous Clockify POST responses trigger exact live readback before any
  retry.
- Shared-report/API discrepancies block publication until explained.
- FX failures block the converted grand total and publication.
- Slack/report adapter failures preserve the ready contract and retry with the
  same idempotency key.

## Testing Strategy

All production behavior is implemented test-first.

### Calendly and Meeting Reconciliation

- complete paginated recording collection and resume;
- scheduled event without recording is excluded with reason;
- Calendly-only recording becomes one full-duration fixed meeting;
- Fathom/Calendly duplicate becomes one canonical meeting;
- conflicting duplicate becomes an exception;
- multi-project split sums exactly to the recording duration;
- incomplete Calendly source blocks period readiness.

### Coordinator and Coverage

- exact failed source interval remains debt until exact recollection;
- independent retry budgets for two failing sources;
- completed routine and backlog slices derive the same correctness gates;
- downstream artifact drift invalidates readiness;
- corrupt or truncated append-only event history fails closed;
- Desktop limitation appears in approval and publication contracts.

### Timeout and Recovery

- hung child terminates at timeout, lock releases, and timeout receipt persists;
- grace-period kill is bounded to the owned child;
- verified slice checkpoints survive timeout and resume;
- routine and backlog timeout configurations validate independently.

### Approval and Posting

- missing, expired, wrong-target, wrong-period, wrong-operation, or digest-drifted
  approval is rejected;
- exact existing entries are not duplicated;
- ambiguous POST is reconciled through live readback;
- interrupted post resumes from append-only receipts;
- tampered receipt history is rejected;
- final live ledger matches the posting receipt.

### Publication and Currency

- stale runner state, incomplete coverage, missing receipt, period mismatch,
  report mismatch, or stale FX quote produces no publication contract;
- native USD and EUR buckets remain separate;
- every bucket converts deterministically and sums to the USD grand total;
- rounding uses decimal half-up cents and detects arithmetic drift;
- same idempotency key creates or updates only one Slack publication;
- verified report update followed by Slack failure retries only the Slack phase
  under the same unexpired publication approval and idempotency identity;
- a correction revision retains prior publication evidence;
- August 1-15 boundaries exclude August 16 in Europe/Bucharest.

### End-to-End Acceptance

One fixture covers a routine two-day period. A second covers an exceptional
multi-slice backlog with a transient source failure, one Calendly-only
recording, one Fathom/Calendly duplicate, one reviewed device limitation, a
recovered ambiguous Clockify POST, EUR and USD costs, and an idempotent report
retry.

## Operational and Safety Constraints

- Work only in the exact authorized checkout and preserved private state/cache
  references.
- Preserve private evidence, credentials, cursors, transcripts, and untracked
  runtime artifacts.
- Use only the latest approved DeepSeek V4 Flash cloud revision for private
  semantic inference. DeepSeek Pro is prohibited.
- Do not restart expensive inference blindly; reuse content-addressed caches.
- External reads needed for reconciliation are separate from guarded writes.
- Clockify creation, shared-report mutation, Slack correction, schedule changes,
  push, deployment, merge, publishing, and credential/permission changes occur
  only under the exact required approval.
- Calendly credential creation or permission grants require separate approval;
  absence of access remains an explicit capability gap.

## Acceptance Criteria

The design is complete when current evidence proves all of the following:

- every recoverable August Fathom and Calendly recording is represented once;
- every new fixed meeting duration is fully allocated or explicitly unresolved;
- the ten-minute report/API residual is explained with entry/filter evidence;
- August 15 recoverable MacBook evidence is reconciled and Desktop is visibly
  unavailable;
- the August packet passes quality and immutable replay;
- exact human approval binds the final portfolio and residual limitations;
- approved entries are posted once and match a fresh Clockify ledger;
- the refreshed shared report matches the verified period;
- native currency totals and the USD-equivalent grand total are correct;
- the stale Slack publication is corrected idempotently;
- future incomplete periods defer instead of publishing partial totals;
- routine and exceptional backlog fixtures pass the same manifest gates;
- focused and full regression suites pass;
- operational documentation is current;
- the verified implementation is committed and pushed under explicit approval.
