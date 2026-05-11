# agentctl

[![CI](https://github.com/agentctl/agentctl/actions/workflows/ci.yml/badge.svg)](https://github.com/agentctl/agentctl/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/agentctl.svg)](https://pypi.org/project/agentctl/)
[![Python](https://img.shields.io/pypi/pyversions/agentctl.svg)](https://pypi.org/project/agentctl/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

**The runtime contract for AI agents in production.**

A container runtime assumes deterministic code. An agent isn't deterministic. Run your agents on something that knows that.

---

## Why this exists

For 15 years, "container in production" meant **microservice**. Every piece of cloud-native infrastructure — Kubernetes, Istio, Prometheus, OPA — was designed around assumptions a microservice satisfies. Those assumptions are load-bearing.

An agent in a container looks identical. Same Dockerfile, same pod spec, same `kubectl apply`. But it violates almost every one of those assumptions:

| Property | Microservice | Agent |
|---|---|---|
| Output for the same input | Same | Different every time |
| Execution path | Coded, finite | Decided by the LLM at runtime |
| Cost per request | Sub-cent, predictable | $0.01 to $20+, unbounded |
| Outbound calls | Static dependency graph | LLM decides at runtime |
| Failure mode | Crash / 500 / timeout | "Confidently wrong" — returns 200 with garbage |
| Identity | Service identity | Service identity + invoking user + agent role |
| Trust boundary | Code trusted, input untrusted | Plus: the LLM's own decisions are untrusted |

The infrastructure stack hasn't caught up. That's why your agent looped for 63 hours and burned $4,200. That's why a malicious PR title made three production coding agents leak their own API keys. That's why your platform team can't tell you how many agents are in production right now.

**`agentctl` is the missing runtime layer.** Deterministic enforcement of identity, budget, and audit on top of any agent stack you already use.

---

## What it does (v0)

Three primitives. Nothing else.

1. **Scoped identity** — every agent run gets a session-bound principal. No shared API keys. Audit logs are identity-linked from line one.
2. **Hard budget kill-switches** — cost, tokens, wall-clock, recursion depth. The runtime stops the agent. Not the system prompt. Not the LLM. The runtime.
3. **Structured audit log** — identity-linked, append-only, replayable record of every prompt, tool call, and outcome. Forensic-grade, not debug-grade.

What it deliberately does **not** do (yet):
- Policy engine (v0.2)
- Egress allowlist proxy (v0.3)
- Approval gates (v0.4)
- Hosted dashboard (v1.0, paid)
- Prompt management or eval (integrate Langfuse — we don't compete)

---

## Install

```bash
pip install agentctl
```

Python 3.10+. Zero hard dependencies beyond `pydantic` and `httpx`. Works with any LLM provider (OpenAI, Anthropic, Bedrock, raw HTTP). Works alongside any agent framework (LangChain, LlamaIndex, MCP, custom).

---

## Hello world

```python
from agentctl import Agent, Budget, AuditSink

agent = Agent(
    identity="support-bot/v1",
    budget=Budget(usd=5.0, tokens=100_000, wall_seconds=120),
    audit=AuditSink.file("./audit.jsonl"),
)

with agent.session(user_id="alice", task="refund order #4521") as session:
    # Wrap your existing LLM call — provider-agnostic.
    response = session.llm.chat(
        model="claude-sonnet-4-5",
        messages=[{"role": "user", "content": "..."}],
    )

    # Wrap your existing tool call.
    result = session.tool("refund_api.refund", order_id=4521)
```

That's it. The session:

- Generates a short-lived per-session identity (`support-bot/v1@session-7f3a...`)
- Tracks tokens and dollars against the budget; raises `BudgetExceeded` deterministically when hit
- Emits a structured event for every LLM call and tool invocation, identity-linked, append-only
- Stops the agent if wall-clock or recursion limits are hit, no matter what the LLM "decides"

If the budget is exceeded mid-loop, the session raises. The agent cannot talk its way out of it.

---

## Real-provider examples

Working end-to-end demos with live LLM calls:

- [`examples/openai_demo.py`](examples/openai_demo.py) — OpenAI Chat Completions
- [`examples/anthropic_demo.py`](examples/anthropic_demo.py) — Anthropic Messages
- [`examples/basic.py`](examples/basic.py) — provider-free walkthrough
- [`examples/budget_kill.py`](examples/budget_kill.py) — the runtime stopping a runaway loop

```bash
pip install openai
export OPENAI_API_KEY=sk-...
python examples/openai_demo.py
```

---

## Where it fits next to what you already use

| Tool | What it does | Where agentctl fits |
|---|---|---|
| **Okta / Auth0 / WorkOS** | User identity, OAuth | Sits underneath — agentctl ties the user identity to per-session agent principals |
| **Langfuse / Helicone / LangSmith** | LLM observability and prompt management | Complementary — Langfuse is debug-grade, agentctl is enforcement-grade. Run both. |
| **Lakera / Prompt Security** | Input-layer prompt-injection filtering | Complementary — they guard inputs, agentctl guards actions |
| **LangChain / LlamaIndex / MCP / OpenAI Agents SDK** | Agent frameworks | agentctl wraps your sessions; you keep your framework |
| **OPA / Cedar** | General authorization policy | v0.2 will compose on top of these for action-layer policy |

agentctl is not a replacement for any of these. It is the **runtime layer** they all assume but none of them ship.

---

## What an audit event looks like

```json
{
  "ts": "2026-05-11T09:14:22.481Z",
  "session_id": "sess_01HQXY...",
  "agent_identity": "support-bot/v1",
  "invoking_user": "alice",
  "event": "tool_call",
  "tool": "refund_api.refund",
  "args": {"order_id": 4521},
  "result_summary": "ok",
  "tokens_in": 0,
  "tokens_out": 0,
  "cost_usd": 0.0,
  "elapsed_ms": 142,
  "trace_id": "tr_01HQXY..."
}
```

Designed so you can answer the question every team eventually asks: *what did the agent do at 14:23, and why?*

---

## Design principles

- **Wrapper, not framework.** `agentctl` works with your existing stack. We will never ask you to rewrite an agent to use us.
- **Deterministic enforcement.** The system prompt is not a security boundary. The runtime is.
- **Identity is first-class.** Every event ties to *agent identity + invoking user*. Authorization is the intersection.
- **Audit is forensic, not debug.** Append-only, structured, replayable. Not log lines.
- **Zero ambient credentials.** Sessions get short-lived scoped principals. Never share an API key.
- **Provider and framework agnostic.** OpenAI, Anthropic, Bedrock. LangChain, LlamaIndex, MCP, custom. We don't pick sides.

---

## Status

**v0.1 — early. Stable surface, narrow scope.** The three primitives shipped here are the foundation; expect them to remain backwards-compatible. v0.2 adds a policy engine, v0.3 adds the egress allowlist proxy.

48 tests, 94% line coverage, ruff clean. CI green on Python 3.10, 3.11, 3.12.

---

## Roadmap

- **v0.1** — scoped identity, budget kill-switches, audit log _(shipped)_
- **v0.1.x** — provider helpers (OpenAI/Anthropic/litellm), async/await, redaction rules
- **v0.2** — action-level policy DSL (`agent X may call tool Y only with args Z`)
- **v0.3** — egress allowlist proxy (network-level enforcement)
- **v0.4** — approval gates for irreversible actions
- **v1.0** — hosted control plane (paid)

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).

---

## License

Apache 2.0.

