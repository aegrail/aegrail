"""Sample agent for the kind ConfigMap integration test (v0.2.6).

Validates that an operator can configure an aegrail-protected agent
end-to-end via a ConfigMap — no code change between platforms. The
container reads `AEGRAIL_*` env vars from envFrom: configMapRef and
constructs the Agent with `Agent.from_env()`. Tools still come from
code, which is the documented split.

Asserts:
  1. Agent constructed from env has the identity, budget, egress
     allowlist, and audit sink that the ConfigMap declared.
  2. A normal session can record an LLM event and a tool call without
     hitting the budget.
  3. The audit chain validates end-to-end.

Exits 0 on PASS, non-zero on FAIL. The orchestrator greps for the
PASS line.
"""

from __future__ import annotations

import json
import os
import sys

from aegrail import Agent, Tool
from aegrail.audit import AuditEvent, FileAuditSink, verify_chain


def main() -> int:
    expected_identity = "kind-configmap-agent/v1"
    expected_egress = ["api.openai.com", "*.anthropic.com"]
    audit_path = os.environ.get("AEGRAIL_AUDIT_FILE", "")

    if not audit_path:
        print("FAIL: AEGRAIL_AUDIT_FILE not set on the pod")
        return 1
    # Fresh-run: ensure no stale log from previous container restart
    if os.path.exists(audit_path):
        os.unlink(audit_path)

    agent = Agent.from_env(
        tools={"echo": Tool(name="echo", fn=lambda msg: msg)},
    )

    print(f"identity={agent.identity}")
    print(f"budget.usd={agent.budget.usd}")
    print(f"budget.tokens={agent.budget.tokens}")
    print(f"egress={agent.egress_allowlist}")
    print(f"audit_sink={type(agent.audit).__name__}")

    if agent.identity != expected_identity:
        print(f"FAIL: identity {agent.identity!r} != {expected_identity!r}")
        return 1
    if agent.budget.usd != 1.0:
        print(f"FAIL: budget.usd {agent.budget.usd} != 1.0")
        return 1
    if agent.budget.tokens != 5000:
        print(f"FAIL: budget.tokens {agent.budget.tokens} != 5000")
        return 1
    if agent.egress_allowlist != expected_egress:
        print(f"FAIL: egress {agent.egress_allowlist!r} != {expected_egress!r}")
        return 1
    if not isinstance(agent.audit, FileAuditSink):
        print(f"FAIL: audit sink {type(agent.audit).__name__} != FileAuditSink")
        return 1

    with agent.session(user_id="configmap-test", task="env-config check") as s:
        s.record_llm(model="qwen2.5:7b", tokens_in=100, tokens_out=50, cost_usd=0.001)
        out = s.call_tool("echo", msg="ping")
        if out != "ping":
            print(f"FAIL: tool returned {out!r} != 'ping'")
            return 1

    agent.close()

    with open(audit_path) as f:
        lines = [line for line in f.read().splitlines() if line.strip()]
    events = [AuditEvent.model_validate(json.loads(line)) for line in lines]
    ok, bad = verify_chain(events)
    if not ok:
        print(f"FAIL: audit chain broken at index {bad}")
        return 1
    print(f"OK: {len(events)} audit events, chain valid")
    print("PASS - aegrail v0.2.6 Agent.from_env() configured by ConfigMap")
    return 0


if __name__ == "__main__":
    sys.exit(main())
