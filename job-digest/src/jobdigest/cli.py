"""Phase 1 entry point: run the job sources and print what came back."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from .config import Config, ConfigError
from .models import SourceStatus
from .runner import run_sources


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobdigest",
        description="Daily job digest - job source collection (phase 1).",
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
    print()

    report = run_sources(sources, pacer)

    print("=" * 78)
    print("RUN SUMMARY")
    print("=" * 78)
    for result in report.results:
        print("  " + result.summary_line())
    if report.aborted_reason:
        print(f"\n  !! RUN ABORTED: {report.aborted_reason}")
        print("     A board rate-limited or challenged us. Not retried by design.")

    unique = report.unique_postings
    total = len(report.postings)
    print(
        f"\n  {total} postings collected, {len(unique)} unique after dedup "
        f"({total - len(unique)} cross-board duplicates), "
        f"{report.duration_seconds:.1f}s total"
    )

    if unique:
        print("\n" + "=" * 78)
        print(f"POSTINGS (showing up to {args.limit})")
        print("=" * 78)
        def sort_key(p):
            return (p.date_posted is None, -(p.date_posted.toordinal() if p.date_posted else 0))
        for posting in sorted(unique, key=sort_key)[: args.limit]:
            when = posting.date_posted.isoformat() if posting.date_posted else "date unknown"
            print(f"\n  {posting.title}")
            print(f"    {posting.company or 'unknown company'} - {posting.display_location}")
            print(f"    {when} - via {posting.source} - query: {posting.query!r}")
            print(f"    {posting.url}")

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
    if all(r.status.is_problem or r.status == SourceStatus.SKIPPED for r in report.results):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
