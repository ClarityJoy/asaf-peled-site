"""Render a run into digests/YYYY-MM-DD.md.

The ordering is deliberate: what broke comes first, then what is new and
worth acting on, then everything else, then what was thrown away and why.

A digest that quietly omits a dead source is worse than no digest, so the run
summary leads even when everything worked, and a failing source gets a
callout rather than a row in a table nobody reads to the bottom of.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .models import HiringPost, JobPosting, SourceStatus
from .runner import RunReport
from .scoring import BUCKET_STRETCH, BUCKET_STRONG, BUCKET_WORTH, Score

STATUS_ICON = {
    SourceStatus.OK: "ok",
    SourceStatus.EMPTY: "empty",
    SourceStatus.FAILED: "**FAILED**",
    SourceStatus.BLOCKED: "**BLOCKED**",
    SourceStatus.SKIPPED: "skipped",
}


def _cell(text: Any) -> str:
    """Make a value safe to drop in a markdown table cell."""
    if text is None:
        return "-"
    return str(text).replace("|", "\\|").replace("\n", " ").strip() or "-"


def _posted(posting: JobPosting) -> str:
    return posting.date_posted.isoformat() if posting.date_posted else "date unknown"


def _link(posting: JobPosting) -> str:
    url = posting.direct_url or posting.url
    return f"[open posting]({url})" if url else "no link"


class DigestRenderer:
    def __init__(
        self,
        *,
        run_id: int | None,
        generated_at: datetime,
        report: RunReport,
        scored: list[tuple[JobPosting, Score]],
        posts: list[HiringPost],
        new_post_keys: set[str],
        profile: dict[str, Any],
        source_streaks: dict[str, int] | None = None,
        store_enabled: bool = True,
    ) -> None:
        self.run_id = run_id
        self.generated_at = generated_at
        self.report = report
        self.scored = scored
        self.posts = posts
        self.new_post_keys = new_post_keys
        self.profile = profile
        self.source_streaks = source_streaks or {}
        self.store_enabled = store_enabled

        self.kept = [pair for pair in scored if not pair[1].filtered]
        self.dropped = [pair for pair in scored if pair[1].filtered]

    # -- helpers -----------------------------------------------------------

    def _bucket(self, bucket: str, new: bool | None = None):
        pairs = [p for p in self.kept if p[1].bucket == bucket]
        if new is not None:
            pairs = [p for p in pairs if bool(p[0].is_new) is new]
        return sorted(pairs, key=lambda pair: -pair[1].total)

    # -- sections ----------------------------------------------------------

    def _header(self) -> list[str]:
        healthy = sum(1 for r in self.report.results if not r.status.is_problem)
        return [
            f"# Job digest - {self.generated_at:%Y-%m-%d}",
            "",
            f"_Generated {self.generated_at:%Y-%m-%d %H:%M}"
            + (f" · run #{self.run_id}" if self.run_id else "")
            + f" · {healthy}/{len(self.report.results)} sources healthy_",
            "",
        ]

    def _run_summary(self) -> list[str]:
        lines = ["## Run summary", "",
                 "| Source | Status | Found | Calls | Time |",
                 "| --- | --- | ---: | ---: | ---: |"]
        for result in self.report.results:
            lines.append(
                f"| {_cell(result.source)} | {STATUS_ICON.get(result.status, result.status.value)} "
                f"| {result.count} | {result.scrape_calls} "
                f"| {result.duration_seconds:.0f}s |"
            )
        lines.append("")

        problems = [r for r in self.report.results
                    if r.status.is_problem or r.status == SourceStatus.SKIPPED]
        for result in problems:
            if result.status == SourceStatus.SKIPPED and not result.detail:
                continue
            streak = self.source_streaks.get(result.source, 0)
            streak_note = (f" This has now failed {streak} runs in a row."
                           if streak > 1 else "")
            label = ("Session" if result.status == SourceStatus.SKIPPED
                     else result.status.value.upper())
            lines.append(f"> **{_cell(result.source)} - {label}.** "
                         f"{_cell(result.detail)}{streak_note}")
            lines.append("")

        if self.report.aborted_reason:
            lines += [
                f"> **Run aborted.** {_cell(self.report.aborted_reason)}",
                ">",
                "> A board rate-limited or challenged us. The run stopped rather "
                "than backing off and retrying, and sources after it did not run.",
                "",
            ]

        total = len(self.report.postings)
        unique = len(self.scored)
        new = sum(1 for p, _ in self.scored if p.is_new)
        line = (f"{total} collected · {unique} unique "
                f"({total - unique} cross-board duplicates) · {new} new since last run")
        if not self.store_enabled:
            line += "  \n_Store disabled for this run, so everything reads as new._"
        return lines + [line, ""]

    def _job_entry(self, posting: JobPosting, score: Score) -> list[str]:
        lines = [
            f"### {_cell(posting.title)} - {_cell(posting.company or 'unknown company')}",
            "",
            f"**{score.total}/100** · " + " · ".join(str(c) for c in score.components),
            "",
            score.reason,
            "",
        ]
        if score.salary_note:
            lines += [f"_{score.salary_note}_", ""]
        seen = ""
        if posting.times_seen and posting.times_seen > 1:
            seen = f" · seen in {posting.times_seen} runs since {(posting.first_seen or '')[:10]}"
        if score.penalties:
            lines += [f"_Scored down for: {', '.join(score.penalties)}_", ""]
        lines += [
            f"{_cell(posting.display_location)} · posted {_posted(posting)} · "
            f"via {posting.source}{seen} · {_link(posting)}",
            "",
        ]
        return lines

    def _compact_table(self, pairs) -> list[str]:
        lines = ["| Score | Role | Company | Where | Source | Link |",
                 "| ---: | --- | --- | --- | --- | --- |"]
        for posting, score in pairs:
            url = posting.direct_url or posting.url
            link = f"[open]({url})" if url else "-"
            lines.append(
                f"| {score.total} | {_cell(posting.title)} | "
                f"{_cell(posting.company)} | {_cell(posting.display_location)} | "
                f"{posting.source} | {link} |"
            )
        return lines + [""]

    def _strong(self) -> list[str]:
        new = self._bucket(BUCKET_STRONG, new=True)
        old = self._bucket(BUCKET_STRONG, new=False)
        lines: list[str] = []

        lines += [f"## Strong matches - new since last run ({len(new)})", ""]
        if new:
            for posting, score in new:
                lines += self._job_entry(posting, score)
        else:
            lines += ["_Nothing new in this bucket today._", ""]

        if old:
            lines += [f"## Strong matches - already seen ({len(old)})", "",
                      "<details><summary>Still listed from earlier runs</summary>", ""]
            lines += self._compact_table(old)
            lines += ["</details>", ""]
        return lines

    def _worth(self) -> list[str]:
        pairs = self._bucket(BUCKET_WORTH)
        lines = [f"## Worth a look ({len(pairs)})", ""]
        if not pairs:
            return lines + ["_Nothing in this bucket today._", ""]
        new = [p for p in pairs if p[0].is_new]
        old = [p for p in pairs if not p[0].is_new]
        for posting, score in new:
            lines += self._job_entry(posting, score)
        if old:
            lines += ["<details><summary>"
                      f"{len(old)} already seen in earlier runs</summary>", ""]
            lines += self._compact_table(old)
            lines += ["</details>", ""]
        return lines

    def _posts(self) -> list[str]:
        new_count = sum(1 for p in self.posts if p.fingerprint() in self.new_post_keys)
        lines = [f"## Hiring-signal posts ({len(self.posts)}, {new_count} new)", ""]
        if not self.posts:
            skipped = [r for r in self.report.results
                       if r.source == "linkedin_posts"
                       and r.status != SourceStatus.OK]
            if skipped:
                lines += [f"_Post search did not run: {_cell(skipped[0].detail)}_", ""]
            else:
                lines += ["_No hiring posts matched in the window._", ""]
            return lines

        lines += ["Informal posts, unscored. These often appear before a formal "
                  "listing exists.", ""]
        ordered = sorted(self.posts,
                         key=lambda p: p.fingerprint() not in self.new_post_keys)
        for post in ordered:
            marker = "**NEW** " if post.fingerprint() in self.new_post_keys else ""
            who = post.poster_name or "unknown poster"
            if post.company:
                who += f" - {post.company}"
            lines.append(f"### {marker}{_cell(who)}")
            lines.append("")
            if post.poster_headline:
                lines += [f"_{_cell(post.poster_headline)}_", ""]
            lines += ["> " + (post.summary or "(no text)"), ""]
            meta = f"[{post.language or '??'}] query: `{_cell(post.query)}`"
            if post.posted_at:
                meta += f" · {_cell(post.posted_at)}"
            if post.url:
                meta += f" · [open post]({post.url})"
            lines += [meta, ""]
        return lines

    def _stretch(self) -> list[str]:
        pairs = self._bucket(BUCKET_STRETCH)
        lines = [f"## Stretch - likely resume-screen rejection ({len(pairs)})", ""]
        if not pairs:
            return lines + ["_None today._", ""]
        lines += [
            "<details><summary>Show long shots</summary>",
            "",
            "These score low against the profile. Listed for completeness, not "
            "because they look promising.",
            "",
        ]
        lines += self._compact_table(pairs)
        lines += ["</details>", ""]
        return lines

    def _filtered(self) -> list[str]:
        lines = [f"## Filtered out ({len(self.dropped)})", ""]
        if not self.dropped:
            return lines + ["_Nothing was filtered out this run._", ""]
        lines += ["Removed before scoring mattered. Listed so the rules in "
                  "`config/profile.yaml` can be tuned rather than guessed at.", ""]
        lines += ["| Role | Company | Why it was dropped |", "| --- | --- | --- |"]
        for posting, score in self.dropped:
            lines.append(f"| {_cell(posting.title)} | {_cell(posting.company)} | "
                         f"{_cell(score.filter_reason)} |")
        return lines + [""]

    def _footer(self) -> list[str]:
        comp = self.profile.get("compensation") or {}
        floor = comp.get("income_floor_monthly_ils")
        floor_line = (
            f"Income floor {float(floor):,.0f} ILS/month. Only postings that "
            f"**state** a salary below it are filtered; most state none."
            if floor else
            "No income floor set in `config/profile.yaml`, so none was applied."
        )
        return [
            "---",
            "",
            "### Tuning this",
            "",
            floor_line,
            "",
            "Scoring weights, keywords and commute tiers live in "
            "`config/profile.yaml`. Search terms live in `config/queries.yaml`. "
            "Per-board budgets live in `config/sources.yaml`. "
            "Change one and re-run with `--dry-run` to see the effect without "
            "touching any live service.",
            "",
        ]

    def render(self) -> str:
        parts: list[str] = []
        for section in (self._header, self._run_summary, self._strong, self._worth,
                        self._posts, self._stretch, self._filtered, self._footer):
            parts.extend(section())
        return "\n".join(parts).rstrip() + "\n"


def write_digest(markdown: str, directory: Path, when: datetime) -> Path:
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{when:%Y-%m-%d}.md"
    path.write_text(markdown, encoding="utf-8")
    return path
