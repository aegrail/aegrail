import pytest

from aegrail import Agent, AuditSink, Budget, BudgetExceeded, SessionTerminated


def _agent(**budget_kw) -> tuple[Agent, "MemorySinkType"]:
    sink = AuditSink.memory()
    a = Agent(identity="bot/v1", budget=Budget(**budget_kw), audit=sink)
    return a, sink


MemorySinkType = type(AuditSink.memory())


class TestLifecycle:
    def test_start_and_end_emit_events(self) -> None:
        agent, sink = _agent(usd=1.0)
        with agent.session(user_id="alice", task="t") as s:
            assert s.principal.startswith("bot/v1@sess_")
        kinds = [e.event for e in sink.events]
        assert kinds == ["session_start", "session_end"]
        assert sink.events[-1].payload["reason"] == "normal"

    def test_use_after_close_raises(self) -> None:
        agent, _ = _agent(usd=1.0)
        with agent.session() as s:
            pass
        with pytest.raises(SessionTerminated):
            s.record_llm(model="x", tokens_in=1, tokens_out=1, cost_usd=0.0)

    def test_session_principal_is_unique_per_session(self) -> None:
        agent, _ = _agent(usd=1.0)
        with agent.session() as s1, agent.session() as s2:
            assert s1.principal != s2.principal


class TestLLMRecording:
    def test_records_event_and_advances_budget(self) -> None:
        agent, sink = _agent(usd=1.0, tokens=1000)
        with agent.session() as s:
            s.record_llm(
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
        assert llm_events[0].payload["tokens_out"] == 200

    def test_overspend_raises_and_emits_budget_event(self) -> None:
        agent, sink = _agent(usd=0.01)
        with pytest.raises(BudgetExceeded) as excinfo, agent.session() as s:
            s.record_llm(model="m", tokens_in=10, tokens_out=10, cost_usd=0.02)
        assert excinfo.value.reason == "usd"

        events = [e.event for e in sink.events]
        assert "budget_exceeded" in events
        assert events[-1] == "session_end"
        end_event = sink.events[-1]
        assert end_event.payload["reason"].startswith("budget_exceeded:")


class TestToolCall:
    def test_records_success(self) -> None:
        agent, sink = _agent(usd=1.0, max_tool_calls=5)

        def refund(order_id: int) -> str:
            return f"refunded {order_id}"

        with agent.session() as s:
            result = s.call_tool("refund", refund, order_id=4521)

        assert result == "refunded 4521"
        tool_events = [e for e in sink.events if e.event == "tool_call"]
        assert len(tool_events) == 1
        assert tool_events[0].payload["ok"] is True
        # Default redaction: keys only, no values.
        assert tool_events[0].payload["args"] == {
            "positional_count": 0,
            "kwarg_keys": ["order_id"],
        }

    def test_records_tool_failure_then_reraises(self) -> None:
        agent, sink = _agent(usd=1.0, max_tool_calls=5)

        def broken() -> None:
            raise RuntimeError("boom")

        with pytest.raises(RuntimeError), agent.session() as s:
            s.call_tool("broken", broken)

        tool_events = [e for e in sink.events if e.event == "tool_call"]
        assert tool_events[0].payload["ok"] is False
        assert tool_events[0].payload["error_type"] == "RuntimeError"

    def test_tool_call_budget_overage(self) -> None:
        agent, _ = _agent(usd=10.0, max_tool_calls=2)

        def noop() -> None:
            return None

        with pytest.raises(BudgetExceeded) as excinfo, agent.session() as s:
            s.call_tool("a", noop)
            s.call_tool("b", noop)
            s.call_tool("c", noop)
        assert excinfo.value.reason == "tool_calls"

    def test_custom_arg_summary_overrides_redaction(self) -> None:
        agent, sink = _agent(usd=1.0, max_tool_calls=5)

        def t(**kw) -> None:
            return None

        with agent.session() as s:
            s.call_tool("t", t, _arg_summary={"order_id": 4521, "amount_cents": 100}, foo="bar")

        tool_events = [e for e in sink.events if e.event == "tool_call"]
        assert tool_events[0].payload["args"] == {"order_id": 4521, "amount_cents": 100}


class TestRecursion:
    def test_enter_and_exit(self) -> None:
        agent, _ = _agent(usd=1.0, max_recursion=3)
        with agent.session() as s:
            s.enter_recursion()
            s.enter_recursion()
            assert s.state_snapshot["recursion_depth"] == 2
            s.exit_recursion()
            assert s.state_snapshot["recursion_depth"] == 1

    def test_recursion_overage_raises(self) -> None:
        agent, _ = _agent(usd=1.0, max_recursion=1)
        with pytest.raises(BudgetExceeded) as excinfo, agent.session() as s:
            s.enter_recursion()
            s.enter_recursion()
        assert excinfo.value.reason == "recursion"


class TestAuditPayloads:
    def test_every_event_carries_identity_and_budget_snapshot(self) -> None:
        agent, sink = _agent(usd=1.0)
        with agent.session(user_id="alice") as s:
            s.record_llm(model="m", tokens_in=1, tokens_out=1, cost_usd=0.0)

        for e in sink.events:
            assert e.agent_identity == "bot/v1"
            assert e.invoking_user == "alice"
            assert e.principal == s.principal
            assert "tokens_used" in e.budget
            assert "usd_used" in e.budget
