"""Agent and session identity.

Every agent has a stable identity declared at construction (e.g.
`support-bot/v1`). Every session derives a short-lived, unique
principal from that identity, suffixed with a session id. Audit
events are emitted against the session principal so that every
action is attributable to *both* the agent role and the run.
"""

from __future__ import annotations

import re
import secrets
import time

_AGENT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)?$")


def validate_agent_identity(identity: str) -> str:
    """Validate the human-supplied agent identity string.

    Accepts `name` or `name/version`. Lowercase, alphanumerics,
    dot, dash, underscore. Keeps identities greppable and URL-safe.
    """
    if not isinstance(identity, str) or not identity:
        raise ValueError("agent identity must be a non-empty string")
    if not _AGENT_ID_RE.match(identity):
        raise ValueError(
            f"invalid agent identity {identity!r}: expected 'name' or 'name/version' "
            "using [a-z0-9._-]"
        )
    return identity


def new_session_id() -> str:
    """Generate a short, sortable, unique session id.

    Format: `sess_<unix_ms>_<random>`. Sortable by creation time
    when grouped, and unique without coordination.
    """
    ms = int(time.time() * 1000)
    rand = secrets.token_hex(6)
    return f"sess_{ms:013d}_{rand}"


def session_principal(agent_identity: str, session_id: str) -> str:
    """Return the per-session principal string used in audit events.

    Format: `<agent_identity>@<session_id>`. This is what flows
    through logs as the actor — never the raw agent identity alone.
    """
    return f"{agent_identity}@{session_id}"
