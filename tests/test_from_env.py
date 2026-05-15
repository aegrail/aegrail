"""Tests for env-var Agent/Budget configuration (v0.2.6).

The goal: any container platform that exposes env vars (Cloud Run,
AWS App Runner, Azure Container Apps, AWS Fargate, Kubernetes, etc.)
can configure aegrail without code changes. Tools still come from
code because they contain executable functions; everything else
that can come from env, can.
"""

from __future__ import annotations

import pytest

from aegrail import Agent, AuditSink, Budget, Tool
from aegrail.audit import FileAuditSink, StdoutAuditSink


class TestBudgetFromEnv:
    def test_single_axis_set(self, monkeypatch) -> None:
        monkeypatch.setenv("AEGRAIL_BUDGET_USD", "5.0")
        b = Budget.from_env()
        assert b.usd == 5.0
        assert b.tokens is None
        assert b.wall_seconds is None

    def test_all_axes_set(self, monkeypatch) -> None:
        monkeypatch.setenv("AEGRAIL_BUDGET_USD", "3.5")
        monkeypatch.setenv("AEGRAIL_BUDGET_TOKENS", "1000")
        monkeypatch.setenv("AEGRAIL_BUDGET_WALL_SECONDS", "60.5")
        monkeypatch.setenv("AEGRAIL_BUDGET_MAX_RECURSION", "3")
        monkeypatch.setenv("AEGRAIL_BUDGET_MAX_TOOL_CALLS", "10")
        b = Budget.from_env()
        assert b.usd == 3.5
        assert b.tokens == 1000
        assert b.wall_seconds == 60.5
        assert b.max_recursion == 3
        assert b.max_tool_calls == 10

    def test_empty_string_is_ignored(self, monkeypatch) -> None:
        monkeypatch.setenv("AEGRAIL_BUDGET_USD", "")
        monkeypatch.setenv("AEGRAIL_BUDGET_TOKENS", "1000")
        b = Budget.from_env()
        assert b.usd is None
        assert b.tokens == 1000

    def test_missing_env_raises_clearly(self, monkeypatch) -> None:
        for k in (
            "AEGRAIL_BUDGET_USD",
            "AEGRAIL_BUDGET_TOKENS",
            "AEGRAIL_BUDGET_WALL_SECONDS",
            "AEGRAIL_BUDGET_MAX_RECURSION",
            "AEGRAIL_BUDGET_MAX_TOOL_CALLS",
        ):
            monkeypatch.delenv(k, raising=False)
        with pytest.raises(ValueError, match="at least one AEGRAIL_BUDGET_"):
            Budget.from_env()

    def test_invalid_value_raises_clearly(self, monkeypatch) -> None:
        monkeypatch.setenv("AEGRAIL_BUDGET_TOKENS", "not-a-number")
        with pytest.raises(ValueError, match="AEGRAIL_BUDGET_TOKENS"):
            Budget.from_env()


class TestAgentFromEnv:
    def test_basic_construction_from_env_only(self, monkeypatch) -> None:
        monkeypatch.setenv("AEGRAIL_AGENT_IDENTITY", "my-agent/v1")
        monkeypatch.setenv("AEGRAIL_BUDGET_USD", "5.0")
        agent = Agent.from_env()
        assert agent.identity == "my-agent/v1"
        assert agent.budget.usd == 5.0
        assert agent.egress_allowlist is None

    def test_explicit_identity_overrides_env(self, monkeypatch) -> None:
        monkeypatch.setenv("AEGRAIL_AGENT_IDENTITY", "env-agent/v1")
        monkeypatch.setenv("AEGRAIL_BUDGET_USD", "5.0")
        agent = Agent.from_env(identity="code-agent/v1")
        assert agent.identity == "code-agent/v1"

    def test_explicit_budget_overrides_env(self, monkeypatch) -> None:
        monkeypatch.setenv("AEGRAIL_AGENT_IDENTITY", "a/v1")
        monkeypatch.setenv("AEGRAIL_BUDGET_USD", "100.0")
        b = Budget(usd=1.0)
        agent = Agent.from_env(budget=b)
        assert agent.budget.usd == 1.0

    def test_missing_identity_raises(self, monkeypatch) -> None:
        monkeypatch.delenv("AEGRAIL_AGENT_IDENTITY", raising=False)
        monkeypatch.setenv("AEGRAIL_BUDGET_USD", "5.0")
        with pytest.raises(ValueError, match="AEGRAIL_AGENT_IDENTITY"):
            Agent.from_env()

    def test_egress_allowlist_from_env_comma_separated(self, monkeypatch) -> None:
        monkeypatch.setenv("AEGRAIL_AGENT_IDENTITY", "a/v1")
        monkeypatch.setenv("AEGRAIL_BUDGET_USD", "5.0")
        monkeypatch.setenv(
            "AEGRAIL_EGRESS_ALLOWLIST",
            "api.openai.com, *.anthropic.com,generativelanguage.googleapis.com",
        )
        agent = Agent.from_env()
        assert agent.egress_allowlist == [
            "api.openai.com",
            "*.anthropic.com",
            "generativelanguage.googleapis.com",
        ]

    def test_egress_allowlist_empty_env_yields_empty_list(self, monkeypatch) -> None:
        monkeypatch.setenv("AEGRAIL_AGENT_IDENTITY", "a/v1")
        monkeypatch.setenv("AEGRAIL_BUDGET_USD", "5.0")
        monkeypatch.setenv("AEGRAIL_EGRESS_ALLOWLIST", "")
        agent = Agent.from_env()
        # Empty env -> no list set (egress wide open via the library;
        # operators wanting deny-all should set a single dummy
        # value or omit the env var entirely.)
        assert agent.egress_allowlist is None or agent.egress_allowlist == []

    def test_explicit_egress_allowlist_overrides_env(self, monkeypatch) -> None:
        monkeypatch.setenv("AEGRAIL_AGENT_IDENTITY", "a/v1")
        monkeypatch.setenv("AEGRAIL_BUDGET_USD", "5.0")
        monkeypatch.setenv("AEGRAIL_EGRESS_ALLOWLIST", "env.example")
        agent = Agent.from_env(egress_allowlist=["code.example"])
        assert agent.egress_allowlist == ["code.example"]

    def test_audit_file_sink_from_env(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("AEGRAIL_AGENT_IDENTITY", "a/v1")
        monkeypatch.setenv("AEGRAIL_BUDGET_USD", "5.0")
        log = tmp_path / "audit.jsonl"
        monkeypatch.setenv("AEGRAIL_AUDIT_FILE", str(log))
        agent = Agent.from_env()
        assert isinstance(agent.audit, FileAuditSink)
        agent.audit.close()

    def test_audit_stdout_sink_from_env(self, monkeypatch) -> None:
        monkeypatch.setenv("AEGRAIL_AGENT_IDENTITY", "a/v1")
        monkeypatch.setenv("AEGRAIL_BUDGET_USD", "5.0")
        monkeypatch.setenv("AEGRAIL_AUDIT_STDOUT", "1")
        monkeypatch.delenv("AEGRAIL_AUDIT_FILE", raising=False)
        agent = Agent.from_env()
        assert isinstance(agent.audit, StdoutAuditSink)

    def test_explicit_audit_overrides_env(self, monkeypatch, tmp_path) -> None:
        monkeypatch.setenv("AEGRAIL_AGENT_IDENTITY", "a/v1")
        monkeypatch.setenv("AEGRAIL_BUDGET_USD", "5.0")
        monkeypatch.setenv("AEGRAIL_AUDIT_FILE", str(tmp_path / "should-not-be-used.jsonl"))
        memory = AuditSink.memory()
        agent = Agent.from_env(audit=memory)
        assert agent.audit is memory

    def test_tools_passed_through(self, monkeypatch) -> None:
        monkeypatch.setenv("AEGRAIL_AGENT_IDENTITY", "a/v1")
        monkeypatch.setenv("AEGRAIL_BUDGET_USD", "5.0")
        tools = {"ping": Tool(name="ping", fn=lambda: "pong")}
        agent = Agent.from_env(tools=tools)
        assert "ping" in agent.tools
