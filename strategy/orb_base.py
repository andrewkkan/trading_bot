"""
strategy/orb_base.py — Abstract base class for all ORB variants.

DESIGN PRINCIPLE
================
All trading decisions are made on the **underlying price** — always.
This includes breakout detection, stop loss, take profit, and EOD close.
Subclasses (equity, options) only decide *how to execute* once a signal
fires — they never contain price-level logic.

What the base class owns (final — do not override):
  - OHLCV parsing
  - Day boundary reset
  - Market hours gate
  - Daily loss limit
  - Opening range construction
  - Breakout detection   (both long AND short)
  - Position management  (stop/target on underlying price)
  - EOD flatten

What subclasses implement (execution only):
  _make_day_state()             → fresh per-day state dataclass
  _on_new_day(bar_date)         → subclass logging at day start
  _on_range_set()               → subclass logging when range closes
  _on_entry(signal, bar_close,  → build and return an entry order
            bar_date, bar_time)
  _on_exit(reason, exit_price,  → build and return a closing order
           bar_date, bar_time)

Signal model
============
direction = "LONG"  : underlying closed above range high
direction = "SHORT" : underlying closed below range low

Stop loss  : the other side of the opening range
             LONG  stop = range_low
             SHORT stop = range_high

Take profit: entry price ± rr_ratio × range_width
             LONG  target = entry + rr_ratio × range_width
             SHORT target = entry - rr_ratio × range_width

Stop/target are checked against bar_low / bar_high respectively so
a bar that gaps through the level is still caught.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import time, date
from typing import Any

from strategy.utils import ns_to_et, add_minutes, MARKET_OPEN, MARKET_CLOSE
from utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Shared per-day state — all price-level fields live here
# ---------------------------------------------------------------------------

@dataclass
class ORBDayState:
    """
    Core per-day state managed entirely by ORBBase.
    Subclasses extend this with their own execution fields.
    """
    # Opening range
    range_high:     float = 0.0
    range_low:      float = float("inf")
    range_set:      bool  = False
    range_width:    float = 0.0
    # Signal / position
    trade_fired:    bool  = False    # one entry per day
    direction:      str   = ""       # "LONG" or "SHORT"
    position:       int   = 0        # non-zero = in a trade
    entry_price:    float = 0.0      # underlying price at entry
    stop_price:     float = 0.0      # underlying stop level
    target_price:   float = 0.0      # underlying target level
    daily_pnl:      float = 0.0      # running P&L in dollars


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class ORBBase(ABC):
    """
    Abstract base for all Opening Range Breakout strategies.

    Args:
        symbol                : underlying ticker
        opening_range_minutes : minutes after open used to build the range
        rr_ratio              : take-profit distance = rr_ratio × range_width
        max_daily_loss        : halt trading if cumulative day loss exceeds this
    """

    def __init__(
        self,
        symbol:                str   = "QQQ",
        opening_range_minutes: int   = 15,
        rr_ratio:              float = 2.0,
        max_daily_loss:        float = 1000.0,
    ):
        self.symbol                = symbol
        self.opening_range_minutes = opening_range_minutes
        self.rr_ratio              = rr_ratio
        self.max_daily_loss        = max_daily_loss

        self._current_date = None
        self.state         = self._make_day_state()

    # -----------------------------------------------------------------------
    # Abstract interface — subclasses implement these
    # -----------------------------------------------------------------------

    @abstractmethod
    def _make_day_state(self) -> ORBDayState:
        """
        Return a fresh per-day state object.
        Must be a dataclass that includes all fields from ORBDayState
        (either by inheriting it or by composition).
        """

    @abstractmethod
    def _on_new_day(self, bar_date: date) -> None:
        """Called once at day start after state is reset. Use for logging."""

    @abstractmethod
    def _on_range_set(self) -> None:
        """Called once when the opening range window closes. Use for logging."""

    @abstractmethod
    def _on_entry(
        self,
        direction:  str,    # "LONG" or "SHORT"
        bar_close:  float,  # underlying entry price
        bar_date:   date,
        bar_time:   time,
    ) -> Any:
        """
        Called when a breakout signal fires. Build and return an entry order.
        The base class has already recorded entry_price, stop_price,
        target_price, direction, and position on self.state before calling this.
        """

    @abstractmethod
    def _on_exit(
        self,
        reason:     str,    # "Stop loss", "Take profit", "EOD flatten"
        exit_price: float,  # underlying exit price
        bar_date:   date,
        bar_time:   time,
    ) -> Any:
        """
        Called when an exit condition is met. Build and return a closing order.
        The base class will set self.state.position = 0 after this returns.
        """

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

        Record fields (Databento ohlcv-1s, prices fixed-point × 1e9):
            record.ts_event, record.open, record.high, record.low,
            record.close, record.volume
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

        if not self.state.range_set:
            return self._build_opening_range(bar_time, bar_high, bar_low)

        if bar_time >= MARKET_CLOSE and self.has_position:
            return self._eod_close(bar_close, bar_date, bar_time)

        if self.has_position:
            return self._manage_position(bar_high, bar_low, bar_close, bar_date, bar_time)

        return self._check_breakout(bar_close, bar_date, bar_time)

    # -----------------------------------------------------------------------
    # Final shared logic — price decisions live here
    # -----------------------------------------------------------------------

    def _check_breakout(
        self, bar_close: float, bar_date: date, bar_time: time,
    ) -> Any:
        """
        Detect a breakout and fire an entry signal.
        Both long and short directions are handled identically.
        Only one trade fires per day.
        """
        if self.state.trade_fired:
            return None

        if bar_close > self.state.range_high:
            direction = "LONG"
        elif bar_close < self.state.range_low:
            direction = "SHORT"
        else:
            return None

        # Set all price levels on state before calling subclass
        self.state.trade_fired  = True
        self.state.direction    = direction
        self.state.position     = 1          # subclass may adjust to qty/contracts
        self.state.entry_price  = bar_close

        if direction == "LONG":
            self.state.stop_price   = self.state.range_low
            self.state.target_price = bar_close + self.rr_ratio * self.state.range_width
        else:
            self.state.stop_price   = self.state.range_high
            self.state.target_price = bar_close - self.rr_ratio * self.state.range_width

        logger.info(
            f"  BREAKOUT {direction} @ {bar_close:.4f} | "
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
        """
        Check stop and target levels against the underlying price.
        Uses bar_low for long stops, bar_high for short stops,
        so gaps through the level are always caught.
        """
        direction = self.state.direction

        if direction == "LONG":
            if bar_low <= self.state.stop_price:
                return self._trigger_exit("Stop loss", self.state.stop_price, bar_date, bar_time)
            if bar_high >= self.state.target_price:
                return self._trigger_exit("Take profit", self.state.target_price, bar_date, bar_time)

        elif direction == "SHORT":
            if bar_high >= self.state.stop_price:
                return self._trigger_exit("Stop loss", self.state.stop_price, bar_date, bar_time)
            if bar_low <= self.state.target_price:
                return self._trigger_exit("Take profit", self.state.target_price, bar_date, bar_time)

        return None

    def _eod_close(
        self, bar_close: float, bar_date: date, bar_time: time,
    ) -> Any:
        """Flatten any open position at end of day."""
        return self._trigger_exit("EOD flatten", bar_close, bar_date, bar_time)

    def _trigger_exit(
        self, reason: str, exit_price: float, bar_date: date, bar_time: time,
    ) -> Any:
        """
        Update P&L, log the exit, call the subclass _on_exit hook,
        then clear the position.
        """
        if self.state.direction == "LONG":
            pnl_per_unit = exit_price - self.state.entry_price
        else:
            pnl_per_unit = self.state.entry_price - exit_price

        # Raw underlying P&L — subclass _on_exit may record its own
        # instrument-specific P&L (e.g. option premium) on top of this
        self.state.daily_pnl += pnl_per_unit * abs(self.state.position)

        logger.info(
            f"  EXIT [{reason}] "
            f"{self.state.direction} @ {exit_price:.4f} | "
            f"entry={self.state.entry_price:.4f} | "
            f"daily P&L=${self.state.daily_pnl:+.2f}"
        )

        order = self._on_exit(reason, exit_price, bar_date, bar_time)
        self.state.position = 0
        return order

    # -----------------------------------------------------------------------
    # Internal helpers
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

    def _build_opening_range(
        self, bar_time: time, bar_high: float, bar_low: float,
    ) -> None:
        range_end = add_minutes(MARKET_OPEN, self.opening_range_minutes)

        if bar_time < range_end:
            self.state.range_high = max(self.state.range_high, bar_high)
            self.state.range_low  = min(self.state.range_low,  bar_low)
            return None

        self.state.range_set   = True
        self.state.range_width = self.state.range_high - self.state.range_low
        self._on_range_set()
        return None
