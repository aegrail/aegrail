import json
from pathlib import Path

from agentctl.audit import AuditEvent, AuditSink


def _make_event(event: str = "session_start") -> AuditEvent:
    return AuditEvent(
        session_id="sess_test",
        agent_identity="agent/v1",
        invoking_user="alice",
        principal="agent/v1@sess_test",
        event=event,  # type: ignore[arg-type]
        payload={"k": "v"},
        budget={"tokens_used": 0},
    )


class TestAuditEvent:
    def test_serialises_to_jsonl(self) -> None:
        evt = _make_event()
        line = evt.to_json_line()
        parsed = json.loads(line)
        assert parsed["session_id"] == "sess_test"
        assert parsed["event"] == "session_start"
        assert "ts" in parsed
        assert parsed["ts"].endswith("Z")


class TestMemorySink:
    def test_collects_events(self) -> None:
        sink = AuditSink.memory()
        sink.emit(_make_event("session_start"))
        sink.emit(_make_event("session_end"))
        assert len(sink.events) == 2
        assert [e.event for e in sink.events] == ["session_start", "session_end"]


class TestFileSink:
    def test_appends_jsonl(self, tmp_path: Path) -> None:
        path = tmp_path / "audit.jsonl"
        sink = AuditSink.file(path)
        sink.emit(_make_event("session_start"))
        sink.emit(_make_event("session_end"))
        sink.close()

        lines = path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 2
        parsed = [json.loads(line) for line in lines]
        assert [p["event"] for p in parsed] == ["session_start", "session_end"]

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        path = tmp_path / "nested" / "deeper" / "audit.jsonl"
        sink = AuditSink.file(path)
        sink.emit(_make_event())
        sink.close()
        assert path.exists()


class TestSinkSafety:
    def test_sink_errors_dont_propagate(self, capsys) -> None:
        class BrokenSink(AuditSink):
            def _write(self, event: AuditEvent) -> None:
                raise RuntimeError("disk on fire")

        sink = BrokenSink()
        # Must not raise — a broken sink can never break the agent.
        sink.emit(_make_event())
        err = capsys.readouterr().err
        assert "audit sink" in err
        assert "disk on fire" in err
