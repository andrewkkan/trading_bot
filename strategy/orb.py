"""
strategy/orb.py — Equity (shares) ORB execution.

All signal logic (breakout, stop, target, EOD, short) lives in ORBBase.
This file only translates signals into equity orders.
"""

from dataclasses import dataclass
from datetime import time, date

from strategy.orb_base import ORBBase, ORBDayState
from strategy.range_builder import RangeResult
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class Order:
    symbol:      str
    side:        str            # "BUY" or "SELL" or "SELL_SHORT" or "BUY_TO_COVER"
    quantity:    int
    order_type:  str            # "MARKET" or "LIMIT"
    limit_price: float | None = None
    reason:      str = ""


@dataclass
class EquityDayState(ORBDayState):
    """Extends ORBDayState with equity-specific execution fields."""
    quantity: int = 0           # shares filled at entry


class ORBStrategy(ORBBase):
    """
    ORB strategy that trades shares.

    Args:
        symbol                : underlying ticker
        quantity              : shares per order
        opening_range_minutes : length of opening range window
        rr_ratio              : take-profit = entry ± rr_ratio × range_width
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
        super().__init__(
            symbol                = symbol,
            opening_range_minutes = opening_range_minutes,
            rr_ratio              = rr_ratio,
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
            f"RR: {self.rr_ratio}× | qty: {self.quantity} shares"
        )

    def _on_range_set(self, result: RangeResult) -> None:
        prior = " (+ prior day)" if result.used_prior_day else ""
        logger.info(
            f"  Range set{prior}: "
            f"high={result.high:.4f}  "
            f"low={result.low:.4f}  "
            f"width={result.width:.4f}  "
            f"window={result.window_minutes}m"
        )

    def _on_entry(
        self, direction: str, bar_close: float,
        bar_date: date, bar_time: time,
    ) -> Order:
        # Record the actual share quantity on state so _trigger_exit P&L is correct
        self.state.position = self.quantity
        self.state.quantity = self.quantity

        side = "BUY" if direction == "LONG" else "SELL_SHORT"
        logger.info(f"  ORDER {side} {self.quantity} {self.symbol} @ MARKET")

        return Order(
            symbol     = self.symbol,
            side       = side,
            quantity   = self.quantity,
            order_type = "MARKET",
            reason     = f"ORB {direction} breakout",
        )

    def _on_exit(
        self, reason: str, exit_price: float,
        bar_date: date, bar_time: time,
    ) -> Order:
        side = "SELL" if self.state.direction == "LONG" else "BUY_TO_COVER"
        logger.info(f"  ORDER {side} {self.state.quantity} {self.symbol} @ MARKET | {reason}")

        return Order(
            symbol     = self.symbol,
            side       = side,
            quantity   = self.state.quantity,
            order_type = "MARKET",
            reason     = reason,
        )
