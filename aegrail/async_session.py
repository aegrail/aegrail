"""Async variant of Session.

Mirrors the sync Session surface (record_llm, call_tool, check_budget,
enter_recursion / exit_recursion) using async methods, and adds one
load-bearing property the sync mode cannot offer: wall_seconds is
enforced *mid-tool-call* via asyncio.wait_for. If a tool's HTTP call
hangs, the runtime raises BudgetExceeded('wall_seconds') deterministically
rather than waiting for the call to return.

Tool functions can be sync or async. Sync functions are dispatched via
asyncio.to_thread so the timeout still applies at the asyncio level.
CPython cannot kill the underlying thread, so a slow sync function
keeps running in the background until it naturally returns - the
session sees the timeout, the thread leaks the work. This is a Python
limitation, documented; for hard timeout guarantees, write tools as
async def.

Audit sinks remain synchronous. For high-throughput async hot paths
prefer file + callback-into-queue rather than the webhook sink, which
blocks the event loop for its 3s timeout on each emit.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from typing import Any

from .exceptions import BudgetExceeded, ToolNotPermitted
from .session import Session, _args_keys_only, _redact_or_keys


class AsyncSession(Session):
    """Async-aware Session. Construct via `Agent.async_session(...)`.

    Use as `async with`:

        async with agent.async_session(user_id="alice") as s:
            await s.record_llm(model="...", tokens_in=..., tokens_out=..., cost_usd=...)
            result = await s.call_tool("refund", order_id=4521)

    Sync `with` is intentionally disallowed - the context manager protocol
    on this class is async-only.
    """

    # --- block sync context-manager use --------------------------------

    def __enter__(self) -> AsyncSession:  # type: ignore[override]
        raise TypeError(
            "AsyncSession requires 'async with', not 'with'. "
            "Use `async with agent.async_session(...) as s:`."
        )

    def __exit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        return None

    # --- async lifecycle -----------------------------------------------

    async def __aenter__(self) -> AsyncSession:
        from .session import current_session

        self._cv_token = current_session.set(self)
        self._emit("session_start", {"task": self.task})
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        import contextlib

        from .session import current_session

        payload: dict[str, Any] = {"reason": self._terminated_reason or "normal"}
        if exc is not None and not isinstance(exc, BudgetExceeded):
            payload["error_type"] = exc_type.__name__ if exc_type else None
            payload["error_message"] = str(exc)
            if isinstance(exc, asyncio.CancelledError):
                payload["reason"] = "cancelled"
        self._emit("session_end", payload)
        self._closed = True
        with contextlib.suppress(ValueError, LookupError):
            current_session.reset(self._cv_token)

    # --- async recording helpers ---------------------------------------

    async def record_llm(  # type: ignore[override]
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
        """Record an LLM call. Same semantics as sync; provided as `async def`
        for API consistency so callers do not have to remember which methods
        need `await`.

        See `Session.record_llm` for documentation of the cache token
        parameters introduced in v0.2.5.
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

    async def call_tool(self, name: str, /, **kwargs: Any) -> Any:  # type: ignore[override]
        """Invoke a registered tool. Same resolution / ACL semantics as the
        sync `Session.call_tool`. The added behaviour: wall_seconds is
        enforced mid-call via asyncio.wait_for. If the tool runs past the
        remaining wall-clock budget, BudgetExceeded('wall_seconds') raises
        and the abandoned coroutine is cancelled."""
        self._require_open()

        args_summary = _args_keys_only(kwargs)

        if self._tools is None:
            return self._deny(
                name,
                "not_registered",
                "agent has no registered tools",
                args_summary,
            )

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

        remaining = self._remaining_wall_seconds()
        if remaining is not None and remaining <= 0:
            self._terminated_reason = "budget_exceeded:wall_seconds"
            self._emit(
                "budget_exceeded",
                {
                    "reason": "wall_seconds",
                    "message": "wall_seconds budget exhausted before tool invocation",
                    "source": "tool_call",
                },
            )
            raise BudgetExceeded(
                "wall_seconds",
                "wall_seconds budget exhausted before tool invocation",
                state=self._state,
            )

        started = time.monotonic()
        try:
            if inspect.iscoroutinefunction(tool.fn):
                awaitable = tool.fn(**kwargs)
            else:
                awaitable = asyncio.to_thread(tool.fn, **kwargs)
            if remaining is not None:
                result = await asyncio.wait_for(awaitable, timeout=remaining)
            else:
                result = await awaitable
        except (asyncio.TimeoutError, TimeoutError) as exc:
            self._terminated_reason = "budget_exceeded:wall_seconds"
            self._emit(
                "budget_exceeded",
                {
                    "reason": "wall_seconds",
                    "message": f"tool {name!r} exceeded wall_seconds budget",
                    "source": "tool_call",
                },
            )
            raise BudgetExceeded(
                "wall_seconds",
                f"tool {name!r} exceeded wall_seconds budget",
                state=self._state,
            ) from exc
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

    async def check_budget(self) -> None:  # type: ignore[override]
        self._require_open()
        self._check_budget_or_emit("manual_check")

    async def enter_recursion(self) -> None:  # type: ignore[override]
        self._require_open()
        self._state.enter_recursion()
        self._check_budget_or_emit("enter_recursion")

    async def exit_recursion(self) -> None:  # type: ignore[override]
        self._require_open()
        self._state.exit_recursion()

    # --- internals -----------------------------------------------------

    def _remaining_wall_seconds(self) -> float | None:
        """Seconds left in the wall_seconds budget, or None if unset."""
        budget = self._state.budget
        if budget.wall_seconds is None:
            return None
        return budget.wall_seconds - self._state.wall_elapsed
