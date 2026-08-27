"""Cached data for --dry-run.

Scoring rules are meant to be argued with, which means editing profile.yaml
and immediately seeing what moved. Doing that against live boards would be
both slow and a good way to get rate limited for no reason, so dry-run
replays data already collected: either the last completed run from the SQLite
store, or a JSON file exported from one.

Nothing in this module touches the network.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from .models import HiringPost, JobPosting
from .store import Store


def _as_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _posting_from_row(row: Any, source: str | None = None) -> JobPosting:
    posting = JobPosting(
        source=source or row["source"] or "fixture",
        title=row["title"],
        company=row["company"],
        location=row["location"],
        url=row["url"] or "",
        direct_url=row["direct_url"],
        date_posted=_as_date(row["date_posted"]),
        is_remote=bool(row["is_remote"]) if row["is_remote"] is not None else None,
        job_type=row["job_type"],
        description=row["description"],
        salary_min=row["salary_min"],
        salary_max=row["salary_max"],
        salary_currency=row["salary_currency"],
        salary_interval=row["salary_interval"],
    )
    posting.first_seen = row["first_seen_at"]
    posting.times_seen = row["times_seen"]
    return posting


def load_from_store(
    store: Store, run_id: int | None = None
) -> tuple[list[JobPosting], list[HiringPost], int | None]:
    """Replay one run. Defaults to the most recent completed one."""
    if run_id is None:
        row = store.conn.execute(
            "SELECT id FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1"
        ).fetchone()
        run_id = int(row["id"]) if row else None
    if run_id is None:
        return [], [], None

    postings: list[JobPosting] = []
    rows = store.conn.execute(
        "SELECT p.*, s.source AS sighting_source FROM postings p "
        "JOIN sightings s ON s.fingerprint = p.fingerprint "
        "WHERE s.run_id = ? GROUP BY p.fingerprint",
        (run_id,),
    ).fetchall()
    for row in rows:
        posting = _posting_from_row(row, row["sighting_source"])
        # Reproduce what that run reported rather than calling everything new.
        posting.is_new = row["first_seen_run"] == run_id
        postings.append(posting)

    posts: list[HiringPost] = []
    for row in store.conn.execute(
        "SELECT * FROM posts WHERE last_seen_run >= ?", (run_id,)
    ).fetchall():
        post = HiringPost(
            source=row["source"] or "linkedin_posts",
            text=row["text"], url=row["url"] or "",
            poster_name=row["poster_name"], poster_headline=row["poster_headline"],
            company=row["company"], posted_at=row["posted_at"], language=row["language"],
        )
        post.is_new = row["first_seen_run"] == run_id
        post.first_seen = row["first_seen_at"]
        post.times_seen = row["times_seen"]
        posts.append(post)

    return postings, posts, run_id


def export_json(
    path: Path, postings: list[JobPosting], posts: list[HiringPost], run_id: int | None
) -> Path:
    payload = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "run_id": run_id,
        "postings": [
            {
                "source": p.source, "title": p.title, "company": p.company,
                "location": p.location, "url": p.url, "direct_url": p.direct_url,
                "date_posted": p.date_posted.isoformat() if p.date_posted else None,
                "is_remote": p.is_remote, "job_type": p.job_type,
                "description": p.description, "salary_min": p.salary_min,
                "salary_max": p.salary_max, "salary_currency": p.salary_currency,
                "salary_interval": p.salary_interval, "is_new": p.is_new,
                "first_seen": p.first_seen, "times_seen": p.times_seen,
            }
            for p in postings
        ],
        "posts": [
            {
                "source": p.source, "text": p.text, "url": p.url,
                "poster_name": p.poster_name, "poster_headline": p.poster_headline,
                "company": p.company, "posted_at": p.posted_at,
                "language": p.language, "is_new": p.is_new,
            }
            for p in posts
        ],
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def load_json(path: Path) -> tuple[list[JobPosting], list[HiringPost], int | None]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    postings = []
    for record in data.get("postings", []):
        posting = JobPosting(
            source=record.get("source", "fixture"), title=record.get("title", ""),
            url=record.get("url", ""), company=record.get("company"),
            location=record.get("location"), direct_url=record.get("direct_url"),
            date_posted=_as_date(record.get("date_posted")),
            is_remote=record.get("is_remote"), job_type=record.get("job_type"),
            description=record.get("description"), salary_min=record.get("salary_min"),
            salary_max=record.get("salary_max"),
            salary_currency=record.get("salary_currency"),
            salary_interval=record.get("salary_interval"),
        )
        posting.is_new = record.get("is_new")
        posting.first_seen = record.get("first_seen")
        posting.times_seen = record.get("times_seen")
        postings.append(posting)

    posts = []
    for record in data.get("posts", []):
        post = HiringPost(
            source=record.get("source", "linkedin_posts"), text=record.get("text", ""),
            url=record.get("url", ""), poster_name=record.get("poster_name"),
            poster_headline=record.get("poster_headline"), company=record.get("company"),
            posted_at=record.get("posted_at"), language=record.get("language"),
        )
        post.is_new = record.get("is_new")
        posts.append(post)

    return postings, posts, data.get("run_id")
