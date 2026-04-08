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
class TradeRecord:
    """One completed round-trip trade — entry to exit."""
    date:            date
    direction:       str     # "LONG" or "SHORT"
    entry_time:      str
    exit_time:       str
    entry_price:     float   # underlying price at entry
    exit_price:      float   # underlying price at exit
    quantity:        int     # shares
    pnl:             float   # realised P&L in dollars
    exit_reason:     str     # "Stop loss" | "Take profit" | "EOD flatten"
    range_high:      float
    range_low:       float
    range_width:     float
    gap_direction:   str     # from GapSignal: "UP" | "DOWN" | "NONE" | "N/A"
    gap_pct:         float
    vol_rel:         float   # confirm_rel_vol from VolumeSignal (0.0 if no history)


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
    ):
        self.quantity = quantity
        self.trades: list[TradeRecord] = []
        super().__init__(
            symbol                = symbol,
            opening_range_minutes = opening_range_minutes,
            rr_ratio              = rr_ratio,
            max_daily_loss        = max_daily_loss,
            max_window_multiplier = max_window_multiplier,
            min_range_pct         = min_range_pct,
            rolling_lookback_days = rolling_lookback_days,
            min_bootstrap_days    = min_bootstrap_days,
            breakout_bars         = breakout_bars,
            retest_bars           = retest_bars,
            reconfirm_bars        = reconfirm_bars,
            min_hold_minutes      = min_hold_minutes,
            gap_lookback_days     = gap_lookback_days,
            gap_none_threshold    = gap_none_threshold,
            vol_lookback_days     = vol_lookback_days,
            vol_bars_to_track     = vol_bars_to_track,
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
        self.state.position   = self.quantity
        self.state.quantity   = self.quantity
        self._entry_time      = bar_time.strftime("%H:%M:%S")
        self._entry_date      = bar_date

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
        # P&L already computed by ORBBase._trigger_exit
        pnl_per_unit = (
            exit_price - self.state.entry_price
            if self.state.direction == "LONG"
            else self.state.entry_price - exit_price
        )
        pnl = round(pnl_per_unit * self.state.quantity, 2)

        # Pull signal annotations if available
        gap   = self.state.gap_signal
        vol   = self.state.volume_signal

        self.trades.append(TradeRecord(
            date          = self._entry_date,
            direction     = self.state.direction,
            entry_time    = self._entry_time,
            exit_time     = bar_time.strftime("%H:%M:%S"),
            entry_price   = self.state.entry_price,
            exit_price    = exit_price,
            quantity      = self.state.quantity,
            pnl           = pnl,
            exit_reason   = reason,
            range_high    = self.state.range_high,
            range_low     = self.state.range_low,
            range_width   = self.state.range_width,
            gap_direction = gap.direction  if gap else "N/A",
            gap_pct       = gap.gap_pct    if gap else 0.0,
            vol_rel       = vol.confirm_rel_vol if vol else 0.0,
        ))

        side = "SELL" if self.state.direction == "LONG" else "BUY_TO_COVER"
        logger.info(
            f"  ORDER {side} {self.state.quantity} {self.symbol} @ MARKET | "
            f"{reason} | P&L ${pnl:+.2f}"
        )

        return Order(
            symbol     = self.symbol,
            side       = side,
            quantity   = self.state.quantity,
            order_type = "MARKET",
            reason     = reason,
        )
