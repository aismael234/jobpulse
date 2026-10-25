"""``filters.locations.countries`` end to end: a LinkedIn search for "Remote" returns jobs from
anywhere, and only those in your countries are pushed."""

import pytest

from tests.factories import jobspy_frame, jobspy_row
from tests.integration.conftest import Env, ScriptedScrape

pytestmark = pytest.mark.respx(assert_all_called=False)


def only_countries(env: Env, *countries: str) -> None:
    filters = env.config.filters.model_dump()
    filters["locations"]["countries"] = list(countries)
    env.reconfigure(filters=filters)


async def test_only_postings_in_your_countries_are_pushed(env: Env, boards: ScriptedScrape) -> None:
    only_countries(env, "United States")
    env.add_search("ml-remote", "linkedin", location="Remote")
    boards.queue(
        "linkedin",
        jobspy_frame([]),
        jobspy_frame(
            [
                jobspy_row(
                    "linkedin", external_id="4101000031", company="Northwind", location="USA"
                ),
                jobspy_row(
                    "linkedin", external_id="4101000032", company="Calaven", location="Nigeria"
                ),
                jobspy_row(
                    "linkedin",
                    external_id="4101000033",
                    company="Dex",
                    location="London, England, UK",
                ),
            ]
        ),
    )
    await env.poll(sources=["linkedin"])
    report = await env.poll(sources=["linkedin"])

    assert [message.title for message in env.notifier.sent] == [
        "Northwind · Backend Engineer · USA · posted today"
    ]
    abroad = env.posting("linkedin", "4101000032")
    assert (abroad.tier, abroad.tier_reason) == ("filtered", "no location in US (Nigeria)")
    assert env.posting("linkedin", "4101000033").tier == "filtered"
    assert report.targets[0].filtered == 2
