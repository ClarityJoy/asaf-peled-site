"""Digest rendering, with attention to what it must never hide."""
import sys, pathlib, tempfile, datetime
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from jobdigest.config import Config
from jobdigest.digest import DigestRenderer, write_digest
from jobdigest.models import HiringPost, JobPosting, SourceResult, SourceStatus
from jobdigest.runner import RunReport
from jobdigest.scoring import Scorer
from jobdigest.sources.linkedin_posts import detect_language

FAILS = []
def check(label, cond):
    print(f"  {'ok ' if cond else 'FAIL'} {label}")
    if not cond: FAILS.append(label)

profile = Config.load().profile
scorer = Scorer(profile)
NOW = datetime.datetime(2026, 8, 27, 6, 40)

def job(title, company="ACME", location="Tel Aviv, Israel", description=None,
        is_new=True, **kw):
    p = JobPosting(source="indeed", title=title, company=company, location=location,
                   description=description, url="http://x/1", **kw)
    p.is_new, p.times_seen = is_new, 1 if is_new else 3
    p.first_seen = "2026-08-20T00:00:00+00:00"
    return p

def render(results, jobs, posts=(), new_posts=(), streaks=None, store=True, aborted=None):
    report = RunReport(results=list(results), aborted_reason=aborted)
    scored = [(j, scorer.score(j)) for j in jobs]
    return DigestRenderer(
        run_id=7, generated_at=NOW, report=report, scored=scored,
        posts=list(posts), new_post_keys={p.fingerprint() for p in new_posts},
        profile=profile, source_streaks=streaks or {}, store_enabled=store,
    ).render()

OK = SourceResult(source="indeed", status=SourceStatus.OK, scrape_calls=24, duration_seconds=310)
BAD = SourceResult(source="linkedin", status=SourceStatus.FAILED,
                   detail="connection reset by peer", scrape_calls=4)

print("\n-- a failed source is impossible to miss --")
md = render([OK, BAD], [job("Senior Product Manager, Payments", description="KYC AML licensing")])
check("failure has its own callout", "> **linkedin - FAILED.**" in md)
check("reason is shown", "connection reset by peer" in md)
check("appears above the listings", md.index("linkedin - FAILED") < md.index("Strong matches"))
check("healthy count in header", "1/2 sources healthy" in md)

print("\n-- a failure streak is stated, not left to be inferred --")
md = render([OK, BAD], [], streaks={"linkedin": 4})
check("streak reported", "failed 4 runs in a row" in md)

print("\n-- an abort is explained --")
md = render([OK], [], aborted="linkedin: 429 Response - Blocked")
check("abort callout", "> **Run aborted.**" in md)
check("says it did not retry", "rather than backing off and retrying" in md)

print("\n-- an expired session says how to fix itself --")
expired = SourceResult(source="linkedin_posts", status=SourceStatus.SKIPPED,
    detail="LinkedIn session expired or not set up - post search skipped. "
           "Re-authenticate with: uvx mcp-server-linkedin --login. Job sources are unaffected.")
md = render([OK, expired], [])
check("session callout present", "linkedin_posts - Session." in md)
check("renewal command included", "--login" in md)
check("posts section explains the gap", "Post search did not run" in md)

print("\n-- the required sections all exist --")
jobs = [
    job("Senior Product Manager, Regulatory & Licensing", "Rapyd", "Rehovot, Israel",
        "KYC AML licensing payments"),
    job("Product Manager, Payments", "Melio", description="payments compliance"),
    job("Product Manager", "Wix", description="consumer app roadmap"),
    job("Senior Software Engineer", "Fintech", description="python"),
    job("Product Manager, Payments", "LowPay", description="KYC",
        salary_min=20000, salary_max=25000, salary_currency="ILS", salary_interval="monthly"),
]
md = render([OK], jobs)
for section in ("## Run summary", "## Strong matches - new since last run",
                "## Worth a look", "## Hiring-signal posts",
                "## Stretch - likely resume-screen rejection", "## Filtered out"):
    check(f"has {section!r}", section in md)

print("\n-- stretch is collapsed, strong is not --")
check("stretch inside <details>", "<details><summary>Show long shots</summary>" in md)
strong_block = md[md.index("## Strong matches"):md.index("## Worth a look")]
check("strong roles are not collapsed", "<details>" not in strong_block)
check("stretch label not softened", "likely resume-screen rejection" in md)

print("\n-- filtered rows carry their reason --")
check("wrong-discipline reason", "not a product role" in md)
check("floor reason names the number", "below your 35,000 floor" in md)
check("filtered section explains its purpose", "can be tuned rather than guessed at" in md)

print("\n-- posts render with poster, company and link --")
posts = [
    HiringPost(source="linkedin_posts", text="We're hiring a Senior PM for payments!",
               url="https://linkedin.com/posts/1", poster_name="Dana Cohen",
               poster_headline="Talent Partner at Rapyd", company="Rapyd",
               posted_at="2 days ago", query="hiring pm fintech",
               language=detect_language("We're hiring a Senior PM for payments!", "en")),
    HiringPost(source="linkedin_posts", text="מגייסים מנהל מוצר לפינטק, רגולציה ותשלומים",
               url="https://linkedin.com/posts/2", poster_name="יוסי לוי", company="Pepper",
               query="hiring pm fintech",
               language=detect_language("מגייסים מנהל מוצר לפינטק, רגולציה ותשלומים", "en")),
]
md = render([OK], [], posts=posts, new_posts=posts[:1])
check("poster shown", "Dana Cohen - Rapyd" in md)
check("headline shown", "Talent Partner at Rapyd" in md)
check("post text quoted", "> We're hiring a Senior PM" in md)
check("direct link present", "[open post](https://linkedin.com/posts/1)" in md)
check("new post marked", "**NEW** Dana Cohen" in md)
check("hebrew post kept", "מגייסים מנהל מוצר" in md)
check("hebrew tagged he, not the query language", "[he]" in md)
check("english tagged en", "[en]" in md)

print("\n-- store disabled is disclosed --")
md = render([OK], [job("Product Manager, Payments", description="KYC")], store=False)
check("caveat rendered", "everything reads as new" in md)

print("\n-- markdown table injection is escaped --")
md = render([OK], [job("PM | Payments | Fraud", "Ev|il", description="python")])
check("pipes escaped in tables", "PM \\| Payments" in md)

print("\n-- writes to digests/YYYY-MM-DD.md --")
out = pathlib.Path(tempfile.mkdtemp()) / "digests"
path = write_digest(render([OK], []), out, NOW)
check("named by date", path.name == "2026-08-27.md")
check("directory created", path.parent.is_dir())
check("content round-trips", path.read_text(encoding="utf-8").startswith("# Job digest - 2026-08-27"))

print("\n-- empty run still produces a usable digest --")
md = render([SourceResult(source="indeed", status=SourceStatus.EMPTY, detail="no matches")], [])
check("no crash on empty", "## Run summary" in md)
check("says nothing new", "_Nothing new in this bucket today._" in md)

print("\n" + ("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
