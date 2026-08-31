# Clockify reconciliation workflow

Purpose: reconstruct Vlad's direct interactive accomplishments and meetings
from complete evidence, allocate honest non-overlapping effort, and progress
from full-denominator shadow review to an exceptions-only, approval-gated
Clockify review queue after measured acceptance gates pass.

This repository is the canonical implementation. The collector lives only in
`scripts/clockify_sync_collect.py`; the top-level `clockify_sync_collect.py` is
a compatibility wrapper.

The source audit and process-level failure inventory are recorded in
`clockify-description-failure-taxonomy.md`.

## Safe local workflow

```bash
cd /Users/blackthorne/Work/automation-clockify-sync
python3 scripts/clockify_review_run.py
```

The orchestration command defaults to `--review-mode shadow_all` and runs the
complete process:

1. collect full cross-machine session events, commands, commit-backed artifacts,
   existing Clockify entries, Multica issue context, and hydrated Fathom records;
2. write and validate an immutable, content-addressed evidence ledger;
3. classify noise and reconstruct atomic semantic accomplishments with a
   tiered analyzer;
4. use an independent Flash review to select a configured project/task and
   render Caveman descriptions targeting 8–14 words, while local code verifies
   taxonomy membership and structural safety;
5. allocate active effort around fixed Clockify and Fathom blocks without
   overlap, gap filling, overnight bridging, or silent trimming;
6. validate quality and ingest stable review identities; shadow evaluation
   exposes the full denominator, while repeated runs emit only actionable
   deltas. Exceptions-only operation is not activated until its measured gates
   pass.

For month-scale recovery, `scripts/clockify_portfolio_review.py` adds a bounded
second pass over already reviewed, successfully allocated activities. It groups
only one project/day portfolio at a time and asks the pinned Flash model to turn
message-level commands, checks, fixes, tests, and follow-ups into invoice-worthy
accomplishments. A separate Flash call then validates client/project level, task
type, effort, consolidation boundary, and Caveman wording against the same raw
evidence. The stage retains the original non-overlapping allocation segments,
never fills gaps, writes progress to `portfolio-status.json`, and stores both
model decisions in an append-only cache. `scripts/clockify_portfolio_quality.py`
performs only structural/integrity checks and identifies rows requiring another
semantic repair; it does not replace the Flash validator with local semantics.

It prints the absolute path to `autopilot-result.json`. Lower-level commands
remain available for targeted diagnosis. Calendly recordings are first-class,
read-only meeting evidence; scheduled events without a recording are retained
only as `scheduled_without_recording` exclusions and never supply billable time.

## Guarded Google Sheet publication

Google Sheet publication is a separate post-review stage. It is never invoked
by collection, analysis, quality checks, replay, or the scheduled service. Run
it only after the source quality report and immutable replay both pass and a
human board instruction explicitly authorizes the named workbook and interval:

```bash
python3 scripts/clockify_sheet_publish.py \
  --spreadsheet-id <approved-spreadsheet-id> \
  --sheet-title "August 2026 review" \
  --template-title Proposals \
  --proposals /absolute/path/to/source-run/proposals.json \
  --quality-report /absolute/path/to/source-run/quality_report.json \
  --replay-integrity /absolute/path/to/replay-run/replay-integrity.json \
  --run-id <source-run-id> \
  --enable-write
```

Without `--enable-write`, the command validates the artifacts and emits a
no-write preview. The write path verifies the proposal count, quality status,
replay status, replay/source identity, and unique stable activity-segment IDs.
It creates the named month tab from the hidden `Proposals` template when
needed, displays duration as integer minutes, and appends only new stable IDs.
For existing IDs it updates machine-owned evidence fields while preserving the
human-owned `Disposition`, `Review Status`, and `Review Notes` cells. It never
calls Clockify. Later intervals in the same month use the same command and tab;
already published stable IDs are not duplicated.

## Guarded Clockify portfolio posting

The scheduled autopilot does not invoke the production poster. After a board-
approved portfolio has a clean repair, passing quality report, and passing
immutable replay, an operator can validate or execute that exact digest:

```bash
python3 scripts/clockify_post_approved_portfolio.py \
  /absolute/path/to/portfolio-repair.json \
  --quality-report /absolute/path/to/portfolio-quality.json \
  --replay-integrity /absolute/path/to/replay-integrity.json \
  --routing /absolute/path/to/routing.json \
  --receipt /absolute/path/to/private-receipt.json \
  --approval-receipt <board-approval-id> \
  --approval-events /absolute/path/to/approval-events.jsonl \
  --post-events /absolute/path/to/post-events.jsonl \
  --period-manifest /absolute/path/to/period-manifest.json \
  --expected-portfolio-sha256 <approved-sha256> \
  --execute
```

Omit `--execute` for a read-only preflight. Execution validates one unexpired
approval ledger entry before credentials are loaded, binding the exact portfolio,
quality, replay, routing, correction, coverage, residual-exception, target, and
period evidence. Its append-only post-event ledger writes `planned` before a
POST and one terminal result only after exact live readback; a mutable receipt
is compatibility output, never execution history. Ambiguous responses and
interrupted plans are resolved through fresh exact readback and are never
blindly reposted. The final compatibility receipt includes a full-period live
readback digest.

Outputs:

- `runs/<run-id>/evidence/evidence-ledger.json`: immutable evidence and
  completeness manifest;
- `runs/<run-id>/semantic-analysis.json`: cited atomic activities, omissions,
  analyzer provenance, and the reversible content-addressed evidence-bundle
  manifest used for local member expansion;
- `runs/<run-id>/work-accounting-result.json`: semantic, allocation, canonical
  meeting, and exception contract;
- `runs/<run-id>/allocation-report.json`: strict allocation and contested time;
- `runs/<run-id>/fathom-reconciliation.json`: legacy-named, canonical meeting
  disposition for every recording-backed meeting;
- `runs/<run-id>/review-learning-cases.json`: generalized, sanitized correction
  cases derived from the integrity-checked decision log;
- `runs/<run-id>/review-regression-cases.json`: exact local-only expectations
  bound to reviewed evidence; these are never sent to a model;
- `runs/<run-id>/review-regression-results.json`: pass/fail evidence showing
  whether previously corrected behavior was actually learned;
- `runs/<replay-run-id>/replay-integrity.json`: digest-bound proof that a
  distinct replay reused the byte-identical ledger and analyzer versions;
- `runs/<run-id>/proposals.json`: final semantic allocation candidates;
- `runs/<run-id>/legacy-*.json`: collector-only diagnostic candidates, never
  reviewable output and never carried into durable state;
- `runs/<run-id>/quality_report.json`: read-only quality findings;
- `runs/<run-id>/review-snapshot.json`: actionable delta against durable state;
- `runs/<run-id>/autopilot-result.json`: deterministic action contract;
- `runs/<run-id>/autopilot-summary.md`: compact new/changed or coverage summary;
- `runs/<run-id>/review-current.csv`: stable-ID export of the complete active
  review for local inspection or an explicitly approved Sheet patch;
- `runs/<portfolio-run-id>/portfolio-review.csv`: locally consolidated,
  invoice-reviewable activities with their original allocation segments;
- `runs/<portfolio-run-id>/portfolio-status.json`: model/revision-bound progress
  for resumable desktop execution;
- `runs/<portfolio-run-id>/portfolio-quality.json`: overlap, provenance,
  forbidden-content, duration, and semantic-repair findings;
- `state/review-items.json`: mutable local review state, intentionally ignored
  by Git;
- `state/review-corrections.jsonl`: immutable evidence-bound approve, skip, and
  modify decisions, intentionally ignored by Git.
- `state/review-acceptance.jsonl`: integrity-linked shadow/guarded period
  evidence controlling exceptions-only eligibility, intentionally ignored by Git.

Flash analysis and a separate Flash reviewer own semantic classification,
project/task recommendations, consolidation boundaries, effort judgment, and
human-readable wording. Deterministic code owns evidence identity, exact taxonomy
membership, non-overlapping placement, review identity, integrity, and safety; it
does not discard a plausible activity through a grammar-only semantic validator.
Raw evidence may overlap; proposed and existing Clockify allocations may not.
Each activity carries minimum, recommended, and maximum active-effort estimates,
with the recommended estimate normalized to five-minute timesheet granularity.
Deterministic code derives a conservative minimum/maximum safety band of roughly
two-thirds to four-thirds around that estimate, rounded outward to five minutes.
When every cited item has a complete observed interval, their interval union is
only an effort ceiling: the largest conservative band that fits below that
ceiling is used, and timing confidence is normalized to `medium`. The interval
is never treated as proof that all observed wall time was active work. Raw
low-timing model output still triggers the configured fallback or a visible
exception; normalization cannot silently turn it into an accepted proposal.
Action rendering normalizes capitalization and removes only a redundant trailing
verification verb for the same accomplishment; other compound actions still fail.
Allocation prioritizes semantic confidence and direct human-attention signals,
then places only the recommended demand inside observed spans of the cited
semantic workstream. Unrelated evidence elsewhere that day cannot widen that
placement envelope.
If capacity falls below the evidence-backed minimum, token allocations are
rolled back completely. When recommended demand still cannot fit, the process
emits `contested_time` with the full unmet demand instead of shrinking or
discarding the work. Unused capacity stays explicit and empty time remains empty.

The analyzer receives deterministic noise filtering before semantic work begins.
System messages, tool transport, heartbeats, session-control commands, pure
standing-by or approval-waiting messages, polling updates, and injected wrappers
are excluded. Substantive accomplishments that merely mention words such as
"heartbeat", "approval", or "polling" remain eligible.
For explicit conversational evidence, a non-meeting accomplishment must cite a
user instruction and an assistant result from the same session. User-only
requests and assistant-only status/autonomous output fail the semantic contract
and must be omitted or surfaced as exceptions. Meetings remain independently
eligible through their fixed Fathom evidence contract.

The primary analyzer must pass a minimal live probe and its structured response
contract. A sealed contract rejection receives exactly one content-addressed
repair request containing only its allowlisted failure category and narrow
corrective guidance; raw rejected output is never copied into that request or
cache record. Probe, authentication, non-retryable HTTP, and malformed-response
failures are not retried and block the run. A connection loss or retryable HTTP
status (`408`, `425`, `429`, or `5xx`) is sealed; after the same pinned route
passes a fresh evidence-free probe, it receives up to three distinct
content-addressed recovery requests. Exhausted request-specific transport
recovery becomes an evidence-bound exception; it never invents a proposal and
does not abort unrelated workstreams.
A timed-out extraction request is sealed in
the local cache and bisected only at a safe context or complete-turn boundary;
the identical timed-out body is never sent again. If repair still fails, a
configured stronger fallback receives the safe category and handles the bounded
chunk. A sealed semantic-contract rejection may also enter the deterministic
partition recovery below even when the primary is the only qualified route. Conflicting or
low-confidence claims that remain unresolved become explicit exceptions, never
proposals.

The required primary is the generic `deepseek-v4-flash:cloud` alias. Every live
run must additionally pin the full revision reported by the host's current
manifest so the scorecard, analyzer cache, semantic run, and replay cannot
silently mix model releases if the alias moves later. The current Precision
rollout binds revision
`6ca9e29c41ded618e527ee40e305ed5e4d8319b571d5b6695a30e1df65f103cc`.
The equivalent explicit tag `deepseek-v4-flash:0731-cloud` and its historical
revision remain accepted only for already sealed decisions. The exact model tag
is part of each cache route identity, so an in-progress sealed run must retain
the tag it started with rather than migrating accepted decisions to an alias.
The active release must pass the v17 synthetic route gate.
`deepseek-v4-pro:cloud` is not an approved route for this process.

Analyzer requests retain the fail-closed 1,450,000-byte hard ceiling. Normal
extraction uses a 250,000-byte and 250-event operational target with four
deterministically ordered workers so a large-context route does not receive an
unnecessarily broad semantic partition. A single event may exceed the target but
never the hard ceiling; it is never clipped. When every configured qualified
route rejects or times out on one bounded extraction chunk, deterministic recovery bisects only at
source-context or conversation-turn boundaries. This includes a single qualified
primary with no fallback. A user instruction always stays with its following
assistant/tool result. A single indivisible turn rejected by every configured
route becomes one local `analyzer_failure` exception with a failure digest; an
indivisible transport timeout receives exactly one content-addressed recovery
series of at most three requests on the same route with distinct seeds and
timeout markers. The identical timed-out body is never resent. Exhaustion
becomes one `analyzer_failure` exception covering the complete indivisible
evidence unit.
If every configured qualified route rejects repeated-workstream synthesis,
including a single primary with no fallback, those otherwise valid but potentially
duplicative claims become one `analyzer_synthesis_failure` exception. A synthesis
transport failure receives the same sealed, probe-gated recovery bound; exhaustion
keeps the whole candidate workstream out of proposals as one visible exception.
Synthesis candidates require the same normalized project, workstream, and
concrete object; generic model labels alone cannot combine unrelated activities.
No rejected activity can become a proposal.

Within each operational chunk, contiguous evidence from one stable session,
repository, issue, or meeting context is represented as a content-addressed
local bundle. The provider receives only request-local bundle references,
ordered redacted members, and inclusive numeric ranges. It never receives the
original ledger IDs or context identifiers. Models may split one bundle into
multiple atomic accomplishments by returning disjoint member ranges. Local code
expands those ranges to the original immutable evidence IDs and then enforces
complete exactly-once coverage before computing activity identities.

Validated analyzer decisions are stored in the append-only local
`analyzer-cache-v2.jsonl` beside the durable review state. Cache keys bind the endpoint, model, prompt/schema
versions, and complete request-body digest without storing request prose. Both
accepted responses, contract-rejected primary decisions, and extraction timeout
decisions are sealed, so an
immutable replay cannot randomly switch provider wording or fallback routing.
Every cache hit is revalidated; replayed decisions do not require another live
provider probe. Concurrent or stale writers are locked and conflicting decisions
fail closed instead of replacing or interleaving cache records.

The `rendered_description` field is required on semantic activities and starts
as `null`; deterministic routing and rendering populate it only for activities
that can become proposals. Analyzer wording is never trusted as final text. The
Caveman renderer regenerates it from prefix, action, object, and bounded outcome,
then quality checks require proposal `description` to match it exactly.

Recording eligibility requires a valid recorded meeting window and either Vlad
as recorder, organizer, or attendee. Recordings shorter than five minutes
require a transcript. A title-only record remains an exception; it cannot
support an invented outcome. Existing Clockify time reconciles a meeting only
with reciprocal overlap of at least 80%; partial conflicts remain explicit
fixed-block exceptions.

Fathom and Calendly recordings are deduplicated by `meeting-dedup/v1` before
accounting. Shared provider, event, or join identities require agreeing start
and end instants; the fallback requires the same normalized non-Vlad
participant set and each boundary within five minutes. Missing participants,
multiple candidates, or timing disagreement are explicit exceptions, never a
merged or averaged duration. A duplicate pair is one canonical meeting and
every source recording must be cited by that meeting's review allocation.
Multi-project meeting allocations are accepted only when timestamped semantic
evidence partitions the full recorded interval. Human approval or a percentage
share cannot replace a timestamped boundary.

`routing.json.member_identities` is copied into, and therefore bound by, each
immutable evidence-ledger manifest. That bound identity set controls recording
eligibility and the identities removed before participant-window fallback
matching; a later routing change cannot alter a previously collected period.
Recollect a period to apply an identity change.

When present, `duration_seconds` and second-precision `start`/`end` bounds are
authoritative for a recording, proposal, review row, and its allocation
segments. `duration_minutes` remains a display and legacy-compatibility field;
minute-only historical artifacts remain readable, while an artifact that
supplies exact seconds is checked against its exact bounds.

Review corrections are immutable and bound to stable activity plus evidence
identity. Only sanitized general rules reach the analyzer. Exact replacement
values remain local-only regression cases. A reappearing skip is removed from
every segment; a mismatching or missing reviewed activity produces one deduplicated
`correction_regression` exception and no reviewable allocation. A split
correction records the expected child count and an exact, non-overlapping
partition of the parent evidence; replay fails visibly if a child is missing or
evidence is lost, duplicated, or repartitioned.

The immutable ledger is local-only. Private agent/session, meeting, or issue
prose is denied to every analyzer transport by default. A minimal route probe
contains no evidence and remains safe to run while the gate is closed. Sending
the semantic projection requires a separate runtime decision:

```bash
export CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED=approved
```

Do not persist or enable that value merely because a route probe succeeded;
setting it authorizes redacted private prose to leave the local machine for the
configured analyzer endpoint. When explicitly enabled, cloud requests receive
only a strict semantic projection: opaque bundle references, numeric member
ranges and spans, redacted
user/assistant text, structured Fathom summary/action-item/transcript text,
safe commit subjects, and artifact basenames. Tool-call inputs, tool-result
bodies, credentials, emails, URLs, absolute paths, and hashes never cross that
boundary. Safe text is not clipped; an individually oversized projected event
blocks analysis.

Repository commits and changed-file lists remain immutable ledger evidence, but
they are corroborative-only until deterministically bound to a human session or
issue workstream. Observing a repository from a session CWD does not prove that
Vlad authored every commit in its July history: fetched upstream, dependency-bot,
and autonomous-agent commits must never become standalone Clockify activities.

Fathom collection queries records created from one day before the requested
window through its end, then filters locally by recording or scheduled
occurrence overlap. Live July validation showed Fathom records were created
between 9 minutes and 5.7 hours after their meetings; the lead covers the
boundary without crawling thousands of unrelated historical records. The
inventory records the creation-search window, occurrence filter, pagination
count, and completeness. HTTP 429 responses use one bounded
collection-wide retry budget; exhaustion records sanitized page/cursor and
retry provenance and fails closed. Multica issue evidence is fully paginated
and then bounded to issue activity in the requested accounting window.

Canonical remote session exports attest the exact collector-script SHA-256
before their evidence can be called complete. Git worktrees must also match the
coordinator Git SHA with no tracked changes; the Desktop non-Git archive is
identified explicitly and accepted only when its collector-script digest
matches. Month-scale canonical exports use a bounded, configurable timeout and
never turn a timeout or attestation mismatch into legacy metadata evidence.

A currently running Clockify entry is represented as an existing fixed block
only through the earlier of the collection snapshot and the requested end. Its
temporary boundary remains explicit provenance. If any running entry cannot be
bounded safely, Clockify coverage stays partial and accounting fails closed.

### Recovery-only collector checkpoints

The collector keeps private, local checkpoints for paginated source responses
so an interrupted recovery can resume without repeating already committed
pages. The default root is the private repository state directory
`state/collector-checkpoints`. A service may set an explicit private root with
`CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT=/absolute/private/state/collector-checkpoints`.
Keep the live directory private and mode-restricted; checkpoint contents are
recovery evidence, not review artifacts, and must not be shared with an
analyzer or client.

Collection windows are split oldest-first into slices of at most two local
calendar days. Each completed slice is recorded independently, so its
validated report is immediately available for review even if a later slice
fails. A subsequent run retries the oldest incomplete slice first. A source or
slice failure preserves its incomplete checkpoint and fails closed: it never
deletes recovery state, fabricates a result, or marks an incomplete slice as
complete. Cleanup is never automatic during collection.

After review, an operator may remove only completed checkpoints older than a
UTC date boundary by passing an explicit absolute root:

```bash
python3 scripts/clockify_sync_collect.py cleanup-checkpoints \
  --completed-before YYYY-MM-DD \
  --checkpoint-root /absolute/private/state/collector-checkpoints
```

The command refuses relative, missing, or non-directory roots, and reports
aggregate counts plus checkpoint identity digests only. It never prints page
filenames, payloads, cursors, or credentials. Incomplete and corrupt
checkpoints are preserved for fail-closed recovery and are not removed by this
command.

`state/source-coverage.json` retains per-peer coverage debt. If MacBook or
desktop collection is unavailable, the current run still analyzes complete
Precision and central evidence, and records the missed interval instead of
treating it as zero work. The next scheduled run expands its whole collection
window to the earliest debt date, which also expands Clockify and Fathom fixed
blocks for safe deduplication and allocation. Successful recollection clears a
source only when the expanded window reaches that source's original debt date;
retry exhaustion never erases debt.

`review-snapshot.json` categorizes items as `new`, `changed`,
`carried_pending`, or `resolved_disappeared`. A zero-candidate run never closes
pending work. Source failures remain visible as coverage warnings.

`autopilot-result.json` uses one of six actions:

- `no_comment`: healthy coverage and no actionable delta;
- `review_delta`: one or more new or changed review items;
- `review_exceptions`: an accepted exceptions-only run has active ambiguous rows;
- `review_batch`: an accepted exceptions-only run has a new/changed clean batch
  and no new/changed exception;
- `coverage_warning`: incomplete evidence, whether or not candidates exist;
- `blocked`: quality identity/provenance failed and durable state was not
  updated.

The compact summary never reproduces `carried_pending` row content.

## Review modes and acceptance

`shadow_all` is the default and remains the only usable mode today. It presents
the full active denominator, including ambiguous rows. Every active item must
receive an evidence-bound `approve`, `skip`, or `modify` decision before a
period can support promotion.

`exceptions_only` is fail-closed. It may be requested only with an acceptance
ledger whose status reports `exceptions_only_eligible: true`:

```bash
python3 scripts/clockify_review_run.py \
  --review-mode exceptions_only \
  --acceptance-ledger state/review-acceptance.jsonl
```

Eligibility requires one complete `shadow_baseline` period with at least 90%
unchanged approvals, followed by two distinct, later, consecutive `guarded`
periods with at least 95%. Each period must have complete sources, passing
quality, a stable second replay (`0 new / 0 changed`), complete dispositions
for the full denominator (ambiguous rows included), assessments for every
skip/modify decision, and zero critical routing, description-truth, meeting,
or allocation errors. Every analyzer route used in a period must also have a
passing digest-bound evaluation scorecard.

Record and inspect those integrity-linked local periods with:

```bash
python3 scripts/review_acceptance.py record \
  --run-dir /absolute/path/to/first-run \
  --replay-run-dir /absolute/path/to/replay-run \
  --decisions state/review-corrections.jsonl \
  --critical-assessments /absolute/path/to/assessments.json \
  --analyzer-scorecard /absolute/path/to/analyzer-scorecard.json \
  --stage shadow_baseline \
  --ledger state/review-acceptance.jsonl
python3 scripts/review_acceptance.py status \
  --ledger state/review-acceptance.jsonl
```

An acceptance replay must not recollect live sources. Run the first shadow pass
with isolated validation state, then point `--replay-from` at its absolute run
directory while reusing the same state and corrections files:

```bash
python3 scripts/clockify_review_run.py \
  --since 2026-07-01 --until 2026-08-03 \
  --state state/validation/july-baseline/review-items.json \
  --corrections state/validation/july-baseline/review-corrections.jsonl
python3 scripts/clockify_review_run.py \
  --replay-from /absolute/path/to/runs/<first-run-id> \
  --state state/validation/july-baseline/review-items.json \
  --corrections state/validation/july-baseline/review-corrections.jsonl
```

The replay command creates a distinct run, copies the evidence ledger
byte-for-byte, reruns deterministic accounting through the same validated
analyzer-decision cache, and fails before durable-state ingestion if the ledger
identity, semantic evidence digest, evidence-bundle manifest, analyzer route/version, or sealed cache
decision differs. Its `replay-integrity.json` is required alongside replay
`0 new / 0 changed`; running the ordinary collector twice is not replay proof.

In `exceptions_only`, clean pending rows are represented by one
content-addressed `clean_batch` count and ID; active ambiguous rows remain
fully detailed only when new or changed. The machine contract retains the
stable row IDs behind the batch digest so an explicit board approval of that
exact batch can be verified without printing every clean description. The
digest binds each stable review ID, revision, and evidence fingerprint, so any
changed member produces a different batch ID. Neither
mode writes to Clockify.

## Analyzer evaluation

A route probe only proves that the selected transport can return minimal JSON.
Before private-text approval, run the live synthetic evaluator. It probes the
route, submits five fixed non-private cases twice, and writes both a capture and
a digest-bound scorecard:

```bash
python3 scripts/analyzer_live_evaluation.py \
  --tier primary \
  --capture-output state/validation/analyzer/primary-capture.json \
  --scorecard-output state/validation/analyzer/primary-scorecard.json
```

The built-in cases cover one atomic accomplishment, a required split, duplicate
evidence merging, a title-only meeting, and waiting noise. The command accepts
no evidence or prose input, so `CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED` remains
closed. Run it separately with `--tier fallback` if a fallback is configured.
The capture uses the versioned analyzer-evaluation input contract; the scorecard
contains no captured prose.

The transport-free evaluator remains available for inspecting or re-evaluating
an existing capture:

```bash
python3 scripts/analyzer_evaluation.py \
  --input /absolute/path/to/synthetic-capture.json \
  --output /absolute/path/to/analyzer-scorecard.json
```

The live harness validates each replay through the same bounded production
contract-repair and partition-recovery path used by real analysis; exhausted
leaves remain failures rather than being hidden by the harness. The offline
evaluator then checks complete corpus coverage, fixed expected activity,
exception, and omission dispositions, production schema/citation and atomicity
rules, concrete accomplishment concepts, and renderer/forbidden-description
compliance. Adjacent repeated words are rejected. Replay stability
compares the review-relevant decision—disposition, partitions, lifecycle,
effort, and confidence—rather than arbitrary rationale prose.
For waiting-only evidence, `planned` and `noise` are equivalent omission
lifecycles because neither creates a review row; activity boundaries and
exception kinds remain exact.
The append-only validated decision cache owns exact output replay and is separately integrity
checked. Long immutable ledger IDs never rely on model copying: local code
expands disjoint bundle-member ranges before schema and citation validation. Prefix validation
uses a neutral placeholder because deterministic routing owns the final prefix.
If every configured qualified route rejects or times out on a bounded extraction partition, the analyzer bisects its
evidence at the nearest context or complete conversation-turn boundary and
analyzes each child through the same route set, cancellation gate, and sealed cache.
It never retries the identical parent request.
Recovery never separates user intent from its following assistant result. It is
bounded at an indivisible turn, singleton evidence, or depth four, records every
child route decision, and cannot rejoin normal cross-chunk synthesis unless every
evidence ID is classified exactly once.
A passing synthetic
route scorecard proves contract fitness, not July semantic accuracy; the latter
still requires full-denominator human dispositions and the measured acceptance
thresholds. Neither a probe nor a passing synthetic scorecard authorizes
private-text egress.

## Durable host execution

Month-scale accounting and scheduled reviews run under an available host user service, not an SSH
foreground process or a Codex/terminal session.
`scripts/clockify_accounting_runner.py` holds a nonblocking
single-instance lock, resumes the append-only analyzer cache, and atomically
writes `runner-status.json` with only lifecycle metadata. It never serializes
the analyzer environment or credentials. Accounting artifacts are atomically
published before the validated `work-accounting-result.json` completion marker;
the runner trusts that marker only when the complete required artifact set is
present. A completed result is idempotent and will not be recomputed.
Before starting, the runner also requires the approved Flash model and release
revision. A nonempty cache is sealed to exactly one model tag; changing an
in-progress explicit-tag run to the generic alias, or presenting a mixed-model
cache, fails closed before any provider request. The safe model and revision are
recorded in lifecycle status so host drift can be audited without exposing a key.

The reviewed unit is `ops/systemd/clockify-work-accounting.service`. Its
`clockify_autopilot_runner.py` entrypoint runs the complete review process,
writes compact durable status, and returns exit 75 only for incomplete source
peer coverage. Complete Precision and central evidence still produces review
proposals plus a `coverage_warning`. systemd then recollects after one hour, bounded by
`CLOCKIFY_AUTOPILOT_MAX_COVERAGE_RETRIES`; configuration and integrity exit 2
still fail closed without looping. After retry exhaustion, durable source debt
remains and the next primary schedule tries the expanded interval again. Install it
as `~/.config/systemd/user/clockify-work-accounting.service`, create the private
mode-0600 environment file from
`ops/systemd/clockify-work-accounting.env.example`, and enable user lingering on
Precision so the unit survives SSH/Mac/desktop disconnection and Precision
reboot. The
unit clears all inherited fallback-route variables so this validation can use
only the approved pinned Flash route. It restarts unexpected crashes, but
`RestartPreventExitStatus=2` prevents a known fail-closed configuration,
authentication, integrity, or route error from looping indefinitely. Logs remain
in the Precision user journal.

Set `CLOCKIFY_ANALYZER_PRIMARY_REASONING_EFFORT=none` for the current Flash
alias. Ollama otherwise enables a reasoning stream that can spend minutes on a
small contract request. The setting remains model inference, is bound to new
cache route identities, reuses accepted legacy decisions from the same model
revision, and deliberately retries legacy rejections and timeouts.

Set `CLOCKIFY_HTTP_TIMEOUT_SECONDS` to control only read-only Clockify
collection requests. It defaults to `30` seconds and accepts base-10 integers
from `5` through `120`, inclusive. Empty, non-integer, or out-of-range values
fail before a Clockify request; Fathom, SSH, canonical-export, and analyzer
timeouts are unaffected. The systemd and launchd environment examples set the
documented default explicitly.

Calendly configuration is separate from the accounting runner: by default the
collector reads `~/.config/serenichron/calendly.env` (or the mode-0600 path in
`CALENDLY_ENV_FILE`). That file supplies a read-only recording endpoint through
`CALENDLY_RECORDINGS_URL`, its gateway token, and
`CALENDLY_GATEWAY_READ_ONLY=true`; values are intentionally not shown here. An
incomplete recording pagination result, missing recording capability, or a
non-read-only configuration is a visible capability gap: the Calendly source
remains incomplete and complete source coverage cannot pass. The collector may
record scheduled events as `scheduled_without_recording`, but they never create
duration evidence or a Clockify proposal.

Use `scripts/calendly_collector.py preflight` with `--since`, `--until`, and
`--output` to validate the configured read-only gateway and canonical interval
without making a network request. `collect` takes the same arguments plus a
required `--checkpoint-root`; it resumes or creates a checkpointed collection
and emits recordings only after pagination is complete. Any unavailable or
partial result has `complete: false` and no partial recording output.

For a bounded operator-requested review that cannot wait for gateway setup,
`clockify_sync_collect.py run --calendly-optional` records Calendly as explicitly
excluded and does not contact the gateway. The default remains required and
fail-closed; the override does not change scheduled autopilot configuration.

Portfolio quality reports canonical `recording_coverage` while preserving the
read-only `fathom_coverage` alias for historical artifacts. New replay seals
bind the canonical reconciliation digest, deduplication version and tolerance,
and timestamped-split digest. Older Fathom-only seals remain readable and are
verified against the immutable fields they originally contained.

When the desktop is unavailable, macOS can run the same guarded runner through
`ops/launchd/com.serenichron.clockify-work-accounting.plist` and
`ops/launchd/clockify-work-accounting.sh`. The wrapper reads the same private
mode-0600 environment contract, clears every fallback route, restarts unexpected
crashes, and maps the runner's intentional exit `2` to a clean launchd exit so a
known authorization, integrity, configuration, or route block cannot create a
restart loop. Install or load this agent only under separate approval; keeping
the reviewed files in the repository does not activate it.

## Safety contract

- Collector, quality, and review-state steps do not write to Clockify.
- Missing analyzer configuration, incomplete required central or Precision
  evidence, invalid
  correction logs, title-only meetings, and contract failures block or become
  explicit exceptions; they never become invented work.
- Missing non-coordinator peer evidence becomes durable coverage debt and a
  warning; it does not suppress proposals supported by complete sources.
- EmblemStudio work uses the applicable Serenichron project and task type, with
  `ES —` instead of the ordinary `SC —` description prefix.
- Private semantic prose cannot reach a configured analyzer unless
  `CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED=approved` is explicitly present at
  runtime; route-probe success is not privacy approval.
- A blocked accounting stage still writes its local action contract and exits
  nonzero so schedulers cannot mistake it for a healthy run.
- The quality command never updates Google Sheets or any other external system.
- The Sheet publisher requires an explicit `--enable-write`, a passing quality
  report, a passing immutable replay for the same source run, and a separately
  approved workbook/interval. It preserves human decision cells by stable ID.
- Clockify posting requires an explicit board decision for each stable review
  item.
- Approval-gated portfolio posting uses `CLOCKIFY_POST_HTTP_TIMEOUT_SECONDS`,
  which defaults to `45` seconds and accepts the same inclusive `5` through
  `120` base-10 integer range. Invalid values fail before Clockify access. The
  setting changes only request waiting time: it does not authorize `--execute`,
  select a portfolio, or weaken digest, quality, idempotency, reconciliation,
  or receipt gates.
- Sheet synchronization, Multica issue mutation, schedule changes, deployment,
  and fleet rollout are separate guarded actions.
- Do not claim a fix is live until the runtime path and Git SHA are read back on
  every collector host.

## Verification

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile \
  clockify_sync_collect.py \
  scripts/evidence_ledger.py \
  scripts/semantic_analyzer.py \
  scripts/work_allocator.py \
  scripts/caveman_renderer.py \
  scripts/work_accounting_pipeline.py \
  scripts/review_corrections.py \
  scripts/review_acceptance.py \
  scripts/analyzer_evaluation.py \
  scripts/analyzer_live_evaluation.py \
  scripts/clockify_accounting_runner.py \
  scripts/clockify_sync_collect.py \
  scripts/clockify_sync_quality.py \
  scripts/clockify_review_state.py \
  scripts/clockify_review_run.py
git diff --check
```

The approval-gated production sequence and exact live IDs are documented in
[`clockify-review-rollout.md`](clockify-review-rollout.md). Re-read live state
before using it; the snapshot is intentionally not treated as current truth.
The requirement-by-requirement distinction between local proof and missing live
acceptance evidence is tracked in
[`clockify-process-acceptance.md`](clockify-process-acceptance.md).

## Finance-publication contract

`tests/test_publication_end_to_end.py` drives the immutable coordinator event
ledger, consumed Clockify posting approval and receipt, exact API/shared-report
readback, ECB currency conversion, publication gate, receipt store, and finance
adapter ordering with synthetic fixtures. It never uses a production transport:
only the report/Slack boundary is a recording fake.

`tests/fixtures/reconciliation/publication-routine/manifest.json` proves a
two-day, two-slice routine period. Publication is legal only after the exact
post receipt is bound to both fresh readbacks, the prepared contract has a
separate approval, the report is read back, and Slack is upserted. Re-running a
published period returns its persisted receipt without another report update or
Slack message.

`tests/fixtures/reconciliation/publication-backlog/manifest.json` carries a
Calendly-only recording, a Fathom/Calendly duplicate, transient missing-slice
coverage debt, an approved Desktop limitation, an ambiguous POST recovery,
USD/EUR native buckets, retained correction revision, and report-success then
Slack-failure retry. Calendly may be excluded only for a bounded historical
recovery explicitly recorded in coverage; future autopilot coverage treats it
as a first-class required source.

Publication state is fail-closed: `publication_deferred` means preparation or
authorization evidence is absent; `publication_incomplete` means a delivery
attempt retained the verified report receipt but needs a Slack retry; and
`published` requires the bound report and Slack receipts. ECB quotes use the
latest eligible EUR-base quote, convert every native bucket to USD with
half-up cent rounding, and are rejected when older than four calendar days.
