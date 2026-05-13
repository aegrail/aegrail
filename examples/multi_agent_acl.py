"""Multi-agent tool ACL — the v0.2 headline demo.

Two agents in one process. Each has its own bound tool registry.
The FinOps agent cannot invoke the Architect agent's deploy_infra
tool, even if instructed to, because its identity has no entry for
that tool name.

The runtime enforces this in deterministic Python at the boundary,
mapping directly to:

  OWASP Top 10 for Agentic Applications
    ASI02 — Tool Misuse
    ASI03 — Identity & Privilege Abuse

Run:
    python examples/multi_agent_acl.py
"""

from __future__ import annotations

from aegrail import Agent, AuditSink, Budget, Tool, ToolNotPermitted


def cost_report(period: str) -> str:
    return f"AWS spend {period}: $84,201.47"


def deploy_infra(env: str) -> str:
    return f"deployed infra to {env}"


def main() -> None:
    sink = AuditSink.file("./audit_multi_agent.jsonl")
    common_budget = Budget(usd=1.0, max_tool_calls=20, wall_seconds=30)

    finops = Agent(
        identity="finops/v1",
        budget=common_budget,
        audit=sink,
        tools={
            "cost_report": Tool(
                name="cost_report",
                fn=cost_report,
                description="Pull a cost report for an accounting period.",
                when=lambda args: args.get("period") in {"mtd", "qtd", "ytd"},
            ),
        },
    )

    architect = Agent(
        identity="architect/v1",
        budget=common_budget,
        audit=sink,
        tools={
            "deploy_infra": Tool(
                name="deploy_infra",
                fn=deploy_infra,
                description="Apply a terraform plan to a named environment.",
                when=lambda args: args.get("env") in {"staging", "prod"},
            ),
        },
    )

    print("--- finops agent: in-scope call ---")
    with finops.session(user_id="alice", task="month-to-date spend") as s:
        print("  cost_report(mtd) ->", s.call_tool("cost_report", period="mtd"))

    print()
    print("--- finops agent: tries the architect's tool ---")
    with finops.session(user_id="alice", task="prompt-injected to deploy") as s:
        try:
            s.call_tool("deploy_infra", env="prod")
        except ToolNotPermitted as exc:
            print(f"  denied: reason={exc.reason!r} tool={exc.tool_name!r}")
            print(f"          {exc}")

    print()
    print("--- finops agent: tool with out-of-policy args ---")
    with finops.session(user_id="alice", task="ask for all-time") as s:
        try:
            s.call_tool("cost_report", period="all_time")
        except ToolNotPermitted as exc:
            print(f"  denied: reason={exc.reason!r} tool={exc.tool_name!r}")

    print()
    print("--- architect agent: its own tool is allowed ---")
    with architect.session(user_id="bob", task="deploy staging") as s:
        print("  deploy_infra(staging) ->", s.call_tool("deploy_infra", env="staging"))

    finops.close()
    architect.close()
    print()
    print("audit log written to ./audit_multi_agent.jsonl")
    print("controls demonstrated: OWASP ASI02 (Tool Misuse), ASI03 (Identity & Privilege Abuse)")


if __name__ == "__main__":
    main()
