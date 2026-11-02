# jobpulse — notes for Claude Code

Personal job-posting watcher. Read PROGRESS.md first: milestone checklists, decisions, log.

## Commands

- `uv sync` — install everything into `.venv` (Python 3.12)
- `uv run pytest` — tests; the network is blocked. Live tests: `JOBPULSE_LIVE=1 uv run pytest -m live`
- `uv run ruff check . && uv run ruff format --check .` — lint and format check
- `uv run mypy src` — types (strict on `jobpulse.core`)
- `scripts/check.sh` — all of the above in one go
- `uv run jobpulse poll --once --dry-run` — one poll cycle over every source (`--source` narrows it); nothing sent, nothing saved, the LLM never called
- `uv run jobpulse --config tests/fixtures/scenarios/config.yaml poll --once --dry-run --fixtures tests/fixtures/scenarios/first_poll` — the same over recorded responses, JobSpy rows included (set `DATABASE_URL` to a scratch DB, run `db upgrade` and `profile import tests/fixtures/scenarios/import.yaml` first; `second_poll` after a live `first_poll` shows a merge)
- `uv run jobpulse worker` — the scheduler; waits until a profile is onboarded, reads secrets at send time
- `uv run jobpulse profile import [FILE] [--replace] [--write-infra PATH]` / `profile export` / `profile show` — the preferences in the database (M6); import reads an old config.yaml once
- `uv run jobpulse web [--host] [--port]` — the dashboard on 127.0.0.1:8000
- `uv run jobpulse companies list`, `uv run jobpulse notify test`
- `uv run jobpulse companies import FILE --workday-keywords "..." [--dry-run]` — add the boards behind a file's links (validated like the paste box)
- `docker compose up -d` (`--profile postgres` for Postgres) — needs the Docker daemon: `orbctl start` (OrbStack) first. Test with `-p <name>` and an override file mounting a scratch config, so `./config.yaml` and its volumes stay untouched
- `uv run jobpulse embeddings download` — fetch the embedding model (bge-small-en-v1.5, 64 MB) into `data/models`; nothing else ever downloads it
- `uv run jobpulse resume set FILE` / `show [--lines]` / `clear` / `rescore [--backfill]` — the resume postings are scored against (M5b); `rescore --backfill` embeds open matches first (about 15 a second)
- `uv run python -m jobpulse.devtools.resume_eval --database-url sqlite:///<scratch copy> --resume FILE --out DIR` — the M5a resume-similarity analysis (refuses `data/jobpulse.db`)
- `uv run alembic upgrade head` or `uv run jobpulse db upgrade` — migrate the database (the CLI refuses to run on one that is behind)

## Conventions

- Typed code everywhere. `core/` is pure (stdlib only, no I/O) and mypy-strict.
- UTC everywhere: aware datetimes only. The DB layer rejects naive values.
- Adapters return `FetchResult` and never raise for HTTP errors. A failure is `ok=False`, never an empty success.
- Secrets come from `.env` through `Settings`, or from Settings in the dashboard (`credentials.SecretStore`; `.env` wins). Never log, commit or render them.
- Preferences are the database profile (`config.Profile`), infrastructure is config.yaml (`config.InfraConfig`); code takes the composite `AppConfig` (`config.compose`).
- Tests: no network (pytest-socket plus respx `assert_all_mocked`); time through time-machine or an injected clock; sleeps injected.
- Fix the code, not the tests. No test-specific branches in production code.
- Every schema change gets a migration: `uv run alembic revision --autogenerate -m "..."`, then review it and replace TypeDecorators with their impl types.

## Gotchas

- SQLite drops tzinfo, so every timestamp column uses `db.types.UTCDateTime`.
- httpx logs full URLs at INFO, and the ntfy topic and Discord token are in URLs. `logging_setup` pins httpx/httpcore to WARNING and redacts secrets from every formatted log line.
- mypy's `strict` only works globally; the `jobpulse.core.*` override spells out the strict flags.
- JSON columns: assign a new list or dict instead of mutating in place, or the change isn't saved.
- `[tool.uv] exclude-newer` freezes resolution at a date. To take newer releases, move it forward (keep it a few days back) and run `uv lock --upgrade`.
- pydantic-settings runs with `env_ignore_empty=True`, so `NTFY_TOPIC=` in `.env` counts as unset.
- Migrations run with `PRAGMA foreign_keys=ON`, so an SQLite batch rebuild (Alembic's copy, drop, rename) fires the drop's foreign-key actions: rebuilding `postings` would empty `llm_assessments` (CASCADE), and rebuilding `companies` would null `postings.company_id`. Add and drop plain columns in place with `batch_alter_table(..., recreate="never")`; anything that needs a rebuild has to turn the pragma off first. 0004 has a test for this.
- Alembic autogenerate renders `JSONB(astext_type=Text())` without the `sa.` prefix, and boolean server defaults as `1`/`0`, which Postgres rejects. Fix both by hand (`sa.Text()`, `sa.true()`/`sa.false()`); `test_migrations_match_the_models` catches drift.
- Typer renders help with Rich markup, so `[...]` in a help string disappears. Use parentheses.
- `ConfigError` messages must never echo secret values; the tests assert this.
- httpx encodes `str` header values as ASCII, so "·" in a title would raise. `HttpClient` sends header values as UTF-8 bytes, and ntfy reads them as UTF-8.
- `dict(session.execute(...))` fails because a SQLAlchemy `Result` has `.keys()`. Iterate the rows instead; `.tuples()` is deprecated in 2.1.
- `config.py` defines its own pydantic `LocationRules`, so it imports the core filter types as `core_filters`.
- Protocol members such as `name` are plain class attributes (`name = "lever"`), not `ClassVar`, or mypy rejects the adapter.
- The logging handler resolves `sys.stderr` when it writes, and `configure_logging` replaces only its own handler, so CliRunner and pytest capture keep working.
- respx: to change a response between polls, set the route's `return_value` or `side_effect`. Don't register the same pattern again. Routes match in the order they were added, and a `url__regex` route passes its named groups to `side_effect` as keyword arguments.
- Searches are synced from the profile at the start of every cycle, so a `searches` row added by hand is deactivated. Integration tests use `env.add_search()`, which edits the profile.
- JobSpy frames under pandas 3 hold `nan` (a float) for missing text cells; `jobspy_boards._clean` turns them into None. LinkedIn's page delay is the class attribute `jobspy.linkedin.LinkedIn.delay`, set from config on first use.
- Every LinkedIn request (JobSpy searches, job pages) goes through one `LinkedInGate` built in `runtime.build_adapters`. A `JobSpyAdapter` without a `gate` builds its own from `pacing`/`sleep`/`rng`/`monotonic`, which the M2 tests rely on.
- An adapter's `enrich` raises `sources.base.EnrichmentDeferred` when its detail couldn't be read now (LinkedIn's hourly cap, cool-down, a block, a network error, a 5xx). The poller leaves the row `pending` (reported as `waiting`) and the next cycle of its source tries again, until `linkedin.job_page_wait_hours`; then it's decided without the detail. Pending rows of job-board searches aren't tied to one search, so the cycle's `claimed` set hands each to the first target of its source.
- `LinkedInGate.turn(when=...)` checks `when` before the pause and yields False without pausing or counting a request. A caller that may give up must pass it, or every caller queued behind a cap or a cool-down sleeps a full pause. Tests of queuing hold `gate.turn()` while the callers start: respx answers without suspending, so plain `gather` never queues.
- `prescreen` judges locations only when the list gave some (Workday's "2 Locations" comes as none); `evaluate` judges a posting still without locations after its detail call.
- Workday's `searchText` matches descriptions too: a common word ("platform" on Workday's own board) can match every job. Keyword boards never close postings; a board without keywords closes only after a full read whose count reaches the first page's `total`.
- `Settings()` reads `./.env`; tests that build settings pass `_env_file=None`.
- The LLM layer never runs in tests against the API: `test_llm_cycle.py` drives `LlmScorer` with a fake client, and a dry run only reads cached answers. `llm_assessments` rows are both the per-posting cache and the call log the daily cap counts.
- The Simplify feed skips the in-memory ETag cache: its ETag lives in `source_state` and is saved in the same commit as that cycle's rows.
- Two postings with the same company, title and city fingerprint-merge within 30 days, even inside one source. Fixtures need distinct titles.
- Discovery's miss cache is in memory: each `poll --once` starts fresh, the worker keeps it. Integration tests build the discoverer from config; after changing `discovery` settings, call `env.rebuild_discoverer()`.
- Profile and secrets (M6): `db/profile.py` stores the profile (one row, `revision` bumped per save, `onboarded_at`, `scored_hash`); `ProfileReader` parses it again only when the revision moves and keeps the last good one when the stored data stops validating. The poller reads it at cycle start (`_load_profile`): no onboarded profile means the cycle idles (`report.idle`); a new revision recompiles rules and weights and calls `on_profile` (`runtime.follow_profile`: User-Agent contact email, and the location-alias dict every adapter shares, updated in place). The web's `WebState.current()` does the same per request. `config.yaml` may hold infrastructure only: `load_config` reports each moved key; `read_config_lenient` (db upgrade, alembic env) drops them; `load_legacy_config` (profile import) reads the whole old shape. The watchlist is DB rows only (`source_of_discovery` `dashboard`; old `config` rows become `dashboard` on import), so there's no config lock and no `companies sync`.
- Integration tests: `env.config = ...` (and `reconfigure`, `add_search`) saves the profile as a new revision; Acme and Beta are `dashboard` rows from the fixture. Use `env.watch(name, ats, token)`, `env.pause(*tokens, paused=...)` and `env.set_keywords(token, words)` for the watchlist; `make_web(config=...)` saves the profile too, and `make_web(secrets=[...])` masks values as if from .env.
- Re-scoring (M6): when `config.scoring_hash` (filters + scoring) differs from `profile.scored_hash`, the poller re-scores open New and Saved matches (`rescoring.rescore_matches`, through `core.detection.match_score`: filters aside, `now` = `decided_at`, the stored resume score, the stored LLM part kept); a `scored_hash` of NULL (fresh import) is just set. Tiers never change.
- `notify/live.LiveNotifier` resolves the profile's channels and the secrets on every send; with none set it raises `NotifierError`, so the matches stay unpushed and are retried while fresh. `llm/source.LlmSource` rebuilds the scorer when the LLM settings, resume or key change; the LLM reads the stored resume (`llm.resume_path` is gone).
- `logging_setup.SecretRegistry` holds every secret value the process knows; the log formatter and the templates' `finalize` read it on each use, and `SecretStore` adds Settings' values as they're read or saved.
- Templates that use a value the old code doesn't pass guard it (`profile_notice is defined`), because the running dashboard reloads templates before it restarts on new code.
- time-machine 3 doesn't patch `monotonic`, so asyncio runs in real time. Anything that sleeps takes an injected `sleep`.
- FastAPI 0.150 keeps an included router whole (`_IncludedRouter` in `app.routes`), so look for routes on `web.routes.pages` / `web.routes.actions`. Every POST route goes on `actions`, whose dependency checks the CSRF token.
- `anthropic` 1.x runs on `httpx2`, and installing it switched Starlette's TestClient to `httpx2` too (its responses are `httpx2.Response`). respx patches `httpx` only: it never sees the SDK's traffic, so `test_claude.py` gives the SDK an `httpx2.MockTransport`.
- Jinja reloads changed templates in a running `jobpulse web`. A template that uses a new filter or context value breaks the running dashboard until it restarts; prefer built-in filters (`format`) and fields the old code has, or say a restart is needed.
- Templates use `StrictUndefined` and a `finalize` that masks secrets and renders `None` as "". Render through `routes.render`, which adds `now`, `short_labels`, `zone` and `csrf_token`; the filters read them from the context.
- Autoescape turns `'` into `&#39;`: tests compare messages after `html.unescape`.
- HTMX out-of-band swaps of table rows need a `<template>` wrapper (see `_detect.html`).
- Hrefs to source data go through the `http_url` filter: feeds are third-party, and a `javascript:` URL must never become a link.
- Run history is pruned at most once a day per `Poller` (an in-memory timestamp), never in a dry run. Each `env.poll()` builds a new `Poller`; to test the once-a-day rule, keep one `env.poller()`.
- `filters.locations.countries` places labels with the tables in `core/countries.py` (pure, no gazetteer). A label whose country is unknown sides with the posting's other labels. To fix a misplaced label, add the name to a table and a case to `test_countries.py`; leave out a name shared by places in different countries.
- The dashboard's location and pay filters run in Python over the ids and labels the SQL filters leave (`dashboard._matches`); a new filter that SQL can express belongs in `_conditions`. Comparing `coalesce(...)` of timestamps with a bound time needs `type_=UTCDateTime()`, or SQLite compares the wrong format.
- Place parsing below the country is `core/places.py` (tables of city states, areas, metro areas); fix a misplaced label there with a case in `test_places.py`.
- The blocklist is the profile's plus `blocked_companies` rows: the dashboard merges them per request (`routes._blocklist`), the poller at the start of each cycle. `Snapshot.blocklist` (from `WebState.current()`) is the profile's only; `profile import` turns config.yaml's names into rows and keeps its boards in the profile.
- `repo.set_status` takes `now` and sets `status_changed_at`; bulk moves use `repo.set_statuses`.
- htmx logs a console error for an out-of-band swap whose target is missing; to change cards that may not be on the page, send an `HX-Trigger` event and handle it in `static/app.js`.
- There is no digest and no per-cycle push cap: every match (tier `instant`) is pushed, whatever its score. `scoring.instant_threshold` only centers the LLM band. A push that failed is retried while the posting is fresh, open and still `New`.
- A cycle sends its matches as one message (`format.batch_notification`: "3 new job postings were found via watchlist. Top score: Stripe - 0.87"), and only matches from its own sources, so a failed push is retried by the same scheduled job. `report.pushes` keeps each match's title line; integration tests read them from `env.pushed`, while `env.notifier.sent` holds the batch messages (and health alerts).

- A redirect whose final URL path contains "maintenance" is a `maintenance` failure (`HttpAdapter._send`). Each HTTP source's `MaintenanceGate` (built in `runtime.build_adapters`, paced by its `sleep`) retries it, then pauses the source: its targets come back `skipped`, with no run. Health ignores maintenance runs per target and opens one incident for the source (target `*`). respx never suspends, so in tests the first target finishes its retries before the next starts, and the others are skipped.
- `HealthMonitor.review` groups due messages by (source, kind, recovery): several boards failing or recovering together go out as one message, and `DueMessage.incident_ids` are marked sent together.
- `postings.repost_reason` is set only when a posting is decided (`save_evaluation`): LinkedIn's "Reposted …" marker from its page, else `repo.earlier_posting` (same Greenhouse requisition on the board, first seen 7+ days earlier, open, closed or merged). Same company, title and city is deliberately not a signal (your decision 2026-11-01: several openings share generic titles). The dashboard hides reposts in New only (`dashboard.REPOST_FILTER_STATUSES`), and New's count leaves them out. LinkedIn's wording was not yet seen live (2 pages read 2026-11-01, neither a repost).
- `postings.employment_type` is set at insert and again in `save_evaluation` (LinkedIn's criteria line comes with the page). Migration 0006 keeps a copy of `core/employment.py` and `core/reposts.requisition`, and a test compares them: a later rule change needs a new backfill migration, with 0006's samples narrowed to cases both agree on.
- `repo.py` imports `typing.cast`; for an SQL cast use the column's `.cast(...)`.
- Saved filters (M5.1): `db/query_params.py` holds `parse_query` (moved from routes) and `filter_params`, the full write-out a saved filter stores; `saved_query` reads one back only if it round-trips exactly. Pushes and card bullets both go through `saved_filters.fitting` → `dashboard.matching_ids`, so a push and a click agree. A filter is *present* while its row exists: any present (even all off, or invalid) means per-posting pushes (`Poller._push_saved`), none means today's batch. A filter fires only for postings whose `coalesce(decided_at, first_seen_at)` is at or after its `fires_from` (moved on create, filter change and switch-on). The summary is sent first; only per-posting messages write `filter_pushes`, which the daily cap counts.
- `filter_pushes` and `posting_vectors` cascade from `postings` too: a batch rebuild of `postings` would empty them, like `llm_assessments`.
- Static files are served `Cache-Control: no-cache` (revalidated by ETag), so a changed `app.js` reaches a running dashboard's browsers on the next load. The slot colors live in `core/palette.py` and, hand-copied, in `app.css`; `test_palette_css.py` keeps them in sync.
- Integration tests: `env.save_filter(label, slot, **params)` saves a filter a minute before `NOW`; `env.sleep` records the pushes' spacing. Titles like "Backend Engineer 4" read as a level and get filtered; use letters for distinct test titles.

- Resume similarity (M5a): `core/similarity.py` is pure (vectors in, `array('f')`, L2-normalized). `resume_lines` drops headings, contact lines and job headers, so the line shown for a match is never personal information; keep it that way. `FastEmbedEmbedder` loads with `local_files_only=True` and raises `EmbedderUnavailable` when the model isn't cached; tests use `tests.factories.FakeEmbedder`. Resume files are never committed: keep them in the scratchpad.
- Resume scoring (M5b): a match's `resume` part (`scoring.weights.resume`, 0.25) replaces its `stack` part while a resume is stored and the posting has a description; the breakdown has a `resume` key only then. The score is M5a's best variant (the description as one block against the mean resume chunk, `similarity.resume_raw`); the card's line is the single closest resume line. Calibration anchors are measured on open matches at each re-score once 50 have vectors, else `DEFAULT_ANCHORS`. `resume_scoring.py` holds what the poller and `jobpulse resume` share; `db/resume.py` the single-row `resume` table and `posting_vectors` (float32 blobs, keyed by model and input hash). The poller re-scores at cycle start when the resume's `rescored_at` is NULL (a new resume or model), under one lock; only open New and Saved matches change. `build_embedder` returns a `LazyEmbedder`: nothing loads until a resume exists. Tests pass `env.embedder = FakeEmbedder()` and `env.set_resume(text)`; the test config's stack saturation is 2, so a lone python hit is a 0.125 stack part.

## Verified API facts (2026-10-23, Workday and LinkedIn job pages 2026-10-24)

Confirmed by `JOBPULSE_LIVE=1 uv run pytest -m live` (15 passed) and read-only probes.
- **Greenhouse:**
  - The list without `content=true` already carries `first_published` on every job, so the freshness gate runs on list data.
  - The per-job endpoint's `content` is entity-escaped HTML.
  - `absolute_url` is often a custom domain with `?gh_jid=`.
  - Unknown token → 404 JSON.
  - EU boards on `job-boards.eu.greenhouse.io` (for example `proton`) are served by the global `boards-api.greenhouse.io`; `boards-api.eu.greenhouse.io` doesn't resolve.
- **Lever:**
  - A bare array with no default page cap; `createdAt` is epoch ms; requirements live in `lists[]`.
  - EU boards answer only on `api.eu.lever.co`: `olx` → 404 on the US host, 200 on the EU host.
  - Unknown slug → 404 JSON.
  - Lever doesn't gzip; Palantir's board is 6 MB.
- **Ashby:**
  - `id` is always present and equals the `jobUrl` UUID.
  - `isRemote` is also true for Hybrid jobs, so `workplaceType` decides.
  - Unknown slug → 404 plain text. OpenAI's board is 14 MB.
- **All three:** they send ETags, and the live test confirms they answer `If-None-Match` with 304. No rate-limit headers were seen.
- **Conditional requests (probe 2026-11-01):** Greenhouse, Ashby and Lever answer every unchanged poll's `If-None-Match` with 304. Ashby's API sits behind Cloudflare (`public, max-age=60, stale-while-revalidate=60`), Greenhouse behind CloudFront (`max-age=0, private, must-revalidate`, weak ETags). `HttpClient` replays a 304 as a 200 (`X-Jobpulse-Replayed`), so ATS runs never record `not_modified`; only Simplify's do.
- **Simplify feed:** 13 MB (2 MB gzipped). httpx asks for gzip and gets a weak ETag, which answers `If-None-Match` with 304. 19,325 records, 3,075 open (`active && is_visible`); the 14 hidden ones are all inactive. 784 open listings are dated exactly midnight UTC.
- **JobSpy 1.4.0:** failures and ignored options are in `frame.attrs["errors"/"warnings"]`, keyed by site; a board never raises, and a 429 reads `response status code 429 (blocked: use a proxy)`. LinkedIn rows never carry `job_url_direct`. `verbose=0` logs errors only, and with root logging configured JobSpy adds no handler of its own.
- **Workday CXS** (NVIDIA, Salesforce, Workday, Intel, Microchip):
  - Maintenance (2026-10-31, every tenant): `POST .../jobs` answers 303 to `community.workday.com/maintenance-page`, which lands on `static.community.workday.com/maintenance-page.html` (HTML).
  - `POST .../wday/cxs/{tenant}/{site}/jobs`: `limit: 21` answers HTTP 400; `total` is filled on the first page only (later pages say 0). No ETag.
  - Items: `title`, `externalPath`, `locationsText` ("2 Locations" for several, often "US, CA, Santa Clara"), `postedOn`, `bulletFields` (requisition id), sometimes `remoteType` ("Flex", "Remote").
  - Mostly newest first, but not strictly (4 out of order in 120), so a partial read can't be trusted.
  - `searchText` (probed 2026-10-28 on NVIDIA, Salesforce, CVS, Leidos, Associated Bank): fuzzy and ignores quotes. "full stack" finds a bank's "Full Time" jobs, "machine learning" finds CVS's "Pharmacy Intern", and "fullstack" finds almost nothing. Keyword results come newest first on Salesforce and CVS but by relevance on NVIDIA (a job posted yesterday sat near result 1,000), so a keyword finding more than `workday.max_pages` × 20 jobs can hide new ones. `total` caps at 2,000.
  - The site path is case-insensitive. An unknown site answers 404 JSON; an unknown tenant or wrong `wdN` host answers 422.
  - `wdN.myworkdaysite.com/recruiting/{tenant}/{site}` tenants answer on `{tenant}.wdN.myworkdayjobs.com` too.
  - Detail `GET {api}{externalPath}` keys on the last path segment (any location slug works) and returns `jobPostingInfo` with `jobDescription` (HTML), `startDate` (a date), `location`, `additionalLocations`, `remoteType`, `jobReqId`.
  - 928 of 2,939 open Simplify listings link to Workday, nearly all on `{tenant}.wdN.myworkdayjobs.com`.
- **LinkedIn search location:** "Remote" is searched worldwide. On 2026-10-25, `ml-engineer-remote` brought postings from 13 countries; JobSpy 1.4.0 ignores `is_remote` for LinkedIn.
- **LinkedIn guest job page** (`/jobs-guest/jobs/api/jobPosting/{id}`): 200 HTML without login, with jobpulse's own User-Agent. The applicant line is `<figcaption class="num-applicants__caption">` (or a `span`); the description is `div.show-more-less-html__markup`, followed by the criteria list. It sets session cookies, which jobpulse never keeps. No external apply URL on the page.

## When compacting

Preserve the list of modified files and the test commands above.
