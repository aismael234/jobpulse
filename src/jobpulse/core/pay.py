"""A short pay summary for the dashboard from whatever shape a source reports pay in.

- Lever ``salaryRange``: ``{"currency", "interval": "per-year-salary", "min", "max"}``
- JobSpy rows: ``{"min_amount", "max_amount", "currency", "interval": "yearly", ...}``
- Greenhouse ``pay_input_ranges``: ``[{"min_cents", "max_cents", "currency_type", ...}]``
- Ashby: ready-made summaries such as ``"$170K – $220K • Offers Equity"``
"""

import math
import re
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
HOURS_A_YEAR = 2080.0
_PER_YEAR = {
    "per-year-salary": 1.0,
    "yearly": 1.0,
    "per-month-salary": 12.0,
    "monthly": 12.0,
    "per-week-salary": 52.0,
    "weekly": 52.0,
    "per-day-wage": 260.0,
    "daily": 260.0,
    "per-hour-wage": HOURS_A_YEAR,
    "hourly": HOURS_A_YEAR,
}
_DOLLARS = re.compile(r"\$\s?(\d[\d,]*(?:\.\d+)?)\s*([KkMm])?\b")
_FOREIGN_DOLLARS = re.compile(r"[A-Za-z]\$")  # CA$, A$, NZ$
_SUFFIXES = {"k": 1000.0, "m": 1_000_000.0}
_HOURLY = re.compile(r"/\s*h(?:ou)?r\b|\bper hour\b|\ban hour\b|\bhourly\b", re.IGNORECASE)


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


def _yearly(low: float | None, high: float | None, per_year: float) -> tuple[float, float] | None:
    low, high = low or high, high or low
    if low is None or high is None:
        return None
    return min(low, high) * per_year, max(low, high) * per_year


def _per_year(amount: float | None) -> float:
    """For pay given without an interval: an amount under $1,000 is hourly."""
    return HOURS_A_YEAR if amount is not None and amount < 1000 else 1.0


def _summary_range(summary: str) -> tuple[float, float] | None:
    """ "$175K – $250K • Offers Equity" → (175000, 250000); other currencies ("CA$", "€") and
    summaries without amounts give ``None``."""
    amounts = [
        float(number.replace(",", "")) * _SUFFIXES.get(suffix.lower(), 1.0)
        for number, suffix in _DOLLARS.findall(summary)
    ]
    if not amounts or _FOREIGN_DOLLARS.search(summary):
        return None
    per_year = HOURS_A_YEAR if _HOURLY.search(summary) else _per_year(max(amounts))
    return _yearly(min(amounts), max(amounts), per_year)


def yearly_usd_range(value: Mapping[str, Any] | None) -> tuple[float, float] | None:
    """(low, high) US dollars a year, from the shapes ``compensation_summary`` reads: hourly
    pay times 2,080, weekly times 52, monthly times 12. Pay in another currency, or without
    amounts, is ``None``."""
    if not value:
        return None
    for field in _ASHBY_SUMMARIES:
        if summary := _text(value.get(field)):
            return _summary_range(summary)
    ranges = value.get("pay_input_ranges")
    if isinstance(ranges, list):
        for item in ranges:
            if not isinstance(item, Mapping):
                continue
            if (_text(item.get("currency_type")) or "USD").upper() != "USD":
                return None
            low, high = _number(item.get("min_cents")), _number(item.get("max_cents"))
            low, high = (low / 100 if low else None), (high / 100 if high else None)
            if found := _yearly(low, high, _per_year(high or low)):
                return found
        return None
    if (_text(value.get("currency")) or "USD").upper() != "USD":
        return None
    for low_key, high_key in (("min", "max"), ("min_amount", "max_amount")):
        if low_key in value or high_key in value:
            low, high = _number(value.get(low_key)), _number(value.get(high_key))
            interval = _text(value.get("interval")) or ""
            return _yearly(low, high, _PER_YEAR.get(interval) or _per_year(high or low))
    return None
