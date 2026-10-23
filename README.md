# jobpulse

A personal job-posting watcher for one person on one always-on machine. It polls public applicant-tracking-system boards (Greenhouse, Lever, Ashby), the SimplifyJobs New-Grad list and a few Indeed/LinkedIn searches, finds postings it hasn't seen, filters and scores them, and pushes the good ones to your phone.

It detects and notifies only. It never applies, fills forms, logs in anywhere or works around rate limits.

> Work in progress. See [PROGRESS.md](PROGRESS.md) for what is built so far. Full setup docs arrive with Milestone 3.

## Quick start (development)

```bash
uv sync
cp config.example.yaml config.yaml        # then edit: contact_email, filters, watchlist
uv run jobpulse db upgrade
uv run jobpulse poll --once --dry-run     # fetch and evaluate everything, save and send nothing
scripts/check.sh                          # lint, types, tests
```

The first real poll of each board stores what's there silently. After that, `uv run jobpulse worker` polls on a schedule and pushes fresh matches through ntfy. Put `NTFY_TOPIC` in `.env` first; see `.env.example`.
