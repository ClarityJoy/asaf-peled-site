"""Logic tests that need no network."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from jobdigest.models import JobPosting, SourceResult, SourceStatus
from jobdigest.pacing import Pacer, BudgetExhausted, RunAborted
from jobdigest.runner import run_sources
from jobdigest.sources.base import Source

FAILS = []
def check(label, cond):
    print(f"  {'ok ' if cond else 'FAIL'} {label}")
    if not cond: FAILS.append(label)

def mk(source, title, company, **kw):
    return JobPosting(source=source, title=title, company=company,
                      url=kw.pop("url", f"http://{source}/{title}"), **kw)

class Fake(Source):
    def __init__(self, name, items=None, exc=None, enabled=True):
        self.name, self._items, self._exc, self._enabled = name, items or [], exc, enabled
        self.ran = False
    @property
    def enabled(self): return self._enabled
    def fetch(self, pacer):
        self.ran = True
        pacer.take(self.name)
        if self._exc: raise self._exc
        return SourceResult(source=self.name, status=SourceStatus.OK,
                            items=self._items, scrape_calls=1)

nosleep = lambda _s: None

print("\n-- pacer --")
p = Pacer(1, 2, max_scrape_calls=3, sleeper=nosleep)
for i in range(3): p.take()
check("budget allows exactly max_scrape_calls", p.calls_made == 3)
try:
    p.take(); check("raises past budget", False)
except BudgetExhausted:
    check("raises past budget", True)

slept = []
p2 = Pacer(5, 5, 10, sleeper=slept.append)
p2.take(); p2.take(); p2.take()
check("no delay before first call, delays after", slept == [5.0, 5.0])

print("\n-- abort stops the run --")
a = Fake("a", [mk("a", "PM", "X")])
b = Fake("b", exc=RunAborted("b: 429 Response - Blocked by LinkedIn"))
c = Fake("c", [mk("c", "PM2", "Y")])
rep = run_sources([a, b, c], Pacer(0, 0, 10, sleeper=nosleep))
check("aborted_reason recorded", rep.aborted_reason is not None)
check("source after abort did not run", c.ran is False)
check("source after abort marked skipped",
      rep.results[2].status == SourceStatus.SKIPPED)
check("aborting source marked blocked", rep.results[1].status == SourceStatus.BLOCKED)
check("results from before abort kept", len(rep.postings) == 1)
check("had_problem true", rep.had_problem)

print("\n-- one bad adapter does not kill the run --")
rep = run_sources([Fake("boom", exc=ValueError("kaboom")), Fake("good", [mk("good","PM","Z")])],
                  Pacer(0, 0, 10, sleeper=nosleep))
check("bad adapter -> FAILED", rep.results[0].status == SourceStatus.FAILED)
check("later source still ran", rep.results[1].status == SourceStatus.OK)
check("detail carries exception", "kaboom" in (rep.results[0].detail or ""))

print("\n-- disabled source --")
rep = run_sources([Fake("off", enabled=False)], Pacer(0, 0, 10, sleeper=nosleep))
check("disabled -> SKIPPED", rep.results[0].status == SourceStatus.SKIPPED)

print("\n-- cross-board dedup prefers richer record --")
thin = mk("linkedin", "Senior PM, Payments", "Rapyd")
rich = mk("indeed", "senior pm,  payments", "Rapyd ", description="full text")
rep = run_sources([Fake("linkedin", [thin]), Fake("indeed", [rich])],
                  Pacer(0, 0, 10, sleeper=nosleep))
check("2 collected", len(rep.postings) == 2)
check("1 unique", len(rep.unique_postings) == 1)
check("kept the one with a description", rep.unique_postings[0].description == "full text")

print("\n-- status semantics --")
check("EMPTY is not a problem", not SourceStatus.EMPTY.is_problem)
check("FAILED is a problem", SourceStatus.FAILED.is_problem)
check("BLOCKED is a problem", SourceStatus.BLOCKED.is_problem)

print("\n" + ("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
