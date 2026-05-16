"""Sample aegrail-protected agent service for AWS App Runner.

A minimal FastAPI app showing the deployment pattern this repo
documents: ALL aegrail configuration (identity, budget, egress
allowlist, audit destination) comes from AEGRAIL_* env vars set on
the App Runner service. Application code calls Agent.from_env() and
never names the values.

Endpoints:
  GET /         -> health check (App Runner default)
  GET /healthz  -> explicit health check
  POST /chat    -> body: {"message": "..."}; runs one session,
                   calls OpenRouter for a completion, returns the
                   reply plus the aegrail state snapshot

LLM provider: OpenRouter (https://openrouter.ai) — set
OPENROUTER_API_KEY on the App Runner service. The container never
embeds the key.

Audit destination: defaults to stdout sink (AEGRAIL_AUDIT_STDOUT=1),
which streams into CloudWatch via App Runner's log forwarder.
"""

from __future__ import annotations

import os
import sys
import urllib.error
import urllib.request

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from aegrail import Agent, BudgetExceeded


def _build_agent() -> Agent:
    """Construct the agent once at module import time.

    Identity, budget, egress allowlist, and audit destination all
    come from AEGRAIL_* env vars (Agent.from_env). Tools must be
    passed explicitly — they hold callable objects that can't
    serialize through env. This sample registers none; the chat
    endpoint only does an LLM call.
    """
    try:
        return Agent.from_env()
    except ValueError as exc:
        print(f"aegrail config error: {exc}", file=sys.stderr)
        raise


AGENT = _build_agent()
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "openai/gpt-4o-mini")

app = FastAPI(title="aegrail App Runner sample", version="0.2.6")


class ChatIn(BaseModel):
    message: str


class ChatOut(BaseModel):
    reply: str
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    aegrail_state: dict


@app.get("/")
def root() -> dict:
    return {
        "service": "aegrail-app-runner-sample",
        "identity": AGENT.identity,
        "budget": AGENT.budget.model_dump(),
    }


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


@app.post("/chat", response_model=ChatOut)
def chat(body: ChatIn) -> ChatOut:
    if not OPENROUTER_KEY:
        raise HTTPException(500, "OPENROUTER_API_KEY env var not set")

    with AGENT.session(user_id="app-runner-demo", task="chat") as s:
        try:
            reply, model_used, tokens_in, tokens_out = _call_openrouter(body.message)
        except urllib.error.URLError as exc:
            raise HTTPException(502, f"openrouter call failed: {exc}") from exc

        cost = _estimate_cost(model_used, tokens_in, tokens_out)
        try:
            s.record_llm(
                model=model_used,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost,
            )
        except BudgetExceeded as exc:
            raise HTTPException(429, f"aegrail budget exceeded: {exc.reason}") from exc

        return ChatOut(
            reply=reply,
            model=model_used,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
            aegrail_state=s.state_snapshot,
        )


def _call_openrouter(message: str) -> tuple[str, str, int, int]:
    """One-shot completion via OpenRouter; returns (reply, model, in_tokens, out_tokens)."""
    import json

    payload = json.dumps(
        {
            "model": OPENROUTER_MODEL,
            "messages": [{"role": "user", "content": message}],
            "max_tokens": 200,
        }
    ).encode()
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": f"Bearer {OPENROUTER_KEY}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/aegrail/aegrail",
            "X-Title": "aegrail-app-runner-sample",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode())
    reply = body["choices"][0]["message"]["content"]
    model = body.get("model", OPENROUTER_MODEL)
    usage = body.get("usage", {})
    return (
        reply,
        model,
        int(usage.get("prompt_tokens", 0)),
        int(usage.get("completion_tokens", 0)),
    )


_DEMO_PRICES_PER_1M = {
    "openai/gpt-4o-mini": (0.15, 0.60),
    "openai/gpt-4o": (2.50, 10.00),
    "anthropic/claude-3.5-sonnet": (3.00, 15.00),
    "anthropic/claude-3.5-haiku": (0.80, 4.00),
}


def _estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """Demo price lookup. Aegrail's core has no baked-in price table —
    cost calculation is the caller's responsibility (see CLAUDE.md
    design principle).
    """
    in_price, out_price = _DEMO_PRICES_PER_1M.get(model, (1.00, 3.00))
    return (tokens_in / 1_000_000) * in_price + (tokens_out / 1_000_000) * out_price
