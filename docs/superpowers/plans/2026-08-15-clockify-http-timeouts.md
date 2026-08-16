# Clockify HTTP Timeouts Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Clockify collection and approval-gated posting HTTP timeouts independently configurable and fail closed on invalid values.

**Architecture:** Parse each timeout through a small path-specific validator with an inclusive 5–120 second range. Pass the resolved integer explicitly to the existing HTTP boundary; collector calls remain isolated from Fathom and other transports, while poster pagination, reconciliation, and create calls share the one value resolved before any Clockify request.

**Tech Stack:** Python 3 standard library, `urllib.request`, `unittest`, systemd and launchd environment examples.

## Global Constraints

- `CLOCKIFY_HTTP_TIMEOUT_SECONDS` defaults to `30` for read-only collection.
- `CLOCKIFY_POST_HTTP_TIMEOUT_SECONDS` defaults to `45` for approval-gated posting.
- Both values must be base-10 integers in the inclusive range `5` through `120`.
- Empty, non-integer, below-range, and above-range values fail before network access.
- Do not change retry counts, backoff, pagination, posting authorization, idempotency, receipts, or external-write gates.
- Tests must not contact Clockify or another external service.
- Do not commit, push, deploy, merge, publish, or perform an external mutation.

---

### Task 1: Collector timeout validation and propagation

**Files:**
- Modify: `scripts/clockify_sync_collect.py`
- Create: `tests/test_clockify_http_timeouts.py`

**Interfaces:**
- Consumes: the loaded Clockify environment mapping and inherited service environment.
- Produces: `clockify_http_timeout_seconds(cenv: Mapping[str, str]) -> int`, `http_json(url, headers, *, timeout_seconds=30)`, and `clockify_get(path, cenv)` forwarding the validated timeout.

- [ ] **Step 1: Write failing boundary tests**

Add table-driven tests using literal expected values. Patch `urllib.request.urlopen`, call `clockify_get`, and assert the context manager receives `timeout=67`. For `""`, `"abc"`, `"4"`, and `"121"`, assert the collector's configuration exception is raised and the patched boundary is not called. Cover the missing-value default `30`.

- [ ] **Step 2: Run the focused collector tests and verify RED**

Run: `python -m unittest tests.test_clockify_http_timeouts.ClockifyCollectorTimeoutTests -v`

Expected: FAIL because the timeout parser and explicit boundary parameter do not exist.

- [ ] **Step 3: Implement the minimum collector behavior**

Add constants for name, default, minimum, and maximum. Parse the mapping value first and inherited environment second so the private Clockify file can override a service default. Reject present empty values and invalid integers with a collector configuration error. Change only the Clockify path:

```python
def http_json(url: str, headers: dict[str, str], *, timeout_seconds: int = 30) -> Any:
    with urllib.request.urlopen(req, timeout=timeout_seconds) as response:
        return json.loads(response.read())

def clockify_get(path: str, cenv: dict[str, str]) -> Any:
    timeout = clockify_http_timeout_seconds(cenv)
    return http_json(
        CLOCKIFY_API + path,
        {"X-Api-Key": cenv["CLOCKIFY_API_KEY"]},
        timeout_seconds=timeout,
    )
```

Leave Fathom and Multica callers on the unchanged default.

- [ ] **Step 4: Run the focused collector tests and verify GREEN**

Run: `python -m unittest tests.test_clockify_http_timeouts.ClockifyCollectorTimeoutTests -v`

Expected: PASS with no network access.

### Task 2: Poster timeout validation and explicit propagation

**Files:**
- Modify: `scripts/clockify_post_approved_portfolio.py`
- Modify: `tests/test_clockify_http_timeouts.py`

**Interfaces:**
- Consumes: loaded and inherited `CLOCKIFY_POST_HTTP_TIMEOUT_SECONDS`.
- Produces: `post_http_timeout_seconds(environment: Mapping[str, Any]) -> int`; `_request(..., timeout_seconds: int)`; `_paged(..., timeout_seconds: int)`; `_live_entries(..., timeout_seconds: int)`.

- [ ] **Step 1: Write failing parser and HTTP boundary tests**

Patch the poster's `urllib.request.urlopen`, exercise `_request` with `timeout_seconds=83`, and assert the boundary gets `timeout=83`. Add literal parser cases for missing=`45`, valid=`83`, and invalid `""`, `"abc"`, `"4"`, and `"121"`; invalid cases must raise `PortfolioPostError` before the boundary can run.

- [ ] **Step 2: Run the focused poster tests and verify RED**

Run: `python -m unittest tests.test_clockify_http_timeouts.ClockifyPosterTimeoutTests -v`

Expected: FAIL because poster parsing and explicit propagation are absent.

- [ ] **Step 3: Implement minimal explicit propagation**

Resolve the timeout once in `run` immediately after loading the environment and before `_paged`. Thread it through every `_paged`, `_live_entries`, and `_request` call, including reconciliation after an ambiguous POST failure. Keep GET/POST retry decisions and backoff untouched.

- [ ] **Step 4: Run the focused poster tests and verify GREEN**

Run: `python -m unittest tests.test_clockify_http_timeouts.ClockifyPosterTimeoutTests -v`

Expected: PASS with the same retry behavior and no network access.

### Task 3: Operator documentation and complete verification

**Files:**
- Modify: `README.md`
- Modify: `ops/systemd/clockify-work-accounting.env.example`
- Modify: `ops/launchd/clockify-work-accounting.env.example`

**Interfaces:**
- Consumes: the two implemented environment contracts.
- Produces: operator-visible defaults, bounds, and scope without implying scheduled posting.

- [ ] **Step 1: Document the settings**

Add `CLOCKIFY_HTTP_TIMEOUT_SECONDS=30` to both service examples. Describe its read-only scope, the inclusive 5–120 range, and fail-closed behavior in the durable-host README section. Document `CLOCKIFY_POST_HTTP_TIMEOUT_SECONDS=45` beside the separately approval-gated posting command and state that it does not authorize posting.

- [ ] **Step 2: Run focused and complete verification**

Run:

```bash
python -m unittest tests.test_clockify_http_timeouts tests.test_portfolio_repair -v
python -m unittest discover -s tests -v
PYTHONPYCACHEPREFIX=/tmp/clockify-pycache python -m compileall -q scripts tests
git diff --check
```

Expected: all tests pass, compilation exits `0`, and `git diff --check` emits no output.

- [ ] **Step 3: Review the final diff without committing**

Confirm only the approved code, tests, plan/spec, documentation, and previously preserved Clockify modifications are present. Leave the working tree uncommitted and do not launch inference or any external API operation.
