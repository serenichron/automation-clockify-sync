# Clockify Analyst — Reduced MCP Toolset (draft)

**Why:** the agent's failing request carried **304 tools = ~376 KB** of tool schemas sent on *every* call (verified from `~/.hermes/sessions/request_dump_*.json`, 2026-06-17). Ollama Cloud (`deepseek-v4-flash:cloud`) rejects request bodies > ~1.5 MB with a generic `Bad Request (ref:…)`. Tool schemas are fixed overhead on top of system prompt + conversation history, so cutting them buys the most headroom for the least behavioural change.

**Target:** 304 → ~25 tools, tools-array ~376 KB → ~40 KB (≈ −335 KB / call).

## What the agent actually does
1. Run `clockify_sync_collect.py run` (shell/python).
2. Read the bundle (`run-report.md`, `proposals.json`; targeted slices of `evidence/`).
3. POST approved entries to the Clockify API (curl/python via execute_code).
4. Read + post comments on the Multica aggregate issue, rename it, @-mention Vlad.

Everything else (Google, Asana, GitHub, desktop, browser, vision, n8n, funding search, docs lookup) is unused.

## KEEP

### Built-in tools (core agent loop)
- `execute_code` — run the collector + POST to Clockify (primary tool)
- `read_file` — read `run-report.md` / `proposals.json`
- `search_files` — grep `evidence/` for a targeted row/session (never bulk-read)
- `write_file` — write a proposals file before posting, if needed
- `patch` — small edits if required
- `process` / `terminal` — shell for the script run (keep ONE; `execute_code` may suffice — drop `terminal` if so)
- `todo` — step tracking (optional, tiny)
- `memory` — honcho conclusions (optional, tiny)

### MCP servers
- **multica** (13 tools, 7 KB) — read/post issue comments, rename issue, mention. **Required.**

## DROP (unused by this agent)

| Server / builtin | Tools | Size | Reason |
|---|---|---|---|
| **google** (Gmail, Calendar, Drive, Docs, Sheets) | 126 | 183 KB | No Google I/O in the workflow |
| **desktop** (computer-use) | 30 | 60 KB | No GUI automation |
| **asana** | 45 | 33 KB | Reconciliation posts to Multica, not Asana |
| **github** | 26 | 18 KB | No repo ops |
| **n8n** | 9 | 10 KB | No workflow automation |
| **look4fundings** | 10 | 7 KB | Irrelevant |
| **context7** | 6 | 5 KB | No docs lookup |
| **browser** (builtin) | 10 | 6 KB | No browsing |
| **headroom** (mcp) | 7 | 2 KB | Status/config only |
| **sequential** | 1 | 4 KB | Not needed for deterministic reconciliation |
| **brave** | 2 | 2 KB | No web search |
| **web** (builtin: web_search/web_extract) | 2 | 2 KB | No web search |
| **skill / skills** (builtin) | 3 | 5 KB | No skill dispatch needed at runtime |
| **delegate** (builtin) | 1 | 7 KB | No sub-agent delegation |
| **vision** (builtin) | 1 | 1 KB | No image analysis |
| **session_search** (builtin) | 1 | 5 KB | Optional — drop unless cross-session lookup is used |

**Removing these = ~262 MCP tools + ~16 builtins ≈ −335 KB per request.**

## How to apply (Multica side — needs board action)
The toolset is the agent's connected MCP servers + enabled built-in tools in its Multica agent definition (not in `~/.hermes/config.yaml`, which only sets the model). In the Clockify Analyst agent config:
1. Disconnect MCP servers: Google Workspace, Asana, GitHub, n8n, look4fundings, context7, sequential, brave, headroom-mcp, desktop/computer-use.
2. Keep only the **multica** MCP server.
3. Disable built-in tools: browser, web, vision, delegate, skill/skills, session_search (optional).
4. Keep: execute_code, read_file, write_file, search_files, patch, process, todo, memory.

## Verification after applying
Re-trigger the autopilot (or dry-run the agent) and check the new `request_dump` tool count:
```
python3 -c "import json; b=json.load(open('<newest request_dump>'))['request']['body']; b=b if isinstance(b,dict) else json.loads(b); print('tools', len(b['tools']), 'body KB', len(json.dumps(b))//1024)"
```
Expect ~25 tools and a body well under 1.5 MB. Combined with the slimmed `run-report.json` (2.4 MB → 229 KB) and the context-budget instructions, this should clear the HTTP 400.
