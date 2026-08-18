# Calendly Meeting Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collect Calendly recordings as first-class meeting evidence, deduplicate cross-source Fathom/Calendly recordings, and account for every canonical meeting exactly once at its full evidence-supported duration.

**Architecture:** A provider-specific Calendly collector emits a complete, checkpointed source result without inferring time from scheduled events. A pure meeting-reconciliation module canonicalizes Fathom and Calendly recordings under `meeting-dedup/v1`; the existing evidence, accounting, quality, and replay layers consume canonical meetings rather than Fathom-only identities.

**Tech Stack:** Python 3 standard library, dataclasses, `datetime`, `json`, existing page-checkpoint store, `unittest`, JSON Schema.

**Spec:** `docs/superpowers/specs/2026-08-17-clockify-reconciliation-publication-manifest-design.md`

## Global Constraints

- Apply DRY, KISS, and YAGNI; do not introduce a database or provider framework.
- Work only in `/home/blackthorne/Work/automation-clockify-sync-task-f8ea741c` and preserve unrelated/untracked artifacts.
- Calendly access is read-only; credential creation or permission grants require separate approval.
- A scheduled Calendly event without recording evidence is excluded as `scheduled_without_recording`; scheduled duration is never silently billed.
- Incomplete Calendly pagination or missing recording capability is a visible capability gap and blocks complete source coverage.
- A Calendly-only recording is fixed meeting time at its exact recorded duration.
- A Fathom/Calendly duplicate becomes one canonical meeting; missing participant data, multiple candidates, or timing disagreement remains an exception.
- `meeting-dedup/v1` requires the same normalized non-Vlad participant set and start/end differences no greater than five minutes for fallback matching.
- Multi-project splits require timestamped semantic boundaries; human approval cannot substitute for missing duration evidence.
- Preserve and reuse existing content-addressed model caches. Private inference uses only the approved DeepSeek V4 Flash cloud revision; DeepSeek Pro is prohibited.
- Use strict TDD: each production behavior first receives a test that fails for the expected reason.
- Do not call Calendly, Clockify, Sheets, Slack, Multica, or other external systems while implementing unit/integration behavior.

---

### Task 1: Calendly Source Contract and Recording Model

**Files:**
- Create: `schemas/calendly-recording-source-v1.json`
- Create: `scripts/calendly_collector.py`
- Create: `tests/test_calendly_semantic_evidence.py`

**Interfaces:**
- Produces `CalendlyCollectorError`, `CALENDLY_COMPATIBILITY_VERSION`, `normalized_recording(record) -> dict[str, Any]`, and `scheduled_without_recording(event) -> dict[str, Any]`.
- Produces CLI subcommands `preflight` and `collect`; both accept an explicit half-open interval and JSON output path, and `collect` additionally accepts `--checkpoint-root`.
- A normalized recording contains `recording_id`, `meeting_id`, `title`, `start`, `end`, `duration_seconds`, `organizer`, `participants`, `join_url`, `transcript`, `summary`, and `source_digest`.

- [ ] **Step 1: Write the failing normalization and exclusion tests**

```python
class CalendlyRecordingContractTests(unittest.TestCase):
    def test_recording_preserves_exact_window_and_semantics(self):
        value = calendly.normalized_recording({
            "uri": "recordings/rec-1",
            "event_uri": "events/evt-1",
            "name": "Client review",
            "recording_start_time": "2026-08-04T10:00:00Z",
            "recording_end_time": "2026-08-04T10:37:00Z",
            "organizer": {"email": "vlad@example.test"},
            "participants": [{"email": "client@example.test"}],
            "transcript": [{"offset_seconds": 0, "text": "Reviewed launch"}],
        })
        self.assertEqual(2220, value["duration_seconds"])
        self.assertEqual("2026-08-04T10:00:00Z", value["start"])
        self.assertEqual("2026-08-04T10:37:00Z", value["end"])
        self.assertTrue(value["source_digest"].startswith("sha256:"))

    def test_scheduled_event_without_recording_never_supplies_duration(self):
        value = calendly.scheduled_without_recording({
            "uri": "events/evt-2",
            "start_time": "2026-08-04T12:00:00Z",
            "end_time": "2026-08-04T13:00:00Z",
        })
        self.assertEqual("scheduled_without_recording", value["reason"])
        self.assertNotIn("duration_seconds", value)
```

- [ ] **Step 2: Run the tests and verify RED**

Run: `python3 -m unittest tests.test_calendly_semantic_evidence.CalendlyRecordingContractTests -v`

Expected: FAIL because `scripts.calendly_collector` does not exist.

- [ ] **Step 3: Implement the pure source contract**

```python
CALENDLY_COMPATIBILITY_VERSION = "calendly-recordings/v1"

class CalendlyCollectorError(ValueError):
    pass

def normalized_recording(record: Mapping[str, Any]) -> dict[str, Any]:
    start = _parse_required_utc(record.get("recording_start_time"))
    end = _parse_required_utc(record.get("recording_end_time"))
    if end <= start:
        raise CalendlyCollectorError("recording window is invalid")
    document = {
        "recording_id": _required_id(record.get("uri")),
        "meeting_id": _required_id(record.get("event_uri")),
        "title": str(record.get("name") or "Calendly recording"),
        "start": _iso_utc(start),
        "end": _iso_utc(end),
        "duration_seconds": int((end - start).total_seconds()),
        "organizer": _normal_person(record.get("organizer")),
        "participants": _normal_people(record.get("participants")),
        "join_url": str(record.get("join_url") or ""),
        "transcript": _normal_transcript(record.get("transcript")),
        "summary": str(record.get("summary") or ""),
    }
    return {**document, "source_digest": "sha256:" + _digest(document)}
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run: `python3 -m unittest tests.test_calendly_semantic_evidence.CalendlyRecordingContractTests -v`

Expected: both tests pass.

- [ ] **Step 5: Add schema validation tests and commit**

Add a test that validates one recording, rejects missing identities, rejects a naive timestamp, and rejects an end before start using `schemas/calendly-recording-source-v1.json`.

Add CLI parser tests proving `preflight` and `collect` reject missing boundaries, resolve output/checkpoint paths without writing outside the requested roots, and never print credential values.

Run: `python3 -m unittest tests.test_calendly_semantic_evidence -v`

Expected: all Calendly contract tests pass.

```bash
git add schemas/calendly-recording-source-v1.json scripts/calendly_collector.py tests/test_calendly_semantic_evidence.py
git commit -m "feat: add Calendly recording source contract"
```

---

### Task 2: Checkpointed Calendly Recording Collection

**Files:**
- Modify: `scripts/calendly_collector.py`
- Modify: `scripts/clockify_sync_collect.py:107-126,2450-2738,3704-3893`
- Modify: `tests/test_calendly_semantic_evidence.py`
- Modify: `tests/test_collector_burst_context.py`

**Interfaces:**
- Consumes `PageCheckpointStore`, `CheckpointIdentity`, and `CheckpointState` from `scripts.collector_checkpoints`.
- Produces `fetch_calendly(cenv, since, until, *, checkpoint_store=None, http_json=None) -> dict[str, Any]` with `status`, `complete`, `recordings`, `scheduled_without_recording`, `pages_fetched`, `pagination`, and sanitized `failure`.

- [ ] **Step 1: Write a failing pagination/resume test**

```python
def test_fetch_resumes_at_saved_cursor_and_exposes_only_complete_results(self):
    with tempfile.TemporaryDirectory() as directory:
        store = PageCheckpointStore(Path(directory))
        first = responses([
            {"collection": [recording("one")], "pagination": {"next_page_token": "private-next"}},
            OSError("offline"),
        ])
        partial = calendly.fetch_calendly(ENV, SINCE, UNTIL, checkpoint_store=store, http_json=first)
        self.assertFalse(partial["complete"])
        self.assertEqual([], partial["recordings"])

        resumed = calendly.fetch_calendly(
            ENV, SINCE, UNTIL, checkpoint_store=store,
            http_json=reject_first_page_then_return(recording("two")),
        )
        self.assertTrue(resumed["complete"])
        self.assertEqual(["one", "two"], [row["recording_id"] for row in resumed["recordings"]])
```

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m unittest tests.test_calendly_semantic_evidence.CalendlyCollectionTests.test_fetch_resumes_at_saved_cursor_and_exposes_only_complete_results -v`

Expected: FAIL because `fetch_calendly` is absent.

- [ ] **Step 3: Implement bounded pagination and checkpoint identity**

```python
def fetch_calendly(cenv, since, until, *, checkpoint_store=None, http_json=None):
    identity = CheckpointIdentity(
        source="calendly",
        since_utc=_iso_utc(since),
        until_utc=_iso_utc(until),
        request_fingerprint=_request_fingerprint(cenv, since, until),
        compatibility_version=CALENDLY_COMPATIBILITY_VERSION,
    )
    state = checkpoint_store.open(identity) if checkpoint_store else None
    raw, seen, cursor = _checkpoint_items(state)
    while True:
        page = (http_json or _http_json)(_recordings_url(cenv, since, until, cursor), _headers(cenv))
        items, next_cursor = _page_response(page)
        _reject_repeated_cursor_or_page(items, next_cursor, seen)
        state = _append_page(checkpoint_store, state, items, next_cursor)
        raw.extend(items)
        if next_cursor is None:
            _mark_complete(checkpoint_store, state)
            break
        cursor = next_cursor
    return _complete_result(raw)
```

Incomplete/error branches return no recordings and only cursor/page digests in diagnostics.

- [ ] **Step 4: Add capability-gap and malformed-envelope tests**

Test missing credentials, a provider response without a recordings collection, a scheduled event with no recording, repeated cursors, safety-limit exhaustion, and an endpoint that cannot supply exact recording windows. Assert `complete: false` for capability/pagination failures and no credential/cursor leakage.

- [ ] **Step 5: Integrate Calendly into one collector slice**

Add `calendly_env_candidates()`, load the approved environment, call `fetch_calendly`, include it in source completeness, and write `evidence/calendly-recordings.json`. The collector report must include only aggregate status/counts and the evidence path.

- [ ] **Step 6: Run collector regression tests and commit**

Run: `python3 -m unittest tests.test_calendly_semantic_evidence tests.test_collector_burst_context tests.test_process_integration -v`

Expected: all selected tests pass and existing Fathom behavior remains unchanged.

```bash
git add scripts/calendly_collector.py scripts/clockify_sync_collect.py tests/test_calendly_semantic_evidence.py tests/test_collector_burst_context.py tests/test_process_integration.py
git commit -m "feat: collect Calendly recordings with checkpoints"
```

---

### Task 3: Calendly Evidence Ledger and Completeness

**Files:**
- Modify: `scripts/evidence_ledger.py:198-323,446-660`
- Modify: `schemas/evidence-ledger-v1.json`
- Modify: `tests/test_evidence_ledger.py`
- Modify: `tests/test_process_integration.py`

**Interfaces:**
- Consumes the collector result from Task 2.
- Produces immutable `source_type="calendly"` evidence events and a source inventory whose expected/observed counts match complete recording collection.

- [ ] **Step 1: Write failing ledger tests**

```python
def test_calendly_recording_is_immutable_evidence_with_complete_inventory(self):
    snapshot = complete_snapshot(calendly={"status": "ok", "complete": True, "recordings": [CALENDLY_RECORDING]})
    events = ledger.normalize_collector_snapshot(snapshot)
    meeting = next(event for event in events if event.source_type == "calendly")
    inventory = ledger.source_inventory_from_collector(snapshot)
    self.assertEqual("rec-1", meeting.attributes["recording_id"])
    self.assertEqual({"status": "complete", "expected_count": 1, "observed_count": 1}, inventory["calendly"])
```

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m unittest tests.test_evidence_ledger.EvidenceLedgerTests.test_calendly_recording_is_immutable_evidence_with_complete_inventory -v`

Expected: FAIL because Calendly is absent from source normalization.

- [ ] **Step 3: Extend source inventory and event normalization**

Add Calendly to `source_inventory_from_collector()` and `_snapshot_event()`. Preserve exact start/end, participant identities, summary/transcript semantics, provider IDs, and source digest; never embed credentials or raw API envelope metadata.

- [ ] **Step 4: Add incomplete/mismatch tests and verify GREEN**

Test missing credentials, partial source, malformed counts, and digest stability. A complete source whose inventory count differs from ledger events must block completeness.

Run: `python3 -m unittest tests.test_evidence_ledger tests.test_process_integration -v`

Expected: both modules pass.

- [ ] **Step 5: Commit**

```bash
git add scripts/evidence_ledger.py schemas/evidence-ledger-v1.json tests/test_evidence_ledger.py tests/test_process_integration.py
git commit -m "feat: preserve Calendly recording evidence"
```

---

### Task 4: Canonical Meeting Reconciliation (`meeting-dedup/v1`)

**Files:**
- Create: `scripts/meeting_reconciliation.py`
- Create: `schemas/meeting-reconciliation-v1.json`
- Create: `tests/test_meeting_reconciliation.py`

**Interfaces:**
- Produces `CanonicalMeeting`, `MeetingReconciliation`, and `reconcile_meetings(fathom, calendly, *, vlad_identities, tolerance=timedelta(minutes=5), algorithm_version="meeting-dedup/v1")`.
- Produces a CLI that accepts `--period-manifest`, `--fathom-from-manifest`, `--calendly`, `--algorithm`, `--tolerance-seconds`, and `--output`, matching the August recovery runbook exactly.

- [ ] **Step 1: Write failing canonicalization tests**

```python
def test_calendly_only_recording_remains_full_duration(self):
    result = reconcile_meetings([], [CALENDLY_37_MINUTES], vlad_identities={"vlad@example.test"})
    self.assertEqual(1, len(result.meetings))
    self.assertEqual(37 * 60, result.meetings[0].duration_seconds)
    self.assertEqual(("calendly:rec-1",), result.meetings[0].source_ids)

def test_fallback_duplicate_requires_same_participants_and_five_minute_boundaries(self):
    result = reconcile_meetings([FATHOM_MEETING], [CALENDLY_SAME_MEETING], vlad_identities=VLAD_IDS)
    self.assertEqual(1, len(result.meetings))
    self.assertEqual(("calendly:rec-1", "fathom:f-1"), result.meetings[0].source_ids)

def test_missing_participants_or_late_boundary_is_exception(self):
    result = reconcile_meetings([FATHOM_MEETING], [CALENDLY_AMBIGUOUS], vlad_identities=VLAD_IDS)
    self.assertEqual("duplicate_ambiguous", result.exceptions[0]["kind"])
```

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m unittest tests.test_meeting_reconciliation -v`

Expected: import failure because `scripts.meeting_reconciliation` does not exist.

- [ ] **Step 3: Implement deterministic matching and canonical IDs**

```python
DEDUP_VERSION = "meeting-dedup/v1"
DEDUP_TOLERANCE = dt.timedelta(minutes=5)

def reconcile_meetings(fathom, calendly, *, vlad_identities, tolerance=DEDUP_TOLERANCE, algorithm_version=DEDUP_VERSION):
    candidates = _normalized_candidates(fathom, calendly, vlad_identities)
    matches, exceptions = _match_in_priority_order(candidates, tolerance)
    meetings = tuple(_canonical_meeting(group, algorithm_version, tolerance) for group in matches)
    return MeetingReconciliation(algorithm_version, int(tolerance.total_seconds()), meetings, (), tuple(exceptions))
```

Canonical IDs are content-addressed from ordered source identities and source digests. Prefer richer Fathom semantics, then fill missing semantic fields from Calendly. Never average conflicting timing.

- [ ] **Step 4: Add match-priority, multiple-candidate, and replay tests**

Cover shared identity, join URL, participant/window fallback, missing participants, more than five minutes, multiple candidates, source order independence, and byte-identical replay.

Add a CLI fixture test that reads synthetic Fathom/Calendly evidence through the documented arguments, writes one schema-valid reconciliation document, and fails without changing the output when the period manifest or source digest does not verify.

- [ ] **Step 5: Validate schema and commit**

Run: `python3 -m unittest tests.test_meeting_reconciliation -v`

Expected: all canonical meeting tests pass.

```bash
git add scripts/meeting_reconciliation.py schemas/meeting-reconciliation-v1.json tests/test_meeting_reconciliation.py
git commit -m "feat: reconcile canonical meeting recordings"
```

---

### Task 5: Timestamp-Evidenced Multi-Project Meeting Splits

**Files:**
- Modify: `scripts/meeting_reconciliation.py`
- Modify: `scripts/work_accounting_pipeline.py:697-909`
- Modify: `tests/test_meeting_reconciliation.py`
- Modify: `tests/test_work_accounting_pipeline.py`

**Interfaces:**
- Produces `MeetingSplit` and `validate_meeting_splits(meeting, splits, *, granularity_minutes=5) -> tuple[MeetingSplit, ...]`.
- Each split contains canonical meeting ID, index, start/end, project/task route, and timestamped evidence IDs.

- [ ] **Step 1: Write failing split-conservation tests**

```python
def test_timestamped_split_covers_full_meeting_without_overlap(self):
    splits = validate_meeting_splits(MEETING_37_MINUTES, [
        split(0, "10:00", "10:20", "Client A", evidence_ids=("transcript:0-1200",)),
        split(1, "10:20", "10:37", "Client B", evidence_ids=("transcript:1200-2220",)),
    ])
    self.assertEqual(37, sum(item.duration_minutes for item in splits))

def test_untimestamped_share_is_rejected_even_when_human_approved(self):
    with self.assertRaisesRegex(MeetingReconciliationError, "timestamped boundary"):
        validate_meeting_splits(MEETING_37_MINUTES, [share_only(20), share_only(17)])
```

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m unittest tests.test_meeting_reconciliation.MeetingSplitTests -v`

Expected: FAIL because split validation is absent.

- [ ] **Step 3: Implement strict interval and route validation**

Require positive durations, five-minute units except the exact final remainder, ordered non-overlap, full interval coverage, shared canonical ID, distinct validated routes, and timestamped evidence at every boundary.

- [ ] **Step 4: Integrate split activities into accounting**

Generalize `_fathom_events()` to `_recording_events()` and make `run_accounting()` consume canonical meeting reconciliation. One unsplit meeting produces one fixed proposal; validated splits produce non-overlapping fixed sub-proposals whose total equals the recording duration.

- [ ] **Step 5: Run focused accounting tests and commit**

Run: `python3 -m unittest tests.test_meeting_reconciliation tests.test_work_accounting_pipeline -v`

Expected: all selected tests pass, including existing Fathom overlap and route tests.

```bash
git add scripts/meeting_reconciliation.py scripts/work_accounting_pipeline.py tests/test_meeting_reconciliation.py tests/test_work_accounting_pipeline.py
git commit -m "feat: allocate timestamped meeting project splits"
```

---

### Task 6: Canonical Meeting Quality, Replay, and Operations

**Files:**
- Modify: `scripts/clockify_portfolio_quality.py:153-319,434-623`
- Modify: `scripts/clockify_portfolio_replay.py:85-140`
- Modify: `schemas/work-accounting-result-v1.json`
- Modify: `tests/test_portfolio_quality.py`
- Modify: `tests/test_portfolio_replay.py`
- Modify: `README.md`
- Modify: `ops/systemd/clockify-work-accounting.env.example`
- Modify: `ops/launchd/clockify-work-accounting.env.example`

**Interfaces:**
- Consumes canonical reconciliation and splits from Tasks 4-5.
- Produces generalized recording coverage with expected/represented/excluded/exception counts and replay identity fields `meeting_reconciliation_digest`, `meeting_dedup_version`, and `meeting_dedup_tolerance_seconds`.

- [ ] **Step 1: Write failing quality and replay tests**

```python
def test_every_source_recording_is_accounted_once_by_canonical_meeting(self):
    report = quality.audit(PORTFOLIO_WITH_DUPLICATE_SOURCES, ledger_document=LEDGER)
    self.assertEqual("pass", report["status"])
    self.assertEqual({"source_recordings": 2, "canonical_meetings": 1, "missing": 0}, subset(report["recording_coverage"]))

def test_replay_rejects_dedup_algorithm_or_tolerance_drift(self):
    sealed = replay.seal(**FIXTURE)
    changed = replace_reconciliation(FIXTURE, algorithm="meeting-dedup/v2")
    with self.assertRaises(replay.PortfolioReplayError):
        replay.verify(sealed, **changed)
```

- [ ] **Step 2: Run and verify RED**

Run: `python3 -m unittest tests.test_portfolio_quality tests.test_portfolio_replay -v`

Expected: failures because quality/replay remain Fathom-only.

- [ ] **Step 3: Generalize quality coverage and replay binding**

Replace `_accounted_fathom_ids()`/`_fathom_coverage()` with canonical recording coverage while retaining a read-only compatibility alias for old sealed runs. Bind reconciliation digest, algorithm, tolerance, and split digest into replay identity.

- [ ] **Step 4: Document configuration and capability behavior**

Document the Calendly environment path/recording endpoint, read-only requirement, capability-gap behavior, scheduled-without-recording exclusion, dedup rules, and timestamp-only project splits. Do not include credentials.

- [ ] **Step 5: Run the subsystem and full suites**

Run: `python3 -m unittest tests.test_calendly_semantic_evidence tests.test_meeting_reconciliation tests.test_evidence_ledger tests.test_work_accounting_pipeline tests.test_portfolio_quality tests.test_portfolio_replay tests.test_process_integration -v`

Then run: `python3 -m unittest discover -s tests -v`

Expected: all tests pass with only the repository's documented skips.

- [ ] **Step 6: Commit**

```bash
git add scripts/clockify_portfolio_quality.py scripts/clockify_portfolio_replay.py schemas/work-accounting-result-v1.json tests/test_portfolio_quality.py tests/test_portfolio_replay.py README.md ops/systemd/clockify-work-accounting.env.example ops/launchd/clockify-work-accounting.env.example
git commit -m "feat: gate canonical meeting completeness"
```
