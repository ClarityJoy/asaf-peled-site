"""One-shot MCP stdio client.

The LinkedIn post search we depend on is only implemented in an MCP server,
so this speaks MCP to it: start the server as a subprocess, make the calls,
exit. MCP is the wrong shape for an agent loop in cron, but a one-shot stdio
subprocess is just a CLI with JSON framing, and it means we reuse a
maintained implementation instead of writing a LinkedIn scraper.

Read-only is enforced here rather than trusted. The server exposes
send_message and connect_with_person; this client refuses to call them, so
no amount of config or future editing of an adapter can make the pipeline
write to anyone's LinkedIn.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable

log = logging.getLogger(__name__)

# The only tools this pipeline may ever call.
READ_ONLY_TOOLS = frozenset({
    "search_posts", "get_feed", "get_company_posts",
    "search_jobs", "get_job_details", "get_saved_jobs",
    "get_person_profile", "get_my_profile", "get_company_profile",
    "search_people", "search_companies", "get_company_employees",
    "get_sidebar_profiles", "close_session",
})

# Named explicitly so the refusal message can be specific about why.
WRITE_TOOLS = frozenset({"send_message", "connect_with_person"})

# Environment the server actually needs. HOME matters most: the LinkedIn
# session lives in ~/.linkedin-mcp/profile, and the MCP stdio client hands the
# subprocess a scrubbed environment by default, so without this the server
# starts up unable to find the session and reports itself logged out.
ENV_PASSTHROUGH = (
    "HOME", "PATH", "USER", "LANG", "LC_ALL", "TMPDIR",
    "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "UV_CACHE_DIR",
    "XDG_CACHE_HOME", "XDG_CONFIG_HOME",
)


class McpUnavailable(Exception):
    """The server could not be started at all."""


class WriteToolRefused(Exception):
    """Someone tried to make this pipeline do something read-write."""


@dataclass
class ToolCall:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    label: str = ""


@dataclass
class ToolOutcome:
    call: ToolCall
    ok: bool
    text: str = ""
    data: Any = None
    error: str | None = None


def build_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k in ENV_PASSTHROUGH}
    if extra:
        env.update(extra)
    return env


def _check_read_only(calls: list[ToolCall]) -> None:
    for call in calls:
        if call.name in WRITE_TOOLS:
            raise WriteToolRefused(
                f"refusing to call '{call.name}': this tool is read-only and "
                f"never applies, connects, messages, likes or follows"
            )
        if call.name not in READ_ONLY_TOOLS:
            raise WriteToolRefused(
                f"refusing to call unknown tool '{call.name}': not on the "
                f"read-only allowlist"
            )


def _extract(result: Any) -> tuple[str, Any]:
    """Pull text and any structured payload out of a CallToolResult."""
    chunks: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            chunks.append(text)
    text = "\n".join(chunks)

    data = getattr(result, "structured_content", None)
    if data is None:
        data = getattr(result, "structuredContent", None)
    if data is None and text:
        try:
            data = json.loads(text)
        except (ValueError, TypeError):
            data = None
    return text, data


def run_tool_calls(
    calls: list[ToolCall],
    *,
    command: str = "uvx",
    args: list[str] | None = None,
    env: dict[str, str] | None = None,
    per_call_timeout: float = 180.0,
    startup_timeout: float = 240.0,
    before_call: Callable[[ToolCall], None] | None = None,
) -> list[ToolOutcome]:
    """Start the server, run every call in one session, shut it down.

    One session for all calls on purpose: starting the server means starting a
    browser, which is by far the most expensive part. `before_call` is where
    the caller applies pacing between queries.
    """
    _check_read_only(calls)

    try:
        import anyio
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise McpUnavailable(f"mcp client library not installed: {exc}") from exc

    server_args = args if args is not None else ["mcp-server-linkedin", "--transport", "stdio"]
    params = StdioServerParameters(
        command=command, args=server_args, env=env or build_env()
    )
    outcomes: list[ToolOutcome] = []

    async def _run() -> None:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                with anyio.fail_after(startup_timeout):
                    await session.initialize()
                for call in calls:
                    if before_call is not None:
                        before_call(call)
                    try:
                        with anyio.fail_after(per_call_timeout):
                            raw = await session.call_tool(call.name, call.arguments)
                    except Exception as exc:  # noqa: BLE001
                        outcomes.append(
                            ToolOutcome(call=call, ok=False,
                                        error=f"{type(exc).__name__}: {exc}")
                        )
                        continue
                    text, data = _extract(raw)
                    is_error = bool(getattr(raw, "is_error", False) or
                                    getattr(raw, "isError", False))
                    outcomes.append(
                        ToolOutcome(call=call, ok=not is_error, text=text,
                                    data=data, error=text if is_error else None)
                    )

    try:
        anyio.run(_run)
    except Exception as exc:  # noqa: BLE001
        # A failure to start is different in kind from a call that failed:
        # it usually means uvx is missing or the package could not be fetched.
        if not outcomes:
            raise McpUnavailable(f"{type(exc).__name__}: {exc}") from exc
        log.warning("mcp session ended early: %s", exc)
    return outcomes
