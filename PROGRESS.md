# jobpulse progress

Source of truth for what is built, what was decided, and what is left. The approved plan (2026-10-23) covers M0–M3; M4 is skipped except the pure parsers section 7 requires. Work stops after each milestone for approval.

## Decisions

- **Scope:** M0–M3. From M4 only the pure parsers required by section 7: Workday `postedOn` (M1) and the LinkedIn applicant line (M2). No LinkedIn guest fetch, Workday adapter or LLM layer.
- **Day-precision freshness:** effective time = min(end of the posted day in UTC, `first_seen_at`). Taken literally, the spec's "later of" would make every newly seen day-precision job fresh. A job posted today always passes; "Posted 30+ Days Ago" is still screened out.
- **Sync SQLAlchemy, async network:** every DB phase is a synchronous block with no `await` inside, so no transaction is held across network I/O.
- **Dev contact email:** a random `jobpulse-dev-<hex>@example.com` in the gitignored `config.yaml`; `example.com` is reserved, so it can't reach a real person.
- **Dependency freshness:** `[tool.uv] exclude-newer` keeps the lock a few days behind the newest releases. Dev tools are pinned exactly.
- **Secrets:** required only by commands that send (`worker`, a non-dry `poll`, `digest`, `notify test`); `poll --dry-run` and `config check` work without `.env`. `NTFY_TOPIC` must be 24–64 characters of `[A-Za-z0-9_-]`.
- **Empty first poll:** an empty successful first poll still completes the baseline (spec: "first successful poll").
- **Fingerprint window:** the 30-day window compares against `last_seen_at`, so a still-open ATS job keeps absorbing reposts.
- **URL-key merges** use ATS canonical keys only. Generic career-page URLs never merge; only the fingerprint can merge them.
- **`description.years_mode`** defaults to `penalize`, so a too-senior posting drops to the digest instead of vanishing. `exclude` is available.
- **Greenhouse EU:** EU boards (`job-boards.eu.greenhouse.io`) are served by the global `boards-api.greenhouse.io`, so Greenhouse needs no region switch.
- **Ashby `is_remote`** = `workplaceType == "Remote"` (None when null), because `isRemote` is also true for Hybrid jobs.
- **JobSpy 1.4.0, not 1.4.1:** 1.4.1 is newer than `exclude-newer`. Its changes are a LinkedIn salary-from-description fix and fixes for boards jobpulse never calls; core, model and Indeed code are unchanged.
- **Simplify ETag** lives in `source_state` and is saved in the same commit as the fetched rows, so a crash can't skip a feed version. A 304 is a successful run with `not_modified`, which the busy-zero health rule ignores. The feed doesn't use the in-memory body cache: its closures come from `active: false`, so there is nothing to replay.
- **Simplify closures** follow the spec: only `active: false` closes. Listings with `is_visible: false` are ignored (all 14 in today's feed are also inactive).
- **Discovery fetches page HTML only for `gh_jid` links** on custom domains not yet mapped, once per host and capped per cycle. Other non-ATS links (Workday, iCIMS and so on, about 70% of the feed) are never fetched. The M3 paste flow does the full URL-then-HTML detection.
- **Discovery order:** boards with the most matching postings first, then the most referenced. Validations are capped per cycle. Negative results are cached in memory for the worker's lifetime: 7 days for a missing board, 2 h for transient errors.
- **Narrower discovery (your call, 2026-10-23):** the spec adds every board any URL points at; now a board must have a feed or job-board posting that passes your title and location filters (`discovery.require_match`). Boards with more than `max_board_jobs` (1000) open jobs are skipped, reported and not downloaded again for 7 days. At most `max_auto_companies` (50) discovered companies are polled. With the example filters, today's feed has 131 matching boards instead of 381; SpaceX (2,817 jobs) and Anduril (2,506) are over the size limit.
- **Company blocklist:** `blocklist.companies` (whole names, normalized like fingerprints) and `blocklist.boards` (`ats/token`). A blocked company's postings, from any source, are stored as `filtered` and never pushed; a push still waiting when the company is blocked is dropped. Discovery never adds a blocked company or board, and a discovered board that becomes blocked isn't polled (unblocking resumes it without a new baseline). Blocked companies don't count toward `max_auto_companies`. Blocking an active watchlist entry is a config error.
- **LinkedIn back-off:** after a 429, LinkedIn searches are skipped for `linkedin.cooldown_minutes` (30). A skipped search records no run, so it neither fails nor recovers in health checks. JobSpy's own delay between LinkedIn result pages is set from `linkedin.delay_*_seconds`.
- **Search changes:** a changed site, term, location, `is_remote`, country or company-id list resets that search's baseline. A changed `results_wanted` or `hours_old` doesn't.
- **Reverse link:** a new ATS row, in a baseline or not, adopts unmerged feed and job-board copies (same ATS key, `gh_jid` or fingerprint in the window). It inherits their `notified_at`, `digest_sent_at`, status and applicant data, and a baseline row also takes over a match tier (`instant`/`digest`). The ATS row is always the primary.
- **No company, no fingerprint merge:** a posting without a company name never fingerprint-merges.
- **"City, ST, USA" labels** drop the trailing US country, so JobSpy and ATS locations agree.

## Deviations from the spec layout

- Extra modules: `core/atsurls.py` (pure URL/HTML parsing, so dedupe needn't import `sources`), `core/locations.py`, `core/text.py`, `httpclient.py` (not `http.py`, which would shadow the stdlib name), `poller.py`, `logging_setup.py`, `db/types.py`, `devtools/fixtures.py`.
- M2 modules: `core/jobboards.py` (LinkedIn and Indeed ids from URLs), `discovery.py` (planning, caps and the miss cache around `sources/ats_detect.py`).
- Extra columns: `postings.workplace_type`, `department`, `compensation`, `tier`, `tier_reason`, `score_breakdown`; `runs.warnings`, `runs.stats`; `companies.updated_at`; `searches.active`, `searches.created_at`.
- M2 schema (migration 0002): `source_state` (feed ETag and baseline), `health_incidents`, `runs.not_modified`, `companies.discovered_from`.
- `postings.missing_count` is the only missing-poll counter; `companies.consecutive_missing` is omitted.
- `Posting.locations` is a tuple (the dataclass is frozen).
- Dependencies arrive with the milestone that needs them: JobSpy in M2, FastAPI/Jinja2 in M3. Dockerfile and compose arrive in M3.

## M0 — Scaffold

- [x] git repo (`main`) and uv project, Python 3.12, exact dev-tool pins, `exclude-newer`
- [x] ruff / mypy (strict on `jobpulse.core`) / pytest config, `scripts/check.sh`
- [x] package layout: `core`, `sources`, `notify`, `db`, `cli`
- [x] config models, `config.example.yaml`, `.env.example`; field- and file-specific errors
- [x] SQLAlchemy models, `UTCDateTime`, session setup (SQLite WAL), Alembic baseline migration
- [x] Typer CLI skeleton: `poll`, `worker`, `web`, `companies`, `digest`, `db`, `config`
- [x] logging with secret redaction
- [x] CLAUDE.md, PROGRESS.md, README stub
- [x] tests: config validation (item 15), migrations, CLI, logging redaction (46 tests)
- [x] `scripts/check.sh` green; `alembic upgrade head` creates the schema on SQLite
- [x] commit `feat(m0): …`

## M1 — ATS watchlist → detection → ntfy

- [x] core: models, timeparse (incl. Workday `postedOn`), text, locations, filters, scoring, atsurls, dedupe, detection
- [x] `httpclient.py`: per-host limits, timeouts, full-jitter backoff, `Retry-After`, User-Agent, ETag replay cache
- [x] adapters: Greenhouse (list without content, per-job enrich), Lever (US/EU), Ashby
- [x] repo: watchlist upsert, presence plan, insert-if-absent, merges, runs
- [x] poller: baseline, freshness gate, closures, dedupe, enrichment, scoring, notification retry and cap
- [x] notifiers: ntfy (headers + JSON fallback), Discord (embed, 429 `retry_after`); message format
- [x] scheduler: per-source cadence with jitter, worker command
- [x] CLI: `poll --once [--dry-run] [--source] [--target] [--fixtures]`, `worker`, `companies list|sync`, `notify test`
- [x] fixtures for every ATS, scenario route sets (`tests/fixtures/scenarios/`)
- [x] tests: section 7 items 1–7, 9 (ATS forms), 11, 13, 14, 15, 16, adapter parsing, dry-run CLI (352 tests)
- [x] live smoke test (item 17, 9 live tests); VERIFY results in CLAUDE.md
- [x] commit `feat(m1): …`

## M2 — SimplifyJobs, discovery, JobSpy, dedupe, health

- [x] read the python-jobspy 1.4.0 source (columns, attrs, params); pin `python-jobspy==1.4.0` and `pandas>=2.2,<3.1`
- [x] schema (migration 0002): `source_state`, `health_incidents`, `runs.not_modified`, `companies.discovered_from`
- [x] Simplify adapter: persisted ETag (a 304 is an "unchanged" success), keep `active && is_visible`, close on `active: false`, day precision at midnight UTC, categories normalized
- [x] JobSpy adapter: one site per `scrape_jobs` call in a thread, `hours_old`, `verbose=0`, never `proxies`; ids parsed from `job_url`; `attrs` errors → failed run (`http_429` vs `jobspy_board_error`), warnings → run log; LinkedIn one at a time with 4–8 s pauses and a cool-down after a 429
- [x] searches synced from config; a changed definition resets its baseline; per-search baseline
- [x] `ats_detect.py`: URL first, then the page HTML once; one validation call (Lever US, then EU on a 404)
- [x] discovery over every feed and job-board URL: auto-add (`auto`, priority 0, own baseline), per-cycle caps, negative cache
- [x] cross-source dedupe end to end, plus the reverse link: a new ATS row adopts earlier copies and their alert state
- [x] `health.py` + `health_incidents`: 3 consecutive failures; a busy target with only zero-result successes for 24 h; one alert per incident and one recovery message
- [x] `core/applicants.py` (item 10)
- [x] worker runs watchlist, Simplify and job-board jobs on their own cadences; `poll --once` covers every source; `--fixtures` also serves JobSpy rows
- [x] tests: items 5, 6, 8, 9 (all forms, discovery from Simplify URLs, one validation call), 10, 12, JobSpy error handling; live Simplify smoke (569 tests, 11 live)
- [x] acceptance: a fixture dry run shows a LinkedIn duplicate merged onto its Greenhouse row with no alert
- [ ] commit `feat(m2): …`

## M3 — Dashboard and digests

- [ ] FastAPI + Jinja2 + HTMX: New / Saved / Applied / Dismissed, filters, sort, "posted X ago"
- [ ] source-health page; watchlist page (paste URL → detect → validate → add)
- [ ] 127.0.0.1 by default, basic auth when configured, CSRF-safe POSTs
- [ ] twice-daily digest, `digest_sent_at`, at most one digest per posting
- [ ] digest and dashboard leave out blocked companies (rows decided before the company was blocked)
- [ ] Dockerfile, docker-compose (`migrate`, `worker`, `web`, `postgres` profile)
- [ ] worker watchdog: exit non-zero when no job finished within 3× the longest interval (in the approved plan; M1 didn't build it)
- [ ] README complete; optional ruff hook
- [ ] tests: routes, auth on/off, watchlist endpoint, digest
- [ ] commit `feat(m3): …`

## M4 — skipped (decision 2026-10-23)

LinkedIn applicant enrichment, the Workday adapter and the LLM scoring layer are not built.

## Log

- **2026-10-23** — Planning. Probed the Greenhouse, Lever and Ashby APIs and the Simplify feed (findings in CLAUDE.md). Installed uv 0.13.5 (Homebrew); CPython 3.12.15, SQLite 3.53.1. Started M0.
- **2026-10-23** — M0 done.
  - Tests were written first against stubs: 41 failed and 1 module didn't import, all for missing behavior. All 46 now pass.
  - The lock resolves with `exclude-newer = 2026-10-20`: alembic 1.20.0, SQLAlchemy 2.1.5, pydantic 2.14.1, pydantic-settings 2.16.0, httpx 0.28.1, APScheduler 3.11.3, Typer 0.27.3; dev tools pytest 9.1.1, pytest-asyncio 1.4.0, pytest-socket 0.8.1, respx 0.23.1, time-machine 3.5.1, ruff 0.17.1, mypy 2.4.0.
  - The autogenerated baseline was rewritten by hand: Alembic rendered `JSONB(astext_type=Text())` without the `sa.` import and boolean server defaults as `1`/`0`, which Postgres rejects. A test checks that the migration matches the models.
  - Added `tzdata` so timezone names resolve in slim Docker images.
  - Partial cadences (`watchlist: {every_minutes: 10}`) keep the default jitter.
  - Open: nothing.
- **2026-10-23** — M1 done.
  - **Test-first in two waves,** each against interface stubs and run red first:
    - core, HTTP, adapters, notifiers and scheduler: 273 failures, all `NotImplementedError`;
    - repository, poller and CLI: 30 failures, all from `NotImplementedError`.
    - Now 352 tests pass, and 9 live tests pass with `JOBPULSE_LIVE=1`.
  - **Dedupe decisions:**
    - The fingerprint uses the city part of the primary location ("San Francisco, CA" and "San Francisco, California, United States" agree). JobSpy's LinkedIn rows have no `job_url_direct`, so LinkedIn-to-ATS dedupe rests on the fingerprint.
    - URL merges happen only on ATS job keys. A job-board copy merges into any row with the same key, not only ATS rows, because ATS keys are job-specific.
    - A `gh_jid` on a domain not yet mapped falls back to looking up the Greenhouse job id directly.
  - **Notifications:**
    - ntfy header values are sent as UTF-8 bytes, because httpx sends `str` headers as ASCII and ntfy reads UTF-8. A title outside Latin-1 goes to the JSON endpoint, per the spec.
    - A failed push is retried on later cycles while the posting is inside the freshness window.
    - `notify.max_instant_per_cycle` holds the overflow for the digest and sends one "N more" push.
  - **Robustness:**
    - Rows left `pending` by an interrupted cycle are re-evaluated on the next one.
    - List calls use an in-memory ETag cache. A 304 replays the previous body, so closures behave as they would on a 200.
    - The worker uses self-rescheduling one-shot jobs, so a source's runs never overlap. Job exceptions are logged, never raised into APScheduler.
  - **Other:**
    - The watchlist is upserted at the start of every cycle.
    - Workday entries are skipped with a warning (no adapter until M4).
    - Logs are in UTC.
  - **Real dry run** over the 11 example boards (live APIs): about 2,500 postings, baseline lines only, nothing saved, about 3 s.
  - **Left for M2:** the reverse link, where a new ATS posting matches an already-pushed job-board posting. It isn't needed until job-board sources exist.
- **2026-10-23** — M2 done.
  - **Test-first in two waves,** each against interface stubs and run red first:
    - core, adapters, ATS detection, discovery and the health rules: 148 failures, 140 of them `NotImplementedError` and 8 assertions on behavior not changed yet;
    - repository, poller, CLI and worker: 53 failures, all missing behavior (searches not synced, no feed targets, no discovery or health).
    - Now 569 tests pass, and 11 live tests pass with `JOBPULSE_LIVE=1`.
  - **JobSpy 1.4.0, read from the installed source:** errors and warnings in `attrs`, `verbose` 0 = errors only, the LinkedIn class's `delay = (3, 7)` between result pages, 34 columns. pandas 3.0.6 gives `nan` for missing text cells.
  - **Simplify, checked live:** the gzip (weak) ETag answers 304. The feed has 19,325 records, 3,075 open, and 784 open listings are dated exactly midnight UTC.
  - **A test helper changed:** `Env.add_search` used to insert a `searches` row. Searches now come from config, so it adds a config entry that the next poll syncs. The M1 tests that use it are otherwise unchanged.
  - **Real dry run** with the example watchlist on an empty scratch database: 12 baselines (3,095 open Simplify listings), nothing to push, 9–10 boards that discovery would add, 422 candidates left for later cycles. It took 24 s; one run took 87 s because Anduril's 2,800-job Greenhouse list timed out on all 4 attempts.
  - **Fixed after that run:** gh_jid pages are read only while the validation cap has room, so a cycle never reads a page whose board it can't validate.
  - **Schema check:** the CLI and worker refuse to run on a database behind the newest migration, with a message to run `jobpulse db upgrade`. An M1 database (at 0001) used to crash on the first query that touched a new column.
  - **For M3:** the dashboard's paste flow can call `AtsDetector.detect` with page fetches on; the health page can read `runs` and `health_incidents`.
- **2026-10-23** — Narrower discovery and a company blocklist (your request, before M3).
  - **Test-first:** 29 new tests failed against stubs, all on missing behavior (non-matching boards still validated, blocked companies pushed and polled, config rules not enforced). Now 604 tests pass.
  - **Measured on today's feed:** 381 unwatched boards, 131 with a posting that passes the example title filter, 28 with two or more.
  - **Real dry run** (scratch database): 8 boards to add and 2 skipped for size (SpaceX, Anduril) in the first cycle, 138 candidates left; the cycle took 4 s.
