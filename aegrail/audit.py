"""Audit events and sinks.

An audit event is the unit of observation. It is forensic-grade:
structured, append-only, identity-linked, JSON-serializable, and
designed to answer the question "what did the agent do at 14:23,
and why?" — not just to help a developer debug.

Sinks are pluggable. v0 ships file, stdout, and memory. v0.2 will
add Postgres, S3, and a hosted backend. Sinks must never raise
into the caller; they fail loudly to stderr instead. A broken
sink should never break the agent.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import sys
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

EventType = Literal[
    # SDK-produced events (Python library)
    "session_start",
    "session_end",
    "llm_call",
    "tool_call",
    "tool_denied",
    "egress_denied",
    "audit_hook_event",
    "budget_exceeded",
    "error",
    # Engine-produced events (aegrail-engine Go sidecar). Listed
    # here so `AuditEvent.model_validate` accepts cross-language
    # chains end-to-end — a single verifier walks both halves.
    "engine_start",
    "engine_shutdown",
    "engine_heartbeat",
    "egress_allowed",
    "egress_error",
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class AuditEvent(BaseModel):
    """A single immutable record of something the agent did or had done to it.

    Fields are flat for log-ingestion friendliness. The `payload` dict
    carries event-specific detail.

    Tamper-evidence (v0.2.3): every emitted event carries `prev_hash`
    (the SHA-256 of the previous event in the chain, or `None` for the
    genesis event) and `event_hash` (the SHA-256 of this event's own
    serialized body, including its prev_hash). Any post-hoc edit to a
    historical event invalidates the chain from that point forward and
    is detected by `verify_chain()`. The sink computes and sets both
    fields on emit; callers don't.
    """

    ts: str = Field(default_factory=_utc_now_iso)
    session_id: str
    agent_identity: str
    invoking_user: str | None = None
    principal: str
    event: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str | None = None
    event_hash: str | None = None

    def to_json_line(self) -> str:
        return self.model_dump_json()


def compute_event_hash(event: AuditEvent, prev_hash: str | None) -> str:
    """Compute the SHA-256 hash for an AuditEvent in the chain.

    The hash covers every field of the event except `event_hash` itself
    (chicken-and-egg), and explicitly includes `prev_hash` so that any
    re-ordering of events also invalidates the chain.
    """
    body = event.model_dump(exclude={"event_hash"})
    body["prev_hash"] = prev_hash
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verify_chain(events: list[AuditEvent]) -> tuple[bool, int]:
    """Verify the tamper-evident chain over a list of audit events.

    Returns `(valid, first_bad_index)`. If the chain verifies end-to-end,
    `(True, -1)`. If any event's recomputed hash disagrees with what's
    stored on the event, returns `(False, i)` where `i` is the first
    failing index. Auditors and ops teams should run this on archived
    audit logs to confirm no tampering.
    """
    prev: str | None = None
    for i, evt in enumerate(events):
        expected = compute_event_hash(evt, prev_hash=prev)
        if evt.event_hash != expected:
            return False, i
        prev = evt.event_hash
    return True, -1


class _SinkBase:
    """Base class for sinks.

    Concrete sinks override `_write` and (optionally) `close`. The
    `emit` path is wrapped so that any internal error is logged to
    stderr and swallowed — a broken sink must never break the agent.

    Each sink instance maintains its own chain state in `_last_hash`,
    used to compute `prev_hash`/`event_hash` for each emitted event.
    If an event arrives with `event_hash` already set (e.g. it was
    chained by an outer composite), the sink advances its own chain
    state to match instead of recomputing.
    """

    _last_hash: str | None = None

    def __init__(self) -> None:
        self._chain_lock = threading.Lock()

    def emit(self, event: AuditEvent) -> None:
        try:
            with self._chain_lock:
                if event.event_hash is None:
                    prev = self._last_hash
                    h = compute_event_hash(event, prev_hash=prev)
                    event = event.model_copy(update={"prev_hash": prev, "event_hash": h})
                    self._last_hash = h
                else:
                    # Hash was set upstream (composite or test fixture); just
                    # advance our own chain state so subsequent events link.
                    self._last_hash = event.event_hash
            self._write(event)
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[aegrail] audit sink {type(self).__name__} failed: {exc}", file=sys.stderr)

    def _write(self, event: AuditEvent) -> None:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:
        return

    # Factory helpers — `AuditSink.file(path)` reads naturally at the call site.
    @staticmethod
    def file(path: str | os.PathLike[str]) -> FileAuditSink:
        return FileAuditSink(path)

    @staticmethod
    def stdout() -> StdoutAuditSink:
        return StdoutAuditSink()

    @staticmethod
    def memory() -> MemoryAuditSink:
        return MemoryAuditSink()

    @staticmethod
    def callback(fn: Callable[[AuditEvent], None]) -> CallbackAuditSink:
        return CallbackAuditSink(fn)

    @staticmethod
    def webhook(
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 3.0,
    ) -> WebhookAuditSink:
        return WebhookAuditSink(url, headers=headers, timeout=timeout)

    @staticmethod
    def composite(*sinks: _SinkBase) -> CompositeAuditSink:
        return CompositeAuditSink(list(sinks))


class FileAuditSink(_SinkBase):
    """Append-only JSONL file sink. Line-buffered, thread-safe.

    On open, scans an existing file (if any) for the last event's
    `event_hash` and uses it to continue the tamper-evident chain
    across process restarts — the chain spans process lifecycles
    naturally.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        super().__init__()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._last_hash = self._read_last_event_hash(self.path)
        self._fh = self.path.open("a", encoding="utf-8")

    @staticmethod
    def _read_last_event_hash(path: Path) -> str | None:
        if not path.exists() or path.stat().st_size == 0:
            return None
        last_hash: str | None = None
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    parsed = json.loads(line)
                    last_hash = parsed.get("event_hash")
                except json.JSONDecodeError:
                    continue
        return last_hash

    def _write(self, event: AuditEvent) -> None:
        line = event.to_json_line()
        with self._lock:
            self._fh.write(line + "\n")
            self._fh.flush()

    def close(self) -> None:
        with self._lock, contextlib.suppress(Exception):
            self._fh.close()


class StdoutAuditSink(_SinkBase):
    """JSONL to stdout. Useful in dev and containers with log shippers."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()

    def _write(self, event: AuditEvent) -> None:
        line = event.to_json_line()
        with self._lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()


class MemoryAuditSink(_SinkBase):
    """In-memory sink for tests and ephemeral inspection."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = threading.Lock()
        self.events: list[AuditEvent] = []

    def _write(self, event: AuditEvent) -> None:
        with self._lock:
            self.events.append(event)


class CallbackAuditSink(_SinkBase):
    """Invokes a user-supplied function on each event.

    Useful for routing events into existing alerting code, metrics
    pipelines, or custom logic. The callback runs synchronously on
    the agent's thread; exceptions inside it are caught by the base
    class and logged to stderr — they never propagate.
    """

    def __init__(self, callback: Callable[[AuditEvent], None]) -> None:
        super().__init__()
        self._callback = callback

    def _write(self, event: AuditEvent) -> None:
        self._callback(event)


class WebhookAuditSink(_SinkBase):
    """POST each event as a single JSON object to a URL.

    Synchronous and dependency-free (uses stdlib `urllib`). Each
    emit blocks the agent for at most `timeout` seconds. For
    high-volume agents, prefer `file`/`stdout` plus a separate log
    shipper, and use webhook only for low-volume alert events via a
    filtering callback or composite.

    Network failures, non-2xx responses, and timeouts are caught by
    the base class and logged to stderr — they never break the agent.
    """

    def __init__(
        self,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 3.0,
    ) -> None:
        super().__init__()
        self.url = url
        self.timeout = timeout
        self._headers = {"Content-Type": "application/json"}
        if headers:
            self._headers.update(headers)

    def _write(self, event: AuditEvent) -> None:
        import urllib.request

        body = event.to_json_line().encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=body,
            headers=self._headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            resp.read()


class CompositeAuditSink(_SinkBase):
    """Fan one event out to multiple sinks.

    Each child sink's `emit` is called independently, so a failure
    in one child cannot affect the others — the base-class wrapping
    isolates each child.
    """

    def __init__(self, sinks: list[_SinkBase]) -> None:
        super().__init__()
        self._sinks = list(sinks)

    def _write(self, event: AuditEvent) -> None:
        for sink in self._sinks:
            sink.emit(event)

    def close(self) -> None:
        for sink in self._sinks:
            with contextlib.suppress(Exception):
                sink.close()


# Public alias used in the README and examples. `AuditSink.file(...)`
# reads more naturally than `_SinkBase.file(...)`.
AuditSink = _SinkBase
