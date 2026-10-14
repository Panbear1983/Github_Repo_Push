# Github_Repo_Push - agent rules

**Memory wing: `github_repo_push`.** All agents (Claude Code, Hermes, Codex, Antigravity) share ONE long-term
memory, the mempalace, reached through the `mempalace_*` tools. Full rules and wing table:
`~/.mempalace/MEMORY_PROTOCOL.md`.

ghrp - the canonical repo push/drift tool; the 09:00 daily drift job pushes committed work in every repo.

## Every task in this repo
1. Start: `mempalace_search(<what you are about to do>, wing="github_repo_push")` before planning.
2. Finish: one `mempalace_add_drawer(wing="github_repo_push", room=<decisions|backend|bugs|reference>)`:
   what was done, what was verified, what is still open, Peter's words verbatim where they matter.
   Dated facts (bug fixed, decision made, thing deployed) also go to `mempalace_kg_add`.
3. Never file secrets, tokens or financial figures.
4. The Hermes kanban board is live work state, not memory; do not copy cards into the palace.

## Standing rules for this repo
- The pip-installed ghrp binary is broken; run with PYTHONPATH=src.
