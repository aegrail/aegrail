import http.server
import json
import socket
import socketserver
import threading
from pathlib import Path
from typing import ClassVar

from aegrail.audit import AuditEvent, AuditSink


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


class TestCallbackSink:
    def test_invokes_callback(self) -> None:
        captured: list[AuditEvent] = []
        sink = AuditSink.callback(captured.append)
        sink.emit(_make_event("session_start"))
        sink.emit(_make_event("session_end"))
        assert [e.event for e in captured] == ["session_start", "session_end"]

    def test_callback_exception_does_not_propagate(self, capsys) -> None:
        def explode(evt: AuditEvent) -> None:
            raise RuntimeError("callback boom")

        sink = AuditSink.callback(explode)
        sink.emit(_make_event())  # must not raise
        err = capsys.readouterr().err
        assert "callback boom" in err


class TestCompositeSink:
    def test_fans_out_to_all_sinks(self) -> None:
        a = AuditSink.memory()
        b = AuditSink.memory()
        sink = AuditSink.composite(a, b)
        sink.emit(_make_event())
        assert len(a.events) == 1
        assert len(b.events) == 1

    def test_one_broken_child_does_not_break_others(self, capsys) -> None:
        class Broken(AuditSink):
            def _write(self, event: AuditEvent) -> None:
                raise RuntimeError("child boom")

        good = AuditSink.memory()
        sink = AuditSink.composite(Broken(), good)
        sink.emit(_make_event())
        assert len(good.events) == 1
        assert "child boom" in capsys.readouterr().err

    def test_close_closes_all_children(self, tmp_path: Path) -> None:
        a = AuditSink.file(tmp_path / "a.jsonl")
        b = AuditSink.file(tmp_path / "b.jsonl")
        sink = AuditSink.composite(a, b)
        sink.emit(_make_event())
        sink.close()
        assert len((tmp_path / "a.jsonl").read_text().splitlines()) == 1
        assert len((tmp_path / "b.jsonl").read_text().splitlines()) == 1


def _find_closed_port() -> int:
    """Bind+close to claim a port the OS will refuse on connect."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _CapturingHandler(http.server.BaseHTTPRequestHandler):
    received_bodies: ClassVar[list[str]] = []
    received_headers: ClassVar[list[dict[str, str]]] = []

    def do_POST(self) -> None:
        n = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(n).decode("utf-8")
        type(self).received_bodies.append(body)
        type(self).received_headers.append({k: v for k, v in self.headers.items()})
        self.send_response(204)
        self.end_headers()

    def log_message(self, fmt, *args) -> None:
        return


class TestWebhookSink:
    def _serve(self):
        # Fresh handler subclass per server so received_bodies isolates per test.
        class Handler(_CapturingHandler):
            received_bodies: ClassVar[list[str]] = []
            received_headers: ClassVar[list[dict[str, str]]] = []

        srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        return srv, Handler

    def test_posts_event_json(self) -> None:
        srv, handler = self._serve()
        try:
            port = srv.server_address[1]
            sink = AuditSink.webhook(f"http://127.0.0.1:{port}/")
            sink.emit(_make_event("session_start"))
        finally:
            srv.shutdown()
        assert len(handler.received_bodies) == 1
        parsed = json.loads(handler.received_bodies[0])
        assert parsed["event"] == "session_start"
        assert parsed["session_id"] == "sess_test"

    def test_custom_headers_are_sent(self) -> None:
        srv, handler = self._serve()
        try:
            port = srv.server_address[1]
            sink = AuditSink.webhook(
                f"http://127.0.0.1:{port}/",
                headers={"Authorization": "Bearer xyz"},
            )
            sink.emit(_make_event())
        finally:
            srv.shutdown()
        assert handler.received_headers[0].get("Authorization") == "Bearer xyz"
        assert handler.received_headers[0].get("Content-Type") == "application/json"

    def test_network_failure_does_not_propagate(self, capsys) -> None:
        port = _find_closed_port()
        sink = AuditSink.webhook(f"http://127.0.0.1:{port}/", timeout=0.5)
        sink.emit(_make_event())  # must not raise
        err = capsys.readouterr().err
        assert "audit sink" in err.lower()
