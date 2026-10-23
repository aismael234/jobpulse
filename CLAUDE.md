# jobpulse — notes for Claude Code

Personal job-posting watcher. Read PROGRESS.md first: milestone checklists, decisions, log.

## Commands

- `uv sync` — install everything into `.venv` (Python 3.12)
- `uv run pytest` — tests; the network is blocked. Live tests: `JOBPULSE_LIVE=1 uv run pytest -m live`
- `uv run ruff check . && uv run ruff format --check .` — lint and format check
- `uv run mypy src` — types (strict on `jobpulse.core`)
- `scripts/check.sh` — all of the above in one go
- `uv run jobpulse poll --once --dry-run` — one poll cycle; nothing sent, nothing saved (M1+)
- `uv run alembic upgrade head` or `uv run jobpulse db upgrade` — migrate the database

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

## Verified API facts (2026-10-23)

- **Greenhouse:** the list without `content=true` already carries `first_published` on every job. `content` from the per-job endpoint is entity-escaped HTML. `absolute_url` is often a custom domain with `?gh_jid=`. Unknown token → 404 JSON. EU boards on `job-boards.eu.greenhouse.io` are served by `boards-api.greenhouse.io`; `boards-api.eu.greenhouse.io` doesn't resolve.
- **Lever:** a bare array with no default page cap; `createdAt` is epoch ms; requirements live in `lists[]`. EU boards answer only on `api.eu.lever.co` (US host → 404). Unknown slug → 404 JSON. Lever doesn't gzip; Palantir's board is 6 MB.
- **Ashby:** `id` is always present and equals the `jobUrl` UUID. `isRemote` is also true for Hybrid jobs. Unknown slug → 404 plain text. OpenAI's board is 14 MB.
- **All three** send ETags and answer `If-None-Match` with 304. No rate-limit headers were seen.
- **Simplify feed:** 13 MB, an ETag, and 304 on `If-None-Match`.

## When compacting

Preserve the list of modified files and the test commands above.
