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
- Use only `deepseek-v4-flash:cloud`; Calendly is optional and Gemini is non-blocking.
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
