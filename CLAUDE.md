# jobpulse — notes for Claude Code

Personal job-posting watcher. Read PROGRESS.md first: milestone checklists, decisions, log.

## Commands

- `uv sync` — install everything into `.venv` (Python 3.12)
- `uv run pytest` — tests; the network is blocked. Live tests: `JOBPULSE_LIVE=1 uv run pytest -m live`
- `uv run ruff check . && uv run ruff format --check .` — lint and format check
- `uv run mypy src` — types (strict on `jobpulse.core`)
- `scripts/check.sh` — all of the above in one go
- `uv run jobpulse poll --once --dry-run` — one poll cycle over every source (`--source` narrows it); nothing sent, nothing saved
- `uv run jobpulse --config tests/fixtures/scenarios/config.yaml poll --once --dry-run --fixtures tests/fixtures/scenarios/first_poll` — the same over recorded responses, JobSpy rows included (set `DATABASE_URL` to a scratch DB and run `db upgrade` first; `second_poll` after a live `first_poll` shows a merge)
- `uv run jobpulse worker` — the scheduler (needs `NTFY_TOPIC` when ntfy is enabled)
- `uv run jobpulse companies list` / `companies sync`, `uv run jobpulse notify test`
- `uv run alembic upgrade head` or `uv run jobpulse db upgrade` — migrate the database (the CLI refuses to run on one that is behind)

## Conventions

- Typed code everywhere. `core/` is pure (stdlib only, no I/O) and mypy-strict.
- UTC everywhere: aware datetimes only. The DB layer rejects naive values.
- Adapters return `FetchResult` and never raise for HTTP errors. A failure is `ok=False`, never an empty success.
- Secrets come only from `.env` through `Settings`. Never log, commit or render them.
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
- Alembic autogenerate renders `JSONB(astext_type=Text())` without the `sa.` prefix, and boolean server defaults as `1`/`0`, which Postgres rejects. Fix both by hand (`sa.Text()`, `sa.true()`/`sa.false()`); `test_migrations_match_the_models` catches drift.
- Typer renders help with Rich markup, so `[...]` in a help string disappears. Use parentheses.
- `ConfigError` messages must never echo secret values; the tests assert this.
- httpx encodes `str` header values as ASCII, so "·" in a title would raise. `HttpClient` sends header values as UTF-8 bytes, and ntfy reads them as UTF-8.
- `dict(session.execute(...))` fails because a SQLAlchemy `Result` has `.keys()`. Iterate the rows instead; `.tuples()` is deprecated in 2.1.
- `config.py` defines its own pydantic `LocationRules`, so it imports the core filter types as `core_filters`.
- Protocol members such as `name` are plain class attributes (`name = "lever"`), not `ClassVar`, or mypy rejects the adapter.
- The logging handler resolves `sys.stderr` when it writes, and `configure_logging` replaces only its own handler, so CliRunner and pytest capture keep working.
- respx: to change a response between polls, set the route's `return_value` or `side_effect`. Don't register the same pattern again.
- Searches are synced from config at the start of every cycle, so a `searches` row added by hand is deactivated. Integration tests use `env.add_search()`, which edits the config.
- JobSpy frames under pandas 3 hold `nan` (a float) for missing text cells; `jobspy_boards._clean` turns them into None. LinkedIn's page delay is the class attribute `jobspy.linkedin.LinkedIn.delay`, set from config on first use.
- The Simplify feed skips the in-memory ETag cache: its ETag lives in `source_state` and is saved in the same commit as that cycle's rows.
- Two postings with the same company, title and city fingerprint-merge within 30 days, even inside one source. Fixtures need distinct titles.
- Discovery's miss cache is in memory: each `poll --once` starts fresh, the worker keeps it.
- time-machine 3 doesn't patch `monotonic`, so asyncio runs in real time. Anything that sleeps takes an injected `sleep`.

## Verified API facts (2026-10-23)

Confirmed by `JOBPULSE_LIVE=1 uv run pytest -m live` (11 passed) and read-only probes.
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
- **Simplify feed:** 13 MB (2 MB gzipped). httpx asks for gzip and gets a weak ETag, which answers `If-None-Match` with 304. 19,325 records, 3,075 open (`active && is_visible`); the 14 hidden ones are all inactive. 784 open listings are dated exactly midnight UTC.
- **JobSpy 1.4.0:** failures and ignored options are in `frame.attrs["errors"/"warnings"]`, keyed by site; a board never raises, and a 429 reads `response status code 429 (blocked: use a proxy)`. LinkedIn rows never carry `job_url_direct`. `verbose=0` logs errors only, and with root logging configured JobSpy adds no handler of its own.

## When compacting

Preserve the list of modified files and the test commands above.
