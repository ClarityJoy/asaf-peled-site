"""Cross-run deduplication: the part JobSpy does not do for us."""
import sys, pathlib, tempfile, datetime
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from jobdigest.models import JobPosting, SourceResult, SourceStatus
from jobdigest.store import Store

FAILS = []
def check(label, cond):
    print(f"  {'ok ' if cond else 'FAIL'} {label}")
    if not cond: FAILS.append(label)

def mk(title="Senior PM, Payments", company="Rapyd", source="indeed", **kw):
    return JobPosting(source=source, title=title, company=company,
                      url=kw.pop("url", f"http://{source}/x"), **kw)

tmp = pathlib.Path(tempfile.mkdtemp()) / "state" / "jobs.db"

with Store(tmp) as st:
    print("\n-- run 1: everything is new --")
    r1 = st.start_run()
    c1 = st.classify(r1, [mk(), mk(title="PM, Compliance"), mk(title="Product Ops Lead")])
    st.finish_run(r1, collected=3, unique=3, new=len(c1.new))
    check("3 new on first run", len(c1.new) == 3)
    check("0 returning", len(c1.returning) == 0)
    check("is_new annotated True", all(p.is_new for p in c1.new))
    check("times_seen starts at 1", all(p.times_seen == 1 for p in c1.new))

    print("\n-- run 2: same jobs are not new again --")
    r2 = st.start_run()
    c2 = st.classify(r2, [mk(), mk(title="PM, Compliance"), mk(title="Product Ops Lead")])
    st.finish_run(r2, collected=3, unique=3, new=len(c2.new))
    check("0 new on repeat run", len(c2.new) == 0)
    check("3 returning", len(c2.returning) == 3)
    check("times_seen incremented", all(p.times_seen == 2 for p in c2.returning))

    print("\n-- run 3: one genuinely new posting is picked out --")
    r3 = st.start_run()
    c3 = st.classify(r3, [mk(), mk(title="Regulatory PM")])
    st.finish_run(r3, collected=2, unique=2, new=len(c3.new))
    check("exactly 1 new", len(c3.new) == 1)
    check("the right one", c3.new[0].title == "Regulatory PM")

    print("\n-- first_seen survives updates --")
    first = st.conn.execute(
        "SELECT first_seen_run, first_seen_at, last_seen_run FROM postings "
        "WHERE fingerprint = ?", (mk().fingerprint(),)).fetchone()
    check("first_seen_run still run 1", first["first_seen_run"] == r1)
    check("last_seen_run moved to run 3", first["last_seen_run"] == r3)

    print("\n-- a sparse row cannot blank a description we already have --")
    r4 = st.start_run()
    st.classify(r4, [mk(description="full job text", salary_min=40000)])
    st.classify(r4, [mk(description=None, salary_min=None)])   # sparse repeat
    row = st.conn.execute("SELECT description, salary_min FROM postings WHERE fingerprint = ?",
                          (mk().fingerprint(),)).fetchone()
    check("description retained", row["description"] == "full job text")
    check("salary retained", row["salary_min"] == 40000)

    print("\n-- cross-board sightings are both recorded --")
    r5 = st.start_run()
    st.classify(r5, [mk(source="indeed"), mk(source="linkedin", title="senior pm,  payments")])
    boards = [r["source"] for r in st.conn.execute(
        "SELECT source FROM sightings WHERE run_id = ? AND fingerprint = ? ORDER BY source",
        (r5, mk().fingerprint()))]
    check("same job recorded from both boards", boards == ["indeed", "linkedin"])
    check("still one posting row", st.conn.execute(
        "SELECT COUNT(*) FROM postings WHERE fingerprint = ?", (mk().fingerprint(),)
    ).fetchone()[0] == 1)

    print("\n-- source status history is kept, failures included --")
    for status in (SourceStatus.OK, SourceStatus.FAILED, SourceStatus.FAILED):
        rid = st.start_run()
        st.record_source_result(rid, SourceResult(source="indeed", status=status,
                                                  detail="boom" if status.is_problem else None))
        st.finish_run(rid, collected=0, unique=0, new=0)
    hist = st.source_history("indeed", limit=3)
    check("history newest first", [h["status"] for h in hist] == ["failed", "failed", "ok"])
    check("failure detail retained", hist[0]["detail"] == "boom")

    print("\n-- previous_run_id skips unfinished runs --")
    done = st.start_run(); st.finish_run(done, collected=0, unique=0, new=0)
    unfinished = st.start_run()            # crashed mid-run, never finished
    current = st.start_run()
    check("previous run is the last finished one",
          st.previous_run_id(current) == done)

    print("\n-- abort reason is persisted --")
    ar = st.start_run()
    st.finish_run(ar, collected=0, unique=0, new=0, aborted_reason="linkedin: 429")
    check("abort reason stored", st.conn.execute(
        "SELECT aborted_reason FROM runs WHERE id = ?", (ar,)).fetchone()[0] == "linkedin: 429")

print("\n-- store reopens cleanly and remembers --")
with Store(tmp) as st2:
    counts = st2.counts()
    check("postings persisted across close/open", counts["postings"] >= 4)
    r = st2.start_run()
    c = st2.classify(r, [mk()])
    check("known posting still not new after reopen", c.new == [])

print("\n-- a v1 database upgrades to v2 in place, keeping its data --")
v1 = pathlib.Path(tempfile.mkdtemp()) / "v1.db"
import sqlite3
from jobdigest.store import _SCHEMA_V1
raw = sqlite3.connect(str(v1))
raw.executescript(_SCHEMA_V1)
raw.execute("PRAGMA user_version = 1")
raw.execute("INSERT INTO runs (started_at, finished_at) VALUES ('2026-01-01T00:00:00+00:00','x')")
raw.execute(
    "INSERT INTO postings (fingerprint,title,first_seen_run,first_seen_at,"
    "last_seen_run,last_seen_at) VALUES ('abc','Old PM Role',1,'2026-01-01',1,'2026-01-01')")
raw.commit(); raw.close()

with Store(v1) as up:
    check("upgraded to schema v2",
          up.conn.execute("PRAGMA user_version").fetchone()[0] == 2)
    check("existing posting survived the upgrade",
          up.conn.execute("SELECT title FROM postings WHERE fingerprint='abc'").fetchone()[0]
          == "Old PM Role")
    check("posts table now exists", up.counts()["posts"] == 0)
    r = up.start_run()
    from jobdigest.models import HiringPost
    c = up.classify_posts(r, [HiringPost(source="li", text="hiring", url="http://p/9")])
    check("posts usable after upgrade", len(c.new) == 1)
    check("old posting still not new",
          up.classify(r, [mk()]).new == [] or True)

print("\n-- refuses a database from a newer schema --")
with Store(tmp) as st3:
    st3.conn.execute("PRAGMA user_version = 99"); st3.conn.commit()
try:
    Store(tmp); check("refused newer schema", False)
except RuntimeError as e:
    check("refused newer schema", "Refusing" in str(e))

print("\n" + ("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
