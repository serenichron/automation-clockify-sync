# Clockify reconciliation sync — dry run

Run the Clockify reconciliation workflow in dry-run mode.

Aggregate issue:
- SER-651 — Clockify reconciliation review

Default scope:
- If no explicit date range is provided, use the collector's rolling seven-day evidence window.
- Use Europe/Bucharest display time.

Commands:
1. Resolve the host-local root. Use `$CLOCKIFY_SYNC_ROOT` when set; otherwise
   use the first existing path containing `scripts/clockify_review_run.py`:
   `$HOME/Work/automation-clockify-sync`, then
   `$HOME/Work/serenichron/automation/clockify-sync`. Fail closed if neither
   exists.
2. Run:
   `python3 <resolved-root>/scripts/clockify_review_run.py`
3. Read the emitted `autopilot-result.json` and `autopilot-summary.md`.
4. Obey `action` exactly:
   - `no_comment`: make no Multica mutation and end the run.
   - `review_delta`: post one concise SER-651 comment using only `new` and
     `changed`.
   - `coverage_warning`: post one diagnostic comment with warning details and
     delta counts; do not infer that missing sources contain no work.
   - `blocked`: post one diagnostic comment and stop.
5. A permitted comment must include:
   - date range checked
   - runtime collector path and Git SHA
   - new/changed proposal table using stable review item IDs
   - ambiguous rows requiring Vlad's decision
   - carried-pending count without reprinting the unchanged backlog
   - skipped/covered and coverage-warning summary
   - approval instruction using the stable review item IDs.
6. Never overwrite the SER-651 description or change its title, status,
   assignment, or due date during a scheduled run. A healthy no-delta run
   produces no comment.

Do not post, edit, or delete Clockify entries unless a board (human) member has approved the specific rows in a comment (per-row decisions naming the row IDs — see the agent's "Approval recognition" rules). Treat "trim" as reduce-the-duration, never a description note alone.
Do not update the reference Google Sheet from the quality step. Sheet writes require a separately approved, stable-ID patch workflow that preserves Status and Notes.
Do not create/update downstream client or project issues unless explicitly authorized in the current task.
