# Changelog

All notable changes to `aegrail` are documented in this file.
The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.3] — 2026-05-14

### Added — tamper-evident audit chain

- **`AuditEvent.prev_hash` and `AuditEvent.event_hash`.** Every emitted
  event now carries a SHA-256 chain link. `prev_hash` references the
  previous event in the chain (or `None` for the genesis event);
  `event_hash` is the hash of this event's serialized body (including
  its `prev_hash`). Any post-hoc edit to a historical event invalidates
  the chain from that point forward.
- **`aegrail.audit.verify_chain(events) -> (valid, first_bad_index)`** —
  pure function that walks a list of `AuditEvent` and confirms the
  chain. Returns `(True, -1)` on a valid chain or `(False, i)` at the
  first failing index. Auditors and ops teams run this on archived
  audit logs to confirm no tampering.
- **`aegrail.audit.compute_event_hash(event, prev_hash)`** — building
  block exposed for users who want to compute hashes outside the sink
  emit path (e.g. for streaming verification).
- **Chain continuation across process restarts.** `FileAuditSink`
  reads the last line of an existing audit file on open and continues
  the chain from that event's `event_hash`. A single audit file written
  by many sessions over weeks remains one verifiable chain.

### Added — Tool schema exports (DX)

- **`Tool.parameters` and `Tool.required` fields.** Optional;
  declare the LLM-visible tool schema directly on the aegrail Tool so
  it doesn't need to be repeated.
- **`Tool.to_openai_schema()`** — returns the dict suitable for passing
  to OpenAI's Chat Completions / Responses API `tools=[...]`.
- **`Tool.to_anthropic_schema()`** — returns the dict suitable for
  passing to Anthropic Messages API `tools=[...]`.
- Eliminates the DRY violation in `examples/prompt_injection_demo.py`
  and similar code where the Tool definition and the LLM schema were
  declared twice. Existing callers unaffected — the new fields are
  optional and the methods only fire when `parameters` is set.

### Added — `COMPLIANCE.md`

- Explicit mapping from aegrail's emitted events to specific SOC 2
  Trust Services Criteria controls (CC6.1, CC6.2, CC6.3, CC7.1, CC7.2,
  CC7.3, CC9.2, PI1.1, C1.1), ISO 27001:2022 Annex A controls (A.5.15,
  A.5.16, A.5.17, A.5.18, A.8.15, A.8.16, A.8.24), and NIST SP 800-53
  controls (AC-2, AC-3, AC-6, AU-2, AU-3, AU-9, AU-12, SI-4).
- `jq` recipes for the four evidence extractions auditors most
  commonly ask for.
- Honest scope disclaimers: aegrail does not authenticate users, does
  not ship a SIEM, does not enforce retention, does not substitute for
  network or process isolation. Read before quoting compliance claims.
- Ships in the sdist; visible on the GitHub repo.

### Project

- 109 tests (75 sync + 16 async + 11 chain + 7 schema). Ruff clean.
- Fully additive on top of 0.2.2; no breaking changes. Existing audit
  logs from 0.2.2 lack the chain fields and are still parseable as
  AuditEvent (the new fields default to `None`).
- The 0.2.3 wheel was verified against `examples/async_demo.py` driving
  Ollama `llama3.1:8b` in an isolated venv before PyPI upload, per the
  release rule. Both OWASP ASI02 + ASI03 controls fired through the
  async path; the chain validated end-to-end on the resulting log.

## [0.2.2] — 2026-05-13

### Added

- **`AsyncSession`** — async variant of `Session`, constructed via
  `Agent.async_session(*, user_id=None, task=None)`. Supports
  `async with`, `await s.call_tool(...)`, `await s.record_llm(...)`,
  `await s.check_budget()`, `await s.enter_recursion()` /
  `await s.exit_recursion()`. Mirrors the sync API; same exception
  types (`BudgetExceeded`, `ToolNotPermitted`, `SessionTerminated`)
  and same audit events.
- **Hard `wall_seconds` enforcement mid-tool-call.** When a `Budget`
  declares `wall_seconds`, every tool invocation inside an
  `AsyncSession` runs under `asyncio.wait_for(...,
  timeout=remaining)`. If the call hangs past the remaining budget,
  the runtime raises `BudgetExceeded('wall_seconds')` deterministically
  rather than waiting for the call to return. Sync `Session` could
  only enforce at event boundaries; this is the load-bearing
  improvement.
- **Sync + async tool dispatch.** `Tool.fn` may be `async def` or
  regular `def`. The runtime detects via `inspect.iscoroutinefunction`;
  sync functions are dispatched via `asyncio.to_thread` so the
  `wall_seconds` timeout still applies at the asyncio level.
  Documented caveat: CPython cannot kill the underlying thread, so
  a slow sync function continues running in the background until it
  naturally returns. For hard timeout guarantees, write tools as
  `async def`.
- **`examples/async_demo.py`** — async twin of
  `examples/prompt_injection_demo.py`. Same adversarial-customer-message
  scenario, run through `agent.async_session(...)` against local Ollama
  `llama3.1:8b`. Demonstrates both ASI02 + ASI03 denials firing under
  async dispatch.

### Project

- 91 tests (75 sync + 16 new async). Ruff clean. No API breakage,
  fully additive on top of 0.2.1.
- New dev dependency: `pytest-asyncio>=0.21` (only in `[dev]` extras;
  no runtime dependency added).
- Audit sinks remain synchronous. For high-throughput async hot paths,
  prefer `composite(file, callback(queue.put_nowait))` and drain the
  queue in a background task; avoid the webhook sink in the agent's
  hot path.

### Out of scope (intentionally)

- Async `Tool.when` predicates. Predicates must remain fast and
  non-IO; for policy-service lookups, do them inside `fn`.
- Async audit sinks (would add an aiohttp / httpx runtime dep,
  which the project does not take).
- Trio support. Asyncio only; trio users can bridge via `anyio` in
  their own code if desired.

## [0.2.1] — 2026-05-13

### Changed

- **Build hygiene.** `[tool.hatch.build.targets.sdist]` now declares an
  explicit `include` manifest instead of a denylist. The source
  distribution ships exactly what the project documents shipping
  (`aegrail/`, `examples/`, `tests/`, top-level docs, `LICENSE`,
  `pyproject.toml`, `.gitignore`, `.github/`) and nothing else. Local
  dev-tool configuration files no longer require explicit mention in
  the public build manifest.
- **`.gitignore`** narrowed to canonical Python / OS / venv patterns
  plus secret-file globs. Per-developer editor and tool configuration
  is now handled out-of-band via `.git/info/exclude`, which is
  intentionally not tracked and never reaches the published artifact.

### Project

- 75 tests. Ruff clean. No API changes from 0.2.0.

## [0.2.0] — 2026-05-13 (withdrawn)

> Withdrawn from PyPI on 2026-05-13. Superseded by [0.2.1]; every
> feature listed below ships unchanged in 0.2.1. The git tag `v0.2.0`
> has been removed; the commits remain visible in `main`'s history
> as part of the linear path to `v0.2.1`. Do not pin to `0.2.0`.

### Added

- **`Tool`** — a pydantic model declaring a callable the agent is
  authorised to invoke. Carries an optional `when(args) -> bool`
  predicate (denies on `False` or on raise), an optional
  `redact(args) -> dict` for per-tool audit redaction, and a
  human-readable `description` that surfaces into audit payloads.
- **`Agent.tools`** — agents now accept a `tools: Mapping[str, Tool]`
  registry at construction. Keys must match their `Tool.name`. Stored
  as an immutable `MappingProxyType`; cannot be swapped at runtime.
- **`ToolNotPermitted`** — new exception with three machine-readable
  reasons: `'not_registered'`, `'predicate_false'`, `'predicate_error'`.
- **`tool_denied` audit event** — every denial emits a distinct event
  type (separate from `tool_call`) so denials are forensically
  queryable. Denied calls do not consume the tool-call budget.
- **`examples/multi_agent_acl.py`** — FinOps and Architect agents in
  one process. The FinOps session cannot invoke the Architect's
  `deploy_infra` tool, demonstrating OWASP ASI03 enforcement.

### Changed (breaking)

- **`Session.call_tool` signature.** Was
  `call_tool(name, fn, *args, _arg_summary=None, **kwargs)`; now
  `call_tool(name, /, **kwargs)`. The callable is looked up from the
  agent's registry — callers no longer pass it. Positional args are
  no longer supported (LLM tool-calls arrive as JSON objects, which
  map naturally to kwargs).
- **Agents without `tools=` are strict.** Any `call_tool(...)` on an
  agent constructed without a tool registry raises
  `ToolNotPermitted('not_registered', ...)`. v0.2 forces opt-in to
  the ACL rather than silently allowing arbitrary calls.
- **`_arg_summary` kwarg removed.** Replaced by the `redact`
  parameter on the `Tool` definition itself.

### Mapping to OWASP Top 10 for Agentic Applications

- **ASI02 (Tool Misuse)** — the registry caps the callable set; the
  `when` predicate caps the args. Both enforced in deterministic
  Python at the runtime boundary, not via the LLM.
- **ASI03 (Identity & Privilege Abuse)** — tool registries are bound
  to an Agent's identity. Two agents in the same process with
  disjoint registries cannot cross-invoke each other's tools.

### Project

- 75 tests. Ruff clean. CI green on Python 3.10/3.11/3.12.
- No new runtime dependencies.

## [0.1.1] — 2026-05-12

### Added

- **`AuditSink.callback(fn)`** — invoke a user-supplied function on each
  audit event. Synchronous; callback exceptions are caught and logged to
  stderr, never propagated.
- **`AuditSink.webhook(url, *, headers=None, timeout=3.0)`** — POST each
  event as JSON to a URL. Uses stdlib `urllib` — no new runtime dependency.
  Network failures, non-2xx responses, and timeouts are caught.
- **`AuditSink.composite(*sinks)`** — fan one event out to multiple sinks.
  Per-child error isolation: a failure in one child cannot affect the
  others or the agent.

### Project

- 56 tests, 95% line coverage. Ruff clean.

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

[Unreleased]: https://github.com/arpitcoder/aegrail/compare/v0.2.5...HEAD
[0.2.5]: https://github.com/arpitcoder/aegrail/releases/tag/v0.2.5
[0.2.4]: https://github.com/arpitcoder/aegrail/releases/tag/v0.2.4
[0.2.3]: https://github.com/arpitcoder/aegrail/releases/tag/v0.2.3
[0.2.2]: https://github.com/arpitcoder/aegrail/releases/tag/v0.2.2
[0.2.1]: https://github.com/arpitcoder/aegrail/releases/tag/v0.2.1
[0.2.0]: # withdrawn from PyPI on 2026-05-13 — superseded by 0.2.1
[0.1.1]: https://github.com/arpitcoder/aegrail/releases/tag/v0.1.1
[0.1.0]: https://github.com/arpitcoder/aegrail/releases/tag/v0.1.0
