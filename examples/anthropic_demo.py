"""End-to-end demo with the Anthropic Messages API.

Makes a real LLM call and routes it through agentctl. Shows the
provider-agnostic shape of `record_llm` — agentctl never wraps the
provider SDK; you call it however you like and tell the runtime
what it cost.

Requirements:
    pip install anthropic
    export ANTHROPIC_API_KEY=sk-ant-...

Run:
    python examples/anthropic_demo.py
"""

from __future__ import annotations

import os
import sys
import time

from agentctl import Agent, AuditSink, Budget, BudgetExceeded

# Public Anthropic pricing as of 2026-05. Kept inline so the example
# stays self-contained — agentctl itself does not bake in a price
# table (cost calculation is the caller's responsibility).
PRICES_PER_MTOK = {
    # model: (input $/Mtok, output $/Mtok)
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-opus-4-7": (15.0, 75.0),
}


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    inp, out = PRICES_PER_MTOK.get(model, (3.0, 15.0))
    return (tokens_in * inp + tokens_out * out) / 1_000_000


def main() -> None:
    try:
        from anthropic import Anthropic
    except ImportError:
        print("install anthropic first:  pip install anthropic", file=sys.stderr)
        sys.exit(2)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("set ANTHROPIC_API_KEY first", file=sys.stderr)
        sys.exit(2)

    client = Anthropic()
    model = "claude-sonnet-4-5"

    agent = Agent(
        identity="demo-bot/v1",
        budget=Budget(usd=0.05, tokens=10_000, wall_seconds=30, max_tool_calls=3),
        audit=AuditSink.file("./audit_anthropic.jsonl"),
    )

    try:
        with agent.session(user_id="alice", task="answer a question") as session:
            started = time.monotonic()
            response = client.messages.create(
                model=model,
                max_tokens=200,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            "In one sentence: why isn't an AI agent the same as a microservice?"
                        ),
                    }
                ],
            )
            latency_ms = (time.monotonic() - started) * 1000

            tokens_in = response.usage.input_tokens
            tokens_out = response.usage.output_tokens

            session.record_llm(
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd(model, tokens_in, tokens_out),
                prompt_summary="why isn't an agent a microservice?",
                response_summary=response.content[0].text[:80] + "...",
                latency_ms=round(latency_ms, 1),
            )

            print("model:", model)
            print("response:", response.content[0].text)
            print("tokens:", tokens_in, "in /", tokens_out, "out")
            print("budget snapshot:", session.state_snapshot)
    except BudgetExceeded as exc:
        print(f"[runtime] killed by reason={exc.reason}: {exc}")

    agent.close()
    print("audit log written to ./audit_anthropic.jsonl")


if __name__ == "__main__":
    main()
