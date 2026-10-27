"""Pay summaries for the dashboard, from every shape a source reports pay in."""

from typing import Any

import pytest

from jobpulse.core.pay import compensation_summary, yearly_usd_range


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # Lever's salaryRange
        (
            {"currency": "USD", "interval": "per-year-salary", "min": 180000, "max": 230000},
            "$180k–$230k/yr",
        ),
        ({"currency": "USD", "interval": "per-hour-wage", "min": 45, "max": 60.5}, "$45–$60.5/hr"),
        (
            {"currency": "EUR", "interval": "per-month-salary", "min": 4500, "max": 5200},
            "€4.5k–€5.2k/mo",
        ),
        # JobSpy rows (Indeed)
        (
            {
                "min_amount": 150000.0,
                "max_amount": 190000.0,
                "currency": "USD",
                "interval": "yearly",
                "salary_source": "direct_data",
            },
            "$150k–$190k/yr",
        ),
        ({"min_amount": 152500.0, "max_amount": None, "currency": "CHF"}, "CHF 152.5k+"),
        ({"min_amount": None, "max_amount": 90000.0, "currency": "GBP"}, "up to £90k"),
        ({"min_amount": 120000.0, "max_amount": 120000.0, "currency": "USD"}, "$120k"),
        # Greenhouse pay_input_ranges (cents)
        (
            {
                "pay_input_ranges": [
                    {
                        "min_cents": 18000000,
                        "max_cents": 24000000,
                        "currency_type": "USD",
                        "title": "US base salary",
                    },
                    {"min_cents": 1, "max_cents": 2, "currency_type": "CAD", "title": "Canada"},
                ]
            },
            "$180k–$240k",
        ),
        # Ashby summaries are already human-readable
        (
            {
                "compensationTierSummary": "$170K – $220K • Offers Equity",
                "scrapeableCompensationSalarySummary": "$170K - $220K",
            },
            "$170K – $220K • Offers Equity",
        ),
        (
            {"compensationTierSummary": None, "scrapeableCompensationSalarySummary": "$90K"},
            "$90K",
        ),
    ],
)
def test_known_shapes(value: dict[str, Any], expected: str) -> None:
    assert compensation_summary(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"foo": 1},
        {"min": "a lot", "max": None, "currency": "USD"},
        {"pay_input_ranges": []},
        {"pay_input_ranges": [{"title": "no numbers"}]},
        {"compensationTierSummary": "  ", "compensationTiers": []},
    ],
)
def test_unknown_or_empty_shapes_give_none(value: dict[str, Any] | None) -> None:
    assert compensation_summary(value) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        # Lever
        (
            {"currency": "USD", "interval": "per-year-salary", "min": 180000, "max": 230000},
            (180000, 230000),
        ),
        # JobSpy, yearly, hourly and monthly
        (
            {
                "min_amount": 120000.0,
                "max_amount": 150000.0,
                "currency": "USD",
                "interval": "yearly",
            },
            (120000, 150000),
        ),
        (
            {"min_amount": 50.0, "max_amount": 60.0, "currency": "USD", "interval": "hourly"},
            (104000, 124800),
        ),
        (
            {"min_amount": 9000, "max_amount": 10000, "currency": "USD", "interval": "monthly"},
            (108000, 120000),
        ),
        ({"min_amount": 110000.0, "currency": "USD", "interval": "yearly"}, (110000, 110000)),
        ({"max_amount": 140000.0, "currency": "USD", "interval": "yearly"}, (140000, 140000)),
        # no currency given: dollars
        ({"min_amount": 100000, "max_amount": 130000, "interval": "yearly"}, (100000, 130000)),
        # Greenhouse, in cents a year
        (
            {
                "pay_input_ranges": [
                    {"min_cents": 6590000, "max_cents": 12000000, "currency_type": "USD"}
                ]
            },
            (65900, 120000),
        ),
        # Ashby summaries
        ({"compensationTierSummary": "$175K – $250K • Offers Equity"}, (175000, 250000)),
        ({"scrapeableCompensationSalarySummary": "$185K - $215K"}, (185000, 215000)),
        ({"compensationTierSummary": "$45 – $60 per hour"}, (93600, 124800)),
        ({"compensationTierSummary": "$1.2M"}, (1200000, 1200000)),
    ],
)
def test_yearly_usd_ranges(value: dict[str, Any], expected: tuple[float, float]) -> None:
    assert yearly_usd_range(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"currency": "EUR", "interval": "per-year-salary", "min": 60000, "max": 80000},
        {
            "pay_input_ranges": [
                {"min_cents": 6000000, "max_cents": 8000000, "currency_type": "GBP"}
            ]
        },
        {"compensationTierSummary": "€60K – €80K"},
        {"compensationTierSummary": "CA$120K – CA$150K"},
        {"compensationTierSummary": "Competitive"},
        {"min": "a lot", "currency": "USD"},
    ],
)
def test_pay_that_isnt_comparable_dollars(value: dict[str, Any] | None) -> None:
    assert yearly_usd_range(value) is None
