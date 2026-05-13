"""Exceptions raised by the aegrail runtime.

These are *deterministic* — they fire when the runtime decides the session
cannot continue, regardless of what the LLM has been instructed to do.
"""

from __future__ import annotations


class AegrailError(Exception):
    """Base class for all aegrail runtime errors."""


class BudgetExceeded(AegrailError):
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


class SessionTerminated(AegrailError):
    """Raised when a caller attempts to use a session that has already ended."""
