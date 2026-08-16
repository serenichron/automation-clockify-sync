# Recovery-Only Collector Checkpoints

## Objective

Make routine one- or two-day Clockify autopilot harvests cheap to retry without allowing an incomplete source harvest to become accounting evidence. A failure after one or more remote pages must preserve successful work locally, and a later invocation for the identical interval must resume rather than start from page one.

This design covers the paginated Clockify, Fathom, and Multica collectors only. It does not change semantic analysis, reconciliation, external reporting, Google Sheets, or Clockify mutation behavior.

## Chosen Approach

Use an append-only page spool plus a small atomic manifest for each source and collection interval.

This is preferable to rewriting one growing JSON checkpoint after every page because each successful page becomes an immutable recovery unit. It is preferable to SQLite because the expected workload is a short one- or two-day interval and does not justify a database lifecycle.

## Safety Invariant

Checkpointed pages are recovery state, not evidence.

- An incomplete manifest must never expose its records to proposal generation, reconciliation, acceptance scoring, exports, or source-coverage completion.
- The existing collector result remains fail-closed on interruption: `complete` is false and the public records list is empty.
- Only a complete manifest may be normalized into the existing source result contract.
- A corrupt, incompatible, or mismatched checkpoint must fail closed. It must not be silently reset, merged into a new interval, or reported as complete.

## Identity and Layout

Each checkpoint identity binds:

- schema version;
- source name;
- exact UTC `since` and `until` boundaries;
- sanitized request fingerprint, including source-specific query behavior;
- collector/runtime compatibility version.

The identity produces a deterministic directory name below a private, configurable checkpoint root. `CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT` may override the location; the default is `state/collector-checkpoints` under the canonical repository root, which is already within local untracked state. The directory contains:

- `manifest.json`: atomically replaced state describing identity, ordered page references, the next page/cursor/offset, completion, and aggregate digest;
- `pages/000001.json`, `pages/000002.json`, and so on: immutable, atomically published page envelopes;
- no credentials, authorization headers, or raw cursor text in diagnostic output.

Page envelopes may contain the private source payload needed to reconstruct the final sanitized result. Their filenames and manifest digests are safe operational metadata; their contents remain private evidence.

## Collection Flow

For each source:

1. Resolve the exact checkpoint identity before the first request.
2. Load and validate any existing manifest and every referenced page digest.
3. If the manifest is complete, reconstruct and return the existing normalized source result without a remote request.
4. If it is incomplete, reconstruct pagination guards from stored pages and resume from the recorded continuation token.
5. After each successful remote page, atomically write the page envelope, then atomically advance the manifest. The ordering ensures a manifest never references a partially written page.
6. When the remote source proves pagination complete, atomically mark the manifest complete and bind an aggregate digest over the ordered page digests.
7. Normalize all stored pages through the existing source-specific sanitization and interval filtering, returning the same output shape as an uninterrupted run.

Source continuations remain source-specific:

- Clockify: next one-based page number.
- Fathom: next cursor, stored privately; logs and returned failures retain only the existing hashed cursor reference.
- Multica: selected API path and next offset, so fallback between `/api/issues` and `/issues` cannot mix pages.

## Failure Semantics

- Network, timeout, retry-budget, validation, repeated-page, or safety-limit failure leaves the last valid manifest incomplete.
- The collector returns no source records and `complete: false`, preserving current downstream behavior.
- Orphan page files created before a manifest advance are ignored and may be reclaimed later.
- Missing referenced pages, digest mismatches, invalid schema, invalid continuation state, or identity mismatch produce an explicit checkpoint error and no network continuation.
- A complete checkpoint is immutable. A conflicting attempt for the same identity is rejected rather than appended.

## Retention

Keep retention bounded and simple:

- the current incomplete checkpoint for an identity is retained for retry;
- completed checkpoints may be reused for the exact interval;
- cleanup is limited to a separate local maintenance helper that removes only validated, completed checkpoints older than a configured age;
- incomplete or corrupt checkpoints are never deleted automatically.

No cleanup is required on the critical collection path. This keeps normal one- or two-day runs fast and avoids destructive recovery behavior.

## Interfaces

Introduce one source-neutral checkpoint component responsible for identity validation, atomic page publication, manifest advancement, digest verification, completion, and reconstruction. Source collectors supply only:

- their identity fields;
- continuation state;
- raw page payload;
- existing normalization logic.

The three collector functions retain their current public result shapes. Each accepts an optional injected checkpoint store for isolated tests; production collection constructs one store from `CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT` or the default root and passes it explicitly. Checkpointing is enabled by default for the `run` command.

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

Targeted collector tests must pass after every red-green cycle, followed by the complete repository regression suite.

## Exclusions

- No external writes or connector mutations.
- No change to approval gates.
- No Multica reporting adapter.
- No Google Sheets transaction recovery.
- No semantic-analysis cache changes.
- No automatic recovery across different date intervals.
- No broad checkpoint database or generalized workflow engine.

## Acceptance Criteria

The change is ready for local commit when:

- all three paginated collectors resume after a simulated later-page failure;
- incomplete harvests remain invisible downstream;
- resumed and uninterrupted complete results are byte-equivalent after canonical JSON encoding;
- corrupt or mismatched state fails closed with credential-free diagnostics;
- the full test suite passes;
- private checkpoint data and the nine unrelated untracked artifacts remain untracked and untouched.
