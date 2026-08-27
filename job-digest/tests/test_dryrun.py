"""Dry run, fixture round-trip, and the once-per-day guard."""
import sys, pathlib, tempfile, hashlib, io, contextlib, datetime
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import pandas as pd, jobspy
from jobdigest.cli import main
from jobdigest.fixtures import export_json, load_from_store, load_json
from jobdigest.store import Store

FAILS = []
def check(label, cond):
    print(f"  {'ok ' if cond else 'FAIL'} {label}")
    if not cond: FAILS.append(label)

ROWS = [
 dict(id="1", site="indeed", job_url="http://i/1",
      title="Senior Product Manager, Regulatory & Licensing", company="Rapyd",
      location="Rehovot, Israel", date_posted="2026-08-26", is_remote=False,
      description="KYC and AML for a payments platform. Licensing."),
 dict(id="3", site="indeed", job_url="http://i/3", title="מנהל מוצר פינטק",
      company="Pepper", location="תל אביב", date_posted="2026-08-25",
      is_remote=False, description="רגולציה ותשלומים"),
 dict(id="4", site="indeed", job_url="http://i/4", title="Product Manager",
      company="Wix", location="Tel Aviv, Israel", date_posted="2026-08-25",
      is_remote=False, description="consumer app roadmap"),
]
jobspy.scrape_jobs = lambda **kw: (pd.DataFrame(ROWS)
    if kw.get("search_term") == "product manager fintech" else pd.DataFrame())
import jobdigest.config as cfg
cfg.Config.build_post_sources = lambda self: []

d = pathlib.Path(tempfile.mkdtemp())
DB, DIG = d / "j.db", d / "digests"
BASE = ["--source", "indeed", "--max-queries", "1", "--no-pacing",
        "--db", str(DB), "--digest-dir", str(DIG)]

def run(extra=()):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(BASE + list(extra))
    return code, buf.getvalue()

print("\n-- a real run first --")
code, out = run()
check("exit 0", code == 0)
check("digest written", (DIG / f"{datetime.date.today():%Y-%m-%d}.md").exists())

print("\n-- one run per day is enforced --")
code, out = run()
check("second run declines", "already completed today" in out)
check("declines cleanly, not as an error", code == 0)
check("points at the alternatives", "--force" in out and "--dry-run" in out)
with Store(DB) as s:
    check("no second run recorded", s.counts()["runs"] == 1)

print("\n-- --force overrides it --")
code, out = run(["--force"])
check("forced run happened", "already completed today" not in out)
with Store(DB) as s:
    check("second run recorded", s.counts()["runs"] == 2)

print("\n-- dry run touches nothing --")
before = hashlib.sha256(DB.read_bytes()).hexdigest()
code, out = run(["--dry-run"])
after = hashlib.sha256(DB.read_bytes()).hexdigest()
check("exit 0", code == 0)
check("database byte-identical", before == after)
check("banner says nothing live was contacted", "nothing live was contacted" in out)
check("says the store was not written", "store was not written to" in out)
check("replays and scores", "STRONG MATCHES" in out)

print("\n-- dry-run digest does not clobber the real one --")
real = DIG / f"{datetime.date.today():%Y-%m-%d}.md"
dry = DIG / f"{datetime.date.today():%Y-%m-%d}-dryrun.md"
check("separate dry-run file", dry.exists())
check("real digest still there", real.exists())
check("they are different files", real.read_text() != dry.read_text())

print("\n-- fixture export and replay round-trip --")
fx = d / "fx.json"
code, out = run(["--export-fixture", str(fx)])
check("export exits 0", code == 0)
check("fixture written", fx.exists())
check("export reports counts", "3 postings" in out)

_, store_out = run(["--dry-run"])
_, file_out = run(["--dry-run", "--fixture", str(fx)])
strip = lambda s: "\n".join(l for l in s.splitlines()
                            if "replaying" not in l and "digest written" not in l)
check("store replay and fixture replay agree", strip(store_out) == strip(file_out))

postings, posts, run_id = load_json(fx)
check("fixture keeps hebrew", any("מנהל מוצר" in p.title for p in postings))
check("fixture keeps descriptions", all(p.description for p in postings))
check("fixture records the run id", run_id is not None)

print("\n-- editing scoring rules changes a dry run, with no live call --")
import yaml
cfgdir = d / "config"
cfgdir.mkdir()
src = pathlib.Path(__file__).resolve().parents[1] / "config"
for name in ("profile.yaml", "queries.yaml", "sources.yaml"):
    (cfgdir / name).write_text((src / name).read_text(encoding="utf-8"), encoding="utf-8")
prof = yaml.safe_load((cfgdir / "profile.yaml").read_text(encoding="utf-8"))
prof["scoring"]["buckets"]["strong"] = 95      # make 'strong' nearly unreachable
(cfgdir / "profile.yaml").write_text(yaml.safe_dump(prof, allow_unicode=True), encoding="utf-8")
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    main(BASE + ["--dry-run", "--config-dir", str(cfgdir)])
tightened = buf.getvalue()
check("raising the bar empties the strong bucket",
      "STRONG MATCHES" not in tightened or "STRONG MATCHES (0)" in tightened)
check("the role is still present, just re-bucketed",
      "Regulatory & Licensing" in tightened)

print("\n-- dry run with no cached data says so --")
empty = d / "empty.db"
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    code = main(["--dry-run", "--db", str(empty), "--digest-dir", str(DIG)])
check("exits non-zero", code == 1)

print("\n" + ("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
