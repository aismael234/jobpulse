# jobpulse

A self-hosted job-posting watcher for software engineers who want to apply early. It polls public applicant-tracking-system boards (Greenhouse, Lever, Ashby, Workday; about 360 company boards out of the box), the SimplifyJobs New-Grad list and a few Indeed/LinkedIn searches, finds postings it hasn't seen, filters and scores them against your roles, level, locations and resume, and pushes every match to your phone within minutes, best first. A small dashboard keeps them for triage.

One install serves one person. It runs on any always-on machine (a home server, a NAS, a small VPS) and keeps everything, your resume included, in its own database.

## Quick start (Docker)

```bash
docker run -d --name jobpulse --restart unless-stopped \
  -p 127.0.0.1:8000:8000 -v jobpulse-data:/app/data \
  ghcr.io/aismael234/jobpulse:latest
```

Open http://127.0.0.1:8000/ and follow the setup wizard (below). The image runs on amd64 and arm64 and has the resume-similarity model built in, so nothing else is downloaded. Everything you enter, the postings and the database backups live in the `jobpulse-data` volume. To upgrade, pull the new image and start the container again: the database is backed up and migrated on startup. With Docker Compose, `docker compose up -d` in a checkout of this repository does the same (see "Run it with Docker").

## Privacy and the sites it reads

- **Nothing leaves your machine except the requests it needs.** Postings come from the public APIs of the boards and feeds you watch; pushes go to the ntfy server or Discord webhook you choose. There is no telemetry and no account.
- **Your resume** is read on your machine, embedded by a local model and stored in the database. It is sent nowhere unless you turn on the optional LLM layer, which sends it to Anthropic's API with the postings it reviews.
- **Secrets** (ntfy topic, Discord webhook, API key) are stored in the database in plain text, so protect the data folder and its backups like the secrets themselves. The dashboard password is stored only as a salted hash.
- **The board seed sync** fetches this repository's `boards.json` once a day (when "Add new boards from the shared jobpulse list" is on, Settings → Sources & schedule) to add boards other people contributed; it sends nothing about you.
- **LinkedIn and Indeed:** jobpulse reads their public search results and LinkedIn's public job pages as a guest, slowly and one request at a time. Their terms of service restrict automated access, and they may rate-limit or block your IP address. That risk is yours: if you'd rather not take it, untick "Also search LinkedIn and Indeed" in the wizard (or later through Settings → Job search) and jobpulse uses only the company boards and the SimplifyJobs feed.

## What it does, and what it never does

It **detects and notifies**. It never applies, fills in forms or submits anything. On top of that:

- No logged-in LinkedIn or Indeed sessions, no cookies from a real account, no accounts of any kind. LinkedIn job pages are read as a guest, and jobpulse keeps no cookies at all.
- No CAPTCHA solving, proxies, proxy rotation or anti-detection tricks. Glassdoor, ZipRecruiter and Google Jobs are never queried.
- LinkedIn (searches and job pages alike) is asked one request at a time with a random 4–8 s pause, and backs off for 30 minutes after a 429.
- A failed, blocked, empty or errored response is recorded as a failure. It never closes a posting or counts as "no new jobs".
- Every ATS request carries a descriptive User-Agent with your contact email.
- Secrets (ntfy topic, Discord webhook, LLM API key) are saved on Settings and never logged or shown again; the dashboard password is stored only as a salted hash.
- The optional LLM layer is off unless you turn it on. When it's on, the postings close to your threshold, and your resume, are sent to Anthropic's API; nothing else is.

## How it works

1. **Sources.** The worker polls each source on its own schedule with random jitter: your watchlist boards every 30 min, the SimplifyJobs feed every 30 min (an unchanged feed costs one tiny conditional request), job-board searches every 60–90 min asking for the last 3 hours, so a late or failed run leaves no gap.
2. **New or not.** Each posting is stored under its source's own stable id. It's new exactly when that insert succeeds.
3. **Silent baseline.** The first successful poll of a new board, search or feed stores everything and notifies about nothing.
4. **Freshness.** Only postings younger than 48 h (by the source's own timestamp when there is one) can alert, so reopened listings and old reposts stay quiet.
5. **Dedupe.** A LinkedIn, Indeed or SimplifyJobs copy of a job you already have from its ATS board (same canonical URL, a `gh_jid` link, or the same company, title and city within 30 days) is merged onto the ATS posting instead of alerting twice.
6. **Details.** New postings that pass the title and location rules get one detail call: a Greenhouse or Workday job's description, and a LinkedIn job's public page, which also gives its applicant count ("Be among the first 25 applicants").
7. **Filters and score.** Title, level, years-of-experience, stack and location rules from your profile (Settings), plus the applicant count when LinkedIn gave one. Every posting that passes is pushed right away, whatever its score; each scheduled job sends its matches as one push naming the best score.
8. **A second opinion (optional).** With the LLM layer on, postings that score close to the threshold are read by Claude against your resume, and its answer moves the score a little up or down (see "The LLM layer").
9. **Closures.** A job missing from two complete, successful polls of its board is marked closed (and reopened if it comes back). Job-board results, and Workday boards searched by keyword, never close anything.
10. **Health.** One alert when a target fails 3 runs in a row or a normally busy one returns nothing for 24 h, and one message when it recovers. Targets of one source that fail or recover together share one message. A source that redirects to its maintenance page (Workday does, for every board at once) is retried twice and then paused for 30 minutes at a time, with one message saying it's down and one when it's back.
11. **Discovery.** Greenhouse, Lever and Ashby boards linked from the feed and from job-board postings are added to the watchlist automatically when one of their postings passes your filters. Workday boards are only added by you.

## Run it from a checkout (uv)

You need [uv](https://docs.astral.sh/uv/). It installs Python 3.12 for you.

```bash
uv sync
uv run jobpulse up                        # http://127.0.0.1:8000
```

`jobpulse up` runs everything in one process: it backs up an SQLite database that needs an upgrade (into `data/backups/`, keeping three), migrates it, and serves the dashboard with the scheduler alongside. The first visit opens the **setup wizard**:

1. **Resume** (optional): read on your machine to suggest roles, skills and a level; postings are scored by how close they are to it.
2. **Roles and skills**: the kinds of roles you want (back end, full stack, ML…) and your main skills.
3. **Level**: entry, mid, senior or staff, and the most years of experience a posting may ask for.
4. **Locations**: countries, optionally metro areas or states, and whether remote is fine.
5. **Pay and companies**: a pay floor, dream companies (scored as your top priority), companies to avoid, whether to hide staffing agencies, and whether to search LinkedIn and Indeed too (see "Privacy and the sites it reads").
6. **Notifications**: a generated ntfy topic to subscribe to, or a Discord webhook, with a test push.
7. **Review**: the searches and rules your answers make. Finishing starts watching about 360 company boards from the built-in seed.

The first check of each board, search and feed is a silent baseline, so the first pushes arrive once something new is posted; until then the dashboard says postings are on the way. Change any answer later from Settings → Job search; anything you edit by hand under Settings → Advanced stays as you left it.

`up` takes a lease on the database, so a second `up` on the same database refuses to start and nothing is pushed twice. `uv run jobpulse poll --once --dry-run` fetches and evaluates everything without saving or sending.

**Everything is set on the dashboard.** There is no config file and no `.env`: your answers, the Settings tabs and the secrets you save there all live in the database, in `./data` (`--data-dir` picks another folder, as in `uv run jobpulse --data-dir ~/jobpulse-data up`). `--host` and `--port` choose where the dashboard listens (127.0.0.1:8000 by default).

**Upgrading:** stop jobpulse, pull the new version, run `uv sync`, and start `uv run jobpulse up` again: it backs up the database and migrates it first.

## Run it with Docker

The image runs `jobpulse up`: it backs up and migrates the database, then serves the dashboard with the scheduler in the same process. The Quick start's `docker run` is all it needs. With Compose, from a checkout:

```bash
docker compose up -d                      # pulls ghcr.io/aismael234/jobpulse, or builds it
docker compose logs -f jobpulse
```

- The dashboard is published on `127.0.0.1:8000` only. To reach it from other devices, use a VPN such as Tailscale (below), or set a password on Settings → Security before publishing the port more widely.
- The SQLite database, its backups (`backups/`, three kept), your profile, resume and saved secrets live in the `jobpulse-data` volume.
- If a source stops finishing runs, the scheduler's watchdog exits non-zero and Docker restarts the container.
- To upgrade: `docker compose pull && docker compose up -d` (or `--build` from a checkout).
- Small hosts: the resume model takes about 200 MB of memory once a resume is stored; untick "Score postings against my resume" on Settings → Sources & schedule to turn it off.

## Phone notifications with ntfy

[ntfy](https://ntfy.sh) pushes to the ntfy app on Android and iOS. A public topic on ntfy.sh can be read by anyone who knows its name, so the name has to be long and random:

```bash
python3 -c "import secrets; print('jobpulse-' + secrets.token_urlsafe(24))"
```

1. Save the result on Settings → Notifications as the ntfy topic (the setup wizard generates one for you). jobpulse refuses topics shorter than 24 characters.
2. Install the ntfy app, tap **+**, and subscribe to that exact topic on `https://ntfy.sh`.
3. Press **Send a test push** on Settings, or run `uv run jobpulse notify test` (or `docker compose exec jobpulse jobpulse --data-dir /app/data notify test`).

Each scheduled job (watchlist, Simplify, job boards) sends one push per run for all its new matches, such as `3 new job postings were found via watchlist. Top score: Stripe - 0.87`; tapping it opens the top posting's apply page, and the dashboard has them all. Every match counts, with no per-cycle cap. That changes once you save a filter (below). A failed push is retried by the same job's next run while its postings are still fresh, open and untriaged. `poll --once` and the worker log list each match's title line, like `Stripe · Backend Engineer · SF / Remote · posted 25 min ago`.

**Saved filters narrow pushes.** Once at least one saved filter exists (Settings), only new matches that fit an active one are pushed, one message each: `🟢🟣 Stripe · Backend Engineer · SF / Remote · 0.87`, a colored bullet per saved filter it fits. Tapping it opens the posting. Within a run, messages go out fewest bullets first (then lowest score), so the posting that fits the most lands last, on top of your list. To stay under ntfy.sh's limits they are sent 5 s apart, at most 10 per run and 200 a day (`notify.saved_filters`); the rest of a run go out first as one summary, `+7 more matching your saved filters: 🟢 4 · 🟣 3`. A filter fires only for postings decided after you save it, change its filters or turn it on. Matches that fit no filter still land in New. Turning every filter off means no pushes at all; deleting them all brings back one push per run. Health alerts are never affected.

**Self-hosting ntfy:** set `notify.ntfy.base_url` in your profile to your server, and the ntfy access token on Settings if it uses access control. Subscribe the app to your server instead of ntfy.sh.

**Discord as well (or instead):** create a webhook (Server Settings → Integrations → Webhooks), save it on Settings → Notifications, and tick Discord under "Where pushes go".

## The dashboard

`jobpulse up` serves it on `http://127.0.0.1:8000`.

- **New / Saved / Applied / Dismissed.** New holds the matches nobody has triaged yet. One click moves a posting between views (with undo). Each card shows the score (hover for its breakdown), pay when the source gives it, "posted 25 min ago" (or "first seen 3 h ago" when the source has no timestamp), and where else the job was seen.
- **Filters,** all visible: text (title, company, description); location; when it was pushed; company, source, remote, experience, applicants, contract work, a pay floor, minimum score, open only and, in New, "Include reposted". Sort by score, newest (the source's posted date) or most recently pushed.
  - **Location** is one list of the places in the current view, each with its count: countries, US states and Canadian provinces, metro areas (Bay Area, NYC, Seattle, DC, Boston, LA, Dallas–Fort Worth, Austin, Denver–Boulder, Research Triangle, Atlanta, Chicago) and cities. A country includes worldwide-remote postings; a state, metro area or city never includes remote ones.
  - **Pushed** is when jobpulse pushed the posting to you, or first saw it if it never pushed it: within the last hour up to 30 days, or longer ago than a day up to 30 days.
  - **Pay at least $X k/yr** compares the top of the posted range, with hourly and monthly pay turned into yearly; pay in another currency counts as not listed. Postings without pay stay unless you tick "Pay listed".
  - **Experience:** the most years the description asks for (0–2, 3–5, 6+, or not stated); cards show "asks 3+ yrs".
  - **Reposted postings are hidden from New** unless you tick "Include reposted"; Saved, Applied and Dismissed always show them, with a Reposted badge whose tooltip says why. A posting is a repost when LinkedIn's page says "Reposted 3 hours ago", or when it's a Greenhouse posting whose requisition (the company's own id for the opening) jobpulse first saw on that board a week or more earlier, open or closed. The same company, title and city alone doesn't count: companies hire several people under one title. Other sources give no reliable signal, so their reposts under a new link show as ordinary postings. Reposts are still matched and pushed; only the dashboard hides them.
  - **Applicants:** "Under 25/50/100/200 applicants" hides postings whose LinkedIn count is known to reach the limit ("Over 200" counts as 200, "Be among the first 25" as 0). Postings without a count (every ATS, Indeed and Simplify posting) always show.
  - **Contract work:** shown, hidden or shown alone. The type comes from the source's own field where it has one (Ashby, Lever, Indeed, Greenhouse metadata, Workday, LinkedIn's criteria list), else from phrases such as "6-month contract", "contract-to-hire", "C2C" or "W2 contract" ("smart contracts" doesn't count). Contract-to-hire and temporary jobs count as contract; no signal means full time. Cards show Contract, Part-time or Internship.
- **Applications log:** moving a posting records when, and cards outside New show "applied 3 d ago". Saved, Applied and Dismissed sort by it ("Recently applied"). **Export CSV** (next to the count) downloads every posting the current view and filters show, all pages; cells that would start a spreadsheet formula get a leading `'`.
- **Dismiss all N** (New and Saved only) dismisses what the filters show, with undo. Only postings pushed and moved before the page was shown are touched, so nothing that arrived after you looked goes with them. Applied is never emptied in bulk.
- **Block company** on any card hides the company's postings at once (Applied aside), with undo. The worker reads the dashboard's blocks at the start of each cycle, so they apply without a restart: new postings are filtered, waiting pushes dropped, boards neither polled nor discovered. The Watchlist page lists blocked companies, the profile's and the dashboard's; the dashboard's can be unblocked there. Blocking a watched company stops polling its board.
- **Health:** per board, search and feed: last run, last success, failures in a row, the last 24 h of runs, fetched and new postings, the last error, and open incidents.
- **Watchlist:** every company with its priority, status, origin and baseline. Paste a careers URL to add a board (see below); a Workday board gets a keywords field. Priority and pause apply to every company.
- A posting the LLM layer assessed shows "LLM +0.12" (or minus); hover for its verdict.
- **Saved filters:** "Save these filters" (under the filters) saves what the filters show; give it a name and one of 8 colors. Each saved filter gets a button above the filters that applies it, with a badge counting its open, untriaged matches first seen since you last clicked it, and cards show the bullets of the filters they fit. Settings → Saved filters lists them with an on/off switch, edit (back on the dashboard, with the filter applied) and delete, says which push mode is on, and flags a filter that no longer reads back after an upgrade (it is skipped, never widened). **Color-blind mode** switches to Okabe–Ito-based colors and adds a shape per filter (● ■ ▲ ◆ ★ ✚ ▼ ⬢), on the dashboard and in pushes.

- **Settings,** in tabs:
  - **Notifications:** where pushes go (ntfy, Discord), the topic, token and webhook (saved values never show again, and are masked in pages and logs from the moment they're saved), a test push, and color-blind mode.
  - **Job search:** your setup answers (with a link to change them) and your resume (PDF, DOCX, TXT or Markdown, at most 2 MB) with the lines postings are compared with.
  - **Saved filters:** the list, as above.
  - **Sources & schedule:** how often boards and searches are checked, the freshness window, the SimplifyJobs feed, automatic discovery and its limit, daily seed updates, LinkedIn's pace and job pages, health alerts and resume matching. The scheduler picks a change up within a minute.
  - **Security:** the dashboard password.
  - **Advanced:** the Anthropic API key for the LLM layer, and every preference as YAML. A save lists any errors by field; one made in another tab or by the CLI since the page loaded is refused (409) with your text kept. New weights or rules re-score open New and Saved matches at the next check.

Companies on your blocklist (the profile's or the dashboard's) are hidden everywhere except Applied.

**Security.** It listens on 127.0.0.1 unless you say otherwise. A password set on Settings → Security turns on HTTP basic auth at once (any user name; forgot it? `jobpulse password clear`). Every POST needs the page's CSRF token, which lives in a `SameSite=Strict` cookie. Pages load scripts and styles only from the dashboard itself (htmx 2.0.11 is vendored, plus a few lines of `app.js`), under a strict Content-Security-Policy.

## Reach the dashboard from your phone with Tailscale

Keep the dashboard on 127.0.0.1 and let [Tailscale](https://tailscale.com) carry it to your devices, encrypted and private to your tailnet:

1. Install Tailscale on the always-on machine and on your phone, signed in to the same tailnet.
2. On the machine, serve the dashboard to your tailnet over HTTPS:

   ```bash
   tailscale serve --bg 8000
   ```

   It prints the address, `https://<machine>.<tailnet>.ts.net/`. Allow HTTPS certificates in the Tailscale admin console if it asks.
3. Optionally set a password on Settings → Security as well.

Never use `tailscale funnel` for this: it would put the dashboard on the public internet.

## Adding companies

- **Board tokens** are the board's name in its URL: `job-boards.greenhouse.io/<token>`, `jobs.lever.co/<token>` (EU boards on `jobs.eu.lever.co` are found automatically), `jobs.ashbyhq.com/<token>`.
- **Workday** boards have a token `<tenant>.wd<N>/<site>` and keywords: for `https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite` the token is `nvidia.wd5/NVIDIAExternalCareerSite` (pasting the URL works too). Big tenants have thousands of jobs, so give them `keywords`: each one is a search on the board, and only its matches are polled. A board searched by keyword never closes postings (it never sees the whole board); one without keywords is paged through, at most `workday.max_pages` (25) pages of 20 jobs, and closes jobs only after a full read. Changing the keywords starts a new silent baseline.
- **From the dashboard:** paste a board, job or careers-page URL on the Watchlist page. jobpulse recognizes the board from the URL, or reads the page once to find a Greenhouse, Lever, Ashby or Workday link or embed. It checks the board with one API call, shows the company and its open jobs, and adds it with the priority (and, for Workday, the keywords) you choose.
- **From a file:** `uv run jobpulse companies import FILE --workday-keywords "software engineer, full stack"` adds the boards behind a file's links, for example an export from another job service. In JSON, every value that is a URL counts, with the company name beside it; in any other text, every URL. Each board is checked like a pasted URL (blocklist, one validation call, `discovery.max_board_jobs`), and `--dry-run` shows what would be added. Workday boards are searched for the keywords; a keyword is dropped for a board when it finds more jobs than a poll reads (`workday.max_pages` pages of 20), or when it costs more than one page and nothing on its first page passes your title rules (Workday's search is fuzzy: "full stack" finds a bank's "Full Time" jobs). Imported companies show as `import`.
- **From the seed:** `uv run jobpulse companies seed apply` adds the boards that ship with jobpulse (`src/jobpulse/data/seed/boards.json`, built from a real watchlist less staffing agencies) that you don't have yet; `--dry-run` shows what would change. Known, blocked and paused boards are skipped, and Workday boards keep only the keywords that fit what a poll reads (software engineer, full stack, backend engineer, machine learning engineer). Seed companies show as `seed`. With "Add new boards from the shared jobpulse list" on (Settings → Sources & schedule; off by default for now), a watchlist cycle fetches the latest seed once a day and adds new boards the same way, each validated once.
- **Back to the seed:** `uv run jobpulse companies contribute [--out FILE]` writes the boards you added that the seed lacks, in the seed's format, with the steps for a pull request. CI checks a pull request that changes the seed: every board it adds must answer on its ATS and must not be a staffing agency (`python -m jobpulse.devtools.seed_check`).
- **Automatically:** discovery adds boards linked from the SimplifyJobs feed and job-board postings when one of their postings passes your title and location filters. It adds at most `discovery.max_auto_companies` (50) and skips boards with more than `max_board_jobs` (1,000) open jobs. `uv run jobpulse companies list` shows where each company came from.

Every new company's first poll is a silent baseline. Workday boards are never added by discovery. To never hear about a company again, block it from one of its cards, or add it to `blocklist.companies` in your profile (a board to `blocklist.boards`): its postings are kept but never pushed, and discovery leaves it alone.

## The LLM layer (optional)

Off by default. When it's on, postings whose rule score lands within `llm.band` (0.10) of `instant_threshold` (the borderline ones) are sent to Claude together with your resume. The threshold no longer gates pushes (every match is pushed); it only marks where the LLM looks. Claude answers with the level the role really hires for, how much of it is backend work (0–100) and how well your resume fits it (0–100). The two scores average into a signal that moves the rule score by up to `scoring.weights.llm` (0.15) either way; a level that isn't in `llm.wanted_levels` counts as a signal of 0. So a borderline posting moves up or down the push order and shows its adjusted score; nothing is ever filtered out by it.

To turn it on:

1. Upload your resume on Settings (or run `uv run jobpulse resume set FILE`). The LLM reads its whole text.
2. Save your Anthropic API key on Settings → Advanced.
3. In your profile (Settings → Advanced):

   ```yaml
   llm:
     enabled: true
     role: backend software engineer  # how the prompt describes you
     wanted_levels: [entry, mid]      # intern, entry, mid, senior, staff, principal, manager; [] = any
   ```

4. Save. The next check uses it.

Cost guards: at most `llm.max_calls_per_day` (25) calls in any 24 hours, failed ones included; every answer is cached per posting, so a posting is never asked about twice (a changed resume, model or effort asks again); descriptions are cut at 16,000 characters; a dry run never calls (it uses cached answers only). It uses `claude-opus-5-5` at `effort: low` by default; `llm.model` and `llm.effort` change that. The cycle log says how many postings were answered, came from the cache, or were left to the rules, and an invalid key stops the asking until the key changes or the worker restarts.

## Configuration

All of it is on the dashboard: the setup wizard writes your profile, and Settings edits it (see "The dashboard"). The profile is kept in the database; `profile.example.yaml` documents every key of the YAML that Settings → Advanced shows, and `jobpulse profile export` prints yours (`profile import FILE` restores it).

| Section | What it controls |
|---|---|
| `contact_email` | Sent in the User-Agent so API operators can reach you. |
| `timezone` | The dashboard's displayed times. Storage is always UTC. |
| `filters` | Title include/exclude regexes, level words, years of experience, stack keywords, location rules and aliases. `filters.locations.countries` (for example `[US]`) keeps only postings with a location in those countries; "Remote (Global)" counts everywhere, and "Remote (EMEA)" only in EMEA. `filters.locations.places` (`US-TX`, `metro:bay-area`, `US-TX/Austin`) narrows them further, with remote postings kept while `remote_ok` is on. `filters.pay.min_yearly_usd` hides postings whose listed US-dollar pay tops out below it (unlisted pay passes). |
| `scoring` | Score weights (including `applicants`, `resume` and `llm`), `priority_cap`, `applicant_cap`, `instant_threshold` (the center of the LLM's band), `dream_companies` (their postings get the full company-priority part, from any source). |
| `notify` | `channels`, ntfy server, priority and tags; `saved_filters` limits one-message-per-posting pushes (`max_per_cycle` 10, `max_per_day` 200, `spacing_seconds` 5). |
| `searches` | Indeed and LinkedIn searches through JobSpy. |
| `blocklist` | Companies and boards to stay away from (companies blocked from a card are listed on the Watchlist page). |
| `hide_agencies` | Block the staffing agencies jobpulse ships a list of, and those you mark (on by default). "Block company" on a card asks whether the company is an agency: an agency goes on your agency list (Watchlist page) instead of the block list. |
| `sources` | Settings → Sources & schedule. |
| `answers` | The setup wizard's answers. They build `filters`, `scoring.dream_companies`, `notify.channels`, `searches`, `blocklist.companies` and `hide_agencies`, except the sections named in `answers.custom` (edited by hand). Absent for a profile written by hand. |
| `llm` | The optional LLM layer: on/off, model, effort, role, band, wanted levels, daily call cap. |

Everything else about how jobpulse runs (HTTP timeouts and per-host limits, closure and merge windows, Workday page caps, health thresholds, maintenance retries, run-history retention) is fixed.

## Commands

| Command | What it does |
|---|---|
| `jobpulse up [--host H] [--port P] [--no-migrate]` | Back up and upgrade the database, then the dashboard and the scheduler in one process. |
| `jobpulse poll --once [--dry-run] [--source NAME] [--target KEY]` | One cycle over every source (or some). A dry run sends nothing and rolls back. |
| `jobpulse companies list` | The watchlist with origin and baseline state. |
| `jobpulse companies import FILE [--workday-keywords K] [--priority N] [--dry-run]` | Add the boards behind a file's links (see Adding companies). |
| `jobpulse companies seed apply [--file F] [--dry-run]` | Add the seed's boards you don't have yet. |
| `jobpulse companies seed export [--out F] [--add ats/token=Name]` | Build the seed from your watchlist (maintainers). |
| `jobpulse companies contribute [--out F]` | Your boards the seed lacks, ready for a pull request. |
| `jobpulse notify test` | One test push through every channel. |
| `jobpulse password clear` | Remove the dashboard password if you can't sign in. |
| `jobpulse db upgrade` / `current` | Apply migrations / show the revision. The other commands refuse a database that is behind. |
| `jobpulse profile export` / `import FILE [--replace]` / `show` | Back the profile up as YAML / restore it / summarize it. |
| `jobpulse resume set FILE` / `show` / `clear` / `rescore [--backfill]` | The resume postings are scored against (also on Settings). |

Each takes `--data-dir PATH` (default `./data`) and `--log-level LEVEL` before the command name.

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
uv run pytest -k llm                      # a subset
JOBPULSE_LIVE=1 uv run pytest -m live     # the live smoke tests against real APIs
uv run alembic revision --autogenerate -m "..."   # after a schema change; review it by hand
```

To watch a whole cycle without the network, `poll --fixtures DIR` answers HTTP and JobSpy from recorded responses; `tests/integration/test_cli_poll.py` replays `tests/fixtures/scenarios/` that way on a scratch database.

`CLAUDE.md` lists conventions and gotchas; `docs/DECISIONS.md` records how it was built and why.

## Troubleshooting

- **`run jobpulse db upgrade first`:** the database is new or behind the code. Run `jobpulse db upgrade` (`jobpulse up` and the Docker image do it on startup).
- **No pushes after starting:** expected until something new is posted; the first poll of every source is silent. `jobpulse poll --once --dry-run` shows what each source returns.
- **`missing or invalid secrets`:** a command that sends needs the ntfy topic (or the Discord webhook) on Settings → Notifications for the channels ticked there.
- **A source keeps failing:** the Health page shows the last error.
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
  health.py    source health alerts    discovery.py   automatic watchlist
  migrations/  Alembic, shipped in the package so an install migrates itself
  onboarding.py  the setup wizard's logic   up.py   `jobpulse up`
  cli.py       the jobpulse command
tests/         unit, integration, live
```

## License

MIT (see `LICENSE`). The vendored htmx is 0BSD; see `THIRD_PARTY_NOTICES.md`.
