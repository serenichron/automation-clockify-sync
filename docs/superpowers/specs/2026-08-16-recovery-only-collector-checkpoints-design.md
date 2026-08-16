# Recovery-Only Collector Checkpoints

## Objective

Make routine one- or two-day Clockify autopilot harvests cheap to retry while also supporting arbitrarily long finite backlog and outage-recovery intervals. A failure after one or more remote pages must preserve successful work locally, and a later invocation for the identical interval must resume rather than start from page one.

Long intervals must not become another all-or-nothing run. They are partitioned into bounded date slices. Each fully completed slice becomes independently reviewable and eligible for the existing approval-gated sync workflow immediately, while incomplete pages within the current slice remain private recovery state.

This design covers deterministic date slicing, per-slice local artifacts, and the paginated Clockify, Fathom, and Multica collectors. It does not change semantic analysis rules, reconciliation rules, external reporting, Google Sheets, or Clockify mutation behavior.

## Chosen Approach

Use two deterministic checkpoint levels:

1. a backlog plan partitions any finite requested interval into chronological slices of at most two local accounting days and records each slice's status and immutable result identity;
2. an append-only page spool plus a small atomic manifest resumes pagination within each source and slice.

This is preferable to rewriting one growing JSON checkpoint after every page because each successful page becomes an immutable recovery unit. It is preferable to SQLite because sequential bounded slices and immutable files avoid a database lifecycle while supporting both routine runs and exceptional backlogs.

## Safety Invariant

Checkpointed pages are recovery state, not evidence. Completed date slices are independent evidence units.

- An incomplete manifest must never expose its records to proposal generation, reconciliation, acceptance scoring, exports, or source-coverage completion.
- An interrupted source within a slice remains fail-closed: `complete` is false and its public records list is empty.
- A slice may be finalized only when all required sources satisfy their existing completeness rules. Once finalized, it receives an immutable result identity and may proceed independently through review and approval-gated sync even when later backlog slices remain pending.
- Only complete source manifests within a complete slice may be normalized into the existing source result contract.
- A corrupt, incompatible, or mismatched checkpoint must fail closed. It must not be silently reset, merged into a new interval, or reported as complete.

## Identity and Layout

The backlog-plan identity binds the exact requested UTC interval, local accounting timezone, slice-width policy, routing/runtime compatibility version, and ordered slice boundaries. Slice boundaries are deterministic, contiguous, non-overlapping, and half-open. There is no hard requested-range limit; processing remains bounded because slices run sequentially and completed results are emitted before the next slice starts.

Each source checkpoint identity binds:

- schema version;
- source name;
- exact UTC `since` and `until` boundaries;
- sanitized request fingerprint, including source-specific query behavior;
- collector/runtime compatibility version.

The identities produce deterministic directory names below a private, configurable checkpoint root. `CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT` may override the location; the default is `state/collector-checkpoints` under the canonical repository root, which is already within local untracked state. The backlog directory contains an atomically updated `backlog-manifest.json` and one child directory per slice. Each source directory within a slice contains:

- `manifest.json`: atomically replaced state describing identity, ordered page references, the next page/cursor/offset, completion, and aggregate digest;
- `pages/000001.json`, `pages/000002.json`, and so on: immutable, atomically published page envelopes;
- no credentials, authorization headers, or raw cursor text in diagnostic output.

Page envelopes may contain the private source payload needed to reconstruct the final sanitized result. Their filenames and manifest digests are safe operational metadata; their contents remain private evidence.

## Backlog Flow

1. Normalize and validate the requested interval.
2. Partition it chronologically into slices of no more than two local accounting days. A routine one- or two-day request is one slice; longer requests produce multiple slices.
3. Process slices sequentially from oldest to newest so overdue work becomes reviewable first.
4. Skip a previously completed slice only after verifying its immutable result identity and artifacts.
5. After every slice completes, atomically record its result identity, expose that slice through the existing local review workflow, and continue to the next slice. Clockify or other external writes remain separately approval-gated.
6. If a slice fails, stop the backlog attempt with that slice incomplete. Earlier complete slices remain reviewable/syncable and a retry resumes the failed slice before continuing.
7. Mark the parent backlog complete only when every slice is complete. Parent completion is operational bookkeeping, not a prerequisite for using an already complete slice.

## Source Collection Flow

For each source:

1. Resolve the exact checkpoint identity before the first request.
2. Load and validate any existing manifest and every referenced page digest.
3. If the manifest is complete, reconstruct and return the existing normalized source result without a remote request.
4. If it is incomplete, reconstruct pagination guards from stored pages and resume from the recorded continuation token.
5. After each successful remote page, atomically write the page envelope, then atomically advance the manifest. The ordering ensures a manifest never references a partially written page.
6. When the remote source proves pagination complete, atomically mark the manifest complete and bind an aggregate digest over the ordered page digests.
7. Normalize all stored pages through the existing source-specific sanitization and slice filtering, returning the same output shape as an uninterrupted run.

Source continuations remain source-specific:

- Clockify: next one-based page number.
- Fathom: next cursor, stored privately; logs and returned failures retain only the existing hashed cursor reference.
- Multica: selected API path and next offset, so fallback between `/api/issues` and `/issues` cannot mix pages.

## Failure Semantics

- Network, timeout, retry-budget, validation, repeated-page, or safety-limit failure leaves the last valid source and slice manifests incomplete.
- The failed slice returns no reviewable result and the failed source returns no records with `complete: false`, preserving fail-closed downstream behavior.
- Previously complete slices retain their immutable artifacts and remain independently reviewable/syncable.
- Orphan page files created before a manifest advance are ignored and may be reclaimed later.
- Missing referenced pages, digest mismatches, invalid schema, invalid continuation state, or identity mismatch produce an explicit checkpoint error and no network continuation.
- A complete source checkpoint is immutable. A conflicting attempt for the same identity is rejected rather than appended.

## Retention

Keep retention bounded and simple:

- the current incomplete checkpoint for an identity is retained for retry;
- completed checkpoints may be reused for the exact interval;
- completed slice artifacts are retained independently of parent-backlog completion;
- cleanup is limited to a separate local maintenance helper that removes only validated, completed checkpoints older than a configured age;
- incomplete or corrupt checkpoints are never deleted automatically.

No cleanup is required on the critical collection path. This keeps normal one- or two-day runs fast, permits long backlogs to drain incrementally, and avoids destructive recovery behavior.

## Interfaces

Introduce a small slice planner responsible for deterministic interval partitioning, chronological progress, slice receipts, and parent completion. Introduce one source-neutral checkpoint component responsible for identity validation, atomic page publication, manifest advancement, digest verification, completion, and reconstruction. Source collectors supply only:

- their identity fields;
- continuation state;
- raw page payload;
- existing normalization logic.

The three collector functions retain their current public result shapes. Each accepts an optional injected checkpoint store for isolated tests; production collection constructs one store from `CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT` or the default root and passes it explicitly. Checkpointing and slice planning are enabled by default for the `run` command. Each complete slice produces a normal run artifact set with its own immutable identity, rather than accumulating the entire backlog in memory or one output file.

## Tests

Use test-first development. Required behavior tests are:

1. a second-page failure persists page one but returns no records;
2. retrying the identical interval requests only the next page and produces the same result as an uninterrupted run;
3. replaying a complete checkpoint performs no remote request;
4. repeated-page and repeated-cursor guards include restored checkpoint state;
5. interval, request, source-path, or compatibility mismatch cannot reuse a checkpoint;
6. missing pages, page digest mismatch, malformed manifest, and invalid continuation fail closed;
7. an orphan page is ignored;
8. Clockify, Fathom, and Multica output contracts remain unchanged after successful reconstruction;
9. incomplete manifests do not contribute records or complete source coverage;
10. cleanup removes only old, validated, completed checkpoints and preserves incomplete/corrupt state.
11. a long interval partitions into contiguous, non-overlapping slices of at most two local accounting days;
12. a later-slice failure leaves earlier slices complete and independently reviewable;
13. retry resumes the failed slice, skips digest-verified complete slices, and then continues chronologically;
14. parent-backlog completion is not required to use a complete slice;
15. resumed long-backlog results equal the ordered set of uninterrupted slice results without duplicates or gaps.

Targeted collector tests must pass after every red-green cycle, followed by the complete repository regression suite.

## Exclusions

- No external writes or connector mutations.
- No change to approval gates.
- No Multica reporting adapter.
- No Google Sheets transaction recovery.
- No semantic-analysis cache changes.
- No checkpoint reuse across different slice boundaries or request identities.
- No broad checkpoint database or generalized workflow engine.

## Acceptance Criteria

The change is ready for local commit when:

- all three paginated collectors resume after a simulated later-page failure;
- incomplete harvests remain invisible downstream;
- arbitrary finite date ranges drain through bounded chronological slices without accumulating the full backlog in memory;
- every completed slice is independently reviewable and eligible for the existing approval-gated sync path before the parent backlog completes;
- retries neither repeat verified complete slices nor skip the first incomplete slice;
- resumed and uninterrupted complete results are byte-equivalent after canonical JSON encoding;
- corrupt or mismatched state fails closed with credential-free diagnostics;
- the full test suite passes;
- private checkpoint data and the nine unrelated untracked artifacts remain untracked and untouched.
