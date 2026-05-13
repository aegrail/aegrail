# aegrail

[![CI](https://github.com/arpitcoder/aegrail/actions/workflows/ci.yml/badge.svg)](https://github.com/arpitcoder/aegrail/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/aegrail.svg)](https://pypi.org/project/aegrail/)
[![Python](https://img.shields.io/pypi/pyversions/aegrail.svg)](https://pypi.org/project/aegrail/)
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

**`aegrail` is the missing runtime layer.** Deterministic enforcement of identity, budget, and audit on top of any agent stack you already use.

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
pip install aegrail
```

> **Note:** the PyPI release lands with `v0.1.0`. Until then, install from source:
> ```bash
> git clone https://github.com/arpitcoder/aegrail
> cd aegrail && pip install -e .
> ```

Python 3.10+. Zero hard dependencies beyond `pydantic`. Works with any LLM provider (OpenAI, Anthropic, Bedrock, raw HTTP). Works alongside any agent framework (LangChain, LlamaIndex, MCP, custom).

---

## Hello world

```python
from aegrail import Agent, Budget, AuditSink

agent = Agent(
    identity="support-bot/v1",
    budget=Budget(usd=5.0, tokens=100_000, wall_seconds=120, max_tool_calls=10),
    audit=AuditSink.file("./audit.jsonl"),
)

def refund(order_id: int) -> str:
    # Your real tool — could be an API call, DB write, anything.
    return f"refunded order {order_id}"

with agent.session(user_id="alice", task="refund order #4521") as s:
    # 1. Call your LLM however you like (OpenAI SDK, Anthropic SDK, raw HTTP).
    #    Then tell the runtime what it cost. Provider-agnostic by design.
    s.record_llm(
        model="claude-sonnet-4-5",
        tokens_in=120,
        tokens_out=300,
        cost_usd=0.012,
    )

    # 2. Run a tool through the session — counted against the budget, audited.
    result = s.call_tool("refund_api.refund", refund, order_id=4521)
```

That's it. The session:

- Generates a short-lived per-session principal (`support-bot/v1@sess_<ms>_<rand>`)
- Tracks tokens and dollars against the budget; raises `BudgetExceeded` deterministically when hit
- Emits a structured event for every LLM call and tool invocation, identity-linked, append-only
- Stops the agent if wall-clock, recursion, or tool-call limits are hit, no matter what the LLM "decides"

If the budget is exceeded mid-loop, the session raises. The agent cannot talk its way out of it.

---

## First 60 seconds

```bash
git clone https://github.com/arpitcoder/aegrail
cd aegrail
pip install -e .

# Happy path — synthetic LLM call, real audit log.
python examples/basic.py

# The kill-switch — agent loops greedily, runtime stops it deterministically.
python examples/budget_kill.py
```

`examples/budget_kill.py` prints:

```
iteration 1: state={'tokens_used': 500, 'usd_used': 0.01, ...}
iteration 2: state={'tokens_used': 1000, 'usd_used': 0.02, ...}
iteration 3: state={'tokens_used': 1500, 'usd_used': 0.03, ...}
iteration 4: state={'tokens_used': 2000, 'usd_used': 0.04, ...}
iteration 5: state={'tokens_used': 2500, 'usd_used': 0.05, ...}

[runtime] killed by reason=usd: usd budget exceeded: 0.0600 > 0.0500
```

That's the `$4,200-weekend` scenario, prevented in code.

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

| Tool | What it does | Where aegrail fits |
|---|---|---|
| **Okta / Auth0 / WorkOS** | User identity, OAuth | Sits underneath — aegrail ties the user identity to per-session agent principals |
| **Langfuse / Helicone / LangSmith** | LLM observability and prompt management | Complementary — Langfuse is debug-grade, aegrail is enforcement-grade. Run both. |
| **Lakera / Prompt Security** | Input-layer prompt-injection filtering | Complementary — they guard inputs, aegrail guards actions |
| **LangChain / LlamaIndex / MCP / OpenAI Agents SDK** | Agent frameworks | aegrail wraps your sessions; you keep your framework |
| **OPA / Cedar** | General authorization policy | v0.2 will compose on top of these for action-layer policy |

aegrail is not a replacement for any of these. It is the **runtime layer** they all assume but none of them ship.

---

## What an audit event looks like

Every line of `audit.jsonl` is one event. Identity-linked, append-only, JSON.

```json
{
  "ts": "2026-05-11T09:14:22.481Z",
  "session_id": "sess_1778480062481_4bf0a4f8cf1c",
  "agent_identity": "support-bot/v1",
  "invoking_user": "alice",
  "principal": "support-bot/v1@sess_1778480062481_4bf0a4f8cf1c",
  "event": "tool_call",
  "payload": {
    "tool": "refund_api.refund",
    "args": {"order_id": 4521},
    "ok": true,
    "elapsed_ms": 0.42
  },
  "budget": {
    "tokens_used": 420,
    "usd_used": 0.012,
    "tool_calls": 1,
    "recursion_depth": 0,
    "wall_elapsed": 0.18
  }
}
```

Top-level fields are flat for log-ingestion friendliness (ship to S3, ClickHouse, Loki, Datadog, anything that takes JSONL). `payload` carries event-specific detail; `budget` carries a snapshot of consumption *at the moment of emission*, so you can reconstruct cost-over-time from the log alone.

Designed so you can answer the question every team eventually asks: *what did the agent do at 14:23, and why?*

---

## Alerts and fanout

The three core sinks (`file`, `stdout`, `memory`) cover persistence. Three more cover routing:

```python
from aegrail import Agent, AuditSink, Budget


def on_event(evt):
    if evt.event == "budget_exceeded":
        # Send to PagerDuty, Slack, your incident pipeline — anything.
        ...


agent = Agent(
    identity="payments-bot/v1",
    budget=Budget(usd=5.0, wall_seconds=120),
    audit=AuditSink.composite(
        AuditSink.file("./audit.jsonl"),                          # forensic record
        AuditSink.webhook("https://alerts.example.com/aegrail"), # real-time
        AuditSink.callback(on_event),                             # in-process routing
    ),
)
```

- **`AuditSink.callback(fn)`** — invoke a Python function on every event. Synchronous; exceptions are caught.
- **`AuditSink.webhook(url, *, headers=None, timeout=3.0)`** — POST events as JSON. Stdlib only, no `requests` dependency. Network errors, non-2xx responses, and timeouts are caught.
- **`AuditSink.composite(*sinks)`** — fan out to multiple sinks. A failure in one child cannot affect the others — every child is isolated.

Sink failures **never** break the agent. Every sink wraps its write path; errors land on stderr.

---

## Design principles

- **Wrapper, not framework.** `aegrail` works with your existing stack. We will never ask you to rewrite an agent to use us.
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

