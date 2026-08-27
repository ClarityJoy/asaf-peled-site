"""Does a real 429 actually stop the run?

We cannot provoke a genuine 429 on demand, so this drives the real
JobSpySource against a stubbed scrape_jobs that logs exactly what JobSpy logs
when LinkedIn rate-limits it -- and then returns an empty frame, which is the
part that makes a block look like a quiet day.
"""
import logging, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import jobspy  # noqa: F401  - creates the JobSpy:* loggers
from jobdigest.pacing import Pacer, RunAborted
from jobdigest.models import SourceStatus
from jobdigest.runner import run_sources
from jobdigest.sources import jobspy_source
from jobdigest.sources.jobspy_source import JobSpySource

FAILS = []
def check(label, cond):
    print(f"  {'ok ' if cond else 'FAIL'} {label}")
    if not cond: FAILS.append(label)

LOC = [{"label": "central-israel", "location": "Tel Aviv, Israel",
        "country_indeed": "israel", "distance": 50, "is_remote": False}]

def source(name="linkedin", queries=("q1", "q2")):
    return JobSpySource(name=name, site=name, queries=list(queries), locations=LOC)

def stub(message, level=logging.ERROR, logger="JobSpy:LinkedIn"):
    def _scrape(**kwargs):
        logging.getLogger(logger).log(level, message)
        return None          # JobSpy returns an empty result alongside the log
    return _scrape

class FakeJobspyModule:
    def __init__(self, fn): self.scrape_jobs = fn

def install(monkey_fn):
    sys.modules["jobspy"].scrape_jobs = monkey_fn

real_scrape = sys.modules["jobspy"].scrape_jobs

print("\n-- a logged 429 aborts the run --")
install(stub("429 Response - Blocked by LinkedIn for too many requests"))
src = source()
try:
    src.fetch(Pacer(0, 0, 10, sleeper=lambda _s: None))
    check("RunAborted raised", False)
except RunAborted as exc:
    check("RunAborted raised", True)
    check("reason names the board", "linkedin" in str(exc).lower())
    check("reason carries the 429", "429" in str(exc))

print("\n-- abort stops before the second query --")
calls = []
def counting(**kw):
    calls.append(kw.get("search_term"))
    logging.getLogger("JobSpy:LinkedIn").error("429 Response - Blocked by LinkedIn")
    return None
install(counting)
try:
    source(queries=("q1", "q2", "q3")).fetch(Pacer(0, 0, 10, sleeper=lambda _s: None))
except RunAborted:
    pass
check("stopped after the first query, did not push on", calls == ["q1"])

print("\n-- runner marks it BLOCKED and skips the rest --")
install(stub("429 Response - Blocked by LinkedIn"))
a, b = source("linkedin"), source("bayt")
rep = run_sources([a, b], Pacer(0, 0, 10, sleeper=lambda _s: None))
check("first source BLOCKED", rep.results[0].status == SourceStatus.BLOCKED)
check("second source SKIPPED", rep.results[1].status == SourceStatus.SKIPPED)
check("run records abort reason", rep.aborted_reason is not None)

print("\n-- a network error does NOT abort (it is not a ban) --")
install(stub("LinkedIn: HTTPSConnectionPool(host='www.linkedin.com', port=443): "
             "Max retries exceeded (Caused by ProxyError('Unable to connect to "
             "proxy', OSError('Tunnel connection failed: 403 Forbidden')))"))
rep = run_sources([source("linkedin")], Pacer(0, 0, 10, sleeper=lambda _s: None))
check("no abort on a network error", rep.aborted_reason is None)
check("reported FAILED, not EMPTY", rep.results[0].status == SourceStatus.FAILED)

print("\n-- a genuinely quiet board is EMPTY, not FAILED --")
install(lambda **kw: None)   # no log output at all, no results
rep = run_sources([source("indeed")], Pacer(0, 0, 10, sleeper=lambda _s: None))
check("quiet board -> EMPTY", rep.results[0].status == SourceStatus.EMPTY)
check("EMPTY is not flagged as a problem", not rep.results[0].status.is_problem)

install(real_scrape)
print("\n" + ("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
