"""Exceptions raised by the agentctl runtime.

These are *deterministic* — they fire when the runtime decides the session
cannot continue, regardless of what the LLM has been instructed to do.
"""

from __future__ import annotations


class AgentctlError(Exception):
    """Base class for all agentctl runtime errors."""


class BudgetExceeded(AgentctlError):
    """Raised when a hard budget ceiling is hit.

    `reason` is a short machine-readable string: 'usd', 'tokens',
    'wall_seconds', 'recursion', 'tool_calls'. `state` carries the
    BudgetState at the moment of the violation for use by callers
    that want to surface it.
    """

    def __init__(self, reason: str, message: str, state: object | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.state = state


class SessionTerminated(AgentctlError):
    """Raised when a caller attempts to use a session that has already ended."""
