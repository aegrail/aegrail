"""The Agent is the top-level factory.

An Agent ties together an identity (the role), a budget (the
ceiling), an audit sink (where evidence goes), and — from v0.2 — a
registry of tools the agent is authorised to invoke. It hands out
Sessions: each Session is one bounded run on behalf of a user, and
inherits the Agent's tool registry frozen at construction time.
"""

from __future__ import annotations

import contextlib
from collections.abc import Mapping
from types import MappingProxyType

from .async_session import AsyncSession
from .audit import AuditSink, StdoutAuditSink
from .budget import Budget
from .identity import validate_agent_identity
from .session import Session
from .tool import Tool


class Agent:
    """An agent role with declared identity, budget, audit sink, and tool ACL.

    Construction:

        agent = Agent(
            identity="support-bot/v1",
            budget=Budget(usd=5.0, tokens=100_000, wall_seconds=120),
            audit=AuditSink.file("./audit.jsonl"),
            tools={
                "refund": Tool(name="refund", fn=do_refund, when=lambda a: a["amount"] < 100),
            },
        )

    Use:

        with agent.session(user_id="alice", task="refund #4521") as s:
            s.call_tool("refund", amount=42, order_id=4521)

    If `tools` is omitted, the agent has no registered tools and any
    `session.call_tool(...)` call is denied with `reason='not_registered'`.
    This is deliberate: v0.2 forces opt-in to the tool ACL rather than
    silently allowing arbitrary calls.
    """

    @classmethod
    def from_env(
        cls,
        *,
        identity: str | None = None,
        budget: Budget | None = None,
        audit: AuditSink | None = None,
        tools: Mapping[str, Tool] | None = None,
        egress_allowlist: list[str] | None = None,
    ) -> Agent:
        """Construct an Agent with `AEGRAIL_*` env vars filling in
        anything not explicitly passed.

        Explicit kwargs always win. Env vars are fallback defaults so
        the same code runs on Cloud Run, AWS App Runner, Azure
        Container Apps, AWS Fargate, Kubernetes, or anywhere else the
        platform exposes env vars to the container.

          AEGRAIL_AGENT_IDENTITY         -> identity (required if not
                                            passed and no env var)
          AEGRAIL_BUDGET_USD, _TOKENS,   -> Budget axes (see
            _WALL_SECONDS,                  Budget.from_env)
            _MAX_RECURSION,
            _MAX_TOOL_CALLS
          AEGRAIL_EGRESS_ALLOWLIST       -> comma-separated host
                                            patterns
          AEGRAIL_AUDIT_FILE             -> AuditSink.file(path)
          AEGRAIL_AUDIT_STDOUT=1         -> AuditSink.stdout()
                                            (default if neither set)

        Tools must still be passed explicitly via `tools=` — they
        contain callable functions and predicates that have to live
        in code. The future external policy-file feature (see roadmap)
        adds operator-controlled tool gating on top of code-registered
        tool implementations.
        """
        import os

        if identity is None:
            identity = os.environ.get("AEGRAIL_AGENT_IDENTITY")
            if not identity:
                raise ValueError(
                    "Agent.from_env requires identity= or AEGRAIL_AGENT_IDENTITY env var"
                )

        if budget is None:
            budget = Budget.from_env()

        if audit is None:
            audit_file = os.environ.get("AEGRAIL_AUDIT_FILE")
            if audit_file:
                from .audit import FileAuditSink

                audit = FileAuditSink(audit_file)
            elif os.environ.get("AEGRAIL_AUDIT_STDOUT") == "1":
                from .audit import StdoutAuditSink

                audit = StdoutAuditSink()
            # else: Agent.__init__ defaults to StdoutAuditSink

        if egress_allowlist is None:
            raw = os.environ.get("AEGRAIL_EGRESS_ALLOWLIST")
            if raw is not None:
                egress_allowlist = [h.strip() for h in raw.split(",") if h.strip()]

        return cls(
            identity=identity,
            budget=budget,
            audit=audit,
            tools=tools,
            egress_allowlist=egress_allowlist,
        )

    def __init__(
        self,
        *,
        identity: str,
        budget: Budget,
        audit: AuditSink | None = None,
        tools: Mapping[str, Tool] | None = None,
        egress_allowlist: list[str] | None = None,
    ) -> None:
        self.identity = validate_agent_identity(identity)
        if not isinstance(budget, Budget):
            raise TypeError("budget must be an aegrail.Budget instance")
        self.budget = budget
        self.audit = audit if audit is not None else StdoutAuditSink()
        self.tools = _freeze_tools(tools)
        self.egress_allowlist: list[str] | None = (
            list(egress_allowlist) if egress_allowlist is not None else None
        )
        # If AEGRAIL_INTERCEPT=1 is set in the environment, auto-install
        # the in-process interceptors when this Agent is constructed.
        # Deployment platforms set this env var to enforce non-skippable
        # in-process defense-in-depth without requiring developer code
        # changes. See aegrail.interceptors for details.
        import os

        if os.environ.get("AEGRAIL_INTERCEPT") == "1":
            from .integrations import install_all as _install_provider_integrations
            from .interceptors import install_audit_hook, intercept_outbound

            intercept_outbound()
            install_audit_hook(self)
            # Auto-instrument LLM provider SDKs already imported in
            # the process (openai, anthropic, litellm, ...). Patched
            # `create` methods read the active session from the
            # ContextVar and transparently call session.check_budget
            # + session.record_llm around each LLM request — no code
            # change required by the agent author.
            _install_provider_integrations()

    def session(
        self,
        *,
        user_id: str | None = None,
        task: str | None = None,
    ) -> Session:
        """Open a new sync session. Use as a context manager."""
        s = Session(
            agent_identity=self.identity,
            budget=self.budget,
            sink=self.audit,
            user_id=user_id,
            task=task,
            tools=self.tools,
        )
        s._egress_allowlist = self.egress_allowlist
        return s

    def async_session(
        self,
        *,
        user_id: str | None = None,
        task: str | None = None,
    ) -> AsyncSession:
        """Open a new async session. Use as an async context manager.

        Adds one property over `session()`: when `budget.wall_seconds` is
        set, the runtime enforces the timeout mid-tool-call via
        asyncio.wait_for. Sync `session()` can only check at event
        boundaries.
        """
        s = AsyncSession(
            agent_identity=self.identity,
            budget=self.budget,
            sink=self.audit,
            user_id=user_id,
            task=task,
            tools=self.tools,
        )
        s._egress_allowlist = self.egress_allowlist
        return s

    def close(self) -> None:
        """Close the underlying audit sink. Idempotent."""
        with contextlib.suppress(Exception):
            self.audit.close()


def _freeze_tools(tools: Mapping[str, Tool] | None) -> Mapping[str, Tool] | None:
    if tools is None:
        return None
    validated: dict[str, Tool] = {}
    for key, value in tools.items():
        if not isinstance(value, Tool):
            raise TypeError(
                f"tool registry values must be aegrail.Tool instances; "
                f"got {type(value).__name__} for key {key!r}"
            )
        if key != value.name:
            raise ValueError(f"tool registry key {key!r} does not match Tool.name {value.name!r}")
        validated[key] = value
    return MappingProxyType(validated)
