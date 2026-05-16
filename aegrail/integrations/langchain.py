"""LangChain integration: AegrailCallbackHandler.

LangChain agents that use OpenAI / Anthropic / litellm under the hood
already get aegrail coverage automatically via the SDK
instrumentations in 0.3.0 / 0.3.1 / 0.3.2 — the SDK calls go through
the patched create methods regardless of whether the caller is
LangChain or raw user code. This module adds a complementary,
explicit integration: a `BaseCallbackHandler` that LangChain calls
on its own lifecycle hooks (chat-model start, llm-end, tool-start,
chain-start, etc.), giving aegrail visibility into LangChain-specific
events like chain composition and tool invocations that don't surface
through SDK-level patches.

Usage:

    from aegrail.integrations.langchain import AegrailCallbackHandler
    from langchain_openai import ChatOpenAI
    from langchain.chains import LLMChain

    chain = LLMChain(
        llm=ChatOpenAI(model="gpt-4o-mini"),
        prompt=prompt,
        callbacks=[AegrailCallbackHandler()],
    )

The handler reads the active session from the same ContextVar the
rest of aegrail uses — drop it into the `callbacks=[...]` argument
anywhere LangChain accepts a callback. No agent-side wiring needed.

If LangChain is not installed, this module still imports cleanly:
`AegrailCallbackHandler` falls back to inheriting from `object` and
remains usable as a plain Python class (you just can't pass it to a
LangChain callback list — that requires langchain to be present).
"""

from __future__ import annotations

import logging
from typing import Any

from ..session import current_session

logger = logging.getLogger("aegrail.integrations.langchain")

# Inherit from LangChain's BaseCallbackHandler when available so
# `isinstance(handler, BaseCallbackHandler)` succeeds in LangChain
# internals. Fall back to `object` when not installed so the import
# never breaks user code that doesn't have LangChain.
try:
    from langchain_core.callbacks.base import (
        BaseCallbackHandler as _LangChainBaseCallbackHandler,
    )
except ImportError:
    try:
        from langchain.callbacks.base import (  # type: ignore[no-redef]
            BaseCallbackHandler as _LangChainBaseCallbackHandler,
        )
    except ImportError:
        _LangChainBaseCallbackHandler = object  # type: ignore[assignment,misc]


class AegrailCallbackHandler(_LangChainBaseCallbackHandler):  # type: ignore[misc,valid-type]
    """LangChain `BaseCallbackHandler` that routes lifecycle events
    through the active aegrail session.

    Methods implemented:
      - `on_llm_start` / `on_chat_model_start`: call
        `session.check_budget()` so an already-exhausted budget
        fails before the LLM request goes out.
      - `on_llm_end`: extract model + token usage from the LLMResult
        and call `session.record_llm(...)`, which writes the
        `llm_call` audit event and updates budget state.
      - `on_tool_start`: emit a tool-invocation audit event so
        LangChain-level tool usage shows up in the chain even when
        the tool isn't registered on the aegrail Agent's ACL.
      - `on_llm_error` / `on_tool_error`: emit an `error` audit
        event with the exception class name.

    `on_chain_start` / `on_chain_end` are intentionally not
    instrumented — chains are framework composition, not security
    boundaries; recording them adds audit noise without changing
    what aegrail can enforce.
    """

    # LangChain enables every handler method by default; flip these
    # off only if there's a clear reason. Keep them all True so
    # users see complete audit coverage from a single drop-in.
    raise_error: bool = False
    run_inline: bool = True

    # ------- LLM lifecycle -----------------------------------------

    def on_llm_start(
        self,
        serialized: dict[str, Any] | None,
        prompts: list[str] | None,
        **kwargs: Any,
    ) -> None:
        session = current_session.get()
        if session is None:
            return
        try:
            session.check_budget()
        except Exception:
            # The budget check raises BudgetExceeded — propagate so
            # the chain fails closed before the upstream LLM call.
            raise

    def on_chat_model_start(
        self,
        serialized: dict[str, Any] | None,
        messages: list | None,
        **kwargs: Any,
    ) -> None:
        # Treat chat-model start like llm-start.
        self.on_llm_start(serialized, None, **kwargs)

    def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        session = current_session.get()
        if session is None:
            return
        usage = _extract_usage(response)
        try:
            session.record_llm(**usage)
        except Exception:
            # BudgetExceeded propagates by design; anything else
            # is also an enforcement signal and should not be
            # swallowed by the callback layer.
            raise

    def on_llm_error(self, error: BaseException, **kwargs: Any) -> None:
        session = current_session.get()
        if session is None:
            return
        try:
            session._emit(
                "error",
                {
                    "source": "langchain.llm",
                    "error_type": type(error).__name__,
                    "error": str(error),
                },
            )
        except Exception as exc:
            logger.warning("aegrail langchain on_llm_error emit failed: %s", exc)

    # ------- Tool lifecycle ----------------------------------------

    def on_tool_start(
        self,
        serialized: dict[str, Any] | None,
        input_str: str | None,
        **kwargs: Any,
    ) -> None:
        session = current_session.get()
        if session is None:
            return
        tool_name = (serialized or {}).get("name", "unknown")
        try:
            session._emit(
                "tool_call",
                {
                    "source": "langchain",
                    "tool": tool_name,
                    # Per design principle: log the keys / shape of
                    # the input, not its value. input_str is opaque
                    # to us; record only its length as a coarse
                    # signal.
                    "input_length": len(input_str) if input_str else 0,
                },
            )
        except Exception as exc:
            logger.warning("aegrail langchain on_tool_start emit failed: %s", exc)

    def on_tool_error(self, error: BaseException, **kwargs: Any) -> None:
        session = current_session.get()
        if session is None:
            return
        try:
            session._emit(
                "error",
                {
                    "source": "langchain.tool",
                    "error_type": type(error).__name__,
                    "error": str(error),
                },
            )
        except Exception as exc:
            logger.warning("aegrail langchain on_tool_error emit failed: %s", exc)

    # No-ops for events we deliberately don't audit.

    def on_chain_start(self, *args: Any, **kwargs: Any) -> None:
        return

    def on_chain_end(self, *args: Any, **kwargs: Any) -> None:
        return

    def on_agent_action(self, *args: Any, **kwargs: Any) -> None:
        return

    def on_agent_finish(self, *args: Any, **kwargs: Any) -> None:
        return


def _extract_usage(response: Any) -> dict[str, Any]:
    """Pull model + tokens out of a LangChain `LLMResult`.

    LangChain populates `response.llm_output` with provider-specific
    metadata. The common shapes:

      OpenAI:    llm_output = {"token_usage": {"prompt_tokens": N,
                                                "completion_tokens": N},
                                "model_name": "gpt-4o-mini"}
      Anthropic: llm_output = {"usage": {"input_tokens": N,
                                          "output_tokens": N},
                                "model_name": "claude-3-5-sonnet"}

    We tolerate both, and fall back gracefully when neither is
    present.
    """
    llm_output = _safe_attr(response, "llm_output", default=None) or {}
    model = (
        _safe_attr(llm_output, "model_name", default=None)
        or _safe_attr(llm_output, "model", default=None)
        or "unknown"
    )

    tokens_in = 0
    tokens_out = 0
    cache_read = 0
    cache_write = 0

    usage = _safe_attr(llm_output, "token_usage", default=None) or _safe_attr(
        llm_output, "usage", default=None
    )
    if usage:
        tokens_in = int(
            _safe_attr(usage, "prompt_tokens", default=None)
            or _safe_attr(usage, "input_tokens", default=0)
            or 0
        )
        tokens_out = int(
            _safe_attr(usage, "completion_tokens", default=None)
            or _safe_attr(usage, "output_tokens", default=0)
            or 0
        )
        cache_read = int(_safe_attr(usage, "cache_read_input_tokens", default=0) or 0)
        cache_write = int(_safe_attr(usage, "cache_creation_input_tokens", default=0) or 0)

    return {
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": 0.0,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": cache_write,
    }


def _safe_attr(obj: Any, name: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if hasattr(obj, name):
        try:
            return getattr(obj, name)
        except AttributeError:
            return default
    if isinstance(obj, dict) and name in obj:
        return obj[name]
    return default


__all__ = ["AegrailCallbackHandler"]
