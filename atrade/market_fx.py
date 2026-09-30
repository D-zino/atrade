"""FX calendar helpers for the isolated XAUUSD book.

All boundaries are evaluated in America/New_York, including DST transitions.
FX-day labels name the rollover date: a Monday 17:00 ET tick belongs to
Tuesday's FX day; Sunday 18:00 ET belongs to Monday's FX day.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ROLLOVER = time(17, 0)
REOPEN = time(18, 0)
# Deliberately thin calendar, as specified for the paper book. Venue-specific
# closures can be added by an operator in the FX config.
FX_HOLIDAYS = frozenset({(1, 1), (12, 25)})


def as_et(value: datetime | date | str | None = None) -> datetime:
    """Normalize a datetime/date/ISO string to an aware New York datetime.

    Naive datetimes are interpreted as ET, which makes fake-clock tests and
    operator-supplied local timestamps unambiguous.
    """
    if value is None:
        return datetime.now(ET)
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=ET)
        return value.astimezone(ET)
    if isinstance(value, date):
        return datetime.combine(value, time.min, tzinfo=ET)
    raise TypeError(f"unsupported clock value: {type(value).__name__}")


def fx_day(now_et: datetime | date | str | None = None) -> date:
    """Return the rollover-date label for ``now_et``.

    The label advances at 17:00 ET, not midnight. Thus Sunday 18:00 labels
    Monday, and Monday 17:00 labels Tuesday. Weekend/holiday labels remain
    useful for marker rollover even when the venue is closed.
    """
    local = as_et(now_et)
    label = local.date()
    if local.timetz().replace(tzinfo=None) >= ROLLOVER:
        label += timedelta(days=1)
    return label


def is_fx_trading_day(day_or_time: date | datetime | str) -> bool:
    """Whether a rollover-date label is a weekday FX day under the thin calendar.

    Datetimes are first translated to their FX-day label; plain dates are
    treated as already-labeled FX days.
    """
    if isinstance(day_or_time, datetime) or isinstance(day_or_time, str):
        label = fx_day(day_or_time)
    elif isinstance(day_or_time, date):
        label = day_or_time
    else:
        raise TypeError(f"unsupported FX day value: {type(day_or_time).__name__}")
    return label.weekday() < 5 and (label.month, label.day) not in FX_HOLIDAYS


def fx_sessions(now_et: datetime | date | str | None = None) -> tuple[str, ...]:
    """Return all named FX sessions active at a New York time.

    Sessions overlap intentionally (Tokyo/London and London/New York). The
    more specific ``overlap`` label is included during the London/New York
    overlap; ``fx_session`` returns a single primary classification.
    """
    local = as_et(now_et)
    weekday = local.weekday()  # Monday=0, Sunday=6
    hm = local.hour * 60 + local.minute

    # Structural weekly closure: Friday 17:00 through Sunday 16:59 ET.
    if (weekday == 4 and hm >= 17 * 60) or weekday == 5 or (weekday == 6 and hm < 17 * 60):
        return ("weekend",)

    # The paper venue treats 17:00-18:00 ET as a maintenance break. On Friday
    # the weekly closure above takes precedence; Sunday reopens at 18:00.
    if 17 * 60 <= hm < 18 * 60:
        return ("break",)

    if not is_fx_trading_day(fx_day(local)):
        return ("holiday",)

    active: list[str] = []
    # Sydney/Asia broad session and Tokyo overlap as given in the design.
    if hm >= 18 * 60 or hm < 3 * 60:
        active.append("asia")
    if hm >= 19 * 60 or hm < 4 * 60:
        active.append("tokyo")
    if 3 * 60 <= hm < 11 * 60 + 30:
        active.append("london")
    if 8 * 60 <= hm < 17 * 60:
        active.append("ny")
    if 8 * 60 <= hm < 11 * 60 + 30:
        active.append("overlap")
    return tuple(active)


def fx_session(now_et: datetime | date | str | None = None) -> str:
    """Return a primary session label: overlap, asia, tokyo, london, ny, etc."""
    sessions = fx_sessions(now_et)
    for label in ("weekend", "break", "holiday", "overlap", "london", "ny", "tokyo", "asia"):
        if label in sessions:
            return label
    return "closed"


def is_fx_open(now_et: datetime | date | str | None = None) -> bool:
    """True only during a weekday, non-holiday tradable session (not break)."""
    return fx_session(now_et) not in {"weekend", "break", "holiday", "closed"}


def fx_trading_days_between(start: date, end: date) -> int:
    """Count FX-day labels in ``(start, end]``; useful for thesis-age checks."""
    if end <= start:
        return 0
    count = 0
    day = start + timedelta(days=1)
    while day <= end:
        if is_fx_trading_day(day):
            count += 1
        day += timedelta(days=1)
    return count


def next_fx_day(day_or_time: date | datetime | str | None = None) -> date:
    """Return the next weekday rollover-date label after the current FX day."""
    current = fx_day(day_or_time)
    candidate = current + timedelta(days=1)
    while not is_fx_trading_day(candidate):
        candidate += timedelta(days=1)
    return candidate
