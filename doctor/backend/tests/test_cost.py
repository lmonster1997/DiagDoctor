"""
Tests for src.observability.cost — TokenAccountant token tracking.
"""

from src.observability.cost import (
    TokenAccountant,
    UsageRecord,
    get_accountant,
    set_accountant,
)


class TestUsageRecord:
    """Tests for the UsageRecord dataclass."""

    def test_create_record(self) -> None:
        """Should create a record with all fields."""
        r = UsageRecord(
            model="gpt-4",
            prompt_tokens=100,
            completion_tokens=50,
            cost_usd=0.005,
        )
        assert r.model == "gpt-4"
        assert r.prompt_tokens == 100
        assert r.completion_tokens == 50
        assert r.cost_usd == 0.005

    def test_default_cost(self) -> None:
        """cost_usd should default to 0.0."""
        r = UsageRecord(model="gpt-3.5", prompt_tokens=10, completion_tokens=5)
        assert r.cost_usd == 0.0


class TestTokenAccountant:
    """Tests for the TokenAccountant class."""

    def test_record_single(self) -> None:
        """Recording a single usage should store it."""
        a = TokenAccountant()
        a.record(model="gpt-4o", prompt_tokens=500, completion_tokens=200, cost_usd=0.01)
        assert len(a.records) == 1

    def test_record_multiple(self) -> None:
        """Recording multiple usages should store all."""
        a = TokenAccountant()
        a.record("a", 10, 20, 0.001)
        a.record("b", 30, 40, 0.002)
        a.record("a", 50, 60, 0.003)
        assert len(a.records) == 3

    def test_get_summary_empty(self) -> None:
        """Summary of empty accountant should return zeros."""
        a = TokenAccountant()
        s = a.get_summary()
        assert s["total_cost_usd"] == 0.0
        assert s["total_tokens"] == 0
        assert s["total_calls"] == 0
        assert s["by_model"] == {}

    def test_get_summary_single_model(self) -> None:
        """Summary should aggregate correctly for one model."""
        a = TokenAccountant()
        a.record("gpt-4o", 100, 50, 0.010)
        s = a.get_summary()
        assert s["total_cost_usd"] == 0.010
        assert s["total_tokens"] == 150
        assert s["total_calls"] == 1
        assert s["by_model"]["gpt-4o"]["calls"] == 1
        assert s["by_model"]["gpt-4o"]["prompt_tokens"] == 100
        assert s["by_model"]["gpt-4o"]["completion_tokens"] == 50
        assert s["by_model"]["gpt-4o"]["total_tokens"] == 150
        assert s["by_model"]["gpt-4o"]["cost_usd"] == 0.010

    def test_get_summary_multi_model(self) -> None:
        """Summary should group by model."""
        a = TokenAccountant()
        a.record("gpt-4o", 100, 50, 0.010)
        a.record("gpt-4o-mini", 200, 100, 0.002)
        a.record("gpt-4o", 300, 150, 0.030)
        s = a.get_summary()

        assert s["total_cost_usd"] == 0.042
        assert s["total_tokens"] == 900
        assert s["total_calls"] == 3

        gpt4 = s["by_model"]["gpt-4o"]
        assert gpt4["calls"] == 2
        assert gpt4["prompt_tokens"] == 400
        assert gpt4["completion_tokens"] == 200
        assert gpt4["total_tokens"] == 600

        mini = s["by_model"]["gpt-4o-mini"]
        assert mini["calls"] == 1
        assert mini["total_tokens"] == 300

    def test_reset(self) -> None:
        """reset() should clear all records."""
        a = TokenAccountant()
        a.record("x", 1, 1)
        a.reset()
        assert len(a.records) == 0
        assert a.get_summary()["total_calls"] == 0


class TestContextVarAccountant:
    """Tests for the contextvar-based per-session accountant."""

    def test_default_accountant(self) -> None:
        """Default accountant should be a fresh TokenAccountant."""
        # Set a fresh one to avoid cross-test pollution
        set_accountant(TokenAccountant())
        acc = get_accountant()
        assert isinstance(acc, TokenAccountant)

    def test_set_and_get(self) -> None:
        """set_accountant followed by get_accountant should return same instance."""
        custom = TokenAccountant()
        custom.record("test-model", 1, 1)
        set_accountant(custom)
        assert get_accountant() is custom

    def test_set_accountant_empty(self) -> None:
        """Newly set accountant should start with no records."""
        fresh = TokenAccountant()
        set_accountant(fresh)
        assert get_accountant().get_summary()["total_calls"] == 0
