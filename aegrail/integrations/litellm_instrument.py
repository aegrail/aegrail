"""litellm auto-instrumentation.

litellm normalizes every LLM provider (OpenAI, Anthropic, Bedrock,
Cohere, Mistral, ollama, vLLM, Groq, Vertex, Together, Replicate,
HuggingFace, and ~100 others) to the OpenAI Chat Completions
response shape. Patching its two module-level entry points covers
the long tail of providers that aren't directly instrumented by
the openai/anthropic adapters.

Patches:
    litellm.completion        (sync)
    litellm.acompletion       (async)

Unlike the openai/anthropic SDKs, litellm exposes these as module-
level functions rather than instance methods, so the patch
replaces the names on the `litellm` module itself.

Each wrapper:
  1. Looks up the active session from the ContextVar.
  2. If none, passes through.
  3. Calls `session.check_budget()` pre-call.
  4. Runs the original litellm function.
  5. Extracts model + token usage (OpenAI-shaped because litellm
     normalizes everything).
  6. Calls `session.record_llm(...)`.

Streaming (stream=True) passes through. Cost stays caller-provided
per CLAUDE.md ("No baked-in price table"). litellm has its own
cost model — if the caller has set `litellm.completion_cost = ...`
they can read it independently; aegrail records 0.0 cost.
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

from ..session import current_session

logger = logging.getLogger("aegrail.integrations.litellm")

_PATCHED_MARKER = "__aegrail_patched__"


def install() -> bool:
    """Install the litellm patches. Returns True if litellm was
    importable and patched. Idempotent."""
    try:
        import litellm
    except ImportError:
        return False

    patched_any = False
    if _patch_module_fn(litellm, "completion"):
        patched_any = True
    if _patch_module_fn(litellm, "acompletion", is_async=True):
        patched_any = True
    return patched_any


def _patch_module_fn(module: Any, fn_name: str, is_async: bool = False) -> bool:
    """Replace a module-level function with an aegrail-wrapped version."""
    original = getattr(module, fn_name, None)
    if original is None or getattr(original, _PATCHED_MARKER, False):
        return False

    if is_async:

        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            session = current_session.get()
            if session is None:
                return await original(*args, **kwargs)
            if kwargs.get("stream"):
                return await original(*args, **kwargs)
            await _maybe_await(session.check_budget())
            result = await original(*args, **kwargs)
            await _record_from_response_async(session, result)
            return result

    else:

        def wrapper(*args: Any, **kwargs: Any) -> Any:  # type: ignore[misc]
            session = current_session.get()
            if session is None:
                return original(*args, **kwargs)
            if kwargs.get("stream"):
                return original(*args, **kwargs)
            session.check_budget()
            result = original(*args, **kwargs)
            _record_from_response(session, result)
            return result

    wrapper.__name__ = original.__name__
    wrapper.__qualname__ = getattr(original, "__qualname__", original.__name__)
    wrapper.__doc__ = original.__doc__
    setattr(wrapper, _PATCHED_MARKER, True)
    setattr(module, fn_name, wrapper)
    return True


def _extract_usage(response: Any) -> dict[str, Any]:
    """Extract usage from litellm's OpenAI-shaped ModelResponse."""
    model = _safe_attr(response, "model", default="unknown")
    usage = _safe_attr(response, "usage", default=None)
    tokens_in = 0
    tokens_out = 0
    if usage is not None:
        tokens_in = int(_safe_attr(usage, "prompt_tokens", default=0) or 0)
        tokens_out = int(_safe_attr(usage, "completion_tokens", default=0) or 0)
    return {
        "model": model,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_usd": 0.0,
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
