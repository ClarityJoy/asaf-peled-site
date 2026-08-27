# job-digest

A local daily digest of job postings and hiring-signal posts. Runs from cron on
one machine, writes a markdown file, uploads nothing anywhere.

## Status

Phases 1-3 complete: job sources working end to end, a SQLite store that
remembers what it has already shown you, and deterministic fit scoring.
Phases 4-6 (post search, digest rendering, cron) are not built yet.

## Install

Python 3.11 or newer.

```
cd job-digest
python3 -m venv .venv
.venv/bin/pip install -e .
```

## Run

```
.venv/bin/jobdigest                          # all enabled sources
.venv/bin/jobdigest --source indeed          # one source
.venv/bin/jobdigest --max-queries 2 --no-pacing   # quick local look
.venv/bin/jobdigest --no-store               # ignore history, everything is new
```

`--no-pacing` removes the delays between requests. Use it while editing config,
never from cron.

## What it remembers

State lives in `state/jobs.db` (SQLite, gitignored). JobSpy has no memory
between runs, so this is the part that turns a repeated search into a digest:
each run records what it saw, and a posting is reported as new exactly once.

| Table | Holds |
| --- | --- |
| `runs` | one row per run, with counts and any abort reason |
| `source_runs` | per-source status per run, so a source failing for days is visible as a streak |
| `postings` | one row per job, keyed on the dedup fingerprint, with `first_seen` / `last_seen` / `times_seen` |
| `sightings` | one row per (run, job, board) -- keeps the fact that a job appeared on both Indeed and LinkedIn |

`first_seen` is never overwritten, and an update only fills fields the
incoming record actually carries, so a sparse row from one board cannot blank
out a description another board already gave us.

Delete `state/jobs.db` to start over; the next run will report everything as
new again.

## Configuration

Everything tunable lives in `config/`, and all three files are meant to be
edited by hand.

| File | Holds |
| --- | --- |
| `profile.yaml` | background, target roles, geography, income floor, scoring weights and keywords |
| `queries.yaml` | search terms for jobs, and for posts in English and Hebrew |
| `sources.yaml` | which boards run, per-board budgets, request pacing |

`config/profile.yaml` has one placeholder to fill in:
`compensation.income_floor_monthly_ils`. Until it is set the floor is not
applied, and the digest will say so rather than filter silently.

## Scoring

Rule-based and deterministic. No LLM call anywhere, so every number traces
back to a keyword list or a weight in `profile.yaml` and you can argue with it.

Four components, reported separately so a 60 reads as "great domain, wrong
commute" rather than a bare number:

| Component | Weight | What it asks |
| --- | --- | --- |
| domain fit | 45 | does it touch regulation, payments or compliance at all |
| seniority fit | 20 | senior IC or small-team lead, not junior and not a VP |
| role type fit | 20 | is it actually a product role |
| location fit | 15 | reachable from Rishon LeZion, or remote enough not to matter |

Penalties are subtracted from the total afterwards rather than folded into a
component, so the digest can name them out loud: `no_domain_surface`,
`large_team_management`, `compliance_analyst`, `wrong_discipline`,
`too_junior`, `too_senior`.

Three buckets: **strong** (>=70), **worth a look** (>=45), and
**stretch - likely resume-screen rejection** below that. The third label is
deliberately not softened.

Two things worth knowing about how it behaves:

- **Domain dominates by design.** A generic PM role cannot reach `strong`
  even if it is senior, a product title, and on your doorstep -- the
  `no_domain_surface` penalty caps it. That shape is the one that has been
  getting cut at the resume screen, so it must never read as promising.
- **Keywords are bilingual.** Hebrew terms sit alongside the English ones.
  Without them a Hebrew regtech posting scored zero domain fit and got
  penalised as generic, which for this market is a serious miss.

### Commute model

`profile.yaml` holds three tiers of place names relative to Rishon LeZion.
Note what it encodes: **Tel Aviv is `medium`, not `near`**. Under your
30-minute rule that means most Tel Aviv roles need WFH days to score well,
which is a real consequence of the constraint rather than an oversight. A
`hybrid_markers` hit in the posting text rescues a medium or far commute.

### Income floor

35,000 ILS/month, in `profile.yaml`. A posting is filtered out only when it
**states** a salary below the floor; most Israeli postings state none at all,
in which case the floor simply does not apply and nothing is hidden. Foreign
currencies are converted with static, hand-editable rates -- nothing calls a
live FX service.

Everything filtered out is listed at the bottom of the output with its
reason, so the rules can be tuned rather than guessed at.

## Sources

Job listings come from JobSpy, which covers several boards in one call and
needs no authentication of any kind.

| Board | Status | Why |
| --- | --- | --- |
| Indeed | primary | supports Israel; the only board upstream reports as not rate limited |
| LinkedIn | secondary, small budget | unauthenticated guest endpoint; rate limits around the 10th page from one IP |
| Bayt | included | MENA board, included for Gulf/UAE regtech roles |
| Glassdoor | excluded | raises "Glassdoor is not available for ISRAEL" before any network call |
| Google Jobs | excluded | JobSpy issue #302: returns 0 results / 403, open since 2026 |
| ZipRecruiter | excluded | same issue, and US/Canada only in practice |

## Ground rules encoded in the code

These are not conventions, they are enforced:

- **No proxies, no fingerprint spoofing, no multiple sessions.** The risk
  strategy is one residential IP, as one person, at low volume. LinkedIn's
  budget in `sources.yaml` is deliberately small because we answer its rate
  limit by asking for less rather than by evading it.
- **A 429 or challenge ends the run.** `blocking.py` classifies JobSpy's log
  output and raises `RunAborted`; nothing retries. JobSpy's own retry-on-429
  for LinkedIn is patched out at startup by `disable_jobspy_retries()`.
- **Sequential only.** One source at a time, one query at a time.
- **A failed source is reported, never hidden.** JobSpy logs a block and then
  returns an empty list, so "blocked" and "no jobs today" look identical at
  the return value. The adapter reads the logs to tell them apart, and the
  run summary shows per-source status.
- **Read-only.** Nothing applies, connects, messages, likes or follows.
- **Secrets stay local.** `.env` is gitignored; `.env.example` shows the shape.

## Layout

```
config/                 hand-edited YAML
src/jobdigest/
  models.py             JobPosting, SourceResult, SourceStatus, dedup key
  pacing.py             randomised delays + run-wide scrape-call budget
  blocking.py           tells "blocked" from "quiet" from "network broke"
  runner.py             sequential execution, cross-board dedup
  cli.py                stdout report
  sources/
    base.py             the interface every source implements
    jobspy_source.py    Indeed / LinkedIn / Bayt via JobSpy
```

Every source sits behind `sources/base.Source`. Replacing a broken library
means writing one new adapter; nothing downstream changes.
