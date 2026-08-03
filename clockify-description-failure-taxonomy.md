# Clockify description failure taxonomy

This audit records why the supplied 86-row review was not usable as a work
ledger. The private source prose is not committed. Its SHA and deterministic,
non-identifying feature vectors are bound by
`tests/fixtures/clockify-regression/v1/manifest.json`.

Counts overlap because one row can fail several contracts. The human-reviewed
corpus currently maps 33 rows to omission, 31 to exception, 21 to one rendered
activity, and one to an explicit split. Those mappings are regression
expectations, not rules inferred mechanically from feature counts.

## Surface failures in the supplied descriptions

| Failure | Observed signal | Why it is not Clockify-ready |
|---|---:|---|
| Truncated extraction | 64 rows contain ellipsis/truncation shapes | The end of the evidence is missing, so the claimed outcome cannot be trusted. |
| Review/status residue | 34 `[NEEDS REVIEW]` shapes; 35 status-like rows | Workflow state is not an accomplishment and forces proofreading back onto the user. |
| Message or prompt copied as work | 11 prompt-like rows; 23 human-reviewed `prompt_not_outcome` cases | Instructions, requests, and session messages describe what was said, not what was accomplished. |
| Markdown or presentation debris | 38 rows | Markup is transport formatting, not ledger wording. |
| Commands and runtime mechanics | 2 command-like rows | A command is evidence of an action, not a concise bounded result. |
| URLs and bare domains | 4 URL and 4 domain rows | These expose source material and substitute identifiers for meaning. |
| Paths, hashes, and email addresses | 2 path, 5 hash, and 2 email rows | They leak evidence identifiers or private data and do not explain the outcome. |
| Agent/process status dumps | 30 human-reviewed `forbidden_status_dump` cases | Running, waiting, completion, polling, and handoff chatter are not human work outcomes. |
| Unlabeled sessions | 2 cases | No trustworthy project, object, or outcome can be inferred. |
| Title-only meetings | 5 cases | A title cannot prove the meeting topic or outcome; use Fathom context or emit an exception. |
| Compound accomplishments | 8 split-required cases | Independently meaningful objects or deliverables need separate activities and effort. |
| Unbounded or vague wording | Present across status/exception cases | Labels such as “work session,” “worked on,” or a copied sentence lack a verb, specific object, and bounded result. |
| Wrong grammatical altitude | Present across copied prose | First-person narration, future intent, approval requests, and conversation fragments are not terse past-tense accounting. |
| Missing deterministic prefix/routing | Process-level defect | The description can look plausible while being charged to the wrong Clockify project or tag. |

## Generating-process failures behind the wording

1. **Snippet extraction replaced analysis.** First/last messages and terminal
   fragments cannot reconstruct a long, branching workstream.
2. **Session boundaries were treated as task boundaries.** One session may
   contain several accomplishments; one accomplishment may span sessions and
   machines.
3. **Evidence and allocation were conflated.** Overlapping evidence from heavy
   multitasking was trimmed, dropped, or squeezed instead of preserved and
   separately allocated.
4. **Wall-clock gaps were treated as capacity to consume.** Empty time is not
   proof of work and must remain unallocated.
5. **Parallel work was compressed into token durations.** A long supported
   workstream must retain an effort range and emit `contested_time` when honest
   capacity is insufficient.
6. **Existing Clockify and Fathom blocks were reconciled heuristically.** A
   mere overlap is not proof of the same meeting or accomplishment.
7. **Fathom titles were over-trusted.** Recording times are authoritative for
   placement, while transcript, summary, or action items are needed for a
   specific outcome.
8. **Descriptions were mutable model prose.** Models should propose structured
   action/object/outcome claims; deterministic code must render and reject the
   final text.
9. **Evidence citations were not an exact partition.** The same message could
   influence multiple rows or disappear, making truth and replay unverifiable.
10. **Stable semantic identities were absent or too weak.** Re-runs could
    duplicate, misassociate, or silently rewrite review rows.
11. **Corrections were treated as replacement text.** A durable process must
    learn routing, wording, split, omission, and allocation regressions against
    the same evidence identity.
12. **Quality was measured as row presence, not reviewer independence.** A
    stable 86-row proofreading queue is still failure; promotion requires
    unchanged-approval rates and zero critical errors.

## Replacement contract

Every proposed row must be one evidence-backed atomic accomplishment rendered
as `Prefix — Verb + object + bounded outcome`, normally 8–14 words. Evidence
may overlap; proposed time may not. Meetings and existing Clockify rows are
fixed. Other effort is flexibly placed only inside the cited semantic
workstream's observed span, never across unrelated daily gaps. Insufficient
meaning, routing, meeting context, or capacity becomes an explicit exception.

The normal experience may become one content-addressed clean batch plus genuine
exceptions only after the executable acceptance ledger proves the full shadow
and guarded thresholds. Calendly and overlapping proposed Clockify entries
remain excluded from this candidate.
