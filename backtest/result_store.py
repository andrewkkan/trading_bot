"""
backtest/result_store.py — SQLite result store for backtest runs.

Creates and manages three tables:
  daily_summary  — one row per trading day (range, gap, trade outcome)
  trades         — one row per completed round-trip trade
  events         — one row per state transition (full intraday audit log)

Usage:
    store = ResultStore("backtest/results/my_run.db")
    store.open()
    # ... pass to strategy, which calls log_event / log_trade / log_daily
    store.close()

    # Query afterward
    import sqlite3, pandas as pd
    conn = sqlite3.connect("backtest/results/my_run.db")
    pd.read_sql("SELECT * FROM events WHERE date = '2024-03-14'", conn)

Event types
-----------
  RANGE_SET       range established (high, low, width, window_minutes, used_prior_day)
  RANGE_SKIPPED   range too narrow, day skipped
  GAP_SIGNAL      overnight gap computed (direction, pct, multiple, full_gap)
  VOLUME_SIGNAL   volume evaluated at entry (rel_vol, is_increasing)
  BREAKOUT_LONG   N consecutive closes above range_high
  BREAKOUT_SHORT  N consecutive closes below range_low
  RETEST_LONG     N consecutive closes inside window after LONG breakout
  RETEST_SHORT    N consecutive closes inside window after SHORT breakout
  WINDOW_EXPAND   window boundary expanded (direction, old_boundary, new_boundary)
  ENTRY_LONG      LONG position opened (fill, stop, target)
  ENTRY_SHORT     SHORT position opened (fill, stop, target)
  STOP_LOSS       position closed at stop level
  TAKE_PROFIT     position closed at target level
  EOD_CLOSE       position closed at end of day
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import date, time
from typing import Any

from utils.logger import get_logger

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_summary (
    date              TEXT PRIMARY KEY,
    symbol            TEXT,
    -- Range
    range_high        REAL,
    range_low         REAL,
    range_width       REAL,
    window_minutes    INTEGER,
    used_prior_day    INTEGER,   -- 0/1
    range_skipped     INTEGER,   -- 0/1
    -- Gap
    gap_direction     TEXT,
    gap_pct           REAL,
    gap_multiple      REAL,
    gap_is_full       INTEGER,   -- 0/1
    gap_prior_range   TEXT,      -- ABOVE_HIGH / WITHIN_RANGE / BELOW_LOW / N/A
    -- Trade outcome
    trade_fired       INTEGER,   -- 0/1
    direction         TEXT,
    entry_time        TEXT,
    entry_price       REAL,
    exit_time         TEXT,
    exit_price        REAL,
    pnl               REAL,
    exit_reason       TEXT
);

CREATE TABLE IF NOT EXISTS trades (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    date              TEXT,
    symbol            TEXT,
    direction         TEXT,
    -- Execution
    entry_time        TEXT,
    entry_price       REAL,
    exit_time         TEXT,
    exit_price        REAL,
    quantity          REAL,
    pnl               REAL,
    exit_reason       TEXT,
    -- Range context
    range_high        REAL,
    range_low         REAL,
    range_width       REAL,
    stop_price        REAL,
    target_price      REAL,
    -- Gap signal
    gap_direction     TEXT,
    gap_pct           REAL,
    gap_multiple      REAL,
    gap_is_full       INTEGER,
    gap_prior_range   TEXT,
    -- Volume signal
    vol_rel           REAL,
    vol_is_increasing INTEGER,
    -- Retest context
    window_expanded   INTEGER,   -- 0/1 whether window expansion occurred
    breakout_bars     INTEGER,
    retest_bars       INTEGER,
    reconfirm_bars    INTEGER
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    date        TEXT,
    time        TEXT,
    symbol      TEXT,
    event_type  TEXT,
    bar_close   REAL,
    bar_high    REAL,
    bar_low     REAL,
    detail      TEXT    -- JSON string
);

CREATE INDEX IF NOT EXISTS idx_events_date       ON events(date);
CREATE INDEX IF NOT EXISTS idx_events_type       ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_trades_date       ON trades(date);
CREATE INDEX IF NOT EXISTS idx_daily_date        ON daily_summary(date);
"""


# ---------------------------------------------------------------------------
# ResultStore
# ---------------------------------------------------------------------------

class ResultStore:
    """
    SQLite-backed result store for backtest runs.

    Args:
        db_path : path to the SQLite database file.
                  Parent directory is created if it doesn't exist.
        symbol  : underlying symbol (stored on every row for multi-symbol runs)
    """

    def __init__(self, db_path: str, symbol: str = "QQQ"):
        self.db_path = db_path
        self.symbol  = symbol
        self._conn: sqlite3.Connection | None = None

        # Per-day mutable state — accumulated as the day progresses,
        # written to daily_summary at day boundary or run end
        self._day: dict = {}

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    def open(self) -> None:
        """Open (or create) the database and apply schema."""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._conn = sqlite3.connect(self.db_path)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        logger.info(f"ResultStore opened: {self.db_path}")

    def close(self) -> None:
        """Flush the last day's summary and close the connection."""
        self._flush_daily()
        if self._conn:
            self._conn.commit()
            self._conn.close()
            self._conn = None
        logger.info(f"ResultStore closed: {self.db_path}")

    # -----------------------------------------------------------------------
    # Public logging API — called by ORBBase at each state transition
    # -----------------------------------------------------------------------

    def log_event(
        self,
        bar_date:   date,
        bar_time:   time,
        event_type: str,
        bar_close:  float,
        bar_high:   float,
        bar_low:    float,
        detail:     dict | None = None,
    ) -> None:
        """
        Record one intraday event to the events table.
        Called by ORBBase at every notable state transition.
        """
        if not self._conn:
            return

        # Roll day if needed
        date_str = str(bar_date)
        if self._day.get("date") != date_str:
            self._flush_daily()
            self._day = {"date": date_str, "symbol": self.symbol,
                         "trade_fired": 0, "range_skipped": 0,
                         "window_expanded": 0}

        self._conn.execute(
            """INSERT INTO events
               (date, time, symbol, event_type, bar_close, bar_high, bar_low, detail)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                date_str,
                bar_time.strftime("%H:%M:%S"),
                self.symbol,
                event_type,
                round(bar_close, 6),
                round(bar_high,  6),
                round(bar_low,   6),
                json.dumps(detail) if detail else None,
            ),
        )

        # Keep daily_summary in sync with key events
        self._update_daily_from_event(event_type, detail or {}, bar_time)

    def log_trade(
        self,
        bar_date:       date,
        direction:      str,
        entry_time:     str,
        entry_price:    float,
        exit_time:      str,
        exit_price:     float,
        quantity:       float,
        pnl:            float,
        exit_reason:    str,
        range_high:     float,
        range_low:      float,
        range_width:    float,
        stop_price:     float,
        target_price:   float,
        gap_signal:     Any | None,
        volume_signal:  Any | None,
        window_expanded: bool,
        breakout_bars:  int,
        retest_bars:    int,
        reconfirm_bars: int,
    ) -> None:
        """
        Record one completed trade to the trades table.
        Call this from _on_exit in the subclass.
        """
        if not self._conn:
            return

        gap = gap_signal
        vol = volume_signal

        self._conn.execute(
            """INSERT INTO trades (
                date, symbol, direction,
                entry_time, entry_price, exit_time, exit_price,
                quantity, pnl, exit_reason,
                range_high, range_low, range_width,
                stop_price, target_price,
                gap_direction, gap_pct, gap_multiple, gap_is_full, gap_prior_range,
                vol_rel, vol_is_increasing,
                window_expanded, breakout_bars, retest_bars, reconfirm_bars
            ) VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )""",
            (
                str(bar_date), self.symbol, direction,
                entry_time, round(entry_price, 6),
                exit_time,  round(exit_price,  6),
                quantity, round(pnl, 4), exit_reason,
                round(range_high,  6), round(range_low, 6), round(range_width, 6),
                round(stop_price,  6), round(target_price, 6),
                gap.direction        if gap else "N/A",
                round(gap.gap_pct,  6) if gap else 0.0,
                round(gap.gap_multiple, 4) if gap else 0.0,
                int(gap.is_full_gap) if gap else 0,
                gap.prior_range_pos  if gap else "N/A",
                round(vol.confirm_rel_vol, 4) if vol else 0.0,
                int(vol.is_increasing)        if vol else 0,
                int(window_expanded),
                breakout_bars, retest_bars, reconfirm_bars,
            ),
        )

        # Update daily summary with trade outcome
        date_str = str(bar_date)
        if self._day.get("date") == date_str:
            self._day.update({
                "trade_fired":  1,
                "direction":    direction,
                "entry_time":   entry_time,
                "entry_price":  round(entry_price, 6),
                "exit_time":    exit_time,
                "exit_price":   round(exit_price, 6),
                "pnl":          round(pnl, 4),
                "exit_reason":  exit_reason,
            })

        self._conn.commit()

    def commit(self) -> None:
        """Flush pending writes. Call periodically for long runs."""
        if self._conn:
            self._conn.commit()

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _update_daily_from_event(
        self, event_type: str, detail: dict, bar_time: time
    ) -> None:
        """Keep the in-memory daily dict in sync as events arrive."""
        if event_type == "RANGE_SET":
            self._day.update({
                "range_high":     detail.get("high"),
                "range_low":      detail.get("low"),
                "range_width":    detail.get("width"),
                "window_minutes": detail.get("window_min"),
                "used_prior_day": int(detail.get("used_prior_day", False)),
                "range_skipped":  0,
            })
        elif event_type == "RANGE_SKIPPED":
            self._day["range_skipped"] = 1
        elif event_type == "GAP_SIGNAL":
            self._day.update({
                "gap_direction":  detail.get("direction"),
                "gap_pct":        detail.get("pct"),
                "gap_multiple":   detail.get("multiple"),
                "gap_is_full":    int(detail.get("full_gap", False)),
                "gap_prior_range":detail.get("prior_range_pos"),
            })
        elif event_type == "WINDOW_EXPAND":
            self._day["window_expanded"] = 1

    def _flush_daily(self) -> None:
        """Write the current day dict to daily_summary if it has data."""
        if not self._conn or not self._day.get("date"):
            return

        d = self._day
        self._conn.execute(
            """INSERT OR REPLACE INTO daily_summary (
                date, symbol,
                range_high, range_low, range_width, window_minutes,
                used_prior_day, range_skipped,
                gap_direction, gap_pct, gap_multiple, gap_is_full, gap_prior_range,
                trade_fired, direction,
                entry_time, entry_price, exit_time, exit_price,
                pnl, exit_reason
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                d.get("date"),        d.get("symbol"),
                d.get("range_high"),  d.get("range_low"),
                d.get("range_width"), d.get("window_minutes"),
                d.get("used_prior_day", 0),
                d.get("range_skipped", 0),
                d.get("gap_direction"), d.get("gap_pct"),
                d.get("gap_multiple"),  d.get("gap_is_full", 0),
                d.get("gap_prior_range"),
                d.get("trade_fired", 0), d.get("direction"),
                d.get("entry_time"),  d.get("entry_price"),
                d.get("exit_time"),   d.get("exit_price"),
                d.get("pnl"),         d.get("exit_reason"),
            ),
        )
        self._day = {}
