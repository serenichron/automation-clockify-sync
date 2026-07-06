# Clockify reconciliation sync — dry run

Run the Clockify reconciliation workflow in dry-run mode.

Aggregate issue:
- SER-106 — Clockify sync inbox / reconciliation autopilot

Default scope:
- If no explicit date range is provided, use one day before the latest Clockify entry through now.
- Use Europe/Bucharest display time.

Commands:
1. Run:
   python3 /Users/blackthorne/Work/serenichron/automation/clockify-sync/scripts/clockify_sync_collect.py run
2. Read the generated run report path printed by the command.
3. Reconcile evidence and prepare approval-ready proposals. Propose each row's duration already net of any overlapping already-logged meetings/entries (do not propose full spans that overlap logged meetings).
4. Post a concise comment to the aggregate Clockify sync issue/current run issue with:
   - date range checked
   - evidence files written
   - proposal table (durations already net of overlaps)
   - ambiguous rows requiring Vlad's decision
   - skipped/covered summary
   - approval instruction: "Reply with per-row decisions naming the row IDs — e.g. 'P001 accept, P002 trim, P003 log, P004 skip'. Any clear accept/log/post, trim, edit, or skip decision is honored; no special phrase required."

Do not post, edit, or delete Clockify entries unless a board (human) member has approved the specific rows in a comment (per-row decisions naming the row IDs — see the agent's "Approval recognition" rules). Treat "trim" as reduce-the-duration, never a description note alone.
Do not create/update downstream client or project issues unless explicitly authorized in the current task.
