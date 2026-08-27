"""Runs the enabled sources, in sequence, and collects their results."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from .models import JobPosting, SourceResult, SourceStatus
from .pacing import Pacer, RunAborted
from .sources.base import Source

log = logging.getLogger(__name__)


@dataclass
class RunReport:
    results: list[SourceResult] = field(default_factory=list)
    aborted_reason: str | None = None
    duration_seconds: float = 0.0

    @property
    def postings(self) -> list[JobPosting]:
        out: list[JobPosting] = []
        for result in self.results:
            out.extend(result.items)
        return out

    @property
    def unique_postings(self) -> list[JobPosting]:
        """Collapse duplicates across boards, preferring the richer record."""
        best: dict[str, JobPosting] = {}
        for posting in self.postings:
            key = posting.fingerprint()
            existing = best.get(key)
            if existing is None:
                best[key] = posting
                continue
            # Prefer whichever record carries a description, then a direct URL.
            score = (bool(posting.description), bool(posting.direct_url))
            prior = (bool(existing.description), bool(existing.direct_url))
            if score > prior:
                best[key] = posting
        return list(best.values())

    @property
    def had_problem(self) -> bool:
        return self.aborted_reason is not None or any(
            r.status.is_problem for r in self.results
        )


def run_sources(sources: list[Source], pacer: Pacer) -> RunReport:
    """Execute each source in turn. Never parallel -- that is the point."""
    report = RunReport()
    started = time.monotonic()

    for source in sources:
        if not source.enabled:
            report.results.append(
                SourceResult(
                    source=source.name,
                    status=SourceStatus.SKIPPED,
                    detail="disabled in sources.yaml",
                )
            )
            continue

        if report.aborted_reason:
            report.results.append(
                SourceResult(
                    source=source.name,
                    status=SourceStatus.SKIPPED,
                    detail="run aborted before this source ran",
                )
            )
            continue

        log.info("running source %s", source.name)
        try:
            report.results.append(source.fetch(pacer))
        except RunAborted as exc:
            # A board said no. Stop the run; do not back off and retry.
            report.aborted_reason = str(exc)
            report.results.append(
                SourceResult(
                    source=source.name,
                    status=SourceStatus.BLOCKED,
                    detail=str(exc)[:200],
                )
            )
        except Exception as exc:  # noqa: BLE001 - a bad adapter must not kill the run
            log.exception("source %s raised", source.name)
            report.results.append(
                SourceResult(
                    source=source.name,
                    status=SourceStatus.FAILED,
                    detail=f"{type(exc).__name__}: {exc}"[:200],
                )
            )

    report.duration_seconds = time.monotonic() - started
    return report
