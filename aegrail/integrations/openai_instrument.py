"""OpenAI Python SDK auto-instrumentation.

Patches the `create` methods on:
    openai.resources.chat.completions.Completions       (sync chat)
    openai.resources.chat.completions.AsyncCompletions  (async chat)
    openai.resources.responses.Responses                (sync Responses API)
    openai.resources.responses.AsyncResponses           (async Responses API)

Each patched method:
  1. Looks up the active `aegrail.Session` from the ContextVar.
  2. If no session is active, passes through untouched.
  3. Else calls `session.check_budget()` to fail-fast on an already-
     exceeded ceiling.
  4. Calls the original method.
  5. Extracts model + token usage from the response.
  6. Calls `session.record_llm(...)`, which writes the `llm_call`
     audit event and increments the budget state. May raise
     `BudgetExceeded` post-hoc if the call pushed over a ceiling.

Streaming responses (`stream=True`) are not auto-instrumented in
this release. The OpenAI streaming API exposes usage only via the
`stream_options={"include_usage": True}` flag in the last chunk,
which requires response-shape handling beyond what fits in a
transparent wrapper. Streams pass through; the caller must record
explicitly via `session.record_llm(...)` after consuming the stream.

Cost calculation is intentionally left to the caller (see
`CLAUDE.md` design principle: "No baked-in price table"). Auto-
recorded cost defaults to 0.0; the user can override via an Agent-
level cost estimator hook in a later release.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from ..session import current_session

logger = logging.getLogger("aegrail.integrations.openai")

# Marker attribute set on a patched method so the second install()
# call is a no-op.
_PATCHED_MARKER = "__aegrail_patched__"


def install() -> bool:
    """Install the OpenAI patches. Returns True if the openai SDK was
    found and patched, False if the SDK is not installed (graceful
    no-op). Idempotent — safe to call repeatedly.
    """
    try:
        import openai  # noqa: F401  # only need to verify importability
    except ImportError:
        return False

    patched_any = False
    try:
        from openai.resources.chat import completions as _chat

        if _patch_sync(_chat.Completions, "create"):
            patched_any = True
        if _patch_async(_chat.AsyncCompletions, "create"):
            patched_any = True
    except (ImportError, AttributeError):
        logger.debug("aegrail: openai.chat.completions not patchable; skipping")

    try:
        from openai.resources import responses as _responses

        if _patch_sync(_responses.Responses, "create"):
            patched_any = True
        if _patch_async(_responses.AsyncResponses, "create"):
            patched_any = True
    except (ImportError, AttributeError):
        # The Responses API is not present in older openai versions.
        # That's fine — chat.completions covers the bulk of traffic.
        logger.debug("aegrail: openai.responses not patchable; skipping")

    return patched_any


def _patch_sync(cls: type, method_name: str) -> bool:
    """Wrap a sync `create` method on `cls` with aegrail instrumentation.
    Returns True on a fresh patch, False if already patched.
    """
    original = getattr(cls, method_name, None)
    if original is None or getattr(original, _PATCHED_MARKER, False):
        return False

    def wrapper(self, *args: Any, **kwargs: Any) -> Any:
        session = current_session.get()
        if session is None:
            return original(self, *args, **kwargs)
        if kwargs.get("stream"):
            # Streams: pass through; usage isn't available until the
            # last chunk and we don't intercept the stream object.
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
    """Wrap an async `create` method on `cls` with aegrail instrumentation.
    Returns True on a fresh patch, False if already patched.
    """
    original = getattr(cls, method_name, None)
    if original is None or getattr(original, _PATCHED_MARKER, False):
        return False

    async def wrapper(self, *args: Any, **kwargs: Any) -> Any:
        session = current_session.get()
        if session is None:
            return await original(self, *args, **kwargs)
        if kwargs.get("stream"):
            return await original(self, *args, **kwargs)
        # AsyncSession.check_budget and .record_llm are coroutines;
        # Session's are sync. Detect and await as needed.
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
    """Pull model + tokens out of an openai response shape. Tolerant
    of both chat.completions and responses-API shapes.
    """
    model = _safe_attr(response, "model", default="unknown")
    usage = _safe_attr(response, "usage", default=None)
    tokens_in = 0
    tokens_out = 0
    cache_read = 0
    if usage is not None:
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
        details = _safe_attr(usage, "prompt_tokens_details", default=None) or _safe_attr(
            usage, "input_tokens_details", default=None
        )
        if details is not None:
            cache_read = int(_safe_attr(details, "cached_tokens", default=0) or 0)
    return {
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": 0.0,
        "cache_read_tokens": cache_read,
        "cache_write_tokens": 0,
    }


def _record_from_response(session: Any, response: Any) -> None:
    """Sync path: extract usage and call session.record_llm. Any
    BudgetExceeded / SessionTerminated propagates by design — those
    are the deterministic enforcement signals the wrapped call must
    not swallow.
    """
    session.record_llm(**_extract_usage(response))


async def _record_from_response_async(session: Any, response: Any) -> None:
    """Async path. AsyncSession.record_llm is a coroutine."""
    await _maybe_await(session.record_llm(**_extract_usage(response)))


async def _maybe_await(value: Any) -> Any:
    """Await a value if it is awaitable; pass through otherwise.

    The patched method may be wrapping either Session.check_budget
    (sync) or AsyncSession.check_budget (coroutine). Same for
    record_llm. This keeps one wrapper that handles both.
    """
    if inspect.isawaitable(value):
        return await value
    return value


def _safe_attr(obj: Any, name: str, default: Any = None) -> Any:
    """Attribute access that tolerates both pydantic models and dicts."""
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
