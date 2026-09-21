# Cross-Release Source Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:test-driven-development and execute each task as RED, GREEN, then refactor. Do not commit, publish, deploy, or touch live services.

**Goal:** Recover actionable source gaps from an immutable old-release deterministic bundle in a distinct current-runtime namespace without recollecting compatible complete sources or permitting publication before current-runtime verification.

**Architecture:** Extend the existing completion-bundle, exact source-debt, recovery-parent, and attempt-journal contracts. A normal stage must match the current runtime; a structurally valid old-runtime stage may only become an immutable recovery parent and exact debt source. Recovery adopts compatible parent artifacts by digest, recollects one requested peer facet, merges records by stable identity into a new run, and records an attestation binding every adopted digest to the parent.

**Tech Stack:** Python standard library, `unittest`, existing collector/review/source-debt modules.

**Spec:** User-supplied cross-release recovery invariant dated 2026-09-21.

## Global Constraints

- Preserve old runs and the four reconciliation snapshots byte-for-byte.
- Fail closed on runtime, schema, compatibility, digest, or lineage ambiguity.
- Never treat an old-runtime result as a current-runtime source or publication input.
- Recollect only the exact requested source facet; reuse compatible complete evidence explicitly by digest.
- Retry reactivation is append-only and occurs once per material runtime/source-health epoch.
- Synthetic local tests only; no external collectors, inference, Sheets, Clockify, Multica, Git integration, or service mutation.

## Review Focus

- Legacy debt ledgers migrate deterministically without losing provenance.
- Repeating the same unhealthy epoch cannot reactivate exhausted debt.
- Two source facets from one host merge without duplicate evidence identities.
- A compatibility/schema mismatch fails before transport or artifact adoption.
- Publisher execution remains unreachable until source and replay stages are current-runtime complete.

---

### Task 1: Current-runtime stage gate and old-runtime classification

**Files:**
- Modify: `scripts/clockify_review_cycle.py`
- Test: `tests/test_review_cycle_source_debt.py`

**Interfaces:**
- Consumes: verified completion bundle plus `run-report.json.runtime_identity`.
- Produces: a current-runtime stage, or an explicitly historical incomplete stage usable only to create exact debt and a recovery-parent binding.

- [ ] Write failing tests proving old-runtime bundles are not directly adopted, generate exact debts, and cannot reach replay/publication.
- [ ] Run the focused tests and capture the expected failures.
- [ ] Add the minimal runtime identity gate and historical-gap classification.
- [ ] Re-run focused tests to green.

### Task 2: Source-only derived recovery and adoption attestation

**Files:**
- Modify: `scripts/clockify_source_debt_recover.py`
- Modify: `scripts/clockify_review_run.py`
- Test: `tests/test_source_debt_recovery.py`

**Interfaces:**
- Consumes: one immutable parent, exact source name, attempt ID, current runtime.
- Produces: a distinct current-runtime recovery run and `source-adoption.json` binding parent artifact/snapshot digests, requested source, and merge identity.

- [ ] Write failing tests for exact-source collection, immutable parent/snapshots, compatible adoption, incompatible failure, idempotent stable-identity merge, and current runtime.
- [ ] Run focused tests and capture RED.
- [ ] Implement source-facet collection, digest-bound adoption, and deterministic merge in the existing recovery namespace.
- [ ] Re-run focused tests to green.

### Task 3: Transition-scoped retry budget and end-to-end acceptance

**Files:**
- Modify: `scripts/source_coverage.py`
- Modify: `scripts/clockify_review_cycle.py`
- Test: `tests/test_source_coverage.py`
- Test: `tests/test_review_cycle_source_debt_end_to_end.py`

**Interfaces:**
- Consumes: debt plus material runtime/source-health epoch digest.
- Produces: at most one reactivation event for a changed epoch; unchanged exhausted state stays ineligible.

- [ ] Write failing migration, one-shot reactivation, unchanged-state, complete merge, current-runtime verification, and exactly-once publication tests.
- [ ] Run focused tests and capture RED.
- [ ] Add the append-only epoch transition and coordinator wiring.
- [ ] Run focused tests, then the complete offline suite, `py_compile`, and `git diff --check`.
