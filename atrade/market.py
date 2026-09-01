"""US market calendar: sessions, holidays, trading-hour logic.

2026 NYSE holiday calendar embedded (open to manual override via config).
Early close days: the Friday after Thanksgiving and (observed) July 3rd.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

# NYSE 2026 holidays (US market closed)
HOLIDAYS_2026 = {
    date(2026, 1, 1),   # New Year's Day
    date(2026, 1, 19),  # MLK Day
    date(2026, 2, 16),  # Washington's Birthday
    date(2026, 4, 3),   # Good Friday
    date(2026, 5, 25),  # Memorial Day
    date(2026, 6, 19),  # Juneteenth (observed)
    date(2026, 7, 3),   # Independence Day (observed)
    date(2026, 9, 7),   # Labor Day
    date(2026, 11, 26),  # Thanksgiving
    date(2026, 12, 25),  # Christmas
}
EARLY_CLOSE_2026 = {
    date(2026, 7, 3),     # closes 1:00 pm
    date(2026, 11, 27),   # day after Thanksgiving
    date(2026, 12, 24),   # Christmas Eve
}

TZ = ZoneInfo("America/New_York")


def is_trading_day(d: date) -> bool:
    if d.weekday() >= 5:
        return False
    if d in HOLIDAYS_2026:
        return False
    return True


def is_early_close(d: date) -> bool:
    return d in EARLY_CLOSE_2026


def next_trading_day(d: date) -> date:
    nxt = d + timedelta(days=1)
    while not is_trading_day(nxt):
        nxt += timedelta(days=1)
    return nxt


def prev_trading_day(d: date) -> date:
    prv = d - timedelta(days=1)
    while not is_trading_day(prv):
        prv -= timedelta(days=1)
    return prv


def session_bounds(d: date) -> tuple[datetime, datetime]:
    """Return (open, close) as ET-aware datetimes for a trading day."""
    open_dt = datetime.combine(d, time(9, 30), tzinfo=TZ)
    close_dt = datetime.combine(d, time(13, 0), tzinfo=TZ) if is_early_close(d) else datetime.combine(d, time(16, 0), tzinfo=TZ)
    return open_dt, close_dt


def market_status(now_et: datetime) -> dict:
    """Classify a moment: 'pre', 'open', 'post', 'closed_day', 'weekend', 'holiday'."""
    d = now_et.date()
    if not is_trading_day(d):
        reason = "holiday" if d in HOLIDAYS_2026 else "weekend"
        return {"status": "closed", "reason": reason}
    open_dt, close_dt = session_bounds(d)
    if now_et < open_dt:
        return {"status": "pre", "reason": "before_open"}
    if now_et > close_dt:
        return {"status": "post", "reason": "after_close"}
    return {"status": "open", "reason": "session"}


def next_schedule(now_utc: datetime, open_time_et: str = "09:25", close_time_et: str = "15:50",
                  early_close_time_et: str = "12:50") -> list[dict]:
    """Next (up to 3) upcoming run slots: {type: open|close, at_et, at_utc}."""
    hh, mm = map(int, open_time_et.split(":"))
    chh, cmm = map(int, close_time_et.split(":"))
    ehh, emm = map(int, early_close_time_et.split(":"))
    now_et = now_utc.astimezone(TZ)
    slots = []
    d = now_et.date()
    for _ in range(12):  # look ahead up to 12 calendar days
        if is_trading_day(d):
            close_t = time(ehh, emm) if is_early_close(d) else time(chh, cmm)
            for kind, t in (("open", time(hh, mm)), ("close", close_t)):
                at_et = datetime.combine(d, t, tzinfo=TZ)
                if at_et > now_et:
                    slots.append({"type": kind, "date": d.isoformat(),
                                  "at_et": at_et.isoformat(timespec="minutes"),
                                  "at_utc": at_et.astimezone(timezone.utc).isoformat(timespec="minutes")})
            if len(slots) >= 3:
                return slots[:3]
        d += timedelta(days=1)
    return slots


def date_str(d: date | datetime | None = None) -> str:
    if d is None:
        d = datetime.now(TZ).date()
    if isinstance(d, datetime):
        d = d.date()
    return d.isoformat()
