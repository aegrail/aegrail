"""In-process interceptors for v0.2.4.

Two surfaces:

1. `intercept_outbound()` — monkey-patches the HTTP clients commonly
   used by agents (`urllib.request`, `requests`, `httpx`) so any
   outbound HTTP call made from inside an active aegrail Session is
   checked against the active agent's `egress_allowlist`. Denied
   destinations raise `EgressNotPermitted` and emit `egress_denied`
   audit events.

2. `install_audit_hook(agent)` — registers a `sys.addaudithook`
   callback. CPython emits audit events for `subprocess.Popen`,
   `os.system`, `os.exec`, `socket.connect`, `open`,
   `urllib.Request`, and many more (PEP 578). The hook observes
   these and emits `audit_hook_event` records into the agent's
   audit sink. It does NOT block — that's the sidecar's job.

Both surfaces fall through outside an active Session: they never
interfere with non-agent code in the same Python process.

Limitations (documented for honesty, not papered over):
  - `aiohttp` is not patched in v0.2.4. Use `requests` / `httpx` or
    add an explicit egress check.
  - A determined bypass via `ctypes.CDLL` or raw sockets evades
    these interceptors. That's what the sidecar in `aegrail-engine`
    is for.
  - The PEP 578 audit hook can't be removed once installed (CPython
    limitation). Re-running `install_audit_hook` is idempotent —
    we track the registered callbacks ourselves and short-circuit
    duplicates.
"""

from __future__ import annotations

import fnmatch
import sys
import threading
import urllib.parse
from typing import Any
from urllib.parse import urlparse

from .exceptions import EgressNotPermitted

# --- module-level state for idempotent install / uninstall -----------

_outbound_lock = threading.Lock()
_outbound_installed = False
_original_urllib_open = None
_original_requests_send = None
_original_httpx_send = None
_original_httpx_async_send = None

_audit_hook_installed = False
_audit_hook_agents: list[Any] = []


# --- helpers ---------------------------------------------------------


def _match_allowlist(host: str, allowlist: list[str] | None) -> bool:
    """Return True if `host` matches any pattern in `allowlist`.

    Patterns are fnmatch-style. `None` allowlist means "no enforcement"
    (open egress). `[]` empty allowlist means "deny everything".
    """
    if allowlist is None:
        return True
    return any(fnmatch.fnmatchcase(host, pat) for pat in allowlist)


def _check_egress(url: str) -> None:
    """If a Session is active and its agent has a non-None allowlist,
    enforce the check; otherwise pass through.

    Imported lazily to avoid circular imports (interceptors must not
    pull in session at module import time).
    """
    from .session import current_session

    session = current_session.get()
    if session is None:
        return  # No active session — don't interfere with non-agent code

    # The session is created by Agent.session(...) which captures the
    # agent's identity but not the agent object itself. We reach back
    # to the agent's egress_allowlist via the audit sink — but the
    # cleaner path is to store the allowlist on the session at
    # construction. For v0.2.4 we look it up via the session's
    # internal reference.
    allowlist = getattr(session, "_egress_allowlist", None)
    if allowlist is None:
        return  # Agent didn't declare an allowlist — open egress

    parsed = urlparse(url)
    host = parsed.hostname or ""
    if not _match_allowlist(host, allowlist):
        # Emit egress_denied through the session, then raise
        session._emit(
            "egress_denied",
            {
                "host": host,
                "url": url,
                "reason": "not_in_allowlist",
            },
        )
        raise EgressNotPermitted(host, url, "not_in_allowlist")


# --- intercept_outbound --------------------------------------------


def intercept_outbound() -> None:
    """Monkey-patch HTTP clients so outbound calls flow through aegrail.

    Currently patches:
      - `urllib.request.OpenerDirector.open` (covers `urlopen`)
      - `requests.adapters.HTTPAdapter.send` (if `requests` is installed)
      - `httpx.Client.send` and `httpx.AsyncClient.send` (if `httpx`
        is installed)

    Idempotent: calling twice is a no-op. Use `uninstall_outbound()`
    to restore the original behavior (for tests).
    """
    global _outbound_installed
    global _original_urllib_open, _original_requests_send
    global _original_httpx_send, _original_httpx_async_send

    with _outbound_lock:
        if _outbound_installed:
            return

        # urllib.request — always present (stdlib)
        import urllib.request as _ur

        _original_urllib_open = _ur.OpenerDirector.open

        def _patched_open(self, fullurl, *args, **kwargs):
            url = (
                fullurl.full_url
                if hasattr(fullurl, "full_url")
                else (fullurl.get_full_url() if hasattr(fullurl, "get_full_url") else fullurl)
            )
            _check_egress(url)
            return _original_urllib_open(self, fullurl, *args, **kwargs)

        _ur.OpenerDirector.open = _patched_open

        # requests — optional
        try:
            import requests.adapters as _ra

            _original_requests_send = _ra.HTTPAdapter.send

            def _patched_requests_send(self, request, *args, **kwargs):
                _check_egress(request.url)
                return _original_requests_send(self, request, *args, **kwargs)

            _ra.HTTPAdapter.send = _patched_requests_send
        except ImportError:
            pass

        # httpx — optional
        try:
            import httpx as _hx

            _original_httpx_send = _hx.Client.send

            def _patched_httpx_send(self, request, *args, **kwargs):
                _check_egress(str(request.url))
                return _original_httpx_send(self, request, *args, **kwargs)

            _hx.Client.send = _patched_httpx_send

            _original_httpx_async_send = _hx.AsyncClient.send

            async def _patched_httpx_async_send(self, request, *args, **kwargs):
                _check_egress(str(request.url))
                return await _original_httpx_async_send(self, request, *args, **kwargs)

            _hx.AsyncClient.send = _patched_httpx_async_send
        except ImportError:
            pass

        _outbound_installed = True


def uninstall_outbound() -> None:
    """Restore HTTP clients to their pre-patch state.

    Mostly useful for tests. Production code does not need to call
    this — the patches are designed to live for the process lifetime.
    """
    global _outbound_installed
    global _original_urllib_open, _original_requests_send
    global _original_httpx_send, _original_httpx_async_send

    with _outbound_lock:
        if not _outbound_installed:
            return

        if _original_urllib_open is not None:
            import urllib.request as _ur

            _ur.OpenerDirector.open = _original_urllib_open
            _original_urllib_open = None

        if _original_requests_send is not None:
            try:
                import requests.adapters as _ra

                _ra.HTTPAdapter.send = _original_requests_send
            except ImportError:
                pass
            _original_requests_send = None

        if _original_httpx_send is not None:
            try:
                import httpx as _hx

                _hx.Client.send = _original_httpx_send
                if _original_httpx_async_send is not None:
                    _hx.AsyncClient.send = _original_httpx_async_send
            except ImportError:
                pass
            _original_httpx_send = None
            _original_httpx_async_send = None

        _outbound_installed = False


# --- install_audit_hook -------------------------------------------


def install_audit_hook(agent: Any) -> None:
    """Register a PEP 578 audit hook that emits `audit_hook_event`
    records into the agent's audit sink for built-in CPython audit
    events fired during an active session.

    Idempotent: calling twice for the same agent is a no-op. The
    underlying `sys.addaudithook` cannot be removed (CPython
    limitation), so we keep a list of agents-of-interest and the
    single registered hook dispatches to all of them.

    Only events fired during an active aegrail Session are recorded.
    Outside any session, the hook silently passes.
    """
    global _audit_hook_installed

    if agent in _audit_hook_agents:
        return  # idempotent per-agent

    _audit_hook_agents.append(agent)

    if _audit_hook_installed:
        return

    def _hook(event: str, args: tuple) -> None:
        # We only care about a subset of CPython's audit events to keep
        # the log readable. Filter to the security-relevant ones.
        if not _is_interesting_audit_event(event):
            return
        from .session import current_session

        session = current_session.get()
        if session is None:
            return
        # Render args defensively — some audit-event args are non-
        # serialisable (e.g. file descriptors). Best-effort:
        rendered_args = []
        for a in args:
            try:
                # Truncate long strings; coerce non-str to repr
                if isinstance(a, str):
                    rendered_args.append(a[:200])
                elif isinstance(a, (int, float, bool, type(None))):
                    rendered_args.append(a)
                else:
                    rendered_args.append(repr(a)[:200])
            except Exception:
                rendered_args.append("<unrenderable>")
        # Audit hooks must never raise into CPython's internals —
        # that would crash the agent. Swallow any error here.
        import contextlib

        with contextlib.suppress(Exception):
            session._emit(
                "audit_hook_event",
                {
                    "audit_name": event,
                    "args": rendered_args,
                },
            )

    sys.addaudithook(_hook)
    _audit_hook_installed = True


_INTERESTING_AUDIT_PREFIXES = (
    "subprocess.",
    "os.system",
    "os.exec",
    "os.spawn",
    "os.posix_spawn",
    "socket.connect",
    "socket.bind",
    "urllib.Request",
    "open",
)


def _is_interesting_audit_event(event: str) -> bool:
    return event.startswith(_INTERESTING_AUDIT_PREFIXES)


# Suppress unused-import lint complaint — urllib.parse is used via fnmatch above
_ = urllib.parse
