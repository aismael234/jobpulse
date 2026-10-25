# jobpulse progress

Source of truth for what is built, what was decided, and what is left. The approved plan (2026-10-23) covered M0–M3 and skipped M4; on 2026-10-24 you asked for M4 as well. Work stops after each milestone for approval.

## Decisions

- **Scope:** M0–M4. M4 was skipped on 2026-10-23 (only its two pure parsers were built, in M1 and M2); you asked for the rest on 2026-10-24: LinkedIn job pages, the Workday adapter and the LLM layer.
- **Day-precision freshness:** effective time = min(end of the posted day in UTC, `first_seen_at`). Taken literally, the spec's "later of" would make every newly seen day-precision job fresh. A job posted today always passes; "Posted 30+ Days Ago" is still screened out.
- **Sync SQLAlchemy, async network:** every DB phase is a synchronous block with no `await` inside, so no transaction is held across network I/O.
- **Dev contact email:** a random `jobpulse-dev-<hex>@example.com` in the gitignored `config.yaml`; `example.com` is reserved, so it can't reach a real person.
- **Dependency freshness:** `[tool.uv] exclude-newer` keeps the lock a few days behind the newest releases. Dev tools are pinned exactly.
- **Secrets:** required only by commands that send (`worker`, a non-dry `poll`, `notify test`); `poll --dry-run` and `config check` work without `.env`. `NTFY_TOPIC` must be 24–64 characters of `[A-Za-z0-9_-]`.
- **Empty first poll:** an empty successful first poll still completes the baseline (spec: "first successful poll").
- **Fingerprint window:** the 30-day window compares against `last_seen_at`, so a still-open ATS job keeps absorbing reposts.
- **URL-key merges** use ATS canonical keys only. Generic career-page URLs never merge; only the fingerprint can merge them.
- **`description.years_mode`** defaults to `penalize`, so a too-senior posting is still pushed, with a lower score, instead of vanishing. `exclude` is available.
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
- **Reverse link:** a new ATS row, in a baseline or not, adopts unmerged feed and job-board copies (same ATS key, `gh_jid` or fingerprint in the window). It inherits their `notified_at`, status and applicant data, and a baseline row also takes over a match tier (`instant`). The ATS row is always the primary.
- **No company, no fingerprint merge:** a posting without a company name never fingerprint-merges.
- **"City, ST, USA" labels** drop the trailing US country, so JobSpy and ATS locations agree.
- **Dashboard views:** New holds untriaged matches (the `instant` tier). Saved, Applied and Dismissed hold every posting with that status, whatever its tier, so a triaged posting never disappears because of its tier. Merged copies never show; their primary does, with `also_seen_on`.
- **Blocked companies in the dashboard** are hidden everywhere except Applied, which is also your record of applications. It's decided when the page is read, so unblocking brings them back.
- **No digest, no push cap (your request, 2026-10-24):** every posting that passes the filters is a match (tier `instant`) and is pushed in the cycle that decides it, best score first, whatever the score. `jobpulse digest`, `schedules.digest_times`, `notify.max_instant_per_cycle`, `notify.dashboard_url` and `postings.digest_sent_at` are gone (migration 0004: `digest` rows became `instant`, and rows a digest had listed took its time as `notified_at`, so they aren't pushed again). `scoring.instant_threshold` stays only as the center of the LLM band. A failed push is retried next cycle while the posting is fresh, open and still `New`.
- **Watchdog:** every interval job must finish a run within 3× the longest interval plus jitter (4.5 h with the defaults). It checks each job, not just "any job", so one hung source is caught too, and it uses the monotonic clock, so a machine waking from sleep doesn't look stalled. The worker exits with code 1 and Docker restarts it.
- **Dashboard security:** basic auth when both `DASHBOARD_USER` and `DASHBOARD_PASSWORD` are set (only one is an error); `/healthz` is exempt and says only "ok". The CSRF token is per browser, in an `HttpOnly`, `SameSite=Strict` cookie, and every POST sends it back as `X-CSRF-Token` (HTMX) or a form field; cross-site POSTs are refused by `Sec-Fetch-Site`. Strict CSP (no inline scripts or styles), `no-store` on pages, no API docs, secrets masked in every rendered value, and links to source URLs only when they are http(s).
- **Watchlist paste:** the URL first, then the page once (public hosts only); one validation call (a Workday board answers the first page of its search, and the form adds a keywords field); a board already stored isn't validated again; blocked boards and companies are refused. A board bigger than `discovery.max_board_jobs` can still be added by hand, with a warning. Added boards are `dashboard`-owned with the priority you pick; priority and pause can be changed only for companies that aren't in config.yaml, because each poll applies config.yaml again.
- **FastAPI 0.150.0, pinned:** the newest release before `exclude-newer` (FastAPI shipped 40+ releases since September). Starlette 1.9's TestClient prefers the new `httpx2` package; since M4 brought `httpx2` (through `anthropic`) it uses it, and the filter for its fallback warning is gone.
- **htmx 2.0.11 vendored** from the npm tarball, its sha512 checked against the registry; a test pins the file's sha256.
- **Health page "24 h fetched"** sums `fetched_count` over the window, as the spec asks, next to the number of runs.
- **Run history retention (your request, 2026-10-24):** `retention.runs_days` (30; `null` keeps everything) must cover `health.busy_window_days` while health checks are on. The poller deletes older runs at most once a day per process and never in a dry run. A target that has failed for longer than the window shows "never" as its last success.
- **HEAD (your request, 2026-10-24):** the dashboard's GET pages answer HEAD too (uvicorn sends no body); static files already did.
- **LinkedIn job pages (M4):** `jobs-guest/jobs/api/jobPosting/{id}` for LinkedIn postings that pass the prescreen (never in a baseline, never for merged copies), one at a time through the gate the JobSpy searches use (4–8 s), one attempt each. A 429, 999, 403 or a redirect to the sign-in wall starts the shared cool-down (30 min), which also ends job pages for the rest of the cycle; other failures leave the posting as it was. At most `linkedin.max_job_pages_per_hour` (40). The applicant line is the first applicant phrase in the page's text; the description is the description section plus its criteria list ("Seniority level: Mid-Senior level").
- **No cookies:** the HTTP client keeps none (a cookie policy that allows no domain), so no request carries a session. LinkedIn's guest page sets several.
- **Applicant signal:** `1 - count / scoring.applicant_cap` (200), a range counted from its known end: "Be among the first 25" scores 0.875, "Over 200" scores 0.
- **Workday tokens and ids:** a board is `tenant.wdN/site` (config and the paste box also take its URL). A posting's id is `tenant.wdn/site/jobpostingid`, lowercased, because Workday ids are unique only within a site. Job URLs in every form canonicalize to `workday:{token}:{id}`, so feed and job-board copies merge onto a watched Workday row, and a new Workday board takes over earlier copies.
- **Workday reads:** `limit` fixed at 20 and `total` read from the first page. A board with `keywords` is searched once per keyword and never closes anything (the spec: complete only when fully paged). A board without keywords is complete only when every page was read and the count reaches `total`, so a listing that changes while it's paged pauses closures. `workday.max_pages` (25) caps each search, with a warning to add keywords. An empty first page while `total` > 0 is a failure. The detail call (survivors only) brings the description, `startDate` (day precision), the real locations behind "2 Locations" and the remote type.
- **Workday keywords** live in `companies.params`, for config entries and dashboard additions alike. Changed keywords start a new silent baseline, like a changed search.
- **Discovery leaves Workday alone:** tenants run to thousands of jobs and need keywords, so only you add Workday boards. Feed and job-board copies of their jobs still merge by canonical key.
- **Missing locations:** the prescreen judges locations only when the list has some; a posting still without one after its detail call is judged in `evaluate` (`unknown_location_passes`).
- **LLM provider:** Anthropic's Python SDK, `anthropic>=1.15,<2` (1.15.0 is the newest release before `exclude-newer`; 2.0 shipped on 2026-10-21). Default `claude-opus-5-5` at `effort: low`. One structured-output request per posting (`beta.messages.create` with `Verdict`'s JSON schema), with `stop_reason` checked before parsing, so a refusal or an answer cut off at `max_tokens` is reported as such. Models that support it send `fallbacks: "default"` (beta `server-side-fallback-2026-07-01`). The instructions and the resume are a cached prompt prefix. HTTP 400, 401, 403 and 404 stop the asking until a restart; the SDK retries 429, 5xx and network errors twice, then the failure is recorded.
- **LLM scoring:** only matches within `llm.band` (0.10) of `instant_threshold`, closest first. Signal = mean(backend focus, fit), or 0 when the level is known and not in `llm.wanted_levels`; the score moves by `scoring.weights.llm`·(2·signal - 1), at most ±0.15, and nothing is filtered by it. Answers are cached per posting under a hash of the prompt version, model, effort, resume and posting text. `llm.max_calls_per_day` (25) counts every call in the last 24 h, failures included; dry runs never call. Descriptions are cut at 16,000 characters. `LLM_API_KEY` is required by the worker, a live poll and `config check` while the layer is on; `llm.resume_path` is relative to config.yaml and checked when the config loads.
- **Countries (your request, 2026-10-25):** `filters.locations.countries` (ISO codes or English names; empty = anywhere) keeps postings with a location in those countries. `core/countries.py` places each label from tables: the ISO 3166 names in tzdata's `iso3166.tab` with common variants, US states, Canadian provinces, states abroad that labels give without a country, 575 city names (146 in the US), and regions ("Remote (EMEA)" is every EMEA country; "Remote (Global)" is anywhere). The country, state or province ending a label decides ("Paris, TX" is the US); a label ending in a city lists places ("SEA, SF, NY, Toronto"). A name shared by places in different countries (Georgia, Cambridge, Birmingham, Valencia) stays unknown, and Puerto Rico counts as the US. A label whose country is unknown sides with the posting's other labels: next to a US one it counts, next to foreign ones only it doesn't ("Pune" and "Pimpri-Chinchwad Area"). When no label's country is known, `unknown_location_passes` decides. Include and exclude regexes still apply to the labels that remain.

## Deviations from the spec layout

- Extra modules: `core/atsurls.py` (pure URL/HTML parsing, so dedupe needn't import `sources`), `core/locations.py`, `core/countries.py` (the country tables behind `filters.locations.countries`), `core/text.py`, `httpclient.py` (not `http.py`, which would shadow the stdlib name), `poller.py`, `logging_setup.py`, `db/types.py`, `devtools/fixtures.py`.
- M2 modules: `core/jobboards.py` (LinkedIn and Indeed ids from URLs), `discovery.py` (planning, caps and the miss cache around `sources/ats_detect.py`).
- Extra columns: `postings.workplace_type`, `department`, `compensation`, `tier`, `tier_reason`, `score_breakdown`; `runs.warnings`, `runs.stats`; `companies.updated_at`; `searches.active`, `searches.created_at`.
- M2 schema (migration 0002): `source_state` (feed ETag and baseline), `health_incidents`, `runs.not_modified`, `companies.discovered_from`.
- `postings.missing_count` is the only missing-poll counter; `companies.consecutive_missing` is omitted.
- `Posting.locations` is a tuple (the dataclass is frozen).
- Dependencies arrive with the milestone that needs them: JobSpy in M2, FastAPI/Jinja2 in M3. Dockerfile and compose arrive in M3.
- M3 modules: `web/security.py` (basic auth, CSRF, headers), `web/views.py` (template filters), `db/dashboard.py` (dashboard read models), `core/pay.py` (pay summaries). No schema change in M3.
- `jobpulse web` and `jobpulse digest` replace the M0 placeholders, so the M0 test that asserted the placeholders ("arrives in M3") was replaced by one that checks the real commands.
- M4 modules: `sources/linkedin_gate.py` (pacing and cool-down shared by every LinkedIn request), `sources/linkedin_guest.py`, `sources/workday.py`, `core/assessment.py` (the LLM layer's pure scoring), `llm/base.py`, `llm/prompt.py`, `llm/claude.py`, `llm/scorer.py`; `core/text.texts_by_class`.
- M4 schema (migration 0003): `companies.params` (Workday keywords), `llm_assessments`.
- Two M3 expectations changed with M4: `test_urls_that_are_never_fetched` lost its Workday case (M3 asserted Workday wasn't supported; M4 validates Workday URLs, tested in `test_a_workday_board_is_validated_with_one_call_and_offered_with_keywords`), and `test_worker_jobs_follow_the_config` now expects `workday` in the watchlist job.

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
- [x] commit `feat(m2): …` (`b2440be`)

## M3 — Dashboard and digests

- [x] dependencies: FastAPI (pinned exactly), Jinja2, uvicorn, python-multipart; htmx 2.0.11 vendored from the npm tarball, checksum verified
- [x] dashboard (FastAPI + Jinja2 + HTMX): New / Saved / Applied / Dismissed with counts, one-click status changes (HTMX, plain-form fallback), filters (source, company, remote, min score, text, open only), sort by score or freshness, paging, "posted X ago" / "first seen X ago", score breakdown, pay, also-seen-on
- [x] source-health page from `runs`: last run, last success, failures in a row, 24 h runs/fetched/new, last error, open incidents; active targets that never ran
- [x] watchlist page: companies with priority and status; paste a URL → detect (URL, then the page once) → validate (one API call) → show → set priority → add (`dashboard`); priority and pause for non-config companies
- [x] security: 127.0.0.1 by default, basic auth (constant-time) when both `DASHBOARD_USER` and `DASHBOARD_PASSWORD` are set, CSRF token (SameSite=Strict cookie + `X-CSRF-Token`/form field) on every POST, cross-site POSTs refused, CSP and other headers, secrets redacted from every rendered value
- [x] `digest.py`: digest-tier rows plus instant rows whose push window passed; open, `New` and not blocked; claimed with `digest_sent_at` before sending and released if the send fails, so a posting is in at most one digest; one ntfy/Discord message with a dashboard link
- [x] worker: digest at `schedules.digest_times` in `timezone`; watchdog exits non-zero when a scheduled job hasn't finished within 3× the longest interval
- [x] dashboard and digest leave out blocked companies (rows decided before the company was blocked)
- [x] CLI: `web [--host] [--port]`, `digest [--dry-run]`
- [x] Dockerfile (python:3.12-slim, pinned uv, non-root, `uv sync --frozen --no-dev --extra postgres`), `.dockerignore`, docker-compose (`migrate` → `worker` + `web` on 127.0.0.1:8000, `postgres` profile)
- [x] README complete (guardrails, uv and Docker setup, `.env`/config reference, ntfy topic and phone, adding companies, adding an adapter, Tailscale); optional ruff hook
- [x] tests: routes (status transitions, filters, sort, auth on/off, CSRF), watchlist endpoint with respx, digest content and at most one digest, watchdog, digest schedule, CLI
- [x] verify: `uv run jobpulse web` answers on 127.0.0.1:8000; `docker compose up` and `--profile postgres` (OrbStack, your OK 2026-10-24)
- [x] tests: 758 pass (154 new), 11 live skipped
- [x] commit `feat(m3): …`

## M4 — LinkedIn job pages, Workday, LLM layer (your request, 2026-10-24)

- [x] VERIFY, read-only probes: Workday CXS (limit cap, `total` per page, sort order, case, hosts, errors, detail) and one LinkedIn guest job page (`num-applicants__caption`, description, criteria)
- [x] dependency: `anthropic>=1.15,<2` (1.15.0, the newest before `exclude-newer`; it brings `httpx2`)
- [x] schema (migration 0003): `companies.params` (Workday keywords), `llm_assessments` (the per-posting cache and the call log the daily cap counts)
- [x] shared LinkedIn gate: one request at a time, a 4–8 s pause, a cool-down after a 429 that searches and job pages both respect; the HTTP client keeps no cookies
- [x] `sources/linkedin_guest.py`: for LinkedIn postings that pass the prescreen, the public job page one at a time; description (with the criteria list) and applicant line from one request; a 429, 999, 403 or sign-in redirect stops job pages for the rest of the cycle and starts the cool-down; at most `linkedin.max_job_pages_per_hour`; JobSpy's `fetch_description` stays off
- [x] applicants feed scoring (`scoring.applicant_cap`) and are stored as label/min/max
- [x] `sources/workday.py`: `limit` hard-capped at 20, `total` read from the first page only, `searchText` per configured keyword (never complete, so no closures) or every page (complete only when the count reaches `total`), `workday.max_pages`, a suspicious empty first page is a failure, `postedOn` at day precision; detail call for survivors: description, `startDate`, real locations, remote type
- [x] Workday everywhere else: config tokens (`tenant.wdN/site`, or the board URL), keywords per entry (a change resets the baseline), job URLs canonicalize to `workday:` keys so feed and job-board copies merge, the dashboard paste box detects, validates (one call) and adds Workday boards with keywords; discovery leaves Workday alone
- [x] `llm/`: off by default; a provider interface with a Claude client (Anthropic SDK, structured output, refusal fallback); only rule survivors within `llm.band` of `instant_threshold`; level, backend focus and resume fit move the score by up to ±`scoring.weights.llm`; cached per posting; `llm.max_calls_per_day`; dry runs never call; `LLM_API_KEY` from `.env`
- [x] tests first, run red: applicant pages, Workday paging (never `limit > 20`) and parsing, the LLM layer (off by default, the band only, cached per posting, the daily cap), config, migration, cycle integration, dashboard
- [x] live smoke tests: a Workday board, a LinkedIn job page; VERIFY results in CLAUDE.md
- [x] README, `config.example.yaml`, `.env.example`, CLAUDE.md
- [x] `scripts/check.sh` green, commit `feat(m4): …`

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
- **2026-10-24** — M3 done.
  - **Test-first:** against interface stubs, 152 new tests failed, all on missing behavior (130 `NotImplementedError`, 12 CLI placeholder assertions, 5 missing Docker files, 4 from the worker not knowing daily jobs). One test bug was fixed on the way: it blocked a watched company, which is a config error. Now 758 tests pass (11 live skipped).
  - **One M0 test replaced:** `test_later_milestone_commands_exit_2` asserted the M3 placeholders; `test_m3_commands_replace_their_placeholders` checks the real commands instead.
  - **Checked in a real browser** on a scratch database (the recorded scenario plus demo rows): one-click status changes with undo and live counts, the HTMX error notice, the watchlist paste box against the live Greenhouse API (Discord, 46 jobs) with add and pause, the health page, phone width and light mode. No console or CSP errors.
  - **Docker on OrbStack (your OK):** the image is 585 MB (pandas for JobSpy). With SQLite, `migrate` ran and exited 0, the worker scheduled the watchlist and the 09:00/18:00 digest and silently baselined 46 Discord postings, and the dashboard was healthy on 127.0.0.1:8000, all as uid 10001. With the `postgres` profile, migrations ran on Postgres and one worker cycle stored 46 postings. Scratch config and volumes were removed afterwards.
  - **Found and fixed:** a CSRF test that built a "wrong" token by changing its last character to `A`, which isn't wrong 1 time in 64; ruff 0.17 also formats Python blocks in Markdown (README).
  - **Ruff hook** in `.claude/settings.json`: after Edit/Write on a `.py` file in the repo, `ruff check --fix` and `ruff format` run on it (verified to fire).
  - **Not done (small):** `HEAD /` answers 405 (FastAPI GET routes don't take HEAD); the `runs` table has no retention (about 1,400 rows a day with the example watchlist). Both done the same day, below.
- **2026-10-24** — HEAD and run retention (your request, after M3).
  - **Test-first:** 17 new tests failed on missing behavior (no `retention` section, no prune, 405 on HEAD); the static-file HEAD case already passed. Now 776 tests pass (11 live skipped).
  - GET pages are declared through `routes.page`, which registers GET and HEAD. HEAD still needs basic auth when it's on; `/healthz` stays exempt.
  - `retention.runs_days` (30) prunes `runs` once a day in the poller and reports it as a `housekeeping:` line in the cycle log.
- **2026-10-24** — M4 done (your request: "Proceed with Milestone 4").
  - **Probed first, read-only:** Workday CXS on NVIDIA, Salesforce, Workday, Intel and Microchip; one LinkedIn guest job page; the Simplify feed's Workday URL forms. Findings in CLAUDE.md.
  - **Test-first:** against interface stubs, 196 new or changed tests failed: 132 on `NotImplementedError`, 64 on behavior not built yet (Workday tokens rejected by the slug rule, no Workday canonical keys, no LLM layer, no Workday in the watchlist job). Now 999 pass (224 new), 15 live skipped.
  - **Mistakes in my new tests, fixed on the way:** respx passes a URL regex's named groups to side effects as keyword arguments; one stack keyword scores 0.125 with `stack_saturation` 2, not 0.25; "Posted 2 Days Ago" is about 38 h old at 14:00 under the day-precision rule, so the stale case uses 3 days; one LLM band posting had a Python description that took it out of the band.
  - **Live:** `JOBPULSE_LIVE=1 uv run pytest -m live`: 15 passed (the 11 earlier, 3 Workday, 1 LinkedIn job page).
  - **Real runs on scratch databases:** Workday's own board (keywords backend and platform: 386 jobs, because "platform" matches every description there) and NVIDIA (backend: 135) baselined, then polled again with 0 new. The recorded scenario reads its LinkedIn job page and scores "Be among the first 25 applicants" (+0.0875).
  - **Checked in the built-in browser** on a scratch database: pasting a Salesforce Workday job URL found `salesforce.wd12/External_Career_Site` (1,578 jobs, with the keywords warning); adding it with two keywords stored them, and its next dry run read 131 matches instead of 1,578. No console errors.
  - **httpx2:** `anthropic` 1.x depends on it and Starlette's TestClient switched to it; the suite passed unchanged, and the filter for the old fallback warning is gone.
  - **Your running worker and dashboard** (started 11:27 from this checkout) still run M3 code, and `data/jobpulse.db` is untouched at migration 0002. The template changes use only fields and filters the running dashboard has.
  - **Not done:** no real LLM call (`LLM_API_KEY` is empty in `.env`, and each call costs money); the Claude client's requests were checked against a mock transport. The Docker image wasn't rebuilt (that needs OrbStack).
  - **Open:** `mypy tests` (not part of check.sh) reports three errors in older test files.
- **2026-10-24** — Digest removed (your request: every match in the regular notifications, no per-cycle cap, the threshold kept for the LLM).
  - Removed `digest.py`, the `digest` command and worker job, the scheduler's daily-times schedule, the digest and overflow messages, and the config keys above; `config.yaml` lost `schedules.digest_times`, `notify.max_instant_per_cycle` and `notify.dashboard_url`, which would now be config errors.
  - `evaluate` and the LLM layer always return `instant` for a match; the reason is `score 0.48` without a threshold comparison.
  - Migration 0004, tested on SQLite: `digest` rows become `instant`, digested rows keep that time as `notified_at`, `digest_sent_at` is dropped. After it, undigested rows from the last `freshness_hours` are pushed by the next cycle.
  - New: a failed push isn't retried once the posting is triaged or closed.
  - Not done: `data/jobpulse.db` isn't migrated, and the running worker and dashboard still run the old code until they are restarted after `jobpulse db upgrade`.
- **2026-10-25** — Dismissed postings reviewed, and a country filter (your request).
  - **Read-only review** of `data/jobpulse.db`: 41 postings Dismissed, 15 Applied, none left New.
    - 19 of the 41 were outside the US, in 13 countries. All came from LinkedIn, in cycles where `ml-engineer-remote` found new postings (the other LinkedIn searches are for San Francisco); LinkedIn searches its location, "Remote", worldwide. None of your 15 applications was abroad. Your city excludes catch 9 of the 19.
    - Other trends: 10 dismissed postings asked for 6+ years (no application did; `years_mode: penalize` only lowers the score, and every match is pushed), 13 came from recruiting agencies (2 applications did: Actalent, Dex), 4 needed a security clearance (no application did), and two jobs were pushed twice, once per city (MetroStar, Bell). With the country rule, these account for 33 of the 41 dismissals and none of the 15 applications.
  - **Test-first:** against interface stubs, 122 new tests failed, all on missing behavior (95 `NotImplementedError`, 24 assertions, 3 config checks). Now all pass.
  - **Calibrated on every stored label** (2,494 distinct, 26,037 uses): 68.6% US, 25.1% abroad, 2.8% regions or worldwide, 3.5% unknown (mostly "Remote", "Distributed", "In-Office"). Under US-only, all 15 applied postings pass and all 19 dismissed abroad are filtered; of the 18,082 stored postings that pass today's location rules, 3,362 wouldn't. One false drop found that way and fixed: `US > Arizona > Phoenix` next to Singapore.
  - `config check` now warns about a LinkedIn search whose location is "Remote", "Anywhere" or "Worldwide", and the `is_remote` warning no longer suggests that location.
  - `config.yaml`: `countries: [US]`, and with your OK, three more changes: `ml-engineer-remote` searches "United States" (its next run is a new silent baseline), `max_years_experience: 5` with `years_mode: exclude` (6+ years filtered; 5+ no longer marked down), and ExecutivePlacements.com, Torentify, Referment and Big Wave Digital - USA blocked. Replayed through the compiled rules, your triage keeps all 15 applications and stops 33 of the 41 dismissals (19 abroad, 8 blocked companies, 6 asking for 6+ years).
  - Not done: your running worker keeps the old code and config until it is restarted.
