import re

import pytest

from aegrail.identity import (
    new_session_id,
    session_principal,
    validate_agent_identity,
)


class TestValidateAgentIdentity:
    @pytest.mark.parametrize(
        "ident",
        ["support-bot", "support-bot/v1", "a", "a/b", "x.y/z_1", "agent-2/v0.1"],
    )
    def test_accepts_valid(self, ident: str) -> None:
        assert validate_agent_identity(ident) == ident

    @pytest.mark.parametrize(
        "ident",
        ["", "Support-Bot", "support bot", "/v1", "name/", "name//v1", "name/v 1"],
    )
    def test_rejects_invalid(self, ident: str) -> None:
        with pytest.raises(ValueError):
            validate_agent_identity(ident)

    def test_rejects_non_string(self) -> None:
        with pytest.raises(ValueError):
            validate_agent_identity(None)  # type: ignore[arg-type]


class TestSessionId:
    def test_format(self) -> None:
        sid = new_session_id()
        assert re.match(r"^sess_\d{13}_[0-9a-f]{12}$", sid)

    def test_uniqueness(self) -> None:
        ids = {new_session_id() for _ in range(500)}
        assert len(ids) == 500


class TestSessionPrincipal:
    def test_concatenation(self) -> None:
        assert session_principal("support-bot/v1", "sess_123") == "support-bot/v1@sess_123"
