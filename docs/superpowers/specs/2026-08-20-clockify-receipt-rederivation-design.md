# Clockify Receipt Re-Derivation Design

**Status:** Board-approved

**Date:** 2026-08-20

**Parent spec:** `docs/superpowers/specs/2026-08-17-clockify-reconciliation-publication-manifest-design.md`

## Purpose

Make Clockify posting retries idempotent without allowing a prior receipt to authorize different timestamps from the approved portfolio. Every accepted replayed bound must be independently reproduced from the original approved plans and fresh normalized Clockify state.

This replaces the current circular contract in which a receipt supplies adjusted bounds and a digest merely hashes those same supplied values.

## Trust Model

### Authoritative inputs

1. The approved portfolio and its SHA-256 digest.
2. The approved quality and replay artifacts already required by the posting gate.
3. A fresh read-only Clockify ledger snapshot normalized by `_live_entries()`.
4. The repository-owned boundary-adjustment algorithm version.

### Non-authoritative inputs

A prior posting receipt is an idempotency hint and audit record. It may identify candidate `(review_id, segment_index, clockify_entry_id)` tuples, but it does not authorize timestamps, duration, routing, tags, description, blocker removal, or boundary adjustments.

Receipt hashes remain useful for audit linkage, but an unkeyed digest of receipt-supplied fields is never treated as proof that those fields were permitted.

## Core Invariant

For each posting key, the only permitted final plan is the output of:

```text
derive(approved_plans, fresh_live_entries - provisionally_matched_prior_entries)
```

A prior receipt item is accepted only when its referenced live Clockify entry exactly matches the independently derived plan for the same key and matches the approved semantic fields.

## Data Model

### Posting key

```text
(review_id: str, segment_index: positive int)
```

Approved plans, receipt items, derived plans, and accepted live entries must each have unique posting keys.

### Normalized live entry

The existing normalized fields remain authoritative:

- `id`
- `start`
- `end`
- `project_id`
- sorted `tag_ids`
- normalized `description`

Live entry IDs must be nonempty and unique in the fetched snapshot.

### Derivation evidence

Every new receipt records:

- `boundary_adjustment_algorithm`
- `live_snapshot_sha256`: SHA-256 of the complete sorted normalized fresh live snapshot
- `blocker_snapshot_sha256`: SHA-256 of the sorted normalized entries actually passed as blockers
- freshly derived `boundary_adjustments`
- `boundary_adjustments_sha256`, bound to the portfolio digest, algorithm, blocker snapshot digest, and canonical adjustment list

These fields provide audit evidence. Authorization still comes from fresh derivation, not from trusting stored digest fields.

## Deterministic Replay Flow

### 1. Build approved plans

Create immutable approved plans from the portfolio before reading or applying a prior receipt. Reject duplicate posting keys and overlapping approved windows.

### 2. Read fresh Clockify state

Fetch the current ledger using the approved plan range. Normalize and sort entries. Reject duplicate nonempty Clockify entry IDs because receipt ID resolution would be ambiguous.

### 3. Parse prior receipt as candidate identity only

If a prior receipt exists:

- require the exact approved portfolio SHA-256;
- reject duplicate posting keys across `created` and `already_existing`;
- reject duplicate nonempty Clockify entry IDs across receipt items;
- reject unknown posting keys;
- require each candidate entry ID to exist exactly once in the fresh live snapshot;
- require the live entry's project, tags, and description to match the approved plan for that key;
- ignore receipt-supplied start, end, duration, and boundary-adjustment fields for authorization.

At this stage candidates are provisional. They are removed from the blocker set only so their own already-posted intervals do not prevent deterministic re-derivation.

### 4. Derive plans from independent inputs

Run `_align_subminute_boundaries()` using:

- a fresh copy of the original approved plans;
- the fresh normalized live snapshot minus provisional candidate entries;
- exact-match keys found among the remaining live entries.

The algorithm may apply multiple individually permitted sub-minute start shifts and may aggregate several sub-minute deficits into a restoration of 60 seconds or more. There is no independent absolute-delta authorization rule. The exact algorithm output is the authorization rule.

The algorithm must conserve each approved review row's total `duration_seconds`, preserve routing and descriptions, avoid overlaps, and return its canonical adjustment list.

### 5. Validate provisional prior entries against derived output

For each provisional candidate:

- resolve its independently derived plan by posting key;
- require the fresh live entry to exactly match derived `start`, `end`, `project_id`, sorted `tag_ids`, and description;
- require its live duration to match the derived duration;
- require any receipt-supplied bounds and duration fields, if present, to equal the fresh live entry so the audit record cannot contradict readback.

Only then is the candidate accepted as already posted. A candidate that fails any comparison is a hard replay error; it is never returned to the blocker set and retried heuristically.

### 6. Resolve other exact entries and conflicts

Against the same derived plans:

- accept at most one exact live entry per posting key;
- reject multiple exact matches;
- reject overlaps between unfulfilled derived plans and remaining live entries;
- never create a new entry for a key already satisfied by a validated prior or exact live entry.

### 7. Emit receipt from derived state

The new receipt always uses freshly derived plans and adjustments. It never copies prior receipt bounds or prior adjustment documents into the new authorization state.

`created` and `already_existing` remain audit dispositions. Both use live/readback bounds and recomputed exact durations.

## Component Boundaries

### Receipt parser

Replace `_apply_prior_receipt()` with a parser that returns candidate identities without mutating plans:

```python
@dataclass(frozen=True)
class PriorReceiptCandidate:
    review_id: str
    segment_index: int
    clockify_entry_id: str
    disposition: str
    recorded_start: str | None
    recorded_end: str | None
    recorded_duration_seconds: int | None

_prior_receipt_candidates(
    path: Path,
    portfolio_sha: str,
    approved_keys: AbstractSet[tuple[str, int]],
) -> Sequence[PriorReceiptCandidate]
```

The implementation plan must replace the ellipsis with the existing strict JSON validation patterns. The parser performs no plan mutation and no adjustment validation.

### Candidate/live resolver

Add a pure resolver:

```python
_resolve_prior_candidates(
    candidates: Iterable[PriorReceiptCandidate],
    live: Iterable[Mapping[str, Any]],
    approved_by_key: Mapping[tuple[str, int], Mapping[str, Any]],
) -> tuple[dict[tuple[str, int], dict[str, Any]], list[dict[str, Any]]]
```

It returns provisional live entries keyed by posting key and the remaining live blocker list. It validates unique IDs and approved semantic fields, but does not validate bounds until derivation finishes.

### Derived-plan validator

Add a pure comparison step that accepts candidates only after derivation:

```python
_validate_prior_candidates(
    candidates_by_key: Mapping[tuple[str, int], Mapping[str, Any]],
    derived_by_key: Mapping[tuple[str, int], Mapping[str, Any]],
    receipt_candidates: Mapping[tuple[str, int], PriorReceiptCandidate],
) -> dict[tuple[str, int], dict[str, Any]]
```

It returns accepted exact live entries. It cannot adjust, relocate, trim, extend, or otherwise mutate derived plans.

## Failure Modes

The posting command fails closed when:

- the prior receipt targets another portfolio;
- approved, receipt, derived, or live identities are duplicated or ambiguous;
- a prior entry ID is absent from fresh Clockify readback;
- a prior entry's semantic fields differ from approval;
- a prior entry's live bounds differ from fresh derivation;
- receipt audit bounds contradict the live entry;
- live blockers changed such that the prior entry is no longer the derived result;
- the derivation cannot conserve duration or avoid overlap;
- any unfulfilled derived plan conflicts with live state.

No failure path silently drops a candidate, invents replacement time, or posts a duplicate.

## Compatibility

- Exact-second and explicit legacy minute-only approved portfolios remain supported through the existing `_plans()` contract.
- Existing receipts with unchanged original bounds can replay if their live entries independently match derivation.
- Existing adjusted receipts can replay only when fresh live state independently reproduces those exact adjusted bounds.
- A receipt whose entry disappeared or whose blocker environment changed incompatibly fails and requires explicit recovery; it is not repaired from receipt assertions.
- No receipt schema migration mutates historical files in place.

## Test Strategy

### Receipt-forgery regressions

- Reject same-duration relocation forward or backward, even with a recomputed receipt digest.
- Reject a receipt that names an unrelated blocker ID to remove it from derivation.
- Reject duplicate posting keys and duplicate Clockify entry IDs.
- Reject receipt audit bounds that differ from fresh live readback.

### Deterministic replay regressions

- First dry run and retry with unchanged blockers produce byte-equivalent derived plans and canonical adjustments.
- Already-created receipt entries are provisionally excluded, then accepted only because they exactly match re-derived plans.
- Multiple 30/40-second trims may produce a valid 70-second restoration and replay successfully.
- Changed blocker state that alters derived bounds rejects the prior candidate.

### Idempotency and conflict regressions

- A validated prior entry is never posted twice.
- A non-receipt exact live entry satisfies a plan without a write.
- Multiple exact entries fail closed.
- Remaining live overlaps fail closed with evidence-rich diagnostics.

### Integration verification

- Dry-run receipt followed by retry/readback using synthetic Clockify fixtures.
- Execute-mode partial success followed by retry where only the missing entries are posted.
- Exact-second and legacy minute-only portfolios.
- Full portfolio posting, quality, replay, process-integration, and repository test suites.

## Operational and Safety Boundaries

- Tests and implementation use synthetic local fixtures only.
- The correction adds no new credential, permission, schedule, provider, or external-system capability.
- No Clockify write is authorized by this design approval. A future live posting still requires the final digest-bound approval required by the parent manifest design.
- No push, merge, deployment, Sheets mutation, Slack communication, or publication is authorized.

## Acceptance Criteria

1. Prior receipts never mutate approved plans.
2. Every replayed bound is freshly derived from approval and fresh live state.
3. Candidate receipt entries are excluded from blockers only provisionally and must exactly match derived plans before acceptance.
4. Aggregate restorations of 60 seconds or more replay when and only when the derivation produces them.
5. Forged same-duration relocation and blocker-removal attacks fail.
6. Partial retries are idempotent and never duplicate already-created entries.
7. Exact and legacy compatibility tests pass.
8. Full repository tests and independent code review are clean.
