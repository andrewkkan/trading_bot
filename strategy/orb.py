"""
strategy/orb.py — Opening Range Breakout (ORB) strategy.

Logic:
  - Market opens at 09:30 ET
  - First N minutes = "opening range" — track high and low
  - After opening range closes:
      - Price breaks above range high → BUY
      - Price breaks below range low  → SELL / exit long
  - Stop loss  : other side of the opening range
  - Take profit: configurable risk/reward multiplier (default 2x range width)
  - End of day  : flatten all positions at 15:45 ET (15 min before close)

Usage:
    engine = ORBStrategy(opening_range_minutes=15, rr_ratio=2.0)
    for record in store:
        order = engine.on_tick(record)
        if order:
            broker.place_order(order)
"""

from dataclasses import dataclass, field
from datetime import time, timezone, timedelta
from zoneinfo import ZoneInfo
from utils.logger import get_logger

logger = get_logger(__name__)

ET = ZoneInfo("America/New_York")

MARKET_OPEN  = time(9, 30)
MARKET_CLOSE = time(15, 45)   # flatten positions 15 min before close


@dataclass
class Order:
    symbol: str
    side: str                       # "BUY" or "SELL"
    quantity: int
    order_type: str                 # "MARKET" or "LIMIT"
    limit_price: float | None = None
    reason: str = ""                # for logging


@dataclass
class ORBState:
    """Holds the opening range state for one trading day."""
    range_high: float = 0.0
    range_low: float = float("inf")
    range_set: bool = False         # True once opening range period is over
    range_width: float = 0.0
    breakout_fired: bool = False    # only one entry per day
    position: int = 0               # +quantity = long, 0 = flat
    entry_price: float = 0.0
    stop_price: float = 0.0
    target_price: float = 0.0
    daily_pnl: float = 0.0


class ORBStrategy:
    def __init__(
        self,
        symbol: str = "QQQ",
        quantity: int = 10,
        opening_range_minutes: int = 15,
        rr_ratio: float = 2.0,          # take profit = rr_ratio × range width
        max_daily_loss: float = 500.0,
    ):
        self.symbol = symbol
        self.quantity = quantity
        self.opening_range_minutes = opening_range_minutes
        self.rr_ratio = rr_ratio
        self.max_daily_loss = max_daily_loss

        self.state = ORBState()
        self._current_date = None       # track day boundaries

    # ------------------------------------------------------------------
    # Main entry point — called on every 1-second bar
    # ------------------------------------------------------------------

    def on_tick(self, record) -> Order | None:
        """
        Process one OHLCV-1s record. Returns an Order or None.

        record fields (prices are fixed-point — divide by 1e9):
            record.ts_event   nanosecond UTC timestamp
            record.open       open price × 1e9
            record.high       high price × 1e9
            record.low        low price × 1e9
            record.close      close price × 1e9
            record.volume     volume
        """
        # Convert prices from fixed-point to float dollars
        bar_open   = record.open  / 1e9
        bar_high   = record.high  / 1e9
        bar_low    = record.low   / 1e9
        bar_close  = record.close / 1e9

        # Timestamp in ET
        ts_ns = record.ts_event
        ts_et = _ns_to_et(ts_ns)
        bar_time = ts_et.time()
        bar_date = ts_et.date()

        # ---- Day boundary: reset state at start of each new day ----
        if bar_date != self._current_date:
            if self._current_date is not None:
                logger.info(
                    f"[{self._current_date}] Day closed. "
                    f"Daily P&L: ${self.state.daily_pnl:.2f}"
                )
            self._current_date = bar_date
            self.state = ORBState()
            logger.info(f"[{bar_date}] New trading day. Opening range = {self.opening_range_minutes} min.")

        # ---- Outside market hours — ignore ----
        if bar_time < MARKET_OPEN or bar_time >= MARKET_CLOSE:
            return None

        # ---- Daily loss limit hit — no new trades ----
        if self.state.daily_pnl <= -abs(self.max_daily_loss):
            logger.warning("Daily loss limit hit — no new orders today.")
            return None

        # ---- Phase 1: Build the opening range ----
        if not self.state.range_set:
            return self._build_opening_range(bar_time, bar_high, bar_low)

        # ---- End of day: flatten before close ----
        if bar_time >= MARKET_CLOSE and self.state.position != 0:
            return self._flatten("End-of-day flatten", bar_close)

        # ---- Phase 2: Manage open position ----
        if self.state.position != 0:
            return self._manage_position(bar_high, bar_low, bar_close)

        # ---- Phase 3: Watch for breakout ----
        return self._check_breakout(bar_close)

    # ------------------------------------------------------------------
    # Phase 1 — build the opening range
    # ------------------------------------------------------------------

    def _build_opening_range(
        self, bar_time: time, bar_high: float, bar_low: float
    ) -> Order | None:
        range_end = _add_minutes(MARKET_OPEN, self.opening_range_minutes)

        # Still inside the opening range window — expand high/low
        if bar_time < range_end:
            self.state.range_high = max(self.state.range_high, bar_high)
            self.state.range_low  = min(self.state.range_low,  bar_low)
            return None

        # Opening range just closed — log it and arm the breakout watcher
        self.state.range_set   = True
        self.state.range_width = self.state.range_high - self.state.range_low

        logger.info(
            f"Opening range set: "
            f"high={self.state.range_high:.4f}  "
            f"low={self.state.range_low:.4f}  "
            f"width={self.state.range_width:.4f}"
        )
        return None

    # ------------------------------------------------------------------
    # Phase 2 — manage an open position (stop / target)
    # ------------------------------------------------------------------

    def _manage_position(
        self, bar_high: float, bar_low: float, bar_close: float
    ) -> Order | None:
        if self.state.position > 0:             # long
            # Stop hit
            if bar_low <= self.state.stop_price:
                pnl = (self.state.stop_price - self.state.entry_price) * self.state.position
                self.state.daily_pnl += pnl
                logger.info(f"Stop hit. P&L this trade: ${pnl:.2f}")
                return self._flatten("Stop loss", self.state.stop_price)

            # Target hit
            if bar_high >= self.state.target_price:
                pnl = (self.state.target_price - self.state.entry_price) * self.state.position
                self.state.daily_pnl += pnl
                logger.info(f"Target hit. P&L this trade: ${pnl:.2f}")
                return self._flatten("Take profit", self.state.target_price)

        return None

    # ------------------------------------------------------------------
    # Phase 3 — watch for breakout
    # ------------------------------------------------------------------

    def _check_breakout(self, bar_close: float) -> Order | None:
        if self.state.breakout_fired:
            return None

        # Bullish breakout — close above range high
        if bar_close > self.state.range_high:
            self.state.breakout_fired = True
            self.state.position       = self.quantity
            self.state.entry_price    = bar_close
            self.state.stop_price     = self.state.range_low
            self.state.target_price   = (
                bar_close + self.rr_ratio * self.state.range_width
            )

            logger.info(
                f"BREAKOUT LONG @ {bar_close:.4f} | "
                f"stop={self.state.stop_price:.4f} | "
                f"target={self.state.target_price:.4f}"
            )
            return Order(
                symbol=self.symbol,
                side="BUY",
                quantity=self.quantity,
                order_type="MARKET",
                reason="ORB bullish breakout",
            )

        # Bearish breakout — close below range low
        if bar_close < self.state.range_low:
            self.state.breakout_fired = True
            logger.info(
                f"BREAKOUT SHORT @ {bar_close:.4f} — long-only mode, skipping entry."
            )
            # Return None here if you're long-only.
            # To enable short selling, mirror the long logic above with side="SELL".

        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _flatten(self, reason: str, price: float) -> Order:
        logger.info(f"Flattening position: {reason} @ {price:.4f}")
        self.state.position = 0
        return Order(
            symbol=self.symbol,
            side="SELL",
            quantity=self.quantity,
            order_type="MARKET",
            reason=reason,
        )


# ------------------------------------------------------------------
# Utility functions
# ------------------------------------------------------------------

def _ns_to_et(ts_ns: int):
    """Convert a nanosecond UTC timestamp to an ET-aware datetime."""
    ts_s  = ts_ns / 1e9
    dt_utc = __import__("datetime").datetime.fromtimestamp(ts_s, tz=timezone.utc)
    return dt_utc.astimezone(ET)


def _add_minutes(t: time, minutes: int) -> time:
    """Add N minutes to a time object."""
    dt = __import__("datetime").datetime(2000, 1, 1, t.hour, t.minute, t.second)
    dt += timedelta(minutes=minutes)
    return dt.time()
