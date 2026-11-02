# Design decisions

Why jobpulse works the way it does. The goal behind most of them: get you to a fresh posting among its first applicants, without ever claiming "no new jobs" when a source simply failed. CLAUDE.md has the code-level conventions and gotchas; the README covers using it.

## Principles

- **Detect and notify, never act.** jobpulse never applies, fills in forms or logs in anywhere.
- **A failure is never an empty success.** Adapters return `FetchResult` and don't raise for HTTP errors; a failed, blocked or errored response is recorded as a failure. It never closes a posting or counts as an empty run.
- **New means "the insert succeeded".** Each posting is stored under its source's own stable id, and it's new exactly when that insert succeeds.
- **Silent baselines.** The first successful poll of a board, search or feed stores everything and notifies about nothing, even when it is empty. A changed search definition or changed Workday keywords start a new baseline; `results_wanted` and `hours_old` don't.
- **UTC everywhere.** Only aware datetimes; the database layer rejects naive values, and SQLite columns use a type that restores tzinfo.
- **Sync database, async network.** Every database phase is a synchronous block with no `await` inside, so no transaction is held across network I/O.
- **Pure core.** `jobpulse.core` is stdlib only, does no I/O and is type-checked strictly; everything with judgment in it (filters, scoring, dedupe, places, roles) lives there and is tested in isolation.
- **Dependencies a little behind.** `[tool.uv] exclude-newer` keeps the lock a few days behind the newest releases; dev tools and a few fast-moving libraries (FastAPI, JobSpy) are pinned exactly. CI actions are pinned to commit SHAs.

## Sources

- **Greenhouse, Lever, Ashby:** the public board APIs. Greenhouse's list already carries `first_published`, so the freshness gate runs on list data and only survivors get a detail call. Lever EU boards answer only on the EU host; Greenhouse EU boards are served by the global API. Ashby's `isRemote` is true for hybrid jobs too, so `workplaceType` decides remote.
- **Conditional requests:** the HTTP client sends stored ETags and replays a 304 as the cached 200, so an unchanged board costs almost nothing.
- **Workday:** `limit` is fixed at 20 (more is an HTTP 400) and `total` is read from the first page only. Big tenants have thousands of jobs, so a board can have keywords: each is one search, and only its results are polled. Workday's search is fuzzy and sometimes sorted by relevance, so a keyword that finds more than a poll reads is dropped for that board. A board searched by keyword never closes postings; one without keywords closes only after a complete read whose count reaches `total`. Posting ids are scoped to the site, because Workday ids are unique only within one.
- **SimplifyJobs feed:** one conditional request; its ETag is saved in the same commit as the rows it produced, so a crash can't skip a version. Only `active: false` closes a listing.
- **Indeed and LinkedIn searches (JobSpy):** one site per call in a thread, asking for the last 3 hours, so a late or failed run leaves no gap. JobSpy reports failures in frame attributes rather than raising; those become failed runs.
- **LinkedIn job pages:** the public guest page, read for LinkedIn postings that pass the prescreen, for their description and applicant count. Every LinkedIn request (searches and pages) goes through one gate: one at a time, a random pause, a shared cool-down after a 429, 999, 403 or sign-in redirect, and an hourly cap. A page that couldn't be read leaves the posting pending, retried next cycle for up to `linkedin.job_page_wait_hours`, then judged without it. The HTTP client keeps no cookies, so no request ever carries a session.
- **Maintenance pages:** a redirect to a maintenance page (Workday does this for every tenant at once) is retried, then the whole source pauses for a cool-down; health opens one incident for the source rather than one per board.

## Detection and dedupe

- **Freshness:** only postings younger than `freshness_hours` (48) can alert. A source with day precision uses min(end of the posted day in UTC, first seen), so a job posted today always passes and old reposts stay quiet.
- **Closures:** a job missing from `closure_missing_polls` complete, successful polls of its board closes, and reopens if it comes back. Job-board results never close anything.
- **Merging copies:** a LinkedIn, Indeed or Simplify copy of a job already known from its ATS merges onto the ATS row by canonical key (ATS keys only; generic career URLs never merge), a `gh_jid` link, or the same company, title and city within 30 days of the row's last sighting. A posting without a company never fingerprint-merges. A new ATS row adopts earlier copies with their push state, and is always the primary.
- **Reposts:** marked only on strong source signals: LinkedIn's "Reposted" marker, or the same Greenhouse requisition seen on the board 7+ days earlier. Same company, title and city is deliberately not a signal, since several openings often share a generic title. Reposts are hidden from New by default.
- **Employment type:** from each source's own field (Ashby, Lever, Indeed, Workday, Greenhouse metadata, LinkedIn's criteria list); no signal means full-time.

## Filters and scoring

- **Rules:** title include patterns with weights, title and level excludes, years of experience asked (penalize or exclude), countries, places (state, metro area, city) with a remote switch, a pay floor in USD a year (unlisted or other-currency pay passes) and a company blocklist. Location tables live in `core/countries.py` and `core/places.py`; a name shared by places in different countries stays unknown rather than guessed.
- **Every match is pushed.** A posting that passes the filters is a match and is pushed in the cycle that decides it, whatever its score. There is no digest and no per-cycle cap; the score orders and labels.
- **Score parts:** title weight, freshness, a level-mismatch penalty, applicant count (`1 - count / 200`, so "Be among the first 25" scores 0.875), company priority (dream companies at the top), and either stack keywords or, when a resume is stored and the posting has a description, resume similarity (weight 0.25, a like-for-like swap: in an evaluation it tracked one user's apply/dismiss choices about as well as stack keywords, AUC ≈ 0.61 for both).
- **Resume similarity:** local embeddings (bge-small-en-v1.5 through fastembed, ONNX, no API key). Descriptions are embedded once per model and cached; the card shows the single closest resume line, and resume lines that look like contact details or headings are never shown. The model loads only once a resume exists.
- **Re-scoring:** changing filters, weights or the resume re-scores open New and Saved matches with the values stored when each was decided; tiers never change. A setting added with a default doesn't trigger a re-score.
- **The LLM layer** (off by default) reads only matches close to the threshold against the resume and moves the score by at most ±0.15; it never filters. Answers are cached per posting and calls are capped per day; dry runs never call it.

## Notifications

- **One message per scheduled job run** by default ("3 new job postings were found via watchlist. Top score: …"): one push per posting got rate-limited by ntfy.sh (60 burst, then 1 per 5 s, 250 a day). A failed push is retried by the same job's next run while the postings are fresh, open and untriaged.
- **Saved filters narrow pushes:** once any saved filter exists, only matches that fit one are pushed, one message each with a colored bullet per fitting filter, fewest bullets first so the best lands on top, at most 10 per run and 200 a day plus one summary. A filter fires only for postings decided after it was saved or changed.
- **Health alerts:** one when a target fails 3 runs in a row or a normally busy one returns nothing for 24 h, one when it recovers; targets failing or recovering together share a message.

## Preferences, secrets and onboarding

- **Preferences live in the database** (one profile row, revisioned), edited by the setup wizard and Settings; `config.yaml` holds infrastructure only and is optional. The worker re-reads the profile at every cycle start, so changes apply without a restart; an invalid stored profile keeps the last good one.
- **Secrets** come from the environment/`.env` or Settings; the environment wins. They are masked in every page and log line from the moment they are known. They sit in the database in plain text, which the README says plainly.
- **The wizard** turns answers (roles, level, locations, pay, skills, companies, notification channel) into rules through a pure role catalog. Sections edited by hand on Settings are remembered and left alone when answers change. A reference snapshot test pins what the catalog produces.
- **Day one:** the first poll of everything is a silent baseline, so the dashboard says postings are on the way and shows what is being watched.

## Watchlist and the seed

- **Discovery** adds Greenhouse, Lever and Ashby boards linked from the feed and job-board postings, but only boards with a posting that passes your filters, under a size limit and a cap on discovered companies. Workday boards are never discovered (they need keywords).
- **The seed** (`src/jobpulse/data/seed/boards.json`) ships about 360 validated boards, applied once when the wizard finishes; Workday boards keep only the keywords that fit what a poll reads. Staffing agencies ship as a separate list and are hidden while `hide_agencies` is on (default). With `seed.remote` on, installs fetch the latest seed daily (conditional GET, size and count caps, each new board validated) and `companies contribute` exports locally added boards for a pull request, which CI validates.

## Running it

- **`jobpulse up`** runs the dashboard and the scheduler in one process, the scheduler on its own thread and event loop, because the poller's synchronous database phases and large JSON parses would freeze the dashboard on a shared loop. It backs up an SQLite database before migrating it (three backups kept). Migrations ship inside the package.
- **One worker per database:** a lease row with a heartbeat; a stale holder on the same host whose process is gone is taken over at once, others after 5 minutes.
- **Watchdog:** every scheduled job must finish within 3× the longest interval plus jitter (monotonic clock, so sleep doesn't look like a stall); otherwise the process exits non-zero and Docker restarts it. Daily work (seed sync, run-history pruning, re-scoring) runs as phases of a cycle, not as scheduled jobs, so it doesn't stretch that limit.
- **The image** is one container running `up`, non-root, with the embedding model baked in, for amd64 and arm64. The dashboard is published on 127.0.0.1 by default; basic auth, CSRF tokens, a strict CSP and secret masking protect it when it is exposed.
