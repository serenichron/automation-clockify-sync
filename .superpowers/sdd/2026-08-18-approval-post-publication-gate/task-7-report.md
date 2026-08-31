# Task 7 — Publication end-to-end fixtures and operations

## Scope and changed paths

- `tests/fixtures/reconciliation/publication-routine/manifest.json`
- `tests/fixtures/reconciliation/publication-backlog/manifest.json`
- `tests/test_publication_end_to_end.py`
- `README.md`
- `clockify-process-acceptance.md`
- `multica-clockify-autopilot-prompt.md`

No production transport, service/timer/launchd artifact, external network call,
Slack/Sheets/Clockify mutation, credential change, push, merge, or deployment
was performed.

## RED evidence

Initial Task 7 tests were run with the real preparation path but without a
publication authorization. The command was:

```bash
python3 -m unittest discover -s tests -p 'test_publication_end_to_end.py' -v
```

It failed as intended with both expectations showing `published` versus actual
`publication_deferred`. The coordinator had reached the prepared state and the
scheduled adapter deferred because authorization was absent. Earlier harness
construction errors (non-monotonic fixture timestamps) were corrected before
accepting this RED result; the recorded RED failure was the intended assertion
mismatch, not an import or fixture error.

## GREEN implementation and contracts exercised

The final synthetic harness does not shortcut expected states. It builds and
uses the real `ReconciliationCoordinator`, `ApprovalReceiptStore`,
`PostEventStore`, `normalize_readback`, currency conversion,
`prepare_publication`, `authorize_publication`, `PublicationReceiptStore`, and
scheduled finance adapter. The only fake is `RecordingTransport`, the external
report/Slack boundary.

Routine fixture coverage:

- Two contiguous one-day slices form a two-day period.
- The consumed posting receipt has exact terminal events and exact final API /
  shared-report readbacks.
- The report is read back before a single Slack upsert; a published rerun makes
  no additional transport call.

Backlog fixture coverage:

- Contains one Calendly-only recording and one Fathom/Calendly duplicate.
- First proves a real missing required slice defers publication; the next stage
  proves complete slices still defer while Desktop coverage lacks immutable
  approval; the final stage binds the approved Desktop limitation event.
- Uses a real `recovered_after_ambiguous_response` terminal POST event, retains
  correction revision `2` in fixture data, preserves USD/EUR native buckets,
  and makes the external Slack step fail once after report receipt persistence.
- The retry publishes without a second report update; the following rerun is
  idempotent.

Documentation now names manifest paths, fail-closed state meanings, the ECB
EUR-base/four-calendar-day/half-up-cent rule, report-readback-before-Slack
ordering, idempotency, and the separate approval boundary. It also changes the
future-autopilot Calendly statement to first-class required coverage while
retaining only the bounded historical-recovery exception.

## Verification evidence

Focused Task 7 command after GREEN:

```bash
python3 -m unittest discover -s tests -p 'test_publication_end_to_end.py' -v
```

Result: `Ran 2 tests ... OK`.

Relevant integration set, run with repository-local discovery because
`tests/__init__.py` intentionally does not exist:

```bash
python3 -m unittest discover -s tests -p 'test_posting_receipts.py' -v
python3 -m unittest discover -s tests -p 'test_clockify_portfolio_post.py' -v
python3 -m unittest discover -s tests -p 'test_clockify_readback.py' -v
python3 -m unittest discover -s tests -p 'test_clockify_currency.py' -v
python3 -m unittest discover -s tests -p 'test_publication_gate.py' -v
python3 -m unittest discover -s tests -p 'test_publication_adapter_contract.py' -v
python3 -m unittest discover -s tests -p 'test_clockify_finance_report_adapter.py' -v
python3 -m unittest discover -s tests -p 'test_publication_end_to_end.py' -v
```

Result: all eight commands exited `0` (20, 64, 41, 9, 37, 14, 8, and 2 tests).

Full verification:

```bash
python3 -m unittest discover -s tests -v
git diff --check
```

Result: full discovery exited `0`, `Ran 933 tests ... OK (skipped=2)`; diff
check exited `0`. Fixture JSON parsing also succeeded. `python3 -m py_compile
tests/test_publication_end_to_end.py` was not used as evidence because this
workspace mounts `tests/__pycache__` read-only; unittest discovery already
imported and executed the module successfully.

## Self-review, concerns, blockers

Reviewed the owned diff and fixture parsing. The fixture runner intentionally
keeps all generated receipts in `TemporaryDirectory`; no private state/runs
directory was read, listed, or changed. The transport fake cannot contact any
external system and the production adapter remains unconfigured.

No functional blocker remains. The only environment concern is the read-only
`tests/__pycache__` mount for an optional direct `py_compile` output; it does
not affect the required unittest verification.
