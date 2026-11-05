# Review instructions

## Passes
Run three passes and tag each finding with its pass:
- **bugs**: logic errors, broken edge cases, subtle regressions, concurrency, error handling
- **security**: injection, authn/authz gaps, secrets or PII in code/logs/errors, unsafe deserialization, SSRF
- **compliance**: the diff matches the change's spec.md and plan.md, CLAUDE.md conventions and our design principles

## What Important means here
Reserve **Important** for findings that would break behaviour, leak data or breach a policy.
Style, naming and preference are **Nit**. Issues not introduced by this diff are **Pre-existing**.

## Cap the nits
Report at most five nits per review; summarise the rest as a count.

## Do not report
- Generated files and vendored code
- Anything CI already enforces (formatting, lint rules)

## Feedback loop
When a finding repeats a mistake already seen once, propose the CLAUDE.md line that would have prevented it.
Flag CLAUDE.md content this change made outdated.
