# Changelog

All notable changes to `agentctl` are documented in this file.
The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — 2026-05-11

Initial public release. Three primitives, deliberately.

### Added

- **`Agent`** — top-level factory that ties an identity, a budget, and an audit sink together.
- **`Session`** — context-managed unit of bounded agent work. Generates a unique
  per-session principal of the form `<agent_identity>@<session_id>`.
- **`Budget`** — declarative hard ceilings on `usd`, `tokens`, `wall_seconds`,
  `max_recursion`, and `max_tool_calls`. At least one limit is required.
- **`BudgetState`** — live consumption tracker. Raises `BudgetExceeded`
  deterministically when any ceiling is crossed, regardless of what the LLM
  has been instructed to do.
- **`session.record_llm(...)`** — provider-agnostic LLM call recording.
  Works with OpenAI, Anthropic, Bedrock, litellm, or raw HTTP.
- **`session.call_tool(name, fn, ...)`** — wraps any callable. Counts toward
  the budget, captures success/failure and timing, emits a structured event.
  Defaults to PII-safe argument logging (keys only, not values).
- **`session.check_budget()`**, **`session.enter_recursion()`**,
  **`session.exit_recursion()`** — explicit budget controls for agent loops.
- **`AuditEvent`** — Pydantic model with flat top-level fields and a typed
  `payload` dict. Every event is identity-linked and carries a budget snapshot.
- **`AuditSink.file(path)`** — append-only JSONL, line-buffered, thread-safe.
- **`AuditSink.stdout()`** — JSONL to stdout for containerized log shippers.
- **`AuditSink.memory()`** — in-memory sink for tests.
- **Defensive sink wrapping** — sink failures are logged to stderr and never
  propagate to the agent.
- **Examples** — `basic.py`, `budget_kill.py`, `openai_demo.py`, `anthropic_demo.py`.

### Project

- Apache 2.0 licensed.
- Python 3.10, 3.11, 3.12 supported.
- Zero hard runtime dependencies beyond `pydantic`.
- 48 tests, 94% line coverage. Ruff clean.

[Unreleased]: https://github.com/agentctl/agentctl/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/agentctl/agentctl/releases/tag/v0.1.0
