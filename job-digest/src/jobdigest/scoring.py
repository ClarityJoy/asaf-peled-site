"""Deterministic, rule-based fit scoring. No LLM anywhere in here.

Every number this module produces can be traced to a keyword list or a weight
in profile.yaml, which is the point: the rules are meant to be read and argued
with, not trusted. Change a weight, re-run, see what moves.

Four components, weighted in profile.yaml and reported separately so a total
of 60 can be read as "great domain, wrong commute" rather than a bare number:

    domain fit      does it touch regulation, payments or compliance at all
    seniority fit   senior IC or small-team lead, not junior and not a VP
    role type fit   is it actually a product role
    location fit    reachable from Rishon LeZion, or remote enough not to matter

Penalties are subtracted from the total afterwards rather than folded into a
component, so the digest can name them out loud.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .models import JobPosting

BUCKET_STRONG = "strong"
BUCKET_WORTH = "worth a look"
BUCKET_STRETCH = "stretch"

# Deliberately not softened. A digest where everything looks promising is
# worse than no digest.
BUCKET_LABELS = {
    BUCKET_STRONG: "strong",
    BUCKET_WORTH: "worth a look",
    BUCKET_STRETCH: "stretch - likely resume-screen rejection",
}


@dataclass
class Component:
    name: str
    points: float
    max_points: float
    notes: list[str] = field(default_factory=list)

    def __str__(self) -> str:
        return f"{self.name} {self.points:.0f}/{self.max_points:.0f}"


@dataclass
class Score:
    total: int
    components: list[Component]
    bucket: str
    reason: str
    penalties: list[str] = field(default_factory=list)
    filtered: bool = False
    filter_reason: str | None = None
    salary_note: str | None = None
    thin_evidence: bool = False

    @property
    def bucket_label(self) -> str:
        return BUCKET_LABELS[self.bucket]

    def component_line(self) -> str:
        return "  ".join(str(c) for c in self.components)


def _contains(haystack: str, needle: str) -> bool:
    """Word-boundary match for ASCII terms, plain substring for Hebrew.

    "aml" must not match "amling", but Hebrew has no \b that behaves, so
    Hebrew terms fall back to substring matching.
    """
    if needle.isascii():
        return re.search(rf"(?<![a-z0-9]){re.escape(needle)}(?![a-z0-9])", haystack) is not None
    return needle in haystack


class Scorer:
    def __init__(self, profile: dict[str, Any]) -> None:
        self.profile = profile
        scoring = profile.get("scoring") or {}
        self.weights = scoring.get("weights") or {}
        self.buckets = scoring.get("buckets") or {}
        self.positive = scoring.get("positive_keywords") or {}
        self.negative = scoring.get("negative_keywords") or {}
        self.penalty_points = scoring.get("penalties") or {}

        location = profile.get("location") or {}
        self.tiers = location.get("commute_tiers") or {}
        self.hybrid_markers = [m.lower() for m in location.get("hybrid_markers", [])]

        comp = profile.get("compensation") or {}
        self.floor = comp.get("income_floor_monthly_ils")
        self.fx = comp.get("fx_to_ils") or {"ILS": 1.0}

        targets = (profile.get("target_roles") or {}).get("titles", [])
        self.target_titles = [t.lower() for t in targets]

    # -- helpers -----------------------------------------------------------

    def _weight(self, name: str, default: float) -> float:
        return float(self.weights.get(name, default))

    def _monthly_ils(self, posting: JobPosting) -> float | None:
        """Best-effort conversion of a posting's salary to monthly ILS."""
        amount = posting.salary_max or posting.salary_min
        if not amount:
            return None
        rate = float(self.fx.get((posting.salary_currency or "ILS").upper(), 0) or 0)
        if not rate:
            return None
        value = float(amount) * rate
        interval = (posting.salary_interval or "").lower()
        if interval in ("yearly", "annual", "year"):
            return value / 12
        if interval in ("weekly", "week"):
            return value * 4.33
        if interval in ("daily", "day"):
            return value * 21
        if interval in ("hourly", "hour"):
            return value * 182
        return value  # assume monthly

    # -- components --------------------------------------------------------

    def _domain(self, text: str, title: str) -> tuple[Component, bool]:
        cap = self._weight("domain_fit", 45)
        strong = [k.lower() for k in self.positive.get("strong", [])]
        moderate = [k.lower() for k in self.positive.get("moderate", [])]

        points = 0.0
        hits: list[str] = []
        for term in strong:
            if _contains(title, term):
                points += 12; hits.append(term)
            elif _contains(text, term):
                points += 5; hits.append(term)
        for term in moderate:
            if _contains(title, term):
                points += 6; hits.append(term)
            elif _contains(text, term):
                points += 2; hits.append(term)

        if hits:
            unique_hits = list(dict.fromkeys(hits))[:6]
            notes = [f"matched: {', '.join(unique_hits)}"]
        else:
            notes = ["no regulatory, payments or compliance surface"]
        return Component("domain", min(points, cap), cap, notes), not hits

    def _seniority(self, text: str, title: str) -> tuple[Component, list[str]]:
        cap = self._weight("seniority_fit", 20)
        penalties: list[str] = []
        points = cap * 0.5   # neutral starting point

        if any(_contains(title, t) for t in ("senior", "principal", "lead", "staff")):
            points = cap
        elif any(t in title for t in self.target_titles):
            points = cap * 0.8

        if any(_contains(title, t) for t in ("junior", "associate", "intern", "entry level", "graduate")):
            points = 0
            penalties.append("too_junior")

        large = [k.lower() for k in self.negative.get("large_team_management", [])]
        if any(_contains(title, t) or _contains(text, t) for t in large):
            penalties.append("large_team_management")
            if any(_contains(title, t) for t in ("vp", "vice president", "chief", "cpo", "director")):
                penalties.append("too_senior")

        return Component("seniority", points, cap, []), penalties

    def _role_type(self, text: str, title: str) -> tuple[Component, list[str], str | None]:
        cap = self._weight("role_type_fit", 20)
        penalties: list[str] = []
        filter_reason = None
        points = 0.0

        product_markers = ("product manager", "product owner", "product operations",
                           "product ops", "product lead", "group product", "מנהל מוצר",
                           "מנהלת מוצר", "product management")
        manager_markers = ("regulatory manager", "compliance manager", "technology manager",
                           "program manager", "delivery manager")

        if any(m in title for m in product_markers):
            points = cap
        elif any(m in title for m in manager_markers):
            points = cap * 0.6
        elif any(m in text for m in product_markers):
            points = cap * 0.4

        analyst = [k.lower() for k in self.negative.get("compliance_analyst_dressed_as_product", [])]
        if any(m in title for m in analyst):
            penalties.append("compliance_analyst")
            points = 0

        wrong = [k.lower() for k in self.negative.get("wrong_discipline", [])]
        if any(m in title for m in wrong) and not any(m in title for m in product_markers):
            penalties.append("wrong_discipline")
            points = 0
            filter_reason = "not a product role"

        return Component("role type", points, cap, []), penalties, filter_reason

    def _location(self, posting: JobPosting, text: str) -> Component:
        cap = self._weight("location_fit", 15)
        where = (posting.location or "").lower()
        hybrid = any(m in text for m in self.hybrid_markers)

        if posting.is_remote:
            return Component("location", cap, cap, ["remote"])

        def tier_of(name: str) -> str | None:
            for tier in ("near", "medium", "far"):
                for place in self.tiers.get(tier, []):
                    if place.lower() in name:
                        return tier
            return None

        tier = tier_of(where)
        if tier == "near":
            return Component("location", cap, cap, ["within commute range"])
        if tier == "medium":
            if hybrid:
                return Component("location", cap, cap, ["beyond 30min, but hybrid"])
            return Component("location", cap * 0.55, cap,
                             ["beyond 30min in peak; no WFH days mentioned"])
        if tier == "far":
            if hybrid:
                return Component("location", cap * 0.65, cap, ["far, but hybrid"])
            return Component("location", cap * 0.2, cap,
                             ["far commute, no WFH days mentioned"])
        if hybrid:
            return Component("location", cap * 0.7, cap, ["location unclear, hybrid mentioned"])
        return Component("location", cap * 0.45, cap, ["location unclear"])

    # -- entry point -------------------------------------------------------

    def score(self, posting: JobPosting) -> Score:
        title = (posting.title or "").lower()
        description = (posting.description or "").lower()
        text = f"{title} {description}"

        domain, no_domain = self._domain(text, title)
        seniority, sen_pen = self._seniority(text, title)
        role, role_pen, filter_reason = self._role_type(text, title)
        location = self._location(posting, text)

        components = [domain, seniority, role, location]
        penalties = list(sen_pen) + list(role_pen)

        # A product role with no regulatory, payments or compliance surface is
        # the exact shape that has been getting cut at the resume screen.
        if no_domain and role.points > 0:
            penalties.append("no_domain_surface")

        subtotal = sum(c.points for c in components)
        deduction = sum(float(self.penalty_points.get(p, 0)) for p in dict.fromkeys(penalties))
        total = int(max(0, min(100, round(subtotal - deduction))))

        # Salary floor. Applied only when the posting actually states a salary,
        # which most Israeli postings do not.
        filtered = filter_reason is not None
        salary_note = None
        monthly = self._monthly_ils(posting)
        if self.floor and monthly is not None:
            if monthly < float(self.floor):
                filtered = True
                filter_reason = (
                    f"stated pay ~{monthly:,.0f} ILS/month is below your "
                    f"{float(self.floor):,.0f} floor"
                )
            else:
                salary_note = f"stated pay ~{monthly:,.0f} ILS/month clears the floor"
        elif self.floor and (posting.salary_min or posting.salary_max):
            salary_note = "salary stated in an unrecognised currency; floor not applied"

        strong_at = float(self.buckets.get("strong", 70))
        worth_at = float(self.buckets.get("worth_a_look", 45))
        bucket = (BUCKET_STRONG if total >= strong_at
                  else BUCKET_WORTH if total >= worth_at
                  else BUCKET_STRETCH)

        # Descriptions are only fetched from Indeed, so LinkedIn and Bayt rows
        # are scored on their title alone. Say so rather than implying the
        # score saw more than it did.
        thin = not description
        return Score(
            total=total,
            components=components,
            bucket=bucket,
            reason=self._reason(posting, domain, role, location, penalties, bucket, thin),
            penalties=list(dict.fromkeys(penalties)),
            filtered=filtered,
            filter_reason=filter_reason,
            salary_note=salary_note,
            thin_evidence=thin,
        )

    def _reason(self, posting, domain, role, location, penalties, bucket, thin) -> str:
        """One plain-language line. Leads with whatever dominates the score."""
        if "wrong_discipline" in penalties:
            return "Not a product role."
        if "compliance_analyst" in penalties:
            return "Compliance analyst role, not product - the shape you said to score down."
        if "no_domain_surface" in penalties:
            base = "Generic PM role with no regulatory, payments or compliance surface."
            if bucket == BUCKET_STRETCH:
                base += " This is the pattern that has been getting cut at the resume screen."
            return base

        bits: list[str] = []
        if domain.notes and domain.notes[0].startswith("matched"):
            bits.append(domain.notes[0].replace("matched:", "Touches"))
        if "large_team_management" in penalties:
            bits.append("but it is a large-team people-management role")
        if "too_junior" in penalties:
            bits.append("and it is pitched below your level")
        if location.notes:
            bits.append(location.notes[0])
        if thin:
            bits.append("scored on title only - no description from this board")
        return "; ".join(bits).capitalize() + "." if bits else "No strong signal either way."
