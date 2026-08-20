# Clockify Receipt Re-Derivation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Clockify portfolio retries idempotent by accepting prior receipt entries only when immutable approval plus a fresh live ledger independently re-derive their exact timestamps and semantics.

**Architecture:** Keep `_plans()` output immutable, parse prior receipts only into typed candidate identities, provisionally remove candidate live entries from blockers, then run the existing boundary algorithm from fresh inputs. Validate each provisional candidate against that derived output before resolving other exact entries or conflicts, and emit digests for the full live snapshot, actual blocker snapshot, and freshly derived adjustments.

**Tech Stack:** Python 3 standard library (`dataclasses`, `hashlib`, `json`, `unittest`), existing Clockify posting CLI and synthetic mocked Clockify fixtures.

**Spec:** `docs/superpowers/specs/2026-08-20-clockify-receipt-rederivation-design.md`

## Global Constraints

- Original approved plans remain immutable; receipt-supplied bounds never mutate them.
- Fresh Clockify readback normalized by `_live_entries()` is authoritative.
- Prior receipts provide only provisional posting keys and Clockify entry IDs until post-derivation validation succeeds.
- Exact-second and explicit legacy minute-only portfolios remain supported through `_plans()`.
- Aggregate restorations of 60 seconds or more are valid when `_align_subminute_boundaries()` derives them.
- Existing historical receipt files are never migrated or modified in place.
- Tests use synthetic local fixtures only and must make no network calls or external mutations.
- No Clockify write, shared-report mutation, Slack message, push, merge, deployment, schedule, credential, or permission change is authorized by this plan.
- Preserve private untracked `state/`, `reports/`, `.serena/`, and `CLAUDE.md` artifacts.

---

## File Structure

- `scripts/clockify_post_approved_portfolio.py`: owns receipt candidate parsing, live resolution, deterministic derivation integration, snapshot digests, conflict checks, and receipt emission. Keep these cohesive posting-gate responsibilities in the existing module rather than introducing a one-use abstraction.
- `tests/test_clockify_portfolio_post.py`: owns pure trust-boundary regressions and end-to-end synthetic retry/partial-success coverage.
- `README.md`: documents that `--prior-receipt` is an idempotency hint verified against fresh Clockify readback, plus the new receipt evidence fields.

### Task 1: Replace Receipt-Driven Plan Mutation with Pure Candidate Validation

**Files:**
- Modify: `scripts/clockify_post_approved_portfolio.py`
- Modify: `tests/test_clockify_portfolio_post.py`

**Interfaces:**
- Consumes: immutable approved plans returned by `_plans()` and normalized live entries returned by `_live_entries()`.
- Produces: `PriorReceiptCandidate`, `_prior_receipt_candidates(path, portfolio_sha, approved_keys)`, `_resolve_prior_candidates(candidates, live, approved_by_key)`, and `_validate_prior_candidates(candidates_by_key, derived_by_key, receipt_candidates)` with the exact signatures below.

- [ ] **Step 1: Add failing parser tests for strict candidate identity and uniqueness**

Add `dataclasses`-compatible expectations that prove receipts cannot introduce unknown keys, duplicate keys, duplicate nonempty Clockify IDs, absent IDs, or malformed audit values. Use a helper that writes a synthetic receipt and call the new parser directly:

```python
def test_prior_candidate_parser_rejects_duplicate_clockify_entry_id(self) -> None:
    approved = {("review-a", 1), ("review-b", 1)}
    receipt = {
        "portfolio_sha256": "approved-sha",
        "created": [
            {"review_id": "review-a", "segment_index": 1, "clockify_entry_id": "entry-1"},
            {"review_id": "review-b", "segment_index": 1, "clockify_entry_id": "entry-1"},
        ],
        "already_existing": [],
    }
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "receipt.json"
        path.write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaisesRegex(poster.PortfolioPostError, "duplicate Clockify entry ID"):
            poster._prior_receipt_candidates(path, "approved-sha", approved)
```

Add sibling tests for duplicate posting keys across `created` and `already_existing`, unknown keys, missing/blank `clockify_entry_id`, non-list dispositions, non-object items, wrong portfolio digest, invalid optional timestamp strings, and invalid/non-positive optional `duration_seconds`. Assert a valid item preserves its disposition and optional audit fields without interpreting them as authorization.

- [ ] **Step 2: Run the parser tests and confirm the new interface is absent**

Run:

```bash
python3 -m unittest \
  tests.test_clockify_portfolio_post.ClockifyPortfolioPostTests.test_prior_candidate_parser_rejects_duplicate_clockify_entry_id -v
```

Expected: `ERROR` because `_prior_receipt_candidates` is not defined.

- [ ] **Step 3: Implement the immutable candidate type and strict parser**

Import `dataclass` and `AbstractSet`, `Sequence`, then add:

```python
@dataclass(frozen=True)
class PriorReceiptCandidate:
    review_id: str
    segment_index: int
    clockify_entry_id: str
    disposition: str
    recorded_start: str | None
    recorded_end: str | None
    recorded_duration_seconds: int | None


def _prior_receipt_candidates(
    path: Path,
    portfolio_sha: str,
    approved_keys: AbstractSet[tuple[str, int]],
) -> Sequence[PriorReceiptCandidate]:
    prior = _read(path)
    if not isinstance(prior, Mapping) or prior.get("portfolio_sha256") != portfolio_sha:
        raise PortfolioPostError("prior posting receipt does not match the approved portfolio")
    candidates: list[PriorReceiptCandidate] = []
    seen_keys: set[tuple[str, int]] = set()
    seen_ids: set[str] = set()
    for disposition in ("created", "already_existing"):
        items = prior.get(disposition, [])
        if not isinstance(items, list):
            raise PortfolioPostError("prior posting receipt items must be a list")
        for item in items:
            if not isinstance(item, Mapping):
                raise PortfolioPostError("prior posting receipt contains an invalid item")
            key = _receipt_key(item, kind="receipt")
            if key in seen_keys:
                raise PortfolioPostError("prior posting receipt contains a duplicate receipt key")
            if key not in approved_keys:
                raise PortfolioPostError("prior posting receipt contains an unknown approved key")
            entry_id = str(item.get("clockify_entry_id") or "").strip()
            if not entry_id:
                raise PortfolioPostError("prior posting receipt lacks a Clockify entry ID")
            if entry_id in seen_ids:
                raise PortfolioPostError("prior posting receipt contains a duplicate Clockify entry ID")
            recorded_start = _utc(str(item["start"])) if "start" in item else None
            recorded_end = _utc(str(item["end"])) if "end" in item else None
            raw_duration = item.get("duration_seconds")
            if raw_duration is not None and (
                isinstance(raw_duration, bool)
                or not isinstance(raw_duration, int)
                or raw_duration <= 0
            ):
                raise PortfolioPostError("prior posting receipt contains invalid duration seconds")
            candidates.append(PriorReceiptCandidate(
                key[0], key[1], entry_id, disposition,
                recorded_start, recorded_end, raw_duration,
            ))
            seen_keys.add(key)
            seen_ids.add(entry_id)
    return tuple(candidates)
```

Use the code above as the minimal implementation. Do not parse or validate prior `boundary_adjustments`, and do not mutate plans.

- [ ] **Step 4: Add failing live-resolution tests for ID ambiguity, absence, and semantic forgery**

Create complete approved plans with `project_id`, sorted `tag_ids`, and normalized `description`, then exercise the resolver:

```python
def test_prior_candidate_cannot_remove_unrelated_blocker_by_id(self) -> None:
    candidate = poster.PriorReceiptCandidate(
        "review-a", 1, "blocker-1", "created", None, None, None
    )
    approved = {("review-a", 1): {
        "review_id": "review-a", "segment_index": 1,
        "project_id": "project-a", "tag_ids": ["tag-a"],
        "description": "AA — Approved work",
    }}
    live = [{
        "id": "blocker-1", "start": "2026-08-14T10:00:30Z",
        "end": "2026-08-14T10:01:30Z", "project_id": "project-other",
        "tag_ids": ["tag-a"], "description": "AA — Approved work",
    }]
    with self.assertRaisesRegex(poster.PortfolioPostError, "semantic fields"):
        poster._resolve_prior_candidates([candidate], live, approved)
```

Add cases rejecting duplicate nonempty live IDs, empty live IDs, a candidate ID absent from readback, project mismatch, tag mismatch, and description mismatch. Add a positive case asserting that only the semantically matching candidate entry is removed and all unrelated live entries remain in blocker order.

- [ ] **Step 5: Run the focused resolver tests and confirm RED**

Run:

```bash
python3 -m unittest tests.test_clockify_portfolio_post -k prior_candidate -v
```

Expected: failures for the missing resolver while the parser cases pass.

- [ ] **Step 6: Implement pure provisional live resolution**

Add:

```python
def _resolve_prior_candidates(
    candidates: Iterable[PriorReceiptCandidate],
    live: Iterable[Mapping[str, Any]],
    approved_by_key: Mapping[tuple[str, int], Mapping[str, Any]],
) -> tuple[dict[tuple[str, int], dict[str, Any]], list[dict[str, Any]]]:
    live_rows = [dict(entry) for entry in live]
    live_by_id: dict[str, dict[str, Any]] = {}
    for entry in live_rows:
        entry_id = str(entry.get("id") or "").strip()
        if not entry_id:
            raise PortfolioPostError("fresh Clockify readback contains an empty entry ID")
        if entry_id in live_by_id:
            raise PortfolioPostError("fresh Clockify readback contains a duplicate entry ID")
        live_by_id[entry_id] = entry
    resolved: dict[tuple[str, int], dict[str, Any]] = {}
    removed_ids: set[str] = set()
    for candidate in candidates:
        key = (candidate.review_id, candidate.segment_index)
        if key in resolved:
            raise PortfolioPostError("prior posting receipt contains a duplicate receipt key")
        entry = live_by_id.get(candidate.clockify_entry_id)
        if entry is None:
            raise PortfolioPostError("prior Clockify entry is absent from fresh readback")
        approved = approved_by_key[key]
        semantic_match = (
            str(entry.get("project_id") or "") == str(approved.get("project_id") or "")
            and sorted(str(value) for value in entry.get("tag_ids", []))
            == sorted(str(value) for value in approved.get("tag_ids", []))
            and str(entry.get("description") or "").strip()
            == str(approved.get("description") or "").strip()
        )
        if not semantic_match:
            raise PortfolioPostError("prior Clockify entry semantic fields differ from approval")
        resolved[key] = entry
        removed_ids.add(candidate.clockify_entry_id)
    return resolved, [entry for entry in live_rows if entry["id"] not in removed_ids]
```

Use the code above as the minimal implementation. Candidate bounds are deliberately not compared here; the returned blocker list excludes exactly the resolved candidate IDs and nothing else.

- [ ] **Step 7: Add failing post-derivation validation tests**

Cover exact derived/live acceptance, same-duration relocation forward and backward, changed blocker state producing different derived bounds, receipt audit bounds contradicting live readback, receipt duration contradicting live duration, and a missing derived key. Duplicate derived posting keys are rejected while `run()` builds `derived_by_key` in Task 2 because a mapping cannot represent that invalid input:

```python
def test_prior_candidate_rejects_same_duration_relocation_after_derivation(self) -> None:
    key = ("review-a", 1)
    live = {key: {
        "id": "entry-1", "start": "2026-08-14T10:01:00Z",
        "end": "2026-08-14T10:11:00Z", "project_id": "project-a",
        "tag_ids": ["tag-a"], "description": "AA — Approved work",
    }}
    derived = {key: {
        "review_id": "review-a", "segment_index": 1,
        "start": "2026-08-14T10:00:00Z", "end": "2026-08-14T10:10:00Z",
        "duration_seconds": 600, "project_id": "project-a",
        "tag_ids": ["tag-a"], "description": "AA — Approved work",
    }}
    receipts = {key: poster.PriorReceiptCandidate(
        "review-a", 1, "entry-1", "created", None, None, None
    )}
    with self.assertRaisesRegex(poster.PortfolioPostError, "freshly derived plan"):
        poster._validate_prior_candidates(live, derived, receipts)
```

- [ ] **Step 8: Implement exact post-derivation validation**

Add:

```python
def _validate_prior_candidates(
    candidates_by_key: Mapping[tuple[str, int], Mapping[str, Any]],
    derived_by_key: Mapping[tuple[str, int], Mapping[str, Any]],
    receipt_candidates: Mapping[tuple[str, int], PriorReceiptCandidate],
) -> dict[tuple[str, int], dict[str, Any]]:
    accepted: dict[tuple[str, int], dict[str, Any]] = {}
    if set(candidates_by_key) != set(receipt_candidates):
        raise PortfolioPostError("prior candidate identity sets do not match")
    for key, receipt_candidate in receipt_candidates.items():
        live = candidates_by_key[key]
        derived = derived_by_key.get(key)
        if derived is None:
            raise PortfolioPostError("prior candidate has no freshly derived plan")
        if not _exact(derived, live):
            raise PortfolioPostError("prior Clockify entry differs from its freshly derived plan")
        live_seconds = int((_parse(str(live["end"])) - _parse(str(live["start"]))).total_seconds())
        if live_seconds <= 0 or live_seconds != int(derived["duration_seconds"]):
            raise PortfolioPostError("prior Clockify entry duration differs from derivation")
        if (
            receipt_candidate.recorded_start is not None
            and not _same_instant(receipt_candidate.recorded_start, live["start"])
        ) or (
            receipt_candidate.recorded_end is not None
            and not _same_instant(receipt_candidate.recorded_end, live["end"])
        ):
            raise PortfolioPostError("prior receipt audit bounds contradict fresh readback")
        if (
            receipt_candidate.recorded_duration_seconds is not None
            and receipt_candidate.recorded_duration_seconds != live_seconds
        ):
            raise PortfolioPostError("prior receipt audit duration contradicts fresh readback")
        accepted[key] = dict(live)
    return accepted
```

Use the code above as the minimal implementation. Return copied live entries keyed by posting key; never adjust either input mapping.

- [ ] **Step 9: Replace obsolete mutation tests and run the pure contract suite**

Delete tests that call `_apply_prior_receipt()` or authorize bounds through `_adjustment_digest()`. Preserve their legitimate legacy/exact compatibility assertions by expressing them through parser/resolver/validator tests. Run:

```bash
python3 -m unittest tests.test_clockify_portfolio_post -v
```

Expected: all focused posting tests pass; no test references `_apply_prior_receipt`.

- [ ] **Step 10: Commit the pure trust-boundary implementation**

```bash
git add scripts/clockify_post_approved_portfolio.py tests/test_clockify_portfolio_post.py
git commit -m "fix: validate replay receipts against live derivation"
```

### Task 2: Integrate Fresh Derivation, Evidence Digests, and Retry Idempotency

**Files:**
- Modify: `scripts/clockify_post_approved_portfolio.py`
- Modify: `tests/test_clockify_portfolio_post.py`
- Modify: `README.md`

**Interfaces:**
- Consumes: Task 1's three pure receipt functions and existing `_plans()`, `_live_entries()`, `_align_subminute_boundaries()`, `_exact()`, and `_receipt_item()`.
- Produces: `_normalized_snapshot_sha256(entries) -> str`, `_adjustment_digest(portfolio_sha, blocker_snapshot_sha256, adjustments) -> str`, and a `run()` receipt containing `boundary_adjustment_algorithm`, `live_snapshot_sha256`, `blocker_snapshot_sha256`, and freshly derived `boundary_adjustments_sha256`.

- [ ] **Step 1: Add failing digest determinism and binding tests**

Prove live ordering and tag ordering do not affect snapshot identity, while any ID, bound, route, tag, or description change does. Prove the adjustment digest changes when the blocker snapshot changes:

```python
def test_adjustment_digest_is_bound_to_blocker_snapshot(self) -> None:
    adjustments = [{
        "review_id": "review-a", "segment_index": 1,
        "original_start": "2026-08-14T10:00:00Z",
        "original_end": "2026-08-14T10:10:00Z",
        "posted_start": "2026-08-14T10:00:30Z",
        "posted_end": "2026-08-14T10:10:30Z",
        "algorithm": poster.BOUNDARY_ADJUSTMENT_ALGORITHM,
    }]
    left = poster._adjustment_digest("portfolio-sha", "blockers-a", adjustments)
    right = poster._adjustment_digest("portfolio-sha", "blockers-b", adjustments)
    self.assertNotEqual(left, right)
```

- [ ] **Step 2: Run the digest tests and confirm RED**

Run:

```bash
python3 -m unittest tests.test_clockify_portfolio_post -k digest -v
```

Expected: failures because snapshot hashing is absent and `_adjustment_digest()` lacks the blocker digest parameter.

- [ ] **Step 3: Implement canonical live/blocker snapshot hashing**

Implement `_normalized_snapshot_sha256()` by copying only `id`, normalized `start`, normalized `end`, `project_id`, sorted `tag_ids`, and stripped `description`; sort canonical rows by `(start, end, id, project_id, tag_ids, description)`; serialize with UTF-8, sorted keys, and compact separators; hash with SHA-256. Change `_adjustment_digest()` to include `portfolio_sha256`, `BOUNDARY_ADJUSTMENT_ALGORITHM`, `blocker_snapshot_sha256`, and the canonical sorted adjustment list.

- [ ] **Step 4: Add failing end-to-end retry and attack regressions around `run()`**

Use mocked `_paged()` fixtures and temporary approved artifacts to cover:

1. Dry run followed by retry with unchanged blockers yields equal plans, adjustments, and evidence digests.
2. A prior created entry is provisionally removed, freshly re-derived, validated, and emitted once under `already_existing` without POST.
3. A prior receipt naming an unrelated blocker ID fails before derivation can remove that blocker.
4. Two individually valid 30-second and 40-second trims restore 70 seconds into a later segment and replay successfully.
5. Changed blocker state that alters a candidate's freshly derived bounds fails closed.
6. A non-receipt exact live entry satisfies one plan without a write.
7. Multiple exact entries for one derived plan fail closed.
8. Remaining overlap conflicts include review ID, approved window, live entry ID, live window, and descriptions.
9. Exact-second and legacy minute-only portfolios follow the same derivation/replay flow.

For partial success, patch `_request()` so the first missing plan returns an entry ID and the second POST raises `PortfolioPostError`; assert the interrupted receipt contains only the first created item. On retry, provide that first entry through fresh readback and assert only the second plan is posted:

```python
self.assertEqual(["entry-first"], [item["clockify_entry_id"] for item in interrupted["created"]])
self.assertEqual(1, retry_post.call_count)
self.assertEqual("complete", completed["status"])
self.assertEqual(2, len(completed["created"] + completed["already_existing"]))
```

- [ ] **Step 5: Run the integration regressions and confirm RED**

Run:

```bash
python3 -m unittest tests.test_clockify_portfolio_post -v
```

Expected: the new replay tests fail because `run()` still applies prior receipt bounds before live readback and reuses prior adjustments.

- [ ] **Step 6: Rewire `run()` to derive exclusively from approval plus fresh state**

Implement this order exactly:

```python
approved_plans = _plans(portfolio, _resolved_routes(routing, project_ids, tag_ids))
approved_by_key = {_receipt_key(plan, kind="approved plan"): plan for plan in approved_plans}
if len(approved_by_key) != len(approved_plans):
    raise PortfolioPostError("approved portfolio contains duplicate posting keys")

live = _live_entries(
    workspace, user, api_key, approved_plans, timeout_seconds=timeout_seconds
)
candidates = (
    _prior_receipt_candidates(prior_path.resolve(), portfolio_sha, set(approved_by_key))
    if prior_path is not None else ()
)
receipt_candidates = {
    (candidate.review_id, candidate.segment_index): candidate for candidate in candidates
}
candidate_live, blockers = _resolve_prior_candidates(candidates, live, approved_by_key)
```

Resolve exact matches only from `blockers`, reject multiple matches, and pass a fresh `dict(plan)` copy list plus those exact keys into `_align_subminute_boundaries()`. Build a unique `derived_by_key`, validate provisional candidates only after derivation, merge them into the satisfied `exact` mapping, then resolve adjusted exact matches among remaining blockers. Check overlaps only for unfulfilled plans against remaining blockers. Never copy prior receipt adjustments or bounds into `plans`.

- [ ] **Step 7: Emit fresh derivation evidence and preserve interrupted-run safety**

Compute `live_snapshot_sha256` from the entire normalized live readback and `blocker_snapshot_sha256` from the exact list passed into `_align_subminute_boundaries()`. Emit:

```python
{
    "boundary_adjustment_algorithm": BOUNDARY_ADJUSTMENT_ALGORITHM,
    "live_snapshot_sha256": live_snapshot_sha256,
    "blocker_snapshot_sha256": blocker_snapshot_sha256,
    "boundary_adjustments": boundary_adjustments,
    "boundary_adjustments_sha256": _adjustment_digest(
        portfolio_sha, blocker_snapshot_sha256, boundary_adjustments
    ),
}
```

Ensure dry-run, running, interrupted, recovered, and complete receipts all use derived plans and these same evidence fields. On an execute-mode ambiguous POST failure, keep the existing fresh readback recovery behavior; if no exact derived entry appears, atomically persist `interrupted` and fail. A subsequent retry must treat persisted IDs only as candidates and post only still-unfulfilled derived plans.

- [ ] **Step 8: Run focused posting, replay, and process-integration suites**

Run:

```bash
python3 -m unittest \
  tests.test_clockify_portfolio_post \
  tests.test_portfolio_review \
  tests.test_portfolio_replay \
  tests.test_process_integration -v
```

Expected: all tests pass with no network access.

- [ ] **Step 9: Document the replay trust boundary and evidence aliases**

Update the `clockify_post_approved_portfolio.py` README example with a short paragraph: `--prior-receipt` identifies candidate live entries but never authorizes receipt timestamps; every retry reads Clockify again, excludes candidates provisionally, re-derives approved bounds, and fails if live readback differs. List `boundary_adjustment_algorithm`, `live_snapshot_sha256`, `blocker_snapshot_sha256`, and `boundary_adjustments_sha256` as receipt audit evidence, and state that an interrupted receipt is safe to retry but not proof that its entries remain valid.

- [ ] **Step 10: Run the complete repository test suite**

Run the repository's established full-suite command from the exact checkout:

```bash
python3 -m unittest discover -s tests -v
```

Expected: all tests pass; the two established skips remain the only skips unless the repository baseline has intentionally changed and is separately explained.

- [ ] **Step 11: Inspect the final diff and verify repository boundaries**

Run:

```bash
git diff --check
git status --short
git diff -- scripts/clockify_post_approved_portfolio.py tests/test_clockify_portfolio_post.py README.md
```

Expected: no whitespace errors; only the three planned tracked files are modified; private untracked artifacts remain unmodified and unstaged.

- [ ] **Step 12: Commit the integrated deterministic replay flow**

```bash
git add scripts/clockify_post_approved_portfolio.py tests/test_clockify_portfolio_post.py README.md
git commit -m "fix: rederive Clockify retries from live state"
```

- [ ] **Step 13: Perform independent review and final verification**

Review the committed diff against every acceptance criterion in the spec. Re-run the focused suite and full suite after any correction, verify `git diff --check`, and record exact test totals and commit SHAs. Do not push or perform a live Clockify execution; those remain separately guarded actions.
