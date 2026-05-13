"""Tool definitions and the per-agent tool ACL (v0.2).

A Tool is a callable an agent has been explicitly authorised to invoke.
It carries an optional argument predicate (`when`) and an optional
redactor (`redact`) for what gets emitted into the audit payload.

The runtime enforces two OWASP Top 10 for Agentic Applications controls:

  - ASI02 (Tool Misuse) — the registry caps the set of callables, and
    the `when` predicate caps the argument shapes the agent may call
    them with. Both are deterministic Python code, not LLM-mediated.
  - ASI03 (Identity & Privilege Abuse) — the registry hangs off the
    Agent's identity. Two agents in the same process with disjoint
    registries cannot cross-invoke each other's tools.

Tools are immutable after construction. They are bound to the agent
when the Agent is constructed; a session inherits its agent's registry
and cannot be re-bound mid-flight.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

_TOOL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


class Tool(BaseModel):
    """A callable an agent may invoke, with optional arg constraint and redactor.

    Fields:
      name:         tool identifier (lowercase, `[a-z0-9._-]`). Used as the
                    key in the agent's registry and as `payload.tool` in
                    audit events.
      fn:           the Python callable. Invoked with keyword arguments
                    only — positional args are not supported at the
                    call site.
      description:  free-form human/LLM-readable description, surfaced
                    into audit payloads for forensic review.
      when:         optional predicate `dict -> bool`. Receives the
                    kwargs dict the caller passed. Returning False, or
                    raising any exception, denies the call.
      redact:       optional `dict -> dict`. Lets the tool definition
                    control what shows up in `payload.args` for the
                    audit event. If absent, only kwarg *keys* are
                    logged — never values. If the redactor itself
                    raises, the runtime falls back to keys-only and
                    logs the failure to stderr.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    name: str
    fn: Callable[..., Any]
    description: str | None = None
    when: Callable[[dict[str, Any]], bool] | None = None
    redact: Callable[[dict[str, Any]], dict[str, Any]] | None = None

    @model_validator(mode="after")
    def _validate(self) -> Tool:
        if not isinstance(self.name, str) or not _TOOL_NAME_RE.match(self.name):
            raise ValueError(f"invalid tool name {self.name!r}: expected [a-z0-9._-]")
        if not callable(self.fn):
            raise ValueError("Tool.fn must be callable")
        if self.when is not None and not callable(self.when):
            raise ValueError("Tool.when must be callable if provided")
        if self.redact is not None and not callable(self.redact):
            raise ValueError("Tool.redact must be callable if provided")
        return self
