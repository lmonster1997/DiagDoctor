"""
Token usage and cost tracking.

TokenAccountant records LLM token usage per session and provides
per-model summaries. Uses contextvars to maintain per-session state.

Usage:
    from src.observability.cost import get_accountant

    accountant = get_accountant()
    accountant.record(model="gpt-4o", prompt_tokens=500, completion_tokens=200, cost_usd=0.005)
    summary = accountant.get_summary()
"""

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any


@dataclass
class UsageRecord:
    """A single LLM usage event."""

    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float = 0.0


@dataclass
class TokenAccountant:
    """
    Tracks token usage and cost across LLM calls within a session.

    Attributes:
        records: The list of all usage records in order of occurrence.
    """

    records: list[UsageRecord] = field(default_factory=list)

    def record(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float = 0.0,
    ) -> None:
        """
        Record a single LLM usage event.

        Args:
            model: The model identifier (e.g., "gpt-4o-mini").
            prompt_tokens: Number of tokens in the prompt.
            completion_tokens: Number of tokens in the completion.
            cost_usd: Estimated cost in USD (default 0.0).
        """
        self.records.append(
            UsageRecord(
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=cost_usd,
            )
        )

    def get_summary(self) -> dict[str, Any]:
        """
        Get usage summary grouped by model.

        Returns:
            A dict with structure:
            {
                "by_model": {
                    "model-a": {
                        "calls": int,
                        "prompt_tokens": int,
                        "completion_tokens": int,
                        "total_tokens": int,
                        "cost_usd": float,
                    },
                    ...
                },
                "total_cost_usd": float,
                "total_tokens": int,
                "total_calls": int,
            }
        """
        summary: dict[str, dict[str, Any]] = {}
        for r in self.records:
            if r.model not in summary:
                summary[r.model] = {
                    "calls": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                    "cost_usd": 0.0,
                }
            s = summary[r.model]
            s["calls"] += 1
            s["prompt_tokens"] += r.prompt_tokens
            s["completion_tokens"] += r.completion_tokens
            s["total_tokens"] += r.prompt_tokens + r.completion_tokens
            s["cost_usd"] += r.cost_usd

        total_cost = sum(s["cost_usd"] for s in summary.values())
        total_tokens = sum(s["total_tokens"] for s in summary.values())
        total_calls = sum(s["calls"] for s in summary.values())

        return {
            "by_model": summary,
            "total_cost_usd": total_cost,
            "total_tokens": total_tokens,
            "total_calls": total_calls,
        }

    def reset(self) -> None:
        """Clear all recorded usage data."""
        self.records.clear()


# ── Context variable for per-session accountant ──────────────────────


_accountant_ctx: ContextVar[TokenAccountant | None] = ContextVar("accountant", default=None)


def get_accountant() -> TokenAccountant:
    """Get the current session's TokenAccountant from context."""
    acct = _accountant_ctx.get()
    if acct is None:
        acct = TokenAccountant()
        _accountant_ctx.set(acct)
    return acct


def set_accountant(accountant: TokenAccountant) -> None:
    """Set a new TokenAccountant for the current session context."""
    _accountant_ctx.set(accountant)
