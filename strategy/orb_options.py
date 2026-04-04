"""
strategy/orb_options.py — Options ORB strategy.

Inherits all opening range logic from ORBBase.
This file contains only what is options-specific:
  - Entry : buy a call (bullish) or put (bearish), sized by premium risk budget
  - Stop  : option value drops to stop_loss_pct of entry premium
  - Target: option value rises to target_mult × entry premium
  - Exit  : sell to close
  - Pricing: Black-Scholes now; real OPRA data via use_real_pricing=True hook

Upgrade path:
    Replace _get_option_price() with a real OPRA lookup once you have
    pulled historical options data from Databento. Everything else stays.
"""

from dataclasses import dataclass
from datetime import time, date

from strategy.orb_base import ORBBase
from strategy.option_pricing import (
    price_option, get_iv_estimate, select_strike,
    days_to_nearest_expiry, OptionPrice,
)
from strategy.utils import get_expiry_date
from utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Trade record — one per completed round-trip
# ---------------------------------------------------------------------------

@dataclass
class TradeRecord:
    date:             date
    direction:        str     # "CALL" or "PUT"
    strike:           float
    expiry_dte:       int
    entry_time:       str
    entry_underlying: float   # QQQ price at entry
    entry_option:     float   # ask price paid per contract
    contracts:        int
    premium_paid:     float   # total cash out = contracts × ask × 100
    exit_time:        str   = ""
    exit_option:      float = 0.0
    pnl:              float = 0.0
    exit_reason:      str   = ""
    entry_delta:      float = 0.0
    entry_iv:         float = 0.0


# ---------------------------------------------------------------------------
# Per-day state
# ---------------------------------------------------------------------------

@dataclass
class OptionsDayState:
    # Opening range (managed by ORBBase)
    range_high:       float = 0.0
    range_low:        float = float("inf")
    range_set:        bool  = False
    range_width:      float = 0.0
    # Options position
    trade_fired:      bool  = False
    position:         int   = 0       # contracts held (0 = flat)
    direction:        str   = ""      # "CALL" or "PUT"
    strike:           float = 0.0
    entry_price:      float = 0.0     # option ask at entry
    stop_price:       float = 0.0
    target_price:     float = 0.0
    entry_time:       str   = ""
    entry_underlying: float = 0.0
    entry_delta:      float = 0.0
    entry_iv:         float = 0.0
    daily_pnl:        float = 0.0


# ---------------------------------------------------------------------------
# Options ORB strategy
# ---------------------------------------------------------------------------

class ORBOptionsStrategy(ORBBase):
    """
    ORB strategy that buys options instead of shares.

    Args:
        symbol                : underlying ticker
        opening_range_minutes : length of the opening range window
        target_dte            : days to expiry  (0=0DTE, 1=next day, 7=weekly)
        strike_offset_pct     : how far OTM  (0.0=ATM, 0.005=0.5% OTM)
        strike_interval       : round strike to nearest N dollars
        max_risk_per_trade    : max premium dollars per trade
        stop_loss_pct         : exit if option loses this fraction  (0.5 = 50%)
        target_mult           : exit if option gains this multiple  (2.0 = 2×)
        max_daily_loss        : halt trading if day P&L drops below this
        use_real_pricing      : True to use OPRA data (stub), False for B-S
        spread_pct            : estimated bid/ask half-spread (B-S mode only)
    """

    def __init__(
        self,
        symbol:                str   = "QQQ",
        opening_range_minutes: int   = 15,
        target_dte:            int   = 1,
        strike_offset_pct:     float = 0.0,
        strike_interval:       float = 1.0,
        max_risk_per_trade:    float = 500.0,
        stop_loss_pct:         float = 0.50,
        target_mult:           float = 2.0,
        max_daily_loss:        float = 1000.0,
        use_real_pricing:      bool  = False,
        spread_pct:            float = 0.05,
    ):
        self.target_dte         = target_dte
        self.strike_offset_pct  = strike_offset_pct
        self.strike_interval    = strike_interval
        self.max_risk_per_trade = max_risk_per_trade
        self.stop_loss_pct      = stop_loss_pct
        self.target_mult        = target_mult
        self.use_real_pricing   = use_real_pricing
        self.spread_pct         = spread_pct
        self.trades: list[TradeRecord] = []

        super().__init__(
            symbol                = symbol,
            opening_range_minutes = opening_range_minutes,
            max_daily_loss        = max_daily_loss,
        )

    # -----------------------------------------------------------------------
    # ORBBase hooks
    # -----------------------------------------------------------------------

    def _make_day_state(self) -> OptionsDayState:
        return OptionsDayState()

    def _on_new_day(self, bar_date: date) -> None:
        logger.info(
            f"[{bar_date}] New day | "
            f"ORB window: {self.opening_range_minutes} min | "
            f"DTE: {self.target_dte} | "
            f"Risk/trade: ${self.max_risk_per_trade:.0f}"
        )

    def _on_range_set(self) -> None:
        logger.info(
            f"  Range set: "
            f"high={self.state.range_high:.2f}  "
            f"low={self.state.range_low:.2f}  "
            f"width={self.state.range_width:.2f}"
        )

    def _check_breakout(
        self, bar_close: float, bar_high: float, bar_low: float,
        bar_date: date, bar_time: time,
    ) -> dict | None:
        if self.state.trade_fired:
            return None

        direction = None
        if bar_close > self.state.range_high:
            direction = "CALL"
        elif bar_close < self.state.range_low:
            direction = "PUT"

        if direction is None:
            return None

        opt = self._get_option_price(bar_close, direction, bar_date)
        if opt is None:
            return None

        cost_per_contract = opt.ask * 100.0
        if cost_per_contract <= 0:
            logger.warning("Option ask is zero — skipping trade.")
            return None

        contracts = int(self.max_risk_per_trade / cost_per_contract)
        if contracts < 1:
            logger.warning(
                f"Option too expensive for risk budget "
                f"(ask=${opt.ask:.2f}, budget=${self.max_risk_per_trade:.0f}) — skipping."
            )
            return None

        premium_paid = contracts * cost_per_contract
        stop_price   = opt.ask * (1.0 - self.stop_loss_pct)
        target_price = opt.ask * self.target_mult

        self.state.trade_fired      = True
        self.state.position         = contracts
        self.state.direction        = direction
        self.state.strike           = opt.strike
        self.state.entry_price      = opt.ask
        self.state.stop_price       = stop_price
        self.state.target_price     = target_price
        self.state.entry_time       = bar_time.strftime("%H:%M:%S")
        self.state.entry_underlying = bar_close
        self.state.entry_delta      = opt.delta
        self.state.entry_iv         = opt.iv

        logger.info(
            f"  BREAKOUT {direction} @ {bar_close:.2f} | "
            f"strike={opt.strike:.1f}  ask=${opt.ask:.2f}  "
            f"delta={opt.delta:.3f}  IV={opt.iv:.1%} | "
            f"contracts={contracts}  risk=${premium_paid:.0f} | "
            f"stop=${stop_price:.2f}  target=${target_price:.2f}"
        )

        return {
            "symbol":      self.symbol,
            "option_type": direction,
            "strike":      opt.strike,
            "expiry_date": get_expiry_date(bar_date, self.target_dte),
            "side":        "BUY_OPEN",
            "contracts":   contracts,
            "limit_price": round(opt.ask + 0.05, 2),
            "reason":      f"ORB {direction} breakout",
        }

    def _manage_position(
        self, bar_close: float, bar_high: float, bar_low: float,
        bar_date: date, bar_time: time,
    ) -> dict | None:
        opt = self._get_option_price(bar_close, self.state.direction, bar_date)
        if opt is None:
            return None

        if opt.price <= self.state.stop_price:
            return self._close(bar_close, bar_time, "Stop loss", opt.price)

        if opt.price >= self.state.target_price:
            return self._close(bar_close, bar_time, "Take profit", opt.price)

        return None

    def _on_eod_close(self, bar_close: float, bar_time: time) -> dict:
        opt = self._get_option_price(bar_close, self.state.direction, self._current_date)
        exit_price = opt.price if opt else self.state.entry_price * 0.5
        return self._close(bar_close, bar_time, "EOD flatten", exit_price)

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _close(
        self, underlying: float, bar_time: time,
        reason: str, exit_option_price: float,
    ) -> dict:
        pnl = (
            (exit_option_price - self.state.entry_price)
            * self.state.position * 100.0
        )
        self.state.daily_pnl += pnl

        logger.info(
            f"  EXIT [{reason}] "
            f"entry=${self.state.entry_price:.2f}  "
            f"exit=${exit_option_price:.2f}  "
            f"contracts={self.state.position}  "
            f"P&L=${pnl:+.2f}  daily=${self.state.daily_pnl:+.2f}"
        )

        self.trades.append(TradeRecord(
            date             = self._current_date,
            direction        = self.state.direction,
            strike           = self.state.strike,
            expiry_dte       = self.target_dte,
            entry_time       = self.state.entry_time,
            entry_underlying = self.state.entry_underlying,
            entry_option     = self.state.entry_price,
            contracts        = self.state.position,
            premium_paid     = self.state.position * self.state.entry_price * 100,
            exit_time        = bar_time.strftime("%H:%M:%S"),
            exit_option      = exit_option_price,
            pnl              = round(pnl, 2),
            exit_reason      = reason,
            entry_delta      = self.state.entry_delta,
            entry_iv         = self.state.entry_iv,
        ))

        self.state.position = 0
        return {
            "symbol":      self.symbol,
            "option_type": self.state.direction,
            "strike":      self.state.strike,
            "expiry_date": get_expiry_date(self._current_date, self.target_dte),
            "side":        "SELL_CLOSE",
            "contracts":   self.state.position,
            "limit_price": round(exit_option_price - 0.05, 2),
            "reason":      reason,
        }

    def _get_option_price(
        self, spot: float, direction: str, bar_date: date,
    ) -> OptionPrice | None:
        """
        Price the target option. Currently uses Black-Scholes.

        To upgrade to real OPRA data:
          1. Set use_real_pricing=True in config
          2. Implement the lookup below — find the contract matching
             (symbol, strike, expiry, direction) at timestamp bar_date
          3. Return an OptionPrice built from real bid/ask/greeks
        """
        if self.use_real_pricing:
            raise NotImplementedError(
                "Real OPRA pricing not yet implemented. "
                "Set use_real_pricing=False to use Black-Scholes estimates."
            )

        strike = select_strike(
            spot,
            offset_pct = self.strike_offset_pct if direction == "CALL"
                         else -self.strike_offset_pct,
            interval   = self.strike_interval,
        )
        return price_option(
            S           = spot,
            K           = strike,
            T           = days_to_nearest_expiry(self.target_dte),
            sigma       = get_iv_estimate(bar_date.year),
            option_type = direction,
            spread_pct  = self.spread_pct,
        )
