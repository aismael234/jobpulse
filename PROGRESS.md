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

## Deviations from the spec layout

- Extra modules: `core/atsurls.py` (pure URL/HTML parsing, so dedupe needn't import `sources`), `core/locations.py`, `core/text.py`, `httpclient.py` (not `http.py`, which would shadow the stdlib name), `poller.py`, `logging_setup.py`, `db/types.py`, `devtools/fixtures.py`.
- Extra columns: `postings.workplace_type`, `department`, `compensation`, `tier`, `tier_reason`, `score_breakdown`; `runs.warnings`, `runs.stats`; `companies.updated_at`; `searches.active`, `searches.created_at`.
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

- [ ] read the python-jobspy 1.4.0 source (columns, attrs, params); pin it
- [ ] Simplify adapter with persisted ETag (`source_state`, migration 0002); close on `active: false`
- [ ] `ats_detect.py`: URL, then page HTML; one validation call; auto-add with its own baseline
- [ ] JobSpy adapter: Indeed and LinkedIn as separate calls in a thread, `hours_old=3`, LinkedIn pacing
- [ ] searches synced from config; per-search baseline
- [ ] cross-source dedupe end to end, including the reverse link
- [ ] `health.py` + `health_incidents`: 3 failures, busy-source zero for 24 h, one alert + recovery
- [ ] `core/applicants.py` (item 10)
- [ ] tests: items 5, 6, 8, 9 (all forms + discovery), 10, 12, JobSpy errors; live Simplify smoke
- [ ] commit `feat(m2): …`

## M3 — Dashboard and digests

- [ ] FastAPI + Jinja2 + HTMX: New / Saved / Applied / Dismissed, filters, sort, "posted X ago"
- [ ] source-health page; watchlist page (paste URL → detect → validate → add)
- [ ] 127.0.0.1 by default, basic auth when configured, CSRF-safe POSTs
- [ ] twice-daily digest, `digest_sent_at`, at most one digest per posting
- [ ] Dockerfile, docker-compose (`migrate`, `worker`, `web`, `postgres` profile)
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
