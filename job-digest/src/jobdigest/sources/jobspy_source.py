"""Job-board adapter backed by JobSpy.

One instance per board. Indeed, LinkedIn and Bayt are three instances of this
class, not three classes, because JobSpy already normalises across boards --
our job is budgeting, block detection and mapping into our own model.

Pinned to a git commit rather than the PyPI release: PyPI's python-jobspy
1.1.82 is from 2025-07-28 while the repository has carried fixes since,
including LinkedIn date parsing for the current listing format.
"""

from __future__ import annotations

import logging
import math
import time
from datetime import date, datetime
from typing import Any

from ..blocking import Classification, JobSpyLogCapture
from ..models import JobPosting, SourceResult, SourceStatus
from ..pacing import BudgetExhausted, Pacer, RunAborted
from .base import Source

log = logging.getLogger(__name__)

_RETRIES_PATCHED = False


def disable_jobspy_retries() -> bool:
    """Stop JobSpy from retrying a 429 behind our back.

    JobSpy builds LinkedIn's session with ``has_retry=True, delay=5``, which
    installs a urllib3 Retry whose ``status_forcelist`` includes 429 -- so a
    rate-limited request is retried three times with backoff before we ever
    see it. That is precisely the behaviour the ground rules forbid: on a 429
    we stop and record, we do not push harder.

    Neutralised by making retry setup a no-op. Returns True if the patch was
    applied, False if the upstream shape changed and it could not be.
    """
    global _RETRIES_PATCHED
    if _RETRIES_PATCHED:
        return True
    try:
        from jobspy.util import RequestsRotating
    except Exception:
        return False
    if not hasattr(RequestsRotating, "setup_session"):
        return False

    def _no_retries(self, has_retry=False, delay=1):  # noqa: ANN001
        return None

    RequestsRotating.setup_session = _no_retries
    _RETRIES_PATCHED = True
    return True


def _clean(value: Any) -> Any:
    """pandas gives NaN for missing cells; we want None."""
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _as_date(value: Any) -> date | None:
    value = _clean(value)
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


class JobSpySource(Source):
    kind = "jobs"

    def __init__(
        self,
        name: str,
        site: str,
        queries: list[str],
        locations: list[dict[str, Any]],
        results_wanted: int = 25,
        hours_old: int = 72,
        fetch_description: bool = False,
        enabled: bool = True,
    ) -> None:
        self.name = name
        self.site = site
        self.queries = queries
        self.locations = locations
        self.results_wanted = results_wanted
        self.hours_old = hours_old
        self.fetch_description = fetch_description
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    def fetch(self, pacer: Pacer) -> SourceResult:
        from jobspy import scrape_jobs

        disable_jobspy_retries()

        started = time.monotonic()
        items: list[JobPosting] = []
        warnings: list[str] = []
        seen: set[str] = set()
        calls = 0
        budget_hit = False

        for location in self.locations:
            for query in self.queries:
                label = f"{self.name}:{query}@{location.get('label')}"
                try:
                    pacer.take(label)
                except BudgetExhausted as exc:
                    warnings.append(str(exc))
                    budget_hit = True
                    break
                calls += 1

                with JobSpyLogCapture() as capture:
                    try:
                        frame = scrape_jobs(
                            site_name=[self.site],
                            search_term=query,
                            location=location.get("location"),
                            country_indeed=location.get("country_indeed", "israel"),
                            distance=location.get("distance", 50),
                            is_remote=bool(location.get("is_remote", False)),
                            results_wanted=self.results_wanted,
                            hours_old=self.hours_old,
                            linkedin_fetch_description=self.fetch_description,
                            verbose=0,
                        )
                    except Exception as exc:  # noqa: BLE001 - adapters absorb
                        frame = None
                        warnings.append(f"{query}: {type(exc).__name__}: {exc}")

                    kind, message = capture.verdict()

                # A board that answered "no" ends the run. Not retried.
                if kind == Classification.BLOCKED:
                    raise RunAborted(f"{self.name}: {message}")
                if kind == Classification.NETWORK and message:
                    warnings.append(f"{query}: {message}")

                if frame is not None and len(frame):
                    for _, row in frame.iterrows():
                        posting = self._to_posting(row, query, location)
                        if posting.url in seen:
                            continue
                        seen.add(posting.url)
                        items.append(posting)

            if budget_hit:
                break

        duration = time.monotonic() - started
        status, detail = self._verdict(items, warnings, calls, budget_hit)
        return SourceResult(
            source=self.name,
            status=status,
            items=items,
            detail=detail,
            scrape_calls=calls,
            duration_seconds=duration,
            warnings=warnings,
        )

    def _verdict(
        self,
        items: list[JobPosting],
        warnings: list[str],
        calls: int,
        budget_hit: bool,
    ) -> tuple[SourceStatus, str | None]:
        # Every attempt produced a warning and nothing came back: the source
        # did not work. Reporting that as "empty" is the silent-short-digest
        # failure we are trying to avoid.
        if calls and not items and len(warnings) >= calls:
            return SourceStatus.FAILED, warnings[0][:200]
        if not items:
            note = "no matches in window"
            if budget_hit:
                note += "; run budget exhausted"
            return SourceStatus.EMPTY, note
        detail = None
        if warnings:
            detail = f"{len(warnings)} query/queries had trouble"
        if budget_hit:
            detail = (detail + "; " if detail else "") + "run budget exhausted"
        return SourceStatus.OK, detail

    def _to_posting(
        self, row: Any, query: str, location: dict[str, Any]
    ) -> JobPosting:
        return JobPosting(
            source=self.name,
            external_id=_clean(row.get("id")),
            title=str(_clean(row.get("title")) or "(untitled)"),
            company=_clean(row.get("company")),
            location=_clean(row.get("location")),
            url=str(_clean(row.get("job_url")) or ""),
            direct_url=_clean(row.get("job_url_direct")),
            date_posted=_as_date(row.get("date_posted")),
            is_remote=bool(row.get("is_remote")) if _clean(row.get("is_remote")) is not None else None,
            job_type=_clean(row.get("job_type")),
            description=_clean(row.get("description")),
            salary_min=_clean(row.get("min_amount")),
            salary_max=_clean(row.get("max_amount")),
            salary_currency=_clean(row.get("currency")),
            salary_interval=_clean(row.get("interval")),
            query=query,
            location_label=location.get("label"),
        )
