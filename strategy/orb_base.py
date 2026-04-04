"""
strategy/orb_base.py — Abstract base class for all ORB variants.

Owns everything that is identical between equity and options ORB:
  - OHLCV record parsing (fixed-point → float)
  - Day boundary detection and state reset
  - Market hours gate (before 09:30 and after 15:45 ET → ignore)
  - Daily loss limit check
  - Opening range construction (high/low accumulation + range-set event)
  - Top-level on_tick() dispatch to the four abstract phase methods

Subclasses must implement:
  _make_day_state()   → return a fresh per-day state object
  _on_new_day()       → called once at the start of each new trading day
  _on_range_set()     → called once when the opening range window closes
  _check_breakout()   → called each bar after range is set, no position
  _manage_position()  → called each bar while a position is open
  _on_eod_close()     → called when bar_time >= MARKET_CLOSE with open position

The base class never imports broker or order types — those belong to the
subclass. on_tick() returns whatever the subclass methods return, typed
as Any so subclasses can return Order, dict, or None freely.
"""

from abc import ABC, abstractmethod
from datetime import time, date
from typing import Any

from strategy.utils import ns_to_et, add_minutes, MARKET_OPEN, MARKET_CLOSE
from utils.logger import get_logger

logger = get_logger(__name__)


class ORBBase(ABC):
    """
    Abstract base for Opening Range Breakout strategies.

    Args:
        symbol                : underlying ticker symbol
        opening_range_minutes : how many minutes after 09:30 to build the range
        max_daily_loss        : stop trading for the day if cumulative loss hits this
    """

    def __init__(
        self,
        symbol:                str   = "QQQ",
        opening_range_minutes: int   = 15,
        max_daily_loss:        float = 1000.0,
    ):
        self.symbol                = symbol
        self.opening_range_minutes = opening_range_minutes
        self.max_daily_loss        = max_daily_loss

        self._current_date = None
        self.state         = self._make_day_state()

    # -----------------------------------------------------------------------
    # Abstract interface — subclasses fill these in
    # -----------------------------------------------------------------------

    @abstractmethod
    def _make_day_state(self):
        """Return a fresh per-day state dataclass instance."""

    @abstractmethod
    def _on_new_day(self, bar_date: date) -> None:
        """
        Called once at the start of each new trading day, after state is reset.
        Use for subclass-specific logging or initialisation.
        """

    @abstractmethod
    def _on_range_set(self) -> None:
        """
        Called once when the opening range window has just closed.
        self.state.range_high, range_low, and range_width are already set.
        Use for subclass-specific logging.
        """

    @abstractmethod
    def _check_breakout(
        self, bar_close: float, bar_high: float, bar_low: float,
        bar_date: date, bar_time: time,
    ) -> Any:
        """
        Called every bar after the range is set and no position is open.
        Return an order/dict to trade, or None to do nothing.
        """

    @abstractmethod
    def _manage_position(
        self, bar_close: float, bar_high: float, bar_low: float,
        bar_date: date, bar_time: time,
    ) -> Any:
        """
        Called every bar while a position is open.
        Return an order/dict to exit/adjust, or None to hold.
        """

    @abstractmethod
    def _on_eod_close(
        self, bar_close: float, bar_time: time,
    ) -> Any:
        """
        Called when bar_time >= MARKET_CLOSE and a position is still open.
        Should return a closing order.
        """

    # -----------------------------------------------------------------------
    # Shared property — subclasses read this to check if a position is open
    # -----------------------------------------------------------------------

    @property
    def has_position(self) -> bool:
        """True if the strategy currently holds a position."""
        return getattr(self.state, "position", 0) != 0

    @property
    def daily_pnl(self) -> float:
        """Cumulative P&L for the current trading day."""
        return getattr(self.state, "daily_pnl", 0.0)

    # -----------------------------------------------------------------------
    # Main entry point — do not override in subclasses
    # -----------------------------------------------------------------------

    def on_tick(self, record) -> Any:
        """
        Process one ohlcv-1s record. Returns an order or None.

        This method is final — all ORB variants share this exact dispatch
        logic. To change ORB behaviour, override one of the abstract methods.

        Record fields (Databento ohlcv-1s, prices are fixed-point × 1e9):
            record.ts_event   nanosecond UTC timestamp
            record.open       open  price × 1e9
            record.high       high  price × 1e9
            record.low        low   price × 1e9
            record.close      close price × 1e9
            record.volume     bar volume
        """
        bar_open  = record.open  / 1e9   # noqa: F841 — available for subclasses via record
        bar_high  = record.high  / 1e9
        bar_low   = record.low   / 1e9
        bar_close = record.close / 1e9

        ts_et    = ns_to_et(record.ts_event)
        bar_time = ts_et.time()
        bar_date = ts_et.date()

        # ---- day boundary ----
        if bar_date != self._current_date:
            self._reset_day(bar_date)

        # ---- outside market hours ----
        if bar_time < MARKET_OPEN or bar_time >= MARKET_CLOSE:
            return None

        # ---- daily loss limit ----
        if self.daily_pnl <= -abs(self.max_daily_loss):
            return None

        # ---- phase 1: build opening range ----
        if not self.state.range_set:
            return self._build_opening_range(bar_time, bar_high, bar_low)

        # ---- EOD close ----
        if bar_time >= MARKET_CLOSE and self.has_position:
            return self._on_eod_close(bar_close, bar_time)

        # ---- phase 2: manage open position ----
        if self.has_position:
            return self._manage_position(bar_close, bar_high, bar_low, bar_date, bar_time)

        # ---- phase 3: watch for breakout ----
        return self._check_breakout(bar_close, bar_high, bar_low, bar_date, bar_time)

    # -----------------------------------------------------------------------
    # Internal shared logic
    # -----------------------------------------------------------------------

    def _reset_day(self, bar_date: date) -> None:
        """Reset per-day state and call the subclass new-day hook."""
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
        """
        Accumulate the high/low during the opening range window.
        When the window closes, sets range_set=True and calls _on_range_set().
        Always returns None — range building never produces an order.
        """
        range_end = add_minutes(MARKET_OPEN, self.opening_range_minutes)

        if bar_time < range_end:
            self.state.range_high = max(self.state.range_high, bar_high)
            self.state.range_low  = min(self.state.range_low,  bar_low)
            return None

        # Window just closed — finalise and notify subclass
        self.state.range_set   = True
        self.state.range_width = self.state.range_high - self.state.range_low
        self._on_range_set()
        return None
