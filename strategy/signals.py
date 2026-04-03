"""
strategy/signals.py — Signal logic and risk management.

Starter strategy: a simple bid/ask spread-based signal.
Replace the generate_signal() method with your own logic.
"""

from dataclasses import dataclass
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class Order:
    symbol: str
    side: str           # "BUY" or "SELL"
    quantity: int
    order_type: str     # "MARKET" or "LIMIT"
    limit_price: float | None = None


class SignalEngine:
    def __init__(self, max_position_size: int, max_daily_loss: float):
        self.max_position_size = max_position_size
        self.max_daily_loss = max_daily_loss
        self.daily_pnl = 0.0
        self.position = 0           # Net position (+ long, - short)

    def on_tick(self, record) -> Order | None:
        """
        Called on every incoming tick. Returns an Order if a trade should fire,
        or None to do nothing.
        """
        signal = self.generate_signal(record)
        if signal is None:
            return None

        if not self._passes_risk_checks(signal):
            return None

        return signal

    def generate_signal(self, record) -> Order | None:
        """
        Starter signal: buy when bid/ask spread is unusually tight.
        Replace this with your actual strategy.

        record fields for schema="mbp-1":
            record.bid_px_00  — best bid price (fixed-point, divide by 1e9)
            record.ask_px_00  — best ask price
            record.bid_sz_00  — best bid size
            record.ask_sz_00  — best ask size
            record.instrument_id — numeric instrument ID
        """
        try:
            bid = record.bid_px_00 / 1e9
            ask = record.ask_px_00 / 1e9
            spread = ask - bid
        except AttributeError:
            # Record type doesn't have bid/ask (e.g. a heartbeat)
            return None

        symbol = "AAPL"             # TODO: map instrument_id → symbol
        spread_threshold = 0.02     # Example: trade when spread < 2 cents

        if spread < spread_threshold and self.position <= 0:
            return Order(
                symbol=symbol,
                side="BUY",
                quantity=min(10, self.max_position_size),
                order_type="LIMIT",
                limit_price=round(bid + 0.01, 2),
            )

        return None

    def _passes_risk_checks(self, order: Order) -> bool:
        """Basic pre-trade risk guardrails."""
        if self.daily_pnl <= -abs(self.max_daily_loss):
            logger.warning("Daily loss limit hit — no new orders.")
            return False

        if abs(self.position + order.quantity) > self.max_position_size:
            logger.warning("Order would exceed max position size — skipped.")
            return False

        return True

    def update_pnl(self, pnl_delta: float):
        """Call this after a fill to track daily P&L."""
        self.daily_pnl += pnl_delta
        logger.info(f"Daily P&L updated: ${self.daily_pnl:.2f}")
