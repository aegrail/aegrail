<div align="center">

<img src=".assets/logo.svg" alt="aegrail" width="140">

# aegrail

**Layer 4 for AI agents. The runtime governance contract that every production agent is missing.**

[![CI](https://img.shields.io/github/actions/workflow/status/aegrail/aegrail/ci.yml?style=flat-square&label=CI&logo=github&logoColor=white&color=2DD4BF&labelColor=0F172A)](https://github.com/aegrail/aegrail/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/aegrail?style=flat-square&label=PyPI&color=2DD4BF&labelColor=0F172A)](https://pypi.org/project/aegrail/)
[![Python](https://img.shields.io/pypi/pyversions/aegrail?style=flat-square&label=Python&color=2DD4BF&labelColor=0F172A)](https://pypi.org/project/aegrail/)
[![License](https://img.shields.io/badge/license-Apache%202.0-2DD4BF?style=flat-square&labelColor=0F172A)](LICENSE)
[![Engine](https://img.shields.io/badge/engine-v0.4.3-2DD4BF?style=flat-square&labelColor=0F172A&logo=kubernetes&logoColor=white)](https://github.com/aegrail/aegrail-engine)

</div>

A small, opinionated, provider-neutral runtime that enforces identity, budget, audit, and capability ACL on AI agents — across any LLM provider, any framework, any vertical. Engineers can install it on Friday. CISOs can read the code on Monday.

---

## The missing Layer 4

Every production AI agent stack looks like this:

```
┌──────────────────────────────────────────────────────────┐
│  Layer 5: Domain business logic                          │  insurance / finance / health / legal
├──────────────────────────────────────────────────────────┤
│  Layer 4: Runtime governance contract       ← THE GAP    │  identity, budget, audit, ACL
├──────────────────────────────────────────────────────────┤
│  Layer 3: Agent framework                                │  LangChain, LlamaIndex, AutoGen, MCP
├──────────────────────────────────────────────────────────┤
│  Layer 2: LLM provider SDK                               │  openai, anthropic, google-genai, bedrock
├──────────────────────────────────────────────────────────┤
│  Layer 1: LLM API                                        │  gpt-4o, claude-opus, gemini, llama
└──────────────────────────────────────────────────────────┘
```

Layer 4 is missing. Frameworks won't fill it (they compete on ergonomics, not opinions). SDKs can't (they ship per-provider). Domain teams shouldn't (every team re-inventing audit chains badly). The model itself can't (it's the thing you're trying to constrain).

So every company building agents in production today either reinvents Layer 4 in-house — eight engineers, eight repos, eight broken audit formats — or pretends it doesn't exist and waits for the regulator. `aegrail` is the open-source implementation of Layer 4.

---

## Why agents specifically need it

A container runtime assumes deterministic code. An agent isn't deterministic. The cloud-native stack — Kubernetes, Istio, Prometheus, OPA — was built around assumptions a microservice satisfies. Agents violate them:

| Property | Microservice | Agent |
|---|---|---|
| Output for the same input | Same | Different every time |
| Execution path | Coded, finite | Decided by the LLM at runtime |
| Cost per request | Sub-cent, predictable | $0.01 to $20+, unbounded |
| Outbound calls | Static dependency graph | LLM decides at runtime |
| Failure mode | Crash / 500 / timeout | "Confidently wrong" — returns 200 with garbage |
| Identity | Service identity | Service identity + invoking user + agent role |
| Trust boundary | Code trusted, input untrusted | Plus: the LLM's own decisions are untrusted |

That's why your agent looped for 63 hours and burned $4,200. Why a malicious PR title made three production coding agents leak their own API keys. Why your platform team can't tell you how many agents are running right now.

Layer 4 codifies the runtime contract that fills the gap — the same way Kubernetes codified the microservice contract in 2014.

---

## What it does

Five primitives. Nothing else.

1. **Scoped identity** — every agent run gets a session-bound principal of the form `<agent_identity>@sess_<ms>_<rand>`. The invoking user is stamped alongside. Audit logs are identity-linked from line one. No shared API keys.

2. **Hard budget kill-switches** — USD, tokens, wall-clock, recursion depth, tool calls. The runtime raises `BudgetExceeded` from Python (or returns HTTP 429 from the engine). Not the system prompt. Not the LLM. The runtime.

3. **Tamper-evident audit log** — append-only JSONL with SHA-256 chain links between events. Identity-linked, replayable, regulator-presentable. Provider-neutral schema: same fields whether the call was Anthropic or OpenAI or Bedrock or Vertex.

4. **Per-agent tool ACL** — each agent declares its tool registry at construction. Tools not in the registry cannot be called, regardless of what the LLM decides. Optional argument predicates. Three machine-readable deny reasons: `not_registered`, `predicate_false`, `predicate_error`. Maps to **OWASP Top 10 for Agentic Applications**: **ASI02 (Tool Misuse)** and **ASI03 (Identity & Privilege Abuse)**.

5. **Egress + data boundary** _(network layer, via [`aegrail-engine`](https://github.com/aegrail/aegrail-engine))_ — the Go sidecar enforces allowlist + token budgets + audit chain at the L7 HTTPS boundary. Works for any agent in any language, including ones that never imported the Python SDK. HTTPS MITM with parsers for OpenAI / Anthropic / Bedrock / Vertex / Gemini / Ollama response shapes.

---

## Provider-neutral by design

A serious production agent uses multiple providers — Anthropic for reasoning, OpenAI for structured output, Vertex Gemini for vision, Bedrock for data-residency. Layer 4 has to abstract over Layer 2.

| Capability | OpenAI SDK | Anthropic SDK | Bedrock | Vertex | LangChain | `aegrail` |
|---|---|---|---|---|---|---|
| Cross-provider identity | — | — | IAM | SA | callbacks | **provider-neutral principal** |
| Cross-provider cost rollup | — | — | — | — | partial | **single Budget** |
| Hard kill-switch at runtime boundary | — | — | — | — | — | **`BudgetExceeded`** |
| Tamper-evident audit chain | — | — | — | — | — | **SHA-256 JSONL chain** |
| Per-agent tool registry with arg predicates | — | — | — | — | partial | **ASI02/03 aligned** |
| Egress allowlist at L7 (any language) | — | — | VPC | VPC | — | **engine sidecar** |
| Token budget on direct HTTPS | — | — | — | — | — | **MITM proxy + parser** |
| Provider-neutral event schema | — | — | — | — | partial | **yes** |

`session.record_llm(cost_usd=..., prompt_tokens=..., completion_tokens=..., model=...)` accepts numbers, not provider objects. Where the caller got the numbers (provider response, LiteLLM, internal price table) is the caller's problem. The library enforces.

---

## What it deliberately does NOT do

Layer 4's value is in being small and load-bearing, not big and ergonomic. `aegrail` is not:

- **Not a prompt firewall** — compose with Lakera or Prompt Security upstream
- **Not an eval framework** — use Langfuse, Braintrust, or Galileo
- **Not a model gateway** — use LiteLLM or your provider's SDK
- **Not a multi-agent orchestrator** — use LangGraph / CrewAI / AutoGen / MCP; the contract holds regardless
- **Not an IAM replacement** — identity attribution, yes; authentication, no

---

## Adoption shape

| Layer | Component | License | Status |
|---|---|---|---|
| In-process SDK | [`aegrail`](https://github.com/aegrail/aegrail) (Python) | Apache 2.0 | Stable |
| Network sidecar | [`aegrail-engine`](https://github.com/aegrail/aegrail-engine) (Go + Helm) | Apache 2.0 | Stable |
| Hosted control plane | Fleet inventory, audit search, RBAC, SOC2 inheritance | Commercial | Roadmap (v1.0) |

The two open-source components are Apache 2.0 and will stay that way. A security library you can't audit is not a security library. The future hosted control plane is the commercial layer — same shape as Sentry, Grafana, GitLab. If you self-host everything, you're never blocked.

---

## Install

```bash
pip install aegrail
```

> **Note:** the first PyPI release will be `v0.2.0`. Until then, install from source:
> ```bash
> git clone https://github.com/aegrail/aegrail
> cd aegrail && pip install -e .
> ```

Python 3.10+. Zero hard dependencies beyond `pydantic`. Works with any LLM provider (OpenAI, Anthropic, Bedrock, raw HTTP). Works alongside any agent framework (LangChain, LlamaIndex, MCP, custom).

---

## Hello world

```python
from aegrail import Agent, AuditSink, Budget, Tool

def refund(order_id: int) -> str:
    # Your real tool — could be an API call, DB write, anything.
    return f"refunded order {order_id}"

agent = Agent(
    identity="support-bot/v1",
    budget=Budget(usd=5.0, tokens=100_000, wall_seconds=120, max_tool_calls=10),
    audit=AuditSink.file("./audit.jsonl"),
    tools={
        "refund_api.refund": Tool(
            name="refund_api.refund",
            fn=refund,
            description="Issue a refund for a customer order.",
            when=lambda args: isinstance(args.get("order_id"), int),
        ),
    },
)

with agent.session(user_id="alice", task="refund order #4521") as s:
    # 1. Call your LLM however you like (OpenAI SDK, Anthropic SDK, raw HTTP).
    #    Then tell the runtime what it cost. Provider-agnostic by design.
    s.record_llm(
        model="claude-sonnet-4-5",
        tokens_in=120,
        tokens_out=300,
        cost_usd=0.012,
    )

    # 2. Run a registered tool through the session — looked up by name,
    #    arg predicate enforced, counted against the budget, audited.
    result = s.call_tool("refund_api.refund", order_id=4521)
```

That's it. The session:

- Generates a short-lived per-session principal (`support-bot/v1@sess_<ms>_<rand>`)
- Tracks tokens and dollars against the budget; raises `BudgetExceeded` deterministically when hit
- Emits a structured event for every LLM call, tool invocation, and policy denial — identity-linked, append-only
- Refuses tools the agent is not registered for, or tool args that fail the `when` predicate — raising `ToolNotPermitted` deterministically (mapped to OWASP ASI02 / ASI03)
- Stops the agent if wall-clock, recursion, or tool-call limits are hit, no matter what the LLM "decides"

If the budget is exceeded mid-loop, or a tool is denied, the session raises. The agent cannot talk its way out of it.

---

## Async — `AsyncSession` (v0.2.2)

For agents running on `asyncio` (FastAPI, MCP servers, anything using the OpenAI/Anthropic async clients), use `agent.async_session(...)`:

```python
import asyncio
from aegrail import Agent, AuditSink, Budget, Tool

async def real_refund(order_id: int) -> str:
    # any async work here — DB call, async HTTP, etc.
    return f"refunded {order_id}"

agent = Agent(
    identity="support-bot/v1",
    budget=Budget(usd=5.0, wall_seconds=30, max_tool_calls=10),
    audit=AuditSink.file("./audit.jsonl"),
    tools={"refund": Tool(name="refund", fn=real_refund)},
)

async def main() -> None:
    async with agent.async_session(user_id="alice") as s:
        await s.record_llm(model="gpt-4", tokens_in=100, tokens_out=200, cost_usd=0.01)
        result = await s.call_tool("refund", order_id=4521)
        print(result)

asyncio.run(main())
```

The async surface mirrors the sync one — same exceptions, same audit events, same tool ACL semantics — and adds one load-bearing property: **`wall_seconds` is enforced mid-tool-call** via `asyncio.wait_for`. If a tool call hangs past the remaining wall-clock budget, the runtime raises `BudgetExceeded('wall_seconds')` deterministically, rather than waiting for the call to return. Sync `Session` could only check at event boundaries.

Tool functions can be sync or async — the runtime detects via `inspect.iscoroutinefunction` and dispatches accordingly. Sync functions are wrapped in `asyncio.to_thread(...)` so the timeout still applies at the asyncio level.

Full async demo (against local Ollama, no API key): [`examples/async_demo.py`](examples/async_demo.py).

---

## First 60 seconds

```bash
git clone https://github.com/aegrail/aegrail
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
- [`examples/multi_agent_acl.py`](examples/multi_agent_acl.py) — _(v0.2)_ FinOps and Architect agents in one process, with cross-agent tool denial enforced deterministically

```bash
pip install openai
export OPENAI_API_KEY=sk-...
python examples/openai_demo.py
```

---

## Tool ACL — v0.2

Each `Agent` carries an explicit catalogue of tools it is permitted to invoke. Two agents in the same process with disjoint registries cannot cross-invoke each other's tools, no matter what the LLM is instructed to do.

```python
from aegrail import Agent, AuditSink, Budget, Tool, ToolNotPermitted

finops = Agent(
    identity="finops/v1",
    budget=Budget(usd=1.0, max_tool_calls=10),
    audit=AuditSink.stdout(),
    tools={
        "cost_report": Tool(
            name="cost_report",
            fn=lambda period: f"AWS spend {period}: $84,201.47",
            when=lambda args: args.get("period") in {"mtd", "qtd", "ytd"},
        ),
    },
)

architect = Agent(
    identity="architect/v1",
    budget=Budget(usd=1.0, max_tool_calls=10),
    audit=AuditSink.stdout(),
    tools={
        "deploy_infra": Tool(
            name="deploy_infra",
            fn=lambda env: f"deployed infra to {env}",
            when=lambda args: args.get("env") in {"staging", "prod"},
        ),
    },
)

with finops.session(user_id="alice") as s:
    try:
        s.call_tool("deploy_infra", env="prod")  # not in finops's registry
    except ToolNotPermitted as exc:
        print(exc.reason)   # 'not_registered'
        print(exc.tool_name)  # 'deploy_infra'
```

Three denial reasons surface on `ToolNotPermitted.reason`:

- `'not_registered'` — the tool name isn't in this agent's registry (ASI03).
- `'predicate_false'` — the tool's `when(args)` predicate returned `False` (ASI02).
- `'predicate_error'` — the predicate raised; the original exception is on `__cause__`.

Every denial emits a `tool_denied` audit event with the agent's identity, principal, and a snapshot of the budget — so denied attempts are forensically queryable, not just thrown away.

Tools also accept an optional `redact(args) -> dict` to control what shows up in the audit payload's `args` field. The default emits **keys only**, never values.

---

## Where this sits — defense-in-depth at the capability layer

aegrail's tool ACL is one of three complementary layers. Each protects against a different threat; none replaces the others.

| Layer | Enforces | Threat it stops | aegrail role |
|---|---|---|---|
| **Network egress (L3/L4)** | Which hosts/ports the pod can reach | An agent dials an unapproved domain | _Out of scope today_ — use Kubernetes NetworkPolicy, Cilium, an egress proxy. v0.3 will add a proxy. |
| **Tool ACL (L7 capability)** | Which named callables an identity may invoke, and with what args | A FinOps agent invokes a deploy tool because the LLM was prompt-injected to | **This is v0.2.** |
| **Process isolation** | What the OS lets the agent's process do | A compromised agent reads another agent's memory or files | _Out of scope_ — use containers, gVisor, Firecracker, separate pods. |

Two agents in the same pod look identical to network policy: same source IP, same kube ServiceAccount, same outbound CIDR. The L3/L4 layer cannot tell them apart, which is why functional limits — *what tool a given identity may call* — must live at L7. That's what aegrail enforces, deterministically, in Python at the runtime boundary.

**The discipline this requires.** aegrail only governs actions that flow through `session.call_tool(...)`. An agent that imports `requests` and POSTs to a banking API directly is invisible to the runtime: no audit event, no ACL check, no budget update. The contract is to register every sensitive action as a `Tool` and invoke it through the session. The library cannot prevent off-path bypasses without process-level isolation, which is intentionally out of scope.

Use aegrail v0.2 *with* network policy and process isolation, not as a substitute. Defense-in-depth only works when the layers compose.

---

## Where it fits next to what you already use

| Tool | What it does | Where aegrail fits |
|---|---|---|
| **Okta / Auth0 / WorkOS** | User identity, OAuth | Sits underneath — aegrail ties the user identity to per-session agent principals |
| **Langfuse / Helicone / LangSmith** | LLM observability and prompt management | Complementary — Langfuse is debug-grade, aegrail is enforcement-grade. Run both. |
| **Lakera / Prompt Security** | Input-layer prompt-injection filtering | Complementary — they guard inputs, aegrail guards actions |
| **LangChain / LlamaIndex / MCP / OpenAI Agents SDK** | Agent frameworks | aegrail wraps your sessions; you keep your framework |
| **OPA / Cedar** | General authorization policy | Complementary — aegrail v0.2 ships per-agent tool ACL in Python; a future release may compose with OPA/Cedar for org-wide policy |

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
    "description": "Issue a refund for a customer order.",
    "args": {"kwarg_keys": ["order_id"]},
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

**v0.2 — narrow scope, growing surface.** Identity, budget, audit, and now the per-agent tool ACL. v0.3 adds the egress allowlist proxy; v0.4 adds approval gates.

109 tests (75 sync + 16 async + 11 chain + 7 schema), ruff clean. CI green on Python 3.10, 3.11, 3.12.

For SOC 2 / ISO 27001 / NIST SP 800-53 control mappings and audit evidence extraction recipes, see [`COMPLIANCE.md`](COMPLIANCE.md).

For K8s deployment patterns (developer-effortless `AEGRAIL_INTERCEPT=1` env-var enforcement, plus a working kind cluster integration test), see [`docs/kubernetes.md`](docs/kubernetes.md).

---

## Roadmap

- **v0.1** — scoped identity, budget kill-switches, audit log _(shipped)_
- **v0.1.x** — alerting sinks (callback/webhook/composite) _(shipped)_
- **v0.2** — per-agent tool ACL with arg predicates (OWASP ASI02 + ASI03) _(shipped)_
- **v0.2.2** — `AsyncSession` with hard `wall_seconds` enforcement mid-tool-call _(shipped)_
- **v0.2.3** — tamper-evident audit chain + `COMPLIANCE.md` (SOC 2 / ISO 27001 / NIST mappings) + Tool schema exports for OpenAI/Anthropic _(shipped)_
- **v0.2.x** — provider helpers (OpenAI/Anthropic/litellm)
- **v0.3** — egress allowlist proxy (network-level enforcement)
- **v0.4** — approval gates for irreversible actions
- **v1.0** — hosted control plane (paid)

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).

---

## License

Apache License 2.0. See [LICENSE](LICENSE) for full terms.

Copyright © 2026 [Arpit Nigam](https://github.com/arpitcoder).

`aegrail` is permissively licensed for commercial and non-commercial use. Contributions are welcome under the same license — see [CONTRIBUTING.md](CONTRIBUTING.md).

