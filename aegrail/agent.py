"""The Agent is the top-level factory.

An Agent ties together an identity (the role), a budget (the
ceiling), and an audit sink (where evidence goes). It hands out
Sessions — each Session is one bounded run on behalf of a user.

Agent itself is cheap to construct and reusable across many
sessions. Sessions are the unit of work.
"""

from __future__ import annotations

import contextlib

from .audit import AuditSink, StdoutAuditSink
from .budget import Budget
from .identity import validate_agent_identity
from .session import Session


class Agent:
    """An agent role with declared identity, budget, and audit sink.

    Construction:

        agent = Agent(
            identity="support-bot/v1",
            budget=Budget(usd=5.0, tokens=100_000, wall_seconds=120),
            audit=AuditSink.file("./audit.jsonl"),
        )

    Use:

        with agent.session(user_id="alice", task="refund #4521") as s:
            ...
    """

    def __init__(
        self,
        *,
        identity: str,
        budget: Budget,
        audit: AuditSink | None = None,
    ) -> None:
        self.identity = validate_agent_identity(identity)
        if not isinstance(budget, Budget):
            raise TypeError("budget must be an aegrail.Budget instance")
        self.budget = budget
        self.audit = audit if audit is not None else StdoutAuditSink()

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
        )

    def close(self) -> None:
        """Close the underlying audit sink. Idempotent."""
        with contextlib.suppress(Exception):
            self.audit.close()
