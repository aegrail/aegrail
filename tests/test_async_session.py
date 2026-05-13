"""Tests for AsyncSession (v0.2.2).

The async surface mirrors the sync Session API and adds one
load-bearing property: wall_seconds is enforced mid-tool-call via
asyncio.wait_for. Sync mode could only enforce at event boundaries.
"""

import asyncio
import time

import pytest

from aegrail import (
    Agent,
    AsyncSession,
    AuditSink,
    Budget,
    BudgetExceeded,
    SessionTerminated,
    Tool,
    ToolNotPermitted,
)


def _agent(*, tools=None, **budget_kw):
    sink = AuditSink.memory()
    a = Agent(identity="bot/v1", budget=Budget(**budget_kw), audit=sink, tools=tools)
    return a, sink


class TestAsyncLifecycle:
    @pytest.mark.asyncio
    async def test_async_with_emits_start_and_end(self) -> None:
        agent, sink = _agent(usd=1.0)
        async with agent.async_session(user_id="alice", task="t") as s:
            assert isinstance(s, AsyncSession)
            assert s.principal.startswith("bot/v1@sess_")
        kinds = [e.event for e in sink.events]
        assert kinds == ["session_start", "session_end"]
        assert sink.events[-1].payload["reason"] == "normal"

    @pytest.mark.asyncio
    async def test_use_after_close_raises(self) -> None:
        agent, _ = _agent(usd=1.0)
        async with agent.async_session() as s:
            pass
        with pytest.raises(SessionTerminated):
            await s.record_llm(model="x", tokens_in=1, tokens_out=1, cost_usd=0.0)

    @pytest.mark.asyncio
    async def test_sync_with_on_async_session_raises(self) -> None:
        agent, _ = _agent(usd=1.0)
        s = agent.async_session()
        with pytest.raises(TypeError, match=r"async with"), s:
            pass

    @pytest.mark.asyncio
    async def test_cancelled_session_records_reason(self) -> None:
        agent, sink = _agent(usd=1.0)

        async def run() -> None:
            async with agent.async_session():
                await asyncio.sleep(10)

        task = asyncio.create_task(run())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        end = next(e for e in sink.events if e.event == "session_end")
        assert end.payload["reason"] == "cancelled"


class TestAsyncLLMRecording:
    @pytest.mark.asyncio
    async def test_records_event_and_advances_budget(self) -> None:
        agent, sink = _agent(usd=1.0, tokens=1000)
        async with agent.async_session() as s:
            await s.record_llm(
                model="claude-sonnet-4-5",
                tokens_in=100,
                tokens_out=200,
                cost_usd=0.01,
            )
            snap = s.state_snapshot
            assert snap["tokens_used"] == 300
            assert snap["usd_used"] == 0.01

        llm_events = [e for e in sink.events if e.event == "llm_call"]
        assert len(llm_events) == 1
        assert llm_events[0].payload["model"] == "claude-sonnet-4-5"


class TestAsyncToolDispatch:
    @pytest.mark.asyncio
    async def test_invokes_async_tool(self) -> None:
        async def refund(order_id: int) -> str:
            await asyncio.sleep(0.001)
            return f"refunded {order_id}"

        agent, sink = _agent(
            usd=1.0,
            max_tool_calls=5,
            tools={"refund": Tool(name="refund", fn=refund)},
        )
        async with agent.async_session() as s:
            result = await s.call_tool("refund", order_id=4521)
        assert result == "refunded 4521"
        tool_events = [e for e in sink.events if e.event == "tool_call"]
        assert tool_events[0].payload["ok"] is True

    @pytest.mark.asyncio
    async def test_invokes_sync_tool_via_to_thread(self) -> None:
        def lookup(order_id: int) -> str:
            return f"order #{order_id}"

        agent, sink = _agent(
            usd=1.0,
            max_tool_calls=5,
            tools={"lookup": Tool(name="lookup", fn=lookup)},
        )
        async with agent.async_session() as s:
            result = await s.call_tool("lookup", order_id=4521)
        assert result == "order #4521"
        tool_events = [e for e in sink.events if e.event == "tool_call"]
        assert tool_events[0].payload["ok"] is True

    @pytest.mark.asyncio
    async def test_unregistered_tool_is_denied(self) -> None:
        agent, sink = _agent(
            usd=1.0,
            max_tool_calls=5,
            tools={"refund": Tool(name="refund", fn=lambda: None)},
        )
        async with agent.async_session() as s:
            with pytest.raises(ToolNotPermitted) as excinfo:
                await s.call_tool("wire_transfer", amount=1_000_000)
        assert excinfo.value.reason == "not_registered"
        denied = [e for e in sink.events if e.event == "tool_denied"]
        assert denied[0].payload["tool"] == "wire_transfer"

    @pytest.mark.asyncio
    async def test_predicate_denial(self) -> None:
        agent, sink = _agent(
            usd=1.0,
            max_tool_calls=5,
            tools={
                "refund": Tool(
                    name="refund",
                    fn=lambda amount: f"ok {amount}",
                    when=lambda args: args.get("amount", 0) <= 50,
                )
            },
        )
        async with agent.async_session() as s:
            with pytest.raises(ToolNotPermitted) as excinfo:
                await s.call_tool("refund", amount=1000)
        assert excinfo.value.reason == "predicate_false"
        denied = [e for e in sink.events if e.event == "tool_denied"]
        assert denied[0].payload["reason"] == "predicate_false"

    @pytest.mark.asyncio
    async def test_tool_failure_then_reraise(self) -> None:
        async def broken() -> None:
            raise RuntimeError("boom")

        agent, sink = _agent(
            usd=1.0,
            max_tool_calls=5,
            tools={"broken": Tool(name="broken", fn=broken)},
        )
        async with agent.async_session() as s:
            with pytest.raises(RuntimeError):
                await s.call_tool("broken")
        tool_events = [e for e in sink.events if e.event == "tool_call"]
        assert tool_events[0].payload["ok"] is False
        assert tool_events[0].payload["error_type"] == "RuntimeError"


class TestWallSecondsTimeout:
    """The load-bearing v0.2.2 feature: timeout fires mid-tool-call."""

    @pytest.mark.asyncio
    async def test_async_tool_hits_wall_seconds_timeout(self) -> None:
        async def slow() -> str:
            await asyncio.sleep(10)
            return "never"

        agent, sink = _agent(
            usd=10.0,
            wall_seconds=0.1,
            max_tool_calls=5,
            tools={"slow": Tool(name="slow", fn=slow)},
        )

        started = time.monotonic()
        async with agent.async_session() as s:
            with pytest.raises(BudgetExceeded) as excinfo:
                await s.call_tool("slow")
        elapsed = time.monotonic() - started

        assert excinfo.value.reason == "wall_seconds"
        # Timeout fires deterministically within the budget; ~0.1s with some slack.
        assert elapsed < 2.0
        be = [e for e in sink.events if e.event == "budget_exceeded"]
        assert be[0].payload["reason"] == "wall_seconds"

    @pytest.mark.asyncio
    async def test_sync_tool_in_thread_hits_wall_seconds_timeout(self) -> None:
        # Sync tools are wrapped in asyncio.to_thread so the timeout still
        # applies at the asyncio level. CPython can't kill the underlying
        # thread, so the function continues running in the background until
        # it naturally returns — but the session sees the timeout.
        def slow_sync() -> str:
            time.sleep(10)
            return "never"

        agent, sink = _agent(
            usd=10.0,
            wall_seconds=0.1,
            max_tool_calls=5,
            tools={"slow_sync": Tool(name="slow_sync", fn=slow_sync)},
        )

        started = time.monotonic()
        async with agent.async_session() as s:
            with pytest.raises(BudgetExceeded) as excinfo:
                await s.call_tool("slow_sync")
        elapsed = time.monotonic() - started

        assert excinfo.value.reason == "wall_seconds"
        assert elapsed < 2.0
        be = [e for e in sink.events if e.event == "budget_exceeded"]
        assert be[0].payload["reason"] == "wall_seconds"

    @pytest.mark.asyncio
    async def test_no_wall_seconds_means_no_timeout(self) -> None:
        # If wall_seconds is None, we should not wrap in wait_for.
        async def quick() -> str:
            await asyncio.sleep(0.01)
            return "ok"

        agent, _ = _agent(
            usd=1.0,
            max_tool_calls=5,
            tools={"quick": Tool(name="quick", fn=quick)},
        )
        async with agent.async_session() as s:
            result = await s.call_tool("quick")
        assert result == "ok"


class TestAsyncBudgetEnforcement:
    @pytest.mark.asyncio
    async def test_token_overage_raises(self) -> None:
        agent, sink = _agent(usd=10.0, tokens=100)
        async with agent.async_session() as s:
            with pytest.raises(BudgetExceeded) as excinfo:
                await s.record_llm(model="m", tokens_in=200, tokens_out=200, cost_usd=0.0)
        assert excinfo.value.reason == "tokens"
        kinds = [e.event for e in sink.events]
        assert "budget_exceeded" in kinds

    @pytest.mark.asyncio
    async def test_tool_call_overage(self) -> None:
        agent, _ = _agent(
            usd=10.0,
            max_tool_calls=2,
            tools={
                "a": Tool(name="a", fn=lambda: None),
                "b": Tool(name="b", fn=lambda: None),
                "c": Tool(name="c", fn=lambda: None),
            },
        )
        async with agent.async_session() as s:
            with pytest.raises(BudgetExceeded) as excinfo:
                await s.call_tool("a")
                await s.call_tool("b")
                await s.call_tool("c")
        assert excinfo.value.reason == "tool_calls"

    @pytest.mark.asyncio
    async def test_recursion_overage(self) -> None:
        agent, _ = _agent(usd=1.0, max_recursion=1)
        async with agent.async_session() as s:
            with pytest.raises(BudgetExceeded) as excinfo:
                await s.enter_recursion()
                await s.enter_recursion()
        assert excinfo.value.reason == "recursion"
