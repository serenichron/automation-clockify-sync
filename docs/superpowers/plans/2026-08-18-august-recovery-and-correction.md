# August 2026 Recovery and Finance Correction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reconcile every recoverable evidence-supported Vlad activity for August 1-15, 2026, post the exact approved missing entries once, and publish a verified corrected finance report without hiding unavailable-device limitations.

**Architecture:** Execute the approved repository contracts from the preceding Calendly, manifest/resilience, and publication plans against one private period directory. Reuse preserved Clockify/Fathom/device/model artifacts by digest, perform only bounded missing-partition inference, and stop at explicit approval gates before Clockify, shared-report, or Slack mutations.

**Tech Stack:** Existing Python reconciliation CLI, new period/calendar/publication CLIs from the companion plans, JSON/JSONL receipts, SHA-256, Clockify/Fathom/Calendly read-only APIs, approved DeepSeek V4 Flash cloud revision.

**Spec:** `docs/superpowers/specs/2026-08-17-clockify-reconciliation-publication-manifest-design.md`

## Prerequisites

Implement and integrate the companion plans in this exact order because they share replay, accounting, runner, and operations files:

1. `docs/superpowers/plans/2026-08-18-calendly-meeting-reconciliation.md`
2. `docs/superpowers/plans/2026-08-18-period-manifest-resilience.md`
3. `docs/superpowers/plans/2026-08-18-approval-post-publication-gate.md`

After plan 3, run the union of all three focused suites and `python3 -m unittest discover -s tests -v` from the combined HEAD. Begin private August recovery only after this shared-file regression gate passes.

## Global Constraints

- Work only in `/home/blackthorne/Work/automation-clockify-sync-task-f8ea741c` and the preserved private Precision cache/runtime roots already authorized for this task.
- Private source records, transcripts, paths, credentials, entry IDs, cursors, and receipts stay untracked and out of messages/commits.
- The period is half-open Europe/Bucharest `[2026-08-01T00:00:00+03:00, 2026-08-16T00:00:00+03:00)`, Vlad, correction revision 1.
- Preserve all sealed analyzer/portfolio caches. Submit only missing content-addressed partitions to the latest approved DeepSeek V4 Flash cloud revision; DeepSeek Pro is prohibited.
- Calendly scheduled events without recordings are not billed.
- Desktop absence remains an explicit coverage limitation; never invent Desktop time.
- MacBook/Precision evidence already proven complete through August 14 is reused, not recollected blindly.
- No guessed ten-minute adjustment is allowed; identify exact entries or report filters.
- No Clockify creation occurs until an exact clean packet receives a new digest-bound approval.
- No shared-report or Slack mutation occurs until a separate exact publication approval names both operations, the contract digest, and idempotency identity.
- Native currency buckets remain visible; ECB receipt-bound USD conversions are summed into the USD-equivalent total.
- Every external mutation is followed by readback and an append-only receipt.

## Private Working Layout

Use this untracked root for all recovery outputs:

```text
/home/blackthorne/Work/automation-clockify-sync-task-f8ea741c/state/recovery/august-2026-vlad/
  period-events.jsonl
  period-manifest.json
  imported-artifacts.json
  collection/
  meeting-reconciliation.json
  review/
  approval/
  posting/
  readback/
  publication/
```

---

### Task 0: Implement and Verify Recovery-Specific CLI Contracts

**Files:**
- Modify: `scripts/clockify_portfolio_repair.py`
- Modify: `tests/test_portfolio_repair.py`
- Modify: `tests/test_process_integration.py`
- Modify: `README.md`

**Interfaces:**
- Extends `clockify_portfolio_repair.py` with `--build-recovery-manifest`, `--period-manifest`, repeatable `--include-failure-class`, `--include-overlap-residuals`, `--output`, `--no-inference`, `--recovery-manifest`, `--analyzer-cache`, and `--model`, while preserving the existing positional repair interface.
- Uses `scripts/analyzer_live_evaluation.py --tier primary --capture-output ... --scorecard-output ...` as the credential-free synthetic route probe. The scorecard must pass and the resolved route must be exactly `deepseek-v4-flash:cloud` at the approved revision before private repair.

- [ ] **Step 1: Write failing recovery-manifest CLI tests**

Test that `--build-recovery-manifest --no-inference` selects only the requested structural-failure, routing-exception, and overlap identities from synthetic sealed artifacts; accepted cache members must be absent and no analyzer transport may be constructed.

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m unittest tests.test_portfolio_repair.PortfolioRecoveryCliTests -v`

Expected: parser failure because the recovery-specific arguments are absent.

- [ ] **Step 3: Implement deterministic manifest selection and bounded execution**

The manifest records each unresolved content digest, failure class, route/overlap status, approved model route/revision, and sealed cache identity. Execution accepts only identities in that manifest, rejects an accepted cache member or model mismatch before transport creation, writes one result per content digest, and keeps unresolved validator failures as explicit exceptions.

- [ ] **Step 4: Add route-probe and cache-preservation process tests**

Run the exact synthetic `analyzer_live_evaluation.py` command with a fake transport and require a passing scorecard for `deepseek-v4-flash:cloud`. Then run one recovery manifest containing a cache hit and one missing digest; assert the cache hit makes no request, the missing digest makes at most one request, and DeepSeek Pro/revision drift fails before private input is loaded.

- [ ] **Step 5: Run focused tests and commit**

Run: `python3 -m unittest tests.test_portfolio_repair tests.test_analyzer_evaluation tests.test_process_integration -v`

Expected: all selected tests pass.

```bash
git add scripts/clockify_portfolio_repair.py tests/test_portfolio_repair.py tests/test_process_integration.py README.md
git commit -m "feat: add bounded August repair manifest"
```

---

### Task 1: Initialize the Period and Import Preserved Evidence by Digest

**Files:**
- Read: `reports/vlad-2026-08-01-through-15-diagnostic/artifact.json`
- Read: preserved private August run artifacts under the authorized target/runtime roots
- Create untracked: `state/recovery/august-2026-vlad/imported-artifacts.json`
- Create untracked: `state/recovery/august-2026-vlad/period-events.jsonl`
- Create untracked: `state/recovery/august-2026-vlad/period-manifest.json`

**Interfaces:**
- Consumes `PeriodIdentity` and `CoordinatorEventStore`.
- Produces a `collecting` revision-1 manifest containing only safe artifact references/digests.

- [ ] **Step 1: Run a dry-run period initialization**

```bash
python3 scripts/reconciliation_manifest.py init \
  --workspace-id-from-routing routing.json \
  --member-id-from-routing routing.json \
  --timezone Europe/Bucharest \
  --since 2026-08-01T00:00:00+03:00 \
  --until 2026-08-16T00:00:00+03:00 \
  --revision 1 \
  --events state/recovery/august-2026-vlad/period-events.jsonl \
  --manifest state/recovery/august-2026-vlad/period-manifest.json \
  --dry-run
```

Expected: exit 0; reports one deterministic period ID and no writes.

- [ ] **Step 2: Initialize the private period**

Run the same command without `--dry-run` only after checking the resolved output paths remain under the private recovery root.

Expected: manifest state `collecting`; no raw evidence fields.

- [ ] **Step 3: Build and verify the import inventory**

```bash
python3 scripts/reconciliation_manifest.py import-artifacts \
  --events state/recovery/august-2026-vlad/period-events.jsonl \
  --manifest state/recovery/august-2026-vlad/period-manifest.json \
  --diagnostic reports/vlad-2026-08-01-through-15-diagnostic/artifact.json \
  --discover-preserved-august \
  --output state/recovery/august-2026-vlad/imported-artifacts.json
```

Expected: safe kinds/counts/digests for the pre-posting ledger, approved prior posting, post-write API ledger, source ledgers, review/repair/replay artifacts, and model caches; no raw source text or credentials.

- [ ] **Step 4: Verify invariants**

```bash
python3 scripts/reconciliation_manifest.py verify \
  --events state/recovery/august-2026-vlad/period-events.jsonl \
  --manifest state/recovery/august-2026-vlad/period-manifest.json
```

Expected: event chain valid, artifact digests match, state remains `collecting`.

No commit: all outputs are private recovery state.

---

### Task 2: Resolve the Exact Ten-Minute Clockify/Shared-Report Residual

**Files:**
- Create untracked: `state/recovery/august-2026-vlad/readback/clockify-api-before.json`
- Create untracked: `state/recovery/august-2026-vlad/readback/shared-report-before.json`
- Create untracked: `state/recovery/august-2026-vlad/readback/ten-minute-reconciliation.json`

**Interfaces:**
- Consumes the read-only `ClockifyReadbackGateway`.
- Produces an entry/filter-level explanation whose exact duration delta is 600 seconds or proves the current residual has changed.

- [ ] **Step 1: Read the current full-period API ledger and shared report**

```bash
python3 scripts/clockify_period_readback.py capture \
  --routing routing.json \
  --timezone Europe/Bucharest \
  --since 2026-08-01T00:00:00+03:00 \
  --until 2026-08-16T00:00:00+03:00 \
  --shared-report-id 6a81522d2934a7581b4c65aa \
  --api-output state/recovery/august-2026-vlad/readback/clockify-api-before.json \
  --report-output state/recovery/august-2026-vlad/readback/shared-report-before.json
```

Expected: read-only call; exact member/workspace/filters/refresh, entry count/IDs/duration, and native currency buckets are captured privately.

- [ ] **Step 2: Produce exact reconciliation**

```bash
python3 scripts/clockify_period_readback.py reconcile \
  --api state/recovery/august-2026-vlad/readback/clockify-api-before.json \
  --shared-report state/recovery/august-2026-vlad/readback/shared-report-before.json \
  --output state/recovery/august-2026-vlad/readback/ten-minute-reconciliation.json
```

Expected: identifies exact excluded/included entry IDs or an exact filter/refresh/running/deleted-entry cause. If it only reports an unexplained 600-second mismatch, keep `readback_mismatch` and do not advance.

- [ ] **Step 3: Append the verified explanation**

Append `report_residual_resolved` only when the artifact states a deterministic cause and its recomputed delta matches both readbacks. Otherwise append `readback_mismatch` and continue safe recovery work without publication.

- [ ] **Step 4: Verify no mutation occurred**

Re-read the before API digest and confirm capture/reconcile commands made no Clockify changes.

No commit: outputs contain private entry/filter evidence.

---

### Task 3: Repair Only the Missing Fathom Partitions

**Files:**
- Read: preserved August analyzer/portfolio caches
- Create untracked: `state/recovery/august-2026-vlad/review/fathom-repair-manifest.json`
- Create untracked: `state/recovery/august-2026-vlad/review/fathom-repair.json`

**Interfaces:**
- Consumes sealed failure/cache identities.
- Produces decisions for exactly nine structurally failed meetings, two routing exceptions, and sixteen uncovered overlap minutes without rerunning accepted month-scale decisions.

- [ ] **Step 1: Build a no-inference repair manifest**

```bash
python3 scripts/clockify_portfolio_repair.py \
  --build-recovery-manifest \
  --period-manifest state/recovery/august-2026-vlad/period-manifest.json \
  --include-failure-class reviewer_structural_exhaustion \
  --include-failure-class unresolved_project_route \
  --include-overlap-residuals \
  --output state/recovery/august-2026-vlad/review/fathom-repair-manifest.json \
  --no-inference
```

Expected: exactly nine structurally failed meeting partitions, two distinct project-route exceptions, and the sixteen-minute overlap residual set; accepted cached decisions are absent.

- [ ] **Step 2: Verify the model route before private inference**

```bash
python3 scripts/analyzer_live_evaluation.py \
  --tier primary \
  --capture-output state/recovery/august-2026-vlad/review/flash-route-capture.json \
  --scorecard-output state/recovery/august-2026-vlad/review/flash-route-scorecard.json
```

Require a passing scorecard for the latest approved `deepseek-v4-flash:cloud` revision. Reject DeepSeek Pro, revision drift, failed evaluation, or a manifest that includes accepted cache members.

- [ ] **Step 3: Execute one bounded repair pass**

```bash
python3 scripts/clockify_portfolio_repair.py \
  --recovery-manifest state/recovery/august-2026-vlad/review/fathom-repair-manifest.json \
  --analyzer-cache state/precision-analyzer-cache-v2.jsonl \
  --model deepseek-v4-flash:cloud \
  --output state/recovery/august-2026-vlad/review/fathom-repair.json
```

Expected: one content-addressed request per unresolved partition at most; output retains explicit exceptions for anything not validated.

- [ ] **Step 4: Verify duration and overlap accounting**

Require the nine structurally failed recordings plus the two separately routed fixed-duration recordings to conserve the preserved 13:00 review total, and uncovered overlap tails to total exactly 0:16 before later Calendly/device additions. Existing covered intervals must not be added again, and the two routing exceptions must not be mislabeled as structural inference failures.

- [ ] **Step 5: Append repair evidence**

Append `fathom_repair_complete` only if every repaired row is Flash-validated and route/overlap checks pass; otherwise append `semantic_exceptions` and route remaining items to the human review packet.

No commit: private model inputs/outputs remain untracked.

---

### Task 4: Configure and Reconcile Calendly Recordings

**Files:**
- Create untracked: `state/recovery/august-2026-vlad/collection/calendly-recordings.json`
- Create untracked: `state/recovery/august-2026-vlad/meeting-reconciliation.json`

**Interfaces:**
- Consumes the Calendly collector and `meeting-dedup/v1`.
- Produces complete Calendly recording inventory, scheduled-without-recording exclusions, and canonical Fathom/Calendly meetings.

- [ ] **Step 1: Run the capability preflight without exposing credentials**

```bash
python3 scripts/calendly_collector.py preflight \
  --since 2026-08-01T00:00:00+03:00 \
  --until 2026-08-16T00:00:00+03:00 \
  --output state/recovery/august-2026-vlad/collection/calendly-preflight.json
```

Expected: proves stable recording identities, exact recording windows/duration, participant eligibility, semantics, pagination, and completeness. If credentials/permissions or recording fields are unavailable, stop this branch and report the exact capability gap; do not substitute scheduled duration.

- [ ] **Step 2: Collect the complete interval read-only**

```bash
python3 scripts/calendly_collector.py collect \
  --since 2026-08-01T00:00:00+03:00 \
  --until 2026-08-16T00:00:00+03:00 \
  --checkpoint-root state/recovery/august-2026-vlad/collection/calendly-checkpoints \
  --output state/recovery/august-2026-vlad/collection/calendly-recordings.json
```

Expected: complete paginated result; scheduled events without recordings have no billed duration.

- [ ] **Step 3: Reconcile with Fathom**

```bash
python3 scripts/meeting_reconciliation.py \
  --period-manifest state/recovery/august-2026-vlad/period-manifest.json \
  --fathom-from-manifest \
  --calendly state/recovery/august-2026-vlad/collection/calendly-recordings.json \
  --algorithm meeting-dedup/v1 \
  --tolerance-seconds 300 \
  --output state/recovery/august-2026-vlad/meeting-reconciliation.json
```

Expected: Calendly-only recordings remain full duration; duplicates become one canonical meeting; conflicts/multiple candidates remain exceptions.

- [ ] **Step 4: Validate timestamped multi-project splits**

Allow a split only when transcript/recording timestamps cite each boundary and the validated routes are distinct. Otherwise use one evidenced route or surface an exception.

- [ ] **Step 5: Append complete inventory evidence**

Advance only if observed/expected counts match and every source recording is canonicalized, excluded with reason, or explicitly exceptional.

No commit: all Calendly evidence is private.

---

### Task 5: Recollect Recoverable Device Evidence and Build the Review Packet

**Files:**
- Create untracked: `state/recovery/august-2026-vlad/collection/device-coverage.json`
- Create untracked: `state/recovery/august-2026-vlad/review/review-packet.json`
- Create untracked: `state/recovery/august-2026-vlad/review/review-packet.csv`
- Create untracked: `state/recovery/august-2026-vlad/review/quality-report.json`

**Interfaces:**
- Reuses verified MacBook/Precision slices through August 14.
- Attempts only August 15 MacBook recovery and records Desktop limitation.

- [ ] **Step 1: Verify preserved device slice bundles**

Re-hash MacBook and Precision artifacts through August 14 and require compatible complete bundles. Do not recollect a verified slice.

- [ ] **Step 2: Attempt bounded August 15 MacBook recollection**

Collect only `[2026-08-15T00:00:00+03:00, 2026-08-16T00:00:00+03:00)`. If unavailable, retain exact interval debt. Reuse existing Precision evidence and keep agent/runtime-only activity excluded.

- [ ] **Step 3: Record Desktop limitation**

Create one `coverage_incomplete` item for the exact Desktop interval with reason `source_unavailable`. It remains visible in the review, approval, and publication contracts.

- [ ] **Step 4: Run accounting and quality**

Produce one packet containing every new/changed row, canonical meeting, route, fixed duration/split, overlap, billability, coverage limitation, and ambiguity. Do not estimate cost for unrouted rows.

- [ ] **Step 5: Verify packet totals and privacy**

Require no duplicate canonical IDs, no overlap, exact meeting duration conservation, zero silent omissions, and private evidence references only by safe IDs/digests in the board-facing CSV.

- [ ] **Step 6: Present the packet for human review**

This is a review gate, not Clockify approval. Record approve/skip/modify decisions and explicit acceptance or rejection of the Desktop limitation.

No commit: decisions and private review evidence remain in ignored state.

---

### Task 6: Replay, Exact Approval, and Clockify Posting

**Files:**
- Create untracked: `state/recovery/august-2026-vlad/review/replay-integrity.json`
- Create untracked: `state/recovery/august-2026-vlad/approval/clockify-approval.json`
- Create untracked: `state/recovery/august-2026-vlad/posting/post-events.jsonl`
- Create untracked: `state/recovery/august-2026-vlad/posting/post-receipt.json`

**Interfaces:**
- Consumes final review decisions, quality, routing, corrections, meeting reconciliation, period manifest, and slice bundles.
- Produces a clean replay and, only after exact approval, append-only Clockify post/readback evidence.

- [ ] **Step 1: Run immutable replay**

Replay byte-identical evidence and sealed model decisions. Require exact bindings for ledger, caches, accounting, routing, corrections, acceptance input, meeting reconciliation, period manifest, and slice bundles.

Expected: replay `pass` with no new/changed decision drift.

- [ ] **Step 2: Produce a no-write Clockify preflight**

Run the poster without `--execute` using the final portfolio/quality/replay/routing/coverage artifacts. Require zero duplicate/multiple-exact-match/overlap conflict and record the exact operation identity and portfolio digest.

- [ ] **Step 3: Request exact Clockify approval**

Present member/workspace, half-open period, portfolio digest, quality/replay/routing/correction/coverage digests, entry count/duration, residual Desktop limitation, and exact operation. Do not execute until the human explicitly approves this artifact.

- [ ] **Step 4: Record the approval receipt**

Create the immutable approval receipt with approver, timestamp, expiry/single-use semantics, exact target/operation, and all digests. Verify it before credential loading or network.

- [ ] **Step 5: Execute approved posting once**

Run the poster with `--execute`, approval receipt, period manifest, and append-only post events. Resume exact existing/recovered items; never blindly retry ambiguous responses.

- [ ] **Step 6: Verify the final full-period ledger**

Freshly read the entire period, bind all created/existing IDs, exact seconds, and native costs, and require the final post receipt/live-readback digest to match.

No commit: approvals, entry IDs, and receipts remain private.

---

### Task 7: Prepare, Authorize, and Publish the Corrected Finance Report

**Files:**
- Create untracked: `state/recovery/august-2026-vlad/publication/fx-quote.json`
- Create untracked: `state/recovery/august-2026-vlad/publication/publication-prepared.json`
- Create untracked: `state/recovery/august-2026-vlad/publication/publication-authorized.json`
- Create untracked: `state/recovery/august-2026-vlad/approval/publication-approval.json`
- Create untracked: `state/recovery/august-2026-vlad/publication/publication-receipts.jsonl`
- Modify after verification: `clockify-process-acceptance.md`

**Interfaces:**
- Consumes final Clockify readback, period manifest, ECB quote, and publication approval.
- Produces native currency totals, USD-converted buckets/grand total, corrected shared-report and Slack receipts, and final `published` state.

- [ ] **Step 1: Capture and validate the ECB quote**

Run `python3 scripts/clockify_currency.py fetch-ecb --publication-date-from-clock Europe/Bucharest --output state/recovery/august-2026-vlad/publication/fx-quote.json`. The command uses the actual invocation date, selects that date or the latest prior business day no more than four calendar days old, and persists provider, effective date, fetched time, EUR-base rates, and payload digest.

- [ ] **Step 2: Build `publication_prepared`**

```bash
python3 scripts/clockify_publication_gate.py prepare \
  --manifest state/recovery/august-2026-vlad/period-manifest.json \
  --post-receipt state/recovery/august-2026-vlad/posting/post-receipt.json \
  --clockify-readback state/recovery/august-2026-vlad/readback/clockify-api-after.json \
  --fx-quote state/recovery/august-2026-vlad/publication/fx-quote.json \
  --quality state/recovery/august-2026-vlad/review/quality-report.json \
  --replay state/recovery/august-2026-vlad/review/replay-integrity.json \
  --coverage state/recovery/august-2026-vlad/collection/device-coverage.json \
  --output state/recovery/august-2026-vlad/publication/publication-prepared.json
```

Require verified slices, complete/approved coverage, no unresolved semantic/routing/overlap/billability exceptions, quality/replay pass, valid Clockify approval/post/readback, native currency buckets, and recomputed FX summary. Render each native bucket, each USD conversion, and `usd_equivalent_total`.

- [ ] **Step 3: Request separate publication approval**

Present the exact shared-report correction, Slack correction, period/revision, contract digest, idempotency key, verified duration, native currency buckets, converted USD buckets, USD grand total, and retained Desktop limitation. Do not mutate report or Slack until explicitly approved.

- [ ] **Step 4: Authorize and execute the report phase**

```bash
python3 scripts/clockify_publication_gate.py authorize \
  --prepared state/recovery/august-2026-vlad/publication/publication-prepared.json \
  --approval state/recovery/august-2026-vlad/approval/publication-approval.json \
  --output state/recovery/august-2026-vlad/publication/publication-authorized.json
python3 scripts/clockify_finance_report_adapter.py execute \
  --period-manifest state/recovery/august-2026-vlad/period-manifest.json \
  --events state/recovery/august-2026-vlad/period-events.jsonl \
  --authorized state/recovery/august-2026-vlad/publication/publication-authorized.json \
  --receipts state/recovery/august-2026-vlad/publication/publication-receipts.jsonl
```

The adapter refreshes or updates the named shared report, then reads it back. Require exact filters, period, member, duration, and native currency buckets. On mismatch, record `report_mismatch` and make no Slack call. On success, require the adapter to append `shared_report_verified`; after Slack succeeds and is read back, it appends `publication_complete` bound to both receipts before the manifest may derive `published`.

- [ ] **Step 5: Execute or retry the Slack phase**

Only after report readback passes, upsert the finance-report message under:

```text
finance-report:<workspace>:<member>:2026-08-01:2026-08-16:1
```

The message shows verified duration, every native currency total, every USD conversion, and the USD-equivalent grand total. If Slack fails after report success, retry only Slack with the same unexpired approval/key.

- [ ] **Step 6: Verify final publication receipts**

Re-read the shared report and Slack message; require both receipts to bind the same contract digest/idempotency identity and the coordinator to derive `published`.

- [ ] **Step 7: Update acceptance documentation and run the completion audit**

Record only aggregate safe evidence in `clockify-process-acceptance.md`: period, final duration, native/converted totals, source coverage status, residual Desktop limitation, manifest/replay/post/publication digests, and verification timestamps. Run the full test suite and the goal's requirement-by-requirement audit before declaring completion.

- [ ] **Step 8: Commit and request push approval for the final implementation**

Private recovery artifacts remain untracked. Commit only code, tests, schemas, and documentation produced by the implementation plans.

```bash
git add README.md clockify-process-acceptance.md scripts schemas tests ops
git commit -m "feat: complete verified August Clockify reconciliation"
```

Before `git push`, present the exact commit range and target branch for the separately guarded push approval unless a still-valid direct approval explicitly names that range/target.
