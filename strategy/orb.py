"""
strategy/orb.py — Equity (shares) ORB strategy.

Inherits all opening range logic from ORBBase.
This file contains only what is equity-specific:
  - Entry: market buy on bullish breakout
  - Stop : range low (underlying price)
  - Target: entry + rr_ratio × range width
  - Exit : market sell
  - Short side is logged but skipped (long-only)
"""

from dataclasses import dataclass
from datetime import time, date

from strategy.orb_base import ORBBase
from utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Order dataclass
# ---------------------------------------------------------------------------

@dataclass
class Order:
    symbol:      str
    side:        str            # "BUY" or "SELL"
    quantity:    int
    order_type:  str            # "MARKET" or "LIMIT"
    limit_price: float | None = None
    reason:      str = ""


# ---------------------------------------------------------------------------
# Per-day state
# ---------------------------------------------------------------------------

@dataclass
class EquityDayState:
    # Opening range (managed by ORBBase)
    range_high:     float = 0.0
    range_low:      float = float("inf")
    range_set:      bool  = False
    range_width:    float = 0.0
    # Equity position
    breakout_fired: bool  = False
    position:       int   = 0       # shares held (0 = flat)
    entry_price:    float = 0.0
    stop_price:     float = 0.0
    target_price:   float = 0.0
    daily_pnl:      float = 0.0


# ---------------------------------------------------------------------------
# Equity ORB strategy
# ---------------------------------------------------------------------------

class ORBStrategy(ORBBase):
    """
    ORB strategy that trades shares.

    Args:
        symbol                : underlying ticker
        quantity              : shares per order
        opening_range_minutes : length of the opening range window
        rr_ratio              : take-profit = entry + rr_ratio × range width
        max_daily_loss        : halt trading if day P&L drops below this
    """

    def __init__(
        self,
        symbol:                str   = "QQQ",
        quantity:              int   = 10,
        opening_range_minutes: int   = 15,
        rr_ratio:              float = 2.0,
        max_daily_loss:        float = 500.0,
    ):
        self.quantity = quantity
        self.rr_ratio = rr_ratio
        super().__init__(
            symbol                = symbol,
            opening_range_minutes = opening_range_minutes,
            max_daily_loss        = max_daily_loss,
        )

    # -----------------------------------------------------------------------
    # ORBBase hooks
    # -----------------------------------------------------------------------

    def _make_day_state(self) -> EquityDayState:
        return EquityDayState()

    def _on_new_day(self, bar_date: date) -> None:
        logger.info(
            f"[{bar_date}] New day | "
            f"ORB window: {self.opening_range_minutes} min | "
            f"RR: {self.rr_ratio}×"
        )

    def _on_range_set(self) -> None:
        logger.info(
            f"  Range set: "
            f"high={self.state.range_high:.4f}  "
            f"low={self.state.range_low:.4f}  "
            f"width={self.state.range_width:.4f}"
        )

    def _check_breakout(
        self, bar_close: float, bar_high: float, bar_low: float,
        bar_date: date, bar_time: time,
    ) -> Order | None:
        if self.state.breakout_fired:
            return None

        # Bullish breakout
        if bar_close > self.state.range_high:
            self.state.breakout_fired = True
            self.state.position       = self.quantity
            self.state.entry_price    = bar_close
            self.state.stop_price     = self.state.range_low
            self.state.target_price   = bar_close + self.rr_ratio * self.state.range_width

            logger.info(
                f"  BREAKOUT LONG @ {bar_close:.4f} | "
                f"stop={self.state.stop_price:.4f} | "
                f"target={self.state.target_price:.4f}"
            )
            return Order(
                symbol     = self.symbol,
                side       = "BUY",
                quantity   = self.quantity,
                order_type = "MARKET",
                reason     = "ORB bullish breakout",
            )

        # Bearish breakout — long-only, just flag and skip
        if bar_close < self.state.range_low:
            self.state.breakout_fired = True
            logger.info(f"  BREAKOUT SHORT @ {bar_close:.4f} — long-only, skipping.")

        return None

    def _manage_position(
        self, bar_close: float, bar_high: float, bar_low: float,
        bar_date: date, bar_time: time,
    ) -> Order | None:
        # Stop hit
        if bar_low <= self.state.stop_price:
            pnl = (self.state.stop_price - self.state.entry_price) * self.state.position
            self.state.daily_pnl += pnl
            logger.info(f"  Stop hit. Trade P&L: ${pnl:+.2f}")
            return self._close("Stop loss", self.state.stop_price)

        # Target hit
        if bar_high >= self.state.target_price:
            pnl = (self.state.target_price - self.state.entry_price) * self.state.position
            self.state.daily_pnl += pnl
            logger.info(f"  Target hit. Trade P&L: ${pnl:+.2f}")
            return self._close("Take profit", self.state.target_price)

        return None

    def _on_eod_close(self, bar_close: float, bar_time: time) -> Order:
        pnl = (bar_close - self.state.entry_price) * self.state.position
        self.state.daily_pnl += pnl
        logger.info(f"  EOD flatten. Trade P&L: ${pnl:+.2f}")
        return self._close("EOD flatten", bar_close)

    # -----------------------------------------------------------------------
    # Internal helper
    # -----------------------------------------------------------------------

    def _close(self, reason: str, price: float) -> Order:
        logger.info(f"  Closing position: {reason} @ {price:.4f}")
        self.state.position = 0
        return Order(
            symbol     = self.symbol,
            side       = "SELL",
            quantity   = self.quantity,
            order_type = "MARKET",
            reason     = reason,
        )
