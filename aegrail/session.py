"""A Session is one bounded execution of an agent on behalf of a user.

Every action the agent takes inside a session is recorded against
its session principal and counted against its budget. The session
is the runtime unit that makes enforcement deterministic — code
decides what happens at each boundary, not the LLM.
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any, TypeVar

from .audit import AuditEvent, AuditSink
from .budget import Budget, BudgetState
from .exceptions import BudgetExceeded, SessionTerminated
from .identity import new_session_id, session_principal

T = TypeVar("T")


class Session(AbstractContextManager["Session"]):
    """A scoped, budgeted, audited unit of agent work.

    Constructed via `Agent.session(...)`. Use as a context manager:

        with agent.session(user_id="alice", task="...") as s:
            s.record_llm(model="...", tokens_in=..., tokens_out=..., cost_usd=...)
            s.call_tool("refund", my_refund_fn, order_id=4521)

    Outside the `with` block the session is closed and further
    calls raise SessionTerminated.
    """

    def __init__(
        self,
        agent_identity: str,
        budget: Budget,
        sink: AuditSink,
        user_id: str | None,
        task: str | None,
    ) -> None:
        self.agent_identity = agent_identity
        self.user_id = user_id
        self.task = task
        self.session_id = new_session_id()
        self.principal = session_principal(agent_identity, self.session_id)
        self._sink = sink
        self._state = BudgetState(budget)
        self._closed = False
        self._terminated_reason: str | None = None

    # --- lifecycle --------------------------------------------------

    def __enter__(self) -> Session:
        self._emit("session_start", {"task": self.task})
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        payload: dict[str, Any] = {"reason": self._terminated_reason or "normal"}
        if exc is not None and not isinstance(exc, BudgetExceeded):
            payload["error_type"] = exc_type.__name__ if exc_type else None
            payload["error_message"] = str(exc)
        self._emit("session_end", payload)
        self._closed = True
        # Don't suppress.

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
    ) -> None:
        """Record an LLM call that already happened.

        Provider-agnostic by design: you call your LLM however you
        like (OpenAI SDK, Anthropic SDK, litellm, raw HTTP) and tell
        aegrail what it cost. The runtime updates the budget and
        emits an audit event. Budget violations surface here.
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
            },
        )
        self._check_budget_or_emit("llm_call")

    def call_tool(
        self,
        name: str,
        fn: Callable[..., T],
        /,
        *args: Any,
        _arg_summary: dict | None = None,
        **kwargs: Any,
    ) -> T:
        """Invoke a tool through the session.

        Wraps the call so that:
          - the tool-call counter advances against the budget
          - timing is recorded
          - a structured audit event is emitted whether the call
            succeeds, raises, or is short-circuited by the budget
          - budget violations propagate as BudgetExceeded

        `_arg_summary` lets the caller log a redacted view of the
        arguments. If omitted, aegrail emits only the argument
        *keys* — not their values — to avoid leaking PII into logs.
        """
        self._require_open()
        self._state.add_tool_call()
        self._check_budget_or_emit("tool_call")

        started = time.monotonic()
        try:
            result = fn(*args, **kwargs)
        except Exception as exc:
            elapsed_ms = (time.monotonic() - started) * 1000
            self._emit(
                "tool_call",
                {
                    "tool": name,
                    "args": _arg_summary or _keys_only(args, kwargs),
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
                "args": _arg_summary or _keys_only(args, kwargs),
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
            # Constructing an event should never fail; if it does,
            # write a low-level error to stderr but don't raise.
            import sys

            print(
                f"[aegrail] failed to build audit event ({event}): {exc}\n{traceback.format_exc()}",
                file=sys.stderr,
            )
            return
        self._sink.emit(evt)


def _keys_only(args: tuple, kwargs: dict) -> dict:
    """Return a redacted argument summary.

    Positional args are summarised as a count; keyword args expose
    their keys but not their values. Callers that want richer
    summaries can pass `_arg_summary` explicitly.
    """
    return {"positional_count": len(args), "kwarg_keys": sorted(kwargs.keys())}
