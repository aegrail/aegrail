"""Tests for the anthropic SDK auto-instrumentation (v0.3.1)."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from aegrail import Agent, Budget, BudgetExceeded

# ---------------------------------------------------------------
# Fake anthropic SDK
# ---------------------------------------------------------------


class _FakeUsage:
    def __init__(
        self,
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int = 0,
        cache_read_input_tokens: int = 0,
    ) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_creation_input_tokens = cache_creation_input_tokens
        self.cache_read_input_tokens = cache_read_input_tokens


class _FakeMessageResponse:
    def __init__(self, model: str, usage: _FakeUsage) -> None:
        self.model = model
        self.usage = usage


class _FakeMessages:
    """Mirrors anthropic.resources.messages.Messages."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def create(
        self, *, model: str, max_tokens: int, messages: list, **kwargs: Any
    ) -> _FakeMessageResponse:
        self.calls.append(
            {"model": model, "max_tokens": max_tokens, "stream": kwargs.get("stream", False)}
        )
        return _FakeMessageResponse(
            model=model,
            usage=_FakeUsage(
                input_tokens=12,
                output_tokens=24,
                cache_read_input_tokens=4,
                cache_creation_input_tokens=2,
            ),
        )


class _FakeAsyncMessages:
    """Mirrors anthropic.resources.messages.AsyncMessages."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(
        self, *, model: str, max_tokens: int, messages: list, **kwargs: Any
    ) -> _FakeMessageResponse:
        self.calls.append({"model": model, "max_tokens": max_tokens})
        return _FakeMessageResponse(
            model=model,
            usage=_FakeUsage(input_tokens=18, output_tokens=30),
        )


@pytest.fixture
def fake_anthropic(monkeypatch):
    anthropic_mod = types.ModuleType("anthropic")
    resources_mod = types.ModuleType("anthropic.resources")
    messages_mod = types.ModuleType("anthropic.resources.messages")

    messages_mod.Messages = _FakeMessages
    messages_mod.AsyncMessages = _FakeAsyncMessages
    resources_mod.messages = messages_mod
    anthropic_mod.resources = resources_mod

    monkeypatch.setitem(sys.modules, "anthropic", anthropic_mod)
    monkeypatch.setitem(sys.modules, "anthropic.resources", resources_mod)
    monkeypatch.setitem(sys.modules, "anthropic.resources.messages", messages_mod)
    return messages_mod


def _make_agent(monkeypatch, budget: Budget) -> Agent:
    monkeypatch.setenv("AEGRAIL_INTERCEPT", "1")
    from aegrail import AuditSink

    return Agent(
        identity="test-agent/v1",
        budget=budget,
        audit=AuditSink.memory(),
    )


class TestAnthropicInstrument:
    def test_install_patches_messages_create(self, fake_anthropic, monkeypatch) -> None:
        _make_agent(monkeypatch, Budget(usd=5.0, tokens=10_000))
        assert getattr(fake_anthropic.Messages.create, "__aegrail_patched__", False)
        assert getattr(fake_anthropic.AsyncMessages.create, "__aegrail_patched__", False)

    def test_install_is_idempotent(self, fake_anthropic, monkeypatch) -> None:
        _make_agent(monkeypatch, Budget(usd=5.0, tokens=10_000))
        first = fake_anthropic.Messages.create
        _make_agent(monkeypatch, Budget(usd=5.0, tokens=10_000))
        assert fake_anthropic.Messages.create is first

    def test_sync_call_records_tokens_and_cache(self, fake_anthropic, monkeypatch) -> None:
        agent = _make_agent(monkeypatch, Budget(usd=5.0, tokens=10_000))
        client = fake_anthropic.Messages()
        with agent.session() as s:
            resp = client.create(
                model="claude-3-5-sonnet-20240620",
                max_tokens=100,
                messages=[{"role": "user", "content": "hi"}],
            )
            assert resp.model == "claude-3-5-sonnet-20240620"
            assert s.state_snapshot["tokens_used"] == 36  # 12 + 24
        events = [e for e in agent.audit.events if e.event == "llm_call"]
        assert len(events) == 1
        ev = events[0]
        assert ev.payload["tokens_in"] == 12
        assert ev.payload["tokens_out"] == 24
        # Anthropic-specific cache attribution
        assert ev.payload["cache_read_tokens"] == 4
        assert ev.payload["cache_write_tokens"] == 2

    def test_no_session_active_is_passthrough(self, fake_anthropic, monkeypatch) -> None:
        agent = _make_agent(monkeypatch, Budget(usd=5.0, tokens=10_000))
        client = fake_anthropic.Messages()
        resp = client.create(
            model="claude-3-5-sonnet-20240620",
            max_tokens=100,
            messages=[{"role": "user", "content": "hi"}],
        )
        assert resp.model == "claude-3-5-sonnet-20240620"
        assert [e for e in agent.audit.events if e.event == "llm_call"] == []

    def test_streaming_passes_through(self, fake_anthropic, monkeypatch) -> None:
        agent = _make_agent(monkeypatch, Budget(usd=5.0, tokens=10_000))
        client = fake_anthropic.Messages()
        with agent.session() as s:
            resp = client.create(
                model="claude-3-5-sonnet-20240620",
                max_tokens=100,
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
            )
            assert resp.model == "claude-3-5-sonnet-20240620"
            assert s.state_snapshot["tokens_used"] == 0

    def test_first_call_over_budget_raises_in_record(self, fake_anthropic, monkeypatch) -> None:
        agent = _make_agent(monkeypatch, Budget(tokens=20))
        client = fake_anthropic.Messages()
        with agent.session():
            with pytest.raises(BudgetExceeded) as ei:
                client.create(
                    model="claude-3-5-sonnet-20240620",
                    max_tokens=100,
                    messages=[{"role": "user", "content": "hi"}],
                )
            assert ei.value.reason == "tokens"
            assert len(client.calls) == 1

    def test_pre_check_rejects_already_over_state(self, fake_anthropic, monkeypatch) -> None:
        agent = _make_agent(monkeypatch, Budget(tokens=100))
        client = fake_anthropic.Messages()
        with agent.session() as s:
            s._state.add_tokens(150)
            with pytest.raises(BudgetExceeded):
                client.create(
                    model="claude-3-5-sonnet-20240620",
                    max_tokens=100,
                    messages=[{"role": "user", "content": "hi"}],
                )
            assert len(client.calls) == 0

    @pytest.mark.asyncio
    async def test_async_call_records_tokens(self, fake_anthropic, monkeypatch) -> None:
        agent = _make_agent(monkeypatch, Budget(usd=5.0, tokens=10_000))
        client = fake_anthropic.AsyncMessages()
        async with agent.async_session() as s:
            resp = await client.create(
                model="claude-3-5-sonnet-20240620",
                max_tokens=100,
                messages=[{"role": "user", "content": "hi"}],
            )
            assert resp.model == "claude-3-5-sonnet-20240620"
            assert s.state_snapshot["tokens_used"] == 48  # 18 + 30
        events = [e for e in agent.audit.events if e.event == "llm_call"]
        assert len(events) == 1
        assert events[0].payload["tokens_in"] == 18
        assert events[0].payload["tokens_out"] == 30
