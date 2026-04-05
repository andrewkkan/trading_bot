"""
strategy/range_builder.py — Adaptive opening range construction.

Replaces the simple high/low accumulation in ORBBase with a statistically
grounded range that adapts to current volatility conditions.

ALGORITHM
=========

Phase 1 — Initial window (e.g. 15 min)
  Accumulate bar high/low as before. When the window closes, check:
    range_width / midpoint >= min_range_pct × rolling_avg_range_pct
  If valid → done, use the range.
  If too narrow → enter Phase 2.

Phase 2 — Expansion (up to max_window_multiplier × initial window)
  Continue accumulating bars. After each new bar re-evaluate:

  A) Does the expanded window alone now meet threshold?
       YES → use it.

  B) Is current price still within prior day's high/low?
       YES → compute candidate = union(expanded_window, prior_day)
             Does candidate meet threshold?
               YES → use candidate range.
               NO  → keep expanding.
       NO  → price already outside prior day range (gap/strong move).
             Ignore prior day. Keep expanding on window only.

Phase 3 — Max window reached, still too narrow
  Skip the day. Log the reason.

ROLLING AVERAGE
===============
After each trading day, the day's final opening range width (as a % of
midpoint) is recorded. The rolling average is computed over the last
rolling_lookback_days samples.

If fewer than min_bootstrap_days samples exist, range validation is
skipped and the raw window range is accepted as-is — this prevents the
strategy from skipping all early trades during the warm-up period.

PRIOR DAY DATA
==============
The builder tracks the prior trading day's high and low from the ohlcv
stream automatically — no separate data source needed.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, time
from typing import Optional

from strategy.utils import add_minutes, MARKET_OPEN
from utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass returned to ORBBase
# ---------------------------------------------------------------------------

@dataclass
class RangeResult:
    """
    Returned by RangeBuilder.on_bar() when a valid range has been established.
    None is returned while still building.
    """
    high:           float       # effective range high
    low:            float       # effective range low
    width:          float       # high - low
    midpoint:       float       # (high + low) / 2
    window_minutes: int         # how many minutes the window ran
    used_prior_day: bool        # whether prior day boundaries contributed
    skipped:        bool = False  # True when max window hit and still too narrow


# ---------------------------------------------------------------------------
# Per-day builder state
# ---------------------------------------------------------------------------

@dataclass
class _DayBuildState:
    window_high:    float = 0.0
    window_low:     float = float("inf")
    phase:          str   = "building"   # "building" | "expanding" | "done" | "skipped"
    bars_seen:      int   = 0
    result:         Optional[RangeResult] = None


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

class RangeBuilder:
    """
    Stateful opening range builder. One instance lives for the life of the
    strategy and persists rolling-average state across trading days.

    Args:
        opening_range_minutes   : initial window length in minutes
        max_window_multiplier   : cap expansion at this multiple of the initial
                                  window (default 16 → up to 240 min for 15m base)
        min_range_pct           : range must be >= this fraction of the rolling
                                  average to be considered valid (default 0.5)
        rolling_lookback_days   : number of prior trading days used to compute
                                  the rolling average range width (default 50)
        min_bootstrap_days      : minimum samples before validation is applied;
                                  below this, all ranges are accepted (default 5)
    """

    def __init__(
        self,
        opening_range_minutes:  int   = 15,
        max_window_multiplier:  int   = 16,
        min_range_pct:          float = 0.5,
        rolling_lookback_days:  int   = 50,
        min_bootstrap_days:     int   = 5,
    ):
        self.opening_range_minutes = opening_range_minutes
        self.max_window_minutes    = opening_range_minutes * max_window_multiplier
        self.min_range_pct         = min_range_pct
        self.rolling_lookback_days = rolling_lookback_days
        self.min_bootstrap_days    = min_bootstrap_days

        # Rolling history of (range_width / midpoint) for recent trading days
        self._range_pct_history: deque[float] = deque(maxlen=rolling_lookback_days)

        # Prior day OHLCV summary — updated at end of each day
        self._prior_day_high:  float | None = None
        self._prior_day_low:   float | None = None
        self._prior_day_date:  date  | None = None

        # Intraday tracking for prior day computation
        self._today_high:  float = 0.0
        self._today_low:   float = float("inf")
        self._today_date:  date  | None = None

        # Current day build state
        self._state = _DayBuildState()
        self._current_date: date | None = None

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def on_bar(
        self,
        bar_date:  date,
        bar_time:  time,
        bar_high:  float,
        bar_low:   float,
        bar_close: float,
    ) -> RangeResult | None:
        """
        Feed one bar. Returns a RangeResult once a valid range is established,
        or None while still building.

        A RangeResult with skipped=True is returned when the max window is
        reached without finding a valid range — the caller should skip trading
        that day.
        """
        self._update_daily_tracking(bar_date, bar_high, bar_low)

        if bar_date != self._current_date:
            self._roll_day(bar_date)

        if self._state.phase == "done" or self._state.phase == "skipped":
            return self._state.result

        # Expand the window high/low
        self._state.window_high = max(self._state.window_high, bar_high)
        self._state.window_low  = min(self._state.window_low,  bar_low)
        self._state.bars_seen  += 1

        elapsed_minutes = self._elapsed_minutes(bar_time)

        # Still inside initial window — keep accumulating
        if elapsed_minutes < self.opening_range_minutes:
            return None

        # Initial window just closed or we are in expansion phase
        result = self._evaluate(bar_close, elapsed_minutes)
        if result is not None:
            self._state.phase  = "skipped" if result.skipped else "done"
            self._state.result = result
            return result

        return None

    def record_day_complete(self) -> None:
        """
        Call this at end of day (or day boundary) to record the day's opening
        range in the rolling history. Called automatically by on_bar() on day
        rollover, but can also be called explicitly.
        """
        if self._state.result and not self._state.result.skipped:
            r = self._state.result
            if r.midpoint > 0:
                pct = r.width / r.midpoint
                self._range_pct_history.append(pct)
                logger.debug(
                    f"Range history updated: {pct:.4%} "
                    f"(n={len(self._range_pct_history)}, "
                    f"avg={self.rolling_avg_range_pct:.4%})"
                )

    @property
    def rolling_avg_range_pct(self) -> float:
        """Current rolling average range width as a fraction of midpoint."""
        if not self._range_pct_history:
            return 0.0
        return sum(self._range_pct_history) / len(self._range_pct_history)

    @property
    def has_sufficient_history(self) -> bool:
        """True once enough samples exist to apply range validation."""
        return len(self._range_pct_history) >= self.min_bootstrap_days

    @property
    def threshold_pct(self) -> float:
        """Current minimum acceptable range width as a fraction of midpoint."""
        return self.min_range_pct * self.rolling_avg_range_pct

    # -----------------------------------------------------------------------
    # Core evaluation logic
    # -----------------------------------------------------------------------

    def _evaluate(
        self, bar_close: float, elapsed_minutes: int,
    ) -> RangeResult | None:
        """
        Check whether the current accumulated window forms a valid range.
        Returns a RangeResult if valid or if max window is reached,
        None to keep accumulating.
        """
        w_high  = self._state.window_high
        w_low   = self._state.window_low
        w_width = w_high - w_low
        w_mid   = (w_high + w_low) / 2.0

        # Bootstrap period — accept whatever we have at initial window close
        if not self.has_sufficient_history:
            if elapsed_minutes >= self.opening_range_minutes:
                logger.info(
                    f"  Range (bootstrap, n={len(self._range_pct_history)}): "
                    f"high={w_high:.4f} low={w_low:.4f} width={w_width:.4f}"
                )
                return self._make_result(w_high, w_low, elapsed_minutes, False)
            return None

        threshold_width = self.threshold_pct * w_mid

        # A) Window alone meets threshold
        if w_width >= threshold_width:
            logger.info(
                f"  Range valid (window alone): "
                f"high={w_high:.4f} low={w_low:.4f} "
                f"width={w_width:.4f} vs threshold={threshold_width:.4f} "
                f"window={elapsed_minutes}m"
            )
            return self._make_result(w_high, w_low, elapsed_minutes, False)

        # B) Try incorporating prior day if price is within prior day range
        if (
            self._prior_day_high is not None
            and self._prior_day_low  is not None
            and self._prior_day_low <= bar_close <= self._prior_day_high
        ):
            c_high  = max(w_high, self._prior_day_high)
            c_low   = min(w_low,  self._prior_day_low)
            c_width = c_high - c_low
            c_mid   = (c_high + c_low) / 2.0
            c_threshold = self.threshold_pct * c_mid

            if c_width >= c_threshold:
                logger.info(
                    f"  Range valid (window + prior day): "
                    f"high={c_high:.4f} low={c_low:.4f} "
                    f"width={c_width:.4f} vs threshold={c_threshold:.4f} "
                    f"window={elapsed_minutes}m "
                    f"prior_day=({self._prior_day_low:.4f}-{self._prior_day_high:.4f})"
                )
                return self._make_result(c_high, c_low, elapsed_minutes, True)
            else:
                logger.debug(
                    f"  Prior day union still too narrow "
                    f"(width={c_width:.4f} vs threshold={c_threshold:.4f})"
                )
        else:
            if self._prior_day_high is not None:
                logger.debug(
                    f"  Price {bar_close:.4f} outside prior day range "
                    f"({self._prior_day_low:.4f}-{self._prior_day_high:.4f}) "
                    f"— ignoring prior day"
                )

        # C) Max window reached — skip the day
        if elapsed_minutes >= self.max_window_minutes:
            logger.warning(
                f"  Max window ({self.max_window_minutes}m) reached, "
                f"range still too narrow "
                f"(width={w_width:.4f} vs threshold={threshold_width:.4f}) "
                f"— skipping day."
            )
            return RangeResult(
                high=w_high, low=w_low, width=w_width,
                midpoint=w_mid, window_minutes=elapsed_minutes,
                used_prior_day=False, skipped=True,
            )

        # Still expanding — keep accumulating
        return None

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _make_result(
        self, high: float, low: float,
        elapsed_minutes: int, used_prior_day: bool,
    ) -> RangeResult:
        width    = high - low
        midpoint = (high + low) / 2.0
        return RangeResult(
            high           = high,
            low            = low,
            width          = width,
            midpoint       = midpoint,
            window_minutes = elapsed_minutes,
            used_prior_day = used_prior_day,
            skipped        = False,
        )

    def _elapsed_minutes(self, bar_time: time) -> int:
        """Minutes elapsed since MARKET_OPEN."""
        open_seconds = MARKET_OPEN.hour * 3600 + MARKET_OPEN.minute * 60
        bar_seconds  = bar_time.hour  * 3600 + bar_time.minute  * 60 + bar_time.second
        return max(0, (bar_seconds - open_seconds) // 60)

    def _roll_day(self, new_date: date) -> None:
        """Called on day boundary — persist today's data as prior day."""
        self.record_day_complete()

        if self._today_date is not None:
            self._prior_day_high = self._today_high
            self._prior_day_low  = self._today_low
            self._prior_day_date = self._today_date
            logger.debug(
                f"Prior day set: {self._prior_day_date} "
                f"high={self._prior_day_high:.4f} low={self._prior_day_low:.4f}"
            )

        self._current_date      = new_date
        self._today_date        = new_date
        self._today_high        = 0.0
        self._today_low         = float("inf")
        self._state             = _DayBuildState()

    def _update_daily_tracking(
        self, bar_date: date, bar_high: float, bar_low: float,
    ) -> None:
        """Track intraday high/low for prior day computation."""
        if bar_date == self._today_date:
            self._today_high = max(self._today_high, bar_high)
            self._today_low  = min(self._today_low,  bar_low)
