"""Tests for the litellm auto-instrumentation (v0.3.2)."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from aegrail import Agent, Budget, BudgetExceeded

# ---------------------------------------------------------------
# Fake litellm module (OpenAI-shaped responses, litellm-style API)
# ---------------------------------------------------------------


class _FakeUsage:
    def __init__(self, prompt_tokens: int, completion_tokens: int) -> None:
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeModelResponse:
    def __init__(self, model: str, usage: _FakeUsage) -> None:
        self.model = model
        self.usage = usage


def _build_fake_litellm_module() -> types.ModuleType:
    mod = types.ModuleType("litellm")
    mod._call_log = []
    mod._async_call_log = []

    def completion(*, model: str, messages: list, **kwargs: Any) -> _FakeModelResponse:
        mod._call_log.append({"model": model, "stream": kwargs.get("stream", False)})
        return _FakeModelResponse(model=model, usage=_FakeUsage(11, 22))

    async def acompletion(*, model: str, messages: list, **kwargs: Any) -> _FakeModelResponse:
        mod._async_call_log.append({"model": model, "stream": kwargs.get("stream", False)})
        return _FakeModelResponse(model=model, usage=_FakeUsage(14, 28))

    mod.completion = completion
    mod.acompletion = acompletion
    return mod


@pytest.fixture
def fake_litellm(monkeypatch):
    mod = _build_fake_litellm_module()
    monkeypatch.setitem(sys.modules, "litellm", mod)
    return mod


def _make_agent(monkeypatch, budget: Budget) -> Agent:
    monkeypatch.setenv("AEGRAIL_INTERCEPT", "1")
    from aegrail import AuditSink

    return Agent(
        identity="test-agent/v1",
        budget=budget,
        audit=AuditSink.memory(),
    )


class TestLitellmInstrument:
    def test_install_patches_completion_and_acompletion(self, fake_litellm, monkeypatch) -> None:
        _make_agent(monkeypatch, Budget(usd=5.0, tokens=10_000))
        assert getattr(fake_litellm.completion, "__aegrail_patched__", False)
        assert getattr(fake_litellm.acompletion, "__aegrail_patched__", False)

    def test_install_is_idempotent(self, fake_litellm, monkeypatch) -> None:
        _make_agent(monkeypatch, Budget(usd=5.0, tokens=10_000))
        first = fake_litellm.completion
        _make_agent(monkeypatch, Budget(usd=5.0, tokens=10_000))
        assert fake_litellm.completion is first

    def test_sync_call_records_tokens_and_audit(self, fake_litellm, monkeypatch) -> None:
        agent = _make_agent(monkeypatch, Budget(usd=5.0, tokens=10_000))
        with agent.session() as s:
            resp = fake_litellm.completion(
                model="bedrock/anthropic.claude-3-5-sonnet-20240620-v1:0",
                messages=[{"role": "user", "content": "hi"}],
            )
            assert resp.model.startswith("bedrock/")
            assert s.state_snapshot["tokens_used"] == 33  # 11 + 22
        events = [e for e in agent.audit.events if e.event == "llm_call"]
        assert len(events) == 1
        assert events[0].payload["tokens_in"] == 11
        assert events[0].payload["tokens_out"] == 22

    def test_no_session_active_is_passthrough(self, fake_litellm, monkeypatch) -> None:
        agent = _make_agent(monkeypatch, Budget(usd=5.0, tokens=10_000))
        resp = fake_litellm.completion(
            model="ollama/llama3", messages=[{"role": "user", "content": "hi"}]
        )
        assert resp.model == "ollama/llama3"
        assert [e for e in agent.audit.events if e.event == "llm_call"] == []

    def test_streaming_passes_through(self, fake_litellm, monkeypatch) -> None:
        agent = _make_agent(monkeypatch, Budget(usd=5.0, tokens=10_000))
        with agent.session() as s:
            fake_litellm.completion(
                model="gpt-4o-mini",
                messages=[{"role": "user", "content": "hi"}],
                stream=True,
            )
            assert s.state_snapshot["tokens_used"] == 0

    def test_first_call_over_budget_raises(self, fake_litellm, monkeypatch) -> None:
        agent = _make_agent(monkeypatch, Budget(tokens=20))
        with agent.session():
            with pytest.raises(BudgetExceeded) as ei:
                fake_litellm.completion(
                    model="claude-3-haiku",
                    messages=[{"role": "user", "content": "hi"}],
                )
            assert ei.value.reason == "tokens"
            assert len(fake_litellm._call_log) == 1

    def test_pre_check_rejects_already_over_state(self, fake_litellm, monkeypatch) -> None:
        agent = _make_agent(monkeypatch, Budget(tokens=100))
        with agent.session() as s:
            s._state.add_tokens(150)
            with pytest.raises(BudgetExceeded):
                fake_litellm.completion(
                    model="claude-3-haiku",
                    messages=[{"role": "user", "content": "hi"}],
                )
            assert len(fake_litellm._call_log) == 0

    @pytest.mark.asyncio
    async def test_async_call_records_tokens(self, fake_litellm, monkeypatch) -> None:
        agent = _make_agent(monkeypatch, Budget(usd=5.0, tokens=10_000))
        async with agent.async_session() as s:
            resp = await fake_litellm.acompletion(
                model="azure/gpt-4",
                messages=[{"role": "user", "content": "hi"}],
            )
            assert resp.model == "azure/gpt-4"
            assert s.state_snapshot["tokens_used"] == 42  # 14 + 28
        events = [e for e in agent.audit.events if e.event == "llm_call"]
        assert len(events) == 1
        assert events[0].payload["tokens_in"] == 14
        assert events[0].payload["tokens_out"] == 28
