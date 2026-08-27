"""Post search, driven through real MCP stdio against a fake server.

The transport is real, so the protocol handling is genuinely exercised --
only the LinkedIn data is fake.
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
HERE = pathlib.Path(__file__).resolve().parent

from jobdigest.mcp_client import ToolCall, WriteToolRefused, run_tool_calls, build_env
from jobdigest.models import SourceStatus
from jobdigest.pacing import Pacer, RunAborted
from jobdigest.runner import run_sources
from jobdigest.sources.base import Source
from jobdigest.models import SourceResult
from jobdigest.sources.linkedin_posts import (
    LinkedInPostsSource, parse_posts, payload_understood, looks_like_auth_failure)

FAILS = []
def check(label, cond):
    print(f"  {'ok ' if cond else 'FAIL'} {label}")
    if not cond: FAILS.append(label)

def source(scenario, queries=None, command=None, args=None):
    return LinkedInPostsSource(
        queries=queries or [("hiring product manager fintech", "en")],
        command=command or sys.executable,
        server_args=args if args is not None else [str(HERE / "fake_linkedin_server.py")],
        env_extra={"SCENARIO": scenario})

nopace = lambda: Pacer(0, 0, 20, sleeper=lambda _s: None)

print("\n-- read-only is enforced before anything starts --")
for bad in ("send_message", "connect_with_person", "invented_tool"):
    try:
        run_tool_calls([ToolCall(name=bad)])
        check(f"refuses {bad}", False)
    except WriteToolRefused:
        check(f"refuses {bad}", True)

print("\n-- happy path --")
r = source("ok").fetch(nopace())
check("status ok", r.status == SourceStatus.OK)
check("two posts", r.count == 2)
p = r.items[0]
check("poster extracted", p.poster_name == "Dana Cohen")
check("company extracted", p.company == "Rapyd")
check("headline extracted", "Talent Partner" in (p.poster_headline or ""))
check("direct link extracted", p.url.startswith("https://www.linkedin.com/posts/"))
check("post text extracted", "hiring" in p.text.lower())
check("hebrew post preserved", "מגייסים" in r.items[1].text)

print("\n-- an expired session degrades, it does not fail the run --")
r = source("auth").fetch(nopace())
check("skipped, not failed", r.status == SourceStatus.SKIPPED)
check("not flagged as a problem", not r.status.is_problem)
check("detail tells you how to fix it", "--login" in (r.detail or ""))
check("detail says jobs are unaffected", "unaffected" in (r.detail or "").lower())

print("\n-- a rate limit aborts the run --")
try:
    source("ratelimit").fetch(nopace())
    check("RunAborted raised", False)
except RunAborted as exc:
    check("RunAborted raised", True)
    check("names the 429", "429" in str(exc))

print("\n-- an unreadable reply is reported, not silently empty --")
r = source("garbage").fetch(nopace())
check("garbage -> failed", r.status == SourceStatus.FAILED)
check("warning explains why", any("could not be parsed" in w for w in r.warnings))

print("\n-- a genuinely quiet week is empty, not failed --")
r = source("empty").fetch(nopace())
check("empty -> EMPTY", r.status == SourceStatus.EMPTY)
check("EMPTY is not a problem", not r.status.is_problem)

print("\n-- a missing uvx is actionable, not a traceback --")
r = source("ok", command="definitely-not-a-real-binary", args=["x"]).fetch(nopace())
check("failed cleanly", r.status == SourceStatus.FAILED)
check("detail mentions uvx", "uvx" in (r.detail or "").lower())

print("\n-- dead post source does not stop job sources --")
class FakeJobs(Source):
    name, kind = "indeed", "jobs"
    def fetch(self, pacer):
        return SourceResult(source="indeed", status=SourceStatus.OK, items=[], scrape_calls=1)
rep = run_sources([FakeJobs(), source("auth")], nopace())
check("job source still ran", rep.results[0].status == SourceStatus.OK)
check("post source skipped", rep.results[1].status == SourceStatus.SKIPPED)
check("run not aborted", rep.aborted_reason is None)

print("\n-- payload unwrapping --")
check("plain list", len(parse_posts([{"text": "a", "url": "u"}], "q", "en")) == 1)
check("dict with posts", len(parse_posts({"posts": [{"text": "a", "url": "u"}]}, "q", "en")) == 1)
check("json string", len(parse_posts('{"posts":[{"text":"a","url":"u"}]}', "q", "en")) == 1)
check("string nested in result wrapper",
      len(parse_posts({"result": '{"posts":[{"text":"a","url":"u"}]}'}, "q", "en")) == 1)
check("understood: empty list", payload_understood({"posts": []}))
check("not understood: html", not payload_understood("<html>nope</html>"))

print("\n-- auth marker detection --")
check("detects 'not logged in'", looks_like_auth_failure("Error: not logged in"))
check("detects authwall", looks_like_auth_failure("hit the authwall"))
check("does not fire on normal text", not looks_like_auth_failure("We are hiring a PM"))

print("\n" + ("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
