"""A daily spend ceiling for the AI routes.

Speech-to-text and LLM calls are the only unbounded cost in this system, and
the live consultation loop calls them several times a minute per active
consultation. A stuck client — or a bug in the client's flush interval — can
run up a bill with no upper bound. Rate limiting caps *requests per minute*,
which is not the same thing: cheap calls and expensive calls cost the same
number of tokens against that limit.

This tracks estimated spend for the current UTC day from the same cost figures
the metrics layer records, and refuses the expensive routes once the budget is
exhausted. In-process and non-durable, like the rate limiter: a restart resets
the day. That is a real limitation and it is the right trade for a
single-instance prototype — the alternative is a shared store this project
does not otherwise need.
"""
from __future__ import annotations

import logging
import threading
from datetime import date

from .config import get_settings
from .metrics import registry

log = logging.getLogger("budget")


class DailyBudget:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._day: date = date.today()
        self._spent_usd: float = 0.0

    def _roll(self) -> None:
        today = date.today()
        if today != self._day:
            self._day, self._spent_usd = today, 0.0

    def record(self, usd: float) -> None:
        with self._lock:
            self._roll()
            self._spent_usd += max(usd, 0.0)
            registry.gauge("cma_ai_spend_today_usd", round(self._spent_usd, 6))

    def exhausted(self) -> bool:
        limit = get_settings().ai_daily_budget_usd
        if limit <= 0:
            return False
        with self._lock:
            self._roll()
            return self._spent_usd >= limit

    def status(self) -> dict:
        limit = get_settings().ai_daily_budget_usd
        with self._lock:
            self._roll()
            spent = self._spent_usd
        return {
            "day": self._day.isoformat(),
            "spent_usd": round(spent, 6),
            "limit_usd": limit,
            "enforced": limit > 0,
            "remaining_usd": round(max(limit - spent, 0.0), 6) if limit > 0 else None,
        }

    def reset(self) -> None:
        """Tests only."""
        with self._lock:
            self._day, self._spent_usd = date.today(), 0.0


budget = DailyBudget()
