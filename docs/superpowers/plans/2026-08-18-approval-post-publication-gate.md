# Approval, Posting, and Publication Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Require exact approval, append-only Clockify posting evidence, full-period readback, audited currency conversion, and a fail-closed publication contract before any shared-report or Slack correction.

**Architecture:** Repository-owned pure gates validate approval, post/readback, native currency, FX, and period-manifest evidence. The existing external reporter remains a thin adapter governed by a digest-bound `publication_authorized` contract and one idempotent report-then-Slack operation bundle.

**Tech Stack:** Python 3 standard library, dataclasses, `datetime`, `decimal.Decimal`, JSON/JSONL, `urllib`, typing protocols, `unittest`, JSON Schema.

**Spec:** `docs/superpowers/specs/2026-08-17-clockify-reconciliation-publication-manifest-design.md`

## Global Constraints

- Apply DRY, KISS, and YAGNI; repository code owns contracts and gates, not the external reporter scheduler.
- Work only in `/home/blackthorne/Work/automation-clockify-sync-task-f8ea741c`; preserve unrelated and private artifacts.
- No Clockify, shared-report, Slack, Sheets, schedule, push, merge, deployment, credential, or permission mutation occurs without exact separate approval.
- Clockify posting approval and publication approval are separate artifacts unless one human approval explicitly names both exact operations and digests.
- Publication authorization covers one idempotent report-then-Slack bundle through expiry; report success plus Slack failure retries Slack only under the same identity.
- Native ISO-4217 currency buckets are preserved. USD-equivalent conversion is reporting metadata and never alters Clockify native costs.
- Default FX source is the ECB daily EUR-base reference feed; quote date is publication date or latest prior business day, no more than four calendar days old.
- Convert with `Decimal`, round each bucket half-up to USD cents, then sum rounded USD buckets.
- The reporter never infers readiness from runner exit code or current Clockify totals.
- Shared-report readback must pass before any Slack call.
- Use strict TDD; external integrations are represented by complete protocol fakes in tests.

---

### Task 1: Approval and Append-Only Posting Receipt Contracts

**Files:**
- Create: `schemas/approval-receipt-v1.json`
- Create: `schemas/post-event-v1.json`
- Create: `scripts/posting_receipts.py`
- Create: `tests/test_posting_receipts.py`

**Interfaces:**
- Produces `ApprovalReceipt`, `ApprovalReceiptStore`, `PostEvent`, and `PostEventStore`.

```python
class ApprovalReceiptStore:
    def append(self, receipt: ApprovalReceipt) -> None: ...
    def require(self, receipt_id: str, *, operation_identity: str, now: datetime) -> ApprovalReceipt: ...
    def consume(self, receipt_id: str, *, operation_identity: str, consumed_at: str) -> None: ...
    def verify(self) -> None: ...

class PostEventStore:
    def append(self, event: PostEvent) -> None: ...
    def verify(self) -> tuple[PostEvent, ...]: ...
    def derive_receipt(self, operation_identity: str) -> dict[str, Any]: ...
```

- [ ] **Step 1: Write failing approval-scope tests**

```python
def test_approval_is_bound_to_target_period_operation_and_artifact_digests(self):
    receipt = approval_receipt(
        operation="clockify_post", period_id="period-1",
        workspace_id="workspace-1", member_id="member-1",
        portfolio_digest="sha256:p", quality_digest="sha256:q",
        replay_digest="sha256:r", routing_digest="sha256:t",
    )
    store.append(receipt)
    self.assertEqual(receipt, store.require(receipt.approval_id, operation_identity=receipt.operation_identity, now=NOW))
    with self.assertRaises(PostingReceiptError):
        store.require(receipt.approval_id, operation_identity="different", now=NOW)
```

- [ ] **Step 2: Write failing post-chain tests**

```python
def test_post_history_rejects_reorder_truncation_and_duplicate_terminal_event(self):
    store.append(post_event("planned", review_id="r1", segment_index=0))
    store.append(post_event("created", review_id="r1", segment_index=0, clockify_entry_id="entry-1"))
    self.assertEqual("created", store.derive_receipt(OP)["entries"][0]["disposition"])
    reorder_log(store.path)
    with self.assertRaises(PostingReceiptError):
        store.verify()
```

- [ ] **Step 3: Run and verify RED**

Run: `python3 -m unittest tests.test_posting_receipts -v`

Expected: import failure because the module does not exist.

- [ ] **Step 4: Implement canonical approval and post event chains**

Use canonical JSON, contiguous sequence, previous digest, event digest, period/operation identity, and append/fsync semantics. Approval validation checks approver/timestamps, expiry, target, period, operation, every artifact digest, residual exception digest, and consumption status.

- [ ] **Step 5: Add expiry, wrong-target, drift, and allowed disposition tests**

Only `created`, `already_existing`, `recovered_after_ambiguous_response`, and `interrupted` are valid terminal dispositions. Every created/recovered event requires a Clockify entry ID and live-readback digest.

- [ ] **Step 6: Run and commit**

Run: `python3 -m unittest tests.test_posting_receipts -v`

Expected: all receipt tests pass.

```bash
git add schemas/approval-receipt-v1.json schemas/post-event-v1.json scripts/posting_receipts.py tests/test_posting_receipts.py
git commit -m "feat: add approval and posting receipt chains"
```

---

### Task 2: Approval-Bound Clockify Poster and Final Receipt

**Files:**
- Modify: `scripts/clockify_post_approved_portfolio.py:413-679`
- Modify: `tests/test_clockify_portfolio_post.py`
- Modify: `README.md`

**Interfaces:**
- Consumes `ApprovalReceiptStore` and `PostEventStore`.
- Extends poster CLI with `--approval-receipt`, `--approval-events`, `--post-events`, and `--period-manifest`.
- Produces a derived compatibility receipt plus append-only events and a final full-period live-readback digest.

- [ ] **Step 1: Write failing poster gate tests**

```python
def test_execute_requires_exact_unexpired_approval_receipt(self):
    args = post_args(execute=True, approval_receipt=None)
    with self.assertRaisesRegex(post.PortfolioPostError, "approval receipt"):
        post.run(args)

def test_wrong_period_or_coverage_digest_is_rejected_before_network(self):
    args = post_args(execute=True, approval_receipt=wrong_period_approval())
    with mock.patch.object(post, "_request") as request:
        with self.assertRaises(post.PortfolioPostError):
            post.run(args)
    request.assert_not_called()
```

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m unittest tests.test_clockify_portfolio_post -v`

Expected: new assertions fail because expected SHA and `--execute` are currently the only external guard.

- [ ] **Step 3: Bind approval before credential loading or network**

Verify portfolio, quality, replay, routing, correction log, coverage, target, period, and operation digests. Keep the existing dry-run behavior, exact duplicate detection, overlap rejection, subminute alignment, and ambiguous-POST readback.

- [ ] **Step 4: Replace mutable execution updates with post events**

Append `planned` before POST; append one terminal disposition after exact readback. On process interruption, existing planned events derive `interrupted`. The old receipt path becomes a derived read-only compatibility output and is never trusted as source history.

- [ ] **Step 5: Add interrupted resume and ambiguous POST tests**

Assert resume does not repeat `created`/`already_existing`; an ambiguous 5xx result performs exact live readback and yields `recovered_after_ambiguous_response` or `interrupted`, never a blind second POST.

- [ ] **Step 6: Run and commit**

Run: `python3 -m unittest tests.test_clockify_portfolio_post tests.test_posting_receipts -v`

Expected: all posting tests pass.

```bash
git add scripts/clockify_post_approved_portfolio.py tests/test_clockify_portfolio_post.py README.md
git commit -m "feat: bind Clockify posting to approval receipts"
```

---

### Task 3: Full-Period Clockify Readback Contract

**Files:**
- Create: `schemas/clockify-period-readback-v1.json`
- Create: `scripts/clockify_period_readback.py`
- Create: `tests/test_clockify_readback.py`

**Interfaces:**
- Produces `ClockifyReadbackError`, `ClockifyPeriodReadback`, `ClockifyReadbackGateway` protocol, `normalize_readback()`, and `verify_readback()`.
- Produces read-only CLI subcommands `capture` and `reconcile` with the exact routing, period, shared-report, API-output, report-output, and reconciliation-output arguments used by the August recovery plan.

```python
class ClockifyReadbackGateway(Protocol):
    def read_period(
        self, *, workspace_id: str, member_id: str,
        start: datetime, end: datetime, filters: Mapping[str, Any],
    ) -> ClockifyPeriodReadback: ...
```

- [ ] **Step 1: Write failing exact-period and residual tests**

```python
def test_readback_binds_entries_duration_filters_and_native_currency_buckets(self):
    result = normalize_readback(API_FIXTURE)
    self.assertEqual(172, result.entry_count)
    self.assertEqual(132217, result.duration_seconds)
    self.assertEqual({"USD": Decimal("983.70"), "EUR": Decimal("7.31")}, result.native_costs)

def test_ten_minute_report_difference_is_a_blocker_not_rounding(self):
    with self.assertRaisesRegex(ClockifyReadbackError, "duration mismatch: 600 seconds"):
        verify_readback(API_36_43_37, SHARED_REPORT_36_33_37)
```

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m unittest tests.test_clockify_readback -v`

Expected: import failure because the module does not exist.

- [ ] **Step 3: Implement read-only normalization and exact verification**

Bind workspace/member, Europe/Bucharest half-open boundaries, filters, refresh timestamp, running/deleted inclusion, entry IDs/count, exact seconds, native currency buckets, and canonical digest. Never collapse currencies or infer a ten-minute difference as rounding.

- [ ] **Step 4: Add wrong-filter/member/period and stale-refresh tests**

Test August 16 exclusion, a mismatched user, missing native currency code, stale report refresh, and post receipt whose created/existing IDs do not appear in the final ledger.

Add process-level CLI tests proving `capture` uses only gateway reads, `reconcile` identifies exact entry/filter deltas, an unexplained difference remains a blocker, and neither command exposes credentials or mutates Clockify.

- [ ] **Step 5: Run and commit**

Run: `python3 -m unittest tests.test_clockify_readback -v`

Expected: all readback tests pass.

```bash
git add schemas/clockify-period-readback-v1.json scripts/clockify_period_readback.py tests/test_clockify_readback.py
git commit -m "feat: verify full Clockify period readback"
```

---

### Task 4: Native Currency and ECB FX Receipt

**Files:**
- Create: `schemas/fx-quote-receipt-v1.json`
- Create: `scripts/clockify_currency.py`
- Create: `tests/test_clockify_currency.py`

**Interfaces:**
- Produces `CurrencyContractError`, `FxQuoteReceipt`, `CurrencySummary`, `parse_ecb_quote()`, and `convert_native_buckets()`.
- Produces a read-only `fetch-ecb` CLI that accepts `--publication-date-from-clock Europe/Bucharest` and `--output`, writes the provider/effective-date/rates/payload-digest receipt, and rejects quotes older than four calendar days. Tests inject the clock; production uses the actual invocation date in the named timezone.

- [ ] **Step 1: Write failing conversion and freshness tests**

```python
def test_native_buckets_are_preserved_and_summed_as_rounded_usd(self):
    quote = FxQuoteReceipt(
        provider="ECB", effective_date=date(2026, 8, 17), fetched_at=NOW,
        base_currency="EUR", rates={"USD": Decimal("1.1000")}, payload_digest="sha256:q",
    )
    result = convert_native_buckets(
        {"USD": Decimal("983.70"), "EUR": Decimal("7.31")},
        quote, publication_date=date(2026, 8, 18),
    )
    self.assertEqual({"USD": Decimal("983.70"), "EUR": Decimal("7.31")}, result.native_buckets)
    self.assertEqual(Decimal("8.04"), result.usd_buckets["EUR"])
    self.assertEqual(Decimal("991.74"), result.usd_equivalent_total)

def test_quote_older_than_four_calendar_days_is_rejected(self):
    with self.assertRaisesRegex(CurrencyContractError, "stale"):
        convert_native_buckets({"EUR": Decimal("10")}, OLD_QUOTE, publication_date=date(2026, 8, 18))
```

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m unittest tests.test_clockify_currency -v`

Expected: import failure because the currency module does not exist.

- [ ] **Step 3: Implement ECB cross-rates and Decimal rounding**

```python
CENT = Decimal("0.01")

def _to_usd(amount: Decimal, currency: str, quote: FxQuoteReceipt) -> Decimal:
    if currency == "USD":
        raw = amount
    elif currency == "EUR":
        raw = amount * quote.rates["USD"]
    else:
        raw = amount / quote.rates[currency] * quote.rates["USD"]
    return raw.quantize(CENT, rounding=ROUND_HALF_UP)
```

Validate ISO uppercase codes, nonnegative finite decimals, ECB provider/base, payload digest, effective date, max age, required rates, and recomputed total.

- [ ] **Step 4: Add weekend, unknown currency, missing rate, and arithmetic-drift tests**

Use latest prior business-day quote within four days; reject float inputs and any serialized summary whose recomputed bucket/total differs.

Add a process-level `fetch-ecb` test with an injected HTTP fixture. Assert it performs one GET, selects the latest eligible ECB quote, writes a schema-valid receipt, and exposes no payload or credential in status output.

- [ ] **Step 5: Run and commit**

Run: `python3 -m unittest tests.test_clockify_currency -v`

Expected: all currency tests pass.

```bash
git add schemas/fx-quote-receipt-v1.json scripts/clockify_currency.py tests/test_clockify_currency.py
git commit -m "feat: add audited USD currency summary"
```

---

### Task 5: Publication Prepared and Authorized Contracts

**Files:**
- Create: `schemas/publication-contract-v1.json`
- Create: `scripts/clockify_publication_gate.py`
- Create: `tests/test_publication_gate.py`
- Modify: `scripts/reconciliation_manifest.py`
- Modify: `tests/test_reconciliation_manifest.py`

**Interfaces:**
- Produces `PublicationGateError`, `PublicationContract`, `AuthorizedPublication`, `prepare_publication()`, `authorize_publication()`, and `publication_idempotency_key()`.
- Produces CLI subcommands `prepare` and `authorize`. `prepare` accepts manifest/post/readback/FX artifacts and writes only `publication_prepared`; `authorize` accepts that contract plus the exact publication approval and writes only `publication_authorized`.

- [ ] **Step 1: Write failing prepared-gate tests**

```python
def test_prepare_requires_verified_manifest_post_readback_and_fx(self):
    contract = prepare_publication(
        READY_MANIFEST,
        post_receipt=COMPLETE_POST_RECEIPT,
        clockify_readback=MATCHING_READBACK,
        currency_summary=VALID_CURRENCY,
    )
    self.assertEqual("publication_prepared", contract.state)
    self.assertEqual(
        "finance-report:workspace-1:member-1:2026-08-01:2026-08-16:1",
        contract.idempotency_key,
    )

def test_any_coverage_exception_without_explicit_approval_blocks_prepare(self):
    with self.assertRaisesRegex(PublicationGateError, "coverage"):
        prepare_publication(MANIFEST_WITH_UNAPPROVED_DESKTOP, **VALID_INPUTS)
```

- [ ] **Step 2: Write failing authorization-scope tests**

```python
def test_authorization_names_exact_report_and_slack_bundle(self):
    authorized = authorize_publication(PREPARED, PUBLICATION_APPROVAL, now=NOW)
    self.assertEqual("publication_authorized", authorized.state)
    self.assertEqual(PREPARED.contract_digest, authorized.contract_digest)
    self.assertEqual(("shared_report_update", "slack_correction"), authorized.operations)
```

- [ ] **Step 3: Run and verify RED**

Run: `python3 -m unittest tests.test_publication_gate -v`

Expected: import failure because the gate does not exist.

- [ ] **Step 4: Implement fail-closed gates**

`prepare_publication()` requires exact period/target, verified slice bundles, complete or explicitly approved coverage, complete canonical meeting accounting, no unresolved semantic/routing/overlap/billability exception, quality pass, replay pass, valid Clockify approval/post/readback, and valid FX summary. `authorize_publication()` requires a separate unexpired approval naming the exact report mutation, Slack correction, contract digest, revision, and idempotency key.

- [ ] **Step 5: Add every blocker and state-transition test**

Cover stale runner state, missing bundle, artifact drift, incomplete Calendly, unapproved Desktop limitation, quality/replay failure, expired approval, readback mismatch, stale FX, and wrong publication target. Assert no authorized contract is emitted.

Add process-level tests for the documented `prepare` and `authorize` arguments. A failed gate must leave an existing output unchanged and make no adapter call.

- [ ] **Step 6: Run and commit**

Run: `python3 -m unittest tests.test_publication_gate tests.test_reconciliation_manifest tests.test_clockify_readback tests.test_clockify_currency -v`

Expected: all gate tests pass.

```bash
git add schemas/publication-contract-v1.json scripts/clockify_publication_gate.py scripts/reconciliation_manifest.py tests/test_publication_gate.py tests/test_reconciliation_manifest.py
git commit -m "feat: gate Clockify finance publication"
```

---

### Task 6: External Adapter Contract and Idempotent Partial Retry

**Files:**
- Create: `schemas/publication-receipt-v1.json`
- Create: `scripts/publication_adapter_contract.py`
- Create: `scripts/clockify_finance_report_adapter.py`
- Create: `tests/test_publication_adapter_contract.py`
- Create: `tests/test_clockify_finance_report_adapter.py`
- Create: `ops/systemd/clockify-finance-report.service`
- Create: `ops/systemd/clockify-finance-report.timer`
- Create: `ops/systemd/clockify-finance-report.env.example`
- Create: `ops/launchd/com.serenichron.clockify-finance-report.plist`
- Create: `ops/launchd/clockify-finance-report.env.example`

**Interfaces:**
- Produces `PublicationAdapterError`, `PublicationAdapter` protocol, `SharedReportReceipt`, `SlackReceipt`, `PublicationReceipt`, `PublicationReceiptStore`, and `execute_authorized_publication(authorized, adapter, receipt_store, *, now) -> PublicationReceipt`.
- Produces the repository-owned scheduled entry point `scripts/clockify_finance_report_adapter.py execute --period-manifest ... --events ... --authorized ... --receipts ...` plus systemd timer/service and launchd definitions whose only executable report path is that entry point. Installing/enabling them and disabling any legacy schedule is a separately guarded operational action.

- [ ] **Step 1: Write failing ordering/idempotency tests**

```python
def test_report_readback_passes_before_slack_call(self):
    adapter = RecordingAdapter(report_readback=MATCHING_REPORT)
    receipt = execute_authorized_publication(AUTHORIZED, adapter, store, now=NOW)
    self.assertEqual(["update_report", "read_report", "upsert_slack"], adapter.calls)
    self.assertEqual("published", receipt.state)

def test_report_mismatch_makes_no_slack_call(self):
    adapter = RecordingAdapter(report_readback=TEN_MINUTES_LOW)
    with self.assertRaises(PublicationAdapterError):
        execute_authorized_publication(AUTHORIZED, adapter, store, now=NOW)
    self.assertEqual(["update_report", "read_report"], adapter.calls)

def test_report_success_slack_failure_retries_only_slack(self):
    adapter = FailSlackOnceAdapter(report_readback=MATCHING_REPORT)
    with self.assertRaises(PublicationAdapterError):
        execute_authorized_publication(AUTHORIZED, adapter, store, now=NOW)
    execute_authorized_publication(AUTHORIZED, adapter, store, now=LATER)
    self.assertEqual(1, adapter.update_report_count)
    self.assertEqual(2, adapter.slack_count)
```

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m unittest tests.test_publication_adapter_contract -v`

Expected: import failure because the contract module does not exist.

- [ ] **Step 3: Implement protocol-only orchestration and receipt verification**

Repository code calls only injected protocol methods. Persist report receipt before Slack. On retry, verify the same contract/approval/idempotency key and reuse the report receipt; target/content/revision drift invalidates authorization.

- [ ] **Step 4: Add revision retention and unready no-op tests**

Assert same identity never creates a second Slack publication, a new revision retains the prior receipt, and a non-authorized contract invokes no adapter method.

- [ ] **Step 5: Gate the scheduled reporter entry point**

Implement the executable adapter so missing, deferred, prepared-only, expired, or digest-drifted input appends a `publication_deferred` coordinator event through `--events`, refreshes the derived `--period-manifest`, exits with the same machine-readable result, and invokes neither report nor Slack. Only a verified `publication_authorized` contract may construct the external gateway, which then executes report-update, report-readback, and Slack-upsert in order. After exact report readback, append `shared_report_verified` bound to its receipt; after exact Slack readback, append `publication_complete` bound to both receipts and the same contract/idempotency identity, then re-derive `published`.

Add process-level tests that run the exact scheduled command with protocol fakes at a fixed 09:00 Europe/Bucharest timestamp. Assert every unready state makes zero external calls and derives the expected `publication_deferred` blocker, report mismatch makes zero Slack calls, and report-success/Slack-failure retry invokes only Slack on the second run. On full success, require `shared_report_verified` followed by `publication_complete`, with both event artifacts matching the persisted report/Slack receipts, contract digest, and idempotency identity, and require the coordinator to derive `published`. Parse both the systemd and launchd definitions and assert their scheduled executable resolves to `scripts/clockify_finance_report_adapter.py` with manifest/event/authorization/receipt inputs; no unit may invoke report or Slack transport directly.

- [ ] **Step 6: Run and commit**

Run: `python3 -m unittest tests.test_publication_adapter_contract tests.test_clockify_finance_report_adapter -v`

Expected: all adapter contract tests pass without external calls.

```bash
git add schemas/publication-receipt-v1.json scripts/publication_adapter_contract.py scripts/clockify_finance_report_adapter.py tests/test_publication_adapter_contract.py tests/test_clockify_finance_report_adapter.py ops/systemd/clockify-finance-report.service ops/systemd/clockify-finance-report.timer ops/systemd/clockify-finance-report.env.example ops/launchd/com.serenichron.clockify-finance-report.plist ops/launchd/clockify-finance-report.env.example
git commit -m "feat: define idempotent finance reporter adapter"
```

---

### Task 7: Publication End-to-End Fixtures and Operations

**Files:**
- Create: `tests/fixtures/reconciliation/publication-routine/manifest.json`
- Create: `tests/fixtures/reconciliation/publication-backlog/manifest.json`
- Create: `tests/test_publication_end_to_end.py`
- Modify: `README.md`
- Modify: `clockify-process-acceptance.md`
- Modify: `multica-clockify-autopilot-prompt.md`

**Interfaces:**
- Consumes all contracts from Tasks 1-6.
- Produces an end-to-end synthetic proof that report publication is impossible before full readiness and idempotent after authorization.

- [ ] **Step 1: Write failing routine/backlog acceptance tests**

```python
def test_routine_two_day_period_publishes_after_exact_receipts(self):
    result = run_fixture("publication-routine")
    self.assertEqual("published", result.state)
    self.assertEqual(1, result.adapter.slack_count)

def test_exceptional_backlog_defers_until_all_slices_and_limitations_are_approved(self):
    fixture = load_fixture("publication-backlog")
    self.assertEqual("publication_deferred", run_until_coverage_complete(fixture).state)
    final = run_after_approved_desktop_limitation(fixture)
    self.assertEqual("published", final.state)
    self.assertEqual({"USD", "EUR"}, set(final.currency.native_buckets))
```

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m unittest tests.test_publication_end_to_end -v`

Expected: failures until all prior contracts integrate.

- [ ] **Step 3: Complete fixtures and documentation**

The backlog fixture includes a Calendly-only recording, one Fathom/Calendly duplicate, transient coverage debt, approved Desktop limitation, ambiguous POST recovery, USD/EUR buckets, report-success/Slack-failure retry, and retained correction revision. Document contract paths, state meanings, ECB quote rules, adapter ordering, idempotency, and explicit approvals.

- [ ] **Step 4: Run focused and full suites**

Run: `python3 -m unittest tests.test_posting_receipts tests.test_clockify_portfolio_post tests.test_clockify_readback tests.test_clockify_currency tests.test_publication_gate tests.test_publication_adapter_contract tests.test_clockify_finance_report_adapter tests.test_publication_end_to_end -v`

Then run: `python3 -m unittest discover -s tests -v`

Expected: all tests pass with only documented skips.

- [ ] **Step 5: Commit**

```bash
git add tests/fixtures/reconciliation/publication-routine tests/fixtures/reconciliation/publication-backlog tests/test_publication_end_to_end.py README.md clockify-process-acceptance.md multica-clockify-autopilot-prompt.md
git commit -m "test: prove fail-closed finance publication"
```
