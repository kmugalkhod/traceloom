# Agent evals

Regression tests for the agent's configuration (CLAUDE.md, REVIEW.md, `.claude/` skills/agents/hooks).
They run in CI on every change to that configuration and nightly, and gate the merge on the pass rate.

```bash
python .sdlc/bin/evals.py                 # all cases, threshold from .sdlc/config.json
python .sdlc/bin/evals.py --case my-case --keep   # one case, keep its worktree to inspect
```

## Writing cases
- Start with 20–50 real tasks from recent work, each with its accepted outcome.
- A case = prompt + checks that define "acceptable": tests pass, lint clean, behaviour unchanged, policy followed.
- Use `must_not_change` to prove the agent did not weaken tests or touch generated/governance files.
- Every production incident gets a case, written by the owning team, kept as a permanent regression test.
- Retire cases that no longer discriminate (every model passes) and add harder ones from monitoring.
