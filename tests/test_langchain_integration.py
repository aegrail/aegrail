"""Tests for the LangChain callback handler (v0.3.3).

The tests don't require LangChain to be installed — they exercise
the handler by invoking its callback methods directly, the same way
LangChain would. This keeps the test surface clean and matches the
library's "no langchain dependency" promise.
"""

from __future__ import annotations

import pytest

from aegrail import Agent, AuditSink, Budget, BudgetExceeded
from aegrail.integrations.langchain import AegrailCallbackHandler

# ---------------------------------------------------------------
# Fake LLMResult shapes (no LangChain dependency required)
# ---------------------------------------------------------------


class _FakeLLMResult:
    """Mimics LangChain's `LLMResult.llm_output` payload shapes."""

    def __init__(self, llm_output: dict) -> None:
        self.llm_output = llm_output


def _openai_result(prompt_tokens: int, completion_tokens: int, model: str) -> _FakeLLMResult:
    return _FakeLLMResult(
        llm_output={
            "model_name": model,
            "token_usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
        }
    )


def _anthropic_result(
    input_tokens: int,
    output_tokens: int,
    model: str,
    cache_read: int = 0,
    cache_write: int = 0,
) -> _FakeLLMResult:
    return _FakeLLMResult(
        llm_output={
            "model_name": model,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write,
            },
        }
    )


# ---------------------------------------------------------------
# Agent factory
# ---------------------------------------------------------------


def _make_agent(budget: Budget) -> Agent:
    return Agent(
        identity="lc-test/v1",
        budget=budget,
        audit=AuditSink.memory(),
    )


# ---------------------------------------------------------------
# Tests
# ---------------------------------------------------------------


class TestAegrailCallbackHandler:
    def test_no_session_active_is_passthrough(self) -> None:
        handler = AegrailCallbackHandler()
        # Calling outside any session must not raise and must not
        # try to emit anything.
        handler.on_llm_start({"name": "ChatOpenAI"}, ["hi"])
        handler.on_llm_end(_openai_result(10, 20, "gpt-4o-mini"))
        handler.on_tool_start({"name": "search"}, "weather in Paris")

    def test_on_llm_end_records_openai_shape(self) -> None:
        agent = _make_agent(Budget(usd=5.0, tokens=10_000))
        handler = AegrailCallbackHandler()
        with agent.session() as s:
            handler.on_llm_start({"name": "ChatOpenAI"}, ["hi"])
            handler.on_llm_end(_openai_result(15, 25, "gpt-4o-mini"))
            assert s.state_snapshot["tokens_used"] == 40
        events = [e for e in agent.audit.events if e.event == "llm_call"]
        assert len(events) == 1
        assert events[0].payload["model"] == "gpt-4o-mini"
        assert events[0].payload["tokens_in"] == 15
        assert events[0].payload["tokens_out"] == 25

    def test_on_llm_end_records_anthropic_shape_with_cache(self) -> None:
        agent = _make_agent(Budget(usd=5.0, tokens=10_000))
        handler = AegrailCallbackHandler()
        with agent.session() as s:
            handler.on_chat_model_start({"name": "ChatAnthropic"}, [])
            handler.on_llm_end(
                _anthropic_result(20, 30, "claude-3-5-sonnet-20240620", cache_read=8, cache_write=4)
            )
            assert s.state_snapshot["tokens_used"] == 50
        events = [e for e in agent.audit.events if e.event == "llm_call"]
        assert len(events) == 1
        assert events[0].payload["cache_read_tokens"] == 8
        assert events[0].payload["cache_write_tokens"] == 4

    def test_on_llm_start_pre_checks_budget(self) -> None:
        agent = _make_agent(Budget(tokens=100))
        handler = AegrailCallbackHandler()
        with agent.session() as s:
            s._state.add_tokens(150)
            with pytest.raises(BudgetExceeded):
                handler.on_llm_start({"name": "ChatOpenAI"}, ["hi"])

    def test_on_tool_start_emits_audit_event(self) -> None:
        agent = _make_agent(Budget(usd=5.0, tokens=10_000))
        handler = AegrailCallbackHandler()
        with agent.session():
            handler.on_tool_start({"name": "search"}, "weather in Paris")
        events = [e for e in agent.audit.events if e.event == "tool_call"]
        assert len(events) == 1
        assert events[0].payload["tool"] == "search"
        assert events[0].payload["source"] == "langchain"
        # Per design principle: log the length, not the value
        assert events[0].payload["input_length"] == len("weather in Paris")

    def test_on_llm_error_emits_error_event(self) -> None:
        agent = _make_agent(Budget(usd=5.0, tokens=10_000))
        handler = AegrailCallbackHandler()
        with agent.session():
            handler.on_llm_error(RuntimeError("upstream timeout"))
        events = [e for e in agent.audit.events if e.event == "error"]
        assert len(events) == 1
        assert events[0].payload["error_type"] == "RuntimeError"
        assert events[0].payload["source"] == "langchain.llm"

    def test_on_tool_error_emits_error_event(self) -> None:
        agent = _make_agent(Budget(usd=5.0, tokens=10_000))
        handler = AegrailCallbackHandler()
        with agent.session():
            handler.on_tool_error(PermissionError("denied"))
        events = [e for e in agent.audit.events if e.event == "error"]
        assert len(events) == 1
        assert events[0].payload["error_type"] == "PermissionError"
        assert events[0].payload["source"] == "langchain.tool"

    def test_chain_lifecycle_events_are_noops(self) -> None:
        agent = _make_agent(Budget(usd=5.0, tokens=10_000))
        handler = AegrailCallbackHandler()
        with agent.session():
            handler.on_chain_start({"name": "LLMChain"}, {"input": "x"})
            handler.on_chain_end({"output": "y"})
            handler.on_agent_action(None)
            handler.on_agent_finish(None)
        # No audit events emitted by these methods
        assert agent.audit.events != []  # session_start at minimum
        # But no tool_call / llm_call / error from the chain methods
        non_session = [
            e for e in agent.audit.events if e.event not in ("session_start", "session_end")
        ]
        assert non_session == []
