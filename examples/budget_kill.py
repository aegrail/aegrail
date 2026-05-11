"""Demonstrates the budget kill-switch.

The agent loops greedily and would never stop on its own. The
runtime stops it deterministically when the budget is exceeded —
the $4,200-weekend scenario, prevented in code.
"""

from __future__ import annotations

from agentctl import Agent, AuditSink, Budget, BudgetExceeded


def main() -> None:
    agent = Agent(
        identity="loopy-bot/v1",
        budget=Budget(usd=0.05, max_tool_calls=100, wall_seconds=10),
        audit=AuditSink.file("./audit_kill.jsonl"),
    )

    try:
        with agent.session(user_id="alice", task="retry forever") as s:
            iteration = 0
            while True:
                iteration += 1
                s.record_llm(
                    model="claude-sonnet-4-5",
                    tokens_in=200,
                    tokens_out=300,
                    cost_usd=0.01,
                )
                print(f"iteration {iteration}: state={s.state_snapshot}")
    except BudgetExceeded as exc:
        print()
        print(f"[runtime] killed by reason={exc.reason}: {exc}")
        print(f"[runtime] final state={exc.state.snapshot()}")  # type: ignore[union-attr]

    agent.close()


if __name__ == "__main__":
    main()
