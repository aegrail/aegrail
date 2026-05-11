"""agentctl — the runtime contract for AI agents in production.

Three primitives, deliberately:

  - Scoped identity         (Agent + Session)
  - Hard budget kill-switch (Budget + BudgetState)
  - Structured audit log    (AuditEvent + AuditSink)

Everything else is for later versions.
"""

from .agent import Agent
from .audit import AuditEvent, AuditSink, FileAuditSink, MemoryAuditSink, StdoutAuditSink
from .budget import Budget, BudgetState
from .exceptions import AgentctlError, BudgetExceeded, SessionTerminated
from .session import Session

__version__ = "0.1.0a0"

__all__ = [
    "Agent",
    "AgentctlError",
    "AuditEvent",
    "AuditSink",
    "Budget",
    "BudgetExceeded",
    "BudgetState",
    "FileAuditSink",
    "MemoryAuditSink",
    "Session",
    "SessionTerminated",
    "StdoutAuditSink",
    "__version__",
]
