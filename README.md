# jobpulse

A personal job-posting watcher for one person on one always-on machine. It polls public applicant-tracking-system boards (Greenhouse, Lever, Ashby, Workday), the SimplifyJobs New-Grad list and a few Indeed/LinkedIn searches, finds postings it hasn't seen, filters and scores them, and pushes the strong matches to your phone within minutes. The rest go into a twice-daily digest and a small dashboard.

## What it does, and what it never does

It **detects and notifies**. It never applies, fills in forms or submits anything. On top of that:

- No logged-in LinkedIn or Indeed sessions, no cookies from a real account, no accounts of any kind. LinkedIn job pages are read as a guest, and jobpulse keeps no cookies at all.
- No CAPTCHA solving, proxies, proxy rotation or anti-detection tricks. Glassdoor, ZipRecruiter and Google Jobs are never queried.
- LinkedIn (searches and job pages alike) is asked one request at a time with a random 4–8 s pause, and backs off for 30 minutes after a 429.
- A failed, blocked, empty or errored response is recorded as a failure. It never closes a posting or counts as "no new jobs".
- Every ATS request carries a descriptive User-Agent with your contact email.
- Secrets (ntfy topic, Discord webhook, LLM API key, dashboard password) come only from `.env`. They are never committed, logged or rendered in a page.
- The optional LLM layer is off unless you turn it on. When it's on, the postings close to your threshold, and your resume, are sent to Anthropic's API; nothing else is.

## How it works

1. **Sources.** The worker polls each source on its own schedule with random jitter: your watchlist boards every 30 min, the SimplifyJobs feed every 30 min (an unchanged feed costs one tiny conditional request), job-board searches every 60–90 min asking for the last 3 hours, so a late or failed run leaves no gap.
2. **New or not.** Each posting is stored under its source's own stable id. It's new exactly when that insert succeeds.
3. **Silent baseline.** The first successful poll of a new board, search or feed stores everything and notifies about nothing.
4. **Freshness.** Only postings younger than 48 h (by the source's own timestamp when there is one) can alert, so reopened listings and old reposts stay quiet.
5. **Dedupe.** A LinkedIn, Indeed or SimplifyJobs copy of a job you already have from its ATS board (same canonical URL, a `gh_jid` link, or the same company, title and city within 30 days) is merged onto the ATS posting instead of alerting twice.
6. **Details.** New postings that pass the title and location rules get one detail call: a Greenhouse or Workday job's description, and a LinkedIn job's public page, which also gives its applicant count ("Be among the first 25 applicants").
7. **Filters and score.** Title, level, years-of-experience, stack and location rules from `config.yaml`, plus the applicant count when LinkedIn gave one. A score at or above `instant_threshold` is pushed right away; other passing postings go to the digest and the dashboard.
8. **A second opinion (optional).** With the LLM layer on, postings that score close to the threshold are read by Claude against your resume, and its answer moves the score a little up or down (see "The LLM layer").
9. **Closures.** A job missing from two complete, successful polls of its board is marked closed (and reopened if it comes back). Job-board results, and Workday boards searched by keyword, never close anything.
10. **Health.** One alert when a target fails 3 runs in a row or a normally busy one returns nothing for 24 h, and one message when it recovers.
11. **Discovery.** Greenhouse, Lever and Ashby boards linked from the feed and from job-board postings are added to the watchlist automatically when one of their postings passes your filters. Workday boards are only added by you.

## Quick start (uv)

You need [uv](https://docs.astral.sh/uv/). It installs Python 3.12 for you.

```bash
uv sync
cp config.example.yaml config.yaml        # then edit contact_email, filters and watchlist
cp .env.example .env                      # then set NTFY_TOPIC (see "Phone notifications")
uv run jobpulse db upgrade
uv run jobpulse config check
uv run jobpulse poll --once --dry-run     # fetch and evaluate everything; save and send nothing
uv run jobpulse notify test               # one test push to your phone
uv run jobpulse worker                    # poll on schedule, push, send the digest
uv run jobpulse web                       # the dashboard on http://127.0.0.1:8000
```

The worker and the dashboard are separate processes; run both (two terminals, or Docker below). The first poll of each board, search and feed is a silent baseline, so the first pushes arrive once something new is posted.

**Upgrading:** after pulling a new version, stop the worker and the dashboard, run `uv sync` and `uv run jobpulse db upgrade`, then start them again. The commands refuse a database that is behind the code.

## Run it with Docker

The image holds the worker, the dashboard and the migrations. `config.yaml` and `.env` stay on your machine: compose mounts the first read-only and passes the second as environment variables.

```bash
cp config.example.yaml config.yaml        # edit it
cp .env.example .env                      # set NTFY_TOPIC
docker compose up -d                      # migrate, then worker + web
docker compose logs -f worker             # scheduled jobs and every poll
```

- `migrate` applies migrations and exits; `worker` and `web` start after it succeeds.
- The dashboard is published on `127.0.0.1:8000` only.
- The SQLite database lives in the `jobpulse-data` volume.
- If a source stops finishing runs (APScheduler can stop scheduling after an error), the worker's watchdog exits non-zero and Docker restarts it.
- To upgrade: `git pull && docker compose up -d --build`. Migrations run first.
- With the LLM layer on, mount your resume next to the config: add `- ./resume.txt:/app/resume.txt:ro` to the `volumes:` list at the top of `docker-compose.yml`, and set `llm.resume_path: resume.txt`.

**Postgres instead of SQLite:** put a password in `.env` twice, then start the `postgres` profile:

```bash
# .env
POSTGRES_PASSWORD=<a long random password>
DATABASE_URL=postgresql+psycopg://jobpulse:<the same password>@postgres:5432/jobpulse
```

```bash
docker compose --profile postgres up -d
```

Migrations wait for Postgres to be healthy and run on startup. Existing SQLite data isn't copied over.

## Phone notifications with ntfy

[ntfy](https://ntfy.sh) pushes to the ntfy app on Android and iOS. A public topic on ntfy.sh can be read by anyone who knows its name, so the name has to be long and random:

```bash
python3 -c "import secrets; print('jobpulse-' + secrets.token_urlsafe(24))"
```

1. Put the result in `.env` as `NTFY_TOPIC=...`. jobpulse refuses topics shorter than 24 characters.
2. Install the ntfy app, tap **+**, and subscribe to that exact topic on `https://ntfy.sh`.
3. Run `uv run jobpulse notify test` (or `docker compose run --rm worker jobpulse notify test`).

A push looks like `Stripe · Backend Engineer · SF / Remote · posted 25 min ago`; tapping it opens the apply page. At most `notify.max_instant_per_cycle` (10) postings are pushed per cycle; the rest wait for the digest, with one "N more" push.

**Self-hosting ntfy:** set `notify.ntfy.base_url` to your server, and `NTFY_TOKEN` in `.env` if it uses access control. Subscribe the app to your server instead of ntfy.sh.

**Discord as well (or instead):** create a webhook (Server Settings → Integrations → Webhooks), put it in `.env` as `DISCORD_WEBHOOK_URL`, and add `discord` to `notify.channels`.

**The digest** goes out at `schedules.digest_times` (09:00 and 18:00) in `timezone`: one message listing the matches that didn't get their own push, best first, with a link to the dashboard (`notify.dashboard_url`). A posting appears in at most one digest. `uv run jobpulse digest --dry-run` shows what the next one would say.

## The dashboard

`uv run jobpulse web` serves it on `http://127.0.0.1:8000`.

- **New / Saved / Applied / Dismissed.** New holds the matches nobody has triaged yet. One click moves a posting between views (with undo). Each card shows the score (hover for its breakdown), pay when the source gives it, "posted 25 min ago" (or "first seen 3 h ago" when the source has no timestamp), and where else the job was seen.
- **Filters:** text (title, company, description), company, source, remote, minimum score, open only; sort by score or newest.
- **Health:** per board, search and feed: last run, last success, failures in a row, the last 24 h of runs, fetched and new postings, the last error, and open incidents.
- **Watchlist:** every company with its priority, status, origin and baseline. Paste a careers URL to add a board (see below); a Workday board gets a keywords field. Priority and pause apply to companies that didn't come from `config.yaml`.
- A posting the LLM layer assessed shows "LLM +0.12" (or minus); hover for its verdict.

Companies on your blocklist are hidden everywhere except Applied.

**Security.** It listens on 127.0.0.1 unless you say otherwise. Set both `DASHBOARD_USER` and `DASHBOARD_PASSWORD` in `.env` to turn on HTTP basic auth (setting only one is an error). Every POST needs the page's CSRF token, which lives in a `SameSite=Strict` cookie. Pages load scripts and styles only from the dashboard itself (htmx 2.0.11 is vendored), under a strict Content-Security-Policy.

## Reach the dashboard from your phone with Tailscale

Keep the dashboard on 127.0.0.1 and let [Tailscale](https://tailscale.com) carry it to your devices, encrypted and private to your tailnet:

1. Install Tailscale on the always-on machine and on your phone, signed in to the same tailnet.
2. On the machine, serve the dashboard to your tailnet over HTTPS:

   ```bash
   tailscale serve --bg 8000
   ```

   It prints the address, `https://<machine>.<tailnet>.ts.net/`. Allow HTTPS certificates in the Tailscale admin console if it asks.
3. Set `notify.dashboard_url` in `config.yaml` to that address, so digests link to it.
4. Optionally add `DASHBOARD_USER` and `DASHBOARD_PASSWORD` for a password on top.

Never use `tailscale funnel` for this: it would put the dashboard on the public internet.

## Adding companies

- **In `config.yaml`:** add `{name, ats, token, priority}` under `watchlist:`. The token is the board name in the URL: `job-boards.greenhouse.io/<token>`, `jobs.lever.co/<token>` (add `region: eu` for `jobs.eu.lever.co`), `jobs.ashbyhq.com/<token>`. Every poll applies the list; removing an entry deactivates the company and keeps its history.
- **Workday** boards are `{name, ats: workday, token: <tenant>.wd<N>/<site>, keywords: [...]}`: for `https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite` the token is `nvidia.wd5/NVIDIAExternalCareerSite` (pasting the URL as the token works too). Big tenants have thousands of jobs, so give them `keywords`: each one is a search on the board, and only its matches are polled. A board searched by keyword never closes postings (it never sees the whole board); one without keywords is paged through, at most `workday.max_pages` (25) pages of 20 jobs, and closes jobs only after a full read. Changing the keywords starts a new silent baseline.
- **From the dashboard:** paste a board, job or careers-page URL on the Watchlist page. jobpulse recognizes the board from the URL, or reads the page once to find a Greenhouse, Lever, Ashby or Workday link or embed. It checks the board with one API call, shows the company and its open jobs, and adds it with the priority (and, for Workday, the keywords) you choose.
- **Automatically:** discovery adds boards linked from the SimplifyJobs feed and job-board postings when one of their postings passes your title and location filters. It adds at most `discovery.max_auto_companies` (50) and skips boards with more than `max_board_jobs` (1,000) open jobs. `uv run jobpulse companies list` shows where each company came from.

Every new company's first poll is a silent baseline. Workday boards are never added by discovery. To never hear about a company again, add it to `blocklist.companies` (or a board to `blocklist.boards`): its postings are kept but never pushed or put in a digest, and discovery leaves it alone.

## The LLM layer (optional)

Off by default. When it's on, postings whose rule score lands within `llm.band` (0.10) of `instant_threshold` (the ones the rules can't call) are sent to Claude together with your resume. Claude answers with the level the role really hires for, how much of it is backend work (0–100) and how well your resume fits it (0–100). The two scores average into a signal that moves the rule score by up to `scoring.weights.llm` (0.15) either way; a level that isn't in `llm.wanted_levels` counts as a signal of 0. So a borderline posting can be lifted to an instant push or dropped to the digest; nothing is ever filtered out by it.

To turn it on:

1. Save your resume as plain text, for example `data/resume.txt` (`data/` is gitignored; keep it out of git wherever it lives).
2. Put your Anthropic API key in `.env` as `LLM_API_KEY=...`.
3. In `config.yaml`:

   ```yaml
   llm:
     enabled: true
     resume_path: data/resume.txt    # relative to config.yaml
     wanted_levels: [entry, mid]     # intern, entry, mid, senior, staff, principal, manager; [] = any
   ```

4. Run `uv run jobpulse config check`.

Cost guards: at most `llm.max_calls_per_day` (25) calls in any 24 hours, failed ones included; every answer is cached per posting, so a posting is never asked about twice (a changed resume, model or effort asks again); descriptions are cut at 16,000 characters; a dry run never calls (it uses cached answers only). It uses `claude-opus-5-5` at `effort: low` by default; `llm.model` and `llm.effort` change that. The cycle log says how many postings were answered, came from the cache, or were left to the rules, and an invalid key stops the asking until the worker restarts.

## Configuration

### `config.yaml`

`config.example.yaml` documents every key. The sections:

| Section | What it controls |
|---|---|
| `contact_email` | Sent in the User-Agent so API operators can reach you. Required. |
| `timezone` | Digest times and the dashboard's displayed times. Storage is always UTC. |
| `database_url` | SQLite by default (`sqlite:///data/jobpulse.db`, WAL mode). `DATABASE_URL` overrides it. |
| `http` | Timeouts, retries with backoff, and concurrent requests per host. |
| `schedules` | Each source group's interval and jitter, and `digest_times`. |
| `detection` | `freshness_hours` (48), closure after missing polls (2), fingerprint window (30 days). |
| `filters` | Title include/exclude regexes, level words, years of experience, stack keywords, location rules and aliases. |
| `scoring` | Score weights (including `applicants` and `llm`), `priority_cap`, `applicant_cap`, `instant_threshold`. |
| `notify` | `channels`, ntfy server and tags, the per-cycle push cap, `dashboard_url`. |
| `watchlist` | Companies on Greenhouse, Lever, Ashby and Workday (with `keywords`). |
| `searches` | Indeed and LinkedIn searches through JobSpy. |
| `linkedin` | Pause between LinkedIn requests, the cool-down after a 429, and the job pages (`job_pages`, `max_job_pages_per_hour`). |
| `workday` | `max_pages`: how many pages of 20 jobs one search may read. |
| `simplify` | The SimplifyJobs New-Grad feed on or off. |
| `discovery` | Automatic board discovery and its limits. |
| `blocklist` | Companies and boards to stay away from. |
| `health` | When a source counts as failing or silent. |
| `retention` | How long the run history behind the Health page is kept (30 days). |
| `dashboard` | Address and port of `jobpulse web`. |
| `llm` | The optional LLM layer: on/off, model, effort, resume, band, wanted levels, daily call cap. |

Level, location and stack values in the example are placeholders; set your own. `jobpulse config check` validates the file and names the exact field of any mistake.

### `.env`

| Variable | Purpose |
|---|---|
| `NTFY_TOPIC` | Your long random ntfy topic. Needed when `ntfy` is in `notify.channels`. |
| `NTFY_TOKEN` | Bearer token for a self-hosted ntfy server with access control. |
| `DISCORD_WEBHOOK_URL` | Needed when `discord` is in `notify.channels`. |
| `LLM_API_KEY` | Your Anthropic API key, needed when `llm.enabled` is true (by `worker` and a non-dry `poll`). |
| `DASHBOARD_USER`, `DASHBOARD_PASSWORD` | Basic auth for the dashboard (both or neither). |
| `DATABASE_URL` | Overrides `database_url`, e.g. Postgres. |
| `POSTGRES_PASSWORD` | The Docker `postgres` profile's password. |
| `JOBPULSE_CONFIG` | Another `config.yaml` path (`--config` beats it). |
| `LOG_LEVEL` | `INFO` by default. |

Empty values count as unset. Only commands that send (`worker`, a non-dry `poll`, `digest`, `notify test`) need the notification secrets.

## Commands

| Command | What it does |
|---|---|
| `jobpulse poll --once [--dry-run] [--source NAME] [--target KEY]` | One cycle over every source (or some). A dry run sends nothing and rolls back. |
| `jobpulse worker` | The scheduler: every source on its own cadence, plus the digest. |
| `jobpulse web [--host H] [--port P]` | The dashboard. |
| `jobpulse digest [--dry-run]` | Send (or show) the digest now. |
| `jobpulse companies list` / `sync` | The watchlist with origin and baseline state / apply `config.yaml` now. |
| `jobpulse notify test` | One test push through every channel. |
| `jobpulse db upgrade` / `current` | Apply migrations / show the revision. The other commands refuse a database that is behind. |
| `jobpulse config check` | Validate `config.yaml` and the secrets your channels need. |

Each takes `--config PATH` and `--env-file PATH` before the command name.

## Adding a source adapter

Every source sits behind one small interface (`jobpulse.sources.base.SourceAdapter`):

```python
class SourceAdapter(Protocol):
    name: str

    async def fetch(self, target: Target) -> FetchResult: ...
    async def enrich(self, posting: Posting) -> Posting: ...  # optional detail call
```

For a new JSON board API, subclass `HttpAdapter`. Its HTTP client already handles per-host limits, retries with backoff, `Retry-After` and ETags.

```python
class ExampleAdapter(HttpAdapter):
    name = "example"

    async def _fetch(self, target: Target, fetched_at: datetime) -> FetchResult:
        company = require_company(target)
        payload = await self.get_json(f"https://api.example.com/boards/{company.token}/jobs")
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise FetchError("parse", "expected an object with a 'jobs' list")
        postings = [self._parse(item, company) for item in payload["jobs"]]
        return FetchResult.success(postings, fetched_at=fetched_at, complete_listing=True)
```

The rules that keep detection correct:

- **Never raise for HTTP errors.** A non-2xx response, a timeout or unreadable data is `ok=False` with an `error_kind` (`HttpAdapter` turns a `FetchError` into exactly that). Never return an empty success for a failure.
- **`complete_listing=True` only** when the response is the board's full list of open jobs. Only those results can close postings.
- **A stable `external_id`** from the source (never a position in the list), and the source's own posting time in `posted_at` with `posted_at_precision` (`exact`, `day` or `none`). Never use an "updated" timestamp.
- Normalize locations with `jobpulse.core.locations.normalize_locations`, and set `canonical_url` (`jobpulse.core.dedupe.ats_key` for ATS jobs) so dedupe can match copies.

Then wire it up: add the name to the source literals in `core/models.py` (and `config.py` for a watchlist ATS), register the adapter in `runtime.build_adapters` and its source group, and teach `core/atsurls.py` its URLs if discovery and the paste box should find it.

Tests come with it, and never touch the network (`pytest-socket` blocks it; respx refuses unmocked requests):

1. Save a trimmed real response under `tests/fixtures/<source>/`, with nothing personal in it.
2. In `tests/unit/sources/test_<source>.py`, serve it with the `respx_mock` fixture and assert the parsed postings, then cover 404, 429, 500, a timeout and malformed JSON (each `ok=False` with the right `error_kind`). `tests/unit/sources/test_lever.py` is a compact example.
3. Add a live smoke test in `tests/live/` marked `@pytest.mark.live`; it runs only with `JOBPULSE_LIVE=1`.

## Development

```bash
scripts/check.sh                          # ruff, ruff format --check, mypy, pytest (everything)
uv run pytest -k digest                   # a subset
JOBPULSE_LIVE=1 uv run pytest -m live     # the live smoke tests against real APIs
uv run alembic revision --autogenerate -m "..."   # after a schema change; review it by hand
```

To watch a whole cycle without the network, replay the recorded scenario on a scratch database:

```bash
export DATABASE_URL=sqlite:////tmp/jobpulse-demo.db
uv run jobpulse --config tests/fixtures/scenarios/config.yaml db upgrade
uv run jobpulse --config tests/fixtures/scenarios/config.yaml poll --once --fixtures tests/fixtures/scenarios/first_poll
uv run jobpulse --config tests/fixtures/scenarios/config.yaml poll --once --fixtures tests/fixtures/scenarios/second_poll
uv run jobpulse --config tests/fixtures/scenarios/config.yaml web
```

`CLAUDE.md` lists conventions and gotchas; `PROGRESS.md` records what was built, the decisions behind it and what is left.

## Troubleshooting

- **`run jobpulse db upgrade first`:** the database is new or behind the code. Run `jobpulse db upgrade` (Docker does it in `migrate`).
- **No pushes after starting:** expected until something new is posted; the first poll of every source is silent. `jobpulse poll --once --dry-run` shows what each source returns.
- **`missing or invalid secrets`:** a command that sends needs `NTFY_TOPIC` (or `DISCORD_WEBHOOK_URL`) for the channels in `notify.channels`.
- **A source keeps failing:** the Health page shows the last error. Lever boards on `jobs.eu.lever.co` need `region: eu`.
- **`database is locked` on SQLite:** avoid a dry run while the worker is polling; a dry run holds the write lock until it rolls back.

## Project layout

```
src/jobpulse/
  core/        pure logic: models, detection, dedupe, filters, scoring, time parsing
  sources/     Greenhouse, Lever, Ashby, Workday, SimplifyJobs, JobSpy (Indeed/LinkedIn),
               LinkedIn job pages and the shared LinkedIn gate, ATS detection
  llm/         the optional LLM layer: prompt, Claude client, scorer
  notify/      ntfy, Discord, message formatting
  db/          tables, repository, dashboard queries, session
  web/         FastAPI app, routes, templates, static files (htmx)
  poller.py    one poll cycle          scheduler.py   cadences, watchdog
  digest.py    the digest              health.py      source health alerts
  discovery.py automatic watchlist     cli.py         the jobpulse command
migrations/    Alembic                 tests/         unit, integration, live
```
