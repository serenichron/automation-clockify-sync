Clockify sync inbox / reconciliation autopilot.

Purpose:
- Track dry-run reconciliation of Vlad's Clockify entries against Hermes/Claude Code session histories across the fleet, Fathom meetings, and Multica issue context.
- Collect proposal tables for human approval before any Clockify posting.

Local automation root:
/Users/blackthorne/Work/serenichron/automation/clockify-sync

Key files:
- README.md
- routing.json
- fleet.json
- state.json
- scripts/clockify_sync_collect.py
- multica-clockify-analyst-agent-instructions.md
- multica-clockify-autopilot-prompt.md
- runs/

Safety policy:
- Dry-run by default.
- No unattended Clockify POST/edit/delete.
- No unattended downstream issue mutations.
- Do not paste full private session histories into Multica; reference local run files and summarize evidence.
- Unmapped or ambiguous project/client routing must be escalated to Vlad.

Approval format:
Vlad should approve explicit proposal row IDs, e.g. "Approve P001 and P003 to post". Without that, the workflow only reports proposals.
