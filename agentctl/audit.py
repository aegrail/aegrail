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
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

EventType = Literal[
    "session_start",
    "session_end",
    "llm_call",
    "tool_call",
    "budget_exceeded",
    "error",
]


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class AuditEvent(BaseModel):
    """A single immutable record of something the agent did or had done to it.

    Fields are flat for log-ingestion friendliness. The `payload` dict
    carries event-specific detail.
    """

    ts: str = Field(default_factory=_utc_now_iso)
    session_id: str
    agent_identity: str
    invoking_user: str | None = None
    principal: str
    event: EventType
    payload: dict[str, Any] = Field(default_factory=dict)
    budget: dict[str, Any] = Field(default_factory=dict)

    def to_json_line(self) -> str:
        return self.model_dump_json()


class _SinkBase:
    """Base class for sinks.

    Concrete sinks override `_write` and (optionally) `close`. The
    `emit` path is wrapped so that any internal error is logged to
    stderr and swallowed — a broken sink must never break the agent.
    """

    def emit(self, event: AuditEvent) -> None:
        try:
            self._write(event)
        except Exception as exc:  # pragma: no cover - defensive
            print(f"[agentctl] audit sink {type(self).__name__} failed: {exc}", file=sys.stderr)

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


class FileAuditSink(_SinkBase):
    """Append-only JSONL file sink. Line-buffered, thread-safe."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._fh = self.path.open("a", encoding="utf-8")

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
        self._lock = threading.Lock()

    def _write(self, event: AuditEvent) -> None:
        line = event.to_json_line()
        with self._lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()


class MemoryAuditSink(_SinkBase):
    """In-memory sink for tests and ephemeral inspection."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[AuditEvent] = []

    def _write(self, event: AuditEvent) -> None:
        with self._lock:
            self.events.append(event)


# Public alias used in the README and examples. `AuditSink.file(...)`
# reads more naturally than `_SinkBase.file(...)`.
AuditSink = _SinkBase
