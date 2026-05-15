"""Provider SDK auto-instrumentation for aegrail.

Goal: when `AEGRAIL_INTERCEPT=1` is set and an Agent is constructed,
the LLM provider SDKs already imported in the user's process get
monkey-patched so every model call carries identity, gets
budget-checked, and emits a `llm_call` audit event — without the
user changing a line of code.

Pattern is the same one Datadog / Sentry / OpenTelemetry use:
instrument-on-import. We patch each supported provider's create
method to read the active session from the ContextVar, run a
budget check before the call, run the call, then record token /
cost info on the way out.

Currently supported:
    openai          (sync + async clients, chat completions + responses)
    anthropic       (planned in 0.3.0.x)
    litellm         (planned in 0.3.0.x)

Tools still come from code via the Agent's tool registry. Only
LLM-call instrumentation is automatic.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("aegrail.integrations")


def install_all() -> list[str]:
    """Install every available provider integration. Returns the
    list of integration names that successfully installed.

    Idempotent: safe to call multiple times. Missing provider SDKs
    are silently skipped (no error if `openai` isn't installed).
    """
    installed: list[str] = []
    for name, install_fn in _registry().items():
        try:
            if install_fn():
                installed.append(name)
        except Exception as exc:
            # Per design principle 8: integration failures must never
            # break the caller. Log and continue.
            logger.warning("aegrail integration %s failed to install: %s", name, exc)
    return installed


def _registry() -> dict[str, callable]:
    from .openai_instrument import install as _install_openai

    return {
        "openai": _install_openai,
    }
