# Task 1 — Exact-seconds authority

## Result

Exact `duration_seconds` and second-precision bounds are authoritative from
accounting proposal through review, quality validation, and posting.
`duration_minutes` is display-only and always derives as `duration_seconds //
60`; a valid 43-second interval therefore displays as zero minutes.

## Changed paths

- `schemas/work-accounting-result-v1.json`
- `scripts/work_accounting_pipeline.py`
- `scripts/clockify_portfolio_review.py`
- `scripts/clockify_portfolio_quality.py`
- `scripts/clockify_post_approved_portfolio.py`
- `tests/test_work_accounting_pipeline.py`
- `tests/test_portfolio_review.py`
- `tests/test_portfolio_quality.py`
- `tests/test_clockify_portfolio_post.py`

## Behaviour and compatibility decisions

- Proposal bounds must describe a positive interval. Exact timestamps and
  seconds survive source splits without minute rounding loss.
- In exact documents, second totals are conserved; each minutes field is the
  floor of its corresponding seconds total, not a sum of individually floored
  fragments.
- Legacy minute-only accounting remains available only when no exact-seconds
  declaration exists. Mixed exact/legacy rows, segments, or source proposals
  block fail-closed.
- Posting recomputes both duration fields after shifts, trims, receipt replay,
  and dry-run boundary-adjustment replay. Contiguous exact segments merge by
  aggregate seconds, including equivalent instants written with different ISO
  offsets.
- Malformed posting allocation segments now raise `PortfolioPostError`, so the
  posting CLI returns a controlled block rather than an uncaught conversion
  error.

## RED/GREEN evidence

Initial regressions reproduced the expected defects:

- The schema rejected a valid emitted 43-second proposal because display
  minutes had a minimum of one.
- Minute conservation invented a false exclusion for a 301-second source split
  as 151 + 150 seconds.
- Quality validation blocked a valid 43-second row and could throw when
  `allocation_segments` was `None`.
- Posting retained stale `630/600` duration fields after adjusted boundaries
  produced `613/617` seconds.
- Posting restored prior adjusted bounds without recomputing durations, skipped
  dry-run-only boundary adjustments, raised a raw error for malformed segment
  objects, and did not merge same-instant segments expressed with different
  offsets.

Focused posting RED command:

```text
python3 -m unittest discover -s tests -p 'test_clockify_portfolio_post.py' -v
```

It failed with the expected three assertions and one raw `ValueError` before
the posting correction. A subsequent executed-receipt regression also failed
against the first repair, then passed after receipt/adjustment agreement was
enforced.

GREEN verification:

```text
python3 -m unittest discover -s tests -v
Ran 681 tests in 2.368s
OK (skipped=2)

python3 -m json.tool schemas/work-accounting-result-v1.json >/dev/null
git diff --check
```

The schema parse and whitespace check completed successfully.

## Review and concerns

An independent task review identified the posting receipt-replay and legacy
packaging gaps recorded below. The work was then self-reviewed locally against
the stated exact/legacy contracts. An earlier duplicate, read-only reviewer
made no file, stage, commit, or external changes; no reviewer was spawned for
the fix round.

## Commit

Initial implementation commit: `71fb2c3100cdaa9867b25fe3b2c8fdd96794a8cc`

## Fix round 1 — legacy conservation and evidence correction

### Findings and changes

- Posting now derives `approved_duration_seconds` from the pre-mutation merged
  segment bounds for every row, including a fully legacy row. Receipt replay,
  adjustments, and boundary alignment therefore reject any changed aggregate
  timestamp duration.
- Review packaging now selects exact versus legacy allocation from the source
  declaration. Sources without `duration_seconds` use the minute allocator and
  produce rows and allocation segments without `duration_seconds`; source
  declarations that include seconds retain exact-second packaging.
- Corrected the inaccurate review attribution and removed the stale pending
  commit claim from this report.

Fix-round code and regression paths:

- `scripts/clockify_post_approved_portfolio.py`
- `scripts/clockify_portfolio_review.py`
- `tests/test_clockify_portfolio_post.py`
- `tests/test_portfolio_review.py`
- `.superpowers/sdd/2026-08-18-calendly-final-review-corrections/task-1-report.md`

### RED evidence

```text
python3 -m unittest discover -s tests -p 'test_clockify_portfolio_post.py' -v
```

The new legacy receipt test failed because the generated plan reported
`approved_duration_seconds == 0`, rather than the timestamp-supported 60
seconds.

```text
python3 -m unittest discover -s tests -p 'test_portfolio_review.py' -v
```

The new one-minute legacy package test failed with
`PortfolioReviewError: group source proposals mix exact and legacy durations`.

### GREEN and final verification

After the minimal fixes, the four owned focused suites passed:

```text
test_work_accounting_pipeline.py: 50 tests
test_portfolio_review.py: 15 tests
test_portfolio_quality.py: 30 tests
test_clockify_portfolio_post.py: 11 tests
```

The final command completed with `Ran 683 tests in 2.377s` and
`OK (skipped=2)`:

```text
python3 -m unittest discover -s tests -v
python3 -m json.tool schemas/work-accounting-result-v1.json >/dev/null
git diff --check
```

### Self-review

- Every posting row now receives its baseline from merged timestamp bounds,
  independent of whether its source declared exact seconds.
- A legacy source does not synthesize seconds in package output, while a source
  declaration with seconds remains on the exact path. Mixed source declarations
  continue to block in accounting.
- The fix touches only the four code/test files above. Unrelated untracked
  private artifacts were not staged or changed.

Fix-round code commit: `a47b284426b42c1327bc9903621c07ca4c1aeb21`
