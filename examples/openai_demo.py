"""End-to-end demo with the OpenAI Chat Completions API.

Makes a real LLM call and routes it through agentctl. Shows the
provider-agnostic shape of `record_llm` — agentctl never wraps the
provider SDK; you call it however you like and tell the runtime
what it cost.

Requirements:
    pip install openai
    export OPENAI_API_KEY=sk-...

Run:
    python examples/openai_demo.py
"""

from __future__ import annotations

import os
import sys
import time

from agentctl import Agent, AuditSink, Budget, BudgetExceeded

# Public OpenAI pricing snapshot. Kept inline; agentctl itself
# does not bake in a price table — cost calculation is the
# caller's responsibility.
PRICES_PER_MTOK = {
    # model: (input $/Mtok, output $/Mtok)
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-4.1": (2.0, 8.0),
}


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    inp, out = PRICES_PER_MTOK.get(model, (2.5, 10.0))
    return (tokens_in * inp + tokens_out * out) / 1_000_000


def main() -> None:
    try:
        from openai import OpenAI
    except ImportError:
        print("install openai first:  pip install openai", file=sys.stderr)
        sys.exit(2)

    if not os.environ.get("OPENAI_API_KEY"):
        print("set OPENAI_API_KEY first", file=sys.stderr)
        sys.exit(2)

    client = OpenAI()
    model = "gpt-4o-mini"

    agent = Agent(
        identity="demo-bot/v1",
        budget=Budget(usd=0.05, tokens=10_000, wall_seconds=30, max_tool_calls=3),
        audit=AuditSink.file("./audit_openai.jsonl"),
    )

    try:
        with agent.session(user_id="alice", task="answer a question") as session:
            started = time.monotonic()
            response = client.chat.completions.create(
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

            usage = response.usage
            tokens_in = usage.prompt_tokens if usage else 0
            tokens_out = usage.completion_tokens if usage else 0
            text = response.choices[0].message.content or ""

            session.record_llm(
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd(model, tokens_in, tokens_out),
                prompt_summary="why isn't an agent a microservice?",
                response_summary=text[:80] + "...",
                latency_ms=round(latency_ms, 1),
            )

            print("model:", model)
            print("response:", text)
            print("tokens:", tokens_in, "in /", tokens_out, "out")
            print("budget snapshot:", session.state_snapshot)
    except BudgetExceeded as exc:
        print(f"[runtime] killed by reason={exc.reason}: {exc}")

    agent.close()
    print("audit log written to ./audit_openai.jsonl")


if __name__ == "__main__":
    main()
