"""Pay summaries for the dashboard, from every shape a source reports pay in."""

from typing import Any

import pytest

from jobpulse.core.pay import compensation_summary


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
