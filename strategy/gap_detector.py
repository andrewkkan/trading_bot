"""
strategy/gap_detector.py — Overnight gap detection and classification.

Measures two things on every trading day:
  1. Open vs prior close  — magnitude and direction of the overnight move
  2. Open vs prior range  — whether the gap opened into clear air or
                            within the prior day's familiar price zone

Both are expressed as percentages of the prior close so they are
comparable across different price levels and time periods.

A rolling average of abs(gap_pct) over the last N trading days gives a
regime-aware baseline — the same way RangeBuilder uses a rolling average
for range width. During the bootstrap period (fewer than min_bootstrap_days
samples) gap_multiple is 0.0 and has_history is False.

The GapSignal is frozen once per day on the first bar at or after
MARKET_OPEN and is read-only for the rest of the session.

Usage:
    detector = GapDetector()

    for record in store:
        bar_date  = ...
        bar_time  = ...
        bar_open  = record.open  / 1e9
        bar_close = record.close / 1e9

        signal = detector.on_bar(bar_date, bar_time, bar_open, bar_close)
        if signal is not None and signal.is_new:
            logger.info(f"Gap detected: {signal}")
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import date, time

from strategy.utils import MARKET_OPEN
from utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# GapSignal — frozen once per day at market open
# ---------------------------------------------------------------------------

@dataclass
class GapSignal:
    """
    Complete gap characterisation for one trading day.

    vs prior close
    --------------
    gap_pct         : signed pct move from prior close to today's open
                      positive = gap up, negative = gap down
                      e.g. +0.0073 means open was 0.73% above prior close
    direction       : "UP", "DOWN", or "NONE" (abs < none_threshold)
    gap_multiple    : abs(gap_pct) / rolling_avg_gap_pct
                      how many times larger than the recent average gap
                      0.0 during bootstrap period
    avg_gap_pct     : current rolling average of abs(gap_pct) over
                      the last rolling_lookback_days trading days
    has_history     : False during bootstrap (gap_multiple unreliable)

    vs prior high/low
    -----------------
    prior_range_pos : "ABOVE_HIGH"   — open above prior day's high (full gap up)
                      "BELOW_LOW"    — open below prior day's low  (full gap down)
                      "WITHIN_RANGE" — open inside prior day's range
                      "NO_DATA"      — no prior day data yet
    dist_from_high  : (open - prior_high) / prior_close  signed pct
                      positive = open was above prior high
                      negative = open was below prior high (within/below range)
    dist_from_low   : (open - prior_low) / prior_close  signed pct
                      positive = open was above prior low
                      negative = open was below prior low (full gap down)

    derived
    -------
    is_full_gap     : True if prior_range_pos is ABOVE_HIGH or BELOW_LOW
    is_partial_gap  : True if direction is not NONE but within range
    is_new          : True on the bar the signal was first computed
                      (False on subsequent bars of the same day)
    trade_date      : the trading date this signal applies to
    today_open      : underlying open price used to compute the signal
    prior_close     : prior day close used as the reference price
    prior_high      : prior day high
    prior_low       : prior day low
    """

    # vs prior close
    gap_pct:          float
    direction:        str
    gap_multiple:     float
    avg_gap_pct:      float
    has_history:      bool

    # vs prior high/low
    prior_range_pos:  str
    dist_from_high:   float
    dist_from_low:    float

    # derived
    is_full_gap:      bool
    is_partial_gap:   bool
    is_new:           bool

    # context
    trade_date:       date
    today_open:       float
    prior_close:      float
    prior_high:       float
    prior_low:        float

    def __str__(self) -> str:
        dir_str  = f"{self.direction} {self.gap_pct:+.2%}"
        mult_str = f"{self.gap_multiple:.2f}× avg" if self.has_history else "no history"
        pos_str  = self.prior_range_pos
        full_str = " FULL GAP" if self.is_full_gap else ""
        return (
            f"Gap {dir_str} ({mult_str}) | "
            f"{pos_str}{full_str} | "
            f"dist_high={self.dist_from_high:+.2%} "
            f"dist_low={self.dist_from_low:+.2%}"
        )


# ---------------------------------------------------------------------------
# GapDetector
# ---------------------------------------------------------------------------

class GapDetector:
    """
    Stateful gap detector. One instance lives for the life of the strategy.
    Persists prior day data and rolling gap history across trading days.

    Args:
        rolling_lookback_days : days in rolling avg of abs(gap_pct) (default 50)
        min_bootstrap_days    : samples before gap_multiple is meaningful (default 5)
        none_threshold        : abs(gap_pct) below this is classified as NONE (default 0.001 = 0.1%)
    """

    def __init__(
        self,
        rolling_lookback_days: int   = 50,
        min_bootstrap_days:    int   = 5,
        none_threshold:        float = 0.001,
    ):
        self.rolling_lookback_days = rolling_lookback_days
        self.min_bootstrap_days    = min_bootstrap_days
        self.none_threshold        = none_threshold

        # Rolling history of abs(gap_pct) for recent trading days
        self._gap_pct_history: deque[float] = deque(maxlen=rolling_lookback_days)

        # Prior day OHLC — updated at day boundary
        self._prior_close: float | None = None
        self._prior_high:  float | None = None
        self._prior_low:   float | None = None
        self._prior_date:  date  | None = None

        # Intraday tracking for prior day computation
        self._today_open:  float | None = None
        self._today_high:  float = 0.0
        self._today_low:   float = float("inf")
        self._today_close: float = 0.0
        self._today_date:  date  | None = None

        # Current day signal — set once at market open
        self._current_signal: GapSignal | None = None
        self._current_date:   date | None       = None

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def on_bar(
        self,
        bar_date:  date,
        bar_time:  time,
        bar_open:  float,
        bar_close: float,
        bar_high:  float,
        bar_low:   float,
    ) -> GapSignal | None:
        """
        Feed one bar. Returns the GapSignal for today, or None if the
        signal hasn't been computed yet (pre-market bars).

        The signal is computed once on the first bar at or after MARKET_OPEN
        and returned with is_new=True. All subsequent bars of the same day
        return the same signal with is_new=False.
        """
        # Day boundary
        if bar_date != self._current_date:
            self._roll_day(bar_date)

        # Update intraday tracking
        self._update_intraday(bar_open, bar_high, bar_low, bar_close)

        # Compute signal on first bar at or after market open
        if self._current_signal is None and bar_time >= MARKET_OPEN:
            self._current_signal = self._compute_signal(bar_date, bar_open)
            if self._current_signal is not None:
                logger.info(
                    f"[{bar_date}] {self._current_signal}"
                )
            return self._current_signal

        # Return existing signal with is_new=False after the first bar
        if self._current_signal is not None:
            if self._current_signal.is_new:
                self._current_signal.is_new = False
            return self._current_signal

        return None

    @property
    def rolling_avg_gap_pct(self) -> float:
        """Current rolling average of abs(gap_pct) over recent trading days."""
        if not self._gap_pct_history:
            return 0.0
        return sum(self._gap_pct_history) / len(self._gap_pct_history)

    @property
    def has_sufficient_history(self) -> bool:
        return len(self._gap_pct_history) >= self.min_bootstrap_days

    # -----------------------------------------------------------------------
    # Signal computation
    # -----------------------------------------------------------------------

    def _compute_signal(self, bar_date: date, today_open: float) -> GapSignal | None:
        """
        Compute the GapSignal using today's open and prior day data.
        Returns None if no prior day data is available yet.
        """
        if (
            self._prior_close is None
            or self._prior_high  is None
            or self._prior_low   is None
        ):
            logger.debug(f"[{bar_date}] No prior day data yet — gap signal unavailable.")
            return None

        prior_close = self._prior_close
        prior_high  = self._prior_high
        prior_low   = self._prior_low

        # --- vs prior close ---
        gap_pct   = (today_open - prior_close) / prior_close
        abs_gap   = abs(gap_pct)

        if abs_gap < self.none_threshold:
            direction = "NONE"
        elif gap_pct > 0:
            direction = "UP"
        else:
            direction = "DOWN"

        avg_gap     = self.rolling_avg_gap_pct
        has_history = self.has_sufficient_history
        gap_multiple = (abs_gap / avg_gap) if (has_history and avg_gap > 0) else 0.0

        # --- vs prior high/low ---
        dist_from_high = (today_open - prior_high) / prior_close
        dist_from_low  = (today_open - prior_low)  / prior_close

        if today_open > prior_high:
            prior_range_pos = "ABOVE_HIGH"
        elif today_open < prior_low:
            prior_range_pos = "BELOW_LOW"
        else:
            prior_range_pos = "WITHIN_RANGE"

        is_full_gap    = prior_range_pos in ("ABOVE_HIGH", "BELOW_LOW")
        is_partial_gap = (direction != "NONE") and (prior_range_pos == "WITHIN_RANGE")

        # Record gap in rolling history
        self._gap_pct_history.append(abs_gap)

        return GapSignal(
            gap_pct         = round(gap_pct,        6),
            direction       = direction,
            gap_multiple    = round(gap_multiple,   3),
            avg_gap_pct     = round(avg_gap,        6),
            has_history     = has_history,
            prior_range_pos = prior_range_pos,
            dist_from_high  = round(dist_from_high, 6),
            dist_from_low   = round(dist_from_low,  6),
            is_full_gap     = is_full_gap,
            is_partial_gap  = is_partial_gap,
            is_new          = True,
            trade_date      = bar_date,
            today_open      = round(today_open,  4),
            prior_close     = round(prior_close, 4),
            prior_high      = round(prior_high,  4),
            prior_low       = round(prior_low,   4),
        )

    # -----------------------------------------------------------------------
    # Day management
    # -----------------------------------------------------------------------

    def _roll_day(self, new_date: date) -> None:
        """
        Called on day boundary. Persist today's OHLC as prior day.
        Reset intraday tracking for the new day.
        """
        if self._today_date is not None and self._today_close > 0:
            self._prior_close = self._today_close
            self._prior_high  = self._today_high
            self._prior_low   = self._today_low
            self._prior_date  = self._today_date
            logger.debug(
                f"Prior day set: {self._prior_date} "
                f"close={self._prior_close:.4f} "
                f"high={self._prior_high:.4f} "
                f"low={self._prior_low:.4f}"
            )

        self._current_date   = new_date
        self._today_date     = new_date
        self._today_open     = None
        self._today_high     = 0.0
        self._today_low      = float("inf")
        self._today_close    = 0.0
        self._current_signal = None

    def _update_intraday(
        self,
        bar_open:  float,
        bar_high:  float,
        bar_low:   float,
        bar_close: float,
    ) -> None:
        """Track OHLC for current day to build prior day data."""
        if self._today_open is None:
            self._today_open = bar_open
        self._today_high  = max(self._today_high, bar_high)
        self._today_low   = min(self._today_low,  bar_low)
        self._today_close = bar_close
