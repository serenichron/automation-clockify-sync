# Calendly Final Review Corrections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the five load-bearing residuals from the Calendly whole-plan review so exact recording seconds, standalone collection configuration, and participant reconciliation are production-correct.

**Architecture:** Treat `duration_seconds` and timestamp bounds as authoritative throughout accounting, review, quality, posting, and receipts; `duration_minutes` remains a derived display value and may be zero for a positive sub-minute segment. Share the existing file-based Calendly environment semantics with the standalone CLI without network access during preflight. Represent participant identities per person with typed tokens and compare people by compatible stable overlap rather than exact token-set equality.

**Tech Stack:** Python 3 standard library, `unittest`, JSON Schema, existing checkpoint/evidence/reconciliation modules.

**Spec:** `docs/superpowers/specs/2026-08-17-clockify-reconciliation-publication-manifest-design.md`

## Global Constraints

- Scheduled Calendly events without recordings are never billable.
- Calendly-only recordings retain their complete timestamp-evidenced duration, including seconds.
- Fathom/Calendly duplicates become one canonical meeting; ambiguous or contradictory evidence remains quarantined.
- Fallback matching requires identical non-Vlad participant people and start/end differences no greater than 300 seconds.
- Organizer and recorder identities are provenance, not participant evidence.
- Immutable ledger timezone and member identities remain digest-bound.
- Historical minute-only artifacts remain readable; new exact-second artifacts fail closed on inconsistent bounds or totals.
- Preflight performs no network request and collection remains read-only and checkpointed.
- Tests use synthetic fixtures only. No Calendly, Clockify, Sheets, Slack, Multica, deployment, push, merge, credential, permission, or schedule mutation.

---

### Task 1: Make Exact Seconds Authoritative End to End

**Files:**
- Modify: `scripts/work_accounting_pipeline.py`
- Modify: `scripts/clockify_portfolio_review.py`
- Modify: `scripts/clockify_portfolio_quality.py`
- Modify: `scripts/clockify_post_approved_portfolio.py`
- Modify: `schemas/work-accounting-result-v1.json`
- Test: `tests/test_work_accounting_pipeline.py`
- Test: `tests/test_portfolio_review.py`
- Test: `tests/test_portfolio_quality.py`
- Test: `tests/test_clockify_portfolio_post.py`

**Interfaces:**
- Consumes: second-precision proposal bounds and positive `duration_seconds` from canonical meeting accounting.
- Produces: exact-second-conserving proposals, reviewed rows, posting plans, adjusted bounds, and receipts; `duration_minutes = duration_seconds // 60` is display-only and may be `0` when seconds are positive.

- [ ] **Step 1: Add failing sub-minute accounting and schema regressions**

Add `test_subminute_final_meeting_segment_preserves_positive_seconds` to `tests/test_work_accounting_pipeline.py`. Build a canonical final segment with a 43-second interval and assert the emitted proposal has exact second bounds, `duration_seconds == 43`, and `duration_minutes == 0`. Validate that proposal against `schemas/work-accounting-result-v1.json`; the current schema must fail because `duration_minutes` has minimum `1`.

- [ ] **Step 2: Verify the accounting regression fails for the reviewed reason**

Run: `python3 -m unittest discover -s tests -p 'test_work_accounting_pipeline.py' -v`

Expected: FAIL because the positive 43-second proposal is rejected or the schema disallows zero display minutes.

- [ ] **Step 3: Permit positive sub-minute proposals without weakening exact duration validation**

Change the proposal schema's `duration_minutes` minimum to `0` while retaining `duration_seconds` minimum `1`. Keep proposal bounds at second precision and derive both values from the same `start`/`end` interval:

```python
seconds = int((end - start).total_seconds())
proposal["duration_seconds"] = seconds
proposal["duration_minutes"] = seconds // 60
```

Reject non-positive bounds; never infer authoritative seconds from display minutes when exact bounds exist.

- [ ] **Step 4: Add failing aggregate allocation regressions**

In `tests/test_portfolio_review.py`, add `test_exact_second_split_does_not_create_floor_minute_exclusion` using a 301-second source split into 151 and 150 seconds. Assert `source_seconds == review_seconds == 301`, `excluded_seconds == 0`, no exclusion reason is required, and aggregate display minutes are derived from total seconds (`5`) rather than the sum of segment floors (`4`).

In `tests/test_portfolio_quality.py`, add `test_positive_subminute_segment_is_valid_when_seconds_match_bounds` and assert a 43-second row/segment is not blocked for zero display minutes.

Run: `python3 -m unittest discover -s tests -p 'test_portfolio_review.py' -v` and `python3 -m unittest discover -s tests -p 'test_portfolio_quality.py' -v`.

Expected: FAIL on minute-based exclusion/structural checks.

- [ ] **Step 5: Make review and quality conservation second-first**

In `clockify_portfolio_review.py`, compute conservation and exclusion from exact seconds whenever bounds/seconds exist:

```python
excluded_seconds = source_seconds - review_seconds
source_minutes = source_seconds // 60
review_minutes = review_seconds // 60
excluded_minutes = excluded_seconds // 60
exclusion_reasons = (
    _exclusion_reasons(source_activities, exceptions, omissions)
    if excluded_seconds else []
)
```

Do not require row display minutes to equal the sum of independently floored segment minutes. Instead require row seconds to equal segment seconds, validate each exact segment against its bounds, and derive row display minutes from aggregate row seconds. Preserve the legacy minute-only branch only when exact seconds are absent.

In `clockify_portfolio_quality.py`, accept `duration_minutes == 0` only when positive `duration_seconds` exactly matches positive bounds. Continue blocking zero/non-positive seconds, mismatched bounds, and mixed exact/legacy segment declarations.

- [ ] **Step 6: Add failing posting-boundary and receipt regressions**

In `tests/test_clockify_portfolio_post.py`, add:

```python
def test_boundary_adjustment_recomputes_exact_duration_fields(self):
    # Use the existing overlap-adjustment fixture that changes 630/600 seconds
    # to 613/617 seconds.
    adjusted, _changes = poster._adjust_boundaries(plans, live)
    self.assertEqual([613, 617], [item["duration_seconds"] for item in adjusted])
    self.assertEqual([10, 10], [item["duration_minutes"] for item in adjusted])
    self.assertEqual(1230, sum(item["duration_seconds"] for item in adjusted))
```

Also assert `_receipt_item()` copies the recomputed values. Run the focused posting suite and verify it fails with stale `630/600` seconds.

- [ ] **Step 7: Recompute posting durations after every boundary mutation**

After `_adjust_boundaries()` finishes shifting, trimming, and restoring bounds, recompute for every non-exact plan:

```python
seconds = int((_parse(plan["end"]) - _parse(plan["start"])).total_seconds())
if seconds <= 0:
    raise PortfolioPostError("adjusted portfolio block is not positive")
plan["duration_seconds"] = seconds
plan["duration_minutes"] = seconds // 60
```

For a review row spanning multiple adjusted plans, assert total seconds still equal the approved row's seconds. Receipts must copy only recomputed plan values.

- [ ] **Step 8: Run focused and full verification, then commit**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_work_accounting_pipeline.py' -v
python3 -m unittest discover -s tests -p 'test_portfolio_review.py' -v
python3 -m unittest discover -s tests -p 'test_portfolio_quality.py' -v
python3 -m unittest discover -s tests -p 'test_clockify_portfolio_post.py' -v
python3 -m unittest discover -s tests -v
python3 -m json.tool schemas/work-accounting-result-v1.json >/dev/null
git diff --check
```

Commit only owned tracked paths with message: `fix: conserve exact recording seconds`.

---

### Task 2: Load Standalone Calendly Configuration Canonically

**Files:**
- Create: `scripts/provider_env.py`
- Modify: `scripts/clockify_sync_collect.py`
- Modify: `scripts/calendly_collector.py`
- Test: `tests/test_calendly_semantic_evidence.py`
- Test: `tests/test_collector_burst_context.py`

**Interfaces:**
- Consumes: `CALENDLY_ENV_FILE`, default `~/.config/serenichron/calendly.env`, direct process environment, and existing required gateway keys.
- Produces: one shared, deterministic file parser/candidate resolver; direct environment values override file values; `_missing` is recomputed after merging.

- [ ] **Step 1: Add failing standalone file-loading regressions**

In `tests/test_calendly_semantic_evidence.py`, add one CLI test that writes a temporary env file containing all read-only gateway keys, sets only `CALENDLY_ENV_FILE`, patches the HTTP JSON boundary with a complete synthetic response, runs `collect`, and asserts a complete recording document plus a persisted checkpoint. Add a preflight test with the same env file that patches the network boundary to raise if called and asserts `status == "ready"`.

Run: `python3 -m unittest discover -s tests -p 'test_calendly_semantic_evidence.py' -v`.

Expected: FAIL with `capability_unavailable` because the standalone CLI reads only `os.environ` gateway keys.

- [ ] **Step 2: Extract the shared pure env-file contract**

Create `scripts/provider_env.py` with pure helpers:

```python
def home_candidates() -> list[Path]:
    homes = [Path.home()]
    try:
        homes.append(Path(pwd.getpwuid(os.getuid()).pw_dir))
    except (KeyError, OSError):
        pass
    return list(dict.fromkeys(homes))

def calendly_env_candidates(
    environment: Mapping[str, str] = os.environ,
) -> list[str]:
    candidates = [environment.get("CALENDLY_ENV_FILE", "")]
    candidates.extend(
        str(home / ".config/serenichron/calendly.env")
        for home in home_candidates()
    )
    return list(dict.fromkeys(candidates))

def load_env_file(
    candidates: Sequence[str], required_keys: Sequence[str],
) -> dict[str, Any]:
    values: dict[str, str] = {}
    used: str | None = None
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            used = candidate
            for raw in Path(candidate).read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
            break
    missing = [key for key in required_keys if not values.get(key)]
    return {"_env_file": used or "missing", "_missing": missing, **values}

def merged_provider_environment(
    file_values: Mapping[str, Any],
    environment: Mapping[str, str],
    required_keys: Sequence[str],
) -> dict[str, Any]:
    merged = {
        str(key): str(value)
        for key, value in file_values.items()
        if key not in {"_env_file", "_missing"} and str(value)
    }
    for key in required_keys:
        if environment.get(key):
            merged[key] = environment[key]
    missing = [key for key in required_keys if not merged.get(key)]
    return {
        "_env_file": file_values.get("_env_file", "missing"),
        "_missing": missing,
        **merged,
    }
```

`merged_provider_environment()` excludes metadata keys from provider credentials, overlays direct environment values on file values, then recomputes `_missing` from the merged result. It performs no network operation and never logs secret values.

- [ ] **Step 3: Preserve the collector's existing public helper surface**

In `clockify_sync_collect.py`, keep `calendly_env_candidates()` and `load_env_file()` callable for existing tests/callers, but implement them as thin wrappers around `provider_env`. Do not change Clockify/Fathom candidate ordering.

In `calendly_collector.run()`, build one configured environment using the shared helpers for both `preflight` and `collect`; pass it to `_gateway_configuration()` or `fetch_calendly()` respectively. Direct environment variables override the file.

- [ ] **Step 4: Run focused, integration, and full verification, then commit**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_calendly_semantic_evidence.py' -v
python3 -m unittest discover -s tests -p 'test_collector_burst_context.py' -v
python3 -m unittest discover -s tests -p 'test_process_integration.py' -v
python3 -m unittest discover -s tests -v
git diff --check
```

Commit only owned tracked paths with message: `fix: load standalone calendly environment`.

---

### Task 3: Match Participant People by Typed Stable Identity

**Files:**
- Modify: `scripts/meeting_reconciliation.py`
- Test: `tests/test_meeting_reconciliation.py`
- Test: `tests/test_work_accounting_pipeline.py`

**Interfaces:**
- Consumes: per-participant mappings containing any of `email`, `id`, and `name`, plus digest-bound member identities.
- Produces: typed identity tokens per person and a deterministic one-to-one participant-set relation used by explicit and fallback matching.

- [ ] **Step 1: Add failing compatible-identity and conflict regressions**

In `tests/test_meeting_reconciliation.py`, add:

```python
def test_same_email_with_extra_name_token_is_same_participant(self):
    # Fathom: {email: client@example.test, name: Client}
    # Calendly: {email: client@example.test}
    # Explicit identity and fallback variants each reconcile to one meeting.

def test_same_name_with_disjoint_explicit_emails_is_participant_conflict(self):
    # Shared display name must not override contradictory email identities.

def test_participant_people_require_one_to_one_matching(self):
    # Two source people cannot both match one target person through a shared weak name.
```

Run: `python3 -m unittest discover -s tests -p 'test_meeting_reconciliation.py' -v`.

Expected: the first test fails with `participant_conflict`; the disjoint-email and one-to-one tests must remain quarantined.

- [ ] **Step 2: Introduce typed per-person identities**

Normalize each participant to tokens such as `email:client@example.test`, `id:abc`, and `name:client`. Member filtering compares the value portion against the digest-bound member identity set. A pair of people is compatible when:

```python
if both_have_email:
    compatible = bool(left_emails & right_emails)
elif both_have_id:
    compatible = bool(left_ids & right_ids)
else:
    compatible = bool(left_tokens & right_tokens)
```

Compare participant collections with deterministic one-to-one matching. Equal cardinality plus a unique compatible partner for every person is `same_people`; nonempty near-window collections without a complete one-to-one match are `participant_conflict`. Preserve the existing ambiguity quarantine across stronger and fallback tiers.

- [ ] **Step 3: Verify accounting consumes the same relation**

Add an accounting regression using canonical reconciliation with the extra-name/same-email fixture and assert exactly one canonical meeting proposal with both source evidence IDs. Run the reconciliation and accounting focused suites.

- [ ] **Step 4: Run full verification and commit**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_meeting_reconciliation.py' -v
python3 -m unittest discover -s tests -p 'test_work_accounting_pipeline.py' -v
python3 -m unittest discover -s tests -v
python3 -m json.tool schemas/meeting-reconciliation-v1.json >/dev/null
git diff --check
```

Commit only owned tracked paths with message: `fix: reconcile typed participant identities`.

---

## Completion Gate

- All three tasks have clean task-scoped spec and quality reviews.
- The final whole-plan review covers `ade3edc..HEAD` and verifies all five original residuals.
- The full suite passes on the exact final HEAD with no tracked drift.
- No guarded external action occurs; the branch remains local until separately approved for push/merge/publication.
