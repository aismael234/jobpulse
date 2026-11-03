"""The source-health page, built from ``runs`` and ``health_incidents``."""

import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from jobpulse.db.tables import Company, HealthIncident, Run, Search
from tests.factories import NOW
from tests.integration.conftest import Web, make_config


def run(
    web: Web, source: str, target: str, started: datetime, *, ok: bool = True, **fields: Any
) -> None:
    with web.sessions() as session:
        session.add(
            Run(
                source=source,
                target=target,
                started_at=started,
                finished_at=started + timedelta(seconds=4),
                ok=ok,
                **fields,
            )
        )
        session.commit()


def row(page: str, source: str, target: str) -> str:
    match = re.search(rf'<tr[^>]*id="target-{source}-{target}".*?</tr>', page, re.DOTALL)
    assert match is not None, f"{source}/{target} not on the page"
    return " ".join(re.sub(r"<[^>]+>", " ", match.group(0)).split())


def order(page: str) -> list[str]:
    return re.findall(r'<tr[^>]*id="target-([a-z]+-[\w.-]+)"', page)


def test_each_target_shows_its_recent_runs_and_last_error(web: Web) -> None:
    web.watch()  # greenhouse/acme and lever/beta
    run(web, "greenhouse", "acme", NOW - timedelta(hours=30), fetched_count=10, new_count=1)
    run(web, "greenhouse", "acme", NOW - timedelta(hours=2), fetched_count=12, new_count=2)
    run(
        web,
        "greenhouse",
        "acme",
        NOW - timedelta(hours=1),
        ok=False,
        error="HTTP 502 from boards-api.greenhouse.io",
        error_kind="http_5xx",
    )
    run(
        web,
        "greenhouse",
        "acme",
        NOW - timedelta(minutes=30),
        ok=False,
        error="timed out after retries (ReadTimeout)",
        error_kind="timeout",
    )
    run(web, "simplify", "new-grad", NOW - timedelta(minutes=10), fetched_count=3075, new_count=4)
    run(web, "simplify", "new-grad", NOW - timedelta(minutes=40), not_modified=True)

    page = web.page("/health")

    acme = row(page, "greenhouse", "acme")
    assert "Acme" in acme
    assert "30 min ago" in acme  # last run
    assert "2 h ago" in acme  # last success
    assert "2 failed in a row" in acme
    assert "3 runs" in acme
    assert "12 fetched" in acme
    assert "2 new" in acme
    assert "timeout: timed out after retries (ReadTimeout)" in acme

    feed = row(page, "simplify", "new-grad")
    assert "10 min ago" in feed
    assert "2 runs" in feed
    assert "3075 fetched" in feed
    assert "4 new" in feed
    assert "failed" not in feed

    beta = row(page, "lever", "beta")
    assert "never run" in beta

    assert order(page)[0] == "greenhouse-acme"  # problems first


def test_open_incidents_are_listed(web: Web) -> None:
    web.watch()
    with web.sessions() as session:
        session.add(
            HealthIncident(
                source="greenhouse",
                target="acme",
                kind="failing",
                opened_at=NOW - timedelta(hours=3),
                title="jobpulse: greenhouse/acme is failing",
                detail="3 runs in a row failed",
                alerted_at=NOW - timedelta(hours=2),
            )
        )
        session.add(
            HealthIncident(
                source="lever",
                target="beta",
                kind="silent",
                opened_at=NOW - timedelta(days=3),
                title="jobpulse: lever/beta returned no postings for 48 h",
                detail="...",
                alerted_at=NOW - timedelta(days=2),
                resolved_at=NOW - timedelta(days=1),
                recovery_sent_at=NOW - timedelta(days=1),
            )
        )
        session.commit()

    page = web.page("/health")

    assert "jobpulse: greenhouse/acme is failing" in page
    assert "jobpulse: greenhouse/acme is failing" in row(page, "greenhouse", "acme")
    assert "returned no postings for 48 h" not in page  # resolved
    assert re.search(
        r'data-view="health"[^>]*>.*?<span class="count alert">1</span>', web.page("/"), re.DOTALL
    )


def test_targets_that_are_no_longer_polled_are_marked(make_web: Callable[..., Web]) -> None:
    web = make_web(config=make_config(blocklist={"boards": ["lever/delta"]}))
    with web.sessions() as session:
        session.add_all(
            [
                Company(
                    name="Delta",
                    ats_type="lever",
                    token="delta",
                    source_of_discovery="auto",
                    created_at=NOW,
                    updated_at=NOW,
                ),
                Company(
                    name="Old",
                    ats_type="ashby",
                    token="old",
                    active=False,
                    created_at=NOW,
                    updated_at=NOW,
                ),
                Search(
                    name="backend-sf",
                    site="linkedin",
                    search_term="backend engineer",
                    params={},
                    created_at=NOW,
                ),
            ]
        )
        session.commit()
    run(web, "lever", "delta", NOW - timedelta(days=1), fetched_count=2)
    run(web, "ashby", "old", NOW - timedelta(days=5), fetched_count=8)

    page = web.page("/health")

    assert "not polled" in row(page, "lever", "delta")
    assert "not polled" in row(page, "ashby", "old")
    assert "never run" in row(page, "linkedin", "backend-sf")
    assert "not polled" not in row(page, "linkedin", "backend-sf")


def test_the_seed_sync_is_a_target_while_seed_remote_is_on(make_web: Callable[..., Web]) -> None:
    web = make_web(config=make_config(sources={"seed_updates": True}))  # Sources & schedule
    page = web.page("/health")
    assert "never run" in row(page, "seed", "remote")
    run(web, "seed", "remote", NOW - timedelta(hours=2), fetched_count=350, new_count=3)
    shown = row(web.page("/health"), "seed", "remote")
    assert "not polled" not in shown
    assert "350" in shown
