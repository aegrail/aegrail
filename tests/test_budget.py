import time

import pytest

from aegrail.budget import Budget, BudgetState
from aegrail.exceptions import BudgetExceeded


class TestBudgetValidation:
    def test_requires_at_least_one_limit(self) -> None:
        with pytest.raises(ValueError):
            Budget()

    def test_rejects_negative(self) -> None:
        with pytest.raises(ValueError):
            Budget(usd=-1)
        with pytest.raises(ValueError):
            Budget(tokens=-1)

    def test_zero_is_a_valid_limit(self) -> None:
        # A zero ceiling means "no work allowed" — odd, but legal.
        b = Budget(tokens=0)
        assert b.tokens == 0


class TestBudgetStateAccounting:
    def test_accumulates(self) -> None:
        s = BudgetState(Budget(usd=10.0, tokens=1000, max_tool_calls=5))
        s.add_tokens(100)
        s.add_tokens(50)
        s.add_usd(0.5)
        s.add_tool_call()
        snap = s.snapshot()
        assert snap["tokens_used"] == 150
        assert snap["usd_used"] == 0.5
        assert snap["tool_calls"] == 1

    def test_rejects_negative_increments(self) -> None:
        s = BudgetState(Budget(tokens=10))
        with pytest.raises(ValueError):
            s.add_tokens(-1)
        with pytest.raises(ValueError):
            s.add_usd(-0.01)


class TestBudgetCheck:
    def test_under_limit_passes(self) -> None:
        s = BudgetState(Budget(usd=1.0))
        s.add_usd(0.99)
        s.check()  # no raise

    def test_usd_overage_raises(self) -> None:
        s = BudgetState(Budget(usd=1.0))
        s.add_usd(1.01)
        with pytest.raises(BudgetExceeded) as excinfo:
            s.check()
        assert excinfo.value.reason == "usd"

    def test_tokens_overage_raises(self) -> None:
        s = BudgetState(Budget(tokens=100))
        s.add_tokens(101)
        with pytest.raises(BudgetExceeded) as excinfo:
            s.check()
        assert excinfo.value.reason == "tokens"

    def test_tool_call_overage_raises(self) -> None:
        s = BudgetState(Budget(max_tool_calls=2))
        s.add_tool_call()
        s.add_tool_call()
        s.add_tool_call()
        with pytest.raises(BudgetExceeded) as excinfo:
            s.check()
        assert excinfo.value.reason == "tool_calls"

    def test_recursion_overage_raises(self) -> None:
        s = BudgetState(Budget(max_recursion=2))
        s.enter_recursion()
        s.enter_recursion()
        s.enter_recursion()
        with pytest.raises(BudgetExceeded) as excinfo:
            s.check()
        assert excinfo.value.reason == "recursion"

    def test_recursion_exit_decrements(self) -> None:
        s = BudgetState(Budget(max_recursion=1))
        s.enter_recursion()
        s.exit_recursion()
        s.enter_recursion()  # back to depth 1, still ok
        s.check()

    def test_wall_seconds_overage_raises(self) -> None:
        s = BudgetState(Budget(wall_seconds=0.05))
        time.sleep(0.07)
        with pytest.raises(BudgetExceeded) as excinfo:
            s.check()
        assert excinfo.value.reason == "wall_seconds"

    def test_first_violation_reason_wins(self) -> None:
        # usd is checked before tokens; both exceeded — caller sees usd first.
        s = BudgetState(Budget(usd=1.0, tokens=10))
        s.add_usd(2.0)
        s.add_tokens(100)
        with pytest.raises(BudgetExceeded) as excinfo:
            s.check()
        assert excinfo.value.reason == "usd"

    def test_state_attached_to_exception(self) -> None:
        s = BudgetState(Budget(usd=0.5))
        s.add_usd(1.0)
        with pytest.raises(BudgetExceeded) as excinfo:
            s.check()
        assert excinfo.value.state is s
