"""
strategy/volume_evaluator.py — Volume evaluation for breakout confirmation.

Measures volume context at the moment a breakout fires, answering:
  - Is this breakout happening on above or below average volume?
  - Is volume increasing or fading across the confirmation bars?

The evaluator maintains two rolling averages:
  1. Per-bar volume average (all bars, market hours only)
  2. Time-of-day adjusted average — coming in a future iteration

VolumeSignal is computed once when the breakout confirmation fires
(i.e. when _check_breakout detects N consecutive bars) and stored
on state for the rest of the day.

Fields
------
avg_volume        : rolling average of per-bar volume over recent days
confirm_volume    : total volume accumulated across the N confirm bars
confirm_rel_vol   : confirm_volume / (avg_volume * confirm_bars)
                    > 1.0 = above average volume on breakout
                    < 1.0 = below average volume (quiet breakout)
bar_volumes       : list of individual bar volumes during confirmation
                    first bar = earliest, last bar = the trigger bar
is_increasing     : True if volume trended up across confirm bars
                    (each bar >= previous bar)
is_decreasing     : True if volume trended down across confirm bars
                    (each bar <= previous bar)
has_history       : False during bootstrap period
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, time

from strategy.utils import MARKET_OPEN, MARKET_CLOSE
from utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# VolumeSignal
# ---------------------------------------------------------------------------

@dataclass
class VolumeSignal:
    """
    Volume characterisation at the moment of breakout confirmation.

    avg_volume      : rolling avg of per-bar volume (market hours only)
    confirm_volume  : total volume across the N confirmation bars
    confirm_rel_vol : confirm_volume / (avg_volume * confirm_bars)
                      ratio > 1.0 means above-average volume breakout
    bar_volumes     : volume of each confirmation bar in sequence
    is_increasing   : True if each bar's volume >= the previous bar's
    is_decreasing   : True if each bar's volume <= the previous bar's
    has_history     : False during bootstrap (confirm_rel_vol unreliable)
    confirm_bars    : number of bars in the confirmation window
    trade_date      : the trading date this signal applies to
    """
    avg_volume:      float
    confirm_volume:  float
    confirm_rel_vol: float
    bar_volumes:     list[float]
    is_increasing:   bool
    is_decreasing:   bool
    has_history:     bool
    confirm_bars:    int
    trade_date:      date

    def __str__(self) -> str:
        trend = "increasing" if self.is_increasing else \
                "decreasing" if self.is_decreasing else "mixed"
        hist  = f"{self.confirm_rel_vol:.2f}× avg" if self.has_history \
                else "no history"
        vols  = " → ".join(f"{int(v):,}" for v in self.bar_volumes)
        return (
            f"Volume {hist} | trend={trend} | "
            f"bars=[{vols}] | total={int(self.confirm_volume):,}"
        )


# ---------------------------------------------------------------------------
# VolumeEvaluator
# ---------------------------------------------------------------------------

class VolumeEvaluator:
    """
    Stateful volume evaluator. One instance lives for the life of the
    strategy and persists rolling average state across trading days.

    The evaluator tracks two things continuously:
      1. A rolling average of per-bar volume (market hours bars only)
         updated on every bar throughout the day
      2. A sliding window of the last N bar volumes so that when a
         breakout fires, the confirmation bar volumes are available

    Args:
        rolling_lookback_days : days used in rolling avg (default 50)
        min_bootstrap_days    : samples before has_history=True (default 5)
        bars_to_track         : sliding window of recent bar volumes
                                must be >= confirm_bars (default 20)
    """

    def __init__(
        self,
        rolling_lookback_days: int = 50,
        min_bootstrap_days:    int = 5,
        bars_to_track:         int = 20,
    ):
        self.rolling_lookback_days = rolling_lookback_days
        self.min_bootstrap_days    = min_bootstrap_days
        self.bars_to_track         = bars_to_track

        # Rolling history of daily avg per-bar volume
        self._daily_avg_history: deque[float] = deque(maxlen=rolling_lookback_days)

        # Intraday state
        self._current_date:    date  | None = None
        self._intraday_vols:   deque[float] = deque(maxlen=bars_to_track)
        self._intraday_count:  int   = 0      # market hours bars seen today
        self._intraday_total:  float = 0.0    # sum of volumes today

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def on_bar(
        self,
        bar_date:   date,
        bar_time:   time,
        bar_volume: float,
    ) -> None:
        """
        Feed one bar. Updates rolling state but does not return a signal.
        Call evaluate() when a breakout is confirmed to get VolumeSignal.
        """
        if bar_date != self._current_date:
            self._roll_day(bar_date)

        # Only track market hours bars
        if bar_time < MARKET_OPEN or bar_time >= MARKET_CLOSE:
            return

        self._intraday_vols.append(bar_volume)
        self._intraday_total += bar_volume
        self._intraday_count += 1

    def evaluate(
        self,
        confirm_bars: int,
        trade_date:   date,
    ) -> VolumeSignal:
        """
        Compute and return a VolumeSignal using the last confirm_bars
        bars from the intraday sliding window.

        Call this at the moment the breakout is confirmed — the last
        confirm_bars entries in _intraday_vols are the confirmation bars.
        """
        avg_vol     = self.rolling_avg_volume
        has_history = self.has_sufficient_history

        # Extract the confirmation bar volumes from the sliding window
        available  = list(self._intraday_vols)
        bar_vols   = available[-confirm_bars:] if len(available) >= confirm_bars \
                     else available

        confirm_vol = sum(bar_vols)

        # Relative volume: total confirm vol vs what we'd expect on average
        expected_vol  = avg_vol * len(bar_vols)
        confirm_rel   = (confirm_vol / expected_vol) if (has_history and expected_vol > 0) \
                        else 0.0

        # Volume trend across confirmation bars
        is_increasing = all(
            bar_vols[i] >= bar_vols[i - 1]
            for i in range(1, len(bar_vols))
        ) if len(bar_vols) > 1 else False

        is_decreasing = all(
            bar_vols[i] <= bar_vols[i - 1]
            for i in range(1, len(bar_vols))
        ) if len(bar_vols) > 1 else False

        signal = VolumeSignal(
            avg_volume      = round(avg_vol,     2),
            confirm_volume  = round(confirm_vol, 2),
            confirm_rel_vol = round(confirm_rel, 3),
            bar_volumes     = [round(v, 0) for v in bar_vols],
            is_increasing   = is_increasing,
            is_decreasing   = is_decreasing,
            has_history     = has_history,
            confirm_bars    = len(bar_vols),
            trade_date      = trade_date,
        )

        logger.info(f"[{trade_date}] {signal}")
        return signal

    @property
    def rolling_avg_volume(self) -> float:
        """Rolling average of per-bar volume across recent trading days."""
        if not self._daily_avg_history:
            return 0.0
        return sum(self._daily_avg_history) / len(self._daily_avg_history)

    @property
    def has_sufficient_history(self) -> bool:
        return len(self._daily_avg_history) >= self.min_bootstrap_days

    # -----------------------------------------------------------------------
    # Day management
    # -----------------------------------------------------------------------

    def _roll_day(self, new_date: date) -> None:
        """
        Called on day boundary. Record today's avg per-bar volume into
        the rolling history then reset intraday state.
        """
        if self._current_date is not None and self._intraday_count > 0:
            daily_avg = self._intraday_total / self._intraday_count
            self._daily_avg_history.append(daily_avg)
            logger.debug(
                f"Volume history updated: daily_avg={daily_avg:.1f} "
                f"(n={len(self._daily_avg_history)}, "
                f"rolling_avg={self.rolling_avg_volume:.1f})"
            )

        self._current_date   = new_date
        self._intraday_vols  = deque(maxlen=self.bars_to_track)
        self._intraday_count = 0
        self._intraday_total = 0.0
