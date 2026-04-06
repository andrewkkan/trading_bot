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
  - Breakout detection   (both LONG and SHORT, N-bar confirmation, entry cutoff)
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
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import time, date
from typing import Any

from strategy.range_builder import RangeBuilder, RangeResult
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
    # Signal / position
    trade_fired:         bool  = False
    direction:           str   = ""      # "LONG" or "SHORT"
    position:            int   = 0       # non-zero = in a trade
    entry_price:         float = 0.0
    stop_price:          float = 0.0
    target_price:        float = 0.0
    daily_pnl:           float = 0.0
    # Breakout confirmation counters
    long_confirm_count:  int   = 0   # consecutive closes above range_high
    short_confirm_count: int   = 0   # consecutive closes below range_low


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
        confirm_bars          : consecutive closes required to confirm breakout (default 3)
        min_hold_minutes      : minimum minutes between entry and EOD close (default 30)
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
        confirm_bars:          int   = 3,
        min_hold_minutes:      int   = 30,
    ):
        self.symbol                = symbol
        self.opening_range_minutes = opening_range_minutes
        self.rr_ratio              = rr_ratio
        self.max_daily_loss        = max_daily_loss
        self.confirm_bars          = confirm_bars
        self.min_hold_minutes      = min_hold_minutes

        self._range_builder = RangeBuilder(
            opening_range_minutes = opening_range_minutes,
            max_window_multiplier = max_window_multiplier,
            min_range_pct         = min_range_pct,
            rolling_lookback_days = rolling_lookback_days,
            min_bootstrap_days    = min_bootstrap_days,
        )

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
        bar_high  = record.high  / 1e9
        bar_low   = record.low   / 1e9
        bar_close = record.close / 1e9

        ts_et    = ns_to_et(record.ts_event)
        bar_time = ts_et.time()
        bar_date = ts_et.date()

        if bar_date != self._current_date:
            self._reset_day(bar_date)

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
            self._on_range_set(result)

        return None

    def _check_breakout(
        self, bar_close: float, bar_date: date, bar_time: time,
    ) -> Any:
        """
        Detect breakout with N-consecutive-bar confirmation.

        Counters are independent — a cross to the opposite side resets
        the active counter to 0 and starts the other at 1.
        One trade per day; once trade_fired is True this method is a no-op.
        """
        if self.state.trade_fired:
            return None

        # Latest entry cutoff — don't start a new trade if there isn't
        # enough time left for a meaningful hold before EOD close.
        # Cutoff = MARKET_CLOSE - min_hold_minutes - confirm_bars seconds
        # (confirm_bars seconds account for the time still needed to confirm)
        cutoff_seconds = (
            MARKET_CLOSE.hour * 3600 + MARKET_CLOSE.minute * 60
            - self.min_hold_minutes * 60
            - self.confirm_bars
        )
        bar_seconds = bar_time.hour * 3600 + bar_time.minute * 60 + bar_time.second
        if bar_seconds >= cutoff_seconds:
            return None

        above = bar_close > self.state.range_high
        below = bar_close < self.state.range_low

        # Update counters — crossing to the other side resets both
        if above:
            self.state.long_confirm_count  += 1
            self.state.short_confirm_count  = 0
        elif below:
            self.state.short_confirm_count += 1
            self.state.long_confirm_count   = 0
        else:
            # Bar closed back inside the range — reset both
            self.state.long_confirm_count  = 0
            self.state.short_confirm_count = 0
            return None

        # Check if either direction has reached the confirmation threshold
        if self.state.long_confirm_count >= self.confirm_bars:
            direction = "LONG"
        elif self.state.short_confirm_count >= self.confirm_bars:
            direction = "SHORT"
        else:
            # Still accumulating — log progress on first and subsequent bars
            count = self.state.long_confirm_count if above else self.state.short_confirm_count
            side  = "LONG" if above else "SHORT"
            logger.debug(
                f"  Confirmation {count}/{self.confirm_bars} {side} "
                f"@ {bar_close:.4f}"
            )
            return None

        # Confirmation achieved — arm the trade
        self.state.trade_fired  = True
        self.state.direction    = direction
        self.state.position     = 1
        self.state.entry_price  = bar_close

        if direction == "LONG":
            self.state.stop_price   = self.state.range_low
            self.state.target_price = (
                bar_close + self.rr_ratio * self.state.range_width
            )
        else:
            self.state.stop_price   = self.state.range_high
            self.state.target_price = (
                bar_close - self.rr_ratio * self.state.range_width
            )

        logger.info(
            f"  BREAKOUT CONFIRMED {direction} "
            f"({self.confirm_bars} consecutive bars) "
            f"@ {bar_close:.4f} | "
            f"stop={self.state.stop_price:.4f} | "
            f"target={self.state.target_price:.4f}"
        )

        return self._on_entry(direction, bar_close, bar_date, bar_time)

    def _manage_position(
        self,
        bar_high:  float,
        bar_low:   float,
        bar_close: float,
        bar_date:  date,
        bar_time:  time,
    ) -> Any:
        """Check stop and target against underlying price."""
        direction = self.state.direction

        if direction == "LONG":
            if bar_low <= self.state.stop_price:
                return self._trigger_exit(
                    "Stop loss", self.state.stop_price, bar_date, bar_time
                )
            if bar_high >= self.state.target_price:
                return self._trigger_exit(
                    "Take profit", self.state.target_price, bar_date, bar_time
                )
        else:  # SHORT
            if bar_high >= self.state.stop_price:
                return self._trigger_exit(
                    "Stop loss", self.state.stop_price, bar_date, bar_time
                )
            if bar_low <= self.state.target_price:
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
        if self.state.direction == "LONG":
            pnl_per_unit = exit_price - self.state.entry_price
        else:
            pnl_per_unit = self.state.entry_price - exit_price

        self.state.daily_pnl += pnl_per_unit * abs(self.state.position)

        logger.info(
            f"  EXIT [{reason}] {self.state.direction} @ {exit_price:.4f} | "
            f"entry={self.state.entry_price:.4f} | "
            f"daily P&L=${self.state.daily_pnl:+.2f}"
        )

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
        self._current_date = bar_date
        self.state         = self._make_day_state()
        self._on_new_day(bar_date)
