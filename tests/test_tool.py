"""Tests for the per-agent tool ACL introduced in v0.2.

Maps to OWASP Top 10 for Agentic Applications:
  ASI02 (Tool Misuse) — registry + arg predicate must deny unintended use.
  ASI03 (Identity & Privilege Abuse) — tools are bound to agent identity;
  a different agent's session has no entry for them and is denied.
"""

import pytest
from pydantic import ValidationError

from aegrail import (
    Agent,
    AuditSink,
    Budget,
    Tool,
    ToolNotPermitted,
)


def _budget() -> Budget:
    return Budget(usd=1.0, max_tool_calls=10)


class TestToolModel:
    def test_minimal_construction(self) -> None:
        def fn() -> str:
            return "ok"

        t = Tool(name="probe", fn=fn)
        assert t.name == "probe"
        assert t.fn() == "ok"
        assert t.when is None
        assert t.description is None
        assert t.redact is None

    def test_rejects_invalid_name(self) -> None:
        with pytest.raises(ValidationError):
            Tool(name="BadName", fn=lambda: None)
        with pytest.raises(ValidationError):
            Tool(name="bad name", fn=lambda: None)
        with pytest.raises(ValidationError):
            Tool(name="", fn=lambda: None)

    def test_rejects_non_callable_fn(self) -> None:
        with pytest.raises(ValidationError):
            Tool(name="x", fn="not-a-callable")  # type: ignore[arg-type]

    def test_is_frozen(self) -> None:
        t = Tool(name="x", fn=lambda: None)
        with pytest.raises(ValidationError):
            t.name = "y"  # type: ignore[misc]


class TestAgentToolValidation:
    def test_agent_accepts_tools_mapping(self) -> None:
        t = Tool(name="ping", fn=lambda: "pong")
        a = Agent(identity="bot/v1", budget=_budget(), tools={"ping": t})
        assert "ping" in a.tools
        assert a.tools["ping"] is t

    def test_agent_rejects_key_mismatch(self) -> None:
        t = Tool(name="ping", fn=lambda: None)
        with pytest.raises(ValueError, match="key 'pong' does not match"):
            Agent(identity="bot/v1", budget=_budget(), tools={"pong": t})

    def test_agent_rejects_non_tool_values(self) -> None:
        with pytest.raises(TypeError, match=r"must be aegrail\.Tool"):
            Agent(identity="bot/v1", budget=_budget(), tools={"x": "nope"})  # type: ignore[dict-item]

    def test_agent_tools_default_is_none(self) -> None:
        a = Agent(identity="bot/v1", budget=_budget())
        assert a.tools is None

    def test_agent_tools_is_immutable_after_construction(self) -> None:
        t = Tool(name="ping", fn=lambda: None)
        a = Agent(identity="bot/v1", budget=_budget(), tools={"ping": t})
        with pytest.raises(TypeError):
            a.tools["new"] = t  # type: ignore[index]


class TestCallToolDispatch:
    def test_invokes_registered_tool(self) -> None:
        def refund(order_id: int) -> str:
            return f"refunded {order_id}"

        sink = AuditSink.memory()
        a = Agent(
            identity="support/v1",
            budget=_budget(),
            audit=sink,
            tools={"refund": Tool(name="refund", fn=refund, description="issue refund")},
        )
        with a.session() as s:
            assert s.call_tool("refund", order_id=4521) == "refunded 4521"

        tool_events = [e for e in sink.events if e.event == "tool_call"]
        assert len(tool_events) == 1
        assert tool_events[0].payload["tool"] == "refund"
        assert tool_events[0].payload["description"] == "issue refund"
        assert tool_events[0].payload["ok"] is True
        # PII default: keys only, no values.
        assert tool_events[0].payload["args"] == {"kwarg_keys": ["order_id"]}

    def test_unregistered_tool_is_denied(self) -> None:
        sink = AuditSink.memory()
        a = Agent(
            identity="support/v1",
            budget=_budget(),
            audit=sink,
            tools={"refund": Tool(name="refund", fn=lambda **kw: None)},
        )
        with a.session() as s, pytest.raises(ToolNotPermitted) as excinfo:
            s.call_tool("wire_transfer", amount=1_000_000)

        assert excinfo.value.reason == "not_registered"
        assert excinfo.value.tool_name == "wire_transfer"

        denied = [e for e in sink.events if e.event == "tool_denied"]
        assert len(denied) == 1
        assert denied[0].payload["tool"] == "wire_transfer"
        assert denied[0].payload["reason"] == "not_registered"

    def test_agent_with_no_tool_registry_denies_all_calls(self) -> None:
        sink = AuditSink.memory()
        a = Agent(identity="support/v1", budget=_budget(), audit=sink)
        with a.session() as s, pytest.raises(ToolNotPermitted) as excinfo:
            s.call_tool("anything")
        assert excinfo.value.reason == "not_registered"
        denied = [e for e in sink.events if e.event == "tool_denied"]
        assert len(denied) == 1
        assert denied[0].payload["reason"] == "not_registered"

    def test_denied_call_does_not_consume_budget(self) -> None:
        a = Agent(
            identity="support/v1",
            budget=Budget(usd=1.0, max_tool_calls=2),
            tools={"ping": Tool(name="ping", fn=lambda: None)},
        )
        with a.session() as s:
            for _ in range(50):
                with pytest.raises(ToolNotPermitted):
                    s.call_tool("nope")
            # All denials should not have advanced the counter.
            assert s.state_snapshot["tool_calls"] == 0

    def test_positional_args_are_rejected(self) -> None:
        a = Agent(
            identity="support/v1",
            budget=_budget(),
            tools={"echo": Tool(name="echo", fn=lambda x=None: x)},
        )
        with a.session() as s, pytest.raises(TypeError):
            s.call_tool("echo", "positional")  # type: ignore[misc]


class TestPredicateEnforcement:
    def test_predicate_allows_call_when_true(self) -> None:
        sink = AuditSink.memory()
        a = Agent(
            identity="finops/v1",
            budget=_budget(),
            audit=sink,
            tools={
                "budget_query": Tool(
                    name="budget_query",
                    fn=lambda period: f"data for {period}",
                    when=lambda args: args.get("period") in ("mtd", "ytd"),
                )
            },
        )
        with a.session() as s:
            assert s.call_tool("budget_query", period="mtd") == "data for mtd"

    def test_predicate_denies_call_when_false(self) -> None:
        sink = AuditSink.memory()
        a = Agent(
            identity="finops/v1",
            budget=_budget(),
            audit=sink,
            tools={
                "budget_query": Tool(
                    name="budget_query",
                    fn=lambda period: "x",
                    when=lambda args: args.get("period") in ("mtd", "ytd"),
                )
            },
        )
        with a.session() as s, pytest.raises(ToolNotPermitted) as excinfo:
            s.call_tool("budget_query", period="all_time")
        assert excinfo.value.reason == "predicate_false"
        assert excinfo.value.tool_name == "budget_query"
        denied = [e for e in sink.events if e.event == "tool_denied"]
        assert denied[0].payload["reason"] == "predicate_false"

    def test_predicate_exception_is_caught_and_denies(self) -> None:
        sink = AuditSink.memory()

        def broken_predicate(args: dict) -> bool:
            raise KeyError("missing")

        a = Agent(
            identity="bot/v1",
            budget=_budget(),
            audit=sink,
            tools={
                "t": Tool(name="t", fn=lambda **kw: None, when=broken_predicate),
            },
        )
        with a.session() as s, pytest.raises(ToolNotPermitted) as excinfo:
            s.call_tool("t", x=1)
        assert excinfo.value.reason == "predicate_error"
        denied = [e for e in sink.events if e.event == "tool_denied"]
        assert denied[0].payload["reason"] == "predicate_error"
        assert "KeyError" in denied[0].payload["error"]

    def test_predicate_denial_emits_keys_only_args(self) -> None:
        sink = AuditSink.memory()
        a = Agent(
            identity="bot/v1",
            budget=_budget(),
            audit=sink,
            tools={
                "t": Tool(name="t", fn=lambda **kw: None, when=lambda a: False),
            },
        )
        with a.session() as s, pytest.raises(ToolNotPermitted):
            s.call_tool("t", account_number="4242424242424242")
        denied = [e for e in sink.events if e.event == "tool_denied"]
        # PII default: no values, only key.
        assert denied[0].payload["args"] == {"kwarg_keys": ["account_number"]}


class TestRedact:
    def test_per_tool_redactor_overrides_default(self) -> None:
        sink = AuditSink.memory()

        def redact_amount(args: dict) -> dict:
            return {"amount_bucket": "small" if args.get("amount", 0) < 100 else "large"}

        a = Agent(
            identity="support/v1",
            budget=_budget(),
            audit=sink,
            tools={
                "refund": Tool(
                    name="refund",
                    fn=lambda amount, account: f"refunded {amount}",
                    redact=redact_amount,
                ),
            },
        )
        with a.session() as s:
            s.call_tool("refund", amount=42, account="ABC")
            s.call_tool("refund", amount=500, account="XYZ")

        events = [e for e in sink.events if e.event == "tool_call"]
        assert events[0].payload["args"] == {"amount_bucket": "small"}
        assert events[1].payload["args"] == {"amount_bucket": "large"}

    def test_redactor_error_falls_back_to_keys_only(self, capsys) -> None:
        sink = AuditSink.memory()

        def bad_redact(args: dict) -> dict:
            raise RuntimeError("redact boom")

        a = Agent(
            identity="bot/v1",
            budget=_budget(),
            audit=sink,
            tools={
                "t": Tool(name="t", fn=lambda **kw: None, redact=bad_redact),
            },
        )
        with a.session() as s:
            s.call_tool("t", secret="s3cr3t")

        events = [e for e in sink.events if e.event == "tool_call"]
        # Falls back to keys-only — never logs the value.
        assert events[0].payload["args"] == {"kwarg_keys": ["secret"]}
        assert "redact boom" in capsys.readouterr().err


class TestMultiAgentACL:
    """ASI03 — Identity & Privilege Abuse.

    Two agents with disjoint tool registries in the same process.
    Each agent's session is bound to its own identity; calling the
    other's tool name is denied because the calling agent's registry
    has no entry for it.
    """

    def test_finops_cannot_call_architect_tool(self) -> None:
        sink = AuditSink.memory()

        finops = Agent(
            identity="finops/v1",
            budget=_budget(),
            audit=sink,
            tools={"cost_report": Tool(name="cost_report", fn=lambda: "$$$")},
        )
        architect = Agent(
            identity="architect/v1",
            budget=_budget(),
            audit=sink,
            tools={"deploy_infra": Tool(name="deploy_infra", fn=lambda env: f"deployed to {env}")},
        )

        with finops.session() as fs:
            with pytest.raises(ToolNotPermitted) as excinfo:
                fs.call_tool("deploy_infra", env="prod")
            assert excinfo.value.reason == "not_registered"

        with architect.session() as as_:
            assert as_.call_tool("deploy_infra", env="staging") == "deployed to staging"

        # Audit shows the denial under finops identity, success under architect.
        finops_denied = [
            e for e in sink.events if e.agent_identity == "finops/v1" and e.event == "tool_denied"
        ]
        assert len(finops_denied) == 1
        assert finops_denied[0].payload["tool"] == "deploy_infra"

        architect_calls = [
            e for e in sink.events if e.agent_identity == "architect/v1" and e.event == "tool_call"
        ]
        assert len(architect_calls) == 1
        assert architect_calls[0].payload["ok"] is True
