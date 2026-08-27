"""Normalised data model shared by every source adapter.

Sources differ wildly in what they return. Everything downstream of an adapter
(dedup, scoring, digest) sees only these types, so swapping a broken library
for a working one is an adapter change and nothing more.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any


class SourceStatus(str, Enum):
    """Outcome of one adapter's run.

    The distinction between EMPTY and BLOCKED matters more than it looks.
    JobSpy logs a 429 and then returns an empty result list, so a
    rate-limited board and a genuinely quiet board are byte-identical at the
    return value. Only log inspection tells them apart, and conflating them
    is how you get a short digest that looks fine.
    """

    OK = "ok"
    EMPTY = "empty"        # ran cleanly, found nothing
    FAILED = "failed"      # network error, bad config, unexpected exception
    BLOCKED = "blocked"    # 429 / challenge / authwall -> abort the whole run
    SKIPPED = "skipped"    # disabled in config, or a precondition was missing

    @property
    def is_problem(self) -> bool:
        return self in (SourceStatus.FAILED, SourceStatus.BLOCKED)


_WS = re.compile(r"\s+")
_NON_ALNUM = re.compile(r"[^a-z0-9֐-׿ ]+")


def _norm(text: str | None) -> str:
    """Lowercase, strip punctuation, collapse whitespace. Keeps Hebrew."""
    if not text:
        return ""
    return _WS.sub(" ", _NON_ALNUM.sub(" ", text.strip().lower())).strip()


@dataclass
class JobPosting:
    source: str
    title: str
    url: str
    external_id: str | None = None
    company: str | None = None
    location: str | None = None
    direct_url: str | None = None
    date_posted: date | None = None
    is_remote: bool | None = None
    job_type: str | None = None
    description: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None
    salary_interval: str | None = None
    query: str | None = None
    location_label: str | None = None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    # Populated by the store, never by a source. A posting straight out of an
    # adapter has is_new=None, which means "nobody has asked the database yet"
    # -- distinct from False, which means "the database says we've seen it".
    is_new: bool | None = None
    first_seen: str | None = None
    times_seen: int | None = None

    def fingerprint(self) -> str:
        """Stable identity for cross-run and cross-board deduplication.

        Company plus normalised title, and deliberately nothing else.

        Not the URL: the same role appears on Indeed and LinkedIn under
        different URLs, and boards mutate their own URLs with tracking
        parameters between runs, so a URL key would report every duplicate as
        new every night.

        Not the location either, which is the non-obvious one. Boards disagree
        about Israeli place names -- Indeed returns "Tel Aviv-Yafo, Tel Aviv,
        Israel" where LinkedIn returns "Tel Aviv, Israel" for the same job --
        so including the town silently defeats cross-board matching, which is
        the main thing dedup is for here.

        The cost: one company advertising the identical title in two towns
        collapses to a single entry. For a search inside one metro area that
        is the behaviour you want anyway.
        """
        return hashlib.sha256(
            f"{_norm(self.company)}|{_norm(self.title)}".encode()
        ).hexdigest()[:16]

    @property
    def display_location(self) -> str:
        if self.is_remote and not self.location:
            return "Remote"
        if self.is_remote:
            return f"{self.location} (remote)"
        return self.location or "-"


@dataclass
class SourceResult:
    """What an adapter reports back, success or failure."""

    source: str
    status: SourceStatus
    items: list[JobPosting] = field(default_factory=list)
    detail: str | None = None          # human-readable, goes into the digest
    scrape_calls: int = 0
    duration_seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.items)

    def summary_line(self) -> str:
        line = (
            f"{self.source:<10} {self.status.value:<8} {self.count:>4} found"
            f"  {self.scrape_calls:>2} calls  {self.duration_seconds:>6.1f}s"
        )
        if self.detail:
            # Keep the summary one line wide; PROBLEMS prints it in full.
            detail = self.detail if len(self.detail) <= 60 else self.detail[:57] + "..."
            line += f"  - {detail}"
        return line
