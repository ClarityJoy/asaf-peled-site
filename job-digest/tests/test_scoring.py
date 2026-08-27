"""Scoring against the real profile.yaml, on postings shaped like the real thing."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from jobdigest.config import Config
from jobdigest.models import JobPosting
from jobdigest.scoring import Scorer, BUCKET_STRONG, BUCKET_WORTH, BUCKET_STRETCH

FAILS = []
def check(label, cond):
    print(f"  {'ok ' if cond else 'FAIL'} {label}")
    if not cond: FAILS.append(label)

scorer = Scorer(Config.load().profile)

def mk(title, company="ACME", location="Tel Aviv, Israel", description=None, **kw):
    return JobPosting(source="indeed", title=title, company=company,
                      location=location, description=description,
                      url="http://x", **kw)

def show(label, posting):
    s = scorer.score(posting)
    flag = " [FILTERED]" if s.filtered else ""
    print(f"\n  {label}")
    print(f"    {posting.title}  @ {posting.location}")
    print(f"    score {s.total:3d}  bucket: {s.bucket_label}{flag}")
    print(f"    {s.component_line()}")
    if s.penalties: print(f"    penalties: {', '.join(s.penalties)}")
    if s.filter_reason: print(f"    filtered: {s.filter_reason}")
    if s.salary_note: print(f"    salary: {s.salary_note}")
    print(f"    reason: {s.reason}")
    return s

print("\n=== the bullseye ===")
s = show("regulatory PM, fintech, near home", mk(
    "Senior Product Manager, Regulatory & Licensing",
    location="Rehovot, Israel",
    description="Own KYC and AML product for a payments platform. Licensing across jurisdictions."))
check("bullseye is strong", s.bucket == BUCKET_STRONG)
check("no penalties", s.penalties == [])
check("domain scores high", s.components[0].points >= 35)

print("\n=== the three shapes to score down ===")
s = show("generic PM, no reg surface", mk(
    "Product Manager",
    description="Own the roadmap for our consumer app. Work with design and engineering."))
check("generic PM penalised", "no_domain_surface" in s.penalties)
check("generic PM is not strong", s.bucket != BUCKET_STRONG)
check("reason names the resume screen",
      "resume screen" in s.reason.lower() or "generic" in s.reason.lower())

s = show("large-team people management", mk(
    "VP Product",
    description="Hire and manage a team of 20 product managers across payments and compliance."))
check("large team penalised", "large_team_management" in s.penalties)

s = show("compliance analyst dressed as product", mk(
    "KYC Analyst",
    description="Review customer onboarding files and escalate AML alerts."))
check("analyst penalised", "compliance_analyst" in s.penalties)
check("analyst is a stretch", s.bucket == BUCKET_STRETCH)

print("\n=== hard filters ===")
s = show("not a product role at all", mk(
    "Senior Software Engineer", description="Python, payments, KYC systems."))
check("wrong discipline filtered", s.filtered)
check("filter reason given", s.filter_reason == "not a product role")

print("\n=== income floor (35,000 ILS/month) ===")
s = show("below floor", mk(
    "Product Manager, Payments", description="KYC and AML.",
    salary_min=20000, salary_max=25000, salary_currency="ILS", salary_interval="monthly"))
check("below floor filtered", s.filtered)
check("floor reason names the number", "35,000" in (s.filter_reason or ""))

s = show("above floor", mk(
    "Product Manager, Payments", description="KYC and AML.",
    salary_min=40000, salary_max=48000, salary_currency="ILS", salary_interval="monthly"))
check("above floor not filtered", not s.filtered)
check("clears-floor note", "clears the floor" in (s.salary_note or ""))

s = show("USD yearly converted", mk(
    "Product Manager, Payments", description="KYC and AML.",
    salary_min=150000, salary_max=180000, salary_currency="USD", salary_interval="yearly"))
check("USD yearly clears floor", not s.filtered)

s = show("no salary stated (the common case)", mk(
    "Product Manager, Payments", description="KYC and AML."))
check("no salary -> not filtered", not s.filtered)
check("no salary -> no note", s.salary_note is None)

print("\n=== commute model ===")
near = scorer.score(mk("Senior Product Manager, Payments", location="Rishon LeZion, Israel", description="KYC"))
tlv  = scorer.score(mk("Senior Product Manager, Payments", location="Tel Aviv, Israel", description="KYC"))
tlv_h= scorer.score(mk("Senior Product Manager, Payments", location="Tel Aviv, Israel", description="KYC. Hybrid, 2 days from home."))
far  = scorer.score(mk("Senior Product Manager, Payments", location="Haifa, Israel", description="KYC"))
rem  = scorer.score(mk("Senior Product Manager, Payments", location="Israel", description="KYC", is_remote=True))
print(f"    near {near.components[3].points:.0f} | tel aviv {tlv.components[3].points:.0f} | "
      f"tel aviv+hybrid {tlv_h.components[3].points:.0f} | haifa {far.components[3].points:.0f} | remote {rem.components[3].points:.0f}")
check("near beats tel aviv", near.components[3].points > tlv.components[3].points)
check("hybrid rescues tel aviv", tlv_h.components[3].points > tlv.components[3].points)
check("haifa worst of the commutes", far.components[3].points < tlv.components[3].points)
check("remote is full marks", rem.components[3].points == 15)

print("\n=== thin evidence is admitted ===")
s = show("title only, no description (LinkedIn/Bayt)", mk("Product Manager, Payments", description=None))
check("thin flagged", s.thin_evidence)
check("reason admits it", "title only" in s.reason)

print("\n=== hebrew title ===")
s = show("hebrew product manager", mk("מנהל מוצר פינטק", location="תל אביב", description="רגולציה ותשלומים"))
check("hebrew role recognised", s.components[2].points > 0)
check("hebrew domain recognised", s.components[0].points > 0)
check("hebrew role not penalised as generic", "no_domain_surface" not in s.penalties)
check("hebrew role clears stretch", s.bucket != BUCKET_STRETCH)

s2 = show("hebrew compliance analyst still scored down", mk("אנליסט ציות", description="בדיקת לקוחות"))
check("hebrew analyst penalised", "compliance_analyst" in s2.penalties)

print("\n=== bucket ordering sanity ===")
check("strong > worth > stretch thresholds",
      scorer.buckets["strong"] > scorer.buckets["worth_a_look"])
check("stretch label is not softened",
      "likely resume-screen rejection" in scorer.score(mk("Product Manager")).bucket_label)

print("\n=== domain has to dominate ===")
# The failure mode to guard: a generic PM role scoring 'strong' purely on
# being senior, nearby and a product title. That is the exact shape that has
# been getting cut at the resume screen, so it must never read as strong.
perfect_but_generic = scorer.score(mk(
    "Senior Product Manager", location="Rishon LeZion, Israel",
    description="Own the roadmap for our consumer app. Work with design."))
print(f"    generic PM, senior, on his doorstep -> {perfect_but_generic.total} "
      f"({perfect_but_generic.bucket_label})")
check("generic PM cannot reach strong even with everything else perfect",
      perfect_but_generic.bucket != BUCKET_STRONG)

near_weak = scorer.score(mk("Product Manager, Banking", location="Rehovot, Israel",
                            description="Consumer banking app."))
far_strong = scorer.score(mk("Product Manager, Regulatory Licensing", location="Haifa, Israel",
                             description="KYC, AML, licensing, payments, compliance."))
print(f"    weak domain nearby {near_weak.total} vs strong domain far away {far_strong.total}")
check("strong domain far away beats weak domain nearby",
      far_strong.total > near_weak.total)

print("\n" + ("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
