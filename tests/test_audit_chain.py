"""Tests for the tamper-evident audit chain introduced in v0.2.3.

Every AuditEvent carries:
  - prev_hash: SHA-256 of the previous event's serialized body, or None
    for the chain genesis
  - event_hash: SHA-256 of this event's serialized body (excluding
    event_hash itself) plus its prev_hash

A consumer (auditor) can verify the chain end-to-end by recomputing each
event's hash and confirming each prev_hash points at the previous event's
event_hash. Any tampering with a historical event invalidates the chain
from that point forward.
"""

from __future__ import annotations

import json
from pathlib import Path

from aegrail import (
    Agent,
    AuditEvent,
    AuditSink,
    Budget,
    Tool,
)
from aegrail.audit import compute_event_hash, verify_chain


def _agent_with_memory_sink(**budget_kw):
    sink = AuditSink.memory()
    a = Agent(identity="bot/v1", budget=Budget(**budget_kw), audit=sink)
    return a, sink


class TestEventHashing:
    def test_compute_event_hash_is_deterministic(self) -> None:
        evt1 = AuditEvent(
            session_id="sess_1",
            agent_identity="bot/v1",
            principal="bot/v1@sess_1",
            event="session_start",
            payload={"task": "t"},
            budget={"tokens_used": 0},
            ts="2026-05-14T00:00:00.000Z",
        )
        evt2 = AuditEvent(
            session_id="sess_1",
            agent_identity="bot/v1",
            principal="bot/v1@sess_1",
            event="session_start",
            payload={"task": "t"},
            budget={"tokens_used": 0},
            ts="2026-05-14T00:00:00.000Z",
        )
        h1 = compute_event_hash(evt1, prev_hash=None)
        h2 = compute_event_hash(evt2, prev_hash=None)
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex digest

    def test_compute_event_hash_changes_with_payload(self) -> None:
        base = dict(
            session_id="sess_1",
            agent_identity="bot/v1",
            principal="bot/v1@sess_1",
            event="session_start",
            payload={"task": "t"},
            budget={"tokens_used": 0},
            ts="2026-05-14T00:00:00.000Z",
        )
        evt = AuditEvent(**base)
        h1 = compute_event_hash(evt, prev_hash=None)
        evt_tampered = AuditEvent(**{**base, "payload": {"task": "t2"}})
        h2 = compute_event_hash(evt_tampered, prev_hash=None)
        assert h1 != h2

    def test_compute_event_hash_changes_with_prev_hash(self) -> None:
        evt = AuditEvent(
            session_id="sess_1",
            agent_identity="bot/v1",
            principal="bot/v1@sess_1",
            event="session_start",
            payload={"task": "t"},
            budget={"tokens_used": 0},
            ts="2026-05-14T00:00:00.000Z",
        )
        h1 = compute_event_hash(evt, prev_hash=None)
        h2 = compute_event_hash(evt, prev_hash="a" * 64)
        assert h1 != h2


class TestMemorySinkChain:
    def test_chain_populated_on_emit(self) -> None:
        agent, sink = _agent_with_memory_sink(usd=1.0)
        with agent.session(user_id="alice"):
            pass
        assert len(sink.events) >= 2
        # First event: prev_hash is None (genesis)
        assert sink.events[0].prev_hash is None
        assert sink.events[0].event_hash is not None
        assert len(sink.events[0].event_hash) == 64
        # Subsequent events chain
        for i in range(1, len(sink.events)):
            assert sink.events[i].prev_hash == sink.events[i - 1].event_hash

    def test_verify_chain_returns_valid_for_untampered(self) -> None:
        agent, sink = _agent_with_memory_sink(usd=1.0)
        with agent.session(user_id="alice") as s:
            s.record_llm(model="m", tokens_in=1, tokens_out=1, cost_usd=0.0)
        valid, bad_index = verify_chain(sink.events)
        assert valid is True
        assert bad_index == -1

    def test_verify_chain_detects_payload_tampering(self) -> None:
        agent, sink = _agent_with_memory_sink(usd=1.0)
        with agent.session(user_id="alice") as s:
            s.record_llm(model="m", tokens_in=1, tokens_out=1, cost_usd=0.0)

        # Tamper with the LLM event's payload — without recomputing its hash,
        # the chain becomes invalid from that index forward.
        llm_idx = next(i for i, e in enumerate(sink.events) if e.event == "llm_call")
        # Replace the events list in-place with a tampered copy
        tampered_payload = {**sink.events[llm_idx].payload, "model": "DIFFERENT"}
        sink.events[llm_idx] = sink.events[llm_idx].model_copy(update={"payload": tampered_payload})

        valid, bad_index = verify_chain(sink.events)
        assert valid is False
        assert bad_index == llm_idx


class TestFileSinkChain:
    def test_chain_persisted_to_jsonl(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        sink = AuditSink.file(path)
        agent = Agent(
            identity="bot/v1",
            budget=Budget(usd=1.0),
            audit=sink,
            tools={"ping": Tool(name="ping", fn=lambda: "pong")},
        )
        with agent.session(user_id="alice") as s:
            s.call_tool("ping")
        sink.close()

        lines = path.read_text(encoding="utf-8").strip().splitlines()
        parsed = [json.loads(line) for line in lines]
        assert parsed[0]["prev_hash"] is None
        for i in range(1, len(parsed)):
            assert parsed[i]["prev_hash"] == parsed[i - 1]["event_hash"]

    def test_chain_continues_across_process_open(self, tmp_path: Path) -> None:
        """Closing and reopening the file sink should pick up where the
        existing chain left off — not start a new genesis."""
        path = tmp_path / "audit.jsonl"

        agent = Agent(
            identity="bot/v1",
            budget=Budget(usd=1.0),
            audit=AuditSink.file(path),
        )
        with agent.session(user_id="alice"):
            pass
        agent.close()

        # New sink instance on the same file
        agent2 = Agent(
            identity="bot/v1",
            budget=Budget(usd=1.0),
            audit=AuditSink.file(path),
        )
        with agent2.session(user_id="alice"):
            pass
        agent2.close()

        lines = path.read_text(encoding="utf-8").strip().splitlines()
        parsed = [json.loads(line) for line in lines]
        # 4 events total (2 sessions x start+end)
        assert len(parsed) == 4
        # First event of session 2 must chain to last event of session 1
        assert parsed[2]["prev_hash"] == parsed[1]["event_hash"]
        # Whole chain must verify
        events = [AuditEvent(**p) for p in parsed]
        valid, bad_index = verify_chain(events)
        assert valid is True, f"chain broken at index {bad_index}"

    def test_verify_chain_detects_file_tampering(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        agent = Agent(
            identity="bot/v1",
            budget=Budget(usd=1.0),
            audit=AuditSink.file(path),
        )
        with agent.session(user_id="alice") as s:
            s.record_llm(model="m", tokens_in=1, tokens_out=1, cost_usd=0.0)
        agent.close()

        # Tamper: edit the LLM event's model field but leave the hashes
        lines = path.read_text(encoding="utf-8").strip().splitlines()
        parsed = [json.loads(line) for line in lines]
        llm_idx = next(i for i, p in enumerate(parsed) if p["event"] == "llm_call")
        parsed[llm_idx]["payload"]["model"] = "DIFFERENT"

        events = [AuditEvent(**p) for p in parsed]
        valid, bad_index = verify_chain(events)
        assert valid is False
        assert bad_index == llm_idx


class TestEmptyChain:
    def test_verify_empty_chain_is_valid(self) -> None:
        valid, bad_index = verify_chain([])
        assert valid is True
        assert bad_index == -1

    def test_verify_single_event_chain(self) -> None:
        evt = AuditEvent(
            session_id="sess_1",
            agent_identity="bot/v1",
            principal="bot/v1@sess_1",
            event="session_start",
            payload={"task": "t"},
            budget={"tokens_used": 0},
            ts="2026-05-14T00:00:00.000Z",
        )
        evt = evt.model_copy(update={"event_hash": compute_event_hash(evt, prev_hash=None)})
        valid, bad_index = verify_chain([evt])
        assert valid is True
        assert bad_index == -1
