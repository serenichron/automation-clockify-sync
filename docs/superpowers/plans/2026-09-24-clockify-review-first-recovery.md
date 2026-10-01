# Clockify Review-First Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the review cycle account for due closed periods and recoverable gaps while preserving read-only Clockify reconciliation and pending/unposted publication.

**Architecture:** Keep the existing scheduler, collectors, reconciliation, replay, and Sheet publication boundaries. Add only the smallest behavior needed for due-period selection, explicit source coverage, duplicate-safe reconciliation, snapshot replay, and recovery. The behavior contract is [the accompanying spec](../specs/2026-09-22-clockify-consumer-owned-verification-design.md).

**Tech Stack:** Existing Python CLI, JSON/JSONL snapshots and receipts, synthetic offline fixtures, and the repository's `unittest` suites.

**Spec:** `docs/superpowers/specs/2026-09-22-clockify-consumer-owned-verification-design.md`

## Global Constraints

- Process closed one- or two-day periods; never claim a later period closes an earlier gap.
- Collect all authorized available evidence and name optional/unavailable sources.
- Reconcile Clockify read-only; exact matches suppress duplicates, partial overlap remains a full proposal with a warning.
- Deduplicate the same meeting across sources; retain unrelated simultaneous work.
- Use only the approved pinned `deepseek-v4.1-flash:cloud` route for fresh inference; preserve older sealed decisions for replay. Calendly is optional and Gemini is non-blocking.
- Publish only review `pending` / posting `unposted`; never write Clockify without explicit approval.
- Publish unresolved evidence to the separate `unresolved-evidence` tab with its warning; do not hide it in the main review rows.
- Replay from a saved snapshot and valid cache; preserve caches and receipts.

## Review Focus

- A later successful slice must not advance through an earlier source gap — cover in source-debt and cycle tests.
- Partial overlap must not shorten evidence — cover in reconciliation tests.
- Same-meeting copies must collapse without collapsing unrelated work — cover in meeting/review tests.
- Replay must not recollect or call inference — cover in resume/repair tests.
- Unroutable evidence must remain visible and unposted — cover in acceptance/publication tests.

### Task 1: Lock due-period and coverage behavior

**Files:** `scripts/clockify_review_cycle.py`, `scripts/source_coverage.py`, `scripts/clockify_source_debt_recover.py`; tests in `tests/test_review_cycle*.py`, `tests/test_source_coverage.py`, and `tests/test_source_debt_recovery.py`.

- [ ] Add failing synthetic tests for first, incremental, and named recovery periods; two missing sources; and a later slice that cannot move the contiguous completion marker past a gap.
- [ ] Run `python3 -m unittest discover -s tests -p 'test_source_coverage.py' -v`, `python3 -m unittest discover -s tests -p 'test_review_cycle_source_debt.py' -v`, and `python3 -m unittest discover -s tests -p 'test_source_debt_recovery.py' -v`; confirm the new cases fail.
- [ ] Implement the smallest scheduler/coverage change that records each missing or unavailable source explicitly before progress and preserves the gap during later success.
- [ ] Rerun those three discover commands and then `python3 -m unittest discover -s tests -p 'test_review_cycle.py' -v` plus `python3 -m unittest discover -s tests -p 'test_review_cycle_source_debt_end_to_end.py' -v`.

### Task 2: Lock reconciliation and publication outcomes

**Files:** `scripts/clockify_portfolio_review.py`, `scripts/clockify_portfolio_quality.py`, `scripts/review_acceptance.py`, and `scripts/clockify_sheet_publish.py`; tests in `tests/test_review_cycle.py`, `tests/test_review_acceptance.py`, `tests/test_portfolio_review.py`, and `tests/test_review_cycle_delivery.py`.

- [ ] Add failing fixtures for exact Clockify match, partial/non-reciprocal overlap, Fathom/Google Meet duplicate, unrelated simultaneous work, and unresolved routing.
- [ ] Run `python3 -m unittest discover -s tests -p 'test_review_cycle.py' -v`, `python3 -m unittest discover -s tests -p 'test_review_acceptance.py' -v`, `python3 -m unittest discover -s tests -p 'test_portfolio_review.py' -v`, and `python3 -m unittest discover -s tests -p 'test_review_cycle_delivery.py' -v`; confirm the new cases fail.
- [ ] Implement full-duration proposals with structured warnings, same-meeting dedupe, unresolved visibility, and pending/unposted readback without any Clockify write.
- [ ] Rerun those four discover commands and inspect the write-seam assertions for zero Clockify mutations.

### Task 3: Make replay and recovery snapshot-only

**Files:** `scripts/clockify_portfolio_replay.py`, `scripts/clockify_review_run.py`, and `scripts/clockify_portfolio_repair.py`; tests in `tests/test_review_run_resume.py`, `tests/test_review_run_repair.py`, and `tests/test_review_run.py`.

- [ ] Add failing offline tests proving replay uses saved inputs/cache, completed recovery adopts its terminal result, and an incomplete terminal attempt gets a new identity.
- [ ] Run `python3 -m unittest discover -s tests -p 'test_review_run.py' -v`, `python3 -m unittest discover -s tests -p 'test_review_run_resume.py' -v`, and `python3 -m unittest discover -s tests -p 'test_review_run_repair.py' -v`; confirm the new cases fail.
- [ ] Implement snapshot-only replay and durable parent/attempt receipts, preserving existing caches and publication identity.
- [ ] Rerun those three discover commands with network access disabled by the existing test seam.

### Task 4: Verify the review-first release

- [ ] Run the focused suites from Tasks 1–3 plus `python3 -m unittest discover -s tests -p 'test_review*.py' -v`.
- [ ] Run the exact coverage audit for the September month-end period and verify every gap is named or every required source is covered.
- [ ] Confirm one normal one- or two-day run and one exceptional recovery run produce only pending/unposted review output and zero Clockify writes.
- [ ] Commit the tested implementation with a message describing the review-first recovery behavior.

## Recovery checkpoint — 2026-10-01

September review publication is verified live: 186 pending/unposted rows,
including 141 for September 17–30. These duration sums include warned overlaps;
they are not net approved time. Existing approved/posted rows remain protected.

The remaining semantic recovery targets are four citation-quarantine groups
and two failed reviews, containing 243 evidence members. Cached replay cannot
resolve them: it faithfully reproduces the prior exception. A focused repair
must review only the selected source-bound members, preserve all other semantic
decisions, retain the original cache, and replay from the newly sealed decisions
without network access. Contextual session windows are not billable durations.

Review fix: when session message ordinals are available, use them before wall
clock timestamps to preserve instruction/reply pairs under clock skew. The
regression first demonstrated swapped pairs, then passed after the ordering fix.

Ruling: retain the original first-retry path for already sealed structural-failure
history. Its transport guard permits only selected retry requests and its output
check protects non-target decisions; unrelated cache misses fail closed. New
quarantine and repeated-failure recoveries use scoped review. Changing the legacy
request identities here would invalidate proven historical cache replay.

Ruling: extend the existing review-retry seam for scoped recovery rather than
recollecting the month or replacing the pipeline. First add an offline regression
that creates a real quarantined parent, recovers it, and checks immutable parent
artifacts plus cache-only replay. Keep the ordinary historical retry unchanged.

The installed release is not yet this local implementation. Publication does
not prove scheduler completion, and manual Sheet receipts must not be relabeled
as canonical cycle receipts. The full goal remains active until both recovery
and recurring operation are verified.

## Scoped recovery outcome — 2026-10-01

The selected six failure/quarantine groups were processed without recollecting
September. The first four-call response-format failure is retained and replays
offline; a versioned typed contract then recovered 13 activities for 23–25
September and 34 for 25–27. The resulting 21 new proposals / 50 minutes were
published pending/unposted and read back exactly. All 77 earlier proposal
candidates in these periods remained unchanged.

The main review now has 207 pending/unposted rows, 57 protected Approved/posted
rows, and 31 superseded rows. September 17–30 has 162 pending proposals / 1,983
minutes including warned overlap. Twenty-four new unresolved summaries were
published separately; unresolved history is not claimed to be recovered time.
Repeating publication against the same snapshots finds zero new rows.

Replay regression: scoped recovery retains the source's activity order and
appends recovered rows, while fixture validation sorted by identity. The
allocator serialized demand evidence in incoming order, causing an otherwise
identical accounting result to fail exact replay. Restore validated fixture
order using unique evidence groups and preserve optional extractor provenance.
Both real sealed recoveries now replay exactly without inference or rewriting
their sources. Full suite: 1,426 passed, 2 skipped, 538 subtests passed.

Remaining release work is durable verified-posted equivalence for the three
excluded historical candidates, truthful canonical delivery/adoption of the
already published history, and release/recurring verification. Do not infer
completion from Sheet publication, or relabel manual receipts as cycle delivery.

Remaining semantic audit: two scoped groups (4 and 62 evidence members) are
still rejected for invalid effort. Their sealed cache stores the rejection and
citation partitions, not response bodies; the precise effort defect and valid
completed rows cannot be certified offline. Citation conflicts are also present.
Do not broaden citation quarantine to bypass effort validation or invent zero
time. Their evidence remains available in unresolved review; any further paid
repair must have a concrete diagnostic/contract improvement rather than repeat
the same unsuccessful request blindly.

Autopilot audit: 13 canonical September slices are still incomplete and lack
canonical delivery receipts; only six interval candidates currently have sealed
replay completion bundles. The installed release lacks historical adoption;
the local function also needs an explicit operator CLI. Exposing the existing
verified adoption operation does not authorize adopting unproven slices, and
manual delivery needs truthful source/row/target verification, not relabeling.

Historical source selection audit (2026-10-01): the selected September 13–15
and 15–17 repairs descend from older incomplete collections. Separate complete
collector bundles exist and validate with their own exact publication rows;
they still need surviving checkpoint bindings and sealed exact-source replay.
Do not infer a need to recollect from the stale selection.

A fresh cache-only replay of the selected September 7–9 repair exposed real
behavior drift: its old accounting silently credited four candidates to fixed
Clockify intervals, whereas the corrected overlap behavior preserves them for
review. Semantic analysis was identical. Do not force those old outputs to
pass or call the failed replay a delivery proof. A new snapshot-only repair
completed, and its distinct sealed replay passed with original source bytes
unchanged. Check the additional candidates against current Sheet rows and
exact posted-work evidence before publishing anything; no publication or
canonical adoption had been performed at that checkpoint. Subsequent exact
Sheet comparison confirmed all six IDs existed, four were machine-tombstoned
by the old temporal-credit rule and one pending interval was stale. A scoped
pending correction reopened those four and updated that interval with exact
readback, adding 120 proposed minutes without appending IDs or touching
Approved/posted rows. A separate four-cell follow-up added counterpart project
names from the source routing snapshot; its genuine new publication passes
the cycle's exact routed six-row contract. An isolated adoption proof passed
twice, idempotently; no canonical state or frontier was changed.

The subsequent September 9–17 correction restored 16 machine-superseded rows,
refreshed seven pending rows and appended six missing proposals. Three exact
posted equivalents were excluded using source and Clockify readback evidence.
Live verification confirms 301 unique IDs, 233 pending/unposted proposals
totalling 3,404 minutes, 57 Approved/posted preserved and 11 superseded. These
are overlap-inclusive review totals, not approved net time. The publication
receipt explicitly covers a subset; credit-aware producer accounting and
full historical adoption remain unfinished. No Clockify writes or deployment.

## Current recovery checkpoint — 2026-10-02 local

The latest exact Sheet readback contains 307 unique review IDs: 239
pending/unposted, 57 protected Approved/posted, and 11 superseded. Pending
duration sums total 3,425 minutes (57h05m); September 17–30 accounts for 168
pending proposals / 2,004 minutes (33h24m). These remain overlap-inclusive
review totals, not net approved time. Six newly recovered proposals /21 minutes
were appended at A303:O308 with all prior rows preserved. The receipt covers
only those six rows, not the entire 51-proposal repaired source.

The fresh-only citation-quarantine contract now retains validated whole,
uncontested activities after ordinary fields, effort, spans and taxonomy have
passed. Historical request bytes remain unchanged. One targeted 62-member
request on the approved V4.1 route recovered the six proposals; its source and
sealed offline replay agree exactly. Seven disputed members remain unresolved
without effort. Their stable summary and seven additional timing summaries
were published at A381:L388 in the visible unresolved tab, with exact full-tab
readback and native validation/format preservation. That tab now has 387 unique
summaries. Four issue-only records in the other failed group have no positive
observed attention interval; this is not proof of zero work. Do not repeat
inference blindly or manufacture minutes from later issue status changes.

The latest frozen-code suite checkpoint is 1,476 passed, 2 skipped, 552
subtests. The installed release is still older than these local changes.
Multica remains the sole active calendar scheduler; a scheduled service exit
or an idle run does not prove that backlog processing is restored.

Release preparation must preserve a genuine historical-lineage gap: the
selected September 9–11 repair binds a collector completion bundle that no
longer matches its preserved checkpoint digest. Do not rewrite the checkpoint,
swap completion bundles, or advance the canonical frontier through that gap.
Investigate the separately preserved genuine collector and, if necessary,
derive a distinct verified source from it. Local clone proofs and canonical
adoption are separate; neither manual publication nor a passing replay alone
proves operational delivery. The full goal remains active through tested
release preparation, approved publication/deployment, and verified recurring
processing without duplicate review rows or repeated valid inference.

Release-review correction: the normalization layer still treated any proposal
overlapping a meeting as duplicate time. A failing regression proved the loss
of a distinct 30-minute meeting and unrelated simultaneous work. The narrow
fix compares nonempty canonical meeting identities; matching non-meeting
activity segments retain their established duplicate handling. Distinct
meetings remain full duration with warnings, even if an activity ID happens to
be reused. The old integration expectation for splitting distinct meetings
was corrected to assert both full intervals and their warning. Independent
full suite after the fix: 1,481 passed, 2 skipped, 554 subtests. Audit actual
September credit records and rederive affected snapshots before claiming
that this code fix has corrected the published month.

Ruling: saved publisher and posted-row proofs are trusted captures from the
connector or operator, not remote attestations. Offline fixture construction
does not prove an ordinary publication failure. Keep the exact source/row/
Clockify and checkpoint comparisons, require genuine verified captures for
actual imports, and forbid previews or fabricated expected receipts. Do not
add a remote-signature protocol or invalidate genuine later historical
captures solely because the original publication run is unavailable. If a
trusted operator supplies false evidence, credit or frontier can be wrong;
the coordinator must retain and verify real import evidence before canonical
mutation. No canonical adoption or credit seeding is yet performed.
