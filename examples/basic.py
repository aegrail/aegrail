"""Minimal end-to-end example.

Run:
    python examples/basic.py

This shows the four primitives in action without any LLM
provider — `record_llm` is called manually with synthetic
numbers so the example has no external dependencies.
"""

from __future__ import annotations

from aegrail import Agent, AuditSink, Budget, BudgetExceeded, Tool


def refund(order_id: int) -> str:
    return f"refunded order {order_id}"


def main() -> None:
    agent = Agent(
        identity="support-bot/v1",
        budget=Budget(usd=0.10, tokens=10_000, wall_seconds=30, max_tool_calls=5),
        audit=AuditSink.file("./audit.jsonl"),
        tools={
            "refund_api.refund": Tool(
                name="refund_api.refund",
                fn=refund,
                description="Issue a refund for a customer order.",
                when=lambda args: isinstance(args.get("order_id"), int),
                redact=lambda args: {"order_id_present": "order_id" in args},
            ),
        },
    )

    try:
        with agent.session(user_id="alice", task="refund order 4521") as s:
            s.record_llm(
                model="claude-sonnet-4-5",
                tokens_in=120,
                tokens_out=300,
                cost_usd=0.012,
                prompt_summary="user wants refund for #4521",
                response_summary="call refund tool with order_id=4521",
            )

            result = s.call_tool("refund_api.refund", order_id=4521)
            print("tool result:", result)

            s.record_llm(
                model="claude-sonnet-4-5",
                tokens_in=80,
                tokens_out=40,
                cost_usd=0.004,
                response_summary="confirm refund to user",
            )

            print("final budget snapshot:", s.state_snapshot)
    except BudgetExceeded as exc:
        print(f"agent stopped by runtime: {exc.reason} — {exc}")

    agent.close()
    print("audit log written to ./audit.jsonl")


if __name__ == "__main__":
    main()
