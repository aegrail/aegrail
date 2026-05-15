"""Tests for the openai SDK auto-instrumentation (v0.3.0).

These tests use a fake `openai` module with the same class hierarchy
as the real SDK, so they exercise the patching logic without
requiring a real OpenAI API key. The shape of `Completions.create`
matches the real SDK: instance method on a class that lives at
`openai.resources.chat.completions.Completions`, returns an object
with `model` and `usage` attributes.

Streaming and missing-usage cases are covered as edge paths.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from aegrail import Agent, Budget, BudgetExceeded
from aegrail.exceptions import SessionTerminated  # noqa: F401  (kept for clarity)

# ---------------------------------------------------------------
# Fake openai SDK
# ---------------------------------------------------------------


class _FakeUsage:
    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.prompt_tokens_details = types.SimpleNamespace(cached_tokens=0)


class _FakeChatResponse:
    def __init__(self, model: str, usage: _FakeUsage) -> None:
        self.model = model
        self.usage = usage


class _FakeCompletions:
    """Mirrors openai.resources.chat.completions.Completions."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(self, *, model: str, messages: list, **kwargs: Any) -> _FakeChatResponse:
        self.calls.append(
            {"model": model, "messages": messages, "stream": kwargs.get("stream", False)}
        )
        return _FakeChatResponse(
            model=model,
            usage=_FakeUsage(prompt_tokens=10, completion_tokens=20),
        )


class _FakeAsyncCompletions:
    """Mirrors openai.resources.chat.completions.AsyncCompletions."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, *, model: str, messages: list, **kwargs: Any) -> _FakeChatResponse:
        self.calls.append({"model": model, "messages": messages})
        return _FakeChatResponse(
            model=model,
            usage=_FakeUsage(prompt_tokens=15, completion_tokens=25),
        )


@pytest.fixture
def fake_openai(monkeypatch):
    """Install a minimal fake `openai` module structured like the real
    SDK, with patchable `Completions.create` / `AsyncCompletions.create`.
    """
    # Build a stack of modules: openai, openai.resources, openai.resources.chat,
    # openai.resources.chat.completions
    openai_mod = types.ModuleType("openai")
    resources_mod = types.ModuleType("openai.resources")
    chat_pkg = types.ModuleType("openai.resources.chat")
    completions_mod = types.ModuleType("openai.resources.chat.completions")

    completions_mod.Completions = _FakeCompletions
    completions_mod.AsyncCompletions = _FakeAsyncCompletions
    chat_pkg.completions = completions_mod
    resources_mod.chat = chat_pkg
    openai_mod.resources = resources_mod

    monkeypatch.setitem(sys.modules, "openai", openai_mod)
    monkeypatch.setitem(sys.modules, "openai.resources", resources_mod)
    monkeypatch.setitem(sys.modules, "openai.resources.chat", chat_pkg)
    monkeypatch.setitem(sys.modules, "openai.resources.chat.completions", completions_mod)
    return completions_mod


# ---------------------------------------------------------------
# Tests
# ---------------------------------------------------------------


def _make_agent(monkeypatch, budget: Budget) -> Agent:
    """Build a small Agent with stdout-suppressed audit and intercept enabled."""
    monkeypatch.setenv("AEGRAIL_INTERCEPT", "1")
    from aegrail import AuditSink

    return Agent(
        identity="test-agent/v1",
        budget=budget,
        audit=AuditSink.memory(),
    )


class TestOpenAIInstrument:
    def test_install_patches_completions_create(self, fake_openai, monkeypatch) -> None:
        _make_agent(monkeypatch, Budget(usd=5.0, tokens=10_000))
        # After Agent construction, the create method must be the wrapped one.
        assert getattr(fake_openai.Completions.create, "__aegrail_patched__", False)
        assert getattr(fake_openai.AsyncCompletions.create, "__aegrail_patched__", False)

    def test_install_is_idempotent(self, fake_openai, monkeypatch) -> None:
        _make_agent(monkeypatch, Budget(usd=5.0, tokens=10_000))
        first = fake_openai.Completions.create
        # Second Agent construction must not re-wrap
        _make_agent(monkeypatch, Budget(usd=5.0, tokens=10_000))
        assert fake_openai.Completions.create is first

    def test_synchronous_call_records_tokens_and_emits_audit(
        self, fake_openai, monkeypatch
    ) -> None:
        agent = _make_agent(monkeypatch, Budget(usd=5.0, tokens=10_000))
        client = fake_openai.Completions()
        with agent.session(user_id="alice", task="chat") as s:
            resp = client.create(model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}])
            assert resp.model == "gpt-4o-mini"
            assert s.state_snapshot["tokens_used"] == 30  # 10 prompt + 20 completion
        # Memory sink captured the llm_call event
        events = [e for e in agent.audit.events if e.event == "llm_call"]
        assert len(events) == 1
        assert events[0].payload["model"] == "gpt-4o-mini"
        assert events[0].payload["tokens_in"] == 10
        assert events[0].payload["tokens_out"] == 20

    def test_no_session_active_is_passthrough(self, fake_openai, monkeypatch) -> None:
        # Construct Agent (installs patches) but make the call OUTSIDE
        # any session — should pass through without recording.
        agent = _make_agent(monkeypatch, Budget(usd=5.0, tokens=10_000))
        client = fake_openai.Completions()
        resp = client.create(model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}])
        assert resp.model == "gpt-4o-mini"
        # Agent never opened a session; no llm_call events
        events = [e for e in agent.audit.events if e.event == "llm_call"]
        assert events == []

    def test_streaming_request_passes_through(self, fake_openai, monkeypatch) -> None:
        agent = _make_agent(monkeypatch, Budget(usd=5.0, tokens=10_000))
        client = fake_openai.Completions()
        with agent.session() as s:
            resp = client.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
            )
            assert resp.model == "gpt-4o-mini"
            # Streams pass through — no auto-recorded tokens
            assert s.state_snapshot["tokens_used"] == 0

    def test_first_call_over_budget_raises_in_record(self, fake_openai, monkeypatch) -> None:
        # 30 tokens recorded against a 20-token ceiling — record_llm
        # raises BudgetExceeded from inside the wrapper.
        agent = _make_agent(monkeypatch, Budget(tokens=20))
        client = fake_openai.Completions()
        with agent.session():
            with pytest.raises(BudgetExceeded) as ei:
                client.create(model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}])
            assert ei.value.reason == "tokens"
            # The upstream call DID run — the budget breach is detected
            # post-record. This matches the design: budget is enforced
            # on tally, not on the API call itself.
            assert len(client.calls) == 1

    def test_second_call_pre_check_rejects_already_over_state(
        self, fake_openai, monkeypatch
    ) -> None:
        # Manually push state over the ceiling, then make a call —
        # the wrapper's pre-check rejects before the upstream call.
        agent = _make_agent(monkeypatch, Budget(tokens=100))
        client = fake_openai.Completions()
        with agent.session() as s:
            # Push state over manually (simulates a prior call that ran
            # but caller chose to continue)
            s._state.add_tokens(150)
            with pytest.raises(BudgetExceeded) as ei:
                client.create(model="gpt-4o-mini", messages=[{"role": "user", "content": "hi"}])
            assert ei.value.reason == "tokens"
            # Upstream NEVER got called — pre-check fail
            assert len(client.calls) == 0

    @pytest.mark.asyncio
    async def test_async_call_records_tokens(self, fake_openai, monkeypatch) -> None:
        agent = _make_agent(monkeypatch, Budget(usd=5.0, tokens=10_000))
        client = fake_openai.AsyncCompletions()
        async with agent.async_session() as s:
            resp = await client.create(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "hi"}],
            )
            assert resp.model == "gpt-4o-mini"
            assert s.state_snapshot["tokens_used"] == 40  # 15 + 25
        events = [e for e in agent.audit.events if e.event == "llm_call"]
        assert len(events) == 1
        assert events[0].payload["tokens_in"] == 15
        assert events[0].payload["tokens_out"] == 25
