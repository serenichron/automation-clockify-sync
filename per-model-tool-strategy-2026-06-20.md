# Per-Model Tool-Calling & Context-Budgeting Strategy (research 2026-06-20)

> Source: background research agent (Tavily/web). Saved here; may move to automation/multica/ once skill location is decided.

## Verified root cause of the Clockify Analyst 400
Ollama Cloud (deepseek-v4-flash:cloud) rejects request bodies above ~1.5MB with a generic
`Bad Request (ref:UUID)`. TESTED 2026-06-20 against the captured failing payload:
- real 304-tool array (incl. oneOf×2, anyOf×21) + tiny message → **200 OK** → schema-keyword
  hypothesis REFUTED; tool count alone is fine.
- full 1.9MB body → 400; trimmed <~1.5MB → 200 → **cause = total body size**.
Tools = 376KB fixed overhead; rest is history/evidence. Trim levers stack.

## The 30-tool rule (universal)
Selection accuracy degrades sharply past 30–50 tools across all frontier models; 304 is 6–10× over.
Prefer deferred/searchable tools over inline-all for >10–30 tools.

## Native lever: Hermes MCP Tool Search (shipped May 2026)
`~/.hermes/config.yaml tools.tool_search.enabled` (currently `auto`). Forcing on defers MCP schemas,
sends ~5 hot tools + a search tool, fetches the rest on demand. Non-destructive. Open Q: per-agent vs global.

## Comparison matrix
| Model/Runtime | Context | Request-size limit | Tool sweet spot | Tool-search | Gotchas |
|---|---|---|---|---|---|
| Hermes→GPT-5.5 | 1.05M / 128K out | token-governed | ~30–50 | Yes (Hermes) | reasoning tokens count; per-call fees on builtins |
| Qwen3.6-27B local | 256K | num_ctx (RAM/VRAM) | 5–15 | client-side only | Ollama default num_ctx 2–4K silently truncates; /v1 ignores num_ctx → Modelfile |
| Deepseek V4 Flash cloud | 1M / 384K out | ~1.5MB body (verified) | 5–15 inline | harness-side | generic 400; oneOf/anyOf tolerated in practice |
| Claude Opus 4.8 | 1M / 128K out | std API | 1000s with search | Yes (best) | ≥1 non-deferred tool required; caching preserved across expansion |
| Pi (Deepseek/Qwen) | 128K dflt / 16K out | backend's | 4-tool core by design | no | open-source pi-mono (NOT Inflection Pi); thinkingFormat must match backend |

## Per-model recommendations (short)
- GPT-5.5: enable Hermes tool-search; ~5 hot tools inline; budget reasoning tokens.
- Qwen3.6-27B: set num_ctx explicitly (Modelfile/native /api/chat); hard-cap 5–15 tools + RAG-over-tools.
- Deepseek V4 Flash: never inline 304; harness-side tool-search to send 3–10; 1M window irrelevant to the 400.
- Opus 4.8: server-side tool-search (bm25_20251119), defer all, keep 3–5 hot; ~85% token cut, 49→74% MCP eval.
- Pi: keep 4-tool core, reach capability via Bash/MCP sub-invocation.

## Confidence
- ~1.5MB body limit: now VERIFIED locally (was the agent's "low confidence" proxy hypothesis).
- "Pi" assumed = pi-mono (Mario Zechner), not Inflection Pi.
