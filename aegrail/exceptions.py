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


class ToolNotPermitted(AegrailError):
    """Raised when a tool call is denied by the agent's policy.

    `reason` is a short machine-readable string:
      'not_registered'    — name not in the agent's tool registry, or
                            the agent has no registry at all.
      'predicate_false'   — the tool's `when` predicate returned False.
      'predicate_error'   — the tool's `when` predicate raised; the
                            wrapped exception is the `__cause__`.

    `tool_name` is the name the caller attempted to invoke (kept on the
    exception even when the tool is unregistered, so callers branching
    on it have something to log).
    """

    def __init__(
        self,
        reason: str,
        message: str,
        tool_name: str | None = None,
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.tool_name = tool_name
