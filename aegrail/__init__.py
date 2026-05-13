"""aegrail — the runtime contract for AI agents in production.

Three primitives, deliberately:

  - Scoped identity         (Agent + Session)
  - Hard budget kill-switch (Budget + BudgetState)
  - Structured audit log    (AuditEvent + AuditSink)

Everything else is for later versions.
"""

from .agent import Agent
from .audit import (
    AuditEvent,
    AuditSink,
    CallbackAuditSink,
    CompositeAuditSink,
    FileAuditSink,
    MemoryAuditSink,
    StdoutAuditSink,
    WebhookAuditSink,
)
from .budget import Budget, BudgetState
from .exceptions import AegrailError, BudgetExceeded, SessionTerminated
from .session import Session

__version__ = "0.1.1"

__all__ = [
    "AegrailError",
    "Agent",
    "AuditEvent",
    "AuditSink",
    "Budget",
    "BudgetExceeded",
    "BudgetState",
    "CallbackAuditSink",
    "CompositeAuditSink",
    "FileAuditSink",
    "MemoryAuditSink",
    "Session",
    "SessionTerminated",
    "StdoutAuditSink",
    "WebhookAuditSink",
    "__version__",
]
