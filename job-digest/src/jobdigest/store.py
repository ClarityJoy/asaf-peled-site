"""SQLite store: what we saw, when we first saw it, and how each run went.

JobSpy does no deduplication and no change tracking -- every run hands back
whatever the boards currently list, with no memory that yesterday's run
already showed you the same job. That memory is this module, and it is the
part of the tool worth owning locally.

Three questions it exists to answer:

  * is this posting new since the last run, or have I already seen it?
  * when did it first appear, and how many runs has it been up?
  * did a source fail, and has it been failing for several days?

Plain stdlib sqlite3. No ORM, no migrations framework -- schema version
lives in PRAGMA user_version and upgrades are a numbered list below.
"""

from __future__ import annotations

import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .models import HiringPost, JobPosting, SourceResult

SCHEMA_VERSION = 2

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS runs (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at         TEXT NOT NULL,
    finished_at        TEXT,
    aborted_reason     TEXT,
    postings_collected INTEGER NOT NULL DEFAULT 0,
    postings_unique    INTEGER NOT NULL DEFAULT 0,
    postings_new       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS source_runs (
    run_id           INTEGER NOT NULL REFERENCES runs(id),
    source           TEXT NOT NULL,
    status           TEXT NOT NULL,
    found            INTEGER NOT NULL DEFAULT 0,
    scrape_calls     INTEGER NOT NULL DEFAULT 0,
    duration_seconds REAL,
    detail           TEXT,
    PRIMARY KEY (run_id, source)
);

CREATE TABLE IF NOT EXISTS postings (
    fingerprint     TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    company         TEXT,
    location        TEXT,
    url             TEXT,
    direct_url      TEXT,
    source          TEXT,
    date_posted     TEXT,
    is_remote       INTEGER,
    job_type        TEXT,
    description     TEXT,
    salary_min      REAL,
    salary_max      REAL,
    salary_currency TEXT,
    salary_interval TEXT,
    first_seen_run  INTEGER NOT NULL,
    first_seen_at   TEXT NOT NULL,
    last_seen_run   INTEGER NOT NULL,
    last_seen_at    TEXT NOT NULL,
    times_seen      INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_postings_first_seen ON postings(first_seen_run);
CREATE INDEX IF NOT EXISTS idx_postings_last_seen  ON postings(last_seen_run);

-- One row per (run, posting, board). Keeps the fact that a job showed up on
-- both Indeed and LinkedIn, which the deduplicated posting row cannot.
CREATE TABLE IF NOT EXISTS sightings (
    run_id      INTEGER NOT NULL REFERENCES runs(id),
    fingerprint TEXT NOT NULL REFERENCES postings(fingerprint),
    source      TEXT NOT NULL,
    url         TEXT,
    query       TEXT,
    PRIMARY KEY (run_id, fingerprint, source)
);
"""


_SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS posts (
    fingerprint     TEXT PRIMARY KEY,
    text            TEXT NOT NULL,
    url             TEXT,
    poster_name     TEXT,
    poster_headline TEXT,
    company         TEXT,
    posted_at       TEXT,
    language        TEXT,
    source          TEXT,
    first_seen_run  INTEGER NOT NULL,
    first_seen_at   TEXT NOT NULL,
    last_seen_run   INTEGER NOT NULL,
    last_seen_at    TEXT NOT NULL,
    times_seen      INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_posts_first_seen ON posts(first_seen_run);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Classified:
    """This run's unique postings, split by whether we have seen them before."""

    new: list[JobPosting] = field(default_factory=list)
    returning: list[JobPosting] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.new) + len(self.returning)


@dataclass
class ClassifiedPosts:
    new: list[HiringPost] = field(default_factory=list)
    returning: list[HiringPost] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.new) + len(self.returning)


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    # -- schema ------------------------------------------------------------

    def _migrate(self) -> None:
        with closing(self.conn.cursor()) as cur:
            version = cur.execute("PRAGMA user_version").fetchone()[0]
            if version < 1:
                cur.executescript(_SCHEMA_V1)
            if version < 2:
                cur.executescript(_SCHEMA_V2)
            if version < SCHEMA_VERSION:
                cur.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            elif version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database at {self.path} is schema v{version}, but this "
                    f"code understands v{SCHEMA_VERSION}. Refusing to touch it."
                )
        self.conn.commit()

    # -- runs --------------------------------------------------------------

    def start_run(self) -> int:
        cur = self.conn.execute("INSERT INTO runs (started_at) VALUES (?)", (_now(),))
        self.conn.commit()
        return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        collected: int,
        unique: int,
        new: int,
        aborted_reason: str | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE runs SET finished_at = ?, aborted_reason = ?, "
            "postings_collected = ?, postings_unique = ?, postings_new = ? "
            "WHERE id = ?",
            (_now(), aborted_reason, collected, unique, new, run_id),
        )
        self.conn.commit()

    def record_source_result(self, run_id: int, result: SourceResult) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO source_runs "
            "(run_id, source, status, found, scrape_calls, duration_seconds, detail) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                run_id,
                result.source,
                result.status.value,
                result.count,
                result.scrape_calls,
                round(result.duration_seconds, 2),
                result.detail,
            ),
        )
        self.conn.commit()

    def previous_run_id(self, before_run_id: int) -> int | None:
        row = self.conn.execute(
            "SELECT id FROM runs WHERE id < ? AND finished_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (before_run_id,),
        ).fetchone()
        return int(row["id"]) if row else None

    def source_history(self, source: str, limit: int = 7) -> list[sqlite3.Row]:
        """Recent outcomes for one source, newest first.

        Lets the digest say "Indeed has failed three runs running" rather than
        reporting each failure as if it were the first.
        """
        return list(
            self.conn.execute(
                "SELECT r.id, r.started_at, s.status, s.found, s.detail "
                "FROM source_runs s JOIN runs r ON r.id = s.run_id "
                "WHERE s.source = ? ORDER BY r.id DESC LIMIT ?",
                (source, limit),
            )
        )

    # -- postings ----------------------------------------------------------

    def _is_known(self, fingerprint: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM postings WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return row is not None

    def upsert(self, run_id: int, posting: JobPosting) -> bool:
        """Insert or refresh one posting. Returns True if it is new to us.

        On an update, first_seen_* is deliberately never touched -- that is
        the whole point of the table -- and fields are only overwritten when
        the incoming record actually carries a value, so a board that returns
        a sparse row cannot blank out a description we already have.
        """
        fingerprint = posting.fingerprint()
        now = _now()
        known = self._is_known(fingerprint)

        if known:
            self.conn.execute(
                """
                UPDATE postings SET
                    title           = COALESCE(?, title),
                    company         = COALESCE(?, company),
                    location        = COALESCE(?, location),
                    url             = COALESCE(?, url),
                    direct_url      = COALESCE(?, direct_url),
                    date_posted     = COALESCE(?, date_posted),
                    is_remote       = COALESCE(?, is_remote),
                    job_type        = COALESCE(?, job_type),
                    description     = COALESCE(?, description),
                    salary_min      = COALESCE(?, salary_min),
                    salary_max      = COALESCE(?, salary_max),
                    salary_currency = COALESCE(?, salary_currency),
                    salary_interval = COALESCE(?, salary_interval),
                    last_seen_run   = ?,
                    last_seen_at    = ?,
                    times_seen      = times_seen + 1
                WHERE fingerprint = ?
                """,
                (
                    posting.title or None,
                    posting.company,
                    posting.location,
                    posting.url or None,
                    posting.direct_url,
                    posting.date_posted.isoformat() if posting.date_posted else None,
                    int(posting.is_remote) if posting.is_remote is not None else None,
                    posting.job_type,
                    posting.description,
                    posting.salary_min,
                    posting.salary_max,
                    posting.salary_currency,
                    posting.salary_interval,
                    run_id,
                    now,
                    fingerprint,
                ),
            )
        else:
            self.conn.execute(
                """
                INSERT INTO postings (
                    fingerprint, title, company, location, url, direct_url,
                    source, date_posted, is_remote, job_type, description,
                    salary_min, salary_max, salary_currency, salary_interval,
                    first_seen_run, first_seen_at, last_seen_run, last_seen_at,
                    times_seen
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)
                """,
                (
                    fingerprint,
                    posting.title,
                    posting.company,
                    posting.location,
                    posting.url,
                    posting.direct_url,
                    posting.source,
                    posting.date_posted.isoformat() if posting.date_posted else None,
                    int(posting.is_remote) if posting.is_remote is not None else None,
                    posting.job_type,
                    posting.description,
                    posting.salary_min,
                    posting.salary_max,
                    posting.salary_currency,
                    posting.salary_interval,
                    run_id,
                    now,
                    run_id,
                    now,
                ),
            )

        self.conn.execute(
            "INSERT OR REPLACE INTO sightings (run_id, fingerprint, source, url, query) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, fingerprint, posting.source, posting.url, posting.query),
        )
        return not known

    def classify(self, run_id: int, postings: list[JobPosting]) -> Classified:
        """Store this run's postings and split them into new vs returning.

        Annotates each posting with is_new / first_seen / times_seen so the
        digest can render "new since last run" without a second query.
        """
        result = Classified()
        for posting in postings:
            is_new = self.upsert(run_id, posting)
            row = self.conn.execute(
                "SELECT first_seen_at, times_seen FROM postings WHERE fingerprint = ?",
                (posting.fingerprint(),),
            ).fetchone()
            posting.is_new = is_new
            posting.first_seen = row["first_seen_at"] if row else None
            posting.times_seen = int(row["times_seen"]) if row else 1
            (result.new if is_new else result.returning).append(posting)
        self.conn.commit()
        return result

    # -- posts -------------------------------------------------------------

    def classify_posts(self, run_id: int, posts: list[HiringPost]) -> "ClassifiedPosts":
        result = ClassifiedPosts()
        for post in posts:
            fingerprint = post.fingerprint()
            now = _now()
            known = self.conn.execute(
                "SELECT 1 FROM posts WHERE fingerprint = ?", (fingerprint,)
            ).fetchone() is not None

            if known:
                self.conn.execute(
                    "UPDATE posts SET text = COALESCE(?, text), "
                    "url = COALESCE(?, url), poster_name = COALESCE(?, poster_name), "
                    "poster_headline = COALESCE(?, poster_headline), "
                    "company = COALESCE(?, company), posted_at = COALESCE(?, posted_at), "
                    "language = COALESCE(?, language), last_seen_run = ?, "
                    "last_seen_at = ?, times_seen = times_seen + 1 "
                    "WHERE fingerprint = ?",
                    (post.text or None, post.url or None, post.poster_name,
                     post.poster_headline, post.company, post.posted_at,
                     post.language, run_id, now, fingerprint),
                )
            else:
                self.conn.execute(
                    "INSERT INTO posts (fingerprint, text, url, poster_name, "
                    "poster_headline, company, posted_at, language, source, "
                    "first_seen_run, first_seen_at, last_seen_run, last_seen_at, "
                    "times_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)",
                    (fingerprint, post.text, post.url, post.poster_name,
                     post.poster_headline, post.company, post.posted_at,
                     post.language, post.source, run_id, now, run_id, now),
                )

            row = self.conn.execute(
                "SELECT first_seen_at, times_seen FROM posts WHERE fingerprint = ?",
                (fingerprint,),
            ).fetchone()
            post.is_new = not known
            post.first_seen = row["first_seen_at"] if row else None
            post.times_seen = int(row["times_seen"]) if row else 1
            (result.new if not known else result.returning).append(post)
        self.conn.commit()
        return result

    # -- reporting ---------------------------------------------------------

    def counts(self) -> dict[str, int]:
        get = lambda sql: int(self.conn.execute(sql).fetchone()[0])  # noqa: E731
        return {
            "runs": get("SELECT COUNT(*) FROM runs"),
            "postings": get("SELECT COUNT(*) FROM postings"),
            "sightings": get("SELECT COUNT(*) FROM sightings"),
            "posts": get("SELECT COUNT(*) FROM posts"),
        }
