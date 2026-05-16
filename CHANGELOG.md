# Changelog

All notable changes to `aegrail` are documented in this file.
The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.1] — 2026-05-16

### Added — Anthropic SDK auto-instrumentation

Same shape as the OpenAI integration shipped in 0.3.0, now extended
to the Anthropic Python SDK. With `AEGRAIL_INTERCEPT=1` set,
constructing an Agent patches:

- `anthropic.resources.messages.Messages.create` (sync)
- `anthropic.resources.messages.AsyncMessages.create` (async)
- The `anthropic.resources.beta.messages` equivalents when present

Each call now transparently runs `session.check_budget()` pre-call,
runs the original method, then extracts model + usage and calls
`session.record_llm(...)`. Includes first-class support for
Anthropic's prompt-caching attribution:

- `cache_read_input_tokens` → `cache_read_tokens` in the audit event
- `cache_creation_input_tokens` → `cache_write_tokens`

Streaming requests pass through (the SSE stream object would need
shape-specific handling for the final `message_stop` usage frame).

8 new tests in `tests/test_anthropic_integration.py` covering patch
installation, idempotency, sync token recording with cache fields,
no-session passthrough, streaming passthrough, post-record budget
breach, pre-check rejection, and async coroutine flow. Total suite
is now 169 tests, all green.

## [0.3.0] — 2026-05-16

### Added — OpenAI SDK auto-instrumentation

The adoption story. With `AEGRAIL_INTERCEPT=1` set, constructing an
Agent now patches the OpenAI Python SDK in place so every `chat.
completions.create` and `responses.create` call (sync and async)
transparently:

1. Looks up the active session from `aegrail.session.current_session`
   (the existing ContextVar).
2. Calls `session.check_budget()` before the request — already-
   exceeded ceilings fail-fast with `BudgetExceeded` and the upstream
   call never happens.
3. Runs the original OpenAI method.
4. Extracts model + token usage from the response (supports both
   Chat Completions and Responses API shapes; reads
   `prompt_tokens_details.cached_tokens` for cache attribution).
5. Calls `session.record_llm(...)`, which emits the `llm_call` audit
   event and updates the budget state. Token-budget violations raise
   `BudgetExceeded` post-record.

No code change is required by the agent author. The pattern works
for OpenAI directly and for any code that wraps `openai.OpenAI` /
`openai.AsyncOpenAI` (LangChain's `ChatOpenAI` and similar use the
SDK underneath and get coverage transparently).

Streaming requests (`stream=True`) pass through; usage isn't
available until the final chunk and the wrapped stream object would
need shape-specific handling. The caller can still record manually
via `session.record_llm(...)` after consuming the stream.

Cost calculation stays the caller's responsibility (no baked-in
price table — see `CLAUDE.md` design principle). Auto-recorded
`cost_usd=0.0`; budgets on `tokens`, `wall_seconds`, `max_recursion`,
`max_tool_calls` still fire.

### Added — `aegrail.integrations` module

New module surface for provider SDK adapters. `aegrail.integrations.
install_all()` is called automatically by `Agent.__init__` when
`AEGRAIL_INTERCEPT=1` is set; failures during install are caught
and logged at WARNING (per design principle #8: integrations must
never break the caller).

Currently shipping: `openai`. Anthropic and litellm will land as
0.3.0.x fast-follows.

### Tests

8 new tests in `tests/test_openai_integration.py` covering: patch
installation, idempotent re-install, sync token recording + audit,
no-session passthrough, streaming passthrough, post-record budget
breach, pre-check rejection when state is already over the ceiling,
and async coroutine flow. Total suite is now 161 tests; all green
on Python 3.10 / 3.11 / 3.12.

## [0.2.7] — 2026-05-15

### Added — engine event types in `AuditEvent`

- `EventType` literal now accepts `engine_start`, `engine_shutdown`,
  `engine_heartbeat`, `egress_allowed`, `egress_error` so
  `AuditEvent.model_validate` and `verify_chain` can walk chains
  produced by the aegrail-engine Go sidecar end-to-end. One verifier
  function handles SDK + engine events; auditors don't need
  language-aware tooling.
- Validated by the engine's kind+Ollama integration test, which
  produces a 5-event chain (engine_start, two egress_allowed, two
  egress_denied) from Go and confirms `verify_chain` returns
  `(True, -1)` from Python over the exact same JSONL.

## [0.2.6] — 2026-05-15

### Added — env-var configuration for containerised deployments

- **`Agent.from_env(...)`** and **`Budget.from_env()`** classmethods.
  Read sensible defaults from `AEGRAIL_*` environment variables so the
  same Dockerfile can run on Cloud Run, AWS App Runner, Azure Container
  Apps, AWS Fargate, or Kubernetes with deployment-time configuration
  rather than code changes. Explicit kwargs always win; env vars are
  fallback defaults.
- **Env var surface:**
  - `AEGRAIL_AGENT_IDENTITY` — agent identity string (required if not
    passed)
  - `AEGRAIL_BUDGET_USD` / `AEGRAIL_BUDGET_TOKENS` /
    `AEGRAIL_BUDGET_WALL_SECONDS` / `AEGRAIL_BUDGET_MAX_RECURSION` /
    `AEGRAIL_BUDGET_MAX_TOOL_CALLS` — at least one must be set
  - `AEGRAIL_EGRESS_ALLOWLIST` — comma-separated host patterns
  - `AEGRAIL_AUDIT_FILE=/path` — route audit to a file sink
  - `AEGRAIL_AUDIT_STDOUT=1` — explicit stdout sink
  - `AEGRAIL_INTERCEPT=1` — auto-install in-process interceptors
    (existing behavior, now documented alongside its peers)
- **Tools still come from code.** `Tool` instances carry callable
  functions and `when=` predicates that cannot be serialised to env
  vars. The future external policy-file feature (roadmap v0.2.x) adds
  operator-controlled tool gating on top of code-registered tools.
- **Tests:** 16 new tests covering each env var path, explicit-kwarg
  override behavior, parse-failure messages, and the required-axis
  guarantee. Total suite now passes on Python 3.10/3.11/3.12.

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
