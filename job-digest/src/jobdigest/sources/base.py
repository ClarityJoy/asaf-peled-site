"""The interface every data source implements.

Adapters are the only place that knows about a third-party library. When one
breaks -- and one will -- the replacement implements this same interface and
nothing downstream changes.

An adapter never raises for an ordinary failure. It returns a SourceResult
carrying a status and a human-readable detail, so the digest can say plainly
that a source failed. The single exception is RunAborted, which propagates on
purpose: a 429 ends the run.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import SourceResult
from ..pacing import Pacer


class Source(ABC):
    name: str
    kind: str = "jobs"     # "jobs" | "posts"

    @property
    def enabled(self) -> bool:
        return True

    @abstractmethod
    def fetch(self, pacer: Pacer) -> SourceResult:
        """Collect from this source. Must not raise except RunAborted."""
        raise NotImplementedError
