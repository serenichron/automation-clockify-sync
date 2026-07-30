Clockify sync inbox / reconciliation autopilot.

Purpose:
- Track dry-run reconciliation of Vlad's Clockify entries against Hermes/Claude Code session histories across the fleet, Fathom meetings, and Multica issue context.
- Collect proposal tables for human approval before any Clockify posting.

Local automation root:
/Users/blackthorne/Work/automation-clockify-sync

Key files:
- README.md
- routing.json
- fleet.json
- state.json
- state/review-items.json (mutable durable review state; ignored by Git)
- scripts/clockify_sync_collect.py
- scripts/clockify_sync_quality.py
- scripts/clockify_review_state.py
- multica-clockify-analyst-agent-instructions.md
- multica-clockify-autopilot-prompt.md
- runs/

Safety policy:
- Dry-run by default.
- No unattended Clockify POST/edit/delete.
- No unattended downstream issue mutations.
- No automatic Google Sheet overwrite from the quality step.
- Do not paste full private session histories into Multica; reference local run files and summarize evidence.
- Unmapped or ambiguous project/client routing must be escalated to Vlad.

Approval format:
Vlad should approve stable review item IDs from `review-snapshot.json`. Without
that, the workflow only reports proposals. Raw `P001`-style collector IDs are
run-local and must not be reused as durable approval IDs.
