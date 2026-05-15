"""Budget definition and runtime state.

A Budget is a declarative ceiling. A BudgetState is the live
counter the session updates on every recorded event. The runtime
calls `check()` before and after recording work; if any limit is
exceeded, BudgetExceeded is raised — deterministically, in code,
not via the LLM.
"""

from __future__ import annotations

import time
from typing import Any

from pydantic import BaseModel, Field, model_validator

from .exceptions import BudgetExceeded


class Budget(BaseModel):
    """Hard ceilings for a session.

    Any field left as None means "no limit on this axis". At least
    one limit must be set — an unbounded session defeats the point.
    """

    usd: float | None = Field(default=None, ge=0)
    tokens: int | None = Field(default=None, ge=0)
    wall_seconds: float | None = Field(default=None, ge=0)
    max_recursion: int | None = Field(default=None, ge=0)
    max_tool_calls: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _at_least_one_limit(self) -> Budget:
        if all(
            v is None
            for v in (
                self.usd,
                self.tokens,
                self.wall_seconds,
                self.max_recursion,
                self.max_tool_calls,
            )
        ):
            raise ValueError("Budget must set at least one limit")
        return self

    @classmethod
    def from_env(cls) -> Budget:
        """Construct a Budget from `AEGRAIL_BUDGET_*` environment variables.

        Reads the following env vars; each is optional but at least one
        must be set (Budget requires at least one limit):

          AEGRAIL_BUDGET_USD              float  -> Budget.usd
          AEGRAIL_BUDGET_TOKENS           int    -> Budget.tokens
          AEGRAIL_BUDGET_WALL_SECONDS     float  -> Budget.wall_seconds
          AEGRAIL_BUDGET_MAX_RECURSION    int    -> Budget.max_recursion
          AEGRAIL_BUDGET_MAX_TOOL_CALLS   int    -> Budget.max_tool_calls

        Raises ValueError with a clear message if none are set — that's
        the same failure mode as `Budget()` with no args, just with a
        more specific error.
        """
        import os

        env_to_field: dict[str, tuple[str, type]] = {
            "AEGRAIL_BUDGET_USD": ("usd", float),
            "AEGRAIL_BUDGET_TOKENS": ("tokens", int),
            "AEGRAIL_BUDGET_WALL_SECONDS": ("wall_seconds", float),
            "AEGRAIL_BUDGET_MAX_RECURSION": ("max_recursion", int),
            "AEGRAIL_BUDGET_MAX_TOOL_CALLS": ("max_tool_calls", int),
        }
        kwargs: dict[str, Any] = {}
        for env_name, (field, cast) in env_to_field.items():
            raw = os.environ.get(env_name)
            if raw is None or raw == "":
                continue
            try:
                kwargs[field] = cast(raw)
            except ValueError as exc:
                raise ValueError(
                    f"{env_name}={raw!r} cannot be parsed as {cast.__name__}: {exc}"
                ) from exc
        if not kwargs:
            raise ValueError(
                "Budget.from_env requires at least one AEGRAIL_BUDGET_* "
                "environment variable to be set "
                "(USD, TOKENS, WALL_SECONDS, MAX_RECURSION, or MAX_TOOL_CALLS)"
            )
        return cls(**kwargs)


class BudgetState:
    """Mutable, per-session counter that tracks consumption against a Budget.

    Not thread-safe across sessions; each session owns its own state.
    Within a session, increments are expected from the main event loop.
    If you call aegrail from multiple threads inside one session, wrap
    `add_*` calls in your own lock.
    """

    def __init__(self, budget: Budget) -> None:
        self.budget = budget
        self.tokens_used: int = 0
        self.usd_used: float = 0.0
        self.tool_calls: int = 0
        self.recursion_depth: int = 0
        self.started_at: float = time.monotonic()

    @property
    def wall_elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def add_tokens(self, n: int) -> None:
        if n < 0:
            raise ValueError("tokens must be non-negative")
        self.tokens_used += n

    def add_usd(self, amount: float) -> None:
        if amount < 0:
            raise ValueError("cost must be non-negative")
        self.usd_used += amount

    def add_tool_call(self) -> None:
        self.tool_calls += 1

    def enter_recursion(self) -> None:
        self.recursion_depth += 1

    def exit_recursion(self) -> None:
        if self.recursion_depth > 0:
            self.recursion_depth -= 1

    def check(self) -> None:
        """Raise BudgetExceeded if any ceiling has been crossed.

        Called by the session before and after every recorded event,
        so violations surface deterministically at the nearest
        runtime boundary.
        """
        b = self.budget
        if b.usd is not None and self.usd_used > b.usd:
            raise BudgetExceeded(
                "usd",
                f"usd budget exceeded: {self.usd_used:.4f} > {b.usd:.4f}",
                state=self,
            )
        if b.tokens is not None and self.tokens_used > b.tokens:
            raise BudgetExceeded(
                "tokens",
                f"token budget exceeded: {self.tokens_used} > {b.tokens}",
                state=self,
            )
        if b.wall_seconds is not None and self.wall_elapsed > b.wall_seconds:
            raise BudgetExceeded(
                "wall_seconds",
                f"wall-clock budget exceeded: {self.wall_elapsed:.2f}s > {b.wall_seconds}s",
                state=self,
            )
        if b.max_recursion is not None and self.recursion_depth > b.max_recursion:
            raise BudgetExceeded(
                "recursion",
                f"recursion depth exceeded: {self.recursion_depth} > {b.max_recursion}",
                state=self,
            )
        if b.max_tool_calls is not None and self.tool_calls > b.max_tool_calls:
            raise BudgetExceeded(
                "tool_calls",
                f"tool-call budget exceeded: {self.tool_calls} > {b.max_tool_calls}",
                state=self,
            )

    def snapshot(self) -> dict:
        """Return a JSON-serializable snapshot for inclusion in audit events."""
        return {
            "tokens_used": self.tokens_used,
            "usd_used": round(self.usd_used, 6),
            "tool_calls": self.tool_calls,
            "recursion_depth": self.recursion_depth,
            "wall_elapsed": round(self.wall_elapsed, 3),
        }
