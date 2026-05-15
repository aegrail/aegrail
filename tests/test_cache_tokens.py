"""Tests for cache_read_tokens / cache_write_tokens on record_llm (v0.2.5).

The fields are observational only — they surface in the llm_call audit
event payload so ops teams can derive cache hit rate over time. They
do not affect budget consumption (cost_usd remains the caller's
calculation; tokens_used counts total tokens regardless of cache).
"""

from __future__ import annotations

import json

import pytest

from aegrail import Agent, AuditSink, Budget
from aegrail.audit import verify_chain


def _agent_with_memory_sink():
    sink = AuditSink.memory()
    a = Agent(identity="bot/v1", budget=Budget(usd=10.0, tokens=10_000), audit=sink)
    return a, sink


class TestRecordLLMCacheTokens:
    def test_cache_fields_default_to_zero(self) -> None:
        agent, sink = _agent_with_memory_sink()
        with agent.session(user_id="alice") as s:
            s.record_llm(model="m", tokens_in=100, tokens_out=200, cost_usd=0.01)
        llm = next(e for e in sink.events if e.event == "llm_call")
        assert llm.payload["cache_read_tokens"] == 0
        assert llm.payload["cache_write_tokens"] == 0

    def test_cache_read_recorded_in_payload(self) -> None:
        agent, sink = _agent_with_memory_sink()
        with agent.session(user_id="alice") as s:
            s.record_llm(
                model="claude-sonnet-4-5",
                tokens_in=1000,
                tokens_out=500,
                cost_usd=0.012,
                cache_read_tokens=800,
            )
        llm = next(e for e in sink.events if e.event == "llm_call")
        assert llm.payload["cache_read_tokens"] == 800
        assert llm.payload["cache_write_tokens"] == 0

    def test_cache_write_recorded_in_payload(self) -> None:
        agent, sink = _agent_with_memory_sink()
        with agent.session(user_id="alice") as s:
            s.record_llm(
                model="claude-sonnet-4-5",
                tokens_in=1000,
                tokens_out=500,
                cost_usd=0.025,
                cache_write_tokens=200,
            )
        llm = next(e for e in sink.events if e.event == "llm_call")
        assert llm.payload["cache_write_tokens"] == 200

    def test_both_cache_fields_recorded(self) -> None:
        agent, sink = _agent_with_memory_sink()
        with agent.session(user_id="alice") as s:
            s.record_llm(
                model="claude-sonnet-4-5",
                tokens_in=1000,
                tokens_out=500,
                cost_usd=0.005,
                cache_read_tokens=900,
                cache_write_tokens=100,
            )
        llm = next(e for e in sink.events if e.event == "llm_call")
        assert llm.payload["cache_read_tokens"] == 900
        assert llm.payload["cache_write_tokens"] == 100

    def test_cache_tokens_do_not_affect_budget(self) -> None:
        """tokens_used counts the total regardless of cache; the discount is
        reflected in cost_usd which is the caller's calculation."""
        agent, _ = _agent_with_memory_sink()
        with agent.session(user_id="alice") as s:
            s.record_llm(
                model="m",
                tokens_in=1000,
                tokens_out=500,
                cost_usd=0.012,
                cache_read_tokens=800,
                cache_write_tokens=200,
            )
            assert s.state_snapshot["tokens_used"] == 1500

    def test_cache_fields_chain_validly(self) -> None:
        agent, sink = _agent_with_memory_sink()
        with agent.session(user_id="alice") as s:
            s.record_llm(
                model="m",
                tokens_in=100,
                tokens_out=200,
                cost_usd=0.01,
                cache_read_tokens=80,
            )
        valid, bad = verify_chain(sink.events)
        assert valid is True, f"chain broken at index {bad}"

    def test_cache_fields_serialize_to_json(self) -> None:
        agent, sink = _agent_with_memory_sink()
        with agent.session(user_id="alice") as s:
            s.record_llm(
                model="m",
                tokens_in=100,
                tokens_out=200,
                cost_usd=0.01,
                cache_read_tokens=80,
                cache_write_tokens=20,
            )
        llm = next(e for e in sink.events if e.event == "llm_call")
        parsed = json.loads(llm.to_json_line())
        assert parsed["payload"]["cache_read_tokens"] == 80
        assert parsed["payload"]["cache_write_tokens"] == 20


class TestAsyncRecordLLMCacheTokens:
    @pytest.mark.asyncio
    async def test_async_cache_tokens_recorded(self) -> None:
        agent, sink = _agent_with_memory_sink()
        async with agent.async_session(user_id="alice") as s:
            await s.record_llm(
                model="claude-sonnet-4-5",
                tokens_in=1000,
                tokens_out=500,
                cost_usd=0.005,
                cache_read_tokens=900,
                cache_write_tokens=100,
            )
        llm = next(e for e in sink.events if e.event == "llm_call")
        assert llm.payload["cache_read_tokens"] == 900
        assert llm.payload["cache_write_tokens"] == 100

    @pytest.mark.asyncio
    async def test_async_cache_fields_default_to_zero(self) -> None:
        agent, sink = _agent_with_memory_sink()
        async with agent.async_session(user_id="alice") as s:
            await s.record_llm(model="m", tokens_in=100, tokens_out=200, cost_usd=0.01)
        llm = next(e for e in sink.events if e.event == "llm_call")
        assert llm.payload["cache_read_tokens"] == 0
        assert llm.payload["cache_write_tokens"] == 0
