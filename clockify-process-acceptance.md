# Clockify process acceptance matrix

This document separates local implementation proof from the guarded live
evidence required before this process can replace manual review. A passing unit
suite is not evidence that an analyzer route, fleet deployment, July source
inventory, Sheet surface, or review-acceptance target is healthy.

Calendly is optional only for a bounded, explicitly recorded historical
recovery. Future autopilot coverage treats Calendly as a first-class required
source. Existing Clockify entries are immutable fixed evidence; raw evidence
may overlap, but proposals may not overlap fixed blocks or one another.

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
| Tiered privacy-gated model analysis | DeepSeek V4 Flash is the required primary; deny-by-default private-text gating, bounded concurrent chunks, release-bound live evaluation, and fail-closed analyzer exceptions are proven locally; authorized July accuracy remains under validation | `scripts/semantic_analyzer.py`; `scripts/analyzer_live_evaluation.py`; `scripts/analyzer_evaluation.py`; a fresh v15 scorecard bound to the resolved Flash release |
| Evidence-bound correction learning | Proven locally for approve, skip, modify, structural split partitions, omission, wording, routing, and allocation behavior; live decision ingestion unproven | `scripts/review_corrections.py`; `tests/test_review_corrections.py`; correction integration tests in `tests/test_work_accounting_pipeline.py` |
| Exceptions-only durable review | Not activated; `shadow_all` is the default and the executable acceptance gate remains unproven live | `scripts/clockify_review_run.py`; `scripts/review_acceptance.py`; related tests and integrity-linked period ledger |

## Structured activity contract

The versioned semantic schema and validator require stable activity/workstream
identity, parent workstream name, lifecycle, action, object, outcome, evidence citations and spans,
project/tags, minimum/recommended/maximum effort, semantic/timing confidence,
split/merge/omit rationale, analyzer provenance, and a render field. The render
field begins as `null`; deterministic routing and Caveman rendering populate it
before an activity can become a proposal.

Provider extraction uses content-addressed local semantic bundles and opaque
request-local bundle references. Models cite inclusive member ranges; local code
expands them back to immutable ledger IDs, rejects missing or overlapping
coverage, and computes final activity identity only after expansion. The bundle
schema and manifest digest are part of immutable replay verification.

Exhausted qualified-route contract rejections use deterministic
failed-partition recovery, including when only one qualified primary is configured:
children split only at context or complete conversation-turn boundaries and are
analyzed through the same route set, cancellation gate, and append-only cache until
they pass or reach an indivisible turn, singleton, or bounded depth limit.
Explicit session accomplishments must cite paired user intent and assistant
result from the same conversation; user requests and assistant-only status cannot
become human-attention work. Meetings remain separately eligible.
The parent retains recursive child metadata and cannot rejoin synthesis unless
every evidence ID is classified exactly once. Acceptance and replay accounting
read the actual leaf model/tier records; the deterministic recovery coordinator
is never treated as a synthetic analyzer route.
Repeated-workstream synthesis follows the same route boundary: exhausted sealed
contract rejection becomes an explicit `analyzer_synthesis_failure`, while a
probe, authentication, or non-retryable route failure blocks the run. Retryable
request transport gets three distinct cache-bound, probe-gated attempts;
exhaustion becomes an evidence-bound extraction or synthesis exception so one
unavailable semantic unit cannot discard unrelated month-scale work.

Planned and noise activities do not become proposals. Title-only meetings,
missing sources, routing conflicts, low confidence, exhausted recovered chunks,
workstream-synthesis rejection, and contested capacity become explicit
exclusions or exceptions. The analyzer uses a 250,000-byte and 250-event
operational target with four deterministically ordered workers beneath its
1,450,000-byte hard body ceiling; complete individual evidence is never clipped.

## Acceptance gates

| Gate | Status | Evidence needed |
|---|---|---|
| 100% forbidden-content and no-truncation compliance | Proven for local emitted fixtures; live corpus unproven | Quality report from complete guarded shadow run |
| 100% cited or explicitly insufficient | Proven by local validator/fixtures; live corpus unproven | Complete ledger plus semantic/accounting artifacts |
| 100% atomic or split | Proven by local fixtures; live semantic accuracy unproven | Human dispositions for full guarded shadow denominator |
| 100% non-overlap | Proven locally | Complete guarded allocation report checked against live Clockify blocks |
| Zero gap filling | Proven locally | Guarded allocation report and audit |
| Every eligible Fathom meeting reconciled/excluded | Implemented locally; live denominator missing | Complete paginated Fathom inventory and reconciliation artifact |
| Replay produces `0 new / 0 changed` | Immutable replay path, evidence-bundle manifest binding, and validated append-only analyzer-decision cache proven locally; live proof missing | First shadow run plus a distinct `--replay-from` run with matching bundle/cache-decision digests, passing `replay-integrity.json`, and `0 new / 0 changed` |
| Period-bound replay resilience | Proven locally with routine and exceptional synthetic fixtures; live proof missing | Every fresh run snapshots its exact period manifest, routing, corrections, and acceptance inputs before accounting; replay accepts no overrides and consumes only those snapshots, with matching period revision/event digest, canonical meeting reconciliation, and every ordered verified slice bundle; no raw evidence paths or credentials in the binding |
| Every analyzer route passes digest-bound evaluation | Missing live proof | One verified scorecard per model/tier used in each acceptance period |
| Versioned 86-record corpus | Proven locally | `tests/fixtures/clockify-regression/v1/manifest.json`; 86 content-addressed records |
| At least 90% approve unchanged baseline | Missing | One complete integrity-linked `shadow_baseline` report with decisions for every active row, including ambiguous rows |
| Two distinct later consecutive guarded periods at least 95% | Missing | Two complete `guarded` reports immediately after the baseline, each with full dispositions and zero critical errors |
| Every skip/modify decision assessed | Missing | Critical-assessment record for every changed decision; unassessed decisions fail the denominator |
| No critical routing, truth, meeting, or allocation errors | Missing live proof | Exception audit and human dispositions for guarded periods |
| One batch summary plus genuine exceptions | Implemented locally; live usability unproven | Guarded run outputs and user review result |
| Fail-closed finance publication | Proven locally with routine and backlog fixtures; live transport unproven | `tests/test_publication_end_to_end.py`; immutable coordinator/receipt/readback/gate evidence and a separately approved external transport |

## Guarded evidence sequence

1. Resolve the clean local candidate with `git rev-parse HEAD` and bind every
   approval, deployment, run manifest, and readback to that immutable SHA. A
   later local edit creates a new candidate and invalidates the prior binding.
2. After explicit publication/deployment approval, push only the feature branch,
   deploy that exact SHA to Mac, Precision, and Desktop, and verify Git/readback
   hashes without merging or changing any scheduled automation.
3. Run `scripts/analyzer_live_evaluation.py` for the intended primary analyzer
   and any configured fallback. Require every raw replay to pass schema,
   citation, partition, atomicity, concrete-concept, and rendering gates and to agree on the
   review-relevant semantic decision. All cases are built-in and no ledger
   evidence is sent.
4. Obtain a separate explicit decision before setting
   `CLOCKIFY_ANALYZER_PRIVATE_TEXT_APPROVED=approved`; a successful probe alone
   does not authorize private session, meeting, or issue prose to leave the
   machine.
5. Run one complete July 1 through August 3 read-only shadow reconciliation and
   require complete source manifests, including every eligible Fathom meeting.
6. Replay the same immutable input and model versions through the same durable
   state and validated analyzer cache. Do not supply period, routing,
   corrections, or acceptance overrides: replay must consume only the four
   snapshots retained by the source run. Require identical cache-decision digests
   plus matching period/revision, routing, correction, acceptance, canonical
   meeting, and ordered completion-bundle identities, and `0 new / 0 changed`; this is the second run, not an additional
   independently collected period.
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

The guarded candidate is maintained on
`codex/clockify-analyzer-determinism`; resolve and record its exact SHA at every
publication, deployment, and run boundary because any later edit creates a new
candidate. It is not merged to `master`. No Clockify, Multica, or schedule
mutation is authorized. Exact-SHA fleet readback, fresh v15 route scorecards,
the fixed-denominator canary, one complete July 1–August 4 shadow run plus
immutable replay, and any approved stable-ID review-surface patch remain
evidence gates rather than assumptions.

## Local verification

```bash
python3 -m unittest discover -s tests
python3 -m py_compile scripts/*.py
python3 -c 'import json, pathlib; [json.loads(p.read_text()) for p in sorted(pathlib.Path("schemas").glob("*.json"))]; [json.loads(p.read_text()) for p in (pathlib.Path("routing.json"), pathlib.Path("fleet.json"))]'
git diff --check
```

## Finance-publication acceptance

The publication gate consumes only artifacts bound into the verified period
manifest: each required contiguous slice, source coverage and any immutable
limitation approval, quality/replay results, consumed post approval, chained
post-event receipt, fresh API and shared-report readbacks, and an eligible ECB
quote. Native currency buckets remain visible beside their rounded USD
equivalents; the quote must be EUR-base and no more than four calendar days old.

The adapter order is fixed: update report, read it back exactly, persist the
report receipt, then idempotently upsert Slack and persist the publication
receipt. A Slack failure after report verification is `publication_incomplete`;
retry uses the retained report receipt and must not update the report again.
Only a separately approved contract may create `published`; an unchanged
idempotency key returns the existing receipt without an external call.
