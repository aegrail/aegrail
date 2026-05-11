# Security policy

agentctl is a runtime governance layer — security bugs in it can compromise
the systems it is meant to protect. We take reports seriously and respond
quickly.

## Reporting a vulnerability

**Do not open a public GitHub issue for security reports.**

Use [GitHub's private vulnerability reporting](https://docs.github.com/en/code-security/security-advisories/guidance-on-reporting-and-writing-information-about-vulnerabilities/privately-reporting-a-security-vulnerability)
on this repository — that puts a coordinated disclosure channel in place with
project maintainers.

If you cannot use that channel, email the maintainer listed in the repository
metadata. Include:

- A clear description of the issue
- Steps to reproduce
- The agentctl version affected
- Your assessment of impact

## What we consider in scope

- A bypass that lets an action proceed despite a `BudgetExceeded` condition
- A path that allows an audit event to be silently dropped or modified after
  emission
- Credential or PII leakage through audit event fields that should be
  redacted
- Any path that allows a `Session` to outlive its declared budget
- Use-after-close paths that bypass `SessionTerminated`

## What we consider out of scope

- The LLM behaving unexpectedly. The system prompt is not a security boundary;
  agentctl exists *because* it isn't.
- DoS against a single agent process by exhausting its budget.
- Issues in `examples/` that depend on third-party SDKs (OpenAI, Anthropic,
  etc.) — report those upstream.

## Disclosure

We aim to acknowledge reports within 72 hours and ship a fix within 30 days for
confirmed high-severity issues. We will credit reporters in the release notes
unless you ask us not to.
