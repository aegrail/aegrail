"""A Session is one bounded execution of an agent on behalf of a user.

Every action the agent takes inside a session is recorded against
its session principal and counted against its budget. The session
is the runtime unit that makes enforcement deterministic — code
decides what happens at each boundary, not the LLM.
"""

from __future__ import annotations

import contextvars
import sys
import time
import traceback
from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import Any

from .audit import AuditEvent, AuditSink
from .budget import Budget, BudgetState
from .exceptions import BudgetExceeded, SessionTerminated, ToolNotPermitted
from .identity import new_session_id, session_principal
from .tool import Tool

# Tracks the currently active Session for the current task / thread.
# In-process interceptors (aegrail.interceptors) read this to scope
# their enforcement to active sessions only — outside any session, the
# interceptors pass through and don't interfere with non-agent code.
current_session: contextvars.ContextVar[Session | None] = contextvars.ContextVar(
    "aegrail_current_session", default=None
)


class Session(AbstractContextManager["Session"]):
    """A scoped, budgeted, audited unit of agent work.

    Constructed via `Agent.session(...)`. Use as a context manager:

        with agent.session(user_id="alice", task="...") as s:
            s.record_llm(model="...", tokens_in=..., tokens_out=..., cost_usd=...)
            s.call_tool("refund", order_id=4521)

    The session's `call_tool` looks up the named tool in the agent's
    registry; calls to unregistered tools or to tools whose `when`
    predicate denies the args raise ToolNotPermitted deterministically.

    Outside the `with` block the session is closed and further calls
    raise SessionTerminated.
    """

    def __init__(
        self,
        agent_identity: str,
        budget: Budget,
        sink: AuditSink,
        user_id: str | None,
        task: str | None,
        tools: Mapping[str, Tool] | None,
    ) -> None:
        self.agent_identity = agent_identity
        self.user_id = user_id
        self.task = task
        self.session_id = new_session_id()
        self.principal = session_principal(agent_identity, self.session_id)
        self._sink = sink
        self._state = BudgetState(budget)
        self._tools = tools
        self._closed = False
        self._terminated_reason: str | None = None
        # Set by Agent.session() / Agent.async_session() right after
        # construction. Read by aegrail.interceptors at HTTP-call time.
        # None means "no allowlist configured for this agent — open egress".
        self._egress_allowlist: list[str] | None = None

    # --- lifecycle --------------------------------------------------

    def __enter__(self) -> Session:
        self._cv_token = current_session.set(self)
        self._emit("session_start", {"task": self.task})
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        import contextlib

        payload: dict[str, Any] = {"reason": self._terminated_reason or "normal"}
        if exc is not None and not isinstance(exc, BudgetExceeded):
            payload["error_type"] = exc_type.__name__ if exc_type else None
            payload["error_message"] = str(exc)
        self._emit("session_end", payload)
        self._closed = True
        with contextlib.suppress(ValueError, LookupError):
            current_session.reset(self._cv_token)
        # Don't suppress (the outer except).

    # --- recording helpers -----------------------------------------

    def record_llm(
        self,
        *,
        model: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
        prompt_summary: str | None = None,
        response_summary: str | None = None,
        latency_ms: float | None = None,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
    ) -> None:
        """Record an LLM call that already happened.

        Provider-agnostic by design: you call your LLM however you
        like (OpenAI SDK, Anthropic SDK, litellm, raw HTTP) and tell
        aegrail what it cost. The runtime updates the budget and
        emits an audit event. Budget violations surface here.

        Prompt-caching support (v0.2.5):
          - `cache_read_tokens`: input tokens served from the
            provider's prompt cache on this call. Subset of
            `tokens_in`; caller's responsibility to keep that
            relationship honest. Surfaces in the `llm_call` audit
            event so ops can derive cache hit rate over time.
          - `cache_write_tokens`: input tokens written to the
            provider's cache on this call (Anthropic) or accounted
            as cache writes by the provider's pricing model. Same
            audit treatment.
          - Both fields default to 0 (no caching). Budget's
            `tokens_used` counts total tokens regardless of cache
            status — `cost_usd` is where the cache discount is
            reflected, and that stays the caller's calculation.
        """
        self._require_open()
        total_tokens = max(0, int(tokens_in)) + max(0, int(tokens_out))
        self._state.add_tokens(total_tokens)
        self._state.add_usd(float(cost_usd))
        self._emit(
            "llm_call",
            {
                "model": model,
                "tokens_in": int(tokens_in),
                "tokens_out": int(tokens_out),
                "cost_usd": round(float(cost_usd), 6),
                "prompt_summary": prompt_summary,
                "response_summary": response_summary,
                "latency_ms": latency_ms,
                "cache_read_tokens": int(cache_read_tokens),
                "cache_write_tokens": int(cache_write_tokens),
            },
        )
        self._check_budget_or_emit("llm_call")

    def call_tool(self, name: str, /, **kwargs: Any) -> Any:
        """Invoke a registered tool by name.

        Resolution:
          1. If the agent has no tool registry, the call is denied
             (`not_registered`).
          2. If `name` is not in the registry, the call is denied
             (`not_registered`).
          3. If the tool has a `when` predicate, evaluate it against
             `kwargs`. Returning False denies (`predicate_false`).
             Raising any exception denies (`predicate_error`).
          4. Otherwise advance the tool-call counter, check the
             budget, then invoke `tool.fn(**kwargs)`.

        Denied calls do not consume the tool-call budget — they emit
        a `tool_denied` audit event and raise ToolNotPermitted.
        """
        self._require_open()

        args_summary = _args_keys_only(kwargs)

        if self._tools is None:
            return self._deny(name, "not_registered", "agent has no registered tools", args_summary)

        tool = self._tools.get(name)
        if tool is None:
            return self._deny(
                name,
                "not_registered",
                f"tool {name!r} not registered for {self.agent_identity}",
                args_summary,
            )

        if tool.when is not None:
            try:
                allowed = bool(tool.when(dict(kwargs)))
            except Exception as exc:
                self._emit_denied(
                    name,
                    "predicate_error",
                    args_summary,
                    error=f"{type(exc).__name__}: {exc}",
                )
                raise ToolNotPermitted(
                    "predicate_error",
                    f"predicate raised for tool {name!r}: {type(exc).__name__}: {exc}",
                    tool_name=name,
                ) from exc
            if not allowed:
                return self._deny(
                    name,
                    "predicate_false",
                    f"args denied by policy for tool {name!r}",
                    args_summary,
                )

        payload_args = _redact_or_keys(tool, kwargs, args_summary)

        self._state.add_tool_call()
        self._check_budget_or_emit("tool_call")

        started = time.monotonic()
        try:
            result = tool.fn(**kwargs)
        except Exception as exc:
            elapsed_ms = (time.monotonic() - started) * 1000
            self._emit(
                "tool_call",
                {
                    "tool": name,
                    "description": tool.description,
                    "args": payload_args,
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "elapsed_ms": round(elapsed_ms, 3),
                },
            )
            raise

        elapsed_ms = (time.monotonic() - started) * 1000
        self._emit(
            "tool_call",
            {
                "tool": name,
                "description": tool.description,
                "args": payload_args,
                "ok": True,
                "elapsed_ms": round(elapsed_ms, 3),
            },
        )
        return result

    def check_budget(self) -> None:
        """Raise BudgetExceeded if any ceiling has been crossed.

        Safe to call between LLM iterations or before expensive work.
        """
        self._require_open()
        self._check_budget_or_emit("manual_check")

    def enter_recursion(self) -> None:
        """Increment the recursion counter — call when an agent calls itself."""
        self._require_open()
        self._state.enter_recursion()
        self._check_budget_or_emit("enter_recursion")

    def exit_recursion(self) -> None:
        self._require_open()
        self._state.exit_recursion()

    # --- introspection ---------------------------------------------

    @property
    def state_snapshot(self) -> dict:
        return self._state.snapshot()

    # --- internals --------------------------------------------------

    def _require_open(self) -> None:
        if self._closed:
            raise SessionTerminated(f"session {self.session_id} is closed")

    def _check_budget_or_emit(self, source: str) -> None:
        try:
            self._state.check()
        except BudgetExceeded as exc:
            self._terminated_reason = f"budget_exceeded:{exc.reason}"
            self._emit(
                "budget_exceeded",
                {"reason": exc.reason, "message": str(exc), "source": source},
            )
            raise

    def _deny(
        self,
        name: str,
        reason: str,
        message: str,
        args_summary: dict,
    ) -> None:
        self._emit_denied(name, reason, args_summary)
        raise ToolNotPermitted(reason, message, tool_name=name)

    def _emit_denied(
        self,
        name: str,
        reason: str,
        args_summary: dict,
        *,
        error: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "tool": name,
            "reason": reason,
            "args": args_summary,
        }
        if error is not None:
            payload["error"] = error
        self._emit("tool_denied", payload)

    def _emit(self, event: str, payload: dict[str, Any]) -> None:
        try:
            evt = AuditEvent(
                session_id=self.session_id,
                agent_identity=self.agent_identity,
                invoking_user=self.user_id,
                principal=self.principal,
                event=event,  # type: ignore[arg-type]
                payload=payload,
                budget=self._state.snapshot(),
            )
        except Exception as exc:
            print(
                f"[aegrail] failed to build audit event ({event}): {exc}\n{traceback.format_exc()}",
                file=sys.stderr,
            )
            return
        self._sink.emit(evt)


def _args_keys_only(kwargs: dict) -> dict:
    """PII-safe default: log kwarg keys, never values."""
    return {"kwarg_keys": sorted(kwargs.keys())}


def _redact_or_keys(tool: Tool, kwargs: dict, fallback: dict) -> dict:
    """Apply the tool's redactor if present; fall back to keys-only on error."""
    if tool.redact is None:
        return fallback
    try:
        return dict(tool.redact(dict(kwargs)))
    except Exception as exc:
        print(
            f"[aegrail] tool {tool.name!r} redactor failed: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return fallback
