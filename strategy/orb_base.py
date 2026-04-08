"""
strategy/orb_base.py — Abstract base class for all ORB variants.

DESIGN PRINCIPLE
================
All trading decisions are made on the underlying price — always.
Subclasses only decide how to execute once a signal fires.

What the base class owns (final — do not override):
  - OHLCV parsing
  - Day boundary reset
  - Market hours gate
  - Daily loss limit
  - Opening range construction (via RangeBuilder — adaptive, validated)
  - Gap detection per day (via GapDetector — stored on state, annotation only)
  - Volume evaluation at breakout (via VolumeEvaluator — stored on state, annotation only)
  - Breakout/retest/entry state machine (via RetestEngine — both directions)
  - Position management  (stop/target on underlying price)
  - EOD flatten

What subclasses implement (execution only):
  _make_day_state()                         → fresh per-day state dataclass
  _on_new_day(bar_date)                     → subclass logging at day start
  _on_range_set(result)                     → subclass logging when range closes
  _on_entry(direction, bar_close, ...)      → build and return entry order
  _on_exit(reason, exit_price, ...)         → build and return closing order

Signal model
============
direction = "LONG"  : underlying closed above range high
direction = "SHORT" : underlying closed below range low

Stop  : opposite side of range      (LONG=range_low,  SHORT=range_high)
Target: entry ± rr_ratio × width   (LONG=entry+dist, SHORT=entry-dist)

Stops checked with bar_low (long) / bar_high (short) to catch gaps.

OHLCV bar sequence assumption
==============================
Within a 1-second bar we don't know the order of high and low. We infer:
  close_position = (bar_close - bar_low) / (bar_high - bar_low)
  >= 0.5 → close near high → high came first
  <  0.5 → close near low  → low came first

For LONG: if high first, check target before stop (and vice versa).
For SHORT: if low first, check target before stop (and vice versa).
This eliminates the ambiguity of a bar hitting both stop and target.

Slippage model
==============
A fixed per-share slippage is applied to every fill:
  LONG  entry : fill = bar_close + slippage
  SHORT entry : fill = bar_close - slippage
  LONG  exit  : fill = exit_price - slippage
  SHORT exit  : fill = exit_price + slippage
EOD flattens use bar_close with slippage applied.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import time, date
from typing import Any

from strategy.range_builder import RangeBuilder, RangeResult
from strategy.gap_detector import GapDetector, GapSignal
from strategy.volume_evaluator import VolumeEvaluator, VolumeSignal
from strategy.retest_engine import RetestEngine, RetestResult
from strategy.utils import ns_to_et, MARKET_OPEN, MARKET_CLOSE
from utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Shared per-day state
# ---------------------------------------------------------------------------

@dataclass
class ORBDayState:
    """
    Core per-day state managed entirely by ORBBase.
    Subclasses extend this with their own execution fields.
    """
    # Range (populated by RangeBuilder)
    range_high:     float = 0.0
    range_low:      float = float("inf")
    range_set:      bool  = False
    range_width:    float = 0.0
    range_skipped:  bool  = False   # True when day was skipped (too narrow)
    gap_signal:     object = None    # GapSignal — set on first bar at market open
    volume_signal:  object = None    # VolumeSignal — set when breakout is confirmed
    # Signal / position
    trade_fired:    bool  = False
    direction:      str   = ""      # "LONG" or "SHORT"
    position:       int   = 0       # non-zero = in a trade
    entry_price:    float = 0.0
    stop_price:     float = 0.0
    target_price:   float = 0.0
    daily_pnl:      float = 0.0


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class ORBBase(ABC):
    """
    Abstract base for all Opening Range Breakout strategies.

    Args:
        symbol                : underlying ticker
        opening_range_minutes : initial window length in minutes
        rr_ratio              : take-profit = rr_ratio × range_width
        max_daily_loss        : halt if cumulative day loss exceeds this
        max_window_multiplier : cap expansion at N × initial window (default 16)
        min_range_pct         : range must be >= this × rolling avg (default 0.5)
        rolling_lookback_days : days in rolling avg (default 50)
        min_bootstrap_days    : samples needed before validation applied (default 5)
        breakout_bars         : consecutive closes outside range to confirm breakout (default 3)
        retest_bars           : consecutive closes inside window to credit retest (default 3)
        reconfirm_bars        : consecutive closes outside window after retest to enter (default 3)
        min_hold_minutes      : minimum minutes between entry and EOD close (default 30)
        gap_lookback_days     : rolling avg lookback for gap history (default 50)
        gap_none_threshold    : abs(gap_pct) below this = NONE direction (default 0.001)
        vol_lookback_days     : rolling avg lookback for volume history (default 50)
        vol_bars_to_track     : sliding window of recent bar volumes (default 20)
        slippage              : fixed per-share slippage per fill (default 0.01)
    """

    def __init__(
        self,
        symbol:                str   = "QQQ",
        opening_range_minutes: int   = 15,
        rr_ratio:              float = 2.0,
        max_daily_loss:        float = 1000.0,
        max_window_multiplier: int   = 16,
        min_range_pct:         float = 0.5,
        rolling_lookback_days: int   = 50,
        min_bootstrap_days:    int   = 5,
        breakout_bars:         int   = 3,
        retest_bars:           int   = 3,
        reconfirm_bars:        int   = 3,
        min_hold_minutes:      int   = 30,
        gap_lookback_days:     int   = 50,
        gap_none_threshold:    float = 0.001,
        vol_lookback_days:     int   = 50,
        vol_bars_to_track:     int   = 20,
        slippage:              float = 0.01,
    ):
        self.symbol                = symbol
        self.opening_range_minutes = opening_range_minutes
        self.rr_ratio              = rr_ratio
        self.max_daily_loss        = max_daily_loss
        self.breakout_bars         = breakout_bars
        self.retest_bars           = retest_bars
        self.reconfirm_bars        = reconfirm_bars
        self.min_hold_minutes      = min_hold_minutes
        self.slippage              = slippage

        self._range_builder = RangeBuilder(
            opening_range_minutes = opening_range_minutes,
            max_window_multiplier = max_window_multiplier,
            min_range_pct         = min_range_pct,
            rolling_lookback_days = rolling_lookback_days,
            min_bootstrap_days    = min_bootstrap_days,
        )

        self._gap_detector = GapDetector(
            rolling_lookback_days = gap_lookback_days,
            min_bootstrap_days    = min_bootstrap_days,
            none_threshold        = gap_none_threshold,
        )

        self._volume_evaluator = VolumeEvaluator(
            rolling_lookback_days = vol_lookback_days,
            min_bootstrap_days    = min_bootstrap_days,
            bars_to_track         = vol_bars_to_track,
        )

        # RetestEngine is reset on every new day in _reset_day()
        self._retest_engine: RetestEngine | None = None

        self._current_date = None
        self.state         = self._make_day_state()

    # -----------------------------------------------------------------------
    # Abstract interface
    # -----------------------------------------------------------------------

    @abstractmethod
    def _make_day_state(self) -> ORBDayState:
        """Return a fresh per-day state. Must include all ORBDayState fields."""

    @abstractmethod
    def _on_new_day(self, bar_date: date) -> None:
        """Called once at day start after state is reset."""

    @abstractmethod
    def _on_range_set(self, result: RangeResult) -> None:
        """
        Called once when a valid range is established.
        result contains high, low, width, window_minutes, used_prior_day.
        """

    @abstractmethod
    def _on_entry(
        self,
        direction:  str,
        bar_close:  float,
        bar_date:   date,
        bar_time:   time,
    ) -> Any:
        """Signal fired — build and return an entry order."""

    @abstractmethod
    def _on_exit(
        self,
        reason:     str,
        exit_price: float,
        bar_date:   date,
        bar_time:   time,
    ) -> Any:
        """Exit condition met — build and return a closing order."""

    # -----------------------------------------------------------------------
    # Shared properties
    # -----------------------------------------------------------------------

    @property
    def has_position(self) -> bool:
        return self.state.position != 0

    @property
    def daily_pnl(self) -> float:
        return self.state.daily_pnl

    # -----------------------------------------------------------------------
    # Main entry point — FINAL, do not override
    # -----------------------------------------------------------------------

    def on_tick(self, record) -> Any:
        """
        Process one ohlcv-1s record. Returns an order or None.
        All ORB variants share this exact dispatch — do not override.
        """
        bar_open  = record.open  / 1e9
        bar_high  = record.high  / 1e9
        bar_low   = record.low   / 1e9
        bar_close = record.close / 1e9

        ts_et    = ns_to_et(record.ts_event)
        bar_time = ts_et.time()
        bar_date = ts_et.date()

        if bar_date != self._current_date:
            self._reset_day(bar_date)

        # Feed gap detector on every bar — it manages its own day boundary
        # and computes the signal once on the first bar at market open
        gap_signal = self._gap_detector.on_bar(
            bar_date  = bar_date,
            bar_time  = bar_time,
            bar_open  = bar_open,
            bar_close = bar_close,
            bar_high  = bar_high,
            bar_low   = bar_low,
        )
        if gap_signal is not None and gap_signal.is_new:
            self.state.gap_signal = gap_signal

        # Feed volume evaluator — updates rolling state on every bar
        self._volume_evaluator.on_bar(
            bar_date   = bar_date,
            bar_time   = bar_time,
            bar_volume = record.volume,
        )

        if bar_time < MARKET_OPEN or bar_time >= MARKET_CLOSE:
            return None

        if self.daily_pnl <= -abs(self.max_daily_loss):
            return None

        # ---- range building (delegates to RangeBuilder) ----
        if not self.state.range_set:
            return self._build_range(
                bar_date, bar_time, bar_high, bar_low, bar_close
            )

        # ---- skip day if range was invalid ----
        if self.state.range_skipped:
            return None

        # ---- EOD close ----
        if bar_time >= MARKET_CLOSE and self.has_position:
            return self._eod_close(bar_close, bar_date, bar_time)

        # ---- manage open position ----
        if self.has_position:
            return self._manage_position(
                bar_high, bar_low, bar_close, bar_date, bar_time
            )

        # ---- watch for breakout ----
        return self._check_breakout(bar_close, bar_date, bar_time)

    # -----------------------------------------------------------------------
    # Final shared logic — all price decisions live here
    # -----------------------------------------------------------------------

    def _build_range(
        self,
        bar_date:  date,
        bar_time:  time,
        bar_high:  float,
        bar_low:   float,
        bar_close: float,
    ) -> None:
        """Feed bar to RangeBuilder. Arm state when a valid range is returned."""
        result = self._range_builder.on_bar(
            bar_date, bar_time, bar_high, bar_low, bar_close
        )
        if result is None:
            return None

        # Range is resolved (valid or skipped)
        self.state.range_set     = True
        self.state.range_high    = result.high
        self.state.range_low     = result.low
        self.state.range_width   = result.width
        self.state.range_skipped = result.skipped

        if not result.skipped:
            # Initialise a fresh RetestEngine for this day's range
            self._retest_engine = RetestEngine(
                breakout_bars  = self.breakout_bars,
                retest_bars    = self.retest_bars,
                reconfirm_bars = self.reconfirm_bars,
            )
            self._retest_engine.set_range(result.high, result.low)
            self._on_range_set(result)

        return None

    def _check_breakout(
        self, bar_close: float, bar_date: date, bar_time: time,
    ) -> Any:
        """
        Delegate to RetestEngine. Handles full breakout → retest → entry
        state machine for both LONG and SHORT directions independently.
        One position per day; once trade_fired is True this is a no-op.
        """
        if self.state.trade_fired:
            return None

        if self._retest_engine is None:
            return None

        # Latest entry cutoff
        cutoff_seconds = (
            MARKET_CLOSE.hour * 3600 + MARKET_CLOSE.minute * 60
            - self.min_hold_minutes * 60
            - self.reconfirm_bars
        )
        bar_seconds = (
            bar_time.hour * 3600
            + bar_time.minute * 60
            + bar_time.second
        )
        if bar_seconds >= cutoff_seconds:
            return None

        # Feed bar to engine — get list of events
        events = self._retest_engine.on_bar(bar_close)

        # Update state range boundaries in case of expansion
        self.state.range_high  = self._retest_engine.range_high
        self.state.range_low   = self._retest_engine.range_low
        self.state.range_width = self.state.range_high - self.state.range_low

        for result in events:
            if result.event == "ENTRY":
                direction = result.direction
                self.state.trade_fired = True
                self.state.direction   = direction
                self.state.position    = 1

                # Apply slippage to entry fill price
                if direction == "LONG":
                    fill_price = bar_close + self.slippage
                    self.state.entry_price  = fill_price
                    self.state.stop_price   = self.state.range_low
                    self.state.target_price = (
                        fill_price + self.rr_ratio * self.state.range_width
                    )
                else:
                    fill_price = bar_close - self.slippage
                    self.state.entry_price  = fill_price
                    self.state.stop_price   = self.state.range_high
                    self.state.target_price = (
                        fill_price - self.rr_ratio * self.state.range_width
                    )

                logger.info(
                    f"  ENTRY {direction} @ {fill_price:.4f} "
                    f"(close={bar_close:.4f} slip={self.slippage:.4f}) | "
                    f"stop={self.state.stop_price:.4f} | "
                    f"target={self.state.target_price:.4f}"
                )

                # Evaluate volume at moment of entry
                self.state.volume_signal = self._volume_evaluator.evaluate(
                    confirm_bars = self.reconfirm_bars,
                    trade_date   = bar_date,
                )

                return self._on_entry(direction, bar_close, bar_date, bar_time)

        return None

    def _manage_position(
        self,
        bar_high:  float,
        bar_low:   float,
        bar_close: float,
        bar_date:  date,
        bar_time:  time,
    ) -> Any:
        """
        Check stop and target against underlying price.

        Bar sequence assumption: infer whether high or low came first
        within the bar based on where close landed.
          close_position >= 0.5 → close near high → high came first
          close_position <  0.5 → close near low  → low came first

        For LONG:  high first → check target before stop
        For SHORT: low first  → check target before stop
        """
        direction = self.state.direction
        bar_range = bar_high - bar_low

        # Infer intrabar sequence
        if bar_range > 0:
            close_pos  = (bar_close - bar_low) / bar_range
            high_first = close_pos >= 0.5
        else:
            high_first = True   # flat bar — order doesn't matter

        if direction == "LONG":
            stop_hit   = bar_low  <= self.state.stop_price
            target_hit = bar_high >= self.state.target_price

            if high_first:
                # target checked first
                if target_hit:
                    return self._trigger_exit(
                        "Take profit", self.state.target_price, bar_date, bar_time
                    )
                if stop_hit:
                    return self._trigger_exit(
                        "Stop loss", self.state.stop_price, bar_date, bar_time
                    )
            else:
                # stop checked first
                if stop_hit:
                    return self._trigger_exit(
                        "Stop loss", self.state.stop_price, bar_date, bar_time
                    )
                if target_hit:
                    return self._trigger_exit(
                        "Take profit", self.state.target_price, bar_date, bar_time
                    )

        else:  # SHORT
            stop_hit   = bar_high >= self.state.stop_price
            target_hit = bar_low  <= self.state.target_price

            if not high_first:
                # low came first — check target before stop
                if target_hit:
                    return self._trigger_exit(
                        "Take profit", self.state.target_price, bar_date, bar_time
                    )
                if stop_hit:
                    return self._trigger_exit(
                        "Stop loss", self.state.stop_price, bar_date, bar_time
                    )
            else:
                # high came first — check stop before target
                if stop_hit:
                    return self._trigger_exit(
                        "Stop loss", self.state.stop_price, bar_date, bar_time
                    )
                if target_hit:
                    return self._trigger_exit(
                        "Take profit", self.state.target_price, bar_date, bar_time
                    )

        return None

    def _eod_close(
        self, bar_close: float, bar_date: date, bar_time: time,
    ) -> Any:
        return self._trigger_exit("EOD flatten", bar_close, bar_date, bar_time)

    def _trigger_exit(
        self, reason: str, exit_price: float,
        bar_date: date, bar_time: time,
    ) -> Any:
        # Apply slippage to exit fill — adverse to the position
        if self.state.direction == "LONG":
            fill_price   = exit_price - self.slippage
            pnl_per_unit = fill_price - self.state.entry_price
        else:
            fill_price   = exit_price + self.slippage
            pnl_per_unit = self.state.entry_price - fill_price

        self.state.daily_pnl += pnl_per_unit * abs(self.state.position)

        logger.info(
            f"  EXIT [{reason}] {self.state.direction} @ {fill_price:.4f} "
            f"(level={exit_price:.4f} slip={self.slippage:.4f}) | "
            f"entry={self.state.entry_price:.4f} | "
            f"daily P&L=${self.state.daily_pnl:+.2f}"
        )
        exit_price = fill_price   # pass slippage-adjusted price to subclass

        order = self._on_exit(reason, exit_price, bar_date, bar_time)
        self.state.position = 0
        return order

    # -----------------------------------------------------------------------
    # Day reset
    # -----------------------------------------------------------------------

    def _reset_day(self, bar_date: date) -> None:
        if self._current_date is not None:
            logger.info(
                f"[{self._current_date}] Day closed | "
                f"P&L: ${self.daily_pnl:+.2f}"
            )
        self._current_date  = bar_date
        self.state          = self._make_day_state()
        self._retest_engine = None   # fresh engine created when range is set
        self._on_new_day(bar_date)
