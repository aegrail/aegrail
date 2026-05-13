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

    def __init__(
        self,
        *,
        identity: str,
        budget: Budget,
        audit: AuditSink | None = None,
        tools: Mapping[str, Tool] | None = None,
    ) -> None:
        self.identity = validate_agent_identity(identity)
        if not isinstance(budget, Budget):
            raise TypeError("budget must be an aegrail.Budget instance")
        self.budget = budget
        self.audit = audit if audit is not None else StdoutAuditSink()
        self.tools = _freeze_tools(tools)

    def session(
        self,
        *,
        user_id: str | None = None,
        task: str | None = None,
    ) -> Session:
        """Open a new session. Use as a context manager."""
        return Session(
            agent_identity=self.identity,
            budget=self.budget,
            sink=self.audit,
            user_id=user_id,
            task=task,
            tools=self.tools,
        )

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
