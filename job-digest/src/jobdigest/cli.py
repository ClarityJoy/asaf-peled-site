"""Entry point: run the job sources, remember what we saw, print the result."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import Config, ConfigError
from .models import JobPosting, SourceStatus
from .runner import run_sources
from .scoring import BUCKET_STRETCH, BUCKET_STRONG, BUCKET_WORTH, Score, Scorer
from .store import Store

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB = PROJECT_ROOT / "state" / "jobs.db"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobdigest",
        description="Daily job digest - collection and cross-run dedup.",
    )
    parser.add_argument(
        "--source", action="append", dest="sources",
        help="only run this source (repeatable). Default: all enabled.",
    )
    parser.add_argument(
        "--config-dir", type=Path, default=None,
        help="directory holding profile.yaml, queries.yaml, sources.yaml",
    )
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB,
        help=f"SQLite store for cross-run dedup (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--no-store", action="store_true",
        help="do not read or write the database. Everything looks new.",
    )
    parser.add_argument(
        "--max-queries", type=int, default=None,
        help="cap queries per source, for a quick look",
    )
    parser.add_argument(
        "--no-pacing", action="store_true",
        help="skip inter-request delays. Local testing only - never in cron.",
    )
    parser.add_argument("--limit", type=int, default=40, help="rows to print")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser


def _sort_key(pair: tuple[JobPosting, Score]):
    posting, score = pair
    # Highest score first; within a score, newly-seen before already-seen.
    return (-score.total, not bool(posting.is_new))


def _print_group(heading: str, pairs: list[tuple[JobPosting, Score]], limit: int) -> None:
    if not pairs:
        return
    print("\n" + "=" * 78)
    print(f"{heading} ({len(pairs)})")
    print("=" * 78)
    for posting, score in sorted(pairs, key=_sort_key)[:limit]:
        marker = "NEW  " if posting.is_new else "seen "
        when = posting.date_posted.isoformat() if posting.date_posted else "date unknown"
        print(f"\n  [{score.total:3d}] {marker}{posting.title}")
        print(f"        {posting.company or 'unknown company'} - {posting.display_location}")
        print(f"        {score.component_line()}")
        print(f"        {score.reason}")
        if score.salary_note:
            print(f"        {score.salary_note}")
        age = ""
        if posting.times_seen and posting.times_seen > 1:
            age = f" - seen in {posting.times_seen} runs since {(posting.first_seen or '')[:10]}"
        print(f"        {when} - via {posting.source}{age}")
        print(f"        {posting.url}")
    if len(pairs) > limit:
        print(f"\n  ... and {len(pairs) - limit} more")


def _print_stretch(pairs: list[tuple[JobPosting, Score]], limit: int) -> None:
    """Collapsed: titles only. These are long shots and should read that way."""
    if not pairs:
        return
    print("\n" + "=" * 78)
    print(f"STRETCH - LIKELY RESUME-SCREEN REJECTION ({len(pairs)})")
    print("=" * 78)
    for posting, score in sorted(pairs, key=_sort_key)[:limit]:
        print(f"  [{score.total:3d}] {posting.title} - {posting.company or '?'} "
              f"({posting.source})")
    if len(pairs) > limit:
        print(f"  ... and {len(pairs) - limit} more")


def _print_filtered(pairs: list[tuple[JobPosting, Score]]) -> None:
    """What was dropped and why, so the rules can be tuned."""
    if not pairs:
        return
    print("\n" + "=" * 78)
    print(f"FILTERED OUT ({len(pairs)})")
    print("=" * 78)
    for posting, score in pairs:
        print(f"  {posting.title} - {posting.company or '?'}")
        print(f"      {score.filter_reason}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    try:
        config = Config.load(args.config_dir)
        sources = config.build_job_sources(only=args.sources)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.max_queries is not None:
        for source in sources:
            source.queries = source.queries[: args.max_queries]

    pacer = config.make_pacer(sleeper=(lambda _s: None) if args.no_pacing else None)
    planned = sum(len(s.queries) * len(s.locations) for s in sources if s.enabled)

    print(f"sources: {', '.join(s.name for s in sources)}")
    print(f"planned scrape calls: {planned} (budget {pacer.max_scrape_calls})")
    if args.no_pacing:
        print("pacing DISABLED - local testing only")
    print(f"store: {'disabled' if args.no_store else args.db}")
    print()

    store = None if args.no_store else Store(args.db)
    run_id = store.start_run() if store else None

    try:
        report = run_sources(sources, pacer)

        unique = report.unique_postings
        if store and run_id is not None:
            for result in report.results:
                store.record_source_result(run_id, result)
            classified = store.classify(run_id, unique)
            store.finish_run(
                run_id,
                collected=len(report.postings),
                unique=len(unique),
                new=len(classified.new),
                aborted_reason=report.aborted_reason,
            )
            new_postings, returning = classified.new, classified.returning
        else:
            new_postings, returning = unique, []

        print("=" * 78)
        print("RUN SUMMARY" + (f"  (run #{run_id})" if run_id else ""))
        print("=" * 78)
        for result in report.results:
            print("  " + result.summary_line())
            # A source failing once is noise; failing for days is the thing
            # you actually need told to your face.
            if store and result.status.is_problem:
                history = store.source_history(result.source, limit=5)
                streak = 0
                for row in history:
                    if row["status"] in ("failed", "blocked"):
                        streak += 1
                    else:
                        break
                if streak > 1:
                    print(f"             ^ {result.source} has now failed "
                          f"{streak} runs in a row")

        if report.aborted_reason:
            print(f"\n  !! RUN ABORTED: {report.aborted_reason}")
            print("     A board rate-limited or challenged us. Not retried by design.")

        total = len(report.postings)
        print(
            f"\n  {total} collected, {len(unique)} unique "
            f"({total - len(unique)} cross-board duplicates), "
            f"{len(new_postings)} new since last run, "
            f"{report.duration_seconds:.1f}s"
        )
        if args.no_store:
            print("  (store disabled - 'new' is meaningless in this mode)")

        scorer = Scorer(config.profile)
        scored = [(p, scorer.score(p)) for p in unique]
        kept = [pair for pair in scored if not pair[1].filtered]
        dropped = [pair for pair in scored if pair[1].filtered]

        by_bucket: dict[str, list[tuple[JobPosting, Score]]] = {
            BUCKET_STRONG: [], BUCKET_WORTH: [], BUCKET_STRETCH: []
        }
        for pair in kept:
            by_bucket[pair[1].bucket].append(pair)

        floor = (config.profile.get("compensation") or {}).get("income_floor_monthly_ils")
        print(f"  scored against profile: {len(kept)} kept, {len(dropped)} filtered out"
              + (f"; income floor {float(floor):,.0f} ILS/month" if floor else
                 "; no income floor set"))

        _print_group("STRONG MATCHES", by_bucket[BUCKET_STRONG], args.limit)
        _print_group("WORTH A LOOK", by_bucket[BUCKET_WORTH], args.limit)
        _print_stretch(by_bucket[BUCKET_STRETCH], args.limit)
        _print_filtered(dropped)

        problems = [r for r in report.results if r.status.is_problem]
        if problems:
            print("\n" + "=" * 78)
            print("PROBLEMS")
            print("=" * 78)
            for result in problems:
                print(f"  {result.source}: {result.status.value} - {result.detail}")
                for warning in result.warnings[:3]:
                    print(f"      {warning[:150]}")

        if report.aborted_reason:
            return 3
        if all(
            r.status.is_problem or r.status == SourceStatus.SKIPPED
            for r in report.results
        ):
            return 1
        return 0
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
