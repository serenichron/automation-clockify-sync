# Configurable Clockify HTTP Timeouts Design

## Scope

Make Clockify HTTP request timeouts configurable in both execution paths that
call the Clockify API:

- read-only harvesting in `scripts/clockify_sync_collect.py`;
- separately approval-gated posting in
  `scripts/clockify_post_approved_portfolio.py`.

This change does not alter retry counts, backoff, pagination, posting
authorization, idempotency, receipt recovery, or any external-write gate.

## Chosen approach

Use two independent environment settings:

- `CLOCKIFY_HTTP_TIMEOUT_SECONDS` for read-only collection, default `30`;
- `CLOCKIFY_POST_HTTP_TIMEOUT_SECONDS` for approval-gated posting, default
  `45`.

Both settings accept base-10 integers from `5` through `120`, inclusive.
Missing values preserve the current defaults. Empty, non-integer, below-range,
and above-range values fail closed before a Clockify network request.

The alternatives were rejected for these reasons:

- One shared timeout couples routine read-only harvesting to guarded posting,
  even though their latency and recovery contracts differ.
- CLI-only flags bypass the persistent private environment contract used by
  systemd, launchd, and resumable posting commands.
- Silent clamping hides configuration drift and makes the effective timeout
  harder to audit.

## Components and data flow

### Collector

Add a focused parser that reads `CLOCKIFY_HTTP_TIMEOUT_SECONDS` from the loaded
Clockify environment mapping. `clockify_get` resolves that value and passes it
to `http_json`, which forwards it to `urllib.request.urlopen`.

All Clockify list and latest-entry requests already flow through
`clockify_get`, so the setting applies consistently without changing Fathom,
SSH, Git, canonical-export, or analyzer timeouts.

### Approved portfolio poster

Add an equivalent focused parser for `CLOCKIFY_POST_HTTP_TIMEOUT_SECONDS`.
Resolve it once in `run`, then pass the integer explicitly through `_paged`,
`_live_entries`, and `_request`.

The explicit parameter keeps request behavior deterministic and avoids mutable
module state. GET requests retain their existing bounded retries. POST requests
remain non-retryable after an ambiguous transport failure and continue using
live-entry reconciliation plus the durable receipt before any further create.

### Configuration documentation

Document both variables in the README. Add the collector setting to the
systemd and launchd environment examples because those runners harvest
Clockify evidence. Document the posting setting beside the separately guarded
posting command; do not imply that scheduled review services may post entries.

## Error handling and safety

- Missing values use the existing `30`- and `45`-second behavior.
- Invalid values raise the path's existing configuration error type before
  `urlopen` is called.
- Timeout exceptions retain the existing collection-incomplete or posting-
  interrupted behavior.
- No timeout value, credential, request payload, or private evidence is added
  to logs or status artifacts.
- The collector remains read-only. Posting still requires the exact approved
  portfolio digest and explicit execution flag.

## Testing

Use test-driven development at the actual HTTP boundary:

1. Collector tests patch `urllib.request.urlopen`, exercise `clockify_get`, and
   prove the configured timeout reaches the call.
2. Collector tests prove invalid and out-of-range values fail before the HTTP
   boundary.
3. Poster tests exercise `_request` through an explicit configured timeout and
   prove the value reaches `urlopen` without changing retry behavior.
4. Poster tests prove invalid and out-of-range values fail before any network
   call.
5. Run focused tests, the complete unittest discovery suite, static bytecode
   compilation with its cache redirected to `/tmp`, and `git diff --check`.

No test may contact Clockify or another external service.
