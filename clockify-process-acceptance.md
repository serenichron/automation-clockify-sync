# Clockify process acceptance matrix

This document separates local implementation proof from the guarded live
evidence required before this process can replace manual review. A passing unit
suite is not evidence that an analyzer route, fleet deployment, July source
inventory, Sheet surface, or review-acceptance target is healthy.

Calendly is intentionally excluded from this candidate. Existing Clockify
entries are immutable fixed evidence; raw evidence may overlap, but proposals
may not overlap fixed blocks or one another.

## Required process

| Requirement | Current status | Authoritative evidence |
|---|---|---|
| Immutable evidence ledger | Proven locally | `scripts/evidence_ledger.py`; `tests/test_evidence_ledger.py`; `schemas/evidence-ledger-v1.json` |
| Complete cross-machine, repository, Clockify, Multica, and Fathom collection | Implemented locally; live completeness unproven | `scripts/clockify_sync_collect.py`; `tests/test_process_integration.py`; requires a guarded fleet run |
| Noise removal without discarding substantive work | Proven for deterministic classes; live semantic accuracy unproven | `scripts/work_accounting_pipeline.py`; `scripts/semantic_analyzer.py`; `tests/test_work_accounting_pipeline.py` |
| Cross-session and cross-machine semantic workstreams | Proven with fixtures; live analyzer quality unproven | `scripts/semantic_analyzer.py`; cross-chunk tests in `tests/test_semantic_analyzer.py` |
| Atomic split and duplicate-evidence merge | Proven locally by validator and fixtures; live semantic accuracy unproven | `scripts/semantic_analyzer.py`; `tests/test_semantic_analyzer.py`; `tests/test_work_accounting_pipeline.py` |
| Semantic Fathom reconciliation | Proven with fixtures, including full creation-history fetch plus occurrence filtering; live denominator unproven | `tests/test_fathom_semantic_evidence.py`; Fathom tests in `tests/test_work_accounting_pipeline.py` |
| Separate evidence and allocation timelines | Proven locally | `scripts/evidence_ledger.py`; `scripts/work_allocator.py`; `tests/test_work_allocator.py` |
| Honest non-overlapping effort allocation | Proven locally | `scripts/work_allocator.py`; allocator and pipeline tests for fixed blocks, no gap filling, minimum rollback, and `contested_time` |
| Deterministic Caveman descriptions | Proven locally for emitted fixtures and the 86-record corpus | `scripts/caveman_renderer.py`; `tests/test_caveman_renderer.py`; `tests/test_regression_corpus.py` |
| Tiered privacy-gated model analysis | Deny-by-default private-text gate and mocked transports proven; live route and authorized evaluation unproven | `scripts/semantic_analyzer.py`; `scripts/analyzer_evaluation.py`; requires a live probe, separate private-text approval, and digest-bound offline evaluation |
| Evidence-bound correction learning | Proven locally for approve, skip, modify, structural split partitions, omission, wording, routing, and allocation behavior; live decision ingestion unproven | `scripts/review_corrections.py`; `tests/test_review_corrections.py`; correction integration tests in `tests/test_work_accounting_pipeline.py` |
| Exceptions-only durable review | Not activated; `shadow_all` is the default and the executable acceptance gate remains unproven live | `scripts/clockify_review_run.py`; `scripts/review_acceptance.py`; related tests and integrity-linked period ledger |

## Structured activity contract

The versioned semantic schema and validator require stable activity/workstream
identity, parent workstream name, lifecycle, action, object, outcome, evidence citations and spans,
project/tags, minimum/recommended/maximum effort, semantic/timing confidence,
split/merge/omit rationale, analyzer provenance, and a render field. The render
field begins as `null`; deterministic routing and Caveman rendering populate it
before an activity can become a proposal.

Planned and noise activities do not become proposals. Title-only meetings,
missing sources, routing conflicts, low confidence, and contested capacity become
explicit exclusions or exceptions.

## Acceptance gates

| Gate | Status | Evidence needed |
|---|---|---|
| 100% forbidden-content and no-truncation compliance | Proven for local emitted fixtures; live corpus unproven | Quality report from complete guarded shadow run |
| 100% cited or explicitly insufficient | Proven by local validator/fixtures; live corpus unproven | Complete ledger plus semantic/accounting artifacts |
| 100% atomic or split | Proven by local fixtures; live semantic accuracy unproven | Human dispositions for full guarded shadow denominator |
| 100% non-overlap | Proven locally | Complete guarded allocation report checked against live Clockify blocks |
| Zero gap filling | Proven locally | Guarded allocation report and audit |
| Every eligible Fathom meeting reconciled/excluded | Implemented locally; live denominator missing | Complete paginated Fathom inventory and reconciliation artifact |
| Replay produces `0 new / 0 changed` | Missing live proof | Two runs of the same immutable inputs/model versions against fresh durable state |
| Every analyzer route passes digest-bound evaluation | Missing live proof | One verified scorecard per model/tier used in each acceptance period |
| Versioned 86-record corpus | Proven locally | `tests/fixtures/clockify-regression/v1/manifest.json`; 86 content-addressed records |
| At least 90% approve unchanged baseline | Missing | One complete integrity-linked `shadow_baseline` report with decisions for every active row, including ambiguous rows |
| Two distinct later consecutive guarded periods at least 95% | Missing | Two complete `guarded` reports immediately after the baseline, each with full dispositions and zero critical errors |
| Every skip/modify decision assessed | Missing | Critical-assessment record for every changed decision; unassessed decisions fail the denominator |
| No critical routing, truth, meeting, or allocation errors | Missing live proof | Exception audit and human dispositions for guarded periods |
| One batch summary plus genuine exceptions | Implemented locally; live usability unproven | Guarded run outputs and user review result |

## Guarded evidence sequence

1. Freeze the candidate as an immutable Git SHA after explicit approval.
2. Probe the intended primary analyzer route and configured fallback without
   sending ledger evidence until the minimal probe succeeds.
3. Obtain a separate explicit decision before setting
   `CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED=approved`; a successful probe alone
   does not authorize private session, meeting, or issue prose to leave the
   machine.
4. Deploy and read back the exact SHA on Mac, Precision, and Desktop without
   changing Clockify, Sheets, or Multica review content.
5. Run a complete July 1 through August 3 read-only shadow reconciliation and
   require complete source manifests, including every eligible Fathom meeting.
6. Replay the same immutable input/model versions and require `0 new / 0 changed`.
7. Collect full-denominator approve/skip/modify dispositions and calculate the
   unchanged-approval rate; ambiguous rows are part of that denominator and
   every skip/modify needs a criticality assessment.
8. Append the period with `scripts/review_acceptance.py record`, supplying every
   analyzer scorecard with `--analyzer-scorecard`; then require a
   complete >=90% baseline and two distinct later consecutive complete >=95%
   guarded periods, each with source, quality, coverage, analyzer evaluation,
   replay, and zero-critical-error gates passing. Confirm eligibility with
   `scripts/review_acceptance.py status`.
9. Request separate approval before any Sheet refresh, Multica configuration,
   Clockify posting, merge, or production schedule change.

## Current rollout status

No candidate commit, route probe, private-text approval, deployment, Sheet
refresh, or Clockify write has been completed for this candidate. The live
route probe, explicit `CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED=approved`
decision, exact-SHA fleet deployment/readback, two complete July 1–August 3
shadow runs plus replay, the acceptance periods, and any Sheet refresh remain
separate guarded gates.

## Local verification

```bash
python3 -m unittest discover -s tests
python3 -m py_compile scripts/*.py
python3 -c 'import json, pathlib; [json.loads(p.read_text()) for p in sorted(pathlib.Path("schemas").glob("*.json"))]; [json.loads(p.read_text()) for p in (pathlib.Path("routing.json"), pathlib.Path("fleet.json"))]'
git diff --check
```
