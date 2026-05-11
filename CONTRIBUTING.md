# Contributing to agentctl

Thanks for considering a contribution. agentctl is intentionally small —
three primitives, no more — and the bar for new features is high. The bar
for bug fixes, docs, and tests is low. Send those freely.

## Scope

agentctl is the **runtime contract for AI agents in production**. It owns:

- Scoped identity (agent role + per-session principal)
- Hard budget kill-switches
- Structured, identity-linked audit log

It deliberately does **not** own:

- Prompt management or eval — integrate Langfuse
- Observability dashboards — out of scope at the SDK layer
- LLM provider abstraction — `record_llm` is provider-agnostic by design
- Framework wrappers that ask you to rewrite your agent

If you're unsure whether your idea fits, open an issue first.

## Development setup

```bash
git clone https://github.com/<your-fork>/agentctl
cd agentctl
uv venv --python 3.12 .venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

If you don't use `uv`, plain `python -m venv .venv` works too.

## The loop

```bash
pytest                          # run tests
pytest --cov=agentctl           # with coverage
ruff check agentctl tests       # lint
ruff format agentctl tests      # format
```

CI runs the same commands on every PR across Python 3.10, 3.11, 3.12.

## Pull request guidelines

- **One change per PR.** Big PRs get reviewed slowly.
- **Tests required** for behavior changes. We hold ~94% coverage and would like
  to keep it there.
- **No new runtime dependencies** without discussion. Zero-dep core is a
  feature, not an accident.
- **Public API changes** require a CHANGELOG entry under `[Unreleased]`.
- **Docs first** for non-trivial features. Update the README example and
  reasoning before the code.
- **Conventional commits** are appreciated but not required.

## Reporting bugs

Use the bug template in `.github/ISSUE_TEMPLATE/`. Include:

- agentctl version (`python -c "import agentctl; print(agentctl.__version__)"`)
- Python version and OS
- Minimal reproducer
- Expected vs. actual behavior

## Security

Don't open public issues for security reports. See `SECURITY.md`.

## License

By contributing, you agree your contributions are licensed under Apache 2.0,
the same license as the project.
