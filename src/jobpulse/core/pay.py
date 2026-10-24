"""A short pay summary for the dashboard from whatever shape a source reports pay in.

- Lever ``salaryRange``: ``{"currency", "interval": "per-year-salary", "min", "max"}``
- JobSpy rows: ``{"min_amount", "max_amount", "currency", "interval": "yearly", ...}``
- Greenhouse ``pay_input_ranges``: ``[{"min_cents", "max_cents", "currency_type", ...}]``
- Ashby: ready-made summaries such as ``"$170K – $220K • Offers Equity"``
"""

import math
from collections.abc import Mapping
from typing import Any

_SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "CAD": "CA$", "AUD": "A$", "INR": "₹"}
_INTERVALS = {
    "per-year-salary": "/yr",
    "yearly": "/yr",
    "per-month-salary": "/mo",
    "monthly": "/mo",
    "per-week-salary": "/wk",
    "weekly": "/wk",
    "per-day-wage": "/day",
    "daily": "/day",
    "per-hour-wage": "/hr",
    "hourly": "/hr",
}
_ASHBY_SUMMARIES = ("compensationTierSummary", "scrapeableCompensationSalarySummary")


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _amount(value: float) -> str:
    if value >= 1000:
        return f"{value / 1000:.1f}".removesuffix(".0") + "k"
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _range(
    low: float | None, high: float | None, currency: str | None, interval: str
) -> str | None:
    if low is None and high is None:
        return None
    code = (currency or "").upper()
    symbol = _SYMBOLS.get(code)

    def money(value: float) -> str:
        return f"{symbol}{_amount(value)}" if symbol else _amount(value)

    if low is not None and high is not None and low != high:
        text = f"{money(min(low, high))}–{money(max(low, high))}"
    elif low is not None:
        text = money(low) if high is not None else f"{money(low)}+"
    else:
        text = f"up to {money(high)}" if high is not None else ""
    if code and not symbol:
        text = f"up to {code} {text.removeprefix('up to ')}" if low is None else f"{code} {text}"
    return text + interval


def compensation_summary(value: Mapping[str, Any] | None) -> str | None:
    """ "$180k–$230k/yr" from Lever, JobSpy, Greenhouse or Ashby pay data; ``None`` if unknown."""
    if not value:
        return None
    for field in _ASHBY_SUMMARIES:
        if summary := _text(value.get(field)):
            return summary
    ranges = value.get("pay_input_ranges")
    if isinstance(ranges, list):
        for item in ranges:
            if not isinstance(item, Mapping):
                continue
            low, high = _number(item.get("min_cents")), _number(item.get("max_cents"))
            text = _range(
                low / 100 if low else None,
                high / 100 if high else None,
                _text(item.get("currency_type")),
                "",
            )
            if text:
                return text
        return None
    interval = _INTERVALS.get(_text(value.get("interval")) or "", "")
    for low_key, high_key in (("min", "max"), ("min_amount", "max_amount")):
        if low_key in value or high_key in value:
            return _range(
                _number(value.get(low_key)),
                _number(value.get(high_key)),
                _text(value.get("currency")),
                interval,
            )
    return None
