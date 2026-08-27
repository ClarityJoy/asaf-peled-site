"""Telling "blocked" apart from "quiet" and from "network broke".

JobSpy reports trouble by logging it and returning an empty list. Three very
different situations therefore look identical at the return value:

    * the board genuinely had no matching jobs
    * the board rate-limited us (429) or served a challenge page
    * the network never reached the board at all

The digest has to distinguish these -- a silently short digest is the failure
mode this whole tool is built to avoid -- so we capture JobSpy's log records
and classify them.

Ordering matters. A proxy error carries the text "403 Forbidden" in its
message, so network markers are checked *before* block markers; otherwise a
dead network reads as a LinkedIn ban and aborts the run for no reason.
"""

from __future__ import annotations

import logging

# Checked first. These mean we never got a real answer from the board.
NETWORK_MARKERS = (
    "max retries exceeded",
    "proxyerror",
    "unable to connect to proxy",
    "connectionerror",
    "connection refused",
    "connection aborted",
    "connection reset",
    "temporary failure in name resolution",
    "nameresolutionerror",
    "failed to resolve",
    "read timed out",
    "timeout",
    "ssl",
    "certificate verify failed",
    "host not in allowlist",
)

# Checked second. These mean the board answered, and the answer was "no".
BLOCK_MARKERS = (
    "429",
    "too many requests",
    "blocked by",
    "challenge",
    "checkpoint",
    "captcha",
    "authwall",
    "unusual activity",
    "403",
    "access denied",
)


class Classification:
    NETWORK = "network"
    BLOCKED = "blocked"
    OTHER = "other"


def classify(message: str) -> str:
    lowered = message.lower()
    for marker in NETWORK_MARKERS:
        if marker in lowered:
            return Classification.NETWORK
    for marker in BLOCK_MARKERS:
        if marker in lowered:
            return Classification.BLOCKED
    return Classification.OTHER


class _Collector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.messages.append(record.getMessage())
        except Exception:  # never let logging break the run
            pass


class JobSpyLogCapture:
    """Context manager that watches every ``JobSpy:*`` logger.

    JobSpy builds one logger per board via ``create_logger`` and sets
    ``propagate = False`` on each, so a handler on the root logger sees
    nothing. Handlers have to be attached to the individual loggers, which
    all exist as soon as ``jobspy`` is imported.
    """

    def __init__(self) -> None:
        self._handler = _Collector()
        self._attached: list[logging.Logger] = []

    def __enter__(self) -> "JobSpyLogCapture":
        for name in list(logging.Logger.manager.loggerDict):
            if name.startswith("JobSpy"):
                logger = logging.getLogger(name)
                logger.addHandler(self._handler)
                self._attached.append(logger)
        return self

    def __exit__(self, *exc) -> None:
        for logger in self._attached:
            logger.removeHandler(self._handler)
        self._attached.clear()

    @property
    def messages(self) -> list[str]:
        return list(self._handler.messages)

    def verdict(self) -> tuple[str | None, str | None]:
        """Return ``(classification, message)`` for the most serious record.

        A block outranks a network error, which outranks anything else, so a
        single 429 among a pile of timeouts still stops the run.
        """
        best: tuple[str | None, str | None] = (None, None)
        for message in self._handler.messages:
            kind = classify(message)
            if kind == Classification.BLOCKED:
                return kind, message
            if kind == Classification.NETWORK and best[0] is None:
                best = (kind, message)
            elif kind == Classification.OTHER and best[0] is None:
                best = (kind, message)
        return best
