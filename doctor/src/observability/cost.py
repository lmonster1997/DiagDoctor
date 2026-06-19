"""
Token usage and cost tracking.

TokenAccountant records LLM token usage per session and provides
summary by model.
"""

from contextvars import ContextVar
from dataclasses import dataclass, field


@dataclass
class UsageRecord:
    """A single LLM usage record."""

    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float = 0.0


@dataclass
class TokenAccountant:
    """Tracks token usage and cost for a session."""

    records: list[UsageRecord] = field(default_factory=list)

    def record(
        self,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float = 0.0,
    ) -> None:
        """Record a single LLM usage."""
        self.records.append(
            UsageRecord(
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                cost_usd=cost_usd,
            )
        )

    def get_summary(self) -> dict:
        """
        Get usage summary grouped by model.

        Returns:
            dict with model names as keys and aggregated stats as values.
        """
        summary: dict[str, dict] = {}
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

        return {
            "by_model": summary,
            "total_cost_usd": total_cost,
            "total_tokens": total_tokens,
        }


# Context variable for per-session accountant
_accountant_ctx: ContextVar[TokenAccountant] = ContextVar(
    "accountant", default=TokenAccountant()
)


def get_accountant() -> TokenAccountant:
    """Get the current session's TokenAccountant."""
    return _accountant_ctx.get()


def set_accountant(accountant: TokenAccountant) -> None:
    """Set the current session's TokenAccountant."""
    _accountant_ctx.set(accountant)
