# Clockify Sync Autopilot

Purpose: reconcile Vlad's direct Hermes/Claude Code work sessions across the fleet, Fathom meetings, existing Clockify entries, and Multica issues into approval-ready Clockify proposals.

Safety posture: dry-run by default. Clockify posting requires explicit approval of proposal row IDs.

Key files:
- routing.json — non-secret client/project/tag routing and safety policy.
- fleet.json — non-secret machine/session path inventory.
- state.json — run state, no secrets.
- scripts/clockify_sync_collect.py — deterministic stdlib collector/analyzer.
- multica-clockify-analyst-agent-instructions.md — dedicated Multica agent instructions.
- multica-clockify-autopilot-prompt.md — autopilot task prompt.
- runs/ — generated evidence and proposal bundles.

Credential probe order is implemented in the script and should not be documented with values. Expected secret files are under ~/.config/serenichron/ where available.
