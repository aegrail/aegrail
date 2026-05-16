"""Anthropic Python SDK auto-instrumentation.

Patches the `create` methods on:
    anthropic.resources.messages.Messages       (sync)
    anthropic.resources.messages.AsyncMessages  (async)

Each patched method:
  1. Looks up the active `aegrail.Session` from the ContextVar.
  2. If no session is active, passes through untouched.
  3. Else calls `session.check_budget()` to fail-fast on an already-
     exceeded ceiling.
  4. Calls the original method.
  5. Extracts model + token usage from the response, including
     Anthropic's cache_creation_input_tokens and cache_read_input_tokens
     for prompt caching (Claude 3.5+).
  6. Calls `session.record_llm(...)`, which writes the `llm_call`
     audit event and increments the budget state.

Streaming requests (`stream=True`) pass through without auto-record;
usage is delivered on the final message_stop event and the wrapped
stream object needs shape-specific handling that's deferred to a
later release.

Cost calculation is the caller's responsibility (CLAUDE.md design
principle: "No baked-in price table"). Auto-recorded cost_usd is 0.0.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from ..session import current_session

logger = logging.getLogger("aegrail.integrations.anthropic")

_PATCHED_MARKER = "__aegrail_patched__"


def install() -> bool:
    """Install the Anthropic patches. Returns True if the SDK was
    found and patched, False if it's not installed. Idempotent.
    """
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False

    patched_any = False
    try:
        from anthropic.resources import messages as _messages

        if _patch_sync(_messages.Messages, "create"):
            patched_any = True
        if _patch_async(_messages.AsyncMessages, "create"):
            patched_any = True
    except (ImportError, AttributeError):
        logger.debug("aegrail: anthropic.messages not patchable; skipping")

    # Newer SDK versions expose a separate beta.messages namespace.
    # Patch it the same way if present so beta callers don't bypass
    # the runtime governance layer.
    try:
        from anthropic.resources.beta import messages as _beta_messages

        if _patch_sync(_beta_messages.Messages, "create"):
            patched_any = True
        if _patch_async(_beta_messages.AsyncMessages, "create"):
            patched_any = True
    except (ImportError, AttributeError):
        logger.debug("aegrail: anthropic.beta.messages not patchable; skipping")

    return patched_any


def _patch_sync(cls: type, method_name: str) -> bool:
    original = getattr(cls, method_name, None)
    if original is None or getattr(original, _PATCHED_MARKER, False):
        return False

    def wrapper(self, *args: Any, **kwargs: Any) -> Any:
        session = current_session.get()
        if session is None:
            return original(self, *args, **kwargs)
        if kwargs.get("stream"):
            return original(self, *args, **kwargs)
        session.check_budget()
        result = original(self, *args, **kwargs)
        _record_from_response(session, result)
        return result

    wrapper.__name__ = original.__name__
    wrapper.__qualname__ = original.__qualname__
    wrapper.__doc__ = original.__doc__
    setattr(wrapper, _PATCHED_MARKER, True)
    setattr(cls, method_name, wrapper)
    return True


def _patch_async(cls: type, method_name: str) -> bool:
    original = getattr(cls, method_name, None)
    if original is None or getattr(original, _PATCHED_MARKER, False):
        return False

    async def wrapper(self, *args: Any, **kwargs: Any) -> Any:
        session = current_session.get()
        if session is None:
            return await original(self, *args, **kwargs)
        if kwargs.get("stream"):
            return await original(self, *args, **kwargs)
        await _maybe_await(session.check_budget())
        result = await original(self, *args, **kwargs)
        await _record_from_response_async(session, result)
        return result

    wrapper.__name__ = original.__name__
    wrapper.__qualname__ = original.__qualname__
    wrapper.__doc__ = original.__doc__
    setattr(wrapper, _PATCHED_MARKER, True)
    setattr(cls, method_name, wrapper)
    return True


def _extract_usage(response: Any) -> dict[str, Any]:
    """Pull model + tokens out of an Anthropic response. The shape:
    {
      model: "claude-3-5-sonnet-20240620",
      usage: {
        input_tokens: int,
        output_tokens: int,
        cache_creation_input_tokens: int (optional),
        cache_read_input_tokens: int (optional),
      },
      ...
    }
    """
    model = _safe_attr(response, "model", default="unknown")
    usage = _safe_attr(response, "usage", default=None)
    tokens_in = 0
    tokens_out = 0
    cache_read = 0
    cache_write = 0
    if usage is not None:
        tokens_in = int(_safe_attr(usage, "input_tokens", default=0) or 0)
        tokens_out = int(_safe_attr(usage, "output_tokens", default=0) or 0)
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


def _record_from_response(session: Any, response: Any) -> None:
    session.record_llm(**_extract_usage(response))


async def _record_from_response_async(session: Any, response: Any) -> None:
    await _maybe_await(session.record_llm(**_extract_usage(response)))


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


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
