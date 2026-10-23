"""Indian financial-year helpers.

FY 2026-27 runs 1 Apr 2026 -> 31 Mar 2027 and is identified internally by its
start year (2026). Periods are expressed as month keys "YYYY-MM" so that any
document or obligation can be reduced to a set of months for coverage checks.
"""
from __future__ import annotations

import calendar
from datetime import date

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
QUARTER_START_MONTH = {1: 4, 2: 7, 3: 10, 4: 1}


def fy_of(d: date) -> int:
    return d.year if d.month >= 4 else d.year - 1


def fy_label(fy: int) -> str:
    return f"FY {fy}-{str(fy + 1)[-2:]}"


def ay_label(fy: int) -> str:
    return f"AY {fy + 1}-{str(fy + 2)[-2:]}"


def month_key(y: int, m: int) -> str:
    return f"{y:04d}-{m:02d}"


def parse_month_key(k: str) -> tuple[int, int]:
    y, m = k.split("-")
    return int(y), int(m)


def month_label(k: str) -> str:
    y, m = parse_month_key(k)
    return f"{MONTHS[m - 1]} {y}"


def fy_months(fy: int) -> list[str]:
    return [month_key(fy, m) for m in range(4, 13)] + [month_key(fy + 1, m) for m in range(1, 4)]


def quarter_months(fy: int, q: int) -> list[str]:
    return fy_months(fy)[(q - 1) * 3 : q * 3]


def quarter_of_month(k: str) -> int:
    _, m = parse_month_key(k)
    return {4: 1, 5: 1, 6: 1, 7: 2, 8: 2, 9: 2, 10: 3, 11: 3, 12: 3, 1: 4, 2: 4, 3: 4}[m]


def next_month(k: str) -> tuple[int, int]:
    y, m = parse_month_key(k)
    return (y + 1, 1) if m == 12 else (y, m + 1)


def safe_date(y: int, m: int, d: int) -> date:
    return date(y, m, min(d, calendar.monthrange(y, m)[1]))


def months_between(start: str, end: str) -> list[str]:
    """Inclusive list of month keys from start to end."""
    out, cur = [], start
    while cur <= end:
        out.append(cur)
        cur = month_key(*next_month(cur))
    return out


def period_label(months: list[str], fy: int) -> str:
    if months == fy_months(fy):
        return fy_label(fy)
    if len(months) == 1:
        return month_label(months[0])
    for q in range(1, 5):
        if months == quarter_months(fy, q):
            return f"Q{q} {fy_label(fy)}"
    return f"{month_label(months[0])} – {month_label(months[-1])}"
