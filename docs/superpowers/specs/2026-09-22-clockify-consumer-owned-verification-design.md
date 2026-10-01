# Clockify review-cycle behavior

## Outcome

The scheduler owns which closed one- or two-day periods are due. It can also
run a bounded recovery for an older gap. Every run makes the available review
evidence visible without writing to Clockify.

September month-end review is the first priority. After that, normal operation
reliably covers one or two days at a time, with recovery available for an
exceptional backlog.

## Run behavior

- A first run starts at the requested closed period. An incremental run starts
  after the last completed period. A recovery run names the missing period and
  keeps its relationship to the original run.
- The run collects every authorized source that is available for the period.
  It records optional, unavailable, and failed sources explicitly; they are
  never silently treated as empty.
- Calendly is optional. Gemini is planned but non-blocking. Live inference uses
  only the approved, pinned `deepseek-v4.1-flash:cloud` revision. Older routes
  remain usable only to replay their already sealed decisions.
- The run keeps the evidence, coverage result, Clockify read-only baseline, and
  replay inputs needed to explain and reproduce its result. Replaying a run
  uses that saved snapshot and valid cache only; it does not recollect or use
  newer data.
- Caches and receipts are preserved. A recovery attempt is distinct from its
  parent, and repeating a completed attempt reuses its result instead of
  recollecting or running inference again.

## Reconciliation behavior

- Evidence is reconciled against Clockify in read-only mode.
- An exact existing Clockify match suppresses a duplicate proposal.
- Partial or non-reciprocal overlap keeps the complete evidence-backed
  proposal and adds a warning naming the conflicting entry and overlap.
  Overlap is never silently subtracted.
- The same meeting found through multiple sources becomes one proposal.
  Unrelated work at the same time remains separate and keeps its warning.
- Evidence with invalid or missing routing or required fields remains visible
  as unresolved with an explicit warning; it is not discarded.
- A recovery of a rejected review can retain whole activities whose evidence
  is uncontested. Conflicting or uncited evidence stays visible for manual
  review, without invented minutes or choosing a winner between conflicting
  claims. The saved result identifies this local repair and replays unchanged.

## Publication and progress

- Every published row is review `pending` and posting `unposted`. Publication
  is read back and its receipt is retained.
- The run never creates, edits, or deletes Clockify entries. Those writes need
  explicit approval of the relevant review rows.
- The scheduler advances its completed-period marker only through a contiguous
  range whose coverage and publication are verified. A later successful period
  does not hide an earlier gap.
- The coverage audit must name every missing or unavailable required source and
  stop when coverage is not exact. It must not claim complete coverage while a
  named gap remains.

## Acceptance examples

The implementation is correct when these observable cases hold:

1. First, incremental, and recovery runs select the expected closed periods;
   later periods can produce review rows without moving past an earlier gap.
2. Two missing sources create two visible source gaps; resolving one leaves the
   other visible.
3. A 60-minute meeting with 15 minutes of unrelated Clockify overlap produces
   a 60-minute proposal and a 15-minute warning.
4. Fathom and Google Meet copies of one meeting produce one proposal, while
   unrelated simultaneous work remains separate.
5. Replay with network access disabled reproduces rows from the saved snapshot
   and valid cache; a completed recovery attempt is adopted without new work.
6. An invalid or unroutable item is published as unresolved, pending, and
   unposted with a warning.
7. A publication retry converges on one set of rows and one receipt.
8. Autonomous review produces zero Clockify writes.

Tests use synthetic local fixtures and existing offline test seams. The
September month-end review, exact coverage audit, ordinary one- or two-day
cadence, and exceptional recovery path are all checked before release.
