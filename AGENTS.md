## Agent skills

### Issue tracker

Issues and PRDs live in GitHub Issues; use the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Use `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, and `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

This is a single-context repo; read root `CONTEXT.md` and `docs/adr/`. See `docs/agents/domain.md`.

## Issue Development Rules

- When accepting a `ready-for-agent` Issue, first inspect the current Git branch and worktree status.
- If currently on `main`, create and switch to `feat/<issue-number>-<short-description>` before editing or committing code.
- Never modify or commit implementation work directly on `main`.
- Reading code, discussing an Issue, and proposing an implementation plan may happen on `main` before a feature branch exists.
- Before coding, provide an implementation plan covering affected areas, data flow, tests, and risks. Wait for approval when the plan contains a project-wide or difficult-to-reverse architecture decision.
- After implementation, run the relevant checks and open a Pull Request. Do not merge the Pull Request or close the Issue without explicit human approval.
- Git worktrees are optional. Use one feature branch per Issue; use separate worktrees only when multiple agents need to work concurrently.
