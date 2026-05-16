"""aegrail — the runtime contract for AI agents in production.

Primitives:

  - Scoped identity         (Agent + Session)
  - Hard budget kill-switch (Budget + BudgetState)
  - Structured audit log    (AuditEvent + AuditSink)
  - Per-agent tool ACL      (Tool + ToolNotPermitted)   [v0.2]

The tool ACL enforces OWASP Top 10 for Agentic Applications controls
ASI02 (Tool Misuse) and ASI03 (Identity & Privilege Abuse) in
deterministic Python at the runtime boundary.
"""

from .agent import Agent
from .async_session import AsyncSession
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
from .exceptions import (
    AegrailError,
    BudgetExceeded,
    EgressNotPermitted,
    SessionTerminated,
    ToolNotPermitted,
)
from .interceptors import (
    install_audit_hook,
    intercept_outbound,
    uninstall_outbound,
)
from .session import Session
from .tool import Tool

__version__ = "0.3.3"

__all__ = [
    "AegrailError",
    "Agent",
    "AsyncSession",
    "AuditEvent",
    "AuditSink",
    "Budget",
    "BudgetExceeded",
    "BudgetState",
    "CallbackAuditSink",
    "CompositeAuditSink",
    "EgressNotPermitted",
    "FileAuditSink",
    "MemoryAuditSink",
    "Session",
    "SessionTerminated",
    "StdoutAuditSink",
    "Tool",
    "ToolNotPermitted",
    "WebhookAuditSink",
    "__version__",
    "install_audit_hook",
    "intercept_outbound",
    "uninstall_outbound",
]
