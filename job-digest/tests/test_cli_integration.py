"""Drive the real CLI end to end against a stubbed board.

Proves the thing that matters across runs: the same jobs reported once as new
and thereafter as already seen.
"""
import sys, pathlib, tempfile, io, contextlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pandas as pd
import jobspy
from jobdigest.cli import main

FAILS = []
def check(label, cond):
    print(f"  {'ok ' if cond else 'FAIL'} {label}")
    if not cond: FAILS.append(label)

ROWS = [
    {"id": "1", "site": "indeed", "job_url": "http://i/1", "title": "Senior Product Manager, Payments",
     "company": "Rapyd", "location": "Tel Aviv-Yafo, Tel Aviv, Israel", "date_posted": "2026-08-26",
     "is_remote": False, "description": "KYC and licensing surface."},
    {"id": "2", "site": "indeed", "job_url": "http://i/2", "title": "Regulatory Product Manager",
     "company": "Melio", "location": "Tel Aviv, Israel", "date_posted": "2026-08-25",
     "is_remote": True, "description": "AML."},
]

def fake_scrape(**kwargs):
    # Only the first query returns anything, so repeated queries do not
    # multiply the row count and muddy the assertions.
    if kwargs.get("search_term") != "product manager fintech":
        return pd.DataFrame()
    return pd.DataFrame(ROWS)

jobspy.scrape_jobs = fake_scrape
sys.modules["jobdigest.sources.jobspy_source"].__dict__.setdefault("_", None)

db = pathlib.Path(tempfile.mkdtemp()) / "jobs.db"
# --force because this drives several runs in one day on purpose; the
# once-per-day guard is exercised in test_dryrun.py instead.
ARGS = ["--source", "indeed", "--max-queries", "2", "--no-pacing",
        "--db", str(db), "--force", "--no-digest"]

def run():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(ARGS)
    return code, buf.getvalue()

print("\n-- run 1 --")
code1, out1 = run()
check("exit 0", code1 == 0)
check("2 unique found", "2 unique" in out1)
check("2 new since last run", "2 new since last run" in out1)
check("both marked NEW", out1.count("NEW  ") == 2)
check("nothing marked seen", "] seen " not in out1)
check("posting rendered", "Senior Product Manager, Payments" in out1)

print("\n-- run 2: identical data --")
code2, out2 = run()
check("exit 0", code2 == 0)
check("0 new since last run", "0 new since last run" in out2)
check("both marked seen", out2.count("seen ") >= 2)
check("nothing marked NEW", "NEW  " not in out2)
check("run counter advanced", "(run #2)" in out2)
check("repeat count surfaced", "seen in 2 runs" in out2)

print("\n-- run 3: one new posting appears --")
ROWS.append({"id": "3", "site": "indeed", "job_url": "http://i/3",
             "title": "Product Operations Lead", "company": "Payoneer",
             "location": "Herzliya, Israel", "date_posted": "2026-08-27",
             "is_remote": False, "description": "Compliance ops."})
code3, out3 = run()
check("exit 0", code3 == 0)
check("exactly 1 new", "1 new since last run" in out3)
check("the new one is marked NEW", "NEW  Product Operations Lead" in out3)
check("other two marked seen", out3.count("seen in 2 runs") + out3.count("seen in 3 runs") == 2)

print("\n-- --no-store makes everything new again --")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    code4 = main(ARGS + ["--no-store"])
out4 = buf.getvalue()
check("exit 0", code4 == 0)
check("store reported disabled", "store: disabled" in out4)
check("all 3 look new", "3 new since last run" in out4)
check("caveat printed", "'new' is meaningless in this mode" in out4)

print("\n" + ("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
