# Review pipeline efficiency: offline before/after (2026-10-26)

All numbers come from sealed artifacts on omarchy-precision
(`~/Work/automation-clockify-sync/state` and `runs/`). No model or Clockify
call was made, and nothing under `state/` or `runs/` was written.
`scripts/efficiency_measurement.py` reproduces each table:

```bash
python3 scripts/efficiency_measurement.py --repo ~/Work/automation-clockify-sync cache-rejections
python3 scripts/efficiency_measurement.py --repo ~/Work/automation-clockify-sync partitions
python3 scripts/efficiency_measurement.py --repo ~/Work/automation-clockify-sync projection \
  20261022T135014Z-repair-pfm8f5cs 20261022T134828Z-repair-ksgx0xlf
python3 scripts/efficiency_measurement.py --repo ~/Work/automation-clockify-sync clockify-overlap
journalctl --user -u clockify-review-cycle.service --since 2026-10-19 -o short-iso > cycle-journal.txt
python3 scripts/efficiency_measurement.py circuit-breaker cycle-journal.txt
```

## Summary

| Item | Default | Before | After | Saving |
|---|---|---|---|---|
| 1. Partition normalization | on (pipeline cache) | 122 bookkeeping rejections in corpus, each followed by 1–2 repair calls | 38 accepted locally (31%) | ≥38 repair calls in corpus; est. ~210 of 1,612 cache decisions (~13%) |
| 2. Trimmed projection v2 | opt-in (routing) | 6.17 MB extract bodies, 35 chunks | 4.00 MB, 24 chunks | −35.3% bytes, −31% extract calls |
| 3. Bounded Clockify read v3 | opt-in (routing) | 64 pages, 83–94 s per slice | est. 2 pages (~3 s) | ~97% of pages |
| 4. Cycle circuit breaker | on | 11 identical failed cycles, 149.8 CPU-min | 3 cycles, 40.8 CPU-min | −73% CPU, other slices unblocked |
| 5. Call diagnostics | on (pipeline cache) | no per-call cost data | numeric record per live call | measurement enabler; ~1.6 ms/call |

## 1. Review partition normalization

Denominator (`cache-rejections`): the live cache holds 1,612 decisions: 718
accepted, and 674 (41.8%) rejected for bookkeeping (367
`invalid_evidence_ids`, 245 `duplicate_evidence`, 62 `omitted_evidence`).
Rejected cache records keep only the failure code, so the replay corpus is
the 136 unique numeric rejection skeletons in the review-diagnostics
sidecars (122 bookkeeping).

| Failure code | Skeletons | Lossless | Quarantine ≤25% (applied) | >25% (model repair kept) |
|---|---|---|---|---|
| duplicate_evidence | 59 | 0 | 26 | 33 |
| invalid_evidence_ids | 49 | 2 | 1 | 46 |
| omitted_evidence | 14 | 0 | 9 | 5 |

- In the 38 applied cases, 199 of 2,016 members (9.9%) go to an
  `analyzer_review_partial_quarantine` exception for manual review and
  scoped recovery. Each case is checked to be an exact cover: no member is
  lost or cited twice.
- Most `invalid_evidence_ids` responses left whole rows with no evidence
  (641 such rows). No deterministic fix exists for those, so they keep the
  model repair path.
- Cap sensitivity, accepted locally out of 122: 10% → 16, 25% → 38,
  50% → 57, 100% → 122.
- The ~210 extrapolation applies the corpus rate (31%) to the 674 cached
  bookkeeping rejections. It is an estimate.

## 2. Trimmed projection (`clockify-semantic-projection/v2`)

These are extraction request bodies for the two latest full runs, built with
the pipeline's own noise filter, route hints, actor context and chunker.
Review requests carry the same bundles, so they shrink by a similar amount.

| Run | Analysis events | Bytes v1 → v2 | Chunks v1 → v2 | Members over 32 KB |
|---|---|---|---|---|
| 20261022T135014Z-repair-pfm8f5cs (Oct 21–22) | 1,609 | 3,764,850 → 2,122,065 | 22 → 12 | 12 → 1 |
| 20261022T134828Z-repair-ksgx0xlf (Oct 19–21) | 1,438 | 2,408,101 → 1,873,273 | 13 → 12 | 2 → 0 |

All 3,047 members are still sent. The remaining member over 32 KB is a
non-user source description, which the user-body cap does not touch.

## 3. Bounded Clockify overlap read (`clockify-overlap-pagination/v3`)

| Saved v2 checkpoint | Pages | Wire rows | Retained | Retained under v3 bound | Identical | Time |
|---|---|---|---|---|---|---|
| 2026-10-18T21Z – 10-20T21Z | 64 | 12,652 | 3 | 3 | yes | 94.5 s |
| 2026-10-20T21Z – 10-22T03Z | 64 | 12,652 | 0 | 0 | yes | 83.2 s |

- Across 434 saved completed entries, the longest is 3 h 29 min.
- At the busiest saved density (43 entries/day), a 2-day slice plus the
  7-day lookback is about 387 rows, or 2 pages.
- The default stays the complete v2 scan. The v3 bound would miss an entry
  that began more than 7 days before a window and still overlaps it, a case
  `test_collector_burst_context` deliberately covers. Enabling v3 means
  accepting that trade-off.

## 4. Cycle circuit breaker

Between 2026-10-20 and 2026-10-26 the journal shows 11 cycles. Each one
re-selected failed slice 2026-10-01, ran the same failing child (13.5–13.7
CPU-min) and exited 75. Replaying those timestamps through the breaker
(2 failures, 72 h cooldown) leaves 3 runs. The estimate assumes the inputs
did not change between runs; any release or input change in that window
would add up to 2 runs.

## 5. Per-call diagnostics

Each record is about 470 bytes, and writing one costs about 1.6 ms for a
332 KB body. Sealed replays make no live calls and write no records.

## Replay stability

- The full suite passes: 2,813 tests, 17 skipped (master: 2,780).
- Two sealed runs were replayed with this branch into temporary roots,
  using copied caches and unreachable model and Clockify endpoints:
  `20261026T114810Z-repair-ifo0i2dr` and `20261022T135014Z-repair-pfm8f5cs`
  (with its 8-run repair lineage).
- Both replays pass integrity. `semantic-analysis.json`, `proposals.json`,
  `work-accounting-result.json` and `allocation-report.json` are
  byte-identical to the sealed replays, and the cache copies are unchanged.
- A master-code replay of the first run differs from the branch replay only
  in run IDs and temporary paths.
