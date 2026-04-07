"""
strategy/orb_options.py — Options ORB execution.

All signal logic (breakout, stop, target, EOD, short) lives in ORBBase
and operates on the underlying price only.

This file only translates signals into option orders:
  LONG  breakout → buy a call
  SHORT breakout → buy a put
  Exit  (any)    → sell to close

Position sizing:
  contracts = floor(max_risk_per_trade / (ask × 100))
  Maximum loss on any trade = premium paid. No stop-loss slippage.

Pricing:
  Black-Scholes now. Swap _get_option_price() for real OPRA data later.
"""

from dataclasses import dataclass, field
from datetime import time, date

from strategy.orb_base import ORBBase, ORBDayState
from strategy.range_builder import RangeResult
from strategy.option_pricing import (
    price_option, get_iv_estimate, select_strike,
    days_to_nearest_expiry, OptionPrice,
)
from strategy.utils import get_expiry_date
from utils.logger import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Trade record
# ---------------------------------------------------------------------------

@dataclass
class TradeRecord:
    date:             date
    direction:        str     # "LONG" (call) or "SHORT" (put)
    option_type:      str     # "CALL" or "PUT"
    strike:           float
    expiry_dte:       int
    entry_time:       str
    entry_underlying: float
    entry_option:     float   # ask price paid per contract
    contracts:        int
    premium_paid:     float   # contracts × ask × 100
    exit_time:        str   = ""
    exit_underlying:  float = 0.0
    exit_option:      float = 0.0
    pnl:              float = 0.0
    exit_reason:      str   = ""
    entry_delta:      float = 0.0
    entry_iv:         float = 0.0


# ---------------------------------------------------------------------------
# Per-day state
# ---------------------------------------------------------------------------

@dataclass
class OptionsDayState(ORBDayState):
    """Extends ORBDayState with options execution fields."""
    option_type:      str   = ""      # "CALL" or "PUT"
    strike:           float = 0.0
    entry_option:     float = 0.0     # ask price at entry
    contracts:        int   = 0
    entry_time:       str   = ""
    entry_underlying: float = 0.0
    entry_delta:      float = 0.0
    entry_iv:         float = 0.0


# ---------------------------------------------------------------------------
# Options ORB strategy
# ---------------------------------------------------------------------------

class ORBOptionsStrategy(ORBBase):
    """
    ORB strategy that buys options on breakout signals.

    All breakout and exit decisions are made on the underlying price by
    ORBBase. This class only handles option selection, sizing, and order
    construction.

    Args:
        symbol                : underlying ticker
        opening_range_minutes : length of opening range window
        rr_ratio              : take-profit = entry ± rr_ratio × range_width
        target_dte            : days to expiry (0=0DTE, 1=next day, 7=weekly)
        strike_offset_pct     : how far OTM (0.0=ATM, 0.005=0.5% OTM)
        strike_interval       : round strike to nearest N dollars
        max_risk_per_trade    : max premium dollars per trade
        max_daily_loss        : halt trading if day P&L drops below this
        use_real_pricing      : True to use OPRA data (stub), False for B-S
        spread_pct            : estimated bid/ask half-spread (B-S mode only)
    """

    def __init__(
        self,
        symbol:                str   = "QQQ",
        opening_range_minutes: int   = 15,
        rr_ratio:              float = 2.0,
        target_dte:            int   = 1,
        strike_offset_pct:     float = 0.0,
        strike_interval:       float = 1.0,
        max_risk_per_trade:    float = 500.0,
        max_daily_loss:        float = 1000.0,
        max_window_multiplier: int   = 16,
        min_range_pct:         float = 0.5,
        rolling_lookback_days: int   = 50,
        min_bootstrap_days:    int   = 5,
        confirm_bars:          int   = 3,
        min_hold_minutes:      int   = 30,
        gap_lookback_days:     int   = 50,
        gap_none_threshold:    float = 0.001,
        use_real_pricing:      bool  = False,
        spread_pct:            float = 0.05,
    ):
        self.target_dte         = target_dte
        self.strike_offset_pct  = strike_offset_pct
        self.strike_interval    = strike_interval
        self.max_risk_per_trade = max_risk_per_trade
        self.use_real_pricing   = use_real_pricing
        self.spread_pct         = spread_pct
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
            confirm_bars          = confirm_bars,
            min_hold_minutes      = min_hold_minutes,
            gap_lookback_days     = gap_lookback_days,
            gap_none_threshold    = gap_none_threshold,
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
            f"RR: {self.rr_ratio}× | "
            f"DTE: {self.target_dte} | "
            f"Risk/trade: ${self.max_risk_per_trade:.0f}"
        )

    def _on_range_set(self, result: RangeResult) -> None:
        prior = " (+ prior day)" if result.used_prior_day else ""
        logger.info(
            f"  Range set{prior}: "
            f"high={result.high:.2f}  "
            f"low={result.low:.2f}  "
            f"width={result.width:.2f}  "
            f"window={result.window_minutes}m"
        )

    def _on_entry(
        self, direction: str, bar_close: float,
        bar_date: date, bar_time: time,
    ) -> dict | None:
        """
        Price and size the option. LONG = buy call, SHORT = buy put.
        Returns an order dict or None if sizing fails.
        """
        option_type = "CALL" if direction == "LONG" else "PUT"

        opt = self._get_option_price(bar_close, option_type, bar_date)
        if opt is None:
            return None

        cost_per_contract = opt.ask * 100.0
        if cost_per_contract <= 0:
            logger.warning("Option ask is zero — skipping.")
            return None

        contracts = int(self.max_risk_per_trade / cost_per_contract)
        if contracts < 1:
            logger.warning(
                f"Option too expensive for risk budget "
                f"(ask=${opt.ask:.2f}, budget=${self.max_risk_per_trade:.0f}) — skipping."
            )
            # Clear the base class position so we don't get stuck
            self.state.position = 0
            return None

        # Record execution details on state
        self.state.position         = contracts
        self.state.option_type      = option_type
        self.state.strike           = opt.strike
        self.state.entry_option     = opt.ask
        self.state.contracts        = contracts
        self.state.entry_time       = bar_time.strftime("%H:%M:%S")
        self.state.entry_underlying = bar_close
        self.state.entry_delta      = opt.delta
        self.state.entry_iv         = opt.iv

        premium_paid = contracts * cost_per_contract

        logger.info(
            f"  ORDER BUY_OPEN {contracts}x {option_type} "
            f"strike={opt.strike:.1f} ask=${opt.ask:.2f} | "
            f"delta={opt.delta:.3f} IV={opt.iv:.1%} | "
            f"risk=${premium_paid:.0f}"
        )

        return {
            "symbol":      self.symbol,
            "option_type": option_type,
            "strike":      opt.strike,
            "expiry_date": get_expiry_date(bar_date, self.target_dte),
            "side":        "BUY_OPEN",
            "contracts":   contracts,
            "limit_price": round(opt.ask + 0.05, 2),
            "reason":      f"ORB {direction} breakout",
        }

    def _on_exit(
        self, reason: str, exit_price: float,
        bar_date: date, bar_time: time,
    ) -> dict:
        """
        Re-price the option at the exit underlying price and record the trade.
        P&L is option-premium based (not underlying points).
        """
        opt = self._get_option_price(
            exit_price, self.state.option_type, bar_date
        )
        exit_option = opt.price if opt else self.state.entry_option * 0.1

        # Override base class underlying P&L with real option P&L
        option_pnl = (
            (exit_option - self.state.entry_option)
            * self.state.contracts * 100.0
        )
        # Correct the daily_pnl — base class added underlying points,
        # we replace that with actual option premium P&L
        underlying_pnl = self.state.daily_pnl  # already updated by base
        self.state.daily_pnl = (
            underlying_pnl
            - abs(self.state.position) * (
                (self.state.entry_price - exit_price)
                if self.state.direction == "LONG"
                else (exit_price - self.state.entry_price)
            )
            + option_pnl
        )

        logger.info(
            f"  ORDER SELL_CLOSE {self.state.contracts}x "
            f"{self.state.option_type} strike={self.state.strike:.1f} | "
            f"entry=${self.state.entry_option:.2f} exit=${exit_option:.2f} | "
            f"option P&L=${option_pnl:+.2f} | {reason}"
        )

        self.trades.append(TradeRecord(
            date             = self._current_date,
            direction        = self.state.direction,
            option_type      = self.state.option_type,
            strike           = self.state.strike,
            expiry_dte       = self.target_dte,
            entry_time       = self.state.entry_time,
            entry_underlying = self.state.entry_underlying,
            entry_option     = self.state.entry_option,
            contracts        = self.state.contracts,
            premium_paid     = self.state.contracts * self.state.entry_option * 100,
            exit_time        = bar_time.strftime("%H:%M:%S"),
            exit_underlying  = exit_price,
            exit_option      = exit_option,
            pnl              = round(option_pnl, 2),
            exit_reason      = reason,
            entry_delta      = self.state.entry_delta,
            entry_iv         = self.state.entry_iv,
        ))

        return {
            "symbol":      self.symbol,
            "option_type": self.state.option_type,
            "strike":      self.state.strike,
            "expiry_date": get_expiry_date(bar_date, self.target_dte),
            "side":        "SELL_CLOSE",
            "contracts":   self.state.contracts,
            "limit_price": round(exit_option - 0.05, 2),
            "reason":      reason,
        }

    # -----------------------------------------------------------------------
    # Option pricer — swap for real OPRA data when available
    # -----------------------------------------------------------------------

    def _get_option_price(
        self, spot: float, option_type: str, bar_date: date,
    ) -> OptionPrice | None:
        """
        Price the option at the given underlying spot price.

        To upgrade to real OPRA data:
          1. Set use_real_pricing=True in config
          2. Implement the lookup below — find the OPRA record matching
             (symbol, strike, expiry, option_type) at bar_date
          3. Return an OptionPrice built from real bid/ask/greeks
        """
        if self.use_real_pricing:
            raise NotImplementedError(
                "Real OPRA pricing not yet implemented. "
                "Set use_real_pricing=False to use Black-Scholes."
            )

        strike = select_strike(
            spot,
            offset_pct = self.strike_offset_pct if option_type == "CALL"
                         else -self.strike_offset_pct,
            interval   = self.strike_interval,
        )
        return price_option(
            S           = spot,
            K           = strike,
            T           = days_to_nearest_expiry(self.target_dte),
            sigma       = get_iv_estimate(bar_date.year),
            option_type = option_type,
            spread_pct  = self.spread_pct,
        )
