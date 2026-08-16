# Recovery-Only Collector Checkpoints Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add durable page-level resume and incremental, at-most-two-local-day backlog slices so completed periods become reviewable immediately while incomplete source pages remain private recovery state.

**Architecture:** A focused checkpoint module owns immutable page files, atomic manifests, digest validation, and safe cleanup. A separate slice module owns deterministic Europe/Bucharest date partitioning and parent backlog receipts. The existing collector, review workflow, and persistent runner retain their current single-period contracts while learning to emit and consume an ordered sequence of independently complete slice artifacts.

**Tech Stack:** Python 3 standard library (`dataclasses`, `datetime`, `hashlib`, `json`, `os`, `pathlib`, `zoneinfo`), `unittest`, existing Clockify/Fathom/Multica HTTP adapters.

## Global Constraints

- Apply DRY, KISS, and YAGNI; add no database or workflow framework.
- Work only in `/home/blackthorne/Work/automation-clockify-sync-task-f8ea741c`.
- Keep `.serena/`, `state/`, private cache/evidence, credentials, and unrelated untracked artifacts untouched and untracked.
- Incomplete source pages are recovery state only and must never reach evidence ledgers, review state, acceptance scoring, exports, or source-coverage completion.
- A completed slice is independently reviewable and eligible for the existing approval-gated sync path before its parent backlog completes.
- Process slices oldest-first, sequentially, with contiguous half-open Europe/Bucharest boundaries no wider than two local accounting days.
- Accept any finite valid requested interval; keep memory bounded to one slice and one source page stream.
- Preserve existing public source result shapes and the existing single-slice CLI behavior.
- Keep Clockify, Sheets, Multica, push, deployment, merge, publishing, and client mutations disabled unless separately approved.
- Do not run live inference or discard existing analyzer caches.
- Use strict TDD: each production behavior must first have a test that fails for the expected reason.
- Do not push any commit without approval of its exact SHA.

---

## File Structure

- Create `scripts/collector_checkpoints.py`: source-neutral page checkpoint identity, atomic storage, verification, completion, reconstruction, and cleanup.
- Create `scripts/collector_slices.py`: deterministic local-day slices, backlog manifest, slice receipts, and resume selection.
- Modify `scripts/clockify_sync_collect.py`: inject checkpoints into the three paginated collectors, extract single-slice collection, and orchestrate ordered slices.
- Modify `scripts/clockify_review_run.py`: consume every completed collector slice even when a later slice fails.
- Modify `scripts/clockify_autopilot_runner.py`: accept multiple completed action contracts and record them without losing single-result compatibility.
- Create `tests/test_collector_checkpoints.py`: checkpoint corruption, crash, resume, immutability, and cleanup tests.
- Create `tests/test_collector_slices.py`: boundary, manifest, receipt, and resume tests.
- Modify `tests/test_collector_burst_context.py`: source-specific pagination restart tests.
- Modify `tests/test_process_integration.py`: multi-slice collector artifact and failure tests.
- Modify `tests/test_review_run.py`: ordered multi-slice ingestion tests.
- Modify `tests/test_autopilot_runner.py`: multi-result status and coverage tests.
- Modify `README.md`, `ops/systemd/clockify-work-accounting.env.example`, and `ops/launchd/clockify-work-accounting.env.example`: private checkpoint configuration and operational recovery instructions.

---

### Task 1: Atomic Page Checkpoint Store

**Files:**
- Create: `scripts/collector_checkpoints.py`
- Create: `tests/test_collector_checkpoints.py`

**Interfaces:**
- Produces: `CheckpointError`, `CheckpointIdentity`, `CheckpointState`, and `PageCheckpointStore`.
- `CheckpointIdentity(source: str, since_utc: str, until_utc: str, request_fingerprint: str, compatibility_version: str)` contains no secret values.
- `PageCheckpointStore.open(identity, *, initial_metadata=None) -> CheckpointState` validates the complete manifest and page chain.
- `PageCheckpointStore.append_page(state, *, payload, continuation, signature, metadata=None) -> CheckpointState` publishes one immutable page before atomically advancing the manifest.
- `PageCheckpointStore.mark_complete(state, *, metadata=None) -> CheckpointState` binds the ordered page digest and makes the source checkpoint immutable.
- `PageCheckpointStore.remove_completed_before(cutoff: datetime) -> tuple[Path, ...]` removes only verified complete checkpoints.

- [ ] **Step 1: Write failing identity and first-page persistence tests**

```python
class PageCheckpointStoreTests(unittest.TestCase):
    def identity(self):
        return checkpoints.CheckpointIdentity(
            source="clockify",
            since_utc="2026-08-01T00:00:00Z",
            until_utc="2026-08-03T00:00:00Z",
            request_fingerprint="sha256:request",
            compatibility_version="collector/v1",
        )

    def test_append_publishes_page_before_manifest_reference(self):
        with tempfile.TemporaryDirectory() as directory:
            store = checkpoints.PageCheckpointStore(Path(directory))
            state = store.open(self.identity(), initial_metadata={"snapshot_at": "fixed"})
            state = store.append_page(
                state,
                payload=[{"id": "one"}],
                continuation={"page": 2},
                signature="sha256:page-one",
            )
            manifest = json.loads((state.directory / "manifest.json").read_text())
            page = state.directory / manifest["pages"][0]["path"]
            self.assertTrue(page.is_file())
            self.assertEqual({"page": 2}, state.continuation)
            self.assertFalse(state.complete)

    def test_identity_document_never_contains_secret_fields(self):
        document = self.identity().document()
        self.assertEqual(
            {"source", "since_utc", "until_utc", "request_fingerprint", "compatibility_version"},
            set(document),
        )
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `python3 -m unittest tests.test_collector_checkpoints.PageCheckpointStoreTests.test_append_publishes_page_before_manifest_reference tests.test_collector_checkpoints.PageCheckpointStoreTests.test_identity_document_never_contains_secret_fields -v`

Expected: import failure because `scripts.collector_checkpoints` does not exist.

- [ ] **Step 3: Implement canonical identity, atomic writes, and page append**

```python
SCHEMA_VERSION = "collector-page-checkpoint/v1"

class CheckpointError(ValueError):
    pass

@dataclass(frozen=True)
class CheckpointIdentity:
    source: str
    since_utc: str
    until_utc: str
    request_fingerprint: str
    compatibility_version: str

    def document(self) -> dict[str, str]:
        return dataclasses.asdict(self)

def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")

def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()

def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(_canonical(value) + b"\n")
    os.replace(temporary, path)
```

Implement `CheckpointState` as a frozen dataclass containing the identity, directory, ordered page documents, continuation, metadata, and complete flag. Derive the checkpoint directory from `_digest(identity.document())[7:]`; do not interpolate source values into paths.

- [ ] **Step 4: Run the two tests and verify GREEN**

Run: `python3 -m unittest tests.test_collector_checkpoints.PageCheckpointStoreTests.test_append_publishes_page_before_manifest_reference tests.test_collector_checkpoints.PageCheckpointStoreTests.test_identity_document_never_contains_secret_fields -v`

Expected: 2 tests pass.

- [ ] **Step 5: Write failing corruption, orphan, immutable-completion, and cleanup tests**

Add tests that mutate a referenced page after append, delete a referenced page, add an unreferenced `pages/999999.json`, reopen with a different identity, append after completion, and invoke cleanup across complete, incomplete, and malformed directories. Assert corruption raises `CheckpointError`, the orphan is ignored, completed state rejects append, and cleanup returns only the old verified complete directory.

- [ ] **Step 6: Run the new tests and verify RED**

Run: `python3 -m unittest tests.test_collector_checkpoints -v`

Expected: failures for missing digest-chain validation, immutable completion, and cleanup.

- [ ] **Step 7: Implement full manifest validation, completion, and safe cleanup**

Manifest validation must require exact schema and identity, sequential page indexes, safe relative `pages/NNNNNN.json` paths, matching payload digests, matching aggregate digest when complete, and a valid mapping continuation. Cleanup must load through the same validator and select only `complete: true` manifests whose recorded `completed_at` parses before the cutoff; malformed and incomplete directories are preserved.

- [ ] **Step 8: Run checkpoint tests and commit**

Run: `python3 -m unittest tests.test_collector_checkpoints -v`

Expected: all checkpoint tests pass.

```bash
git add scripts/collector_checkpoints.py tests/test_collector_checkpoints.py
git commit -m "feat: add atomic collector page checkpoints"
```

---

### Task 2: Clockify Pagination Resume

**Files:**
- Modify: `scripts/clockify_sync_collect.py:2180-2286`
- Modify: `tests/test_collector_burst_context.py:435-565`

**Interfaces:**
- Consumes: `PageCheckpointStore` from Task 1.
- Produces: `fetch_clockify(..., checkpoint_store: PageCheckpointStore | None = None)` with its existing result fields unchanged.
- Stores one stable `snapshot_at` in checkpoint metadata at creation and reuses it on retry so running-entry normalization cannot drift.

- [ ] **Step 1: Write a failing second-page resume test**

Create a temporary checkpoint store. First invocation returns 200 fixed entries for page one and raises `OSError("offline")` for page two; assert the public result is `complete: false` with `entries == []`. Invoke again with a replacement HTTP function that raises if page one is requested and returns one final entry for page two; assert 201 entries, two pages, and byte-equality with a separate uninterrupted collection after canonical JSON encoding.

- [ ] **Step 2: Run the Clockify resume test and verify RED**

Run: `python3 -m unittest tests.test_collector_burst_context.CollectorBurstContextTests.test_clockify_retry_resumes_after_persisted_page -v`

Expected: failure because `fetch_clockify` has no `checkpoint_store` argument and requests page one again.

- [ ] **Step 3: Implement Clockify checkpoint integration**

Build a credential-free request fingerprint from workspace ID, user ID, page size, and query contract. Open a `CheckpointIdentity(source="clockify", ...)`; use manifest metadata `snapshot_at` as the stable observation time. Restore page signatures into `seen_pages`, restore `continuation["page"]`, append each validated raw page before incrementing, and mark complete only after a short page. On any `CheckpointError` or remote error, return the existing empty error result and a credential-free reason.

- [ ] **Step 4: Run Clockify pagination and running-entry tests**

Run: `python3 -m unittest tests.test_collector_burst_context.CollectorBurstContextTests.test_clockify_retry_resumes_after_persisted_page tests.test_collector_burst_context.CollectorBurstContextTests.test_clockify_collection_paginates_all_existing_fixed_blocks tests.test_collector_burst_context.CollectorBurstContextTests.test_running_clockify_entry_becomes_snapshot_bounded_existing_block -v`

Expected: all selected tests pass.

- [ ] **Step 5: Add and pass complete-replay and corruption tests**

Assert reopening a complete checkpoint performs zero `clockify_get` calls, and a modified page returns empty entries with `complete: false`. Run `python3 -m unittest tests.test_collector_burst_context -v` and expect the entire module to pass.

- [ ] **Step 6: Commit Clockify resume**

```bash
git add scripts/clockify_sync_collect.py tests/test_collector_burst_context.py
git commit -m "feat: resume Clockify page collection"
```

---

### Task 3: Fathom Cursor Resume Without Page Replay

**Files:**
- Modify: `scripts/clockify_sync_collect.py:2289-2463`
- Modify: `tests/test_collector_burst_context.py:567-720`

**Interfaces:**
- Consumes: Task 1 checkpoint store.
- Produces: `fetch_fathom(..., checkpoint_store: PageCheckpointStore | None = None)` with raw cursor stored only in private manifest continuation and hashed in returned diagnostics.
- Keeps the existing collection-wide retry ceiling per invocation; process restarts receive a fresh bounded retry budget but resume at the stored cursor instead of replaying successful pages.

- [ ] **Step 1: Write a failing cursor-resume test**

First invocation returns page one with `next_cursor="private-next"` and fails page two. Assert no meetings are exposed and returned diagnostics contain only `_fathom_cursor_reference("private-next")`. Retry with an HTTP function that rejects a request without the stored cursor; assert it requests page two directly and returns the ordered complete meetings.

- [ ] **Step 2: Run the cursor-resume test and verify RED**

Run: `python3 -m unittest tests.test_collector_burst_context.CollectorBurstContextTests.test_fathom_retry_resumes_from_private_cursor -v`

Expected: failure because Fathom restarts without persisted continuation.

- [ ] **Step 3: Implement Fathom checkpoint integration**

Fingerprint the endpoint query shape, creation lookback, inclusion flags, and interval without the API key. Restore pages, cursor guard, and page count, then advance the checkpoint after each page. Construct one existing `FathomRetryBudget` per invocation so its ceiling still spans every newly requested page in that invocation. Raw cursor values must never enter stderr, returned error text, run reports, or checkpoint directory names.

- [ ] **Step 4: Run Fathom retry and pagination tests**

Run: `python3 -m unittest tests.test_collector_burst_context.CollectorBurstContextTests.test_fathom_retry_resumes_from_private_cursor tests.test_collector_burst_context.CollectorBurstContextTests.test_fathom_collection_retry_budget_cannot_multiply_across_pages tests.test_collector_burst_context.CollectorBurstContextTests.test_fathom_429_exhaustion_fails_closed_without_network -v`

Expected: all selected tests pass.

- [ ] **Step 5: Add complete-replay, repeated-cursor-after-resume, and corrupt-page tests**

Assert no HTTP call for complete replay, restored cursor signatures catch a repeated cursor, and corrupt state fails closed with no meetings. Run the full collector test module and expect it to pass.

- [ ] **Step 6: Commit Fathom resume**

```bash
git add scripts/clockify_sync_collect.py tests/test_collector_burst_context.py
git commit -m "feat: resume Fathom cursor collection"
```

---

### Task 4: Multica Offset Resume With Endpoint Binding

**Files:**
- Modify: `scripts/clockify_sync_collect.py:2466-2578`
- Modify: `tests/test_collector_burst_context.py:350-433`

**Interfaces:**
- Consumes: Task 1 checkpoint store.
- Produces: `fetch_multica_issues(..., checkpoint_store: PageCheckpointStore | None = None)`.
- Persists both selected endpoint path and next offset; fallback to the second path is allowed only before any page has been committed.

- [ ] **Step 1: Write a failing offset-resume and path-binding test**

Commit 100 `/api/issues` rows, fail at offset 100, then retry. Assert the retry requests `/api/issues?limit=100&offset=100` first, never requests offset zero, and never tries `/issues`. Complete with one row and assert 101 ordered issues.

- [ ] **Step 2: Run the Multica resume test and verify RED**

Run: `python3 -m unittest tests.test_collector_burst_context.CollectorBurstContextTests.test_multica_retry_resumes_bound_endpoint_and_offset -v`

Expected: failure because collection restarts and endpoint choice is not persisted.

- [ ] **Step 3: Implement Multica checkpoint integration**

Use a fingerprint containing sanitized server origin, workspace ID, interval, page size, and API contract. Before the first committed page, endpoint failure may select the alternate path and create that path's checkpoint identity. After a committed page, any failure returns incomplete state and retains the same path and offset for retry. Restore repeated-page signatures before making a request.

- [ ] **Step 4: Run Multica tests and verify GREEN**

Run: `python3 -m unittest tests.test_collector_burst_context.CollectorBurstContextTests.test_multica_retry_resumes_bound_endpoint_and_offset tests.test_collector_burst_context.CollectorBurstContextTests.test_multica_issue_collection_paginates_past_one_hundred tests.test_collector_burst_context.CollectorBurstContextTests.test_multica_issue_collection_filters_to_requested_activity_window -v`

Expected: all selected tests pass.

- [ ] **Step 5: Add complete-replay, endpoint-mismatch, and corruption tests, then commit**

Run: `python3 -m unittest tests.test_collector_burst_context -v`

Expected: the full module passes.

```bash
git add scripts/clockify_sync_collect.py tests/test_collector_burst_context.py
git commit -m "feat: resume Multica issue collection"
```

---

### Task 5: Deterministic Backlog Slices and Receipts

**Files:**
- Create: `scripts/collector_slices.py`
- Create: `tests/test_collector_slices.py`

**Interfaces:**
- Produces: `CollectionSlice`, `BacklogIdentity`, `BacklogState`, and `BacklogStore`.
- `plan_slices(since: datetime, until: datetime, *, zone: ZoneInfo, max_days: int = 2) -> tuple[CollectionSlice, ...]` returns contiguous half-open intervals.
- `BacklogStore.open(identity, slices) -> BacklogState`, `record_complete(state, slice_id, result_path, result_digest) -> BacklogState`, and `next_incomplete(state) -> CollectionSlice | None` validate immutable receipts before skipping work.

- [ ] **Step 1: Write failing boundary tests**

```python
def test_long_interval_slices_oldest_first_without_gaps(self):
    slices = collector_slices.plan_slices(
        dt.datetime(2026, 8, 1, tzinfo=BUCHAREST),
        dt.datetime(2026, 8, 8, tzinfo=BUCHAREST),
        zone=BUCHAREST,
    )
    self.assertEqual(4, len(slices))
    self.assertEqual(slices[0].until, slices[1].since)
    self.assertLessEqual((slices[0].until.date() - slices[0].since.date()).days, 2)

def test_dst_boundaries_are_local_midnights_not_fixed_48_hours(self):
    slices = collector_slices.plan_slices(
        dt.datetime(2026, 10, 24, tzinfo=BUCHAREST),
        dt.datetime(2026, 10, 27, tzinfo=BUCHAREST),
        zone=BUCHAREST,
    )
    self.assertEqual(dt.time.min, slices[0].until.astimezone(BUCHAREST).time())
```

- [ ] **Step 2: Run slice tests and verify RED**

Run: `python3 -m unittest tests.test_collector_slices -v`

Expected: import failure because `scripts.collector_slices` does not exist.

- [ ] **Step 3: Implement slice identity and partitioning**

Construct boundaries by local calendar dates with `datetime.combine(date, time.min, tzinfo=zone)`, not `timedelta(hours=48)`. Preserve partial first and last boundaries, reject naive/reversed/non-finite values, and derive each `slice_id` from canonical timezone and UTC boundary strings.

- [ ] **Step 4: Add failing backlog receipt tests**

Write tests proving that a complete first slice is returned in `completed`, the next slice remains selected, a missing or digest-mismatched result artifact raises `BacklogError`, and parent completion remains false until every receipt verifies.

- [ ] **Step 5: Implement atomic backlog manifest and verified resume**

Use the same write-page-before-manifest ordering concept: compute and verify the result artifact digest before atomically appending a receipt. Bind schema, requested interval, timezone, max-days policy, runtime compatibility, and ordered slice identities into the parent identity. Never mark a failed or incomplete run directory complete.

- [ ] **Step 6: Run tests and commit**

Run: `python3 -m unittest tests.test_collector_slices -v`

Expected: all slice and receipt tests pass.

```bash
git add scripts/collector_slices.py tests/test_collector_slices.py
git commit -m "feat: add incremental collection backlog slices"
```

---

### Task 6: Emit Independent Completed Slice Bundles

**Files:**
- Modify: `scripts/clockify_sync_collect.py:3047-3218`
- Modify: `tests/test_process_integration.py:73-143`

**Interfaces:**
- Consumes: Tasks 1 and 5.
- Produces: `_collect_slice(args, routing, fleet, cenv, fenv, since, until, reason, checkpoint_store, run_dir) -> tuple[Path, dict[str, object]]` for exactly one bounded slice.
- `run(args)` prints one `run-report.md` path per complete slice, flushes after each path, stops at the first incomplete slice, and returns nonzero without retracting earlier paths.

- [ ] **Step 1: Write a failing long-range artifact test**

Patch `compute_range` to return a seven-day interval, patch all collectors and session collectors to return complete fixtures, and patch `RUNS` plus checkpoint root to temporary directories. Assert four distinct direct-child run directories, contiguous slice date ranges, and four ordered stdout paths. Assert each evidence ledger contains only its slice's evidence.

- [ ] **Step 2: Run the integration test and verify RED**

Run: `python3 -m unittest tests.test_process_integration.ProcessIntegrationTests.test_collector_emits_each_completed_backlog_slice -v`

Expected: one whole-range artifact is emitted instead of four slices.

- [ ] **Step 3: Extract single-slice collection without changing its bundle contract**

Move the body that collects sources, sessions, enriched context, proposals, evidence ledger, compact report, and markdown into `_collect_slice`. Pass the slice boundaries to every local and remote session collector so exceptional backlogs do not accumulate whole-range session evidence in memory. Construct a deterministic collision-safe run directory from slice boundary dates plus the slice identity digest; if it already exists, verify its receipt digest instead of overwriting it.

- [ ] **Step 4: Orchestrate slices and inject one checkpoint store**

`run` resolves the overall range once, opens the parent backlog, skips only verified receipts, calls `_collect_slice` oldest-first, verifies all required evidence-ledger sources complete, records the slice receipt, prints its report path with `flush=True`, and releases the slice's in-memory evidence before continuing.

- [ ] **Step 5: Write a failing later-slice failure test**

Make the first slice complete and the second slice return incomplete Fathom evidence. Assert the first run bundle and receipt remain complete, the second is not printed as reviewable, later slices are not attempted, and retry starts at the second slice without recollecting the first.

- [ ] **Step 6: Implement fail-closed stop and verified retry**

Create a diagnostic run bundle for the failed slice if current diagnostics require it, but keep it out of completed receipts and stdout's reviewable paths. Return exit code 2 after preserving the parent backlog and source checkpoint state.

- [ ] **Step 7: Run collector integration tests and commit**

Run: `python3 -m unittest tests.test_process_integration tests.test_entrypoints -v`

Expected: all selected modules pass and single-slice output remains compatible.

```bash
git add scripts/clockify_sync_collect.py tests/test_process_integration.py tests/test_entrypoints.py
git commit -m "feat: emit completed collection slices incrementally"
```

---

### Task 7: Process Every Completed Slice Through Review and Runner State

**Files:**
- Modify: `scripts/clockify_review_run.py:359-376, 735-930`
- Modify: `scripts/clockify_autopilot_runner.py:55-75, 164-263`
- Modify: `tests/test_review_run.py`
- Modify: `tests/test_autopilot_runner.py`

**Interfaces:**
- Produces: `_collector_run_dirs(stdout: str) -> tuple[Path, ...]` validating every direct child of `RUNS` in order.
- Produces: `_process_run(args, run_dir, acceptance_gate) -> tuple[int, Path]` containing the existing accounting, quality, review-state, action-contract, and summary sequence for one slice.
- Produces: runner `_result_paths(stdout, root) -> tuple[Path, ...]`; status keeps legacy `result` as the newest path and adds ordered `results`.

- [ ] **Step 1: Write a failing review test for successful paths followed by collector failure**

Mock collector stdout with two valid completed run-report paths and return code 2 representing a third failed slice. Mock `_process_run` and assert the first two directories are processed oldest-first, both action-contract paths are printed, and main then returns 2 without pretending the backlog completed.

- [ ] **Step 2: Run the review test and verify RED**

Run: `python3 -m unittest tests.test_review_run.ReviewRunResultTests.test_completed_slices_are_processed_before_later_collection_failure -v`

Expected: current main returns immediately on collector return code 2 and processes zero slices.

- [ ] **Step 3: Extract one-slice review and accept ordered collector outputs**

Replace `_collector_run_dir` with `_collector_run_dirs`, rejecting duplicates, unsafe parents, or missing reports. Move the existing post-collection body into `_process_run`. Process every validated completed path even if the collector later returned nonzero; stop on the first review failure and preserve already written action contracts.

- [ ] **Step 4: Write failing runner multi-result tests**

Return stdout containing two `autopilot-result.json` paths. Assert status includes both under `results`, keeps the second under `result`, updates source coverage once per result's own date range, and `mark_reported` accepts either path listed in the current status while rejecting unrelated paths.

- [ ] **Step 5: Implement multi-result runner status**

Validate every result path under `runs`, load and update coverage chronologically for each, compute retry status from the first incomplete/failed result, and write one atomic compact status. Do not send comments or perform external mutations.

- [ ] **Step 6: Run review and runner modules**

Run: `python3 -m unittest tests.test_review_run tests.test_autopilot_runner tests.test_source_coverage -v`

Expected: all selected tests pass.

- [ ] **Step 7: Commit incremental review handling**

```bash
git add scripts/clockify_review_run.py scripts/clockify_autopilot_runner.py tests/test_review_run.py tests/test_autopilot_runner.py
git commit -m "feat: review completed backlog slices incrementally"
```

---

### Task 8: Safe Retention Command, Operations Documentation, and Full Verification

**Files:**
- Modify: `scripts/clockify_sync_collect.py:3221-3279`
- Modify: `tests/test_entrypoints.py`
- Modify: `README.md`
- Modify: `ops/systemd/clockify-work-accounting.env.example`
- Modify: `ops/launchd/clockify-work-accounting.env.example`

**Interfaces:**
- Produces CLI: `python3 scripts/clockify_sync_collect.py cleanup-checkpoints --completed-before YYYY-MM-DD --checkpoint-root ABSOLUTE_PATH`.
- Cleanup prints only counts and checkpoint identity digests, never page filenames, payloads, cursor values, or credentials.

- [ ] **Step 1: Write failing cleanup CLI tests**

Create old complete, recent complete, incomplete, and corrupt checkpoints under a temporary explicit root. Invoke `main` with `cleanup-checkpoints`; assert only the old complete directory is removed, output contains `removed=1 preserved=3`, and invalid or relative roots return 2 without deletion.

- [ ] **Step 2: Run entrypoint tests and verify RED**

Run: `python3 -m unittest tests.test_entrypoints -v`

Expected: parser rejects `cleanup-checkpoints`.

- [ ] **Step 3: Implement the bounded maintenance command**

Require an explicit absolute root for CLI cleanup even though runtime collection has a default. Parse the cutoff as a UTC date boundary, call `remove_completed_before`, and report aggregate counts only. Do not run cleanup automatically during collection.

- [ ] **Step 4: Document runtime and recovery behavior**

Document `CLOCKIFY_COLLECTOR_CHECKPOINT_ROOT=/absolute/private/state/collector-checkpoints`, the default private root, two-day maximum slices, immediate completed-slice review availability, oldest-first retry, failure semantics, cleanup command, and the fact that checkpoint contents are private recovery evidence. Add the environment variable to both service examples without embedding a credential or real private path.

- [ ] **Step 5: Run targeted verification**

Run:

```bash
python3 -m unittest tests.test_collector_checkpoints tests.test_collector_slices tests.test_collector_burst_context tests.test_process_integration tests.test_review_run tests.test_autopilot_runner tests.test_source_coverage tests.test_entrypoints -v
```

Expected: all targeted tests pass with no warnings or external calls.

- [ ] **Step 6: Run full regression verification**

Run: `python3 -m unittest discover -s tests -v`

Expected: the complete repository suite passes; the prior baseline was 505 passed with 2 skipped before this feature.

- [ ] **Step 7: Verify repository hygiene and private-state preservation**

Run:

```bash
git diff --check
git status --short
git diff --stat origin/codex/clockify-analyzer-determinism...HEAD
```

Expected: only intentional tracked implementation, tests, docs, and plan/spec commits appear; `.serena/` and `state/` remain unrelated untracked directories; no private payload is staged.

- [ ] **Step 8: Commit the completed feature locally**

```bash
git add scripts/clockify_sync_collect.py README.md ops/systemd/clockify-work-accounting.env.example ops/launchd/clockify-work-accounting.env.example tests/test_entrypoints.py
git commit -m "docs: document resilient collection recovery"
```

- [ ] **Step 9: Obtain independent review and prepare exact-SHA approval**

Review every task diff against the approved design, rerun any reviewer-requested targeted tests, then report the local HEAD SHA, commit list, full-suite result, remaining live-acceptance gaps, and confirmation that no external mutation occurred. Request separate approval before `git push` of that exact SHA.
