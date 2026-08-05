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
4. route projects deterministically and render Caveman descriptions targeting
   8–14 words, with a five-word hard floor for complete atomic examples;
5. allocate active effort around fixed Clockify and Fathom blocks without
   overlap, gap filling, overnight bridging, or silent trimming;
6. validate quality and ingest stable review identities; shadow evaluation
   exposes the full denominator, while repeated runs emit only actionable
   deltas. Exceptions-only operation is not activated until its measured gates
   pass.

It prints the absolute path to `autopilot-result.json`. Lower-level commands
remain available for targeted diagnosis. Calendly is intentionally outside the
current process.

Outputs:

- `runs/<run-id>/evidence/evidence-ledger.json`: immutable evidence and
  completeness manifest;
- `runs/<run-id>/semantic-analysis.json`: cited atomic activities, omissions,
  analyzer provenance, and the reversible content-addressed evidence-bundle
  manifest used for local member expansion;
- `runs/<run-id>/work-accounting-result.json`: semantic, allocation, Fathom,
  and exception contract;
- `runs/<run-id>/allocation-report.json`: strict allocation and contested time;
- `runs/<run-id>/fathom-reconciliation.json`: disposition for every eligible
  meeting;
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
- `state/review-items.json`: mutable local review state, intentionally ignored
  by Git;
- `state/review-corrections.jsonl`: immutable evidence-bound approve, skip, and
  modify decisions, intentionally ignored by Git.
- `state/review-acceptance.jsonl`: integrity-linked shadow/guarded period
  evidence controlling exceptions-only eligibility, intentionally ignored by Git.

Models propose semantics only. Deterministic code owns evidence identity,
project routing, descriptions, time allocation, review identity, and safety.
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
cache record. Probe, connection, authentication, and malformed-response failures
are not retried and block the run. A timed-out extraction request is sealed in
the local cache and bisected only at a safe context or complete-turn boundary;
the identical timed-out body is never sent again. If repair still fails, a
configured stronger fallback receives the safe category and handles the bounded
chunk. A sealed semantic-contract rejection may also enter the deterministic
partition recovery below even when the primary is the only qualified route. Conflicting or
low-confidence claims that remain unresolved become explicit exceptions, never
proposals.

The required primary is `deepseek-v4-flash:cloud`. The current release must pass
the v15 synthetic route gate after its moving Ollama tag is refreshed. Every
live cloud route must include the resolved 64-character model revision so the
scorecard, analyzer cache, semantic run, and replay cannot silently mix model
releases. `deepseek-v4-pro:cloud` is not an approved route for this process.

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
request on the same route with a distinct seed and timeout marker. The identical
timed-out body is never resent, and a second timeout still blocks.
If every configured qualified route rejects repeated-workstream synthesis,
including a single primary with no fallback, those otherwise valid but potentially
duplicative claims become one `analyzer_synthesis_failure` exception. A synthesis
transport failure still blocks the run.
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

Fathom eligibility requires a valid meeting window and either Vlad as recorder
or attendee. Recordings shorter than five minutes require a transcript. A
title-only record remains an exception; it cannot support an invented outcome.
Existing Clockify time reconciles a meeting only with reciprocal overlap of at
least 80%; partial conflicts remain explicit fixed-block exceptions.

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
bounded at an indivisible turn, singleton evidence, or depth eight, records every
child route decision, and cannot rejoin normal cross-chunk synthesis unless every
evidence ID is classified exactly once.
A passing synthetic
route scorecard proves contract fitness, not July semantic accuracy; the latter
still requires full-denominator human dispositions and the measured acceptance
thresholds. Neither a probe nor a passing synthetic scorecard authorizes
private-text egress.

## Safety contract

- Collector, quality, and review-state steps do not write to Clockify.
- Missing analyzer configuration, incomplete canonical remote evidence, invalid
  correction logs, title-only meetings, and contract failures block or become
  explicit exceptions; they never become invented work.
- Private semantic prose cannot reach a configured analyzer unless
  `CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED=approved` is explicitly present at
  runtime; route-probe success is not privacy approval.
- A blocked accounting stage still writes its local action contract and exits
  nonzero so schedulers cannot mistake it for a healthy run.
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
  scripts/evidence_ledger.py \
  scripts/semantic_analyzer.py \
  scripts/work_allocator.py \
  scripts/caveman_renderer.py \
  scripts/work_accounting_pipeline.py \
  scripts/review_corrections.py \
  scripts/review_acceptance.py \
  scripts/analyzer_evaluation.py \
  scripts/analyzer_live_evaluation.py \
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
