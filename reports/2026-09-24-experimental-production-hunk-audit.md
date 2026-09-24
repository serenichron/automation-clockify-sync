# Experimental production hunk audit

Bounded range: `1a09002f4450d24ea399ef8ed8afece82fe77495..5dc10eb61405751672856b7758ce7eccc9db24d0`

Production-only diff SHA-256: `a9eedd80bb1944d62b57050f46b4c2f1e0796b7aea7142ca98fe13c34504a88d`

Every retained production hunk in the bounded range is listed once below. The
guarantee number refers to the six guarantees in
`docs/superpowers/specs/2026-09-22-clockify-consumer-owned-verification-design.md`.
No hunk is treated as justified merely by broad-suite coverage.

| File and hunk | Guarantee | Focused regression test |
|---|---:|---|
| `clockify_review_cycle.py @@ -83,0 +84,4` | 2 | `test_quality_failure_persists_exact_peer_debt_from_verified_collector_source` |
| `clockify_review_cycle.py @@ -965 +969` | 2 | `test_quality_failure_persists_exact_peer_debt_from_verified_collector_source` |
| `clockify_review_cycle.py @@ -1098,0 +1103,173` | 2 | `test_quality_failure_persists_exact_peer_debt_from_verified_collector_source` |
| `clockify_review_cycle.py @@ -1114,8 +1291,22` | 2 | `test_delivered_legacy_source_and_replay_bind_historical_runtime_once` |
| `clockify_review_cycle.py @@ -1150,11 +1341,28` | 2 | `test_old_release_incomplete_bundle_is_recovery_parent_not_current_source` |
| `clockify_review_cycle.py @@ -1377,0 +1586,11` | 2 | `test_exact_debt_preserves_verified_opaque_collector_compatibility` |
| `clockify_review_cycle.py @@ -1620,2 +1839,8` | 2 | `test_generic_retry_resolves_only_after_verified_complete_bundle` |
| `clockify_review_cycle.py @@ -1980,2 +2205,8` | 2 | `test_recovery_accepts_raw_parent_after_only_derived_artifact_drift` |
| `clockify_review_cycle.py @@ -2001 +2232,5` | 2 | `test_recovery_accepts_raw_parent_after_only_derived_artifact_drift` |
| `clockify_review_cycle.py @@ -2293,2 +2528,2` | 2 | `test_old_release_incomplete_bundle_is_recovery_parent_not_current_source` |
| `clockify_review_cycle.py @@ -2297,0 +2533,2` | 2 | `test_old_release_incomplete_bundle_is_recovery_parent_not_current_source` |
| `clockify_review_cycle.py @@ -2309,0 +2547` | 2 | `test_quality_failure_persists_exact_peer_debt_from_verified_collector_source` |
| `clockify_review_cycle.py @@ -2348,0 +2587,8` | 2 | `test_quality_failure_persists_exact_peer_debt_from_verified_collector_source` |
| `clockify_review_cycle.py @@ -2350 +2596,7` | 2 | `test_delivered_legacy_source_and_replay_bind_historical_runtime_once` |
| `clockify_review_cycle.py @@ -2352,5 +2604,22` | 2 | `test_quality_failure_persists_exact_peer_debt_from_verified_collector_source` |
| `clockify_review_run.py @@ -23 +23` | 4 | `test_reused_collector_bundle_derives_in_immutable_attempt_with_split_runtime_provenance` |
| `clockify_review_run.py @@ -53,0 +54,7` | 4 | `test_reused_collector_bundle_derives_in_immutable_attempt_with_split_runtime_provenance` |
| `clockify_review_run.py @@ -518 +525,7` | 1 | `test_collector_source_bundle_survives_derived_drift_but_binds_raw_evidence` |
| `clockify_review_run.py @@ -531 +544` | 1 | `test_collector_source_bundle_survives_derived_drift_but_binds_raw_evidence` |
| `clockify_review_run.py @@ -645,2 +658,2` | 1 | `test_collector_source_bundle_survives_derived_drift_but_binds_raw_evidence` |
| `clockify_review_run.py @@ -656,0 +670` | 1 | `test_collector_source_bundle_survives_derived_drift_but_binds_raw_evidence` |
| `clockify_review_run.py @@ -658,0 +673` | 1 | `test_collector_source_bundle_survives_derived_drift_but_binds_raw_evidence` |
| `clockify_review_run.py @@ -666 +681` | 1 | `test_collector_source_bundle_survives_derived_drift_but_binds_raw_evidence` |
| `clockify_review_run.py @@ -670,0 +686,22` | 1 | `test_collector_derivation_verifier_rejects_symlinked_lineage_file` |
| `clockify_review_run.py @@ -811,0 +849,345` | 5 | `test_reused_collector_bundle_derives_in_immutable_attempt_with_split_runtime_provenance`; `test_collector_derivation_successfully_finalizes_verified_completion` |
| `clockify_review_run.py @@ -1491,0 +1874` | 5 | `test_collector_derivation_successfully_finalizes_verified_completion` |
| `clockify_review_run.py @@ -1497 +1880,5` | 5 | `test_collector_derivation_successfully_finalizes_verified_completion` |
| `clockify_review_run.py @@ -1501,0 +1889,2` | 5 | `test_collector_derivation_successfully_finalizes_verified_completion` |
| `clockify_review_run.py @@ -1836,0 +2226,9` | 5 | `test_fresh_collection_adopts_terminal_derivation_without_overwrite` |
| `clockify_source_debt_recover.py @@ -384 +384` | 5 | `test_recovery_accepts_raw_parent_after_only_derived_artifact_drift` |
| `clockify_source_debt_recover.py @@ -441,2 +441,19` | 5 | `test_recovery_accepts_raw_parent_after_only_derived_artifact_drift` |
| `clockify_sync_collect.py @@ -54,0 +55` | 1 | `test_collector_source_bundle_survives_derived_drift_but_binds_raw_evidence` |
| `clockify_sync_collect.py @@ -78,0 +80` | 1 | `test_collector_source_bundle_survives_derived_drift_but_binds_raw_evidence` |
| `clockify_sync_collect.py @@ -4299 +4301,3` | 1 | `test_collector_source_bundle_survives_derived_drift_but_binds_raw_evidence` |
| `collector_receipts.py @@ -10,0 +11` | 1 | `test_collector_source_bundle_survives_derived_drift_but_binds_raw_evidence` |
| `collector_receipts.py @@ -14,0 +16,5` | 1 | `test_collector_source_bundle_survives_derived_drift_but_binds_raw_evidence` |
| `collector_receipts.py @@ -30,0 +37,7` | 1 | `test_collector_source_bundle_survives_derived_drift_but_binds_raw_evidence` |
| `collector_receipts.py @@ -89,0 +103,24` | 1 | `test_collector_source_bundle_survives_derived_drift_but_binds_raw_evidence` |
| `collector_receipts.py @@ -270,0 +308,24` | 1 | `test_collector_source_bundle_survives_derived_drift_but_binds_raw_evidence` |
| `collector_receipts.py @@ -319,0 +381,9` | 1 | `test_collector_source_bundle_survives_derived_drift_but_binds_raw_evidence` |
| `collector_receipts.py @@ -483,0 +554,149` | 1 | `test_collector_source_bundle_survives_derived_drift_but_binds_raw_evidence` |

Result: 41 of 41 production hunks are retained and mapped exactly once. No
unmapped production hunk remains in the bounded range. Guarantees 3 and 6 are
also locked by the exact reviewed Task 1 unresolved-routing port from commit
`943bb23` and the publication delivery tests. Those additional schema,
accounting, quality, and publication hunks are outside this historical bounded
range and are identified separately in the Task 3 report.

## Ten-scenario executable coverage

These are behavior tests, not source/name checks. Each named scenario invokes
`assert_scenario_contract` against its own real outputs and independently
checks nonempty unique stable identities, exact immutable parent bytes,
duplicate-free emitted rows/receipts/events, and zero Clockify write-adapter
calls at the narrowest boundary exposed by that scenario.

| Scenario | Exercising tests and observable assertions |
|---|---|
| Same-open bytes | `test_collector_source_bundle_survives_derived_drift_but_binds_raw_evidence` replaces the path after descriptor open, compares returned bytes, and independently checks the bundle/slice identities, exact completion-bundle parent bytes, unique artifact receipts, and the pure receipt boundary. |
| Later slice preserves peer debt | `test_quality_failure_persists_exact_peer_debt_from_verified_collector_source` runs the later slice and independently checks the exact debt ID, immutable collector-source bytes, singleton failure event, and absence of a Clockify posting command. |
| Independent debts | `test_two_incomplete_sources_create_independent_exact_debts` independently checks two unique exact debt IDs/events, immutable routing/correction parents, independent retry counts, and absence of a Clockify posting command. |
| Debt-persistence crash convergence | `test_debt_write_crash_converges_without_duplicate_exact_failure` independently checks one stable debt ID, unchanged routing/correction parents, one failure event after retry, and absence of a Clockify posting command. |
| Full proposal plus partial-overlap warning | `test_partial_clockify_overlap_keeps_full_meeting_proposal_with_review_warning` independently checks the stable proposal/row identity, immutable evidence-ledger bytes, exact full duration and overlap warning, duplicate-free row emission, and `external_writes == false`. |
| Cross-provider explicit bridge ambiguity | `test_ambiguous_explicit_bridge_is_quarantined_independent_of_provider_ids` independently checks three singleton stable source identities under three lexical provider-label assignments, immutable normalized source bytes, duplicate-free meeting emissions, a visible timing conflict, and the pure reconciliation boundary. |
| Offline replay | `test_normal_inference_run_seals_used_cache_then_replays_without_mutable_state` drives the real normal accounting subprocess against a valid legacy v2 cache whose records lack route metadata and include an extra unused decision, proves the run derives the exact digest-matching configured route and atomically seals only its used records with path/count/SHA binding evidence, deletes the mutable cache, clears route environment, and completes replay from the run snapshot. `test_snapshot_rejects_legacy_decision_under_conflicting_configured_route` proves a conflicting current route cannot upgrade a legacy decision. `test_real_offline_replay_main_reuses_accepted_cache_and_passes_integrity` independently checks stable proposal/cache IDs, exact immutable source-file bytes, duplicate-free reused cache records, and `external_writes == false`. `test_inference_metadata_requires_cache_even_without_cache_summary_records` proves inference detection does not depend on summary records; missing or tampered sealed caches and evidence, prompt, model, request-body, or output drift fail closed. |
| Publication crash exactly once | `test_crash_after_publish_before_receipt_retries_same_stable_rows` independently checks stable receipt row IDs, exact immutable source-run bytes, duplicate-free receipt IDs, identical publisher commands after crash, and absence of a Clockify posting command. |
| Terminal-attempt adoption | `test_fresh_collection_adopts_terminal_derivation_without_overwrite` independently checks distinct source/attempt identities, exact source and terminal-result bytes, one adopted result identity, and a zero-call processing boundary. |
| Unresolved-route pending/unposted | `test_unresolved_routing_meeting_retains_partial_overlap_warning` independently checks the stable proposal/serialized row identity, exact immutable evidence-ledger bytes, duplicate-free row emission, and `external_writes == false`, while asserting the exact warning contract and J=`pending`, M=serialized marker, N=`unposted`. |

## Final whole-range hardening follow-up

The final whole-range review found three additional Important defects outside
the historical 41-hunk mapping above. Each was reproduced before production
code changed and now has a focused behavior regression:

| Finding | Minimal repair | Focused regression |
|---|---|---|
| A one-to-many explicit-identity component could union its first edge before discovering that one provider appeared twice; adding an empty provider therefore changed the result. | Precompute each explicit component's provider cardinality and quarantine the complete component before any union when a provider repeats. | `test_one_to_many_explicit_identity_is_quarantined_with_empty_provider` |
| Replay preparation parsed, hashed, and copied mutable source paths through separate reads, while final integrity did not enforce every digest sealed in `replay-source.json`. | Parse, hash, and snapshot ledger, semantic-analysis, accounting, and meeting inputs from retained stable-descriptor bytes; validate source, fixture, and optional cache digests against the sealed replay provenance during final integrity derivation. | `test_replay_preparation_copies_the_exact_preflight_semantic_bytes`; `test_replay_integrity_enforces_sealed_source_provenance_digests` |
| Publication treated an empty tag list as an unresolved route even when the project identity was valid. | Define a blank route from the project identity and explicit unresolved disposition, leaving tags optional for routed projects while preserving the exact unresolved contract. | `test_routed_tagless_project_publishes_as_pending`; existing unresolved-routing contract tests |

Fresh verification after these repairs: the eight-module focused matrix passed
195 tests; the complete offline suite passed 1,323 tests with 2 skips; and
`python -m compileall -q scripts tests` plus `git diff --check` both exited 0.
No network, inference, Google Sheets, Clockify, deployment, merge, push, or
other external mutation was performed.

### Participant-window ambiguity variant

A final reviewer follow-up found the same pre-union one-to-many hazard in the
`participant_window` fallback tier when an empty third provider selects the
global matcher. `test_one_to_many_participant_window_is_quarantined_with_empty_provider`
reproduced the partial `[1, 2]` grouping under two provider-name assignments
without any meeting ID or join URL. The component/cardinality prepass now runs
for every matching tier before union; the fallback and explicit variants both
remain three visible singleton meetings with a `multiple_candidates` exception.

Fresh verification for this variant: all 29 meeting reconciliation tests
passed; the complete offline suite passed 1,324 tests with 2 skips; and
`python -m compileall -q scripts tests` plus `git diff --check` both exited 0.
