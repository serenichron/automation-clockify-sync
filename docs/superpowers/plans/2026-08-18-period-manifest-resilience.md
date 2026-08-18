# Period Manifest and Runtime Resilience Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a tamper-evident period coordinator, exact source-interval coverage debt, digest-bound slice completion, and bounded retry/timeout behavior for both routine and exceptional reconciliation runs.

**Architecture:** An append-only event store references immutable artifacts by digest and derives one period manifest through explicit states. Existing page checkpoints and two-day slices remain intact; focused modules add exact interval debt, structured failure receipts, completion bundles, and owned-child process control.

**Tech Stack:** Python 3 standard library, dataclasses, `datetime`, `hashlib`, JSON/JSONL, `subprocess`, `fcntl`, `unittest`, JSON Schema.

**Spec:** `docs/superpowers/specs/2026-08-17-clockify-reconciliation-publication-manifest-design.md`

## Global Constraints

- Apply DRY, KISS, and YAGNI; do not add a database, queue, or workflow framework.
- Work only in `/home/blackthorne/Work/automation-clockify-sync-task-f8ea741c`; preserve unrelated and private untracked artifacts.
- The manifest stores paths, schemas, compatibility versions, digests, counts, and safe statuses—never raw private evidence.
- Every state transition re-hashes referenced artifacts; drift fails closed.
- Exact source/half-open-interval debt cannot be cleared by a broader day-level status unless the exact compatible slice completion verifies.
- Every debt item owns its retry count, eligibility time, and terminal reason.
- A timed-out child cannot retain the runner lock indefinitely; terminate only the owned process group and preserve completed slice receipts.
- Failure receipts never contain raw cursors, private filenames, payloads, credentials, or client evidence.
- Existing checkpoint compatibility and content-addressed caches remain reusable.
- Use strict TDD and watch every new behavior fail before implementation.
- No external calls or mutations are required for implementation tests.

---

### Task 1: Period Identity and Append-Only Coordinator Events

**Files:**
- Create: `schemas/reconciliation-period-v1.json`
- Create: `schemas/reconciliation-event-v1.json`
- Create: `scripts/reconciliation_manifest.py`
- Create: `tests/test_reconciliation_manifest.py`

**Interfaces:**
- Produces `ManifestError`, `ArtifactIdentity`, `PeriodIdentity`, `CoordinatorEvent`, and `CoordinatorEventStore`.

```python
@dataclass(frozen=True)
class PeriodIdentity:
    member_id: str
    workspace_id: str
    timezone: str
    since: datetime
    until: datetime
    revision: int
    compatibility_version: str = "reconciliation-period/v1"

    def document(self) -> dict[str, object]: ...

    @property
    def period_id(self) -> str: ...

class CoordinatorEventStore:
    def __init__(self, path: Path): ...
    def append(self, identity, event_type, payload, *, occurred_at) -> CoordinatorEvent: ...
    def load(self, identity) -> tuple[CoordinatorEvent, ...]: ...
    def verify(self, identity) -> tuple[CoordinatorEvent, ...]: ...
```

- [ ] **Step 1: Write failing identity and hash-chain tests**

```python
class PeriodIdentityTests(unittest.TestCase):
    def test_bucharest_august_period_is_half_open_and_deterministic(self):
        identity = august_identity()
        self.assertEqual("2026-07-31T21:00:00Z", identity.document()["since_utc"])
        self.assertEqual("2026-08-15T21:00:00Z", identity.document()["until_utc"])
        self.assertEqual(identity.period_id, august_identity().period_id)

class CoordinatorEventStoreTests(unittest.TestCase):
    def test_append_chains_events_and_rejects_reorder_or_truncation(self):
        store = CoordinatorEventStore(self.path)
        first = store.append(self.identity, "period_opened", {"revision": 1}, occurred_at=NOW)
        second = store.append(self.identity, "slice_completed", {"slice_id": "s1"}, occurred_at=LATER)
        self.assertEqual(first.event_digest, second.previous_digest)
        reorder_jsonl(self.path)
        with self.assertRaises(ManifestError):
            store.verify(self.identity)
```

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m unittest tests.test_reconciliation_manifest.PeriodIdentityTests tests.test_reconciliation_manifest.CoordinatorEventStoreTests -v`

Expected: import failure because the module does not exist.

- [ ] **Step 3: Implement canonical identity and append-only verification**

```python
def _canonical(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()

def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()

def _event_document(sequence, period_id, event_type, payload, previous_digest, occurred_at):
    unsigned = {
        "sequence": sequence,
        "period_id": period_id,
        "event_type": event_type,
        "payload": payload,
        "previous_digest": previous_digest,
        "occurred_at": occurred_at,
    }
    return {**unsigned, "event_digest": _digest(unsigned)}
```

Append one canonical JSON line with `O_APPEND`, flush, and `os.fsync`. Verification requires contiguous sequences from 1, matching period, monotonic timestamps, correct previous digest, and exact event digest.

- [ ] **Step 4: Add wrong-period, duplicate, blank-line, digest, and invalid-interval tests**

Reject naive/reversed intervals, revision below 1, unsupported timezone, wrong-period lines, duplicates, blank/truncated final lines, and mutated payloads.

- [ ] **Step 5: Validate schemas and commit**

Run: `python3 -m unittest tests.test_reconciliation_manifest -v`

Expected: all identity/event tests pass.

```bash
git add schemas/reconciliation-period-v1.json schemas/reconciliation-event-v1.json scripts/reconciliation_manifest.py tests/test_reconciliation_manifest.py
git commit -m "feat: add append-only reconciliation periods"
```

---

### Task 2: Derived Manifest and Artifact Drift Gates

**Files:**
- Create: `schemas/reconciliation-manifest-v1.json`
- Modify: `scripts/reconciliation_manifest.py`
- Modify: `tests/test_reconciliation_manifest.py`

**Interfaces:**
- Produces `ReconciliationManifest` and `ReconciliationCoordinator.derive() -> ReconciliationManifest`.
- Allowed states: `collecting`, `reconciling`, `awaiting_review`, `approved`, `posting`, `verifying`, `publication_prepared`, `publication_authorized`, `published`.
- Produces CLI subcommands `init`, `import-artifacts`, and `verify` with the exact arguments used by the August recovery plan. `init --dry-run` performs no write; every output path is resolved and constrained beneath its requested private recovery root.

- [ ] **Step 1: Write failing legal-transition and drift tests**

```python
def test_complete_verified_events_derive_awaiting_review(self):
    coordinator = fixture_with_complete_slices_and_evidence()
    manifest = coordinator.derive()
    self.assertEqual("awaiting_review", manifest.state)
    self.assertTrue(manifest.manifest_digest.startswith("sha256:"))

def test_referenced_artifact_drift_blocks_state_advance(self):
    coordinator, artifact = fixture_with_complete_slices_and_evidence(return_artifact=True)
    artifact.write_text("changed")
    with self.assertRaisesRegex(ManifestError, "artifact digest mismatch"):
        coordinator.derive()
```

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m unittest tests.test_reconciliation_manifest.ReconciliationCoordinatorTests -v`

Expected: FAIL because derivation is absent.

- [ ] **Step 3: Implement pure derivation and transition table**

```python
ADVANCING_EVENTS = {
    "period_opened": "collecting",
    "collection_complete": "reconciling",
    "reconciliation_complete": "awaiting_review",
    "review_approved": "approved",
    "posting_started": "posting",
    "posting_complete": "verifying",
    "clockify_readback_verified": "verifying",
    "publication_prepared": "publication_prepared",
    "publication_authorized": "publication_authorized",
    "shared_report_verified": "publication_authorized",
    "publication_complete": "published",
}

BLOCKER_EVENTS = {
    "coverage_incomplete", "semantic_exceptions", "awaiting_approval",
    "post_interrupted", "readback_mismatch", "report_mismatch",
    "currency_quote_unavailable", "publication_deferred",
}

AUDIT_EVENTS = {
    "report_residual_resolved", "fathom_repair_complete",
    "coverage_limitation_approved",
}

def derive(self) -> ReconciliationManifest:
    events = self.store.verify(self.identity)
    state = "collecting"
    for event in events:
        _verify_artifact_refs(event.payload.get("artifacts", []))
        state = _apply_transition(state, event)
    return _manifest(self.identity, state, events)
```

Blocker events update blockers and exceptions without advancing state. Audit events attach verified artifacts without advancing state. `shared_report_verified` is legal only after `publication_authorized`, and `publication_complete` is legal only when verified report and Slack receipts bind the same contract and idempotency identity. Every event name above is represented in `schemas/reconciliation-event-v1.json`; all other event types fail closed. Manifest serialization is canonical and digest-bound.

- [ ] **Step 4: Add illegal transition and raw-evidence rejection tests**

Reject posting before approval, `publication_prepared` before `clockify_readback_verified`, authorization before preparation, report verification before authorization, completion before report plus Slack receipts, unknown event types, duplicated terminal transitions, and payload keys named `transcript`, `raw_payload`, `cursor`, `api_key`, or `credential`. Exercise every advancing, blocker, and audit event in schema and derivation tests, including the exact event names used by the August runbook.

Add process-level CLI tests for `init --dry-run`, `init`, `import-artifacts`, and `verify`. Assert the dry run leaves no files, import stores only safe artifact identities, verification detects artifact drift, and invalid output traversal is rejected.

- [ ] **Step 5: Run and commit**

Run: `python3 -m unittest tests.test_reconciliation_manifest -v`

Expected: all tests pass.

```bash
git add schemas/reconciliation-manifest-v1.json scripts/reconciliation_manifest.py tests/test_reconciliation_manifest.py
git commit -m "feat: derive verified reconciliation manifests"
```

---

### Task 3: Exact Source-Interval Debt and Per-Debt Retries

**Files:**
- Modify: `scripts/source_coverage.py:22-162`
- Modify: `tests/test_source_coverage.py`
- Modify: `scripts/clockify_autopilot_runner.py:130-284`
- Modify: `tests/test_autopilot_runner.py`

**Interfaces:**
- Produces `SourceInterval`, `DebtItem`, and `SourceDebtStore` while preserving `read()`, `write()`, and `active_debt()` as migration adapters.

```python
class SourceDebtStore:
    def record_failure(self, interval, *, failure_class, retryable, resume_state_digest, attempted_at) -> DebtItem: ...
    def record_complete(self, interval, *, completion_bundle_digest, completed_at) -> DebtItem: ...
    def eligible(self, now) -> tuple[DebtItem, ...]: ...
    def active(self) -> tuple[DebtItem, ...]: ...
    def verify(self) -> None: ...
```

- [ ] **Step 1: Write failing exact-interval tests**

```python
def test_completing_one_failed_slice_does_not_clear_adjacent_debt(self):
    first = interval("sessions/macbook", "2026-08-01", "2026-08-03")
    second = interval("sessions/macbook", "2026-08-03", "2026-08-05")
    store.record_failure(first, failure_class="offline", retryable=True, resume_state_digest="sha256:a", attempted_at=NOW)
    store.record_failure(second, failure_class="offline", retryable=True, resume_state_digest="sha256:b", attempted_at=NOW)
    store.record_complete(first, completion_bundle_digest="sha256:c", completed_at=LATER)
    self.assertEqual((second.debt_id,), tuple(item.debt_id for item in store.active()))

def test_two_sources_have_independent_retry_counts(self):
    mac = store.record_failure(MAC, failure_class="offline", retryable=True, resume_state_digest="sha256:m", attempted_at=NOW)
    desktop = store.record_failure(DESKTOP, failure_class="offline", retryable=True, resume_state_digest="sha256:d", attempted_at=NOW)
    mac = store.record_failure(MAC, failure_class="offline", retryable=True, resume_state_digest="sha256:m", attempted_at=LATER)
    self.assertEqual(2, mac.retry_count)
    self.assertEqual(1, desktop.retry_count)
```

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m unittest tests.test_source_coverage -v`

Expected: failures because coverage is currently day/source-granular.

- [ ] **Step 3: Implement canonical interval identity and append-only debt events**

Debt ID includes source, UTC half-open boundaries, slice ID, and compatibility version. `record_complete()` resolves only the exact matching identity after verifying its bundle digest. Exhaustion sets `status="exhausted"` but remains active coverage debt for later scheduled runs.

- [ ] **Step 4: Integrate independent retry selection into the runner**

Replace the single `coverage_retry_attempts` decision with eligible debt records. The runner may summarize aggregate counts in status, but retry scheduling and exhaustion are per debt ID.

- [ ] **Step 5: Add migration and conservative-corruption tests**

Import the old schema only as oldest-day debt without claiming exact coverage. Invalid old state creates a visible migration warning instead of silently returning empty debt.

- [ ] **Step 6: Run and commit**

Run: `python3 -m unittest tests.test_source_coverage tests.test_autopilot_runner -v`

Expected: selected suites pass.

```bash
git add scripts/source_coverage.py scripts/clockify_autopilot_runner.py tests/test_source_coverage.py tests/test_autopilot_runner.py
git commit -m "feat: track exact source interval debt"
```

---

### Task 4: Structured Failure Receipts and Slice Completion Bundles

**Files:**
- Create: `scripts/collector_receipts.py`
- Create: `tests/test_collector_receipts.py`
- Modify: `scripts/collector_slices.py:42-231`
- Modify: `scripts/clockify_sync_collect.py:3594-3982`
- Modify: `tests/test_collector_slices.py`
- Modify: `tests/test_process_integration.py`

**Interfaces:**
- Produces `FailureReceipt`, `FailureReceiptStore`, `SliceArtifact`, `SliceCompletionBundle`, `build_completion_bundle()`, and `verify_completion_bundle()`.

- [ ] **Step 1: Write failing sanitized failure-receipt tests**

```python
def test_failure_receipt_contains_only_safe_identity_digests(self):
    receipt = failure_receipt(source="fathom", cursor="private", credential="secret")
    document = receipt.document()
    self.assertEqual({
        "source", "slice_id", "checkpoint_identity_digest", "failure_class",
        "retryable", "resume_state_digest", "occurred_at", "receipt_digest",
    }, set(document))
    self.assertNotIn("private", json.dumps(document))
    self.assertNotIn("secret", json.dumps(document))
```

- [ ] **Step 2: Write failing completion-bundle drift tests**

```python
def test_bundle_binds_every_downstream_artifact(self):
    bundle = build_completion_bundle(RUN_DIR, slice_=SLICE)
    self.assertEqual(REQUIRED_KINDS, {item.kind for item in bundle.artifacts})
    (RUN_DIR / "quality_report.json").write_text("{}")
    with self.assertRaises(CollectorReceiptError):
        verify_completion_bundle(bundle)
```

- [ ] **Step 3: Run and verify RED**

Run: `python3 -m unittest tests.test_collector_receipts tests.test_collector_slices -v`

Expected: import failures for the new receipt types.

- [ ] **Step 4: Implement append-only failure receipts and canonical bundles**

Required artifact kinds are `run_report`, `evidence_ledger`, `semantic_analysis`, `accounting_result`, `quality_report`, and `review_snapshot`; replay identity is required only after replay. Bind source coverage and runtime identity digests.

- [ ] **Step 5: Integrate collector failure and completion branches**

On incomplete slice, append a safe receipt and preserve checkpoints/run directory. On completed slice, verify all required files before `BacklogStore.record_complete()`, then call `SourceDebtStore.record_complete()` with the exact source/UTC half-open interval identity and verified completion-bundle digest. The two stores update only after the bundle verifies; a failure between writes is retry-safe because replay derives both records idempotently from the same bundle. Artifact drift prevents downstream reuse but never deletes page checkpoints.

Add an integration test with two adjacent debt intervals: completing one verified bundle clears only its exact debt ID, a drifted bundle clears neither, and replay after an interrupted store update converges without duplicating completion events.

- [ ] **Step 6: Run and commit**

Run: `python3 -m unittest tests.test_collector_receipts tests.test_collector_slices tests.test_process_integration -v`

Expected: all selected tests pass.

```bash
git add scripts/collector_receipts.py scripts/collector_slices.py scripts/clockify_sync_collect.py tests/test_collector_receipts.py tests/test_collector_slices.py tests/test_process_integration.py
git commit -m "feat: bind collector completion and failure receipts"
```

---

### Task 5: Bounded Owned-Child Runtime

**Files:**
- Create: `scripts/autopilot_process.py`
- Create: `tests/test_autopilot_process.py`
- Modify: `scripts/clockify_autopilot_runner.py:38-55,130-284`
- Modify: `tests/test_autopilot_runner.py`
- Modify: `ops/systemd/clockify-work-accounting.env.example`
- Modify: `ops/launchd/clockify-work-accounting.env.example`

**Interfaces:**
- Produces `ChildTimeoutConfig`, `ChildResult`, and `run_child_bounded(command, *, cwd, timeout, environment) -> ChildResult`.

- [ ] **Step 1: Write a failing timeout/termination test**

```python
def test_hung_child_is_terminated_and_returns_sanitized_timeout(self):
    result = run_child_bounded(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=self.root,
        timeout=ChildTimeoutConfig(total_seconds=1, grace_seconds=1),
        environment={},
    )
    self.assertTrue(result.timed_out)
    self.assertIsNone(result.returncode)
    self.assertLess(result.duration_seconds, 5)
```

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m unittest tests.test_autopilot_process -v`

Expected: import failure because `scripts.autopilot_process` does not exist.

- [ ] **Step 3: Implement owned process-group control**

```python
process = subprocess.Popen(
    command, cwd=cwd, env=dict(environment), text=True,
    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    start_new_session=True,
)
try:
    stdout, stderr = process.communicate(timeout=timeout.total_seconds)
except subprocess.TimeoutExpired:
    os.killpg(process.pid, signal.SIGTERM)
    try:
        stdout, stderr = process.communicate(timeout=timeout.grace_seconds)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        stdout, stderr = process.communicate()
    return ChildResult(None, stdout, _sanitize(stderr), True, elapsed())
```

Never address a PID/process group not created by this function.

- [ ] **Step 4: Integrate routine and backlog budgets**

Add validated environment variables `CLOCKIFY_AUTOPILOT_SLICE_TIMEOUT_SECONDS`, `CLOCKIFY_AUTOPILOT_TOTAL_TIMEOUT_SECONDS`, and `CLOCKIFY_AUTOPILOT_TERMINATE_GRACE_SECONDS`. The runner records a timeout failure event, exits retryably when debt permits, and releases the lock on every path.

- [ ] **Step 5: Add lock-release and completed-slice reuse tests**

Run the runner twice after a simulated timeout; assert the second obtains the lock and does not rerun a verified completed slice. Validate routine and backlog timeout settings independently.

- [ ] **Step 6: Run and commit**

Run: `python3 -m unittest tests.test_autopilot_process tests.test_autopilot_runner -v`

Expected: all timeout and runner tests pass.

```bash
git add scripts/autopilot_process.py scripts/clockify_autopilot_runner.py tests/test_autopilot_process.py tests/test_autopilot_runner.py ops/systemd/clockify-work-accounting.env.example ops/launchd/clockify-work-accounting.env.example
git commit -m "feat: bound Clockify autopilot child runtime"
```

---

### Task 6: Replay Binding and End-to-End Resilience

**Files:**
- Modify: `scripts/clockify_review_run.py:417-710,887-970`
- Modify: `scripts/clockify_portfolio_replay.py:85-140`
- Modify: `tests/test_review_run.py`
- Modify: `tests/test_portfolio_replay.py`
- Create: `tests/fixtures/reconciliation/routine-two-day/manifest.json`
- Create: `tests/fixtures/reconciliation/exceptional-backlog/manifest.json`
- Create: `tests/test_reconciliation_resilience.py`
- Modify: `README.md`
- Modify: `clockify-process-acceptance.md`

**Interfaces:**
- Produces `_reconciliation_binding(run_dir, *, period_manifest, routing, corrections, acceptance) -> dict[str, str]` and binds completion bundles into replay integrity.

- [ ] **Step 1: Write failing replay-drift tests**

```python
def test_replay_rejects_manifest_routing_corrections_or_acceptance_drift(self):
    source, replay = complete_replay_fixture()
    integrity = review_run._verify_replay_integrity(source, replay)
    self.assertEqual("pass", integrity["status"])
    for name in ("period-manifest.json", "routing.json", "review-corrections.jsonl", "review-acceptance.jsonl"):
        mutate(replay / name)
        with self.assertRaises(review_run.ReviewRunError):
            review_run._verify_replay_integrity(source, replay)
        restore(replay / name)
```

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m unittest tests.test_review_run tests.test_portfolio_replay -v`

Expected: new assertions fail because these identities are not explicitly bound.

- [ ] **Step 3: Extend replay identity and completion-bundle verification**

Bind period manifest, routing snapshot, correction log, acceptance input, canonical meeting reconciliation, and every slice bundle. Keep existing ledger/analyzer/cache/accounting checks unchanged.

- [ ] **Step 4: Add routine and exceptional fixtures**

The routine fixture contains one complete two-day slice. The exceptional fixture contains multiple slices, one transient source failure, independently retried debt, a reviewed Desktop limitation, and a completed-slice reuse after timeout. Use synthetic credential-free records only.

- [ ] **Step 5: Run focused and full suites**

Run: `python3 -m unittest tests.test_reconciliation_manifest tests.test_source_coverage tests.test_collector_receipts tests.test_collector_slices tests.test_autopilot_process tests.test_autopilot_runner tests.test_review_run tests.test_portfolio_replay tests.test_reconciliation_resilience -v`

Then run: `python3 -m unittest discover -s tests -v`

Expected: all tests pass with only documented skips.

- [ ] **Step 6: Document and commit**

Document period/event paths, exact debt semantics, timeout configuration, failure receipts, bundle retention, replay bindings, and publication deferral. Do not expose private paths or evidence.

```bash
git add scripts/clockify_review_run.py scripts/clockify_portfolio_replay.py tests/test_review_run.py tests/test_portfolio_replay.py tests/fixtures/reconciliation tests/test_reconciliation_resilience.py README.md clockify-process-acceptance.md
git commit -m "feat: bind reconciliation resilience to replay"
```
