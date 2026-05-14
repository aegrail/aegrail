"""Tests for the v0.2.4 in-process interceptors.

Two surfaces:
  - aegrail.intercept_outbound() — monkey-patches requests / urllib /
    httpx so outbound HTTP from inside an active Session is checked
    against the agent's egress_allowlist. Denied destinations raise
    EgressNotPermitted and emit `egress_denied` audit events.
  - aegrail.install_audit_hook() — registers a PEP 578 audit hook
    that observes CPython's built-in audit events and emits
    `audit_hook_event` records. Observes only; does not block.

Outside an active Session both interceptors fall through (don't
interfere with non-agent code in the same Python process).
"""

from __future__ import annotations

import socket
import urllib.error
import urllib.request

import pytest

from aegrail import (
    Agent,
    AuditSink,
    Budget,
    EgressNotPermitted,
)
from aegrail.interceptors import (
    install_audit_hook,
    intercept_outbound,
    uninstall_outbound,
)


def _agent(allowlist=None, **budget_kw):
    sink = AuditSink.memory()
    a = Agent(
        identity="bot/v1",
        budget=Budget(**(budget_kw or {"usd": 1.0})),
        audit=sink,
        egress_allowlist=allowlist,
    )
    return a, sink


def _find_closed_port() -> int:
    """Bind+close to claim a port the OS will refuse on connect."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture(autouse=True)
def _cleanup_interceptors():
    """Make sure every test starts with no global patches active."""
    yield
    uninstall_outbound()


class TestEgressAllowlistAgent:
    def test_allowlist_field_optional(self) -> None:
        agent = Agent(identity="bot/v1", budget=Budget(usd=1.0))
        assert agent.egress_allowlist is None

    def test_allowlist_field_accepts_list(self) -> None:
        agent = Agent(
            identity="bot/v1",
            budget=Budget(usd=1.0),
            egress_allowlist=["api.openai.com", "*.anthropic.com"],
        )
        assert agent.egress_allowlist == ["api.openai.com", "*.anthropic.com"]


class TestInterceptOutboundUrllib:
    def test_allowed_host_passes_through(self, capsys) -> None:
        agent, _sink = _agent(allowlist=["127.0.0.1"])
        port = _find_closed_port()
        intercept_outbound()
        # Allowed host: passes the egress check, then fails on the
        # network (port closed). The point is we see a URLError, not
        # an EgressNotPermitted — meaning the check let it through.
        with agent.session(user_id="alice"), pytest.raises(urllib.error.URLError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.2)

    def test_denied_host_raises_egress_not_permitted(self) -> None:
        agent, sink = _agent(allowlist=["api.openai.com"])
        intercept_outbound()
        with agent.session(user_id="alice"), pytest.raises(EgressNotPermitted) as excinfo:
            urllib.request.urlopen("http://example.com/", timeout=0.5)
        assert excinfo.value.host == "example.com"
        denied = [e for e in sink.events if e.event == "egress_denied"]
        assert len(denied) == 1
        assert denied[0].payload["host"] == "example.com"

    def test_wildcard_pattern_matches_subdomain(self) -> None:
        agent, _sink = _agent(allowlist=["127.*"])
        intercept_outbound()
        port = _find_closed_port()
        # 127.0.0.1 matches 127.* — passes the egress check, then
        # fails on the network (port closed), proving the check
        # let it through.
        with agent.session(user_id="alice"), pytest.raises(urllib.error.URLError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.2)

    def test_wildcard_does_not_match_naked_domain(self) -> None:
        agent, _sink = _agent(allowlist=["*.openai.com"])
        intercept_outbound()
        with agent.session(user_id="alice"), pytest.raises(EgressNotPermitted):
            # openai.com does not match *.openai.com (fnmatch semantics)
            urllib.request.urlopen("http://openai.com/", timeout=0.5)

    def test_no_session_means_no_interception(self) -> None:
        """Outside a Session, intercept_outbound() must NOT interfere."""
        intercept_outbound()
        # Random host with closed port — should fail on the network, not
        # because of any egress check (there's no active session).
        port = _find_closed_port()
        with pytest.raises(urllib.error.URLError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.1)

    def test_empty_allowlist_denies_all(self) -> None:
        agent, sink = _agent(allowlist=[])
        intercept_outbound()
        with agent.session(user_id="alice"), pytest.raises(EgressNotPermitted):
            urllib.request.urlopen("http://example.com/", timeout=0.5)
        assert any(e.event == "egress_denied" for e in sink.events)

    def test_allowlist_none_means_open(self) -> None:
        """An agent without egress_allowlist set is the explicit
        opt-out; interceptors should let calls through.
        """
        agent, _sink = _agent(allowlist=None)
        intercept_outbound()
        port = _find_closed_port()
        with agent.session(user_id="alice"), pytest.raises(urllib.error.URLError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.1)


class TestInterceptOutboundRequests:
    def test_denied_host_raises_egress_not_permitted(self) -> None:
        requests = pytest.importorskip("requests")
        agent, sink = _agent(allowlist=["api.openai.com"])
        intercept_outbound()
        with agent.session(user_id="alice"), pytest.raises(EgressNotPermitted) as excinfo:
            requests.get("http://example.com/", timeout=0.5)
        assert excinfo.value.host == "example.com"
        denied = [e for e in sink.events if e.event == "egress_denied"]
        assert len(denied) == 1


class TestEgressDeniedAuditEvent:
    def test_event_carries_principal_and_session_id(self) -> None:
        agent, sink = _agent(allowlist=["api.openai.com"])
        intercept_outbound()
        with agent.session(user_id="alice") as s, pytest.raises(EgressNotPermitted):
            urllib.request.urlopen("http://example.com/", timeout=0.5)
        denied = next(e for e in sink.events if e.event == "egress_denied")
        assert denied.session_id == s.session_id
        assert denied.principal == s.principal
        assert denied.invoking_user == "alice"
        assert denied.payload["host"] == "example.com"
        assert denied.payload["url"].startswith("http://example.com")
        assert denied.payload["reason"] == "not_in_allowlist"

    def test_egress_events_chain_with_other_events(self) -> None:
        from aegrail.audit import verify_chain

        agent, sink = _agent(allowlist=["allowed.example"])
        intercept_outbound()
        with agent.session(user_id="alice") as s:
            s.record_llm(model="m", tokens_in=1, tokens_out=1, cost_usd=0.0)
            with pytest.raises(EgressNotPermitted):
                urllib.request.urlopen("http://blocked.example/", timeout=0.5)
        valid, bad = verify_chain(sink.events)
        assert valid is True, f"chain broken at {bad}"


class TestAuditHook:
    def test_install_audit_hook_observes_subprocess(self) -> None:
        import subprocess

        agent, sink = _agent()
        install_audit_hook(agent)
        with agent.session(user_id="alice"):
            subprocess.run(["true"], check=False, capture_output=True)
        hook_events = [e for e in sink.events if e.event == "audit_hook_event"]
        # subprocess emits at least one audit event
        assert len(hook_events) >= 1
        # one of them mentions subprocess
        names = {e.payload["audit_name"] for e in hook_events}
        assert any("subprocess" in n for n in names)

    def test_audit_hook_outside_session_does_not_emit(self) -> None:
        import subprocess

        agent, sink = _agent()
        install_audit_hook(agent)
        # Outside session — hook fires but does not emit
        subprocess.run(["true"], check=False, capture_output=True)
        hook_events = [e for e in sink.events if e.event == "audit_hook_event"]
        assert hook_events == []

    def test_install_audit_hook_is_idempotent(self) -> None:
        """Installing twice must not register duplicate hooks (which
        would produce duplicate events)."""
        agent, sink = _agent()
        install_audit_hook(agent)
        install_audit_hook(agent)
        with agent.session(user_id="alice") as s:
            s.record_llm(model="m", tokens_in=1, tokens_out=1, cost_usd=0.0)
        # The record_llm itself shouldn't trigger built-in audit events,
        # but the test is about idempotency — checking no crash from
        # double-install and the chain stays valid.
        from aegrail.audit import verify_chain

        valid, _ = verify_chain(sink.events)
        assert valid is True


class TestEnvVarAutoInstall:
    def test_env_var_auto_installs_on_agent_construction(self, monkeypatch) -> None:
        """With AEGRAIL_INTERCEPT=1 set, constructing an Agent auto-
        installs intercept_outbound + install_audit_hook."""
        monkeypatch.setenv("AEGRAIL_INTERCEPT", "1")
        agent, sink = _agent(allowlist=["allowed.example"])
        # The interceptor must now be active without us calling it
        with agent.session(user_id="alice"), pytest.raises(EgressNotPermitted):
            urllib.request.urlopen("http://blocked.example/", timeout=0.5)
        assert any(e.event == "egress_denied" for e in sink.events)

    def test_env_var_not_set_means_no_auto_install(self, monkeypatch) -> None:
        monkeypatch.delenv("AEGRAIL_INTERCEPT", raising=False)
        agent, _sink = _agent(allowlist=["allowed.example"])
        # Without env var or explicit intercept_outbound(), the call
        # passes through (no interception)
        port = _find_closed_port()
        with agent.session(user_id="alice"), pytest.raises(urllib.error.URLError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.1)


class TestUninstallOutbound:
    def test_uninstall_restores_original_behavior(self) -> None:
        agent, _sink = _agent(allowlist=["api.openai.com"])
        intercept_outbound()
        uninstall_outbound()
        # Now egress check should not fire
        port = _find_closed_port()
        with agent.session(user_id="alice"), pytest.raises(urllib.error.URLError):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=0.1)


class TestEgressNotPermitted:
    def test_exception_has_attributes(self) -> None:
        exc = EgressNotPermitted("example.com", "http://example.com/x", "not_in_allowlist")
        assert exc.host == "example.com"
        assert exc.url == "http://example.com/x"
        assert exc.reason == "not_in_allowlist"
        assert "example.com" in str(exc)
