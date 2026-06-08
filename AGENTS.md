# Dinify Backend — Agent Instructions

Codex must treat `CLAUDE.md` as the authoritative project context and development guide for this repo.

Before making changes:
1. Read `CLAUDE.md`.
2. Follow the repo's existing Django/backend architecture and patterns.
3. Make the smallest safe change for the requested task.
4. Do not make broad refactors unless explicitly requested.
5. Run the relevant verification command before preparing a PR.

For Codex Desktop work:
- Use Worktree mode by default.
- Keep changes isolated to the requested task.
- Summarize the diff before committing or creating a PR.
- If SSH push fails, use the connected GitHub app to create the branch, commit, and PR.
