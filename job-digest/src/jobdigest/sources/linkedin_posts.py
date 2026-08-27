"""LinkedIn post search -- the one authenticated source.

This is the only part of the tool that touches an account, and it is built to
be the least important part: if the session is dead, if uvx is missing, if
the server will not start, the digest says so in one line and the
unauthenticated job sources carry the run. Nothing here can abort the run
except a genuine rate limit or challenge.

Expiry is detected by reading what came back, not by trusting a status code.
LinkedIn answers an expired session with HTTP 200 and a login page, so a
transport-level check would report everything as fine.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from ..blocking import BLOCK_MARKERS
from ..models import HiringPost, SourceResult, SourceStatus
from ..mcp_client import McpUnavailable, ToolCall, ToolOutcome, build_env, run_tool_calls
from ..pacing import BudgetExhausted, Pacer, RunAborted
from .base import Source

log = logging.getLogger(__name__)

# An expired session is not a ban. It degrades the digest; it must not abort
# the run, so these are checked before the block markers.
AUTH_MARKERS = (
    "not logged in", "not authenticated", "no session", "session expired",
    "please log in", "please sign in", "sign in to linkedin", "login required",
    "authentication required", "authwall", "no stored session", "run --login",
    "session invalid", "logged out",
)

# Keys the server might plausibly use. Written defensively because the output
# shape is not part of the tool's contract and can change between releases.
_TEXT_KEYS = ("text", "content", "commentary", "post_text", "body", "summary")
_URL_KEYS = ("url", "link", "post_url", "permalink", "postUrl")
_AUTHOR_KEYS = ("author", "poster", "actor", "author_name", "poster_name", "name")
_HEADLINE_KEYS = ("headline", "author_headline", "actor_headline", "subtitle")
_COMPANY_KEYS = ("company", "company_name", "organization", "author_company")
_DATE_KEYS = ("posted_at", "date", "published_at", "time", "posted", "age")
_LIST_KEYS = ("posts", "results", "items", "data", "content")

# MCP servers commonly hand back a JSON *string* nested inside the structured
# payload -- {"result": "{\"posts\": [...]}"} rather than {"posts": [...]}.
# Unwrapping is not optional: without it every reply parses as zero posts and
# a working source reports itself empty.
_WRAPPER_KEYS = ("result", "output", "response", "content", "data")


def _unwrap(payload: Any, depth: int = 0) -> Any:
    """Peel JSON-encoded strings until an actual structure appears."""
    if depth > 4:
        return payload
    if isinstance(payload, (str, bytes)):
        try:
            return _unwrap(json.loads(payload), depth + 1)
        except (ValueError, TypeError):
            return payload
    if isinstance(payload, dict):
        # A real list under a known key wins over any wrapper.
        for key in _LIST_KEYS:
            if isinstance(payload.get(key), list):
                return payload
        for key in _WRAPPER_KEYS:
            value = payload.get(key)
            if isinstance(value, (str, bytes)):
                unwrapped = _unwrap(value, depth + 1)
                if not isinstance(unwrapped, (str, bytes)):
                    return unwrapped
    return payload


def _first(record: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = record.get(key)
        if isinstance(value, dict):
            value = value.get("name") or value.get("text") or value.get("title")
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value not in (None, "", [], {}):
            return value
    return None


def looks_like_auth_failure(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in AUTH_MARKERS)


def looks_like_block(text: str) -> bool:
    lowered = (text or "").lower()
    if looks_like_auth_failure(lowered):
        return False
    return any(marker in lowered for marker in BLOCK_MARKERS)


def payload_understood(payload: Any) -> bool:
    """Did the reply have a shape we recognise, even if it held no posts?

    "LinkedIn had nothing this week" and "the reply was gibberish" both yield
    zero posts, and only one of them is a problem worth reporting.
    """
    payload = _unwrap(payload)
    if isinstance(payload, list):
        return True
    if isinstance(payload, dict):
        return any(isinstance(payload.get(key), list) for key in _LIST_KEYS)
    return False


def parse_posts(payload: Any, query: str, language: str) -> list[HiringPost]:
    """Turn whatever the tool returned into HiringPosts, or nothing."""
    payload = _unwrap(payload)
    records: list[Any] = []
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        for key in _LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                records = value
                break

    posts: list[HiringPost] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        text = _first(record, _TEXT_KEYS)
        url = _first(record, _URL_KEYS)
        if not text and not url:
            continue
        posts.append(
            HiringPost(
                source="linkedin_posts",
                text=str(text or ""),
                url=str(url or ""),
                poster_name=_first(record, _AUTHOR_KEYS),
                poster_headline=_first(record, _HEADLINE_KEYS),
                company=_first(record, _COMPANY_KEYS),
                posted_at=str(_first(record, _DATE_KEYS) or "") or None,
                query=query,
                language=language,
                raw=record,
            )
        )
    return posts


class LinkedInPostsSource(Source):
    kind = "posts"

    def __init__(
        self,
        queries: list[tuple[str, str]],
        *,
        name: str = "linkedin_posts",
        date_posted: str = "past-week",
        max_pages: int = 2,
        command: str = "uvx",
        server_args: list[str] | None = None,
        per_call_timeout: float = 180.0,
        env_extra: dict[str, str] | None = None,
        enabled: bool = True,
    ) -> None:
        self.name = name
        self.queries = queries
        self.date_posted = date_posted
        self.max_pages = max_pages
        self.command = command
        self.server_args = server_args
        self.per_call_timeout = per_call_timeout
        self.env_extra = env_extra
        self._enabled = enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    def fetch(self, pacer: Pacer) -> SourceResult:
        started = time.monotonic()
        if not self.queries:
            return SourceResult(source=self.name, status=SourceStatus.SKIPPED,
                                detail="no post queries configured")

        calls = [
            ToolCall(
                name="search_posts",
                arguments={
                    "keywords": query,
                    "date_posted": self.date_posted,
                    "max_pages": self.max_pages,
                },
                label=f"{language}:{query}",
            )
            for query, language in self.queries
        ]

        budget_hit: list[str] = []

        def before(call: ToolCall) -> None:
            try:
                pacer.take(f"{self.name}:{call.label}")
            except BudgetExhausted as exc:
                budget_hit.append(str(exc))
                raise

        try:
            outcomes = run_tool_calls(
                calls,
                command=self.command,
                args=self.server_args,
                env=build_env(self.env_extra),
                per_call_timeout=self.per_call_timeout,
                before_call=before,
            )
        except McpUnavailable as exc:
            return SourceResult(
                source=self.name,
                status=SourceStatus.FAILED,
                detail=(
                    f"could not start the LinkedIn MCP server ({exc}). "
                    f"Check that uvx is installed and on PATH."
                ),
                duration_seconds=time.monotonic() - started,
            )

        return self._collect(outcomes, budget_hit, started)

    def _collect(
        self, outcomes: list[ToolOutcome], budget_hit: list[str], started: float
    ) -> SourceResult:
        posts: list[HiringPost] = []
        warnings: list[str] = []
        seen: set[str] = set()
        auth_failed = False

        for outcome in outcomes:
            language, _, query = outcome.call.label.partition(":")
            blob = outcome.text or outcome.error or ""

            # Order matters. An expired session degrades; a rate limit aborts.
            if looks_like_auth_failure(blob):
                auth_failed = True
                warnings.append(f"{query}: session not accepted")
                continue
            if looks_like_block(blob):
                raise RunAborted(f"{self.name}: {blob[:160]}")
            if not outcome.ok:
                warnings.append(f"{query}: {blob[:160]}")
                continue

            source_payload = outcome.data if outcome.data is not None else outcome.text
            parsed = parse_posts(source_payload, query, language)
            if not parsed and blob.strip() and not payload_understood(source_payload):
                # Got a reply we could not read. Say so rather than reporting
                # an empty result as though LinkedIn had nothing to show.
                warnings.append(
                    f"{query}: reply could not be parsed as posts "
                    f"({blob[:80].strip()!r})"
                )
            for post in parsed:
                key = post.fingerprint()
                if key in seen:
                    continue
                seen.add(key)
                posts.append(post)

        duration = time.monotonic() - started

        if auth_failed and not posts:
            return SourceResult(
                source=self.name,
                status=SourceStatus.SKIPPED,
                items=[],
                detail=(
                    "LinkedIn session expired or not set up - post search "
                    "skipped. Re-authenticate with: "
                    "uvx mcp-server-linkedin --login   (or --import-from-browser). "
                    "Job sources are unaffected."
                ),
                scrape_calls=len(outcomes),
                duration_seconds=duration,
                warnings=warnings,
            )

        detail = None
        if budget_hit:
            detail = "run budget exhausted"
        elif warnings:
            detail = f"{len(warnings)} of {len(outcomes)} queries had trouble"

        if not posts:
            status = (SourceStatus.FAILED
                      if outcomes and len(warnings) >= len(outcomes)
                      else SourceStatus.EMPTY)
            return SourceResult(
                source=self.name, status=status, items=[],
                detail=detail or "no hiring posts matched in the window",
                scrape_calls=len(outcomes), duration_seconds=duration,
                warnings=warnings,
            )

        return SourceResult(
            source=self.name, status=SourceStatus.OK, items=posts,
            detail=detail, scrape_calls=len(outcomes),
            duration_seconds=duration, warnings=warnings,
        )
