"""
strategy/utils.py — Shared time/date utilities for all strategy classes.
"""

import datetime as dt
from datetime import time, date, timedelta, timezone
from zoneinfo import ZoneInfo

ET           = ZoneInfo("America/New_York")
MARKET_OPEN  = time(9, 30)
MARKET_CLOSE = time(15, 45)   # flatten 15 min before close


def ns_to_et(ts_ns: int) -> dt.datetime:
    """Convert a nanosecond UTC timestamp to an ET-aware datetime."""
    ts_s   = ts_ns / 1e9
    dt_utc = dt.datetime.fromtimestamp(ts_s, tz=timezone.utc)
    return dt_utc.astimezone(ET)


def add_minutes(t: time, minutes: int) -> time:
    """Add N minutes to a time object."""
    base  = dt.datetime(2000, 1, 1, t.hour, t.minute, t.second)
    base += timedelta(minutes=minutes)
    return base.time()


def get_expiry_date(trade_date: date, dte: int) -> date:
    """
    Return the expiry date for an option bought on trade_date with target DTE.
    0DTE = same day. 1+ DTE skips weekends (does not skip holidays).
    """
    if dte == 0:
        return trade_date
    expiry     = trade_date
    days_added = 0
    while days_added < dte:
        expiry += timedelta(days=1)
        if expiry.weekday() < 5:    # Mon–Fri only
            days_added += 1
    return expiry
