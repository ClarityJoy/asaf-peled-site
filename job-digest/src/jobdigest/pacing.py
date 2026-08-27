"""Request pacing and the run-wide budget.

Ground rule: randomised delays, a hard cap per run, one run per day, no
parallelism against LinkedIn. This module owns the first two. Sequential
execution owns the third -- the runner calls one adapter at a time and each
adapter calls one board at a time, so there is no thread pool to configure.
"""

from __future__ import annotations

import logging
import random
import time

log = logging.getLogger(__name__)


class BudgetExhausted(Exception):
    """The run hit its scrape-call ceiling. Ends the run cleanly."""


class RunAborted(Exception):
    """A board returned 429 or a challenge. Stop everything, record it.

    Deliberately not retried and deliberately not caught by adapters: the
    whole point is that the run ends rather than pushing harder against a
    board that just said no.
    """


class Pacer:
    """Randomised sleeps between calls, plus a hard ceiling on how many.

    The ceiling counts *scrape calls*, not HTTP requests, because JobSpy owns
    the HTTP layer and does not report request counts. One scrape call is one
    query against one board, and pagination inside it is bounded by
    ``results_wanted``. So the real HTTP ceiling is roughly
    ``max_scrape_calls x ceil(results_wanted / page_size)``, which for the
    shipped config is well under LinkedIn's rate-limit band.
    """

    def __init__(
        self,
        min_delay: float = 12.0,
        max_delay: float = 35.0,
        max_scrape_calls: int = 40,
        sleeper=time.sleep,
    ) -> None:
        if min_delay > max_delay:
            raise ValueError("min_delay must not exceed max_delay")
        self.min_delay = min_delay
        self.max_delay = max_delay
        self.max_scrape_calls = max_scrape_calls
        self.calls_made = 0
        self._sleeper = sleeper
        self._first_call = True

    @property
    def remaining(self) -> int:
        return max(0, self.max_scrape_calls - self.calls_made)

    def take(self, label: str = "") -> None:
        """Claim one scrape call, sleeping first. Raises when out of budget."""
        if self.remaining <= 0:
            raise BudgetExhausted(
                f"run budget of {self.max_scrape_calls} scrape calls exhausted"
            )
        # No delay before the very first call of the run; there is nothing to
        # pace away from yet.
        if not self._first_call:
            delay = random.uniform(self.min_delay, self.max_delay)
            log.debug("pacing %.1fs before %s", delay, label or "next call")
            self._sleeper(delay)
        self._first_call = False
        self.calls_made += 1
